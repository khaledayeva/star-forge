---
name: forge-plan
description: Star Forge planning — write or revise Blueprint.md and Plan.md. Use when the user asks to blueprint, plan, define requirements or acceptance criteria, break work into tasks, split or parallelize work, set task modes or dependencies, or validate the plan before building.
---

# Forge Plan

Resolve `<plugin-root>` as two directories up from this skill file (`skills/forge-plan/`). Start the turn with `run`; phase `plan` means Blueprint or Plan work is next.

## Blueprint.md

The Blueprint is the contract the review wave checks the build against.

- Ask the minimum questions needed, draft the Blueprint in complete form, and ask for approval ONCE. Do not re-interview when an approved Blueprint already covers the request.
- Give every acceptance criterion a stable `AC-n` id (`AC-1`, `AC-2`, ...). Reviewers cite these ids; vague criteria produce vague reviews.
- Approval sentinel: the Blueprint counts as approved only when it contains a line reading exactly `Status: approved` (or a `Last approved:` line starting with an ISO date). Set it after the user approves — never before.

## Plan.md

Derive tasks from the approved Blueprint into one table with EXACTLY these columns:

```
| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |
```

- **Status** — `queued`, `ready`, `in_progress`, `blocked`, `reviewing`, `complete`. Never hand-edit a row to `complete`; only `complete-task` does that.
- **Mode** — the only delegation signal:
  - `solo` — trivial glue the coordinator may implement inline.
  - `delegate` — real code; MUST be implemented by a spawned `starforge-builder`.
  - `docs` — no code; a no-op verify is allowed.
- **Files** — the files this task owns. Drives parallel-safety (tasks with disjoint Files can run in the same wave) and is injected into the builder spawn prompt. Be precise.
- **Depends** — comma-separated task ids, or `-`.
- **Verify** — the exact command that proves the task (e.g. `npm test -- --run`), or `noop` for docs tasks. `verify` executes this literally; a vague entry proves nothing.
- **Evidence** — leave `-`; `complete-task` fills it.

Size each task so one builder can own the entire edit scope of its Files. Split anything two builders would collide on.

## Validate

```bash
python3 <plugin-root>/scripts/star_forge.py validate-plan --file Plan.md --project . --strict
```

Fix every finding — invalid status or mode, missing verify command, unknown dependency, missing evidence on complete/blocked rows — before building.

## Apply Learnings

`run` prints a `learnings_digest`: durable lessons mined from past projects on this machine, matched to this stack. Read them while planning and encode the relevant ones into task descriptions and Verify commands — they exist because a previous project paid for them.
