"""The rusthinq management API as an IL producer: daemon publications in, IL commands out.

rusthinq publishes what its drivers produce on the `/api/events` websocket (`scriptOutput`, whose payload is the
`{"topic", "payload", "retain"}` an MQTT broker would receive) and answers `/api/devices/{id}/presentation` with the
current publications of an online device. This client replays both into an IL transport, and turns the IL commands
consumers publish on `<prefix>/<device>/<prop>/set` into `invoke(__command, {"prop", "value"})`, the same call
rusthinq's own MQTT command path makes. Nothing here imports Home Assistant.
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
            self._start_resync(state)
            if not self.connected.is_set():
                self.connected.set()
                await self._presence(True)
            self._set_devices(state)
            self._schedule_status(STATUS_INTERVAL)
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

    def _set_devices(self, state: dict[str, Any]) -> None:
        self.devices = dict(state.get("devices") or {})
        self.features = dict(state.get("features") or {})
        self._changed()

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
        if self.connected.is_set():
            self._set_devices(state)
        return state

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
        if not isinstance(payload, str):
            payload = json.dumps(payload)
        await self._transport.publish(topic, payload, qos=1, retain=bool(publication.get("retain")))

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
        """Invoke the driver's `__command`; once more with a fresh session if the device reconnected meanwhile."""
        for attempt in range(2):
            session = self._sessions.get(device)
            if session is None or attempt:
                session = await self._refresh_session(device)
            if session is None:
                _LOGGER.warning("command %s=%s for %s dropped: the device is offline", prop, value, device)
                return False
            status, reason = await self._invoke(device, session, prop, value)
            if status == 202:
                return True
            if status != 409:
                break
        _LOGGER.warning("command %s=%s for %s rejected by rusthinq (%s): %s", prop, value, device, status, reason)
        return False

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
