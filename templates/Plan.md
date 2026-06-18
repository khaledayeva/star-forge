# Plan.md

Status: active

This rolling task ledger is derived from Blueprint.md. Blueprint.md wins when the two conflict.

## Task Ledger

| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |
|------|-------------|--------|------|-------|---------|--------|----------|
| SF-001 | Define the first build task. | queued | delegate | - | - | replace-with-real-tasks | - |

## Columns

- **Status**: queued, ready, in_progress, blocked, reviewing, complete.
- **Mode**: `delegate` (spawn `starforge-builder` — required for real code), `solo` (coordinator may implement trivial glue), `docs` (no code; no-op verify allowed).
- **Files**: the files this task owns (used for parallel-safety and the builder prompt).
- **Verify**: the exact verification command, or `noop` for docs tasks.
- **Evidence**: filled by `complete-task`; do not hand-edit.

## Operating Rules

- Keep completed task detail concise. Update Plan.md after every meaningful step.
- `delegate` tasks must be implemented by a spawned `starforge-builder`, not inline.
- Every non-docs task needs a fresh passing `verify` run before `complete-task`.
- User-facing UI tasks need a passing `browser-run` (desktop + mobile, interaction + console evidence).
- A task is complete only through `complete-task`; the review wave and `done` run at the project level.
