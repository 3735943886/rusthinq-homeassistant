"""rusthinq's own entities, beside the IL ones the ildevice hub creates.

Each device: diagnostic entities for whether it is connected locally, its LG bridge, its driver and its firmware, as
rusthinq's `/api/devices` describes them (`client.ManagementClient.devices`), and, with the LG bridge, the controls the
rusthinq dashboard has for it (enable/disable switch, pair and unpair buttons). They join the Home Assistant device the
hub creates for the same id (the IL id is rusthinq's device id), and create it, named after rusthinq's name for the
device, when the device has no driver output yet.

The daemon: an "LG account" sensor on the rusthinq service device (signing in and out is in the options flow).

All are unavailable while the daemon is unreachable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import Entity

from .client import ManagementError
from .const import DOMAIN

PLATFORMS = ("binary_sensor", "button", "sensor", "switch")
"""The (ildevice) platforms that also carry these entities."""

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import RusthinqConfigEntry
    from .client import ManagementClient


def _signal(entry_id: str) -> str:
    return f"{DOMAIN}_status_{entry_id}"


def service_device(entry: RusthinqConfigEntry) -> DeviceInfo:
    """The device that stands for the daemon itself."""
    daemon = entry.runtime_data.daemon
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="rusthinq",
        manufacturer="rusthinq",
        model="Embedded daemon" if daemon else "Management API",
        sw_version=daemon.version if daemon else None,
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://github.com/3735943886/rusthinq",
    )


class DeviceStatus:
    """Creates the status entities of each device as it first appears, and tells them when anything changed."""

    def __init__(self, hass: HomeAssistant, entry: RusthinqConfigEntry, client: ManagementClient) -> None:
        self._hass = hass
        self._entry = entry
        self.client = client
        self._adders: dict[str, AddEntitiesCallback] = {}
        self._known: set[str] = set()
        self._bridge_known: set[str] = set()
        self._account_added = False

    def start(self) -> Callable[[], None]:
        return self.client.add_listener(self._changed)

    def register_platform(self, platform: str, add_entities: AddEntitiesCallback) -> None:
        self._adders[platform] = add_entities
        self._changed()

    @callback
    def _changed(self) -> None:
        if set(PLATFORMS) <= self._adders.keys():
            bridge = bool(self.client.features.get("bridge"))
            if bridge and not self._account_added:
                self._account_added = True
                self._adders["binary_sensor"]([Account(self)])
            for device in self.client.devices:
                if device not in self._known:
                    self._known.add(device)
                    self._adders["binary_sensor"]([LocalConnection(self, device), Driver(self, device)])
                    self._adders["sensor"]([Firmware(self, device)])
                if bridge and device not in self._bridge_known:
                    self._bridge_known.add(device)
                    self._adders["binary_sensor"]([Bridge(self, device)])
                    self._adders["switch"]([BridgeSwitch(self, device)])
                    self._adders["button"]([BridgeButton(self, device, "pair"), BridgeButton(self, device, "unpair")])
        async_dispatcher_send(self._hass, _signal(self._entry.entry_id))

    def info(self, device: str) -> dict[str, Any] | None:
        return self.client.devices.get(device) if self.client.connected.is_set() else None

    @property
    def signed_in(self) -> bool:
        return self.client.connected.is_set() and bool(self.client.account.get("loggedIn"))

    async def bridge(self, device: str, action: str) -> None:
        try:
            await self.client.bridge(device, action)
        except ManagementError as err:
            raise HomeAssistantError(f"LG bridge {action} failed: {err}") from err


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


class Account(BinarySensorEntity):
    """Signed in to LG, on the rusthinq service device."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "LG account"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, status: DeviceStatus) -> None:
        self._status = status
        self._attr_unique_id = f"{status._entry.entry_id}-lg-account"
        self._attr_device_info = service_device(status._entry)

    @property
    def available(self) -> bool:
        return self._status.client.connected.is_set()

    @property
    def is_on(self) -> bool:
        return bool(self._status.client.account.get("loggedIn"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        account = self._status.client.account
        return {"country": account.get("country"), "error": account.get("error"), "busy": bool(account.get("busy"))}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, _signal(self._status._entry.entry_id), self.async_write_ha_state
        ))


class BridgeSwitch(StatusEntity, SwitchEntity):
    """Whether rusthinq keeps this device's LG bridge connected (enable/disable); only for a paired device."""

    key = "bridge-enabled"
    _attr_name = "LG bridge"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:cloud-sync"

    @property
    def available(self) -> bool:
        info = self._status.info(self._device)
        return info is not None and bool(info.get("bridgePaired"))

    @property
    def is_on(self) -> bool:
        return bool(self.info.get("bridgeEnabled"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._status.bridge(self._device, "enable")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._status.bridge(self._device, "disable")


class BridgeButton(StatusEntity, ButtonEntity):
    """Register the device with the LG account (pair) or remove it (unpair, also what settles a pending pair)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, status: DeviceStatus, device: str, action: str) -> None:
        self.key = f"bridge-{action}"
        self._action = action
        self._attr_name = "Pair with LG" if action == "pair" else "Unpair from LG"
        self._attr_icon = "mdi:cloud-plus" if action == "pair" else "mdi:cloud-remove"
        super().__init__(status, device)

    @property
    def available(self) -> bool:
        info = self._status.info(self._device)
        if info is None:
            return False
        registered = bool(info.get("bridgePaired") or info.get("bridgePending"))
        if self._action == "pair":
            return not registered and self._status.signed_in
        return registered

    async def async_press(self) -> None:
        await self._status.bridge(self._device, self._action)
