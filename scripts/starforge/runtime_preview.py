"""Cohesive Star Forge runtime extracted from the CLI facade."""

from __future__ import annotations
import argparse
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Sequence
from live_collectors import browser_safety
from live_collectors import common as live_common
from live_collectors import preview as preview_collector
from .policy_data import mapping as policy_mapping, value as _policy_value
from .runtime_support import BLOCKING_SEVERITIES, SERVER_LEASE, artifact_entry, dirty_paths_missing_from_source_snapshot, file_sha256, git_head, now_utc, read_text, redact, relative_to_project, tree_clean_for_commit_binding
from .runtime_project import ensure_state_dirs, resolve_project, safe_release_snapshot, try_source_hash
from .runtime_store import write_run_record

PREVIEW_POLICY = _policy_value("runtime_preview.POLICY")

def live_problem(message: str, *, severity: str = "high", rule: str = "live-proof", path: str = "") -> dict[str, Any]:
    return {"severity": severity, "rule": rule, "message": message} | ({"path": path} if path else {})

def flag_live_problem(problems: list[dict[str, Any]], condition: Any, message: str, **attributes: Any) -> bool:
    if condition:
        problems.append(live_problem(message, **attributes))
    return bool(condition)

def append_live_problem_once(problems: list[dict[str, Any]], problem: dict[str, Any] | None) -> None:
    if not problem:
        return
    def identity(item: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(next((item.get(field) for field in fields if item.get(field)), "")) for fields in PREVIEW_POLICY["problem_identity_fields"])
    if identity(problem) not in {identity(item) for item in problems}:
        problems.append(dict(problem))

def current_live_source_hash(project: Path, problems: list[dict[str, Any]]) -> str | None:
    current, problem = try_source_hash(project)
    if problem:
        append_live_problem_once(problems, problem)
    return None if problem else current

def live_has_blockers(problems: Sequence[dict[str, Any]]) -> bool:
    return any(bool(item.get("blocking")) or str(item.get("severity") or "").lower() in BLOCKING_SEVERITIES for item in problems)

def live_rel(project: Path, path: Path) -> str:
    return relative_to_project(path, project)

def scoped_live_path_parts(project: Path, path: Path, collector: str | None = None) -> tuple[str, ...] | None:
    try:
        parts = path.resolve().relative_to(project.resolve()).parts
    except ValueError:
        return None
    expected_collector = live_common.sanitize_segment(collector, fallback="collector")
    return parts if len(parts) >= 4 and parts[:2] == (".starforge", "live") and (collector is None or parts[3] == expected_collector) else None

def is_task_scoped_live_path(project: Path, path: Path, task: str | None, collector: str | None) -> bool:
    parts = scoped_live_path_parts(project, path, collector)
    return bool(parts and (task is None or parts[2] == live_common.sanitize_segment(task, fallback="task")))

def task_from_scoped_live_path(project: Path, path: Path, collector: str | None) -> str | None:
    parts = scoped_live_path_parts(project, path, collector)
    return parts[2] if parts else None

def json_load_path(path: Path) -> Any:
    return json.loads(read_text(path))

def validate_artifact_arg(
    project: Path,
    raw_path: str | None,
    label: str,
    problems: list[dict[str, Any]],
    *,
    task: str | None = None,
    collector: str | None = None,
    require_scoped: bool = True,
    require_json: bool = False,
    require_object: bool = False,
    require_image: bool = False,
    require_dir: bool = False,
    optional: bool = False,
) -> tuple[dict[str, Any] | None, Any | None]:
    if not raw_path:
        if not optional:
            problems.append(live_problem(f"{label} is required", rule="artifact-missing"))
        return None, None
    try:
        path = live_common.safe_project_path(project, raw_path, must_exist=False)
    except ValueError as exc:
        problems.append(live_problem(f"{label} path is unsafe: {exc}", rule="artifact-path", path=str(raw_path)))
        return None, None
    rel = live_rel(project, path)
    flag_live_problem(
        problems, require_scoped and not is_task_scoped_live_path(project, path, task, collector),
        f"{label} must be under .starforge/live/{live_common.sanitize_segment(task or 'task')}/{live_common.sanitize_segment(collector or 'collector')}/",
        rule="artifact-scope", path=rel,
    )
    if not path.exists():
        problems.append(live_problem(f"{label} does not exist", rule="artifact-missing", path=rel))
        return {"kind": label, "path": rel, "exists": False}, None
    if require_dir:
        flag_live_problem(problems, not path.is_dir(), f"{label} must be a directory", rule="artifact-shape", path=rel)
        return {"kind": label, "path": rel, "exists": path.exists(), "directory": path.is_dir()}, None
    entry = artifact_entry(project, path, kind="screenshot" if require_image else label)
    payload = None
    if require_json:
        try:
            payload = json_load_path(path)
        except Exception as exc:
            problems.append(live_problem(f"{label} is malformed JSON: {exc}", rule="artifact-json", path=rel))
        else:
            flag_live_problem(problems, require_object and not isinstance(payload, dict), f"{label} must be a JSON object", rule="artifact-shape", path=rel)
    flag_live_problem(problems, require_image and not entry.get("valid_image"), f"{label} is not a decodable PNG/JPEG image", rule="artifact-image", path=rel)
    return entry, payload

def default_live_manifest_path(project: Path, task: str, collector: str) -> Path:
    return live_common.live_collector_dir(project, task, collector, create=False) / "manifest.json"

def iter_manifest_artifact_records(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict):
        values = artifacts.values()
    elif isinstance(artifacts, list):
        values = artifacts
    else:
        values = ()
    return (item for item in values if isinstance(item, dict))

def manifest_artifact_record_for_path(project: Path, manifest: dict[str, Any] | None, path: Path) -> dict[str, Any] | None:
    if not isinstance(manifest, dict):
        return None
    target = path.resolve()
    for record in iter_manifest_artifact_records(manifest):
        raw_path = str(record.get("path") or "")
        if not raw_path:
            continue
        try:
            artifact = live_common.safe_project_path(project, raw_path, must_exist=False)
        except ValueError:
            continue
        if artifact.resolve() == target:
            return record
    return None

def manifest_artifact_path_for_kind(project: Path, manifest: dict[str, Any] | None, *kinds: str) -> Path | None:
    if not isinstance(manifest, dict):
        return None
    normalized = {re.sub(r"[^a-z0-9]+", "-", kind.lower()).strip("-") for kind in kinds}
    for record in iter_manifest_artifact_records(manifest):
        kind = re.sub(r"[^a-z0-9]+", "-", str(record.get("kind") or "").lower()).strip("-")
        if kind not in normalized:
            continue
        raw_path = str(record.get("path") or "")
        if not raw_path:
            continue
        try:
            return live_common.safe_project_path(project, raw_path, must_exist=False)
        except ValueError:
            return None
    return None

def iter_raw_artifact_hashes(manifest: dict[str, Any] | None) -> Iterable[tuple[str, str]]:
    raw_hashes = manifest.get("raw_artifact_hashes") if isinstance(manifest, dict) else None
    if not isinstance(raw_hashes, dict):
        return
    for key, value in raw_hashes.items():
        if isinstance(value, dict):
            yield str(value.get("path") or key), str(value.get("sha256") or "")
        else:
            yield str(key), value if isinstance(value, str) else ""

def validate_raw_artifact_hashes(
    project: Path,
    manifest: dict[str, Any],
    problems: list[dict[str, Any]],
    *,
    task: str | None = None,
    collector: str | None = None,
    require_scoped: bool = False,
) -> None:
    for raw_path, expected in iter_raw_artifact_hashes(manifest):
        if flag_live_problem(problems, not expected, "raw artifact hash entry is missing sha256", rule="artifact-hash", path=str(raw_path)):
            continue
        try:
            path = live_common.safe_project_path(project, raw_path, must_exist=False)
        except ValueError as exc:
            problems.append(live_problem(f"raw artifact hash path is unsafe: {exc}", rule="artifact-path", path=str(raw_path)))
            continue
        rel = live_rel(project, path)
        if flag_live_problem(problems, require_scoped and not is_task_scoped_live_path(project, path, task, collector), "raw artifact hash path must stay under the task-scoped live collector directory", rule="artifact-scope", path=rel):
            continue
        if flag_live_problem(problems, not path.exists() or not path.is_file(), "raw artifact hash target is missing", rule="artifact-missing", path=rel):
            continue
        flag_live_problem(problems, file_sha256(path) != expected, "raw artifact hash does not match current bytes", rule="artifact-hash", path=rel)

def manifest_raw_hash_for_path(project: Path, manifest: dict[str, Any] | None, path: Path) -> str:
    rel = live_rel(project, path)
    return next((sha256 for raw_path, sha256 in iter_raw_artifact_hashes(manifest) if raw_path == rel), "")

def require_raw_hash_for_artifact(
    project: Path,
    manifest: dict[str, Any] | None,
    path: Path,
    problems: list[dict[str, Any]],
    *,
    label: str,
    rule: str = "artifact-hash",
) -> str:
    rel = live_rel(project, path)
    actual = ""
    record = manifest_artifact_record_for_path(project, manifest, path)
    if record is None:
        problems.append(live_problem(f"{label} must be recorded in manifest artifacts", rule=rule, path=rel))
    elif path.exists() and path.is_file():
        actual = file_sha256(path)
        record_hash = str(record.get("sha256") or "")
        if not record_hash:
            problems.append(live_problem(f"{label} manifest artifact is missing sha256", rule=rule, path=rel))
        elif actual != record_hash:
            problems.append(live_problem(f"{label} manifest artifact sha256 does not match current bytes", rule=rule, path=rel))
    expected = manifest_raw_hash_for_path(project, manifest, path)
    if not expected:
        problems.append(live_problem(f"{label} must be recorded in raw_artifact_hashes", rule=rule, path=rel))
        return ""
    if not path.exists() or not path.is_file():
        return expected
    if not actual:
        actual = file_sha256(path)
    if actual != expected:
        problems.append(live_problem(f"{label} raw artifact hash does not match current bytes", rule=rule, path=rel))
    return actual

def append_artifact_once(artifacts: list[dict[str, Any]], entry: dict[str, Any] | None) -> None:
    path = str(entry.get("path") or "") if entry else ""
    if entry and (not path or not any(str(item.get("path") or "") == path for item in artifacts)):
        artifacts.append(entry)

def validate_manifest_bound_artifact_arg(
    project: Path,
    raw_path: str | Path | None,
    label: str,
    problems: list[dict[str, Any]],
    *,
    manifest: dict[str, Any] | None,
    raw_hash_rule: str = "artifact-hash",
    task: str | None = None,
    collector: str | None = None,
    require_scoped: bool = True,
    require_json: bool = False,
    require_object: bool = False,
    require_image: bool = False,
    require_dir: bool = False,
    optional: bool = False,
) -> tuple[dict[str, Any] | None, Any | None]:
    entry, payload = validate_artifact_arg(
        project,
        str(raw_path) if raw_path is not None else None,
        label,
        problems,
        task=task,
        collector=collector,
        require_scoped=require_scoped,
        require_json=require_json,
        require_object=require_object,
        require_image=require_image,
        require_dir=require_dir,
        optional=optional,
    )
    if entry and entry.get("exists") and not entry.get("directory"):
        try:
            path = live_common.safe_project_path(project, str(entry.get("path") or ""), must_exist=False)
        except ValueError as exc:
            problems.append(live_problem(f"{label} path is unsafe: {exc}", rule="artifact-path", path=str(entry.get("path") or "")))
        else:
            require_raw_hash_for_artifact(project, manifest, path, problems, label=label, rule=raw_hash_rule)
    return entry, payload

def validate_manifest_artifact_scopes(
    project: Path,
    manifest: dict[str, Any],
    problems: list[dict[str, Any]],
    *,
    task: str | None,
    collector: str | None,
    require_scoped: bool,
) -> None:
    if not require_scoped:
        return
    for record in iter_manifest_artifact_records(manifest):
        artifact_path = str(record.get("path") or "")
        if not artifact_path:
            continue
        try:
            artifact = live_common.safe_project_path(project, artifact_path, must_exist=False)
        except ValueError:
            continue
        artifact_rel = live_rel(project, artifact)
        flag_live_problem(problems, not is_task_scoped_live_path(project, artifact, task, collector), "manifest artifact path must stay under the task-scoped live collector directory", rule="artifact-scope", path=artifact_rel)

def load_and_validate_live_manifest(
    project: Path,
    raw_path: str | Path | None,
    problems: list[dict[str, Any]],
    *,
    task: str | None = None,
    collector: str | None = None,
    require_scoped: bool = True,
) -> tuple[dict[str, Any] | None, Path | None]:
    if not raw_path:
        problems.append(live_problem("manifest is required", rule="manifest-missing"))
        return None, None
    try:
        path = live_common.safe_project_path(project, raw_path, must_exist=False)
    except ValueError as exc:
        problems.append(live_problem(f"manifest path is unsafe: {exc}", rule="manifest-path", path=str(raw_path)))
        return None, None
    rel = live_rel(project, path)
    if require_scoped and not is_task_scoped_live_path(project, path, task, collector):
        scope_task = live_common.sanitize_segment(task or "task")
        scope_collector = live_common.sanitize_segment(collector or "collector")
        problems.append(live_problem(f"manifest must be under .starforge/live/{scope_task}/{scope_collector}/", rule="manifest-scope", path=rel))
    if not path.exists():
        problems.append(live_problem("manifest does not exist", rule="manifest-missing", path=rel))
        return None, path
    try:
        payload = json_load_path(path)
    except Exception as exc:
        problems.append(live_problem(f"manifest is malformed JSON: {exc}", rule="manifest-json", path=rel))
        return None, path
    if not isinstance(payload, dict):
        problems.append(live_problem("manifest must be a JSON object", rule="manifest-shape", path=rel))
        return None, path
    problems.extend(live_common.validate_manifest_payload(payload))
    flag_live_problem(problems, collector is not None and payload.get("collector") != live_common.sanitize_segment(collector, fallback="collector"), f"manifest collector must be `{collector}`", rule="manifest-collector", path=rel)
    flag_live_problem(problems, task is not None and payload.get("task") != task, f"manifest task must be `{task}`", rule="manifest-task", path=rel)
    flag_live_problem(problems, payload.get("degraded") is True, "manifest is marked degraded", rule="manifest-degraded", path=rel)
    unavailable = payload.get("unavailable_capabilities")
    flag_live_problem(problems, isinstance(unavailable, list) and unavailable, "manifest records unavailable required capabilities: " + ", ".join(str(item) for item in unavailable or []), rule="manifest-unavailable", path=rel)
    manifest_problems = payload.get("problems")
    if isinstance(manifest_problems, list):
        for item in manifest_problems:
            if not isinstance(item, dict):
                problems.append(live_problem("manifest contains a malformed problem entry", rule="manifest-problem", path=rel))
                continue
            severity = str(item.get("severity") or "").lower()
            if item.get("blocking") or severity in BLOCKING_SEVERITIES:
                msg = str(item.get("message") or "manifest contains a blocking problem")
                problems.append(live_problem(msg, severity=severity or "high", rule=str(item.get("rule") or "manifest-problem"), path=str(item.get("path") or rel)))
    current_source = current_live_source_hash(project, problems)
    if current_source is not None:
        for field in ("source_hash_before", "source_hash_after"):
            value = str(payload.get(field) or "")
            flag_live_problem(problems, value != current_source, f"manifest {field} does not match current source hash", rule="manifest-source", path=rel)
    flag_live_problem(problems, str(payload.get("runtime_asset_hash") or "") != live_common.compute_runtime_asset_hash(project), "manifest runtime_asset_hash does not match current runtime assets", rule="manifest-runtime", path=rel)
    flag_live_problem(problems, not isinstance(payload.get("redaction_report"), dict), "manifest redaction_report must be an object", rule="manifest-shape", path=rel)
    for record in iter_manifest_artifact_records(payload):
        artifact_path = str(record.get("path") or "")
        if not artifact_path:
            problems.append(live_problem("manifest artifact is missing path", rule="artifact-shape", path=rel))
            continue
        try:
            artifact = live_common.safe_project_path(project, artifact_path, must_exist=False)
        except ValueError as exc:
            problems.append(live_problem(f"manifest artifact path is unsafe: {exc}", rule="artifact-path", path=artifact_path))
            continue
        artifact_rel = live_rel(project, artifact)
        if require_scoped and not is_task_scoped_live_path(project, artifact, task, collector):
            problems.append(live_problem("manifest artifact path must stay under the task-scoped live collector directory", rule="artifact-scope", path=artifact_rel))
            continue
        flag_live_problem(problems, not artifact.exists(), "manifest artifact is missing", rule="artifact-missing", path=artifact_rel)
        flag_live_problem(problems, record.get("problem"), f"manifest artifact problem: {record.get('problem')}", rule="artifact-problem", path=artifact_rel)
        flag_live_problem(problems, record.get("sha256") and artifact.exists() and artifact.is_file() and file_sha256(artifact) != record.get("sha256"), "manifest artifact sha256 does not match current bytes", rule="artifact-hash", path=artifact_rel)
    validate_raw_artifact_hashes(project, payload, problems, task=task, collector=collector, require_scoped=require_scoped)
    return payload, path

def live_manifest_summary(manifest: dict[str, Any] | None) -> dict[str, Any]:
    return summary if isinstance(summary := manifest.get("summary") if isinstance(manifest, dict) else {}, dict) else {}

def result_indicates_failure(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "result must be a JSON object"
    true_failure = next((key for key in PREVIEW_POLICY["result_true_failure_fields"] if payload.get(key) is True), None)
    if true_failure:
        return f"{true_failure} is true"
    false_failure = next((key for key in PREVIEW_POLICY["result_false_failure_fields"] if key in payload and payload.get(key) is False), None)
    if false_failure:
        return f"{false_failure} is false"
    for key in PREVIEW_POLICY["result_code_fields"]:
        if key in payload:
            try:
                if int(payload.get(key)) != 0:
                    return f"{key} is nonzero"
            except (TypeError, ValueError):
                return f"{key} is not numeric"
    status = str(payload.get("status") or payload.get("conclusion") or "").lower()
    if status in PREVIEW_POLICY["result_failure_statuses"]:
        return f"status is {status}"
    return None

def json_has_failed_smoke(payload: Any) -> list[str]:
    fields = PREVIEW_POLICY["smoke_success_fields"]
    if isinstance(payload, dict):
        messages = [f"smoke {key} is false" for key in fields if payload.get(key) is False]
        checks = payload.get("checks")
    elif isinstance(payload, list):
        messages, checks = [], payload
    else:
        return ["smoke checks must be a JSON object or array"]
    if isinstance(checks, list):
        messages.extend(f"smoke check {idx + 1} failed" for idx, check in enumerate(checks) if isinstance(check, dict) and any(check.get(key) is False for key in fields))
    return messages

def preview_url_safety_problems(url: str, *, allow_local: bool = False) -> list[dict[str, Any]]:
    return [
        live_problem(str(item.get("message") or "preview URL is unsafe"),
                     severity=str(item.get("severity") or "high"),
                     rule=str(item.get("rule") or "preview-url"),
                     path=str(item.get("path") or "")) for item in preview_collector.validate_url_safety(url, allow_local=allow_local) if isinstance(item, dict)
    ]

def preview_connected_ip_problems(url: str, raw_ips: Any, *, allow_local: bool, path: str = "") -> list[dict[str, Any]]:
    if not isinstance(raw_ips, list) or not raw_ips:
        return [live_problem("preview HTTP connected IP evidence must be a non-empty list", rule="preview-url", path=path)]
    parsed = urllib.parse.urlparse(url or "")
    host = parsed.hostname or ""
    host_ip = browser_safety.parse_ip(host)
    explicit_local = host.lower() == "localhost" or bool(host_ip and host_ip.is_loopback)
    problems: list[dict[str, Any]] = []
    for raw in raw_ips:
        ip = preview_collector.parse_ip(str(raw))
        if ip is None:
            problems.append(live_problem("preview HTTP connected IP evidence contains an invalid address", rule="preview-url", path=path))
            continue
        blocked = preview_collector.is_blocked_ip(ip, explicit_local_allowed=explicit_local and allow_local)
        if blocked:
            problems.append(live_problem(f"preview HTTP connected to unsafe address {ip}: {blocked}", rule="preview-url", path=path))
    return problems

def strict_preview_http_artifact_problems(
    *,
    url: str,
    summary_url: str,
    expect_status: int,
    http_payload: dict[str, Any],
    path: str,
    allow_local: bool,
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for field, expected, message in PREVIEW_POLICY["http_value_checks"]:
        flag_live_problem(problems, http_payload.get(field) != expected, message, rule="preview-http", path=path)
    flag_live_problem(problems, str(http_payload.get("method") or "").upper() != PREVIEW_POLICY["http_method"], "preview HTTP evidence must record method GET", rule="preview-http", path=path)
    http_url = str(http_payload.get("url") or "")
    if not http_url:
        problems.append(live_problem("preview HTTP evidence must record the requested URL", rule="preview-url", path=path))
    else:
        flag_live_problem(problems, http_url != str(url), "preview HTTP evidence URL does not match proof URL", rule="preview-url", path=path)
        flag_live_problem(problems, summary_url and http_url != summary_url, "preview HTTP evidence URL does not match live manifest URL", rule="preview-url", path=path)
    flag_live_problem(problems, http_payload.get("expected_status") != expect_status, f"preview HTTP expected_status must equal {expect_status}", rule="preview-status", path=path)
    final_url = str(http_payload.get("final_url") or "")
    if not final_url:
        problems.append(live_problem("preview HTTP evidence must record final_url", rule="preview-url", path=path))
    else:
        for item in preview_url_safety_problems(final_url, allow_local=allow_local):
            problems.append(live_problem(f"final_url is unsafe: {item.get('message')}", rule="preview-redirect", path=path))
    raw_artifact_problems = http_payload.get("problems")
    if raw_artifact_problems:
        if not isinstance(raw_artifact_problems, list):
            problems.append(live_problem("preview HTTP artifact problems must be a list", rule="preview-http", path=path))
        else:
            for item in raw_artifact_problems:
                if not isinstance(item, dict):
                    problems.append(live_problem("preview HTTP artifact contains a malformed problem entry", rule="preview-http", path=path))
                    continue
                if live_has_blockers([item]):
                    message = str(item.get("message") or "preview HTTP artifact recorded a blocking problem")
                    problems.append(
                        live_problem(f"preview HTTP artifact recorded a blocking problem: {message}",
                                     severity=str(item.get("severity") or "high"),
                                     rule=str(item.get("rule") or "preview-http"),
                                     path=str(item.get("path") or path)))
    pinning = http_payload.get("connection_pinning")
    if not isinstance(pinning, dict):
        problems.append(live_problem("preview HTTP evidence must include connection_pinning", rule="preview-http", path=path))
    else:
        pinning_url = http_url or str(url)
        scheme = urllib.parse.urlparse(pinning_url).scheme
        strategy = str(pinning.get("strategy") or "")
        if scheme == "http":
            flag_live_problem(problems, strategy != PREVIEW_POLICY["http_pinning_strategies"]["http"], "preview HTTP connection_pinning.strategy must be http-connect-vetted-ip", rule="preview-http", path=path)
        elif scheme == "https":
            problems.append(live_problem("HTTPS preview evidence is rejected until verifiable SNI pinning is available from the collector", rule="preview-http", path=path))
    return problems

def preview_url_requires_server_lease(url: str) -> bool:
    parsed, problems = browser_safety.validate_url(url or "")
    return bool(not problems and browser_safety.is_local_origin(parsed))

def preview_manifest_server_lease_path(project: Path, manifest: dict[str, Any] | None) -> Path | None:
    summary = live_manifest_summary(manifest)
    raw = next((summary.get(key) for key in PREVIEW_POLICY["lease_summary_fields"] if isinstance(summary.get(key), str) and summary.get(key).strip()), None)
    if raw:
        try:
            return live_common.safe_project_path(project, raw, must_exist=False)
        except ValueError:
            return None
    return manifest_artifact_path_for_kind(project, manifest, *PREVIEW_POLICY["lease_artifact_kinds"])

def validate_preview_server_lease_artifact(
    project: Path,
    *,
    task: str,
    url: str,
    manifest: dict[str, Any] | None,
    artifacts: list[dict[str, Any]],
    problems: list[dict[str, Any]],
) -> bool:
    lease_path = preview_manifest_server_lease_path(project, manifest)
    if lease_path is None:
        problems.append(live_problem("local preview URL requires a server lease artifact recorded in the live manifest", rule="preview-localhost"))
        return False
    entry, _payload = validate_manifest_bound_artifact_arg(
        project,
        lease_path,
        "server lease",
        problems,
        manifest=manifest,
        raw_hash_rule="server-lease",
        task=task,
        collector="preview",
        require_scoped=True,
        require_json=True,
        require_object=True,
    )
    append_artifact_once(artifacts, entry)
    try:
        from live_collectors import browser_playwright
        parsed_url, url_problems = browser_playwright.validate_url(url or "")
        problems.extend(dict(item) for item in url_problems)
        if url_problems:
            return False
        current_source = current_live_source_hash(project, problems)
        if current_source is None:
            return False
        _lease_path, lease_payload, lease_problems = browser_playwright.validate_server_lease(
            project,
            str(lease_path),
            parsed_url,
            current_source,
            live_common.compute_runtime_asset_hash(project, exclude_paths=[project / SERVER_LEASE]),
        )
        problems.extend(dict(item) for item in lease_problems)
        return lease_payload is not None and not lease_problems
    except Exception as exc:
        problems.append(live_problem(f"server lease validation failed: {exc}", rule="server-lease", path=live_rel(project, lease_path)))
        return False

def preview_allow_local_for_url(
    project: Path,
    *,
    task: str,
    url: str,
    manifest: dict[str, Any] | None,
    artifacts: list[dict[str, Any]],
    problems: list[dict[str, Any]],
    lease_cache: dict[str, bool],
) -> bool:
    if not url or not preview_url_requires_server_lease(url):
        return False
    if url not in lease_cache:
        lease_cache[url] = validate_preview_server_lease_artifact(
            project,
            task=task,
            url=url,
            manifest=manifest,
            artifacts=artifacts,
            problems=problems,
        )
    return lease_cache[url]

def deployment_bound_to_current(project: Path, deployment: Any, *, current_source_hash: str | None = None) -> bool:
    if not isinstance(deployment, dict):
        return False
    source_fields = PREVIEW_POLICY["source_hash_fields"]
    has_source_binding = any(str(deployment.get(key) or "") for key in source_fields)
    if current_source_hash is not None:
        if any(str(deployment.get(key) or "") == current_source_hash for key in source_fields):
            return not dirty_paths_missing_from_source_snapshot(project)
    head = git_head(project)
    if head and tree_clean_for_commit_binding(project) and any(str(deployment.get(key) or "") == head for key in PREVIEW_POLICY["commit_hash_fields"]):
        return True
    if current_source_hash is None and has_source_binding:
        return True
    return False

def write_live_proof_record(
    project: Path,
    *,
    kind: str,
    task: str | None,
    strict: bool,
    inputs: dict[str, Any],
    problems: list[dict[str, Any]],
    manifest_path: Path | None = None,
    manifest: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    summary: str = "",
) -> int:
    snapshot, snapshot_problem = safe_release_snapshot(project)
    problems = list(problems)
    if snapshot_problem:
        append_live_problem_once(problems, snapshot_problem)
    verdict = PREVIEW_POLICY["proof_verdicts"]["blocking" if live_has_blockers(problems) else "clear"]
    safe_inputs = {key: value for key, value in inputs.items() if key != "func"}
    payload = {"schema": f"star-forge.{kind}.v1", **policy_mapping(
        "live_proof_record", kind=kind, created_at=now_utc(), project=str(project), task=task, strict=bool(strict),
        inputs=redact(safe_inputs), verdict=verdict, source_snapshot=snapshot,
        runtime_asset_hash=live_common.compute_runtime_asset_hash(project),
        manifest=live_rel(project, manifest_path) if manifest_path else None,
        collector=manifest.get("collector") if isinstance(manifest, dict) else None,
        artifacts=artifacts or [], problems=problems, summary=summary,
    )}
    path = write_run_record(project, payload)
    payload["artifact"] = live_rel(project, path)
    print(json.dumps(payload, indent=2))
    return 0 if verdict == "PASS" or not strict else 1

def finish_live_proof_command(
    project: Path, args: argparse.Namespace, kind: str, problems: list[dict[str, Any]],
    manifest: dict[str, Any] | None, manifest_path: Path | None, artifacts: list[dict[str, Any]], summary: str,
) -> int:
    return write_live_proof_record(
        project, kind=kind, task=args.task, strict=args.strict, inputs=vars(args), problems=problems,
        manifest_path=manifest_path, manifest=manifest, artifacts=artifacts, summary=summary,
    )

def validate_preview_proof_artifacts(
    project: Path,
    *,
    task: str,
    url: str,
    expect_status: int,
    deployment_metadata: str,
    smoke_checks: str,
    strict: bool,
    manifest: dict[str, Any] | None,
    manifest_path: Path,
    problems: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    summary = live_manifest_summary(manifest)
    if not summary.get("url"):
        problems.append(live_problem("preview live manifest must include URL provenance", rule="preview-url"))
    elif url and str(summary.get("url")) != str(url):
        problems.append(live_problem("preview URL does not match live manifest URL", rule="preview-url"))
    lease_cache: dict[str, bool] = {}
    def allow_local(target_url: str) -> bool:
        return preview_allow_local_for_url(
            project, task=task, url=target_url, manifest=manifest, artifacts=artifacts,
            problems=problems, lease_cache=lease_cache,
        )
    def bound_artifact(raw_path: str, label: str, **requirements: Any) -> tuple[dict[str, Any] | None, Any | None]:
        result = validate_manifest_bound_artifact_arg(
            project, raw_path, label, problems, manifest=manifest, task=task, collector="preview", **requirements,
        )
        append_artifact_once(artifacts, result[0])
        return result
    initial_allow_local = allow_local(url)
    problems.extend(preview_url_safety_problems(url, allow_local=initial_allow_local))
    http_entry, http_payload = bound_artifact(str(manifest_path.parent / "http.json"), "http evidence", require_json=True, require_object=True)
    if isinstance(http_payload, dict):
        http_path = http_entry.get("path", "") if http_entry else ""
        if strict:
            problems.extend(
                strict_preview_http_artifact_problems(
                    url=url,
                    summary_url=str(summary.get("url") or ""),
                    expect_status=expect_status,
                    http_payload=http_payload,
                    path=http_path,
                    allow_local=initial_allow_local,
                ))
        status = http_payload.get("status") if "status" in http_payload else http_payload.get("status_code")
        if status != expect_status:
            problems.append(live_problem(f"http status {status} did not match expected {expect_status}", rule="preview-status", path=http_path))
        connected_url = str(http_payload.get("final_url") or url)
        if strict or http_payload.get("connected_ips") is not None:
            problems.extend(preview_connected_ip_problems(
                connected_url,
                http_payload.get("connected_ips"),
                allow_local=allow_local(connected_url),
                path=http_path,
            ))
        for key in ("final_url", "redirect_url"):
            if http_payload.get(key):
                checked_url = str(http_payload.get(key))
                for item in preview_url_safety_problems(checked_url, allow_local=allow_local(checked_url)):
                    problems.append(live_problem(f"{key} is unsafe: {item.get('message')}", rule="preview-redirect", path=http_path))
        redirects = http_payload.get("redirect_chain")
        if isinstance(redirects, list):
            for idx, item in enumerate(redirects):
                redirect_url = item.get("url") if isinstance(item, dict) else item
                if redirect_url:
                    redirect_text = str(redirect_url)
                    for problem_item in preview_url_safety_problems(redirect_text, allow_local=allow_local(redirect_text)):
                        problems.append(live_problem(f"redirect {idx + 1} is unsafe: {problem_item.get('message')}", rule="preview-redirect", path=http_path))
    deployment_entry, deployment_payload = bound_artifact(deployment_metadata, "deployment metadata", require_json=True, require_object=True)
    current_source = current_live_source_hash(project, problems)
    if deployment_payload is not None and not deployment_bound_to_current(project, deployment_payload, current_source_hash=current_source):
        problems.append(
            live_problem("deployment metadata is not bound to the current source", rule="preview-source-binding",
                         path=deployment_entry.get("path", "") if deployment_entry else ""))
    smoke_entry, smoke_payload = bound_artifact(smoke_checks, "smoke checks", require_json=True)
    if smoke_payload is not None:
        for message in json_has_failed_smoke(smoke_payload):
            problems.append(live_problem(message, rule="preview-smoke", path=smoke_entry.get("path", "") if smoke_entry else ""))
    return artifacts

def cmd_preview_proof(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    problems: list[dict[str, Any]] = []
    manifest_path = default_live_manifest_path(project, args.task, "preview")
    manifest, manifest_resolved = load_and_validate_live_manifest(project, manifest_path, problems, task=args.task, collector="preview")
    artifacts = validate_preview_proof_artifacts(
        project,
        task=args.task,
        url=args.url,
        expect_status=args.expect_status,
        deployment_metadata=args.deployment_metadata,
        smoke_checks=args.smoke_checks,
        strict=args.strict,
        manifest=manifest,
        manifest_path=manifest_path,
        problems=problems,
    )
    return finish_live_proof_command(project, args, "preview-proof", problems, manifest, manifest_resolved, artifacts, "preview proof")

def collector_for_profile(profile: str) -> str | None:
    return PREVIEW_POLICY["profile_collectors"].get(profile)

def dedicated_strict_proof_command_for_profile(profile: str) -> str:
    return PREVIEW_POLICY["strict_proof_commands"].get(profile, "")

def cmd_proof_run(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    problems: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    profile = str(args.profile or "").strip()
    collector = collector_for_profile(profile)
    manifest, manifest_path = load_and_validate_live_manifest(project, args.artifact, problems, task=args.task, collector=collector, require_scoped=collector is not None)
    if not profile:
        problems.append(live_problem("proof-run requires --profile", rule="proof-profile"))
    elif profile in PREVIEW_POLICY["github_source_profiles"]:
        problems.append(live_problem(PREVIEW_POLICY["proof_profile_messages"]["github"], rule="proof-profile"))
    dedicated_command = dedicated_strict_proof_command_for_profile(profile)
    if args.strict and dedicated_command:
        problems.append(live_problem(PREVIEW_POLICY["proof_profile_messages"]["dedicated"].format(profile=profile, command=dedicated_command), rule="proof-profile"))
    if profile == "preview" and manifest_path is not None:
        summary = live_manifest_summary(manifest)
        expected_status = summary.get("expected_status") if "expected_status" in summary else summary.get("status")
        try:
            expected_status_int = int(expected_status if expected_status is not None else 200)
        except (TypeError, ValueError):
            expected_status_int = 200
            problems.append(live_problem("preview manifest expected status is not numeric", rule="preview-status"))
        artifacts.extend(
            validate_preview_proof_artifacts(
                project,
                task=args.task,
                url=str(summary.get("url") or ""),
                expect_status=expected_status_int,
                deployment_metadata=str(manifest_path.parent / "deployment.json"),
                smoke_checks=str(manifest_path.parent / "smoke.json"),
                strict=args.strict,
                manifest=manifest,
                manifest_path=manifest_path,
                problems=problems,
            ))
    return finish_live_proof_command(project, args, "proof-run", problems, manifest, manifest_path, artifacts, f"profile={args.profile}")
NATIVE_IOS_RESULT_SCHEMA = "star-forge.native-ios.result.v1"

__all__ = tuple(name for name in globals() if not name.startswith("__"))
