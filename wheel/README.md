# rusthinq

Prebuilt, statically linked binaries of [rusthinq](https://github.com/3735943886/rusthinq), a local server for
LG ThinQ appliances, packaged as platform wheels (Linux x86_64/aarch64, musl and glibc). It exists so the
[rusthinq Home Assistant integration](https://github.com/3735943886/rusthinq-homeassistant) can run the daemon as
a subprocess. The package only ships the binaries; it does not start, configure or supervise anything.

```python
import rusthinq
rusthinq.binary_path()               # Path to the rusthinq daemon
rusthinq.binary_path("rusthinqctl")  # Path to the management CLI
rusthinq.RUSTHINQ_TAG                # The rusthinq release these binaries come from
```

The binaries are taken unmodified from the rusthinq GitHub release named by `RUSTHINQ_TAG`, after checking them
against that release's `SHA256SUMS`. Version mapping: release `v0.2.0-rc8` is package `0.2.0rc8`; a `.postN`
suffix means the same binaries repackaged.

## License and source

rusthinq is licensed under GPL-2.0-or-later; the full text is in `COPYING`. The binaries statically link
OpenSSL 3 (Apache-2.0) and are therefore distributed under the terms of GPL version 3 or later where that
matters.

The complete corresponding source for every packaged release is the rusthinq git tag of the same name
(`rusthinq.SOURCE_URL` points at it), and the release build is reproduced by that tag's
`.github/workflows/release.yml` and `scripts/build-openssl.sh`. If that source is ever unavailable there, ask in
the [packaging repository](https://github.com/3735943886/rusthinq-homeassistant/issues) and it will be provided
for at least three years after the release was published.
