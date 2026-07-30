"""Shared live artifact helpers for Star Forge collectors.
Collectors are suppliers only. They write task-scoped files under
`.starforge/live/<task-id>/<collector>/` and hand the manifest to the existing
proof command surfaces in `scripts/star_forge.py`.
"""
from __future__ import annotations
from live_collectors.policy_data import policy_bindings
from live_collectors.provider_engine import failed_checks, render_descriptor
from starforge import safe_io
from starforge.sensitive import sensitive_key_name
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
LIVE_ROOT = Path(".starforge") / "live"
RUNTIME_DIR = Path(".starforge") / "runtime"
LIVE_EVIDENCE_FILENAME = "evidence.v2.json"
globals().update(policy_bindings(
    "common", "CONSTANTS", "REQUIRED_MANIFEST_FIELDS", "LIVE_MANIFEST_TEMPLATE", "MANIFEST_CHECKS",
    "FALLBACK_IGNORED_PARTS", "PATH_REDACTION_RULES",
    "REDACTION_PATTERNS", "SENSITIVE_KEYS", "SENSITIVE_KEY_MARKERS",
    "STRING_REDACTION_RULES",
))
globals().update(CONSTANTS)
VCS_INTERNAL_PARTS = {".git", ".hg", ".svn"}
STAR_FORGE_STATE_PARTS = {".starforge", "the-loop"}
SECRET_RE = re.compile(REDACTION_PATTERNS["secret"], re.IGNORECASE)
AUTH_CREDENTIAL_RE = re.compile(REDACTION_PATTERNS["auth"], re.IGNORECASE)
JWT_LIKE_RE = re.compile(REDACTION_PATTERNS["jwt"])
GENERIC_TOKEN_ASSIGNMENT_RE = re.compile(REDACTION_PATTERNS["generic_token"], re.IGNORECASE)
URL_RE = re.compile(REDACTION_PATTERNS["url"], re.IGNORECASE)
URL_TRAILING_PUNCTUATION = ".,;)]}"
@dataclass(frozen=True)
class LiveProblem:
    severity: str
    message: str
    rule: str = "live-artifact"
    path: str = ""
    blocking: bool = True
    def to_dict(self) -> dict[str, Any]:
        return problem(**vars(self))
class SourceSnapshotError(ValueError):
    """Source or Git state could not be represented safely."""
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
    safe_io.atomic_write_text(safe_io.infer_root(path), path, str(output))
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
def assert_collector_project_safe(project: Path) -> Path:
    """Return the resolved project after rejecting unsafe Star Forge state."""
    if project.is_symlink():
        raise ValueError(f"refusing symlinked collector project root: {project}")
    project_root = project.resolve()
    if not project_root.is_dir():
        raise ValueError(f"collector project is not a directory: {project}")
    try:
        safe_io.assert_tree_no_symlinks(project_root, ".starforge")
    except OSError as exc:
        raise ValueError(f"cannot safely inspect Star Forge state: {exc}") from exc
    return project_root
def live_collector_dir(project: Path, task_id: str, collector: str, *, create: bool = True) -> Path:
    project_root = assert_collector_project_safe(project)
    live_root = project_root / LIVE_ROOT
    root = live_root / sanitize_segment(task_id, fallback="task") / sanitize_segment(collector, fallback="collector")
    if root.parent.parent != live_root:
        raise ValueError(f"live collector path escapes {LIVE_ROOT}")
    if create:
        safe_io.make_directory(project_root, root)
        assert_collector_project_safe(project_root)
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
    if must_exist:
        try:
            safe_io.read_bytes(project_root, resolved, limit=0)
        except OSError:
            try:
                exists = safe_io.directory_exists(project_root, resolved)
            except OSError as exc:
                raise ValueError(f"path is unsafe: {raw_text}: {exc}") from exc
            if not exists:
                raise ValueError(f"path does not exist: {raw_text}")
    return resolved
def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))
def project_relative(project: Path, path: Path) -> str:
    project_root = project.resolve()
    try:
        return str(_lexical_absolute(path).relative_to(project_root))
    except ValueError:
        pass
    try:
        return str(path.resolve().relative_to(project_root))
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
def file_sha256(path: Path, *, root: Path | None = None) -> str:
    return safe_io.digest_size(root or safe_io.infer_root(path), path)[0]
def run_git(args: Sequence[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(["git", *args], cwd=os.fspath(cwd), text=False,
                          capture_output=True, check=False)
    return proc.returncode, os.fsdecode(proc.stdout), os.fsdecode(proc.stderr)
def run_git_path_records(args: Sequence[str], cwd: Path) -> tuple[int, list[str], str]:
    code, output, error = run_git(args, cwd)
    if code != 0:
        return code, [], error
    if output and not output.endswith("\0"):
        raise SourceSnapshotError("Git path output is not NUL terminated")
    records = output[:-1].split("\0") if output else []
    if "" in records:
        raise SourceSnapshotError("Git path output contains an empty record")
    return code, records, error
def is_git_repo(project: Path) -> bool:
    return run_git(["rev-parse", "--is-inside-work-tree"], project)[0] == 0
def path_has_source_hash_excluded_part(rel_path: str | Path) -> bool:
    parts = PurePosixPath(os.fspath(rel_path)).parts
    return bool(parts) and (parts[0] in STAR_FORGE_STATE_PARTS or any(part in VCS_INTERNAL_PARTS for part in parts))
def source_hash_dirty_entries(project: Path, entries: Sequence[str]) -> list[str]:
    return [path for path in entries if path and not path_has_source_hash_excluded_part(path)]
def git_status_path(path: str) -> str:
    return path
def git_head(project: Path) -> str:
    code, out, _ = run_git(["rev-parse", "HEAD"], project)
    return out.strip() if code == 0 else ""
def git_status(project: Path) -> list[str]:
    code, records, error = run_git_path_records(
        ["status", "--porcelain=v1", "-z", "--no-renames",
         "--untracked-files=all", "--", "."],
        project,
    )
    if code != 0:
        raise SourceSnapshotError("Git status unavailable: " + error.strip())
    if any(len(record) < 4 or record[2] != " " or not record[3:]
           for record in records):
        raise SourceSnapshotError("Git status output contains a malformed record")
    return [record[3:] for record in records]
def source_dirty_entries(project: Path) -> list[str]:
    return source_hash_dirty_entries(project, git_status(project))
def source_tree_clean_at_head(project: Path) -> bool:
    try:
        return bool(git_head(project)) and not source_dirty_entries(project)
    except ValueError:
        return False
def source_snapshot_rel_paths(project: Path) -> set[str]:
    return {project_relative(project, path) for path in snapshot_file_candidates(project)}
def dirty_paths_missing_from_source_snapshot(project: Path) -> list[str]:
    snapshot_paths = source_snapshot_rel_paths(project)
    return [path for path in source_dirty_entries(project) if path not in snapshot_paths]
def _source_symlink_component(project: Path, path: Path) -> Path | None:
    project_root = project.resolve()
    candidate = _lexical_absolute(path)
    try:
        relative = candidate.relative_to(project_root)
    except ValueError:
        return None
    current = project_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return current
    return None
def _validated_source_candidate(project: Path, path: Path) -> Path | None:
    symlink = _source_symlink_component(project, path)
    if symlink is not None:
        raise SourceSnapshotError(
            f"source snapshot refuses symlink: {project_relative(project, symlink)}"
        )
    try:
        return path if stat.S_ISREG(_lexical_absolute(path).lstat().st_mode) else None
    except OSError:
        return None
def snapshot_file_candidates(project: Path) -> list[Path]:
    git_repo = is_git_repo(project)
    if git_repo:
        code, paths, error = run_git_path_records(
            ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            project,
        )
        if code != 0:
            raise SourceSnapshotError(
                "source snapshot cannot enumerate Git paths: "
                f"{error.strip() or f'exit {code}'}")
        files = []
        for rel in paths:
            if not path_has_source_hash_excluded_part(rel):
                path = project / rel
                _validated_source_candidate(project, path)
                files.append(path)
    else:
        files = []
        project_root = project.resolve()
        for root, dirs, names in os.walk(project):
            root_path = Path(root)
            kept_dirs = []
            for name in sorted(dirs):
                candidate = root_path / name
                if name in FALLBACK_IGNORED_PARTS and (
                    name not in STAR_FORGE_STATE_PARTS or _lexical_absolute(root_path) == project_root
                ):
                    continue
                if candidate.is_symlink():
                    raise SourceSnapshotError(
                        f"source snapshot refuses symlink: {project_relative(project, candidate)}"
                    )
                kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in sorted(names):
                candidate = _validated_source_candidate(project, root_path / name)
                if candidate is not None:
                    files.append(candidate)
    return sorted(files, key=lambda item: project_relative(project, item))
def files_fingerprint(project: Path, paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        symlink = _source_symlink_component(project, path)
        if symlink is not None:
            raise SourceSnapshotError(
                f"source fingerprint refuses symlink: {project_relative(project, symlink)}"
            )
        rel = project_relative(project, path)
        candidate = _validated_source_candidate(project, path)
        content_hash = file_sha256(candidate, root=project) if candidate else ""
        executable = b"x" if candidate and candidate.lstat().st_mode & 0o111 else b"-"
        kind = b"file" if candidate else b"missing-or-nonregular"
        digest.update(os.fsencode(rel) + b"\0" + kind + b"\0" + executable + b"\0"
                      + content_hash.encode("ascii") + b"\0")
    return digest.hexdigest()
def git_index_fingerprint(project: Path) -> str:
    code, records, error = run_git_path_records(["ls-files", "--stage", "-z", "--cached"], project)
    if code != 0:
        raise SourceSnapshotError(
            "source snapshot cannot inspect Git index: " + (error.strip() or f"exit {code}"))
    digest = hashlib.sha256()
    for record in records:
        header, separator, rel = record.partition("\t")
        fields = header.split()
        if not separator or len(fields) != 3 or fields[2] != "0":
            raise SourceSnapshotError("Git index contains a malformed or unmerged record")
        mode, object_id, _stage = fields
        if not re.fullmatch(r"[0-7]{6}", mode) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", object_id):
            raise SourceSnapshotError("Git index contains invalid mode or object identity")
        if path_has_source_hash_excluded_part(rel):
            continue
        worktree_hash = ""
        link_root = project / rel
        if mode != "160000" and _validated_source_candidate(project, link_root):
            continue
        symlink = _source_symlink_component(project, link_root)
        if symlink is not None:
            raise SourceSnapshotError(
                f"source snapshot refuses symlink: {project_relative(project, symlink)}")
        if mode == "160000" and link_root.is_dir():
            code, top, _ = run_git(["rev-parse", "--show-toplevel"], link_root)
            if code == 0 and Path(top.strip()).resolve() == link_root.resolve():
                worktree_hash = compute_source_hash(link_root)
        digest.update(os.fsencode(rel) + b"\0" + mode.encode("ascii") + b"\0"
                      + object_id.encode("ascii") + b"\0" + worktree_hash.encode("ascii") + b"\0")
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
    content = files_fingerprint(project, snapshot_file_candidates(project))
    if not is_git_repo(project):
        return content
    index = git_index_fingerprint(project)
    return hashlib.sha256(f"star-forge.git-source.v2\0{index}\0{content}".encode()).hexdigest()
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
def _image_meta(head: bytes) -> dict[str, Any]:
    if not head.startswith(b"\x89PNG\r\n\x1a\n"):
        return {"valid_image": True, "image_format": "jpeg"} if head.startswith(b"\xff\xd8\xff") else {"valid_image": False}
    if len(head) < 24 or head[12:16] != b"IHDR":
        return {"valid_image": False, "image_format": "png"}
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    return {"valid_image": width > 0 and height > 0, "image_format": "png", "decoded_width": width, "decoded_height": height}
def artifact_record(project: Path, path: str | Path, *, kind: str = "artifact", must_exist: bool = False) -> dict[str, Any]:
    try:
        resolved = safe_project_path(project, path)
        rel = project_relative(project, resolved)
        try:
            head, digest, byte_count = safe_io.snapshot(
                project,
                resolved,
                prefix_limit=26 if kind in {"screenshot", "image"} else 0,
            )
        except FileNotFoundError:
            if must_exist:
                raise ValueError(f"path does not exist: {path}")
            return {"kind": kind, "path": rel, "exists": False}
        except OSError:
            if safe_io.directory_exists(project, resolved):
                return {"kind": kind, "path": rel, "exists": True, "directory": True}
            raise
        entry: dict[str, Any] = {
            "kind": kind,
            "path": rel,
            "exists": True,
            "sha256": digest,
            "bytes": byte_count,
        }
        if kind in {"screenshot", "image"}:
            entry.update(_image_meta(head))
        return entry
    except (OSError, ValueError) as exc:
        return {"kind": kind, "path": sanitize_external_path(Path(str(path))), "exists": False, "problem": str(exc)}
def blocking_problem(message: str, *, rule: str = "live-artifact", path: str = "", severity: str = "high") -> dict[str, Any]:
    return problem(message, rule=rule, path=path, severity=severity)
def _redact_params(raw: str) -> tuple[str, int]:
    pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True)
    sensitive = {key for key, _value in pairs if sensitive_key_name(key)}
    redacted = [
        (key, value if value in {"[REDACTED]", "[REDACTED_SECRET]"}
         else "[REDACTED_SECRET]" if key in sensitive else value)
        for key, value in pairs]
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
    result = urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query and query, fragment))
    return (result.replace("%5BREDACTED%5D", "[REDACTED]")
            .replace("%5BREDACTED_SECRET%5D", "[REDACTED_SECRET]")), redactions
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
        if sensitive_key_name(key_hint) and isinstance(item, str):
            report["sensitive_keys"] += 1
            return "[REDACTED]"
        if isinstance(item, str):
            return _redact_string(item, report)
        if isinstance(item, list):
            return [clean(child, key_hint) for child in item]
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
    path = write_json(path, redacted, redact=False)[0]
    from starforge import evidence
    try:
        envelope = evidence.adapt_v1_manifest(redacted)
        evidence.write_envelope(
            path.parent / LIVE_EVIDENCE_FILENAME,
            envelope,
            project_root=project,
            verify_artifacts=True,
        )
    except evidence.EvidenceError:
        pass
    return path
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
