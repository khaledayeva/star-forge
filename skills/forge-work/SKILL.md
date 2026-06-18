---
name: forge-work
description: Star Forge build phase — implement Plan.md tasks. Use when the user asks to implement, build, code, execute the plan, work the ready tasks, spawn builders, run verification, record browser evidence for UI, or continue building toward review.
---

# Forge Work

Resolve `<plugin-root>` as two directories up from this skill file (`skills/forge-work/`). Start the turn with `run`; build from its operating card and the `spawn_plan` in `.starforge/state.json`.

## Per-Task Loop

Route each ready task by its Mode:

1. **delegate** — spawn a builder. Paste the exact `spawn_agent starforge-builder "..."` command from the operating card / `spawn_plan`; it already carries the task row, owned Files, and verify command. Add the Blueprint `AC-n` text the task serves. Never implement a delegate task inline.
2. **solo** — implement directly in the task's Files. Keep it to trivial glue; if it grows real logic, stop and re-mode it to delegate.
3. **docs** — write the docs; no code.

Then ALWAYS record verification yourself — the captured output is the evidence; neither your claims nor the builder's self-report count. The `--command` must MATCH the task's `Verify` cell (completion binds the recorded run to it) and must be a real command — `true`, `:`, `echo ...`, and bare `exit 0` are rejected as no-ops:

```bash
python3 <plugin-root>/scripts/star_forge.py verify --project . --task <id> --command "<exact command from the Verify cell>" --strict
```

For docs-mode tasks only:

```bash
python3 <plugin-root>/scripts/star_forge.py verify --project . --task <id> --noop --summary "<why no command applies>" --strict
```

## UI Tasks

User-facing UI work additionally needs browser proof against the running app:

```bash
python3 <plugin-root>/scripts/star_forge.py server-lease --project . --action claim --port <port> --base-url "http://127.0.0.1:<port>" --command "<dev server command>"
python3 <plugin-root>/scripts/star_forge.py browser-run --project . --task <id> --scenario "<what was exercised>" --url "<url>" --server-lease --viewport "desktop=1280x800:<desktop png>" --viewport "mobile=390x844:<mobile png>" --interaction-evidence "<path>" --console-evidence "<path>" --strict
```

Screenshots must be real PNGs captured from the browser — the CLI validates image bytes, not filenames.

## Complete

```bash
python3 <plugin-root>/scripts/star_forge.py complete-task --project . --task <id> --changed-file <file> --changed-file <file2> --summary "<what shipped>"
```

`complete-task` refuses unless dependencies are complete, a passing verify matches the CURRENT source tree (or a recorded no-op for docs), and visual tasks have a passing browser-run. Never hand-edit Plan.md Status cells — a row reaches `complete` only through this command.

## Parallel Waves

Run independent delegate tasks in parallel when their Files do not overlap. Codex caps threads at 6: spawn waves of at most 5 builders, `wait_agent` between waves, and close finished agents before the next wave. After each wave, `verify` and `complete-task` every task, then rerun `run` for the next state.
