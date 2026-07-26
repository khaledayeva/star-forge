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
from typing import Sequence
from starforge import learnings as global_learnings
from starforge import changes as project_changes
from starforge import lifecycle as project_lifecycle
from live_collectors import common as live_common
from starforge.runtime_support import CANONICAL_STATE, HOOK_TRUST_NOTICE_FILE, LEDGER_FILE, MAX_AUTO_CONTINUES, PLAN_FILE, PROOF_FILE, SECRET_RE, SERVER_LEASE, SF_VERSION, SOURCE_PROFILE_FILE, SOURCE_PROFILE_SCHEMA, WAIVES_FILE, ForgeError, append_jsonl, ensure_git_repo, file_sha256, git_head, git_status, is_text_file, read_json, redact, relative_to_project, run_git, scan_paths, snapshot_file_candidates, source_dirty_entries, source_hash, write_json
from starforge.runtime_project import enforcement_mode, ensure_project_manifest, ensure_source_profile, ensure_state_dirs, git_history_has_fast_mvp_before_gates, hooks_liveness, project_profile, read_source_profile, required_review_policy, required_review_roles, resolve_project, review_profile, review_roles_for_profile
from starforge.runtime_plan import blueprint_has_valid_lock, blueprint_is_approved, command_is_noop, parse_tasks, parse_tasks_from_text, plan_parse_problem, ready_tasks, scope_hash, task_files, task_files_are_infrastructure, task_files_are_python_control_plane, task_is_visual, task_owns_visual_source, task_proof_kinds, task_requires_real_workers, validate_tasks
from starforge.runtime_records import cmd_browser_run, cmd_server_lease, cmd_verify, fresh_passing_verify, has_noop_verify, load_run_records, passing_browser_runs
from starforge.runtime_preview import cmd_preview_proof, cmd_proof_run
from starforge.runtime_native import cmd_native_ios_proof, cmd_native_macos_proof
from starforge.runtime_security import cmd_security_handoff_packet, cmd_security_proof, cmd_source_packet_github_pr_review, cmd_source_packet_proof
from starforge.runtime_review import cmd_complete_task, cmd_done, cmd_review, cmd_waive, known_subagent_ids, load_review_findings, local_subagent_ids, merge_review, review_findings_for_done, reviews_scope_dir, secret_scan_findings
from starforge.runtime_orchestration import agent_role_names, cmd_agents_install, cmd_approve_blueprint, cmd_approve_change, cmd_doctor, cmd_init, cmd_learn, cmd_migrate_plan, cmd_quality, cmd_run, cmd_status, cmd_validate_plan, learnings_digest, learnings_report, operating_card, render_agent_toml, reviewer_spawn_prompt, scaffold_amend, spawn_plan, version_core, version_key
from starforge.runtime_hooks import cmd_hook, cmd_post_hook, cmd_pre_compact_hook, cmd_prompt_hook, cmd_self_test, cmd_session_start_hook, cmd_stop_hook, cmd_subagent_start_hook, cmd_subagent_stop_hook, should_block_stop

__all__ = ("build_parser", "main", "cmd_run", "cmd_init", "cmd_review", "cmd_done", "cmd_approve_blueprint", "cmd_browser_run", "cmd_migrate_plan", "cmd_validate_plan", "CANONICAL_STATE", "HOOK_TRUST_NOTICE_FILE", "LEDGER_FILE", "MAX_AUTO_CONTINUES", "PROOF_FILE", "SECRET_RE", "SERVER_LEASE", "SF_VERSION", "SOURCE_PROFILE_FILE", "SOURCE_PROFILE_SCHEMA", "WAIVES_FILE", "append_jsonl", "blueprint_has_valid_lock", "blueprint_is_approved", "command_is_noop", "enforcement_mode", "ensure_git_repo", "ensure_project_manifest", "ensure_source_profile", "ensure_state_dirs", "file_sha256", "fresh_passing_verify", "git_head", "git_history_has_fast_mvp_before_gates", "git_status", "has_noop_verify", "hooks_liveness", "is_text_file", "known_subagent_ids", "learnings_digest", "learnings_report", "live_common", "load_review_findings", "load_run_records", "local_subagent_ids", "merge_review", "operating_card", "parse_tasks", "parse_tasks_from_text", "passing_browser_runs", "plan_parse_problem", "project_changes", "project_lifecycle", "project_profile", "read_json", "read_source_profile", "ready_tasks", "redact", "relative_to_project", "required_review_policy", "required_review_roles", "resolve_project", "review_findings_for_done", "review_profile", "review_roles_for_profile", "reviewer_spawn_prompt", "reviews_scope_dir", "run_git", "scaffold_amend", "scan_paths", "scope_hash", "secret_scan_findings", "should_block_stop", "snapshot_file_candidates", "source_dirty_entries", "source_hash", "spawn_plan", "task_files", "task_files_are_infrastructure", "task_files_are_python_control_plane", "task_is_visual", "task_owns_visual_source", "task_proof_kinds", "task_requires_real_workers", "validate_tasks", "version_core", "version_key", "write_json")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Star Forge deterministic helper")
    sub = parser.add_subparsers(dest="command", required=True)
    def command(name: str, help: str, func: object, *, project: bool = True,
                task: bool = False, strict: bool = False) -> argparse.ArgumentParser:
        child = sub.add_parser(name, help=help)
        if project:
            child.add_argument("--project", default=".")
        if task:
            child.add_argument("--task", required=True)
        if strict:
            child.add_argument("--strict", action="store_true")
        child.set_defaults(func=func)
        return child
    p = command("run", "Run the Star Forge Forge-Loop state machine", cmd_run, strict=True)
    p.add_argument("--objective", default="")
    p.add_argument("--mode", default="cruise", choices=["cruise", "sync"])
    p.add_argument("--fast-mvp", action="store_true")
    p.add_argument("--profile", default="", choices=["", "standard", "fast-mvp"])
    p.add_argument("--product-slug", default="")
    p.add_argument("--adopt-root", action="store_true", help="Deliberately build in an existing foreign project root (recorded in the manifest)")
    p.add_argument("--no-auto-init", action="store_true")
    p.add_argument("--no-hooks", action="store_true", help="Suppress optional hook trust prompts for this run")
    p.add_argument("--no-agents", action="store_true", help="Do not generate project-local agent profiles during auto-init")
    p.add_argument(
        "--global-learnings",
        action="store_true",
        help="Opt in to reading validated global learnings for this run",
    )
    p = command("init", "Initialize Star Forge artifacts", cmd_init)
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-agents", action="store_true")
    p.add_argument("--no-hooks", action="store_true", help="Compatibility flag; init does not install project-local hooks")
    p.add_argument("--product-slug", default="")
    p.add_argument("--adopt-root", action="store_true")
    p.add_argument("--fast-mvp", action="store_true")
    p.add_argument("--profile", default="", choices=["", "standard", "fast-mvp"])
    p = command(
        "approve-blueprint",
        help="Write a content lock after explicit user approval of Blueprint.md",
        func=cmd_approve_blueprint,
    )
    p = command(
        "approve-change",
        help="Approve and activate a derived post-completion change packet",
        func=cmd_approve_change,
    )
    p.add_argument("--change", required=True)
    p = command("validate-plan", "Validate Plan.md", cmd_validate_plan, strict=True)
    p.add_argument("--file", default=PLAN_FILE)
    p = command(
        "migrate-plan",
        help="Create a separate reviewable Plan v2 draft from a legacy Plan",
        func=cmd_migrate_plan,
    )
    p.add_argument("--file", default=PLAN_FILE)
    p.add_argument(
        "--output",
        required=True,
        help="New draft path; the legacy Plan is never overwritten",
    )
    command("status", "Read-only Star Forge state (no mutation)", cmd_status)
    p = command("doctor", "Read-only Codex installation diagnostics", cmd_doctor, project=False, strict=True)
    p.add_argument("--codex-home", default="")
    p.add_argument("--source-root", "--plugin-root", dest="source_root", default="")
    p.add_argument("--active-plugin-root", default="")
    p = command(
        "quality",
        help="Classify source and report deterministic architecture debt",
        func=cmd_quality,
        strict=True,
    )
    p.add_argument("--include-files", action="store_true")
    p = command("verify", "Run and record a Star Forge-owned verification command", cmd_verify, task=True, strict=True)
    p.add_argument("--command", default="")
    p.add_argument("--summary", default="")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--noop", action="store_true")
    p = command("browser-run", "Record a deterministic browser scenario with viewport/interaction/console evidence", cmd_browser_run, task=True, strict=True)
    p.add_argument("--url", default="")
    p.add_argument("--scenario", required=True)
    p.add_argument("--viewport", action="append", help="NAME=WIDTHxHEIGHT:SCREENSHOT or NAME=SCREENSHOT")
    p.add_argument("--screenshot", action="append")
    p.add_argument("--interaction-evidence", action="append")
    p.add_argument("--console-evidence", action="append")
    p.add_argument("--live-manifest", default="")
    p.add_argument("--summary", default="")
    p.add_argument("--require-viewports", action="store_true", default=True)
    p.add_argument("--no-require-viewports", action="store_false", dest="require_viewports")
    p.add_argument("--require-interaction", action="store_true", default=True)
    p.add_argument("--no-require-interaction", action="store_false", dest="require_interaction")
    p.add_argument("--require-console", action="store_true", default=True)
    p.add_argument("--no-require-console", action="store_false", dest="require_console")
    p.add_argument("--server-lease", nargs="?", const=str(SERVER_LEASE), default="")
    p.add_argument("--require-server-lease", action="store_true")
    p.add_argument("--degraded", action="store_true")
    p = command("preview-proof", "Validate and record provider-neutral preview proof evidence", cmd_preview_proof, task=True, strict=True)
    p.add_argument("--url", default="")
    p.add_argument("--expect-status", type=int, default=200)
    p.add_argument("--deployment-metadata", default="")
    p.add_argument("--smoke-checks", default="")
    p = command("proof-run", "Validate and record a generic live proof profile artifact", cmd_proof_run, task=True, strict=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--artifact", default="")
    p = command("native-ios-proof", "Validate and record native iOS proof evidence", cmd_native_ios_proof, task=True, strict=True)
    p.add_argument("--scheme", default="")
    p.add_argument("--simulator", default="")
    p.add_argument("--build-result", default="")
    p.add_argument("--launch-result", default="")
    p.add_argument("--test-result", default="")
    p.add_argument("--screenshot", default="")
    p.add_argument("--ui-snapshot", default="")
    p = command("native-macos-proof", "Validate and record native macOS proof evidence", cmd_native_macos_proof, task=True, strict=True)
    p.add_argument("--app-name", default="")
    p.add_argument("--bundle-id", default="")
    p.add_argument("--build-result", default="")
    p.add_argument("--run-result", default="")
    p.add_argument("--test-result", default="")
    p.add_argument("--screenshot", default="")
    p.add_argument("--app-bundle", default="")
    p.add_argument("--signing-note", default="")
    p.add_argument("--packaging-note", default="")
    p = command("security-handoff-packet", "Validate and record a security scanner handoff packet", cmd_security_handoff_packet, strict=True)
    p.add_argument("--kind", default="")
    p.add_argument("--input", default="")
    p = command("security-proof", "Validate and record security proof evidence", cmd_security_proof, task=True, strict=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--scanner", default="")
    p.add_argument("--scanner-version", default="")
    p.add_argument("--findings", default="")
    p.add_argument("--artifact", default="")
    p = command("source-packet-proof", "Validate and record source packet proof evidence", cmd_source_packet_proof, task=True, strict=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--input", default="")
    p = command("source-packet-github-pr-review", "Validate and record read-only GitHub PR source packet evidence", cmd_source_packet_github_pr_review, strict=True)
    p.add_argument("--input", default="")
    p = command("server-lease", "Claim, release, or inspect the local dev-server lease", cmd_server_lease)
    p.add_argument("--action", choices=["claim", "release", "status"], default="claim")
    p.add_argument("--port", type=int)
    p.add_argument("--base-url", default="")
    p.add_argument("--command", default="")
    p.add_argument("--owner", default="star-forge")
    p.add_argument("--pid", type=int)
    command("review", "Merge reviewer findings + tree scan into the fix queue", cmd_review, strict=True)
    p = command("waive", "Waive a review finding with a recorded reason", cmd_waive)
    p.add_argument("--finding", required=True)
    p.add_argument("--reason", required=True)
    p = command("complete-task", "Mark one Plan.md task complete after proof checks pass", cmd_complete_task, task=True)
    p.add_argument("--changed-file", action="append")
    p.add_argument("--summary", default="")
    p = command("done", "Compute the completion predicate from git facts and record proof", cmd_done, strict=True)
    p.add_argument("--write-summary", action="store_true")
    p = command(
        "learn",
        help="Write a validated global learning after explicit opt-in",
        func=cmd_learn,
    )
    p.add_argument("--title", required=True)
    p.add_argument("--rule", required=True)
    p.add_argument("--trigger", action="append")
    p.add_argument(
        "--category",
        default="general",
        choices=sorted(global_learnings.ALLOWED_CATEGORIES),
    )
    p.add_argument("--detail", default="")
    p.add_argument(
        "--source",
        default="manual",
        choices=sorted(global_learnings.ALLOWED_ORIGINS),
    )
    p.add_argument(
        "--confidence",
        default="medium",
        choices=sorted(global_learnings.ALLOWED_CONFIDENCE),
    )
    p.add_argument(
        "--global-learnings",
        action="store_true",
        help="Opt in to writing this validated global learning",
    )
    command("agents-install", "Install Star Forge roles as native Codex agents (.codex/agents/*.toml)", cmd_agents_install)
    command("self-test", "Validate the Star Forge plugin package", cmd_self_test, project=False, strict=True)
    for name, func in [
        ("hook", cmd_hook),
        ("post-hook", cmd_post_hook),
        ("prompt-hook", cmd_prompt_hook),
        ("session-start-hook", cmd_session_start_hook),
        ("subagent-start-hook", cmd_subagent_start_hook),
        ("subagent-stop-hook", cmd_subagent_stop_hook),
        ("stop-hook", cmd_stop_hook),
        ("pre-compact-hook", cmd_pre_compact_hook),
    ]:
        p = sub.add_parser(name, help=f"Codex {name} handler")
        p.set_defaults(func=func)
    return parser
HOOK_COMMANDS = {
    "hook",
    "post-hook",
    "prompt-hook",
    "session-start-hook",
    "subagent-start-hook",
    "subagent-stop-hook",
    "stop-hook",
    "pre-compact-hook",
}

def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    is_hook = str(getattr(args, "command", "")) in HOOK_COMMANDS
    try:
        return int(args.func(args))
    except ForgeError as exc:
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
