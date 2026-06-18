# Star Forge Workflow

Star Forge runs the Forge Loop. The canonical state is `.starforge/state.json`,
produced by the command that starts every turn:

```bash
python3 <plugin-root>/scripts/star_forge.py run --project . --objective "<objective>"
```

The first lines of its output are the operating card (version, hook liveness,
phase, required next action, paste-ready spawn commands). Follow `phase`,
`required_next_action`, and `spawn_plan`.

## 1. Plan

`forge-plan` creates `Blueprint.md` — the product contract with `AC-n` acceptance
criteria — and asks for approval exactly once (`Status: approved`). Then it writes
`Plan.md`:

```
| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |
```

- `Mode`: `delegate` (a spawned `starforge-builder` implements it — required for
  real code), `solo` (coordinator may implement trivial glue), `docs` (no code).
- `Files`: what the task owns — drives parallel safety and the builder prompt.
- `Verify`: the exact verification command.

Validate with `validate-plan --strict`. Apply the `learnings_digest` lines from
`run` output — they are lessons from past projects.

## 2. Build

For each ready task, the operating card prints a paste-ready spawn command for
`delegate` tasks. Run independent tasks in parallel waves when their `Files` do
not overlap (Codex caps ~6 threads; spawn ≤5 per wave, `wait_agent` between).

After each task, the coordinator records verification — captured output is the
evidence, claims are not:

```bash
python3 <plugin-root>/scripts/star_forge.py verify --project . --task "<id>" --command "<test command>" --strict
```

UI tasks additionally need browser proof (claim a `server-lease` first for local
apps):

```bash
python3 <plugin-root>/scripts/star_forge.py browser-run --project . --task "<id>" --scenario "<scenario>" --url "<url>" \
  --live-manifest ".starforge/live/<id>/browser/manifest.json" --server-lease ".starforge/runtime/server.json" \
  --viewport "desktop=1280x800:<png>" --viewport "mobile=390x844:<png>" \
  --interaction-evidence "<path>" --console-evidence "<path>" --strict
```

Then:

```bash
python3 <plugin-root>/scripts/star_forge.py complete-task --project . --task "<id>" --changed-file "<file>"
```

`complete-task` refuses without a passing verify that matches the *current*
source tree. Manual Plan.md row edits are not completion.

## 3. Review

When all tasks are complete, spawn `starforge-reviewer` agents (the spawn command
is in the operating card). Each reviewer writes its own findings file to
`.starforge/reviews/<scope>/<role>.findings.json` — an empty findings array is a
valid clean result. Then:

```bash
python3 <plugin-root>/scripts/star_forge.py review --project . --strict
```

This merges reviewer findings with a tree scan (secrets, AI residuals,
architecture debt) into the fix queue. Fix each blocking finding and re-verify,
or waive false positives with a recorded reason:

```bash
python3 <plugin-root>/scripts/star_forge.py waive --project . --finding F-3 --reason "<why this is not a real blocker>"
```

Re-run `review` after fixes. Review cannot be back-filled: no findings files
means `done` reports review-not-performed; a source change after review makes it
stale.

## 4. Done

```bash
python3 <plugin-root>/scripts/star_forge.py done --project . --strict
```

`done` is a predicate computed from git facts: Blueprint approved, all tasks
complete via `complete-task`, fresh passing verifies, browser proof for UI work,
a fresh review with an empty (or waived) fix queue, and a clean tree. On pass it
writes `.starforge/final/proof.json` and `--write-summary` writes the human
summary. Quote the verdict line verbatim — including the `(advisory)` label when
hooks were not live.

## 5. Amend

Any source edit after a passing `done` flips the next `run` to phase `amend` and
scaffolds an `AMEND-n` task from the changed files. The amendment flows through
build → review → done like any other work; the proof is superseded by the new
pass. There is no flag to remember — re-entry is automatic.

Star Forge does not push, publish, deploy, migrate, or create remote PRs unless
explicitly asked.
