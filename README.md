<div align="center">
  <img src="assets/star-forge-icon.png" alt="Star Forge logo" width="240">
  <h1>Star Forge</h1>
  <p><strong>A Codex-native software factory that interviews, plans, builds, reviews, delivers, and proves completion.</strong></p>
  <p>
    <a href="https://github.com/khaledayeva/star-forge/actions/workflows/ci.yml"><img src="https://github.com/khaledayeva/star-forge/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
    <img src="https://img.shields.io/badge/Codex-plugin-F59E0B" alt="Codex plugin">
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB" alt="Python 3.10 or newer">
    <img src="https://img.shields.io/badge/license-MIT-22C55E" alt="MIT license">
  </p>
</div>

Star Forge is a lean control plane for completing software projects in Codex. One
`$forge` invocation gathers material decisions, researches design when UI applies,
locks one approved Blueprint, establishes the requested repository foundation,
routes work through the best available capabilities, verifies and reviews it, and
satisfies an explicit Delivery Contract.

Star Forge owns lifecycle state, contracts, routing, proof, review policy, and the
completion verdict. Official plugins, MCP servers, Codex tools, and repository
workflows perform specialist operations. Star Forge does not duplicate them.

## Install

GitHub is the canonical marketplace source:

```sh
codex plugin marketplace add https://github.com/khaledayeva/star-forge
codex plugin add star-forge@star-forge
```

Start a new Codex task after installation. Optionally run `/hooks` and trust the
Star Forge entries for continuity and diagnostics. Hooks are not required and do
not become trusted completion witnesses.

Check an installation without changing it:

```sh
python3 /path/to/star-forge/scripts/star_forge.py doctor \
  --source-root /path/to/star-forge \
  --strict
```

The doctor reports stale marketplaces, duplicate installs, active-version drift,
stale hook trust, and duplicate Mobbin connections. It never removes or rewrites
anything. See [docs/install.md](docs/install.md) for clean installs and upgrades.

## Start Or Resume

Open the target project in Codex:

```text
$forge build this idea end to end
```

For a faster profile that keeps every risk-required review and delivery gate:

```text
$forge build a fast MVP for this idea
```

Resume the same way after a break or compaction:

```text
$forge resume where we left off
```

The public lifecycle is:

```text
intake -> design -> plan -> foundation -> build -> review -> deliver -> done
```

Design is skipped when it does not apply. Existing v0.3 projects retain their
compatible lifecycle until migrated. A source change after completion enters
`amend` through an isolated change packet, then repeats only the affected gates.

## What One Forge Loop Does

1. Asks only questions that can materially change scope, architecture, design,
   security, or delivery. Conservative defaults are recorded as assumptions.
2. For UI projects, researches two or three grounded design directions when a
   capable source is available. Mobbin is preferred for real interaction patterns.
3. Presents one complete Blueprint for approval and writes
   `Blueprint.lock.json`. Any Blueprint edit invalidates that content lock.
4. Creates a Plan v2 table with `ACs` and `Proof` traceability.
5. Initializes local Git. When approved, it establishes a new private GitHub
   repository, `origin`, default branch, initial commit, and CI before feature work.
6. Routes each need from the versioned capability catalog and discloses missing
   preferred providers, selected fallbacks, and blockers.
7. Builds with task-scoped ownership, coordinator-recorded verification, and
   platform-specific proof.
8. Selects up to four review lenses from project risks. Correctness always applies.
9. Delivers exactly the approved target and records source-bound delivery proof.
10. Runs `done --strict`, which refuses completion until every required gate passes.

## Capability Routing

Routing is deterministic and data-driven. Preference order is dedicated plugin or
MCP, native Codex capability, Computer Use, safe shell fallback, then an explicit
blocker. An accepted fallback is reported as degraded, never as if the preferred
provider ran.

Important routes include:

| Need | Preferred | Fallback |
| --- | --- | --- |
| UI pattern discovery | Mobbin | Figma, ImageGen, supplied references, blocker |
| Web implementation | Build Web Apps | Repository framework guidance |
| Local web QA | In-app Browser | Playwright collector |
| Authenticated browser state | Chrome | In-app Browser |
| iOS implementation | Build iOS Apps | Xcode project workflow |
| iOS verification | XcodeBuildMCP | iOS Simulator Browser, then blocker |
| macOS | Build macOS Apps | Computer Use, then project workflow |
| React Native or Expo | Official Expo plugin | Existing repository Expo CLI workflow |
| Security | Codex Security | Security reviewer and project scanners |
| GitHub lifecycle | GitHub plugin | Read-only `gh`; creation is narrowly `gh repo create --private` |
| Simple or internal hosting | Sites | Source-only handoff |
| Production web hosting | Vercel | Contract-selected provider or source-only handoff |

Optional plugins are suggestion-only. Star Forge never installs or connects one
without user action. Workplace connectors provide intake context only when named
by the user or required by the Blueprint.

## Mobbin And Original Design

The plugin exposes the registered Mobbin App through `.app.json`. In Codex Desktop,
connect Mobbin in ChatGPT using its supported OAuth flow, then retry the design
step. Codex Desktop reuses that App credential. For Codex CLI:

```sh
codex mcp add mobbin --url https://api.mobbin.com/mcp
codex mcp login mobbin
```

Star Forge never asks for or stores a Mobbin API key, packages a repository
`.mcp.json`, or uses an undocumented REST fallback. Mobbin research feeds the
Blueprint design section directly. Findings become original `Borrow`, `Avoid`, and
product-specific constraints, not clone instructions or copied screens.

## Repository, Delivery, And Authority

The Blueprint distinguishes local-only work, creation of a new private GitHub
repository, and adoption of an existing repository. Adoption begins with read-only
identity and visibility checks. Star Forge never changes visibility implicitly.

Delivery targets are `source-only`, `private-repo`, `preview`, `production`,
`package`, or a named platform-specific target. Suitable simple or internal web
apps may route to Sites. Production web applications may route to Vercel. The
contract selects one provider by fit and never configures both by default.

Blueprint approval authorizes only the non-destructive external writes stated in
the Repository and Delivery contracts. Visibility changes, destructive
replacement, paid resources, billing, signing, notarization, production migrations,
and public publication need specific authority. Missing credentials or authority
become one honest blocker while completed local work is preserved.

## Proof And Completion

Plan verification, live proof, foundation evidence, review findings, and delivery
evidence are coordinator-owned and bound to current source. Provider narratives do
not count as proof. Source changes stale affected evidence.

Platform routes use:

- In-app Browser for interactive local web QA, with Playwright for CI or headless
  fallback.
- XcodeBuildMCP and Simulator evidence for iOS.
- Build macOS Apps plus the most specific required build, UI, test, signing, and
  packaging capability for macOS.
- Normal task verification plus separate delivery proof for React Native and Expo.
- Codex Security when available, normalized through the existing security proof
  path.

Copy-ready examples are in [docs/proof-recipes.md](docs/proof-recipes.md).

`done --strict` requires an approved and unchanged Blueprint, a valid traced Plan,
passing current-source task and platform proof, a fresh adaptive review with no
unresolved blockers, a satisfied Delivery Contract, and a clean tree. Local hooks
and sub-agent ledgers remain diagnostic, so a passing verdict may include an
advisory trust suffix.

## Change Packets

Post-completion work is isolated under:

```text
.starforge/changes/<change-id>/
  change.md
  Plan.md
  evidence/
  review/
```

The packet records the original completed source hash, scope delta, affected ACs,
delivery impact, and approval. Its task modes and proof derive from affected scope.
The historical root Plan and v0.3 `AMEND-n` rows remain unchanged.

## Core Commands

```sh
python3 scripts/star_forge.py run --project . --objective "<objective>"
python3 scripts/star_forge.py approve-blueprint --project .
python3 scripts/star_forge.py validate-plan --file Plan.md --project . --strict
python3 scripts/star_forge.py status --project .
python3 scripts/star_forge.py quality --project . --strict
python3 scripts/star_forge.py review --project . --strict
python3 scripts/star_forge.py done --project . --strict --write-summary
```

Use `--no-hooks` to suppress optional hook trust prompts for that run. Use
`--no-agents` to skip generated project-local agent profiles. The flags have
different meanings.

## Documentation

- [Installation and doctor](docs/install.md)
- [Complete workflow](docs/workflow.md)
- [Proof recipes](docs/proof-recipes.md)
- [Validation and release checks](docs/validation.md)
- [v0.3 to v0.4 migration](docs/migration-v04.md)
- [Troubleshooting FAQ](docs/faq.md)

## Security

Do not commit credentials, OAuth tokens, private screenshots, scanner secrets, or
private project content as evidence or global learnings. Global learnings are
disabled by default and accept only validated, provenance-labeled, redacted,
abstract rules after explicit opt-in.

Report vulnerabilities privately through GitHub Security Advisories as described
in [SECURITY.md](SECURITY.md).

## Development

```sh
scripts/check.sh
python3 scripts/star_forge.py self-test --strict
```

The full release gate is:

```sh
scripts/release-check.sh
```

See [docs/validation.md](docs/validation.md) and
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
