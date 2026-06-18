# Blueprint.md

Status: approved
Owner: project team
Last approved: 2026-06-18

<!-- Approval sentinel: Star Forge treats this blueprint as approved only when the
     Status line reads exactly `Status: approved` or the Last approved line starts
     with an ISO date, e.g. `Last approved: 2026-06-10`. Keep both lines intact. -->

## Product Summary

Star Forge is a Codex-native software factory plugin. This build adds live tool artifact suppliers that collect real external evidence and hand it to existing Star Forge proof commands without changing the deterministic core.

## Product Goal

Make live browser, preview, native, security, and GitHub evidence available as local artifacts while keeping Star Forge authoritative for planning, verification, proof records, reviewer findings, fix queues, and final completion predicates.

## Target Users

Codex users who use Star Forge to plan, delegate, review, and prove software work through the Forge Loop.

## User-Facing Behavior

Users can run collector commands to write task-scoped artifacts under `.starforge/live/<task-id>/<collector>/`. Collectors print exact strict proof commands by default and invoke proof recording only with an explicit `--record` flag. Existing commands remain the only proof gates.

## Tech Stack

Python standard library first, with optional local tool integrations. Tests remain plain Python and run through `python3 tests/test_star_forge.py` plus focused live collector test modules.

## Architecture Expectations

Live code must be outside completion logic. Shared helpers belong under `scripts/live_collectors/`. Star Forge proof command additions belong in `scripts/star_forge.py`. Collectors write only local artifacts and manifests, fail closed on degraded evidence, and never mutate `Plan.md`, waive findings, approve work, replace reviewers, or decide completion.

## Security Expectations

Collectors must sanitize paths, redact secrets, avoid cookies and auth state, reject unsafe URLs, forbid remote writes except explicit read-only evidence collection, and record provenance, hashes, degraded state, and blocking problems for strict proof.

## Performance Expectations

Collectors should be bounded, deterministic, timeout-aware, and testable with fixtures. Optional expensive live tools must degrade clearly when unavailable.

## UI Design Direction

No new UI is required. Plugin metadata and docs should remain clear enough for Codex to surface Star Forge as a plugin and for users to understand the live tool boundaries.

## UX Quality Bar

Browser evidence must include desktop and mobile screenshots, console evidence, and falsifiable visual observations. Preview and native evidence must not pretend reachability or logs alone prove UI quality.

## UX Standards

Live tools should feel like proof suppliers in the Forge Loop, not a second workflow. Commands should explain degraded states and print the next strict proof command clearly.

## Non-Goals

No Slack intake, task packet intake, bug triage, Sentry, Linear, support tickets, scheduled sweeps, OpenAI API upgrade packets, deployment creators, signing pipelines, notarization pipelines, waiver systems, provider lock-in, or new completion predicate.

## Acceptance Criteria

Give each criterion a stable `AC-n` id so the review wave can check the build against it.

- AC-1: Shared live artifact helpers create task-scoped manifests with required fields, source and runtime hashes, raw artifact hashes, degraded state, unavailable capabilities, redaction reports, and blocking problems.
- AC-2: Existing proof commands are extended or added for live evidence handoff, and strict proof fails closed on missing, stale, malformed, degraded, source-mismatched, or runtime-mismatched artifacts.
- AC-3: The local Playwright browser supplier captures or validates browser artifacts through a declarative scenario contract and feeds only `browser-run --strict`.
- AC-4: The provider-neutral preview URL collector performs read-only URL evidence collection, URL safety checks, redaction, source-bound deployment identity validation, and preview proof handoff.
- AC-5: The XcodeBuildMCP iOS adapter treats MCP as agent-mediated evidence, validates transcript ordering and runtime artifacts, and never uses shell fallback for strict iOS proof.
- AC-6: The macOS baseline collector uses explicit structured local commands, bounded runtime observation, app identity, metadata-only signing and packaging notes, and strict native macOS proof handoff.
- AC-7: The security scanner adapter normalizes trusted scanner reports or the documented Star Forge schema with provenance, scope, staleness checks, redaction, deterministic fingerprints, and strict security handoff.
- AC-8: The GitHub PR evidence adapter performs only allowlisted read operations, binds PR evidence to fresh base and head SHAs, redacts bounded logs, and feeds source packet proof commands without remote mutation.
- AC-9: Verification and dogfood coverage proves collectors are artifact suppliers only, uses task-scoped paths, limits fixtures to schema and failure tests, and documents exact release gates.

## Definition Of Done

A change is done only when every acceptance criterion is met, each task carries a fresh passing `verify` (and `browser-run` for UI), the review wave's fix queue is empty or waived, the tree is clean, and `done --strict` returns COMPLETE.
