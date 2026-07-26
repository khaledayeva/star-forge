"""Cohesive Star Forge runtime extracted from the CLI facade."""

from __future__ import annotations
import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence
from starforge import changes as project_changes
from starforge import contracts as project_contracts
from starforge import doctor as installation_doctor
from starforge import learnings as global_learnings
from starforge import lifecycle as project_lifecycle
from starforge import quality as project_quality
from starforge import review_policy as adaptive_review_policy
from .policy_data import mapping as policy_mapping, project as project_record, record as policy_record, value as _policy_value
from .runtime_support import AGENT_NAME_PREFIX, BLOCKING_SEVERITIES, BLUEPRINT_FILE, CANONICAL_STATE, HOOK_EVENTS, HOOK_TRUST_NOTICE_FILE, INCIDENTS_FILE, LEDGER_FILE, PLAN_FILE, PROJECT_MANIFEST, REVIEW_ROLE_LENSES, SF_VERSION, SOURCE_PROFILE_FILE, ForgeError, append_jsonl, blocking_items, ensure_git_repo, ensure_gitignore_entries, file_sha256, git_status, git_status_path, is_git_repo, jsonl_payloads, now_utc, plugin_root, read_json, read_text, relative_to_project, slugify, snapshot_file_candidates, template_text, write_json, write_json_stable, write_text
from .runtime_project import enforcement_mode, ensure_project_manifest, ensure_state_dirs, fast_mvp_profile_lock_state, fast_mvp_profile_selected_before_gates, find_star_forge_project_root, hooks_liveness, normalize_project_profile, profile_downgrade_lock_reasons, project_profile, required_review_policy, resolve_project, review_profile, root_needs_product_isolation, setup_ledger_records_fast_mvp_before_gates, source_hash_exception_problem, source_hash_unavailable_problem, source_hash_unavailable_state, source_profile_path, try_source_hash
from .runtime_plan import all_tasks_complete, append_plan_task, blueprint_has_valid_lock, blueprint_is_approved, blueprint_lifecycle_contract, blueprint_lock_state, command_is_noop, lifecycle_gate_state, parse_tasks, plan_contract_mode, plan_is_placeholder, plan_parse_problem, ready_tasks, scope_hash, task_allows_noop_verification, task_counts, task_files, task_requires_real_workers, task_verify_command, update_plan_task_row, validate_project_plan_contract, validate_tasks
from .runtime_review import annotate_drift_coverage, change_packet_for_drift, change_scope_files, completed_amendment_covering_drift, completed_change_packet_covering_drift, detect_drift, done_payload, load_merged_review, load_proof, review_findings_for_done, reviews_scope_dir

ORCHESTRATION = _policy_value("runtime_orchestration.POLICY")

def _print_record(name: str, *sources: dict[str, Any], **values: Any) -> None:
    print(json.dumps(project_record(name, *sources, **values), indent=2))

def learnings_home() -> Path:
    """Compatibility wrapper for the configured, opt-in global store."""
    return global_learnings.learnings_home()

def project_keywords(project: Path) -> set[str]:
    """Compatibility wrapper for deterministic project matching terms."""
    return global_learnings.project_keywords(project, candidate_names=[relative_to_project(path, project) for path in snapshot_file_candidates(project)])

def learnings_report(
    project: Path,
    limit: int = 5,
    *,
    explicit_opt_in: bool = False,
) -> dict[str, Any]:
    """Return a bounded digest report; corrupt global state never escapes."""
    try:
        return global_learnings.read_digest(project, keywords=project_keywords(project), limit=limit, explicit_opt_in=explicit_opt_in)
    except Exception as exc:
        unavailable = ORCHESTRATION["learnings_unavailable"]
        return project_record(
            "learnings_unavailable",
            unavailable,
            limit=max(0, min(int(limit), global_learnings.MAX_DIGEST_LIMIT)),
            rejection_reasons={type(exc).__name__: 1},
        )

def learnings_digest(project: Path, limit: int = 5, *, explicit_opt_in: bool = False) -> list[dict[str, Any]]:
    return list(learnings_report(project, limit=limit, explicit_opt_in=explicit_opt_in)["items"])

def cmd_learn(args: argparse.Namespace) -> int:
    if not str(args.title or "").strip() or not str(args.rule or "").strip():
        raise ForgeError("learn requires --title and --rule")
    project = resolve_project(args.project)
    current_hash, hash_problem = try_source_hash(project)
    if current_hash is None:
        message = str((hash_problem or {}).get("message") or "source hash unavailable")
        raise ForgeError(f"cannot persist a global learning: {message}")
    try:
        result = global_learnings.write_learning(
            project, title=args.title, rule=args.rule, triggers=args.trigger or [],
            category=args.category, detail=args.detail, confidence=args.confidence,
            origin=args.source, source_hash=current_hash,
            explicit_opt_in=bool(args.global_learnings),
        )
    except global_learnings.LearningsError as exc:
        raise ForgeError(str(exc)) from exc
    record = result["record"]
    _print_record("learn", result, record)
    return 0

def hook_trust_notice(project: Path) -> dict[str, Any]:
    show = bool(not hooks_liveness(project).get("local_events_observed") and not (project / HOOK_TRUST_NOTICE_FILE).exists())
    return policy_mapping("hook_notice", show=show, message=ORCHESTRATION["hook_notice"]["message"], marker=str(HOOK_TRUST_NOTICE_FILE))

def mark_hook_trust_notice_seen(project: Path) -> None:
    ensure_state_dirs(project)
    write_json_stable(project / HOOK_TRUST_NOTICE_FILE, policy_record("hook_trust_notice", shown_at=now_utc(), message=ORCHESTRATION["hook_notice"]["shown_message"]))

def version_core(raw: str) -> str:
    return re.split(r"[+-]", raw, maxsplit=1)[0]

def version_key(raw: str) -> tuple[Any, ...]:
    """Numeric-aware version ordering so 0.10.0 sorts above 0.3.0."""
    return tuple(int(piece) if piece.isdigit() else piece for piece in version_core(raw).split("."))

def newest_cache_version() -> str | None:
    cache = Path.home() / ".codex" / "plugins" / "cache"
    versions: list[str] = []
    for path in cache.glob("*/star-forge/*/.codex-plugin/plugin.json"):
        try:
            versions.append(str(read_json(path).get("version") or path.parent.parent.name))
        except Exception:
            versions.append(path.parent.parent.name)
    return max(versions, key=version_key, default=None)

def version_info(project: Path) -> dict[str, Any]:
    manifest_version: str | None = None
    try:
        manifest_version = str(read_json(plugin_root() / ".codex-plugin" / "plugin.json").get("version") or "")
    except Exception:
        manifest_version = None
    newest = newest_cache_version()
    return policy_mapping("version_info", script=SF_VERSION, plugin_manifest=manifest_version, newest_cache=newest, stale_cache=bool(newest and version_key(newest) < version_key(SF_VERSION)))

def toml_multiline(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return f'"""\n{escaped}"""'

def agent_role_names() -> list[str]:
    return sorted(path.parent.name for path in (plugin_root() / "agents").glob("*/agent.md"))

def render_agent_toml(role: str) -> str:
    source = plugin_root() / "agents" / role / "agent.md"
    body = read_text(source)
    match = re.search(r"## Mission\n+(.+?)(?:\n\n|\n##)", body, re.DOTALL)
    mission = " ".join(match.group(1).split()) if match else ""
    description = (mission[:200] or f"Star Forge {role} role.").rstrip()
    return (f'name = "{AGENT_NAME_PREFIX}{role}"\n'
            f"description = {json.dumps(description)}\n"
            f"developer_instructions = {toml_multiline(body.rstrip())}\n")

def install_agents(project: Path, target: Path) -> list[str]:
    target.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for role in agent_role_names():
        path = target / f"{AGENT_NAME_PREFIX}{role}.toml"
        write_text(path, render_agent_toml(role))
        installed.append(relative_to_project(path, project))
    return installed

def cmd_agents_install(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    _print_record("agents_install", installed=install_agents(project, project / ".codex" / "agents"))
    return 0

def resolve_isolation(raw_project: Path, *, product_slug: str, adopt_root: bool) -> tuple[Path | None, dict[str, Any] | None]:
    """Shared isolation guard for run and init. Returns (project, blocked_payload).
    A foreign project gets its own work/<slug>/ with a root redirect; building at
    the root requires an explicit --adopt-root the manifest records, so a
    contaminated root can never silently bless itself (the Boss Fight failure).
    """
    existing = find_star_forge_project_root(raw_project)
    if existing:
        return existing, None
    if not root_needs_product_isolation(raw_project):
        return raw_project, None
    if str(product_slug or "").strip():
        slug = slugify(product_slug).lower()
        project = (raw_project / "work" / slug).resolve()
        project.mkdir(parents=True, exist_ok=True)
        # The nested manifest must exist before the redirect to prevent the target
        # from resolving back to the foreign root while it has no markers.
        ensure_project_manifest(project, product_slug=slug)
        write_json(raw_project / PROJECT_MANIFEST, policy_record("project_redirect", project_root=str(project)))
        if (raw_project / ".git").exists():
            ensure_gitignore_entries(raw_project, [".starforge/", "work/"])
        for carried in ORCHESTRATION["carried_project_files"]:
            src, dst = raw_project / carried, project / carried
            if src.exists() and not dst.exists():
                write_text(dst, read_text(src))
        return project, None
    if adopt_root:
        ensure_project_manifest(raw_project, root_mode="adopted-root")
        return raw_project, None
    return None, policy_record("isolation_blocked", created_at=now_utc(), project=str(raw_project), phase="blocked:isolation-required",
                               required_next_action=ORCHESTRATION["isolation_next_action"])

def cmd_init(args: argparse.Namespace) -> int:
    raw_project = Path(args.project).resolve()
    project, blocked = resolve_isolation(raw_project, product_slug=args.product_slug or "", adopt_root=args.adopt_root)
    if blocked is not None:
        print(json.dumps(blocked, indent=2))
        return 1
    assert project is not None
    project.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    skipped: list[str] = []
    git_created = ensure_git_repo(project)
    (created if git_created else skipped).append(".git/" if git_created else ".git/ already exists")
    ensure_state_dirs(project)
    requested_profile = "fast-mvp" if getattr(args, "fast_mvp", False) else str(getattr(args, "profile", "") or "")
    profile_selected_before_gates = requested_profile == "fast-mvp" and not profile_downgrade_lock_reasons(project)
    ensure_project_manifest(project, product_slug=args.product_slug or "", profile=requested_profile)
    created.append(str(PROJECT_MANIFEST))
    if requested_profile:
        created.append(SOURCE_PROFILE_FILE)
    for template_name, target_name in ORCHESTRATION["init_templates"]:
        target = project / target_name
        if args.force or not target.exists():
            write_text(target, template_text(template_name))
            created.append(target_name)
        else:
            skipped.append(f"{target_name} already exists")
    gitignore_changes = ensure_gitignore_entries(project, ORCHESTRATION["init_gitignore"])
    created.extend(f".gitignore {item}" for item in gitignore_changes)
    if not getattr(args, "no_agents", False):
        install_agents(project, project / ".codex" / "agents")
        created.append(".codex/agents/")
    setup_values = {"timestamp": now_utc(), **ORCHESTRATION["setup_ledger"], "profile": normalize_project_profile(requested_profile or "standard"),
                    "profile_selected_before_gates": profile_selected_before_gates}
    profile_path = source_profile_path(project)
    descriptor = "ledger_setup_profile" if profile_path.exists() else "ledger_setup"
    if profile_path.exists():
        setup_values["source_profile_sha256"] = file_sha256(profile_path)
    append_jsonl(project / LEDGER_FILE, policy_record(descriptor, **setup_values))
    _print_record("init", created=created, skipped=skipped, project=str(project))
    return 0

def reviewer_spawn_prompt(project: Path, scope: str, role: str = "correctness", *, source_hash_value: str | None = None, selection_reason: str = "") -> str:
    rel = relative_to_project(reviews_scope_dir(project, scope) / f"{role}.findings.json", project)
    if source_hash_value is None:
        source_hash_value, hash_problem = try_source_hash(project)
        if hash_problem or source_hash_value is None:
            message = (hash_problem or source_hash_exception_problem(ForgeError("source_hash unavailable"))).get("message")
            raise ForgeError(str(message))
    sh = source_hash_value
    lens = REVIEW_ROLE_LENSES.get(role, "project quality risks, regressions, and release blockers")
    safe_reason = re.sub(r"[^A-Za-z0-9 .,/_():;-]+", " ", str(selection_reason or "")).strip()
    reason_instruction = (f"Applicability: {safe_reason}. " if safe_reason else "")
    sample = json.dumps(
        {"role": role, "agent_id": "<your real thread id>", "source_hash": sh, "findings": [ORCHESTRATION["review_sample_finding"]]},
        separators=(",", ":"),
    ).replace('"', '\\"')
    return ORCHESTRATION["reviewer_prompt"].format(agent=f"{AGENT_NAME_PREFIX}reviewer", role=role, lens=lens, reason=reason_instruction, findings_file=rel, sample=sample, source_hash=sh)

def builder_spawn_prompt(task: dict[str, Any]) -> str:
    files = ", ".join(task_files(task)) or "(see Plan row)"
    return (f"spawn_agent {AGENT_NAME_PREFIX}builder \"[SF:{task['id']}] Implement task {task['id']}: {task.get('description','')}. "
            f"Files you own: {files}. After implementing, the coordinator records `verify` and (for UI) `browser-run`.\"")

def spawn_plan(project: Path, tasks: Sequence[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
    if phase in {"build", "amend"}:
        eligible = [task for task in tasks if task.get("status") in ORCHESTRATION["builder_statuses"] and task_requires_real_workers(task)]
        return [{"task": task["id"], "agent": f"{AGENT_NAME_PREFIX}builder", "tag": f"SF:{task['id']}", "spawn": builder_spawn_prompt(task)} for task in eligible[:6]]
    if phase != "review":
        return []
    scope = scope_hash(project) or "noscope"
    current_source_hash, hash_problem = try_source_hash(project)
    if hash_problem or current_source_hash is None:
        return []
    plan: list[dict[str, Any]] = []
    for selection in required_review_policy(project, source_hash_value=current_source_hash).selections:
        role = selection.role
        reasons = "; ".join(selection.reasons)
        plan.append(
            policy_mapping(
                "review_spawn",
                role=role, reasons=list(selection.reasons),
                findings_file=relative_to_project(reviews_scope_dir(project, scope) / f"{role}.findings.json", project),
                tag=f"SF:review:{role}", spawn=reviewer_spawn_prompt(project, scope, role, source_hash_value=current_source_hash, selection_reason=reasons),
            ))
    return plan

def operating_card(project: Path, state: dict[str, Any]) -> str:
    versions = state.get("versions", {})
    liveness = hooks_liveness(project)
    hook_kind = "witnessed" if state.get("enforcement") == "witnessed" else "local" if liveness.get("local_events_observed") else "none"
    hooks = ORCHESTRATION["operating_card"]["hook_labels"][hook_kind]
    lines = [
        f"star-forge {versions.get('script')} | hooks: {hooks} | phase: {state.get('phase')}",
        f"NEXT: {state.get('required_next_action')}",
    ]
    profile_lock = state.get("profile_lock") or {}
    if profile_lock.get("status") in ORCHESTRATION["profile_attention_statuses"]:
        note = str(profile_lock.get("message") or "")
        action = str(profile_lock.get("next_action") or "")
        if note:
            lines.append(f"PROFILE: {note}" + (f" Next: {action}" if action else ""))
    if versions.get("stale_cache"):
        lines.append(ORCHESTRATION["operating_card"]["stale_cache"].format(**versions))
    hook_notice = state.get("hook_trust_notice") or {}
    if hook_notice.get("show") and hook_notice.get("message"):
        lines.append(f"HOOKS: {hook_notice.get('message')}")
    spawn = state.get("spawn_plan") or []
    if spawn:
        spawn_limit = (adaptive_review_policy.MAX_REVIEW_AGENTS if state.get("phase") == "review" else 3)
        lines.extend(["SPAWN (paste as-is):", *[f"  {entry.get('spawn')}" for entry in spawn[:spawn_limit]]])
    lines.extend(ORCHESTRATION["operating_card"]["rules"])
    digest = state.get("learnings_digest") or []
    if digest:
        lines.append("LEARNINGS: " + "; ".join(item.get("title", "") for item in digest[:3]))
    return "\n".join(lines)

def lifecycle_next_action(phase: str, state: Mapping[str, Any]) -> str:
    hash_problem = state.get("hash_problem")
    profile_lock, blueprint_state = state["profile_lock"], state["blueprint_state"]
    drift, proof, parse_problem = state["drift"], state.get("proof"), state.get("parse_problem")
    foundation_gate, delivery_gate = state["foundation_gate"], state["delivery_gate"]
    active_change, done = state.get("active_change"), state.get("done")
    if hash_problem and not parse_problem:
        return f"{hash_problem.get('message')} Repair the source hash blocker, then rerun."
    if profile_lock.get("status") == "blocked" and not parse_problem:
        return f"{profile_lock.get('message')} Next action: {profile_lock.get('next_action')}"
    if blueprint_state.get("status") in ORCHESTRATION["blueprint_invalid_statuses"]:
        return "Blueprint approval is invalid. Review the current contract with the user, then run `approve-blueprint` after explicit approval."
    if blueprint_state.get("status") == "legacy-approved" and drift.get("actionable") and proof:
        return "This legacy Blueprint is readable, but amendments require a v0.4 content lock. Run `approve-blueprint` only after explicit user approval."
    actions = ORCHESTRATION["phase_actions"]
    if phase in {"foundation", "deliver"}:
        gate = foundation_gate if phase == "foundation" else delivery_gate
        blocker = next(iter(gate.get("blockers") or []), "")
        return actions[phase] + (f" First blocker: {blocker}" if blocker else "")
    if phase == "amend":
        if active_change and active_change.get("approval_state") == "draft":
            change_id = active_change["change_id"]
            return f"Review change packet {change_id} and run `approve-change --change {change_id}` only after explicit approval."
        return actions["amend_approved"]
    if phase == "done":
        return actions["done_complete" if done and done.get("is_complete") else "done_pending"]
    if phase == "blocked" and parse_problem:
        return f"Repair Plan.md before continuing: {parse_problem}"
    return actions.get(phase, actions["fallback"])

def canonical_state_payload(
    project: Path,
    *,
    objective: str = "",
    mode: str = "cruise",
    fast_mvp: bool = False,
    global_learnings_opt_in: bool = False,
) -> dict[str, Any]:
    manifest = ensure_project_manifest(project, objective=objective)
    profile_lock = fast_mvp_profile_lock_state(project)
    blueprint_state = blueprint_lock_state(project)
    lifecycle_contract = blueprint_lifecycle_contract(project)
    modern_lifecycle = not lifecycle_contract.get("legacy", True)
    current_source_hash, hash_problem = try_source_hash(project)
    source_hash_blocked = hash_problem is not None
    plan_path = project / PLAN_FILE
    tasks = parse_tasks(plan_path) if plan_path.exists() else []
    adaptive_policy = required_review_policy(project, source_hash_value=current_source_hash, bind_source_hash=not source_hash_blocked)
    parse_problem = plan_parse_problem(plan_path, tasks)
    proof = load_proof(project)
    drift = source_hash_unavailable_state(profile_lock, problems=[hash_problem] if hash_problem else None) if source_hash_blocked else detect_drift(project, proof)
    setup_missing = (not is_git_repo(project) or not (project / BLUEPRINT_FILE).exists() or not (project / PLAN_FILE).exists() or not (project / LEDGER_FILE).exists())
    scope = scope_hash(project) or "noscope"
    drift = annotate_drift_coverage(project, tasks, drift)
    active_change = change_packet_for_drift(project, drift, proof)
    effective_tasks = tasks
    effective_plan_path = PLAN_FILE
    if active_change is not None:
        try:
            effective_tasks = project_changes.change_plan_tasks(project, active_change["change_id"])
            effective_plan_path = str(Path(active_change["path"]) / active_change["plan_path"])
        except project_changes.ChangePacketError:
            effective_tasks = []
    change_approved = bool(active_change is None or active_change.get("approval_state") == "approved")
    effective_ready = (ready_tasks(effective_tasks) if change_approved else [])
    review_blockers = ([] if source_hash_blocked else review_findings_for_done(project, effective_tasks))
    gates = {
        kind: lifecycle_gate_state(
            project, kind=kind, required=modern_lifecycle, current_source_hash=current_source_hash,
            **({"expected_delivery_target": str((lifecycle_contract.get("delivery") or {}).get("target") or "")} if kind == "delivery" else {}),
        )
        for kind in ORCHESTRATION["lifecycle_gates"]
    }
    foundation_gate, delivery_gate = gates["foundation"], gates["delivery"]
    legacy_amend_requires_lock = bool(blueprint_state.get("status") == "legacy-approved" and drift.get("actionable") and proof)
    plan_complete = bool(blueprint_state.get("approved") and tasks and not plan_is_placeholder(tasks) and not legacy_amend_requires_lock)
    build_complete = all_tasks_complete(effective_tasks)
    review_complete = build_complete and not review_blockers
    done = done_payload(project) if review_complete else None
    phase = project_lifecycle.resolve_phase(
        legacy=not modern_lifecycle, setup_complete=not setup_missing,
        blocked=bool(source_hash_blocked or profile_lock.get("status") == "blocked" or (parse_problem and (not modern_lifecycle or plan_complete))),
        intake_complete=bool(blueprint_state.get("approved") or (lifecycle_contract.get("intake") or {}).get("complete")),
        design_required=(lifecycle_contract.get("design") or {}).get("required"),
        design_complete=bool(blueprint_state.get("approved") or (lifecycle_contract.get("design") or {}).get("complete")),
        plan_complete=plan_complete, foundation_complete=bool(foundation_gate.get("satisfied")),
        amendment_required=bool(drift.get("actionable") and proof), build_complete=build_complete,
        review_complete=review_complete, delivery_complete=bool(delivery_gate.get("satisfied")),
        completion_complete=bool(done and done.get("is_complete")),
    )
    next_action = lifecycle_next_action(phase, locals())
    global_learning_report = learnings_report(project, explicit_opt_in=global_learnings_opt_in)
    state = project_record(
        "state", locals(), created_at=now_utc(), project=str(project), project_manifest=manifest,
        enforcement=enforcement_mode(project), hook_trust_notice=hook_trust_notice(project), versions=version_info(project),
        lifecycle=lifecycle_contract, scope_hash=scope,
        blueprint={**blueprint_state, "sha256": blueprint_state.get("current_sha256")},
        plan={"path": effective_plan_path, "historical_root_path": PLAN_FILE, "task_count": len(effective_tasks), "counts": task_counts(effective_tasks),
              "ready": [task["id"] for task in effective_ready], "parse_problem": parse_problem},
        change_packet=active_change, foundation=foundation_gate, delivery=delivery_gate,
        review_policy=adaptive_policy.to_dict(),
        review=(review_summary_source_hash_unavailable(project, scope, profile_lock, problems=[hash_problem] if hash_problem else None)
                if source_hash_blocked else review_summary(project, scope)),
        spawn_plan=spawn_plan(project, effective_tasks, phase) if change_approved else [], source_hash_unavailable=source_hash_blocked,
        source_hash_problems=[hash_problem] if hash_problem else [], global_learnings={key: value for key, value in global_learning_report.items() if key != "items"},
        learnings_digest=global_learning_report["items"], required_next_action=next_action,
    )
    state["operating_card"] = operating_card(project, state)
    return state

def review_summary_source_hash_unavailable(project: Path, scope: str, profile_lock: dict[str, Any], *, problems: Sequence[dict[str, Any]] | None = None) -> dict[str, Any]:
    merged = load_merged_review(project, scope)
    problem_list = list(problems) if problems is not None else profile_lock.get("problems") or []
    if not merged:
        return policy_mapping("review_unavailable_empty", problems=problem_list)
    return policy_mapping("review_unavailable_recorded", problems=problem_list, reviewer_count=merged.get("reviewer_count", 0),
                          open_findings=len(merged.get("fix_queue") or []), waived=len(merged.get("waived") or []))

def review_summary(project: Path, scope: str) -> dict[str, Any]:
    merged = load_merged_review(project, scope)
    if not merged:
        return policy_mapping("review_empty")
    current, hash_problem = try_source_hash(project)
    if hash_problem or current is None:
        return review_summary_source_hash_unavailable(project, scope, fast_mvp_profile_lock_state(project), problems=[hash_problem] if hash_problem else None)
    return policy_mapping("review_recorded", fresh=merged.get("source_hash") == current, reviewer_count=merged.get("reviewer_count", 0),
                          open_findings=len(merged.get("fix_queue") or []), waived=len(merged.get("waived") or []))

def scaffold_amend(project: Path, drift: dict[str, Any]) -> str | None:
    """On post-done drift, add an AMEND task so work flows back through the loop."""
    plan_path = project / PLAN_FILE
    if not plan_path.exists():
        return None
    try:
        tasks = parse_tasks(plan_path)
    except ForgeError:
        return None
    open_amend = [task for task in tasks if task["id"].startswith("AMEND-") and task["status"] != "complete"]
    if open_amend:
        return open_amend[0]["id"]
    existing = [task["id"] for task in tasks if task["id"].startswith("AMEND-")]
    n = len(existing) + 1
    task_id = f"AMEND-{n}"
    changed = [git_status_path(item) if item[:1] in {" ", "M", "A", "D", "R", "?"} else item for item in (drift.get("changed_files") or [])]
    files = ", ".join(dict.fromkeys(item for item in changed if item)) or "-"
    # Inherit a real Verify command from an existing non-docs task so the amendment
    # is completable: verify is now bound to this cell, so a prose placeholder would
    # be uncompleteable. The coordinator may edit it to a more specific command.
    inherited = next(
        (task_verify_command(t) for t in tasks if not task_allows_noop_verification(t) and task_verify_command(t) and not command_is_noop(task_verify_command(t))),
        "",
    )
    amend_task = {"task": task_id, **ORCHESTRATION["amend_task"]}
    amend_task.update(files=files, verify=inherited or amend_task["verify"])
    appended = append_plan_task(plan_path, amend_task)
    return task_id if appended else None

def saved_phase(path: Path) -> str | None:
    try:
        return str(read_json(path).get("phase") or "") or None
    except Exception:
        return None

def cmd_run(args: argparse.Namespace) -> int:
    raw_project = Path(args.project).resolve()
    project, blocked = resolve_isolation(raw_project, product_slug=args.product_slug or "", adopt_root=args.adopt_root)
    if blocked is not None:
        print(json.dumps(blocked, indent=2))
        return 1 if args.strict else 0
    assert project is not None
    project.mkdir(parents=True, exist_ok=True)
    requested_profile = "fast-mvp" if args.fast_mvp else (args.profile or "")
    setup_missing = (not is_git_repo(project) or not (project / BLUEPRINT_FILE).exists() or not (project / PLAN_FILE).exists() or not (project / LEDGER_FILE).exists())
    if setup_missing and not args.no_auto_init:
        init_values = {**vars(args), "project": str(project), "force": False, "adopt_root": True, "profile": requested_profile}
        init_values.update(no_agents=bool(getattr(args, "no_agents", False)), no_hooks=bool(getattr(args, "no_hooks", False)))
        init_args = argparse.Namespace(**init_values)
        code = cmd_init(init_args)
        if code != 0:
            return code
    fast_mvp_already_proven = requested_profile == "fast-mvp" and (
        fast_mvp_profile_selected_before_gates(project) or (project_profile(project) == "fast-mvp" and setup_ledger_records_fast_mvp_before_gates(project)))
    profile_for_manifest = "" if fast_mvp_already_proven else requested_profile
    ensure_project_manifest(project, objective=args.objective or "", product_slug=args.product_slug or "", profile=profile_for_manifest)
    plan_path = project / PLAN_FILE
    if blueprint_is_approved(project) and plan_path.exists():
        try:
            for task in ready_tasks(parse_tasks(plan_path)):
                if task.get("status") == "queued":
                    update_plan_task_row(plan_path, task["id"], {"status": "ready"})
        except ForgeError:
            pass
    # Post-done drift: isolate new work in a scope-derived v0.4 change packet.
    proof = load_proof(project)
    profile_lock_for_run = fast_mvp_profile_lock_state(project)
    _current_source_hash, hash_problem = try_source_hash(project)
    source_hash_blocked = hash_problem is not None
    drift = source_hash_unavailable_state(profile_lock_for_run, problems=[hash_problem] if hash_problem else None) if source_hash_blocked else detect_drift(project, proof)
    drift_tasks: list[dict[str, Any]] = []
    if plan_path.exists():
        try:
            drift_tasks = parse_tasks(plan_path)
        except ForgeError:
            drift_tasks = []
    if (not source_hash_blocked and drift.get("detected") and proof and plan_path.exists() and change_scope_files(drift) and blueprint_has_valid_lock(project) and
            not completed_amendment_covering_drift(project, drift_tasks, drift) and not completed_change_packet_covering_drift(project, drift, proof)):
        existing_packet = change_packet_for_drift(project, drift, proof)
        try:
            packet = project_changes.create_or_select_change_packet(
                project, original_completed_source_hash=str(proof.get("source_hash") or ""),
                changed_files=change_scope_files(drift), profile=review_profile(project),
            )
        except project_changes.ChangePacketError as exc:
            raise ForgeError(f"could not derive post-completion change packet: {exc}") from exc
        if (existing_packet is None or existing_packet.get("change_id") != packet.get("change_id")):
            append_jsonl(
                project / INCIDENTS_FILE,
                policy_record("incident", timestamp=now_utc(), kind="post-done-drift", change_packet=packet["change_id"], changed_files=drift.get("changed_files")),
            )
    payload = canonical_state_payload(project, objective=args.objective or "", mode=args.mode, fast_mvp=review_profile(project) == "fast-mvp",
                                      global_learnings_opt_in=bool(getattr(args, "global_learnings", False)))
    if getattr(args, "no_hooks", False):
        payload["hook_trust_notice"] = {**(payload.get("hook_trust_notice") or {}), "show": False}
        payload["operating_card"] = operating_card(project, payload)
    ensure_state_dirs(project)
    state_path = project / CANONICAL_STATE
    previous_phase = saved_phase(state_path) if state_path.exists() else None
    write_json_stable(state_path, payload)
    if payload["phase"] != previous_phase:
        append_jsonl(
            project / LEDGER_FILE,
            policy_record("ledger_state", timestamp=now_utc(), event="state-machine", summary=f"phase={payload['phase']}", artifacts=[str(CANONICAL_STATE)]),
        )
    print(payload["operating_card"])
    print(json.dumps(payload, indent=2))
    if (payload.get("hook_trust_notice") or {}).get("show"):
        mark_hook_trust_notice_seen(project)
    profile_lock_status = str((payload.get("profile_lock") or {}).get("status") or "")
    strict_blocked = payload["phase"] == "blocked" or profile_lock_status in ORCHESTRATION["strict_profile_statuses"] or bool(payload.get("source_hash_unavailable"))
    return 1 if args.strict and strict_blocked else 0

def cmd_status(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    plan_path = project / PLAN_FILE
    tasks = parse_tasks(plan_path) if plan_path.exists() else []
    profile_lock = fast_mvp_profile_lock_state(project)
    blueprint_state = blueprint_lock_state(project)
    hash_problem = source_hash_unavailable_problem(project)
    scope = scope_hash(project) or "noscope"
    review = review_summary_source_hash_unavailable(project, scope, profile_lock, problems=[hash_problem] if hash_problem else None) if hash_problem else review_summary(project, scope)
    _print_record(
        "status",
        project=str(project), versions=version_info(project),
        enforcement=enforcement_mode(project), hooks_live=hooks_liveness(project),
        blueprint_approved=blueprint_is_approved(project), blueprint=blueprint_state,
        plan_exists=plan_path.exists(), task_count=len(tasks), counts=task_counts(tasks),
        ready=[task["id"] for task in ready_tasks(tasks)], review=review,
        profile_lock=profile_lock, source_hash_unavailable=bool(hash_problem), git_status=git_status(project),
        canonical_state=str(project / CANONICAL_STATE) if (project / CANONICAL_STATE).exists() else None,
    )
    return 0

def cmd_approve_blueprint(args: argparse.Namespace) -> int:
    """Lock the current Blueprint after the coordinator obtains user approval."""
    project = resolve_project(args.project)
    before = blueprint_lock_state(project)
    try:
        lock = project_contracts.write_blueprint_lock(project)
    except project_contracts.ContractError as exc:
        raise ForgeError(str(exc)) from exc
    state = blueprint_lock_state(project)
    _print_record(
        "blueprint_approval", lock,
        project=str(project), blueprint=BLUEPRINT_FILE, lock=project_contracts.BLUEPRINT_LOCK_FILE,
        previous_status=before.get("status"), status=state.get("status"),
    )
    return 0

def cmd_approve_change(args: argparse.Namespace) -> int:
    """Approve and activate one already-derived change packet."""
    project = resolve_project(args.project)
    try:
        packet = project_changes.approve_change_packet(project, args.change)
        tasks = project_changes.activate_change_plan(project, args.change)
    except project_changes.ChangePacketError as exc:
        raise ForgeError(str(exc)) from exc
    _print_record(
        "change_approval", packet,
        project=str(project), plan=str(Path(packet["path"]) / packet["plan_path"]),
        ready=[task["id"] for task in ready_tasks(tasks)]
    )
    return 0

def cmd_doctor(args: argparse.Namespace) -> int:
    """Inspect Codex installation state without mutating it."""
    codex_home = Path(args.codex_home or os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    source_root = Path(args.source_root) if args.source_root else plugin_root()
    active_root = Path(args.active_plugin_root) if args.active_plugin_root else None
    payload = installation_doctor.diagnose_installation(codex_home=codex_home, source_root=source_root, runtime_version=SF_VERSION, active_plugin_root=active_root)
    print(json.dumps(payload, indent=2))
    return installation_doctor.doctor_exit_code(payload, strict=args.strict)

def cmd_quality(args: argparse.Namespace) -> int:
    """Classify project source and report explainable architecture debt."""
    project = resolve_project(args.project)
    payload = project_quality.quality_report(project, include_files=args.include_files)
    blocking = blocking_items(payload["findings"])
    payload["verdict"] = ORCHESTRATION["verdicts"]["pass" if not blocking else "changes"]
    payload["blocking_findings"] = len(blocking)
    print(json.dumps(payload, indent=2))
    return 1 if args.strict and blocking else 0

def cmd_validate_plan(args: argparse.Namespace) -> int:
    raw = Path(args.file)
    project = resolve_project(args.project)
    project_path = project / raw
    plan_path = raw.resolve() if raw.is_absolute() else project_path if project_path.exists() or str(args.file) == str(PLAN_FILE) else raw.resolve() if raw.exists() else project_path
    tasks = parse_tasks(plan_path)
    problems = validate_tasks(tasks)
    problems.extend(validate_project_plan_contract(project, tasks))
    if not tasks:
        problems.append({"severity": "critical", "task": None, "line": 0, "message": plan_parse_problem(plan_path, tasks) or "Plan.md contains no parseable tasks"})
    blocking = [item for item in problems if item["severity"] in BLOCKING_SEVERITIES]
    mode = plan_contract_mode(tasks)
    traceability = ORCHESTRATION["traceability_by_plan_mode"].get(mode, ORCHESTRATION["traceability_by_plan_mode"]["unknown"])
    payload = policy_record(
        "plan_validate",
        verdict=ORCHESTRATION["verdicts"]["pass" if not blocking else "changes"],
        plan_version=mode,
        traceability=traceability,
        task_count=len(tasks),
        ready=[task["id"] for task in ready_tasks(tasks)],
        problems=problems,
    )
    print(json.dumps(payload, indent=2))
    return 0 if payload["verdict"] == "PASS" or not args.strict else 1

def cmd_migrate_plan(args: argparse.Namespace) -> int:
    """Create a separate Plan v2 draft from an eight-column legacy Plan."""
    project = resolve_project(args.project)
    source_raw = Path(args.file)
    output_raw = Path(args.output)
    source = source_raw.resolve() if source_raw.is_absolute() else (project / source_raw).resolve()
    output = output_raw.resolve() if output_raw.is_absolute() else (project / output_raw).resolve()
    try:
        payload = project_contracts.write_plan_v2_migration(source, output)
    except project_contracts.ContractError as exc:
        raise ForgeError(str(exc)) from exc
    print(json.dumps(payload | {"project": str(project)}, indent=2))
    return 0

__all__ = tuple(name for name in globals() if not name.startswith("__"))
