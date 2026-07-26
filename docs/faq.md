# FAQ And Troubleshooting

## Is Star Forge A Standalone CLI?

No. The Python runtime provides deterministic state, contracts, routing, and proof
checks. Codex supplies the skills, agents, plugins, MCP servers, Apps, and user
interaction that execute the Forge Loop.

## Why Does The Project Stay In Intake Or Plan?

Run:

```sh
python3 scripts/star_forge.py status --project .
python3 scripts/star_forge.py validate-plan --file Plan.md --project . --strict
```

Resolve every material intake decision, design applicability state, risk flag,
Repository Contract field, and Delivery Contract field. After the user approves
the complete Blueprint, record its lock with `approve-blueprint --project .`.
Editing Blueprint.md afterward intentionally invalidates the lock.

## Why Is A Preferred Plugin Missing?

Routing uses only capabilities discovered in the current host. Mentioning a plugin
in the Blueprint does not make it available. The route reports the missing
preferred option and either selects an accepted fallback or blocks.

Optional installation is suggestion-only. Star Forge never installs or connects a
plugin automatically. Start a new Codex task after installing a capability, then
rerun `$forge` so routing can discover it.

## How Do I Connect Mobbin?

In Codex Desktop, connect the Mobbin App in ChatGPT through OAuth, then retry in a
new or refreshed Codex task.

For Codex CLI:

```sh
codex mcp add mobbin --url https://api.mobbin.com/mcp
codex mcp login mobbin
```

Do not create a repository `.mcp.json` or store an API key. Star Forge packages
the registered App binding only. If duplicate App or MCP connections appear, run
the doctor and review its remediation before changing configuration.

Mobbin authentication, permission, transport, empty-result, and rate-limit failures
are explicit states. Star Forge keeps grounded candidates, tries accepted
fallbacks in catalog order, and never claims unavailable research ran.

## Why Did Star Forge Not Create A GitHub Repository?

Creation requires an approved Repository Contract with the owner, repository name,
private visibility, and write authority. The GitHub plugin is preferred. The only
narrow creation fallback is `gh repo create --private`.

Existing repositories are adopted read-only first. Identity or visibility mismatch
blocks rather than triggering an overwrite or visibility change.

## Why Are Sites And Vercel Not Both Configured?

The Delivery Contract selects one provider by fit. Sites serves suitable simple or
internal apps. Vercel serves applications requiring its production workflow.
Selecting both is a contract conflict, not redundancy.

## Why Is Delivery Blocked After Build And Review Passed?

Build completion is not delivery. Check the approved target and the single blocker
reported in state. Delivery evidence must match current source and the exact
contract, identify the repository commit and delivery or package result, include a
live URL when required, and pass smoke validation.

Credentials, signing, billing, production access, public release, and destructive
changes need authority beyond ordinary implementation. Star Forge preserves safe
local work while waiting.

## Why Did Browser Proof Fail?

For a local URL, confirm the application is running on loopback and that the
server lease matches its port, base URL, command, and process. Strict local browser
proof normally requires desktop and mobile screenshots, interaction evidence, and
console evidence.

Use the in-app Browser for interactive local QA. Use Playwright only when routing
selects the headless or CI fallback. Use Chrome only for authenticated or
extension-dependent state.

## Why Did iOS Proof Fail?

XcodeBuildMCP must run `session_show_defaults` before the first build, run, or
test action. Evidence must identify the selected project or workspace, scheme,
Simulator, build, launch, test, and visual result. A filename without real
artifact content does not pass.

## Why Did Expo Verification Pass But Delivery Fail?

React Native and Expo have no dedicated live collector. The Plan Verify command
proves the task. A named platform target separately requires current-source
delivery proof for `expo-platform-delivery`, using the provider that actually ran.

## Why Does A Passing Project Say Advisory?

A verdict such as `COMPLETE (advisory: ...)` means enforceable local gates passed
while Star Forge declined to treat project-local hooks and sub-agent ledgers as
trusted host witnesses. The suffix is a trust disclosure, not a skipped test.
Quote the verdict exactly.

## Do I Need To Trust Hooks?

No. Hook trust enables continuity and diagnostics. Hooks do not block edits, grant
external authority, or become completion witnesses.

## What Is The Difference Between `--no-hooks` And `--no-agents`?

`--no-hooks` suppresses optional hook trust prompts for the run. `--no-agents`
skips generation of project-local agent profiles during initialization. They are
independent flags.

## Why Did A Completed Project Reopen?

Current source no longer matches the completion proof. v0.4 creates or selects an
isolated change packet, derives affected ACs and proof, and repeats the required
gates. Review and approve it with:

```sh
python3 scripts/star_forge.py approve-change \
  --project . \
  --change CHANGE-1
```

Star Forge does not append a generic solo amendment or overwrite the historical
root Plan.

## How Do I Upgrade A Legacy Plan?

Create a separate draft:

```sh
python3 scripts/star_forge.py migrate-plan \
  --project . \
  --file Plan.md \
  --output drafts/Plan.v2.md
```

The draft marks missing AC and Proof mappings for human review. It never invents
them or overwrites the source. See [migration-v04.md](migration-v04.md).

## How Do I Diagnose An Installation?

```sh
python3 /path/to/star-forge/scripts/star_forge.py doctor \
  --source-root /path/to/star-forge \
  --strict
```

The command is read-only. It reports exact paths and remediation for stale
marketplaces, duplicate installs, version drift, stale hook trust, and duplicate
Mobbin connections.
