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
from starforge import runtime_support as _runtime_support
from starforge import runtime_project as _runtime_project
from starforge import runtime_plan as _runtime_plan
from starforge import runtime_records as _runtime_records
from starforge import runtime_preview as _runtime_preview
from starforge import runtime_native as _runtime_native
from starforge import runtime_security as _runtime_security
from starforge import runtime_review as _runtime_review
from starforge import runtime_orchestration as _runtime_orchestration
from starforge import runtime_hooks as _runtime_hooks

_RUNTIME_MODULES = (
    _runtime_support,
    _runtime_project,
    _runtime_plan,
    _runtime_records,
    _runtime_preview,
    _runtime_native,
    _runtime_security,
    _runtime_review,
    _runtime_orchestration,
    _runtime_hooks,
)
_RUNTIME_NAMESPACE = {}
for _module in _RUNTIME_MODULES:
    _RUNTIME_NAMESPACE.update({name: value for name, value in vars(_module).items() if not name.startswith("__")})
for _module in _RUNTIME_MODULES:
    vars(_module).update(_RUNTIME_NAMESPACE)
globals().update(_RUNTIME_NAMESPACE)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Star Forge deterministic helper")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="Run the Star Forge Forge-Loop state machine")
    p.add_argument("--project", default=".")
    p.add_argument("--objective", default="")
    p.add_argument("--mode", default="cruise", choices=["cruise", "sync"])
    p.add_argument("--fast-mvp", action="store_true")
    p.add_argument("--profile", default="", choices=["", "standard", "fast-mvp"])
    p.add_argument("--product-slug", default="")
    p.add_argument("--adopt-root", action="store_true", help="Deliberately build in an existing foreign project root (recorded in the manifest)")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--no-auto-init", action="store_true")
    p.add_argument("--no-hooks", action="store_true", help="Suppress optional hook trust prompts for this run")
    p.add_argument("--no-agents", action="store_true", help="Do not generate project-local agent profiles during auto-init")
    p.add_argument(
        "--global-learnings",
        action="store_true",
        help="Opt in to reading validated global learnings for this run",
    )
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("init", help="Initialize Star Forge artifacts")
    p.add_argument("--project", default=".")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-agents", action="store_true")
    p.add_argument("--no-hooks", action="store_true", help="Compatibility flag; init does not install project-local hooks")
    p.add_argument("--product-slug", default="")
    p.add_argument("--adopt-root", action="store_true")
    p.add_argument("--fast-mvp", action="store_true")
    p.add_argument("--profile", default="", choices=["", "standard", "fast-mvp"])
    p.set_defaults(func=cmd_init)
    p = sub.add_parser(
        "approve-blueprint",
        help="Write a content lock after explicit user approval of Blueprint.md",
    )
    p.add_argument("--project", default=".")
    p.set_defaults(func=cmd_approve_blueprint)
    p = sub.add_parser(
        "approve-change",
        help="Approve and activate a derived post-completion change packet",
    )
    p.add_argument("--project", default=".")
    p.add_argument("--change", required=True)
    p.set_defaults(func=cmd_approve_change)
    p = sub.add_parser("validate-plan", help="Validate Plan.md")
    p.add_argument("--file", default=PLAN_FILE)
    p.add_argument("--project", default=".")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_validate_plan)
    p = sub.add_parser(
        "migrate-plan",
        help="Create a separate reviewable Plan v2 draft from a legacy Plan",
    )
    p.add_argument("--project", default=".")
    p.add_argument("--file", default=PLAN_FILE)
    p.add_argument(
        "--output",
        required=True,
        help="New draft path; the legacy Plan is never overwritten",
    )
    p.set_defaults(func=cmd_migrate_plan)
    p = sub.add_parser("status", help="Read-only Star Forge state (no mutation)")
    p.add_argument("--project", default=".")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("doctor", help="Read-only Codex installation diagnostics")
    p.add_argument("--codex-home", default="")
    p.add_argument("--source-root", "--plugin-root", dest="source_root", default="")
    p.add_argument("--active-plugin-root", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser(
        "quality",
        help="Classify source and report deterministic architecture debt",
    )
    p.add_argument("--project", default=".")
    p.add_argument("--include-files", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_quality)
    p = sub.add_parser("verify", help="Run and record a Star Forge-owned verification command")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--command", default="")
    p.add_argument("--summary", default="")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--noop", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("browser-run", help="Record a deterministic browser scenario with viewport/interaction/console evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
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
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_browser_run)
    p = sub.add_parser("preview-proof", help="Validate and record provider-neutral preview proof evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--url", default="")
    p.add_argument("--expect-status", type=int, default=200)
    p.add_argument("--deployment-metadata", default="")
    p.add_argument("--smoke-checks", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_preview_proof)
    p = sub.add_parser("proof-run", help="Validate and record a generic live proof profile artifact")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--artifact", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_proof_run)
    p = sub.add_parser("native-ios-proof", help="Validate and record native iOS proof evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--scheme", default="")
    p.add_argument("--simulator", default="")
    p.add_argument("--build-result", default="")
    p.add_argument("--launch-result", default="")
    p.add_argument("--test-result", default="")
    p.add_argument("--screenshot", default="")
    p.add_argument("--ui-snapshot", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_native_ios_proof)
    p = sub.add_parser("native-macos-proof", help="Validate and record native macOS proof evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--app-name", default="")
    p.add_argument("--bundle-id", default="")
    p.add_argument("--build-result", default="")
    p.add_argument("--run-result", default="")
    p.add_argument("--test-result", default="")
    p.add_argument("--screenshot", default="")
    p.add_argument("--app-bundle", default="")
    p.add_argument("--signing-note", default="")
    p.add_argument("--packaging-note", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_native_macos_proof)
    p = sub.add_parser("security-handoff-packet", help="Validate and record a security scanner handoff packet")
    p.add_argument("--project", default=".")
    p.add_argument("--kind", default="")
    p.add_argument("--input", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_security_handoff_packet)
    p = sub.add_parser("security-proof", help="Validate and record security proof evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--scanner", default="")
    p.add_argument("--scanner-version", default="")
    p.add_argument("--findings", default="")
    p.add_argument("--artifact", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_security_proof)
    p = sub.add_parser("source-packet-proof", help="Validate and record source packet proof evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--input", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_source_packet_proof)
    p = sub.add_parser("source-packet-github-pr-review", help="Validate and record read-only GitHub PR source packet evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--input", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_source_packet_github_pr_review)
    p = sub.add_parser("server-lease", help="Claim, release, or inspect the local dev-server lease")
    p.add_argument("--project", default=".")
    p.add_argument("--action", choices=["claim", "release", "status"], default="claim")
    p.add_argument("--port", type=int)
    p.add_argument("--base-url", default="")
    p.add_argument("--command", default="")
    p.add_argument("--owner", default="star-forge")
    p.add_argument("--pid", type=int)
    p.set_defaults(func=cmd_server_lease)
    p = sub.add_parser("review", help="Merge reviewer findings + tree scan into the fix queue")
    p.add_argument("--project", default=".")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_review)
    p = sub.add_parser("waive", help="Waive a review finding with a recorded reason")
    p.add_argument("--project", default=".")
    p.add_argument("--finding", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_waive)
    p = sub.add_parser("complete-task", help="Mark one Plan.md task complete after proof checks pass")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--changed-file", action="append")
    p.add_argument("--summary", default="")
    p.set_defaults(func=cmd_complete_task)
    p = sub.add_parser("done", help="Compute the completion predicate from git facts and record proof")
    p.add_argument("--project", default=".")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--write-summary", action="store_true")
    p.set_defaults(func=cmd_done)
    p = sub.add_parser(
        "learn",
        help="Write a validated global learning after explicit opt-in",
    )
    p.add_argument("--project", default=".")
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
    p.set_defaults(func=cmd_learn)
    p = sub.add_parser("agents-install", help="Install Star Forge roles as native Codex agents (.codex/agents/*.toml)")
    p.add_argument("--project", default=".")
    p.set_defaults(func=cmd_agents_install)
    p = sub.add_parser("self-test", help="Validate the Star Forge plugin package")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_self_test)
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

for _module in _RUNTIME_MODULES:
    vars(_module)["build_parser"] = build_parser

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
