"""Build a portable rusthinq backup from the embedded daemon's state files."""

from __future__ import annotations

import io
import json
import tarfile
import time
import tomllib
from pathlib import Path


def create(directory: Path) -> bytes:
    """Return the same tar format the migration flow accepts, without binaries or driver scripts."""
    config_path = directory / "config.toml"
    config_data = config_path.read_bytes()
    config = tomllib.loads(config_data.decode())
    files: dict[str, bytes] = {}
    sources: dict[str, Path] = {}
    for key in ("ca_key", "ca_certificate", "device_ledger", "cloud_account"):
        value = config.get(key)
        if not isinstance(value, str) or not value:
            continue
        source = (directory / value).resolve()
        source.relative_to(directory.resolve())
        if source.is_file():
            sources[value] = source
            files[source.name] = source.read_bytes()
        elif key in ("ca_key", "ca_certificate", "device_ledger"):
            raise FileNotFoundError(source)
        if key == "cloud_account":
            pairings = Path(f"{value}.pairings")
            pairings_source = (directory / pairings).resolve()
            pairings_source.relative_to(directory.resolve())
            if pairings_source.is_file():
                sources[str(pairings)] = pairings_source
                files[pairings_source.name] = pairings_source.read_bytes()

    manifest = {
        "format": 1,
        "created": int(time.time()),
        "config": str(config_path),
        "directory": str(directory.resolve()),
        "files": [{"name": source.name, "path": str(source)} for source in sources.values()],
    }
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        entries = {"config.toml": config_data, "backup.json": json.dumps(manifest).encode(), **files}
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return archive.getvalue()
