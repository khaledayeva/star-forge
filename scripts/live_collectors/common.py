"""Shared live artifact helpers for Star Forge collectors.

Collectors are suppliers only. They write task-scoped files under
`.starforge/live/<task-id>/<collector>/` and hand the manifest to the existing
proof command surfaces in `scripts/star_forge.py`.
"""

from __future__ import annotations

from live_collectors.policy_data import policy_bindings
from live_collectors.provider_engine import failed_checks, render_descriptor

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


LIVE_ROOT = Path(".starforge") / "live"
RUNTIME_DIR = Path(".starforge") / "runtime"
globals().update(policy_bindings(
    "common", "CONSTANTS", "REQUIRED_MANIFEST_FIELDS", "LIVE_MANIFEST_TEMPLATE", "MANIFEST_CHECKS",
    "FALLBACK_IGNORED_PARTS", "PATH_REDACTION_RULES",
    "REDACTION_PATTERNS", "SENSITIVE_KEYS", "SENSITIVE_KEY_MARKERS",
    "STRING_REDACTION_RULES",
))
globals().update(CONSTANTS)
VCS_INTERNAL_PARTS = {".git", ".hg", ".svn"}
STAR_FORGE_STATE_PARTS = {".starforge", "the-loop"}
SOURCE_HASH_EXCLUDED_PARTS = VCS_INTERNAL_PARTS | STAR_FORGE_STATE_PARTS

SECRET_RE = re.compile(REDACTION_PATTERNS["secret"], re.IGNORECASE)
AUTH_CREDENTIAL_RE = re.compile(REDACTION_PATTERNS["auth"], re.IGNORECASE)
JWT_LIKE_RE = re.compile(REDACTION_PATTERNS["jwt"])
GENERIC_TOKEN_ASSIGNMENT_RE = re.compile(REDACTION_PATTERNS["generic_token"], re.IGNORECASE)
URL_RE = re.compile(REDACTION_PATTERNS["url"], re.IGNORECASE)
URL_TRAILING_PUNCTUATION = ".,;)]}"

NORMALIZED_SENSITIVE_KEYS = {re.sub(r"[^a-z0-9]+", "", key.lower()) for key in SENSITIVE_KEYS}


@dataclass(frozen=True)
class LiveProblem:
    severity: str
    message: str
    rule: str = "live-artifact"
    path: str = ""
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        return problem(**vars(self))


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def read_text(path: Path, default: str = "") -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else default


def write_json(
    path: Path,
    payload: Any,
    *,
    redact: bool = True,
    preserve_fields: Sequence[str] = (),
) -> tuple[Path, dict[str, Any]]:
    output, report = redact_sensitive_values(payload) if redact else (payload, {})
    if redact and isinstance(payload, Mapping) and isinstance(output, dict):
        output.update({field: payload[field] for field in preserve_fields if field in payload})
    write_text(path, json.dumps(output, indent=2, sort_keys=True) + "\n", redact=False)
    return path, report


def write_text(path: Path, text: str, *, redact: bool = True) -> tuple[Path, dict[str, Any]]:
    output, report = redact_sensitive_values(text) if redact else (text, {})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(output), encoding="utf-8")
    return path, report


def merge_reports(*reports: Mapping[str, Any]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for report in reports:
        for key, value in report.items():
            if isinstance(value, bool):
                continue
            try:
                merged[str(key)] = merged.get(str(key), 0) + int(value)
            except (TypeError, ValueError):
                continue
    return merged


def problem(
    message: str,
    *,
    rule: str,
    path: str = "",
    severity: str = "high",
    blocking: bool = True,
    include_empty_path: bool = False,
) -> dict[str, Any]:
    payload = {"severity": severity, "rule": rule, "message": message, "blocking": blocking}
    if path or include_empty_path:
        payload["path"] = path
    return payload


def sanitize_segment(value: str, *, fallback: str = "artifact") -> str:
    raw = str(value or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip(".-")
    return (fallback if not cleaned or cleaned in {".", ".."} else cleaned)[:90]


def live_collector_dir(project: Path, task_id: str, collector: str, *, create: bool = True) -> Path:
    live_root = (project.resolve() / LIVE_ROOT).resolve()
    root = live_root / sanitize_segment(task_id, fallback="task") / sanitize_segment(collector, fallback="collector")
    if live_root not in root.resolve().parents:
        raise ValueError(f"live collector path escapes {LIVE_ROOT}")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def safe_project_path(project: Path, raw_path: str | Path, *, must_exist: bool = False) -> Path:
    if raw_path is None:
        raise ValueError("missing path")
    raw_text = str(raw_path)
    if "\0" in raw_text or raw_text.startswith("~"):
        message = "path contains a null byte" if "\0" in raw_text else "home-relative paths are not allowed in live artifacts"
        raise ValueError(message)
    project_root = project.resolve()
    candidate = Path(raw_text)
    resolved = (candidate if candidate.is_absolute() else project_root / candidate).resolve()
    if resolved != project_root and project_root not in resolved.parents:
        raise ValueError(f"path escapes project: {raw_text}")
    if must_exist and not resolved.exists():
        raise ValueError(f"path does not exist: {raw_text}")
    return resolved


def project_relative(project: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(project.resolve()))
    except ValueError:
        return sanitize_external_path(path)


def project_cli_arg(project: Path, *, cwd: Path | None = None) -> str:
    """Return a stable value for proof command --project arguments."""
    resolved_project = project.expanduser().resolve()
    resolved_cwd = (cwd or Path.cwd()).expanduser().resolve()
    return "." if resolved_project == resolved_cwd else str(resolved_project)


def sanitize_external_path(path: Path) -> str:
    name = sanitize_segment(path.name or "external")
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    return f"[external]/{name}-{digest}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_git(args: Sequence[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def is_git_repo(project: Path) -> bool:
    return run_git(["rev-parse", "--is-inside-work-tree"], project)[0] == 0


def path_has_source_hash_excluded_part(rel_path: str | Path) -> bool:
    return any(part in SOURCE_HASH_EXCLUDED_PARTS for part in Path(rel_path).parts)


def source_hash_dirty_entries(project: Path, entries: Sequence[str]) -> list[str]:
    return [
        line for line in entries
        if (path := git_status_path(line)) and not path_has_source_hash_excluded_part(path)
    ]


def git_status_path(line: str) -> str:
    path = line[3:] if len(line) > 3 else line.strip()
    path = path.strip().strip('"')
    return path.split(" -> ", 1)[-1].strip().strip('"')


def git_head(project: Path) -> str:
    code, out, _ = run_git(["rev-parse", "HEAD"], project)
    return out.strip() if code == 0 else ""


def git_status(project: Path) -> list[str]:
    code, out, _ = run_git(["status", "--short", "--untracked-files=all", "--", "."], project)
    return (
        [line for line in out.splitlines() if line.strip()]
        if code == 0 else ["?? <git status unavailable>"]
    )


def source_dirty_entries(project: Path) -> list[str]:
    return source_hash_dirty_entries(project, git_status(project))


def source_tree_clean_at_head(project: Path) -> bool:
    return bool(git_head(project)) and not source_dirty_entries(project)


def source_snapshot_rel_paths(project: Path) -> set[str]:
    return {project_relative(project, path) for path in snapshot_file_candidates(project)}


def dirty_paths_missing_from_source_snapshot(project: Path) -> list[str]:
    snapshot_paths = source_snapshot_rel_paths(project)
    return [
        line for line in source_dirty_entries(project)
        if (git_status_path(line) and git_status_path(line) not in snapshot_paths)
    ]


def snapshot_file_candidates(project: Path) -> list[Path]:
    git_repo = is_git_repo(project)
    if git_repo:
        listings = (
            run_git(["ls-files"], project),
            run_git(["ls-files", "--others", "--exclude-standard"], project),
        )
        files = [
            path for code, out, _ in listings if code == 0
            for rel in out.splitlines()
            if rel.strip() and not path_has_source_hash_excluded_part(rel.strip())
            and (path := project / rel.strip()).exists() and path.is_file()
        ]
    else:
        files = []
        for root, dirs, names in os.walk(project):
            root_path = Path(root)
            dirs[:] = sorted(name for name in dirs if name not in FALLBACK_IGNORED_PARTS and not (root_path / name).is_symlink())
            files.extend(path for name in sorted(names) if (path := root_path / name).is_file())
    return sorted(files, key=lambda item: project_relative(project, item))


source_snapshot_includes = Path.is_file


def files_fingerprint(project: Path, paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        rel = project_relative(project, path)
        content_hash = file_sha256(path) if path.exists() and path.is_file() else ""
        digest.update(f"{rel}\0{content_hash}\0".encode("utf-8"))
    return digest.hexdigest()


def tree_sha256(path: Path | None) -> str:
    return "" if path is None or not path.is_dir() else files_fingerprint(path, sorted(
        item for item in path.glob("**/*") if item.is_file() and not item.is_symlink()
    ))


def trusted_python_command(
    command: Sequence[str],
    *,
    script_path: Path,
) -> list[str]:
    actual = [str(item) for item in command]
    if actual and Path(actual[0]).name in {"python", "python3"}:
        actual[0] = os.fspath(Path(os.sys.executable))
    if len(actual) > 1 and actual[1] == "scripts/star_forge.py":
        actual[1] = str(script_path)
    return actual


def run_trusted_command(
    command: Sequence[str],
    *,
    cwd: Path,
    script_path: Path,
) -> dict[str, Any]:
    actual = trusted_python_command(command, script_path=script_path)
    proc = subprocess.run(
        actual, cwd=str(cwd), shell=False, text=True, capture_output=True, check=False,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "command_argv": actual,
    }


def hash_artifacts(project: Path, artifacts: Mapping[str, str | Path] | Sequence[str | Path]) -> dict[str, dict[str, Any]]:
    items = artifacts.items() if isinstance(artifacts, Mapping) else enumerate(artifacts)
    records = ((str(name), artifact_record(project, raw)) for name, raw in items)
    return {
        name: {"path": record["path"], "sha256": record["sha256"], "bytes": record.get("bytes")}
        for name, record in records
        if record.get("exists") and record.get("sha256")
    }


def compute_source_hash(project: Path) -> str:
    return files_fingerprint(project, snapshot_file_candidates(project))


def compute_runtime_asset_hash(project: Path, *, exclude_paths: Sequence[str | Path] | None = None) -> str:
    excluded: set[Path] = set()
    for raw in exclude_paths or []:
        try:
            excluded.add(safe_project_path(project, raw))
        except (OSError, ValueError):
            continue
    root = project / RUNTIME_DIR
    candidates = [] if not root.exists() else [
        path for path in sorted(root.glob("**/*"))
        if path.is_file() and not path.is_symlink() and path.resolve() not in excluded
    ]
    return files_fingerprint(project, candidates)


def _image_meta(path: Path) -> dict[str, Any]:
    try:
        head = path.read_bytes()[:26]
    except OSError:
        return {"valid_image": False}
    if not head.startswith(b"\x89PNG\r\n\x1a\n"):
        return {"valid_image": True, "image_format": "jpeg"} if head.startswith(b"\xff\xd8\xff") else {"valid_image": False}
    if len(head) < 24 or head[12:16] != b"IHDR":
        return {"valid_image": False, "image_format": "png"}
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    return {"valid_image": width > 0 and height > 0, "image_format": "png", "decoded_width": width, "decoded_height": height}


def artifact_record(project: Path, path: str | Path, *, kind: str = "artifact", must_exist: bool = False) -> dict[str, Any]:
    try:
        resolved = safe_project_path(project, path, must_exist=must_exist)
        rel = project_relative(project, resolved)
        exists = resolved.exists()
        entry: dict[str, Any] = {"kind": kind, "path": rel, "exists": exists}
        if exists and resolved.is_file():
            entry.update({"sha256": file_sha256(resolved), "bytes": resolved.stat().st_size})
            if kind in {"screenshot", "image"}:
                entry.update(_image_meta(resolved))
        elif exists and resolved.is_dir():
            entry["directory"] = True
        return entry
    except ValueError as exc:
        return {"kind": kind, "path": sanitize_external_path(Path(str(path))), "exists": False, "problem": str(exc)}


def blocking_problem(message: str, *, rule: str = "live-artifact", path: str = "", severity: str = "high") -> dict[str, Any]:
    return problem(message, rule=rule, path=path, severity=severity)


def sensitive_key_name(raw: str) -> bool:
    key_norm = re.sub(r"[^a-z0-9]+", "", str(raw or "").lower())
    return key_norm in NORMALIZED_SENSITIVE_KEYS or any(
        marker in key_norm for marker in SENSITIVE_KEY_MARKERS
    )


def _redact_params(raw: str) -> tuple[str, int]:
    pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True)
    sensitive = {key for key, _value in pairs if sensitive_key_name(key)}
    redacted = [(key, "[REDACTED_SECRET]" if key in sensitive else value) for key, value in pairs]
    return urllib.parse.urlencode(redacted, doseq=True), sum(key in sensitive for key, _ in pairs)


def _redact_url_parts(raw_url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return raw_url, 0
    redactions = 0
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
        redactions += 1
    query, query_count = _redact_params(parsed.query)
    fragment, fragment_count = (
        _redact_params(parsed.fragment) if "=" in parsed.fragment else (parsed.fragment, 0)
    )
    redactions += query_count + fragment_count
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query and query, fragment)), redactions


def _redact_urls(text: str) -> tuple[str, int]:
    total = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal total
        raw = match.group(0)
        trimmed = raw.rstrip(URL_TRAILING_PUNCTUATION)
        raw, suffix = trimmed, raw[len(trimmed):]
        redacted, count = _redact_url_parts(raw)
        total += count
        return redacted + suffix

    return URL_RE.sub(repl, text), total


def _redact_string(text: str, report: dict[str, int]) -> str:
    out, url_count = _redact_urls(text)
    report["secret_values"] += url_count
    for regex_name, replacement, counter in STRING_REDACTION_RULES:
        out, count = globals()[regex_name].subn(replacement, out)
        report[counter] += count
    home = str(Path.home())
    if home and home in out:
        out = out.replace(home, "[REDACTED_HOME]")
        report["home_paths"] += 1
    for pattern, replacement, counter in PATH_REDACTION_RULES:
        out, count = re.subn(pattern, replacement, out)
        report[counter] += count
    return out


def redact_sensitive_values(value: Any) -> tuple[Any, dict[str, Any]]:
    report = {"secret_values": 0, "sensitive_keys": 0, "home_paths": 0, "temp_paths": 0, "env_values": 0}

    def clean(item: Any, key_hint: str = "") -> Any:
        if sensitive_key_name(key_hint):
            report["sensitive_keys"] += 1
            return "[REDACTED]"
        if isinstance(item, str):
            return _redact_string(item, report)
        if isinstance(item, list):
            return [clean(child) for child in item]
        return (
            {str(key): clean(child, str(key)) for key, child in item.items()}
            if isinstance(item, dict) else item
        )

    return clean(value), dict(report)


def _artifact_records_from_input(project: Path, artifacts: Mapping[str, Any] | Sequence[Any]) -> list[dict[str, Any]]:
    def record(name: str, raw: Any) -> dict[str, Any]:
        mapping = raw if isinstance(raw, Mapping) else {}
        path = mapping.get("path") if mapping else raw
        kind = str(mapping.get("kind") or name)
        return (
            artifact_record(project, path, kind=kind) if path is not None else
            {"kind": kind, "path": "", "exists": False, "problem": "missing artifact path"}
        )

    items = artifacts.items() if isinstance(artifacts, Mapping) else enumerate(artifacts)
    return [record(str(name), raw) for name, raw in items]


def write_live_manifest(
    project: Path,
    *,
    task: str,
    collector: str,
    command_argv: Sequence[str],
    tool_versions: Mapping[str, Any] | None = None,
    artifacts: Mapping[str, Any] | Sequence[Any] | None = None,
    summary: Mapping[str, Any] | str | None = None,
    degraded: bool = False,
    unavailable_capabilities: Sequence[str] | None = None,
    problems: Sequence[Mapping[str, Any] | LiveProblem] | None = None,
    source_hash_before: str | None = None,
    source_hash_after: str | None = None,
    runtime_asset_hash: str | None = None,
    manifest_name: str = "manifest.json",
) -> Path:
    records = _artifact_records_from_input(project, artifacts or {})
    raw_hashes = {
        str(record["path"]): {"sha256": record["sha256"], "bytes": record.get("bytes")}
        for record in records
        if record.get("exists") and record.get("sha256")
    }
    problem_items = [item.to_dict() if isinstance(item, LiveProblem) else dict(item) for item in (problems or [])]
    payload = render_descriptor(LIVE_MANIFEST_TEMPLATE, {
        "collector": sanitize_segment(collector, fallback="collector"), "created_at": now_utc(),
        "command_argv": [str(item) for item in command_argv],
        "tool_versions": dict(tool_versions or {}),
        "project": {"path": ".", "name": project.resolve().name}, "task": str(task),
        "source_hash_before": source_hash_before or compute_source_hash(project),
        "source_hash_after": source_hash_after or compute_source_hash(project),
        "runtime_asset_hash": runtime_asset_hash or compute_runtime_asset_hash(project),
        "artifacts": records, "raw_artifact_hashes": raw_hashes, "summary": summary or {},
        "degraded": bool(degraded),
        "unavailable_capabilities": [str(item) for item in (unavailable_capabilities or [])],
        "problems": problem_items,
    })
    redacted, report = redact_sensitive_values(payload)
    redacted["redaction_report"] = report
    path = live_collector_dir(project, task, collector) / sanitize_segment(manifest_name, fallback="manifest.json")
    return write_json(path, redacted, redact=False)[0]


def validate_manifest_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return [blocking_problem("manifest must be a JSON object", rule="manifest-shape")]
    problems = [
        blocking_problem(f"manifest missing required field `{field}`", rule="manifest-field")
        for field in REQUIRED_MANIFEST_FIELDS if field not in payload
    ]
    for message in failed_checks(payload, MANIFEST_CHECKS):
        rule = "manifest-schema" if message.startswith("manifest schema") else "manifest-shape"
        problems.append(blocking_problem(message, rule=rule))
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, (dict, list)):
        problems.append(blocking_problem("manifest artifacts must be an object or array", rule="manifest-shape"))
    else:
        for index, item in enumerate(artifacts.values() if isinstance(artifacts, dict) else artifacts):
            if not isinstance(item, dict):
                problems.append(blocking_problem(f"manifest artifact {index + 1} must be an object", rule="manifest-shape"))
                continue
            if not isinstance(item.get("path"), str) or not item.get("path"):
                problems.append(blocking_problem(f"manifest artifact {index + 1} must include a path", rule="manifest-shape"))
    return problems
