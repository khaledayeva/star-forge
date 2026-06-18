---
name: forge
description: Star Forge entry point — the Forge Loop. Use when the user asks to set up Star Forge, build a project end-to-end, run cruise control, resume or continue work, check status or where the project is, recover after compaction or a new session, start a hackathon MVP or prototype, or keep going from plan to build to review to done.
---

# Forge

Star Forge moves a repo through one loop: plan → build → review → done, with automatic re-entry as `amend` when source changes after a passing `done`. Gates rest on evidence that is expensive to fake — captured command output bound to each task's declared verify command, screenshot bytes, git tree state, and hook-observed sub-agent ids that decide whether completion is *witnessed* or *advisory*. Narrative claims are display-only.

## The One Habit

Resolve `<plugin-root>` as two directories up from this skill file (`skills/forge/`). Start EVERY turn — first turn, resume, status check, post-compaction recovery — with:

```bash
python3 <plugin-root>/scripts/star_forge.py run --project . --objective "<objective>"
```

Its first lines are the operating card: version, hooks LIVE/ABSENT, phase, next action, and paste-ready spawn commands. Then read `.starforge/state.json` and obey `required_next_action` and `spawn_plan`. Never navigate from memory: the card survives compaction, your context does not.

## First-Time Setup

`run` auto-initializes everything missing — git repo, Blueprint.md, Plan.md, ledger, `.gitignore` guardrails, and the `starforge-builder`/`starforge-reviewer` roles under `.codex/agents/`. There is no separate setup step (`agents-install --project .` re-installs the roles if they were removed).

One thing the CLI cannot do: Codex silently skips untrusted plugin hooks. On first use (and after any plugin upgrade), tell the user once to run `/hooks` in Codex and trust the Star Forge entries. Line 1 of `run` shows `hooks: LIVE` or `hooks: ABSENT`. Hooks are observers only — nothing blocks at edit time — but with hooks absent the final verdict is labeled `COMPLETE (advisory ...)` instead of a witnessed `COMPLETE`.

## Fast MVP

When the user says MVP, hackathon, prototype, demo, proof of concept, or quick build, add `--fast-mvp`:

```bash
python3 <plugin-root>/scripts/star_forge.py run --project . --fast-mvp --objective "<objective>"
```

This records the `fast-mvp` profile on the project: same gates, lighter review wave (one reviewer instead of 2–3).

## Isolation

If `run` returns phase `blocked:isolation-required`, the directory already holds a non-Star-Forge project. Rerun with one of:

```bash
# recommended: build under work/<name>/ with its own git repo; a root redirect
# makes every later command resolve there, and Blueprint/Plan are carried over
python3 <plugin-root>/scripts/star_forge.py run --project . --product-slug <name> --objective "<objective>"

# or deliberately build in place (recorded in the project manifest)
python3 <plugin-root>/scripts/star_forge.py run --project . --adopt-root --objective "<objective>"
```

## Phases

| Phase | What to do |
|---|---|
| `setup` | `run` auto-initializes; remind the user about `/hooks` trust once. |
| `plan` | Draft Blueprint.md with `AC-n` criteria, get one approval, write Plan.md. Use $forge-plan. |
| `build` | Implement ready tasks: spawn `starforge-builder` for delegate tasks, `verify` everything, `browser-run` for UI, `complete-task`. Use $forge-work. |
| `review` | Spawn `starforge-reviewer` agents, run `review`, clear the fix queue, then `done --strict`. Use $forge-review. |
| `done` | Project complete. Stop; publish or push only on explicit request. |
| `amend` | Post-done source changes were detected and an `AMEND-n` task was auto-scaffolded from the changed files. Build, verify, review it, re-run `done`. |
| `blocked` | Read `required_next_action`; repair the named problem (usually a Plan.md parse issue) and rerun `run`. |

## Sub-Agents

Codex never auto-spawns sub-agents — you must call `spawn_agent` explicitly. Paste the spawn commands the operating card prints; they already carry the task row and owned files. Thread cap is 6: schedule in waves of at most 5, `wait_agent` between waves, and close finished agents so threads free up.

## Completion Honesty

`done` is a predicate computed from git facts — fresh passing verifies, a fresh review with an empty fix queue, a clean tree — not a recorded claim. Any edit after a passing `done` reopens the project as `amend` on the next `run`. So never tell the user the project is complete without a fresh pass of:

```bash
python3 <plugin-root>/scripts/star_forge.py done --project . --strict
```

Quote its verdict line verbatim in your final message — including the `(advisory ...)` suffix when hooks were not live. If `done` refuses, its `problems` array says exactly what to fix.
