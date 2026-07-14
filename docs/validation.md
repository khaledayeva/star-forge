# Validation

Star Forge uses the standard Codex plugin layout and includes observer hooks in
`hooks/hooks.json`. The generic plugin validator and the Star Forge release checks
are both part of the release gate.

## Release Gate

Run:

```sh
scripts/release-check.sh
```

This wrapper runs:

```sh
scripts/check.sh
python3 scripts/star_forge.py self-test --strict
```

`scripts/check.sh` validates plugin and hook JSON, compiles the runtime and live
collectors, checks shell installer syntax, and runs every test file.
`self-test --strict` checks the package contract, command surface, hook JSON,
core tests, quality gate, and smoke projects.

## Hook Packaging

The current plugin manifest schema does not accept a `hooks` field. Star Forge
therefore keeps `plugin.json` schema-compliant and bundles its hook configuration
at the standard `hooks/hooks.json` path. The hooks provide continuity banners,
changed-file diagnostics, sub-agent event diagnostics, stop handoffs, and
pre-compaction re-anchors.

The hooks are observers. They do not block edits, and they do not create
unqualified witnessed completion in this version.

## Expected Public Release Checks

A public release should satisfy:

- `python3 -m json.tool .codex-plugin/plugin.json`
- `python3 -m json.tool hooks/hooks.json`
- `python3 /path/to/plugin-creator/scripts/validate_plugin.py .`
- `sh -n scripts/install-codex.sh`
- `scripts/check.sh`
- `python3 scripts/star_forge.py self-test --strict`

The final two commands are bundled into:

```sh
scripts/release-check.sh
```
