"""rusthinq-scripts (the Rhai drivers) in `<config>/rusthinq/scripts`: which commit is there, which is upstream, and
how a new one gets in.

A GitHub tarball carries its commit in `VERSION` (`export-subst`: `<sha> <date>`). The daemon watches the directory it
canonicalized at start (`driver_watch.rs`, inotify, recursive), so an update changes files inside `scripts/` (each
replaced atomically, removed ones deleted), as a `git pull` would, and the daemon reloads the drivers by itself;
replacing the directory itself would leave the daemon watching the old one. The first install, with no daemon running,
moves the unpacked tree in whole.
"""

from __future__ import annotations

import io
import os
import shutil
import tarfile
from pathlib import Path

import aiohttp

REPOSITORY = "3735943886/rusthinq-scripts"
BRANCH = "master"
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)
CHECK_TIMEOUT = aiohttp.ClientTimeout(total=30)


class ScriptsError(Exception):
    """rusthinq-scripts could not be checked, downloaded or installed."""


def tarball_url(ref: str) -> str:
    return f"https://codeload.github.com/{REPOSITORY}/tar.gz/{ref}"


def compare_url(old: str, new: str) -> str:
    return f"https://github.com/{REPOSITORY}/compare/{old}...{new}"


def installed_commit(directory: Path) -> str | None:
    """The commit `scripts/VERSION` names, or None (no scripts, or a checkout whose VERSION was not substituted)."""
    try:
        sha = (directory / "scripts" / "VERSION").read_text().split()[0]
    except (OSError, IndexError):
        return None
    return sha if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha) else None


async def latest_commit(http: aiohttp.ClientSession) -> str:
    """The commit at the head of upstream master."""
    try:
        async with http.get(
            f"https://api.github.com/repos/{REPOSITORY}/commits/{BRANCH}",
            headers={"Accept": "application/vnd.github.sha"}, timeout=CHECK_TIMEOUT,
        ) as response:
            response.raise_for_status()
            sha = (await response.text()).strip()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ScriptsError(f"could not check rusthinq-scripts: {err}") from err
    if len(sha) != 40:
        raise ScriptsError(f"unexpected answer from GitHub: {sha[:80]!r}")
    return sha


async def download(http: aiohttp.ClientSession, ref: str = BRANCH) -> bytes:
    try:
        async with http.get(tarball_url(ref), timeout=DOWNLOAD_TIMEOUT) as response:
            response.raise_for_status()
            return await response.read()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ScriptsError(f"could not download rusthinq-scripts: {err}") from err


def install(directory: Path, tarball: bytes) -> None:
    """Put a rusthinq-scripts tarball into `directory/scripts`: whole on the first install, file by file after."""
    staging = directory / ".scripts.new"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
            archive.extractall(staging, filter="data")
        roots = [p for p in staging.iterdir() if p.is_dir()]
        if len(roots) != 1 or not (roots[0] / "drivers").is_dir():
            raise ScriptsError("the rusthinq-scripts download has no drivers directory")
        target = directory / "scripts"
        if target.is_dir():
            _sync(roots[0], target)
        else:
            roots[0].rename(target)
    except (OSError, tarfile.TarError) as err:
        raise ScriptsError(f"could not install rusthinq-scripts: {err}") from err
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _sync(source: Path, target: Path) -> None:
    """Make `target` hold what `source` holds, touching only what differs; `VERSION` last, so it names the commit only
    once every file of it is in place."""
    wanted = {p.relative_to(source) for p in source.rglob("*") if p.is_file()}
    for rel in sorted(wanted - {Path("VERSION")}) + ([Path("VERSION")] if Path("VERSION") in wanted else []):
        new, old = source / rel, target / rel
        if old.is_file() and old.read_bytes() == new.read_bytes():
            continue
        old.parent.mkdir(parents=True, exist_ok=True)
        tmp = old.with_name(f".{old.name}.tmp")
        shutil.copyfile(new, tmp)
        os.replace(tmp, old)
    for old in sorted(target.rglob("*"), reverse=True):
        rel = old.relative_to(target)
        if old.is_file() and rel not in wanted:
            old.unlink()
        elif old.is_dir() and not any(old.iterdir()):
            old.rmdir()
