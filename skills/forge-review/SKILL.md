---
name: forge-review
description: Star Forge review and completion — the review wave, fix queue, and final verdict. Use when the user asks to review the build, run code or security review, handle findings, fix or waive review findings, check completion, finish, ship, declare done, or produce the final proof.
---

# Forge Review

Resolve `<plugin-root>` as two directories up from this skill file (`skills/forge-review/`). Start the turn with `run`; phase `review` means all tasks are complete and the project-level review wave is next.

## 1. Spawn the Review Wave

Spawn `starforge-reviewer` agents with the role-specific prompts from `spawn_plan` in `.starforge/state.json` (the operating card prints them ready to paste). The `standard` profile requires correctness, security, and architecture reviewers. The `fast-mvp` profile requires one correctness reviewer. Each entry names its `role` and `findings_file`; paste it as-is so each reviewer writes only `.starforge/reviews/<scope>/<role>.findings.json` and never edits source. Do not write findings files on a reviewer's behalf: an unperformed review cannot be back-filled.

## 2. Merge

```bash
python3 <plugin-root>/scripts/star_forge.py review --project . --strict
```

This merges and dedups all reviewer findings, scans the tree itself (secrets, residual placeholders, architecture debt), and writes the fix queue into state. Exit 1 under `--strict` means blocking findings remain, reviewer files are malformed or stale, no reviewer files existed, or the project profile's required reviewer roles are missing.

## 3. Clear the Queue

For each blocking finding:

- **Fix it**, then re-record verification for every affected task (`verify ... --strict`) — a verify only counts against the current source tree.
- **Or waive it** when it is a confirmed false positive:

```bash
python3 <plugin-root>/scripts/star_forge.py waive --project . --finding <id> --reason "<why this is not a real blocker>"
```

Never waive to save time; waives are recorded as incidents and mined for learnings.

Fixes change the source, which makes the recorded review stale. After fixing, re-spawn the reviewer wave and re-run `review` until the queue is empty against the current tree.

## 4. Done

```bash
python3 <plugin-root>/scripts/star_forge.py done --project . --strict
```

`done` computes completion from git facts: Blueprint approved, every task complete with fresh passing verifies (and browser-runs for UI), a fresh review with an empty fix queue, and a clean working tree. On pass it writes `.starforge/final/proof.json`. If it refuses, the `problems` array says exactly why — fix those; never argue with the predicate.

After a pass, write the human summary:

```bash
python3 <plugin-root>/scripts/star_forge.py done --project . --strict --write-summary
```

## Report the Verdict

QUOTE THE VERDICT LINE VERBATIM in your final message: `COMPLETE (advisory: ...)`, `NEEDS_CHANGES`, or a future unqualified `COMPLETE` only if the CLI itself reports one. The advisory reasons and any `[N waived finding(s)]` suffix are part of the verdict. Do not paraphrase an advisory verdict into an unqualified "complete". In this version there is no supported host-controlled witness source, so `/hooks` and local reviewer `agent_id` values do not produce unqualified `COMPLETE`. Remember any post-done edit reopens the project as `amend` on the next `run`.
