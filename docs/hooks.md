# Hooks

Star Forge hooks are **observers, never police**. The June 2026 postmortem showed
that a blocking hook with a false positive trains the model to evade enforcement
entirely (and the evasion then hides from the audit trail), so v0.3 has no deny
path at all. Secrets and quality issues are caught at `review`/`done` time by
scanning the git tree — a side-channel write evades nothing, because the file is
in the tree when the scan runs.

## Trust Gate - Read This First

Codex silently skips plugin-bundled hooks until you review and trust them: run
`/hooks` inside Codex after installing or upgrading the plugin and trust every
Star Forge entry. Trust is bound to the hash of each hook command, so ANY edit to
`hooks/hooks.json` silently disables all hooks until you re-trust.

For this reason `hooks/hooks.json` is FROZEN: behavior changes belong in
`scripts/star_forge.py` (which the commands exec), never in the command strings.

In this version, trusting `/hooks` enables observer diagnostics and continuity
re-anchors only. It does not enable an unqualified witnessed `COMPLETE`, because
there is no supported host-controlled witness source for bundled hooks to write.
The first line of every `run` reports advisory witness status and may also note
that local hook diagnostics were observed. A successful `done` verdict remains
`COMPLETE (advisory: ...)` whenever the only hook and sub-agent evidence is
project-local JSONL. Advisory never blocks; it tells the truth in the verdict.

## What Each Hook Does

- `SessionStart` (incl. compaction resume): injects the operating card so a fresh
  or compacted context re-anchors to the state machine; notes unconverted incidents.
- `PreToolUse`: logs the event (liveness signal). Nothing is denied.
- `PostToolUse`: appends the changed-file trail with the session id.
- `UserPromptSubmit`: resets the auto-continue budget and injects a one-line state
  banner (`[star-forge] phase=... next: ...`) as the per-turn compaction antidote.
- `SubagentStart` / `SubagentStop`: append the local sub-agent thread-id ledger
  (`.starforge/state/subagent-events.jsonl`) for provenance diagnostics. `done`
  reports local sub-agent observation separately, but local ids do not make
  delegated tasks or reviewer files trusted witnesses and do not remove the
  advisory suffix.
- `Stop`: writes the continuity handoff; detects a completion claim that
  contradicts the computed predicate (warns + records an incident); bounded
  keep-going in cruise mode (max 3 auto-continues per stuck state).
- `PreCompact`: writes the handoff and injects the operating card before
  compaction. Never skipped.
