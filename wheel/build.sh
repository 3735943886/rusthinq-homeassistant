#!/usr/bin/env bash
# Wraps rusthinq release tarballs in platform wheels. wheel.yml and local checks share this path.
#
#   wheel/build.sh TAG ASSETS_DIR OUT_DIR [POST]
#
# ASSETS_DIR must hold the release's SHA256SUMS and rusthinq-TAG-linux-{amd64,aarch64}.tar.gz.
# POST (an integer) builds a `.postN` version that repackages the same binaries.
# Requires python3 with build, wheel and packaging.
set -euo pipefail

tag=$1 assets=$(realpath "$2") out=$3 post=${4:-}
here=$(cd "$(dirname "$0")" && pwd)

# v0.2.0-rc8 → 0.2.0rc8 (left to PEP 440 normalization)
version=$(python3 -c 'import sys; from packaging.version import Version; print(Version(sys.argv[1]))' "${tag#v}")
if [ -n "$post" ]; then
    version="$version.post$post"
fi
echo "rusthinq $tag → $version"

(cd "$assets" && sha256sum -c --ignore-missing --strict SHA256SUMS)

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -r "$here/." "$work/src"
rm -rf "$work/src/build" "$work/src/dist" "$work/src/rusthinq/bin" "$work/src"/*.egg-info
sed -i \
    -e "s|^__version__ = .*|__version__ = \"$version\"|" \
    -e "s|^RUSTHINQ_TAG = .*|RUSTHINQ_TAG = \"$tag\"|" \
    -e "s|^SOURCE_URL = .*|SOURCE_URL = \"https://github.com/3735943886/rusthinq/tree/$tag\"|" \
    "$work/src/rusthinq/__init__.py"

mkdir -p "$out"
for arch in amd64:x86_64 aarch64:aarch64; do
    asset=${arch%%:*} plat=${arch##*:}
    name="rusthinq-$tag-linux-$asset"
    tarball="$assets/$name.tar.gz"
    # --ignore-missing would let a file absent from SHA256SUMS through, so require its entry
    grep -q " $name.tar.gz\$" "$assets/SHA256SUMS" || { echo "$name.tar.gz not in SHA256SUMS" >&2; exit 1; }
    [ -f "$tarball" ] || { echo "missing $tarball" >&2; exit 1; }

    rm -rf "$work/src/rusthinq/bin" "$work/src/build" "$work/x"
    mkdir -p "$work/src/rusthinq/bin" "$work/x"
    tar xzf "$tarball" -C "$work/x"
    for bin in rusthinq rusthinqctl; do
        install -m 0755 "$work/x/$name/$bin" "$work/src/rusthinq/bin/$bin"
    done

    python3 -m build --wheel --outdir "$work/wheel-$plat" "$work/src"
    python3 -m wheel tags --remove --python-tag py3 --abi-tag none \
        --platform-tag "musllinux_1_2_$plat.manylinux_2_17_$plat" "$work/wheel-$plat"/*.whl
    mv "$work/wheel-$plat"/*.whl "$out/"
done
ls -l "$out"
