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

### Adaptive Interview

First inspect the objective, repository, supplied context, and existing Blueprint.
Classify each intake topic as `confirmed`, `material unanswered`, `safe assumption`,
or `not applicable`. A question is material only when its answer could change
scope, architecture, design, security, or delivery.

Cover these topics without turning them into a fixed questionnaire:

- users and the outcome they need
- core flows and observable success
- project class and target platforms
- data ownership, storage, retention, and sharing
- authentication and authorization
- payments or financial behavior
- external integrations and network access
- design applicability, brand constraints, and supplied references
- delivery target, environment, repository intent, and release intent
- time, budget, compliance, compatibility, performance, and operational constraints

Ask one concise batch containing only the material unanswered decisions. Skip
confirmed and inapplicable topics. For lower-impact gaps, choose a conservative
default and record it under `Explicit Assumptions` with its basis and impact if
wrong. Never hide an assumption in prose or repeat a question already answered.
Follow up only when an answer reveals a new material branch.

### Toolchain and Risk Contract

Record the project class, target platforms, required capabilities, preferred routes,
and accepted fallbacks in `Toolchain`. Routes are preferences rather than implicit
plugin requirements. Record unavailable required capabilities as explicit blockers.

Set every `Risk Flags` entry to `yes`, `no`, or `not applicable` with a reason.
Include auth, payments, secrets, network or external input, user or regulated data,
privacy, dependency exposure, service coupling, persistence or migrations,
performance or reliability, UI, and destructive operations. Do not approve a
Blueprint with an unresolved material decision or risk flag.

### Design Directions

Determine whether user-facing design applies before doing design research. For UI
pattern discovery, follow the capability router's preferred route before its
accepted fallbacks. Keep the Blueprint provider-neutral by recording source type,
reference, findings, and constraints instead of provider-specific commands,
credentials, or schemas. The router may prefer Mobbin for real-world interaction
patterns, so do not bypass that route when it is available. Valid source classes
include Mobbin, Figma, ImageGen, user-supplied references, other capable sources,
and a documented unavailable state.

#### Mobbin-first research

When Mobbin is available, research there before using another source for
real-world interaction patterns. Build concise queries from the product domain,
target platform, primary user job, target flow, interaction pattern, and material
constraints. Query the closest product, platform, and primary flow first; query
the interaction pattern and constraints second; broaden to an adjacent flow only
when the first queries do not produce enough relevant results. Use the
host-discovered Mobbin tool schema and supported OAuth connection. Do not invent a
tool name or call an undocumented REST endpoint.

Aim for three to five grounded research candidates when the available results
support that range. Do not pad the set with duplicates, irrelevant screens, or
untraceable claims. Normalize every candidate into:

- candidate id and source type
- stable reference, or the most precise tool-returned identifier available
- product class, platform, and observed flow
- observed interaction or information-architecture pattern
- why the pattern is relevant to this product
- `Borrow`: the reusable principle or behavior
- `Avoid`: copied expression, mismatch, or failure mode to reject
- product-specific design and verification constraints

The normalized fields are the provider-neutral research record. Provider response
objects, commands, credentials, and schemas do not belong in the Blueprint.

#### OAuth setup and failure handling

Star Forge packages the optional registered Mobbin App. In Codex Desktop, if
Mobbin reports that connection or authorization is required, tell the user to
connect the Mobbin App in ChatGPT through its supported OAuth flow, then retry in
Codex Desktop. In Codex CLI, the supported user-scoped setup is:

```text
codex mcp add mobbin --url https://api.mobbin.com/mcp
codex mcp login mobbin
```

Never request, store, or commit a Mobbin API key or OAuth token. Never add a
repository `.mcp.json`, manually persist credentials, or substitute an
undocumented REST fallback.

Treat authentication failure, permission failure, transport failure, empty
results, and rate limits as explicit states. Do not retry a rate-limited query
indefinitely and do not fabricate missing candidates. Preserve any grounded
candidates already returned. If three distinct candidates remain, continue with
them and record the limitation. Otherwise try accepted fallbacks in router order.
If no capable source succeeds, record which capabilities and queries were checked,
the exact unavailable or rate-limited state, the written constraints used instead,
and the resulting confidence and verification limitation. Never imply that
unavailable research ran.

#### Original synthesis

Turn research into principles, not clone instructions. `Borrow` may capture
interaction behavior, hierarchy, pacing, progressive disclosure, feedback, or
accessibility patterns. `Avoid` must reject copying source branding, trade dress,
assets, copy, proprietary content, distinctive composition, or screen-level
layout. Combine findings across candidates and translate them for this product's
users, content, brand, and constraints. A direction that merely names or recreates
one source is invalid.

When capable sources or supplied references are available, present two or three
materially distinct, grounded directions. Each direction must connect research
findings to an original visual system, layout and interaction model, accessibility
constraints, responsive constraints, and explicit `Borrow` and `Avoid` statements.
Do not present clone instructions or an ungrounded style label.

When no capable source is available, record which capabilities were checked, why
they were unavailable, the written constraints used instead, and the resulting
verification limitation. Never imply that unavailable research ran.

Record the user's selected direction and distilled constraints in the Blueprint.
Design selection is part of the single complete Blueprint approval, never a second
approval gate.

### Delivery Contract

New projects must explicitly choose `source-only`, `private-repo`, `preview`,
`production`, `package`, or a named platform-specific delivery target. Record the
environment, release intent, required artifact or live result, smoke expectation,
and any provider or destination the product contract truly requires. Record GitHub
owner, repository, visibility, adoption intent, default branch, and CI expectation
when repository delivery is requested.

Treat approval as authority only for the non-destructive external writes stated in
the contract. Visibility changes, destructive replacement, paid resource creation,
billing, signing, notarization, production migrations, and public publication need
specific authority or one explicit blocker.

### One Complete Approval

Resolve every material placeholder, include the selected design direction when UI
applies, and show the user the complete Blueprint once. After explicit approval,
create the tracked content lock:

```bash
python3 <plugin-root>/scripts/star_forge.py approve-blueprint --project .
```

Do not create the lock before approval and do not use design selection as a
separate approval checkpoint. Legacy status sentinels remain readable for existing
projects, but new v0.4 contracts use `Blueprint.lock.json`.

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
