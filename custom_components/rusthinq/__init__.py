"""LG ThinQ appliances through rusthinq, without an MQTT broker.

Home Assistant runs the rusthinq daemon (`embedded.EmbeddedDaemon`); its management API feeds an in-process IL
transport (`client.ManagementClient`), and an ildevice-homeassistant hub embedded on that transport turns the IL
devices into entities registered under this integration. ildevice-homeassistant is a separate integration (manifest
`dependencies`); only its modules are used here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import ManagementClient, basic_authorization
from .const import (
    CONF_HTTPS_PORT,
    CONF_MODE,
    CONF_MQTT_PORT,
    CONF_PREFIX,
    CONF_THINQ1_HTTPS_PORT,
    CONF_THINQ1_PORT,
    CONF_URL,
    DEFAULT_PORTS,
    DOMAIN,
    ENTRY_PLATFORMS,
    MODE_EMBEDDED,
)
from .embedded import DaemonError, EmbeddedDaemon

if TYPE_CHECKING:
    from custom_components.ildevice.hub import IlHub


@dataclass
class RusthinqData:
    hub: IlHub
    client: ManagementClient
    daemon: EmbeddedDaemon | None


type RusthinqConfigEntry = ConfigEntry[RusthinqData]


def management_headers(data) -> dict[str, str]:
    """The headers that authenticate to the management API of a URL entry, if it has a user."""
    user = data.get(CONF_USERNAME)
    return basic_authorization(user, data.get(CONF_PASSWORD) or "") if user else {}


def daemon_ports(data) -> dict[str, int]:
    """The appliance-facing ports of an embedded entry, by listener name."""
    ports = {**DEFAULT_PORTS, **{k: v for k, v in data.items() if k in DEFAULT_PORTS}}
    return {"https": ports[CONF_HTTPS_PORT], "mqtt": ports[CONF_MQTT_PORT],
            "thinq1_https": ports[CONF_THINQ1_HTTPS_PORT], "thinq1": ports[CONF_THINQ1_PORT]}


async def async_setup_entry(hass: HomeAssistant, entry: RusthinqConfigEntry) -> bool:
    from custom_components.ildevice.attach import build_hub
    from custom_components.ildevice.const import CONF_AUTO_ADD, CONF_IL_PREFIX
    from custom_components.ildevice.core.memory import InProcessTransport

    prefix = entry.data[CONF_PREFIX]
    daemon = None
    if entry.data.get(CONF_MODE) == MODE_EMBEDDED:
        daemon = EmbeddedDaemon(hass, ports=daemon_ports(entry.data), prefix=prefix)
        try:
            await daemon.async_start()
        except DaemonError as err:
            await daemon.async_stop()
            raise ConfigEntryNotReady(str(err)) from err

        async def stop_daemon(_event: Event) -> None:
            # Home Assistant does not unload entries when it stops.
            await daemon.async_stop()

        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_daemon))
        url, headers = daemon.url, daemon.headers
    else:
        url, headers = entry.data[CONF_URL], management_headers(entry.data)

    transport = InProcessTransport()
    # Every device of a rusthinq is the user's own appliance: add it, never offer it (ildevice's offers would go to
    # the ildevice domain, not this one).
    hub = build_hub(hass, {CONF_IL_PREFIX: prefix, CONF_AUTO_ADD: True}, transport, DOMAIN, entry.entry_id)
    client = ManagementClient(async_get_clientsession(hass), url, transport, prefix, headers=headers)
    entry.runtime_data = RusthinqData(hub, client, daemon)
    await hass.config_entries.async_forward_entry_setups(entry, ENTRY_PLATFORMS)
    await hub.async_start()
    await client.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RusthinqConfigEntry) -> bool:
    data = entry.runtime_data
    await data.client.stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, ENTRY_PLATFORMS)
    if unloaded:
        await data.hub.async_stop()
    if data.daemon is not None:
        await data.daemon.async_stop()
    return unloaded
