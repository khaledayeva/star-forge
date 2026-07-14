# FAQ

## Is Star Forge A Standalone CLI?

No. Star Forge includes a Python runtime for deterministic state and proof
checks, but the full product is a Codex plugin. Skills, native sub-agents, and
observer hooks are part of the intended experience.

The CLI can still run package checks, initialize projects, validate plans, record
proof, and compute completion. Sub-agent orchestration is expected to happen
inside Codex.

## Why Does A Passing Project Say Advisory?

`COMPLETE (advisory: ...)` means the enforceable local gates passed, but Star
Forge refused to pretend that project-local hook and sub-agent ledgers are trusted
host witnesses.

That is intentional. The advisory suffix is a trust-model disclosure, not a failed
build. The build still had to pass blueprint approval, task verification, browser
proof when required, review, fix queue checks, source hash checks, and clean tree
checks.

## What Verdicts Should I Expect?

- `COMPLETE (advisory: ...)`: local gates passed, but this version has no
  host-controlled witness source for hooks or sub-agents.
- `NEEDS_CHANGES`: at least one gate failed.
- `COMPLETE`: reserved for a future version where the CLI itself reports an
  unqualified trusted completion.

Quote the verdict line exactly when reporting project status.

## Why Are Live Collectors So Strict?

The strictness is the product. Collectors write evidence, but proof commands
decide whether the evidence is fresh, scoped, source-bound, and safe to trust.

Loose collectors would make demos easier and completion less meaningful. Star
Forge chooses a paved road with strict gates instead.

## Do I Need To Trust Hooks?

No, but it helps. Hook trust enables continuity re-anchors, local changed-file
trails, sub-agent diagnostics, and pre-compaction handoffs. Star Forge still
works without hooks, and final completion remains advisory in this version either
way.

## Will Star Forge Push Or Deploy For Me?

No. Star Forge does not push, publish, deploy, migrate, or create remote pull
requests unless the user explicitly asks. It keeps proof local and treats remote
mutations as separate intent.
