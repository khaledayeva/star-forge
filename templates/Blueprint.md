# Blueprint.md

Status: draft
Owner: project team
Last approved: not approved yet

<!-- These status lines remain readable by v0.3 projects, but they are not a v0.4
     content lock. After the user explicitly approves the complete contract, the
     coordinator runs `approve-blueprint` to create Blueprint.lock.json. Editing
     this file after approval invalidates that lock until the revision is approved. -->

## Product Summary

Describe the product in plain language.

## Product Goal

State the outcome this repo exists to deliver.

## Target Users

List the people or teams this product serves.

## User-Facing Behavior

Describe the core user flows and visible behavior.

## Intake Decision Record

Replace every placeholder below with a confirmed decision, an explicit assumption,
or `not applicable`. Do not leave a material decision unresolved at approval time.

- Users and their primary need:
- Core flows and success conditions:
- Project class:
- Target platforms:
- Data created, read, stored, or shared:
- Authentication and authorization:
- Payments, billing, or financial behavior:
- External integrations and network access:
- Design applicability and supplied references:
- Delivery outcome:
- Time, budget, compliance, compatibility, and operational constraints:

## Explicit Assumptions

Record decisions that did not justify another interview question. Approval accepts
these assumptions as part of the contract.

| ID | Assumption | Basis | Impact if wrong |
|---|---|---|---|
| A-1 | <specific assumption> | <why it is reasonable> | <scope, architecture, design, security, or delivery impact> |

## Tech Stack

Record the intended stack, package managers, app framework, mobile stack, backend stack, test stack, and deployment assumptions.

## Toolchain

- Project class:
- Target platforms:
- Required capabilities:

| Need | Preferred route | Accepted fallback | Availability or blocker |
|---|---|---|---|
| <material capability> | <capability, not a hard dependency> | <safe fallback or none> | <available, unavailable, or unresolved> |

Routes express preferences. The approved product contract must remain valid if an
accepted fallback is used, and no named provider is required unless the product
itself depends on that provider.

## Architecture Expectations

Define project structure, important boundaries, preferred services/helpers, and patterns that should be reused.

## Security Expectations

Define expectations for auth, sensitive user data, API boundaries, dependency risk, secrets, and destructive operations.

## Performance Expectations

Define expected load, latency, responsiveness, memory, battery, and mobile/web performance constraints.

## Risk Flags

Use `yes`, `no`, or `not applicable` and give a concrete reason. These flags drive
capability routing, task proof, and adaptive review. Approval must not contain an
unresolved risk flag.

| Flag | Value | Reason and required handling |
|---|---|---|
| User-facing UI | <yes/no/not applicable> | <reason and UX/accessibility impact> |
| Authentication or authorization | <yes/no/not applicable> | <reason> |
| Payments or financial data | <yes/no/not applicable> | <reason> |
| Secrets or privileged operations | <yes/no/not applicable> | <reason> |
| Network access or external input | <yes/no/not applicable> | <reason> |
| User, sensitive, or regulated data | <yes/no/not applicable> | <reason and privacy impact> |
| Privacy obligations | <yes/no/not applicable> | <collection, retention, sharing, consent, or compliance impact> |
| Security-sensitive behavior | <yes/no/not applicable> | <threat, trust boundary, and required handling> |
| Meaningful dependency exposure | <yes/no/not applicable> | <reason> |
| Multiple services or high coupling | <yes/no/not applicable> | <reason> |
| Migrations or complex persistence | <yes/no/not applicable> | <reason> |
| Performance or reliability constraints | <yes/no/not applicable> | <reason> |
| Destructive operations | <yes/no/not applicable> | <reason and authority boundary> |

## Design Direction

- Applicability: <applicable/not applicable, with reason>
- Research availability: <available/unavailable/not needed>
- Selected direction: <direction id, or not applicable>
- Selection rationale:
- Selected design constraints:

Keep this contract provider-neutral. A source may be Mobbin, Figma, ImageGen,
user-supplied material, another capable design source, or a documented unavailable
state. Record source identity and useful findings, not provider-specific commands,
credentials, or schemas.

### Research Sources

| Source ID | Source type or provider | Reference | Relevant findings |
|---|---|---|---|
| DS-1 | <real-world pattern, design file, generated concept, or supplied reference> | <stable reference or unavailable reason> | <evidence that grounds a direction> |

### Candidate Direction 1

- Name:
- Grounded by:
- Visual system:
- Layout and interaction model:
- Accessibility and responsive constraints:
- Original product-specific interpretation:

### Candidate Direction 2

- Name:
- Grounded by:
- Visual system:
- Layout and interaction model:
- Accessibility and responsive constraints:
- Original product-specific interpretation:

### Candidate Direction 3

Use this third block only when it offers a materially distinct choice. UI projects
with capable sources must present two or three candidate directions, not one.

- Name:
- Grounded by:
- Visual system:
- Layout and interaction model:
- Accessibility and responsive constraints:
- Original product-specific interpretation:

### Documented Unavailable State

Complete this only when UI applies but no capable design source or supplied
reference is available.

- Capabilities checked:
- Why unavailable:
- Written constraints used instead:
- Effect on confidence or verification:

The selected direction is approved with the complete Blueprint. It is not a
separate approval checkpoint.

## UI Design Direction

Summarize the selected direction's visual language, interaction density, layout
expectations, responsive behavior, and important brand or product signals.

## UX Quality Bar

For web and mobile apps, define the minimum acceptable experience for navigation, responsive behavior, accessibility, loading states, empty states, errors, and visual verification.

## UX Standards

Describe how the product should feel to use and what workflows must remain efficient.

## Non-Goals

List what should not be built or optimized yet.

## Delivery Contract

New projects must select one delivery target explicitly. Supported targets are
`source-only`, `private-repo`, `preview`, `production`, `package`, and a named
platform-specific target. A legacy Blueprint without this section defaults to
`source-only`.

- Delivery target:
- Platform-specific target, when selected:
- Environment:
- Release intent:
- Delivery provider or destination, if contractually required:
- Source handoff location or recipient:
- Required live URL, artifact, package, or repository result:
- Delivery smoke result required:

### Repository Contract

- GitHub requested: <yes/no>
- Owner:
- Repository:
- Visibility: <private/public/not applicable>
- Existing repository adoption: <new/adopt/not applicable>
- Default branch and CI expectation:

### Delivery Authority

Approval authorizes only the non-destructive external writes described above.
Record any additional authority or blocker for visibility changes, destructive
replacement, paid resource creation, billing, signing, notarization, production
migrations, or public publication.

## Acceptance Criteria

Give each criterion a stable `AC-n` id so the review wave can check the build against it.

- AC-1: <observable, testable criterion>
- AC-2: <observable, testable criterion>

## Definition Of Done

A change is done only when every acceptance criterion is met, each task carries a fresh passing `verify` (and `browser-run` for UI), the review wave's fix queue is empty or waived, the tree is clean, and `done --strict` returns COMPLETE.
