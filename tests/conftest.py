"""The ildevice hub comes from the `ildevice-homeassistant` package (`ildevice_ha`), a test requirement in
pyproject.toml pinned like the manifest's."""

import json
from pathlib import Path

import pytest
from fake_rusthinq import FakeRusthinq

from custom_components import rusthinq as integration
from custom_components.rusthinq.const import CONF_MODE, CONF_PREFIX, MODE_EMBEDDED

FIXTURES = Path(__file__).parent / "fixtures"
AC = "00000000-0000-0000-0000-0000000000ac"
EMBEDDED = {CONF_MODE: MODE_EMBEDDED, CONF_PREFIX: "rusthinq"}
"""The data of an embedded entry; with the `rusthinq` fixture, the fake stands in for its daemon."""


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def no_github(monkeypatch):
    """Upstream rusthinq-scripts is checked in the background after setup; tests stay offline unless they stub it."""
    from custom_components.rusthinq import scripts

    async def offline(_http):
        raise scripts.ScriptsError("offline")

    monkeypatch.setattr(scripts, "latest_commit", offline)


class StubDaemon:
    """Stands in for `EmbeddedDaemon`: the entry talks to the fake instead of a daemon of its own."""

    version = "0.2.2-rc4"
    lines = ()

    def __init__(self, url: str) -> None:
        self.url = url
        self.headers: dict[str, str] = {}

    async def async_start(self) -> None:
        pass

    async def async_stop(self) -> None:
        pass


@pytest.fixture
async def old_rusthinq(socket_enabled):
    """A rusthinq running elsewhere. The fake listens on 127.0.0.1, which pytest-homeassistant-custom-component's
    socket guard blocks by default."""
    server = FakeRusthinq()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def rusthinq(old_rusthinq: FakeRusthinq, monkeypatch) -> FakeRusthinq:
    """The fake as the embedded daemon of the entries set up in the test."""
    monkeypatch.setattr(integration, "EmbeddedDaemon", lambda hass, **_: StubDaemon(old_rusthinq.url))
    return old_rusthinq


@pytest.fixture
def air_conditioner(rusthinq: FakeRusthinq) -> FakeRusthinq:
    rusthinq.add_device(AC, load("air_conditioner.json")["publications"])
    return rusthinq
