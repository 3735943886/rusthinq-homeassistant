"""Moving an existing rusthinq installation into the embedded daemon.

The source is a rusthinq backup: what the old daemon's `GET /api/backup` serves (fetched here directly, see `fetch`),
its dashboard's *Download backup*, `rusthinq --backup` or `thinqctl backup` write. That is a tar of `config.toml`, the
files it names, and `backup.json`, which records the path each file came from, so every file the configuration names
is found exactly. A hand-made tar archive (gzip or not) of `config.toml` and the files it names still works: without a
`backup.json`, each file is found by its base name.

What moves: the CA (appliances trust it, so a new one would mean provisioning every appliance again), the device
ledger, the LG account and its pairings, and the settings appliances or drivers notice (`hostname`, `legacy_tls`,
`advertise_requested_host`, `[drivers] topic_prefix`, `bridge_dns`), and the listener ports from the `*_bind`
addresses, which the config flow suggests for this host. External MQTT and its retained-topic inventory stay behind,
since Home Assistant is the MQTT consumer now. Files land under `config-0.2.sh`'s names, as the embedded daemon
expects. Nothing already in the data directory is replaced by something different.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import posixpath
import tarfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import aiohttp

from .const import CONF_HTTPS_PORT, CONF_MQTT_PORT, CONF_THINQ1_HTTPS_PORT, CONF_THINQ1_PORT

MAX_MEMBER = 32 << 20
MAX_ARCHIVE = 64 << 20
"""rusthinq's own bound on a backup."""
MANIFEST = "backup.json"
MANIFEST_FORMAT = 1
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=60)
BINDS = {"https_bind": CONF_HTTPS_PORT, "mqtt_bind": CONF_MQTT_PORT, "thinq1_https_bind": CONF_THINQ1_HTTPS_PORT,
         "thinq1_bind": CONF_THINQ1_PORT}


class MigrateError(Exception):
    """The archive cannot be imported; the message says why."""


class FetchError(Exception):
    """A running rusthinq did not hand over its backup. `reason` is a config flow error key, the message the detail."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


@dataclass
class Origin:
    """What the archive says about the installation it came from, for the user to check before importing."""

    hostname: str | None = None
    devices: int | None = None
    """Appliances in the device ledger."""
    account: str | None = None
    """The LG account's country, when it is signed in."""
    rusthinq: str | None = None
    """The version that wrote the backup (only a `backup.json` records it)."""
    created: dt.datetime | None = None


@dataclass
class Imported:
    files: dict[str, bytes]
    """Contents by file name in the data directory."""
    settings: dict[str, Any] = field(default_factory=dict)
    """The old daemon's settings that carry over (keys as in `config.toml`; `topic_prefix` from `[drivers]`)."""
    origin: Origin = field(default_factory=Origin)
    ports: dict[str, int] = field(default_factory=dict)
    """The old listener ports by entry key (`const.DEFAULT_PORTS`'s); a listener without a usable `*_bind` (one the old
    daemon did not run) is left out."""


async def fetch(session: aiohttp.ClientSession, url: str, headers: dict[str, str]) -> bytes:
    """The backup a running rusthinq (0.2.0 or later) serves at `GET /api/backup`."""
    try:
        async with session.get(f"{url}/api/backup", headers=headers, timeout=FETCH_TIMEOUT) as response:
            if response.status == 401:
                raise FetchError("invalid_auth")
            if response.status == 403:
                # rusthinq serves it only behind credentials or on a loopback bind; its message says so
                raise FetchError("backup_refused", await _reason(response))
            if response.status == 404 or response.content_type != "application/x-tar":
                # older daemons have no such route (with the dashboard on, it answers with the dashboard's page)
                raise FetchError("backup_unsupported")
            if response.status != 200:
                raise FetchError("cannot_connect", await _reason(response))
            archive = await response.content.read(MAX_ARCHIVE + 1)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise FetchError("cannot_connect", str(err)) from err
    if len(archive) > MAX_ARCHIVE:
        raise FetchError("cannot_connect", "the backup exceeds 64 MiB")
    return archive


async def _reason(response: aiohttp.ClientResponse) -> str:
    try:
        return str((await response.json(content_type=None)).get("error") or f"HTTP {response.status}")
    except (ValueError, AttributeError, aiohttp.ClientError):
        return f"HTTP {response.status}"


def read_export(archive: bytes) -> Imported:
    members = _members(archive)
    root = _root(members)
    config = _toml(members[f"{root}config.toml"])
    manifest = _manifest(members.get(f"{root}{MANIFEST}"))

    def named(key: str, path: Any, *, required: bool) -> bytes | None:
        if not isinstance(path, str) or not path:
            if required:
                raise MigrateError(f"config.toml has no {key}")
            return None
        if manifest is not None:
            # the manifest's paths are absolute; config.toml's are absolute or relative to its directory
            wanted = posixpath.normpath(posixpath.join(manifest["directory"], path))
            name = manifest["files"].get(wanted)
            data = None if name is None else members.get(f"{root}{name}")
        else:
            data = _by_base_name(members, PurePosixPath(path).name)
        if data is None and required:
            raise MigrateError(f"{PurePosixPath(path).name} ({key} in config.toml) is not in the archive")
        return data

    files = {"ca.key": named("ca_key", config.get("ca_key"), required=True),
             "ca.cert": named("ca_certificate", config.get("ca_certificate"), required=True),
             "devices-v2.json": named("device_ledger", config.get("device_ledger"), required=True)}
    check_ca(files["ca.key"], files["ca.cert"])
    if (account := config.get("cloud_account")) and (data := named("cloud_account", account, required=False)):
        files["cloud-account-v2.json"] = data
        if (pairings := named("cloud_account", f"{account}.pairings", required=False)) is not None:
            files["cloud-account-v2.json.pairings"] = pairings

    settings: dict[str, Any] = {}
    if isinstance(config.get("hostname"), str) and config["hostname"]:
        settings["hostname"] = config["hostname"]
    for key in ("legacy_tls", "advertise_requested_host"):
        if isinstance(config.get(key), bool):
            settings[key] = config[key]
    if isinstance(config.get("bridge_dns"), list) and all(isinstance(v, str) for v in config["bridge_dns"]):
        settings["bridge_dns"] = config["bridge_dns"]
    drivers = config.get("drivers")
    if isinstance(drivers, dict) and isinstance(drivers.get("topic_prefix"), str) and drivers["topic_prefix"]:
        settings["topic_prefix"] = drivers["topic_prefix"]

    origin = Origin(
        hostname=settings.get("hostname"),
        devices=_devices(files["devices-v2.json"]),
        account=_country(files.get("cloud-account-v2.json")),
        rusthinq=manifest["rusthinq"] if manifest else None,
        created=manifest["created"] if manifest else None,
    )
    ports = {key: port for name, key in BINDS.items() if (port := bind_port(config.get(name)))}
    return Imported(files, settings, origin, ports)


def bind_port(address: Any) -> int | None:
    """The port of a `host:port` bind address (`[::]:443` too), or None."""
    if not isinstance(address, str):
        return None
    _, _, port = address.rpartition(":")
    return int(port) if port.isascii() and port.isdigit() and 0 < int(port) <= 65535 else None


def _members(archive: bytes) -> dict[str, bytes]:
    """The regular files by normalized path."""
    if len(archive) > MAX_ARCHIVE:
        raise MigrateError("the archive exceeds 64 MiB")
    members: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                if member.size > MAX_MEMBER:
                    raise MigrateError(f"{member.name} is too large")
                handle = tar.extractfile(member)
                members[posixpath.normpath(member.name).lstrip("/")] = handle.read() if handle else b""
    except (tarfile.TarError, OSError, EOFError) as err:
        raise MigrateError(f"not a tar archive: {err}") from err
    return members


def _root(members: dict[str, bytes]) -> str:
    """The directory in the archive that holds `config.toml` (`""` for a backup; a hand-made archive may have one)."""
    found = [name for name in members if PurePosixPath(name).name == "config.toml"]
    if not found:
        raise MigrateError("the archive has no config.toml")
    if len(found) > 1:
        raise MigrateError("the archive holds more than one config.toml")
    parent = posixpath.dirname(found[0])
    return f"{parent}/" if parent else ""


def _toml(data: bytes) -> dict[str, Any]:
    try:
        return tomllib.loads(data.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise MigrateError(f"config.toml is not valid TOML: {err}") from err


def _manifest(data: bytes | None) -> dict[str, Any] | None:
    """`backup.json`: the format, and archive name by original absolute path."""
    if data is None:
        return None
    try:
        value = json.loads(data)
        if value["format"] != MANIFEST_FORMAT:
            raise MigrateError(f"backup format {value['format']} is not one this integration reads; update it")
        files = {posixpath.normpath(entry["path"]): entry["name"] for entry in value["files"]}
        directory = str(value["directory"])
    except (ValueError, KeyError, TypeError) as err:
        raise MigrateError(f"{MANIFEST} cannot be read: {err!r}") from err
    created = value.get("created")
    return {
        "directory": directory, "files": files,
        "rusthinq": value.get("rusthinq") if isinstance(value.get("rusthinq"), str) else None,
        "created": dt.datetime.fromtimestamp(created, dt.UTC) if isinstance(created, int) and created > 0 else None,
    }


def _by_base_name(members: dict[str, bytes], name: str) -> bytes | None:
    found = [data for path, data in members.items() if PurePosixPath(path).name == name]
    if len(found) > 1:
        raise MigrateError(f"the archive holds more than one {name}")
    return found[0] if found else None


def _devices(ledger: bytes) -> int | None:
    """Appliances in a device ledger; its format is rusthinq's, which checks it on start, so this is a guess."""
    try:
        entries = json.loads(ledger).get("entries")
    except (ValueError, AttributeError):
        return None
    return len(entries) if isinstance(entries, list) else None


def _country(account: bytes | None) -> str | None:
    try:
        country = json.loads(account)["credentials"]["country"] if account else None
    except (ValueError, KeyError, TypeError):
        return None
    return country if isinstance(country, str) and country else None


def check_ca(key_pem: bytes, cert_pem: bytes) -> None:
    """The key must be the certificate's, and the certificate a CA."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
        cert = x509.load_pem_x509_certificate(cert_pem)
    except (ValueError, TypeError) as err:
        raise MigrateError(f"the CA cannot be read: {err}") from err
    der = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if key.public_key().public_bytes(*der) != cert.public_key().public_bytes(*der):
        raise MigrateError("ca.key does not belong to ca.cert")
    try:
        constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        constraints = None
    if constraints is None or not constraints.ca:
        raise MigrateError("ca.cert is not a CA certificate")


SECRET = {"ca.key", "devices-v2.json", "cloud-account-v2.json", "cloud-account-v2.json.pairings"}


def conflicts(directory: Path, imported: Imported) -> list[str]:
    """The files in `directory` the import would change (the same content is fine: importing again)."""
    return sorted(
        name for name, data in imported.files.items()
        if (directory / name).is_file() and (directory / name).read_bytes() != data
    )


def install(directory: Path, imported: Imported) -> None:
    from .embedded import _write

    if clash := conflicts(directory, imported):
        raise MigrateError(f"{directory} already holds a different {', '.join(clash)}")
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in imported.files.items():
        _write(directory / name, data, 0o600 if name in SECRET else 0o644)
