#!/bin/sh
# Register this checkout as the canonical local Star Forge marketplace.
set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)

codex plugin marketplace add "$REPO_ROOT"
codex plugin add star-forge@star-forge

cat <<'TEXT'

Star Forge installed.
Start a new Codex task, then run /hooks and trust the Star Forge entries if you want observer diagnostics.
TEXT
