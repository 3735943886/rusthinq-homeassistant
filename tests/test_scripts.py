"""rusthinq-scripts: installing and updating the drivers, and the update entity, against the real daemon."""

import asyncio
import gzip
import io
import json
import tarfile
from pathlib import Path

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rusthinq import scripts
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

OLD, NEW = "a" * 40, "b" * 40


def tarball(files: dict[str, str]) -> bytes:
    """A GitHub-style tarball: everything under one top directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"rusthinq-scripts-{NEW[:7]}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue())


OLD_TREE = {"VERSION": f"{OLD} 2026-10-31\n", "drivers/A.rhai": "// a", "drivers/B.rhai": "// b",
            "modules/gone/x.rhai": "// x", "modules/common.rhai": "// c"}
NEW_TREE = {"VERSION": f"{NEW} 2026-11-01\n", "drivers/A.rhai": "// a", "drivers/B.rhai": "// b2",
            "drivers/C.rhai": "// c", "modules/common.rhai": "// c"}


def tree(directory: Path) -> dict[str, str]:
    return {str(p.relative_to(directory)): p.read_text() for p in sorted(directory.rglob("*")) if p.is_file()}


def test_the_first_install_moves_the_tree_in(tmp_path):
    scripts.install(tmp_path, tarball(OLD_TREE))
    assert tree(tmp_path / "scripts") == OLD_TREE
    assert [p.name for p in tmp_path.iterdir()] == ["scripts"]
    assert scripts.installed_commit(tmp_path) == OLD


def test_an_update_changes_files_inside_the_watched_directory(tmp_path):
    scripts.install(tmp_path, tarball(OLD_TREE))
    directory = tmp_path / "scripts"
    inode, unchanged = directory.stat().st_ino, (directory / "drivers/A.rhai").stat().st_ino
    scripts.install(tmp_path, tarball(NEW_TREE))
    assert tree(directory) == NEW_TREE
    assert directory.stat().st_ino == inode  # the daemon keeps watching it
    assert (directory / "drivers/A.rhai").stat().st_ino == unchanged
    assert not (directory / "modules/gone").exists()
    assert scripts.installed_commit(tmp_path) == NEW


def test_a_download_without_drivers_changes_nothing(tmp_path):
    scripts.install(tmp_path, tarball(OLD_TREE))
    with pytest.raises(scripts.ScriptsError, match="no drivers"):
        scripts.install(tmp_path, tarball({"README.md": "?"}))
    assert tree(tmp_path / "scripts") == OLD_TREE
    assert [p.name for p in tmp_path.iterdir()] == ["scripts"]


def test_an_unsubstituted_version_is_no_commit(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "VERSION").write_text("$Format:%H %cs$\n")
    assert scripts.installed_commit(tmp_path) is None
    assert scripts.installed_commit(tmp_path / "missing") is None


@pytest.fixture
def embedded_entry(hass, tmp_path, socket_enabled, enable_custom_integrations, monkeypatch):
    hass.config.config_dir = str(tmp_path)
    scripts.install(tmp_path / DOMAIN, tarball(OLD_TREE))
    downloads = []

    async def latest(_http):
        return NEW

    async def download(_http, ref=scripts.BRANCH):
        downloads.append(ref)
        return tarball(NEW_TREE)

    monkeypatch.setattr(scripts, "latest_commit", latest)
    monkeypatch.setattr(scripts, "download", download)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MODE_EMBEDDED, data={
        CONF_MODE: MODE_EMBEDDED, CONF_PREFIX: "rusthinq", CONF_HTTPS_PORT: 0, CONF_MQTT_PORT: 0,
        CONF_THINQ1_HTTPS_PORT: 0, CONF_THINQ1_PORT: 0})
    entry.add_to_hass(hass)
    entry.downloads = downloads
    return entry


async def daemon_source(entry) -> dict:
    daemon = entry.runtime_data.daemon
    async with aiohttp.ClientSession() as http, http.get(f"{daemon.url}/api/diagnostics",
                                                          headers=daemon.headers) as response:
        return json.loads(await response.text())["drivers"]["source"]


async def test_the_update_entity_installs_upstream_and_the_daemon_reloads(hass, embedded_entry):
    entry = embedded_entry
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get("update.rusthinq_drivers")
    assert state.state == "on"
    assert state.attributes["installed_version"] == OLD[:7]
    assert state.attributes["latest_version"] == NEW[:7]
    assert state.attributes["release_url"] == scripts.compare_url(OLD, NEW)
    assert (await daemon_source(entry))["commit"] == OLD
    pid = entry.runtime_data.daemon._proc.pid

    await hass.services.async_call("update", "install", {"entity_id": "update.rusthinq_drivers"}, blocking=True)
    assert entry.downloads == [NEW]
    assert hass.states.get("update.rusthinq_drivers").state == "off"
    async with asyncio.timeout(5):
        while (await daemon_source(entry))["commit"] != NEW:
            await asyncio.sleep(0.1)
    assert entry.runtime_data.daemon._proc.pid == pid  # reloaded, not restarted
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_url_entry_has_no_update_entity(hass, enable_custom_integrations, air_conditioner):
    from custom_components.rusthinq.const import CONF_URL, MODE_URL

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_MODE: MODE_URL, CONF_URL: air_conditioner.url,
                                                 CONF_PREFIX: "rusthinq"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.async_entity_ids("update") == []
    assert await hass.config_entries.async_unload(entry.entry_id)
