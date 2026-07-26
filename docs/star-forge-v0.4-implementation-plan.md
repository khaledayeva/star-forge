# Star Forge v0.4 Implementation Plan

Status: approved

Last approved: 2026-07-25

Target release: `0.4.0`

Working title: Factory Control Plane

## 1. Outcome

Star Forge v0.4 will turn the current evidence-focused Forge Loop into a complete,
lean software-factory control plane. One `$forge` invocation will guide a project
through:

1. Adaptive interview
2. Design research and direction selection when UI applies
3. One complete Blueprint approval
4. Local Git and private GitHub foundation
5. Capability-routed implementation
6. Platform-appropriate verification
7. Adaptive review
8. Required delivery or source handoff
9. Evidence-bound final completion

Star Forge will orchestrate official Codex plugins, MCP servers, connectors, and
native tools. It will not copy their implementation into the Star Forge codebase.

## 2. Baseline

The v0.3 review established this baseline:

- Four skills and two reusable agent roles.
- 15,651 lines of production Python and 10,359 lines of Python tests.
- `scripts/star_forge.py` contains 6,685 lines.
- 321 tests pass, including browser, preview, iOS, macOS, security, and GitHub
  evidence paths.
- Strict package self-test passes.
- The active installed plugin and GitHub source use different versions and runtime
  bytes.
- The earlier Mobbin workflow exists only in a disabled plugin cache.
- Current plans do not mechanically map tasks to acceptance criteria or proof
  requirements.
- Blueprint approval is a mutable text sentinel with no content lock.
- Automatic amendments are always `solo` and inherit an unrelated verification
  command.
- Review roles are fixed rather than derived from project risk.
- The quality scanner misses the plugin's own large runtime file because it ignores
  code under `scripts/`.

This passing baseline must remain green throughout implementation.

## 3. Product principles

### 3.1 Control plane, not toolbox

Star Forge owns intent, state, routing, evidence contracts, and completion. Dedicated
plugins and tools own design access, code generation patterns, browser interaction,
native builds, deployment, GitHub operations, and security analysis.

### 3.2 One invocation, one approval checkpoint

The user may answer interview questions and approve the resulting Blueprint, but
they should not need to invoke separate plan, work, review, or delivery skills.
After approval, Star Forge continues until complete, blocked on missing authority,
or blocked on unavailable credentials.

### 3.3 Explicit external authority

The approved Blueprint records repository visibility, repository owner, deployment
target, and release intent. That single approval authorizes the corresponding
non-destructive external writes. Visibility changes, destructive replacement, paid
resource creation, production migrations, and public publication still require
specific authority.

### 3.4 Evidence over narrative

Every completion decision remains source-hash bound. Tool output, screenshots,
native transcripts, security findings, deployment identity, repository state, and
review files must use one common evidence envelope.

### 3.5 Lean by construction

- Keep four skills and two agent roles.
- Do not add one skill or agent per stack.
- Do not require Mobbin, Figma, Vercel, Sites, GitHub, or any other SaaS when the
  project does not need it.
- Do not add a new collector for every plugin.
- Prefer a data-driven routing entry and a thin evidence adapter.
- Do not restore the former attestation factory.
- A new runtime module must replace or absorb comparable logic from
  `scripts/star_forge.py`, not merely wrap it.

## 4. Non-goals

- Reimplementing official Codex plugins or MCP clients.
- Making Slack, Notion, Teams, Box, SharePoint, Jira, Linear, or similar services
  default dependencies.
- Automatically creating public repositories or public deployments.
- Signing up for paid services or changing billing plans.
- Shipping a general project-management system.
- Adding long-running hosted Star Forge infrastructure.
- Claiming host-controlled agent witness guarantees that Codex does not expose.
- Supporting every possible deployment vendor in v0.4.

## 5. Target lifecycle

| Phase | Entry condition | Required output | Exit condition |
|---|---|---|---|
| `intake` | New objective | Interview answers and explicit assumptions | Material product decisions are known |
| `design` | User-facing UI applies | Candidate directions and distilled constraints | Direction selected or unavailable state recorded |
| `plan` | Intake and design are resolved | Approved Blueprint lock and Plan v2 | Contract hash and plan validate |
| `foundation` | Blueprint approved | Git, private remote when requested, initial commit, CI | Foundation proof passes |
| `build` | Foundation passes or local-only is approved | Implemented tasks and fresh task proofs | Every planned task is complete |
| `review` | Tasks complete | Adaptive reviewer findings and empty fix queue | Review proof is fresh |
| `deliver` | Review passes | Push, preview, package, or source handoff required by contract | Delivery proof passes |
| `done` | All required gates pass | Final proof and human summary | Completion predicate passes |
| `amend` | Source changes after completion | Approved change packet and affected tasks | Changed scope repeats build through done |

Legacy projects without a Delivery Contract default to `source-only`. New projects
must state the delivery target explicitly.

## 6. Acceptance criteria

### Packaging and installation

- **AC-1:** The GitHub repository is the canonical marketplace source and supports a
  clean Codex install without copying the plugin to a separate local marketplace.
- **AC-2:** Every publishable source change requires a new plugin version or
  cachebuster, enforced by release validation.
- **AC-3:** Plugin manifest metadata includes repository, homepage, visual assets,
  and supported package surfaces.
- **AC-4:** Generated agent TOMLs either are not tracked or exactly match the
  canonical agent prompts byte for byte.
- **AC-5:** A read-only doctor command detects stale marketplaces, duplicate Star
  Forge installs, active-version drift, stale hook trust records, and duplicate
  Mobbin connections without deleting anything.

### Intake, design, and approval

- **AC-6:** A new project receives an adaptive interview covering users, core flows,
  platform, data, auth, payments, integrations, design, delivery, and constraints.
- **AC-7:** Questions are limited to decisions that materially change scope,
  architecture, design, security, or delivery; explicit assumptions cover the rest.
- **AC-8:** UI projects receive two or three grounded design directions before
  Blueprint approval when capable design tools are available.
- **AC-9:** The design contract is provider-neutral. It can use Mobbin, Figma,
  ImageGen, user-supplied references, or a documented unavailable state.
- **AC-10:** Mobbin is preferred for real-world interaction patterns and uses OAuth,
  not repository-stored API keys or an undocumented REST fallback.
- **AC-11:** Mobbin or Figma material is reduced to original `Borrow`, `Avoid`, and
  design-constraint statements. Builders do not receive clone instructions.
- **AC-12:** Design selection is part of the single complete Blueprint approval,
  never a second approval gate.
- **AC-13:** Blueprint approval writes a tracked content lock. Any Blueprint edit
  invalidates approval until the user approves the revised contract.

### Plan and traceability

- **AC-14:** New Plan tables add `ACs` and `Proof` columns while retaining the
  existing task, status, mode, files, dependencies, verification, and evidence
  fields.
- **AC-15:** Every acceptance criterion is covered by at least one task, and every
  task references at least one criterion or an explicit maintenance exemption.
- **AC-16:** `Proof` uses validated values such as `unit`, `integration`, `browser`,
  `preview`, `native-ios`, `native-macos`, `security`, `github`, `package`, and
  `delivery`.
- **AC-17:** Plan validation rejects unknown criteria, unknown proof kinds, uncovered
  criteria, missing delivery tasks, and proof requirements inconsistent with the
  Blueprint.
- **AC-18:** Existing eight-column plans remain readable. A migration command creates
  a reviewable Plan v2 draft and never invents criterion mappings silently.

### Capability routing

- **AC-19:** A data-driven router derives required capabilities from project class,
  Blueprint flags, task proof kinds, and delivery target.
- **AC-20:** Routing preference is dedicated plugin or MCP, then native Codex tool,
  then Computer Use, then an explicit shell fallback when safe.
- **AC-21:** Missing capabilities are reported with the chosen fallback. Star Forge
  never silently claims that a dedicated tool ran.
- **AC-22:** Adding or renaming a preferred plugin normally changes routing data and
  tests, not the state machine.
- **AC-23:** Optional plugin installation is suggested only when the requested
  outcome materially depends on it. Star Forge never installs plugins without user
  action.

### Foundation and GitHub

- **AC-24:** `run` initializes local Git as it does today.
- **AC-25:** When the approved contract requests GitHub, Foundation creates a private
  repository, configures `origin`, creates the default branch and initial commit,
  and installs CI before implementation begins.
- **AC-26:** GitHub connector capabilities are preferred. The narrow fallback for
  missing repository creation is `gh repo create --private`.
- **AC-27:** Foundation proof verifies remote identity, visibility, default branch,
  initial commit, CI path, and current source binding.
- **AC-28:** Existing repositories are adopted only after identity and visibility
  checks. Star Forge never overwrites or changes repository visibility implicitly.

### Build and proof

- **AC-29:** Web projects use Build Web Apps guidance and the in-app Browser for
  interactive local QA. Local Playwright remains a CI and headless fallback.
- **AC-30:** iOS projects use Build iOS Apps and XcodeBuildMCP, including Simulator
  build, launch, tests, UI snapshot, and screenshot proof.
- **AC-31:** macOS projects use Build macOS Apps plus the most specific available UI
  automation, signing, packaging, and test capabilities required by the contract.
- **AC-32:** React Native projects route to the official Expo plugin when available.
- **AC-33:** Chrome is used only when authenticated Chrome state or an extension is
  required. The in-app Browser is the default for local web applications.
- **AC-34:** Security-sensitive projects use Codex Security when available and feed
  its results into the existing normalized security proof path.
- **AC-35:** All live proof types emit or adapt to
  `star-forge.evidence-envelope.v2`, including tool identity, provenance, source
  hash, artifact hashes, timestamps, verdict, degradation, and blockers.
- **AC-36:** Existing v1 evidence remains readable during the v0.4 migration window.

### Adaptive review

- **AC-37:** Correctness review is always required.
- **AC-38:** UX and accessibility review is required for user-facing interfaces.
- **AC-39:** Security and privacy review is required for auth, payments, secrets,
  network access, user data, external input, or meaningful dependency exposure.
- **AC-40:** Architecture review is required for multiple services, migrations,
  complex persistence, or high coupling risk.
- **AC-41:** Performance and reliability review is required when the Blueprint or
  platform establishes those risks.
- **AC-42:** Review selection is deterministic, source-bound, and capped at four
  agents by combining adjacent lenses when necessary.
- **AC-43:** Fast MVP never removes security, privacy, or delivery review required by
  the project's risk flags.

### Delivery and completion

- **AC-44:** The Blueprint supports `source-only`, `private-repo`, `preview`,
  `production`, `package`, and platform-specific delivery targets.
- **AC-45:** Web delivery chooses Sites for suitable simple or internal apps and
  Vercel for applications needing its production workflow. It does not configure
  both by default.
- **AC-46:** Delivery proof is source-hash bound and records the repository commit,
  deployment identity or package identity, live URL when applicable, and smoke
  result.
- **AC-47:** `done --strict` refuses when the approved Delivery Contract has not been
  satisfied.
- **AC-48:** Credentials, signing requirements, billing, or production authority
  that cannot be resolved become one explicit blocker rather than a false
  completion.

### Amendments, quality, and safety

- **AC-49:** New post-completion work uses isolated change packets rather than
  appending repetitive `AMEND-n` rows to the historical Plan.
- **AC-50:** An amendment derives task mode and verification from changed scope; it
  never defaults substantive code to `solo` or inherits an unrelated command.
- **AC-51:** Historical v0.3 amendment rows remain readable and are not rewritten.
- **AC-52:** Source classification recognizes common root, `scripts`, `cmd`,
  `internal`, `Sources`, `packages`, and language-specific layouts while excluding
  generated and vendored code.
- **AC-53:** The plugin's own large-file quality rule evaluates Star Forge source.
- **AC-54:** `scripts/star_forge.py` is reduced below 2,500 lines and no extracted
  runtime module exceeds 1,200 lines without an explicit generated-code exemption.
- **AC-55:** Production Python targets no more than 18,000 lines for v0.4. Exceeding
  that target requires a documented deletion or consolidation plan before release.
- **AC-56:** Global learnings are opt-in, provenance-labeled, limited to abstract
  rules, and scanned for secrets or project-specific content before reuse.
- **AC-57:** `--no-hooks` and `--no-agents` have distinct, tested meanings.
- **AC-58:** All existing 321 tests and all new v0.4 tests pass on macOS and Linux.

## 7. Capability routing catalog

The catalog belongs in one versioned data file and is interpreted by a thin router.
Aliases are preferences, not hard dependencies.

| Project need | Preferred capability | Fallback |
|---|---|---|
| Requirements and OpenAI API work | OpenAI Docs | Official documentation lookup |
| UI pattern discovery | Mobbin MCP | Figma, ImageGen, supplied references, documented unavailable state |
| Existing design implementation | Figma plugin | Supplied exports plus visual inspection |
| Original visual concepts | ImageGen | Structured written directions |
| Web implementation | Build Web Apps | Repository-native framework guidance |
| React and Next.js quality | React best practices, Next.js | Framework tests and review |
| Component systems | shadcn | Existing design system |
| Payments | Stripe guidance | Provider documentation and security review |
| Postgres and backend data | Supabase/Postgres guidance | Repository-native database tooling |
| Local web QA | In-app Browser | Playwright collector |
| Authenticated browser state | Chrome | In-app Browser when authentication is reproducible |
| General GUI QA | Computer Use | Platform-specific tool or manual blocker |
| iOS implementation | Build iOS Apps | Swift/Xcode project-native workflow |
| iOS verification | XcodeBuildMCP, Simulator Browser | Explicit unavailable blocker |
| macOS implementation | Build macOS Apps | Swift/Xcode or SwiftPM workflow |
| React Native | Expo plugin | Repository-native Expo CLI workflow |
| Security | Codex Security | Dependency scanners plus security reviewer |
| GitHub lifecycle | GitHub plugin | Read-only `gh` or narrow mutation fallback |
| Simple/internal hosting | Sites | Source-only handoff |
| Production web hosting | Vercel | Contract-selected provider or source-only handoff |
| AI SDK applications | Vercel AI SDK and AI Gateway when selected | OpenAI SDK plus official docs |
| ChatGPT applications | Relevant OpenAI app guidance | Official OpenAI docs |

Slack, Notion, Teams, Box, SharePoint, and similar connectors may supply intake
context only when the user names them or the Blueprint requires them.

## 8. Artifact and schema changes

### 8.1 `Blueprint.lock.json`

Tracked at the project root:

```json
{
  "schema": "star-forge.blueprint-lock.v1",
  "blueprint_sha256": "<sha256>",
  "approved_at": "<ISO-8601>",
  "contract_version": 1
}
```

The coordinator may write this file only after explicit user approval. Any mismatch
returns the lifecycle to `plan`.

### 8.2 Plan v2

```text
| Task | Description | Status | Mode | Files | Depends | ACs | Proof | Verify | Evidence |
```

`ACs` is a comma-separated list. `Proof` is a comma-separated validated set. Legacy
plans stay readable; new plans and migrated plans use v2.

### 8.3 Toolchain and Delivery Contract

Blueprint sections record:

- Project class and target platforms
- Required capabilities
- Preferred routes and accepted fallbacks
- GitHub owner, repository, and visibility
- Delivery target and environment
- Auth, data, payments, privacy, security, and performance flags
- Design applicability and selected design constraints

### 8.4 Evidence envelope v2

```json
{
  "schema": "star-forge.evidence-envelope.v2",
  "kind": "browser|native-ios|security|foundation|delivery|...",
  "task": "SF-001",
  "capability": "local-web-qa",
  "provider": "browser",
  "provenance": {},
  "source_hash": "<sha256>",
  "runtime_asset_hash": "<sha256>",
  "started_at": "<ISO-8601>",
  "finished_at": "<ISO-8601>",
  "artifacts": [],
  "verdict": "PASS|FAIL|DEGRADED",
  "blockers": []
}
```

Existing collector manifests become adapter inputs. They are not all rewritten in
one change.

### 8.5 Change packets

```text
.starforge/changes/<change-id>/
  change.md
  Plan.md
  evidence/
  review/
```

`change.md` records the original completed source hash, scope delta, affected ACs,
delivery impact, and approval state. The original root Plan remains historical.

## 9. Backward compatibility

| v0.3 state | v0.4 behavior |
|---|---|
| Approved Blueprint with no lock | Readable, but amendments require explicit lock creation |
| Eight-column Plan | Readable in legacy mode; migration creates a draft v2 table |
| Existing `AMEND-n` rows | Preserved as history; new amendments use change packets |
| Evidence manifest v1 | Accepted through compatibility adapters |
| Standard or fast-MVP profile | Converted to risk flags plus a compatibility profile label |
| Existing completion proof | Remains historical for its source hash |
| Old marketplace install | Doctor reports exact removal and canonical reinstall steps |
| Existing global learnings | Ignored until provenance and safety validation pass |

No migration command deletes, rewrites, pushes, changes visibility, or deploys.

## 10. Implementation ledger

Every implementation-ledger sub-agent must be spawned with High reasoning.

The rows below are implementation-sized. Real code uses `delegate`; `solo` is
reserved for final version metadata and mechanical release assembly.

| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |
|---|---|---|---|---|---|---|---|
| SF4-001 | Freeze this v0.4 contract and decision log. Planning-only maintenance exemption; it records but does not satisfy AC-1 through AC-58. | queued | docs | docs/star-forge-v0.4-implementation-plan.md | - | noop | - |
| SF4-002 | Add canonical Git-backed marketplace metadata, complete plugin metadata, cachebuster enforcement, and packaging tests. Covers AC-1, AC-2, AC-3. | queued | delegate | .agents/plugins/marketplace.json, .codex-plugin/plugin.json, scripts/release-check.sh, tests/test_v04_release.py | SF4-001 | python3 tests/test_v04_release.py | - |
| SF4-003 | Remove tracked generated-agent ambiguity or add exact generation drift validation. Covers AC-4. | queued | delegate | .codex/agents/, agents/, scripts/release-check.sh, tests/test_v04_release.py | SF4-002 | python3 tests/test_v04_release.py | - |
| SF4-004 | Implement read-only installation doctor and legacy marketplace diagnostics. Covers AC-5. | queued | delegate | scripts/starforge/doctor.py, scripts/starforge/__init__.py, scripts/star_forge.py, tests/test_v04_doctor.py | SF4-002 | python3 tests/test_v04_doctor.py | - |
| SF4-005 | Separate and test `--no-hooks` and `--no-agents` semantics. Covers AC-57. | queued | delegate | scripts/star_forge.py, tests/test_star_forge.py | SF4-004 | python3 tests/test_star_forge.py | - |
| SF4-006 | Implement Blueprint lock schema, approval command, drift invalidation, and legacy detection. Covers AC-13. | queued | delegate | scripts/starforge/contracts.py, scripts/star_forge.py, templates/Blueprint.md, tests/test_v04_contracts.py | SF4-005 | python3 tests/test_v04_contracts.py | - |
| SF4-007 | Implement Plan v2 parsing, serialization, and non-destructive legacy migration. Covers AC-14, AC-18. | queued | delegate | scripts/starforge/contracts.py, scripts/star_forge.py, templates/Plan.md, tests/test_v04_contracts.py | SF4-006 | python3 tests/test_v04_contracts.py | - |
| SF4-008 | Enforce AC coverage, proof vocabulary, delivery consistency, and maintenance exemptions. Covers AC-15, AC-16, AC-17. | queued | delegate | scripts/starforge/contracts.py, scripts/star_forge.py, tests/test_v04_contracts.py | SF4-007 | python3 tests/test_v04_contracts.py | - |
| SF4-009 | Define adaptive interview, Toolchain, Delivery Contract, risk flags, and provider-neutral Design Direction templates. Covers AC-6 through AC-9, AC-12, AC-44. | queued | delegate | templates/Blueprint.md, skills/forge-plan/SKILL.md, tests/test_v04_skills.py | SF4-006 | python3 tests/test_v04_skills.py | - |
| SF4-010 | Implement data-driven capability catalog and deterministic route resolver. Covers AC-19 through AC-23. | queued | delegate | config/capability-routing.json, scripts/starforge/routing.py, tests/test_v04_routing.py | SF4-001 | python3 tests/test_v04_routing.py | - |
| SF4-011 | Validate Mobbin App, registered app mapping, and plugin-scoped MCP behavior; choose `.app.json` or `.mcp.json`, never both. Covers AC-10. | queued | delegate | docs/decisions/mobbin-integration.md, fixtures/mobbin/, tests/test_v04_mobbin.py | SF4-010 | python3 tests/test_v04_mobbin.py | - |
| SF4-012 | Restore Mobbin-first design research, OAuth setup guidance, candidate normalization, unavailable handling, and originality constraints. Covers AC-8 through AC-12. | queued | delegate | skills/forge-plan/SKILL.md, agents/builder/agent.md, agents/reviewer/agent.md, config/capability-routing.json, .app.json, .mcp.json, tests/test_v04_mobbin.py | SF4-009, SF4-011 | python3 tests/test_v04_mobbin.py | - |
| SF4-013 | Implement Foundation Contract and source-bound foundation evidence. Covers AC-24 through AC-28. | queued | delegate | scripts/starforge/lifecycle.py, fixtures/foundation/, tests/test_v04_lifecycle.py | SF4-008, SF4-010 | python3 tests/test_v04_lifecycle.py | - |
| SF4-014 | Implement Delivery Contract, delivery evidence, and delivery-target validation. Covers AC-44 through AC-48. | queued | delegate | scripts/starforge/lifecycle.py, fixtures/delivery/, tests/test_v04_lifecycle.py | SF4-013 | python3 tests/test_v04_lifecycle.py | - |
| SF4-015 | Add `intake`, `design`, `foundation`, and `deliver` transitions while retaining legacy phase compatibility. Covers the target lifecycle. | queued | delegate | scripts/star_forge.py, scripts/starforge/lifecycle.py, scripts/starforge/contracts.py, tests/test_v04_lifecycle.py | SF4-012, SF4-014 | python3 tests/test_v04_lifecycle.py | - |
| SF4-016 | Update the four skills to orchestrate the complete lifecycle from one invocation and use the capability router without adding skills. Covers AC-6, AC-19 through AC-34, AC-44 through AC-48. | queued | delegate | skills/forge/SKILL.md, skills/forge-plan/SKILL.md, skills/forge-work/SKILL.md, skills/forge-review/SKILL.md, skills/forge/references/capability-routing.md, tests/test_v04_skills.py | SF4-015 | python3 tests/test_v04_skills.py | - |
| SF4-017 | Implement evidence envelope v2 writer, reader, schema validation, and v1 compatibility adapters. Covers AC-35, AC-36. | queued | delegate | scripts/starforge/evidence.py, fixtures/evidence-v2/, tests/test_v04_evidence.py | SF4-008, SF4-010 | python3 tests/test_v04_evidence.py | - |
| SF4-018 | Adapt browser and preview proof to the envelope and route in-app Browser first with Playwright fallback. Covers AC-29, AC-33, AC-35. | queued | delegate | scripts/live_collectors/browser_playwright.py, scripts/live_collectors/preview.py, tests/test_live_browser_playwright.py, tests/test_live_preview.py | SF4-017 | python3 tests/test_live_browser_playwright.py && python3 tests/test_live_preview.py | - |
| SF4-019 | Adapt iOS and macOS proof to the envelope and capability routes. Covers AC-30, AC-31, AC-35. | queued | delegate | scripts/live_collectors/native_ios.py, scripts/live_collectors/native_macos.py, tests/test_live_native_ios.py, tests/test_live_native_macos.py | SF4-017 | python3 tests/test_live_native_ios.py && python3 tests/test_live_native_macos.py | - |
| SF4-020 | Adapt security and GitHub proof to the envelope, including Codex Security and Foundation provenance. Covers AC-26, AC-27, AC-34 through AC-36. | queued | delegate | scripts/live_collectors/security_adapter.py, scripts/live_collectors/github_pr.py, tests/test_live_security_adapter.py, tests/test_live_github_pr.py | SF4-013, SF4-017 | python3 tests/test_live_security_adapter.py && python3 tests/test_live_github_pr.py | - |
| SF4-021 | Add Expo and platform route contracts without a new collector; require standard task evidence plus delivery proof. Covers AC-32, AC-35. | queued | delegate | config/capability-routing.json, skills/forge/references/capability-routing.md, tests/test_v04_routing.py, tests/test_v04_evidence.py | SF4-016, SF4-017 | python3 tests/test_v04_routing.py && python3 tests/test_v04_evidence.py | - |
| SF4-022 | Implement deterministic adaptive review-role selection with a four-agent cap. Covers AC-37 through AC-42. | queued | delegate | scripts/starforge/review_policy.py, scripts/star_forge.py, agents/reviewer/agent.md, tests/test_v04_review_policy.py | SF4-008, SF4-015 | python3 tests/test_v04_review_policy.py | - |
| SF4-023 | Enforce risk floors so Fast MVP cannot omit required security, privacy, UX, or delivery review. Covers AC-38, AC-39, AC-43. | queued | delegate | scripts/starforge/review_policy.py, scripts/star_forge.py, tests/test_v04_review_policy.py | SF4-022 | python3 tests/test_v04_review_policy.py | - |
| SF4-024 | Add dedicated UI originality, accessibility, and visual-quality reviewer instructions. Covers AC-11, AC-38. | queued | delegate | agents/reviewer/agent.md, skills/forge-review/SKILL.md, tests/test_v04_skills.py | SF4-012, SF4-022 | python3 tests/test_v04_skills.py | - |
| SF4-025 | Implement change-packet schema, template, approval state, and history lookup. Covers AC-49, AC-51. | queued | delegate | scripts/starforge/changes.py, templates/Change.md, templates/ChangePlan.md, tests/test_v04_changes.py | SF4-008 | python3 tests/test_v04_changes.py | - |
| SF4-026 | Replace automatic solo amendment rows with risk-aware change packets and affected proof derivation. Covers AC-49, AC-50. | queued | delegate | scripts/starforge/changes.py, scripts/star_forge.py, tests/test_v04_changes.py | SF4-023, SF4-025 | python3 tests/test_v04_changes.py | - |
| SF4-027 | Add migration tests for legacy plans, amendment rows, reviews, completion proofs, and evidence. Covers AC-18, AC-36, AC-51. | queued | delegate | fixtures/legacy-v03/, tests/test_v04_migration.py, scripts/starforge/contracts.py, scripts/starforge/changes.py, scripts/starforge/evidence.py | SF4-017, SF4-026 | python3 tests/test_v04_migration.py | - |
| SF4-028 | Broaden source classification, generated-code exclusions, and architecture-debt scanning. Covers AC-52, AC-53. | queued | delegate | scripts/starforge/quality.py, scripts/star_forge.py, tests/test_v04_quality.py | SF4-027 | python3 tests/test_v04_quality.py | - |
| SF4-029 | Add opt-in, provenance, redaction, and poisoning protections to global learnings. Covers AC-56. | queued | delegate | scripts/starforge/learnings.py, scripts/star_forge.py, skills/forge-plan/SKILL.md, tests/test_v04_learnings.py | SF4-028 | python3 tests/test_v04_learnings.py | - |
| SF4-030 | Extract cohesive runtime modules after characterization tests, keep CLI compatibility, and enforce size budgets. Covers AC-54, AC-55. | queued | delegate | scripts/star_forge.py, scripts/starforge/, tests/test_star_forge.py, tests/test_v04_quality.py | SF4-020, SF4-023, SF4-026, SF4-029 | python3 tests/test_star_forge.py && python3 tests/test_v04_quality.py | - |
| SF4-031 | Build end-to-end fixture projects for web, iOS, macOS, Expo, CLI, Fast MVP risk floor, and amendment flows. Covers AC-24 through AC-58. | queued | delegate | fixtures/v04-projects/, tests/test_v04_e2e.py, scripts/check.sh | SF4-021, SF4-024, SF4-027, SF4-030 | python3 tests/test_v04_e2e.py | - |
| SF4-032 | Update public workflow, installation, proof recipes, security, migration, capability, and troubleshooting documentation. Covers AC-1, AC-5, AC-19 through AC-23, AC-44 through AC-48. | queued | docs | README.md, docs/install.md, docs/workflow.md, docs/proof-recipes.md, docs/validation.md, docs/faq.md, docs/migration-v04.md | SF4-031 | noop | - |
| SF4-033 | Dogfood v0.4 on Star Forge itself from interview through delivery, including one real change packet, and record the report. Covers AC-1 through AC-58. | queued | delegate | docs/v04-dogfood-report.md, fixtures/v04-dogfood/, tests/test_v04_e2e.py | SF4-031, SF4-032 | scripts/release-check.sh | - |
| SF4-034 | Perform isolated clean install, legacy upgrade, Mobbin OAuth, private-repository fixture, and platform smoke matrix. Covers AC-1, AC-5, AC-10, AC-24 through AC-36, AC-44 through AC-48. | queued | delegate | scripts/release-check.sh, tests/test_v04_release.py, docs/v04-release-candidate.md | SF4-033 | scripts/release-check.sh | - |
| SF4-035 | Publish v0.4.0 metadata and final migration notes only after every release gate passes. Covers AC-2, AC-3, AC-58. | queued | solo | .codex-plugin/plugin.json, CHANGELOG.md, docs/migration-v04.md | SF4-034 | scripts/release-check.sh | - |

## 11. Recommended delivery sequence

Use six reviewable pull requests or equivalent local integration checkpoints:

1. **Release hygiene:** SF4-001 through SF4-005
2. **Contracts and routing:** SF4-006 through SF4-012
3. **Lifecycle and evidence:** SF4-013 through SF4-021
4. **Adaptive review and amendments:** SF4-022 through SF4-027
5. **Quality and lean refactor:** SF4-028 through SF4-030
6. **End-to-end validation and release:** SF4-031 through SF4-035

Every checkpoint must keep all previously passing tests green. Do not postpone
compatibility fixes to the final release checkpoint.

## 12. Verification strategy

### Automated

- Existing nine test suites.
- New focused v0.4 suites named in the implementation ledger.
- Manifest, marketplace, MCP/app mapping, hooks, and routing JSON validation.
- Generated agent drift check.
- Plan v1 and v2 parser matrix.
- Blueprint lock mutation tests.
- Foundation and Delivery fixture tests with no real external writes in CI.
- Evidence v1 and v2 compatibility tests.
- Risk-derived reviewer-role matrix.
- Source-layout fixtures for Python, JavaScript, Swift, Kotlin, Rust, Go, and
  monorepos.
- Production-line and module-size budget checks.

### Manual release matrix

1. Web app with Mobbin research, private GitHub repository, Browser QA, security
   scan, and Vercel preview.
2. Simple internal web app routed to Sites rather than Vercel.
3. iOS app built, launched, tested, and visually inspected through XcodeBuildMCP and
   Simulator.
4. macOS app built, run, tested, and packaged without assuming signing authority.
5. Expo app with official plugin available and with the documented fallback.
6. Backend or CLI project where design is not applicable.
7. Fast MVP containing auth and user data, proving the security risk floor remains.
8. Completed project edited afterward, proving a scoped change packet replaces a
   generic solo amendment.
9. Upgrade from the currently installed v0.3 local marketplace.
10. Clean install in an isolated `CODEX_HOME`.

## 13. Release gates

The release is blocked unless all are true:

- All AC-1 through AC-58 have recorded evidence.
- `scripts/release-check.sh` passes on macOS and Linux.
- All existing 321 tests remain passing and every new v0.4 test passes.
- Clean install and legacy upgrade both select the GitHub-backed canonical plugin.
- Active plugin version and source bytes match the release commit.
- No stale generated agent instructions remain.
- Mobbin works through the selected supported connection path, or its unavailable
  behavior is proven.
- The web, iOS, CLI, Fast MVP, and amendment dogfood scenarios pass.
- Required security scan has no unresolved medium, high, or critical finding.
- `scripts/star_forge.py` and production Python remain within the size budgets.
- The final tree is clean and the manifest version is `0.4.0` with a current
  cachebuster if Codex still requires one.

## 14. Principal risks and mitigations

| Risk | Mitigation |
|---|---|
| Plugin aliases or tool schemas change | Data-driven routing, discovery, and explicit fallback tests |
| Mobbin duplicates an existing App connection | Compatibility test first; package `.app.json` or `.mcp.json`, never both |
| External writes occur without clear intent | Blueprint Delivery and Repository contracts provide the authority boundary |
| GitHub connector still cannot create repositories | Use only the narrow `gh repo create --private` fallback |
| Plan v2 breaks existing projects | Dual reader, explicit migration draft, legacy fixtures |
| Blueprint lock is mistaken for host attestation | Document it as drift protection, not cryptographic proof of human identity |
| New phases make the loop feel slower | Skip non-applicable phases and ask only material interview questions |
| Capability routing becomes a dependency graph | Store preferences, not required plugin installations |
| Evidence standardization causes a large rewrite | Adapt existing manifests incrementally |
| Runtime extraction creates regressions | Add characterization tests before moving code |
| Global learnings leak or poison future work | Opt-in, provenance, redaction, and abstract-rule-only validation |
| Delivery requires unavailable credentials | Produce one explicit blocker and preserve all completed local work |

## 15. Definition of done

Star Forge v0.4 is complete only when a user can invoke `$forge` with a software idea,
answer a focused interview, approve one complete Blueprint, and receive the
contracted software output with:

- A locked and traceable specification
- A private GitHub repository when requested
- The right official capabilities selected for the platform
- Fresh platform-specific tests and interaction evidence
- Adaptive quality, security, UX, and architecture review
- The requested preview, package, repository handoff, or production delivery
- A final source-bound proof that refuses unsupported completion claims

The result must preserve Star Forge's current evidence strength while remaining a
four-skill, two-agent control plane.
