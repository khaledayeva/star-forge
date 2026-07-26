# Mobbin integration decision

Status: accepted for SF4-012 implementation

Date: 2026-07-26

Acceptance criterion: AC-10

## Decision

Star Forge will package `.app.json` with the registered Mobbin App ID
`asdk_app_69fdb9081018819193707354f21b366e`. It will not package `.mcp.json`.
The app binding is optional, so the manifest must omit `required` rather than
forcing Mobbin on projects that can use another design-research provider.

The exact file SF4-012 should install is represented by
`fixtures/mobbin/expected-app-manifest.json`:

```json
{
  "apps": {
    "mobbin": {
      "id": "asdk_app_69fdb9081018819193707354f21b366e"
    }
  }
}
```

SF4-012 must also add `"apps": "./.app.json"` to the plugin manifest. It must not
add `mcpServers`, `.mcp.json`, an API-key field, or a Mobbin REST fallback.

## Evidence

Mobbin documents that its MCP server uses Streamable HTTP and OAuth. The supported
endpoint is `https://api.mobbin.com/mcp`; no API key or manually stored token is
needed for MCP access. Mobbin separately documents that Codex Desktop shares
credentials with ChatGPT, so connecting the registered Mobbin App in ChatGPT makes
it available to Codex Desktop.

On 2026-07-26, a forced refresh through Codex Desktop 0.139.0 `app/list` returned:

- ID: `asdk_app_69fdb9081018819193707354f21b366e`
- Name: `Mobbin`
- Category: `DESIGN`
- Distribution: `ECOSYSTEM_DIRECTORY`
- Install URL:
  `https://chatgpt.com/apps/mobbin/asdk_app_69fdb9081018819193707354f21b366e`

The snapshot is stored in `fixtures/mobbin/registered-app-evidence.json`.
`is_accessible` is intentionally recorded but is not a portable property. It
reflects whether the inspecting user has connected the App, not whether the App ID
is registered or valid.

Official Codex source confirms the packaging behavior:

- The plugin loader independently discovers `.app.json` and `.mcp.json`.
- A `.mcp.json` entry creates a plugin-attributed MCP server registration.
- A thread-selected plugin MCP overrides a discovered plugin MCP with the same
  name, while an explicit user config entry overrides either plugin registration.
- Different server names remain separate registrations, even when they reach the
  same remote endpoint.

This means packaging `.mcp.json` would create a second Mobbin configuration path.
If a user already followed Mobbin's Codex CLI setup, the same server name shadows
the plugin registration and a different name risks two active connections.
Packaging both `.app.json` and `.mcp.json` would expose two authentication paths in
Codex Desktop. The registered App is therefore the only package surface.

## Surface behavior

For Codex Desktop:

1. Star Forge exposes the registered Mobbin App through `.app.json`.
2. The user connects Mobbin in ChatGPT through Mobbin's supported OAuth flow.
3. Codex Desktop reuses that ChatGPT App credential.
4. Star Forge stores no Mobbin secret and makes no undocumented REST request.

For Codex CLI, the plugin does not install or mutate a user MCP entry. Guidance may
tell the user to run:

```text
codex mcp add mobbin --url https://api.mobbin.com/mcp
codex mcp login mobbin
```

That CLI connection is user-scoped and OAuth-backed. It is not a reason to package
`.mcp.json`.

## Validation rule

The focused test permits neither root manifest during SF4-011, because SF4-012 owns
the package change. Once `.app.json` exists, the test requires it to match the
fixture exactly and requires `.codex-plugin/plugin.json` to point `apps` to it.
`.mcp.json` and `mcpServers` are always rejected for the Mobbin integration.

## Sources

- Mobbin MCP introduction:
  https://docs.mobbin.com/mcp/introduction
- Mobbin Codex App setup:
  https://docs.mobbin.com/mcp/clients/codex-app
- Mobbin Codex CLI setup:
  https://docs.mobbin.com/mcp/clients/codex-cli
- Mobbin registered ChatGPT App:
  https://chatgpt.com/apps/mobbin/asdk_app_69fdb9081018819193707354f21b366e
- Codex app-server App discovery:
  https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md#apps
- Codex plugin loader:
  https://github.com/openai/codex/blob/main/codex-rs/core-plugins/src/loader.rs
- Codex MCP registration precedence:
  https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/catalog.rs
