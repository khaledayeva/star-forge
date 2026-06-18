# Star Forge Probe Pack

Run this once against your real Codex install before the hackathon. It empirically
answers the six platform questions the frozen `hooks/hooks.json` and the sub-agent
wiring depend on. Total time: ~30 minutes. Budget: this consumes one of the two
planned `/hooks` re-trusts.

## Setup

1. Back up the real hooks file, swap in the probe:

   ```bash
   cp hooks/hooks.json hooks/hooks.real.json
   cp probes/hooks.probe.json hooks/hooks.json
   ```

2. Reinstall/refresh the plugin so Codex picks up the change, then open Codex and
   run `/hooks`. **Trust every Star Forge probe entry.**

3. `rm -rf /tmp/sf-probe` to start clean.

## Probes

Each probe appends labeled JSON to `/tmp/sf-probe/events.log`.

### 1. Trust gate UX (validates the freeze constraint)
- With probes trusted, run any shell command in Codex. Confirm a `PreToolUse` block
  appears in the log.
- Edit one byte of `hooks/hooks.json` (add a space), restart Codex, run another
  shell command. **Expected: no new log entry** (hooks silently skipped).
- Run `/hooks`, re-trust, confirm logging resumes.
- Record: does Codex show ANY visible warning when hooks are untrusted?

### 2. PLUGIN_ROOT / env vars
- In the log's `SessionStart` entry, check the `env | grep` output.
- Record: is `PLUGIN_ROOT` set? `PLUGIN_DATA`? Any `CLAUDE_PLUGIN_*` aliases?
  What exact path does `PLUGIN_ROOT` hold (does it match
  `~/.codex/plugins/cache/<marketplace>/star-forge/<version>/`)?

### 3. Sub-agent events
- Ask Codex: "Spawn a sub-agent to list the files in this directory, then report back."
- Record: did `SubagentStart` / `SubagentStop` entries appear? What fields do they
  carry (`agent_id`? `agent_type`? `session_id`?). This is what distinguishes a
  witnessed completion from an advisory one — note the exact ID field name.

### 4. Hooks inside sub-agent turns
- While the sub-agent from probe 3 runs, check whether `PreToolUse` entries appear
  for the SUB-AGENT's tool calls (compare `session_id` fields).
- Record: do tool hooks fire for sub-agent tool use? Same or different session_id?

### 5. Stop / stop_hook_active
- Let a turn finish naturally. In the `Stop` entry, record: is there a
  `stop_hook_active` (or similarly named) field? This is the infinite-loop escape
  hatch for Cruise keep-going (Batch 8).

### 6. Compaction
- In a long session, trigger compaction (e.g. `/compact` if available).
- Record: does `PreCompact` fire? Does a `SessionStart` with `source: "compact"`
  appear afterward? Any `PostCompact` event in the log?

## Teardown

```bash
cp hooks/hooks.real.json hooks/hooks.json
rm hooks/hooks.real.json
```

Reinstall the plugin, `/hooks`, re-trust the REAL entries once (re-trust #2 of 2),
and do not edit `hooks/hooks.json` again.

## Record results

Write the answers into `probes/probe-results.md` (yes/no per probe + raw payload
excerpts). Batch 8's enforcement switches (warn-then-enforce on agent IDs,
Stop decision:block) should only be tightened after probes 3-5 confirm field names.
