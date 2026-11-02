"""Device status entities: local connection, LG bridge, driver and firmware of each rusthinq device."""

import asyncio

import pytest
from conftest import AC, EMBEDDED
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rusthinq import client as client_module
from custom_components.rusthinq.const import DOMAIN, MODE_EMBEDDED


@pytest.fixture(autouse=True)
def _custom(enable_custom_integrations, monkeypatch):
    monkeypatch.setattr(client_module, "STATUS_SETTLE", 0.05)


async def eventually(hass, check, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.02)
            await hass.async_block_till_done()


def entity_id(hass, device: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "sensor" if key == "firmware" else "binary_sensor", DOMAIN, f"{device}-rusthinq-{key}"
    )


def state(hass, device: str, key: str):
    eid = entity_id(hass, device, key)
    return hass.states.get(eid) if eid else None


async def setup(hass) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MODE_EMBEDDED, data=EMBEDDED)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_each_device_shows_its_connection_bridge_driver_and_firmware(hass, air_conditioner):
    await setup(hass)
    await eventually(hass, lambda: state(hass, AC, "firmware") and state(hass, AC, "bridge"))
    assert state(hass, AC, "local").state == STATE_ON
    assert state(hass, AC, "local").attributes["platform"] == "ThinQ2"
    bridge = state(hass, AC, "bridge")
    assert bridge.state == STATE_ON
    assert {k: bridge.attributes[k] for k in ("enabled", "paired", "pending", "error")} == {
        "enabled": True, "paired": True, "pending": False, "error": None}
    assert state(hass, AC, "driver").state == STATE_OFF
    assert state(hass, AC, "driver").attributes["file"] == "drivers/CST_570004_WW.rhai"
    assert state(hass, AC, "firmware").state == "570004"

    # all on the device the IL entities are on, and diagnostic
    await eventually(hass, lambda: any(e.domain == "climate" for e in er.async_get(hass).entities.values()))
    registry = er.async_get(hass)
    devices = {registry.async_get(entity_id(hass, AC, key)).device_id for key in ("local", "bridge", "driver",
                                                                                   "firmware")}
    climate = next(e for e in registry.entities.values() if e.domain == "climate")
    assert devices == {climate.device_id}
    assert registry.async_get(entity_id(hass, AC, "local")).entity_category == "diagnostic"


async def test_a_connection_event_rereads_the_status(hass, air_conditioner):
    await setup(hass)
    await eventually(hass, lambda: state(hass, AC, "local") and state(hass, AC, "local").state == STATE_ON)
    await eventually(hass, lambda: air_conditioner.device_reads)  # the read after connecting, for the LG account
    await asyncio.sleep(0.1)
    reads = air_conditioner.device_reads
    await air_conditioner.send({"type": "stateChanged", "device": AC, "cloudBound": "{}"})
    await asyncio.sleep(0.2)
    assert air_conditioner.device_reads == reads  # an uplink changes no status

    air_conditioner.devices[AC].update(online=False, bridged=False, bridgeError="offline")
    await air_conditioner.send({"type": "stateChanged", "detail": f"Down(SessionId {{ device: \"{AC}\" }}, Eof)"})
    await eventually(hass, lambda: state(hass, AC, "local").state == STATE_OFF)
    assert state(hass, AC, "bridge").state == STATE_OFF
    assert state(hass, AC, "bridge").attributes["error"] == "offline"


async def test_a_device_without_a_driver_gets_a_device_and_a_problem(hass, rusthinq):
    rusthinq.add_device("washer-1", [], mapped=False, name="세탁기", modelName="F24VDD", bridgeEnabled=False,
                        bridged=False)
    await setup(hass)
    await eventually(hass, lambda: state(hass, "washer-1", "driver"))
    assert state(hass, "washer-1", "driver").state == STATE_ON
    device = dr.async_get(hass).async_get(er.async_get(hass).async_get(entity_id(hass, "washer-1", "driver")).device_id)
    assert (device.name, device.model) == ("세탁기", "F24VDD")


async def test_the_status_is_unavailable_while_the_daemon_is_unreachable(hass, air_conditioner):
    await setup(hass)
    await eventually(hass, lambda: state(hass, AC, "local") and state(hass, AC, "local").state == STATE_ON)
    await air_conditioner.stop()
    await eventually(hass, lambda: state(hass, AC, "local").state == STATE_UNAVAILABLE)
    assert state(hass, AC, "firmware").state == STATE_UNAVAILABLE
