"""The integration in a real (test) Home Assistant, fed by a fake rusthinq."""

import asyncio
import json

import pytest
from conftest import AC
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rusthinq.const import CONF_MODE, CONF_PREFIX, CONF_URL, DOMAIN, MODE_URL, PLATFORMS


@pytest.fixture(autouse=True)
def _custom(enable_custom_integrations):
    return


async def eventually(hass, check, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)
            await hass.async_block_till_done()


def climate_entity(hass) -> str | None:
    registry = er.async_get(hass)
    return next((e.entity_id for e in registry.entities.values() if e.platform == DOMAIN and e.domain == "climate"),
                None)


async def setup(hass, server) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: server.url, CONF_PREFIX: "rusthinq"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_the_platforms_are_ildevices():
    from custom_components.ildevice.const import PLATFORMS as ILDEVICE_PLATFORMS

    assert PLATFORMS == ILDEVICE_PLATFORMS


async def test_a_device_appears_under_this_domain_and_takes_commands(hass, air_conditioner):
    entry = await setup(hass, air_conditioner)
    await eventually(hass, lambda: climate_entity(hass) and hass.states.get(climate_entity(hass)))
    entity_id = climate_entity(hass)
    state = hass.states.get(entity_id)
    assert state.attributes["temperature"] == 18
    assert state.attributes["current_temperature"] == 24

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.identifiers for d in devices] == [{(DOMAIN, AC)}]

    await hass.services.async_call("climate", "set_temperature", {"entity_id": entity_id, "temperature": 22},
                                   blocking=True)
    await eventually(hass, lambda: air_conditioner.invokes)
    device_id, body = air_conditioner.invokes[0]
    assert device_id == AC
    assert json.loads(body["input"]) == {"prop": "target", "value": "22"}

    await air_conditioner.output(AC, f"rusthinq/{AC}/target", "22")
    await eventually(hass, lambda: hass.states.get(entity_id).attributes["temperature"] == 22)


async def test_entities_are_unavailable_while_the_daemon_is_unreachable(hass, air_conditioner):
    await setup(hass, air_conditioner)
    await eventually(hass, lambda: climate_entity(hass) and hass.states.get(climate_entity(hass)))
    entity_id = climate_entity(hass)
    await air_conditioner.stop()
    await eventually(hass, lambda: hass.states.get(entity_id).state == STATE_UNAVAILABLE)


async def test_unload(hass, air_conditioner):
    entry = await setup(hass, air_conditioner)
    await eventually(hass, lambda: climate_entity(hass))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(climate_entity(hass)).state == STATE_UNAVAILABLE


async def test_url_config_flow(hass, rusthinq):
    await async_setup_component(hass, "ildevice", {})
    menu = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": MODE_URL})
    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "http://127.0.0.1:1", CONF_PREFIX: "rusthinq"}
    )
    assert bad["errors"] == {"base": "cannot_connect"}
    done = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: rusthinq.url + "/", CONF_PREFIX: "/rusthinq/"}
    )
    assert done["type"] == "create_entry"
    assert done["data"] == {CONF_MODE: MODE_URL, CONF_URL: rusthinq.url, CONF_PREFIX: "rusthinq"}
