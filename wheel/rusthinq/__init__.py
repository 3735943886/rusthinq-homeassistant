"""Thin package that only locates the rusthinq release binaries (process management is the caller's job)."""
from __future__ import annotations

import os
import stat
from pathlib import Path

__version__ = "0.0.0"  # build.sh substitutes the PEP 440 version
RUSTHINQ_TAG = ""  # build.sh substitutes the source release tag (e.g. v0.2.0)
SOURCE_URL = "https://github.com/3735943886/rusthinq"  # build.sh substitutes that tag's URL

_BIN_DIR = Path(__file__).resolve().parent / "bin"


def binary_path(name: str = "rusthinq") -> Path:
    """Path to the `rusthinq` or `rusthinqctl` executable.

    Restores the executable bit in case the installer dropped it (some pip/uv combinations).
    """
    if name not in ("rusthinq", "rusthinqctl"):
        raise ValueError(f"unknown rusthinq binary: {name}")
    path = _BIN_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"{name} binary missing (wheel for another platform?): {path}")
    if not os.access(path, os.X_OK):
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path
