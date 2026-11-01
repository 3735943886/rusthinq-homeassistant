"""The rusthinq daemon run by Home Assistant: its files, its process and its restarts.

Everything lives in `<config>/rusthinq/` under the names rusthinq's own installers use (`config-0.2.sh`), so an
existing installation's CA and device files can be moved in: `ca.key`/`ca.cert`, `devices-v2.json`,
`cloud-account-v2.json`, `scripts/` (rusthinq-scripts, downloaded on first start), plus `bin/` (the binaries copied out
of the `rusthinq` wheel, so the daemon still starts when the wheel cannot be reinstalled) and `rusthinq.pid`.
`config.toml` is rewritten on every start: external MQTT off, the management API on a free 127.0.0.1 port with fresh
random credentials.

rusthinq 0.2.0-rc8 behavior this relies on: a port in use makes it exit 1 at once with an `AddrInUse` error that names
no address (hence the port check before starting); `/api/health` answers within tens of milliseconds; SIGTERM shuts it
down cleanly; logs go to stderr as ` LEVEL target: message`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import errno
import importlib
import io
import logging
import os
import secrets
import shutil
import signal
import socket
import tarfile
import time
from collections import deque
from pathlib import Path

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.requirements import async_process_requirements

from .client import basic_authorization
from .const import DOMAIN, REQUIREMENT, SCRIPTS_TARBALL

_LOGGER = logging.getLogger(__name__)
_DAEMON_LOGGER = logging.getLogger(f"{__package__}.daemon")

READY_TIMEOUT = 15
STOP_TIMEOUT = 5
RESTART_BACKOFF = (5, 15, 30, 60)
HEALTHY_AFTER = 600
"""Seconds of uptime after which a crash restarts at the shortest backoff again."""
LOG_LINES = 100
_LEVELS = {"ERROR": logging.ERROR, "WARN": logging.WARNING, "INFO": logging.INFO, "DEBUG": logging.DEBUG,
           "TRACE": logging.DEBUG}


class DaemonError(HomeAssistantError):
    """The daemon could not be prepared or started."""


def data_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(DOMAIN))


# ---- files ---------------------------------------------------------------------------------


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_config(*, https_port: int, mqtt_port: int, thinq1_https_port: int, thinq1_port: int, prefix: str,
                  management_port: int, user: str, password: str, hostname: str = "rusthinq.lan") -> str:
    """The daemon configuration, with `config-0.2.sh`'s defaults and file names. Paths are relative to the file."""
    lines = [
        f"hostname = {_toml(hostname)}",
        'ca_key = "ca.key"',
        'ca_certificate = "ca.cert"',
        'device_ledger = "devices-v2.json"',
        f'https_bind = "0.0.0.0:{https_port}"',
        f'mqtt_bind = "0.0.0.0:{mqtt_port}"',
        f'thinq1_https_bind = "0.0.0.0:{thinq1_https_port}"',
        f'thinq1_bind = "0.0.0.0:{thinq1_port}"',
        "legacy_tls = true",
        "advertise_requested_host = true",
        # Appliances reach HTTPS on 443 whatever port it is bound to (a redirect in front of it, as on r5c).
        *(["https_advertise = 443"] if https_port not in (0, 443) else []),
        'cloud_account = "cloud-account-v2.json"',
        'bridge_dns = ["https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query"]',
        "",
        "[drivers]",
        'directory = "scripts"',
        f"topic_prefix = {_toml(prefix)}",
        "watch = true",
        "",
        "[management]",
        f'bind = "127.0.0.1:{management_port}"',
        "gui = true",
        "raw_inject_toggle = false",
        f"user = {_toml(user)}",
        f"password = {_toml(password)}",
    ]
    return "\n".join(lines) + "\n"


def ensure_ca(directory: Path) -> None:
    """Create the CA like `config-0.2.sh` `initialize_ca` unless one is there; never replace half of one."""
    key, cert = directory / "ca.key", directory / "ca.cert"
    if key.exists() or cert.exists():
        if not (key.is_file() and cert.is_file()):
            raise DaemonError(f"incomplete CA in {directory}: restore both ca.key and ca.cert")
        return
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rusthinq CA")])
    now = dt.datetime.now(dt.UTC)
    ski = x509.SubjectKeyIdentifier.from_public_key(private.public_key())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=36500))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                          data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                          encipher_only=False, decipher_only=False),
            critical=True,
        )
        .add_extension(ski, critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski), critical=False)
        .sign(private, hashes.SHA256())
    )
    key_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    _write(key, key_pem, 0o600)
    _write(cert, certificate.public_bytes(serialization.Encoding.PEM), 0o644)


def _write(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def install_scripts(directory: Path, tarball: bytes) -> None:
    """Unpack a rusthinq-scripts GitHub tarball as `directory/scripts`, replacing it in one step."""
    staging = directory / ".scripts.new"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        archive.extractall(staging, filter="data")
    roots = [p for p in staging.iterdir() if p.is_dir()]
    if len(roots) != 1 or not (roots[0] / "drivers").is_dir():
        shutil.rmtree(staging, ignore_errors=True)
        raise DaemonError("the rusthinq-scripts download has no drivers directory")
    target, old = directory / "scripts", directory / ".scripts.old"
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        target.rename(old)
    roots[0].rename(target)
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)


def copy_binaries(package_bin: Path, directory: Path) -> None:
    """Copy the wheel's binaries into `directory/bin` when they differ, replacing each atomically."""
    target = directory / "bin"
    target.mkdir(exist_ok=True)
    for name in ("rusthinq", "rusthinqctl"):
        source, dest = package_bin / name, target / name
        if dest.is_file() and dest.stat().st_size == source.stat().st_size and _same(source, dest):
            continue
        tmp = target / f".{name}.tmp"
        shutil.copyfile(source, tmp)
        tmp.chmod(0o755)
        os.replace(tmp, dest)


def _same(a: Path, b: Path) -> bool:
    with a.open("rb") as fa, b.open("rb") as fb:
        while True:
            ca, cb = fa.read(1 << 20), fb.read(1 << 20)
            if ca != cb:
                return False
            if not ca:
                return True


def busy_ports(ports: dict[str, int]) -> list[str]:
    """The listeners (`name: port`) whose port cannot be bound on all interfaces right now, with the reason."""
    problems = []
    for name, port in ports.items():
        if not port:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("0.0.0.0", port))
            except OSError as err:
                if err.errno == errno.EADDRINUSE:
                    problems.append(f"port {port} ({name}) is already in use")
                elif err.errno == errno.EACCES:
                    problems.append(f"port {port} ({name}) needs root or CAP_NET_BIND_SERVICE")
                else:
                    problems.append(f"port {port} ({name}): {err.strerror}")
    return problems


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def stop_orphan(pidfile: Path, config: Path) -> None:
    """Stop a daemon a previous Home Assistant left running on this configuration (it dies with HA only on a clean stop)."""
    try:
        pid = int(pidfile.read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        pidfile.unlink(missing_ok=True)
        return
    if str(config).encode() not in cmdline:
        pidfile.unlink(missing_ok=True)
        return
    _LOGGER.warning("stopping a rusthinq daemon left running by a previous start (pid %s)", pid)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
        for _ in range(STOP_TIMEOUT * 10):
            if not _alive(pid):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
    pidfile.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    """Running, as opposed to gone or exited and waiting to be reaped (a zombie)."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


# ---- process -------------------------------------------------------------------------------


class EmbeddedDaemon:
    def __init__(self, hass: HomeAssistant, *, ports: dict[str, int], prefix: str) -> None:
        self._hass = hass
        self._dir = data_dir(hass)
        self._ports = ports
        self._prefix = prefix
        self._proc: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task | None = None
        self._supervisor: asyncio.Task | None = None
        self._stopping = False
        self._started = 0.0
        self.lines: deque[str] = deque(maxlen=LOG_LINES)
        self.management_port = 0
        self._user, self._password = "homeassistant", secrets.token_urlsafe(24)
        self.headers = basic_authorization(self._user, self._password)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.management_port}"

    @property
    def config_path(self) -> Path:
        return self._dir / "config.toml"

    @property
    def pidfile(self) -> Path:
        return self._dir / "rusthinq.pid"

    async def async_start(self) -> None:
        await self._prepare()
        problem = await self._spawn_until_ready()
        if problem:
            raise DaemonError(problem)
        self._supervisor = self._hass.async_create_background_task(self._supervise(), f"{DOMAIN} daemon")

    async def async_stop(self) -> None:
        self._stopping = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
        await self._terminate()
        await self._hass.async_add_executor_job(self.pidfile.unlink, True)

    # ---- preparation ---------------------------------------------------------------------

    async def _prepare(self) -> None:
        try:
            await async_process_requirements(self._hass, DOMAIN, [REQUIREMENT])
            module = await self._hass.async_add_import_executor_job(importlib.import_module, "rusthinq")
            package_bin = (await self._hass.async_add_executor_job(module.binary_path)).parent
        except (HomeAssistantError, ImportError, OSError) as err:
            package_bin = None
            _LOGGER.warning("could not install %s (%s); using the binaries copied last time", REQUIREMENT, err)
        await self._hass.async_add_executor_job(self._prepare_files, package_bin)
        if not (self._dir / "scripts").is_dir():
            await self._download_scripts()
        self.management_port = await self._hass.async_add_executor_job(free_local_port)
        config = render_config(
            https_port=self._ports["https"], mqtt_port=self._ports["mqtt"],
            thinq1_https_port=self._ports["thinq1_https"], thinq1_port=self._ports["thinq1"],
            prefix=self._prefix, management_port=self.management_port, user=self._user,
            password=self._password,
        )
        await self._hass.async_add_executor_job(_write, self.config_path, config.encode(), 0o600)

    def _prepare_files(self, package_bin: Path | None) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if package_bin is not None:
            copy_binaries(package_bin, self._dir)
        if not (self._dir / "bin" / "rusthinq").is_file():
            raise DaemonError(f"no rusthinq binary: {REQUIREMENT} could not be installed and none was copied before")
        ensure_ca(self._dir)
        stop_orphan(self.pidfile, self.config_path)
        if problems := busy_ports(self._ports):
            raise DaemonError("; ".join(problems))

    async def _download_scripts(self) -> None:
        _LOGGER.info("downloading rusthinq-scripts from %s", SCRIPTS_TARBALL)
        try:
            async with async_get_clientsession(self._hass).get(
                SCRIPTS_TARBALL, timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
                response.raise_for_status()
                tarball = await response.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DaemonError(f"could not download rusthinq-scripts: {err}") from err
        await self._hass.async_add_executor_job(install_scripts, self._dir, tarball)

    # ---- running -------------------------------------------------------------------------

    async def _spawn_until_ready(self) -> str | None:
        """Start the daemon and wait until its management API answers; the reason if it did not."""
        self.lines.clear()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                str(self._dir / "bin" / "rusthinq"), str(self.config_path),
                cwd=self._dir, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE, limit=1 << 20,
            )
        except OSError as err:
            return f"could not run rusthinq: {err}"
        self._started = asyncio.get_running_loop().time()
        await self._hass.async_add_executor_job(_write, self.pidfile, f"{self._proc.pid}\n".encode(), 0o644)
        self._pump = self._hass.async_create_background_task(self._read_logs(self._proc), f"{DOMAIN} daemon log")
        http = async_get_clientsession(self._hass)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READY_TIMEOUT
        while loop.time() < deadline:
            if self._proc.returncode is not None:
                await self._pump
                return f"rusthinq exited ({self._proc.returncode}): {self._last_error()}"
            with contextlib.suppress(aiohttp.ClientError, TimeoutError):
                async with http.get(f"{self.url}/api/health", headers=self.headers,
                                    timeout=aiohttp.ClientTimeout(total=1)) as response:
                    if response.status == 200:
                        _LOGGER.info("rusthinq started (pid %s, management %s)", self._proc.pid, self.url)
                        return None
            await asyncio.sleep(0.1)
        await self._terminate()
        return f"rusthinq did not answer within {READY_TIMEOUT} s: {self._last_error()}"

    def _last_error(self) -> str:
        return next((line for line in reversed(self.lines) if line.startswith(("ERROR", "Error"))),
                    self.lines[-1] if self.lines else "no output")

    async def _read_logs(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            try:
                raw = await proc.stderr.readline()
            except ValueError:  # an overlong line; it is dropped, and the pipe keeps being read
                continue
            if not raw:
                await proc.wait()
                return
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            self.lines.append(line)
            level = _LEVELS.get(line.split(" ", 1)[0], logging.ERROR)
            _DAEMON_LOGGER.log(level, "%s", line)

    async def _supervise(self) -> None:
        attempt = 0
        loop = asyncio.get_running_loop()
        while not self._stopping:
            assert self._proc is not None
            if self._pump is not None:
                await self._pump
            if self._stopping:
                return
            if loop.time() - self._started > HEALTHY_AFTER:
                attempt = 0
            delay = RESTART_BACKOFF[min(attempt, len(RESTART_BACKOFF) - 1)]
            _LOGGER.error("rusthinq exited (%s): %s; restarting in %s s", self._proc.returncode, self._last_error(),
                          delay)
            await asyncio.sleep(delay)
            attempt += 1
            if self._stopping:
                return
            try:
                await self._hass.async_add_executor_job(self._check_ports)
                problem = await self._spawn_until_ready()
            except DaemonError as err:
                problem = str(err)
            if problem:
                _LOGGER.error("rusthinq did not restart: %s", problem)

    def _check_ports(self) -> None:
        if problems := busy_ports(self._ports):
            raise DaemonError("; ".join(problems))

    async def _terminate(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        if self._pump is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
