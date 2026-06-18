# Live Tools

Star Forge live tools are artifact suppliers. They collect evidence, write it under `.starforge/live/<task-id>/<collector>/`, and print the strict Star Forge proof command that should consume that evidence. They do not complete tasks, update `Plan.md`, approve reviews, waive findings, or change the final completion predicate.

## Release Boundary

The release proof boundary is the Star Forge proof command, not the collector. A collector run is useful only when its manifest passes strict proof.

Every live manifest must include:

- `schema: star-forge.live-manifest.v1`
- the collector name and task id
- source hashes before and after collection
- the runtime asset hash
- artifact records and raw artifact hashes
- redaction counts
- degraded state, unavailable capabilities, and blocking problems

Strict proof fails closed when evidence is missing, malformed, outside the task-scoped live directory, degraded, stale, source-mismatched, runtime-mismatched, or marked with blocking problems.

## Artifact Layout

Collectors write task-scoped output:

```text
.starforge/live/<task-id>/<collector>/
  manifest.json
  ...
```

Fixtures under `fixtures/` are not release proof. They are allowed for schema coverage, happy-path normalization, and negative-path tests. Fixture-sourced GitHub PR packets are rejected by strict production-review proof even after collection, and fixture-only GitHub collection does not emit production proof commands.

## Collectors

### Browser

The browser collector runs a declarative Playwright scenario and hands screenshots, interaction observations, and console evidence to `browser-run --strict`.

```sh
python3 scripts/live_collectors/browser_playwright.py \
  --project . \
  --task SF-123 \
  --url http://127.0.0.1:4173 \
  --scenario fixtures/sloppy-web-app/live-browser-scenarios.json#happy \
  --server-lease .starforge/runtime/server.json
```

For loopback local URLs, use a Star Forge server lease. The lease binds the URL origin, port, process, source hash, and runtime asset hash to the current project. Server leases are not accepted for private network, link-local, reserved, multicast, metadata, or public remote targets. Add `--record` only when you want the collector to invoke the printed proof command after writing artifacts.

Claim local leases with a live server PID:

```sh
python3 scripts/star_forge.py server-lease \
  --project . \
  --action claim \
  --base-url http://127.0.0.1:4173 \
  --port 4173 \
  --pid <server-pid> \
  --command "npm run dev -- --host 127.0.0.1 --port 4173"
```

The printed `browser-run` proof command includes `--live-manifest` and, for loopback local targets, the lease path. Strict browser proof validates the live manifest, artifact hashes, console JSON, interaction JSON, screenshots, source hash, runtime hash, URL provenance, DNS-aware URL safety, and lease binding.

### Preview

The preview collector performs read-only HTTP checks against an existing URL. It never deploys or calls provider CLIs.

```sh
python3 scripts/live_collectors/preview.py \
  --project . \
  --task SF-123 \
  --url https://preview.example.com \
  --deployment-id dep-123 \
  --deployment-source-hash <current-source-hash> \
  --smoke-check "contains:Ready"
```

Use a real source-bound deployment identity, usually `--deployment-source-hash` or `--deployment-commit-sha`. Loopback local previews need `--server-lease`; the collector validates the same project, origin, command, live PID, source hash, runtime asset hash, and loopback scope required by browser proof before it makes any local request. The collector records a scoped `server-lease.json` artifact in the live manifest and raw hashes. `--local-preview-mode` is diagnostic only and does not authorize loopback requests without a strict lease. Signed preview URL query fields and signed request headers for S3, GCS, Azure SAS, and generic signature keys are rejected before any request and redacted in artifacts. For `http` URLs, preview fetch resolves once, rejects unsafe addresses, connects to a vetted IP, preserves the original `Host` header, and records `connected_ips` in `http.json`. `https` preview fetch is fail-closed until SNI-safe connection pinning is available. Strict preview proof rechecks URL provenance, final URLs, redirects, connected IP evidence when present, manifest artifact records, raw hashes, and current artifact bytes with the collector DNS-aware safety model. `--record` uses the resolved absolute project path while the printed command stays convenient for the caller shell.

### Native iOS

The iOS adapter normalizes agent-exported XcodeBuildMCP evidence. It does not call Xcode, `xcrun`, `simctl`, or shell fallbacks.

```sh
python3 scripts/live_collectors/native_ios.py \
  --project . \
  --task SF-123 \
  --scheme App \
  --simulator "iPhone 16" \
  --app-identity com.example.App \
  --mcp-transcript native-ios-inputs/mcp-transcript.json \
  --build-result native-ios-inputs/build.json \
  --launch-result native-ios-inputs/launch.json \
  --test-result native-ios-inputs/test.json \
  --screenshot native-ios-inputs/screenshot.png
```

The transcript must include `session_show_defaults` before native actions. A screenshot or UI snapshot is required for UI proof. Strict proof requires explicit XcodeBuildMCP provenance, `tool_surface: mcp`, server `XcodeBuildMCP`, a non-empty exporter or agent id, and a source hash bound to the current project through the transcript or manifest summary.

### Native macOS

The macOS collector accepts explicit JSON argv arrays, runs bounded local commands, captures runtime observation, and writes metadata-only signing and packaging notes.

```sh
python3 scripts/live_collectors/native_macos.py \
  --project . \
  --task SF-123 \
  --app-name TestApp \
  --bundle-id com.example.TestApp \
  --build-command '["python3","-c","print(\"build ok\")"]' \
  --run-command '["python3","-c","print(\"READY\", flush=True); import time; time.sleep(5)"]' \
  --readiness-text READY \
  --app-bundle BuildProducts/TestApp.app
```

Commands are structured argv only. Shell commands, signing pipelines, notarization, and packaging mutation are intentionally out of scope.

### Security

The security adapter imports trusted scanner output or the documented Star Forge security schema, normalizes findings, records provenance, and prints handoff and proof commands.

```sh
python3 scripts/live_collectors/security_adapter.py \
  --project . \
  --task SF-123 \
  --profile security-diff \
  --input scanner-reports/report.json \
  --input-hash <report-sha256> \
  --source-hash <current-source-hash> \
  --scanner codex-security \
  --scanner-version 1.2.3
```

Every security proof requires the scoped adapter bundle: `handoff-input.json`, `input-hash.json`, `normalized-findings.json`, and `redaction-report.json`. Strict proof verifies trusted scanner schema provenance, ruleset, scan scope, fresh source or commit binding, the input hash against current bytes, manifest artifact records, raw artifact hashes, and that the proof command is using the manifest's normalized findings. Unknown or blocking severities fail strict proof. Low and info findings still require the full adapter bundle. Security handoff packets must be under `.starforge/live/<task-id>/security/` with a sibling scoped manifest.

### GitHub PR

The GitHub PR adapter creates a read-only source packet from production connector exports, production readonly `gh` export directories, or fixtures for tests. It binds evidence to base, head, current base, current head, and merge-base SHAs.

```sh
python3 scripts/live_collectors/github_pr.py \
  --project . \
  --task SF-123 \
  --repo owner/repo \
  --pr 42 \
  --connector-input github-pr-live.json
```

Use `--connector-fixture` or `--gh-fixture-dir` only for tests. Release-capable imports use `--connector-input` or `--gh-readonly-dir`; they must include non-fixture tool versions, positive live provenance, read-only operations or commands, and a collection timestamp. The collector writes `operation-transcript.json` and records every required packet file in the manifest artifacts and raw artifact hashes.

Allowed operations are reads only. CI log reads through `gh run view` or `gh api` Actions endpoints must name the requested repo and reference run or job ids present in check evidence bound to the captured head SHA. Failed checks, pending checks, stale SHAs, check runs not bound to the captured head SHA, partial permissions, incomplete pagination, unsafe `gh` commands, missing or mismatched packet hashes, or fixture provenance block strict proof. Production-review proof requires positive live GitHub provenance, repo and PR identity, freshness refs, timestamps, tool versions, and scoped hash-bound packet artifacts.

## Release Check

Run the full release wrapper before handing off a live tools change:

```sh
scripts/check.sh
```

The wrapper validates plugin JSON, compiles Star Forge and live collectors, runs the core Star Forge suite, and runs every live tools suite.
