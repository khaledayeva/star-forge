# Star Forge v0.4 Release Candidate Matrix

This release candidate gate separates deterministic offline replay from checks
that require a connected account, an interactive host, or permission to mutate
an external system.

## Deterministic release gate

Run:

```sh
scripts/release-check.sh
```

The release script creates an isolated temporary root and runs
`tests/test_v04_release.py` inside it. The matrix covers:

| Area | Deterministic evidence |
| --- | --- |
| Package and install shape | Canonical Git marketplace metadata, plugin manifest metadata, registered Mobbin app shape, required assets, generated agent parity, and a clean simulated cache install |
| Legacy v0.3 migration | A copied completed and amended v0.3 fixture is inspected without writes, then migrated to a separate Plan v2 draft with `REVIEW_REQUIRED` markers |
| Mobbin | The registered App OAuth ID is present without stored credentials, and the read-only doctor reports exactly one duplicate finding when a legacy Mobbin definition is added |
| Private GitHub Foundation | The private new-repository contract and evidence pass only for their bound source hash; stale source and missing write authority block feature work |
| Platform routes and proofs | Web, iOS, macOS, Expo, and CLI Plan v2 fixtures validate, select their expected routes, and declare their required proof kinds |
| Release integrity | Missing assets, unsafe asset paths, prompt drift, untracked publishable files, and a stale release version fail the gate |

The offline Foundation replay does not call GitHub, create a repository, change
visibility, or perform any other external write.

## Connected checks before release

These checks are intentionally outside deterministic replay:

1. Install Star Forge from the public Git marketplace in a fresh Codex home and
   confirm the plugin appears once with the expected version.
2. Open Mobbin through the registered app, complete the interactive OAuth flow,
   and confirm the connected user can search a UI pattern. Do not record tokens
   or account data in release evidence.
3. With explicit user approval, create a disposable private GitHub repository,
   verify owner, name, visibility, default branch, remote, CI, and initial commit,
   then remove it using the account owner's normal cleanup process.
4. Exercise web preview in the in-app browser, iOS in Simulator, macOS in the
   desktop host, and Expo through the installed Expo capability.
5. Complete any platform signing, store submission, or production deployment
   only with the relevant account holder's approval.

If a connected capability, credential, simulator, signing identity, or mutation
approval is unavailable, record the check as account-required or blocked. Never
replace it with fabricated PASS evidence.
