"""Cohesive Star Forge runtime extracted from the CLI facade."""

from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence
from .runtime_support import AUTO_CONTINUE_FILE, CANONICAL_STATE, CHANGED_FILES, HANDOFF_ARTIFACT, HOOK_EVENTS, INCIDENTS_FILE, MAX_AUTO_CONTINUES, PLUGIN_NAME, SF_VERSION, SUBAGENT_EVENTS, append_jsonl, architecture_debt_findings, blocking_items, iter_project_files, now_utc, plugin_root, read_json, read_text, redact, relative_to_project, repo_root, scan_paths, stable_json_hash, write_json, write_json_if_changed
from .runtime_project import enforcement_mode, ensure_state_dirs, find_star_forge_project_root, has_star_forge_project_markers
from .runtime_review import done_payload
from .runtime_orchestration import version_core

def load_hook_event() -> dict[str, Any]:
    # Hooks must never crash on hostile/garbled stdin: UnicodeDecodeError is a
    # ValueError, OSError covers closed/odd stdin. A crashing hook can block a
    # Codex tool call, which an observation-only layer must never do.
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}

def hook_project(event: dict[str, Any]) -> Path | None:
    raw = Path(str(event.get("cwd") or os.getcwd())).resolve()
    # The event cwd may not exist (Path.resolve never raises, but run_git would).
    if not raw.is_dir():
        return None
    found = find_star_forge_project_root(raw)
    if found:
        return found
    try:
        root = repo_root(raw)
    except OSError:
        return None
    return root if has_star_forge_project_markers(root) else None

def hook_output(event_name: str, *, context: str | None = None, system_message: str | None = None, **extra: Any) -> int:
    payload: dict[str, Any] = {}
    if context:
        payload["hookSpecificOutput"] = {"hookEventName": event_name, "additionalContext": context}
    if system_message:
        payload["systemMessage"] = system_message
    payload.update(extra)
    if payload:
        print(json.dumps(redact(payload)))
    return 0

def extract_event_paths(event: dict[str, Any], project: Path) -> list[str]:
    tool_input = event.get("tool_input", {})
    rels: list[str] = []
    if isinstance(tool_input, dict):
        for key in ("file_path", "path"):
            raw = tool_input.get(key)
            if isinstance(raw, str) and raw:
                rels.append(raw)
        command = str(tool_input.get("command", ""))
        for match in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", command, re.MULTILINE):
            rels.append(match.group(1).strip())
    out: list[str] = []
    seen: set[str] = set()
    for raw in rels:
        path = Path(raw)
        candidate = path if path.is_absolute() else project / path
        rel = relative_to_project(candidate, project)
        if rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out

def state_banner(project: Path) -> str | None:
    """One-line operating banner re-injected on every prompt (compaction-proof)."""
    state_path = project / CANONICAL_STATE
    if not state_path.exists():
        return None
    try:
        state = read_json(state_path)
    except Exception:
        return None
    phase = state.get("phase")
    if phase in {None, "done"}:
        return None
    enforcement = "witnessed" if enforcement_mode(project) == "witnessed" else "advisory"
    ready = state.get("plan", {}).get("ready") if isinstance(state.get("plan"), dict) else None
    return f"[star-forge] phase={phase} enforcement={enforcement} ready={ready} | next: {state.get('required_next_action')}"

def reanchor_text(project: Path) -> str:
    """Full re-anchor for SessionStart/PreCompact: regenerate the operating card."""
    try:
        state = read_json(project / CANONICAL_STATE) if (project / CANONICAL_STATE).exists() else {}
    except Exception:
        state = {}
    card = state.get("operating_card")
    if card:
        return "Star Forge continuity — start this turn by running the state helper:\n" + str(card)
    return "Star Forge continuity: run `python3 <plugin-root>/scripts/star_forge.py run --project .` to recompute phase and the operating card before continuing."

def cmd_hook(args: argparse.Namespace) -> int:
    """PreToolUse: observe only. No denial — the session proved blocking trains evasion."""
    event = load_hook_event()
    if not event:
        return 0
    project = hook_project(event)
    if project is None:
        return 0
    ensure_state_dirs(project)
    payload = {"schema": "star-forge.hook-event.v1", "timestamp": now_utc(), "event": str(event.get("hook_event_name", "PreToolUse")), "tool": event.get("tool_name")}
    append_jsonl(project / HOOK_EVENTS, payload)
    return 0

def cmd_post_hook(args: argparse.Namespace) -> int:
    """PostToolUse: log the changed-file trail for the freshness/liveness view. No blocking."""
    event = load_hook_event()
    project = hook_project(event)
    if project is None:
        return 0
    ensure_state_dirs(project)
    for rel in extract_event_paths(event, project):
        append_jsonl(project / CHANGED_FILES, {
            "schema": "star-forge.changed-file.v1",
            "timestamp": now_utc(),
            "file": rel,
            "tool": event.get("tool_name"),
            "session_id": event.get("session_id")
        })
    return 0

def cmd_prompt_hook(args: argparse.Namespace) -> int:
    """UserPromptSubmit: reset the auto-continue budget and inject the state banner."""
    event = load_hook_event()
    project = hook_project(event)
    if project is None:
        return 0
    counter_path = project / AUTO_CONTINUE_FILE
    if counter_path.exists():
        try:
            counter_path.unlink()
        except OSError:
            pass
    banner = state_banner(project)
    if banner:
        return hook_output("UserPromptSubmit", context=banner)
    return 0

def build_handoff(project: Path, source: str) -> dict[str, Any]:
    try:
        state = read_json(project / CANONICAL_STATE) if (project / CANONICAL_STATE).exists() else {}
    except Exception:
        state = {}
    done = done_payload(project)
    return {
        "schema": "star-forge.handoff.v1",
        "created_at": now_utc(),
        "source": source,
        "project": str(project),
        "phase": state.get("phase"),
        "complete": done.get("is_complete"),
        "verdict": done.get("verdict"),
        "next_action": state.get("required_next_action"),
        "operating_card": state.get("operating_card"),
    }

def cmd_session_start_hook(args: argparse.Namespace) -> int:
    event = load_hook_event()
    project = hook_project(event)
    if project is None:
        return 0
    ensure_state_dirs(project)
    payload = {"schema": "star-forge.hook-event.v1", "timestamp": now_utc(), "event": "SessionStart", "source": event.get("source")}
    append_jsonl(project / HOOK_EVENTS, payload)
    incidents = unprocessed_incident_note(project)
    context = reanchor_text(project)
    if incidents:
        context = context + "\n" + incidents
    return hook_output("SessionStart", context=context)

def unprocessed_incident_note(project: Path) -> str | None:
    path = project / INCIDENTS_FILE
    if not path.exists():
        return None
    try:
        count = len([line for line in read_text(path).splitlines() if line.strip()])
    except OSError:
        return None
    if count:
        return f"Star Forge: {count} incident(s) recorded (waived findings, drift, contradictions). Between projects, run `learn` to convert recurring ones into durable learnings."
    return None

def should_block_stop(project: Path, event: dict[str, Any], handoff: dict[str, Any]) -> str | None:
    """Bounded Cruise keep-going. Momentum only — never a correctness gate."""
    if event.get("stop_hook_active"):
        return None
    try:
        state = read_json(project / CANONICAL_STATE) if (project / CANONICAL_STATE).exists() else {}
    except Exception:
        state = {}
    if str(state.get("mode") or "") != "cruise":
        return None
    phase = str(state.get("phase") or "")
    if phase not in {
            "intake",
            "design",
            "plan",
            "foundation",
            "build",
            "review",
            "deliver",
            "amend",
    }:
        return None
    signature = stable_json_hash({"phase": phase, "next": handoff.get("next_action")})
    counter: dict[str, Any] = {}
    counter_path = project / AUTO_CONTINUE_FILE
    if counter_path.exists():
        try:
            counter = read_json(counter_path)
        except Exception:
            counter = {}
    count = int(counter.get("count") or 0) if counter.get("signature") == signature else 0
    if count >= MAX_AUTO_CONTINUES:
        return None
    write_json(counter_path, {"schema": "star-forge.auto-continue.v1", "count": count + 1, "phase": phase, "signature": signature, "updated_at": now_utc()})
    return f"Star Forge: phase `{phase}` is not complete. Continue with: {state.get('required_next_action')}"

def cmd_stop_hook(args: argparse.Namespace) -> int:
    event = load_hook_event()
    project = hook_project(event)
    if project is None:
        return 0
    ensure_state_dirs(project)
    payload = build_handoff(project, str(event.get("hook_event_name", "Stop")))
    write_json_if_changed(project / HANDOFF_ARTIFACT, payload)
    # Contradiction detector: a model claim of completion that the predicate denies.
    claim_complete = bool(event.get("summary", {}).get("complete")) if isinstance(event.get("summary"), dict) else False
    if claim_complete and not payload.get("complete"):
        append_jsonl(project / INCIDENTS_FILE, {"schema": "star-forge.incident.v1", "timestamp": now_utc(), "kind": "completion-contradiction", "verdict": payload.get("verdict")})
        return hook_output(
            "Stop",
            system_message=
            f"Star Forge: a completion claim contradicts the computed predicate ({payload.get('verdict')}). Run `done --strict` before telling the user it is complete.")
    block = should_block_stop(project, event, payload)
    if block:
        print(json.dumps({"decision": "block", "reason": block}))
        return 0
    return hook_output("Stop", system_message=f"Star Forge saved continuity state: {project / HANDOFF_ARTIFACT}")

def record_subagent_event(event: dict[str, Any], event_name: str) -> int:
    project = hook_project(event)
    if project is None:
        return 0
    ensure_state_dirs(project)
    payload = {
        "schema": "star-forge.subagent-event.v1",
        "timestamp": now_utc(),
        "event": event_name,
        "agent_id": event.get("agent_id"),
        "agent_type": event.get("agent_type"),
        "session_id": event.get("session_id"),
        "parent_session_id": event.get("parent_session_id") or event.get("parent_thread_id"),
    }
    append_jsonl(project / SUBAGENT_EVENTS, payload)
    return 0

def cmd_subagent_start_hook(args: argparse.Namespace) -> int:
    return record_subagent_event(load_hook_event(), "SubagentStart")

def cmd_subagent_stop_hook(args: argparse.Namespace) -> int:
    return record_subagent_event(load_hook_event(), "SubagentStop")

def cmd_pre_compact_hook(args: argparse.Namespace) -> int:
    event = load_hook_event()
    project = hook_project(event)
    if project is None:
        return 0
    ensure_state_dirs(project)
    write_json_if_changed(project / HANDOFF_ARTIFACT, build_handoff(project, "PreCompact"))
    return hook_output("PreCompact", context=reanchor_text(project), system_message="Star Forge prepared continuity context for compaction.")

def cmd_self_test(args: argparse.Namespace) -> int:
    root = plugin_root()
    checks: list[dict[str, Any]] = []
    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})
    def run_check(name: str, command: Sequence[str], *, env: dict[str, str] | None = None) -> None:
        proc = subprocess.run(command, cwd=str(root), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        detail = (proc.stdout + proc.stderr).strip()[-1200:]
        check(name, proc.returncode == 0, detail)
    try:
        manifest = read_json(root / ".codex-plugin" / "plugin.json")
        check("manifest-json", manifest.get("name") == PLUGIN_NAME, "plugin.json parsed")
        manifest_version = str(manifest.get("version") or "")
        check("manifest-version", version_core(manifest_version) == version_core(SF_VERSION), f"manifest={manifest_version} script={SF_VERSION}")
    except Exception as exc:
        check("manifest-json", False, str(exc))
    for skill in ["forge", "forge-plan", "forge-work", "forge-review"]:
        path = root / "skills" / skill / "SKILL.md"
        check(f"skill-{skill}", path.exists() and "description:" in read_text(path), str(path))
    for role in ["builder", "reviewer"]:
        path = root / "agents" / role / "agent.md"
        check(f"agent-{role}", path.exists() and "## Mission" in read_text(path), str(path))
    for template in ["Blueprint.md", "Plan.md"]:
        check(f"template-{template}", (root / "templates" / template).exists(), template)
    help_process = subprocess.run(
        [sys.executable, str(root / "scripts" / "star_forge.py"), "--help"],
        cwd=str(root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    help_text = help_process.stdout + help_process.stderr
    for command in [
            "run",
            "init",
            "approve-blueprint",
            "verify",
            "browser-run",
            "preview-proof",
            "proof-run",
            "native-ios-proof",
            "native-macos-proof",
            "security-proof",
            "security-handoff-packet",
            "source-packet-proof",
            "source-packet-github-pr-review",
            "server-lease",
            "review",
            "waive",
            "complete-task",
            "done",
            "learn",
            "agents-install",
            "validate-plan",
            "status",
            "quality",
    ]:
        check(f"command-{command}", help_process.returncode == 0 and command in help_text, command)
    try:
        json.loads(read_text(root / "hooks" / "hooks.json"))
        check("hooks-json", True, "hooks/hooks.json parsed")
    except Exception as exc:
        check("hooks-json", False, str(exc))
    if args.strict:
        with tempfile.TemporaryDirectory(prefix="star-forge-pycache-") as pycache:
            env = dict(os.environ)
            env["PYTHONPYCACHEPREFIX"] = pycache
            run_check("py-compile", [sys.executable, "-m", "py_compile", str(root / "scripts" / "star_forge.py")], env=env)
        test_path = root / "tests" / "test_star_forge.py"
        if test_path.exists():
            run_check("unit-tests", [sys.executable, str(test_path)])
        quality_paths = list(iter_project_files(root, all_files=True))
        quality_findings = [*scan_paths(quality_paths, root), *architecture_debt_findings(quality_paths, root)]
        quality_blocking = blocking_items(quality_findings)
        check("quality-gate-all-strict", not quality_blocking, f"blocking={len(quality_blocking)} scanned_files={len(quality_paths)}")
        with tempfile.TemporaryDirectory(prefix="star-forge-smoke-") as tmp:
            smoke = Path(tmp) / "project"
            script = str(root / "scripts" / "star_forge.py")
            run_check("cli-smoke-init", [sys.executable, script, "init", "--project", str(smoke), "--no-agents"])
            run_check("cli-smoke-run", [sys.executable, script, "run", "--project", str(smoke), "--objective", "smoke", "--no-hooks"])
            run_check("cli-smoke-verify", [sys.executable, script, "verify", "--project", str(smoke), "--task", "SF-SMOKE", "--noop", "--summary", "smoke", "--strict"])
            run_check("cli-smoke-status", [sys.executable, script, "status", "--project", str(smoke)])
            run_check("cli-smoke-done-readonly", [sys.executable, script, "done", "--project", str(smoke)])
    ok = all(item["ok"] for item in checks)
    print(json.dumps({"schema": "star-forge.self-test.v1", "verdict": "PASS" if ok else "FAIL", "checks": checks}, indent=2))
    return 0 if ok or not args.strict else 1

__all__ = tuple(name for name in globals() if not name.startswith("__"))
