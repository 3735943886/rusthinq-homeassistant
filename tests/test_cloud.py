"""The LG account (options flow, account sensor) and each device's LG bridge controls."""

import asyncio

import pytest
from conftest import AC, EMBEDDED
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
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


def entity(hass, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


def state(hass, platform: str, unique_id: str):
    eid = entity(hass, platform, unique_id)
    return hass.states.get(eid) if eid else None


async def setup(hass) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MODE_EMBEDDED, data=EMBEDDED)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await eventually(hass, lambda: state(hass, "binary_sensor", f"{entry.entry_id}-lg-account"))
    return entry


async def test_signing_in_and_out_through_the_options(hass, air_conditioner):
    entry = await setup(hass)
    account = f"{entry.entry_id}-lg-account"
    assert state(hass, "binary_sensor", account).state == STATE_OFF

    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert menu["menu_options"] == ["login"]
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "login"})
    assert form["step_id"] == "login"
    paste = await hass.config_entries.options.async_configure(form["flow_id"], {"country": " kr "})
    assert paste["step_id"] == "login_complete"
    assert paste["description_placeholders"]["sign_in_url"].startswith("https://kr.m.lgaccount.com/")
    done = await hass.config_entries.options.async_configure(
        paste["flow_id"], {"url": "https://kr.m.lgaccount.com/login/iabClose?code=abc"})
    assert done["type"] == "create_entry"
    assert air_conditioner.cloud_calls == [("login", {"country": "KR"}),
                                           ("login/complete", {"url": "https://kr.m.lgaccount.com/login/iabClose?code=abc"})]
    await eventually(hass, lambda: state(hass, "binary_sensor", account).state == STATE_ON)
    assert state(hass, "binary_sensor", account).attributes["country"] == "KR"

    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert menu["menu_options"] == ["logout", "refresh"]
    confirm = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "logout"})
    done = await hass.config_entries.options.async_configure(confirm["flow_id"], {})
    assert done["type"] == "create_entry"
    await eventually(hass, lambda: state(hass, "binary_sensor", account).state == STATE_OFF)


async def test_a_refused_sign_in_says_why(hass, air_conditioner):
    entry = await setup(hass)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "login"})
    paste = await hass.config_entries.options.async_configure(form["flow_id"], {"country": "KR"})
    air_conditioner.cloud_failure = (401, "LG rejected the authorization code")
    again = await hass.config_entries.options.async_configure(paste["flow_id"], {"url": "https://x/login/iabClose"})
    assert again["step_id"] == "login_complete"
    assert again["errors"] == {"base": "cloud_failed"}
    assert again["description_placeholders"]["problem"] == "LG rejected the authorization code"


async def test_the_bridge_switch_enables_and_disables(hass, air_conditioner):
    await setup(hass)
    switch = entity(hass, "switch", f"{AC}-rusthinq-bridge-enabled")
    await eventually(hass, lambda: hass.states.get(switch).state == STATE_ON)
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    assert air_conditioner.cloud_calls[-1] == ("bridge/disable", {"device": AC, "incarnation": "1"})
    await eventually(hass, lambda: hass.states.get(switch).state == STATE_OFF)
    assert state(hass, "binary_sensor", f"{AC}-rusthinq-bridge").state == STATE_OFF


async def test_pair_and_unpair(hass, air_conditioner):
    air_conditioner.devices[AC].update(name="거실에어컨")
    air_conditioner.account.update(loggedIn=True, country="KR")
    await setup(hass)
    pair, unpair = (entity(hass, "button", f"{AC}-rusthinq-bridge-{a}") for a in ("pair", "unpair"))
    switch = entity(hass, "switch", f"{AC}-rusthinq-bridge-enabled")
    await eventually(hass, lambda: hass.states.get(unpair).state != STATE_UNAVAILABLE)
    assert hass.states.get(pair).state == STATE_UNAVAILABLE  # already paired

    await hass.services.async_call("button", "press", {"entity_id": unpair}, blocking=True)
    await eventually(hass, lambda: hass.states.get(pair).state != STATE_UNAVAILABLE)
    assert hass.states.get(switch).state == STATE_UNAVAILABLE  # nothing to enable without a pairing

    await hass.services.async_call("button", "press", {"entity_id": pair}, blocking=True)
    assert air_conditioner.cloud_calls[-1] == ("bridge/pair", {"device": AC, "incarnation": "1", "alias": "거실에어컨"})
    await eventually(hass, lambda: hass.states.get(switch).state == STATE_OFF)


async def test_pairing_needs_an_lg_account(hass, air_conditioner):
    air_conditioner.devices[AC].update(bridgePaired=False, bridgeEnabled=False, bridged=False)
    await setup(hass)
    pair = entity(hass, "button", f"{AC}-rusthinq-bridge-pair")
    await eventually(hass, lambda: hass.states.get(pair) is not None)
    assert hass.states.get(pair).state == STATE_UNAVAILABLE


async def test_a_refused_bridge_action_raises(hass, air_conditioner):
    await setup(hass)
    switch = entity(hass, "switch", f"{AC}-rusthinq-bridge-enabled")
    await eventually(hass, lambda: hass.states.get(switch).state == STATE_ON)
    air_conditioner.cloud_failure = (409, "Rejected")
    with pytest.raises(HomeAssistantError, match="LG bridge disable failed: Rejected"):
        await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
