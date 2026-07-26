"""Artifact normalization and evidence adaptation for the GitHub collector."""

from __future__ import annotations

from live_collectors.github_policy import *  # noqa: F401,F403
from live_collectors.policy_data import (
    foundation_check_detail,
    github_provider,
    github_remote_matches,
    normalize_github_foundation as normalize_foundation_provenance,
    policy_dict, policy_list,
    provider_route,
    write_github_evidence_envelope as write_evidence_envelope,
)
from live_collectors.provider_engine import render_descriptor

CHECK_RUN_TEMPLATE = policy_dict("github_adapter", "CHECK_RUN_TEMPLATE")
LOG_ENTRY_TEMPLATE = policy_dict("github_adapter", "LOG_ENTRY_TEMPLATE")
NORMALIZED_FILE_TEMPLATE = policy_dict("github_adapter", "NORMALIZED_FILE_TEMPLATE")
OPERATION_TRANSCRIPT_TEMPLATE = policy_dict("github_adapter", "OPERATION_TRANSCRIPT_TEMPLATE")
PR_TEMPLATE = policy_dict("github_adapter", "PR_TEMPLATE")
SUMMARY_TEMPLATE = policy_dict("github_adapter", "SUMMARY_TEMPLATE")
PROOF_COMMANDS = policy_list("github_adapter", "PROOF_COMMANDS")
REF_SHA_PATHS = policy_list("github_adapter", "REF_SHA_PATHS")
MERGE_BASE_PATHS = policy_list("github_adapter", "MERGE_BASE_PATHS")
descriptor = render_descriptor
_update_manifest_redaction = update_manifest_redaction_report

def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}

def _load_connector(path: Path, *, live: bool) -> RawEvidence:
    payload = _as_dict(read_json(path, {}))
    pr_payload = payload.get("pr") or payload.get("pull_request") or {}
    final_pr = payload.get("final_pr") or payload.get("freshness") or ({} if live else pr_payload)
    provenance = (
        payload.get("live_provenance") or payload.get("github_provenance")
        or payload.get("provenance") or {}
    )
    foundation = payload.get("foundation") or payload.get("foundation_provenance") or {}
    return RawEvidence(
        source="github-import-live" if live else "connector-fixture",
        pr=_as_dict(pr_payload),
        final_pr=_as_dict(final_pr),
        diff=str(payload.get("diff") or ""),
        files=payload.get("files"),
        reviews=payload.get("reviews"),
        comments=payload.get("comments"),
        check_runs=payload.get("check_runs") or payload.get("checks"),
        annotations=payload.get("annotations"),
        logs=payload.get("logs") or payload.get("ci_logs"),
        commands=parse_gh_commands(payload.get("commands")) if live else [],
        operations=list(payload.get("operations") or []),
        tool_versions=_as_dict(payload.get("tool_versions")),
        live_provenance=_as_dict(provenance) if live else {},
        foundation_provenance=_as_dict(foundation),
    )

def load_connector_fixture(path: Path) -> RawEvidence:
    return _load_connector(path, live=False)

def load_connector_input(path: Path) -> RawEvidence:
    return _load_connector(path, live=True)

def update_manifest_redaction_report(path: Path, report: Mapping[str, int]) -> None:
    _update_manifest_redaction(path, report)
    payload = read_json(path, {})
    payload["degraded"] = bool(payload.get("problems"))
    write_json(path, payload)

def load_gh_fixture_dir(path: Path) -> RawEvidence:
    pr_payload = read_json(path / "pr-view.json", {})
    final_pr = read_json(path / "final-pr-view.json", pr_payload)
    logs = read_json(path / "ci-logs.json", None)
    if logs is None:
        log_dir = path / "logs"
        if log_dir.exists():
            logs = [{"name": item.name, "text": read_text(item)} for item in sorted(log_dir.glob("*.txt"))]
    tool_versions = read_json(path / "tool-versions.json", {})
    return RawEvidence(
        source="gh-fixture",
        pr=pr_payload if isinstance(pr_payload, Mapping) else {},
        final_pr=final_pr if isinstance(final_pr, Mapping) else {},
        diff=read_text(path / "diff.patch"),
        files=read_json(path / "files.json", None),
        reviews=read_json(path / "reviews.json", []),
        comments=read_json(path / "comments.json", []),
        check_runs=read_json(path / "check-runs.json", {}),
        annotations=read_json(path / "annotations.json", []),
        logs=logs,
        commands=parse_gh_commands(read_json(path / "commands.json", [])),
        operations=[],
        tool_versions=tool_versions if isinstance(tool_versions, dict) else {},
    )

def load_gh_readonly_dir(path: Path) -> RawEvidence:
    raw = load_gh_fixture_dir(path)
    raw.source = "gh-readonly-import-live"
    if not (path / "final-pr-view.json").exists():
        raw.final_pr = {}
    raw.live_provenance = _as_dict(read_json(path / "provenance.json", {}))
    foundation = read_json(path / "foundation.json", {}) or raw.live_provenance.get(
        "foundation"
    ) or raw.live_provenance.get("foundation_provenance")
    raw.foundation_provenance = _as_dict(foundation)
    return raw

def extract_ref_sha(value: Any) -> str:
    return value.strip() if isinstance(value, str) else (first_path_text(value, *REF_SHA_PATHS) if isinstance(value, Mapping) else "")

def _extract_side_sha(pr_payload: Mapping[str, Any], side: str) -> str:
    ref = f"{side}Ref"
    return first_text(
        pr_payload.get(f"{side}_sha"), pr_payload.get(f"{side}RefOid"),
        nested(pr_payload, side, "sha"), nested(pr_payload, side, "oid"),
        nested(pr_payload, ref, "target", "oid"), nested(pr_payload, ref, "target", "sha"),
        extract_ref_sha(pr_payload.get(side)), extract_ref_sha(pr_payload.get(ref)),
    )

def extract_base_sha(pr_payload: Mapping[str, Any]) -> str:
    return _extract_side_sha(pr_payload, "base")

def extract_head_sha(pr_payload: Mapping[str, Any]) -> str:
    return _extract_side_sha(pr_payload, "head")

def _extract_current_sha(pr_payload: Mapping[str, Any], side: str) -> str:
    return first_text(
        pr_payload.get(f"current_{side}_sha"),
        pr_payload.get(f"current{side.title()}Sha"),
        _extract_side_sha(pr_payload, side),
    )

def extract_current_base_sha(pr_payload: Mapping[str, Any]) -> str:
    return _extract_current_sha(pr_payload, "base")

def extract_current_head_sha(pr_payload: Mapping[str, Any]) -> str:
    return _extract_current_sha(pr_payload, "head")

def extract_merge_base(raw: RawEvidence, pr_payload: Mapping[str, Any]) -> str:
    return first_text(
        first_path_text(pr_payload, *MERGE_BASE_PATHS),
        first_path_text(raw.pr, ("merge_base", "sha"), ("mergeBase", "sha")),
    )

def normalize_file_item(item: Any) -> dict[str, Any]:
    if isinstance(item, str):
        return {"filename": item}
    if not isinstance(item, Mapping):
        return {"filename": ""}
    return descriptor(
        NORMALIZED_FILE_TEMPLATE,
        filename=first_text(item.get("filename"), item.get("path"), item.get("file")),
        status=first_text(item.get("status"), item.get("changeType")),
        additions=item.get("additions"), deletions=item.get("deletions"),
        changes=item.get("changes"),
    )

def normalize_files(raw: RawEvidence, pr_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = raw.files if raw.files is not None else pr_payload.get("files")
    items, _, _ = flatten_paginated(payload, ("files", "changed_files", "nodes"))
    return [normalize_file_item(item) for item in items]

def normalize_simple_list(payload: Any, keys: Sequence[str]) -> tuple[list[Any], bool, bool]:
    items, partial, incomplete = flatten_paginated(payload, keys)
    normalized = [item if isinstance(item, Mapping) else {"body": str(item)} for item in items]
    return normalized, partial, incomplete

def normalize_check_runs(payload: Any, captured_head: str, problems: list[dict[str, Any]]) -> tuple[dict[str, Any], bool, bool]:
    top_head = first_text(payload.get("head_sha"), payload.get("headSha")) if isinstance(payload, Mapping) else ""
    items, partial, incomplete = flatten_paginated(payload, ("check_runs", "checks", "runs", "nodes"))
    normalized: list[dict[str, Any]] = []
    add = lambda message, rule="github-checks": problems.append(
        blocking_problem(message, rule=rule)
    )
    if not items:
        add("GitHub PR evidence is missing check runs")
    for idx, item in enumerate(items):
        if not isinstance(item, Mapping):
            add(f"check run {idx + 1} is malformed")
            continue
        status = first_text(item.get("status")).lower()
        conclusion = first_text(item.get("conclusion")).lower()
        run_head = check_head_sha(item, top_head)
        if not run_head:
            add(f"check run {idx + 1} is missing head SHA binding")
        elif captured_head and run_head != captured_head:
            add(f"check run {idx + 1} is bound to a different head SHA")
        if not status:
            add(f"check run {idx + 1} is missing status")
        elif status not in COMPLETED_STATUSES:
            add(f"check run {idx + 1} is not complete: {status}")
        if status in COMPLETED_STATUSES and not conclusion:
            add(f"check run {idx + 1} is missing conclusion")
        elif conclusion in BLOCKING_CONCLUSIONS or (conclusion and conclusion not in SUCCESSFUL_CONCLUSIONS):
            add(f"check run {idx + 1} conclusion is {conclusion}")
        elif status in PENDING_STATUSES:
            add(f"check run {idx + 1} is pending")
        normalized.append(descriptor(
            CHECK_RUN_TEMPLATE, id=item.get("id") or item.get("databaseId"),
            run_id=first_path_text(
                item, "run_id", "runId", "workflow_run_id", "workflowRunId",
                ("workflow_run", "id"), ("workflowRun", "id"), ("workflowRun", "databaseId"),
            ),
            job_id=first_path_text(
                item, "job_id", "jobId", "workflow_job_id", "workflowJobId",
                ("workflow_job", "id"), ("workflowJob", "id"), ("workflowJob", "databaseId"),
            ),
            name=first_path_text(item, "name", "displayName", "workflowName"),
            status=status, conclusion=conclusion, head_sha=run_head,
            started_at=first_path_text(item, "started_at", "startedAt"),
            completed_at=first_path_text(item, "completed_at", "completedAt"),
            url=first_path_text(item, "url", "details_url", "detailsUrl"),
        ))
    if partial:
        add("check runs are permission-partial", "github-permissions")
    if incomplete:
        add("check runs pagination is incomplete", "github-pagination")
    return {"head_sha": top_head or captured_head, "partial_permissions": partial, "pagination_incomplete": incomplete, "check_runs": normalized}, partial, incomplete

def normalize_logs(logs: Any, *, include: bool, max_log_bytes: int, problems: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, int]]:
    report: dict[str, int] = {}
    if not include:
        return None, report
    limit = max(0, min(int(max_log_bytes), HARD_MAX_LOG_BYTES))
    if limit <= 0:
        problems.append(blocking_problem("max log bytes must be greater than zero", rule="github-logs"))
        return {"max_log_bytes": limit, "logs": []}, report
    entries: list[Any]
    if logs is None:
        entries = []
    elif isinstance(logs, list):
        entries = logs
    else:
        entries = [logs]
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(entries):
        if isinstance(item, Mapping):
            name = first_text(item.get("name"), item.get("check_name"), f"log-{idx + 1}")
            text = str(item.get("text") or item.get("log") or item.get("body") or "")
            identity = ci_log_identity_fields(item)
        else:
            name = f"log-{idx + 1}"
            text = str(item)
            identity = {}
        raw = text.encode("utf-8", errors="replace")
        clipped = raw[:limit].decode("utf-8", errors="replace")
        redacted, redaction_report = redact_artifact_payload(clipped)
        report = merge_reports(report, redaction_report)
        excerpt_text = str(redacted)
        excerpt_bytes = excerpt_text.encode("utf-8", errors="replace")
        entry = descriptor(
            LOG_ENTRY_TEMPLATE, name=name,
            original_sha256=hashlib.sha256(raw).hexdigest(), original_bytes=len(raw),
            captured_bytes=len(raw[:limit]),
            excerpt_sha256=hashlib.sha256(excerpt_bytes).hexdigest(),
            excerpt_bytes=len(excerpt_bytes), max_log_bytes=limit,
            truncated=len(raw) > limit, text=excerpt_text,
        )
        for field in ("repo", "pr", "captured_head_sha", "check_run_id", "run_id", "job_id"):
            value = str(identity.get(field) or "").strip()
            if value:
                entry[field] = value
        out.append(entry)
    if not out:
        problems.append(blocking_problem("CI logs were requested but no log fixture was available", rule="github-logs"))
    return {"max_log_bytes": limit, "logs": out}, report

def normalize_pr_payload(
    *,
    raw: RawEvidence,
    repo: str,
    pr_number: str,
    captured_base: str,
    captured_head: str,
    current_base: str,
    current_head: str,
    merge_base: str,
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    source = raw.pr
    return descriptor(
        PR_TEMPLATE, repo=repo, number=pr_number,
        title=first_text(source.get("title")), state=first_text(source.get("state")),
        author=source.get("author"), url=first_text(source.get("url"), source.get("html_url")),
        base_sha=captured_base, head_sha=captured_head, current_base_sha=current_base,
        current_head_sha=current_head, merge_base_sha=merge_base, changed_files=files,
    )

def artifact_write_json(path: Path, payload: Any) -> tuple[Path, dict[str, int]]:
    redacted, report = redact_artifact_payload(payload)
    return write_json(path, redacted), report

def artifact_write_text(path: Path, text: str) -> tuple[Path, dict[str, int]]:
    redacted, report = redact_artifact_payload(text)
    return write_text(path, str(redacted)), report

def has_fixture_marker(value: Any) -> bool:
    text = str(value or "").lower()
    return "fixture" in text or text in {"connector-fixture", "gh-fixture", "missing-fixture"}

def payload_repository_identity(payload: Mapping[str, Any]) -> str:
    return first_text(
        payload.get("repo"),
        payload.get("repository_full_name"),
        payload.get("repositoryFullName"),
        payload.get("nameWithOwner"),
        repository_identity(payload.get("repository")),
        repository_identity(nested(payload, "base", "repo")),
    )

def validate_payload_identity(payload: Mapping[str, Any], *, repo: str, pr_number: str, label: str) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    payload_repo = payload_repository_identity(payload)
    if payload_repo and payload_repo != repo:
        problems.append(blocking_problem(f"{label} repository identity does not match --repo", rule="github-live-provenance"))
    payload_pr = first_text(payload.get("number"), payload.get("pr"), payload.get("pull_request"))
    if payload_pr and payload_pr != str(pr_number):
        problems.append(blocking_problem(f"{label} PR metadata does not match --pr", rule="github-live-provenance"))
    for key in ("url", "html_url", "web_url", "pull_request_url", "pullRequestUrl"):
        raw_url = payload.get(key)
        for message in github_url_identity_messages(raw_url, f"{label} {key}", require_url=True):
            problems.append(blocking_problem(message, rule="github-live-provenance"))
        url_repo = repo_from_url(raw_url)
        url_pr = pr_from_url(raw_url)
        if url_repo and url_repo != repo:
            problems.append(blocking_problem(f"{label} URL repository does not match --repo", rule="github-live-provenance"))
        if url_pr and url_pr != str(pr_number):
            problems.append(blocking_problem(f"{label} URL PR does not match --pr", rule="github-live-provenance"))
    return problems

def validate_live_import(
    raw: RawEvidence,
    *,
    repo: str,
    pr_number: str,
    captured_base: str,
    captured_head: str,
    current_base: str,
    current_head: str,
) -> list[dict[str, Any]]:
    if not is_live_source(raw.source):
        return []
    provenance = raw.live_provenance
    collected_at = first_text(provenance.get("collected_at"), provenance.get("captured_at"))
    source = first_text(provenance.get("source"))
    provenance_repo = first_text(provenance.get("repo"), provenance.get("repository"))
    provenance_pr = first_text(provenance.get("pr"), provenance.get("pull_request"), provenance.get("number"))
    conditions = (
        not raw.tool_versions or any(
            has_fixture_marker(key) or has_fixture_marker(value)
            for key, value in raw.tool_versions.items()
        ),
        not provenance, not collected_at, not source,
        bool(source and source != raw.source),
        not provenance_repo, bool(provenance_repo and provenance_repo != repo),
        not provenance_pr, bool(provenance_pr and provenance_pr != str(pr_number)),
    )
    rule = "github-live-provenance"
    messages = policy_tuple("github_adapter", "LIVE_IMPORT_MESSAGES")
    problems = [
        blocking_problem(message, rule=rule)
        for failed, message in zip(conditions[:2], messages[:2]) if failed
    ]
    problems.append(blocking_problem(
        "This GitHub collector has no independently verifiable host-controlled provenance route",
        rule="github-provider-receipt",
    ))
    problems += validate_live_github_host(raw)
    problems += [
        blocking_problem(message, rule=rule)
        for failed, message in zip(conditions[2:], messages[2:9]) if failed
    ]
    problems.extend(validate_payload_identity(raw.pr, repo=repo, pr_number=str(pr_number), label="live GitHub PR metadata"))
    if not raw.final_pr:
        tail = (True, False)
    else:
        problems.extend(validate_payload_identity(raw.final_pr, repo=repo, pr_number=str(pr_number), label="live GitHub final PR metadata"))
        tail = (
            False,
            not extract_current_base_sha(raw.final_pr)
            or not extract_current_head_sha(raw.final_pr),
        )
    tail += (
        not raw.commands and not raw.operations,
        not all((captured_base, captured_head, current_base, current_head)),
    )
    problems += [
        blocking_problem(message, rule=rule)
        for failed, message in zip(tail, messages[9:]) if failed
    ]
    return problems

def operation_transcript_payload(
    *,
    raw: RawEvidence,
    repo: str,
    pr_number: str,
    github_host: str,
    captured_base: str,
    captured_head: str,
    current_base: str,
    current_head: str,
    merge_base: str,
    partial_permissions: bool,
    pagination_incomplete: bool,
) -> dict[str, Any]:
    provenance = dict(raw.live_provenance)
    return descriptor(
        OPERATION_TRANSCRIPT_TEMPLATE, source=raw.source, repo=repo, pr=str(pr_number),
        github_host=github_host,
        collected_at=first_text(provenance.get("collected_at"), provenance.get("captured_at")),
        imported_at=live_common.now_utc(), live_provenance=provenance,
        captured_base_sha=captured_base, current_base_sha=current_base,
        captured_head_sha=captured_head, current_head_sha=current_head,
        merge_base_sha=merge_base, partial_permissions=bool(partial_permissions),
        pagination_incomplete=bool(pagination_incomplete), operations=raw.operations,
        commands=raw.commands, connector_operations=sorted(CONNECTOR_READ_OPERATIONS),
        github_hosts=sorted(APPROVED_GITHUB_HOSTS),
    )
