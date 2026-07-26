"""Cohesive Star Forge runtime extracted from the CLI facade."""

from __future__ import annotations
from .policy_data import mapping as policy_mapping, project as project_record, record as policy_record, value as _policy_value
import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence
from starforge import changes as project_changes
from starforge import contracts as project_contracts
from .runtime_security import *

REVIEW_POLICY = _policy_value("runtime_review.POLICY")

def reviews_scope_dir(project: Path, scope: str) -> Path:
    return project / REVIEWS_DIR / slugify(scope or "noscope")

def review_file_problem(file: str, kind: str, *, rule: str = "review-findings-shape", **values: Any) -> dict[str, Any]:
    message = REVIEW_POLICY["review_file_errors"][kind].format(**values)
    return policy_mapping("review_problem", rule=rule, file=file, message=message)

def normalize_review_finding(item: Mapping[str, Any], role: str, agent_id: Any) -> dict[str, Any]:
    severity = str(item.get("severity") or "medium").lower()
    return policy_mapping(
        "normalized_review_finding", id=str(item.get("id") or ""), role=role, agent_id=agent_id,
        severity=severity if severity in FINDING_SEVERITIES else "medium", file=str(item.get("file") or ""), line=item.get("line"),
        title=str(item.get("title") or item.get("summary") or "")[:200], detail=str(item.get("detail") or item.get("evidence") or "")[:600],
        suggested_fix=str(item.get("suggested_fix") or "")[:400],
    )

def review_file_header(path: Path, rel: str, payload: Any) -> tuple[tuple[str, str, list[Any]] | None, dict[str, Any] | None]:
    if not isinstance(payload, dict):
        return None, review_file_problem(rel, "object")
    raw_role = payload.get("role")
    if not isinstance(raw_role, str) or not raw_role.strip():
        return None, review_file_problem(rel, "role")
    role = raw_role.strip()
    source_attestation, findings = payload.get("source_hash"), payload.get("findings")
    expected = f"{role}.findings.json"
    checks = [
        (role not in KNOWN_REVIEW_ROLES, "unknown_role", {"role": role}),
        (path.name != expected, "filename", {"filename": path.name, "role": role, "expected": expected}),
        (not isinstance(source_attestation, str) or not source_attestation.strip(), "source_hash", {}),
        (not isinstance(findings, list), "findings", {}),
    ]
    problem = next((review_file_problem(rel, kind, **values) for failed, kind, values in checks if failed), None)
    return (None, problem) if problem else ((role, source_attestation, findings), None)

def load_review_findings(project: Path, scope: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read and normalize source-attested reviewer findings for one scope."""
    files: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    root = reviews_scope_dir(project, scope)
    if not root.exists():
        return files, problems
    for path in sorted(root.glob("*.findings.json")):
        rel = relative_to_project(path, project)
        try:
            payload = json.loads(path.read_bytes().decode("utf-8"))
        except Exception as exc:
            problems.append(review_file_problem(rel, "unreadable", rule="review-findings-invalid", error=exc))
            continue
        header, problem = review_file_header(path, rel, payload)
        if problem:
            problems.append(problem)
            continue
        assert header is not None
        role, source_attestation, raw = header
        normalized: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                problems.append(review_file_problem(rel, "finding_object"))
                continue
            normalized.append(normalize_review_finding(item, role, payload.get("agent_id")))
        files.append(policy_mapping("review_file", path=rel, role=role, agent_id=payload.get("agent_id"), declared_source_hash=source_attestation, findings=normalized))
    return files, problems

def finding_fingerprint(finding: dict[str, Any]) -> str:
    file, bucket, _tokens = finding_match_parts(finding)
    return f"{file}:{bucket}:{finding_issue_signature(finding)}"
FINDING_DUPLICATE_STOPWORDS = _policy_value('runtime_review.FINDING_DUPLICATE_STOPWORDS')
FINDING_MARKERS = _policy_value('runtime_review.FINDING_MARKERS')

def finding_text(finding: dict[str, Any]) -> str:
    return " ".join(str(finding.get(key) or "") for key in REVIEW_POLICY["finding_text_fields"]).lower()

def finding_issue_signature(finding: dict[str, Any]) -> str:
    text = finding_text(finding)
    for marker, needles in FINDING_MARKERS.items():
        if any(needle in text for needle in needles):
            return marker
    tokens = [token for token in re.findall(r"[a-z0-9_@-]{4,}", text) if token not in FINDING_DUPLICATE_STOPWORDS]
    if not tokens:
        return re.sub(r"[^a-z0-9]+", "", str(finding.get("title") or "").lower())[:40]
    return "-".join(list(dict.fromkeys(tokens))[:8])

def finding_match_parts(finding: dict[str, Any]) -> tuple[str, int, set[str]]:
    file = re.sub(r"\s+", "", str(finding.get("file") or "")).lower()
    line = finding.get("line")
    tokens = {token for token in re.findall(r"[a-z0-9_@-]{4,}", finding_text(finding)) if token not in FINDING_DUPLICATE_STOPWORDS}
    return file, int(line) // 4 if isinstance(line, int) else -1, tokens

def findings_are_duplicate_variants(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    existing_file, existing_bucket, existing_tokens = finding_match_parts(existing)
    candidate_file, candidate_bucket, candidate_tokens = finding_match_parts(candidate)
    if existing_file != candidate_file:
        return False
    if -1 not in (existing_bucket, candidate_bucket) and existing_bucket != candidate_bucket:
        return False
    if finding_issue_signature(existing) == finding_issue_signature(candidate):
        return True
    if not existing_tokens or not candidate_tokens:
        return False
    overlap = len(existing_tokens & candidate_tokens)
    smaller = min(len(existing_tokens), len(candidate_tokens))
    return overlap >= 2 and overlap / max(1, smaller) >= 0.5

def finding_severity_rank(severity: Any) -> int:
    return FINDING_SEVERITY_RANK.get(str(severity or "").lower(), 0)

def merge_duplicate_finding(existing: dict[str, Any], candidate: dict[str, Any]) -> None:
    agreed_by = existing.setdefault("agreed_by", [])
    for role in (existing.get("role"), candidate.get("role")):
        if role not in agreed_by:
            agreed_by.append(role)
    details = existing.setdefault("role_details", [])
    for finding in (existing, candidate):
        detail = policy_mapping("review_role_detail", **{field: finding.get(field) for field in ("role", "agent_id", "severity")})
        if detail not in details:
            details.append(detail)
    if finding_severity_rank(candidate.get("severity")) > finding_severity_rank(existing.get("severity")):
        existing["severity"] = candidate.get("severity")
        for key in REVIEW_POLICY["finding_upgrade_fields"]:
            if candidate.get(key):
                existing[key] = candidate.get(key)

def assign_finding_ids(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for finding in findings:
        existing = next((item for item in out if findings_are_duplicate_variants(item, finding)), None)
        if existing is not None:
            merge_duplicate_finding(existing, finding)
            continue
        finding = dict(finding)
        finding["id"] = finding.get("id") or f"F-{len(out) + 1}"
        finding["fingerprint"] = finding_fingerprint(finding)
        out.append(finding)
    return out

def load_waives(project: Path, scope: str) -> set[str]:
    path = project / WAIVES_FILE
    waived: set[str] = set()
    if not path.exists():
        return waived
    try:
        for line in read_text(path).splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if payload.get("scope") in {None, scope} and payload.get("finding"):
                waived.add(str(payload["finding"]))
    except Exception:
        return waived
    return waived

def tree_scan_finding(finding: Mapping[str, Any], *, architecture: bool = False) -> dict[str, Any]:
    rule, id_rule, file = finding.get("rule"), finding.get("rule", "scan"), finding.get("file", "")
    suffix = "" if architecture else f"-{finding.get('line', 0)}"
    return policy_mapping(
        "scan_finding",
        id=f"scan-{slugify(id_rule)}-{slugify(file)}{suffix}",
        severity=finding.get("severity") if architecture or rule != "secret-material" else "high",
        file=finding.get("file"), line=finding.get("line"),
        title=rule if architecture else f"{rule} in tree",
        detail=finding.get("evidence", ""),
        suggested_fix="" if architecture else REVIEW_POLICY["scan_remediation"],
    )

def secret_scan_findings(project: Path) -> list[dict[str, Any]]:
    paths = list(iter_project_files(project, all_files=True))
    return [
        tree_scan_finding(finding, architecture=architecture)
        for architecture, findings in ((False, scan_paths(paths, project)), (True, architecture_debt_findings(paths, project))) for finding in findings
        if finding.get("severity", "medium") in BLOCKING_SEVERITIES
    ]

def jsonl_payloads(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return [payload for line in read_text(path).splitlines() if line.strip() for payload in [json.loads(line)] if isinstance(payload, dict)]
    except Exception:
        return []

def subagent_ids_from(path: Path) -> set[str]:
    return {str(payload["agent_id"]) for payload in jsonl_payloads(path) if payload.get("event") == "SubagentStart" and payload.get("agent_id")}

def local_subagent_ids(project: Path) -> set[str]:
    return subagent_ids_from(project / SUBAGENT_EVENTS)

def known_subagent_ids(project: Path) -> set[str]:
    """Trusted subagent ids that could qualify review or done as witnessed.
    This version has no supported host-controlled witness source. Project-local
    subagent events remain useful diagnostics, but they cannot witness reviewer
    files or completion.
    """
    return set()

def review_payload_source_hash_unavailable(project: Path, scope: str, problem: dict[str, Any]) -> dict[str, Any]:
    profile_lock = fast_mvp_profile_lock_state(project)
    required_roles = list(REVIEW_PROFILE_ROLES["standard"])
    return project_record(
        "review_unavailable", locals(), created_at=now_utc(), project=str(project),
        source_hash=None, source_hash_unavailable=True, problems=[problem],
        manifest_profile=project_profile(project), source_profile=read_source_profile(project) or None,
        review_profile="standard", reviewer_roles=[], reviewer_count=0,
        required_review_roles=required_roles, required_reviewer_count=len(required_roles),
        missing_review_roles=required_roles, stale_roles=[], reviewers_witnessed=False, findings=[],
        fix_queue=[problem], waived=sorted(load_waives(project, scope)), file_problems=[problem],
    )

def merge_review(project: Path, scope: str) -> dict[str, Any]:
    files, file_problems = load_review_findings(project, scope)
    current, hash_problem = try_source_hash(project)
    if hash_problem or current is None:
        return review_payload_source_hash_unavailable(project, scope, hash_problem or source_hash_exception_problem(ForgeError("source_hash unavailable")))
    known_ids = known_subagent_ids(project)
    fresh_entries = [entry for entry in files if entry.get("declared_source_hash") == current]
    fresh_findings = [finding for entry in fresh_entries for finding in entry["findings"]]
    fresh_roles = [entry["role"] for entry in fresh_entries]
    stale_roles = [entry["role"] for entry in files if entry.get("declared_source_hash") != current]
    reviewers_witnessed = bool(known_ids) and all(str(entry.get("agent_id") or "") in known_ids for entry in fresh_entries)
    merged = assign_finding_ids([*fresh_findings, *secret_scan_findings(project)])
    waived = load_waives(project, scope)
    for finding in merged:
        finding["waived"] = finding["id"] in waived
    open_blocking = [finding for finding in merged if finding["severity"] in BLOCKING_SEVERITIES and not finding["waived"]]
    reviewer_roles = sorted(set(fresh_roles))
    policy = required_review_policy(project, source_hash_value=current)
    required_roles = list(policy.roles)
    missing_roles = [role for role in required_roles if role not in reviewer_roles]
    manifest_profile = project_profile(project)
    effective_profile = review_profile(project)
    return project_record(
        "review", locals(), created_at=now_utc(), project=str(project), source_hash=current,
        manifest_profile=manifest_profile, source_profile=read_source_profile(project) or None,
        review_profile=effective_profile, profile_lock=fast_mvp_profile_lock_state(project),
        reviewer_count=len(reviewer_roles), required_review_roles=required_roles, required_reviewer_count=len(required_roles),
        review_policy=policy.to_dict(), missing_review_roles=missing_roles, stale_roles=sorted(set(stale_roles)),
        findings=merged, fix_queue=open_blocking, waived=sorted(waived),
    )

def write_merged_review(project: Path, payload: dict[str, Any]) -> Path:
    path = reviews_scope_dir(project, payload.get("scope") or "noscope") / "merged.json"
    write_json(path, payload)
    return path

def load_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return read_json(path)
    except Exception:
        return None

def load_merged_review(project: Path, scope: str) -> dict[str, Any] | None:
    return load_optional_json(reviews_scope_dir(project, scope) / "merged.json")

def done_gate_finding(rule: str, *, file: Any = None, **values: Any) -> list[dict[str, Any]]:
    message = REVIEW_POLICY["review_gate_messages"][rule].format(**values)
    return [policy_mapping("done_gate_finding", rule=rule, file=str(REVIEWS_DIR) if file is None else file, message=message)]

def review_findings_for_done(project: Path, tasks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Done-time review gate rebuilt from reviewer files and the current tree."""
    if not all_tasks_complete(tasks):
        return []
    hash_problem = source_hash_unavailable_problem(project)
    if hash_problem:
        return [hash_problem]
    scope = scope_hash(project) or "noscope"
    if load_merged_review(project, scope) is None:
        return done_gate_finding("review-not-performed")
    merged = merge_review(project, scope)
    problems = merged.get("file_problems") or []
    queue = [item for item in (merged.get("fix_queue") or []) if isinstance(item, dict)]
    required_roles = required_review_roles(project)
    reviewer_roles = {str(role) for role in (merged.get("reviewer_roles") or [])}
    missing_roles = [role for role in required_roles if role not in reviewer_roles]
    first_problem = next((item for item in problems if isinstance(item, dict)), {})
    first_queue = queue[0] if queue else {}
    gates = {
        "review-findings-invalid": (bool(problems), {"file": first_problem.get("file", str(REVIEWS_DIR)), "detail": first_problem.get("message")}),
        "review-stale": (not reviewer_roles and bool(merged.get("stale_roles")), {}),
        "review-empty": (not reviewer_roles and not merged.get("stale_roles"), {}),
        "reviewer-count-insufficient": (bool(missing_roles), {"profile": review_profile(project), "required_count": len(required_roles), "required_roles": ", ".join(required_roles), "missing_roles": ", ".join(missing_roles)}),
        "review-fix-queue-open": (bool(queue), {"queue_count": len(queue), "finding_id": first_queue.get("id"), "title": first_queue.get("title"), "finding_file": first_queue.get("file")}),
    }
    return next((done_gate_finding(rule, **gates[rule][1]) for rule in REVIEW_POLICY["review_gate_order"] if rule in gates and gates[rule][0]), [])

def append_policy_event(project: Path, path: str, record: str, **values: Any) -> None:
    append_jsonl(project / path, policy_record(record, timestamp=now_utc(), **values))

def cmd_review(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    scope = scope_hash(project) or "noscope"
    payload = merge_review(project, scope)
    write_merged_review(project, payload)
    append_policy_event(project, LEDGER_FILE, "ledger_review", event="review", summary=f"reviewers={payload['reviewer_count']} open={len(payload['fix_queue'])}", artifacts=[relative_to_project(reviews_scope_dir(project, scope) / "merged.json", project)])
    print(json.dumps(payload, indent=2))
    blocked = bool(payload.get("fix_queue") or not payload.get("reviewer_roles") or payload.get("missing_review_roles") or payload.get("file_problems"))
    return 0 if not blocked or not args.strict else 1

def cmd_waive(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    if not str(args.reason or "").strip():
        raise ForgeError(REVIEW_POLICY["waive_reason_error"])
    scope = scope_hash(project) or "noscope"
    append_policy_event(project, WAIVES_FILE, "waive", scope=scope, finding=args.finding, reason=args.reason)
    append_policy_event(project, INCIDENTS_FILE, "incident_waive", kind="waive", finding=args.finding, reason=args.reason)
    # Re-merge so the fix queue reflects the waive immediately.
    payload = merge_review(project, scope)
    write_merged_review(project, payload)
    print(json.dumps(policy_record("waive_output", finding=args.finding, reason=args.reason, open_findings=len(payload["fix_queue"])), indent=2))
    return 0

def task_completion_finding(rule: str, **values: Any) -> dict[str, Any]:
    severity = "critical" if rule == "task-missing" else "high"
    return policy_mapping("task_completion_finding", severity=severity, rule=rule, message=REVIEW_POLICY["task_completion_messages"][rule].format(**values))

def evaluate_task_completion(project: Path, args: argparse.Namespace, task: Mapping[str, Any], tasks: Sequence[dict[str, Any]], source_problem: Any) -> list[dict[str, Any]]:
    complete_ids = {item["id"] for item in tasks if item.get("status") == "complete"}
    unmet = [dep for dep in parse_depends(task.get("depends", "")) if dep not in complete_ids]
    allows_noop = task_allows_noop_verification(task)
    conditions = {
        "task-status-not-completable": (task.get("status") not in REVIEW_POLICY["completable_statuses"], {"task": args.task, "status": task.get("status")}),
        "task-dependencies-incomplete": (bool(unmet), {"task": args.task, "dependencies": ", ".join(unmet)}),
        "verify-noop-missing": (not source_problem and allows_noop and not has_noop_verify(project, args.task), {"task": args.task}),
        "verify-stale": (not source_problem and not allows_noop and not fresh_passing_verify(project, task), {"task": args.task}),
        "browser-run-missing": (task_is_visual(task) and not passing_browser_runs(project, args.task), {"task": args.task}),
        "changed-file-missing": (not any(str(item).strip() for item in (args.changed_file or [])), {}),
    }
    return [task_completion_finding(rule, **conditions[rule][1]) for rule in REVIEW_POLICY["task_completion_order"] if conditions[rule][0]]

def cmd_complete_task(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    plan_path, tasks = task_plan(project, args.task)
    task = next((item for item in tasks if item.get("id") == args.task), None)
    findings: list[dict[str, Any]] = []
    findings.extend(validate_project_plan_contract(project, tasks) if plan_path == project / PLAN_FILE else validate_tasks(tasks))
    hash_problem = source_hash_unavailable_problem(project)
    _current_source_hash, snapshot_problem = try_source_hash(project)
    source_problem = hash_problem or snapshot_problem
    if source_problem:
        findings.append(source_problem)
    if task is None:
        findings.append(task_completion_finding("task-missing", task=args.task))
    else:
        findings.extend(evaluate_task_completion(project, args, task, tasks, source_problem))
    if blocking_items(findings):
        print(json.dumps(policy_record("complete_task_refused", task=args.task, verdict="REFUSED", findings=findings, updated=False), indent=2))
        return 1
    evidence = ", ".join(args.changed_file or [])
    summary = args.summary or f"Task {args.task} completed with verified evidence."
    update_plan_task_row(plan_path, args.task, {"Status": "complete", "Evidence": evidence or summary})
    completion_artifact = project / STATE_SUBDIR / f"complete-task-{slugify(args.task)}.json"
    snapshot, _snapshot_problem = safe_release_snapshot(project)
    payload = policy_record("complete_task", created_at=now_utc(), task=args.task, verdict="COMPLETE", changed_files=args.changed_file or [], summary=summary, source_snapshot=snapshot)
    write_json(completion_artifact, payload)
    append_policy_event(project, LEDGER_FILE, "ledger_task_complete", event="task-complete", task=args.task, summary=summary, artifacts=args.changed_file or [])
    print(json.dumps(payload | {"updated": True, "plan": relative_to_project(plan_path, project)}, indent=2))
    return 0

def done_lifecycle_gates(project: Path, contract: Mapping[str, Any], source_hash_value: str | None) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    modern = not contract.get("legacy", True)
    gates = {
        kind: lifecycle_gate_state(
            project, kind=kind, required=modern, current_source_hash=source_hash_value,
            **({"expected_delivery_target": str((contract.get("delivery") or {}).get("target") or "")} if kind == "delivery" else {}),
        )
        for kind in REVIEW_POLICY["lifecycle_gates"]
    }
    problems = [
        {"severity": "high", "rule": f"{name}-gate", "message": REVIEW_POLICY["done_messages"]["gate"].format(
            name=name.title(), detail=str((gate.get("blockers") or [f"{name} proof did not pass"])[0])
        )}
        for name, gate in gates.items() if modern and not gate.get("satisfied")
    ]
    return gates["foundation"], gates["delivery"], problems

def done_witness(project: Path, tasks: Sequence[dict[str, Any]], merged: Mapping[str, Any] | None, enforcement: str) -> tuple[dict[str, Any], list[str], int]:
    hooks = hooks_liveness(project)
    trusted_subagent_observed = bool(known_subagent_ids(project))
    delegated_complete = any(task.get("status") == "complete" and task_requires_real_workers(task) for task in tasks)
    review_performed, review_witnessed = bool(merged and merged.get("reviewer_roles")), bool(merged and merged.get("reviewers_witnessed"))
    waive_count = len(merged.get("waived") or []) if merged else 0
    witness = policy_mapping(
        "done_witness", hooks_live=enforcement == "witnessed", local_hooks_observed=bool(hooks.get("local_events_observed")),
        trusted_hooks_observed=bool(hooks.get("events_observed")), subagent_observed=trusted_subagent_observed,
        local_subagent_observed=bool(local_subagent_ids(project)), trusted_subagent_observed=trusted_subagent_observed,
        delegated_complete=delegated_complete, review_performed=review_performed, review_witnessed=review_witnessed, waived_findings=waive_count,
    )
    conditions = {"hooks": enforcement != "witnessed", "delegation": delegated_complete and not trusted_subagent_observed, "review": review_performed and not review_witnessed}
    reasons = [text for key, text in REVIEW_POLICY["advisory_reasons"].items() if conditions[key]]
    return witness, reasons, waive_count

def done_verdict(blocking: Sequence[Any], advisory_reasons: Sequence[str], waive_count: int) -> str:
    verdicts = REVIEW_POLICY["done_verdicts"]
    verdict = verdicts["blocked"] if blocking else f"{verdicts['complete']} (advisory: {'; '.join(advisory_reasons)})" if advisory_reasons else verdicts["complete"]
    return verdict + (f" [{waive_count} waived finding(s)]" if verdict.startswith(verdicts["complete"]) and waive_count else "")

def done_payload(project: Path) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    profile_lock = fast_mvp_profile_lock_state(project)
    hash_problem = source_hash_unavailable_problem(project)
    lifecycle_contract = blueprint_lifecycle_contract(project)
    if not blueprint_is_approved(project):
        problems.append({"severity": "critical", "message": REVIEW_POLICY["done_messages"]["blueprint"]})
    plan_path = project / PLAN_FILE
    if not plan_path.exists():
        problems.append({"severity": "critical", "message": REVIEW_POLICY["done_messages"]["plan_missing"]})
    else:
        try:
            tasks = parse_tasks(plan_path)
            problems.extend(validate_tasks(tasks))
            problems.extend(validate_project_plan_contract(project, tasks))
            parse_problem = plan_parse_problem(plan_path, tasks)
            if parse_problem:
                problems.append({"severity": "critical", "message": parse_problem})
            elif not tasks or plan_is_placeholder(tasks):
                problems.append({"severity": "critical", "message": REVIEW_POLICY["done_messages"]["plan_empty"]})
            elif not all_tasks_complete(tasks):
                problems.append({"severity": "high", "message": REVIEW_POLICY["done_messages"]["tasks_incomplete"]})
        except ForgeError as exc:
            problems.append({"severity": "critical", "message": str(exc)})
    if hash_problem:
        problems.append(finding_problem(hash_problem))
    else:
        gate_findings = [finding for finder in (verify_findings, browser_findings) for finding in finder(project, tasks)]
        gate_findings.extend(review_findings_for_done(project, tasks))
        problems.extend(finding_problem(finding) for finding in gate_findings if finding["severity"] in BLOCKING_SEVERITIES)
    dirty = source_dirty_entries(git_status(project))
    if dirty:
        problems.append({"severity": "medium", "message": REVIEW_POLICY["done_messages"]["dirty"], "files": dirty[:30]})
    # Drift vs a prior proof is informational here: the verify/review freshness
    # gates above already force a real re-pass after any source change, so a
    # passing predicate legitimately supersedes the old proof. Change-packet
    # re-entry is `run`'s job.
    proof = load_proof(project)
    if hash_problem:
        drift = source_hash_unavailable_state(profile_lock, problems=[hash_problem])
    else:
        try:
            drift = detect_drift(project, proof)
        except (PermissionError, OSError) as exc:
            hash_problem = source_hash_exception_problem(exc)
            problems.append(finding_problem(hash_problem))
            drift = source_hash_unavailable_state(profile_lock, problems=[hash_problem])
    snapshot, snapshot_problem = safe_release_snapshot(project)
    if snapshot_problem and not hash_problem:
        problems.append(finding_problem(snapshot_problem))
    current_source_hash, lifecycle_hash_problem = try_source_hash(project)
    foundation_gate, delivery_gate, lifecycle_problems = done_lifecycle_gates(project, lifecycle_contract, current_source_hash)
    problems.extend(lifecycle_problems)
    if lifecycle_hash_problem and not hash_problem:
        problems.append(finding_problem(lifecycle_hash_problem))
    blocking = blocking_items(problems)
    enforcement = enforcement_mode(project)
    # Project-local JSONL ledgers are useful diagnostics, but they are advisory
    # because the same actors being evaluated can write them.
    scope = scope_hash(project) or "noscope"
    merged = None if hash_problem else (merge_review(project, scope) if load_merged_review(project, scope) is not None else None)
    witness, advisory_reasons, waive_count = done_witness(project, tasks, merged, enforcement)
    verdict = done_verdict(blocking, advisory_reasons, waive_count)
    return project_record(
        "done", locals(), created_at=now_utc(), project=str(project),
        is_complete=verdict.startswith("COMPLETE"), task_count=len(tasks), counts=task_counts(tasks),
        foundation=foundation_gate, delivery=delivery_gate,
        source_hash_unavailable=bool(hash_problem or snapshot_problem),
    )

def load_proof(project: Path) -> dict[str, Any] | None:
    return load_optional_json(project / PROOF_FILE)

def detect_drift(project: Path, proof: dict[str, Any] | None) -> dict[str, Any]:
    if not proof:
        return policy_mapping("drift_empty")
    current_source = source_hash(project)
    current_scope = scope_hash(project) or "noscope"
    source_changed = proof.get("source_hash") != current_source
    scope_changed = (proof.get("scope_hash") or "noscope") != current_scope
    changed: list[str] = []
    if source_changed:
        changed = source_dirty_entries(git_status(project)) or _diff_since(project, proof.get("head"))
    return policy_mapping("drift", detected=bool(source_changed or scope_changed), source_changed=source_changed, scope_changed=scope_changed, changed_files=changed)

def completed_task_matches_source(project: Path, task_id: str, current: str, *, attest_task: bool = False) -> bool:
    payload = load_optional_json(project / STATE_SUBDIR / f"complete-task-{slugify(task_id)}.json")
    snapshot = payload.get("source_snapshot") if payload else None
    return bool(
        payload and payload.get("verdict") == "COMPLETE" and isinstance(snapshot, dict)
        and snapshot.get("source_hash") == current and (not attest_task or payload.get("task") == task_id)
    )

def completed_amendment_covering_drift(project: Path, tasks: Sequence[dict[str, Any]], drift: dict[str, Any]) -> str | None:
    if not drift.get("detected"):
        return None
    if any(task.get("id", "").startswith("AMEND-") and task.get("status") != "complete" for task in tasks):
        return None
    current = source_hash(project)
    completed_amends = (task for task in tasks if str(task.get("id", "")).startswith("AMEND-") and task.get("status") == "complete")
    for task in sorted(completed_amends, key=lambda item: str(item.get("id") or ""), reverse=True):
        task_id = str(task.get("id") or "")
        if completed_task_matches_source(project, task_id, current, attest_task=True):
            return task_id
    return None

def change_scope_files(drift: Mapping[str, Any]) -> list[str]:
    return [str(path) for path in (drift.get("changed_files") or []) if not str(path).strip().strip('"').endswith(project_contracts.BLUEPRINT_LOCK_FILE)]

def change_packet_for_drift(
    project: Path,
    drift: Mapping[str, Any],
    proof: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not drift.get("detected") or not proof:
        return None
    try:
        scope = project_changes.normalize_changed_files(change_scope_files(drift))
        matches = [
            packet for packet in project_changes.list_change_packets(project)
            if packet["original_completed_source_hash"] == proof.get("source_hash") and packet["scope_delta"] == scope
        ]
    except project_changes.ChangePacketError:
        return None
    for packet in reversed(matches):
        try:
            packet_tasks = project_changes.change_plan_tasks(project, packet["change_id"])
        except project_changes.ChangePacketError:
            continue
        if not packet_tasks or not all_tasks_complete(packet_tasks):
            return packet
    return matches[-1] if matches else None

def completed_change_packet_covering_drift(
    project: Path,
    drift: Mapping[str, Any],
    proof: Mapping[str, Any] | None,
) -> str | None:
    packet = change_packet_for_drift(project, drift, proof)
    if not packet or packet["approval_state"] != "approved":
        return None
    try:
        tasks = project_changes.change_plan_tasks(project, packet["change_id"])
    except project_changes.ChangePacketError:
        return None
    if not tasks or not all_tasks_complete(tasks):
        return None
    current = source_hash(project)
    return str(packet["change_id"]) if all(completed_task_matches_source(project, task["id"], current) for task in tasks) else None

def annotate_drift_coverage(project: Path, tasks: Sequence[dict[str, Any]], drift: dict[str, Any]) -> dict[str, Any]:
    proof = load_proof(project)
    covered_by_packet = completed_change_packet_covering_drift(project, drift, proof)
    covered_by_legacy = completed_amendment_covering_drift(project, tasks, drift)
    covered_by = covered_by_packet or covered_by_legacy
    return dict(drift) | {
        "covered_by_completed_amendment": covered_by_legacy,
        "covered_by_completed_change_packet": covered_by_packet,
        "actionable": bool(drift.get("detected") and not covered_by),
    }

def _diff_since(project: Path, head: str | None) -> list[str]:
    if head and is_git_repo(project):
        code, out, _ = run_git(["diff", "--name-only", f"{head}..HEAD"], project)
        return [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
    return []

def cmd_done(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    payload = done_payload(project)
    if payload["is_complete"]:
        ensure_state_dirs(project)
        write_json(
            project / PROOF_FILE,
            policy_record("proof", created_at=now_utc(), head=git_head(project), source_hash=source_hash(project), scope_hash=scope_hash(project) or "noscope", verdict=payload["verdict"]),
        )
        if args.write_summary:
            write_text(project / FINAL_SUMMARY, done_summary_markdown(payload))
            payload["summary_artifact"] = str(FINAL_SUMMARY)
    print(json.dumps(payload, indent=2))
    return 0 if payload["is_complete"] or not args.strict else 1

def done_summary_markdown(payload: dict[str, Any]) -> str:
    return REVIEW_POLICY["done_summary"].format(**{field: payload.get(field) for field in ("created_at", "verdict", "project", "task_count", "counts", "enforcement")})

__all__ = tuple(name for name in globals() if not name.startswith("__"))
