# Validation

Star Forge validates the package, project contracts, current-source evidence, and
release metadata separately. A passing test command is necessary but does not
replace foundation, live, review, or delivery proof.

## Project Checks

Inspect state without changing it:

```sh
python3 scripts/star_forge.py status --project .
```

Validate Blueprint AC coverage, Plan v2 proof kinds, dependencies, delivery
consistency, and evidence state:

```sh
python3 scripts/star_forge.py validate-plan \
  --file Plan.md \
  --project . \
  --strict
```

Scan recognized source layouts and architecture debt:

```sh
python3 scripts/star_forge.py quality \
  --project . \
  --include-files \
  --strict
```

Compute the final gate:

```sh
python3 scripts/star_forge.py done \
  --project . \
  --strict \
  --write-summary
```

`done --strict` refuses stale source binding, missing required proof, incomplete
foundation or delivery, stale review, unresolved findings, and a dirty tree.

## Installation Diagnostics

The doctor is read-only:

```sh
python3 scripts/star_forge.py doctor \
  --source-root . \
  --strict
```

It reports canonical marketplace drift, duplicate installs, active runtime drift,
stale hook trust, and duplicate Mobbin connections. It does not apply remediation.

## Package Checks

Run the development suite:

```sh
scripts/check.sh
```

Run the strict package self-test:

```sh
python3 scripts/star_forge.py self-test --strict
```

Run the release gate:

```sh
scripts/release-check.sh
```

The release wrapper includes package and hook JSON checks, generated-agent drift,
runtime and collector compilation, installer syntax, focused tests, size budgets,
smoke projects, and the strict self-test. Publishable source changes also require
a new plugin version or cachebuster.

## Focused v0.4 Suites

Useful checks while editing a specific surface:

```sh
python3 tests/test_v04_release.py
python3 tests/test_v04_doctor.py
python3 tests/test_v04_contracts.py
python3 tests/test_v04_mobbin.py
python3 tests/test_v04_routing.py
python3 tests/test_v04_lifecycle.py
python3 tests/test_v04_evidence.py
python3 tests/test_v04_review_policy.py
python3 tests/test_v04_changes.py
python3 tests/test_v04_migration.py
python3 tests/test_v04_e2e.py
```

Platform proof also has focused live collector tests under `tests/test_live_*.py`.

## Release Matrix

Before a v0.4 release, verify:

- clean canonical GitHub marketplace install in an isolated Codex home
- upgrade from a v0.3 local marketplace
- Mobbin OAuth connection and unavailable behavior
- new private GitHub foundation and existing-repository adoption
- web Browser QA plus Sites and Vercel contract selection
- iOS XcodeBuildMCP and Simulator proof
- macOS build, run, test, UI, signing, and packaging routes as applicable
- Expo plugin route and repository-native fallback
- CLI or backend project with design marked not applicable
- Fast MVP with auth or user data retaining its security review floor
- post-completion change packet repeating affected proof and delivery

Never use fixtures as release evidence. Never place credentials, private content,
or production data in tracked proof.

## Hook Packaging

Hooks are bundled at `hooks/hooks.json`. The plugin manifest intentionally has no
`hooks` field. Hook records support continuity and diagnostics, but are not
host-controlled witnesses and do not grant an unqualified completion verdict.
