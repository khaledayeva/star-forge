#!/usr/bin/env python3
"""Deterministic helpers for the Star Forge Codex plugin (v0.3 "Forge Loop").

The loop is plan -> build -> review -> done, with automatic amend re-entry on
post-done drift. Gates consume only evidence the model cannot author about itself:
captured command output (verify), screenshot bytes (browser-run), git tree state,
and reviewer freshness attestations. Reviewer findings are load-bearing -- they
feed the fix queue that `done` consumes -- so review cannot be back-filled. Hooks
observe and re-anchor; they never deny. See docs/forge-loop.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import os
import plistlib
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from live_collectors import common as live_common
from live_collectors import native_macos as native_macos_collector
from starforge import contracts as project_contracts
from starforge import doctor as installation_doctor
from starforge import lifecycle as project_lifecycle
from starforge import review_policy as adaptive_review_policy


SF_VERSION = "0.3.0"
PLUGIN_NAME = "star-forge"
BLUEPRINT_FILE = "Blueprint.md"
PLAN_FILE = "Plan.md"
LOOP_DIR = Path("the-loop")
STATE_DIR = Path(".starforge")
STATE_SUBDIR = STATE_DIR / "state"
RUNS_DIR = STATE_DIR / "runs"
TASKS_DIR = STATE_DIR / "tasks"
FINAL_DIR = STATE_DIR / "final"
REVIEWS_DIR = STATE_DIR / "reviews"
SCREENSHOTS_DIR = STATE_DIR / "screenshots"
RUNTIME_DIR = STATE_DIR / "runtime"
CANONICAL_STATE = STATE_DIR / "state.json"
PROJECT_MANIFEST = STATE_DIR / "project.json"
SOURCE_PROFILE_FILE = "StarForge.profile.json"
SOURCE_PROFILE_SCHEMA = "star-forge.source-profile.v1"
PROOF_FILE = FINAL_DIR / "proof.json"
FINAL_SUMMARY = FINAL_DIR / "summary.md"
LEDGER_FILE = STATE_DIR / "ledger.jsonl"
HOOK_EVENTS = STATE_SUBDIR / "hook-events.jsonl"
SUBAGENT_EVENTS = STATE_SUBDIR / "subagent-events.jsonl"
AUTO_CONTINUE_FILE = STATE_SUBDIR / "auto-continue.json"
CHANGED_FILES = STATE_SUBDIR / "changed-files.jsonl"
HANDOFF_ARTIFACT = STATE_SUBDIR / "handoff-artifact.json"
WAIVES_FILE = STATE_SUBDIR / "waives.jsonl"
INCIDENTS_FILE = STATE_SUBDIR / "incidents.jsonl"
HOOK_TRUST_NOTICE_FILE = STATE_SUBDIR / "hook-trust-notice.json"
SERVER_LEASE = RUNTIME_DIR / "server.json"
SCREENSHOT_MANIFEST = SCREENSHOTS_DIR / "manifest.json"
AGENT_NAME_PREFIX = "starforge-"
REVIEW_PROFILE_ROLES = adaptive_review_policy.LEGACY_PROFILE_ROLES
KNOWN_REVIEW_ROLES = adaptive_review_policy.ALL_REVIEW_ROLES
REVIEW_ROLE_LENSES = adaptive_review_policy.ROLE_LENSES
MAX_AUTO_CONTINUES = 3
STAR_FORGE_STATE_VERSION = "3.0"
LEARNINGS_HOME = Path.home() / ".star-forge" / "learnings"

VALID_STATUSES = {"queued", "ready", "in_progress", "blocked", "reviewing", "complete"}
VALID_MODES = {"solo", "delegate", "docs"}
BLOCKING_SEVERITIES = {"critical", "high", "medium"}
FINDING_SEVERITIES = {"critical", "high", "medium", "low", "info"}
FINDING_SEVERITY_RANK = {"info": 1, "low": 2, "medium": 3, "high": 4, "critical": 5}
STOPWORDS = {
    "a", "an", "and", "app", "add", "build", "change", "continue", "create",
    "finish", "fix", "for", "from", "game", "improve", "implement", "make", "mvp",
    "new", "of", "polish", "project", "prototype", "software", "the", "to",
    "update", "with",
}
SOURCE_SNAPSHOT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".html", ".java", ".js",
    ".jsx", ".kt", ".mjs", ".py", ".rb", ".rs", ".sh", ".swift", ".ts", ".tsx",
    ".vue", ".sql", ".json", ".yaml", ".yml", ".toml", ".graphql", ".proto",
    ".prisma", ".ini", ".cfg", ".conf", ".mk",
}
SOURCE_SNAPSHOT_NAMES = {
    ".dockerignore", ".env", ".gitignore", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "requirements.txt", "pyproject.toml", "Cargo.toml", "go.mod", "go.sum",
    BLUEPRINT_FILE, PLAN_FILE,
    ".npmrc", ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs", ".pnp.cjs",
    ".pnp.loader.mjs", ".node-version", ".nvmrc", "npm-shrinkwrap.json",
    "pnpm-workspace.yaml", "bun.lock", "bun.lockb", "deno.lock",
    "Dockerfile", "Containerfile", "Makefile", "GNUmakefile", "BSDmakefile",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "Procfile", "Caddyfile",
}
SOURCE_SNAPSHOT_NAME_PREFIXES = (
    ".env.",
    "Dockerfile.",
    "Containerfile.",
    "Makefile.",
    "Procfile.",
    "Caddyfile.",
    "docker-compose.",
    "compose.",
)
VISUAL_TASK_RE = re.compile(
    r"\b(ui|ux|visual|browser|screenshot|responsive|frontend|front-end|"
    r"layout|styling|css|web ?page|viewport)\b",
    re.IGNORECASE,
)
INFRASTRUCTURE_TASK_PARTS = frozenset(
    {
        "agents",
        "collectors",
        "config",
        "docs",
        "fixtures",
        "live_collectors",
        "skills",
        "templates",
        "test",
        "tests",
    }
)
INFRASTRUCTURE_DOCUMENT_SUFFIXES = frozenset({".md", ".mdx", ".rst"})
VISUAL_SOURCE_SUFFIXES = frozenset(
    {
        ".astro",
        ".css",
        ".html",
        ".htm",
        ".jsx",
        ".less",
        ".sass",
        ".scss",
        ".svelte",
        ".tsx",
        ".vue",
        ".xib",
        ".storyboard",
    }
)
VISUAL_SOURCE_PARTS = frozenset(
    {
        "components",
        "frontend",
        "layouts",
        "pages",
        "screens",
        "ui",
        "views",
    }
)
TEXT_SUFFIXES = {
    "." + "env", ".astro", ".c", ".cc", ".cfg", ".conf", ".cpp", ".cs", ".css",
    ".go", ".h", ".html", ".java", ".js", ".json", ".jsx", ".kt", ".mjs", ".md",
    ".php", ".plist", ".py", ".rb", ".rs", ".sh", ".swift", ".toml", ".ts", ".tsx",
    ".txt", ".vue", ".xml", ".yaml", ".yml",
}
IGNORED_PARTS = {
    ".codex-harness", ".git", ".hg", ".star-forge-pycache", ".starforge", ".svn",
    ".venv", "__pycache__", "build", "coverage", "dist", "node_modules", "target",
    "the-loop", "upstream",
}

# Placeholder-tolerant secret detector (validated 24/24 against the postmortem
# matrix). The name-based clause requires a digit in the value and rejects any
# placeholder word anywhere in the value, so `OPENAI_API_KEY=sk-your-key-here`
# and `=sk-proj-placeholder123` pass while real keys are still caught by the
# format clauses and the digit+length name clause. DATABASE_URL only trips when a
# credential is embedded.
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

AI_RESIDUAL_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\bas an ai\b", re.IGNORECASE), "ai-disclaimer", "medium"),
    (re.compile(r"\bTODO:\s*(implement|wire|fix later|placeholder)\b", re.IGNORECASE), "unfinished-todo", "medium"),
    (re.compile(r"\b(?:lorem ipsum|dummy data|fake data)\b", re.IGNORECASE), "placeholder-content", "low"),
    (re.compile(r"\b(?:quick " + "hack" + r"|temporary " + "hack" + r"|" + "hacky" + r" workaround)\b", re.IGNORECASE), "hack-language", "medium"),
    (re.compile(r"\bconsole\.log\s*\(", re.IGNORECASE), "console-log", "low"),
]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


class ForgeError(Exception):
    """A deterministic Star Forge helper error."""


# --------------------------------------------------------------------------- io


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp_slug() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return slug[:90] or "artifact"


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(read_text(path))
    if not isinstance(payload, dict):
        raise ForgeError(f"{path} must contain a JSON object")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    write_text(path, json.dumps(redact(payload), indent=2, sort_keys=True) + "\n")


def write_json_if_changed(path: Path, payload: dict[str, Any]) -> bool:
    text = json.dumps(redact(payload), indent=2, sort_keys=True) + "\n"
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
        return {key: strip_volatile(item) for key, item in value.items() if key not in {"created_at", "updated_at"}}
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
        raise ForgeError(f"Refusing to append to symlink: {path}")
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
            if str(key).lower() in {"prompt", "raw_prompt", "secret", "token", "password", "api_key"}:
                cleaned[str(key)] = "[REDACTED]"
            else:
                cleaned[str(key)] = redact(item)
        return cleaned
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


# -------------------------------------------------------------------------- git


def run_git(args: Sequence[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(["git", *args], cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def git_head(project: Path) -> str | None:
    if not is_git_repo(project):
        return None
    code, out, _ = run_git(["rev-parse", "HEAD"], project)
    return out.strip() if code == 0 and out.strip() else None


def is_git_repo(cwd: Path) -> bool:
    code, _, _ = run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    return code == 0


def repo_root(cwd: Path) -> Path:
    code, out, _ = run_git(["rev-parse", "--show-toplevel"], cwd)
    if code == 0 and out.strip():
        return Path(out.strip()).resolve()
    return cwd.resolve()


def ensure_git_repo(project: Path) -> bool:
    # A project nested inside a PARENT repo (work/<slug> isolation) must get its
    # own repository; otherwise every git-backed gate points at the user's repo.
    if (project / ".git").exists():
        return False
    code, _, err = run_git(["init"], project)
    if code != 0:
        raise ForgeError(f"git init failed: {err.strip()}")
    return True


def git_status(project: Path) -> list[str]:
    if not is_git_repo(project):
        return []
    code, out, _ = run_git(["status", "--short", "--untracked-files=all", "--", "."], project)
    if code != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]


def git_ignored(project: Path, rel: str) -> bool:
    if not is_git_repo(project):
        return False
    code, _, _ = run_git(["check-ignore", "-q", rel], project)
    return code == 0


def git_status_path(line: str) -> str:
    path = line[3:] if len(line) > 3 else line.strip()
    path = path.strip().strip('"')
    if " -> " in path:
        path = path.split(" -> ", 1)[1].strip().strip('"')
    return path


def source_dirty_entries(entries: Sequence[str]) -> list[str]:
    """Filter Star Forge's own state writes out of dirty-tree checks."""
    return live_common.source_hash_dirty_entries(Path.cwd(), entries)


def git_changed_files(project: Path) -> list[Path]:
    if not is_git_repo(project):
        return []
    code, out, _ = run_git(["diff", "--name-only", "HEAD"], project)
    files = [project / line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
    code, out, _ = run_git(["ls-files", "--others", "--exclude-standard"], project)
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
        raise ForgeError(f"Missing template: {path}")
    return read_text(path)


# ----------------------------------------------------------------- project tree


def has_star_forge_project_markers(project: Path) -> bool:
    # A Star Forge project is identified ONLY by its manifest. Requiring the
    # manifest (not merely a .starforge dir + Blueprint/Plan) closes the
    # grandfathering hole where a contaminated root blessed itself.
    return (project / PROJECT_MANIFEST).exists()


def find_star_forge_project_root(start: Path) -> Path | None:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if has_star_forge_project_markers(candidate):
            return follow_project_redirect(candidate)
        if candidate.parent == candidate:
            break
    return None


def follow_project_redirect(candidate: Path) -> Path:
    """A root-level project.json may redirect to a nested work/<slug>/ project."""
    manifest_path = candidate / PROJECT_MANIFEST
    if not manifest_path.exists():
        return candidate
    try:
        manifest = read_json(manifest_path)
    except Exception:
        return candidate
    if manifest.get("schema") != "star-forge.project-redirect.v1":
        return candidate
    raw_root = str(manifest.get("project_root") or "")
    if not raw_root:
        return candidate
    target = Path(raw_root)
    if target.resolve() != candidate.resolve() and target.exists() and has_star_forge_project_markers(target):
        return target.resolve()
    return candidate


def resolve_project(raw: str) -> Path:
    start = Path(raw).resolve()
    found = find_star_forge_project_root(start)
    return found if found is not None else repo_root(start)


def root_needs_product_isolation(project: Path) -> bool:
    if has_star_forge_project_markers(project):
        return False
    marker_names = {".git", "README.md", "package.json", "pyproject.toml", "Cargo.toml", "go.mod"}
    try:
        entries = {path.name for path in project.iterdir()}
    except OSError:
        return False
    # An existing foreign project (real source markers) needs isolation; a bare
    # dir with only a stray Blueprint/Plan does not.
    return bool(entries & marker_names and entries - {".git", ".gitignore", ".DS_Store"})


def product_slug_from_objective(project: Path, objective: str = "", explicit: str = "") -> str:
    if explicit.strip():
        return slugify(explicit).lower()
    text = objective.strip()
    if not text and (project / BLUEPRINT_FILE).exists():
        try:
            blueprint = read_text(project / BLUEPRINT_FILE)
            match = re.search(r"^#\s+(.+)$", blueprint, re.MULTILINE)
            if match:
                text = match.group(1)
        except OSError:
            text = ""
    if not text:
        text = project.name
    terms = [term for term in re.findall(r"[A-Za-z0-9]+", text.lower()) if term not in STOPWORDS]
    if terms:
        return slugify("-".join(terms[:5])).lower()
    return slugify(project.name or "star-forge-project").lower()


def normalize_project_profile(profile: str) -> str:
    candidate = str(profile or "").strip()
    return candidate if candidate in REVIEW_PROFILE_ROLES else "standard"


def source_profile_path(project: Path) -> Path:
    return project / SOURCE_PROFILE_FILE


def source_profile_exists(project: Path) -> bool:
    try:
        source_profile_path(project).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def source_profile_path_problem(project: Path) -> str:
    project_root = project.resolve()
    path = source_profile_path(project)
    try:
        parent = path.parent.resolve(strict=True)
    except OSError:
        return f"{SOURCE_PROFILE_FILE} parent cannot be inspected"
    anchored = parent / path.name
    try:
        anchored.relative_to(project_root)
    except ValueError:
        return f"{SOURCE_PROFILE_FILE} resolves outside project root"
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return ""
    except OSError:
        return f"{SOURCE_PROFILE_FILE} cannot be inspected"
    if stat.S_ISLNK(mode):
        return f"{SOURCE_PROFILE_FILE} is a symlink"
    if not stat.S_ISREG(mode):
        return f"{SOURCE_PROFILE_FILE} is not a regular file"
    return ""


def source_profile_read_problem(project: Path) -> str:
    path = source_profile_path(project)
    path_problem = source_profile_path_problem(project)
    if path_problem:
        return path_problem
    if not source_profile_exists(project):
        return ""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return f"{SOURCE_PROFILE_FILE} cannot be inspected"
    if mode & 0o444 == 0:
        return f"{SOURCE_PROFILE_FILE} is not readable"
    try:
        read_text(path)
    except PermissionError:
        return f"{SOURCE_PROFILE_FILE} is not readable"
    except UnicodeDecodeError:
        return ""
    except OSError:
        return f"{SOURCE_PROFILE_FILE} cannot be read"
    return ""


def source_profile_invalid_problem(project: Path) -> str:
    if not source_profile_exists(project):
        return ""
    read_problem = source_profile_read_problem(project)
    if read_problem:
        return read_problem
    try:
        payload = read_json(source_profile_path(project))
    except json.JSONDecodeError:
        return f"{SOURCE_PROFILE_FILE} is not valid JSON"
    except ForgeError:
        return f"{SOURCE_PROFILE_FILE} must contain a JSON object"
    except UnicodeDecodeError:
        return f"{SOURCE_PROFILE_FILE} is not valid UTF-8"
    except PermissionError:
        return f"{SOURCE_PROFILE_FILE} is not readable"
    except OSError:
        return f"{SOURCE_PROFILE_FILE} cannot be read"
    if payload.get("schema") != SOURCE_PROFILE_SCHEMA:
        return f"{SOURCE_PROFILE_FILE} has invalid schema"
    profile = str(payload.get("profile") or "").strip()
    if profile not in REVIEW_PROFILE_ROLES:
        return f"{SOURCE_PROFILE_FILE} records an invalid review profile"
    return ""


def source_profile_hash_blocker(project: Path) -> str:
    read_problem = source_profile_read_problem(project)
    if read_problem:
        return read_problem
    if source_profile_exists(project):
        return source_profile_invalid_problem(project)
    return ""


def source_profile_blocks_source_hash(project: Path) -> bool:
    return bool(source_profile_hash_blocker(project))


def source_hash_unavailable_problem(project: Path) -> dict[str, Any] | None:
    problem = source_profile_hash_blocker(project)
    if not problem:
        return None
    return {
        "severity": "high",
        "rule": "source-hash-unavailable",
        "file": SOURCE_PROFILE_FILE,
        "message": f"Cannot compute source_hash because {problem}. Standard review remains required.",
    }


def source_hash_exception_problem(exc: BaseException) -> dict[str, Any]:
    return {
        "severity": "high",
        "rule": "source-hash-unavailable",
        "file": "source tree",
        "message": f"Cannot compute source_hash: {exc}",
    }


def try_source_hash(project: Path) -> tuple[str | None, dict[str, Any] | None]:
    problem = source_hash_unavailable_problem(project)
    if problem:
        return None, problem
    try:
        return source_hash(project), None
    except (PermissionError, OSError) as exc:
        return None, source_hash_exception_problem(exc)


def read_source_profile_payload(project: Path) -> dict[str, Any]:
    path = source_profile_path(project)
    if not source_profile_exists(project):
        return {}
    if source_profile_read_problem(project) or source_profile_invalid_problem(project):
        return {}
    try:
        payload = read_json(path)
    except Exception:
        return {}
    return payload if payload.get("schema") == SOURCE_PROFILE_SCHEMA else {}


def read_source_profile(project: Path) -> str:
    payload = read_source_profile_payload(project)
    if not payload:
        return ""
    profile = str(payload.get("profile") or "").strip()
    return profile if profile in REVIEW_PROFILE_ROLES else ""


def ensure_source_profile(project: Path, profile: str) -> None:
    normalized = normalize_project_profile(profile)
    write_problem = source_profile_path_problem(project)
    if write_problem:
        raise ForgeError(f"Refusing to write {SOURCE_PROFILE_FILE}: {write_problem}")
    existing = read_source_profile_payload(project)
    selected_before_gates = bool(existing.get("selected_before_gates"))
    if not existing:
        selected_before_gates = not profile_downgrade_lock_reasons(project)
    payload = {
        "schema": SOURCE_PROFILE_SCHEMA,
        "profile": normalized,
        "initial_profile": str(existing.get("initial_profile") or normalized),
        "selected_before_gates": selected_before_gates,
        "review_roles": review_roles_for_profile(normalized),
    }
    write_json_if_changed(source_profile_path(project), payload)


def review_records_exist(project: Path) -> bool:
    root = project / REVIEWS_DIR
    if not root.exists():
        return False
    return any(path.is_file() for path in root.rglob("*"))


def profile_downgrade_lock_reasons(project: Path) -> list[str]:
    reasons: list[str] = []
    plan = project / PLAN_FILE
    if blueprint_is_approved(project):
        reasons.append("Blueprint.md is approved")
    if plan.exists():
        try:
            tasks = parse_tasks(plan)
        except ForgeError:
            tasks = []
        if tasks and not plan_is_placeholder(tasks):
            reasons.append("Plan.md exists")
            reasons.append("Plan.md has real tasks")
    if load_proof(project) is not None:
        reasons.append("final proof exists")
    if review_records_exist(project):
        reasons.append("review records exist")
    return reasons


def source_profile_payload_from_text(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    if not isinstance(payload, dict) or payload.get("schema") != SOURCE_PROFILE_SCHEMA:
        return {}
    profile = str(payload.get("profile") or "").strip()
    return payload if profile in REVIEW_PROFILE_ROLES else {}


def plan_text_has_real_tasks(text: str) -> bool:
    try:
        tasks = parse_tasks_from_text(text)
    except Exception:
        return False
    return bool(tasks) and not plan_is_placeholder(tasks)


def git_show_text(project: Path, revision: str, relpath: str) -> str | None:
    code, out, _ = run_git(["show", f"{revision}:{relpath}"], project)
    return out if code == 0 else None


def git_revision_has_review_gates(project: Path, revision: str) -> bool:
    blueprint = git_show_text(project, revision, BLUEPRINT_FILE)
    if blueprint is not None and blueprint_text_is_approved(blueprint):
        return True
    plan = git_show_text(project, revision, PLAN_FILE)
    if plan is not None and plan_text_has_real_tasks(plan):
        return True
    return False


def git_revision_or_ancestors_have_review_gates(project: Path, revision: str) -> bool:
    code, out, _ = run_git(["rev-list", "--reverse", revision], project)
    if code != 0:
        return True
    for ancestor in [line.strip() for line in out.splitlines() if line.strip()]:
        if git_revision_has_review_gates(project, ancestor):
            return True
    return False


def git_history_has_fast_mvp_before_gates(project: Path) -> bool:
    if not is_git_repo(project) or git_head(project) is None:
        return False
    code, out, _ = run_git(["rev-list", "--reverse", "HEAD", "--", SOURCE_PROFILE_FILE], project)
    if code != 0:
        return False
    for revision in [line.strip() for line in out.splitlines() if line.strip()]:
        text = git_show_text(project, revision, SOURCE_PROFILE_FILE)
        payload = source_profile_payload_from_text(text or "")
        if str(payload.get("profile") or "") != "fast-mvp":
            continue
        return not git_revision_or_ancestors_have_review_gates(project, revision)
    return False


def source_profile_lock_is_durable(project: Path) -> bool:
    path = source_profile_path(project)
    if not source_profile_exists(project) or not is_git_repo(project) or git_head(project) is None:
        return False
    if source_profile_read_problem(project):
        return False
    code, _, _ = run_git(["ls-files", "--error-unmatch", "--", SOURCE_PROFILE_FILE], project)
    if code != 0:
        return False
    for entry in git_status(project):
        if git_status_path(entry) == SOURCE_PROFILE_FILE:
            return False
    return any(candidate.resolve() == path.resolve() for candidate in live_common.snapshot_file_candidates(project))


def fast_mvp_profile_predates_gates(project: Path) -> bool:
    if read_source_profile(project) != "fast-mvp":
        return False
    if not source_profile_lock_is_durable(project):
        return False
    return git_history_has_fast_mvp_before_gates(project)


def fast_mvp_profile_selected_before_gates(project: Path) -> bool:
    payload = read_source_profile_payload(project)
    return str(payload.get("profile") or "") == "fast-mvp" and bool(payload.get("selected_before_gates"))


def setup_ledger_records_fast_mvp_before_gates(project: Path) -> bool:
    for payload in jsonl_payloads(project / LEDGER_FILE):
        if payload.get("schema") != "star-forge.ledger.v1":
            continue
        if payload.get("event") != "setup":
            continue
        if str(payload.get("profile") or "") != "fast-mvp":
            continue
        if bool(payload.get("profile_selected_before_gates")):
            return True
    return False


def review_profile(project: Path) -> str:
    manifest_profile = project_profile(project)
    if manifest_profile == "fast-mvp" and fast_mvp_profile_predates_gates(project):
        return "fast-mvp"
    return "standard"


def profile_lock_gate_reasons(project: Path) -> list[str]:
    reasons: list[str] = []
    if blueprint_is_approved(project):
        reasons.append("Blueprint.md is approved")
    plan = project / PLAN_FILE
    if plan.exists():
        try:
            tasks = parse_tasks(plan)
        except ForgeError:
            tasks = []
        if tasks and not plan_is_placeholder(tasks):
            reasons.append("Plan.md has real tasks")
    return reasons


def source_profile_lock_problems(project: Path) -> list[str]:
    problems: list[str] = []
    path = source_profile_path(project)
    if not source_profile_exists(project):
        return [f"{SOURCE_PROFILE_FILE} is missing"]
    profile_problem = source_profile_read_problem(project) or source_profile_invalid_problem(project)
    if profile_problem:
        problems.append(profile_problem)
        return problems
    elif read_source_profile(project) != "fast-mvp":
        problems.append(f"{SOURCE_PROFILE_FILE} does not record fast-mvp")
    if not is_git_repo(project):
        problems.append("project is not a git repository")
        return problems
    if git_head(project) is None:
        problems.append("git history has no commits")
    code, _, _ = run_git(["ls-files", "--error-unmatch", "--", SOURCE_PROFILE_FILE], project)
    if code != 0:
        problems.append(f"{SOURCE_PROFILE_FILE} is not tracked by git")
    for entry in git_status(project):
        if git_status_path(entry) == SOURCE_PROFILE_FILE:
            problems.append(f"{SOURCE_PROFILE_FILE} has uncommitted changes")
            break
    if not any(candidate.resolve() == path.resolve() for candidate in live_common.snapshot_file_candidates(project)):
        problems.append(f"{SOURCE_PROFILE_FILE} is not included in the durable source snapshot")
    if git_head(project) is not None and not git_history_has_fast_mvp_before_gates(project):
        problems.append(f"git history does not show {SOURCE_PROFILE_FILE} committed before Blueprint.md approval or real Plan.md tasks")
    return problems


def fast_mvp_profile_lock_state(project: Path) -> dict[str, Any]:
    manifest_profile = project_profile(project)
    profile_problem = source_profile_read_problem(project) or source_profile_invalid_problem(project)
    source_profile = read_source_profile(project)
    source_payload = read_source_profile_payload(project)
    effective_profile = review_profile(project)
    selected_before_gates = bool(source_payload.get("selected_before_gates"))
    requested_fast_mvp = manifest_profile == "fast-mvp" or source_profile == "fast-mvp"
    gate_reasons = profile_lock_gate_reasons(project)
    problems = source_profile_lock_problems(project) if requested_fast_mvp else []
    status = "inactive"
    message = ""
    next_action = ""
    if effective_profile == "fast-mvp":
        status = "active"
        message = f"{SOURCE_PROFILE_FILE} is durably tracked before Blueprint.md approval or real Plan.md tasks."
    elif requested_fast_mvp and profile_problem:
        status = "blocked"
        message = (
            f"Fast-mvp cannot be proven because {profile_problem}. "
            "Standard review remains required."
        )
        next_action = (
            f"Restore read permission and a valid {SOURCE_PROFILE_FILE}, then rerun. "
            "If durable pre-gate proof cannot be established, keep standard review or switch the project profile to standard."
        )
    elif requested_fast_mvp and selected_before_gates:
        if gate_reasons:
            status = "blocked"
            message = (
                f"Fast-mvp was selected before gates, but {SOURCE_PROFILE_FILE} is not durably tracked "
                "before the current Blueprint.md or Plan.md gates. Standard review remains required."
            )
            next_action = (
                f"Commit only {SOURCE_PROFILE_FILE} now if Blueprint.md and Plan.md gate changes are still uncommitted, "
                "then rerun. If those gates were already committed first, keep standard review or switch the project profile to standard."
            )
        else:
            status = "pending"
            message = f"Fast-mvp is selected, but {SOURCE_PROFILE_FILE} is not durable yet."
            next_action = f"Commit {SOURCE_PROFILE_FILE} by itself before approving Blueprint.md or adding real Plan.md tasks, then rerun."
    elif requested_fast_mvp:
        status = "standard-required"
        message = (
            f"Fast-mvp is recorded without durable pre-gate proof from {SOURCE_PROFILE_FILE}. "
            "Standard review remains required."
        )
        next_action = "Keep standard review roles, or start a new fast-mvp flow before approval and planning."
    return {
        "status": status,
        "manifest_profile": manifest_profile,
        "source_profile": source_profile or None,
        "effective_review_profile": effective_profile,
        "selected_before_gates": selected_before_gates,
        "gate_reasons": gate_reasons,
        "problems": problems,
        "message": message,
        "next_action": next_action,
    }


def project_manifest_payload(project: Path, *, objective: str = "", product_slug: str = "", profile: str = "standard", root_mode: str = "dedicated") -> dict[str, Any]:
    slug = product_slug_from_objective(project, objective, product_slug)
    project_root = str(project.resolve())
    project_id = stable_json_hash({"root": project_root, "slug": slug})[:16]
    blueprint = project / BLUEPRINT_FILE
    plan = project / PLAN_FILE
    normalized_profile = normalize_project_profile(profile)
    return {
        "schema": "star-forge.project.v1",
        "created_at": now_utc(),
        "updated_at": now_utc(),
        "project_root": project_root,
        "product_slug": slug,
        "project_id": project_id,
        "profile": normalized_profile,
        "source_profile_path": SOURCE_PROFILE_FILE,
        "root_mode": root_mode or "dedicated",
        "state_machine_version": STAR_FORGE_STATE_VERSION,
        "blueprint_path": BLUEPRINT_FILE,
        "plan_path": PLAN_FILE,
        "blueprint_hash": file_sha256(blueprint) if blueprint.exists() else None,
        "plan_hash": file_sha256(plan) if plan.exists() else None,
    }


def ensure_project_manifest(project: Path, *, objective: str = "", product_slug: str = "", profile: str = "", root_mode: str = "") -> dict[str, Any]:
    path = project / PROJECT_MANIFEST
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = read_json(path)
        except Exception:
            existing = {}
    if existing.get("schema") == "star-forge.project-redirect.v1":
        raise ForgeError(f"Refusing to overwrite the project redirect at {path}; the project lives at {existing.get('project_root')}.")
    requested_profile = normalize_project_profile(profile) if str(profile or "").strip() else normalize_project_profile(str(existing.get("profile") or "standard"))
    explicit_profile = bool(str(profile or "").strip())
    if explicit_profile and requested_profile == "fast-mvp":
        lock_reasons = profile_downgrade_lock_reasons(project)
        if lock_reasons and not fast_mvp_profile_predates_gates(project):
            raise ForgeError(
                "Refusing to downgrade review profile from standard to fast-mvp after project gates exist: "
                + ", ".join(lock_reasons)
                + ". Start fast-mvp before approval or planning, or keep the standard review roles."
            )
    if explicit_profile:
        ensure_source_profile(project, requested_profile)
    payload = project_manifest_payload(
        project,
        objective=objective,
        product_slug=product_slug or str(existing.get("product_slug") or ""),
        profile=requested_profile,
        root_mode=root_mode or str(existing.get("root_mode") or "dedicated"),
    )
    if existing:
        payload["created_at"] = existing.get("created_at") or payload["created_at"]
        payload["project_id"] = existing.get("project_id") or payload["project_id"]
        if strip_volatile(existing) == strip_volatile(payload):
            return existing
    write_json(path, payload)
    return payload


def project_profile(project: Path) -> str:
    path = project / PROJECT_MANIFEST
    if path.exists():
        try:
            return normalize_project_profile(str(read_json(path).get("profile") or "standard"))
        except Exception:
            return "standard"
    return "standard"


def review_roles_for_profile(profile: str) -> list[str]:
    return adaptive_review_policy.legacy_roles_for_profile(profile)


def required_review_policy(
    project: Path,
    *,
    source_hash_value: str | None = None,
    bind_source_hash: bool = True,
) -> adaptive_review_policy.ReviewPolicySelection:
    blueprint_text = ""
    blueprint_path = project / BLUEPRINT_FILE
    if blueprint_path.exists():
        try:
            blueprint_text = read_text(blueprint_path)
        except OSError:
            blueprint_text = ""
    tasks: list[dict[str, Any]] = []
    plan_path = project / PLAN_FILE
    if plan_path.exists():
        try:
            tasks = parse_tasks(plan_path)
        except ForgeError:
            tasks = []
    if source_hash_value is None and bind_source_hash:
        source_hash_value, _problem = try_source_hash(project)
    return adaptive_review_policy.select_review_policy(
        blueprint_text,
        tasks,
        profile=review_profile(project),
        source_hash=source_hash_value,
    )


def required_review_roles(project: Path) -> list[str]:
    return list(required_review_policy(project).roles)


# -------------------------------------------------------------- file scanning


SECRET_PRONE_SUFFIXES = {".pem", ".key", ".crt", ".cer", ".p12", ".pfx", ".keystore"}


def is_dotenv(path: Path) -> bool:
    # Path('.env').suffix is '' and Path('.env.local').suffix is '.local', so the
    # tree scan missed literal dotenv files entirely. Match them by name.
    return path.name == ".env" or path.name.startswith(".env.")


def is_text_file(path: Path) -> bool:
    return (
        path.name in {BLUEPRINT_FILE, PLAN_FILE, "README.md", ".gitignore"}
        or is_dotenv(path)
        or path.suffix.lower() in TEXT_SUFFIXES
        or path.suffix.lower() in SECRET_PRONE_SUFFIXES
    )


def iter_project_files(project: Path, *, all_files: bool = False) -> Iterable[Path]:
    if all_files:
        for root, dirs, files in os.walk(project):
            root_path = Path(root)
            dirs[:] = sorted(name for name in dirs if name not in IGNORED_PARTS and not (root_path / name).is_symlink())
            for name in sorted(files):
                path = root_path / name
                if any(part in IGNORED_PARTS for part in path.relative_to(project).parts):
                    continue
                if path.is_file() and is_text_file(path):
                    yield path
    else:
        yield from git_changed_files(project)


def scan_paths(paths: Iterable[Path], project: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists() or not path.is_file() or not is_text_file(path):
            continue
        try:
            rel = relative_to_project(path, project)
            if any(part in IGNORED_PARTS for part in Path(rel).parts):
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


def is_source_file(path: Path) -> bool:
    rel_parts = set(path.parts)
    if not ({"src", "app", "components", "lib", "pages", "routes", "services"} & rel_parts):
        return False
    return path.suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".swift", ".kt", ".java", ".go", ".rs"}


def architecture_debt_findings(paths: Iterable[Path], project: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists() or not path.is_file() or not is_source_file(path):
            continue
        try:
            rel = relative_to_project(path, project)
            if any(part in IGNORED_PARTS for part in Path(rel).parts):
                continue
            text = read_text(path)
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        line_count = len(lines)
        ts_ignore_count = len(re.findall(r"@ts-ignore", text))
        if line_count > 1200:
            findings.append({"severity": "medium", "rule": "architecture-debt-large-file", "file": rel, "line": 1, "evidence": f"{line_count} lines; split into smaller cohesive modules"})
        elif line_count > 800:
            findings.append({"severity": "low", "rule": "architecture-debt-large-file", "file": rel, "line": 1, "evidence": f"{line_count} lines; consider a follow-up split"})
        if ts_ignore_count:
            findings.append({"severity": "high", "rule": "architecture-debt-ts-ignore", "file": rel, "line": 1, "evidence": f"{ts_ignore_count} @ts-ignore directive(s); justify or remove"})
    return findings


def snapshot_file_candidates(project: Path) -> list[Path]:
    return live_common.snapshot_file_candidates(project)


def source_snapshot_includes(path: Path) -> bool:
    return path.is_file()


def files_fingerprint(project: Path, paths: Sequence[Path]) -> str:
    return live_common.files_fingerprint(project, paths)


def source_hash(project: Path) -> str:
    return live_common.compute_source_hash(project)


def source_snapshot_rel_paths(project: Path) -> set[str]:
    return {relative_to_project(path, project) for path in snapshot_file_candidates(project)}


def dirty_paths_missing_from_source_snapshot(project: Path) -> list[str]:
    snapshot_paths = source_snapshot_rel_paths(project)
    missing: list[str] = []
    for line in source_dirty_entries(git_status(project)):
        rel = git_status_path(line)
        if not rel or rel in snapshot_paths:
            continue
        missing.append(line)
    return missing


def tree_clean_for_commit_binding(project: Path) -> bool:
    return not source_dirty_entries(git_status(project))


def release_snapshot(project: Path) -> dict[str, Any]:
    source_files = snapshot_file_candidates(project)
    blueprint = project / BLUEPRINT_FILE
    plan = project / PLAN_FILE
    return {
        "schema": "star-forge.release-snapshot.v1",
        "created_at": now_utc(),
        "git_head": git_head(project),
        "source_hash": files_fingerprint(project, source_files),
        "source_files": [relative_to_project(path, project) for path in source_files],
        "blueprint_hash": file_sha256(blueprint) if blueprint.exists() else None,
        "plan_hash": file_sha256(plan) if plan.exists() else None,
    }


def release_snapshot_unavailable(project: Path, problems: Sequence[dict[str, Any]]) -> dict[str, Any]:
    blueprint = project / BLUEPRINT_FILE
    plan = project / PLAN_FILE
    return {
        "schema": "star-forge.release-snapshot.v1",
        "created_at": now_utc(),
        "git_head": git_head(project),
        "source_hash": None,
        "source_hash_unavailable": True,
        "problems": list(problems),
        "source_files": [],
        "blueprint_hash": file_sha256(blueprint) if blueprint.exists() else None,
        "plan_hash": file_sha256(plan) if plan.exists() else None,
    }


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


# ------------------------------------------------------------------ plan model


def split_row(line: str) -> list[str]:
    return project_contracts.split_plan_row(line)


def is_separator_row(line: str) -> bool:
    cells = split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def task_tables(lines: list[str]) -> Iterable[tuple[int, list[str], int, int]]:
    for idx, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        headers = split_row(line)
        lowered = [item.lower() for item in headers]
        if "task" not in lowered or "status" not in lowered:
            continue
        if idx + 1 >= len(lines) or not is_separator_row(lines[idx + 1]):
            continue
        end = idx + 2
        while end < len(lines) and lines[end].strip().startswith("|"):
            end += 1
        yield idx, headers, idx + 2, end


def parse_tasks_from_text(text: str) -> list[dict[str, Any]]:
    return project_contracts.parse_plan_tasks_text(text)


def parse_tasks(plan_path: Path) -> list[dict[str, Any]]:
    if not plan_path.exists():
        raise ForgeError(f"{plan_path} does not exist")
    return parse_tasks_from_text(read_text(plan_path))


def plan_parse_problem(plan_path: Path, tasks: Sequence[dict[str, Any]]) -> str | None:
    if tasks or not plan_path.exists():
        return None
    try:
        lines = read_text(plan_path).splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and not is_separator_row(line) and "task" in stripped.lower():
            return (
                "Plan.md contains a task-like table that did not parse to any tasks. "
                "The header row must use exact `Task` and `Status` columns followed by a `|---|` separator row."
            )
    return None


def parse_depends(raw: str) -> list[str]:
    if not raw or raw.strip() in {"-", "none", "None"}:
        return []
    return [item.strip() for item in re.split(r"[,/]", raw) if item.strip()]


def task_files(task: dict[str, Any]) -> list[str]:
    raw = str(task.get("files") or "")
    if raw.strip() in {"", "-", "none", "n/a", "na"}:
        return []
    return [item.strip() for item in re.split(r"[,;]", raw) if item.strip()]


def task_requires_real_workers(task: dict[str, Any]) -> bool:
    return str(task.get("mode") or "delegate").lower() == "delegate"


CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".java", ".js", ".jsx", ".kt",
    ".mjs", ".cjs", ".php", ".py", ".rb", ".rs", ".sh", ".bash", ".zsh", ".swift",
    ".ts", ".tsx", ".vue", ".sql", ".css", ".html", ".ipynb", ".yaml", ".yml",
    ".tf", ".tfvars", ".gradle", ".scala", ".clj", ".ex", ".exs", ".dart", ".m",
    ".mm", ".lua", ".pl", ".r",
}
# Extension-less files that are nonetheless executable/behavioral code.
CODE_FILENAMES = {"dockerfile", "makefile", "rakefile", "gemfile", "procfile", "justfile", "vagrantfile"}
# Shell tokens whose only effect is unconditional success — a no-op dressed as a
# verification. A regex over the raw string is trivially evadable (`(true)`,
# `true && true`, comments), so we canonicalize: strip grouping/comments/env
# prefixes, split on control operators, recurse through eval/sh -c wrappers, and
# require EVERY clause to be a known no-op. No static check can distinguish every
# elaborate no-op from a real command (undecidable), so this catches the cheap,
# realistic fakes; the human-reviewable Verify cell and the review wave are the
# backstop for the rest.
NOOP_COMMANDS = {"true", ":", "echo", "printf", "sleep", "pwd", "clear"}
# Pure lookups that prove a tool exists but exercise nothing about the task.
PRESENCE_HEADS = {"which", "type", "hash"}
ALWAYS_PRESENT_PATHS = {".", "..", "/", "./", "$pwd", "$home", "$(pwd)"}


def _clause_is_noop(clause: str, depth: int) -> bool:
    tokens = clause.split()
    # Strip leading `VAR=value` env-assignment prefixes (`CI=true true`).
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return True
    head = tokens[0].split("/")[-1].lower()
    rest = tokens[1:]
    if head in NOOP_COMMANDS:
        return True
    if head in {"exit", "return"} and (not rest or rest[0] == "0"):
        return True
    if head == "cat" and rest == ["/dev/null"]:
        return True
    if head in PRESENCE_HEADS:
        return True
    if head == "command":
        if rest and rest[0] in {"-v", "-V"}:
            return True
        if depth < 3 and rest:
            return command_is_noop(" ".join(rest), depth + 1)
    if head == "builtin" and depth < 3 and rest:
        return command_is_noop(" ".join(rest), depth + 1)
    if depth < 3 and head == "eval":
        return command_is_noop(clause[clause.lower().find("eval") + 4:].strip().strip("'\""), depth + 1)
    if depth < 3 and head in {"bash", "sh"} and "-c" in rest:
        inner = " ".join(rest[rest.index("-c") + 1:]).strip().strip("'\"")
        return command_is_noop(inner, depth + 1)
    if head in {"test", "[", "[["}:
        args = [t for t in rest if t not in {"]", "]]"}]
        # Only flag UNAMBIGUOUS tautologies (identical operands, empty -z, literal
        # always-present path). `test -f dist/app.js` / `test -d node_modules` stay
        # REAL — they verify build output / deps and must not be blocked.
        if len(args) == 3 and args[1] in {"=", "==", "-eq"} and args[0] == args[2]:
            return True
        if len(args) == 3 and args[1] == "-ne" and args[0] != args[2]:
            return True
        if len(args) == 2 and args[0] == "-z" and args[1] in {"", "''", '""'}:
            return True
        if len(args) == 2 and args[0] in {"-e", "-d", "-r", "-x", "-s"} and args[1].lower() in ALWAYS_PRESENT_PATHS:
            return True
        if len(args) == 1 and "$" not in args[0] and args[0] not in {"", "''", '""'}:
            return True
        return False
    return False


def _split_top_level(text: str) -> list[str]:
    """Split on top-level && || ; & only, respecting ()/{} nesting so a grouped
    clause like `(true && true) || true` is not shredded by a naive split."""
    clauses: list[str] = []
    depth = 0
    cur = ""
    i = 0
    while i < len(text):
        two = text[i : i + 2]
        c = text[i]
        if c in "([{":
            depth += 1
            cur += c
        elif c in ")]}":
            depth = max(0, depth - 1)
            cur += c
        elif depth == 0 and two in {"&&", "||"}:
            clauses.append(cur)
            cur = ""
            i += 2
            continue
        elif depth == 0 and c in ";&":
            clauses.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    clauses.append(cur)
    return [c.strip() for c in clauses if c.strip()]


def command_is_noop(command: str, depth: int = 0) -> bool:
    text = str(command or "").strip()
    if not text:
        return True
    # Drop an unquoted trailing comment.
    if "'" not in text and '"' not in text:
        text = re.sub(r"\s#.*$", "", text).strip()
    # Peel wrapping ( ... ) or { ... ;} and trailing semicolons, repeatedly.
    for _ in range(6):
        stripped = text.strip().strip(";").strip()
        m = re.fullmatch(r"\((.*)\)", stripped, re.DOTALL) or re.fullmatch(r"\{(.*)\}", stripped, re.DOTALL)
        if m:
            text = m.group(1)
            continue
        text = stripped
        break
    if not text:
        return True
    clauses = _split_top_level(text)
    if not clauses:
        return True
    for clause in clauses:
        # A still-grouped clause recurses (peels its own parens/braces).
        if depth < 4 and (clause.startswith(("(", "{")) or _split_top_level(clause) != [clause]):
            if not command_is_noop(clause, depth + 1):
                return False
        elif not _clause_is_noop(clause, depth):
            return False
    return True


def task_owns_code(task: dict[str, Any]) -> bool:
    for rel in task_files(task):
        path = Path(rel)
        if path.suffix.lower() in CODE_SUFFIXES or path.name.lower() in CODE_FILENAMES:
            return True
    return False


def task_allows_noop_verification(task: dict[str, Any]) -> bool:
    # No-op verification is for genuine documentation tasks only. The old "Verify
    # cell says noop" path let the model self-grant a no-op on a code task, so it
    # is gone: a docs task that owns code files still needs a real verify.
    return str(task.get("mode") or "").lower() == "docs" and not task_owns_code(task)


def normalize_command(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def task_verify_command(task: dict[str, Any]) -> str:
    return str(task.get("verify") or "").strip()


def task_proof_kinds(task: Mapping[str, Any]) -> set[str]:
    return {
        item.strip().casefold()
        for item in str(task.get("proof") or "").split(",")
        if item.strip() and item.strip() != "-"
    }


def task_file_is_infrastructure(raw_path: str) -> bool:
    normalized = str(raw_path or "").strip().replace("\\", "/").strip("/")
    if not normalized:
        return False
    path = Path(normalized)
    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    if parts & INFRASTRUCTURE_TASK_PARTS:
        return True
    if path.suffix.casefold() in INFRASTRUCTURE_DOCUMENT_SUFFIXES:
        return True
    return (
        name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or name in {"readme", "readme.md", "changelog", "changelog.md"}
    )


def task_files_are_infrastructure(task: Mapping[str, Any]) -> bool:
    files = task_files(dict(task))
    return bool(files) and all(task_file_is_infrastructure(path) for path in files)


def task_owns_visual_source(task: Mapping[str, Any]) -> bool:
    for raw_path in task_files(dict(task)):
        if task_file_is_infrastructure(raw_path):
            continue
        path = Path(raw_path.replace("\\", "/"))
        suffix = path.suffix.casefold()
        parts = {part.casefold() for part in path.parts[:-1]}
        if suffix in VISUAL_SOURCE_SUFFIXES:
            return True
        if parts & VISUAL_SOURCE_PARTS and (
            suffix in CODE_SUFFIXES or path.name.casefold() in CODE_FILENAMES
        ):
            return True
        if suffix == ".swift" and (
            path.stem.casefold().endswith("view")
            or path.stem.casefold().endswith("screen")
        ):
            return True
    return False


def task_is_visual(task: dict[str, Any]) -> bool:
    proof_kinds = task_proof_kinds(task)
    if "browser" in proof_kinds:
        return True
    if task_owns_visual_source(task):
        return True
    if task_files_are_infrastructure(task):
        return False
    if str(task.get("plan_version") or "").casefold() == "v2" and proof_kinds:
        return False
    text = " ".join(str(task.get(key, "")) for key in ("description", "verify", "files", "evidence"))
    return bool(VISUAL_TASK_RE.search(text))


def validate_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    ids = {task["id"] for task in tasks}
    for task in tasks:
        status = task["status"]
        if status not in VALID_STATUSES:
            problems.append({"severity": "high", "task": task["id"], "line": task["line"], "message": f"invalid status `{status}`"})
        mode = str(task.get("mode") or "").lower()
        if mode not in VALID_MODES:
            problems.append({"severity": "medium", "task": task["id"], "line": task["line"], "message": f"invalid mode `{mode}`; use solo, delegate, or docs"})
        if status in {"ready", "in_progress", "reviewing", "complete"} and not task_allows_noop_verification(task):
            verify_cell = str(task.get("verify") or "").strip()
            if not verify_cell:
                problems.append({"severity": "high", "task": task["id"], "line": task["line"], "message": "missing verify command; name a real verification command or set mode=docs"})
            elif command_is_noop(verify_cell):
                problems.append({"severity": "high", "task": task["id"], "line": task["line"], "message": f"Verify command `{verify_cell}` is a no-op; name a real verification command"})
        if status == "complete" and (not task["evidence"] or task["evidence"] == "-"):
            problems.append({"severity": "high", "task": task["id"], "line": task["line"], "message": "complete task requires evidence links"})
        if status == "blocked" and (not task["evidence"] or task["evidence"] == "-"):
            problems.append({"severity": "high", "task": task["id"], "line": task["line"], "message": "blocked task requires evidence or reason"})
        for dep in parse_depends(task["depends"]):
            if dep not in ids:
                problems.append({"severity": "medium", "task": task["id"], "line": task["line"], "message": f"unknown dependency `{dep}`"})
    return problems


def plan_contract_mode(tasks: Sequence[dict[str, Any]]) -> str:
    versions = {str(task.get("plan_version") or "compatible") for task in tasks}
    if not versions:
        return "unknown"
    if versions == {"legacy"}:
        return "legacy"
    if versions == {"v2"}:
        return "v2"
    if len(versions) == 1:
        return next(iter(versions))
    return "mixed"


def validate_project_plan_contract(
    project: Path,
    tasks: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply strict traceability only when Plan v2 is present."""
    blueprint_path = project / BLUEPRINT_FILE
    try:
        blueprint_text = read_text(blueprint_path) if blueprint_path.exists() else ""
    except (OSError, UnicodeDecodeError):
        blueprint_text = ""
    return project_contracts.validate_plan_v2_contract(blueprint_text, tasks)


def ready_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    complete = {task["id"] for task in tasks if task["status"] == "complete"}
    ready: list[dict[str, Any]] = []
    for task in tasks:
        if task["status"] not in {"queued", "ready"}:
            continue
        if all(dep in complete for dep in parse_depends(task["depends"])):
            ready.append(task)
    return ready


def all_tasks_complete(tasks: Sequence[dict[str, Any]]) -> bool:
    return bool(tasks) and all(task.get("status") == "complete" for task in tasks)


def plan_is_placeholder(tasks: list[dict[str, Any]]) -> bool:
    if not tasks:
        return True
    if len(tasks) != 1:
        return False
    task = tasks[0]
    return task["id"] == "SF-001" and "define the first build task" in task["description"].lower()


def task_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    return counts


def update_plan_task_row(plan_path: Path, task_id: str, updates: dict[str, str]) -> None:
    lines = read_text(plan_path).splitlines()
    for _, headers, start, end in task_tables(lines):
        index = {name.lower(): i for i, name in enumerate(headers)}
        if "task" not in index:
            continue
        for line_idx in range(start, end):
            cells = split_row(lines[line_idx])
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            if cells[index["task"]].strip() != task_id:
                continue
            for key, value in updates.items():
                raw_idx = index.get(key.lower())
                if raw_idx is not None:
                    cells[raw_idx] = value
            lines[line_idx] = "| " + " | ".join(cells[: len(headers)]) + " |"
            write_text(plan_path, "\n".join(lines) + "\n")
            return
    raise ForgeError(f"Task {task_id} not found in {plan_path}")


def append_plan_task(plan_path: Path, row: dict[str, str]) -> bool:
    """Append a task row to the first task table. Returns False if no table."""
    lines = read_text(plan_path).splitlines()
    tables = list(task_tables(lines))
    if not tables:
        return False
    _, headers, _, end = tables[0]
    cells = [row.get(name.lower(), "") for name in headers]
    new_line = "| " + " | ".join(cells) + " |"
    lines.insert(end, new_line)
    write_text(plan_path, "\n".join(lines) + "\n")
    return True


# ------------------------------------------------------------------- blueprint


def blueprint_text_is_approved(text: str) -> bool:
    """Recognize a v0.3 approval sentinel for compatibility checks."""
    return project_contracts.blueprint_text_has_legacy_approval(text)


def blueprint_lock_state(project: Path) -> dict[str, Any]:
    return project_contracts.blueprint_lock_state(project)


def blueprint_has_valid_lock(project: Path) -> bool:
    return blueprint_lock_state(project).get("status") == "locked"


def blueprint_is_approved(project: Path) -> bool:
    """Accept locked v0.4 contracts and readable v0.3 legacy approvals."""
    return bool(blueprint_lock_state(project).get("approved"))


def blueprint_lifecycle_contract(project: Path) -> dict[str, Any]:
    """Return lifecycle facts without imposing v0.4 phases on legacy projects."""
    path = project / BLUEPRINT_FILE
    try:
        text = read_text(path) if path.exists() else ""
    except (OSError, UnicodeError):
        text = ""
    return project_contracts.parse_blueprint_lifecycle_contract(text)


def lifecycle_gate_state(
    project: Path,
    *,
    kind: str,
    required: bool,
    current_source_hash: str | None,
    expected_delivery_target: str = "",
) -> dict[str, Any]:
    """Load and evaluate one lifecycle gate without mutating its proof files."""
    paths = {
        "foundation": (
            project_lifecycle.FOUNDATION_CONTRACT_PATH,
            project_lifecycle.FOUNDATION_EVIDENCE_PATH,
        ),
        "delivery": (
            project_lifecycle.DELIVERY_CONTRACT_PATH,
            project_lifecycle.DELIVERY_EVIDENCE_PATH,
        ),
    }
    contract_rel, evidence_rel = paths[kind]
    base = {
        "required": required,
        "contract_path": contract_rel,
        "evidence_path": evidence_rel,
    }
    if not required:
        return base | {
            "status": "COMPATIBLE",
            "satisfied": True,
            "blockers": [],
        }

    contract_path = project / contract_rel
    evidence_path = project / evidence_rel
    missing = [
        rel
        for rel, path in (
            (contract_rel, contract_path),
            (evidence_rel, evidence_path),
        )
        if not path.exists()
    ]
    if missing:
        return base | {
            "status": "MISSING",
            "satisfied": False,
            "blockers": ["missing lifecycle artifact: " + item for item in missing],
        }
    try:
        contract = read_json(contract_path)
        evidence = read_json(evidence_path)
    except Exception as exc:
        return base | {
            "status": "BLOCKED",
            "satisfied": False,
            "blockers": [f"{kind} lifecycle artifacts are unreadable: {exc}"],
        }

    if kind == "foundation":
        bound_source_hash = str(evidence.get("source_hash") or "")
        gate = project_lifecycle.evaluate_foundation(
            contract,
            evidence,
            current_source_hash=bound_source_hash,
        )
        payload = gate.to_dict()
        satisfied = gate.ready_for_feature_work
    else:
        gate = project_lifecycle.evaluate_delivery(
            contract,
            evidence,
            current_source_hash=str(current_source_hash or ""),
        )
        payload = gate.to_dict()
        satisfied = gate.ready_for_completion
        actual_target = str((contract.get("target") or {}).get("kind") or "")
        if expected_delivery_target and actual_target != expected_delivery_target:
            payload["status"] = "BLOCKED"
            payload.setdefault("blockers", []).append(
                "delivery contract target does not match Blueprint.md"
            )
            satisfied = False
    return base | payload | {"satisfied": satisfied}


def scope_hash(project: Path) -> str | None:
    state = blueprint_lock_state(project)
    if not state.get("approved"):
        return None
    digest = str(state.get("current_sha256") or "")
    return digest[:16] if len(digest) == 64 else None


def lexical_terms(text: str) -> set[str]:
    terms = {item.lower() for item in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", text)}
    return {term for term in terms if term not in STOPWORDS}


# --------------------------------------------------------------- run records


def run_record_path(project: Path, *, kind: str, task: str | None = None, digest: str | None = None) -> Path:
    parts = [timestamp_slug(), slugify(kind)]
    if task:
        parts.append(slugify(task))
    if digest:
        parts.append(slugify(digest[:12]))
    return project / RUNS_DIR / ("-".join(parts) + ".json")


def write_run_record(project: Path, payload: dict[str, Any]) -> Path:
    ensure_state_dirs(project)
    payload = dict(payload)
    payload.setdefault("created_at", now_utc())
    payload.setdefault("recorded_ns", time.time_ns())
    payload.setdefault("project", str(project))
    kind = str(payload.get("kind") or payload.get("schema") or "run")
    task = str(payload.get("task") or "") or None
    digest = stable_json_hash(redact({key: value for key, value in payload.items() if key != "artifact"}))
    path = run_record_path(project, kind=kind, task=task, digest=digest)
    write_json(path, payload)
    append_jsonl(
        project / LEDGER_FILE,
        {
            "schema": "star-forge.ledger.v1",
            "timestamp": now_utc(),
            "event": kind,
            "task": task,
            "verdict": payload.get("verdict"),
            "summary": payload.get("summary") or "",
            "artifacts": [relative_to_project(path, project)],
        },
    )
    return path


def load_run_records(project: Path, *, kind: str, task: str | None = None) -> list[dict[str, Any]]:
    root = project / RUNS_DIR
    records: list[dict[str, Any]] = []
    if not root.exists():
        return records
    for path in sorted(root.glob("*.json")):
        try:
            payload = read_json(path)
        except Exception:
            continue
        if payload.get("kind") != kind:
            continue
        if task is not None and payload.get("task") != task:
            continue
        payload["_artifact"] = relative_to_project(path, project)
        records.append(payload)
    return records


def latest_record(items: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    def key(item: dict[str, Any]) -> tuple[Any, ...]:
        return (str(item.get("created_at") or ""), int(item.get("recorded_ns") or 0), str(item.get("_artifact") or ""))

    return max(items, key=key) if items else None


# --------------------------------------------------------------- verify spine


def command_output_tail(text: str, limit: int = 6000) -> str:
    return text[-limit:] if len(text) > limit else text


def cmd_verify(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    started = now_utc()
    if args.noop:
        plan_path = project / PLAN_FILE
        if plan_path.exists():
            tasks = parse_tasks(plan_path)
            task = next((item for item in tasks if item.get("id") == args.task), None)
            if task and not task_allows_noop_verification(task):
                raise ForgeError(
                    f"Task {args.task} is not eligible for no-op verification; it is mode=docs only with no code files. Run a real verification command."
                )
        proc_returncode = 0
        stdout = args.summary or "No-op verification recorded for documentation-only task."
        stderr = ""
    else:
        if not args.command:
            raise ForgeError("verify requires --command unless --noop is used")
        if command_is_noop(args.command):
            raise ForgeError(
                f"`{args.command.strip()}` is a trivially-passing no-op, not a verification. Run the task's real Verify command."
            )
        try:
            proc = subprocess.run(
                args.command,
                cwd=str(project),
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=args.timeout,
                check=False,
            )
            proc_returncode = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
        except subprocess.TimeoutExpired as exc:
            proc_returncode = 124
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr if isinstance(exc.stderr, str) else "") + f"\nCommand timed out after {args.timeout} seconds."
    snapshot, snapshot_problem = safe_release_snapshot(project)
    problems = [snapshot_problem] if snapshot_problem else []
    verdict = "PASS" if proc_returncode == 0 and not problems else "FAIL"
    payload = {
        "schema": "star-forge.verify-run.v1",
        "kind": "verify-run",
        "created_at": now_utc(),
        "started_at": started,
        "project": str(project),
        "task": args.task,
        "command": args.command or "NOOP",
        "noop": args.noop,
        "returncode": proc_returncode,
        "verdict": verdict,
        "duration_timeout_seconds": args.timeout,
        "source_snapshot": snapshot,
        "stdout_tail": command_output_tail(stdout),
        "stderr_tail": command_output_tail(stderr),
        "problems": problems,
        "summary": args.summary,
    }
    path = write_run_record(project, payload)
    payload["artifact"] = relative_to_project(path, project)
    print(json.dumps(payload, indent=2))
    return 0 if verdict == "PASS" or not args.strict else 1


def fresh_passing_verify(project: Path, task: dict[str, Any]) -> bool:
    """A fresh passing verify whose recorded command matches the task's declared
    Verify cell and is not a trivially-passing no-op.

    Binding the run to the human-readable Verify cell closes the `verify --command
    true` bypass: to fake it the model would have to write a fake-but-real-looking
    command into the plan, where the review wave and the operating card surface it.
    """
    current, hash_problem = try_source_hash(project)
    if hash_problem or current is None:
        return False
    declared = normalize_command(task_verify_command(task))
    # A non-docs task with no declared Verify command is not completable: the
    # human-reviewable plan must say HOW the task is verified. Without this, an
    # empty cell let any passing command satisfy completion.
    if not declared or command_is_noop(task_verify_command(task)):
        return False
    runs = load_run_records(project, kind="verify-run", task=task["id"])
    for item in runs:
        command = str(item.get("command") or "")
        if command_is_noop(command) or normalize_command(command) != declared:
            continue
        if (
            item.get("verdict") == "PASS"
            and not item.get("noop")
            and isinstance(item.get("source_snapshot"), dict)
            and item["source_snapshot"].get("source_hash") == current
        ):
            return True
    return False


def has_noop_verify(project: Path, task_id: str) -> bool:
    runs = load_run_records(project, kind="verify-run", task=task_id)
    return any(item.get("verdict") == "PASS" and item.get("noop") for item in runs)


def verify_findings(project: Path, tasks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for task in tasks:
        if task.get("status") != "complete":
            continue
        if task_allows_noop_verification(task):
            if not has_noop_verify(project, task["id"]):
                findings.append({"severity": "high", "rule": "verify-noop-missing", "file": str(RUNS_DIR), "task": task["id"], "message": f"docs task {task['id']} needs a recorded no-op verify run."})
            continue
        if not fresh_passing_verify(project, task):
            findings.append({"severity": "high", "rule": "verify-stale", "file": str(RUNS_DIR), "task": task["id"], "message": f"Task {task['id']} has no passing verify run that matches its declared Verify command and the CURRENT source tree; rerun the task's real verify command."})
    return findings


# --------------------------------------------------------------- browser spine


def parse_viewport_spec(raw: str, project: Path) -> tuple[str, dict[str, Any]]:
    name_part, sep, rest = raw.partition("=")
    if not sep:
        raise ForgeError("viewport must use NAME=WIDTHxHEIGHT:SCREENSHOT or NAME=SCREENSHOT")
    name = slugify(name_part).lower()
    size_part = ""
    path_part = rest
    if ":" in rest:
        size_part, path_part = rest.split(":", 1)
    width = height = None
    if size_part:
        match = re.fullmatch(r"(\d+)x(\d+)", size_part)
        if not match:
            path_part = rest
        else:
            width, height = int(match.group(1)), int(match.group(2))
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


def is_local_url(url: str) -> bool:
    return bool(re.search(r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])", url or "", re.IGNORECASE))


def server_lease_origin(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    scheme = parsed.scheme.lower() or "http"
    return f"{scheme}://{host.lower()}:{port}"


def load_server_lease(project: Path) -> dict[str, Any] | None:
    path = project / SERVER_LEASE
    if not path.exists():
        return None
    try:
        return read_json(path)
    except Exception:
        return None


def server_lease_payload(project: Path, args: argparse.Namespace) -> dict[str, Any]:
    port = int(args.port) if args.port else available_port()
    base_url = args.base_url or f"http://127.0.0.1:{port}"
    origin = server_lease_origin(base_url)
    return {
        "schema": "star-forge.server-lease.v1",
        "created_at": now_utc(),
        "updated_at": now_utc(),
        "project": str(project),
        "owner": args.owner or "star-forge",
        "pid": args.pid,
        "port": port,
        "base_url": base_url,
        "origin": origin,
        "command": args.command or "",
        "cleanup_required": True,
        "source_hash": source_hash(project),
        "runtime_asset_hash": live_common.compute_runtime_asset_hash(project, exclude_paths=[project / SERVER_LEASE]),
    }


def cmd_server_lease(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    if args.action == "release":
        existing = load_server_lease(project)
        if (project / SERVER_LEASE).exists():
            (project / SERVER_LEASE).unlink()
        payload = {"schema": "star-forge.server-lease.v1", "action": "release", "released": bool(existing), "previous": existing}
    elif args.action == "status":
        payload = {"schema": "star-forge.server-lease.v1", "action": "status", "lease": load_server_lease(project)}
    else:
        payload = server_lease_payload(project, args)
        write_json(project / SERVER_LEASE, payload)
    print(json.dumps(payload, indent=2))
    return 0


def write_screenshot_manifest(project: Path, *, context: dict[str, Any] | None = None) -> None:
    root = project / SCREENSHOTS_DIR
    entries: list[dict[str, Any]] = []
    if root.exists():
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.name == SCREENSHOT_MANIFEST.name:
                continue
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            entries.append({"path": relative_to_project(path, project), "sha256": file_sha256(path), "bytes": path.stat().st_size, **decode_image_meta(path)})
    write_json(project / SCREENSHOT_MANIFEST, {"schema": "star-forge.screenshot-manifest.v1", "created_at": now_utc(), "context": context or {}, "screenshots": entries})


def browser_run_problem(message: str, *, severity: str = "high", rule: str = "browser-run", path: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"severity": severity, "rule": rule, "message": message}
    if path:
        payload["path"] = path
    return payload


def manifest_artifact_paths(manifest: dict[str, Any] | None) -> set[str]:
    paths: set[str] = set()
    if not isinstance(manifest, dict):
        return paths
    artifacts = manifest.get("artifacts")
    if isinstance(artifacts, dict):
        items = artifacts.values()
    elif isinstance(artifacts, list):
        items = artifacts
    else:
        items = []
    for item in items:
        if isinstance(item, dict) and item.get("path"):
            paths.add(str(item["path"]))
    raw_hashes = manifest.get("raw_artifact_hashes")
    if isinstance(raw_hashes, dict):
        for key, value in raw_hashes.items():
            if isinstance(value, dict) and value.get("path"):
                paths.add(str(value["path"]))
            else:
                paths.add(str(key))
    return paths


def validate_browser_artifact_path(
    project: Path,
    entry: dict[str, Any],
    *,
    task: str,
    manifest: dict[str, Any] | None,
    manifest_paths: set[str],
    problems: list[dict[str, Any]],
) -> Path | None:
    raw_path = str(entry.get("path") or "")
    if not raw_path:
        problems.append(browser_run_problem("browser artifact is missing a path", rule="browser-artifact"))
        return None
    try:
        path = live_common.safe_project_path(project, raw_path, must_exist=False)
    except ValueError as exc:
        problems.append(browser_run_problem(f"browser artifact path is unsafe: {exc}", rule="browser-artifact", path=raw_path))
        return None
    rel = relative_to_project(path, project)
    if not is_task_scoped_live_path(project, path, task, "browser"):
        problems.append(browser_run_problem("browser artifact must be under .starforge/live/<task>/browser/", rule="browser-artifact-scope", path=rel))
    if manifest_paths and rel not in manifest_paths:
        problems.append(browser_run_problem("browser artifact is not recorded in the live manifest", rule="browser-artifact-manifest", path=rel))
    require_raw_hash_for_artifact(project, manifest, path, problems, label="browser artifact", rule="artifact-hash")
    return path


def cmd_browser_run(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    viewports: dict[str, Any] = {}
    problems: list[dict[str, Any]] = []
    snapshot, snapshot_problem = safe_release_snapshot(project)
    if snapshot_problem:
        problems.append(snapshot_problem)
    manifest: dict[str, Any] | None = None
    manifest_path: Path | None = None
    browser_playwright = None
    browser_interaction_paths: list[Path] = []
    browser_allowed_local_origins: tuple[str, ...] = ()
    for raw in args.viewport or []:
        name, entry = parse_viewport_spec(raw, project)
        viewports[name] = entry
        if not entry.get("exists"):
            problems.append(browser_run_problem(f"viewport screenshot does not exist: {entry.get('path')}", rule="screenshot", path=str(entry.get("path") or "")))
        elif args.strict and not entry.get("valid_image"):
            problems.append(browser_run_problem(f"viewport screenshot is not a decodable PNG/JPEG image: {entry.get('path')}", rule="screenshot", path=str(entry.get("path") or "")))
    for raw in args.screenshot or []:
        path = Path(raw)
        candidate = path if path.is_absolute() else project / path
        name = "mobile" if "mobile" in candidate.stem.lower() else ("desktop" if "desktop" in candidate.stem.lower() else f"screenshot-{len(viewports) + 1}")
        entry = artifact_entry(project, candidate, kind="screenshot")
        viewports[name] = entry
        if not entry.get("exists"):
            problems.append(browser_run_problem(f"screenshot does not exist: {entry.get('path')}", rule="screenshot", path=str(entry.get("path") or "")))
        elif args.strict and not entry.get("valid_image"):
            problems.append(browser_run_problem(f"screenshot is not a decodable PNG/JPEG image: {entry.get('path')}", rule="screenshot", path=str(entry.get("path") or "")))
    artifact_lists = {
        "interaction_evidence": args.interaction_evidence or [],
        "console_evidence": args.console_evidence or [],
    }
    artifacts: dict[str, list[dict[str, Any]]] = {}
    for key, values in artifact_lists.items():
        artifacts[key] = []
        for raw in values:
            path = Path(raw)
            candidate = path if path.is_absolute() else project / path
            kind = key.replace("_evidence", "")
            entry = artifact_entry(project, candidate, kind=kind)
            artifacts[key].append(entry)
            if not entry.get("exists"):
                problems.append(browser_run_problem(f"{kind} evidence does not exist: {entry.get('path')}", rule=f"{kind}-evidence", path=str(entry.get("path") or "")))
    if args.degraded:
        problems.append(browser_run_problem("browser run was marked degraded", rule="browser-degraded"))
    if args.require_viewports:
        for required in ["desktop", "mobile"]:
            if required not in viewports:
                problems.append(browser_run_problem(f"browser run requires `{required}` viewport evidence", rule="browser-viewports"))
    if args.require_interaction and not artifacts["interaction_evidence"]:
        problems.append(browser_run_problem("browser run requires interaction evidence", rule="interaction-evidence"))
    if args.require_console and not artifacts["console_evidence"]:
        problems.append(browser_run_problem("browser run requires console evidence", severity="medium", rule="console-evidence"))
    if args.strict and not snapshot_problem:
        if not args.live_manifest:
            problems.append(browser_run_problem("strict browser-run requires --live-manifest from the browser collector", rule="manifest-missing"))
        manifest, manifest_path = load_and_validate_live_manifest(project, args.live_manifest, problems, task=args.task, collector="browser")
        manifest_paths = manifest_artifact_paths(manifest)
        try:
            from live_collectors import browser_playwright
        except Exception as exc:
            browser_playwright = None
            problems.append(browser_run_problem(f"browser artifact validators are unavailable: {exc}", rule="browser-validator"))
        for entry in viewports.values():
            validate_browser_artifact_path(project, entry, task=args.task, manifest=manifest, manifest_paths=manifest_paths, problems=problems)
        for entry in artifacts["console_evidence"]:
            path = validate_browser_artifact_path(project, entry, task=args.task, manifest=manifest, manifest_paths=manifest_paths, problems=problems)
            if path is not None and browser_playwright is not None:
                problems.extend(browser_playwright.validate_console_artifact(path, project))
        for entry in artifacts["interaction_evidence"]:
            path = validate_browser_artifact_path(project, entry, task=args.task, manifest=manifest, manifest_paths=manifest_paths, problems=problems)
            if path is not None and browser_playwright is not None:
                problems.extend(browser_playwright.validate_interaction_artifact(path, project))
                browser_interaction_paths.append(path)
        if manifest is not None:
            summary = live_manifest_summary(manifest)
            if not summary.get("url"):
                problems.append(browser_run_problem("browser live manifest must include URL provenance", rule="browser-url"))
            elif args.url and str(summary.get("url")) != str(args.url):
                problems.append(browser_run_problem("browser-run URL does not match live manifest URL", rule="browser-url"))
    lease = None
    if not snapshot_problem and (args.strict or args.url or args.server_lease or args.require_server_lease):
        try:
            from live_collectors import browser_playwright
            parsed_url, url_problems = browser_playwright.validate_url(args.url or "")
            problems.extend(url_problems)
            if not url_problems:
                current_source = current_live_source_hash(project, problems)
                if current_source is not None:
                    _lease_path, lease, lease_problems = browser_playwright.validate_server_lease(
                        project,
                        str(args.server_lease or ""),
                        parsed_url,
                        current_source,
                        live_common.compute_runtime_asset_hash(project, exclude_paths=[project / SERVER_LEASE]),
                    )
                    problems.extend(lease_problems)
                    if lease:
                        browser_allowed_local_origins = (browser_playwright.normalize_origin(parsed_url),)
        except Exception as exc:
            problems.append(browser_run_problem(f"server lease validation failed: {exc}", rule="server-lease"))
    if args.require_server_lease and not lease:
        problems.append(browser_run_problem("browser run requires a Star Forge server lease", rule="server-lease"))
    if args.server_lease and not lease:
        problems.append(browser_run_problem("--server-lease was passed but no valid lease is claimed", rule="server-lease"))
    if args.strict and browser_playwright is not None:
        for path in browser_interaction_paths:
            problems.extend(browser_playwright.validate_request_safety_artifact(
                path,
                project,
                allowed_local_origins=browser_allowed_local_origins,
            ))
    if viewports:
        write_screenshot_manifest(project, context={"scenario": args.scenario, "url": args.url})
    blocking = blocking_items(problems)
    verdict = "PASS" if not blocking else "FAIL"
    payload = {
        "schema": "star-forge.browser-run.v1",
        "kind": "browser-run",
        "created_at": now_utc(),
        "project": str(project),
        "task": args.task,
        "url": args.url,
        "server_lease": lease,
        "scenario": args.scenario,
        "verdict": verdict,
        "degraded": args.degraded,
        "viewports": viewports,
        "interaction_evidence": artifacts["interaction_evidence"],
        "console_evidence": artifacts["console_evidence"],
        "source_snapshot": snapshot,
        "live_manifest": relative_to_project(manifest_path, project) if manifest_path else None,
        "problems": problems,
        "summary": args.summary,
    }
    path = write_run_record(project, payload)
    payload["artifact"] = relative_to_project(path, project)
    print(json.dumps(payload, indent=2))
    return 0 if verdict == "PASS" or not args.strict else 1


def passing_browser_runs(project: Path, task_id: str | None = None) -> list[dict[str, Any]]:
    return [item for item in load_run_records(project, kind="browser-run", task=task_id) if item.get("verdict") == "PASS"]


def browser_findings(project: Path, tasks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for task in tasks:
        if task.get("status") != "complete" or not task_is_visual(task):
            continue
        if not passing_browser_runs(project, task["id"]):
            findings.append({"severity": "high", "rule": "browser-run-missing", "file": str(RUNS_DIR), "task": task["id"], "message": f"User-facing task {task['id']} needs a passing browser-run with desktop and mobile evidence."})
    return findings


# ------------------------------------------------------------ live proof gates


def live_problem(message: str, *, severity: str = "high", rule: str = "live-proof", path: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"severity": severity, "rule": rule, "message": message}
    if path:
        payload["path"] = path
    return payload


def append_live_problem_once(problems: list[dict[str, Any]], problem: dict[str, Any] | None) -> None:
    if not problem:
        return
    key = (
        str(problem.get("rule") or ""),
        str(problem.get("file") or problem.get("path") or ""),
        str(problem.get("message") or ""),
    )
    for item in problems:
        existing = (
            str(item.get("rule") or ""),
            str(item.get("file") or item.get("path") or ""),
            str(item.get("message") or ""),
        )
        if existing == key:
            return
    problems.append(dict(problem))


def current_live_source_hash(project: Path, problems: list[dict[str, Any]]) -> str | None:
    current, problem = try_source_hash(project)
    if problem:
        append_live_problem_once(problems, problem)
        return None
    return current


def live_has_blockers(problems: Sequence[dict[str, Any]]) -> bool:
    for item in problems:
        if bool(item.get("blocking")):
            return True
        if str(item.get("severity") or "").lower() in BLOCKING_SEVERITIES:
            return True
    return False


def live_rel(project: Path, path: Path) -> str:
    return relative_to_project(path, project)


def is_task_scoped_live_path(project: Path, path: Path, task: str | None, collector: str | None) -> bool:
    try:
        rel = path.resolve().relative_to(project.resolve())
    except ValueError:
        return False
    parts = rel.parts
    if len(parts) < 4 or parts[0] != ".starforge" or parts[1] != "live":
        return False
    if task is not None and parts[2] != live_common.sanitize_segment(task, fallback="task"):
        return False
    if collector is not None and parts[3] != live_common.sanitize_segment(collector, fallback="collector"):
        return False
    return True


def task_from_scoped_live_path(project: Path, path: Path, collector: str | None) -> str | None:
    try:
        rel = path.resolve().relative_to(project.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 4 or parts[0] != ".starforge" or parts[1] != "live":
        return None
    if collector is not None and parts[3] != live_common.sanitize_segment(collector, fallback="collector"):
        return None
    return parts[2]


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
    if require_scoped and not is_task_scoped_live_path(project, path, task, collector):
        problems.append(live_problem(f"{label} must be under .starforge/live/{live_common.sanitize_segment(task or 'task')}/{live_common.sanitize_segment(collector or 'collector')}/", rule="artifact-scope", path=rel))
    if not path.exists():
        problems.append(live_problem(f"{label} does not exist", rule="artifact-missing", path=rel))
        return {"kind": label, "path": rel, "exists": False}, None
    if require_dir:
        if not path.is_dir():
            problems.append(live_problem(f"{label} must be a directory", rule="artifact-shape", path=rel))
        return {"kind": label, "path": rel, "exists": path.exists(), "directory": path.is_dir()}, None
    entry = artifact_entry(project, path, kind="screenshot" if require_image else label)
    payload = None
    if require_json:
        try:
            payload = json_load_path(path)
        except Exception as exc:
            problems.append(live_problem(f"{label} is malformed JSON: {exc}", rule="artifact-json", path=rel))
        else:
            if require_object and not isinstance(payload, dict):
                problems.append(live_problem(f"{label} must be a JSON object", rule="artifact-shape", path=rel))
    if require_image and not entry.get("valid_image"):
        problems.append(live_problem(f"{label} is not a decodable PNG/JPEG image", rule="artifact-image", path=rel))
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
        return
    for item in values:
        if isinstance(item, dict):
            yield item


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


def validate_raw_artifact_hashes(
    project: Path,
    manifest: dict[str, Any],
    problems: list[dict[str, Any]],
    *,
    task: str | None = None,
    collector: str | None = None,
    require_scoped: bool = False,
) -> None:
    raw_hashes = manifest.get("raw_artifact_hashes")
    if not isinstance(raw_hashes, dict):
        return
    for key, value in raw_hashes.items():
        expected = ""
        raw_path = key
        if isinstance(value, dict):
            expected = str(value.get("sha256") or "")
            raw_path = str(value.get("path") or key)
        elif isinstance(value, str):
            expected = value
        if not expected:
            problems.append(live_problem("raw artifact hash entry is missing sha256", rule="artifact-hash", path=str(raw_path)))
            continue
        try:
            path = live_common.safe_project_path(project, raw_path, must_exist=False)
        except ValueError as exc:
            problems.append(live_problem(f"raw artifact hash path is unsafe: {exc}", rule="artifact-path", path=str(raw_path)))
            continue
        rel = live_rel(project, path)
        if require_scoped and not is_task_scoped_live_path(project, path, task, collector):
            problems.append(live_problem("raw artifact hash path must stay under the task-scoped live collector directory", rule="artifact-scope", path=rel))
            continue
        if not path.exists() or not path.is_file():
            problems.append(live_problem("raw artifact hash target is missing", rule="artifact-missing", path=rel))
            continue
        actual = file_sha256(path)
        if actual != expected:
            problems.append(live_problem("raw artifact hash does not match current bytes", rule="artifact-hash", path=rel))


def manifest_raw_hash_for_path(project: Path, manifest: dict[str, Any] | None, path: Path) -> str:
    if not isinstance(manifest, dict):
        return ""
    raw_hashes = manifest.get("raw_artifact_hashes")
    if not isinstance(raw_hashes, dict):
        return ""
    rel = live_rel(project, path)
    for key, value in raw_hashes.items():
        raw_path = str(key)
        sha256 = ""
        if isinstance(value, dict):
            raw_path = str(value.get("path") or key)
            sha256 = str(value.get("sha256") or "")
        elif isinstance(value, str):
            sha256 = value
        if raw_path == rel:
            return sha256
    return ""


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
    if not entry:
        return
    path = str(entry.get("path") or "")
    if path and any(str(item.get("path") or "") == path for item in artifacts):
        return
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


def require_manifest_bound_path(
    project: Path,
    manifest: dict[str, Any] | None,
    raw_path: str | Path | None,
    label: str,
    problems: list[dict[str, Any]],
    *,
    rule: str = "artifact-hash",
) -> None:
    if not raw_path:
        return
    try:
        path = live_common.safe_project_path(project, raw_path, must_exist=False)
    except ValueError as exc:
        problems.append(live_problem(f"{label} path is unsafe: {exc}", rule="artifact-path", path=str(raw_path)))
        return
    require_raw_hash_for_artifact(project, manifest, path, problems, label=label, rule=rule)


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
        if not is_task_scoped_live_path(project, artifact, task, collector):
            problems.append(live_problem("manifest artifact path must stay under the task-scoped live collector directory", rule="artifact-scope", path=artifact_rel))


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
    if collector is not None and payload.get("collector") != live_common.sanitize_segment(collector, fallback="collector"):
        problems.append(live_problem(f"manifest collector must be `{collector}`", rule="manifest-collector", path=rel))
    if task is not None and payload.get("task") != task:
        problems.append(live_problem(f"manifest task must be `{task}`", rule="manifest-task", path=rel))
    if payload.get("degraded") is True:
        problems.append(live_problem("manifest is marked degraded", rule="manifest-degraded", path=rel))
    unavailable = payload.get("unavailable_capabilities")
    if isinstance(unavailable, list) and unavailable:
        problems.append(live_problem("manifest records unavailable required capabilities: " + ", ".join(str(item) for item in unavailable), rule="manifest-unavailable", path=rel))
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
            if value != current_source:
                problems.append(live_problem(f"manifest {field} does not match current source hash", rule="manifest-source", path=rel))
    current_runtime = live_common.compute_runtime_asset_hash(project)
    if str(payload.get("runtime_asset_hash") or "") != current_runtime:
        problems.append(live_problem("manifest runtime_asset_hash does not match current runtime assets", rule="manifest-runtime", path=rel))
    if not isinstance(payload.get("redaction_report"), dict):
        problems.append(live_problem("manifest redaction_report must be an object", rule="manifest-shape", path=rel))
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
        if not artifact.exists():
            problems.append(live_problem("manifest artifact is missing", rule="artifact-missing", path=artifact_rel))
        if record.get("problem"):
            problems.append(live_problem(f"manifest artifact problem: {record.get('problem')}", rule="artifact-problem", path=artifact_rel))
        if record.get("sha256") and artifact.exists() and artifact.is_file() and file_sha256(artifact) != record.get("sha256"):
            problems.append(live_problem("manifest artifact sha256 does not match current bytes", rule="artifact-hash", path=artifact_rel))
    validate_raw_artifact_hashes(project, payload, problems, task=task, collector=collector, require_scoped=require_scoped)
    return payload, path


def live_manifest_summary(manifest: dict[str, Any] | None) -> dict[str, Any]:
    summary = manifest.get("summary") if isinstance(manifest, dict) else {}
    return summary if isinstance(summary, dict) else {}


def result_indicates_failure(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "result must be a JSON object"
    for key in ("timed_out", "timeout", "gui_launch_failed", "screenshot_permission_failed", "cleanup_failed"):
        if payload.get(key) is True:
            return f"{key} is true"
    for key in ("success", "ok", "passed"):
        if key in payload and payload.get(key) is False:
            return f"{key} is false"
    for key in ("returncode", "exit_code"):
        if key in payload:
            try:
                if int(payload.get(key)) != 0:
                    return f"{key} is nonzero"
            except (TypeError, ValueError):
                return f"{key} is not numeric"
    status = str(payload.get("status") or payload.get("conclusion") or "").lower()
    if status in {"failed", "failure", "error", "timed_out", "timeout", "cancelled", "crashed"}:
        return f"status is {status}"
    return None


def json_has_failed_smoke(payload: Any) -> list[str]:
    messages: list[str] = []
    if isinstance(payload, dict):
        for key in ("ok", "passed", "success"):
            if payload.get(key) is False:
                messages.append(f"smoke {key} is false")
        checks = payload.get("checks")
        if isinstance(checks, list):
            for idx, check in enumerate(checks):
                if isinstance(check, dict):
                    for key in ("ok", "passed", "success"):
                        if check.get(key) is False:
                            messages.append(f"smoke check {idx + 1} failed")
                            break
    elif isinstance(payload, list):
        for idx, check in enumerate(payload):
            if isinstance(check, dict) and any(check.get(key) is False for key in ("ok", "passed", "success")):
                messages.append(f"smoke check {idx + 1} failed")
    else:
        messages.append("smoke checks must be a JSON object or array")
    return messages


def unsafe_preview_url_reason(url: str, *, allow_local: bool = False) -> str | None:
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in {"http", "https"}:
        return "preview URL must use http or https"
    if parsed.username or parsed.password:
        return "preview URL must not include credentials"
    host = parsed.hostname or ""
    if not host:
        return "preview URL must include a host"
    if host.lower() == "metadata.google.internal":
        return "preview URL must not target metadata hosts"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    is_local = host.lower() in {"localhost"} or bool(ip and ip.is_loopback)
    if is_local and not allow_local:
        return "preview URL must not target localhost without a local preview proof"
    if ip and not (allow_local and ip.is_loopback):
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or not ip.is_global:
            return "preview URL must not target non-global, private, loopback, link-local, reserved, or multicast IPs"
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    for key, value in query:
        lowered = f"{key}={value}".lower()
        if any(marker in lowered for marker in ("token", "secret", "password", "api_key", "apikey", "auth")):
            return "preview URL query appears to contain sensitive material"
    return None


def preview_url_safety_problems(url: str, *, allow_local: bool = False) -> list[dict[str, Any]]:
    try:
        from live_collectors import preview as preview_collector
        return [
            live_problem(
                str(item.get("message") or "preview URL is unsafe"),
                severity=str(item.get("severity") or "high"),
                rule=str(item.get("rule") or "preview-url"),
                path=str(item.get("path") or ""),
            )
            for item in preview_collector.validate_url_safety(url, allow_local=allow_local)
            if isinstance(item, dict)
        ]
    except Exception:
        reason = unsafe_preview_url_reason(url, allow_local=allow_local)
        return [live_problem(reason, rule="preview-url")] if reason else []


def preview_connected_ip_problems(url: str, raw_ips: Any, *, allow_local: bool, path: str = "") -> list[dict[str, Any]]:
    if raw_ips is None:
        return [live_problem("preview HTTP connected IP evidence must be a non-empty list", rule="preview-url", path=path)]
    if not isinstance(raw_ips, list) or not raw_ips:
        return [live_problem("preview HTTP connected IP evidence must be a non-empty list", rule="preview-url", path=path)]
    parsed = urllib.parse.urlparse(url or "")
    host = parsed.hostname or ""
    explicit_local = host.lower() in {"localhost"}
    try:
        host_ip = ipaddress.ip_address(host.strip("[]"))
        explicit_local = explicit_local or host_ip.is_loopback
    except ValueError:
        pass
    problems: list[dict[str, Any]] = []
    try:
        from live_collectors import preview as preview_collector
        for raw in raw_ips:
            ip = preview_collector.parse_ip(str(raw))
            if ip is None:
                problems.append(live_problem("preview HTTP connected IP evidence contains an invalid address", rule="preview-url", path=path))
                continue
            blocked = preview_collector.is_blocked_ip(ip, explicit_local_allowed=explicit_local and allow_local)
            if blocked:
                problems.append(live_problem(f"preview HTTP connected to unsafe address {ip}: {blocked}", rule="preview-url", path=path))
    except Exception:
        for raw in raw_ips:
            try:
                ip = ipaddress.ip_address(str(raw).strip("[]"))
            except ValueError:
                problems.append(live_problem("preview HTTP connected IP evidence contains an invalid address", rule="preview-url", path=path))
                continue
            blocked = unsafe_preview_url_reason(f"http://{ip}/", allow_local=explicit_local and allow_local)
            if blocked:
                problems.append(live_problem(f"preview HTTP connected to unsafe address {ip}: {blocked}", rule="preview-url", path=path))
    return problems


def preview_https_sni_safe_pinning(url: str, pinning: Any) -> bool:
    if not isinstance(pinning, dict):
        return False
    strategy = str(pinning.get("strategy") or "")
    if strategy not in {"https-connect-vetted-ip-sni-safe", "https-sni-safe-vetted-ip"}:
        return False
    if pinning.get("sni_safe") is not True and pinning.get("tls_sni_safe") is not True:
        return False
    parsed = urllib.parse.urlparse(url or "")
    host = parsed.hostname or ""
    sni_host = str(pinning.get("server_hostname") or pinning.get("sni_hostname") or pinning.get("hostname") or "")
    return bool(host and sni_host and sni_host.lower() == host.lower())


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
    if http_payload.get("schema") != "star-forge.preview-http.v1":
        problems.append(live_problem("preview HTTP evidence must use schema star-forge.preview-http.v1", rule="preview-http", path=path))
    if http_payload.get("attempted") is not True:
        problems.append(live_problem("preview HTTP evidence must record attempted=true", rule="preview-http", path=path))
    if http_payload.get("ok") is not True:
        problems.append(live_problem("preview HTTP evidence must record ok=true", rule="preview-http", path=path))
    if str(http_payload.get("method") or "").upper() != "GET":
        problems.append(live_problem("preview HTTP evidence must record method GET", rule="preview-http", path=path))
    http_url = str(http_payload.get("url") or "")
    if not http_url:
        problems.append(live_problem("preview HTTP evidence must record the requested URL", rule="preview-url", path=path))
    else:
        if http_url != str(url):
            problems.append(live_problem("preview HTTP evidence URL does not match proof URL", rule="preview-url", path=path))
        if summary_url and http_url != summary_url:
            problems.append(live_problem("preview HTTP evidence URL does not match live manifest URL", rule="preview-url", path=path))
    if http_payload.get("expected_status") != expect_status:
        problems.append(live_problem(f"preview HTTP expected_status must equal {expect_status}", rule="preview-status", path=path))
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
                    problems.append(live_problem(f"preview HTTP artifact recorded a blocking problem: {message}", severity=str(item.get("severity") or "high"), rule=str(item.get("rule") or "preview-http"), path=str(item.get("path") or path)))
    pinning = http_payload.get("connection_pinning")
    if not isinstance(pinning, dict):
        problems.append(live_problem("preview HTTP evidence must include connection_pinning", rule="preview-http", path=path))
    else:
        pinning_url = http_url or str(url)
        scheme = urllib.parse.urlparse(pinning_url).scheme
        strategy = str(pinning.get("strategy") or "")
        if scheme == "http":
            if strategy != "http-connect-vetted-ip":
                problems.append(live_problem("preview HTTP connection_pinning.strategy must be http-connect-vetted-ip", rule="preview-http", path=path))
        elif scheme == "https":
            problems.append(live_problem("HTTPS preview evidence is rejected until verifiable SNI pinning is available from the collector", rule="preview-http", path=path))
    return problems


def preview_url_requires_server_lease(url: str) -> bool:
    try:
        from live_collectors import browser_playwright
        parsed, url_problems = browser_playwright.validate_url(url or "")
        if url_problems:
            return False
        requires_lease, _safety_problems = browser_playwright.unsafe_url_reasons(parsed)
        return bool(requires_lease)
    except Exception:
        parsed = urllib.parse.urlparse(url or "")
        host = parsed.hostname or ""
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host.strip("[]")).is_loopback
        except ValueError:
            return False


def preview_manifest_server_lease_path(project: Path, manifest: dict[str, Any] | None) -> Path | None:
    summary = live_manifest_summary(manifest)
    for key in ("server_lease_artifact", "server_lease_path"):
        raw = summary.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                return live_common.safe_project_path(project, raw, must_exist=False)
            except ValueError:
                return None
    raw_summary_lease = summary.get("server_lease")
    if isinstance(raw_summary_lease, str) and raw_summary_lease.strip():
        try:
            return live_common.safe_project_path(project, raw_summary_lease, must_exist=False)
        except ValueError:
            return None
    return manifest_artifact_path_for_kind(project, manifest, "server_lease", "server-lease", "server lease")


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
    has_source_binding = any(str(deployment.get(key) or "") for key in ("source_hash", "sourceHash", "source_hash_after", "sourceHashAfter"))
    if current_source_hash is not None:
        for key in ("source_hash", "sourceHash", "source_hash_after", "sourceHashAfter"):
            if str(deployment.get(key) or "") == current_source_hash:
                return not dirty_paths_missing_from_source_snapshot(project)
    head = git_head(project)
    if head and tree_clean_for_commit_binding(project):
        for key in ("commit_sha", "commitSha", "head_sha", "headSha", "git_head", "gitHead"):
            if str(deployment.get(key) or "") == head:
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
    blocking = live_has_blockers(problems)
    verdict = "FAIL" if blocking else "PASS"
    safe_inputs = {key: value for key, value in inputs.items() if key != "func"}
    payload = {
        "schema": f"star-forge.{kind}.v1",
        "kind": kind,
        "created_at": now_utc(),
        "project": str(project),
        "task": task,
        "strict": bool(strict),
        "inputs": redact(safe_inputs),
        "verdict": verdict,
        "source_snapshot": snapshot,
        "runtime_asset_hash": live_common.compute_runtime_asset_hash(project),
        "manifest": live_rel(project, manifest_path) if manifest_path else None,
        "collector": manifest.get("collector") if isinstance(manifest, dict) else None,
        "artifacts": artifacts or [],
        "problems": problems,
        "summary": summary,
    }
    path = write_run_record(project, payload)
    payload["artifact"] = live_rel(project, path)
    print(json.dumps(payload, indent=2))
    return 0 if verdict == "PASS" or not strict else 1


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
    allow_local = preview_allow_local_for_url(
        project,
        task=task,
        url=url,
        manifest=manifest,
        artifacts=artifacts,
        problems=problems,
        lease_cache=lease_cache,
    )
    problems.extend(preview_url_safety_problems(url, allow_local=allow_local))
    http_entry, http_payload = validate_manifest_bound_artifact_arg(
        project,
        str(manifest_path.parent / "http.json"),
        "http evidence",
        problems,
        manifest=manifest,
        task=task,
        collector="preview",
        require_json=True,
        require_object=True,
    )
    append_artifact_once(artifacts, http_entry)
    if isinstance(http_payload, dict):
        http_path = http_entry.get("path", "") if http_entry else ""
        if strict:
            problems.extend(strict_preview_http_artifact_problems(
                url=url,
                summary_url=str(summary.get("url") or ""),
                expect_status=expect_status,
                http_payload=http_payload,
                path=http_path,
                allow_local=allow_local,
            ))
        status = http_payload.get("status") if "status" in http_payload else http_payload.get("status_code")
        if status != expect_status:
            problems.append(live_problem(f"http status {status} did not match expected {expect_status}", rule="preview-status", path=http_path))
        connected_url = str(http_payload.get("final_url") or url)
        connected_allow_local = preview_allow_local_for_url(
            project,
            task=task,
            url=connected_url,
            manifest=manifest,
            artifacts=artifacts,
            problems=problems,
            lease_cache=lease_cache,
        )
        if strict or http_payload.get("connected_ips") is not None:
            problems.extend(preview_connected_ip_problems(
                connected_url,
                http_payload.get("connected_ips"),
                allow_local=connected_allow_local,
                path=http_path,
            ))
        for key in ("final_url", "redirect_url"):
            if http_payload.get(key):
                checked_url = str(http_payload.get(key))
                checked_allow_local = preview_allow_local_for_url(
                    project,
                    task=task,
                    url=checked_url,
                    manifest=manifest,
                    artifacts=artifacts,
                    problems=problems,
                    lease_cache=lease_cache,
                )
                for item in preview_url_safety_problems(checked_url, allow_local=checked_allow_local):
                    problems.append(live_problem(f"{key} is unsafe: {item.get('message')}", rule="preview-redirect", path=http_path))
        redirects = http_payload.get("redirect_chain")
        if isinstance(redirects, list):
            for idx, item in enumerate(redirects):
                redirect_url = item.get("url") if isinstance(item, dict) else item
                if redirect_url:
                    redirect_text = str(redirect_url)
                    redirect_allow_local = preview_allow_local_for_url(
                        project,
                        task=task,
                        url=redirect_text,
                        manifest=manifest,
                        artifacts=artifacts,
                        problems=problems,
                        lease_cache=lease_cache,
                    )
                    for problem_item in preview_url_safety_problems(redirect_text, allow_local=redirect_allow_local):
                        problems.append(live_problem(f"redirect {idx + 1} is unsafe: {problem_item.get('message')}", rule="preview-redirect", path=http_path))
    deployment_entry, deployment_payload = validate_manifest_bound_artifact_arg(
        project,
        deployment_metadata,
        "deployment metadata",
        problems,
        manifest=manifest,
        task=task,
        collector="preview",
        require_json=True,
        require_object=True,
    )
    append_artifact_once(artifacts, deployment_entry)
    current_source = current_live_source_hash(project, problems)
    if deployment_payload is not None and not deployment_bound_to_current(project, deployment_payload, current_source_hash=current_source):
        problems.append(live_problem("deployment metadata is not bound to the current source", rule="preview-source-binding", path=deployment_entry.get("path", "") if deployment_entry else ""))
    smoke_entry, smoke_payload = validate_manifest_bound_artifact_arg(
        project,
        smoke_checks,
        "smoke checks",
        problems,
        manifest=manifest,
        task=task,
        collector="preview",
        require_json=True,
    )
    append_artifact_once(artifacts, smoke_entry)
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
    return write_live_proof_record(
        project,
        kind="preview-proof",
        task=args.task,
        strict=args.strict,
        inputs=vars(args),
        problems=problems,
        manifest_path=manifest_resolved,
        manifest=manifest,
        artifacts=artifacts,
        summary="preview proof",
    )


def collector_for_profile(profile: str) -> str | None:
    mapping = {
        "preview": "preview",
        "native-ios": "native-ios",
        "native-macos": "native-macos",
        "security": "security",
        "dependency-audit": "security",
        "security-deep": "security",
        "security-diff": "security",
        "vulnerability-fix": "security",
        "production-review": "github",
        "github-pr-review": "github",
    }
    return mapping.get(profile)


def dedicated_strict_proof_command_for_profile(profile: str) -> str:
    if profile in {"security", *SECURITY_PROFILES}:
        return "security-proof"
    if profile == "native-ios":
        return "native-ios-proof"
    if profile == "native-macos":
        return "native-macos-proof"
    return ""


def cmd_proof_run(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    problems: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    profile = str(args.profile or "").strip()
    github_source_profiles = {"production-review", "github-pr-review"}
    collector = collector_for_profile(profile)
    manifest, manifest_path = load_and_validate_live_manifest(project, args.artifact, problems, task=args.task, collector=collector, require_scoped=collector is not None)
    if not profile:
        problems.append(live_problem("proof-run requires --profile", rule="proof-profile"))
    elif profile in github_source_profiles:
        problems.append(live_problem(
            "proof-run does not accept GitHub source packet profiles; use source-packet-proof or source-packet-github-pr-review",
            rule="proof-profile",
        ))
    dedicated_command = dedicated_strict_proof_command_for_profile(profile)
    if args.strict and dedicated_command:
        problems.append(live_problem(
            f"strict proof-run for profile {profile} must use {dedicated_command}",
            rule="proof-profile",
        ))
    if profile == "preview" and manifest_path is not None:
        summary = live_manifest_summary(manifest)
        expected_status = summary.get("expected_status") if "expected_status" in summary else summary.get("status")
        try:
            expected_status_int = int(expected_status if expected_status is not None else 200)
        except (TypeError, ValueError):
            expected_status_int = 200
            problems.append(live_problem("preview manifest expected status is not numeric", rule="preview-status"))
        artifacts.extend(validate_preview_proof_artifacts(
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
    return write_live_proof_record(
        project,
        kind="proof-run",
        task=args.task,
        strict=args.strict,
        inputs=vars(args),
        problems=problems,
        manifest_path=manifest_path,
        manifest=manifest,
        artifacts=artifacts,
        summary=f"profile={args.profile}",
    )


NATIVE_IOS_RESULT_SCHEMA = "star-forge.native-ios.result.v1"


def validate_native_ios_result_artifact(
    project: Path,
    manifest: dict[str, Any] | None,
    payload: Any,
    entry: dict[str, Any] | None,
    *,
    expected_kind: str,
    label: str,
    scheme: str,
    simulator: str,
    problems: list[dict[str, Any]],
) -> None:
    path = entry.get("path", "") if entry else ""
    if not isinstance(payload, dict):
        return
    summary = live_manifest_summary(manifest)
    simulator_summary = summary.get("simulator") if isinstance(summary.get("simulator"), dict) else {}
    expected_runtime = str(summary.get("simulator_runtime") or simulator_summary.get("runtime") or "").strip()
    expected_udid = str(summary.get("simulator_udid") or simulator_summary.get("udid") or "").strip()
    try:
        from live_collectors import native_ios
        native_ios.validate_result_artifact_contract(
            label,
            expected_kind,
            payload,
            problems,
            path=path,
            expected_runtime=expected_runtime,
            expected_udid=expected_udid,
        )
    except Exception as exc:
        problems.append(live_problem(f"{label} command validation failed: {exc}", rule="native-ios-result", path=path))

    result_scheme = str(payload.get("scheme") or payload.get("scheme_name") or "").strip()
    if result_scheme and result_scheme != scheme:
        problems.append(live_problem(f"{label} scheme does not match proof input", rule="native-ios-result", path=path))
    result_simulator = str(payload.get("simulator") or payload.get("device") or payload.get("destination") or "").strip()
    if result_simulator and simulator and simulator not in result_simulator and result_simulator not in simulator:
        problems.append(live_problem(f"{label} simulator does not match proof input", rule="native-ios-result", path=path))


def cmd_native_ios_proof(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    problems: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    if not str(args.scheme or "").strip():
        problems.append(live_problem("native iOS proof requires --scheme", rule="native-ios-scheme"))
    if not str(args.simulator or "").strip():
        problems.append(live_problem("native iOS proof requires --simulator", rule="native-ios-simulator"))
    manifest_path = default_live_manifest_path(project, args.task, "native-ios")
    manifest, manifest_resolved = load_and_validate_live_manifest(project, manifest_path, problems, task=args.task, collector="native-ios")
    summary = live_manifest_summary(manifest)
    session_payload: Any | None = None
    transcript_payload: Any | None = None
    for filename in ("session-defaults.json", "mcp-transcript.json"):
        entry, payload = validate_manifest_bound_artifact_arg(
            project,
            str(manifest_path.parent / filename),
            filename,
            problems,
            manifest=manifest,
            task=args.task,
            collector="native-ios",
            require_json=True,
            require_object=True,
        )
        append_artifact_once(artifacts, entry)
        if filename == "session-defaults.json":
            session_payload = payload
        if filename == "mcp-transcript.json":
            transcript_payload = payload
    for label, raw in (("build result", args.build_result), ("launch result", args.launch_result), ("test result", args.test_result)):
        entry, payload = validate_manifest_bound_artifact_arg(
            project,
            raw,
            label,
            problems,
            manifest=manifest,
            task=args.task,
            collector="native-ios",
            require_json=True,
            require_object=True,
        )
        append_artifact_once(artifacts, entry)
        if args.strict:
            validate_native_ios_result_artifact(
                project,
                manifest,
                payload,
                entry,
                expected_kind=label.split()[0],
                label=label,
                scheme=args.scheme,
                simulator=args.simulator,
                problems=problems,
            )
        failure = result_indicates_failure(payload)
        if failure:
            problems.append(live_problem(f"{label} failed: {failure}", rule="native-ios-result", path=entry.get("path", "") if entry else ""))
    screenshot_entry = None
    snapshot_entry = None
    if args.screenshot:
        screenshot_entry, _ = validate_manifest_bound_artifact_arg(
            project,
            args.screenshot,
            "screenshot",
            problems,
            manifest=manifest,
            task=args.task,
            collector="native-ios",
            require_image=True,
        )
        append_artifact_once(artifacts, screenshot_entry)
    if args.ui_snapshot:
        snapshot_entry, _ = validate_manifest_bound_artifact_arg(
            project,
            args.ui_snapshot,
            "ui snapshot",
            problems,
            manifest=manifest,
            task=args.task,
            collector="native-ios",
            require_json=True,
            require_object=True,
        )
        append_artifact_once(artifacts, snapshot_entry)
    if not screenshot_entry and not snapshot_entry:
        problems.append(live_problem("native iOS proof requires screenshot or UI snapshot evidence", rule="native-ios-ui"))
    if session_payload is not None and transcript_payload is not None:
        try:
            from live_collectors import native_ios
            validator_args = argparse.Namespace(
                mcp_server="",
                mcp_version="",
                agent_id="",
                mcp_unavailable=False,
                manifest_mcp_provenance=None,
                manifest_source_hash="",
            )
            native_ios.validate_session_defaults(
                session_payload,
                scheme=args.scheme,
                simulator=args.simulator,
                runtime="",
                udid="",
                problems=problems,
            )
            current_source = current_live_source_hash(project, problems)
            if current_source is not None:
                _transcript_summary, transcript_problems, _unavailable = native_ios.validate_transcript(
                    transcript_payload,
                    session_payload,
                    scheme=args.scheme,
                    simulator=args.simulator,
                    current_source_hash=current_source,
                    has_screenshot=screenshot_entry is not None,
                    has_ui_snapshot=snapshot_entry is not None,
                    args=validator_args,
                )
                problems.extend(dict(item) for item in transcript_problems)
        except Exception as exc:
            problems.append(live_problem(f"native iOS transcript validation failed: {exc}", rule="native-ios-transcript"))
    if not summary.get("app_identity"):
        problems.append(live_problem("native iOS manifest summary must include app_identity", rule="native-ios-app-identity"))
    return write_live_proof_record(
        project,
        kind="native-ios-proof",
        task=args.task,
        strict=args.strict,
        inputs=vars(args),
        problems=problems,
        manifest_path=manifest_resolved,
        manifest=manifest,
        artifacts=artifacts,
        summary="native iOS proof",
    )


def validate_native_macos_runtime(project: Path, manifest: dict[str, Any] | None, payload: Any, entry: dict[str, Any] | None, problems: list[dict[str, Any]]) -> None:
    path = entry.get("path", "") if entry else ""
    if not isinstance(payload, dict):
        return
    required = ("pid", "readiness", "termination", "cleanup", "stdout_artifact", "stderr_artifact")
    missing = [key for key in required if key not in payload]
    if missing:
        problems.append(live_problem("run result is missing runtime observation fields: " + ", ".join(missing), rule="native-macos-runtime", path=path))
    if payload.get("success") is not True:
        problems.append(live_problem("run result must report success true", rule="native-macos-result", path=path))
    if payload.get("timed_out") is True:
        problems.append(live_problem("run result timed out", rule="native-macos-run-timeout", path=path))
    if payload.get("gui_launch_failed") is True:
        problems.append(live_problem("run result reports GUI launch failure", rule="native-macos-gui-launch", path=path))
    if payload.get("cleanup_failed") is True:
        problems.append(live_problem("run result reports cleanup failure", rule="native-macos-cleanup", path=path))
    readiness = payload.get("readiness")
    if not isinstance(readiness, dict):
        problems.append(live_problem("run result readiness must be an object", rule="native-macos-readiness", path=path))
    elif str(readiness.get("status") or "") not in {"observed", "not_requested"}:
        problems.append(live_problem("run result readiness was not observed", rule="native-macos-readiness", path=path))
    cleanup = payload.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("success") is not True:
        problems.append(live_problem("run result cleanup did not succeed", rule="native-macos-cleanup", path=path))
    termination = payload.get("termination")
    if not isinstance(termination, dict):
        problems.append(live_problem("run result termination must be an object", rule="native-macos-runtime", path=path))
    for key in ("stdout_artifact", "stderr_artifact"):
        raw = payload.get(key)
        if not raw:
            continue
        try:
            artifact = live_common.safe_project_path(project, raw, must_exist=True)
        except ValueError as exc:
            problems.append(live_problem(f"{key} is invalid: {exc}", rule="native-macos-runtime", path=str(raw)))
            continue
        if not artifact.is_file():
            problems.append(live_problem(f"{key} must point to a file", rule="native-macos-runtime", path=relative_to_project(artifact, project)))
            continue
        require_raw_hash_for_artifact(project, manifest, artifact, problems, label=key, rule="native-macos-runtime")


NATIVE_MACOS_RESULT_SCHEMA = "star-forge.native-macos.result.v1"


def numeric_field(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def validate_native_macos_result_artifact(
    project: Path,
    manifest: dict[str, Any] | None,
    payload: Any,
    entry: dict[str, Any] | None,
    *,
    expected_kind: str,
    label: str,
    problems: list[dict[str, Any]],
) -> None:
    path = entry.get("path", "") if entry else ""
    if not isinstance(payload, dict):
        return
    if payload.get("schema") != NATIVE_MACOS_RESULT_SCHEMA:
        problems.append(live_problem(f"{label} must use schema {NATIVE_MACOS_RESULT_SCHEMA}", rule="native-macos-result", path=path))
    if str(payload.get("kind") or "") != expected_kind:
        problems.append(live_problem(f"{label} kind must be {expected_kind}", rule="native-macos-result", path=path))
    command_argv = payload.get("command_argv")
    valid_command_argv: list[str] | None = None
    if (
        isinstance(command_argv, list)
        and command_argv
        and all(isinstance(item, str) and item and "\0" not in item for item in command_argv)
    ):
        valid_command_argv = list(command_argv)
    else:
        problems.append(live_problem(f"{label} must include a non-empty command_argv array", rule="native-macos-result", path=path))
    if valid_command_argv is not None:
        for issue in native_macos_collector.validate_argv(valid_command_argv, label):
            issue = dict(issue)
            if path and not issue.get("path"):
                issue["path"] = path
            problems.append(issue)
    if payload.get("shell") is not False:
        problems.append(live_problem(f"{label} must record shell exactly false", rule="native-macos-result", path=path))
    if payload.get("cwd") != ".":
        problems.append(live_problem(f"{label} must record cwd '.'", rule="native-macos-result", path=path))
    if valid_command_argv is not None and not str(payload.get("executable_path") or ""):
        problems.append(live_problem(f"{label} must include executable_path", rule="native-macos-result", path=path))

    timeout = numeric_field(payload, "timeout_seconds")
    if timeout is None or timeout <= 0 or timeout > 24 * 60 * 60:
        problems.append(live_problem(f"{label} timeout_seconds must be a sane positive number", rule="native-macos-result", path=path))
    duration = numeric_field(payload, "duration_seconds")
    if duration is None or duration < 0:
        problems.append(live_problem(f"{label} duration_seconds must be a non-negative number", rule="native-macos-result", path=path))
    elif timeout is not None and duration > timeout + 60:
        problems.append(live_problem(f"{label} duration_seconds is not consistent with timeout_seconds", rule="native-macos-result", path=path))

    artifact_keys = ("stdout_artifact", "stderr_artifact")
    if valid_command_argv is not None and not any(payload.get(key) for key in artifact_keys):
        problems.append(live_problem(f"{label} must include stdout or stderr artifact evidence", rule="native-macos-result", path=path))
    for key in artifact_keys:
        raw = payload.get(key)
        if not raw:
            continue
        try:
            artifact = live_common.safe_project_path(project, raw, must_exist=True)
        except ValueError as exc:
            problems.append(live_problem(f"{label} {key} is invalid: {exc}", rule="native-macos-result", path=str(raw)))
            continue
        if not artifact.is_file():
            problems.append(live_problem(f"{label} {key} must point to a file", rule="native-macos-result", path=relative_to_project(artifact, project)))
            continue
        require_raw_hash_for_artifact(project, manifest, artifact, problems, label=f"{label} {key}", rule="native-macos-result")


def validate_native_macos_note(payload: Any, kind: str, entry: dict[str, Any] | None, problems: list[dict[str, Any]]) -> None:
    path = entry.get("path", "") if entry else ""
    if not isinstance(payload, dict):
        return
    if payload.get("schema") != "star-forge.native-macos.note.v1":
        problems.append(live_problem(f"{kind} note has an unexpected schema", rule="native-macos-note", path=path))
    if payload.get("kind") != kind:
        problems.append(live_problem(f"{kind} note kind does not match", rule="native-macos-note", path=path))
    if payload.get("metadata_only") is not True:
        problems.append(live_problem(f"{kind} note must be metadata-only", rule="native-macos-note", path=path))
    if payload.get("status") != "not_checked":
        problems.append(live_problem(f"{kind} note status must be not_checked", rule="native-macos-note", path=path))


def validate_native_macos_metadata(payload: Any, app_name: str, bundle_id: str, app_bundle_entry: dict[str, Any] | None, entry: dict[str, Any] | None, problems: list[dict[str, Any]]) -> None:
    path = entry.get("path", "") if entry else ""
    if not isinstance(payload, dict):
        return
    if payload.get("schema") != "star-forge.native-macos.app-bundle-metadata.v1":
        problems.append(live_problem("app bundle metadata has an unexpected schema", rule="native-macos-bundle-metadata", path=path))
    if payload.get("metadata_only") is not True:
        problems.append(live_problem("app bundle metadata must be metadata-only", rule="native-macos-bundle-metadata", path=path))
    if payload.get("valid") is not True:
        problems.append(live_problem("app bundle metadata is not valid", rule="native-macos-bundle-metadata", path=path))
    if bundle_id and payload.get("bundle_id") != bundle_id:
        problems.append(live_problem("app bundle metadata bundle id does not match proof input", rule="native-macos-app-identity", path=path))
    if app_name:
        valid_names = {str(payload.get("app_name") or ""), str(payload.get("display_name") or "")}
        bundle_path = str(payload.get("app_bundle") or "")
        if bundle_path:
            valid_names.add(Path(bundle_path).stem)
        if app_name not in valid_names:
            problems.append(live_problem("app bundle metadata app name does not match proof input", rule="native-macos-app-identity", path=path))
    if payload.get("executable_exists") is not True:
        problems.append(live_problem("app bundle metadata executable is missing", rule="native-macos-bundle-metadata", path=path))
    if app_bundle_entry and app_bundle_entry.get("path") and payload.get("app_bundle") and str(app_bundle_entry.get("path")) != str(payload.get("app_bundle")):
        problems.append(live_problem("app bundle metadata path does not match --app-bundle", rule="native-macos-bundle-metadata", path=path))


def validate_native_macos_app_bundle(project: Path, raw_path: str, app_name: str, bundle_id: str, entry: dict[str, Any] | None, problems: list[dict[str, Any]]) -> None:
    path_text = entry.get("path", "") if entry else str(raw_path or "")
    if not raw_path:
        problems.append(live_problem("native macOS proof requires --app-bundle", rule="native-macos-bundle"))
        return
    try:
        bundle = live_common.safe_project_path(project, raw_path, must_exist=True)
    except ValueError as exc:
        problems.append(live_problem(f"app bundle path is invalid: {exc}", rule="native-macos-bundle", path=path_text))
        return
    if not bundle.is_dir() or bundle.suffix != ".app":
        problems.append(live_problem("app bundle must be an existing .app directory", rule="native-macos-bundle", path=path_text))
        return
    info_plist = bundle / "Contents" / "Info.plist"
    if not info_plist.exists():
        problems.append(live_problem("app bundle is missing Contents/Info.plist", rule="native-macos-bundle-metadata", path=relative_to_project(info_plist, project)))
        return
    try:
        with info_plist.open("rb") as handle:
            plist = plistlib.load(handle)
    except Exception as exc:
        problems.append(live_problem(f"app bundle Info.plist is malformed: {exc}", rule="native-macos-bundle-metadata", path=relative_to_project(info_plist, project)))
        return
    if not isinstance(plist, dict):
        problems.append(live_problem("app bundle Info.plist must be a dictionary", rule="native-macos-bundle-metadata", path=relative_to_project(info_plist, project)))
        return
    found_bundle_id = str(plist.get("CFBundleIdentifier") or "")
    found_executable = str(plist.get("CFBundleExecutable") or "")
    found_name = str(plist.get("CFBundleName") or bundle.stem)
    found_display_name = str(plist.get("CFBundleDisplayName") or "")
    if bundle_id and found_bundle_id != bundle_id:
        problems.append(live_problem("app bundle Info.plist bundle id does not match proof input", rule="native-macos-app-identity", path=relative_to_project(info_plist, project)))
    if app_name and app_name not in {found_name, found_display_name, bundle.stem}:
        problems.append(live_problem("app bundle Info.plist app name does not match proof input", rule="native-macos-app-identity", path=relative_to_project(info_plist, project)))
    if not found_bundle_id:
        problems.append(live_problem("app bundle Info.plist is missing CFBundleIdentifier", rule="native-macos-bundle-metadata", path=relative_to_project(info_plist, project)))
    if not found_executable:
        problems.append(live_problem("app bundle Info.plist is missing CFBundleExecutable", rule="native-macos-bundle-metadata", path=relative_to_project(info_plist, project)))
    elif not (bundle / "Contents" / "MacOS" / found_executable).is_file():
        problems.append(live_problem("app bundle executable is missing", rule="native-macos-bundle-metadata", path=relative_to_project(bundle / "Contents" / "MacOS" / found_executable, project)))


def cmd_native_macos_proof(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    problems: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    if not str(args.app_name or "").strip() and not str(args.bundle_id or "").strip():
        problems.append(live_problem("native macOS proof requires --app-name or --bundle-id", rule="native-macos-app-identity"))
    manifest_path = default_live_manifest_path(project, args.task, "native-macos")
    manifest, manifest_resolved = load_and_validate_live_manifest(project, manifest_path, problems, task=args.task, collector="native-macos")
    for label, raw, optional in (
        ("build result", args.build_result, False),
        ("run result", args.run_result, False),
        ("test result", args.test_result, True),
    ):
        expected_kind = label.split()[0]
        entry, payload = validate_manifest_bound_artifact_arg(
            project,
            raw,
            label,
            problems,
            manifest=manifest,
            task=args.task,
            collector="native-macos",
            require_json=True,
            require_object=True,
            optional=optional,
        )
        append_artifact_once(artifacts, entry)
        if payload is not None:
            if args.strict:
                validate_native_macos_result_artifact(
                    project,
                    manifest,
                    payload,
                    entry,
                    expected_kind=expected_kind,
                    label=label,
                    problems=problems,
                )
            failure = result_indicates_failure(payload)
            if failure:
                problems.append(live_problem(f"{label} failed: {failure}", rule="native-macos-result", path=entry.get("path", "") if entry else ""))
            if label == "run result":
                validate_native_macos_runtime(project, manifest, payload, entry, problems)
    if args.screenshot:
        entry, _ = validate_manifest_bound_artifact_arg(
            project,
            args.screenshot,
            "screenshot",
            problems,
            manifest=manifest,
            task=args.task,
            collector="native-macos",
            require_image=True,
        )
        append_artifact_once(artifacts, entry)
    if args.strict:
        screenshot_result = manifest_artifact_path_for_kind(project, manifest, "screenshot-result", "screenshot_result")
        if screenshot_result is not None:
            entry, payload = validate_manifest_bound_artifact_arg(
                project,
                screenshot_result,
                "screenshot result",
                problems,
                manifest=manifest,
                task=args.task,
                collector="native-macos",
                require_json=True,
                require_object=True,
            )
            append_artifact_once(artifacts, entry)
            validate_native_macos_result_artifact(
                project,
                manifest,
                payload,
                entry,
                expected_kind="screenshot",
                label="screenshot result",
                problems=problems,
            )
    app_bundle_entry = None
    if args.app_bundle:
        entry, _ = validate_artifact_arg(project, args.app_bundle, "app bundle", problems, task=args.task, collector="native-macos", require_scoped=False, require_dir=True)
        if entry:
            artifacts.append(entry)
            app_bundle_entry = entry
        if not str(args.app_bundle).endswith(".app"):
            problems.append(live_problem("app bundle path must end in .app", rule="native-macos-bundle", path=entry.get("path", "") if entry else ""))
    validate_native_macos_app_bundle(project, args.app_bundle, args.app_name, args.bundle_id, app_bundle_entry, problems)
    metadata_entry, metadata_payload = validate_manifest_bound_artifact_arg(
        project,
        str(manifest_path.parent / "app-bundle-metadata.json"),
        "app bundle metadata",
        problems,
        manifest=manifest,
        task=args.task,
        collector="native-macos",
        require_json=True,
        require_object=True,
    )
    append_artifact_once(artifacts, metadata_entry)
    validate_native_macos_metadata(metadata_payload, args.app_name, args.bundle_id, app_bundle_entry, metadata_entry, problems)
    for kind, raw in (("signing", args.signing_note), ("packaging", args.packaging_note)):
        entry, payload = validate_manifest_bound_artifact_arg(
            project,
            raw,
            f"{kind} note",
            problems,
            manifest=manifest,
            task=args.task,
            collector="native-macos",
            require_json=True,
            require_object=True,
        )
        append_artifact_once(artifacts, entry)
        validate_native_macos_note(payload, kind, entry, problems)
    summary = live_manifest_summary(manifest)
    if not summary.get("app_bundle_metadata"):
        problems.append(live_problem("native macOS manifest summary must include app_bundle_metadata", rule="native-macos-bundle-metadata"))
    return write_live_proof_record(
        project,
        kind="native-macos-proof",
        task=args.task,
        strict=args.strict,
        inputs=vars(args),
        problems=problems,
        manifest_path=manifest_resolved,
        manifest=manifest,
        artifacts=artifacts,
        summary="native macOS proof",
    )


SECURITY_PROFILES = {"dependency-audit", "security-deep", "security-diff", "vulnerability-fix"}


def findings_list(payload: Any) -> list[dict[str, Any]] | None:
    raw = payload
    if isinstance(payload, dict):
        raw = payload.get("findings")
    if not isinstance(raw, list):
        return None
    return [item for item in raw if isinstance(item, dict)]


def validate_security_findings_payload(payload: Any, path: str) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    raw = payload
    problems: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        raw = payload.get("findings")
    if not isinstance(raw, list):
        return None, problems
    findings: list[dict[str, Any]] = []
    required = ("schema", "id", "scanner", "scanner_version", "rule_id", "severity", "fingerprint")
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(live_problem(f"security finding {idx + 1} must be a JSON object", rule="security-findings", path=path))
            continue
        missing = [key for key in required if not item.get(key)]
        if missing:
            problems.append(live_problem(f"security finding {idx + 1} is missing normalized fields: " + ", ".join(missing), rule="security-findings", path=path))
        fingerprint = str(item.get("fingerprint") or "")
        if fingerprint and not fingerprint.startswith("sfsec-"):
            problems.append(live_problem(f"security finding {idx + 1} fingerprint is not deterministic", rule="security-findings", path=path))
        findings.append(item)
    return findings, problems


SECURITY_HANDOFF_INPUT_SCHEMA = "star-forge.security-handoff-input.v1"
SECURITY_INPUT_HASH_SCHEMA = "star-forge.security-input-hash.v1"
SECURITY_FINDINGS_SCHEMA = "star-forge.normalized-security-findings.v1"
SECURITY_REDACTION_SCHEMA = "star-forge.security-redaction-report.v1"
TRUSTED_SECURITY_SCHEMA_FAMILIES = {"codex-security", "star-forge"}


def valid_sha256(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", str(value or "")))


def security_clean_problem(message: str, *, path: str = "") -> dict[str, Any]:
    return live_problem(message, rule="security-clean-proof", path=path)


def source_binding_is_fresh(project: Path, binding: Mapping[str, Any], *, current_source_hash: str | None = None) -> bool:
    has_source_hash = bool(str(binding.get("source_hash") or ""))
    if current_source_hash is not None and str(binding.get("source_hash") or "") == current_source_hash:
        return not dirty_paths_missing_from_source_snapshot(project)
    commit_sha = str(binding.get("commit_sha") or "")
    head = git_head(project)
    if not commit_sha or not head or commit_sha != head:
        return current_source_hash is None and has_source_hash
    return tree_clean_for_commit_binding(project)


def validate_clean_security_artifacts(
    project: Path,
    args: argparse.Namespace,
    manifest: dict[str, Any] | None,
    manifest_path: Path | None,
    findings_entry: dict[str, Any] | None,
    findings_payload: Any,
    artifacts: list[dict[str, Any]],
    problems: list[dict[str, Any]],
) -> None:
    if manifest_path is None or not isinstance(manifest, dict):
        problems.append(security_clean_problem("clean security proof requires a scoped security manifest"))
        return
    root = manifest_path.parent
    task = str(args.task or "")
    required = {
        "handoff-input": "handoff-input.json",
        "input-hash": "input-hash.json",
        "normalized-findings": "normalized-findings.json",
        "redaction-report": "redaction-report.json",
    }
    payloads: dict[str, Any] = {}
    entries: dict[str, dict[str, Any]] = {}
    for label, filename in required.items():
        entry, payload = validate_manifest_bound_artifact_arg(
            project,
            str(root / filename),
            label,
            problems,
            manifest=manifest,
            raw_hash_rule="security-clean-proof",
            task=task,
            collector="security",
            require_scoped=True,
            require_json=True,
            require_object=True,
        )
        if entry:
            append_artifact_once(artifacts, entry)
            entries[label] = entry
        payloads[label] = payload
        if not entry or not entry.get("exists"):
            problems.append(security_clean_problem(f"clean security proof requires scoped {filename}", path=live_rel(project, root / filename)))

    normalized_entry = entries.get("normalized-findings")
    if findings_entry and normalized_entry and findings_entry.get("path") != normalized_entry.get("path"):
        problems.append(live_problem("security proof findings argument must match the manifest normalized findings artifact", rule="security-findings", path=str(findings_entry.get("path") or "")))

    handoff = payloads.get("handoff-input")
    input_hash = payloads.get("input-hash")
    normalized = payloads.get("normalized-findings")
    redaction = payloads.get("redaction-report")
    summary = live_manifest_summary(manifest)
    current_source = current_live_source_hash(project, problems)

    if isinstance(normalized, dict):
        if normalized.get("schema") != SECURITY_FINDINGS_SCHEMA:
            problems.append(security_clean_problem("normalized findings schema is invalid", path=str(normalized_entry.get("path") if normalized_entry else "")))
        if normalized.get("task") and str(normalized.get("task")) != task:
            problems.append(live_problem("normalized findings task does not match proof task", rule="security-task", path=str(normalized_entry.get("path") if normalized_entry else "")))
        if normalized.get("profile") and str(normalized.get("profile")) != str(args.profile):
            problems.append(live_problem("normalized findings profile does not match proof profile", rule="security-profile", path=str(normalized_entry.get("path") if normalized_entry else "")))
        normalized_findings = normalized.get("findings")
        if not isinstance(normalized_findings, list):
            problems.append(live_problem("normalized findings artifact must contain a findings array", rule="security-findings", path=str(normalized_entry.get("path") if normalized_entry else "")))
        if findings_payload != normalized:
            problems.append(live_problem("security proof findings payload does not match the manifest normalized findings artifact", rule="security-findings", path=str(normalized_entry.get("path") if normalized_entry else "")))

    normalized_artifact_path = str(normalized_entry.get("path") if normalized_entry else "")
    normalized_artifact_sha = str(normalized_entry.get("sha256") if normalized_entry else "").lower()
    actual_input_hash = ""
    if isinstance(input_hash, dict):
        input_hash_path = str(entries.get("input-hash", {}).get("path") or "")
        if input_hash.get("schema") != SECURITY_INPUT_HASH_SCHEMA:
            problems.append(security_clean_problem("input hash schema is invalid", path=input_hash_path))
        declared = str(input_hash.get("declared_sha256") or "").lower()
        actual = str(input_hash.get("actual_sha256") or "").lower()
        actual_input_hash = actual
        if not valid_sha256(declared) or not valid_sha256(actual) or declared != actual or input_hash.get("matches") is not True:
            problems.append(live_problem("security input hash artifact does not prove matching input bytes", rule="security-input-hash", path=input_hash_path))
        raw_input_path = str(input_hash.get("input_path") or "")
        if not raw_input_path:
            problems.append(live_problem("security input hash artifact is missing input_path", rule="security-input-hash", path=input_hash_path))
        else:
            try:
                resolved_input = live_common.safe_project_path(project, raw_input_path, must_exist=True)
            except ValueError as exc:
                problems.append(live_problem(f"security input hash path is unsafe or missing: {exc}", rule="security-input-hash", path=raw_input_path))
            else:
                if file_sha256(resolved_input).lower() != actual:
                    problems.append(live_problem("security input hash does not match current input bytes", rule="security-input-hash", path=raw_input_path))

    if isinstance(handoff, dict):
        handoff_path = str(entries.get("handoff-input", {}).get("path") or "")
        if handoff.get("schema") != SECURITY_HANDOFF_INPUT_SCHEMA:
            problems.append(security_clean_problem("handoff input schema is invalid", path=handoff_path))
        if str(handoff.get("task") or "") != task:
            problems.append(live_problem("handoff input task does not match proof task", rule="security-task", path=handoff_path))
        if str(handoff.get("profile") or "") != str(args.profile):
            problems.append(live_problem("handoff input profile does not match proof profile", rule="security-profile", path=handoff_path))
        handoff_kind = str(handoff.get("kind") or "")
        expected_kind = str(getattr(args, "kind", "") or "")
        if not handoff_kind:
            problems.append(live_problem("handoff input requires kind", rule="security-kind", path=handoff_path))
        elif expected_kind and handoff_kind != expected_kind:
            problems.append(live_problem("handoff input kind does not match proof kind", rule="security-kind", path=handoff_path))
        provenance = handoff.get("provenance")
        if not isinstance(provenance, dict):
            problems.append(live_problem("handoff input requires scanner provenance", rule="security-provenance", path=handoff_path))
            provenance = {}
        schema_family = str(provenance.get("schema_family") or "")
        trusted_schema = provenance.get("trusted_schema") is True and schema_family in TRUSTED_SECURITY_SCHEMA_FAMILIES
        if not trusted_schema:
            problems.append(live_problem("security proof requires trusted scanner schema provenance", rule="security-provenance", path=handoff_path))
        scanner = str(handoff.get("scanner") or provenance.get("scanner") or "")
        scanner_version = str(handoff.get("scanner_version") or provenance.get("scanner_version") or "")
        if not scanner or not scanner_version:
            problems.append(live_problem("handoff input requires scanner and scanner_version", rule="security-provenance", path=handoff_path))
        elif scanner != str(args.scanner or "") or scanner_version != str(args.scanner_version or ""):
            problems.append(live_problem("handoff scanner provenance does not match proof arguments", rule="security-provenance", path=handoff_path))
        if not handoff.get("ruleset"):
            problems.append(live_problem("security proof requires ruleset provenance", rule="security-ruleset", path=handoff_path))
        if not handoff.get("scan_scope"):
            problems.append(live_problem("security proof requires scan scope", rule="security-scope", path=handoff_path))
        source_binding = handoff.get("source_binding")
        if not isinstance(source_binding, dict) or not source_binding_is_fresh(project, source_binding, current_source_hash=current_source):
            problems.append(live_problem("security proof requires a fresh source_hash or commit binding", rule="security-source-binding", path=handoff_path))
        handoff_input_hash = handoff.get("input_hash")
        if isinstance(handoff_input_hash, dict) and isinstance(input_hash, dict):
            if str(handoff_input_hash.get("actual_sha256") or "").lower() != str(input_hash.get("actual_sha256") or "").lower():
                problems.append(live_problem("handoff input hash does not match input-hash artifact", rule="security-input-hash", path=handoff_path))
        else:
            problems.append(live_problem("handoff input must include input_hash details", rule="security-input-hash", path=handoff_path))
        handoff_findings = handoff.get("normalized_findings")
        if isinstance(handoff_findings, dict):
            declared_path = str(handoff_findings.get("path") or "")
            declared_hash = str(handoff_findings.get("sha256") or "").lower()
            if declared_path and normalized_artifact_path and declared_path != normalized_artifact_path:
                problems.append(live_problem("handoff normalized findings path does not match artifact", rule="security-findings", path=handoff_path))
            if not valid_sha256(declared_hash) or (normalized_artifact_sha and declared_hash != normalized_artifact_sha):
                problems.append(live_problem("handoff normalized findings hash does not match artifact", rule="security-findings", path=handoff_path))
            if isinstance(normalized, dict):
                findings_array = normalized.get("findings")
                if isinstance(findings_array, list) and "finding_count" in handoff_findings:
                    try:
                        declared_count = int(handoff_findings.get("finding_count"))
                    except (TypeError, ValueError):
                        declared_count = -1
                    if declared_count != len(findings_array):
                        problems.append(live_problem("handoff normalized findings count does not match artifact", rule="security-findings", path=handoff_path))
        else:
            problems.append(live_problem("handoff input must include normalized findings hash", rule="security-findings", path=handoff_path))

    if isinstance(redaction, dict):
        redaction_path = str(entries.get("redaction-report", {}).get("path") or "")
        if redaction.get("schema") != SECURITY_REDACTION_SCHEMA:
            problems.append(security_clean_problem("redaction report schema is invalid", path=redaction_path))
        if not isinstance(redaction.get("counts"), dict):
            problems.append(security_clean_problem("redaction report must include counts", path=redaction_path))

    if summary.get("trusted_provenance") is not True:
        problems.append(security_clean_problem("manifest summary must mark trusted_provenance true"))
    if not summary.get("ruleset"):
        problems.append(live_problem("clean security proof is missing ruleset metadata", rule="security-ruleset"))
    if not summary.get("scan_scope"):
        problems.append(live_problem("clean security proof is missing scan scope", rule="security-scope"))
    if actual_input_hash and str(summary.get("input_hash") or "").lower() != actual_input_hash:
        problems.append(live_problem("manifest summary input_hash does not match input-hash artifact", rule="security-input-hash"))


def cmd_security_handoff_packet(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    problems: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    input_entry, input_payload = validate_artifact_arg(project, args.input, "handoff input", problems, require_json=True, require_object=True, require_scoped=False)
    if input_entry:
        artifacts.append(input_entry)
    task = str(getattr(args, "task", "") or "")
    input_path: Path | None = None
    if input_entry and input_entry.get("path"):
        try:
            input_path = live_common.safe_project_path(project, str(input_entry["path"]), must_exist=False)
        except ValueError as exc:
            problems.append(live_problem(f"handoff input path is unsafe: {exc}", rule="artifact-path", path=str(input_entry.get("path") or "")))
        if not task and isinstance(input_payload, dict):
            task = str(input_payload.get("task") or "")
        if not task and input_path is not None:
            task = task_from_scoped_live_path(project, input_path, "security") or ""
        if input_path is not None and not is_task_scoped_live_path(project, input_path, task or None, "security"):
            problems.append(live_problem("handoff input must be under .starforge/live/<task>/security/", rule="artifact-scope", path=input_entry.get("path", "")))
    if isinstance(input_payload, dict) and input_payload.get("task") and task and str(input_payload.get("task")) != task:
        problems.append(live_problem("handoff input task does not match scoped task", rule="security-task", path=input_entry.get("path", "") if input_entry else ""))
    manifest = None
    manifest_path = None
    if input_path is not None:
        manifest_candidate = input_path.parent / "manifest.json"
        manifest, manifest_path = load_and_validate_live_manifest(project, manifest_candidate, problems, task=task or None, collector="security", require_scoped=True)
        require_raw_hash_for_artifact(project, manifest, input_path, problems, label="handoff input", rule="security-clean-proof")
    if not str(args.kind or "").strip():
        problems.append(live_problem("security handoff packet requires --kind", rule="security-kind"))
    profile = ""
    scanner = ""
    scanner_version = ""
    if isinstance(input_payload, dict):
        profile = str(input_payload.get("profile") or "")
        handoff_kind = str(input_payload.get("kind") or "")
        provenance = input_payload.get("provenance") if isinstance(input_payload.get("provenance"), dict) else {}
        scanner = str(input_payload.get("scanner") or provenance.get("scanner") or "")
        scanner_version = str(input_payload.get("scanner_version") or provenance.get("scanner_version") or "")
        if not task:
            problems.append(live_problem("security handoff input must include task", rule="security-task", path=input_entry.get("path", "") if input_entry else ""))
        if not profile:
            problems.append(live_problem("security handoff input must include profile", rule="security-profile", path=input_entry.get("path", "") if input_entry else ""))
        if not handoff_kind:
            problems.append(live_problem("security handoff input must include kind", rule="security-kind", path=input_entry.get("path", "") if input_entry else ""))
        elif args.kind and handoff_kind != str(args.kind):
            problems.append(live_problem("security handoff input kind does not match --kind", rule="security-kind", path=input_entry.get("path", "") if input_entry else ""))
        if not scanner or not scanner_version:
            problems.append(live_problem("security handoff input must include scanner and scanner_version", rule="security-provenance", path=input_entry.get("path", "") if input_entry else ""))
    findings_entry: dict[str, Any] | None = None
    findings_payload: Any = None
    if manifest_path is not None:
        findings_entry, findings_payload = validate_manifest_bound_artifact_arg(
            project,
            str(manifest_path.parent / "normalized-findings.json"),
            "normalized findings",
            problems,
            manifest=manifest,
            raw_hash_rule="security-clean-proof",
            task=task or None,
            collector="security",
            require_scoped=True,
            require_json=True,
            require_object=True,
        )
        append_artifact_once(artifacts, findings_entry)
    validation_args = argparse.Namespace(
        task=task,
        profile=profile,
        kind=str(args.kind or ""),
        scanner=scanner,
        scanner_version=scanner_version,
    )
    validate_clean_security_artifacts(project, validation_args, manifest, manifest_path, findings_entry, findings_payload, artifacts, problems)
    return write_live_proof_record(
        project,
        kind="security-handoff-packet",
        task=task or None,
        strict=args.strict,
        inputs=vars(args),
        problems=problems,
        manifest_path=manifest_path,
        manifest=manifest,
        artifacts=artifacts,
        summary="security handoff packet",
    )


def cmd_security_proof(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    problems: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    if args.profile not in SECURITY_PROFILES:
        problems.append(live_problem("security proof profile is invalid", rule="security-profile"))
    if not str(args.scanner or "").strip():
        problems.append(live_problem("security proof requires --scanner", rule="security-scanner"))
    if not str(args.scanner_version or "").strip():
        problems.append(live_problem("security proof requires --scanner-version", rule="security-scanner"))
    manifest, manifest_path = load_and_validate_live_manifest(project, args.artifact, problems, task=args.task, collector="security")
    findings_entry, findings_payload = validate_manifest_bound_artifact_arg(
        project,
        args.findings,
        "normalized findings",
        problems,
        manifest=manifest,
        raw_hash_rule="security-clean-proof",
        task=args.task,
        collector="security",
        require_json=True,
    )
    append_artifact_once(artifacts, findings_entry)
    findings, finding_shape_problems = validate_security_findings_payload(findings_payload, findings_entry.get("path", "") if findings_entry else "")
    problems.extend(finding_shape_problems)
    if findings is None:
        problems.append(live_problem("normalized findings must be an array or contain a findings array", rule="security-findings", path=findings_entry.get("path", "") if findings_entry else ""))
    else:
        for item in findings:
            severity = str(item.get("severity") or "").lower()
            if severity not in FINDING_SEVERITIES:
                problems.append(live_problem("security finding has unknown severity", rule="security-severity", path=findings_entry.get("path", "") if findings_entry else ""))
            elif severity in BLOCKING_SEVERITIES:
                problems.append(live_problem(f"security finding is blocking severity: {severity}", rule="security-finding", path=findings_entry.get("path", "") if findings_entry else "", severity=severity))
    summary = live_manifest_summary(manifest)
    required = ("trusted_provenance", "ruleset", "scan_scope", "input_hash")
    missing = [key for key in required if not summary.get(key)]
    if missing:
        problems.append(live_problem("security proof is missing trusted provenance fields: " + ", ".join(missing), rule="security-clean-proof"))
    validate_clean_security_artifacts(project, args, manifest, manifest_path, findings_entry, findings_payload, artifacts, problems)
    return write_live_proof_record(
        project,
        kind="security-proof",
        task=args.task,
        strict=args.strict,
        inputs=vars(args),
        problems=problems,
        manifest_path=manifest_path,
        manifest=manifest,
        artifacts=artifacts,
        summary=f"profile={args.profile}",
    )


def github_live_source_marker(value: Any) -> bool:
    return str(value or "") in {"github-live", "github-cli-live", "github-connector-live", "gh-readonly-live", "connector-readonly-live"}


def github_fixture_marker(value: Any) -> bool:
    text = str(value or "").lower()
    return text in {"connector-fixture", "gh-fixture", "missing-fixture"} or "fixture" in text


def validate_github_operation_transcript(
    project: Path,
    manifest: dict[str, Any] | None,
    transcript_path: Path,
    transcript_payload: Any,
    summary: dict[str, Any],
    problems: list[dict[str, Any]],
    *,
    path: str,
    check_runs_payload: Any = None,
    pr_payload: Any = None,
) -> str:
    actual_hash = require_raw_hash_for_artifact(
        project,
        manifest,
        transcript_path,
        problems,
        label="GitHub operation transcript",
        rule="github-live-provenance",
    )
    if not isinstance(transcript_payload, dict):
        problems.append(live_problem("GitHub operation transcript must be a JSON object", rule="github-live-provenance", path=path))
        return actual_hash
    if transcript_payload.get("schema") != "star-forge.github-operation-transcript.v1":
        problems.append(live_problem("GitHub operation transcript schema is invalid", rule="github-live-provenance", path=path))
    transcript_source = str(transcript_payload.get("source") or "")
    if not github_live_source_marker(transcript_source):
        problems.append(live_problem("GitHub operation transcript requires live source provenance", rule="github-live-provenance", path=path))
    if str(transcript_payload.get("repo") or "") != str(summary.get("repo") or ""):
        problems.append(live_problem("GitHub operation transcript repo does not match the manifest summary", rule="github-live-provenance", path=path))
    if str(transcript_payload.get("pr") or "") != str(summary.get("pr") or ""):
        problems.append(live_problem("GitHub operation transcript PR does not match the manifest summary", rule="github-live-provenance", path=path))
    if not str(transcript_payload.get("collected_at") or transcript_payload.get("captured_at") or "").strip():
        problems.append(live_problem("GitHub operation transcript requires a collection timestamp", rule="github-live-provenance", path=path))
    refs = transcript_payload.get("refs")
    if not isinstance(refs, dict):
        problems.append(live_problem("GitHub operation transcript requires freshness refs", rule="github-live-provenance", path=path))
        refs = {}
    for field in ("captured_base_sha", "current_base_sha", "captured_head_sha", "current_head_sha", "merge_base_sha"):
        if str(refs.get(field) or "") != str(summary.get(field) or ""):
            problems.append(live_problem(f"GitHub operation transcript {field} does not match the manifest summary", rule="github-live-provenance", path=path))
    permission_state = transcript_payload.get("permission_state")
    pagination_state = transcript_payload.get("pagination_state")
    if not isinstance(permission_state, dict):
        problems.append(live_problem("GitHub operation transcript requires permission state", rule="github-live-provenance", path=path))
        permission_state = {}
    if not isinstance(pagination_state, dict):
        problems.append(live_problem("GitHub operation transcript requires pagination state", rule="github-live-provenance", path=path))
        pagination_state = {}
    if permission_state.get("partial_permissions"):
        problems.append(live_problem("GitHub operation transcript reports partial permissions", rule="github-permissions", path=path))
    if pagination_state.get("pagination_incomplete"):
        problems.append(live_problem("GitHub operation transcript reports incomplete pagination", rule="github-pagination", path=path))
    operations = transcript_payload.get("operations") if isinstance(transcript_payload.get("operations"), list) else []
    commands = transcript_payload.get("commands") if isinstance(transcript_payload.get("commands"), list) else []
    if not operations and not commands:
        problems.append(live_problem("GitHub operation transcript requires read-only operations", rule="github-live-provenance", path=path))
    try:
        from live_collectors import github_pr
    except Exception as exc:
        problems.append(live_problem(f"GitHub operation validators are unavailable: {exc}", rule="github-live-provenance", path=path))
    else:
        github_host, host_messages = github_pr.validate_transcript_github_host_evidence(
            transcript_payload=transcript_payload,
            summary=summary,
            pr_payload=pr_payload,
            operations=operations,
        )
        for message in host_messages:
            problems.append(live_problem(message, rule="github-live-provenance", path=path))
        for command in commands:
            parsed = github_pr.shell_argv(command)
            if not parsed:
                problems.append(live_problem("GitHub operation transcript command is malformed", rule="github-command", path=path))
                continue
            problems.extend(
                github_pr.validate_gh_command(
                    parsed,
                    repo=str(summary.get("repo") or ""),
                    pr_number=str(summary.get("pr") or ""),
                    check_runs=check_runs_payload,
                    captured_head=str(summary.get("captured_head_sha") or ""),
                    github_host=github_host,
                )
            )
        for operation in operations:
            problems.extend(
                github_pr.validate_connector_operation(
                    operation,
                    repo=str(summary.get("repo") or ""),
                    pr_number=str(summary.get("pr") or ""),
                    check_runs=check_runs_payload,
                    captured_head=str(summary.get("captured_head_sha") or ""),
                    github_host=github_host,
                    require_identity=True,
                )
            )
    return actual_hash


def _github_payload_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _github_payload_ref(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        text = _github_payload_text(payload.get(key))
        if text:
            return text
    return ""


def validate_github_pr_payload(
    pr_payload: Any,
    summary: Mapping[str, Any],
    transcript_payload: Any,
    problems: list[dict[str, Any]],
    *,
    path: str,
) -> None:
    if not isinstance(pr_payload, Mapping):
        problems.append(live_problem("GitHub PR artifact must be a JSON object", rule="github-live-provenance", path=path))
        return
    try:
        from live_collectors import github_pr
    except Exception as exc:
        problems.append(live_problem(f"GitHub PR validators are unavailable: {exc}", rule="github-live-provenance", path=path))
        return

    expected_repo = _github_payload_text(summary.get("repo"))
    expected_pr = _github_payload_text(summary.get("pr"))
    payload_repo = github_pr.payload_repository_identity(pr_payload)
    payload_pr = _github_payload_text(
        pr_payload.get("number"),
        pr_payload.get("pr"),
        pr_payload.get("pull_request"),
        pr_payload.get("pullRequestNumber"),
    )
    if expected_repo and not payload_repo:
        problems.append(live_problem("GitHub PR artifact requires repository identity", rule="github-live-provenance", path=path))
    elif expected_repo and payload_repo != expected_repo:
        problems.append(live_problem("GitHub PR artifact repository does not match the manifest summary", rule="github-live-provenance", path=path))
    if expected_pr and not payload_pr:
        problems.append(live_problem("GitHub PR artifact requires PR identity", rule="github-live-provenance", path=path))
    elif expected_pr and payload_pr != expected_pr:
        problems.append(live_problem("GitHub PR artifact number does not match the manifest summary", rule="github-live-provenance", path=path))

    pr_url_keys = ("url", "html_url", "web_url", "pull_request_url", "pullRequestUrl")
    present_pr_urls: list[tuple[str, str]] = []
    for key in pr_url_keys:
        text = _github_payload_text(pr_payload.get(key))
        if text:
            present_pr_urls.append((key, text))
    if not present_pr_urls:
        problems.append(live_problem("GitHub PR artifact requires PR URL identity", rule="github-live-provenance", path=path))
    for key, pr_url in present_pr_urls:
        label = f"GitHub PR artifact {key}"
        for message in github_pr.github_url_identity_messages(pr_url, label, require_url=True):
            problems.append(live_problem(message, rule="github-live-provenance", path=path))
        url_repo = github_pr.repo_from_url(pr_url)
        url_pr = github_pr.pr_from_url(pr_url)
        if expected_repo and not url_repo:
            problems.append(live_problem(f"{label} must include repository identity", rule="github-live-provenance", path=path))
        elif expected_repo and url_repo != expected_repo:
            problems.append(live_problem(f"{label} repository does not match the manifest summary", rule="github-live-provenance", path=path))
        if expected_pr and not url_pr:
            problems.append(live_problem(f"{label} must include PR identity", rule="github-live-provenance", path=path))
        elif expected_pr and url_pr != expected_pr:
            problems.append(live_problem(f"{label} PR does not match the manifest summary", rule="github-live-provenance", path=path))

    pr_refs = {
        "captured_base_sha": github_pr.extract_base_sha(pr_payload),
        "captured_head_sha": github_pr.extract_head_sha(pr_payload),
        "current_base_sha": _github_payload_ref(pr_payload, "current_base_sha", "currentBaseSha"),
        "current_head_sha": _github_payload_ref(pr_payload, "current_head_sha", "currentHeadSha"),
        "merge_base_sha": _github_payload_ref(pr_payload, "merge_base_sha", "mergeBaseOid"),
    }
    transcript_refs = transcript_payload.get("refs") if isinstance(transcript_payload, Mapping) and isinstance(transcript_payload.get("refs"), Mapping) else {}
    for field, actual in pr_refs.items():
        expected = _github_payload_text(summary.get(field))
        if not actual:
            problems.append(live_problem(f"GitHub PR artifact requires {field}", rule="github-live-provenance", path=path))
        elif expected and actual != expected:
            problems.append(live_problem(f"GitHub PR artifact {field} does not match the manifest summary", rule="github-live-provenance", path=path))
        transcript_value = _github_payload_text(transcript_refs.get(field)) if isinstance(transcript_refs, Mapping) else ""
        if actual and transcript_value and actual != transcript_value:
            problems.append(live_problem(f"GitHub PR artifact {field} does not match the operation transcript", rule="github-live-provenance", path=path))


def validate_source_packet_manifest(project: Path, manifest: dict[str, Any] | None, manifest_path: Path | None, problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    if manifest_path is None:
        return artifacts
    root = manifest_path.parent
    required = ["pr.json", "diff.patch", "reviews.json", "comments.json", "check-runs.json", "annotations.json"]
    summary = live_manifest_summary(manifest)
    check_runs_payload: Any = None
    pr_payload: Any = None
    for filename in required:
        require_json = filename.endswith(".json")
        entry, payload = validate_manifest_bound_artifact_arg(
            project,
            str(root / filename),
            filename,
            problems,
            manifest=manifest,
            raw_hash_rule="github-live-provenance",
            task=str(manifest.get("task") or "") if isinstance(manifest, dict) else None,
            collector="github",
            require_scoped=True,
            require_json=require_json,
        )
        append_artifact_once(artifacts, entry)
        if filename == "pr.json" and payload is not None:
            pr_payload = payload
        if filename == "check-runs.json" and payload is not None:
            check_runs_payload = payload
            validate_check_runs(payload, problems, entry.get("path", "") if entry else "", captured_head=str(summary.get("captured_head_sha") or ""))
    logs_path = root / "ci-log-excerpts.json"
    if logs_path.exists() or summary.get("logs_included"):
        entry, log_payload = validate_manifest_bound_artifact_arg(
            project,
            str(logs_path),
            "ci log excerpts",
            problems,
            manifest=manifest,
            raw_hash_rule="github-live-provenance",
            task=str(manifest.get("task") or "") if isinstance(manifest, dict) else None,
            collector="github",
            require_scoped=True,
            require_json=True,
            require_object=True,
        )
        append_artifact_once(artifacts, entry)
        try:
            from live_collectors import github_pr
        except Exception as exc:
            problems.append(live_problem(f"GitHub CI log validators are unavailable: {exc}", rule="github-live-provenance", path=entry.get("path", "") if entry else live_rel(project, logs_path)))
        else:
            problems.extend(
                github_pr.validate_ci_log_excerpt_payload(
                    log_payload,
                    repo=str(summary.get("repo") or ""),
                    pr_number=str(summary.get("pr") or ""),
                    captured_head=str(summary.get("captured_head_sha") or ""),
                    check_runs=check_runs_payload,
                    path=entry.get("path", "") if entry else live_rel(project, logs_path),
                )
            )
    transcript_entry, transcript_payload = validate_artifact_arg(
        project,
        str(root / "operation-transcript.json"),
        "operation transcript",
        problems,
        task=str(manifest.get("task") or "") if isinstance(manifest, dict) else None,
        collector="github",
        require_scoped=True,
        require_json=True,
        require_object=True,
    )
    if transcript_entry:
        artifacts.append(transcript_entry)
    transcript_actual_hash = ""
    transcript_path = root / "operation-transcript.json"
    if transcript_entry and transcript_entry.get("exists"):
        transcript_actual_hash = validate_github_operation_transcript(
            project,
            manifest,
            transcript_path,
            transcript_payload,
            summary,
            problems,
            path=transcript_entry.get("path", ""),
            check_runs_payload=check_runs_payload,
            pr_payload=pr_payload,
        )
        validate_github_pr_payload(
            pr_payload,
            summary,
            transcript_payload,
            problems,
            path=next((str(item.get("path") or "") for item in artifacts if str(item.get("path") or "").endswith("/pr.json")), live_rel(project, root / "pr.json")),
        )
    else:
        problems.append(live_problem("GitHub PR source packet requires a scoped operation transcript artifact", rule="github-live-provenance", path=live_rel(project, transcript_path)))
    for left, right, label in (
        ("captured_base_sha", "current_base_sha", "base SHA"),
        ("captured_head_sha", "current_head_sha", "head SHA"),
    ):
        if summary.get(left) and summary.get(right) and summary.get(left) != summary.get(right):
            problems.append(live_problem(f"GitHub PR {label} changed after capture", rule="github-freshness"))
    if summary.get("missing_refs"):
        problems.append(live_problem("GitHub PR evidence reports missing refs", rule="github-refs"))
    if summary.get("partial_permissions"):
        problems.append(live_problem("GitHub PR evidence reports partial permissions", rule="github-permissions"))
    if summary.get("pagination_incomplete"):
        problems.append(live_problem("GitHub PR evidence reports incomplete pagination", rule="github-pagination"))
    tool_versions = manifest.get("tool_versions") if isinstance(manifest, dict) else {}
    source_markers = {
        str(summary.get("source") or ""),
        str(tool_versions.get("source") or "") if isinstance(tool_versions, dict) else "",
    }
    if any(github_fixture_marker(item) for item in source_markers):
        problems.append(live_problem("GitHub PR fixture evidence is not production-review proof", rule="github-fixture-provenance"))
    normalized_sources = {item for item in source_markers if item}
    if not normalized_sources or not any(github_live_source_marker(item) for item in normalized_sources):
        problems.append(live_problem("GitHub PR source packet requires positive live GitHub provenance", rule="github-live-provenance"))
    if not isinstance(tool_versions, dict) or not tool_versions or any(github_fixture_marker(key) or github_fixture_marker(value) for key, value in tool_versions.items()):
        problems.append(live_problem("GitHub PR source packet requires collector tool versions", rule="github-live-provenance"))
    provenance = summary.get("live_provenance") or summary.get("github_provenance")
    if not isinstance(provenance, dict):
        problems.append(live_problem("GitHub PR source packet requires live provenance details", rule="github-live-provenance"))
        provenance = {}
    summary_repo = str(summary.get("repo") or "").strip()
    summary_pr = str(summary.get("pr") or "").strip()
    provenance_repo = str(provenance.get("repo") or provenance.get("repository") or "").strip()
    provenance_pr = str(provenance.get("pr") or provenance.get("pull_request") or provenance.get("number") or "").strip()
    if not summary_repo:
        problems.append(live_problem("GitHub PR source packet requires repository identity", rule="github-live-provenance"))
    if not summary_pr:
        problems.append(live_problem("GitHub PR source packet requires PR identity", rule="github-live-provenance"))
    if not provenance_repo:
        problems.append(live_problem("GitHub PR source packet requires provenance repository identity", rule="github-live-provenance"))
    elif summary_repo and provenance_repo != summary_repo:
        problems.append(live_problem("GitHub PR source packet provenance repository does not match the summary", rule="github-live-provenance"))
    if not provenance_pr:
        problems.append(live_problem("GitHub PR source packet requires provenance PR identity", rule="github-live-provenance"))
    elif summary_pr and provenance_pr != summary_pr:
        problems.append(live_problem("GitHub PR source packet provenance PR does not match the summary", rule="github-live-provenance"))
    if not str(provenance.get("collected_at") or provenance.get("captured_at") or summary.get("captured_at") or "").strip():
        problems.append(live_problem("GitHub PR source packet requires a collection timestamp", rule="github-live-provenance"))
    freshness_fields = ("captured_base_sha", "current_base_sha", "captured_head_sha", "current_head_sha", "merge_base_sha")
    missing_freshness = [field for field in freshness_fields if not str(summary.get(field) or provenance.get(field) or "").strip()]
    if missing_freshness:
        problems.append(live_problem("GitHub PR source packet is missing freshness refs: " + ", ".join(missing_freshness), rule="github-live-provenance"))
    read_only_commands = summary.get("read_only_commands") if isinstance(summary.get("read_only_commands"), list) else []
    read_only_operations = summary.get("read_only_operations") if isinstance(summary.get("read_only_operations"), list) else []
    provenance_transcript_hash = str(provenance.get("operation_transcript_sha256") or provenance.get("read_only_transcript_sha256") or "")
    summary_transcript_hash = str(summary.get("read_only_transcript_sha256") or "")
    claimed_transcript_hash = provenance_transcript_hash or summary_transcript_hash
    if not claimed_transcript_hash or not re.fullmatch(r"[0-9a-fA-F]{64}", claimed_transcript_hash):
        problems.append(live_problem("GitHub PR source packet requires a hashed read-only operation transcript", rule="github-live-provenance"))
    if transcript_actual_hash and summary_transcript_hash != transcript_actual_hash:
        problems.append(live_problem("GitHub operation transcript hash does not match the manifest summary", rule="github-live-provenance"))
    if transcript_actual_hash and provenance_transcript_hash != transcript_actual_hash:
        problems.append(live_problem("GitHub operation transcript hash does not match live provenance", rule="github-live-provenance"))
    if not read_only_commands and not read_only_operations and not provenance.get("read_only_operations"):
        problems.append(live_problem("GitHub PR source packet requires read-only GitHub operations", rule="github-live-provenance"))
    return artifacts


def validate_check_runs(payload: Any, problems: list[dict[str, Any]], path: str, *, captured_head: str = "") -> None:
    completed_statuses = {"completed", "success"}
    successful_conclusions = {"success"}
    pending_statuses = {"expected", "in_progress", "pending", "queued", "requested", "waiting"}
    if isinstance(payload, dict):
        if payload.get("partial_permissions"):
            problems.append(live_problem("check runs are permission-partial", rule="github-checks", path=path))
        if payload.get("pagination_incomplete"):
            problems.append(live_problem("check runs pagination is incomplete", rule="github-checks", path=path))
        raw = payload.get("check_runs") or payload.get("checks") or payload.get("runs")
    else:
        raw = payload
    if not isinstance(raw, list) or not raw:
        problems.append(live_problem("check runs must contain at least one check", rule="github-checks", path=path))
        return
    for idx, check in enumerate(raw):
        if not isinstance(check, dict):
            problems.append(live_problem(f"check run {idx + 1} is malformed", rule="github-checks", path=path))
            continue
        status = str(check.get("status") or "").lower()
        conclusion = str(check.get("conclusion") or "").lower()
        if not status:
            problems.append(live_problem(f"check run {idx + 1} is missing status", rule="github-checks", path=path))
        elif status in pending_statuses:
            problems.append(live_problem(f"check run {idx + 1} is pending: {status}", rule="github-checks", path=path))
        elif status not in completed_statuses:
            problems.append(live_problem(f"check run {idx + 1} is not complete: {status}", rule="github-checks", path=path))
        if not conclusion:
            problems.append(live_problem(f"check run {idx + 1} is missing conclusion", rule="github-checks", path=path))
        elif conclusion not in successful_conclusions:
            problems.append(live_problem(f"check run {idx + 1} conclusion is {conclusion}", rule="github-checks", path=path))
        commit = check.get("commit")
        commit_sha = str(commit.get("sha") or "") if isinstance(commit, dict) else ""
        run_head = str(check.get("head_sha") or check.get("headSha") or commit_sha)
        if captured_head and not run_head:
            problems.append(live_problem(f"check run {idx + 1} is missing head SHA binding", rule="github-checks", path=path))
        elif captured_head and run_head != captured_head:
            problems.append(live_problem(f"check run {idx + 1} is bound to a different head SHA", rule="github-checks", path=path))


def cmd_source_packet_proof(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    problems: list[dict[str, Any]] = []
    profile = str(args.profile or "").strip()
    github_source_profiles = {"production-review", "github-pr-review"}
    collector = collector_for_profile(profile) if profile in github_source_profiles else None
    manifest, manifest_path = load_and_validate_live_manifest(project, args.input, problems, task=args.task, collector=collector, require_scoped=collector is not None)
    if not profile:
        problems.append(live_problem("source packet proof requires --profile", rule="source-profile"))
    elif profile not in github_source_profiles:
        problems.append(live_problem(f"source packet proof profile is unsupported: {profile}", rule="source-profile"))
    artifacts = validate_source_packet_manifest(project, manifest, manifest_path, problems) if profile in github_source_profiles else []
    return write_live_proof_record(
        project,
        kind="source-packet-proof",
        task=args.task,
        strict=args.strict,
        inputs=vars(args),
        problems=problems,
        manifest_path=manifest_path,
        manifest=manifest,
        artifacts=artifacts,
        summary=f"profile={args.profile}",
    )


def cmd_source_packet_github_pr_review(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    problems: list[dict[str, Any]] = []
    manifest, manifest_path = load_and_validate_live_manifest(project, args.input, problems, collector="github", require_scoped=False)
    task = str(manifest.get("task") or "") if isinstance(manifest, dict) else None
    if manifest_path is not None:
        if not is_task_scoped_live_path(project, manifest_path, task, "github"):
            problems.append(live_problem("GitHub source packet manifest must be under .starforge/live/<task>/github/", rule="manifest-scope", path=live_rel(project, manifest_path)))
        if isinstance(manifest, dict):
            validate_manifest_artifact_scopes(project, manifest, problems, task=task, collector="github", require_scoped=True)
            validate_raw_artifact_hashes(project, manifest, problems, task=task, collector="github", require_scoped=True)
    artifacts = validate_source_packet_manifest(project, manifest, manifest_path, problems)
    return write_live_proof_record(
        project,
        kind="source-packet-github-pr-review",
        task=task,
        strict=args.strict,
        inputs=vars(args),
        problems=problems,
        manifest_path=manifest_path,
        manifest=manifest,
        artifacts=artifacts,
        summary="GitHub PR source packet proof",
    )


# ----------------------------------------------------------------- review wave


def reviews_scope_dir(project: Path, scope: str) -> Path:
    return project / REVIEWS_DIR / slugify(scope or "noscope")


def load_review_findings(project: Path, scope: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read reviewer-written findings files for the scope.

    Returns (files, file_problems). Each file dict carries its role, agent_id, the
    source_hash the reviewer attested it reviewed, and normalized findings.
    Freshness is keyed on that attested source_hash (merge_review compares it to
    the current tree), so a clean re-review writing the current hash is fresh even
    when its findings are byte-identical, and the freshness witness is not a
    deletable side-file the gate also reads.
    """
    files: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    root = reviews_scope_dir(project, scope)
    if not root.exists():
        return files, problems
    for path in sorted(root.glob("*.findings.json")):
        rel = relative_to_project(path, project)
        try:
            raw_bytes = path.read_bytes()
            payload = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            problems.append({"severity": "high", "rule": "review-findings-invalid", "file": rel, "message": f"reviewer findings file is unreadable: {exc}"})
            continue
        if not isinstance(payload, dict):
            problems.append({"severity": "high", "rule": "review-findings-shape", "file": rel, "message": "reviewer findings file must be a JSON object"})
            continue
        payload_role = payload.get("role")
        if not isinstance(payload_role, str) or not payload_role.strip():
            problems.append({"severity": "high", "rule": "review-findings-shape", "file": rel, "message": "reviewer findings file must contain a top-level `role` string"})
            continue
        role = payload_role.strip()
        if role not in KNOWN_REVIEW_ROLES:
            problems.append({"severity": "high", "rule": "review-findings-shape", "file": rel, "message": f"reviewer findings role `{role}` is not a known review role"})
            continue
        expected_name = f"{role}.findings.json"
        if path.name != expected_name:
            problems.append({"severity": "high", "rule": "review-findings-shape", "file": rel, "message": f"reviewer findings file `{path.name}` must match payload role `{role}` as `{expected_name}`"})
            continue
        source_attestation = payload.get("source_hash")
        if not isinstance(source_attestation, str) or not source_attestation.strip():
            problems.append({"severity": "high", "rule": "review-findings-shape", "file": rel, "message": "reviewer findings file must contain a top-level `source_hash` string"})
            continue
        raw = payload.get("findings")
        if not isinstance(raw, list):
            problems.append({"severity": "high", "rule": "review-findings-shape", "file": rel, "message": "reviewer findings file must contain a `findings` array"})
            continue
        normalized: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                problems.append({"severity": "high", "rule": "review-findings-shape", "file": rel, "message": "each reviewer finding must be a JSON object"})
                continue
            severity = str(item.get("severity") or "medium").lower()
            normalized.append(
                {
                    "id": str(item.get("id") or ""),
                    "role": role,
                    "agent_id": payload.get("agent_id"),
                    "severity": severity if severity in FINDING_SEVERITIES else "medium",
                    "file": str(item.get("file") or ""),
                    "line": item.get("line"),
                    "title": str(item.get("title") or item.get("summary") or "")[:200],
                    "detail": str(item.get("detail") or item.get("evidence") or "")[:600],
                    "suggested_fix": str(item.get("suggested_fix") or "")[:400],
                }
            )
        files.append(
            {
                "path": rel,
                "role": role,
                "agent_id": payload.get("agent_id"),
                "declared_source_hash": source_attestation,
                "findings": normalized,
            }
        )
    return files, problems


def finding_fingerprint(finding: dict[str, Any]) -> str:
    file = re.sub(r"\s+", "", str(finding.get("file") or "")).lower()
    line = finding.get("line")
    bucket = int(line) // 4 if isinstance(line, int) else -1
    signature = finding_issue_signature(finding)
    return f"{file}:{bucket}:{signature}"


FINDING_DUPLICATE_STOPWORDS = {
    "about", "across", "again", "architecture", "because", "blocks", "code",
    "correctness", "could", "failure", "finding", "findings", "from", "high",
    "issue", "needs", "problem", "review", "risk", "security", "should", "this",
    "with",
}


FINDING_MARKERS = {
    "ts-ignore": ("@ts-ignore", "ts-ignore"),
    "secret-material": ("secret", "api key", "api_key", "token", "credential"),
    "large-file": ("large file", "too large", "split into smaller"),
    "shell-wrapper": ("shell", "sh -c", "bash -c", "zsh -c"),
}


def finding_text(finding: dict[str, Any]) -> str:
    return " ".join(
        str(finding.get(key) or "")
        for key in ("rule", "title", "detail", "suggested_fix")
    ).lower()


def finding_issue_signature(finding: dict[str, Any]) -> str:
    text = finding_text(finding)
    for marker, needles in FINDING_MARKERS.items():
        if any(needle in text for needle in needles):
            return marker
    tokens = [
        token
        for token in re.findall(r"[a-z0-9_@-]{4,}", text)
        if token not in FINDING_DUPLICATE_STOPWORDS
    ]
    if not tokens:
        return re.sub(r"[^a-z0-9]+", "", str(finding.get("title") or "").lower())[:40]
    seen: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.append(token)
    return "-".join(seen[:8])


def finding_line_bucket(finding: dict[str, Any]) -> int:
    line = finding.get("line")
    return int(line) // 4 if isinstance(line, int) else -1


def finding_token_set(finding: dict[str, Any]) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9_@-]{4,}", finding_text(finding))
        if token not in FINDING_DUPLICATE_STOPWORDS
    }


def findings_are_duplicate_variants(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    existing_file = re.sub(r"\s+", "", str(existing.get("file") or "")).lower()
    candidate_file = re.sub(r"\s+", "", str(candidate.get("file") or "")).lower()
    if existing_file != candidate_file:
        return False
    existing_bucket = finding_line_bucket(existing)
    candidate_bucket = finding_line_bucket(candidate)
    if existing_bucket != -1 and candidate_bucket != -1 and existing_bucket != candidate_bucket:
        return False
    if finding_issue_signature(existing) == finding_issue_signature(candidate):
        return True
    existing_tokens = finding_token_set(existing)
    candidate_tokens = finding_token_set(candidate)
    if not existing_tokens or not candidate_tokens:
        return False
    overlap = len(existing_tokens & candidate_tokens)
    smaller = min(len(existing_tokens), len(candidate_tokens))
    return overlap >= 2 and overlap / max(1, smaller) >= 0.5


def finding_severity_rank(severity: Any) -> int:
    return FINDING_SEVERITY_RANK.get(str(severity or "").lower(), 0)


def finding_role_detail(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": finding.get("role"),
        "agent_id": finding.get("agent_id"),
        "severity": finding.get("severity"),
    }


def merge_duplicate_finding(existing: dict[str, Any], candidate: dict[str, Any]) -> None:
    existing.setdefault("agreed_by", [])
    existing_role = existing.get("role")
    if existing_role not in existing["agreed_by"]:
        existing["agreed_by"].append(existing_role)
    candidate_role = candidate.get("role")
    if candidate_role not in existing["agreed_by"]:
        existing["agreed_by"].append(candidate_role)

    details = existing.setdefault("role_details", [])
    existing_detail = finding_role_detail(existing)
    if existing_detail not in details:
        details.append(existing_detail)
    candidate_detail = finding_role_detail(candidate)
    if candidate_detail not in details:
        details.append(candidate_detail)

    if finding_severity_rank(candidate.get("severity")) > finding_severity_rank(existing.get("severity")):
        existing["severity"] = candidate.get("severity")
        for key in ("title", "detail", "suggested_fix"):
            if candidate.get(key):
                existing[key] = candidate.get(key)


def assign_finding_ids(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    counter = 0
    for finding in findings:
        existing = next((item for item in out if findings_are_duplicate_variants(item, finding)), None)
        if existing is not None:
            merge_duplicate_finding(existing, finding)
            continue
        counter += 1
        finding = dict(finding)
        finding["id"] = finding.get("id") or f"F-{counter}"
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


def secret_scan_findings(project: Path) -> list[dict[str, Any]]:
    paths = list(iter_project_files(project, all_files=True))
    out: list[dict[str, Any]] = []
    for finding in scan_paths(paths, project):
        sev = finding.get("severity", "medium")
        if sev not in BLOCKING_SEVERITIES:
            continue
        out.append(
            {
                "id": f"scan-{slugify(finding.get('rule', 'scan'))}-{slugify(finding.get('file', ''))}-{finding.get('line', 0)}",
                "role": "tree-scan",
                "severity": "high" if finding.get("rule") == "secret-material" else sev,
                "file": finding.get("file"),
                "line": finding.get("line"),
                "title": f"{finding.get('rule')} in tree",
                "detail": finding.get("evidence", ""),
                "suggested_fix": "Remove the secret/residual or, for a documented placeholder, waive with a reason.",
            }
        )
    for finding in architecture_debt_findings(paths, project):
        if finding.get("severity") in BLOCKING_SEVERITIES:
            out.append(
                {
                    "id": f"scan-{slugify(finding.get('rule', 'scan'))}-{slugify(finding.get('file', ''))}",
                    "role": "tree-scan",
                    "severity": finding.get("severity"),
                    "file": finding.get("file"),
                    "line": finding.get("line"),
                    "title": finding.get("rule"),
                    "detail": finding.get("evidence", ""),
                    "suggested_fix": "",
                }
            )
    return out


def jsonl_payloads(path: Path) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    if not path.exists():
        return payloads
    try:
        for line in read_text(path).splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                payloads.append(payload)
    except Exception:
        return []
    return payloads


def subagent_ids_from(path: Path) -> set[str]:
    ids: set[str] = set()
    for payload in jsonl_payloads(path):
        if payload.get("event") == "SubagentStart" and payload.get("agent_id"):
            ids.add(str(payload["agent_id"]))
    return ids


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
    return {
        "schema": "star-forge.review.v2",
        "created_at": now_utc(),
        "project": str(project),
        "scope": scope,
        "source_hash": None,
        "source_hash_unavailable": True,
        "problems": [problem],
        "manifest_profile": project_profile(project),
        "source_profile": read_source_profile(project) or None,
        "review_profile": "standard",
        "profile_lock": profile_lock,
        "reviewer_roles": [],
        "reviewer_count": 0,
        "required_review_roles": required_roles,
        "required_reviewer_count": len(required_roles),
        "missing_review_roles": required_roles,
        "stale_roles": [],
        "reviewers_witnessed": False,
        "findings": [],
        "fix_queue": [problem],
        "waived": sorted(load_waives(project, scope)),
        "file_problems": [problem],
    }


def merge_review(project: Path, scope: str) -> dict[str, Any]:
    files, file_problems = load_review_findings(project, scope)
    current, hash_problem = try_source_hash(project)
    if hash_problem or current is None:
        return review_payload_source_hash_unavailable(project, scope, hash_problem or source_hash_exception_problem(ForgeError("source_hash unavailable")))
    # Freshness is keyed on the source_hash each reviewer ATTESTED in its own file
    # (the spawn prompt hands it the current hash). A reviewer file counts only if
    # its attested hash equals the current tree. This survives deleting merged.json
    # (the attestation lives in the reviewer files, not a side ledger the gate also
    # reads) and does not livelock a clean re-review (an empty {findings:[]} written
    # at the current hash is fresh even though its findings are byte-identical).
    known_ids = known_subagent_ids(project)
    fresh_findings: list[dict[str, Any]] = []
    fresh_roles: list[str] = []
    stale_roles: list[str] = []
    reviewers_witnessed = True
    for entry in files:
        if entry.get("declared_source_hash") != current:
            stale_roles.append(entry["role"])
            continue
        fresh_roles.append(entry["role"])
        fresh_findings.extend(entry["findings"])
        # Witness check: a future host-controlled source may supply known ids.
        # In this version known_ids is empty, so local reviewer agent_id values are
        # provenance diagnostics only and the verdict remains advisory.
        if known_ids and str(entry.get("agent_id") or "") not in known_ids:
            reviewers_witnessed = False
    if not known_ids:
        reviewers_witnessed = False
    merged = assign_finding_ids([*fresh_findings, *secret_scan_findings(project)])
    waived = load_waives(project, scope)
    open_blocking: list[dict[str, Any]] = []
    for finding in merged:
        finding["waived"] = finding["id"] in waived
        if finding["severity"] in BLOCKING_SEVERITIES and not finding["waived"]:
            open_blocking.append(finding)
    reviewer_roles = sorted(set(fresh_roles))
    policy = required_review_policy(project, source_hash_value=current)
    required_roles = list(policy.roles)
    missing_roles = [role for role in required_roles if role not in reviewer_roles]
    manifest_profile = project_profile(project)
    effective_profile = review_profile(project)
    return {
        "schema": "star-forge.review.v2",
        "created_at": now_utc(),
        "project": str(project),
        "scope": scope,
        "source_hash": current,
        "manifest_profile": manifest_profile,
        "source_profile": read_source_profile(project) or None,
        "review_profile": effective_profile,
        "profile_lock": fast_mvp_profile_lock_state(project),
        "reviewer_roles": reviewer_roles,
        "reviewer_count": len(reviewer_roles),
        "required_review_roles": required_roles,
        "required_reviewer_count": len(required_roles),
        "review_policy": policy.to_dict(),
        "missing_review_roles": missing_roles,
        "stale_roles": sorted(set(stale_roles)),
        "reviewers_witnessed": reviewers_witnessed,
        "findings": merged,
        "fix_queue": open_blocking,
        "waived": sorted(waived),
        "file_problems": file_problems,
    }


def write_merged_review(project: Path, payload: dict[str, Any]) -> Path:
    path = reviews_scope_dir(project, payload.get("scope") or "noscope") / "merged.json"
    write_json(path, payload)
    return path


def load_merged_review(project: Path, scope: str) -> dict[str, Any] | None:
    path = reviews_scope_dir(project, scope) / "merged.json"
    if not path.exists():
        return None
    try:
        return read_json(path)
    except Exception:
        return None


def review_findings_for_done(project: Path, tasks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Done-time review gate rebuilt from reviewer files and the current tree."""
    if not all_tasks_complete(tasks):
        return []
    hash_problem = source_hash_unavailable_problem(project)
    if hash_problem:
        return [hash_problem]
    scope = scope_hash(project) or "noscope"
    if load_merged_review(project, scope) is None:
        return [{"severity": "high", "rule": "review-not-performed", "file": str(REVIEWS_DIR), "message": "No review wave recorded for the current scope. Spawn starforge-reviewer agents, then run `review`."}]
    merged = merge_review(project, scope)
    problems = merged.get("file_problems") or []
    if problems:
        first = next((item for item in problems if isinstance(item, dict)), {})
        return [{"severity": "high", "rule": "review-findings-invalid", "file": first.get("file", str(REVIEWS_DIR)), "message": f"A reviewer findings file is malformed and its findings cannot be trusted: {first.get('message')}. Fix the file and rerun `review`."}]
    if not merged.get("reviewer_roles"):
        if merged.get("stale_roles"):
            return [{"severity": "high", "rule": "review-stale", "file": str(REVIEWS_DIR), "message": "Every reviewer findings file predates the current source; re-spawn reviewers (rewrite their findings files) and rerun `review`."}]
        return [{"severity": "high", "rule": "review-empty", "file": str(REVIEWS_DIR), "message": "No reviewer findings files were present for the review; spawn at least one starforge-reviewer."}]
    queue = [item for item in (merged.get("fix_queue") or []) if isinstance(item, dict)]
    required_roles = required_review_roles(project)
    reviewer_roles = {str(role) for role in (merged.get("reviewer_roles") or [])}
    missing_roles = [role for role in required_roles if role not in reviewer_roles]
    if missing_roles:
        return [{
            "severity": "high",
            "rule": "reviewer-count-insufficient",
            "file": str(REVIEWS_DIR),
            "message": (
                f"Review profile `{review_profile(project)}` requires {len(required_roles)} fresh reviewer role(s): "
                f"{', '.join(required_roles)}. Missing: {', '.join(missing_roles)}. "
                "Spawn the missing starforge-reviewer agents and rerun `review`."
            ),
        }]
    if queue:
        first = queue[0]
        return [{"severity": "high", "rule": "review-fix-queue-open", "file": str(REVIEWS_DIR), "message": f"{len(queue)} unresolved blocking review finding(s); first: {first.get('id')} {first.get('title')} ({first.get('file')}). Fix and re-review, or waive with a reason."}]
    return []


def cmd_review(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    scope = scope_hash(project) or "noscope"
    payload = merge_review(project, scope)
    write_merged_review(project, payload)
    append_jsonl(
        project / LEDGER_FILE,
        {"schema": "star-forge.ledger.v1", "timestamp": now_utc(), "event": "review", "summary": f"reviewers={payload['reviewer_count']} open={len(payload['fix_queue'])}", "artifacts": [relative_to_project(reviews_scope_dir(project, scope) / 'merged.json', project)]},
    )
    print(json.dumps(payload, indent=2))
    blocking = payload.get("fix_queue") or []
    no_reviewers = not payload.get("reviewer_roles")
    missing_reviewers = bool(payload.get("missing_review_roles"))
    bad_files = bool(payload.get("file_problems"))
    return 0 if (not blocking and not no_reviewers and not missing_reviewers and not bad_files) or not args.strict else 1


def cmd_waive(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    if not str(args.reason or "").strip():
        raise ForgeError("waive requires --reason explaining why the finding is not a real blocker")
    scope = scope_hash(project) or "noscope"
    append_jsonl(project / WAIVES_FILE, {"schema": "star-forge.waive.v1", "timestamp": now_utc(), "scope": scope, "finding": args.finding, "reason": args.reason})
    append_jsonl(project / INCIDENTS_FILE, {"schema": "star-forge.incident.v1", "timestamp": now_utc(), "kind": "waive", "finding": args.finding, "reason": args.reason})
    # Re-merge so the fix queue reflects the waive immediately.
    payload = merge_review(project, scope)
    write_merged_review(project, payload)
    print(json.dumps({"schema": "star-forge.waive.v1", "finding": args.finding, "reason": args.reason, "open_findings": len(payload["fix_queue"])}, indent=2))
    return 0


# -------------------------------------------------------------- complete-task


def cmd_complete_task(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    plan_path = project / PLAN_FILE
    tasks = parse_tasks(plan_path)
    task = next((item for item in tasks if item.get("id") == args.task), None)
    findings: list[dict[str, Any]] = []
    findings.extend(validate_project_plan_contract(project, tasks))
    hash_problem = source_hash_unavailable_problem(project)
    _current_source_hash, snapshot_problem = try_source_hash(project)
    if hash_problem:
        findings.append(hash_problem)
    elif snapshot_problem:
        findings.append(snapshot_problem)
    if task is None:
        findings.append({"severity": "critical", "rule": "task-missing", "message": f"Task {args.task} does not exist."})
    else:
        if task.get("status") not in {"ready", "in_progress", "reviewing"}:
            findings.append({"severity": "high", "rule": "task-status-not-completable", "message": f"Task {args.task} has status `{task.get('status')}`; only ready/in_progress/reviewing can be completed."})
        complete_ids = {item["id"] for item in tasks if item.get("status") == "complete"}
        unmet = [dep for dep in parse_depends(task.get("depends", "")) if dep not in complete_ids]
        if unmet:
            findings.append({"severity": "high", "rule": "task-dependencies-incomplete", "message": f"Task {args.task} has incomplete dependencies: {', '.join(unmet)}."})
        if hash_problem or snapshot_problem:
            pass
        elif task_allows_noop_verification(task):
            if not has_noop_verify(project, args.task):
                findings.append({"severity": "high", "rule": "verify-noop-missing", "message": f"Docs task {args.task} needs a recorded no-op verify run."})
        elif not fresh_passing_verify(project, task):
            findings.append({"severity": "high", "rule": "verify-stale", "message": f"Task {args.task} has no passing verify run that matches its declared Verify command and the CURRENT source tree."})
        if task_is_visual(task) and not passing_browser_runs(project, args.task):
            findings.append({"severity": "high", "rule": "browser-run-missing", "message": f"User-facing task {args.task} needs a passing browser-run."})
        if not [item for item in (args.changed_file or []) if str(item).strip()]:
            findings.append({"severity": "high", "rule": "changed-file-missing", "message": "complete-task requires --changed-file evidence."})
    blockers = blocking_items(findings)
    if blockers:
        print(json.dumps({"schema": "star-forge.complete-task.v1", "task": args.task, "verdict": "REFUSED", "findings": findings, "updated": False}, indent=2))
        return 1
    evidence = ", ".join(args.changed_file or [])
    summary = args.summary or f"Task {args.task} completed with verified evidence."
    update_plan_task_row(plan_path, args.task, {"Status": "complete", "Evidence": evidence or summary})
    completion_artifact = project / STATE_SUBDIR / f"complete-task-{slugify(args.task)}.json"
    snapshot, _snapshot_problem = safe_release_snapshot(project)
    payload = {
        "schema": "star-forge.complete-task.v1",
        "created_at": now_utc(),
        "task": args.task,
        "verdict": "COMPLETE",
        "changed_files": args.changed_file or [],
        "summary": summary,
        "source_snapshot": snapshot,
    }
    write_json(completion_artifact, payload)
    append_jsonl(project / LEDGER_FILE, {"schema": "star-forge.ledger.v1", "timestamp": now_utc(), "event": "task-complete", "task": args.task, "summary": summary, "artifacts": args.changed_file or []})
    print(json.dumps(payload | {"updated": True, "plan": PLAN_FILE}, indent=2))
    return 0


# ----------------------------------------------------------------------- done


def done_payload(project: Path) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    profile_lock = fast_mvp_profile_lock_state(project)
    hash_problem = source_hash_unavailable_problem(project)
    lifecycle_contract = blueprint_lifecycle_contract(project)
    modern_lifecycle = not lifecycle_contract.get("legacy", True)
    if not blueprint_is_approved(project):
        problems.append({"severity": "critical", "message": "Blueprint.md is missing or not explicitly approved"})
    plan_path = project / PLAN_FILE
    if not plan_path.exists():
        problems.append({"severity": "critical", "message": "Plan.md is missing"})
    else:
        try:
            tasks = parse_tasks(plan_path)
            problems.extend(validate_tasks(tasks))
            problems.extend(validate_project_plan_contract(project, tasks))
            parse_problem = plan_parse_problem(plan_path, tasks)
            if parse_problem:
                problems.append({"severity": "critical", "message": parse_problem})
            elif not tasks or plan_is_placeholder(tasks):
                problems.append({"severity": "critical", "message": "Plan.md contains no real tasks"})
            elif not all_tasks_complete(tasks):
                problems.append({"severity": "high", "message": "not all Plan.md tasks are complete"})
        except ForgeError as exc:
            problems.append({"severity": "critical", "message": str(exc)})
    if hash_problem:
        problems.append(finding_problem(hash_problem))
    else:
        for finder in (verify_findings, browser_findings):
            for finding in finder(project, tasks):
                if finding["severity"] in BLOCKING_SEVERITIES:
                    problems.append(finding_problem(finding))
        for finding in review_findings_for_done(project, tasks):
            if finding["severity"] in BLOCKING_SEVERITIES:
                problems.append(finding_problem(finding))
    dirty = source_dirty_entries(git_status(project))
    if dirty:
        problems.append({"severity": "medium", "message": "working tree is not clean", "files": dirty[:30]})
    # Drift vs a prior proof is informational here: the verify/review freshness
    # gates above already force a real re-pass after any source change, so a
    # passing predicate legitimately supersedes the old proof. Amend re-entry
    # (scaffolding the AMEND task) is `run`'s job.
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
    foundation_gate = lifecycle_gate_state(
        project,
        kind="foundation",
        required=modern_lifecycle,
        current_source_hash=current_source_hash,
    )
    delivery_gate = lifecycle_gate_state(
        project,
        kind="delivery",
        required=modern_lifecycle,
        current_source_hash=current_source_hash,
        expected_delivery_target=str(
            (lifecycle_contract.get("delivery") or {}).get("target") or ""
        ),
    )
    if modern_lifecycle:
        for name, gate in (
            ("foundation", foundation_gate),
            ("delivery", delivery_gate),
        ):
            if not gate.get("satisfied"):
                blockers = gate.get("blockers") or []
                detail = str(blockers[0]) if blockers else f"{name} proof did not pass"
                problems.append(
                    {
                        "severity": "high",
                        "rule": f"{name}-gate",
                        "message": f"{name.title()} lifecycle gate is incomplete: {detail}",
                    }
                )
    if lifecycle_hash_problem and not hash_problem:
        problems.append(finding_problem(lifecycle_hash_problem))
    blocking = blocking_items(problems)
    enforcement = enforcement_mode(project)
    # Project-local JSONL ledgers are useful diagnostics, but they are advisory
    # because the same actors being evaluated can write them.
    scope = scope_hash(project) or "noscope"
    merged = None if hash_problem else (merge_review(project, scope) if load_merged_review(project, scope) is not None else None)
    review_performed = bool(merged and merged.get("reviewer_roles"))
    review_witnessed = bool(merged and merged.get("reviewers_witnessed"))
    delegated_complete = any(task.get("status") == "complete" and task_requires_real_workers(task) for task in tasks)
    hooks = hooks_liveness(project)
    trusted_subagent_observed = bool(known_subagent_ids(project))
    local_subagent_observed = bool(local_subagent_ids(project))
    waive_count = len(merged.get("waived") or []) if merged else 0
    witness = {
        "hooks_live": enforcement == "witnessed",
        "local_hooks_observed": bool(hooks.get("local_events_observed")),
        "trusted_hooks_observed": bool(hooks.get("events_observed")),
        "subagent_observed": trusted_subagent_observed,
        "local_subagent_observed": local_subagent_observed,
        "trusted_subagent_observed": trusted_subagent_observed,
        "delegated_complete": delegated_complete,
        "review_performed": review_performed,
        "review_witnessed": review_witnessed,
        "waived_findings": waive_count,
    }
    advisory_reasons: list[str] = []
    if enforcement != "witnessed":
        advisory_reasons.append("no trusted hook witness source is enabled in this version")
    if delegated_complete and not trusted_subagent_observed:
        advisory_reasons.append("delegated tasks lack a trusted sub-agent witness (local events are diagnostic only)")
    if review_performed and not review_witnessed:
        advisory_reasons.append("review findings lack a trusted sub-agent witness")
    if blocking:
        verdict = "NEEDS_CHANGES"
    elif advisory_reasons:
        verdict = "COMPLETE (advisory: " + "; ".join(advisory_reasons) + ")"
    else:
        verdict = "COMPLETE"
    if verdict.startswith("COMPLETE") and waive_count:
        verdict += f" [{waive_count} waived finding(s)]"
    return {
        "schema": "star-forge.done.v1",
        "created_at": now_utc(),
        "project": str(project),
        "verdict": verdict,
        "is_complete": verdict.startswith("COMPLETE"),
        "enforcement": enforcement,
        "witness": witness,
        "task_count": len(tasks),
        "counts": task_counts(tasks),
        "snapshot": snapshot,
        "drift": drift,
        "foundation": foundation_gate,
        "delivery": delivery_gate,
        "problems": problems,
        "source_hash_unavailable": bool(hash_problem or snapshot_problem),
    }


def load_proof(project: Path) -> dict[str, Any] | None:
    path = project / PROOF_FILE
    if not path.exists():
        return None
    try:
        return read_json(path)
    except Exception:
        return None


def detect_drift(project: Path, proof: dict[str, Any] | None) -> dict[str, Any]:
    if not proof:
        return {"detected": False, "changed_files": []}
    current_source = source_hash(project)
    current_scope = scope_hash(project) or "noscope"
    source_changed = proof.get("source_hash") != current_source
    scope_changed = (proof.get("scope_hash") or "noscope") != current_scope
    changed: list[str] = []
    if source_changed:
        changed = source_dirty_entries(git_status(project)) or _diff_since(project, proof.get("head"))
    return {
        "detected": bool(source_changed or scope_changed),
        "source_changed": source_changed,
        "scope_changed": scope_changed,
        "changed_files": changed,
    }


def completed_amendment_covering_drift(project: Path, tasks: Sequence[dict[str, Any]], drift: dict[str, Any]) -> str | None:
    if not drift.get("detected"):
        return None
    if any(task.get("id", "").startswith("AMEND-") and task.get("status") != "complete" for task in tasks):
        return None
    current = source_hash(project)
    completed_amends = (
        task for task in tasks
        if str(task.get("id", "")).startswith("AMEND-") and task.get("status") == "complete"
    )
    for task in sorted(completed_amends, key=lambda item: str(item.get("id") or ""), reverse=True):
        task_id = str(task.get("id") or "")
        path = project / STATE_SUBDIR / f"complete-task-{slugify(task_id)}.json"
        try:
            payload = read_json(path)
        except Exception:
            continue
        snapshot = payload.get("source_snapshot")
        if (
            payload.get("task") == task_id
            and payload.get("verdict") == "COMPLETE"
            and isinstance(snapshot, dict)
            and snapshot.get("source_hash") == current
        ):
            return task_id
    return None


def annotate_drift_coverage(project: Path, tasks: Sequence[dict[str, Any]], drift: dict[str, Any]) -> dict[str, Any]:
    covered_by = completed_amendment_covering_drift(project, tasks, drift)
    return dict(drift) | {
        "covered_by_completed_amendment": covered_by,
        "actionable": bool(drift.get("detected") and not covered_by),
    }


def _diff_since(project: Path, head: str | None) -> list[str]:
    if not head or not is_git_repo(project):
        return []
    code, out, _ = run_git(["diff", "--name-only", f"{head}..HEAD"], project)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def cmd_done(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    payload = done_payload(project)
    if payload["is_complete"]:
        ensure_state_dirs(project)
        write_json(
            project / PROOF_FILE,
            {
                "schema": "star-forge.proof.v1",
                "created_at": now_utc(),
                "head": git_head(project),
                "source_hash": source_hash(project),
                "scope_hash": scope_hash(project) or "noscope",
                "verdict": payload["verdict"],
            },
        )
        if args.write_summary:
            write_text(project / FINAL_SUMMARY, done_summary_markdown(payload))
            payload["summary_artifact"] = str(FINAL_SUMMARY)
    print(json.dumps(payload, indent=2))
    return 0 if payload["is_complete"] or not args.strict else 1


def done_summary_markdown(payload: dict[str, Any]) -> str:
    counts = payload.get("counts", {})
    return f"""# Star Forge Final Summary

Generated: {payload.get('created_at')}

## Verdict

{payload.get('verdict')}

## Project

{payload.get('project')}

## Tasks

{payload.get('task_count')} ({counts})

## Enforcement

{payload.get('enforcement')}
"""


# ------------------------------------------------------------------ learnings


def learnings_home() -> Path:
    return Path(os.environ.get("STAR_FORGE_LEARNINGS_HOME") or LEARNINGS_HOME)


def project_keywords(project: Path) -> set[str]:
    terms: set[str] = set()
    bp = project / BLUEPRINT_FILE
    if bp.exists():
        try:
            terms |= lexical_terms(read_text(bp))
        except OSError:
            pass
    for path in snapshot_file_candidates(project):
        terms.add(path.suffix.lower().lstrip("."))
        terms.add(path.name.lower())
    return {term for term in terms if term}


def learnings_digest(project: Path, limit: int = 5) -> list[dict[str, Any]]:
    home = learnings_home()
    if not home.exists():
        return []
    keywords = project_keywords(project)
    scored: list[tuple[int, dict[str, Any]]] = []
    for path in sorted(home.glob("**/*.md")):
        try:
            text = read_text(path)
        except OSError:
            continue
        meta = parse_frontmatter(text)
        triggers = {item.strip().lower() for item in str(meta.get("triggers") or "").split(",") if item.strip()}
        score = len(triggers & keywords)
        if not triggers:
            score = 0
        if score <= 0:
            continue
        scored.append((score, {"title": meta.get("title") or path.stem, "rule": meta.get("rule") or "", "category": meta.get("category") or path.parent.name, "path": str(path)}))
    scored.sort(key=lambda item: (-item[0], item[1]["title"]))
    return [item for _, item in scored[:limit]]


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    return meta


def cmd_learn(args: argparse.Namespace) -> int:
    if not str(args.title or "").strip() or not str(args.rule or "").strip():
        raise ForgeError("learn requires --title and --rule")
    home = learnings_home()
    category = slugify(args.category or "general").lower()
    slug = slugify(args.title).lower()
    path = home / category / f"{slug}.md"
    triggers = ", ".join(item.strip() for item in (args.trigger or []) if item.strip())
    body = f"""---
title: {args.title}
category: {category}
triggers: {triggers}
rule: {args.rule}
source: {args.source or 'manual'}
date: {now_utc()}
---

{args.rule}

{args.detail or ''}
"""
    write_text(path, body)
    print(json.dumps({"schema": "star-forge.learn.v1", "path": str(path), "title": args.title, "category": category, "triggers": triggers}, indent=2))
    return 0


# ----------------------------------------------------------------- state dirs


def ensure_state_dirs(project: Path) -> None:
    for path in [STATE_SUBDIR, RUNS_DIR, TASKS_DIR, FINAL_DIR, REVIEWS_DIR, SCREENSHOTS_DIR, RUNTIME_DIR, LOOP_DIR]:
        (project / path).mkdir(parents=True, exist_ok=True)
    if not (project / LEDGER_FILE).exists():
        write_text(project / LEDGER_FILE, "")


# ------------------------------------------------------------- liveness/version


def hooks_liveness(project: Path) -> dict[str, Any]:
    def last_timestamp(path: Path) -> str | None:
        events = jsonl_payloads(path)
        if not events:
            return None
        return str(events[-1].get("timestamp") or "") or None

    local_last_event_at = last_timestamp(project / HOOK_EVENTS)
    return {
        "events_observed": False,
        "last_event_at": None,
        "local_events_observed": local_last_event_at is not None,
        "local_last_event_at": local_last_event_at,
        "trusted_witness_source": None,
        "remediation": "No trusted hook witness source is enabled in this version. Project-local hook events are advisory diagnostics.",
    }


def hook_trust_notice(project: Path) -> dict[str, Any]:
    liveness = hooks_liveness(project)
    show = bool(
        not liveness.get("local_events_observed")
        and not (project / HOOK_TRUST_NOTICE_FILE).exists()
    )
    return {
        "show": show,
        "message": (
            "Optional observer hooks are not trusted yet. Use Codex `/hooks` and trust the "
            "Star Forge entries to enable continuity re-anchors; Star Forge still works without them."
        ),
        "marker": str(HOOK_TRUST_NOTICE_FILE),
    }


def mark_hook_trust_notice_seen(project: Path) -> None:
    ensure_state_dirs(project)
    write_json_stable(
        project / HOOK_TRUST_NOTICE_FILE,
        {
            "schema": "star-forge.hook-trust-notice.v1",
            "shown_at": now_utc(),
            "message": "Optional observer hooks notice was shown to the user.",
        },
    )


def enforcement_mode(project: Path) -> str:
    return "witnessed" if hooks_liveness(project)["events_observed"] else "advisory"


def version_core(raw: str) -> str:
    core = re.split(r"[+-]", raw, maxsplit=1)[0]
    return core


def version_key(raw: str) -> tuple[Any, ...]:
    """Numeric-aware version ordering so 0.10.0 sorts above 0.3.0."""
    core = version_core(raw)
    parts: list[Any] = []
    for piece in core.split("."):
        parts.append(int(piece) if piece.isdigit() else piece)
    return tuple(parts)


def newest_cache_version() -> str | None:
    cache = Path.home() / ".codex" / "plugins" / "cache"
    versions: list[str] = []
    for path in cache.glob("*/star-forge/*/.codex-plugin/plugin.json"):
        try:
            versions.append(str(read_json(path).get("version") or path.parent.parent.name))
        except Exception:
            versions.append(path.parent.parent.name)
    if not versions:
        return None
    return sorted(versions, key=version_key)[-1]


def version_info(project: Path) -> dict[str, Any]:
    manifest_version: str | None = None
    try:
        manifest_version = str(read_json(plugin_root() / ".codex-plugin" / "plugin.json").get("version") or "")
    except Exception:
        manifest_version = None
    newest = newest_cache_version()
    return {
        "script": SF_VERSION,
        "plugin_manifest": manifest_version,
        "newest_cache": newest,
        "stale_cache": bool(newest and version_key(newest) < version_key(SF_VERSION)),
    }


# ------------------------------------------------------------------ agents


def toml_multiline(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return f'"""\n{escaped}"""'


def agent_role_names() -> list[str]:
    root = plugin_root() / "agents"
    return sorted(path.parent.name for path in root.glob("*/agent.md"))


def render_agent_toml(role: str) -> str:
    source = plugin_root() / "agents" / role / "agent.md"
    body = read_text(source)
    mission = ""
    match = re.search(r"## Mission\n+(.+?)(?:\n\n|\n##)", body, re.DOTALL)
    if match:
        mission = " ".join(match.group(1).split())
    description = (mission[:200] or f"Star Forge {role} role.").rstrip()
    return (
        f'name = "{AGENT_NAME_PREFIX}{role}"\n'
        f"description = {json.dumps(description)}\n"
        f"developer_instructions = {toml_multiline(body.rstrip())}\n"
    )


def cmd_agents_install(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    target = project / ".codex" / "agents"
    target.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for role in agent_role_names():
        path = target / f"{AGENT_NAME_PREFIX}{role}.toml"
        write_text(path, render_agent_toml(role))
        installed.append(relative_to_project(path, project))
    print(json.dumps({"schema": "star-forge.agents-install.v1", "installed": installed}, indent=2))
    return 0


# --------------------------------------------------------------- isolation


def resolve_isolation(raw_project: Path, *, product_slug: str, adopt_root: bool) -> tuple[Path | None, dict[str, Any] | None]:
    """Shared isolation guard for run and init. Returns (project, blocked_payload).

    A foreign project gets its own work/<slug>/ with a root redirect; building at
    the root requires an explicit --adopt-root the manifest records, so a
    contaminated root can never silently bless itself (the Boss Fight failure).
    """
    existing = find_star_forge_project_root(raw_project)
    if existing:
        return existing, None
    if root_needs_product_isolation(raw_project):
        if str(product_slug or "").strip():
            slug = slugify(product_slug).lower()
            project = (raw_project / "work" / slug).resolve()
            project.mkdir(parents=True, exist_ok=True)
            # The nested manifest must exist BEFORE the root redirect: otherwise
            # follow_project_redirect bounces back to the foreign root (no markers
            # in the target yet) and init scaffolds into the user's repo.
            ensure_project_manifest(project, product_slug=slug)
            write_json(raw_project / PROJECT_MANIFEST, {"schema": "star-forge.project-redirect.v1", "project_root": str(project)})
            if (raw_project / ".git").exists():
                ensure_gitignore_entries(raw_project, [".starforge/", "work/"])
            for carried in (BLUEPRINT_FILE, PLAN_FILE):
                src = raw_project / carried
                dst = project / carried
                if src.exists() and not dst.exists():
                    write_text(dst, read_text(src))
            return project, None
        if adopt_root:
            ensure_project_manifest(raw_project, root_mode="adopted-root")
            return raw_project, None
        payload = {
            "schema": "star-forge.state.v3",
            "created_at": now_utc(),
            "project": str(raw_project),
            "phase": "blocked:isolation-required",
            "required_next_action": (
                "This directory already contains a non-Star-Forge project. Rerun with "
                "--product-slug <name> to build under work/<name>/ (recommended), or "
                "--adopt-root to deliberately build in place (recorded in the manifest)."
            ),
        }
        return None, payload
    return raw_project, None


# ----------------------------------------------------------------------- init


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
    if ensure_git_repo(project):
        created.append(".git/")
    else:
        skipped.append(".git/ already exists")
    ensure_state_dirs(project)
    requested_profile = "fast-mvp" if getattr(args, "fast_mvp", False) else str(getattr(args, "profile", "") or "")
    profile_selected_before_gates = requested_profile == "fast-mvp" and not profile_downgrade_lock_reasons(project)
    ensure_project_manifest(project, product_slug=args.product_slug or "", profile=requested_profile)
    created.append(str(PROJECT_MANIFEST))
    if requested_profile:
        created.append(SOURCE_PROFILE_FILE)
    for template_name, target_name in [("Blueprint.md", BLUEPRINT_FILE), ("Plan.md", PLAN_FILE)]:
        target = project / target_name
        if args.force or not target.exists():
            write_text(target, template_text(template_name))
            created.append(target_name)
        else:
            skipped.append(f"{target_name} already exists")
    gitignore_changes = ensure_gitignore_entries(
        project,
        [
            ".starforge/screenshots/*",
            "!.starforge/screenshots/manifest.json",
            ".starforge/state/changed-files.jsonl",
            ".starforge/state/hook-events.jsonl",
            ".starforge/state/subagent-events.jsonl",
            ".starforge/state/auto-continue.json",
            ".starforge/state/incidents.jsonl",
            ".starforge/state/hook-trust-notice.json",
        ],
    )
    created.extend(f".gitignore {item}" for item in gitignore_changes)
    if not getattr(args, "no_agents", False):
        agents_dir = project / ".codex" / "agents"
        for role in agent_role_names():
            write_text(agents_dir / f"{AGENT_NAME_PREFIX}{role}.toml", render_agent_toml(role))
        created.append(".codex/agents/")
    setup_record = {
        "schema": "star-forge.ledger.v1",
        "timestamp": now_utc(),
        "event": "setup",
        "summary": "Initialized Star Forge project",
        "profile": normalize_project_profile(requested_profile or "standard"),
        "profile_selected_before_gates": profile_selected_before_gates,
        "artifacts": [BLUEPRINT_FILE, PLAN_FILE, str(LEDGER_FILE)],
    }
    profile_path = source_profile_path(project)
    if profile_path.exists():
        setup_record["source_profile_sha256"] = file_sha256(profile_path)
    append_jsonl(project / LEDGER_FILE, setup_record)
    print(json.dumps({"schema": "star-forge.init.v1", "created": created, "skipped": skipped, "project": str(project)}, indent=2))
    return 0


# ---------------------------------------------------------------- state engine


def reviewer_spawn_prompt(
    project: Path,
    scope: str,
    role: str = "correctness",
    *,
    source_hash_value: str | None = None,
    selection_reason: str = "",
) -> str:
    rel = relative_to_project(reviews_scope_dir(project, scope) / f"{role}.findings.json", project)
    if source_hash_value is None:
        source_hash_value, hash_problem = try_source_hash(project)
        if hash_problem or source_hash_value is None:
            message = (hash_problem or source_hash_exception_problem(ForgeError("source_hash unavailable"))).get("message")
            raise ForgeError(str(message))
    sh = source_hash_value
    lens = REVIEW_ROLE_LENSES.get(role, "project quality risks, regressions, and release blockers")
    safe_reason = re.sub(
        r"[^A-Za-z0-9 .,/_():;-]+",
        " ",
        str(selection_reason or ""),
    ).strip()
    reason_instruction = (
        f"Applicability: {safe_reason}. " if safe_reason else ""
    )
    sample = json.dumps(
        {
            "role": role,
            "agent_id": "<your real thread id>",
            "source_hash": sh,
            "findings": [
                {
                    "severity": "high",
                    "file": "...",
                    "title": "...",
                    "detail": "...",
                    "suggested_fix": "...",
                }
            ],
        },
        separators=(",", ":"),
    ).replace('"', '\\"')
    return (
        f"spawn_agent {AGENT_NAME_PREFIX}reviewer \"[SF:review:{role}] Review the diff against the approved Blueprint with the {role} lens: {lens}. "
        f"{reason_instruction}"
        f"Write findings ONLY to {rel} as {sample}. "
        f'The role MUST be exactly {role}. '
        f'The source_hash MUST be exactly {sh} (it attests you reviewed the current tree). '
        'Include your real thread id as agent_id for provenance diagnostics only; local ids do not create unqualified COMPLETE in this version. Do not edit source. Report an empty findings array if clean.\"'
    )


def builder_spawn_prompt(task: dict[str, Any]) -> str:
    files = ", ".join(task_files(task)) or "(see Plan row)"
    return (
        f"spawn_agent {AGENT_NAME_PREFIX}builder \"[SF:{task['id']}] Implement task {task['id']}: {task.get('description','')}. "
        f"Files you own: {files}. After implementing, the coordinator records `verify` and (for UI) `browser-run`.\""
    )


def spawn_plan(project: Path, tasks: Sequence[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    if phase in {"build", "amend"}:
        for task in tasks:
            if task.get("status") not in {"ready", "in_progress"} or not task_requires_real_workers(task):
                continue
            plan.append({"task": task["id"], "agent": f"{AGENT_NAME_PREFIX}builder", "tag": f"SF:{task['id']}", "spawn": builder_spawn_prompt(task)})
            if len(plan) >= 6:
                break
    elif phase == "review":
        scope = scope_hash(project) or "noscope"
        current_source_hash, hash_problem = try_source_hash(project)
        if hash_problem or current_source_hash is None:
            return plan
        policy = required_review_policy(
            project,
            source_hash_value=current_source_hash,
        )
        for selection in policy.selections:
            role = selection.role
            reasons = "; ".join(selection.reasons)
            findings_file = relative_to_project(reviews_scope_dir(project, scope) / f"{role}.findings.json", project)
            plan.append({
                "task": "review-wave",
                "role": role,
                "reasons": list(selection.reasons),
                "findings_file": findings_file,
                "agent": f"{AGENT_NAME_PREFIX}reviewer",
                "tag": f"SF:review:{role}",
                "spawn": reviewer_spawn_prompt(
                    project,
                    scope,
                    role,
                    source_hash_value=current_source_hash,
                    selection_reason=reasons,
                ),
            })
    return plan


def operating_card(project: Path, state: dict[str, Any]) -> str:
    versions = state.get("versions", {})
    liveness = hooks_liveness(project)
    if state.get("enforcement") == "witnessed":
        hooks = "TRUSTED (witnessed)"
    elif liveness.get("local_events_observed"):
        hooks = "ADVISORY (local hook diagnostics observed)"
    else:
        hooks = "ADVISORY (no trusted witness source)"
    lines = [
        f"star-forge {versions.get('script')} | hooks: {hooks} | phase: {state.get('phase')}",
        f"NEXT: {state.get('required_next_action')}",
    ]
    profile_lock = state.get("profile_lock") or {}
    if profile_lock.get("status") in {"pending", "blocked", "standard-required"}:
        note = str(profile_lock.get("message") or "")
        action = str(profile_lock.get("next_action") or "")
        if note and action:
            lines.append(f"PROFILE: {note} Next: {action}")
        elif note:
            lines.append(f"PROFILE: {note}")
    if versions.get("stale_cache"):
        lines.append(f"PLUGIN: cache {versions.get('newest_cache')} is older than {versions.get('script')} - reinstall with `codex plugin marketplace add <path>` before relying on bundled hook diagnostics.")
    hook_notice = state.get("hook_trust_notice") or {}
    if hook_notice.get("show") and hook_notice.get("message"):
        lines.append(f"HOOKS: {hook_notice.get('message')}")
    spawn = state.get("spawn_plan") or []
    if spawn:
        lines.append("SPAWN (paste as-is):")
        spawn_limit = (
            adaptive_review_policy.MAX_REVIEW_AGENTS
            if state.get("phase") == "review"
            else 3
        )
        for entry in spawn[:spawn_limit]:
            lines.append(f"  {entry.get('spawn')}")
    lines.extend(
        [
            "RULES: 1) start every turn with `run`  2) delegate-mode tasks need a real spawn",
            "3) verify after every task (output is captured, claims are not)  4) UI work needs browser-run",
            "5) never hand-edit Plan rows — use complete-task; post-done edits auto-reopen as `amend`",
        ]
    )
    digest = state.get("learnings_digest") or []
    if digest:
        lines.append("LEARNINGS: " + "; ".join(item.get("title", "") for item in digest[:3]))
    return "\n".join(lines)


def canonical_state_payload(project: Path, *, objective: str = "", mode: str = "cruise", fast_mvp: bool = False) -> dict[str, Any]:
    manifest = ensure_project_manifest(project, objective=objective)
    profile_lock = fast_mvp_profile_lock_state(project)
    blueprint_state = blueprint_lock_state(project)
    lifecycle_contract = blueprint_lifecycle_contract(project)
    modern_lifecycle = not lifecycle_contract.get("legacy", True)
    current_source_hash, hash_problem = try_source_hash(project)
    source_hash_blocked = hash_problem is not None
    plan_path = project / PLAN_FILE
    tasks = parse_tasks(plan_path) if plan_path.exists() else []
    adaptive_policy = required_review_policy(
        project,
        source_hash_value=current_source_hash,
        bind_source_hash=not source_hash_blocked,
    )
    parse_problem = plan_parse_problem(plan_path, tasks)
    proof = load_proof(project)
    drift = source_hash_unavailable_state(profile_lock, problems=[hash_problem] if hash_problem else None) if source_hash_blocked else detect_drift(project, proof)
    setup_missing = (
        not is_git_repo(project)
        or not (project / BLUEPRINT_FILE).exists()
        or not (project / PLAN_FILE).exists()
        or not (project / LEDGER_FILE).exists()
    )
    scope = scope_hash(project) or "noscope"
    review_blockers = [] if source_hash_blocked else review_findings_for_done(project, tasks)
    drift = annotate_drift_coverage(project, tasks, drift)
    foundation_gate = lifecycle_gate_state(
        project,
        kind="foundation",
        required=modern_lifecycle,
        current_source_hash=current_source_hash,
    )
    delivery_gate = lifecycle_gate_state(
        project,
        kind="delivery",
        required=modern_lifecycle,
        current_source_hash=current_source_hash,
        expected_delivery_target=str(
            (lifecycle_contract.get("delivery") or {}).get("target") or ""
        ),
    )
    legacy_amend_requires_lock = bool(
        blueprint_state.get("status") == "legacy-approved"
        and drift.get("actionable")
        and proof
    )
    plan_complete = bool(
        blueprint_state.get("approved")
        and tasks
        and not plan_is_placeholder(tasks)
        and not legacy_amend_requires_lock
    )
    build_complete = all_tasks_complete(tasks)
    review_complete = build_complete and not review_blockers
    done = done_payload(project) if review_complete else None
    phase = project_lifecycle.resolve_phase(
        legacy=not modern_lifecycle,
        setup_complete=not setup_missing,
        blocked=bool(
            source_hash_blocked
            or profile_lock.get("status") == "blocked"
            or (parse_problem and (not modern_lifecycle or plan_complete))
        ),
        intake_complete=bool(
            blueprint_state.get("approved")
            or (lifecycle_contract.get("intake") or {}).get("complete")
        ),
        design_required=(lifecycle_contract.get("design") or {}).get("required"),
        design_complete=bool(
            blueprint_state.get("approved")
            or (lifecycle_contract.get("design") or {}).get("complete")
        ),
        plan_complete=plan_complete,
        foundation_complete=bool(foundation_gate.get("satisfied")),
        amendment_required=bool(drift.get("actionable") and proof),
        build_complete=build_complete,
        review_complete=review_complete,
        delivery_complete=bool(delivery_gate.get("satisfied")),
        completion_complete=bool(done and done.get("is_complete")),
    )
    if source_hash_blocked and hash_problem and not parse_problem:
        next_action = f"{hash_problem.get('message')} Repair the source hash blocker, then rerun."
    elif profile_lock.get("status") == "blocked" and not parse_problem:
        next_action = f"{profile_lock.get('message')} Next action: {profile_lock.get('next_action')}"
    elif blueprint_state.get("status") in {"drifted", "invalid"}:
        next_action = (
            "Blueprint approval is invalid. Review the current contract with the "
            "user, then run `approve-blueprint` after explicit approval."
        )
    elif (
        blueprint_state.get("status") == "legacy-approved"
        and drift.get("actionable")
        and proof
    ):
        next_action = (
            "This legacy Blueprint is readable, but amendments require a v0.4 "
            "content lock. Run `approve-blueprint` only after explicit user approval."
        )
    else:
        foundation_blocker = next(iter(foundation_gate.get("blockers") or []), "")
        delivery_blocker = next(iter(delivery_gate.get("blockers") or []), "")
        next_action = {
            "setup": "Initialize Star Forge project artifacts.",
            "intake": "Resolve every material decision in the Blueprint Intake Decision Record and record explicit assumptions for the rest.",
            "design": "Select an original grounded Design Direction, or record the checked capabilities and documented unavailable state.",
            "plan": "Create or revise Blueprint.md (with AC-n acceptance criteria), get explicit approval, then a normalized Plan.md.",
            "foundation": "Satisfy the approved Foundation Contract before feature work." + (f" First blocker: {foundation_blocker}" if foundation_blocker else ""),
            "build": "Build ready tasks — spawn starforge-builder for delegate-mode tasks — then `verify` (and `browser-run` for UI).",
            "review": "Spawn starforge-reviewer agents, run `review`, then fix or waive the queue.",
            "deliver": "Produce the exact approved delivery result and fresh source-bound delivery proof." + (f" First blocker: {delivery_blocker}" if delivery_blocker else ""),
            "amend": "Post-completion changes were detected; an amendment task was scaffolded. Build, verify, review it, then re-run `done`.",
            "done": "Project is complete; publish only if explicitly requested." if done and done.get("is_complete") else "Run the final completion predicate and resolve any remaining source, proof, or cleanliness blocker.",
            "blocked": f"Repair Plan.md before continuing: {parse_problem}" if parse_problem else "Inspect blockers and continue the safest unblocked phase.",
        }.get(phase, "Inspect state and continue the safest unblocked phase.")
    state = {
        "schema": "star-forge.state.v3",
        "created_at": now_utc(),
        "project": str(project),
        "project_manifest": manifest,
        "mode": mode,
        "objective": objective,
        "fast_mvp": fast_mvp,
        "profile_lock": profile_lock,
        "enforcement": enforcement_mode(project),
        "hook_trust_notice": hook_trust_notice(project),
        "versions": version_info(project),
        "phase": phase,
        "lifecycle": lifecycle_contract,
        "scope_hash": scope,
        "blueprint": {
            **blueprint_state,
            "sha256": blueprint_state.get("current_sha256"),
        },
        "plan": {
            "path": PLAN_FILE,
            "task_count": len(tasks),
            "counts": task_counts(tasks),
            "ready": [task["id"] for task in ready_tasks(tasks)],
            "parse_problem": parse_problem,
        },
        "proof": proof,
        "drift": drift,
        "foundation": foundation_gate,
        "delivery": delivery_gate,
        "review_policy": adaptive_policy.to_dict(),
        "review": review_summary_source_hash_unavailable(project, scope, profile_lock, problems=[hash_problem] if hash_problem else None) if source_hash_blocked else review_summary(project, scope),
        "spawn_plan": spawn_plan(project, tasks, phase),
        "source_hash_unavailable": source_hash_blocked,
        "source_hash_problems": [hash_problem] if hash_problem else [],
        "learnings_digest": learnings_digest(project),
        "required_next_action": next_action,
    }
    state["operating_card"] = operating_card(project, state)
    return state


def source_hash_unavailable_state(profile_lock: dict[str, Any], *, problems: Sequence[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "detected": False,
        "source_changed": None,
        "scope_changed": False,
        "changed_files": [],
        "source_hash_unavailable": True,
        "problems": list(problems) if problems is not None else profile_lock.get("problems") or [],
    }


def review_summary_source_hash_unavailable(project: Path, scope: str, profile_lock: dict[str, Any], *, problems: Sequence[dict[str, Any]] | None = None) -> dict[str, Any]:
    merged = load_merged_review(project, scope)
    base = {
        "source_hash_unavailable": True,
        "problems": list(problems) if problems is not None else profile_lock.get("problems") or [],
    }
    if not merged:
        return base | {"recorded": False, "open_findings": None, "reviewer_count": 0}
    return base | {
        "recorded": True,
        "fresh": False,
        "reviewer_count": merged.get("reviewer_count", 0),
        "open_findings": len(merged.get("fix_queue") or []),
        "waived": len(merged.get("waived") or []),
    }


def review_summary(project: Path, scope: str) -> dict[str, Any]:
    merged = load_merged_review(project, scope)
    if not merged:
        return {"recorded": False, "open_findings": None, "reviewer_count": 0}
    current, hash_problem = try_source_hash(project)
    if hash_problem or current is None:
        return review_summary_source_hash_unavailable(project, scope, fast_mvp_profile_lock_state(project), problems=[hash_problem] if hash_problem else None)
    return {
        "recorded": True,
        "fresh": merged.get("source_hash") == current,
        "reviewer_count": merged.get("reviewer_count", 0),
        "open_findings": len(merged.get("fix_queue") or []),
        "waived": len(merged.get("waived") or []),
    }


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
    appended = append_plan_task(
        plan_path,
        {
            "task": task_id,
            "description": "Post-completion amendment: re-verify and review the drifted files.",
            "status": "ready",
            "mode": "solo",
            "files": files,
            "depends": "-",
            "verify": inherited or "set the real verification command for the amended files",
            "evidence": "-",
        },
    )
    return task_id if appended else None


def cmd_run(args: argparse.Namespace) -> int:
    raw_project = Path(args.project).resolve()
    project, blocked = resolve_isolation(raw_project, product_slug=args.product_slug or "", adopt_root=args.adopt_root)
    if blocked is not None:
        print(json.dumps(blocked, indent=2))
        return 1 if args.strict else 0
    assert project is not None
    project.mkdir(parents=True, exist_ok=True)
    requested_profile = "fast-mvp" if args.fast_mvp else (args.profile or "")
    setup_missing = (
        not is_git_repo(project)
        or not (project / BLUEPRINT_FILE).exists()
        or not (project / PLAN_FILE).exists()
        or not (project / LEDGER_FILE).exists()
    )
    if setup_missing and not args.no_auto_init:
        init_args = argparse.Namespace(
            project=str(project),
            force=False,
            no_agents=bool(getattr(args, "no_agents", False)),
            no_hooks=bool(getattr(args, "no_hooks", False)),
            product_slug=args.product_slug,
            adopt_root=True,
            profile=requested_profile,
            fast_mvp=args.fast_mvp,
        )
        code = cmd_init(init_args)
        if code != 0:
            return code
    profile_for_manifest = requested_profile
    if requested_profile == "fast-mvp":
        existing_manifest_profile = project_profile(project)
        if fast_mvp_profile_selected_before_gates(project) or (
            existing_manifest_profile == "fast-mvp"
            and setup_ledger_records_fast_mvp_before_gates(project)
        ):
            profile_for_manifest = ""
    ensure_project_manifest(project, objective=args.objective or "", product_slug=args.product_slug or "", profile=profile_for_manifest)
    plan_path = project / PLAN_FILE
    if blueprint_is_approved(project) and plan_path.exists():
        try:
            for task in ready_tasks(parse_tasks(plan_path)):
                if task.get("status") == "queued":
                    update_plan_task_row(plan_path, task["id"], {"status": "ready"})
        except ForgeError:
            pass
    # Post-done drift: scaffold an amendment task so the loop re-enters cleanly.
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
    if (
        not source_hash_blocked
        and drift.get("detected")
        and proof
        and plan_path.exists()
        and blueprint_has_valid_lock(project)
        and not completed_amendment_covering_drift(project, drift_tasks, drift)
    ):
        amend_id = scaffold_amend(project, drift)
        if amend_id:
            append_jsonl(project / INCIDENTS_FILE, {"schema": "star-forge.incident.v1", "timestamp": now_utc(), "kind": "post-done-drift", "amend_task": amend_id, "changed_files": drift.get("changed_files")})
    payload = canonical_state_payload(project, objective=args.objective or "", mode=args.mode, fast_mvp=review_profile(project) == "fast-mvp")
    if getattr(args, "no_hooks", False):
        payload["hook_trust_notice"] = {
            **(payload.get("hook_trust_notice") or {}),
            "show": False,
        }
        payload["operating_card"] = operating_card(project, payload)
    ensure_state_dirs(project)
    previous_phase: str | None = None
    state_path = project / CANONICAL_STATE
    if state_path.exists():
        try:
            previous_phase = str(read_json(state_path).get("phase") or "") or None
        except Exception:
            previous_phase = None
    write_json_stable(state_path, payload)
    if payload["phase"] != previous_phase:
        append_jsonl(project / LEDGER_FILE, {"schema": "star-forge.ledger.v1", "timestamp": now_utc(), "event": "state-machine", "summary": f"phase={payload['phase']}", "artifacts": [str(CANONICAL_STATE)]})
    print(payload["operating_card"])
    print(json.dumps(payload, indent=2))
    if (payload.get("hook_trust_notice") or {}).get("show"):
        mark_hook_trust_notice_seen(project)
    profile_lock_status = str((payload.get("profile_lock") or {}).get("status") or "")
    strict_blocked = payload["phase"] == "blocked" or profile_lock_status in {"blocked", "standard-required"} or bool(payload.get("source_hash_unavailable"))
    return 1 if args.strict and strict_blocked else 0


def cmd_status(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    plan_path = project / PLAN_FILE
    tasks = parse_tasks(plan_path) if plan_path.exists() else []
    profile_lock = fast_mvp_profile_lock_state(project)
    blueprint_state = blueprint_lock_state(project)
    hash_problem = source_hash_unavailable_problem(project)
    scope = scope_hash(project) or "noscope"
    payload = {
        "schema": "star-forge.status.v1",
        "project": str(project),
        "versions": version_info(project),
        "enforcement": enforcement_mode(project),
        "hooks_live": hooks_liveness(project),
        "blueprint_approved": blueprint_is_approved(project),
        "blueprint": blueprint_state,
        "plan_exists": plan_path.exists(),
        "task_count": len(tasks),
        "counts": task_counts(tasks),
        "ready": [task["id"] for task in ready_tasks(tasks)],
        "review": review_summary_source_hash_unavailable(project, scope, profile_lock, problems=[hash_problem] if hash_problem else None) if hash_problem else review_summary(project, scope),
        "profile_lock": profile_lock,
        "source_hash_unavailable": bool(hash_problem),
        "git_status": git_status(project),
        "canonical_state": str(project / CANONICAL_STATE) if (project / CANONICAL_STATE).exists() else None,
    }
    print(json.dumps(payload, indent=2))
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
    payload = {
        "schema": "star-forge.blueprint-approval.v1",
        "project": str(project),
        "blueprint": BLUEPRINT_FILE,
        "lock": project_contracts.BLUEPRINT_LOCK_FILE,
        "blueprint_sha256": lock["blueprint_sha256"],
        "approved_at": lock["approved_at"],
        "contract_version": lock["contract_version"],
        "previous_status": before.get("status"),
        "status": state.get("status"),
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Inspect Codex installation state without mutating it."""
    codex_home = Path(
        args.codex_home
        or os.environ.get("CODEX_HOME")
        or (Path.home() / ".codex")
    )
    source_root = Path(args.source_root) if args.source_root else plugin_root()
    active_root = Path(args.active_plugin_root) if args.active_plugin_root else None
    payload = installation_doctor.diagnose_installation(
        codex_home=codex_home,
        source_root=source_root,
        runtime_version=SF_VERSION,
        active_plugin_root=active_root,
    )
    print(json.dumps(payload, indent=2))
    return installation_doctor.doctor_exit_code(payload, strict=args.strict)


def cmd_validate_plan(args: argparse.Namespace) -> int:
    raw = Path(args.file)
    project = resolve_project(args.project)
    if raw.is_absolute():
        plan_path = raw.resolve()
    else:
        project_path = project / raw
        if project_path.exists() or str(args.file) == str(PLAN_FILE):
            plan_path = project_path
        elif raw.exists():
            plan_path = raw.resolve()
        else:
            plan_path = project_path
    tasks = parse_tasks(plan_path)
    problems = validate_tasks(tasks)
    problems.extend(validate_project_plan_contract(project, tasks))
    if not tasks:
        problems.append({"severity": "critical", "task": None, "line": 0, "message": plan_parse_problem(plan_path, tasks) or "Plan.md contains no parseable tasks"})
    blocking = [item for item in problems if item["severity"] in BLOCKING_SEVERITIES]
    mode = plan_contract_mode(tasks)
    if mode in {"v2", "mixed"}:
        traceability = "strict-v2"
    elif mode == "legacy":
        traceability = "legacy-readable"
    else:
        traceability = "compatible-readable"
    payload = {
        "schema": "star-forge.plan-validate.v1",
        "verdict": "PASS" if not blocking else "REQUEST_CHANGES",
        "plan_version": mode,
        "traceability": traceability,
        "task_count": len(tasks),
        "ready": [task["id"] for task in ready_tasks(tasks)],
        "problems": problems,
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["verdict"] == "PASS" or not args.strict else 1


def cmd_migrate_plan(args: argparse.Namespace) -> int:
    """Create a separate Plan v2 draft from an eight-column legacy Plan."""
    project = resolve_project(args.project)
    source_raw = Path(args.file)
    output_raw = Path(args.output)
    source = (
        source_raw.resolve()
        if source_raw.is_absolute()
        else (project / source_raw).resolve()
    )
    output = (
        output_raw.resolve()
        if output_raw.is_absolute()
        else (project / output_raw).resolve()
    )
    try:
        payload = project_contracts.write_plan_v2_migration(source, output)
    except project_contracts.ContractError as exc:
        raise ForgeError(str(exc)) from exc
    print(json.dumps(payload | {"project": str(project)}, indent=2))
    return 0


# ---------------------------------------------------------------------- hooks


def load_hook_event() -> dict[str, Any]:
    # Hooks must never crash on hostile/garbled stdin: UnicodeDecodeError is a
    # ValueError, OSError covers closed/odd stdin. A crashing hook can block a
    # Codex tool call, which an observation-only layer must never do.
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def hook_project(event: dict[str, Any]) -> Path | None:
    raw = Path(str(event.get("cwd") or os.getcwd())).resolve()
    # The event cwd may not exist (Path.resolve never raises, but run_git would).
    if not raw.is_dir():
        return None
    found = find_star_forge_project_root(raw)
    if found:
        return found
    try:
        root = repo_root(raw)
    except OSError:
        return None
    return root if has_star_forge_project_markers(root) else None


def hook_output(event_name: str, *, context: str | None = None, system_message: str | None = None, **extra: Any) -> int:
    payload: dict[str, Any] = {}
    if context:
        payload["hookSpecificOutput"] = {"hookEventName": event_name, "additionalContext": context}
    if system_message:
        payload["systemMessage"] = system_message
    payload.update(extra)
    if payload:
        print(json.dumps(redact(payload)))
    return 0


def extract_event_paths(event: dict[str, Any], project: Path) -> list[str]:
    tool_input = event.get("tool_input", {})
    rels: list[str] = []
    if isinstance(tool_input, dict):
        for key in ("file_path", "path"):
            raw = tool_input.get(key)
            if isinstance(raw, str) and raw:
                rels.append(raw)
        command = str(tool_input.get("command", ""))
        for match in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", command, re.MULTILINE):
            rels.append(match.group(1).strip())
    out: list[str] = []
    seen: set[str] = set()
    for raw in rels:
        path = Path(raw)
        candidate = path if path.is_absolute() else project / path
        rel = relative_to_project(candidate, project)
        if rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


def state_banner(project: Path) -> str | None:
    """One-line operating banner re-injected on every prompt (compaction-proof)."""
    state_path = project / CANONICAL_STATE
    if not state_path.exists():
        return None
    try:
        state = read_json(state_path)
    except Exception:
        return None
    phase = state.get("phase")
    if phase in {None, "done"}:
        return None
    enforcement = "witnessed" if enforcement_mode(project) == "witnessed" else "advisory"
    ready = state.get("plan", {}).get("ready") if isinstance(state.get("plan"), dict) else None
    return f"[star-forge] phase={phase} enforcement={enforcement} ready={ready} | next: {state.get('required_next_action')}"


def reanchor_text(project: Path) -> str:
    """Full re-anchor for SessionStart/PreCompact: regenerate the operating card."""
    try:
        state = read_json(project / CANONICAL_STATE) if (project / CANONICAL_STATE).exists() else {}
    except Exception:
        state = {}
    card = state.get("operating_card")
    if card:
        return "Star Forge continuity — start this turn by running the state helper:\n" + str(card)
    return "Star Forge continuity: run `python3 <plugin-root>/scripts/star_forge.py run --project .` to recompute phase and the operating card before continuing."


def cmd_hook(args: argparse.Namespace) -> int:
    """PreToolUse: observe only. No denial — the session proved blocking trains evasion."""
    event = load_hook_event()
    if not event:
        return 0
    project = hook_project(event)
    if project is None:
        return 0
    ensure_state_dirs(project)
    payload = {"schema": "star-forge.hook-event.v1", "timestamp": now_utc(), "event": str(event.get("hook_event_name", "PreToolUse")), "tool": event.get("tool_name")}
    append_jsonl(project / HOOK_EVENTS, payload)
    return 0


def cmd_post_hook(args: argparse.Namespace) -> int:
    """PostToolUse: log the changed-file trail for the freshness/liveness view. No blocking."""
    event = load_hook_event()
    project = hook_project(event)
    if project is None:
        return 0
    ensure_state_dirs(project)
    for rel in extract_event_paths(event, project):
        append_jsonl(project / CHANGED_FILES, {"schema": "star-forge.changed-file.v1", "timestamp": now_utc(), "file": rel, "tool": event.get("tool_name"), "session_id": event.get("session_id")})
    return 0


def cmd_prompt_hook(args: argparse.Namespace) -> int:
    """UserPromptSubmit: reset the auto-continue budget and inject the state banner."""
    event = load_hook_event()
    project = hook_project(event)
    if project is None:
        return 0
    counter_path = project / AUTO_CONTINUE_FILE
    if counter_path.exists():
        try:
            counter_path.unlink()
        except OSError:
            pass
    banner = state_banner(project)
    if banner:
        return hook_output("UserPromptSubmit", context=banner)
    return 0


def build_handoff(project: Path, source: str) -> dict[str, Any]:
    try:
        state = read_json(project / CANONICAL_STATE) if (project / CANONICAL_STATE).exists() else {}
    except Exception:
        state = {}
    done = done_payload(project)
    return {
        "schema": "star-forge.handoff.v1",
        "created_at": now_utc(),
        "source": source,
        "project": str(project),
        "phase": state.get("phase"),
        "complete": done.get("is_complete"),
        "verdict": done.get("verdict"),
        "next_action": state.get("required_next_action"),
        "operating_card": state.get("operating_card"),
    }


def release_session_state(project: Path) -> None:
    # No leases in v0.3; kept as a hook seam for symmetry.
    return None


def cmd_session_start_hook(args: argparse.Namespace) -> int:
    event = load_hook_event()
    project = hook_project(event)
    if project is None:
        return 0
    ensure_state_dirs(project)
    payload = {"schema": "star-forge.hook-event.v1", "timestamp": now_utc(), "event": "SessionStart", "source": event.get("source")}
    append_jsonl(project / HOOK_EVENTS, payload)
    incidents = unprocessed_incident_note(project)
    context = reanchor_text(project)
    if incidents:
        context = context + "\n" + incidents
    return hook_output("SessionStart", context=context)


def unprocessed_incident_note(project: Path) -> str | None:
    path = project / INCIDENTS_FILE
    if not path.exists():
        return None
    try:
        count = len([line for line in read_text(path).splitlines() if line.strip()])
    except OSError:
        return None
    if count:
        return f"Star Forge: {count} incident(s) recorded (waived findings, drift, contradictions). Between projects, run `learn` to convert recurring ones into durable learnings."
    return None


def should_block_stop(project: Path, event: dict[str, Any], handoff: dict[str, Any]) -> str | None:
    """Bounded Cruise keep-going. Momentum only — never a correctness gate."""
    if event.get("stop_hook_active"):
        return None
    try:
        state = read_json(project / CANONICAL_STATE) if (project / CANONICAL_STATE).exists() else {}
    except Exception:
        state = {}
    if str(state.get("mode") or "") != "cruise":
        return None
    phase = str(state.get("phase") or "")
    if phase not in {
        "intake",
        "design",
        "plan",
        "foundation",
        "build",
        "review",
        "deliver",
        "amend",
    }:
        return None
    signature = stable_json_hash({"phase": phase, "next": handoff.get("next_action")})
    counter: dict[str, Any] = {}
    counter_path = project / AUTO_CONTINUE_FILE
    if counter_path.exists():
        try:
            counter = read_json(counter_path)
        except Exception:
            counter = {}
    count = int(counter.get("count") or 0) if counter.get("signature") == signature else 0
    if count >= MAX_AUTO_CONTINUES:
        return None
    write_json(counter_path, {"schema": "star-forge.auto-continue.v1", "count": count + 1, "phase": phase, "signature": signature, "updated_at": now_utc()})
    return f"Star Forge: phase `{phase}` is not complete. Continue with: {state.get('required_next_action')}"


def cmd_stop_hook(args: argparse.Namespace) -> int:
    event = load_hook_event()
    project = hook_project(event)
    if project is None:
        return 0
    ensure_state_dirs(project)
    payload = build_handoff(project, str(event.get("hook_event_name", "Stop")))
    write_json_if_changed(project / HANDOFF_ARTIFACT, payload)
    # Contradiction detector: a model claim of completion that the predicate denies.
    claim_complete = bool(event.get("summary", {}).get("complete")) if isinstance(event.get("summary"), dict) else False
    if claim_complete and not payload.get("complete"):
        append_jsonl(project / INCIDENTS_FILE, {"schema": "star-forge.incident.v1", "timestamp": now_utc(), "kind": "completion-contradiction", "verdict": payload.get("verdict")})
        return hook_output("Stop", system_message=f"Star Forge: a completion claim contradicts the computed predicate ({payload.get('verdict')}). Run `done --strict` before telling the user it is complete.")
    block = should_block_stop(project, event, payload)
    if block:
        print(json.dumps({"decision": "block", "reason": block}))
        return 0
    return hook_output("Stop", system_message=f"Star Forge saved continuity state: {project / HANDOFF_ARTIFACT}")


def record_subagent_event(event: dict[str, Any], event_name: str) -> int:
    project = hook_project(event)
    if project is None:
        return 0
    ensure_state_dirs(project)
    payload = {
        "schema": "star-forge.subagent-event.v1",
        "timestamp": now_utc(),
        "event": event_name,
        "agent_id": event.get("agent_id"),
        "agent_type": event.get("agent_type"),
        "session_id": event.get("session_id"),
        "parent_session_id": event.get("parent_session_id") or event.get("parent_thread_id"),
    }
    append_jsonl(project / SUBAGENT_EVENTS, payload)
    return 0


def cmd_subagent_start_hook(args: argparse.Namespace) -> int:
    return record_subagent_event(load_hook_event(), "SubagentStart")


def cmd_subagent_stop_hook(args: argparse.Namespace) -> int:
    return record_subagent_event(load_hook_event(), "SubagentStop")


def cmd_pre_compact_hook(args: argparse.Namespace) -> int:
    event = load_hook_event()
    project = hook_project(event)
    if project is None:
        return 0
    ensure_state_dirs(project)
    write_json_if_changed(project / HANDOFF_ARTIFACT, build_handoff(project, "PreCompact"))
    return hook_output("PreCompact", context=reanchor_text(project), system_message="Star Forge prepared continuity context for compaction.")


# ------------------------------------------------------------------ self-test


def cmd_self_test(args: argparse.Namespace) -> int:
    root = plugin_root()
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    def run_check(name: str, command: Sequence[str], *, env: dict[str, str] | None = None) -> None:
        proc = subprocess.run(command, cwd=str(root), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        detail = (proc.stdout + proc.stderr).strip()[-1200:]
        check(name, proc.returncode == 0, detail)

    try:
        manifest = read_json(root / ".codex-plugin" / "plugin.json")
        check("manifest-json", manifest.get("name") == PLUGIN_NAME, "plugin.json parsed")
        manifest_version = str(manifest.get("version") or "")
        check("manifest-version", version_core(manifest_version) == version_core(SF_VERSION), f"manifest={manifest_version} script={SF_VERSION}")
    except Exception as exc:
        check("manifest-json", False, str(exc))
    for skill in ["forge", "forge-plan", "forge-work", "forge-review"]:
        path = root / "skills" / skill / "SKILL.md"
        check(f"skill-{skill}", path.exists() and "description:" in read_text(path), str(path))
    for role in ["builder", "reviewer"]:
        path = root / "agents" / role / "agent.md"
        check(f"agent-{role}", path.exists() and "## Mission" in read_text(path), str(path))
    for template in ["Blueprint.md", "Plan.md"]:
        check(f"template-{template}", (root / "templates" / template).exists(), template)
    for command in [
        "run", "init", "approve-blueprint", "verify", "browser-run",
        "preview-proof", "proof-run",
        "native-ios-proof", "native-macos-proof", "security-proof",
        "security-handoff-packet", "source-packet-proof",
        "source-packet-github-pr-review", "server-lease", "review", "waive",
        "complete-task", "done", "learn", "agents-install", "validate-plan",
        "status",
    ]:
        parser = build_parser()
        subcommands: set[str] = set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                subcommands.update(action.choices.keys())
        check(f"command-{command}", command in subcommands, command)
    try:
        json.loads(read_text(root / "hooks" / "hooks.json"))
        check("hooks-json", True, "hooks/hooks.json parsed")
    except Exception as exc:
        check("hooks-json", False, str(exc))
    if args.strict:
        with tempfile.TemporaryDirectory(prefix="star-forge-pycache-") as pycache:
            env = dict(os.environ)
            env["PYTHONPYCACHEPREFIX"] = pycache
            run_check("py-compile", [sys.executable, "-m", "py_compile", str(root / "scripts" / "star_forge.py")], env=env)
        test_path = root / "tests" / "test_star_forge.py"
        if test_path.exists():
            run_check("unit-tests", [sys.executable, str(test_path)])
        quality_paths = list(iter_project_files(root, all_files=True))
        quality_findings = [*scan_paths(quality_paths, root), *architecture_debt_findings(quality_paths, root)]
        quality_blocking = blocking_items(quality_findings)
        check("quality-gate-all-strict", not quality_blocking, f"blocking={len(quality_blocking)} scanned_files={len(quality_paths)}")
        with tempfile.TemporaryDirectory(prefix="star-forge-smoke-") as tmp:
            smoke = Path(tmp) / "project"
            script = str(root / "scripts" / "star_forge.py")
            run_check("cli-smoke-init", [sys.executable, script, "init", "--project", str(smoke), "--no-agents"])
            run_check("cli-smoke-run", [sys.executable, script, "run", "--project", str(smoke), "--objective", "smoke", "--no-hooks"])
            run_check("cli-smoke-verify", [sys.executable, script, "verify", "--project", str(smoke), "--task", "SF-SMOKE", "--noop", "--summary", "smoke", "--strict"])
            run_check("cli-smoke-status", [sys.executable, script, "status", "--project", str(smoke)])
            run_check("cli-smoke-done-readonly", [sys.executable, script, "done", "--project", str(smoke)])
    ok = all(item["ok"] for item in checks)
    print(json.dumps({"schema": "star-forge.self-test.v1", "verdict": "PASS" if ok else "FAIL", "checks": checks}, indent=2))
    return 0 if ok or not args.strict else 1


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Star Forge deterministic helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="Run the Star Forge Forge-Loop state machine")
    p.add_argument("--project", default=".")
    p.add_argument("--objective", default="")
    p.add_argument("--mode", default="cruise", choices=["cruise", "sync"])
    p.add_argument("--fast-mvp", action="store_true")
    p.add_argument("--profile", default="", choices=["", "standard", "fast-mvp"])
    p.add_argument("--product-slug", default="")
    p.add_argument("--adopt-root", action="store_true", help="Deliberately build in an existing foreign project root (recorded in the manifest)")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--no-auto-init", action="store_true")
    p.add_argument("--no-hooks", action="store_true", help="Suppress optional hook trust prompts for this run")
    p.add_argument("--no-agents", action="store_true", help="Do not generate project-local agent profiles during auto-init")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("init", help="Initialize Star Forge artifacts")
    p.add_argument("--project", default=".")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-agents", action="store_true")
    p.add_argument("--no-hooks", action="store_true", help="Compatibility flag; init does not install project-local hooks")
    p.add_argument("--product-slug", default="")
    p.add_argument("--adopt-root", action="store_true")
    p.add_argument("--fast-mvp", action="store_true")
    p.add_argument("--profile", default="", choices=["", "standard", "fast-mvp"])
    p.set_defaults(func=cmd_init)

    p = sub.add_parser(
        "approve-blueprint",
        help="Write a content lock after explicit user approval of Blueprint.md",
    )
    p.add_argument("--project", default=".")
    p.set_defaults(func=cmd_approve_blueprint)

    p = sub.add_parser("validate-plan", help="Validate Plan.md")
    p.add_argument("--file", default=PLAN_FILE)
    p.add_argument("--project", default=".")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_validate_plan)

    p = sub.add_parser(
        "migrate-plan",
        help="Create a separate reviewable Plan v2 draft from a legacy Plan",
    )
    p.add_argument("--project", default=".")
    p.add_argument("--file", default=PLAN_FILE)
    p.add_argument(
        "--output",
        required=True,
        help="New draft path; the legacy Plan is never overwritten",
    )
    p.set_defaults(func=cmd_migrate_plan)

    p = sub.add_parser("status", help="Read-only Star Forge state (no mutation)")
    p.add_argument("--project", default=".")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="Read-only Codex installation diagnostics")
    p.add_argument("--codex-home", default="")
    p.add_argument("--source-root", "--plugin-root", dest="source_root", default="")
    p.add_argument("--active-plugin-root", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("verify", help="Run and record a Star Forge-owned verification command")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--command", default="")
    p.add_argument("--summary", default="")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--noop", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("browser-run", help="Record a deterministic browser scenario with viewport/interaction/console evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--url", default="")
    p.add_argument("--scenario", required=True)
    p.add_argument("--viewport", action="append", help="NAME=WIDTHxHEIGHT:SCREENSHOT or NAME=SCREENSHOT")
    p.add_argument("--screenshot", action="append")
    p.add_argument("--interaction-evidence", action="append")
    p.add_argument("--console-evidence", action="append")
    p.add_argument("--live-manifest", default="")
    p.add_argument("--summary", default="")
    p.add_argument("--require-viewports", action="store_true", default=True)
    p.add_argument("--no-require-viewports", action="store_false", dest="require_viewports")
    p.add_argument("--require-interaction", action="store_true", default=True)
    p.add_argument("--no-require-interaction", action="store_false", dest="require_interaction")
    p.add_argument("--require-console", action="store_true", default=True)
    p.add_argument("--no-require-console", action="store_false", dest="require_console")
    p.add_argument("--server-lease", nargs="?", const=str(SERVER_LEASE), default="")
    p.add_argument("--require-server-lease", action="store_true")
    p.add_argument("--degraded", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_browser_run)

    p = sub.add_parser("preview-proof", help="Validate and record provider-neutral preview proof evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--url", default="")
    p.add_argument("--expect-status", type=int, default=200)
    p.add_argument("--deployment-metadata", default="")
    p.add_argument("--smoke-checks", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_preview_proof)

    p = sub.add_parser("proof-run", help="Validate and record a generic live proof profile artifact")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--artifact", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_proof_run)

    p = sub.add_parser("native-ios-proof", help="Validate and record native iOS proof evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--scheme", default="")
    p.add_argument("--simulator", default="")
    p.add_argument("--build-result", default="")
    p.add_argument("--launch-result", default="")
    p.add_argument("--test-result", default="")
    p.add_argument("--screenshot", default="")
    p.add_argument("--ui-snapshot", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_native_ios_proof)

    p = sub.add_parser("native-macos-proof", help="Validate and record native macOS proof evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--app-name", default="")
    p.add_argument("--bundle-id", default="")
    p.add_argument("--build-result", default="")
    p.add_argument("--run-result", default="")
    p.add_argument("--test-result", default="")
    p.add_argument("--screenshot", default="")
    p.add_argument("--app-bundle", default="")
    p.add_argument("--signing-note", default="")
    p.add_argument("--packaging-note", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_native_macos_proof)

    p = sub.add_parser("security-handoff-packet", help="Validate and record a security scanner handoff packet")
    p.add_argument("--project", default=".")
    p.add_argument("--kind", default="")
    p.add_argument("--input", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_security_handoff_packet)

    p = sub.add_parser("security-proof", help="Validate and record security proof evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--scanner", default="")
    p.add_argument("--scanner-version", default="")
    p.add_argument("--findings", default="")
    p.add_argument("--artifact", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_security_proof)

    p = sub.add_parser("source-packet-proof", help="Validate and record source packet proof evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--input", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_source_packet_proof)

    p = sub.add_parser("source-packet-github-pr-review", help="Validate and record read-only GitHub PR source packet evidence")
    p.add_argument("--project", default=".")
    p.add_argument("--input", default="")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_source_packet_github_pr_review)

    p = sub.add_parser("server-lease", help="Claim, release, or inspect the local dev-server lease")
    p.add_argument("--project", default=".")
    p.add_argument("--action", choices=["claim", "release", "status"], default="claim")
    p.add_argument("--port", type=int)
    p.add_argument("--base-url", default="")
    p.add_argument("--command", default="")
    p.add_argument("--owner", default="star-forge")
    p.add_argument("--pid", type=int)
    p.set_defaults(func=cmd_server_lease)

    p = sub.add_parser("review", help="Merge reviewer findings + tree scan into the fix queue")
    p.add_argument("--project", default=".")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("waive", help="Waive a review finding with a recorded reason")
    p.add_argument("--project", default=".")
    p.add_argument("--finding", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_waive)

    p = sub.add_parser("complete-task", help="Mark one Plan.md task complete after proof checks pass")
    p.add_argument("--project", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--changed-file", action="append")
    p.add_argument("--summary", default="")
    p.set_defaults(func=cmd_complete_task)

    p = sub.add_parser("done", help="Compute the completion predicate from git facts and record proof")
    p.add_argument("--project", default=".")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--write-summary", action="store_true")
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("learn", help="Write a durable learning to ~/.star-forge/learnings")
    p.add_argument("--title", required=True)
    p.add_argument("--rule", required=True)
    p.add_argument("--trigger", action="append")
    p.add_argument("--category", default="general")
    p.add_argument("--detail", default="")
    p.add_argument("--source", default="manual")
    p.set_defaults(func=cmd_learn)

    p = sub.add_parser("agents-install", help="Install Star Forge roles as native Codex agents (.codex/agents/*.toml)")
    p.add_argument("--project", default=".")
    p.set_defaults(func=cmd_agents_install)

    p = sub.add_parser("self-test", help="Validate the Star Forge plugin package")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_self_test)

    for name, func in [
        ("hook", cmd_hook),
        ("post-hook", cmd_post_hook),
        ("prompt-hook", cmd_prompt_hook),
        ("session-start-hook", cmd_session_start_hook),
        ("subagent-start-hook", cmd_subagent_start_hook),
        ("subagent-stop-hook", cmd_subagent_stop_hook),
        ("stop-hook", cmd_stop_hook),
        ("pre-compact-hook", cmd_pre_compact_hook),
    ]:
        p = sub.add_parser(name, help=f"Codex {name} handler")
        p.set_defaults(func=func)
    return parser


HOOK_COMMANDS = {
    "hook", "post-hook", "prompt-hook", "session-start-hook",
    "subagent-start-hook", "subagent-stop-hook", "stop-hook", "pre-compact-hook",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    is_hook = str(getattr(args, "command", "")) in HOOK_COMMANDS
    try:
        return int(args.func(args))
    except ForgeError as exc:
        if is_hook:
            # An observation-only hook must never block a tool call on its own bug.
            return 0
        print(json.dumps({"schema": "star-forge.error.v1", "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    except Exception:
        if is_hook:
            return 0
        raise


if __name__ == "__main__":
    raise SystemExit(main())
