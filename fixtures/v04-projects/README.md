# Star Forge v0.4 end-to-end projects

These projects are deterministic, offline inputs for `tests/test_v04_e2e.py`.
Each directory contains runnable product source, a real unit test, and a
`scenario.json` contract. The test harness renders the Blueprint and Plan v2
from that contract, then drives the production CLI through the full lifecycle.

The matrix covers web, iOS, macOS, Expo, CLI, Fast MVP risk floors, and a
post-completion change packet. External providers are represented by validated
source-bound replay evidence. No network write or credential is required.
