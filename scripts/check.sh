#!/bin/sh
# Star Forge verification target: compile, validate JSON, run the full test suite.
set -e
cd "$(dirname "$0")/.."
PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/star-forge-pycache-$$"
export PYTHONPYCACHEPREFIX

python3 -m json.tool .codex-plugin/plugin.json >/dev/null
python3 -m json.tool hooks/hooks.json >/dev/null
sh -n scripts/check.sh
sh -n scripts/install-codex.sh
sh -n scripts/release-check.sh
for file in scripts/star_forge.py scripts/starforge/*.py scripts/live_collectors/*.py; do
    python3 -m py_compile "$file"
done
for suite in tests/test_*.py; do
    python3 "$suite"
done
