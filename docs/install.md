# Install Star Forge

Star Forge is a Codex plugin. The full experience requires Codex plugin
installation so the skills, native agents, and observer hooks are loaded together.

## Install From A Git Clone

Clone the repository, then run the installer. The installer creates a standard
local Codex marketplace snapshot at `~/.star-forge/codex-marketplace`, copies the
plugin into `plugins/star-forge`, registers that marketplace, and installs the
plugin.

```sh
git clone https://github.com/khaledayeva/star-forge.git
cd star-forge
scripts/install-codex.sh
```

Start a new Codex thread after installation. Plugin skills and hooks are loaded at
thread startup.

To choose a different generated marketplace location:

```sh
STAR_FORGE_MARKETPLACE_ROOT="$HOME/.local/share/star-forge-marketplace" scripts/install-codex.sh
```

## Trust Observer Hooks

After installing or upgrading, open Codex and run:

```text
/hooks
```

Trust the Star Forge hook entries if you want continuity re-anchors, local
changed-file trails, and sub-agent provenance diagnostics. Star Forge still works
without trusted hooks. Hook trust does not remove the advisory suffix from final
completion in this version because hook and sub-agent ledgers are project-local
diagnostics, not host-controlled witnesses.

## Verify The Install

In a new Codex thread, try:

```text
$forge resume where we left off
```

For a package-level check from the repository root:

```sh
scripts/release-check.sh
```

That wrapper runs the full release suite and the strict package self-test.

## Repository Shape

This repository is the plugin root. The installer creates the marketplace wrapper
outside the checkout because Codex expects installed local marketplace plugins to
live under a path such as `plugins/star-forge`.

If you later move Star Forge into a larger marketplace repository, use the
standard layout:

```text
.agents/plugins/marketplace.json
plugins/star-forge/
  .codex-plugin/plugin.json
  skills/
  scripts/
  hooks/
```

In that layout, update the marketplace source path to `./plugins/star-forge`.
