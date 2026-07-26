# Blueprint

Status: draft

## Product Summary

A production-shaped slice of the Star Forge control plane that formats lifecycle
gate status for operators.

## Toolchain

- Project class: Python CLI
- Target platforms: Linux and macOS
- Required capabilities: Python standard library, local Git, source-bound verification

## Intake Decision Record

- Users and their primary need: Codex operators need concise, deterministic Forge gate summaries.
- Core flows and success conditions: Format a lifecycle phase, route decision, and source identity without network access.
- Project class: Python CLI
- Target platforms: Linux and macOS
- Data created, read, stored, or shared: Local lifecycle strings and source digests only.
- Authentication and authorization: Not applicable for the local formatter.
- Payments, billing, or financial behavior: Not applicable.
- External integrations and network access: Not applicable in this source-only dogfood scope.
- Design applicability and supplied references: Not applicable for a command line control-plane helper.
- Delivery outcome: Source-only handoff from a clean local Git commit.
- Time, budget, compliance, compatibility, and operational constraints: Python standard library only, deterministic offline tests, and no provider writes.

## Explicit Assumptions

| ID | Assumption | Basis | Impact if wrong |
|---|---|---|---|
| A-1 | The dogfood slice can model Star Forge with one control-plane helper. | The test exercises the production CLI and contracts around the slice. | A wider runtime copy would increase fixture size without changing lifecycle coverage. |

## Design Direction

- Applicability: not applicable, command line control plane with no product UI
- Research availability: not needed
- Selected direction: not applicable
- Selection rationale: The output is plain deterministic text.
- Selected design constraints: Stable field order and no terminal styling.

## Delivery Contract

- Delivery target: source-only
- Platform-specific target, when selected: not applicable
- GitHub requested: no
- Environment: offline fixture
- Required result: A committed source handoff with a passing smoke result.
- Release intent: Test-only dogfood evidence, not publication.

## Risk Flags

| Flag | Value | Reason and required handling |
|---|---|---|
| User-facing UI | no | Plain CLI output has no graphical interface. |
| Authentication or authorization | no | The helper does not authenticate users. |
| Payments or financial data | no | No financial behavior exists. |
| Secrets or privileged operations | no | Inputs are local status strings and a digest. |
| Network access or external input | no | The fixture remains offline. |
| User, sensitive, or regulated data | no | No user data is accepted or retained. |
| Privacy obligations | not applicable | No personal data is collected or shared. |
| Security-sensitive behavior | no | The helper performs no privileged action. |
| Meaningful dependency exposure | no | The Python standard library is the only runtime dependency. |
| Multiple services or high coupling | no | One pure helper has no service boundary. |
| Migrations or complex persistence | no | The helper stores no data. |
| Performance or reliability constraints | yes | Output must be deterministic for proof and operator use. |
| Destructive operations | no | The helper only returns a string. |

## Acceptance Criteria

- AC-1: A status card includes the current phase, route, and abbreviated source digest in stable order.
- AC-2: The helper passes unit and integration verification through the production Star Forge CLI.
- AC-3: Adaptive review and source-only delivery remain bound to the current source.
- AC-4: A post-completion helper change uses an approved change packet and repeats affected gates.
