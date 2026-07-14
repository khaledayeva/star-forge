# Contributing To Star Forge

Thanks for helping improve Star Forge. Contributions are welcome when they make
the Forge Loop clearer, safer, easier to install, or more trustworthy in
practice.

## Before You Start

Open an issue before a large change so the intended behavior and proof model can
be discussed early. Small fixes, tests, and documentation improvements can go
straight to a pull request.

Please keep these design constraints intact:

- Completion is computed from repository evidence, not written as a claim.
- Verification records must be bound to the current source and the task's
  declared command.
- Reviewer findings must remain load-bearing inputs to the fix queue.
- Fixtures and degraded evidence must not pass release proof.
- Hooks remain optional observers and must not block normal editing.
- Remote mutations require explicit user intent.

## Development Setup

Star Forge has no required third-party Python package for its core test suite.
Use Python 3.10 or newer from the repository root.

Run the complete release gate:

```sh
scripts/release-check.sh
```

That command validates JSON and shell scripts, compiles the Python sources, runs
all test files, and executes the strict package self-test.

To test the plugin inside Codex, install the current checkout:

```sh
scripts/install-codex.sh
```

Start a new Codex thread after reinstalling so the updated skills, agents, and
hooks are loaded.

## Pull Requests

A useful pull request includes:

- a focused explanation of the behavior being changed
- tests for new proof rules or state transitions
- documentation updates when commands or installation steps change
- a passing `scripts/release-check.sh` result

Avoid unrelated formatting or cleanup in the same change. Proof and trust-model
changes should explain how stale, malformed, fixture-based, or source-mismatched
evidence is handled.

By contributing, you agree that your contribution is licensed under the MIT
License included in this repository.
