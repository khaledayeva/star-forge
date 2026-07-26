# Migrate From Star Forge v0.3 To v0.4

v0.4 reads v0.3 projects in place. Migration is explicit and non-destructive.
There is no command that deletes, rewrites, pushes, changes repository visibility,
or deploys a legacy project.

## Before You Start

1. Preserve the project, including `.starforge`.
2. Commit or otherwise back up current work.
3. Install Star Forge from the canonical GitHub marketplace.
4. Start a new Codex task.
5. Run the read-only doctor:

```sh
python3 /path/to/star-forge/scripts/star_forge.py doctor \
  --source-root /path/to/star-forge \
  --strict
```

Resolve stale local marketplace registrations, duplicate plugin caches, active
runtime drift, stale hook trust, and duplicate Mobbin connections deliberately.
The doctor reports remediation but never applies it.

## Compatibility At A Glance

| v0.3 artifact or state | v0.4 behavior |
| --- | --- |
| Approved Blueprint without a lock | Readable; new v0.4 approval or amendment work needs an explicit content lock |
| Eight-column Plan | Readable in legacy mode |
| Existing `AMEND-n` rows | Preserved as historical amendment records |
| Evidence manifest v1 | Adapted into the v2 evidence reader |
| Existing review and completion proof | Remains historical for its original source hash |
| Standard or fast MVP profile | Preserved as a compatibility label and interpreted through risk flags |
| Old global learnings | Ignored until provenance and safety validation pass |
| Missing Delivery Contract | Defaults to `source-only` for the legacy project |

Legacy mode does not silently impose new lifecycle gates on already recorded v0.3
history. New v0.4 work should adopt the new contracts.

## Create A Plan v2 Draft

Do not edit the historical Plan first. Create a separate reviewable draft:

```sh
python3 scripts/star_forge.py migrate-plan \
  --project . \
  --file Plan.md \
  --output drafts/Plan.v2.md
```

The command:

- preserves the source Plan byte for byte
- preserves task ids, ordering, dependencies, status, mode, files, verification,
  and evidence text
- adds the `ACs` and `Proof` columns
- marks unknown mappings for review
- never infers acceptance criteria or proof kinds from prose

Review every migrated row against the Blueprint. Map each substantive task to one
or more stable `AC-n` criteria and validated proof kinds. Every Blueprint criterion
must be covered. Use a documented maintenance exemption only for work that truly
does not satisfy a product criterion.

Validate the reviewed draft before adopting it:

```sh
python3 scripts/star_forge.py validate-plan \
  --file drafts/Plan.v2.md \
  --project . \
  --strict
```

Replacing the root Plan is a deliberate project edit. Keep the legacy Plan until
the reviewed draft is accepted and the project history is safely preserved.

## Lock The Blueprint

`Status: approved` remains readable for legacy history. New v0.4 planning and
change packets use a tracked content lock. Present the complete current Blueprint
to the user, obtain explicit approval, then run:

```sh
python3 scripts/star_forge.py approve-blueprint --project .
```

The lock protects contract drift. It does not prove human identity or grant
authority beyond the contract. Any later Blueprint edit invalidates it.

## Add v0.4 Contracts

For active new work, review and record:

- intake decisions and explicit assumptions
- design applicability and selected original direction
- Toolchain needs, preferred routes, accepted fallbacks, and unavailable blockers
- risk flags
- Repository Contract
- Delivery Contract

New delivery choices are `source-only`, `private-repo`, `preview`, `production`,
`package`, or a named platform target. Legacy projects without this section remain
`source-only`; Star Forge does not invent a deployment target.

If GitHub is newly requested, specify whether to create a new private repository or
adopt an existing one. Adoption starts with read-only identity and visibility
checks. A new public repository or an implicit visibility change is not allowed.

## Mobbin Connection Changes

v0.4 packages the optional registered Mobbin App binding. It does not package
`.mcp.json`.

For Codex Desktop, connect Mobbin in ChatGPT through its supported OAuth flow.
For Codex CLI:

```sh
codex mcp add mobbin --url https://api.mobbin.com/mcp
codex mcp login mobbin
```

Keep one intended connection path. Do not migrate API keys into the repository.
Run the doctor if legacy config exposes duplicate Mobbin App or MCP connections.

## Evidence Migration

Evidence manifest v1 remains readable through compatibility adapters. v0.4 does
not rewrite historical evidence in place. New proof uses
`star-forge.evidence-envelope.v2` with provider identity, provenance, source hash,
artifact hashes, timestamps, verdict, degradation, and blockers.

Historical completion remains valid only for its recorded source hash. New source
changes require new proof. A degraded v1 artifact remains degraded after adaptation
and does not become a pass.

## Post-Completion Changes

Historical `AMEND-n` rows remain readable and unchanged. New source drift uses:

```text
.starforge/changes/<change-id>/
  change.md
  Plan.md
  evidence/
  review/
```

The packet records the original completed source hash, scope delta, affected ACs,
delivery impact, and approval. Its task modes and Verify commands derive from the
changed scope. Substantive changes do not default to `solo`, and unrelated legacy
Verify commands are not inherited.

After reviewing the packet:

```sh
python3 scripts/star_forge.py approve-change \
  --project . \
  --change CHANGE-1
```

Automatic packet derivation refuses legacy rows whose AC and Proof mappings have
not been reviewed. Finish the Plan v2 mapping first or create an explicit packet
with reviewed scope.

## Final Checks

```sh
python3 scripts/star_forge.py status --project .
python3 scripts/star_forge.py validate-plan --file Plan.md --project . --strict
python3 scripts/star_forge.py quality --project . --strict
python3 scripts/star_forge.py done --project . --strict --write-summary
```

For plugin release work, also run:

```sh
scripts/release-check.sh
```

If completion blocks, preserve the exact blocker. Do not erase history, weaken
proof, or substitute an unapproved delivery target to obtain a passing verdict.
