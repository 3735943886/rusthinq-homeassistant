"""Moving an existing installation in: reading its export, refusing what does not fit, and the daemon running on it."""

import contextlib
import datetime as dt
import io
import json
import subprocess
import tarfile
import time
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.rusthinq import embedded, migrate
from custom_components.rusthinq.const import (
    CONF_ARCHIVE,
    CONF_HTTPS_PORT,
    CONF_MQTT_PORT,
    CONF_PREFIX,
    CONF_SETTINGS,
    CONF_THINQ1_HTTPS_PORT,
    CONF_THINQ1_PORT,
    DOMAIN,
)

# r5c's configuration, as rusthinq's 0.2 installs write it: absolute CA paths, other names than config-0.2.sh's
CONFIG = """advertise_requested_host = true
ca_certificate = "/mnt/sdcard/data/git/rusthinq-0.2/ca.cert"
ca_key = "/mnt/sdcard/data/git/rusthinq-0.2/ca.key"
cloud_account = "account.json"
device_ledger = "devices.json"
hostname = "rusthinq.local"
https_advertise = 443
https_bind = "0.0.0.0:8443"
legacy_tls = true
mqtt_bind = "0.0.0.0:8883"

[drivers]
directory = "/mnt/sdcard/data/git/rusthinq-0.2/rusthinq-scripts"
topic_prefix = "rusthinq"
watch = true

[external_mqtt]
host = "localhost"
inventory = "/mnt/sdcard/data/git/rusthinq-0.2/retained-import.json"
port = 1883
tls = false

[management]
bind = "0.0.0.0:44401"
gui = true
"""


@pytest.fixture
def ca(tmp_path_factory) -> tuple[bytes, bytes]:
    directory = tmp_path_factory.mktemp("ca")
    embedded.ensure_ca(directory)
    return (directory / "ca.key").read_bytes(), (directory / "ca.cert").read_bytes()


def export(files: dict[str, bytes], prefix: str = "rusthinq-0.2/") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def backup(config_dir: str, files: dict[str, tuple[str, bytes]], *, fmt: int = 1) -> bytes:
    """A backup as rusthinq 0.2.0 writes it (`backup.rs`): an uncompressed tar of config.toml, backup.json and each
    state file under the name it gives it, with the absolute path it came from."""
    manifest = {"format": fmt, "rusthinq": "0.2.0", "created": 1793500000, "config": f"{config_dir}/config.toml",
                "directory": config_dir,
                "files": [{"name": name, "path": path} for name, (path, _) in files.items() if name != "config.toml"]}
    entries = {"config.toml": files["config.toml"][1], "backup.json": json.dumps(manifest).encode(),
               **{name: data for name, (_, data) in files.items() if name != "config.toml"}}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def old_backup(ca, ledger) -> bytes:
    """r5c's installation as its dashboard backs it up: the CA outside the configuration's directory (archived by file
    name), the device ledger in a subdirectory, a signed-in LG account."""
    key, cert = ca
    config = CONFIG.replace('device_ledger = "devices.json"', 'device_ledger = "state/devices.json"')
    here, elsewhere = "/opt/rusthinq", "/mnt/sdcard/data/git/rusthinq-0.2"
    return backup(here, {
        "config.toml": (f"{here}/config.toml", config.encode()),
        "ca.key": (f"{elsewhere}/ca.key", key),
        "ca.cert": (f"{elsewhere}/ca.cert", cert),
        "state/devices.json": (f"{here}/state/devices.json", ledger),
        "account.json": (f"{here}/account.json", b'{"schema":2,"credentials":{"country":"KR","refresh":"r"}}'),
        "account.json.pairings": (f"{here}/account.json.pairings", b"[]"),
        "retained-import.json": (f"{elsewhere}/retained-import.json", b"{}"),
    })


@pytest.fixture(scope="module")
def ledger(tmp_path_factory) -> bytes:
    """A device ledger as the real daemon writes it (its format is rusthinq's business; the daemon checks it)."""
    import rusthinq

    directory = tmp_path_factory.mktemp("ledger")
    embedded.ensure_ca(directory)
    (directory / "config.toml").write_text(
        'hostname = "t"\nca_key = "ca.key"\nca_certificate = "ca.cert"\ndevice_ledger = "devices.json"\n'
    )
    daemon = subprocess.Popen([str(rusthinq.binary_path()), "config.toml"], cwd=directory,
                              stderr=subprocess.DEVNULL)
    for _ in range(50):
        if (directory / "devices.json").stat().st_size if (directory / "devices.json").exists() else 0:
            break
        time.sleep(0.1)
    daemon.terminate()
    daemon.wait(5)
    return (directory / "devices.json").read_bytes()


@pytest.fixture
def old_install(ca, ledger) -> dict[str, bytes]:
    """An installation that never signed in to LG: its account.json does not exist yet."""
    key, cert = ca
    return {"config.toml": CONFIG.encode(), "ca.key": key, "ca.cert": cert, "devices.json": ledger,
            "retained-import.json": b"{}"}


def test_an_export_maps_to_the_embedded_names_and_keeps_the_settings(old_install):
    old_install.update({"account.json": b"{}", "account.json.pairings": b"[]"})
    imported = migrate.read_export(export(old_install))
    assert imported.files == {
        "ca.key": old_install["ca.key"], "ca.cert": old_install["ca.cert"],
        "devices-v2.json": old_install["devices.json"], "cloud-account-v2.json": old_install["account.json"],
        "cloud-account-v2.json.pairings": old_install["account.json.pairings"],
    }
    assert imported.settings == {"hostname": "rusthinq.local", "legacy_tls": True, "advertise_requested_host": True,
                                 "topic_prefix": "rusthinq"}
    assert imported.ports == {CONF_HTTPS_PORT: 8443, CONF_MQTT_PORT: 8883}  # no ThinQ1 listeners on r5c


@pytest.mark.parametrize(("address", "port"), [("0.0.0.0:8443", 8443), ("[::]:443", 443), ("0.0.0.0:0", None),
                                               ("0.0.0.0", None), ("x:99999", None), (8443, None)])
def test_bind_port(address, port):
    assert migrate.bind_port(address) == port


def test_a_backup_is_read_through_its_manifest_and_says_where_it_came_from(old_backup, ca, ledger):
    imported = migrate.read_export(old_backup)
    assert imported.files == {
        "ca.key": ca[0], "ca.cert": ca[1], "devices-v2.json": ledger,
        "cloud-account-v2.json": b'{"schema":2,"credentials":{"country":"KR","refresh":"r"}}',
        "cloud-account-v2.json.pairings": b"[]",
    }
    assert imported.settings["hostname"] == "rusthinq.local"
    assert imported.origin == migrate.Origin(
        hostname="rusthinq.local", devices=0, account="KR", rusthinq="0.2.0",
        created=dt.datetime.fromtimestamp(1793500000, dt.UTC),
    )


def test_a_backup_follows_its_manifest_not_base_names(ca, ledger):
    """A file the manifest does not list is missing, even when the archive holds one of that name elsewhere."""
    key, cert = ca
    here = "/opt/rusthinq"
    archive = backup(here, {
        "config.toml": (f"{here}/config.toml", CONFIG.replace("/mnt/sdcard/data/git/rusthinq-0.2", here).encode()),
        "ca.key": (f"{here}/ca.key", key),
        "ca.cert": (f"{here}/ca.cert", cert),
        "old/devices.json": (f"{here}/old/devices.json", ledger),
    })
    with pytest.raises(migrate.MigrateError, match=r"devices\.json \(device_ledger in config\.toml\)"):
        migrate.read_export(archive)


def test_a_backup_of_a_newer_format_is_refused(ca):
    here = "/opt/rusthinq"
    archive = backup(here, {"config.toml": (f"{here}/config.toml", CONFIG.encode())}, fmt=2)
    with pytest.raises(migrate.MigrateError, match="backup format 2"):
        migrate.read_export(archive)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda f: f.pop("config.toml"), "no config.toml"),
        (lambda f: f.pop("ca.key"), r"ca\.key \(ca_key in config\.toml\) is not in the archive"),
        (lambda f: f.pop("devices.json"), "devices.json"),
        (lambda f: f.update({"ca.cert": f["ca.cert"].replace(b"A", b"B", 1)}), "CA cannot be read|does not belong"),
        (lambda f: f.update({"config.toml": b"not = [toml"}), "not valid TOML"),
    ],
)
def test_an_incomplete_or_broken_export_is_refused(old_install, change, message):
    change(old_install)
    with pytest.raises(migrate.MigrateError, match=message):
        migrate.read_export(export(old_install))


def test_a_key_of_another_ca_is_refused(old_install, tmp_path):
    embedded.ensure_ca(tmp_path)
    old_install["ca.key"] = (tmp_path / "ca.key").read_bytes()
    with pytest.raises(migrate.MigrateError, match="does not belong"):
        migrate.read_export(export(old_install))


def test_two_files_of_one_name_are_refused(old_install):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, data in [*old_install.items(), ("backup/ca.key", b"x")]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(migrate.MigrateError, match="more than one ca.key"):
        migrate.read_export(archive.getvalue())


def test_a_different_installation_already_there_is_kept(old_install, tmp_path):
    embedded.ensure_ca(tmp_path)
    imported = migrate.read_export(export(old_install))
    assert migrate.conflicts(tmp_path, imported) == ["ca.cert", "ca.key"]
    with pytest.raises(migrate.MigrateError, match="already holds a different ca.cert, ca.key"):
        migrate.install(tmp_path, imported)
    fresh = tmp_path / "fresh"
    migrate.install(fresh, imported)
    migrate.install(fresh, imported)  # the same again is fine
    assert oct((fresh / "ca.key").stat().st_mode & 0o777) == "0o600"


def test_imported_settings_reach_the_daemon_configuration():
    config = embedded.render_config(
        https_port=443, mqtt_port=8883, thinq1_https_port=46030, thinq1_port=47878, prefix="rusthinq",
        management_port=40000, user="u", password="p",
        settings={"hostname": "rusthinq.local", "legacy_tls": False, "bridge_dns": []},
    )
    assert 'hostname = "rusthinq.local"' in config
    assert "legacy_tls = false" in config
    assert "advertise_requested_host = true" in config
    assert "bridge_dns = []" in config


def suggested(form) -> dict[str, int]:
    return {str(key): key.description["suggested_value"] for key in form["data_schema"].schema}


async def test_the_migrate_flow_installs_the_export_and_the_daemon_runs_on_it(
    hass, tmp_path, old_install, enable_custom_integrations, socket_enabled, monkeypatch
):
    hass.config.config_dir = str(tmp_path)
    (tmp_path / DOMAIN / "scripts" / "drivers").mkdir(parents=True)
    upload = tmp_path / "upload.tar.gz"
    upload.write_bytes(export(old_install))

    file_id = "9b2c4f5e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"

    @contextlib.contextmanager
    def uploaded(_hass, given):
        assert given == file_id
        yield upload

    monkeypatch.setattr("custom_components.rusthinq.config_flow.process_uploaded_file", uploaded)

    menu = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    menu = await hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": "migrate"})
    assert menu["step_id"] == "migrate"
    form = await hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": "migrate_upload"})
    assert form["step_id"] == "migrate_upload"
    confirm = await hass.config_entries.flow.async_configure(form["flow_id"], {CONF_ARCHIVE: file_id})
    assert confirm["step_id"] == "migrate_confirm", confirm
    assert confirm["description_placeholders"]["hostname"] == "rusthinq.local"
    assert confirm["description_placeholders"]["rusthinq"] == "–"  # a hand-made export does not say
    # r5c's ports are suggested; ThinQ1, which it does not run, at the defaults
    assert suggested(confirm) == {CONF_HTTPS_PORT: 8443, CONF_MQTT_PORT: 8883, CONF_THINQ1_HTTPS_PORT: 46030,
                                  CONF_THINQ1_PORT: 47878}
    https, mqtt = embedded.free_local_port(), embedded.free_local_port()
    done = await hass.config_entries.flow.async_configure(
        confirm["flow_id"], {**suggested(confirm), CONF_HTTPS_PORT: https, CONF_MQTT_PORT: mqtt}
    )
    assert done["type"] == "create_entry", done
    assert done["data"][CONF_PREFIX] == "rusthinq"
    assert done["data"][CONF_SETTINGS] == {"hostname": "rusthinq.local", "legacy_tls": True,
                                           "advertise_requested_host": True}
    directory = Path(tmp_path / DOMAIN)
    assert (directory / "ca.key").read_bytes() == old_install["ca.key"]
    assert (directory / "devices-v2.json").exists()  # the daemon has started on it and reserved generations since

    # the ThinQ1 listeners keep their fixed ports in the entry; ephemeral ones here, so the test runs anywhere
    entry = hass.config_entries.async_get_entry(done["result"].entry_id)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_THINQ1_HTTPS_PORT: 0, CONF_THINQ1_PORT: 0})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED, entry.reason
    assert 'hostname = "rusthinq.local"' in (directory / "config.toml").read_text()
    assert (directory / "ca.key").read_bytes() == old_install["ca.key"]  # kept, not regenerated
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_backup_is_fetched_from_the_old_daemon_which_must_be_stopped_before_it_is_installed(
    hass, tmp_path, old_backup, rusthinq, enable_custom_integrations, monkeypatch
):
    hass.config.config_dir = str(tmp_path)
    monkeypatch.setattr("custom_components.rusthinq.async_setup_entry", _set_up)
    menu = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    menu = await hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": "migrate"})
    form = await hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": "migrate_fetch"})
    assert form["step_id"] == "migrate_fetch"
    source = {"url": rusthinq.url + "/", "username": "admin", "password": "secret"}

    form = await hass.config_entries.flow.async_configure(form["flow_id"], source)
    assert form["errors"] == {"base": "backup_unsupported"}  # an older daemon answers with its dashboard

    rusthinq.backup_refusal = "set [management] user and password"
    form = await hass.config_entries.flow.async_configure(form["flow_id"], source)
    assert form["errors"] == {"base": "backup_refused"}
    assert form["description_placeholders"]["problems"] == "set [management] user and password"

    rusthinq.backup_refusal, rusthinq.backup = None, old_backup
    confirm = await hass.config_entries.flow.async_configure(form["flow_id"], source)
    assert confirm["step_id"] == "migrate_confirm", confirm
    assert {k: confirm["description_placeholders"][k] for k in ("hostname", "devices", "account", "rusthinq")} == {
        "hostname": "rusthinq.local", "devices": "0", "account": "KR", "rusthinq": "0.2.0"}

    ports = {**suggested(confirm), CONF_HTTPS_PORT: embedded.free_local_port(),
             CONF_MQTT_PORT: embedded.free_local_port()}
    again = await hass.config_entries.flow.async_configure(confirm["flow_id"], ports)
    assert again["errors"] == {"base": "source_running"}
    assert not (tmp_path / DOMAIN / "ca.key").exists()

    await rusthinq.stop()
    done = await hass.config_entries.flow.async_configure(confirm["flow_id"], ports)
    assert done["type"] == "create_entry", done
    assert done["data"][CONF_PREFIX] == "rusthinq"
    assert done["data"][CONF_SETTINGS]["hostname"] == "rusthinq.local"
    assert (tmp_path / DOMAIN / "cloud-account-v2.json").exists()
    assert (tmp_path / DOMAIN / "devices-v2.json").stat().st_mode & 0o777 == 0o600


async def test_fetching_needs_the_management_credentials(hass, rusthinq, enable_custom_integrations, monkeypatch):
    async def unauthorized(_request):
        from aiohttp import web

        return web.json_response({"error": "unauthorized"}, status=401)

    monkeypatch.setattr(rusthinq, "_backup", unauthorized)
    rusthinq.backup = b""
    await rusthinq.stop()
    await rusthinq.start()
    menu = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    menu = await hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": "migrate"})
    form = await hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": "migrate_fetch"})
    form = await hass.config_entries.flow.async_configure(form["flow_id"], {"url": rusthinq.url})
    assert form["errors"] == {"base": "invalid_auth"}
    form = await hass.config_entries.flow.async_configure(form["flow_id"], {"url": "http://127.0.0.1:9"})
    assert form["errors"] == {"base": "cannot_connect"}


async def _set_up(hass, entry):
    return True
