"""The retained IL publications kept across Home Assistant restarts, as a broker keeps them.

With a broker, rusthinq's retained inventory and the broker hold every device's descriptor and last values, so an
appliance that is off (a washer drops off Wi-Fi when switched off) keeps its entities. The in-process transport and
an embedded daemon both start empty, and `presentation` answers for online devices only; this stores what the
transport retains and replays it before the hub subscribes.

A restored device is marked unavailable (its `available` role property set to false) until rusthinq says otherwise:
the stored value is whatever it was when Home Assistant stopped. Devices rusthinq no longer knows are left out of the
next save.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .client import ManagementClient
from .const import DOMAIN

if TYPE_CHECKING:
    from ildevice_ha.core.memory import InProcessTransport
    from ildevice_ha.core.transport import Message

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SAVE_DELAY = 30


class RetainedStore:
    def __init__(self, hass: HomeAssistant, entry_id: str, transport: InProcessTransport, prefix: str) -> None:
        self._store = _store(hass, entry_id)
        self._transport = transport
        self._prefix = prefix
        self._client: ManagementClient | None = None
        self._pending = False

    @staticmethod
    async def async_remove(hass: HomeAssistant, entry_id: str) -> None:
        await _store(hass, entry_id).async_remove()

    def _device_of(self, topic: str) -> str | None:
        """The device a topic under the prefix belongs to; None for `_producer` and other reserved levels."""
        if not topic.startswith(f"{self._prefix}/"):
            return None
        device = topic[len(self._prefix) + 1:].split("/", 1)[0]
        return device if device and not device.startswith("_") else None

    async def async_restore(self) -> None:
        """Publish the stored publications into the transport, each device marked unavailable."""
        stored = await self._store.async_load() or {}
        for topic, payload in {**stored, **_unavailable(stored, self._prefix)}.items():
            await self._transport.publish(topic, payload, qos=1, retain=True)
        if stored:
            _LOGGER.debug("restored %d retained publications", len(stored))

    async def start(self, client: ManagementClient):
        """Save whenever a device publishes; returns the function that stops it."""
        self._client = client
        return await self._transport.subscribe(f"{self._prefix}/#", self._on_message)

    def _on_message(self, msg: Message) -> None:
        if not self._pending and self._device_of(msg.topic) is not None:
            self._pending = True
            self._store.async_delay_save(self._data, SAVE_DELAY)

    def _data(self) -> dict[str, str]:
        self._pending = False
        client = self._client
        known = set(client.devices) if client is not None and client.connected.is_set() else None
        data = {}
        for topic, msg in self._transport.retained.items():
            device = self._device_of(topic)
            if device is None or (known is not None and device not in known):
                continue
            payload = msg.payload
            data[topic] = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        return data


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, str]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.retained")


def _unavailable(stored: dict[str, str], prefix: str) -> dict[str, str]:
    """`false` on the state topic of each stored descriptor's `available` role property."""
    from ildevice_ha.core.descriptor import DescriptorError, parse_descriptor
    from ildevice_ha.core.plan import availability_prop
    from ildevice_ha.core.topics import descriptor_id, resolve_topics, state_topic

    marks = {}
    for topic, payload in stored.items():
        if descriptor_id(topic, prefix) is None:
            continue
        try:
            desc = parse_descriptor(json.loads(payload))
        except (ValueError, DescriptorError):
            continue
        if (prop := availability_prop(desc)) is not None:
            marks[state_topic(desc, resolve_topics(desc, prefix), prop)] = "false"
    return marks
