"""LG ThinQ appliances through rusthinq, without an MQTT broker.

Home Assistant runs the rusthinq daemon (`embedded.EmbeddedDaemon`); its management API feeds an in-process IL
transport (`client.ManagementClient`), and an ildevice-homeassistant hub embedded on that transport turns the IL
devices into entities registered under this integration. That hub is the `ildevice_ha` package of
ildevice-homeassistant (manifest `requirements`); the IL device integration itself need not be installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntry

from .client import ManagementClient, ManagementError, basic_authorization
from .const import (
    CONF_HTTPS_PORT,
    CONF_MODE,
    CONF_MQTT_PORT,
    CONF_PREFIX,
    CONF_SETTINGS,
    CONF_THINQ1_HTTPS_PORT,
    CONF_THINQ1_PORT,
    CONF_URL,
    DEFAULT_PORTS,
    DOMAIN,
    ENTRY_PLATFORMS,
    MODE_EMBEDDED,
)
from .embedded import DaemonError, EmbeddedDaemon, PortsUnavailable, ports_issue
from .retained import RetainedStore
from .status import DeviceStatus

if TYPE_CHECKING:
    from ildevice_ha.hub import IlHub


@dataclass
class RusthinqData:
    hub: IlHub
    client: ManagementClient
    daemon: EmbeddedDaemon | None
    status: DeviceStatus


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
    from ildevice_ha.attach import build_hub
    from ildevice_ha.const import CONF_AUTO_ADD, CONF_IL_PREFIX
    from ildevice_ha.core.memory import InProcessTransport

    prefix = entry.data[CONF_PREFIX]
    daemon = None
    if entry.data.get(CONF_MODE) == MODE_EMBEDDED:
        daemon = EmbeddedDaemon(hass, ports=daemon_ports(entry.data), prefix=prefix,
                                settings=entry.data.get(CONF_SETTINGS))
        try:
            await daemon.async_start()
        except DaemonError as err:
            await daemon.async_stop()
            if isinstance(err, PortsUnavailable):
                ports_issue(hass, err.problems)
            raise ConfigEntryNotReady(str(err)) from err
        ports_issue(hass, None)

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
    retained = RetainedStore(hass, entry.entry_id, transport, prefix)
    await retained.async_restore()
    client = ManagementClient(async_get_clientsession(hass), url, transport, prefix, headers=headers)
    entry.async_on_unload(await retained.start(client))
    status = DeviceStatus(hass, entry, client)
    entry.async_on_unload(status.start())
    entry.runtime_data = RusthinqData(hub, client, daemon, status)
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


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: RusthinqConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Deleting an appliance forgets it in rusthinq too, and clears what is retained of it. One that is connected
    would come right back, so it is refused until the appliance is off or gone. The rusthinq device stays."""
    devices = {ident for domain, ident in device_entry.identifiers if domain == DOMAIN}
    if entry.entry_id in devices or entry.state is not ConfigEntryState.LOADED:
        return False
    client = entry.runtime_data.client
    if not client.connected.is_set():
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="not_connected")
    name = device_entry.name_by_user or device_entry.name or ", ".join(sorted(devices))
    for device in devices:
        if (client.devices.get(device) or {}).get("online"):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="device_online",
                                     translation_placeholders={"name": name})
    for device in devices:
        if device in client.devices:
            try:
                await client.forget(device)
            except ManagementError as err:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="forget_failed",
                                         translation_placeholders={"name": name, "error": str(err)}) from err
        await client.clear(device)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: RusthinqConfigEntry) -> None:
    await RetainedStore.async_remove(hass, entry.entry_id)
