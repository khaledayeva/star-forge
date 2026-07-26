# Live Tools Dogfood

Use this workflow when dogfooding a live collector on a real task. The goal is to leave evidence that another reviewer can replay and challenge.

## Evidence Workflow

1. Start from a task id in `Plan.md`.
2. Run the collector for that task without `--record`.
3. Inspect `.starforge/live/<task-id>/<collector>/manifest.json`.
4. Inspect the printed strict proof command.
5. Run the printed proof command yourself.
6. Keep the collector output and proof output in the task handoff.
7. Run `scripts/check.sh` before asking the coordinator to verify.

The collector output alone is not enough. The strict proof output is the release signal.

## What To Capture

For each dogfood run, record:

- task id
- collector command
- manifest path
- strict proof command
- strict proof result
- degraded state
- blocking problem rules, if any
- paths to the key artifacts

Use this compact format in handoff notes:

```text
Collector: browser
Task: SF-123
Collector command: python3 scripts/live_collectors/browser_playwright.py ...
Manifest: .starforge/live/SF-123/browser/manifest.json
Strict proof: python3 scripts/star_forge.py browser-run ... --live-manifest .starforge/live/SF-123/browser/manifest.json --strict
Result: PASS
Artifacts: desktop.png, mobile.png, interaction.json, console.json
Notes: no degraded state, no blocking problems
```

## Good Dogfood Evidence

Good evidence is falsifiable:

- screenshots or UI snapshots show the actual app state
- console and interaction artifacts include observable assertions
- preview evidence is tied to a source-bound deployment identity, and loopback previews include a scoped server lease artifact
- native evidence includes build, launch, test, and visible UI artifacts where applicable
- strict security evidence requires independently verifiable host provenance,
  scan scope, input hash, normalized findings, redaction report, and fresh source
  or commit binding even when findings are only low or info severity
- GitHub PR evidence is fresh against base and head refs, includes completed passing checks bound to the captured head SHA, records positive live provenance, and includes hash-bound packet artifacts

## Red Flags

Treat these as blockers until strict proof says otherwise:

- a collector manifest outside `.starforge/live/<task-id>/<collector>/`
- a fixture path used directly as release proof
- `degraded: true`
- any unavailable capability required for the proof
- source hash or runtime hash mismatch
- missing desktop or mobile browser evidence
- browser proof without the task-scoped live manifest
- browser or preview proof with a private network, metadata, link-local, reserved, multicast, or DNS-resolved unsafe target
- loopback preview proof without a manifest-recorded server lease artifact
- preview URL without a source-bound deployment identity
- native iOS transcript missing `session_show_defaults`, explicit XcodeBuildMCP provenance, exporter identity, or current source hash
- macOS evidence built from shell strings instead of JSON argv arrays
- security report or handoff packet outside `.starforge/live/<task-id>/security/`
- security report without independently verifiable host provenance, input hash
  artifact, normalized findings, redaction report, or fresh source or commit
  binding
- GitHub evidence with stale refs, partial permissions, incomplete pagination, pending checks, checks bound to the wrong head SHA, fixture-only provenance, or missing packet artifact hashes

## Fixture Limits

Fixtures are for tests. They may prove that adapters normalize shapes, reject bad inputs, and fail closed. They do not prove a real task is complete.

Allowed fixture use:

- schema examples
- deterministic happy-path normalization tests
- negative tests for stale, unsafe, missing, or malformed evidence
- redaction and bounded log tests

Disallowed fixture use:

- release proof for a task
- reviewer replacement
- completion evidence without a strict proof command
- fixture-only GitHub packet used as a production-review handoff
- deployment identity for a remote preview
- proof that a local server is still live

## Before Coordinator Verify

Run:

```sh
python3 tests/test_live_collectors_integration.py
scripts/check.sh
```

Then hand the exact command results to the coordinator. Do not run `complete-task`; the coordinator owns verify and task completion.
