#!/usr/bin/env python3
"""Star Forge public CLI and compatibility facade."""

from __future__ import annotations
import sys
from pathlib import Path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import argparse
import json
from functools import partial
from typing import Sequence
from starforge import learnings as global_learnings
from starforge import changes as project_changes
from starforge import lifecycle as project_lifecycle
from live_collectors import common as live_common
from starforge import runtime_hooks as _hooks
from starforge import runtime_native as _native
from starforge import runtime_orchestration as _orchestration
from starforge import runtime_plan as _plan
from starforge import runtime_preview as _preview
from starforge import runtime_project as _project
from starforge import runtime_records as _records
from starforge import runtime_review as _review
from starforge import runtime_security as _security
from starforge import runtime_support as _support

def _export_names(value: str) -> frozenset[str]:
    return frozenset(value.split())

_COMPATIBILITY_EXPORT_GROUPS = {
    _support: _export_names("CANONICAL_STATE HOOK_TRUST_NOTICE_FILE LEDGER_FILE MAX_AUTO_CONTINUES PLAN_FILE PROOF_FILE SECRET_RE SERVER_LEASE SF_VERSION SOURCE_PROFILE_FILE SOURCE_PROFILE_SCHEMA WAIVES_FILE ForgeError append_jsonl ensure_git_repo file_sha256 git_head git_status is_text_file read_json redact relative_to_project run_git scan_paths snapshot_file_candidates source_dirty_entries source_hash write_json"),
    _project: _export_names("enforcement_mode ensure_project_manifest ensure_source_profile ensure_state_dirs git_history_has_fast_mvp_before_gates hooks_liveness project_profile read_source_profile required_review_policy required_review_roles resolve_project review_profile review_roles_for_profile"),
    _plan: _export_names("blueprint_has_valid_lock blueprint_is_approved command_is_noop parse_tasks parse_tasks_from_text plan_parse_problem ready_tasks scope_hash task_files task_files_are_infrastructure task_files_are_python_control_plane task_is_visual task_owns_visual_source task_proof_kinds task_requires_real_workers validate_tasks"),
    _records: _export_names("cmd_browser_run cmd_server_lease cmd_verify fresh_passing_verify has_noop_verify load_run_records passing_browser_runs"),
    _review: _export_names("cmd_complete_task cmd_done cmd_review cmd_waive known_subagent_ids load_review_findings local_subagent_ids merge_review review_findings_for_done reviews_scope_dir secret_scan_findings"),
    _orchestration: _export_names("agent_role_names cmd_agents_install cmd_approve_blueprint cmd_approve_change cmd_doctor cmd_init cmd_learn cmd_migrate_plan cmd_quality cmd_run cmd_status cmd_validate_plan learnings_digest learnings_report operating_card render_agent_toml reviewer_spawn_prompt scaffold_amend spawn_plan version_core version_key"),
    _preview: _export_names("cmd_preview_proof cmd_proof_run"), _native: _export_names("cmd_native_ios_proof cmd_native_macos_proof"), _security: _export_names("cmd_security_handoff_packet cmd_security_proof cmd_source_packet_github_pr_review cmd_source_packet_proof"),
    _hooks: _export_names("cmd_hook cmd_post_hook cmd_pre_compact_hook cmd_prompt_hook cmd_self_test cmd_session_start_hook cmd_stop_hook cmd_subagent_start_hook cmd_subagent_stop_hook should_block_stop"),
}
_COMPATIBILITY_EXPORTS = {name: provider for provider, names in _COMPATIBILITY_EXPORT_GROUPS.items() for name in names}
_LEGACY_DIRECT_ONLY = frozenset("PLAN_FILE ForgeError agent_role_names cmd_agents_install cmd_approve_change cmd_complete_task cmd_doctor cmd_hook cmd_learn cmd_native_ios_proof cmd_native_macos_proof cmd_post_hook cmd_pre_compact_hook cmd_preview_proof cmd_proof_run cmd_prompt_hook cmd_quality cmd_self_test cmd_security_handoff_packet cmd_security_proof cmd_server_lease cmd_session_start_hook cmd_source_packet_github_pr_review cmd_source_packet_proof cmd_status cmd_stop_hook cmd_subagent_start_hook cmd_subagent_stop_hook cmd_verify cmd_waive render_agent_toml".split())
agent_role_names = _orchestration.agent_role_names
render_agent_toml = _orchestration.render_agent_toml
def __getattr__(name: str) -> object:
    """Resolve an explicitly declared compatibility export."""
    provider = _COMPATIBILITY_EXPORTS.get(name)
    if provider is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(provider, name)
__all__ = ("build_parser", "live_common", "main", "project_changes", "project_lifecycle", *sorted(set(_COMPATIBILITY_EXPORTS) - _LEGACY_DIRECT_ONLY))
def _option(*flags: str, **settings: object) -> tuple[tuple[str, ...], dict[str, object]]:
    return flags, settings

_empty = partial(_option, default="")
_flag = partial(_option, action="store_true")
_required = partial(_option, required=True)

_OPTIONS: dict[str, tuple[tuple[str, ...], dict[str, object]]] = {}

def _register(names: str, builder: object) -> None:
    for name in names.split():
        flag = "--" + name.replace("_", "-")
        _OPTIONS[name] = builder(flag)

_register("objective product_slug codex_home active_plugin_root command summary url live_manifest deployment_metadata smoke_checks artifact scheme simulator build_result launch_result test_result ui_snapshot app_name bundle_id run_result app_bundle signing_note packaging_note kind input scanner scanner_version findings base_url detail", _empty)
_register("fast_mvp no_auto_init no_agents force include_files noop require_server_lease degraded write_summary", _flag)
_register("change scenario finding reason title rule", _required)
for _name in "interaction_evidence console_evidence changed_file trigger".split():
    _OPTIONS[_name] = _option("--" + _name.replace("_", "-"), action="append")
_OPTIONS.update({
    "mode": _option("--mode", default="cruise", choices=["cruise", "sync"]),
    "project_profile": _option("--profile", default="", choices=["", "standard", "fast-mvp"]),
    "adopt_root": _flag("--adopt-root", help="Deliberately build in an existing foreign project root (recorded in the manifest)"),
    "run_no_hooks": _flag("--no-hooks", help="Suppress optional hook trust prompts for this run"),
    "init_no_hooks": _flag("--no-hooks", help="Compatibility flag; init does not install project-local hooks"),
    "global_learnings": _flag("--global-learnings", help="Opt in to reading validated global learnings for this run"),
    "plan_file": _option("--file", default=_support.PLAN_FILE),
    "output": _required("--output", help="New draft path; the legacy Plan is never overwritten"),
    "source_root": _empty("--source-root", "--plugin-root", dest="source_root"),
    "timeout": _option("--timeout", type=int, default=120),
    "viewport": _option("--viewport", action="append", help="NAME=WIDTHxHEIGHT:SCREENSHOT or NAME=SCREENSHOT"),
    "append_screenshot": _option("--screenshot", action="append"),
    "screenshot": _empty("--screenshot"),
    "require_viewports": _flag("--require-viewports", default=True),
    "no_require_viewports": _option("--no-require-viewports", action="store_false", dest="require_viewports"),
    "require_interaction": _flag("--require-interaction", default=True),
    "no_require_interaction": _option("--no-require-interaction", action="store_false", dest="require_interaction"),
    "require_console": _flag("--require-console", default=True),
    "no_require_console": _option("--no-require-console", action="store_false", dest="require_console"),
    "server_lease": _option("--server-lease", nargs="?", const=str(_support.SERVER_LEASE), default=""),
    "expect_status": _option("--expect-status", type=int, default=200),
    "proof_profile": _required("--profile"),
    "action": _option("--action", choices=["claim", "release", "status"], default="claim"),
    "port": _option("--port", type=int),
    "owner": _option("--owner", default="star-forge"),
    "pid": _option("--pid", type=int),
    "category": _option("--category", default="general", choices=sorted(global_learnings.ALLOWED_CATEGORIES)),
    "source": _option("--source", default="manual", choices=sorted(global_learnings.ALLOWED_ORIGINS)),
    "confidence": _option("--confidence", default="medium", choices=sorted(global_learnings.ALLOWED_CONFIDENCE)),
})

def _command(help_text: str, func: object, options: str = "", **settings: object) -> dict[str, object]:
    return {"help": help_text, "func": func, "options": tuple(options.split()), **settings}

_COMMANDS = {
    "run": _command("Run the Star Forge Forge-Loop state machine", _orchestration.cmd_run, "objective mode fast_mvp project_profile product_slug adopt_root no_auto_init run_no_hooks no_agents global_learnings", strict=True),
    "init": _command("Initialize Star Forge artifacts", _orchestration.cmd_init, "force no_agents init_no_hooks product_slug adopt_root fast_mvp project_profile"),
    "approve-blueprint": _command("Write a content lock after explicit user approval of Blueprint.md", _orchestration.cmd_approve_blueprint),
    "approve-change": _command("Approve and activate a derived post-completion change packet", _orchestration.cmd_approve_change, "change"),
    "validate-plan": _command("Validate Plan.md", _orchestration.cmd_validate_plan, "plan_file", strict=True),
    "migrate-plan": _command("Create a separate reviewable Plan v2 draft from a legacy Plan", _orchestration.cmd_migrate_plan, "plan_file output"),
    "status": _command("Read-only Star Forge state (no mutation)", _orchestration.cmd_status),
    "doctor": _command("Read-only Codex installation diagnostics", _orchestration.cmd_doctor, "codex_home source_root active_plugin_root", project=False, strict=True),
    "quality": _command("Classify source and report deterministic architecture debt", _orchestration.cmd_quality, "include_files", strict=True),
    "verify": _command("Run and record a Star Forge-owned verification command", _records.cmd_verify, "command summary timeout noop", task=True, strict=True),
    "browser-run": _command("Record a deterministic browser scenario with viewport/interaction/console evidence", _records.cmd_browser_run, "url scenario viewport append_screenshot interaction_evidence console_evidence live_manifest summary require_viewports no_require_viewports require_interaction no_require_interaction require_console no_require_console server_lease require_server_lease degraded", task=True, strict=True),
    "preview-proof": _command("Validate and record provider-neutral preview proof evidence", _preview.cmd_preview_proof, "url expect_status deployment_metadata smoke_checks", task=True, strict=True),
    "proof-run": _command("Validate and record a generic live proof profile artifact", _preview.cmd_proof_run, "proof_profile artifact", task=True, strict=True),
    "native-ios-proof": _command("Validate and record native iOS proof evidence", _native.cmd_native_ios_proof, "scheme simulator build_result launch_result test_result screenshot ui_snapshot", task=True, strict=True),
    "native-macos-proof": _command("Validate and record native macOS proof evidence", _native.cmd_native_macos_proof, "app_name bundle_id build_result run_result test_result screenshot app_bundle signing_note packaging_note", task=True, strict=True),
    "security-handoff-packet": _command("Validate and record a security scanner handoff packet", _security.cmd_security_handoff_packet, "kind input", strict=True),
    "security-proof": _command("Validate and record security proof evidence", _security.cmd_security_proof, "proof_profile scanner scanner_version findings artifact", task=True, strict=True),
    "source-packet-proof": _command("Validate and record source packet proof evidence", _security.cmd_source_packet_proof, "proof_profile input", task=True, strict=True),
    "source-packet-github-pr-review": _command("Validate and record read-only GitHub PR source packet evidence", _security.cmd_source_packet_github_pr_review, "input", strict=True),
    "server-lease": _command("Claim, release, or inspect the local dev-server lease", _records.cmd_server_lease, "action port base_url command owner pid"),
    "review": _command("Merge reviewer findings + tree scan into the fix queue", _review.cmd_review, strict=True),
    "waive": _command("Waive a review finding with a recorded reason", _review.cmd_waive, "finding reason"),
    "complete-task": _command("Mark one Plan.md task complete after proof checks pass", _review.cmd_complete_task, "changed_file summary", task=True),
    "done": _command("Compute the completion predicate from git facts and record proof", _review.cmd_done, "write_summary", strict=True),
    "learn": _command("Write a validated global learning after explicit opt-in", _orchestration.cmd_learn, "title rule trigger category detail source confidence global_learnings"),
    "agents-install": _command("Install Star Forge roles as native Codex agents (.codex/agents/*.toml)", _orchestration.cmd_agents_install),
    "self-test": _command("Validate the Star Forge plugin package", _hooks.cmd_self_test, project=False, strict=True),
}
_HOOK_NAMES = "hook post-hook prompt-hook session-start-hook subagent-start-hook subagent-stop-hook stop-hook pre-compact-hook".split()
_HOOK_HANDLERS = {name: getattr(_hooks, "cmd_" + name.replace("-", "_")) for name in _HOOK_NAMES}
_COMMANDS.update({name: _command(f"Codex {name} handler", func, project=False) for name, func in _HOOK_HANDLERS.items()})

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Star Forge deterministic helper")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, spec in _COMMANDS.items():
        child = sub.add_parser(name, help=str(spec["help"]))
        if spec.get("project", True):
            child.add_argument("--project", default=".")
        if spec.get("task"):
            child.add_argument("--task", required=True)
        if spec.get("strict"):
            child.add_argument("--strict", action="store_true")
        for option_name in spec["options"]:
            flags, settings = _OPTIONS[option_name]
            child.add_argument(*flags, **settings)
        child.set_defaults(func=spec["func"])
    return parser
HOOK_COMMANDS = frozenset(_HOOK_HANDLERS)

def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    is_hook = str(getattr(args, "command", "")) in HOOK_COMMANDS
    try:
        return int(args.func(args))
    except _support.ForgeError as exc:
        if is_hook:
            # An observation-only hook must never block a tool call on its own bug.
            return 0
        print(json.dumps({"schema": "star-forge.error.v1", "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    except Exception:
        if is_hook:
            return 0
        raise

if __name__ == "__main__":
    raise SystemExit(main())
