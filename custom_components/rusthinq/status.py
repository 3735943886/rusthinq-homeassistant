"""Diagnostic entities for each rusthinq device: whether it is connected locally, its LG bridge, its driver and its
firmware, as rusthinq's `/api/devices` describes them (`client.ManagementClient.devices`).

They join the Home Assistant device the ildevice hub creates for the same id (the IL id is rusthinq's device id), and
create it, named after rusthinq's name for the device, when the device has no driver output yet. All are unavailable
while the daemon is unreachable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import Entity

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import RusthinqConfigEntry
    from .client import ManagementClient


def _signal(entry_id: str) -> str:
    return f"{DOMAIN}_status_{entry_id}"


class DeviceStatus:
    """Creates the status entities of each device as it first appears, and tells them when anything changed."""

    def __init__(self, hass: HomeAssistant, entry: RusthinqConfigEntry, client: ManagementClient) -> None:
        self._hass = hass
        self._entry = entry
        self.client = client
        self._adders: dict[str, AddEntitiesCallback] = {}
        self._known: set[str] = set()
        self._bridge_known: set[str] = set()

    def start(self) -> Callable[[], None]:
        return self.client.add_listener(self._changed)

    def register_platform(self, platform: str, add_entities: AddEntitiesCallback) -> None:
        self._adders[platform] = add_entities
        self._changed()

    @callback
    def _changed(self) -> None:
        if {"binary_sensor", "sensor"} <= self._adders.keys():
            bridge = bool(self.client.features.get("bridge"))
            for device in self.client.devices:
                if device not in self._known:
                    self._known.add(device)
                    self._adders["binary_sensor"]([LocalConnection(self, device), Driver(self, device)])
                    self._adders["sensor"]([Firmware(self, device)])
                if bridge and device not in self._bridge_known:
                    self._bridge_known.add(device)
                    self._adders["binary_sensor"]([Bridge(self, device)])
        async_dispatcher_send(self._hass, _signal(self._entry.entry_id))

    def info(self, device: str) -> dict[str, Any] | None:
        return self.client.devices.get(device) if self.client.connected.is_set() else None


class StatusEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    key = ""

    def __init__(self, status: DeviceStatus, device: str) -> None:
        self._status = status
        self._device = device
        self._attr_unique_id = f"{device}-rusthinq-{self.key}"
        info = status.client.devices.get(device) or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device)},
            default_name=info.get("name") or info.get("modelName") or info.get("model") or device,
            default_manufacturer="LG",
            default_model=info.get("modelName") or info.get("model") or None,
        )

    @property
    def info(self) -> dict[str, Any]:
        return self._status.info(self._device) or {}

    @property
    def available(self) -> bool:
        return self._status.info(self._device) is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, _signal(self._status._entry.entry_id), self.async_write_ha_state
        ))


class LocalConnection(StatusEntity, BinarySensorEntity):
    key = "local"
    _attr_name = "Local connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool:
        return bool(self.info.get("online"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"platform": self.info.get("platform") or None}


class Bridge(StatusEntity, BinarySensorEntity):
    key = "bridge"
    _attr_name = "LG bridge"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool:
        return bool(self.info.get("bridged"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self.info
        return {"enabled": bool(info.get("bridgeEnabled")), "paired": bool(info.get("bridgePaired")),
                "pending": bool(info.get("bridgePending")), "error": info.get("bridgeError")}


class Driver(StatusEntity, BinarySensorEntity):
    """A problem when the driver failed, or when the device is online without one."""

    key = "driver"
    _attr_name = "Driver"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool:
        info = self.info
        return bool(info.get("scriptFaulted")) or (bool(info.get("online")) and not info.get("mapped"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        driver = self.info.get("driver")
        return {"file": driver.get("file") if isinstance(driver, dict) else None}


class Firmware(StatusEntity, SensorEntity):
    key = "firmware"
    _attr_name = "Firmware"

    @property
    def native_value(self) -> str | None:
        return self.info.get("swVersion") or None
