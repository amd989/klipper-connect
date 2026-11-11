#!/bin/sh
# Install klipper-connect and every Python package it needs into one folder
# (DEST), for the deb and rpm packages. Usage: build-lib.sh DEST
#
# The Prusa Connect SDK isn't packaged by Debian or Fedora, so the
# dependencies ship with klipper-connect. They go into a plain folder rather
# than a virtualenv: a venv is tied to the Python it was made with, and one
# package has to work on every release (Python 3.9 on Bullseye and EL9, 3.11
# on Bookworm, 3.13 on Trixie).
#
# That works because everything here is pure Python, apart from
# pycryptodome, whose wheels are abi3 (one build for every Python 3). Build
# with the oldest Python you want to support (PYTHON=python3.9), so pip
# picks versions that still run on it.
set -eu

[ $# -eq 1 ] || { echo "Usage: $0 DEST" >&2; exit 2; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$1"
PYTHON="${PYTHON:-python3}"

venv="$(mktemp -d)"
trap 'rm -rf "$venv"' EXIT
"$PYTHON" -m venv "$venv"
"$venv/bin/pip" install -q --disable-pip-version-check --upgrade pip

# charset-normalizer's wheels are compiled for one Python version; its
# source build is pure Python. .pyc files are written on the target by the
# package's postinst, for the Python that's there.
"$venv/bin/pip" install -q --disable-pip-version-check --no-compile \
    --no-warn-script-location --no-binary charset-normalizer \
    --target "$DEST" "$SRC"

# Console scripts point at the build venv; the package ships its own
rm -rf "$DEST/bin"
find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} +

# Anything compiled for one Python version would break on the others
bad="$(find "$DEST" -name '*.so' ! -name '*.abi3.so')"
if [ -n "$bad" ]; then
    echo "Version-specific extension modules in $DEST:" >&2
    echo "$bad" >&2
    exit 1
fi
