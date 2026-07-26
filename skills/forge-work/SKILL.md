---
name: forge-work
description: "Star Forge build phase: execute routed Plan v2 tasks and record current-source proof before returning to the lifecycle coordinator."
---

# Forge Work

Resolve `<plugin-root>` as two directories up from this skill file
(`skills/forge-work/`). Start with `run`, read the complete
`.starforge/state.json`, and work only from its current `build` or `amend`
operating card and `spawn_plan`. A Foundation Contract must already pass before
feature work.

This is a phase playbook inside one `$forge` invocation. After every task wave and
gate change, rerun `run` and return control to `$forge` so the same invocation can
advance through review, deliver, and done.

## Route the Wave

Before dispatch, derive needs from project class, enabled Blueprint risk and
toolchain flags, every ready task's Plan v2 `Proof` values, and the Delivery
Contract target. Resolve them with `starforge.routing.resolve_routes` using
host-discovered capabilities and
`config/capability-routing.json`. Follow
`skills/forge/references/capability-routing.md`.

Pass each builder the selected capability route, unavailable preferred
capabilities, accepted fallback, task row, owned files, relevant `AC-n` text,
required proof kinds, and exact Verify command. Never imply that a missing
dedicated capability ran. An explicit blocker stops the affected work. An
installation suggestion requires user action and is presented only when the
router marks that capability materially required.

The required routing outcomes include:

- Web implementation prefers Build Web Apps guidance. Interactive local QA
  prefers the in-app Browser, with local Playwright only as the headless or CI
  fallback.
- Chrome is reserved for authenticated Chrome state or extension-dependent
  behavior. It is not the default local web QA route.
- iOS implementation prefers Build iOS Apps, and native proof uses
  XcodeBuildMCP with Simulator build, launch, tests, UI snapshot, and screenshot
  evidence.
- macOS work prefers Build macOS Apps plus the most specific routed UI, signing,
  packaging, and test capability required by the contract.
- React Native and Expo work prefers the official Expo plugin when available.
- Security-sensitive work prefers Codex Security when available and normalizes
  results into the declared security proof path.

## Per-Task Loop

Route each ready task by its Plan v2 `Mode`:

1. `delegate`: spawn the exact `starforge-builder` entry from `spawn_plan`. Real
   code belongs here. Paste the operating card's exact
   `spawn_agent starforge-builder "..."` command and never implement a delegate
   task inline.
2. `solo`: implement only truly trivial coordinator glue in the task's owned
   files. If substantive logic appears, stop and revise the task mode.
3. `docs`: write documentation in the owned files; a no-op verification is
   allowed only for this mode.

Plan v2 is authoritative. Every task has explicit `ACs` and validated `Proof`
values in addition to task, description, status, mode, files, dependencies,
verification, and evidence. Do not invent criterion mappings or proof kinds while
building.

## Coordinator-Owned Evidence

The coordinator always records verification after inspecting returned changes.
Builder output and self-reports never count as evidence. The recorded `--command`
must exactly match the task's `Verify` cell and must run against the current source
tree:

```bash
python3 <plugin-root>/scripts/star_forge.py verify --project . --task <id> --command "<exact Verify cell>" --strict
```

For `docs` mode only:

```bash
python3 <plugin-root>/scripts/star_forge.py verify --project . --task <id> --noop --summary "<why no command applies>" --strict
```

For each declared live proof kind, use the routed capability and the corresponding
source-bound proof adapter. UI work must include real interaction, console, and
visual evidence. For local web UI:

```bash
python3 <plugin-root>/scripts/star_forge.py server-lease --project . --action claim --port <port> --base-url "http://127.0.0.1:<port>" --command "<dev server command>"
python3 <plugin-root>/scripts/star_forge.py browser-run --project . --task <id> --scenario "<what was exercised>" --url "<url>" --server-lease --viewport "desktop=1280x800:<desktop png>" --viewport "mobile=390x844:<mobile png>" --interaction-evidence "<path>" --console-evidence "<path>" --strict
```

Screenshots must contain real PNG bytes. Never manufacture collector output,
reviewer findings, foundation evidence, delivery evidence, or a proof envelope on
someone else's behalf.

After the exact verification and every required proof pass, the coordinator
records completion:

```bash
python3 <plugin-root>/scripts/star_forge.py complete-task --project . --task <id> --changed-file <file> --summary "<what shipped>"
```

Never hand-edit a task to `complete`. If source changes later, rerecord every
affected proof because previous evidence is stale.

## Waves and Handoff

Run dependency-ready delegate tasks in parallel only when their owned files do not
overlap. Use the current host thread cap, wait for the wave, inspect changes,
record each task's evidence, and complete each task before starting dependents.

Rerun `run` after every wave. Continue until the build phase exits, then
immediately hand the new state back to `$forge`; do not end merely because the
phase changed.
