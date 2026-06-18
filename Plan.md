# Plan.md

Status: active

This rolling task ledger is derived from Blueprint.md. Blueprint.md wins when the two conflict.

## Task Ledger

| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |
|------|-------------|--------|------|-------|---------|--------|----------|
| SF-001 | Build shared live artifact helpers and strict proof command surfaces for preview, generic profile proof, native iOS, native macOS, security, and GitHub source packets. | complete | delegate | scripts/star_forge.py, scripts/live_collectors/__init__.py, scripts/live_collectors/common.py, tests/test_live_proof_commands.py | - | python3 tests/test_star_forge.py && python3 tests/test_live_proof_commands.py | scripts/star_forge.py, scripts/live_collectors/__init__.py, scripts/live_collectors/common.py, tests/test_live_proof_commands.py |
| SF-002 | Implement Phase 1 local Playwright browser artifact supplier with declarative scenario validation, dependency degradation, server lease checks, trace warnings, and browser-run handoff. | complete | delegate | scripts/live_collectors/browser_playwright.py, tests/test_live_browser_playwright.py, fixtures/sloppy-web-app/live-browser-scenarios.json | SF-001 | python3 tests/test_live_browser_playwright.py | scripts/live_collectors/browser_playwright.py, tests/test_live_browser_playwright.py, fixtures/sloppy-web-app/live-browser-scenarios.json |
| SF-003 | Implement Phase 2 provider-neutral preview URL evidence collector with URL safety, redaction, source-bound deployment identity, smoke checks, and proof command generation. | complete | delegate | scripts/live_collectors/preview.py, tests/test_live_preview.py, fixtures/live-preview/ | SF-001 | python3 tests/test_live_preview.py | scripts/live_collectors/preview.py, tests/test_live_preview.py, fixtures/live-preview/ |
| SF-004 | Implement Phase 5 security scanner handoff adapter with trusted schema normalization, provenance, scope, redaction, severity rules, deterministic fingerprints, and strict handoff output. | complete | delegate | scripts/live_collectors/security_adapter.py, tests/test_live_security_adapter.py, fixtures/security-reports/ | SF-001 | python3 tests/test_live_security_adapter.py | scripts/live_collectors/security_adapter.py, tests/test_live_security_adapter.py, fixtures/security-reports/ |
| SF-005 | Implement Phase 6 GitHub PR evidence adapter with read-only allowlists, stale SHA checks, bounded log redaction, pagination handling, and source packet handoff. | complete | delegate | scripts/live_collectors/github_pr.py, tests/test_live_github_pr.py, fixtures/github-pr/ | SF-001 | python3 tests/test_live_github_pr.py | scripts/live_collectors/github_pr.py, tests/test_live_github_pr.py, fixtures/github-pr/ |
| SF-006 | Implement Phase 4 macOS baseline artifact collector with structured argv commands, runtime observation, app identity validation, metadata-only signing and packaging notes, and native macOS proof handoff. | complete | delegate | scripts/live_collectors/native_macos.py, tests/test_live_native_macos.py, fixtures/native-macos/ | SF-001 | python3 tests/test_live_native_macos.py | scripts/live_collectors/native_macos.py, tests/test_live_native_macos.py, fixtures/native-macos/ |
| SF-007 | Implement Phase 3 XcodeBuildMCP iOS evidence adapter with agent-mediated transcript validation, required tool ordering, no shell fallback, runtime and visible artifact validation, and native iOS proof handoff. | complete | delegate | scripts/live_collectors/native_ios.py, tests/test_live_native_ios.py, fixtures/native-ios/ | SF-001 | python3 tests/test_live_native_ios.py | scripts/live_collectors/native_ios.py, tests/test_live_native_ios.py, fixtures/native-ios/ |
| SF-008 | Implement Phase 7 verification and dogfood documentation, collector release gates, fixture limits, and full live tools check wrapper. | complete | delegate | docs/live-tools.md, docs/live-tools-dogfood.md, scripts/check.sh, tests/test_live_collectors_integration.py | SF-002, SF-003, SF-004, SF-005, SF-006, SF-007 | python3 tests/test_live_collectors_integration.py && scripts/check.sh | docs/live-tools.md, docs/live-tools-dogfood.md, scripts/check.sh, tests/test_live_collectors_integration.py |

## Columns

- **Status**: queued, ready, in_progress, blocked, reviewing, complete.
- **Mode**: `delegate` (spawn `starforge-builder` — required for real code), `solo` (coordinator may implement trivial glue), `docs` (no code; no-op verify allowed).
- **Files**: the files this task owns (used for parallel-safety and the builder prompt).
- **Verify**: the exact verification command, or `noop` for docs tasks.
- **Evidence**: filled by `complete-task`; do not hand-edit.

## Operating Rules

- Keep completed task detail concise. Update Plan.md after every meaningful step.
- `delegate` tasks must be implemented by a spawned `starforge-builder`, not inline.
- Every non-docs task needs a fresh passing `verify` run before `complete-task`.
- User-facing UI tasks need a passing `browser-run` (desktop + mobile, interaction + console evidence).
- A task is complete only through `complete-task`; the review wave and `done` run at the project level.
