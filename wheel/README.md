# rusthinq

Prebuilt binaries of [rusthinq](https://github.com/3735943886/rusthinq), a local server for LG ThinQ appliances:
it accepts appliance connections, runs Rhai device drivers, and exposes a management API, a web dashboard and
optional MQTT output. This package installs the `rusthinq` daemon and the `rusthinqctl` management CLI with pip, with
no Rust toolchain and no system libraries needed.

- Linux x86_64 and aarch64, glibc and musl (Alpine). The binaries are statically linked.
- The binaries are the unmodified ones from the rusthinq GitHub release of the same version, checked against that
  release's `SHA256SUMS`.

## Install

```sh
pip install rusthinq          # or: pipx install rusthinq / uv tool install rusthinq
rusthinq --version
```

## Run the daemon

Create a CA once; appliances are provisioned to trust it, so keep both files:

```sh
mkdir -p ~/rusthinq && cd ~/rusthinq
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 36500 \
  -subj '/CN=rusthinq CA' -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' -addext 'subjectKeyIdentifier=hash' \
  -keyout ca.key -out ca.cert
```

A minimal `config.toml` (paths are relative to it):

```toml
hostname = "thinq.lan"
ca_certificate = "ca.cert"
ca_key = "ca.key"
device_ledger = "devices.json"
https_bind = "0.0.0.0:443"
mqtt_bind = "0.0.0.0:8883"

[management]
bind = "127.0.0.1:44401"
gui = true
```

```sh
rusthinq config.toml
```

Open `http://127.0.0.1:44401/` for the dashboard. Device drivers
([rusthinq-scripts](https://github.com/3735943886/rusthinq-scripts)), MQTT output, the LG cloud bridge, appliance
routing and every other setting are described in the
[rusthinq documentation](https://github.com/3735943886/rusthinq#build-and-run).

## Manage it

```sh
rusthinqctl health
rusthinqctl devices
rusthinqctl set DEVICE PROPERTY VALUE
```

`rusthinqctl` talks to `http://127.0.0.1:44401/` by default; set `RUSTHINQ_API`, and `RUSTHINQ_USER` /
`RUSTHINQ_PASSWORD` when the management API requires a login. `rusthinqctl` without arguments lists every command.

## Use from Python

The package does not start or supervise anything; it only tells you where the binaries are.

```python
import subprocess
import rusthinq

print(rusthinq.RUSTHINQ_TAG)  # the rusthinq release these binaries come from, e.g. v0.2.0
daemon = subprocess.Popen([rusthinq.binary_path(), "config.toml"])
subprocess.run([rusthinq.binary_path("rusthinqctl"), "health"], check=True)
daemon.terminate()  # SIGTERM shuts rusthinq down cleanly
```

The [rusthinq Home Assistant integration](https://github.com/3735943886/rusthinq-local) uses it this way.

## Versions

Release `v0.2.0` is package `0.2.0`; a `.postN` suffix means the same binaries repackaged.

## License and source

rusthinq is licensed under GPL-2.0-or-later; the full text is in `COPYING`. The binaries statically link OpenSSL 3
(Apache-2.0) and are therefore distributed under the terms of GPL version 3 or later where that matters.

The complete corresponding source for every packaged release is the rusthinq git tag of the same name
(`rusthinq.SOURCE_URL` points at it), and the release build is reproduced by that tag's
`.github/workflows/release.yml` and `scripts/build-openssl.sh`. If that source is ever unavailable there, ask in the
[packaging repository](https://github.com/3735943886/rusthinq-local/issues) and it will be provided for at
least three years after the release was published.
