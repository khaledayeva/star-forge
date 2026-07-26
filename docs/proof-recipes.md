# Proof Recipes

Star Forge separates capability output from proof. A plugin, MCP server, collector,
or builder may produce artifacts. The coordinator records the proof against the
current source and exact Plan task.

The common sequence is:

1. Confirm the task id and required `Proof` kinds in Plan v2.
2. Use the selected capability route.
3. Store live artifacts under `.starforge/live/<task>/<collector>/`.
4. Run the strict proof command, or use a collector's `--record` option.
5. Run the exact Plan `Verify` command.
6. Complete the task.

Fixtures and degraded evidence do not count as passing release proof.

## Local Web UI

The in-app Browser is the preferred route for interactive local QA. The Playwright
collector is the CI and headless fallback.

Start the application on loopback and claim the server lease:

```sh
python3 scripts/star_forge.py server-lease \
  --project . \
  --action claim \
  --base-url http://127.0.0.1:4173 \
  --port 4173 \
  --pid <server-pid> \
  --command "npm run dev -- --host 127.0.0.1 --port 4173"
```

When the fallback is selected, use a project-relative scenario JSON file:

```sh
python3 scripts/live_collectors/browser_playwright.py \
  --project . \
  --task SF-123 \
  --url http://127.0.0.1:4173 \
  --scenario tests/browser-scenarios.json#happy \
  --viewport desktop=1280x800 \
  --viewport mobile=390x844 \
  --server-lease .starforge/runtime/server.json \
  --record
```

Desktop and mobile viewports, interaction evidence, console evidence, current
source binding, and a valid lease are required for strict local proof. Release the
lease when the server stops:

```sh
python3 scripts/star_forge.py server-lease \
  --project . \
  --action release
```

Use Chrome only when the proof needs authenticated Chrome state or an extension.

## Existing Preview

The preview collector inspects an existing URL. It never creates a deployment.

```sh
python3 scripts/live_collectors/preview.py \
  --project . \
  --task SF-123 \
  --url https://preview.example.com \
  --expect-status 200 \
  --provider sites \
  --deployment-id dep-123 \
  --deployment-source-hash <current-source-hash> \
  --smoke-check "contains:Ready" \
  --record
```

Use `--deployment-commit-sha` instead of, or in addition to, the deployment source
hash when that is the provider's available binding. Preview proof must match the
approved Delivery Contract. A live URL by itself is not delivery proof.

## GitHub PR Packet

The GitHub plugin is preferred. The collector converts a connector file export or
a read-only `gh` export into a task-scoped diagnostic packet:

```sh
python3 scripts/live_collectors/github_pr.py \
  --project . \
  --task SF-123 \
  --repo owner/repo \
  --pr 42 \
  --connector-input github-pr-live.json
```

Use `--gh-readonly-dir` when the selected fallback is a live read-only `gh` export.
Both public import modes remain untrusted and do not emit production proof
commands. Fixture inputs are test-only. Foundation creation authority is separate
from this read-only PR recipe.

## Native iOS

Use Build iOS Apps and XcodeBuildMCP. Before the first build, run
`session_show_defaults`. Export the selected project or workspace, scheme,
Simulator, build, launch, test, and visual results for normalization:

```sh
python3 scripts/live_collectors/native_ios.py \
  --project . \
  --task SF-123 \
  --scheme App \
  --simulator "iPhone 16" \
  --app-identity com.example.App \
  --mcp-transcript native-ios-inputs/mcp-transcript.json \
  --session-defaults native-ios-inputs/session-defaults.json \
  --build-result native-ios-inputs/build.json \
  --launch-result native-ios-inputs/launch.json \
  --test-result native-ios-inputs/test.json \
  --screenshot native-ios-inputs/screenshot.png \
  --ui-snapshot native-ios-inputs/ui-snapshot.json \
  --record
```

The collector does not call Xcode or replace XcodeBuildMCP with a shell command.
If XcodeBuildMCP is unavailable, record that state explicitly. Strict iOS proof
cannot pass through an invented fallback.

## Native macOS

Prefer Build macOS Apps. Select the most specific UI, test, signing, and packaging
capabilities required by the contract. The collector accepts JSON argument arrays,
not shell strings:

```sh
python3 scripts/live_collectors/native_macos.py \
  --project . \
  --task SF-123 \
  --app-name TestApp \
  --bundle-id com.example.TestApp \
  --build-command '["swift","build"]' \
  --run-command '[".build/debug/TestApp"]' \
  --test-command '["swift","test"]' \
  --readiness-text READY \
  --app-bundle .build/TestApp.app \
  --build-provider build-macos-apps \
  --test-provider build-macos-apps \
  --record
```

Add `--required-capability signing`, `packaging`, `test`, or `ui-automation` when
the Delivery Contract requires it. Do not claim signing or notarization without
authority.

## React Native And Expo

React Native and Expo prefer the official Expo plugin. A discovered
repository-native Expo CLI workflow is a degraded fallback. Its selection does
not authorize installing Expo or changing the application stack.

There is no Expo-specific collector. Record:

- the exact task `Verify` command
- artifacts from the capability that actually ran
- separate current-source delivery proof when the target is Expo or React Native

The delivery envelope uses `kind: delivery`, capability
`expo-platform-delivery`, and the actual provider. Task verification alone never
proves platform delivery.

## Security

Security-sensitive projects prefer Codex Security. The public adapter normalizes
file exports without storing credentials or raw private content:

```sh
python3 scripts/live_collectors/security_adapter.py \
  --project . \
  --task SF-123 \
  --profile security-diff \
  --input scanner-reports/report.json \
  --input-hash <report-sha256> \
  --source-hash <current-source-hash> \
  --scanner codex-security \
  --scanner-version <version> \
  --record
```

File imports are intentionally untrusted because the report author can also
author its provenance fields. They may guide the security review and fallback
scanner workflow, but they cannot satisfy strict proof by themselves. Strict
security proof additionally requires an independently verifiable host-controlled
provenance boundary. The current public adapter exposes no caller-supplied receipt
escape hatch.

Security evidence still records scanner identity, version, scope, input hash,
normalized findings, redaction report, timestamps, and source or commit binding.
Never place OAuth tokens, access tokens, private screenshots, or unredacted
project secrets in tracked evidence.

## Record Plan Verification

After required live proof, run the exact command from the Plan `Verify` cell:

```sh
python3 scripts/star_forge.py verify \
  --project . \
  --task SF-123 \
  --command "<exact Plan Verify command>" \
  --strict
```

For a docs task whose Plan Verify cell is `noop`:

```sh
python3 scripts/star_forge.py verify \
  --project . \
  --task SF-123 \
  --noop \
  --summary "<why no executable check applies>" \
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

Source changes after proof make the affected evidence stale.
