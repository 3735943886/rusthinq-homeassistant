"""The rusthinq management API as an IL producer: daemon publications in, IL commands out.

rusthinq publishes what its drivers produce on the `/api/events` websocket (`scriptOutput`, whose payload is the
`{"topic", "payload", "retain"}` an MQTT broker would receive) and answers `/api/devices/{id}/presentation` with the
current publications of an online device. This client replays both into an IL transport, and turns the IL commands
consumers publish on `<prefix>/<device>/<prop>/set` into `invoke(__command, {"prop", "value"})`, the same call
rusthinq's own MQTT command path makes. Nothing here imports Home Assistant.

A descriptor's `label` is the name a person calls the device, the owner's own when the producer knows it (il.md). The
drivers do not know it, rusthinq does (`name` in `/api/devices`, from the LG account), so it replaces the label of each
descriptor passed on, and a descriptor is passed on again when that name appears or changes.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from custom_components.ildevice.core.transport import Message, Transport, Unsubscribe

_LOGGER = logging.getLogger(__name__)

SOURCE = "rusthinq"
"""The descriptor `source` of rusthinq devices, and so the presence topic's last level (il-mqtt.md M-12)."""

RECONNECT_BACKOFF = (1, 5, 15, 30, 60)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
STATUS_SETTLE = 1.0
"""Seconds to let a burst of connection events settle before re-reading the devices."""
STATUS_INTERVAL = 300
"""Seconds between device re-reads without events (bridge errors, for one, change without any)."""
STATUS_EVENTS = ("stateChanged", "scriptStopped", "metadata")
"""Events after which the devices' status may differ. `stateChanged` with `cloudBound` is a device's uplink, the
most frequent event, and changes no status."""


def basic_authorization(user: str, password: str) -> dict[str, str]:
    """The `Authorization` header for the management API's Basic auth (aiohttp's `BasicAuth` is deprecated in 3.14)."""
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class ManagementError(Exception):
    """The management API refused or failed a request; the message is its reason."""


@dataclass(frozen=True)
class Session:
    """What rusthinq needs to address a device's current connection and driver."""

    incarnation: str
    generation: str
    script_generation: str

    @classmethod
    def from_context(cls, context: dict[str, Any]) -> Session | None:
        try:
            return cls(str(context["incarnation"]), str(context["generation"]), str(context["scriptGeneration"]))
        except KeyError:
            return None

    @classmethod
    def from_device(cls, device: dict[str, Any]) -> Session | None:
        """The session of a device in a snapshot, if it is online with a driver running."""
        if not device.get("online") or device.get("generation") is None or device.get("scriptGeneration") is None:
            return None
        return cls(str(device["incarnation"]), str(device["generation"]), str(device["scriptGeneration"]))


class ManagementClient:
    def __init__(
        self,
        http: aiohttp.ClientSession,
        url: str,
        transport: Transport,
        prefix: str,
        *,
        headers: dict[str, str] | None = None,
        backoff: tuple[float, ...] = RECONNECT_BACKOFF,
    ) -> None:
        self._http = http
        self._url = url.rstrip("/")
        self._transport = transport
        self._prefix = prefix
        self._headers = headers or {}
        self._backoff = backoff
        self._sessions: dict[str, Session] = {}
        self._live: dict[str, set[str]] | None = None
        """Topics published live since the running resync started, which its older presentation must not overwrite."""
        self._resync: asyncio.Task | None = None
        self._runner: asyncio.Task | None = None
        self._commands: set[asyncio.Task] = set()
        self._unsubscribe: Unsubscribe | None = None
        self._status: asyncio.Task | None = None
        self._status_due = 0.0
        self._listeners: list[Callable[[], None]] = []
        self.connected = asyncio.Event()
        self.devices: dict[str, dict[str, Any]] = {}
        """Each device as `/api/devices` describes it (online, bridge, driver, firmware...), by id."""
        self.features: dict[str, Any] = {}
        self.account: dict[str, Any] = {}
        """The LG account as `/api/cloud` reports it (`loggedIn`, `country`, `error`, `busy`); empty without a bridge."""
        self._descriptors: dict[str, tuple[str, dict[str, Any], bool]] = {}
        """The last descriptor each device's driver published, as published: (topic, document, retain)."""
        self._labels: dict[str, str | None] = {}
        """The name each descriptor was passed on with (None: the driver's own label)."""

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` when `devices`, `features` or `connected` change; returns the function that removes it."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _changed(self) -> None:
        for listener in list(self._listeners):
            listener()

    @property
    def presence_topic(self) -> str:
        return f"{self._prefix}/_producer/{SOURCE}"

    async def start(self) -> None:
        await self._presence(False)
        self._unsubscribe = await self._transport.subscribe(f"{self._prefix}/+/+/set", self._on_command)
        self._runner = asyncio.create_task(self._run(), name="rusthinq management client")

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        tasks = [t for t in (self._runner, self._resync, self._status, *self._commands) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._runner = self._resync = self._status = None
        await self._presence(False)

    # ---- events --------------------------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                async with self._http.ws_connect(
                    f"{self._url}/api/events", headers=self._headers, heartbeat=30, max_msg_size=0
                ) as ws:
                    attempt = 0
                    async for msg in ws:
                        if msg.type is aiohttp.WSMsgType.TEXT:
                            await self._handle(json.loads(msg.data))
                        elif msg.type is aiohttp.WSMsgType.ERROR:
                            break
                _LOGGER.warning("rusthinq event stream at %s closed", self._url)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                _LOGGER.warning("rusthinq event stream at %s failed: %s", self._url, err)
            except Exception:  # the loop is the only thing that reconnects; nothing may end it but stop()
                _LOGGER.exception("rusthinq event stream at %s failed unexpectedly", self._url)
            await self._disconnected()
            await asyncio.sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
            attempt += 1

    async def _disconnected(self) -> None:
        if self._resync is not None:
            self._resync.cancel()
            self._resync = None
        self._live = None
        self._sessions.clear()
        if self._status is not None:
            self._status.cancel()
            self._status = None
        if self.connected.is_set():
            self.connected.clear()
            await self._presence(False)
            self._changed()

    async def _handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind in ("snapshot", "lost"):
            if kind == "lost":
                _LOGGER.warning("rusthinq dropped %s events; resynchronizing", event.get("events"))
            state = event.get("state") or {}
            if not self.connected.is_set():
                self.connected.set()
                await self._presence(True)
            await self._set_devices(state)
            self._start_resync(state)
            self._schedule_status(STATUS_SETTLE)  # the LG account is not in the snapshot
        elif kind in STATUS_EVENTS and "cloudBound" not in event:
            self._schedule_status(STATUS_SETTLE)
        elif kind == "scriptOutput":
            context = event.get("context") or {}
            if (device := context.get("device")) and (session := Session.from_context(context)):
                self._sessions[device] = session
            try:
                publication = json.loads(event["payload"])
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug("ignoring a script output that is not a publication: %s", event.get("payload"))
                return
            if self._live is not None and device:
                self._live.setdefault(device, set()).add(publication.get("topic"))
            await self._publish(publication)

    async def _set_devices(self, state: dict[str, Any]) -> None:
        self.devices = dict(state.get("devices") or {})
        self.features = dict(state.get("features") or {})
        self._changed()
        for device, (topic, document, retain) in list(self._descriptors.items()):
            if self._name(device) != self._labels.get(device):
                await self._publish({"topic": topic, "payload": document, "retain": retain})

    def _name(self, device: str) -> str | None:
        return (self.devices.get(device) or {}).get("name") or None

    def _descriptor_of(self, topic: str) -> str | None:
        """The device whose descriptor `topic` carries (`<prefix>/<id>` or `<prefix>/<id>/descriptor`), or None."""
        if not topic.startswith(f"{self._prefix}/"):
            return None
        parts = topic[len(self._prefix) + 1:].split("/")
        if len(parts) == 1 or (len(parts) == 2 and parts[1] == "descriptor"):
            return parts[0] if parts[0] and not parts[0].startswith("_") else None
        return None

    def descriptor(self, device: str) -> dict[str, Any] | None:
        """The device's descriptor as passed on (with rusthinq's name as its label), or as the transport retains it
        (restored from before a restart); None if it has none."""
        if (known := self._descriptors.get(device)) is not None:
            name = self._labels.get(device)
            return {**known[1], "label": name} if name else known[1]
        retained = getattr(self._transport, "retained", {})
        for topic in (f"{self._prefix}/{device}/descriptor", f"{self._prefix}/{device}"):
            if (msg := retained.get(topic)) is not None:
                try:
                    document = json.loads(msg.payload)
                except ValueError:
                    return None
                return document if isinstance(document, dict) else None
        return None

    def _schedule_status(self, delay: float) -> None:
        """Re-read the devices after `delay`, or sooner if already due; then again after `STATUS_INTERVAL`."""
        loop = asyncio.get_running_loop()
        due = loop.time() + delay
        if self._status is not None and not self._status.done():
            if self._status_due <= due:
                return
            self._status.cancel()
        self._status = asyncio.create_task(self._status_after(delay), name="rusthinq device status")
        self._status_due = due

    async def _status_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._read_devices()
        if self.connected.is_set():
            self._status = None
            self._schedule_status(STATUS_INTERVAL)

    async def _read_devices(self) -> dict[str, Any] | None:
        """`/api/devices`, which also refreshes `devices`, `features` and the sessions; None if it failed."""
        try:
            async with self._http.get(
                f"{self._url}/api/devices", headers=self._headers, timeout=REQUEST_TIMEOUT
            ) as response:
                response.raise_for_status()
                state = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("could not read rusthinq devices: %s", err)
            return None
        for device, info in (state.get("devices") or {}).items():
            if (session := Session.from_device(info)) is not None:
                self._sessions[device] = session
            else:
                self._sessions.pop(device, None)
        if (state.get("features") or {}).get("bridge"):
            self.account = await self._account()
        if self.connected.is_set():
            await self._set_devices(state)
        return state

    async def _account(self) -> dict[str, Any]:
        try:
            async with self._http.get(
                f"{self._url}/api/cloud", headers=self._headers, timeout=REQUEST_TIMEOUT
            ) as response:
                response.raise_for_status()
                return (await response.json()).get("account") or {}
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("could not read the LG account status: %s", err)
            return self.account

    # ---- LG account and bridge -----------------------------------------------------------

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._http.post(
                f"{self._url}{path}", json=body, headers=self._headers, timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                try:
                    answer = await response.json(content_type=None)
                except ValueError:
                    answer = {}
                if response.status >= 400:
                    raise ManagementError((answer or {}).get("error") or f"HTTP {response.status}")
                return answer if isinstance(answer, dict) else {}
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ManagementError(str(err)) from err
        finally:
            if self.connected.is_set():
                self._schedule_status(STATUS_SETTLE)

    async def cloud_login(self, country: str) -> str:
        """Start an LG sign-in; the URL to open in a browser."""
        url = (await self._post("/api/cloud/login", {"country": country})).get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ManagementError("LG returned no sign-in address")
        return url

    async def cloud_complete(self, url: str) -> None:
        """Finish the sign-in with the address the browser ended on (LG's `login/iabClose`)."""
        await self._post("/api/cloud/login/complete", {"url": url})

    async def cloud_logout(self) -> None:
        await self._post("/api/cloud/logout", {})

    async def cloud_refresh(self) -> None:
        await self._post("/api/cloud/refresh", {})

    async def bridge(self, device: str, action: str) -> None:
        """`pair`, `enable`, `disable` or `unpair` the LG bridge of a device, in its current incarnation. A pair
        registers it under rusthinq's name for it, when there is one."""
        info = self.devices.get(device) or {}
        if info.get("incarnation") is None:
            raise ManagementError(f"{device} is not known to rusthinq")
        body: dict[str, Any] = {"incarnation": str(info["incarnation"])}
        if action == "pair" and info.get("name"):
            body["alias"] = info["name"]
        await self._post(f"/api/devices/{device}/bridge/{action}", body)

    async def forget(self, device: str) -> None:
        """Forget a device in its current incarnation: rusthinq closes its session, deregisters it from LG when it is
        bridged and drops it from its ledger (202: admitted). An appliance that connects again comes back."""
        info = self.devices.get(device) or {}
        if info.get("incarnation") is None:
            raise ManagementError(f"{device} is not known to rusthinq")
        await self._post(f"/api/devices/{device}/forget", {"incarnation": str(info["incarnation"])})

    def retained_topics(self, device: str) -> list[str]:
        """The topics the transport retains for a device (its descriptor first)."""
        retained = getattr(self._transport, "retained", {})
        base = f"{self._prefix}/{device}"
        return sorted((t for t in retained if t == base or t.startswith(f"{base}/")),
                      key=lambda t: (self._descriptor_of(t) is None, t))

    def retained_values(self, device: str) -> dict[str, str]:
        """What the transport retains for a device, by topic."""
        retained = getattr(self._transport, "retained", {})
        return {topic: (payload.decode("utf-8", "replace") if isinstance(payload := retained[topic].payload, bytes)
                        else payload) for topic in self.retained_topics(device)}

    async def clear(self, device: str) -> None:
        """Clear what the transport retains for a device, as a broker's retained topics are cleared; the IL consumer
        then withdraws the device."""
        for topic in self.retained_topics(device):
            await self._transport.publish(topic, "", qos=1, retain=True)
        self._descriptors.pop(device, None)
        self._labels.pop(device, None)
        self._sessions.pop(device, None)

    async def diagnostics(self) -> dict[str, Any]:
        """`/api/diagnostics`: versions, runtime, devices and drivers; rusthinq leaves out credentials and payloads."""
        try:
            async with self._http.get(
                f"{self._url}/api/diagnostics", headers=self._headers, timeout=REQUEST_TIMEOUT
            ) as response:
                response.raise_for_status()
                answer = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ManagementError(str(err)) from err
        return answer if isinstance(answer, dict) else {}

    def _start_resync(self, state: dict[str, Any]) -> None:
        """Republish every online device's current publications, as a broker would replay retained ones."""
        sessions = {
            device: session
            for device, info in (state.get("devices") or {}).items()
            if (session := Session.from_device(info)) is not None
        }
        self._sessions = dict(sessions)
        if self._resync is not None:
            self._resync.cancel()
        self._live = {}
        self._resync = asyncio.create_task(self._resync_devices(sessions), name="rusthinq resync")

    async def _resync_devices(self, sessions: dict[str, Session]) -> None:
        try:
            for device, session in sessions.items():
                publications = await self._presentation(device, session)
                live = (self._live or {}).get(device, set())
                for publication in publications:
                    if publication.get("topic") not in live:
                        await self._publish(publication)
        finally:
            if self._resync is asyncio.current_task():
                self._resync = None
                self._live = None

    async def _presentation(self, device: str, session: Session) -> list[dict[str, Any]]:
        params = {
            "incarnation": session.incarnation,
            "generation": session.generation,
            "script_generation": session.script_generation,
        }
        try:
            async with self._http.get(
                f"{self._url}/api/devices/{device}/presentation", params=params, headers=self._headers,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status == 409:  # the device reconnected meanwhile; its new driver publishes live
                    return []
                response.raise_for_status()
                return (await response.json()).get("publications") or []
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("could not read the presentation of %s: %s", device, err)
            return []

    async def _publish(self, publication: dict[str, Any]) -> None:
        topic, payload = publication.get("topic"), publication.get("payload", "")
        if not isinstance(topic, str) or not topic:
            return
        retain = bool(publication.get("retain"))
        if (device := self._descriptor_of(topic)) is not None:
            payload = self._labelled(device, topic, payload, retain)
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        await self._transport.publish(topic, payload, qos=1, retain=retain)

    def _labelled(self, device: str, topic: str, payload: Any, retain: bool) -> Any:
        """A descriptor with rusthinq's name for the device as its label, remembered to pass on again if it changes."""
        try:
            document = json.loads(payload) if isinstance(payload, str) else payload
        except ValueError:
            document = None
        if not isinstance(document, dict) or document.get("id") != device:
            self._descriptors.pop(device, None)  # removed (empty payload) or not a descriptor
            self._labels.pop(device, None)
            return payload
        self._descriptors[device] = (topic, document, retain)
        self._labels[device] = name = self._name(device)
        return {**document, "label": name} if name else document

    async def _presence(self, online: bool) -> None:
        await self._transport.publish(self.presence_topic, "online" if online else "offline", qos=1, retain=True)

    # ---- commands ------------------------------------------------------------------------

    def _on_command(self, msg: Message) -> None:
        if msg.retain:
            return
        parts = msg.topic[len(self._prefix) + 1:].split("/")
        if not msg.topic.startswith(f"{self._prefix}/") or len(parts) != 3:
            return
        device, prop, _ = parts
        value = msg.payload.decode("utf-8", "replace") if isinstance(msg.payload, bytes) else str(msg.payload)
        task = asyncio.create_task(self.command(device, prop, value), name=f"rusthinq command {device}/{prop}")
        self._commands.add(task)
        task.add_done_callback(self._commands.discard)

    async def command(self, device: str, prop: str, value: str) -> bool:
        """Invoke the driver's `__command`; once more with a fresh session if the device reconnected meanwhile.

        The driver checks the write and rejects it itself (il.md section 5); what fails before it is reached is
        rejected here as `unavailable`, since the write cannot be delivered or queued."""
        for attempt in range(2):
            session = self._sessions.get(device)
            if session is None or attempt:
                session = await self._refresh_session(device)
            if session is None:
                await self._reject(device, prop, "the device is offline")
                return False
            status, reason = await self._invoke(device, session, prop, value)
            if status == 202:
                return True
            if status != 409:
                break
        await self._reject(device, prop, f"rusthinq refused the command ({status or 'no answer'}): {reason}")
        return False

    async def _reject(self, device: str, prop: str, reason: str) -> None:
        """Publish an IL `Reject` on the device's reject topic (il-mqtt.md), where the consumer reports it."""
        _, document, _ = self._descriptors.get(device) or (None, {}, None)
        template = (document.get("x-mqtt") or {}).get("reject") or f"{self._prefix}/{{id}}/reject"
        body = {"prop": prop, "code": "unavailable", "reason": reason}
        await self._transport.publish(template.replace("{id}", device), json.dumps(body), qos=1, retain=False)

    async def _invoke(self, device: str, session: Session, prop: str, value: str) -> tuple[int, str]:
        body = {
            "incarnation": session.incarnation,
            "generation": session.generation,
            "script_generation": session.script_generation,
            "function": "__command",
            "input": json.dumps({"prop": prop, "value": value}),
        }
        try:
            async with self._http.post(
                f"{self._url}/api/devices/{device}/invoke", json=body, headers=self._headers, timeout=REQUEST_TIMEOUT
            ) as response:
                return response.status, (await response.text()).strip()
        except (aiohttp.ClientError, TimeoutError) as err:
            return 0, str(err)

    async def _refresh_session(self, device: str) -> Session | None:
        if await self._read_devices() is None:
            return None
        return self._sessions.get(device)
