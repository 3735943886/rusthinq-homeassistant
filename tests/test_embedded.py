"""The embedded daemon: the real rusthinq binary from the `rusthinq` wheel, on ephemeral ports in a temporary config
directory. rusthinq-scripts is not downloaded (an empty drivers directory is put in its place), so no devices appear."""

import asyncio
import os
import signal
import socket
import subprocess
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rusthinq import embedded
from custom_components.rusthinq.const import (
    CONF_HTTPS_PORT,
    CONF_MODE,
    CONF_MQTT_PORT,
    CONF_PREFIX,
    CONF_THINQ1_HTTPS_PORT,
    CONF_THINQ1_PORT,
    DOMAIN,
    MODE_EMBEDDED,
)

EPHEMERAL = {CONF_HTTPS_PORT: 0, CONF_MQTT_PORT: 0, CONF_THINQ1_HTTPS_PORT: 0, CONF_THINQ1_PORT: 0}


def test_reset_keeps_the_daemon_and_driver_downloads(config_dir):
    (config_dir / "bin").mkdir()
    (config_dir / "bin" / "rusthinq").write_text("binary")
    (config_dir / "scripts" / "drivers" / "driver.py").write_text("driver")
    (config_dir / "ca.key").write_text("secret")
    (config_dir / "devices-v2.json").write_text("ledger")
    (config_dir / "config.toml").write_text("settings")

    embedded.reset_data(config_dir)

    assert (config_dir / "bin" / "rusthinq").read_text() == "binary"
    assert (config_dir / "scripts" / "drivers" / "driver.py").read_text() == "driver"
    assert {path.name for path in config_dir.iterdir()} == {"bin", "scripts"}


@pytest.fixture(autouse=True)
def _custom(enable_custom_integrations, socket_enabled):
    return


@pytest.fixture
def config_dir(hass, tmp_path) -> Path:
    hass.config.config_dir = str(tmp_path)
    (tmp_path / DOMAIN / "scripts" / "drivers").mkdir(parents=True)
    return tmp_path / DOMAIN


async def eventually(hass, check, timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.05)
            await hass.async_block_till_done()


def running(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


async def setup(hass, **ports) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MODE_EMBEDDED,
                            data={CONF_MODE: MODE_EMBEDDED, CONF_PREFIX: "rusthinq", **EPHEMERAL, **ports})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_the_daemon_runs_with_its_files_and_stops_with_the_entry(hass, config_dir):
    entry = await setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    data = entry.runtime_data
    await eventually(hass, data.client.connected.is_set)
    pid = int((config_dir / "rusthinq.pid").read_text())
    assert running(pid)
    for name in ("ca.key", "ca.cert", "config.toml", "bin/rusthinq", "bin/rusthinqctl"):
        assert (config_dir / name).is_file(), name
    config = (config_dir / "config.toml").read_text()
    assert "external_mqtt" not in config
    assert f'bind = "127.0.0.1:{data.daemon.management_port}"' in config
    assert oct((config_dir / "ca.key").stat().st_mode & 0o777) == "0o600"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not running(pid)
    assert not (config_dir / "rusthinq.pid").exists()


async def test_a_port_in_use_is_named_retried_and_raised_in_repairs(hass, config_dir):
    from homeassistant.helpers import issue_registry as ir

    with socket.socket() as taken:
        taken.bind(("0.0.0.0", 0))
        taken.listen()
        port = taken.getsockname()[1]
        entry = await setup(hass, **{CONF_MQTT_PORT: port})
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert entry.reason == f"port {port} (mqtt) is already in use"
        issue = ir.async_get(hass).async_get_issue(DOMAIN, "ports_unavailable")
        assert issue.translation_placeholders == {"problems": f"port {port} (mqtt) is already in use"}
    # the port is free again: the retry starts the daemon and the issue goes
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, "ports_unavailable") is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_downgrade_is_warned_about(hass, config_dir, caplog):
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".last-version").write_text("9.9.9\n")
    entry = await setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert "runs on data last used by 9.9.9" in caplog.text
    assert (config_dir / ".last-version").read_text().strip() == entry.runtime_data.daemon.version
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_crashed_daemon_is_restarted(hass, config_dir, monkeypatch):
    monkeypatch.setattr(embedded, "RESTART_BACKOFF", (0.1,))
    entry = await setup(hass)
    client = entry.runtime_data.client
    await eventually(hass, client.connected.is_set)
    first = int((config_dir / "rusthinq.pid").read_text())
    os.kill(first, signal.SIGKILL)
    await eventually(hass, lambda: int((config_dir / "rusthinq.pid").read_text()) != first)
    await eventually(hass, client.connected.is_set)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_daemon_left_by_a_previous_start_is_stopped(hass, config_dir):
    entry = await setup(hass)
    await eventually(hass, entry.runtime_data.client.connected.is_set)
    config = entry.runtime_data.daemon.config_path
    assert await hass.config_entries.async_unload(entry.entry_id)
    # what a Home Assistant killed with SIGKILL leaves behind: the daemon, still running and holding its files'
    # locks, and its pidfile
    orphan = await hass.async_add_executor_job(
        lambda: subprocess.Popen([str(config_dir / "bin" / "rusthinq"), str(config)], stderr=subprocess.DEVNULL))
    await asyncio.sleep(0.5)
    (config_dir / "rusthinq.pid").write_text(f"{orphan.pid}\n")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert orphan.wait(timeout=5) == 0
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_half_a_ca_is_refused(tmp_path):
    (tmp_path / "ca.key").write_text("x")
    with pytest.raises(embedded.DaemonError, match="incomplete CA"):
        embedded.ensure_ca(tmp_path)


def test_an_https_port_behind_a_redirect_is_advertised_as_443():
    common = {"mqtt_port": 8883, "thinq1_https_port": 46030, "thinq1_port": 47878, "prefix": "rusthinq",
              "management_port": 40000, "user": "u", "password": 'p"w'}
    assert "https_advertise = 443" in embedded.render_config(https_port=8443, **common)
    assert "https_advertise" not in embedded.render_config(https_port=443, **common)
    assert 'password = "p\\"w"' in embedded.render_config(https_port=443, **common)


async def test_config_flow_checks_ports_and_allows_one_embedded_entry(hass, config_dir):
    with socket.socket() as taken:
        taken.bind(("0.0.0.0", 0))
        taken.listen()
        port = taken.getsockname()[1]
        menu = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": MODE_EMBEDDED})
        assert result["step_id"] == "embedded"
        bad = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HTTPS_PORT: port, CONF_MQTT_PORT: port + 1 if port < 65535 else port - 1}
        )
    assert bad["errors"] == {"base": "ports_unavailable"}
    assert f"port {port} (https) is already in use" in bad["description_placeholders"]["problems"]
    hass.config_entries.flow.async_abort(result["flow_id"])

    MockConfigEntry(domain=DOMAIN, unique_id=MODE_EMBEDDED, data={}).add_to_hass(hass)
    menu = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    again = await hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": MODE_EMBEDDED})
    assert again["type"] == "abort" and again["reason"] == "already_configured"
