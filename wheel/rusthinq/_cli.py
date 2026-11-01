"""Console scripts that replace the Python process with the bundled binary."""
from __future__ import annotations

import os
import sys

from . import binary_path


def _exec(name: str) -> None:
    path = binary_path(name)
    os.execv(path, [name, *sys.argv[1:]])


def rusthinq() -> None:
    _exec("rusthinq")


def rusthinqctl() -> None:
    _exec("rusthinqctl")
