# Install Star Forge

Star Forge is a Codex plugin. Install it from its canonical GitHub marketplace so
the skills, native agent roles, optional Mobbin App binding, and hooks come from one
versioned package.

## Requirements

- Codex with plugin support
- Git
- Python 3.10 or newer
- A POSIX shell for repository checks

Platform tools such as Playwright, XcodeBuildMCP, Sites, Vercel, and security
scanners are optional until the approved project contract needs them.

## Clean Install

```sh
codex plugin marketplace add https://github.com/khaledayeva/star-forge
codex plugin add star-forge@star-forge
```

Start a new Codex task after installation. Plugin skills and hooks are loaded at
task startup.

The repository root is the marketplace package. Its
`.agents/plugins/marketplace.json` points directly at `./`, so no copied local
marketplace wrapper is needed for a public install.

## Hook Trust

In Codex Desktop, run:

```text
/hooks
```

Trust the Star Forge entries if you want continuity re-anchors, changed-file
trails, sub-agent diagnostics, and pre-compaction handoffs. Hooks are optional.
Their project-local ledgers are diagnostic and do not prove host-controlled
witnessing, so hook trust does not remove the final advisory trust suffix.

`run --no-hooks` suppresses optional hook trust prompts for that run.
`run --no-agents` skips generated project-local agent profiles. Neither flag means
the other.

## Verify With The Read-Only Doctor

Run the doctor from a known Star Forge source checkout:

```sh
python3 /path/to/star-forge/scripts/star_forge.py doctor \
  --source-root /path/to/star-forge \
  --strict
```

The doctor checks:

- stale marketplace registrations
- duplicate Star Forge installs, including disabled caches
- active plugin version or runtime drift from the selected source
- stale hook trust records
- duplicate Mobbin App or MCP connections

The report is JSON. Without `--strict`, an `ATTENTION` result still exits zero.
With `--strict`, it exits nonzero. The command is read-only and includes
remediation text, but never deletes, disables, rewrites, or reconnects anything.

Use explicit paths when diagnosing an isolated or nonstandard Codex home:

```sh
python3 scripts/star_forge.py doctor \
  --codex-home /path/to/.codex \
  --source-root . \
  --active-plugin-root /path/to/active/star-forge \
  --strict
```

## Mobbin OAuth

Star Forge packages one optional registered Mobbin App binding in `.app.json`. It
does not package `.mcp.json`, an API key, or a separate MCP registration.

In Codex Desktop, connect the Mobbin App in ChatGPT through OAuth, then retry from
a new or refreshed Codex task. Codex Desktop reuses the ChatGPT App credential.

For Codex CLI, create one user-scoped OAuth connection:

```sh
codex mcp add mobbin --url https://api.mobbin.com/mcp
codex mcp login mobbin
```

Do not add repository credentials or a repository `.mcp.json`. If both an App
binding and one or more MCP registrations are visible, run the doctor and follow
its read-only report before changing configuration.

## Upgrade From v0.3

1. Preserve the project and its `.starforge` directory.
2. Add or refresh the canonical GitHub marketplace.
3. Install `star-forge@star-forge`.
4. Start a new Codex task.
5. Run the doctor and resolve stale or duplicate installations deliberately.
6. Read [migration-v04.md](migration-v04.md) before changing a legacy Plan.

Star Forge reads legacy projects without rewriting them. The Plan migration
command writes a separate review draft:

```sh
python3 scripts/star_forge.py migrate-plan \
  --project . \
  --file Plan.md \
  --output drafts/Plan.v2.md
```

## Source Checkout Validation

From a checkout:

```sh
scripts/check.sh
python3 scripts/star_forge.py self-test --strict
```

For release work:

```sh
scripts/release-check.sh
```

The legacy `scripts/install-codex.sh` helper creates a local marketplace snapshot
for checkout development. It is not the canonical public install path.
