#!/bin/sh
# Star Forge verification target: compile, validate JSON, run the full test suite.
set -e
cd "$(dirname "$0")/.."
PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/star-forge-pycache-$$"
export PYTHONPYCACHEPREFIX

TEST_SUITES="
tests/test_star_forge.py
tests/test_live_proof_commands.py
tests/test_live_browser_playwright.py
tests/test_live_preview.py
tests/test_live_native_ios.py
tests/test_live_native_macos.py
tests/test_live_security_adapter.py
tests/test_live_github_pr.py
tests/test_live_collectors_integration.py
tests/test_v04_e2e.py
"

PYTHON_FILES="
scripts/star_forge.py
scripts/live_collectors/__init__.py
scripts/live_collectors/common.py
scripts/live_collectors/browser_playwright.py
scripts/live_collectors/preview.py
scripts/live_collectors/native_ios.py
scripts/live_collectors/native_macos.py
scripts/live_collectors/security_adapter.py
scripts/live_collectors/github_pr.py
tests/test_v04_e2e.py
"

python3 -m json.tool .codex-plugin/plugin.json >/dev/null
python3 -m json.tool hooks/hooks.json >/dev/null
sh -n scripts/check.sh
sh -n scripts/install-codex.sh
sh -n scripts/release-check.sh
for file in $PYTHON_FILES; do
    python3 -m py_compile "$file"
done
for suite in $TEST_SUITES; do
    python3 "$suite"
done
