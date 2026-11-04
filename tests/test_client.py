"""The management client against a fake rusthinq, with ildevice's in-process transport and no Home Assistant."""

import asyncio
import json

import aiohttp
import pytest
from conftest import AC
from ildevice_ha.core.memory import InProcessTransport

from custom_components.rusthinq.client import ManagementClient

PREFIX = "rusthinq"
TOPIC = f"{PREFIX}/{AC}"


async def eventually(check, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


def retained(transport: InProcessTransport, topic: str):
    msg = transport.retained.get(topic)
    return None if msg is None else msg.payload


@pytest.fixture
async def client(rusthinq):
    transport = InProcessTransport()
    async with aiohttp.ClientSession() as http:
        client = ManagementClient(http, rusthinq.url, transport, PREFIX, backoff=(0.05,))
        client.transport = transport
        await client.start()
        yield client
        await client.stop()


async def test_presentation_is_replayed_as_retained_and_presence_goes_online(air_conditioner, client):
    transport = client.transport
    await eventually(lambda: retained(transport, f"{TOPIC}/target") == "18")
    descriptor = json.loads(retained(transport, f"{TOPIC}/descriptor"))
    assert descriptor["source"] == "rusthinq"
    assert retained(transport, client.presence_topic) == "online"


async def test_live_output_is_forwarded(air_conditioner, client):
    transport = client.transport
    await eventually(lambda: retained(transport, f"{TOPIC}/target") == "18")
    await air_conditioner.output(AC, f"{TOPIC}/target", "22")
    await eventually(lambda: retained(transport, f"{TOPIC}/target") == "22")


async def test_a_live_value_is_not_overwritten_by_an_older_presentation(rusthinq):
    from conftest import load

    rusthinq.add_device(AC, load("air_conditioner.json")["publications"])
    rusthinq.presentation_gate = asyncio.Event()
    transport = InProcessTransport()
    async with aiohttp.ClientSession() as http:
        client = ManagementClient(http, rusthinq.url, transport, PREFIX)
        await client.start()
        await eventually(lambda: rusthinq.sockets)
        await rusthinq.output(AC, f"{TOPIC}/target", "25")
        await eventually(lambda: retained(transport, f"{TOPIC}/target") == "25")
        rusthinq.presentation_gate.set()
        await eventually(lambda: retained(transport, f"{TOPIC}/mode") == "fan_only")
        assert retained(transport, f"{TOPIC}/target") == "25"
        await client.stop()


async def test_a_command_invokes_the_driver_with_the_current_session(air_conditioner, client):
    await eventually(lambda: client.connected.is_set())
    await client.transport.publish(f"{TOPIC}/target/set", "20")
    await eventually(lambda: air_conditioner.invokes)
    device, body = air_conditioner.invokes[0]
    assert device == AC
    assert body == {"incarnation": "1", "generation": "100", "script_generation": "1", "function": "__command",
                    "input": json.dumps({"prop": "target", "value": "20"})}


async def test_a_stale_session_is_refreshed_and_the_command_retried_once(air_conditioner, client):
    await eventually(lambda: client.connected.is_set())
    air_conditioner.reconnect(AC)
    assert await client.command(AC, "power", "true")
    assert [body["generation"] for _, body in air_conditioner.invokes] == ["100", "101"]


async def rejects(transport: InProcessTransport) -> list[dict]:
    """The IL rejects published from now on."""
    seen: list[dict] = []
    await transport.subscribe(f"{PREFIX}/+/reject", lambda msg: seen.append({"topic": msg.topic,
                                                                             **json.loads(msg.payload)}))
    return seen


async def test_a_command_for_an_offline_device_is_rejected(rusthinq, client):
    rusthinq.add_device("washer", [], online=False)
    seen = await rejects(client.transport)
    await eventually(lambda: client.connected.is_set())
    assert not await client.command("washer", "power", "true")
    assert rusthinq.invokes == []
    assert seen == [{"topic": f"{PREFIX}/washer/reject", "prop": "power", "code": "unavailable",
                     "reason": "the device is offline"}]


async def test_a_command_rusthinq_refuses_is_rejected(air_conditioner, client):
    seen = await rejects(client.transport)
    await eventually(lambda: client.connected.is_set())
    air_conditioner.cloud_failure = (429, "queue full")
    assert not await client.command(AC, "power", "true")
    assert [(r["topic"], r["prop"], r["code"]) for r in seen] == [(f"{TOPIC}/reject", "power", "unavailable")]
    assert "429" in seen[0]["reason"] and "queue full" in seen[0]["reason"]


async def test_foreign_set_topics_are_not_commands(air_conditioner, client):
    await eventually(lambda: client.connected.is_set())
    await client.transport.publish(f"other/{AC}/target/set", "20")
    await client.transport.publish(f"{TOPIC}/a/b/set", "20")
    await asyncio.sleep(0.1)
    assert air_conditioner.invokes == []


async def test_a_retained_command_replayed_on_subscribe_is_not_executed(air_conditioner):
    transport = InProcessTransport()
    await transport.publish(f"{TOPIC}/target/set", "20", retain=True)
    async with aiohttp.ClientSession() as http:
        client = ManagementClient(http, air_conditioner.url, transport, PREFIX)
        await client.start()
        await eventually(lambda: client.connected.is_set())
        await asyncio.sleep(0.1)
        await client.stop()
    assert air_conditioner.invokes == []


async def test_presence_follows_the_event_stream(air_conditioner, client):
    transport = client.transport
    await eventually(lambda: retained(transport, client.presence_topic) == "online")
    await air_conditioner.drop_sockets()
    await eventually(lambda: retained(transport, client.presence_topic) == "offline")
    await eventually(lambda: retained(transport, client.presence_topic) == "online")
    assert air_conditioner.connections == 2


def label(transport: InProcessTransport) -> str:
    return json.loads(retained(transport, f"{TOPIC}/descriptor"))["label"]


async def test_the_descriptor_carries_rusthinqs_name_for_the_device(rusthinq):
    from conftest import load

    rusthinq.add_device(AC, load("air_conditioner.json")["publications"], name="거실에어컨")
    transport = InProcessTransport()
    async with aiohttp.ClientSession() as http:
        client = ManagementClient(http, rusthinq.url, transport, PREFIX)
        await client.start()
        await eventually(lambda: retained(transport, f"{TOPIC}/descriptor"))
        assert label(transport) == "거실에어컨"
        await client.stop()


async def test_without_a_name_the_drivers_label_stays(air_conditioner, client):
    await eventually(lambda: retained(client.transport, f"{TOPIC}/descriptor"))
    assert label(client.transport) == "Air conditioner"


async def test_a_name_that_appears_later_is_passed_on(air_conditioner, client, monkeypatch):
    transport = client.transport
    await eventually(lambda: retained(transport, f"{TOPIC}/descriptor"))
    assert label(transport) == "Air conditioner"
    air_conditioner.devices[AC]["name"] = "안방에어컨"
    await air_conditioner.send({"type": "stateChanged", "detail": "names"})
    await eventually(lambda: label(transport) == "안방에어컨", timeout=5)
    # a later descriptor from the driver keeps it
    descriptor = json.loads(retained(transport, f"{TOPIC}/descriptor"))
    await air_conditioner.output(AC, f"{TOPIC}/descriptor", json.dumps({**descriptor, "label": "Air conditioner"}))
    await asyncio.sleep(0.1)
    assert label(transport) == "안방에어컨"
