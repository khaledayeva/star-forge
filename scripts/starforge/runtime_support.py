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
"""Deterministic helpers for the Star Forge Codex plugin (v0.3 "Forge Loop").

The loop is plan -> build -> review -> done, with automatic amend re-entry on
post-done drift. Gates consume only evidence the model cannot author about itself:
captured command output (verify), screenshot bytes (browser-run), git tree state,
and reviewer freshness attestations. Reviewer findings are load-bearing -- they
feed the fix queue that `done` consumes -- so review cannot be back-filled. Hooks
observe and re-anchor; they never deny. See docs/forge-loop.md.
"""
SCRIPT_DIR = Path(__file__).resolve().parents[1]

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
SUPPORT_POLICY = _policy_value("runtime_support.POLICY")
globals().update(SUPPORT_POLICY["constants"])
globals().update({name: Path(value) for name, value in SUPPORT_POLICY["paths"].items()})
globals().update(SUPPORT_POLICY["sets"])
FINDING_SEVERITY_RANK = SUPPORT_POLICY["finding_severity_rank"]
SOURCE_SNAPSHOT_NAMES = set(SUPPORT_POLICY["source_snapshot_names"])
TEXT_SUFFIXES = set(SUPPORT_POLICY["text_suffixes"])
VISUAL_TASK_RE = re.compile(SUPPORT_POLICY["visual_task_pattern"], re.IGNORECASE)
AI_RESIDUAL_PATTERNS = [
    (re.compile(pattern, re.IGNORECASE), rule, severity)
    for pattern, rule, severity in SUPPORT_POLICY["ai_residual_patterns"]]
PNG_MAGIC = bytes.fromhex(SUPPORT_POLICY["image_magic_hex"]["png"])
JPEG_MAGIC = bytes.fromhex(SUPPORT_POLICY["image_magic_hex"]["jpeg"])

def _install_aliases() -> None:
    sources = {
        "live_common": live_common,
        "review_policy": adaptive_review_policy,
        "project_quality": project_quality,
    }
    for group, aliases in SUPPORT_POLICY["aliases"].items():
        globals().update({name: getattr(sources[group], target) for name, target in aliases.items()})

_install_aliases()
STOPWORDS = _policy_value('runtime_support.STOPWORDS')
SOURCE_SNAPSHOT_SUFFIXES = _policy_value('runtime_support.SOURCE_SNAPSHOT_SUFFIXES')
SOURCE_SNAPSHOT_NAME_PREFIXES = _policy_value('runtime_support.SOURCE_SNAPSHOT_NAME_PREFIXES')
INFRASTRUCTURE_TASK_PARTS = _policy_value('runtime_support.INFRASTRUCTURE_TASK_PARTS')
VISUAL_SOURCE_SUFFIXES = _policy_value('runtime_support.VISUAL_SOURCE_SUFFIXES')
VISUAL_SOURCE_PARTS = _policy_value('runtime_support.VISUAL_SOURCE_PARTS')
IGNORED_PARTS = _policy_value('runtime_support.IGNORED_PARTS')
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

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")

def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(read_text(path))
    if not isinstance(payload, dict):
        raise _error("json_object", path=path)
    return payload

def write_json(path: Path, payload: dict[str, Any]) -> None:
    write_text(path, json.dumps(redact(payload), **SUPPORT_POLICY["json_format"]) + "\n")

def write_json_if_changed(path: Path, payload: dict[str, Any]) -> bool:
    text = json.dumps(redact(payload), **SUPPORT_POLICY["json_format"]) + "\n"
    if path.exists():
        try:
            if read_text(path) == text:
                return False
        except OSError:
            pass
    write_text(path, text)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise _error("symlink_append", path=path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redact(payload), sort_keys=True) + "\n")

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
        with path.open("rb") as handle:
            head = handle.read(26)
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

def git_head(project: Path) -> str | None:
    if not is_git_repo(project):
        return None
    code, out, _ = run_git(SUPPORT_POLICY["git_commands"]["head"], project)
    return out.strip() if code == 0 and out.strip() else None

def repo_root(cwd: Path) -> Path:
    code, out, _ = run_git(SUPPORT_POLICY["git_commands"]["root"], cwd)
    if code == 0 and out.strip():
        return Path(out.strip()).resolve()
    return cwd.resolve()

def ensure_git_repo(project: Path) -> bool:
    # A project nested inside a PARENT repo (work/<slug> isolation) must get its
    # own repository; otherwise every git-backed gate points at the user's repo.
    if (project / ".git").exists():
        return False
    code, _, err = run_git(SUPPORT_POLICY["git_commands"]["init"], project)
    if code != 0:
        raise _error("git_init", error=err.strip())
    return True

def git_status(project: Path) -> list[str]:
    if not is_git_repo(project):
        return []
    code, out, _ = run_git(SUPPORT_POLICY["git_commands"]["status"], project)
    if code != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]

def source_dirty_entries(entries: Sequence[str]) -> list[str]:
    """Filter Star Forge's own state writes out of dirty-tree checks."""
    return live_common.source_hash_dirty_entries(Path.cwd(), entries)

def git_changed_files(project: Path) -> list[Path]:
    if not is_git_repo(project):
        return []
    code, out, _ = run_git(SUPPORT_POLICY["git_commands"]["changed"], project)
    files = [project / line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
    code, out, _ = run_git(SUPPORT_POLICY["git_commands"]["untracked"], project)
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
    lines = read_text(path).splitlines() if path.exists() else []
    changed: list[str] = []
    for entry in entries:
        if entry in lines:
            continue
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(entry)
        changed.append(entry)
    if changed:
        write_text(path, "\n".join(lines) + "\n")
    return changed

def template_text(name: str) -> str:
    path = plugin_root() / "templates" / name
    if not path.exists():
        raise _error("template_missing", path=path)
    return read_text(path)

def is_dotenv(path: Path) -> bool:
    # Path('.env').suffix is '' and Path('.env.local').suffix is '.local', so the
    # tree scan missed literal dotenv files entirely. Match them by name.
    return path.name == ".env" or path.name.startswith(".env.")

def is_text_file(path: Path) -> bool:
    return (path.name in SUPPORT_POLICY["text_names"] or is_dotenv(path) or path.suffix.lower() in TEXT_SUFFIXES or
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

def release_snapshot(project: Path) -> dict[str, Any]:
    source_files = snapshot_file_candidates(project)
    blueprint = project / BLUEPRINT_FILE
    plan = project / PLAN_FILE
    return policy_record(
        "release_snapshot",
        created_at=now_utc(),
        git_head=git_head(project),
        source_hash=files_fingerprint(project, source_files),
        source_files=[relative_to_project(path, project) for path in source_files],
        blueprint_hash=file_sha256(blueprint) if blueprint.exists() else None,
        plan_hash=file_sha256(plan) if plan.exists() else None,
    )

def release_snapshot_unavailable(project: Path, problems: Sequence[dict[str, Any]]) -> dict[str, Any]:
    blueprint = project / BLUEPRINT_FILE
    plan = project / PLAN_FILE
    return policy_record(
        "release_snapshot_unavailable",
        created_at=now_utc(),
        git_head=git_head(project),
        source_hash=None,
        source_hash_unavailable=True,
        problems=list(problems),
        source_files=[],
        blueprint_hash=file_sha256(blueprint) if blueprint.exists() else None,
        plan_hash=file_sha256(plan) if plan.exists() else None,
    )

def safe_release_snapshot(project: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    problem = source_hash_unavailable_problem(project)
    if problem:
        return release_snapshot_unavailable(project, [problem]), problem
    try:
        return release_snapshot(project), None
    except (PermissionError, OSError) as exc:
        problem = source_hash_exception_problem(exc)
        return release_snapshot_unavailable(project, [problem]), problem

def artifact_entry(project: Path, path: Path, *, kind: str) -> dict[str, Any]:
    candidate = path if path.is_absolute() else project / path
    entry: dict[str, Any] = {"kind": kind, "path": relative_to_project(candidate, project), "exists": candidate.exists()}
    if candidate.exists() and candidate.is_file():
        entry.update({"sha256": file_sha256(candidate), "bytes": candidate.stat().st_size})
        if kind == "screenshot":
            entry.update(decode_image_meta(candidate))
    return entry

def blocking_items(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if str(item.get("severity", "")).lower() in BLOCKING_SEVERITIES]

def finding_problem(finding: dict[str, Any]) -> dict[str, Any]:
    location = finding.get("file", "unknown")
    rule = finding.get("rule", "finding")
    message = finding.get("message") or finding.get("evidence") or "blocking finding"
    return {"severity": finding.get("severity", "high"), "message": f"{rule} at {location}: {message}"}

__all__ = tuple(name for name in globals() if not name.startswith("__"))
