"""Diagnostics for bug reports: what Home Assistant sees of rusthinq, and rusthinq's own `/api/diagnostics`."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import RusthinqConfigEntry
from .client import ManagementError
from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: RusthinqConfigEntry) -> dict[str, Any]:
    data = entry.runtime_data
    client = data.client
    try:
        rusthinq = await client.diagnostics()
    except ManagementError as err:
        rusthinq = {"error": str(err)}
    daemon = data.daemon
    return {
        "entry": dict(entry.data),
        "daemon": {"version": daemon.version, "log": list(daemon.lines)},
        "connected": client.connected.is_set(),
        "features": client.features,
        "account": client.account,
        "devices": client.devices,
        "rusthinq": rusthinq,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: RusthinqConfigEntry, device_entry: DeviceEntry
) -> dict[str, Any]:
    client = entry.runtime_data.client
    devices = sorted(ident for domain, ident in device_entry.identifiers
                     if domain == DOMAIN and ident != entry.entry_id)
    return {
        device: {
            "rusthinq": client.devices.get(device),
            "descriptor": client.descriptor(device),
            "retained": client.retained_values(device),
        }
        for device in devices
    }
