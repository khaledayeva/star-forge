"""Shared live artifact helpers for Star Forge collectors.

Collectors are suppliers only. They write task-scoped files under
`.starforge/live/<task-id>/<collector>/` and hand the manifest to the existing
proof command surfaces in `scripts/star_forge.py`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


LIVE_MANIFEST_SCHEMA = "star-forge.live-manifest.v1"
LIVE_ROOT = Path(".starforge") / "live"
RUNTIME_DIR = Path(".starforge") / "runtime"
BLOCKING_SEVERITIES = {"critical", "high", "medium"}
REQUIRED_MANIFEST_FIELDS = (
    "schema",
    "collector",
    "created_at",
    "command_argv",
    "tool_versions",
    "project",
    "task",
    "source_hash_before",
    "source_hash_after",
    "runtime_asset_hash",
    "artifacts",
    "raw_artifact_hashes",
    "summary",
    "degraded",
    "unavailable_capabilities",
    "redaction_report",
    "problems",
)

SOURCE_SNAPSHOT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".html", ".java", ".js",
    ".jsx", ".kt", ".mjs", ".py", ".rb", ".rs", ".sh", ".swift", ".ts", ".tsx",
    ".vue", ".sql", ".json", ".yaml", ".yml", ".toml", ".graphql", ".proto",
    ".prisma", ".ini", ".cfg", ".conf",
}
SOURCE_SNAPSHOT_NAMES = {
    ".env", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "requirements.txt", "pyproject.toml", "Cargo.toml", "go.mod", "go.sum",
}
IGNORED_PARTS = {
    ".codex-harness", ".git", ".hg", ".star-forge-pycache", ".starforge", ".svn",
    ".venv", "__pycache__", "build", "coverage", "dist", "node_modules", "target",
    "the-loop", "upstream",
}

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
    r"(?![A-Za-z0-9_./+=:@<>$-]*(?:your|placeholder|example|sample|changeme|dummy|xxx|<|\$\{|\.\.\.))"
    r"(?=[A-Za-z0-9_./+=:@-]*\d)"
    r"[A-Za-z0-9_./+=:@-]{16,}|"
    r"DATABASE_URL\s*=\s*['\"]?\w+://[^\s:@/'\"]+:[^\s@/'\"]{8,}@"
    r")",
    re.IGNORECASE,
)

AUTH_CREDENTIAL_RE = re.compile(
    r"\b(Authorization\s*[:=]\s*(?:Bearer|Basic)\s+)"
    r"(?![A-Za-z0-9_./+=:@<>$~-]*(?:your|placeholder|example|sample|changeme|dummy|xxx|<|\$\{|\.\.\.))"
    r"([A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)

JWT_LIKE_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)

GENERIC_TOKEN_ASSIGNMENT_RE = re.compile(
    r"\b((?:access|refresh|oauth|session)[_-]?token\s*[:=]\s*['\"]?)"
    r"(?![A-Za-z0-9_./+=:@<>$~-]*(?:your|placeholder|example|sample|changeme|dummy|xxx|<|\$\{|\.\.\.))"
    r"([A-Za-z0-9_./+=:@~-]{12,})"
    r"(['\"]?)",
    re.IGNORECASE,
)

URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
URL_TRAILING_PUNCTUATION = ".,;)]}"

SENSITIVE_KEYS = {
    "access_token",
    "accesskey",
    "authorization",
    "awsaccesskeyid",
    "cookie",
    "cookies",
    "credential",
    "key",
    "oauth_token",
    "refresh_token",
    "se",
    "session",
    "sig",
    "signature",
    "signed_headers",
    "signedheaders",
    "sp",
    "sv",
    "set-cookie",
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "api-key",
    "apikey",
    "x-api-key",
    "x-amz-credential",
    "x-amz-signature",
    "x-amz-signedheaders",
    "x-goog-credential",
    "x-goog-signature",
    "x-goog-signedheaders",
    "localstorage",
    "sessionstorage",
}
NORMALIZED_SENSITIVE_KEYS = {re.sub(r"[^a-z0-9]+", "", key.lower()) for key in SENSITIVE_KEYS}
SENSITIVE_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "cookie",
    "authorization",
    "apikey",
    "signature",
    "credential",
    "accesskey",
)


@dataclass(frozen=True)
class LiveProblem:
    severity: str
    message: str
    rule: str = "live-artifact"
    path: str = ""
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "severity": self.severity,
            "rule": self.rule,
            "message": self.message,
            "blocking": self.blocking,
        }
        if self.path:
            payload["path"] = self.path
        return payload


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitize_segment(value: str, *, fallback: str = "artifact") -> str:
    raw = str(value or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip(".-")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = fallback
    return cleaned[:90]


def live_collector_dir(project: Path, task_id: str, collector: str, *, create: bool = True) -> Path:
    root = project.resolve() / LIVE_ROOT / sanitize_segment(task_id, fallback="task") / sanitize_segment(collector, fallback="collector")
    live_root = (project.resolve() / LIVE_ROOT).resolve()
    resolved = root.resolve()
    if resolved != live_root and live_root not in resolved.parents:
        raise ValueError(f"live collector path escapes {LIVE_ROOT}")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def safe_project_path(project: Path, raw_path: str | Path, *, must_exist: bool = False) -> Path:
    if raw_path is None:
        raise ValueError("missing path")
    raw_text = str(raw_path)
    if "\0" in raw_text:
        raise ValueError("path contains a null byte")
    if raw_text.startswith("~"):
        raise ValueError("home-relative paths are not allowed in live artifacts")
    candidate = Path(raw_text)
    if not candidate.is_absolute():
        candidate = project / candidate
    project_root = project.resolve()
    resolved = candidate.resolve()
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
    proc = subprocess.run(["git", *args], cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def is_git_repo(project: Path) -> bool:
    code, _, _ = run_git(["rev-parse", "--is-inside-work-tree"], project)
    return code == 0


def snapshot_file_candidates(project: Path) -> list[Path]:
    files: list[Path] = []
    if is_git_repo(project):
        seen_rel: set[str] = set()
        for ls_args in (["ls-files"], ["ls-files", "--others", "--exclude-standard"]):
            code, out, _ = run_git(ls_args, project)
            if code != 0:
                continue
            for rel in out.splitlines():
                rel = rel.strip()
                if not rel or rel in seen_rel:
                    continue
                seen_rel.add(rel)
                path = project / rel
                if path.exists():
                    files.append(path)
    else:
        for root, dirs, names in os.walk(project):
            root_path = Path(root)
            dirs[:] = sorted(name for name in dirs if name not in IGNORED_PARTS and not (root_path / name).is_symlink())
            for name in sorted(names):
                path = root_path / name
                if path.is_file():
                    files.append(path)
    filtered: list[Path] = []
    for path in files:
        rel = project_relative(project, path)
        parts = Path(rel).parts
        if any(part in IGNORED_PARTS or part == ".starforge" for part in parts):
            continue
        if path.name in SOURCE_SNAPSHOT_NAMES or path.suffix.lower() in SOURCE_SNAPSHOT_SUFFIXES:
            filtered.append(path)
    return sorted(filtered, key=lambda item: project_relative(project, item))


def files_fingerprint(project: Path, paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        rel = project_relative(project, path)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        if path.exists() and path.is_file():
            digest.update(file_sha256(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def compute_source_hash(project: Path) -> str:
    return files_fingerprint(project, snapshot_file_candidates(project))


def runtime_asset_candidates(project: Path) -> list[Path]:
    root = project / RUNTIME_DIR
    if not root.exists():
        return []
    out: list[Path] = []
    for path in sorted(root.glob("**/*")):
        if path.is_file() and not path.is_symlink():
            out.append(path)
    return out


def compute_runtime_asset_hash(project: Path, *, exclude_paths: Sequence[str | Path] | None = None) -> str:
    excluded: set[Path] = set()
    for raw in exclude_paths or []:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = project / candidate
        try:
            excluded.add(candidate.resolve())
        except OSError:
            continue
    candidates = [path for path in runtime_asset_candidates(project) if path.resolve() not in excluded]
    return files_fingerprint(project, candidates)


def _image_meta(path: Path) -> dict[str, Any]:
    try:
        head = path.read_bytes()[:26]
    except OSError:
        return {"valid_image": False}
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(head) >= 24 and head[12:16] == b"IHDR":
            width = int.from_bytes(head[16:20], "big")
            height = int.from_bytes(head[20:24], "big")
            return {"valid_image": width > 0 and height > 0, "image_format": "png", "decoded_width": width, "decoded_height": height}
        return {"valid_image": False, "image_format": "png"}
    if head.startswith(b"\xff\xd8\xff"):
        return {"valid_image": True, "image_format": "jpeg"}
    return {"valid_image": False}


def artifact_record(project: Path, path: str | Path, *, kind: str = "artifact", must_exist: bool = False) -> dict[str, Any]:
    try:
        resolved = safe_project_path(project, path, must_exist=must_exist)
        rel = project_relative(project, resolved)
        entry: dict[str, Any] = {"kind": kind, "path": rel, "exists": resolved.exists()}
        if resolved.exists() and resolved.is_file():
            entry.update({"sha256": file_sha256(resolved), "bytes": resolved.stat().st_size})
            if kind in {"screenshot", "image"}:
                entry.update(_image_meta(resolved))
        elif resolved.exists() and resolved.is_dir():
            entry["directory"] = True
        return entry
    except ValueError as exc:
        return {"kind": kind, "path": sanitize_external_path(Path(str(path))), "exists": False, "problem": str(exc)}


def hash_artifacts(project: Path, artifacts: Mapping[str, str | Path] | Sequence[str | Path]) -> dict[str, dict[str, Any]]:
    if isinstance(artifacts, Mapping):
        items = artifacts.items()
    else:
        items = [(str(idx), path) for idx, path in enumerate(artifacts)]
    out: dict[str, dict[str, Any]] = {}
    for name, raw in items:
        record = artifact_record(project, raw)
        if record.get("exists") and record.get("sha256"):
            out[str(name)] = {"path": record["path"], "sha256": record["sha256"], "bytes": record.get("bytes")}
    return out


def blocking_problem(message: str, *, rule: str = "live-artifact", path: str = "", severity: str = "high") -> dict[str, Any]:
    return LiveProblem(severity=severity, message=message, rule=rule, path=path, blocking=True).to_dict()


def sensitive_key_name(raw: str) -> bool:
    key_norm = re.sub(r"[^a-z0-9]+", "", str(raw or "").lower())
    if key_norm in NORMALIZED_SENSITIVE_KEYS:
        return True
    return any(marker in key_norm for marker in SENSITIVE_KEY_MARKERS)


def _redact_url_parts(raw_url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return raw_url, 0
    redactions = 0
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
        redactions += 1
    query_pairs: list[tuple[str, str]] = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if sensitive_key_name(key):
            query_pairs.append((key, "[REDACTED_SECRET]"))
            redactions += 1
        else:
            query_pairs.append((key, value))
    query = urllib.parse.urlencode(query_pairs, doseq=True)
    fragment = parsed.fragment
    if "=" in parsed.fragment:
        fragment_pairs: list[tuple[str, str]] = []
        for key, value in urllib.parse.parse_qsl(parsed.fragment, keep_blank_values=True):
            if sensitive_key_name(key):
                fragment_pairs.append((key, "[REDACTED_SECRET]"))
                redactions += 1
            else:
                fragment_pairs.append((key, value))
        fragment = urllib.parse.urlencode(fragment_pairs, doseq=True)
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query and query, fragment)), redactions


def _redact_urls(text: str) -> tuple[str, int]:
    total = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal total
        raw = match.group(0)
        suffix = ""
        while raw and raw[-1] in URL_TRAILING_PUNCTUATION:
            suffix = raw[-1] + suffix
            raw = raw[:-1]
        redacted, count = _redact_url_parts(raw)
        total += count
        return redacted + suffix

    return URL_RE.sub(repl, text), total


def _redact_string(text: str, report: dict[str, int]) -> str:
    out, url_count = _redact_urls(text)
    out, auth_count = AUTH_CREDENTIAL_RE.subn(r"\1[REDACTED_SECRET]", out)
    out, generic_count = GENERIC_TOKEN_ASSIGNMENT_RE.subn(r"\1[REDACTED_SECRET]\3", out)
    out, jwt_count = JWT_LIKE_RE.subn("[REDACTED_SECRET]", out)
    out, secret_count = SECRET_RE.subn("[REDACTED_SECRET]", out)
    report["secret_values"] += url_count + auth_count + generic_count + jwt_count + secret_count
    home = str(Path.home())
    if home and home in out:
        out = out.replace(home, "[REDACTED_HOME]")
        report["home_paths"] += 1
    out, user_count = re.subn(r"/Users/[^/\s]+", "[REDACTED_HOME]", out)
    report["home_paths"] += user_count
    out, tmp_count = re.subn(r"(?:/private)?/tmp/[A-Za-z0-9_./-]+", "[REDACTED_TMP]", out)
    report["temp_paths"] += tmp_count
    out, env_count = re.subn(
        r"\b([A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|KEY))=([^\s'\"]+)",
        r"\1=[REDACTED_ENV_VALUE]",
        out,
    )
    report["env_values"] += env_count
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
        if isinstance(item, dict):
            return {str(key): clean(child, str(key)) for key, child in item.items()}
        return item

    return clean(value), dict(report)


def _artifact_records_from_input(project: Path, artifacts: Mapping[str, Any] | Sequence[Any]) -> list[dict[str, Any]]:
    if isinstance(artifacts, Mapping):
        raw_items = artifacts.items()
    else:
        raw_items = [(str(idx), item) for idx, item in enumerate(artifacts)]
    records: list[dict[str, Any]] = []
    for name, raw in raw_items:
        if isinstance(raw, Mapping):
            path = raw.get("path")
            kind = str(raw.get("kind") or name)
        else:
            path = raw
            kind = str(name)
        if path is None:
            records.append({"kind": kind, "path": "", "exists": False, "problem": "missing artifact path"})
        else:
            records.append(artifact_record(project, path, kind=kind))
    return records


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
    root = live_collector_dir(project, task, collector)
    records = _artifact_records_from_input(project, artifacts or {})
    raw_hashes = {
        str(record["path"]): {"sha256": record["sha256"], "bytes": record.get("bytes")}
        for record in records
        if record.get("exists") and record.get("sha256")
    }
    problem_items = [item.to_dict() if isinstance(item, LiveProblem) else dict(item) for item in (problems or [])]
    payload = {
        "schema": LIVE_MANIFEST_SCHEMA,
        "collector": sanitize_segment(collector, fallback="collector"),
        "created_at": now_utc(),
        "command_argv": [str(item) for item in command_argv],
        "tool_versions": dict(tool_versions or {}),
        "project": {"path": ".", "name": project.resolve().name},
        "task": str(task),
        "source_hash_before": source_hash_before or compute_source_hash(project),
        "source_hash_after": source_hash_after or compute_source_hash(project),
        "runtime_asset_hash": runtime_asset_hash or compute_runtime_asset_hash(project),
        "artifacts": records,
        "raw_artifact_hashes": raw_hashes,
        "summary": summary or {},
        "degraded": bool(degraded),
        "unavailable_capabilities": [str(item) for item in (unavailable_capabilities or [])],
        "redaction_report": {},
        "problems": problem_items,
    }
    redacted, report = redact_sensitive_values(payload)
    if isinstance(redacted, dict):
        redacted["redaction_report"] = report
    path = root / sanitize_segment(manifest_name, fallback="manifest.json")
    path.write_text(json.dumps(redacted, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def validate_manifest_payload(payload: Any) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return [blocking_problem("manifest must be a JSON object", rule="manifest-shape")]
    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in payload:
            problems.append(blocking_problem(f"manifest missing required field `{field}`", rule="manifest-field"))
    if payload.get("schema") != LIVE_MANIFEST_SCHEMA:
        problems.append(blocking_problem(f"manifest schema must be {LIVE_MANIFEST_SCHEMA}", rule="manifest-schema"))
    if not isinstance(payload.get("command_argv"), list):
        problems.append(blocking_problem("manifest command_argv must be an array", rule="manifest-shape"))
    if not isinstance(payload.get("tool_versions"), dict):
        problems.append(blocking_problem("manifest tool_versions must be an object", rule="manifest-shape"))
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, (dict, list)):
        problems.append(blocking_problem("manifest artifacts must be an object or array", rule="manifest-shape"))
    else:
        values = artifacts.values() if isinstance(artifacts, dict) else artifacts
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                problems.append(blocking_problem(f"manifest artifact {index + 1} must be an object", rule="manifest-shape"))
                continue
            if not isinstance(item.get("path"), str) or not item.get("path"):
                problems.append(blocking_problem(f"manifest artifact {index + 1} must include a path", rule="manifest-shape"))
    if not isinstance(payload.get("raw_artifact_hashes"), dict):
        problems.append(blocking_problem("manifest raw_artifact_hashes must be an object", rule="manifest-shape"))
    if not isinstance(payload.get("degraded"), bool):
        problems.append(blocking_problem("manifest degraded must be a boolean", rule="manifest-shape"))
    if not isinstance(payload.get("unavailable_capabilities"), list):
        problems.append(blocking_problem("manifest unavailable_capabilities must be an array", rule="manifest-shape"))
    if not isinstance(payload.get("problems"), list):
        problems.append(blocking_problem("manifest problems must be an array", rule="manifest-shape"))
    return problems
