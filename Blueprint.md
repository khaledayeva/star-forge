# Blueprint.md

Status: approved
Owner: Khaled Ayeva
Last approved: 2026-07-25

## Product Summary

Star Forge v0.4 is a lean Codex-native software-factory control plane. One `$forge`
invocation interviews the user, proposes design directions when UI applies, locks
one approved contract, establishes Git and the requested private GitHub repository,
routes work through the best available official capabilities, builds and verifies
the software, performs adaptive review, satisfies the Delivery Contract, and proves
completion from current source-bound evidence.

## Authoritative Contract

The complete normative contract, architecture, migration policy, routing catalog,
task ledger, risks, and release gates are defined in:

`docs/star-forge-v0.4-implementation-plan.md`

If this summary and that document conflict, the detailed v0.4 implementation plan
wins.

## Product Goal

Expand the current evidence-focused Forge Loop into the full software factory
described by the user without turning Star Forge into a large collection of
stack-specific agents, skills, or duplicated tool implementations.

## Target Lifecycle

`intake -> design -> plan -> foundation -> build -> review -> deliver -> done`

Post-completion source changes enter a scoped `amend` change packet and repeat the
affected gates.

## Architecture

- Star Forge owns contracts, lifecycle state, capability routing, proof envelopes,
  review policy, migration, and final completion.
- Dedicated Codex plugins, MCP servers, connectors, and native tools own their
  specialist operations.
- Four skills and two agent roles remain the public orchestration surface.
- A data-driven routing catalog selects capabilities without installing optional
  plugins automatically.
- Existing live collectors remain proof adapters and CI fallbacks.
- New evidence uses one versioned envelope with v0.3 compatibility.
- GitHub is the canonical plugin distribution source.

## Delivery Contract

- Star Forge plugin delivery: GitHub-backed Codex plugin release.
- Repository: `khaledayeva/star-forge`.
- Repository visibility: public for Star Forge itself.
- Implementation branch: `codex/star-forge-v0.4`.
- Publish or push: not authorized by this Blueprint; local implementation and local
  commits are authorized.
- Release target: `0.4.0`.

## Design Applicability

The Star Forge plugin has no product UI in this increment. Mobbin, Figma, and
ImageGen are capabilities Star Forge must route for downstream UI projects, but no
new Star Forge interface design is required.

## Security and Privacy

- No credentials, OAuth tokens, screenshots containing private material, or project
  content may enter tracked fixtures or global learnings.
- External writes must be authorized by the approved Repository and Delivery
  contracts.
- Plugin and MCP connections use official authentication paths.
- Security-sensitive downstream projects use Codex Security when available and
  preserve normalized source-bound proof.

## Performance and Leanness

- Keep four skills and two agent roles.
- Keep production Python at or below 18,000 lines unless a documented deletion or
  consolidation plan is approved.
- Reduce `scripts/star_forge.py` below 2,500 lines.
- Keep extracted runtime modules below 1,200 lines unless generated-code exemption
  applies.
- Do not add one collector, skill, or agent per external plugin.

## Non-Goals

- Reimplementing official plugins, MCP clients, deployment providers, or Xcode
  automation.
- Making workplace connectors default dependencies.
- Automatically creating public repositories or deployments.
- Changing billing, signing identities, production data, or repository visibility.
- Reintroducing the former attestation factory.
- Claiming unsupported host-controlled witness guarantees.

## Acceptance Criteria

The detailed text for each criterion is normative in the authoritative contract.

- AC-1: GitHub is the canonical clean-install marketplace source.
- AC-2: Release validation enforces a new plugin version or cachebuster.
- AC-3: Published manifest metadata and package surfaces are complete.
- AC-4: Generated agent configurations cannot drift from canonical prompts.
- AC-5: A read-only doctor diagnoses stale and duplicate installations.
- AC-6: New projects receive an adaptive material-decision interview.
- AC-7: Explicit assumptions replace unnecessary interview questions.
- AC-8: UI projects receive grounded design directions when tools are available.
- AC-9: Design research is provider-neutral.
- AC-10: Mobbin uses supported OAuth rather than stored API keys.
- AC-11: Design references become original Borrow and Avoid constraints.
- AC-12: Design selection remains inside the one Blueprint approval.
- AC-13: Blueprint approval is content-hash locked.
- AC-14: Plan v2 adds ACs and Proof columns.
- AC-15: Every criterion and task is mechanically traceable.
- AC-16: Proof kinds use a validated vocabulary.
- AC-17: Plan validation rejects inconsistent contracts and proofs.
- AC-18: Legacy plans migrate without invented mappings.
- AC-19: Capability routing is data-driven.
- AC-20: Routing prefers dedicated capabilities over generic fallbacks.
- AC-21: Missing capabilities and fallbacks are explicit.
- AC-22: Plugin alias changes normally update data rather than lifecycle code.
- AC-23: Optional plugin installation always requires user action.
- AC-24: Local Git initialization remains automatic.
- AC-25: Approved GitHub foundations create private repositories and CI.
- AC-26: GitHub connector is preferred with a narrow repository-creation fallback.
- AC-27: Foundation evidence proves remote identity and visibility.
- AC-28: Existing repositories are adopted without implicit mutation.
- AC-29: Web builds use official guidance and in-app Browser QA.
- AC-30: iOS proof uses XcodeBuildMCP and Simulator evidence.
- AC-31: macOS proof uses appropriate native build and UI capabilities.
- AC-32: React Native routes to the official Expo plugin when available.
- AC-33: Chrome is reserved for authenticated or extension-dependent state.
- AC-34: Security-sensitive projects use Codex Security when available.
- AC-35: Live proof adapts to evidence envelope v2.
- AC-36: Evidence v1 remains readable during migration.
- AC-37: Correctness review is always required.
- AC-38: UI work requires UX and accessibility review.
- AC-39: Security and privacy review follows deterministic risk flags.
- AC-40: Architecture review follows complexity flags.
- AC-41: Performance and reliability review follows contract risk.
- AC-42: Adaptive review uses no more than four agents.
- AC-43: Fast MVP cannot remove risk-required review.
- AC-44: Delivery targets are explicit and validated.
- AC-45: Sites and Vercel are selected by fit, never both by default.
- AC-46: Delivery proof is source-hash bound.
- AC-47: Strict completion requires the approved delivery result.
- AC-48: Missing authority or credentials becomes one honest blocker.
- AC-49: New amendments use isolated change packets.
- AC-50: Amendment delegation and verification derive from affected scope.
- AC-51: Historical v0.3 amendment rows remain readable.
- AC-52: Quality scanning recognizes common source layouts.
- AC-53: Star Forge scans its own runtime for large-file debt.
- AC-54: The CLI runtime is split within explicit module-size budgets.
- AC-55: Production Python remains within the v0.4 size budget.
- AC-56: Global learnings are opt-in, provenance-labeled, and redacted.
- AC-57: `--no-hooks` and `--no-agents` have distinct meanings.
- AC-58: Existing and new tests pass on macOS and Linux.

## Definition of Done

Every task in Plan.md must be complete with fresh evidence, every AC must be covered,
the adaptive review queue must be empty, the required delivery proof must pass, the
tree must be clean, and `done --strict` must report its exact completion verdict.
