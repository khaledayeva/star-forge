"""Cohesive Star Forge runtime extracted from the CLI facade."""
from __future__ import annotations
from .policy_data import record as policy_record, value as _policy_value
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence
from live_collectors import common as live_common
from starforge import quality as project_quality
from starforge import review_policy as adaptive_review_policy
from starforge import safe_io
SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
SUPPORT_POLICY = _policy_value("runtime_support.POLICY")
_POLICY_EXPORT_GROUPS = {group: frozenset(SUPPORT_POLICY[group]) for group in ("constants", "paths", "sets")}
_ALIAS_PROVIDERS = {"live_common": live_common, "project_quality": project_quality, "review_policy": adaptive_review_policy}
_PROVIDER_EXPORTS = {_ALIAS_PROVIDERS[group]: exports for group, exports in SUPPORT_POLICY["aliases"].items()}
BLUEPRINT_FILE = SUPPORT_POLICY["constants"]["BLUEPRINT_FILE"]
PLAN_FILE = SUPPORT_POLICY["constants"]["PLAN_FILE"]
BLOCKING_SEVERITIES = SUPPORT_POLICY["sets"]["BLOCKING_SEVERITIES"]
SECRET_PRONE_SUFFIXES = SUPPORT_POLICY["sets"]["SECRET_PRONE_SUFFIXES"]
FINDING_SEVERITY_RANK = SUPPORT_POLICY["finding_severity_rank"]
TEXT_SUFFIXES = set(SUPPORT_POLICY["text_suffixes"])
AI_RESIDUAL_PATTERNS = [(re.compile(pattern, re.IGNORECASE), rule, severity) for pattern, rule, severity in SUPPORT_POLICY["ai_residual_patterns"]]
PNG_MAGIC = bytes.fromhex(SUPPORT_POLICY["image_magic_hex"]["png"])
JPEG_MAGIC = bytes.fromhex(SUPPORT_POLICY["image_magic_hex"]["jpeg"])
def __getattr__(name: str) -> Any:
    """Resolve explicitly declared compatibility names from their owner."""
    for group, names in _POLICY_EXPORT_GROUPS.items():
        if name in names:
            value = SUPPORT_POLICY[group][name]
            return Path(value) if group == "paths" else value
    for provider, exports in _PROVIDER_EXPORTS.items():
        if name in exports:
            return getattr(provider, exports[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
STOPWORDS = _policy_value('runtime_support.STOPWORDS')
SECRET_RE = re.compile(
    r"("
    r"\bsk-[A-Za-z0-9_-]{20,}|"
    r"AKIA[A-Z0-9]{16}|"
    r"ghp_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}|"
    r"npm_[A-Za-z0-9]{20,}|"
    r"pypi-[A-Za-z0-9_-]{20,}|"
    r"BEGIN [A-Z ]*PRIVATE KEY|"
    r"(?:OPENAI_API_KEY|GITHUB_TOKEN|GH_TOKEN|AWS_SECRET_ACCESS_KEY|SUPABASE_SERVICE_ROLE_KEY)"
    r"\s*=\s*['\"]?"
    r"(?![A-Za-z0-9_./+=:@<>$-]*(?:your|placeholder|example|sample|changeme|change[-_]me|replace[-_]?me|dummy|xxx|<|\$\{|\.\.\.))"
    r"(?=[A-Za-z0-9_./+=:@-]*\d)"
    r"[A-Za-z0-9_./+=:@-]{16,}|"
    r"DATABASE_URL\s*=\s*['\"]?\w+://[^\s:@/'\"]+:[^\s@/'\"]{8,}@"
    r")",
    re.IGNORECASE,
)
class ForgeError(Exception):
    """A deterministic Star Forge helper error."""
def _error(name: str, **values: object) -> ForgeError:
    return ForgeError(SUPPORT_POLICY["messages"][name].format(**values))
def timestamp_slug() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return slug[:90] or "artifact"
def plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]
def read_text(path: Path, *, root: Path | None = None) -> str:
    return safe_io.read_text(root or safe_io.infer_root(path), path)
def write_text(path: Path, text: str, *, root: Path | None = None) -> None:
    safe_io.atomic_write_text(root or safe_io.infer_root(path), path, text)
def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(read_text(path))
    if not isinstance(payload, dict):
        raise _error("json_object", path=path)
    return payload
def write_json(path: Path, payload: dict[str, Any], *, root: Path | None = None) -> None:
    write_text(path, json.dumps(redact(payload), **SUPPORT_POLICY["json_format"]) + "\n", root=root)
def write_json_if_changed(
    path: Path,
    payload: dict[str, Any],
    *,
    root: Path | None = None,
) -> bool:
    text = json.dumps(redact(payload), **SUPPORT_POLICY["json_format"]) + "\n"
    if path.exists():
        try:
            if read_text(path, root=root) == text:
                return False
        except OSError:
            pass
    write_text(path, text, root=root)
    return True
def strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: strip_volatile(item) for key, item in value.items()
                if key not in SUPPORT_POLICY["volatile_fields"]}
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    return value
def write_json_stable(path: Path, payload: dict[str, Any]) -> bool:
    """Write payload only when it differs from the existing file ignoring timestamps."""
    if path.exists():
        try:
            existing = read_json(path)
        except Exception:
            existing = {}
        if existing and strip_volatile(redact(existing)) == strip_volatile(redact(payload)):
            return False
    write_json(path, payload)
    return True
def stable_json_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    safe_io.append_text(
        safe_io.infer_root(path),
        path,
        json.dumps(redact(payload), sort_keys=True) + "\n",
    )
def jsonl_payloads(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return [payload for line in read_text(path).splitlines() if line.strip()
                for payload in [json.loads(line)] if isinstance(payload, dict)]
    except Exception:
        return []
def redact(value: Any) -> Any:
    if isinstance(value, str):
        return SECRET_RE.sub("[REDACTED_SECRET]", value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SUPPORT_POLICY["redacted_fields"]:
                cleaned[str(key)] = "[REDACTED]"
            else:
                cleaned[str(key)] = redact(item)
        return cleaned
    return value
def decode_image_meta(path: Path) -> dict[str, Any]:
    """Cheap image validation: magic bytes plus PNG IHDR dimensions."""
    try:
        head = safe_io.read_bytes(safe_io.infer_root(path), path, limit=26)
    except OSError:
        return {"valid_image": False}
    if head.startswith(PNG_MAGIC):
        if len(head) >= 24 and head[12:16] == b"IHDR":
            width = int.from_bytes(head[16:20], "big")
            height = int.from_bytes(head[20:24], "big")
            return {"valid_image": width > 0 and height > 0, "image_format": "png", "decoded_width": width, "decoded_height": height}
        return {"valid_image": False, "image_format": "png"}
    if head.startswith(JPEG_MAGIC):
        return {"valid_image": True, "image_format": "jpeg"}
    return {"valid_image": False}
def _run_git(command: str, project: Path) -> tuple[int, str, str]:
    return live_common.run_git(SUPPORT_POLICY["git_commands"][command], project)
def git_head(project: Path) -> str | None:
    if not live_common.is_git_repo(project):
        return None
    code, out, _ = _run_git("head", project)
    return out.strip() if code == 0 and out.strip() else None
def repo_root(cwd: Path) -> Path:
    code, out, _ = _run_git("root", cwd)
    if code == 0 and out.strip():
        return Path(out.strip()).resolve()
    return cwd.resolve()
def ensure_git_repo(project: Path) -> bool:
    # A project nested inside a PARENT repo (work/<slug> isolation) must get its
    # own repository; otherwise every git-backed gate points at the user's repo.
    if (project / ".git").exists():
        return False
    code, _, err = _run_git("init", project)
    if code != 0:
        raise _error("git_init", error=err.strip())
    return True
def git_status(project: Path) -> list[str]:
    if not live_common.is_git_repo(project):
        return []
    code, out, _ = _run_git("status", project)
    if code != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]
def source_dirty_entries(entries: Sequence[str]) -> list[str]:
    """Filter Star Forge's own state writes out of dirty-tree checks."""
    return live_common.source_hash_dirty_entries(Path.cwd(), entries)
def git_changed_files(project: Path) -> list[Path]:
    if not live_common.is_git_repo(project):
        return []
    code, out, _ = _run_git("changed", project)
    files = [project / line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
    code, out, _ = _run_git("untracked", project)
    if code == 0:
        files.extend(project / line.strip() for line in out.splitlines() if line.strip())
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in files:
        resolved = path.resolve()
        if resolved not in seen and path.exists():
            seen.add(resolved)
            unique.append(path)
    return unique
def relative_to_project(path: Path, project: Path) -> str:
    try:
        return str(path.resolve().relative_to(project.resolve()))
    except ValueError:
        return str(path)
def ensure_gitignore_entries(project: Path, entries: Sequence[str]) -> list[str]:
    path = project / ".gitignore"
    try:
        lines = read_text(path, root=project).splitlines()
    except FileNotFoundError:
        lines = []
    changed: list[str] = []
    for entry in entries:
        if entry in lines:
            continue
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(entry)
        changed.append(entry)
    if changed:
        write_text(path, "\n".join(lines) + "\n", root=project)
    return changed
def template_text(name: str) -> str:
    path = plugin_root() / "templates" / name
    if not path.exists():
        raise _error("template_missing", path=path)
    return read_text(path)
def is_text_file(path: Path) -> bool:
    dotenv = path.name == ".env" or path.name.startswith(".env.")
    return (path.name in SUPPORT_POLICY["text_names"] or dotenv or path.suffix.lower() in TEXT_SUFFIXES or
            path.suffix.lower() in SECRET_PRONE_SUFFIXES)
def iter_project_files(project: Path, *, all_files: bool = False) -> Iterable[Path]:
    if all_files:
        yield from project_quality.iter_project_files(project)
    else:
        yield from git_changed_files(project)
def scan_paths(paths: Iterable[Path], project: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists() or not path.is_file() or not is_text_file(path):
            continue
        try:
            rel = relative_to_project(path, project)
            if project_quality.exclusion_reason(path, project):
                continue
            text = read_text(path)
        except (OSError, UnicodeDecodeError):
            continue
        for idx, line in enumerate(text.splitlines(), start=1):
            if SECRET_RE.search(line):
                findings.append({"severity": "critical", "rule": "secret-material", "file": rel, "line": idx, "evidence": "line contains likely secret material"})
            for pattern, rule, severity in AI_RESIDUAL_PATTERNS:
                if pattern.search(line):
                    findings.append({"severity": severity, "rule": rule, "file": rel, "line": idx, "evidence": line.strip()[:160]})
    return findings
def tree_clean_for_commit_binding(project: Path) -> bool:
    return not source_dirty_entries(git_status(project))
def _release_hashes(project: Path) -> dict[str, str | None]:
    paths = {"blueprint_hash": project / BLUEPRINT_FILE, "plan_hash": project / PLAN_FILE}
    return {name: live_common.file_sha256(path, root=project) if path.exists() else None for name, path in paths.items()}
def release_snapshot(project: Path) -> dict[str, Any]:
    source_files = live_common.snapshot_file_candidates(project)
    return policy_record(
        "release_snapshot",
        created_at=live_common.now_utc(),
        git_head=git_head(project),
        source_hash=live_common.files_fingerprint(project, source_files),
        source_files=[relative_to_project(path, project) for path in source_files],
        **_release_hashes(project),
    )
def release_snapshot_unavailable(project: Path, problems: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return policy_record(
        "release_snapshot_unavailable",
        created_at=live_common.now_utc(),
        git_head=git_head(project),
        source_hash=None,
        source_hash_unavailable=True,
        problems=list(problems),
        source_files=[],
        **_release_hashes(project),
    )
def artifact_entry(project: Path, path: Path, *, kind: str) -> dict[str, Any]:
    return live_common.artifact_record(project, path, kind=kind)
def blocking_items(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if str(item.get("severity", "")).lower() in BLOCKING_SEVERITIES]
def finding_problem(finding: dict[str, Any]) -> dict[str, Any]:
    location = finding.get("file", "unknown")
    rule = finding.get("rule", "finding")
    message = finding.get("message") or finding.get("evidence") or "blocking finding"
    return {"severity": finding.get("severity", "high"), "message": f"{rule} at {location}: {message}"}
