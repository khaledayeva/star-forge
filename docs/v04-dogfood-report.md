# Star Forge v0.4 Dogfood Report

Date: 2026-07-26

Task: SF4-033

## Result

Star Forge v0.4 completed its own production-shaped control-plane fixture twice
through the production CLI:

1. The initial run started at intake, resolved the non-UI design decision, locked
   one complete Blueprint, validated Plan v2, established a local repository
   foundation, built and verified the planned task, passed adaptive review,
   recorded source-only delivery, and passed strict completion.
2. A real edit to the completed fixture source and test opened a draft change
   packet. After explicit approval, Star Forge derived one affected delegate task,
   repeated the affected verification, adaptive review, source-only delivery, and
   strict completion gates, then wrote a fresh final proof for the new source.

The focused dogfood test and the complete v0.4 E2E file pass. No runtime defect was
hidden or waived.

## Scope and Authority

The dogfood uses `fixtures/v04-dogfood/`, copied to a temporary directory for every
run. It is a small Star Forge control-plane slice rather than a second copy of the
entire plugin. Its source helper formats lifecycle phase, route, and source identity
for an operator. The fixture has Star Forge's Python package layout, local CI, unit
tests, a complete intake decision record, Plan v2, and a source-only Delivery
Contract.

The approved authority boundary is intentionally narrow:

- Local Git initialization and commits are allowed.
- Source-only handoff is allowed.
- Network calls, provider writes, publication, deployment, repository creation,
  credentials, billing, signing, and release actions are not allowed.

No external provider success is claimed. Generic proof-envelope replay is labeled
`offline-fixture-replay`. Review records use offline agent identifiers and remain
source-hash bound. Delivery identifies provider `not-applicable`, as required for
source-only handoff.

## Intake and Design Decisions

The initial production CLI invocation created the project and returned `intake`.
The approved fixture contract records these material decisions:

| Decision | Recorded result |
|---|---|
| User | Codex operators |
| Needed outcome | Concise, deterministic Forge gate summaries |
| Project class | Python CLI |
| Platforms | Linux and macOS |
| Data | Local lifecycle strings and source digests only |
| Auth, payments, network, personal data | Not applicable |
| Delivery | Source-only from a clean local Git commit |
| Dependencies | Python standard library only |
| Operational constraint | Deterministic offline verification with no provider writes |

Design was explicitly resolved as not applicable because the slice is a plain
command-line control-plane helper. No Mobbin, Figma, ImageGen, browser, or visual
provider was called or represented as successful. Stable field order and no
terminal styling are the recorded output constraints.

## Contracts and Routes

The test uses the production contract parsers rather than fixture-only parsing:

- `parse_blueprint_lifecycle_contract` proved intake complete, design not
  applicable and complete, and delivery target `source-only`.
- `parse_blueprint_plan_contract` derived the delivery target used by the
  Foundation Contract setup.
- `approve-blueprint` wrote the content-hash lock after the complete draft was
  present.
- `validate-plan --strict` accepted the ten-column Plan v2 table.
- `make_foundation_contract` derived a local-only foundation with no external
  repository requirement.
- `make_delivery_contract` derived a source-only handoff with provider
  `not-applicable`.

The capability resolver received project class `cli`, proof kinds `unit`,
`integration`, and `delivery`, and target `source-only`. It returned no external
capability decision and no blocker. The accepted route was therefore the
repository-native Python verification command through Star Forge's production
`verify` command. This result does not imply that an optional plugin ran.

## Initial Lifecycle Evidence

| Gate | Observed evidence |
|---|---|
| Intake | First `run` reported phase `intake`; all material decision fields were then resolved. |
| Design | The parsed contract reported `required: false` and `complete: true`. |
| Plan | Blueprint lock created; Plan v2 strict validation passed; all fixture ACs mapped to `SF-1`. |
| Foundation | Current source hash, actual root commit, actual default branch, committed helper, committed CI file, and deterministic credential-marker scan passed the production foundation evaluator. |
| Build | Production `verify` ran `python3 -m unittest discover -s tests`; `complete-task` completed `SF-1`; verification was repeated against current source. |
| Evidence v2 | A source artifact hash and source hash were written in a validated v2 envelope labeled `offline-fixture-replay`. |
| Review | Production policy selected correctness, UX/accessibility, and performance/reliability. Source-bound empty replay findings produced an empty strict fix queue. |
| Delivery | Current source hash, current repository commit, source-handoff identity, and smoke result passed the production delivery evaluator with provider `not-applicable`. |
| Done | `done --strict` returned `is_complete: true` and wrote `.starforge/final/proof.json`. |

The UX/accessibility role is a production policy result for the user-facing Python
CLI surface. The fixture does not claim browser or visual proof.

## Approved Change Packet

After initial completion, the test made one real scoped change:

- Added `completion_is_fresh` to
  `scripts/starforge/dogfood_status.py`.
- Added exact current-source freshness assertions to
  `tests/test_fixture.py`.

The next production `run` returned phase `amend` and created a draft packet whose
original completed source hash matched the initial final proof. Its `scope_delta`
contained exactly the two edited files. The root Plan remained historical.

After `approve-change`, Star Forge derived one delegate task with the original
unit-test command. The test then:

1. Recorded passing verification for the change task.
2. Completed the task with both changed files.
3. Repeated verification for the change task.
4. Repeated verification for root task `SF-1`.
5. Committed the scoped change.
6. Repeated all three adaptive review roles.
7. Recreated source-only delivery evidence for the new commit and source hash.
8. Passed `done --strict` again.

The final state reported no drift. The final proof hash equals the current source
hash and differs from the initial completion hash. Change history resolves the
approved item as `change-packet`.

## Defects Found and Fixed

No production runtime defect was found in this task. Two dogfood test-contract
defects were exposed before the isolated test passed:

1. The staged Blueprint initially used `Status: approved`. That invoked the legacy
   approval sentinel and skipped the intended content-lock plan checkpoint. The
   fixture now starts with `Status: draft` and calls `approve-blueprint` explicitly.
2. The first assertion expected only correctness and performance/reliability
   review. Production policy also selected UX/accessibility for the Python CLI
   surface. The assertion now preserves the actual deterministic three-role
   policy.

No finding was waived and no failed gate was converted into a pass.

## Verification

Passing checks:

```text
python3 -c 'import tests.test_v04_e2e as suite; suite.test_star_forge_dogfood_runs_from_intake_through_change_completion()'
python3 tests/test_v04_e2e.py
```

Full E2E result:

```text
test_v04_e2e.py: 7 passed, 0 failed, 7 total
```

The coordinator owns the declared `scripts/release-check.sh` verification for
SF4-033. This builder did not run or record that ledger gate.

## Limitations

- The offline fixture proves lifecycle orchestration around a production-shaped
  Star Forge control-plane slice. It does not duplicate or publish the full plugin.
- Review findings are deterministic, source-bound replay records. No external
  reviewer provider success is claimed.
- The evidence envelope is a labeled replay used to validate the production
  envelope reader and writer. It is not live provider output.
- Source-only delivery does not exercise GitHub repository creation, Sites,
  Vercel, package signing, notarization, native Simulator proof, Mobbin OAuth, or
  browser interaction. Those belong to their dedicated fixtures and release
  matrix rows.
- This dogfood demonstrates the integrated v0.4 lifecycle and amendment contract.
  It does not independently re-run every platform-specific proof behind AC-1
  through AC-58. The release ledger and release check aggregate those focused
  proofs.
