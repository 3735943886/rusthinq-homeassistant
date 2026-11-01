"""Moving an existing rusthinq installation into the embedded daemon.

The export is a tar archive (gzip or not) of the old installation's `config.toml` and the files it names. Paths in the
configuration may be absolute or relative to it, so each file is found in the archive by its base name. What moves:
the CA (appliances trust it, so a new one would mean provisioning every appliance again), the device ledger, the LG
account and its pairings, and the settings appliances or drivers notice (`hostname`, `legacy_tls`,
`advertise_requested_host`, `[drivers] topic_prefix`, `bridge_dns`). Listener ports are chosen anew for this host.
Files land under `config-0.2.sh`'s names, as the embedded daemon expects. Nothing already in the data directory is
replaced by something different.
"""

from __future__ import annotations

import io
import tarfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

MAX_MEMBER = 32 << 20


class MigrateError(Exception):
    """The archive cannot be imported; the message says why."""


@dataclass
class Imported:
    files: dict[str, bytes]
    """Contents by file name in the data directory."""
    settings: dict[str, Any] = field(default_factory=dict)
    """The old daemon's settings that carry over (keys as in `config.toml`; `topic_prefix` from `[drivers]`)."""


def read_export(archive: bytes) -> Imported:
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
            members: dict[str, bytes] = {}
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                name = PurePosixPath(member.name).name
                if name in members:
                    raise MigrateError(f"the archive holds more than one {name}")
                if member.size > MAX_MEMBER:
                    raise MigrateError(f"{name} is too large")
                handle = tar.extractfile(member)
                members[name] = handle.read() if handle else b""
    except (tarfile.TarError, OSError, EOFError) as err:
        raise MigrateError(f"not a tar archive: {err}") from err

    if "config.toml" not in members:
        raise MigrateError("the archive has no config.toml")
    try:
        config = tomllib.loads(members["config.toml"].decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise MigrateError(f"config.toml is not valid TOML: {err}") from err

    def named(key: str, *, required: bool) -> bytes | None:
        path = config.get(key)
        if not isinstance(path, str) or not path:
            if required:
                raise MigrateError(f"config.toml has no {key}")
            return None
        name = PurePosixPath(path).name
        if name not in members:
            if required:
                raise MigrateError(f"{name} ({key} in config.toml) is not in the archive")
            return None
        return members[name]

    files = {"ca.key": named("ca_key", required=True), "ca.cert": named("ca_certificate", required=True),
             "devices-v2.json": named("device_ledger", required=True)}
    check_ca(files["ca.key"], files["ca.cert"])
    if (account := named("cloud_account", required=False)) is not None:
        files["cloud-account-v2.json"] = account
        pairings = PurePosixPath(config["cloud_account"]).name + ".pairings"
        if pairings in members:
            files["cloud-account-v2.json.pairings"] = members[pairings]

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
    return Imported(files, settings)


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
