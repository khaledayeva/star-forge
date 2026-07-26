---
name: forge
description: "Star Forge entry point: run or resume the complete Forge lifecycle from intake through proven delivery and done."
---

# Forge

Star Forge is one coordinator-owned lifecycle:

`intake -> design -> plan -> foundation -> build -> review -> deliver -> done`

One `$forge` invocation owns that whole lifecycle. Do not make the user invoke
`$forge-plan`, `$forge-work`, or `$forge-review` separately. Those skills are phase
playbooks used by this coordinator. A phase transition is a reason to continue,
not a reason to hand control back.

After a passing `done`, a source change enters `amend` and repeats only the
affected build, proof, review, and delivery gates.

## Resume Before Reasoning

Resolve `<plugin-root>` as two directories up from this skill file
(`skills/forge/`). Start every turn, including the first turn, a status request,
post-compaction recovery, or resumed work, with:

```bash
python3 <plugin-root>/scripts/star_forge.py run --project . --objective "<objective>"
```

Add `--fast-mvp` when the user asks for an MVP, hackathon build, prototype, demo,
or proof of concept. Fast MVP reduces optional breadth, but never removes an
authority, security, privacy, proof, review, or delivery requirement.

Read the complete `.starforge/state.json`, especially `phase`,
`required_next_action`, `spawn_plan`, lifecycle gates, Blueprint lock, Plan state,
review state, and delivery state. The operating card and state are authoritative.
Never resume from memory.

## Continuous Coordinator Loop

Keep working in the same invocation until `done --strict` passes or one honest
user-controlled blocker prevents safe progress:

1. Run `run` and read `.starforge/state.json`.
2. Derive capability needs from the approved or draft project class, enabled
   Blueprint flags, Plan v2 `Proof` values, and Delivery Contract target. Resolve
   them through `starforge.routing.resolve_routes` and
   `config/capability-routing.json` using the host-discovered capabilities. Follow
   [capability-routing.md](references/capability-routing.md).
3. Execute the current phase with the matching playbook below. The coordinator
   retains lifecycle, mutation, and evidence ownership even when builders or
   reviewers are delegated.
4. After every material change, gate completion, recorded proof, or returned
   sub-agent wave, rerun `run`, reread state, and immediately continue with the
   newly reported phase.
5. Stop only for a material intake answer, explicit Blueprint approval, a
   router-approved optional installation that requires user action, missing
   external authority or credentials, an unsafe destructive choice, or the exact
   final verdict.

Do not silently skip a failed route or gate. Do not claim that an unavailable
capability ran. Report the selected fallback or consolidate unresolved authority,
credentials, signing, billing, or production access into one explicit blocker.

## Phase Playbooks

| Phase | Coordinator action |
|---|---|
| `setup` | Let `run` initialize local Git and Star Forge artifacts, resolve isolation if needed, then rerun. |
| `intake` | Use `$forge-plan` to ask only material unanswered decisions and record explicit assumptions. |
| `design` | Use `$forge-plan`; when UI research applies, route Mobbin first, then accepted fallbacks, and record an original selected direction or an honest unavailable state. |
| `plan` | Use `$forge-plan` to obtain one complete Blueprint approval, write the content lock, create and validate Plan v2, then rerun. |
| `foundation` | Establish the approved local or GitHub foundation and coordinator-owned source-bound foundation evidence before feature work. |
| `build` | Use `$forge-work` for routed task waves, verification, and required live proof. |
| `review` | Use `$forge-review` for the adaptive review wave and a fresh empty fix queue. |
| `deliver` | Use `$forge-review` to produce exactly the approved source handoff, private repository, preview, production result, package, or platform-specific result and its fresh delivery proof. |
| `done` | Run the strict completion predicate and report its exact verdict. |
| `amend` | Follow state into the approved change packet, then repeat affected build, review, deliver, and done gates. |
| `blocked` | Read the named blockers, make every safe unblocked repair, then request only the user-controlled decision or authority that remains. |

## Foundation Policy

`run` automatically initializes local Git, Blueprint.md, Plan.md, the ledger,
guardrails, and the `starforge-builder` and `starforge-reviewer` roles.
`agents-install --project .` restores those roles if they were removed.

When the approved Repository Contract
requests a new GitHub repository, create a private repository before feature work,
configure `origin`, establish the approved default branch and initial commit, and
install CI. Prefer the routed GitHub connector. Use
`gh repo create --private` only as the narrow repository-creation fallback and
only with approved write authority.

For an existing repository, verify owner, name, remote identity, visibility, and
default branch before adoption. Do not overwrite it, change visibility, replace a
remote, or otherwise mutate it implicitly.

The coordinator, not a builder, records foundation evidence. It must prove the
current source binding, remote identity and private visibility when requested,
default branch, initial or adopted commit, CI path, and every other requested
Foundation Contract check.

## External Authority

Blueprint approval authorizes only the non-destructive external writes explicitly
listed in the Repository and Delivery contracts. Plugin installation always
requires user action. Public repositories, public deployment, visibility changes,
destructive replacement, paid resources, billing changes, signing identities,
notarization, production data changes, and production release require specific
authority. Preserve safe local progress, then surface one precise blocker if that
authority is absent.

## Isolation and Hooks

If `run` reports `blocked:isolation-required`, use the operating card to either
create the recommended isolated `work/<name>/` project or adopt the root only when
the user has deliberately chosen that scope.

```bash
python3 <plugin-root>/scripts/star_forge.py run --project . --product-slug <name> --objective "<objective>"
python3 <plugin-root>/scripts/star_forge.py run --project . --adopt-root --objective "<objective>"
```

On first use or after a plugin upgrade, mention once that `/hooks` enables bundled
observer diagnostics and compaction re-anchors. Hooks are optional observers and
do not upgrade the completion verdict.

## Delegation and Evidence Ownership

Codex never auto-spawns sub-agents. Use the exact entries in `spawn_plan`. Delegate
real implementation to `starforge-builder` and review-only work to
`starforge-reviewer`. Respect owned files, dependencies, and the current thread
cap. When the host cap is 6, schedule waves of at most 5 sub-agents, wait between
waves, and close finished agents before dispatching more.

Builders and reviewers may return source changes or findings, but their narrative
claims are not proof. The coordinator alone runs and records task verification,
live proof, foundation proof, merged review state, delivery proof, and final
completion against the current source hash. Never backfill evidence for work that
did not run.

## Completion Honesty

Run:

```bash
python3 <plugin-root>/scripts/star_forge.py done --project . --strict
```

Only that predicate can declare completion. It requires the approved Blueprint,
complete Plan tasks with fresh proofs, an empty fresh review queue, the exact
approved Delivery Contract result, and a clean tree. A pass writes
`.starforge/final/proof.json`. Quote the verdict line
verbatim, including any advisory or waived-finding suffix. If it refuses, repair
its `problems` and continue the loop. Never paraphrase an advisory result as
unqualified completion.
