# Star Forge Workflow

One `$forge` invocation drives the lifecycle:

```text
intake -> design -> plan -> foundation -> build -> review -> deliver -> done
```

The coordinator reruns `run` after each resolved phase and follows
`.starforge/state.json`, the generated operating card, and its `required_next_action`.

```sh
python3 <plugin-root>/scripts/star_forge.py run \
  --project . \
  --objective "<objective>"
```

Use `status --project .` for a read-only snapshot. Use `run --no-auto-init` when
you want inspection to fail rather than initialize a missing project.

## 1. Intake

Star Forge inspects the objective, repository, supplied context, and existing
Blueprint. It asks one concise batch containing only decisions that could change
scope, architecture, design, security, or delivery.

The Blueprint records resolved decisions, explicit assumptions, Toolchain routes,
accepted fallbacks, risk flags, Repository Contract, and Delivery Contract. New
projects must select `source-only`, `private-repo`, `preview`, `production`,
`package`, or a named platform-specific delivery target.

Existing foreign roots are protected. Use `--product-slug <name>` to create an
isolated `work/<name>/` project, or `--adopt-root` to record deliberate in-place
adoption.

## 2. Design

Non-UI projects record design as not applicable and continue.

UI projects use the capability router. Mobbin is preferred for real-world
interaction patterns, followed by Figma, ImageGen, supplied references, then a
documented unavailable state. Star Forge uses the host-discovered tool schema and
supported OAuth connection. It never invents a Mobbin command, stores an API key,
or uses an undocumented REST fallback.

Research is normalized directly into the Blueprint as stable references, observed
patterns, relevance, `Borrow`, `Avoid`, and product-specific constraints. When
evidence supports it, Star Forge presents two or three original directions. The
selected direction is part of the one complete Blueprint approval, not a second
approval checkpoint.

## 3. Plan And Approval

Once every material decision is resolved, the user approves the complete
Blueprint. The coordinator then records a content lock:

```sh
python3 <plugin-root>/scripts/star_forge.py approve-blueprint --project .
```

Any Blueprint edit invalidates the lock and returns the lifecycle to plan until
the revised contract is explicitly approved.

Plan v2 has exactly these columns:

```text
| Task | Description | Status | Mode | Files | Depends | ACs | Proof | Verify | Evidence |
```

`ACs` maps each task to approved acceptance criteria. `Proof` uses validated kinds
such as `unit`, `integration`, `browser`, `preview`, `native-ios`,
`native-macos`, `security`, `github`, `package`, and `delivery`.

Validate the plan:

```sh
python3 <plugin-root>/scripts/star_forge.py validate-plan \
  --file Plan.md \
  --project . \
  --strict
```

## 4. Foundation

Local Git initialization is automatic. The approved Foundation Contract marks each
obligation as requested, not applicable, or blocking.

For a new GitHub repository, the contract requires private visibility, `origin`,
the approved default branch, an initial commit, and CI before feature work. The
GitHub plugin is preferred. The only narrow creation fallback is:

```sh
gh repo create --private
```

That fallback is valid only with approved write authority and the full approved
owner and repository context. Existing repositories are inspected read-only first.
Identity and visibility must match the contract. Star Forge never overwrites a
remote or changes visibility implicitly.

Foundation evidence is bound to the current source and exact contract. Depending
on risk, it also proves the source scaffold, environment example without secrets,
secret scan, dependency audit, and security plan.

## 5. Capability Routing

The router derives needs from project class, enabled Blueprint flags, Plan proof
kinds, and delivery target. It consumes
`config/capability-routing.json` in stable catalog order.

Each decision reports:

- selected provider
- why it is required
- `available`, `degraded`, or `blocked` status
- missing preferred options
- whether a fallback ran
- a material installation suggestion, when applicable

Preference order is dedicated plugin or MCP, native Codex capability, Computer
Use, safe shell fallback, then blocker. Optional installation is always
suggestion-only and requires user action. If a safe fallback satisfies the
contract, Star Forge discloses it and continues.

Platform rules:

| Work | Route |
| --- | --- |
| Local web QA | In-app Browser, then Playwright |
| Authenticated or extension-dependent web state | Chrome, then in-app Browser |
| iOS | Build iOS Apps and XcodeBuildMCP, including Simulator proof |
| macOS | Build macOS Apps and the most specific UI, test, signing, and packaging route |
| React Native or Expo | Official Expo plugin, then a discovered repository-native CLI workflow |
| Security-sensitive work | Codex Security, then normalized scanner or reviewer fallback |

An unavailable preferred provider is never reported as if it ran.

## 6. Build And Verify

Plan task modes are:

- `delegate`: substantive implementation by a `starforge-builder`
- `solo`: trivial coordinator glue
- `docs`: documentation work, eligible for a recorded no-op

Tasks with disjoint owned files may run in parallel. The coordinator, not a
builder, records the exact `Verify` command:

```sh
python3 <plugin-root>/scripts/star_forge.py verify \
  --project . \
  --task SF-123 \
  --command "<exact Plan Verify command>" \
  --strict
```

Docs tasks use:

```sh
python3 <plugin-root>/scripts/star_forge.py verify \
  --project . \
  --task SF-123 \
  --noop \
  --summary "<why no command applies>" \
  --strict
```

Live proof is additional to the Plan Verify command. See
[proof-recipes.md](proof-recipes.md). After current-source proof passes:

```sh
python3 <plugin-root>/scripts/star_forge.py complete-task \
  --project . \
  --task SF-123 \
  --changed-file path/to/file \
  --summary "What shipped"
```

Manual Plan status edits do not complete a task.

## 7. Adaptive Review

Correctness review always applies. UX and accessibility, security and privacy,
architecture, and performance and reliability are added from deterministic risk
flags, with adjacent lenses combined to keep the wave at four agents or fewer.
Fast MVP cannot remove a risk-required review.

Reviewers write source-bound findings. The coordinator merges them with tree
scans:

```sh
python3 <plugin-root>/scripts/star_forge.py review --project . --strict
```

Fix each blocker and rerun affected proof, or record a justified false-positive
waiver:

```sh
python3 <plugin-root>/scripts/star_forge.py waive \
  --project . \
  --finding F-3 \
  --reason "<why this finding is not applicable>"
```

## 8. Deliver And Complete

The Delivery Contract names one result. Suitable simple or internal apps route to
Sites. Production web apps that need its workflow route to Vercel. Star Forge
never selects both by default or substitutes an opportunistic second provider.

Delivery proof records current source, repository commit, delivery or package
identity, live URL when applicable, and a smoke result. React Native and Expo
platform delivery needs this separate delivery proof in addition to normal task
verification.

Blueprint approval covers only stated non-destructive writes. Credentials, signing,
billing, production access, public release, destructive replacement, and
visibility changes remain user-controlled. Unresolved requirements collapse into
one explicit blocker.

```sh
python3 <plugin-root>/scripts/star_forge.py done \
  --project . \
  --strict \
  --write-summary
```

Completion is computed from the current repository. It requires the Blueprint
lock, traced Plan, task and platform proof, foundation gate, fresh review, empty or
waived fix queue, Delivery Contract, and clean tree. Hooks remain diagnostic, so
the passing verdict may carry an advisory trust suffix.

## 9. Post-Completion Changes

Source drift enters `amend`. New work uses
`.starforge/changes/<change-id>/change.md` and a scoped change Plan instead of
appending another `AMEND-n` row.

The packet records the original completed source hash, changed scope, affected ACs,
delivery impact, and approval state. Review it, then approve it:

```sh
python3 <plugin-root>/scripts/star_forge.py approve-change \
  --project . \
  --change CHANGE-1
```

Only affected build, proof, review, and delivery gates repeat. Historical root
plans, completion proofs, and v0.3 amendment rows remain readable and unchanged.
