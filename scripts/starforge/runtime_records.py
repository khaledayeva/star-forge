"""Cohesive Star Forge runtime extracted from the CLI facade."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import socket
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Sequence
from live_collectors import common as live_common
from .policy_data import mapping as _policy_mapping, value as _policy_value
from .runtime_support import RUNS_DIR, SCREENSHOTS_DIR, SCREENSHOT_MANIFEST, SERVER_LEASE, ForgeError, artifact_entry, blocking_items, decode_image_meta, file_sha256, now_utc, read_json, relative_to_project, slugify, source_hash, write_json
from .runtime_project import ensure_state_dirs, resolve_project, safe_release_snapshot, try_source_hash
from .runtime_plan import command_is_noop, normalize_command, task_allows_noop_verification, task_is_visual, task_plan, task_verify_command
from .runtime_preview import current_live_source_hash, is_task_scoped_live_path, live_manifest_summary, load_and_validate_live_manifest, require_raw_hash_for_artifact, validate_artifact_arg
from .runtime_store import load_run_records, write_run_record
RECORD_POLICY = _policy_value("runtime_records.POLICY")
def _finish_record(project: Path, payload: dict[str, Any], strict: bool) -> int:
    sanitized, report = live_common.redact_sensitive_values(payload)
    if not isinstance(sanitized, dict):
        raise ForgeError("run record redaction did not produce an object")
    sanitized["redaction_report"] = report
    write_run_record(project, sanitized, sanitized=True)
    print(json.dumps(sanitized, indent=2))
    return 0 if sanitized["verdict"] == RECORD_POLICY["verdicts"]["pass"] or not strict else 1
def _task_finding(name: str, task: str) -> dict[str, Any]:
    rule, message = RECORD_POLICY["findings"][name]
    return _policy_mapping("task_finding", rule=rule, file=str(RUNS_DIR),
                           task=task, message=message.format(task=task))
def _browser_problem(name: str, path: str = "", **values: object) -> dict[str, Any]:
    severity, rule, message = RECORD_POLICY["problems"][name]
    formats = {"path": path, **values}
    return browser_run_problem(message.format(**formats), severity=severity,
                               rule=rule.format(**formats), path=path)
def _report_browser_if(condition: bool, problems: list[dict[str, Any]], name: str,
                       path: str = "", **values: object) -> None:
    if condition:
        problems.append(_browser_problem(name, path, **values))
def command_output_tail(text: str, limit: int = 6000) -> str:
    return text[-limit:] if len(text) > limit else text
def command_identity_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(normalize_command(text).encode("utf-8")).hexdigest()
def cmd_verify(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    started = now_utc()
    if args.noop:
        _plan_path, tasks = task_plan(project, args.task)
        task = next((item for item in tasks if item.get("id") == args.task), None)
        if task and not task_allows_noop_verification(task):
            raise ForgeError(RECORD_POLICY["errors"]["noop_ineligible"].format(task=args.task))
        proc_returncode = 0
        stdout = args.summary or RECORD_POLICY["verify"]["noop_summary"]
        stderr = ""
    else:
        if not args.command:
            raise ForgeError(RECORD_POLICY["errors"]["verify_command_required"])
        if command_is_noop(args.command):
            raise ForgeError(RECORD_POLICY["errors"]["verify_command_noop"].format(command=args.command.strip()))
        try:
            proc = subprocess.run(args.command, cwd=str(project), shell=True, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=args.timeout, check=False)
            proc_returncode = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
        except subprocess.TimeoutExpired as exc:
            proc_returncode = RECORD_POLICY["verify"]["timeout_returncode"]
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr if isinstance(exc.stderr, str) else "")
            stderr += RECORD_POLICY["errors"]["verify_timeout"].format(timeout=args.timeout)
    snapshot, snapshot_problem = safe_release_snapshot(project)
    problems = [snapshot_problem] if snapshot_problem else []
    verdicts = RECORD_POLICY["verdicts"]
    verdict = verdicts["pass"] if proc_returncode == 0 and not problems else verdicts["fail"]
    recorded_command = args.command or RECORD_POLICY["verify"]["noop_command"]
    payload = _policy_mapping(
        "verify_record", schema=RECORD_POLICY["schemas"]["verify"],
        kind=RECORD_POLICY["kinds"]["verify"], created_at=now_utc(),
        started_at=started, project=str(project), task=args.task,
        command=recorded_command,
        noop=args.noop, returncode=proc_returncode, verdict=verdict,
        duration_timeout_seconds=args.timeout, source_snapshot=snapshot,
        stdout_tail=command_output_tail(stdout), stderr_tail=command_output_tail(stderr),
        problems=problems, summary=args.summary)
    payload["command_digest"] = command_identity_digest(recorded_command)
    return _finish_record(project, payload, args.strict)
def fresh_passing_verify(project: Path, task: dict[str, Any]) -> bool:
    """Require a passing verify bound to the declared command and current source."""
    current, hash_problem = try_source_hash(project)
    declared = normalize_command(task_verify_command(task))
    if hash_problem or current is None or not declared or command_is_noop(task_verify_command(task)):
        return False
    runs = load_run_records(project, kind="verify-run", task=task["id"])
    declared_digest = command_identity_digest(declared)
    return any(
        ((str(item.get("command_digest")) == declared_digest)
         if item.get("command_digest") else
         (not command_is_noop(str(item.get("command") or ""))
          and normalize_command(str(item.get("command") or "")) == declared))
        and item.get("verdict") == RECORD_POLICY["verdicts"]["pass"]
        and not item.get("noop") and isinstance(item.get("source_snapshot"), dict)
        and item["source_snapshot"].get("source_hash") == current for item in runs)
def has_noop_verify(project: Path, task_id: str) -> bool:
    current, hash_problem = try_source_hash(project)
    runs = load_run_records(project, kind="verify-run", task=task_id)
    return bool(not hash_problem and current is not None and any(
        item.get("verdict") == RECORD_POLICY["verdicts"]["pass"] and item.get("noop")
        and isinstance(item.get("source_snapshot"), dict)
        and item["source_snapshot"].get("source_hash") == current for item in runs))
def verify_findings(project: Path, tasks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for task in tasks:
        if task.get("status") != RECORD_POLICY["verify"]["complete_status"]:
            continue
        noop = task_allows_noop_verification(task)
        finding = ("verify_noop_missing" if noop and not has_noop_verify(project, task["id"])
                   else "verify_stale" if not noop and not fresh_passing_verify(project, task)
                   else None)
        if finding:
            findings.append(_task_finding(finding, task["id"]))
    return findings
def parse_viewport_spec(raw: str, project: Path) -> tuple[str, dict[str, Any]]:
    name_part, sep, rest = raw.partition("=")
    if not sep:
        raise ForgeError(RECORD_POLICY["errors"]["viewport_format"])
    name = slugify(name_part).lower()
    size_part, path_part = rest.split(":", 1) if ":" in rest else ("", rest)
    width = height = 0
    match = re.fullmatch(r"(\d+)x(\d+)", size_part)
    if match:
        width, height = map(int, match.groups())
    elif size_part:
        path_part = rest
    path = Path(path_part)
    candidate = path if path.is_absolute() else project / path
    entry = artifact_entry(project, candidate, kind="screenshot")
    if width and height:
        entry["width"] = width
        entry["height"] = height
    return name, entry
def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
def server_lease_origin(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    policy = RECORD_POLICY["server_lease"]
    port = parsed.port or (policy["https_port"] if parsed.scheme == "https" else policy["http_port"])
    scheme = parsed.scheme.lower() or policy["default_scheme"]
    return f"{scheme}://{host.lower()}:{port}"
def load_server_lease(project: Path) -> dict[str, Any] | None:
    path = project / SERVER_LEASE
    try:
        return read_json(path) if path.exists() else None
    except Exception:
        return None
def server_lease_payload(project: Path, args: argparse.Namespace) -> dict[str, Any]:
    policy = RECORD_POLICY["server_lease"]
    port = int(args.port) if args.port else available_port()
    base_url = args.base_url or f"{policy['default_scheme']}://{policy['default_host']}:{port}"
    origin = server_lease_origin(base_url)
    return _policy_mapping(
        "server_lease", schema=RECORD_POLICY["schemas"]["server_lease"],
        created_at=now_utc(), updated_at=now_utc(), project=str(project),
        owner=args.owner or policy["default_owner"], pid=args.pid, port=port,
        base_url=base_url, origin=origin, command=args.command or "",
        cleanup_required=True, source_hash=source_hash(project),
        runtime_asset_hash=live_common.compute_runtime_asset_hash(
            project, exclude_paths=[project / SERVER_LEASE]))
def cmd_server_lease(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    lease_path = project / SERVER_LEASE
    if args.action == "release":
        existing = load_server_lease(project)
        if lease_path.exists():
            lease_path.unlink()
        payload = {"schema": RECORD_POLICY["schemas"]["server_lease"], "action": "release", "released": bool(existing), "previous": existing}
    elif args.action == "status":
        payload = {"schema": RECORD_POLICY["schemas"]["server_lease"], "action": "status", "lease": load_server_lease(project)}
    else:
        payload = server_lease_payload(project, args)
        write_json(lease_path, payload)
    print(json.dumps(payload, indent=2))
    return 0
def write_screenshot_manifest(project: Path, *, context: dict[str, Any] | None = None) -> None:
    root = project / SCREENSHOTS_DIR
    paths = sorted(root.iterdir()) if root.exists() else []
    entries = [
        {"path": relative_to_project(path, project), "sha256": file_sha256(path),
         "bytes": path.stat().st_size, **decode_image_meta(path)}
        for path in paths if path.is_file() and path.name != SCREENSHOT_MANIFEST.name
        and path.suffix.lower() in RECORD_POLICY["browser"]["screenshot_extensions"]]
    payload = _policy_mapping(
        "screenshot_manifest", schema=RECORD_POLICY["schemas"]["screenshot_manifest"],
        created_at=now_utc(), context=context or {}, screenshots=entries)
    write_json(project / SCREENSHOT_MANIFEST, payload)
def browser_run_problem(message: str, *, severity: str = "high",
                        rule: str = "browser-run", path: str = "") -> dict[str, Any]:
    return {"severity": severity, "rule": rule, "message": message} | ({"path": path} if path else {})
def manifest_artifact_paths(manifest: dict[str, Any] | None) -> set[str]:
    if not isinstance(manifest, dict):
        return set()
    artifacts = manifest.get("artifacts")
    items = (artifacts.values() if isinstance(artifacts, dict)
             else artifacts if isinstance(artifacts, list) else ())
    paths = {str(item["path"]) for item in items
             if isinstance(item, dict) and item.get("path")}
    raw_hashes = manifest.get("raw_artifact_hashes")
    if isinstance(raw_hashes, dict):
        paths.update(str(value["path"]) if isinstance(value, dict) and value.get("path")
                     else str(key) for key, value in raw_hashes.items())
    return paths
def validate_browser_artifact_path(project: Path, entry: dict[str, Any], *, task: str,
                                   manifest: dict[str, Any] | None, manifest_paths: set[str],
                                   problems: list[dict[str, Any]]) -> Path | None:
    raw_path = str(entry.get("path") or "")
    if not raw_path:
        problems.append(_browser_problem("artifact_path_missing"))
        return None
    try:
        path = live_common.safe_project_path(project, raw_path, must_exist=False)
    except ValueError as exc:
        problems.append(_browser_problem("artifact_path_unsafe", raw_path, error=exc))
        return None
    rel = relative_to_project(path, project)
    if not is_task_scoped_live_path(project, path, task, "browser"):
        problems.append(_browser_problem("artifact_scope", rel))
    if manifest_paths and rel not in manifest_paths:
        problems.append(_browser_problem("artifact_manifest", rel))
    require_raw_hash_for_artifact(
        project, manifest, path, problems, label="browser artifact", rule="artifact-hash", attested_entry=entry)
    return path
def cmd_browser_run(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    viewports: dict[str, Any] = {}
    snapshot, snapshot_problem = safe_release_snapshot(project)
    problems = [snapshot_problem] if snapshot_problem else []
    manifest = manifest_path = browser_playwright = None
    browser_interaction_artifacts: list[tuple[Path, Any]] = []
    browser_allowed_local_origins: tuple[str, ...] = ()
    artifact_payloads: dict[str, Any] = {}
    for raw in args.viewport or []:
        name, entry = parse_viewport_spec(raw, project)
        viewports[name] = entry
        path = str(entry.get("path") or "")
        _report_browser_if(not entry.get("exists"), problems, "viewport_missing", path)
        _report_browser_if(bool(entry.get("exists") and args.strict and not entry.get("valid_image")), problems, "viewport_invalid", path)
    for raw in args.screenshot or []:
        path = Path(raw)
        candidate = path if path.is_absolute() else project / path
        name = "mobile" if "mobile" in candidate.stem.lower() else ("desktop" if "desktop" in candidate.stem.lower() else f"screenshot-{len(viewports) + 1}")
        entry = artifact_entry(project, candidate, kind="screenshot")
        viewports[name] = entry
        path = str(entry.get("path") or "")
        _report_browser_if(not entry.get("exists"), problems, "screenshot_missing", path)
        _report_browser_if(bool(entry.get("exists") and args.strict and not entry.get("valid_image")), problems, "screenshot_invalid", path)
    artifacts: dict[str, list[dict[str, Any]]] = {}
    for key, kind in RECORD_POLICY["browser"]["artifact_groups"]:
        artifacts[key] = []
        for raw in getattr(args, key) or []:
            path = Path(raw)
            candidate = path if path.is_absolute() else project / path
            entry, payload = validate_artifact_arg(
                project, str(candidate), kind, problems, require_scoped=False, require_json=True)
            entry = entry or {"kind": kind, "path": str(candidate), "exists": False}
            artifact_payloads[str(entry.get("path") or "")] = payload
            artifacts[key].append(entry)
            path = str(entry.get("path") or "")
            _report_browser_if(not entry.get("exists"), problems, "evidence_missing", path, kind=kind)
    _report_browser_if(args.degraded, problems, "browser_degraded")
    if args.require_viewports:
        for required in RECORD_POLICY["browser"]["required_viewports"]:
            _report_browser_if(required not in viewports, problems, "viewport_required", viewport=required)
    _report_browser_if(args.require_interaction and not artifacts["interaction_evidence"], problems, "interaction_required")
    _report_browser_if(args.require_console and not artifacts["console_evidence"], problems, "console_required")
    if args.strict and not snapshot_problem:
        _report_browser_if(not args.live_manifest, problems, "manifest_missing")
        manifest, manifest_path = load_and_validate_live_manifest(
            project, args.live_manifest, problems, task=args.task,
            collector=RECORD_POLICY["browser"]["collector"])
        manifest_paths = manifest_artifact_paths(manifest)
        try:
            from live_collectors import browser_playwright
        except Exception as exc:
            problems.append(_browser_problem("validator_unavailable", error=exc))
        for entry in viewports.values():
            validate_browser_artifact_path(project, entry, task=args.task, manifest=manifest, manifest_paths=manifest_paths, problems=problems)
        for key, kind in RECORD_POLICY["browser"]["validation_groups"]:
            for entry in artifacts[key]:
                path = validate_browser_artifact_path(project, entry, task=args.task, manifest=manifest, manifest_paths=manifest_paths, problems=problems)
                if path is not None and browser_playwright is not None:
                    validator = getattr(browser_playwright, f"validate_{kind}_artifact")
                    payload = artifact_payloads.get(str(entry.get("path") or ""))
                    problems.extend(validator(path, project, payload))
                    if kind == "interaction":
                        browser_interaction_artifacts.append((path, payload))
        if manifest is not None:
            summary = live_manifest_summary(manifest)
            _report_browser_if(not summary.get("url"), problems, "browser_url_missing")
            _report_browser_if(bool(summary.get("url") and args.url and str(summary["url"]) != str(args.url)), problems, "browser_url_mismatch")
    lease = None
    if not snapshot_problem and (args.strict or args.url or args.server_lease or args.require_server_lease):
        try:
            from live_collectors import browser_playwright
            parsed_url, url_problems = browser_playwright.validate_url(args.url or "")
            problems.extend(url_problems)
            if not url_problems:
                current_source = current_live_source_hash(project, problems)
                if current_source is not None:
                    runtime_hash = live_common.compute_runtime_asset_hash(
                        project, exclude_paths=[project / SERVER_LEASE])
                    _lease_path, lease, lease_problems = browser_playwright.validate_server_lease(
                        project, str(args.server_lease or ""), parsed_url,
                        current_source, runtime_hash)
                    problems.extend(lease_problems)
                    if lease:
                        browser_allowed_local_origins = (browser_playwright.normalize_origin(parsed_url), )
        except Exception as exc:
            problems.append(_browser_problem("server_lease_validation", error=exc))
    _report_browser_if(args.require_server_lease and not lease, problems, "server_lease_required")
    _report_browser_if(args.server_lease and not lease, problems, "server_lease_invalid")
    if args.strict and browser_playwright is not None:
        for path, payload in browser_interaction_artifacts:
            problems.extend(browser_playwright.validate_request_safety_artifact(
                path, project, allowed_local_origins=browser_allowed_local_origins, payload=payload))
    if viewports:
        write_screenshot_manifest(project, context={"scenario": args.scenario, "url": args.url})
    verdicts = RECORD_POLICY["verdicts"]
    verdict = verdicts["pass"] if not blocking_items(problems) else verdicts["fail"]
    payload = _policy_mapping(
        "browser_record", schema=RECORD_POLICY["schemas"]["browser"],
        kind=RECORD_POLICY["kinds"]["browser"], created_at=now_utc(),
        project=str(project), task=args.task, url=args.url, server_lease=lease,
        scenario=args.scenario, verdict=verdict, degraded=args.degraded,
        viewports=viewports, interaction_evidence=artifacts["interaction_evidence"],
        console_evidence=artifacts["console_evidence"], source_snapshot=snapshot,
        live_manifest=relative_to_project(manifest_path, project) if manifest_path else None,
        problems=problems, summary=args.summary)
    return _finish_record(project, payload, args.strict)
def passing_browser_runs(project: Path, task_id: str | None = None) -> list[dict[str, Any]]:
    current, hash_problem = try_source_hash(project)
    if hash_problem or current is None:
        return []
    return [item for item in load_run_records(
        project, kind=RECORD_POLICY["kinds"]["browser"], task=task_id)
        if item.get("verdict") == RECORD_POLICY["verdicts"]["pass"]
        and isinstance(item.get("source_snapshot"), dict)
        and item["source_snapshot"].get("source_hash") == current]
def browser_findings(project: Path, tasks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _task_finding("browser_missing", task["id"]) for task in tasks
        if task.get("status") == RECORD_POLICY["verify"]["complete_status"]
        and task_is_visual(task) and not passing_browser_runs(project, task["id"])]
__all__ = tuple(name for name in globals() if not name.startswith("__"))
