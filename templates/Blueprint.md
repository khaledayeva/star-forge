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

## Tech Stack

Record the intended stack, package managers, app framework, mobile stack, backend stack, test stack, and deployment assumptions.

## Architecture Expectations

Define project structure, important boundaries, preferred services/helpers, and patterns that should be reused.

## Security Expectations

Define expectations for auth, sensitive user data, API boundaries, dependency risk, secrets, and destructive operations.

## Performance Expectations

Define expected load, latency, responsiveness, memory, battery, and mobile/web performance constraints.

## UI Design Direction

Describe visual direction, interaction density, layout expectations, and important brand/product signals.

## UX Quality Bar

For web and mobile apps, define the minimum acceptable experience for navigation, responsive behavior, accessibility, loading states, empty states, errors, and visual verification.

## UX Standards

Describe how the product should feel to use and what workflows must remain efficient.

## Non-Goals

List what should not be built or optimized yet.

## Acceptance Criteria

Give each criterion a stable `AC-n` id so the review wave can check the build against it.

- AC-1: <observable, testable criterion>
- AC-2: <observable, testable criterion>

## Definition Of Done

A change is done only when every acceptance criterion is met, each task carries a fresh passing `verify` (and `browser-run` for UI), the review wave's fix queue is empty or waived, the tree is clean, and `done --strict` returns COMPLETE.
