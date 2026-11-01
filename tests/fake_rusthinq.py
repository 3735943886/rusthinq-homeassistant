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
        self._runner: web.AppRunner | None = None
        self.url = ""

    def add_device(self, device: str, publications: list[dict[str, Any]], *, incarnation: int = 1,
                   generation: int = 100, script_generation: int = 1, online: bool = True) -> None:
        self.devices[device] = {
            "online": online,
            "incarnation": str(incarnation),
            "generation": str(generation) if online else None,
            "scriptGeneration": str(script_generation) if online else None,
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
        return {"devices": self.devices, "version": "0.2.0-rc8", "management": True}

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
        if not all(isinstance(body.get(k), str) for k in ("incarnation", "generation", "script_generation")):
            return web.json_response({"error": "numbers as strings"}, status=400)
        if not self._current(device, body["incarnation"], body["generation"], body["script_generation"]):
            return web.json_response({"error": "StaleSession"}, status=409)
        return web.json_response({"status": "accepted", "sequence": str(len(self.invokes))}, status=202)
