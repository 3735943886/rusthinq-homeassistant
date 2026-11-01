"""The ildevice hub comes from the `ildevice-homeassistant` package (`ildevice_ha`), a test requirement in
pyproject.toml pinned like the manifest's."""

import json
from pathlib import Path

import pytest

from fake_rusthinq import FakeRusthinq

FIXTURES = Path(__file__).parent / "fixtures"
AC = "00000000-0000-0000-0000-0000000000ac"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def no_github(monkeypatch):
    """Upstream rusthinq-scripts is checked in the background after setup; tests stay offline unless they stub it."""
    from custom_components.rusthinq import scripts

    async def offline(_http):
        raise scripts.ScriptsError("offline")

    monkeypatch.setattr(scripts, "latest_commit", offline)


@pytest.fixture
async def rusthinq(socket_enabled):
    """The fake listens on 127.0.0.1, which pytest-homeassistant-custom-component's socket guard blocks by default."""
    server = FakeRusthinq()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def air_conditioner(rusthinq: FakeRusthinq) -> FakeRusthinq:
    rusthinq.add_device(AC, load("air_conditioner.json")["publications"])
    return rusthinq
