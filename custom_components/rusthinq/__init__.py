"""LG ThinQ appliances through rusthinq, without an MQTT broker.

The rusthinq management API feeds an in-process IL transport (`client.ManagementClient`), and an ildevice-homeassistant
hub embedded on that transport turns the IL devices into entities registered under this integration.
ildevice-homeassistant is a separate integration (manifest `dependencies`); only its modules are used here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import ManagementClient
from .const import CONF_PREFIX, CONF_URL, DOMAIN, PLATFORMS

if TYPE_CHECKING:
    from custom_components.ildevice.hub import IlHub


@dataclass
class RusthinqData:
    hub: IlHub
    client: ManagementClient


type RusthinqConfigEntry = ConfigEntry[RusthinqData]


def basic_auth(data) -> aiohttp.BasicAuth | None:
    user = data.get(CONF_USERNAME)
    return aiohttp.BasicAuth(user, data.get(CONF_PASSWORD) or "") if user else None


async def async_setup_entry(hass: HomeAssistant, entry: RusthinqConfigEntry) -> bool:
    from custom_components.ildevice.attach import build_hub
    from custom_components.ildevice.const import CONF_AUTO_ADD, CONF_IL_PREFIX
    from custom_components.ildevice.core.memory import InProcessTransport

    prefix = entry.data[CONF_PREFIX]
    transport = InProcessTransport()
    # Every device of a rusthinq is the user's own appliance: add it, never offer it (ildevice's offers would go to
    # the ildevice domain, not this one).
    hub = build_hub(hass, {CONF_IL_PREFIX: prefix, CONF_AUTO_ADD: True}, transport, DOMAIN, entry.entry_id)
    client = ManagementClient(
        async_get_clientsession(hass), entry.data[CONF_URL], transport, prefix, auth=basic_auth(entry.data)
    )
    entry.runtime_data = RusthinqData(hub, client)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await hub.async_start()
    await client.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RusthinqConfigEntry) -> bool:
    await entry.runtime_data.client.stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.hub.async_stop()
    return unloaded
