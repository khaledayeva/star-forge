#!/bin/sh
# Install this Star Forge checkout into Codex through a local marketplace snapshot.
set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
MARKETPLACE_ROOT=${STAR_FORGE_MARKETPLACE_ROOT:-"$HOME/.star-forge/codex-marketplace"}

if [ -z "$MARKETPLACE_ROOT" ] || [ "$MARKETPLACE_ROOT" = "/" ]; then
  echo "Refusing unsafe STAR_FORGE_MARKETPLACE_ROOT: $MARKETPLACE_ROOT" >&2
  exit 2
fi

PLUGIN_COPY="$MARKETPLACE_ROOT/plugins/star-forge"

case "$PLUGIN_COPY" in
  "$MARKETPLACE_ROOT"/plugins/star-forge) ;;
  *)
    echo "Internal path guard failed for plugin copy: $PLUGIN_COPY" >&2
    exit 2
    ;;
esac

mkdir -p "$MARKETPLACE_ROOT/.agents/plugins" "$MARKETPLACE_ROOT/plugins"
rm -rf "$PLUGIN_COPY"
mkdir -p "$PLUGIN_COPY"

(
  cd "$REPO_ROOT"
  tar \
    --exclude .git \
    --exclude .starforge \
    --exclude .pytest_cache \
    --exclude __pycache__ \
    -cf - .
) | (
  cd "$PLUGIN_COPY"
  tar -xf -
)

cat > "$MARKETPLACE_ROOT/.agents/plugins/marketplace.json" <<'JSON'
{
  "name": "star-forge",
  "interface": {
    "displayName": "Star Forge"
  },
  "plugins": [
    {
      "name": "star-forge",
      "source": {
        "source": "local",
        "path": "./plugins/star-forge"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Productivity"
    }
  ]
}
JSON

codex plugin marketplace add "$MARKETPLACE_ROOT"
codex plugin add star-forge@star-forge

cat <<'TEXT'

Star Forge installed.
Start a new Codex thread, then run /hooks and trust the Star Forge entries if you want observer diagnostics.
TEXT
