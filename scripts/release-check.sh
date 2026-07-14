#!/bin/sh
# Star Forge public release gate.
set -e
cd "$(dirname "$0")/.."

scripts/check.sh
python3 scripts/star_forge.py self-test --strict
