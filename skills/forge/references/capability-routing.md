# Capability Routing

This reference is shared by the four Star Forge skills. The routing catalog owns
capability identities, aliases, selectors, preference order, safe fallbacks, and
optional installation metadata. Lifecycle prose must not duplicate that data.

## Deterministic Resolution

For each coordinator cycle:

1. Discover capabilities actually exposed by the current Codex host. Normalize
   only the discovered names and aliases. Never infer availability from a package
   mentioned in a Blueprint.
2. Build one route request from the Blueprint project class, enabled Toolchain
   and Risk Flags, the union of active Plan v2 `Proof` values, the Delivery Contract target, and any explicit required or materially required need.
3. Invoke `starforge.routing.resolve_routes` from `<plugin-root>/scripts` with
   those values, the host-discovered capabilities, and the default
   `config/capability-routing.json`.
4. Consume decisions in catalog order. Use `selected`, preserve `required_by`,
   and record `status`, `fallback_used`, `unavailable`, and
   `install_suggestion` in the coordinator context supplied to builders,
   reviewers, and proof collection.
5. Rerun resolution when the Blueprint, Plan proof set, Delivery Contract, host
   capabilities, or relevant source scope changes.

The resolver order is dedicated plugin or MCP, native capability, Computer Use,
safe shell fallback, then explicit blocker. Catalog order breaks ties and makes
discovery order irrelevant. Never choose a lower option merely because it is
familiar.

`available` means the preferred route can run. `degraded` means a named preferred
capability is missing and the selected fallback must be disclosed. `blocked`
means the current phase cannot honestly satisfy its contract. Never report a
dedicated provider as used when only its fallback ran.

## Optional Installation

The catalog policy is `suggest-only`, so optional installation is suggestion-only.
Never install or connect an optional plugin
automatically. Present `install_suggestion` only when the resolver returns it for
a materially required need, and make clear that installation requires user action.
If the fallback satisfies the approved outcome, continue with the
fallback without interrupting the lifecycle.

Changing a provider alias or adding a route normally changes
`config/capability-routing.json` and routing tests, not these skills or the
lifecycle state machine.

## Phase Rules

### Intake, design, and plan

Slack, Notion, Teams, Box, SharePoint, and similar workplace connectors may
provide intake context only when the user names them or the Blueprint requires
them.

For UI pattern discovery, use the resolver's `ui-pattern-discovery` decision.
When available, Mobbin is first for real-world interaction patterns. Use its
supported OAuth connection and host-discovered tool schema. Never store an API
key, commit credentials, invent a tool name, or call an undocumented REST
fallback. Normalize findings into provider-neutral references, `Borrow`, `Avoid`,
and product-specific design constraints.

If Mobbin fails, retain grounded findings and use accepted fallbacks in router
order. Distinguish authentication, permission, transport, empty-result, and rate
limit states. If no capable source succeeds, record the checked routes and
queries, exact unavailable state, written constraints, confidence, and proof
limitation. Never fabricate candidates.

### Foundation and GitHub

Local Git initialization does not need an external route. When the approved
Repository Contract requests GitHub, resolve `github-lifecycle`. Prefer the
GitHub connector. For a new repository, the only narrow creation fallback is
`gh repo create --private`, with approved write authority. Configure `origin`,
the approved default branch, initial commit, and CI before feature work.

Adopt existing repositories read-only first. Verify owner, repository, remote,
visibility, and default branch before any approved mutation. Never create a
public repository, overwrite a remote, or change visibility implicitly.

### Build and proof

- Web work uses `web-implementation`; local interactive QA uses `local-web-qa`.
  The in-app Browser is preferred and Playwright is its headless or CI fallback.
- `authenticated-browser-state` is the only ordinary reason to prefer Chrome.
- iOS uses `ios-implementation` and `ios-verification`, including XcodeBuildMCP
  and Simulator proof when selected.
- macOS uses `macos-implementation` and the most specific required native UI,
  signing, packaging, and test capabilities.
- React Native uses `react-native`, and Expo projects use `expo`. Both prefer
  the host-discovered official Expo plugin.
- Security-sensitive work uses `security`, preferring Codex Security and
  preserving normalized source-bound security proof.

#### React Native and Expo

Treat React Native and Expo as separate project-class contracts so an alias
change stays catalog-only and does not alter lifecycle code. For either route,
select `expo-plugin` only when the host exposes one of its configured aliases.
The `expo-cli` fallback means an existing repository-native React Native or Expo
CLI workflow was actually discovered. It does not authorize adding Expo, changing
the application stack, or installing a dependency. Record that fallback as
`degraded`. If neither route is available, preserve the checked preferred and
fallback options and use the route's explicit unavailable blocker.

For an approved named `expo` or `react-native` platform delivery target, also
resolve `expo-platform-delivery`. When the Delivery Contract uses the generic
`platform-specific` target plus a named platform, include the normalized platform
name in the route request's delivery-target values. Do not infer a platform from
an installed tool or choose a second delivery target.

There is no Expo-specific live collector. A React Native or Expo task therefore
requires the normal coordinator-recorded Plan Verify evidence. A platform result
also requires separate, current-source delivery proof in a
`star-forge.evidence-envelope.v2` envelope with `kind` set to `delivery`,
`capability` set to `expo-platform-delivery`, and `provider` set to the capability
that actually ran. Its provenance must preserve the route decision and approved
Delivery Contract, including any fallback. Artifact hashes, timestamps, verdict,
degradation, and blockers remain mandatory. Task verification alone never proves
delivery, and an unavailable route cannot produce passing delivery proof.

### Delivery

Resolve the approved delivery target, never an opportunistic second target.
Suitable simple or internal preview apps route to Sites. Production web
applications that need its workflow route to Vercel. Select exactly one by
contract fit and do not configure both by default.

The route does not grant authority. Credentials, signing, billing, production
access, public release, or destructive changes remain user-controlled. Combine
unresolved requirements into one explicit blocker and preserve all safe work.

## Coordinator-Owned Evidence

Routing selects who or what performs work. It does not transfer evidence
ownership. Only the coordinator may:

- run and record the exact Plan Verify command;
- capture live browser, native, security, or preview proof;
- record source-bound Foundation Contract evidence;
- merge reviewer findings and record the empty fix queue;
- record source-bound Delivery Contract evidence;
- run and report `done --strict`.

A builder, reviewer, plugin, connector, MCP server, or shell command may return
artifacts, but its narrative summary is not evidence. Record only what actually
ran against the current source. Source changes stale the affected proof.
