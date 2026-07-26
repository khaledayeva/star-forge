---
name: forge-review
description: "Star Forge review and delivery phase: clear adaptive review, satisfy the approved Delivery Contract, and run the final predicate."
---

# Forge Review

Resolve `<plugin-root>` as two directories up from this skill file
(`skills/forge-review/`). Start with `run` and read the complete
`.starforge/state.json`. This is the review, delivery, and completion playbook
inside one `$forge` invocation. Rerun `run` after each review, fix, proof,
delivery, or completion action and return control to `$forge` whenever state
changes.

## 1. Route and Spawn the Review Wave

Resolve review capabilities from project class, Blueprint risk flags, Plan v2
`Proof` values, and delivery target through
`starforge.routing.resolve_routes` and
`config/capability-routing.json`. Follow
`skills/forge/references/capability-routing.md`. Correctness review is always
required. Follow the role-specific `spawn_plan` from state for every additional
required lens.

Security-sensitive projects prefer Codex Security when available. Feed actual
results into the normalized security proof path. If it is unavailable, disclose
the selected safe fallback. Never claim it ran. Fast MVP may reduce optional
review breadth, but cannot remove risk-required security, privacy, UX,
accessibility, reliability, or delivery review.

Spawn each exact `starforge-reviewer` entry from `spawn_plan`. Every reviewer
writes only its assigned
`.starforge/reviews/<scope>/<role>.findings.json` and never edits source. Do not
write a reviewer's findings file on its behalf.

### UI originality and accessibility review

For the `ux-accessibility` role, include the paths to any available current
source-bound screenshots, recordings, browser evidence, native UI snapshots, and
interaction results in the spawn prompt. Do not create substitute evidence for the
reviewer or imply that unavailable evidence exists.

Require this lens to review all of the following:

- originality against the selected Design Direction and its `Borrow` and `Avoid`
  constraints, including whether the interface copies any single reference;
- accessibility across keyboard, focus, semantics, labels, assistive technology,
  contrast, non-color cues, text reflow, target size, and reduced motion;
- responsive visual quality across the supported viewports and platforms;
- relevant default, hover, focus, pressed, selected, disabled, loading, empty,
  error, success, and validation states; and
- the available live visual and interaction evidence, including its source binding
  and represented viewport or platform.

The reviewer remains read-only and evidence-bound. It must distinguish an observed
defect from missing, stale, or insufficient evidence, cite a repository file and
line for every finding, and never infer a visual pass or defect from code
inspection alone.

## 2. Merge and Clear the Queue

```bash
python3 <plugin-root>/scripts/star_forge.py review --project . --strict
```

This merges and deduplicates reviewer findings, runs coordinator-owned tree scans,
and writes the fix queue. A strict failure means the review is missing, malformed,
stale, incomplete, or still blocking.

For each blocking finding, fix it in the owning task scope and rerecord every
affected verification and live proof. A source edit makes the prior review stale,
so respawn the required review wave and merge again. Waive only a confirmed false
positive:

```bash
python3 <plugin-root>/scripts/star_forge.py waive --project . --finding <id> --reason "<why this is not a real blocker>"
```

Never waive to save time.

## 3. Deliver the Approved Result

After review passes, rerun `run` and continue directly into `deliver`. Deliver only
the target and environment in the approved Delivery Contract:
`source-only`, `private-repo`, `preview`, `production`, `package`, or its named
platform-specific target.

For web delivery, select exactly one routed provider by fit:

- Sites fits suitable simple sites, prototypes, and internal apps.
- Vercel fits applications that require its production web workflow.

Do not configure, deploy to, or collect proof from both Sites and Vercel by
default. Do not switch providers after approval without revising and reapproving
the contract.

External delivery writes must be explicitly authorized by the Delivery Contract.
Public release, production deployment, credentials, signing, notarization,
billing, paid resources, or destructive replacement need their own authority.
Continue any safe local work, then collapse unresolved authority or credential
requirements into one honest delivery blocker. Never substitute a source-only
handoff for a required preview, production result, package, or private repository
and call it complete.

The coordinator, not a delivery helper or reviewer, records fresh delivery proof.
It must be bound to the current source hash and contract, identify the repository commit and delivered deployment, package, repository, or source handoff, include
the live URL when required, and include a passing smoke result for the exact
approved result. A stale, degraded, or different provider result does not satisfy
delivery.

## 4. Done

After delivery proof passes, rerun `run` and execute:

```bash
python3 <plugin-root>/scripts/star_forge.py done --project . --strict
```

`done` computes completion from the approved Blueprint, complete Plan v2 tasks,
fresh task and live proofs, fresh empty review, the exact approved Delivery Contract result, and clean Git facts. If it refuses, repair the `problems`, rerun
the affected gates, and continue the same `$forge` invocation.

On pass, write the human summary:

```bash
python3 <plugin-root>/scripts/star_forge.py done --project . --strict --write-summary
```

Quote the verdict line verbatim, including any advisory and waived-finding suffix.
Never paraphrase an advisory verdict as unqualified `COMPLETE`. Any later source
edit enters `amend` on the next `run`.
