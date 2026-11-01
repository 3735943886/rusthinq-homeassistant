"""Retained publications kept across restarts: an appliance that is off keeps its entities, unavailable."""

import asyncio
import json

import pytest
from conftest import AC, load
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rusthinq import retained as retained_module
from custom_components.rusthinq.const import CONF_PREFIX, CONF_URL, DOMAIN


@pytest.fixture(autouse=True)
def _custom(enable_custom_integrations, monkeypatch):
    monkeypatch.setattr(retained_module, "SAVE_DELAY", 0)


async def eventually(hass, check, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.02)
            await hass.async_block_till_done()


def climate(hass):
    registry = er.async_get(hass)
    entity_id = next((e.entity_id for e in registry.entities.values() if e.platform == DOMAIN and e.domain == "climate"),
                     None)
    return hass.states.get(entity_id) if entity_id else None


def stored(hass_storage, entry) -> dict:
    return (hass_storage.get(f"{DOMAIN}.{entry.entry_id}.retained") or {}).get("data") or {}


async def test_an_appliance_that_is_off_keeps_its_entities_across_a_restart(hass, hass_storage, air_conditioner):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: air_conditioner.url, CONF_PREFIX: "rusthinq"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await eventually(hass, lambda: climate(hass) and climate(hass).state != STATE_UNAVAILABLE)
    await eventually(hass, lambda: f"rusthinq/{AC}/descriptor" in stored(hass_storage, entry))
    assert stored(hass_storage, entry)[f"rusthinq/{AC}/available"] == "true"

    assert await hass.config_entries.async_unload(entry.entry_id)
    air_conditioner.devices[AC].update(online=False, generation=None, scriptGeneration=None, mapped=False)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await eventually(hass, lambda: climate(hass) is not None)
    assert climate(hass).state == STATE_UNAVAILABLE
    assert not climate(hass).attributes.get("restored")  # the entity itself, not the registry's placeholder

    air_conditioner.devices[AC].update(online=True, generation="101", scriptGeneration="1", mapped=True)
    await air_conditioner.drop_sockets()  # rusthinq sees it again: a new snapshot, and its presentation
    await eventually(hass, lambda: climate(hass).state != STATE_UNAVAILABLE)


async def test_devices_rusthinq_forgot_are_not_kept(hass, hass_storage, air_conditioner):
    gone = "00000000-0000-0000-0000-00000000dead"
    descriptor = json.loads(next(p["payload"] for p in load("air_conditioner.json")["publications"]
                                 if p["topic"].endswith("/descriptor")))
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: air_conditioner.url, CONF_PREFIX: "rusthinq"})
    hass_storage[f"{DOMAIN}.{entry.entry_id}.retained"] = {"version": 1, "minor_version": 1, "key": "x", "data": {
        f"rusthinq/{gone}/descriptor": json.dumps({**descriptor, "id": gone}),
        f"rusthinq/{gone}/available": "true",
    }}
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await eventually(hass, lambda: f"rusthinq/{AC}/descriptor" in stored(hass_storage, entry))
    assert not [t for t in stored(hass_storage, entry) if gone in t]
