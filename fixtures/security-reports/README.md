# Security Report Fixtures

These fixtures cover the Phase 5 security adapter schemas.

## Star Forge Security Report Schema

The documented Star Forge scanner handoff schema is `star-forge.security-report.v1`.

Required fields:

- `schema`: exactly `star-forge.security-report.v1`
- `provenance`: object with scanner identity
- `scanner`: object with `name` and `version`
- `ruleset`: object or string naming the ruleset metadata
- `scan_scope`: string or object describing what was scanned
- `source`: object with `source_hash` or `commit_sha`
- `findings`: array of scanner findings

The adapter also requires a declared input hash, either through `input_hash`,
`input.sha256`, or the CLI `--input-hash`. The declared value must match the
raw input file bytes.

Supported finding fields include `id`, `rule_id`, `severity`, `confidence`,
`title`, `message`, `evidence`, and `remediation`. Raw ids and severities are
preserved, while normalized severity and a deterministic Star Forge fingerprint
are added to the handoff artifact.

Unsupported generic JSON reports intentionally fail closed.
