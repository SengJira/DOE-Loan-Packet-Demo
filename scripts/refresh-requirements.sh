#!/usr/bin/env bash
# Recompile requirements.txt from requirements.in, inside the same base image
# the Containerfile uses, so the lock matches the interpreter that will run it.
#
#   ./scripts/refresh-requirements.sh
#
# Needs network access to PyPI. Works with podman or docker.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${ENGINE:-podman}"
BASE="$(grep -m1 '^FROM ' "$ROOT/Containerfile" | awk '{print $2}')"

echo "compiling requirements.txt with $BASE"
"$ENGINE" run --rm -v "$ROOT:/w:rw" -w /w "$BASE" sh -eu -c '
    python -m pip install --quiet --no-cache-dir "pip-tools==7.6.1"
    python -m piptools compile \
        --quiet \
        --generate-hashes \
        --strip-extras \
        --constraint /w/constraints.txt \
        --output-file /w/requirements.txt \
        /w/requirements.in
'
echo "done. Rebuild with: $ENGINE build -t localhost/loan-packet-demo:local ."
