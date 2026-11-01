"""Deleting an appliance in Home Assistant forgets it in rusthinq; diagnostics for bug reports."""

import pytest
from conftest import AC
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from custom_components.rusthinq.const import DOMAIN
from test_init import appliances, climate_entity, eventually, setup


@pytest.fixture(autouse=True)
def _custom(enable_custom_integrations):
    return


async def remove(hass, ws_client, entry, device) -> dict:
    """Delete a device as the device page does."""
    client = await ws_client(hass)
    await client.send_json_auto_id({"type": "config/device_registry/remove_config_entry",
                                    "config_entry_id": entry.entry_id, "device_id": device.id})
    return await client.receive_json()


async def go_offline(hass, server) -> None:
    server.devices[AC].update(online=False, generation=None, scriptGeneration=None, mapped=False, bridged=False)
    await server.send({"type": "stateChanged", "detail": f"Down(SessionId {{ device: \"{AC}\" }}, Eof)"})
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await eventually(hass, lambda: not entry.runtime_data.client.devices[AC]["online"])


async def test_an_offline_appliance_is_forgotten_in_rusthinq_and_leaves(hass, hass_ws_client, air_conditioner):
    await async_setup_component(hass, "config", {})
    entry = await setup(hass, air_conditioner)
    await eventually(hass, lambda: climate_entity(hass))
    await go_offline(hass, air_conditioner)
    [device] = appliances(hass, entry)

    answer = await remove(hass, hass_ws_client, entry, device)
    assert answer["success"], answer
    assert air_conditioner.forgotten == [(AC, {"incarnation": "1"})]
    assert AC not in air_conditioner.devices
    assert appliances(hass, entry) == []
    assert climate_entity(hass) is None
    assert entry.runtime_data.client.retained_topics(AC) == []  # nothing left to restore after a restart


async def test_a_connected_appliance_is_refused_since_it_would_come_back(hass, hass_ws_client, air_conditioner):
    await async_setup_component(hass, "config", {})
    entry = await setup(hass, air_conditioner)
    await eventually(hass, lambda: climate_entity(hass))
    [device] = appliances(hass, entry)

    answer = await remove(hass, hass_ws_client, entry, device)
    assert not answer["success"]
    assert "would come right back" in answer["error"]["message"]
    assert air_conditioner.forgotten == []
    assert appliances(hass, entry) == [device]


async def test_rusthinqs_refusal_is_shown_and_the_device_stays(hass, hass_ws_client, air_conditioner):
    await async_setup_component(hass, "config", {})
    entry = await setup(hass, air_conditioner)
    await eventually(hass, lambda: climate_entity(hass))
    await go_offline(hass, air_conditioner)
    [device] = appliances(hass, entry)

    air_conditioner.cloud_failure = (409, "bridge registration pending; unpair first")
    answer = await remove(hass, hass_ws_client, entry, device)
    assert not answer["success"]
    assert "bridge registration pending; unpair first" in answer["error"]["message"]
    assert appliances(hass, entry) == [device]
    assert climate_entity(hass) is not None


async def test_the_rusthinq_device_cannot_be_deleted(hass, hass_ws_client, air_conditioner):
    await async_setup_component(hass, "config", {})
    entry = await setup(hass, air_conditioner)

    def service():
        return next((d for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
                     if (DOMAIN, entry.entry_id) in d.identifiers), None)

    await eventually(hass, service)
    answer = await remove(hass, hass_ws_client, entry, service())
    assert not answer["success"]
    assert air_conditioner.forgotten == []


async def diagnostics(hass, hass_client, path: str) -> dict:
    """What *Download diagnostics* serves (the harness's own helpers are newer than Home Assistant 2025.1)."""
    assert await async_setup_component(hass, "diagnostics", {})
    response = await (await hass_client()).get(f"/api/diagnostics/config_entry/{path}")
    assert response.status == 200, await response.text()
    return (await response.json())["data"]


async def test_diagnostics(hass, hass_client, air_conditioner):
    entry = await setup(hass, air_conditioner)
    await eventually(hass, lambda: climate_entity(hass))
    entry_data = await diagnostics(hass, hass_client, entry.entry_id)
    assert entry_data["connected"] is True
    assert entry_data["rusthinq"]["version"] == "0.2.0"
    assert AC in entry_data["devices"]

    [device] = appliances(hass, entry)
    per_device = await diagnostics(hass, hass_client, f"{entry.entry_id}/device/{device.id}")
    assert per_device[AC]["descriptor"]["model"]
    assert per_device[AC]["retained"]
