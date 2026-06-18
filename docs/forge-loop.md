# Star Forge v0.3 — The Forge Loop

This is the architecture Star Forge moved to after the June 2026 Boss Fight
postmortem. It replaces the attestation factory (v0.2) with a Compound-Engineering
loop carried on Star Forge's verification spine.

## The one rule

Gates prefer evidence that is expensive for the model to fake:

- command output captured by the CLI (`verify`), bound to the task's declared command,
- screenshot bytes and image magic (`browser-run`),
- git tree state (source hash, clean tree, HEAD),
- the source hash each reviewer attests in its own findings file (a re-review must re-attest the current tree),
- hook-observed sub-agent thread ids (which decide *witnessed* vs *advisory*).

Reviewer findings are load-bearing because they are the **input** to the fix queue
that `done` consumes — skipping the review starves the pipeline; it cannot be
back-filled. All state lives in local files the model *could* write, so the
"witnessed" label is an integrity signal (the hook layer was live and observed the
sub-agents), not a cryptographic guarantee — when it cannot be established, the
verdict degrades to advisory rather than blocking.

## The loop

```
plan  →  build  →  review  →  done
              ↘  amend  ↙   (auto re-entry on post-done drift)
```

Phases in `.starforge/state.json`: `setup, plan, build, review, done, amend, blocked`
(plus `blocked:isolation-required`).

- **plan** — Blueprint (with `AC-n` acceptance criteria) + Plan.md (`Task | Status |
  Mode | Files | Depends | Verify | Evidence`). One human approval. `Mode ∈
  {solo, delegate, docs}` is the only delegation signal — no self-declared "risk".
- **build** — implement ready tasks. `delegate` tasks must spawn `starforge-builder`;
  the operating card prints a ready-to-paste prompt so delegating is *less* typing
  than coding. Each task completes with a fresh passing `verify` (+ `browser-run`
  for UI). No worker-run attestation exists.
- **review** — spawn `starforge-reviewer` agents; each writes
  `.starforge/reviews/<scope>/<role>.findings.json`. `review` merges/dedups them,
  scans the tree for secrets/quality, and writes a **fix queue** into state. `done`
  refuses while the queue has unresolved blocking findings or the review is stale.
- **done** — a predicate computed from git, not a recorded fact. On pass it writes
  `.starforge/final/proof.json {head, source_hash, scope_hash}`. Verdict is
  `COMPLETE` (witnessed) / `COMPLETE (advisory: ...)` when hooks are dead /
  `NEEDS_CHANGES`. A fresh pass legitimately supersedes the old proof — the
  verify/review freshness gates already force real re-work after any source change.
- **amend** — every `run` recomputes the source hash; if it diverged from the proof
  (post-done edit), the phase becomes `amend` and an `AMEND-n` task is scaffolded
  from the changed files, routing the work back through build → review → done. No
  `--scope-change` flag to forget.

## Hooks are observers, never police

Zero blocking. `PreToolUse`/`PostToolUse` log events for the liveness signal and the
changed-file trail; there is no deny path, no leases, no override grants, no
edit-time secret block (which trained evasion in the session). Secrets are caught at
`review`/`done` by scanning the **tree** with the placeholder-tolerant regex, so an
interpreter-mediated write hides nothing. Every guarantee holds with hooks dead,
degraded only in the *wording* of the verdict.

`hooks/hooks.json` is **frozen** — all behaviour lives in `scripts/star_forge.py`,
which the hooks exec via newest-cache resolution. Trust is re-tripped exactly once.

## Continuity

The compaction antidote is the **operating card**: ~15 lines (version, hooks
live/dead, phase, next action, paste-ready spawn, the rules) re-printed by `run`
every turn and injected by `SessionStart`/`PreCompact`/`UserPromptSubmit`. Per-turn
re-injection is the only continuity compaction cannot erase. The Stop hook writes the
handoff and flags any "complete" claim that contradicts the computed predicate.

## Compounding

`~/.star-forge/learnings/<category>/<slug>.md` (global — hackathon projects are
throwaway, the user is the durable entity). `run`/`review` emit a `learnings_digest`
matched to the project's stack. `learn` writes one; contradictions and waived false
positives are mined into `.starforge/state/incidents.jsonl`.

## Liveness, surfaced first

Line 1 of every `run`: version, newest cache, and `hooks: LIVE/ABSENT`. A stale
plugin cache (the single biggest Boss Fight failure multiplier) prints a reinstall
warning. `done` in advisory mode passes but labels the verdict so a dead-hook session
can never be mistaken for a witnessed one.
