# Legacy v0.3 Migration Fixtures

These projects preserve realistic Star Forge v0.3 contracts and historical
artifacts. The stored `dot-starforge/` directory is renamed to `.starforge/` only
inside a temporary test copy because repository ignore rules exclude nested
`.starforge/` directories.

| Fixture | Historical surfaces | Expected compatibility result |
|---|---|---|
| `completed-amended/` | Eight-column Plan, two `AMEND-n` rows, three reviewer findings files, merged review, four task completion records, final proof, state, and browser evidence v1 | Read-only inspection succeeds and evidence adapts to `PASS` |
| `degraded-preview/` | Eight-column Plan, one `AMEND-n` row, and preview evidence v1 with an unavailable hosted provider | Evidence adapts to `DEGRADED` with only nonblocking blockers |

Tests never migrate or rewrite these source fixtures in place. Any migration draft
or change packet is created in a temporary project copy.
