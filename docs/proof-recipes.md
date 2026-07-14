# Proof Recipes

Collectors supply artifacts. Star Forge proof commands decide whether those
artifacts can count for a task. The usual pattern is:

1. Start from a task id in `Plan.md`.
2. Collect evidence under `.starforge/live/<task-id>/<collector>/`.
3. Run the strict proof command printed by the collector.
4. Record task verification with `verify --strict`.
5. Complete the task with `complete-task`.

Do not use fixtures as release proof. Fixtures exist for tests.

## Local Web UI

Use this for Vite, Next, Remix, Rails, Django, or any local app with a loopback
URL.

```sh
npm run dev -- --host 127.0.0.1 --port 4173
```

Claim the server lease with the live server process id:

```sh
python3 scripts/star_forge.py server-lease \
  --project . \
  --action claim \
  --base-url http://127.0.0.1:4173 \
  --port 4173 \
  --pid <server-pid> \
  --command "npm run dev -- --host 127.0.0.1 --port 4173"
```

Run the browser collector:

```sh
python3 scripts/live_collectors/browser_playwright.py \
  --project . \
  --task SF-123 \
  --url http://127.0.0.1:4173 \
  --scenario path/to/scenarios.json#happy \
  --server-lease .starforge/runtime/server.json
```

Then run the strict `browser-run` command printed by the collector.

## Deployed Preview

Use this when a preview URL already exists. The collector never deploys.

```sh
python3 scripts/live_collectors/preview.py \
  --project . \
  --task SF-123 \
  --url https://preview.example.com \
  --deployment-id dep-123 \
  --deployment-source-hash <current-source-hash> \
  --smoke-check "contains:Ready"
```

Then run the strict `preview-proof` command printed by the collector. A preview
must be bound to source through a deployment source hash or commit SHA.

## GitHub PR Review Packet

Use this for read-only PR evidence. Production proof requires live connector input
or a live read-only `gh` export directory. Fixture packets are rejected for release
proof.

```sh
python3 scripts/live_collectors/github_pr.py \
  --project . \
  --task SF-123 \
  --repo owner/repo \
  --pr 42 \
  --connector-input github-pr-live.json
```

Then run the strict source packet commands printed by the collector.

## Native iOS

Use XcodeBuildMCP to collect the native evidence, then hand the exported artifacts
to the collector. The collector does not call Xcode or shell fallbacks.

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

The transcript must include `session_show_defaults` before native actions.

## Native macOS

Use explicit JSON argv arrays. Shell strings are intentionally rejected.

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

Then run the strict `native-macos-proof` command printed by the collector.

## Security Scanner Handoff

Use this when a trusted scanner has produced a report.

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

Every security proof needs normalized findings, scanner provenance, scan scope,
input hash, redaction report, and source or commit binding.

## Complete The Task

After proof is recorded, record the task's declared verify command:

```sh
python3 scripts/star_forge.py verify \
  --project . \
  --task SF-123 \
  --command "<exact Verify cell command>" \
  --strict
```

Then complete the task:

```sh
python3 scripts/star_forge.py complete-task \
  --project . \
  --task SF-123 \
  --changed-file path/to/file \
  --summary "What shipped"
```
