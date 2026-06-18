# Hooks

Star Forge hooks are **observers, never police**. The June 2026 postmortem showed
that a blocking hook with a false positive trains the model to evade enforcement
entirely (and the evasion then hides from the audit trail), so v0.3 has no deny
path at all. Secrets and quality issues are caught at `review`/`done` time by
scanning the git tree — a side-channel write evades nothing, because the file is
in the tree when the scan runs.

## Trust Gate — Read This First

Codex silently skips plugin-bundled hooks until you review and trust them: run
`/hooks` inside Codex after installing or upgrading the plugin and trust every
Star Forge entry. Trust is bound to the hash of each hook command, so ANY edit to
`hooks/hooks.json` silently disables all hooks until you re-trust.

For this reason `hooks/hooks.json` is FROZEN: behavior changes belong in
`scripts/star_forge.py` (which the commands exec), never in the command strings.

Liveness is surfaced on the first line of every `run` (`hooks: LIVE` /
`hooks: ABSENT`). With hooks absent everything still works; the `done` verdict is
labeled `COMPLETE (advisory: ...)`. The verdict is also advisory when hooks ARE
live but the work claims sub-agents that were never observed — a delegated task
completed with no `SubagentStart`, or a review whose findings files carry no
observed agent id. Advisory never blocks; it tells the truth in the verdict.

## What Each Hook Does

- `SessionStart` (incl. compaction resume): injects the operating card so a fresh
  or compacted context re-anchors to the state machine; notes unconverted incidents.
- `PreToolUse`: logs the event (liveness signal). Nothing is denied.
- `PostToolUse`: appends the changed-file trail with the session id.
- `UserPromptSubmit`: resets the auto-continue budget and injects a one-line state
  banner (`[star-forge] phase=... next: ...`) — the per-turn compaction antidote.
- `SubagentStart` / `SubagentStop`: the sub-agent thread-id ledger
  (`.starforge/state/subagent-events.jsonl`). `done` reads it to decide whether a
  completion is *witnessed*: a delegated task or a review wave that claims
  sub-agents must have matching observed ids, or the verdict is labeled advisory.
  Because all state is local files the model could write, this is an integrity
  signal (the hook layer was live and saw the work), not a cryptographic proof.
- `Stop`: writes the continuity handoff; detects a completion claim that
  contradicts the computed predicate (warns + records an incident); bounded
  keep-going in cruise mode (max 3 auto-continues per stuck state).
- `PreCompact`: writes the handoff and injects the operating card before
  compaction. Never skipped.
