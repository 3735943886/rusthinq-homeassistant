"""A rusthinq management API on localhost, with the behavior the client relies on (rusthinq `management.rs`):
a snapshot first on `/api/events`, session-scoped `presentation` and `invoke` that answer 409 for a stale session."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import WSMsgType, web


class FakeRusthinq:
    def __init__(self) -> None:
        self.devices: dict[str, dict[str, Any]] = {}
        self.publications: dict[str, list[dict[str, Any]]] = {}
        self.invokes: list[tuple[str, dict[str, Any]]] = []
        self.presentation_gate: asyncio.Event | None = None
        """When set to an unset Event, presentations wait for it (to interleave live events)."""
        self.sockets: list[web.WebSocketResponse] = []
        self.connections = 0
        self.device_reads = 0
        self.account: dict[str, Any] = {"busy": False, "country": None, "error": None, "loggedIn": False,
                                        "stored": False}
        self.cloud_calls: list[tuple[str, dict[str, Any]]] = []
        self.cloud_failure: tuple[int, str] | None = None
        """When set, the next LG account, bridge or invoke call answers with this status and error."""
        self.backup: bytes | None = None
        """What `/api/backup` serves; None answers like a daemon older than 0.2.0 with its dashboard on."""
        self.backup_refusal: str | None = None
        """When set, `/api/backup` answers 403 with this (an unauthenticated, non-loopback management bind)."""
        self.forgotten: list[tuple[str, dict[str, Any]]] = []
        self._runner: web.AppRunner | None = None
        self.url = ""

    def add_device(self, device: str, publications: list[dict[str, Any]], *, incarnation: int = 1,
                   generation: int = 100, script_generation: int = 1, online: bool = True, mapped: bool = True,
                   **fields: Any) -> None:
        """A device as `/api/devices` lists it; `fields` override the status fields (names as rusthinq's)."""
        self.devices[device] = {
            "online": online,
            "incarnation": str(incarnation),
            "generation": str(generation) if online else None,
            "scriptGeneration": str(script_generation) if online and mapped else None,
            "mapped": online and mapped,
            "scriptFaulted": False,
            "name": None,
            "model": "CST_570004_WW",
            "modelName": "CST_570004_WW",
            "swVersion": "570004",
            "platform": "ThinQ2",
            "driver": {"file": "drivers/CST_570004_WW.rhai"} if mapped else None,
            "bridgeEnabled": True,
            "bridgePaired": True,
            "bridgePending": False,
            "bridged": True,
            "bridgeError": None,
            **fields,
        }
        self.publications[device] = publications

    def reconnect(self, device: str) -> None:
        """The appliance reconnected: a new generation, so earlier sessions are stale."""
        info = self.devices[device]
        info["generation"] = str(int(info["generation"]) + 1)

    def context(self, device: str) -> dict[str, Any]:
        info = self.devices[device]
        return {"device": device, "incarnation": info["incarnation"], "generation": info["generation"],
                "scriptGeneration": info["scriptGeneration"]}

    def state(self) -> dict[str, Any]:
        return {"devices": self.devices, "version": "0.2.1", "management": True,
                "features": {"bridge": True, "scripting": True}}

    async def output(self, device: str, topic: str, payload: str, retain: bool = True) -> None:
        """A driver publication, as rusthinq sends it on `/api/events`."""
        await self.send({"type": "scriptOutput", "context": self.context(device),
                         "payload": json.dumps({"topic": topic, "payload": payload, "retain": retain})})

    async def send(self, event: dict[str, Any]) -> None:
        for ws in list(self.sockets):
            await ws.send_str(json.dumps(event))

    async def drop_sockets(self) -> None:
        for ws in list(self.sockets):
            await ws.close()

    # ---- server --------------------------------------------------------------------------

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/events", self._events)
        app.router.add_get("/api/devices", self._devices)
        app.router.add_get("/api/devices/{id}/presentation", self._presentation)
        app.router.add_post("/api/devices/{id}/invoke", self._invoke)
        app.router.add_get("/api/cloud", self._cloud)
        app.router.add_post("/api/cloud/{action:login|login/complete|logout|refresh}", self._cloud_action)
        app.router.add_post("/api/devices/{id}/bridge/{action}", self._bridge)
        app.router.add_post("/api/devices/{id}/forget", self._forget)
        app.router.add_get("/api/diagnostics", self._diagnostics)
        app.router.add_get("/api/health", self._health)
        app.router.add_get("/api/backup", self._backup)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        await self.drop_sockets()
        if self._runner is not None:
            await self._runner.cleanup()

    async def _forget(self, request: web.Request) -> web.Response:
        device, body = request.match_info["id"], await request.json()
        self.forgotten.append((device, body))
        if (failure := self._failure()) is not None:
            return failure
        info = self.devices.get(device)
        if info is None or body.get("incarnation") != info["incarnation"]:
            return web.json_response({"error": "unknown device or stale incarnation"}, status=409)
        del self.devices[device]
        return web.json_response({"status": "accepted", "incarnation": body["incarnation"]}, status=202)

    async def _diagnostics(self, request: web.Request) -> web.Response:
        return web.json_response({"version": "0.2.1", "devices": self.devices,
                                  "privacy": "No credentials, certificates or wire payloads are included."})

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _backup(self, request: web.Request) -> web.Response:
        if self.backup_refusal is not None:
            return web.json_response({"error": self.backup_refusal}, status=403)
        if self.backup is None:
            return web.Response(text="<!doctype html>", content_type="text/html")
        return web.Response(body=self.backup, content_type="application/x-tar",
                            headers={"Content-Disposition": 'attachment; filename="rusthinq-backup-1.tar"'})

    def _current(self, device: str, incarnation: str, generation: str, script_generation: str) -> bool:
        info = self.devices.get(device)
        return bool(info and info["online"] and (info["incarnation"], info["generation"], info["scriptGeneration"])
                    == (incarnation, generation, script_generation))

    async def _events(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        self.sockets.append(ws)
        await ws.send_str(json.dumps({"type": "snapshot", "state": self.state()}))
        try:
            async for msg in ws:
                if msg.type is WSMsgType.ERROR:
                    break
        finally:
            self.sockets.remove(ws)
        return ws

    async def _devices(self, request: web.Request) -> web.Response:
        self.device_reads += 1
        return web.json_response(self.state())

    async def _presentation(self, request: web.Request) -> web.Response:
        device, q = request.match_info["id"], request.query
        if self.presentation_gate is not None:
            await self.presentation_gate.wait()
        if not self._current(device, q["incarnation"], q["generation"], q["script_generation"]):
            return web.json_response({"error": "device presentation scope changed"}, status=409)
        return web.json_response({"publications": self.publications[device]})

    async def _invoke(self, request: web.Request) -> web.Response:
        device, body = request.match_info["id"], await request.json()
        self.invokes.append((device, body))
        if (failure := self._failure()) is not None:
            return failure
        if not all(isinstance(body.get(k), str) for k in ("incarnation", "generation", "script_generation")):
            return web.json_response({"error": "numbers as strings"}, status=400)
        if not self._current(device, body["incarnation"], body["generation"], body["script_generation"]):
            return web.json_response({"error": "StaleSession"}, status=409)
        return web.json_response({"status": "accepted", "sequence": str(len(self.invokes))}, status=202)

    # ---- LG account and bridge (rusthinq `management.rs`, `api.rs` cloud_device) -------------

    def _failure(self) -> web.Response | None:
        if self.cloud_failure is None:
            return None
        status, message = self.cloud_failure
        self.cloud_failure = None
        return web.json_response({"error": message}, status=status)

    async def _cloud(self, request: web.Request) -> web.Response:
        return web.json_response({"account": self.account, "enabled": True, "inventory": []})

    async def _cloud_action(self, request: web.Request) -> web.Response:
        action, body = request.match_info["action"], await request.json()
        self.cloud_calls.append((action, body))
        if (failure := self._failure()) is not None:
            return failure
        if action == "login":
            self.account["country"] = body["country"]
            return web.json_response({"url": f"https://kr.m.lgaccount.com/login/signIn?country={body['country']}"})
        if action == "login/complete":
            self.account.update(loggedIn=True, stored=True)
        elif action == "logout":
            self.account.update(loggedIn=False, stored=False)
        return web.json_response(self.account)

    async def _bridge(self, request: web.Request) -> web.Response:
        device, action, body = request.match_info["id"], request.match_info["action"], await request.json()
        self.cloud_calls.append((f"bridge/{action}", {"device": device, **body}))
        if (failure := self._failure()) is not None:
            return failure
        info = self.devices[device]
        if body.get("incarnation") != info["incarnation"]:
            return web.json_response({"error": "stale"}, status=409)
        if action == "pair":
            info.update(bridgePaired=True, bridgeEnabled=False, bridged=False)
        elif action in ("enable", "disable"):
            info.update(bridgeEnabled=action == "enable", bridged=action == "enable")
        elif action == "unpair":
            info.update(bridgePaired=False, bridgeEnabled=False, bridged=False, bridgePending=False)
        return web.json_response({"device": device})
