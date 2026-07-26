"""Safe, opt-in storage for cross-project Star Forge learnings.

Learning records are local data, not instructions.  This module deliberately
accepts a small scalar schema, hashes every record, and returns only validated
abstract rules.  Callers must never execute text obtained from this store.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Mapping, Sequence

from live_collectors import common as live_common


LEARNING_SCHEMA = "star-forge.learning.v2"
DIGEST_SCHEMA = "star-forge.learnings-digest.v2"
OPT_IN_ENV = "STAR_FORGE_GLOBAL_LEARNINGS"
HOME_ENV = "STAR_FORGE_LEARNINGS_HOME"
DEFAULT_RELATIVE_HOME = Path(".star-forge") / "learnings"
MAX_RECORD_BYTES = 16_384
MAX_RECORDS_SCANNED = 200
MAX_DIGEST_LIMIT = 20
MAX_AGE_DAYS = 730
MAX_FUTURE_SKEW_SECONDS = 300
MAX_TITLE_CHARS = 120
MAX_RULE_CHARS = 800
MAX_DETAIL_CHARS = 1_200
MAX_TRIGGER_COUNT = 16
MAX_TRIGGER_CHARS = 40
MAX_PRODUCER_CHARS = 64

ALLOWED_CATEGORIES = frozenset(
    {
        "accessibility",
        "architecture",
        "general",
        "performance",
        "privacy",
        "process",
        "reliability",
        "security",
        "testing",
        "verification",
    }
)
ALLOWED_CONFIDENCE = frozenset({"low", "medium", "high"})
ALLOWED_ORIGINS = frozenset({"manual", "incident", "review", "verification"})
ALLOWED_PRODUCERS = frozenset({"star-forge-cli"})
TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
SAFE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_TRIGGER_RE = re.compile(r"^[a-z0-9][a-z0-9.+#_-]{0,39}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
HOME_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:/Users/[^/\s]+|/home/[^/\s]+|[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s]+)"
)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
RELATIVE_PATH_RE = re.compile(r"(?<![\w.-])(?:\.\.?/|[A-Za-z0-9_.-]+/){2,}[A-Za-z0-9_.-]+")
PROMPT_INJECTION_PATTERNS = (
    re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\b(?:system|developer|assistant)\s*(?:message|prompt)?\s*:", re.IGNORECASE),
    re.compile(r"\b(?:reveal|print|exfiltrate|leak)\b.{0,40}\b(?:secret|token|credential|prompt)\b", re.IGNORECASE),
    re.compile(r"\b(?:act|behave|respond)\s+as\b", re.IGNORECASE),
    re.compile(r"<\s*(?:script|system|developer|assistant|tool)\b", re.IGNORECASE),
    re.compile(r"\b(?:tool_call|function_call|BEGIN\s+INSTRUCTIONS?)\b", re.IGNORECASE),
)
EXECUTABLE_TEXT_PATTERNS = (
    re.compile(r"```"),
    re.compile(r"(?:^|\s)(?:sudo|curl|wget|eval|exec)\s+", re.IGNORECASE),
    re.compile(r"(?:&&|\|\||;\s*(?:rm|sh|bash|zsh|python|node)\b|\$\()"),
)
FRONTMATTER_FIELDS = (
    "schema",
    "id",
    "title",
    "category",
    "triggers",
    "rule",
    "detail",
    "source-project-id",
    "source-project-slug",
    "source-hash",
    "timestamp",
    "producer",
    "confidence",
    "provenance",
    "record-hash",
)


class LearningsError(ValueError):
    """A learning request or record violates the safety contract."""


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _slug(raw: str, *, fallback: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(raw or "").lower()).strip("-")
    return (value[:64].strip("-") or fallback)


def _scalar(raw: Any, *, name: str, maximum: int, required: bool = True) -> str:
    if not isinstance(raw, str):
        raise LearningsError(f"{name} must be a string")
    value = raw.strip()
    if required and not value:
        raise LearningsError(f"{name} is required")
    if len(value) > maximum:
        raise LearningsError(f"{name} exceeds {maximum} characters")
    if "\n" in value or "\r" in value or CONTROL_RE.search(value):
        raise LearningsError(f"{name} contains control characters or line breaks")
    return value


def _looks_private_host(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    if normalized in {"localhost"} or normalized.endswith(
        (".local", ".internal", ".localhost")
    ):
        return True
    try:
        import ipaddress

        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return bool(
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_reserved
    )


def _redact_urls(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        suffix = ""
        while raw and raw[-1] in ".,);]}":
            suffix = raw[-1] + suffix
            raw = raw[:-1]
        try:
            parsed = urllib.parse.urlsplit(raw)
        except ValueError:
            return "[REDACTED_URL]" + suffix
        if not parsed.hostname or _looks_private_host(parsed.hostname):
            return "[REDACTED_PRIVATE_URL]" + suffix
        netloc = parsed.hostname
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        query = ""
        if parsed.query:
            query = urllib.parse.urlencode(
                [
                    ("redacted-key", "[REDACTED_QUERY]")
                    for _key, _value in urllib.parse.parse_qsl(
                        parsed.query, keep_blank_values=True
                    )
                ]
            )
        return urllib.parse.urlunsplit(
            (parsed.scheme, netloc, parsed.path, query, "")
        ) + suffix

    return URL_RE.sub(replace, text)


def redact_text(raw: str, *, project: Path | None = None) -> str:
    """Redact secrets and project-specific identifiers before persistence."""

    text = _redact_urls(raw)
    cleaned, _report = live_common.redact_sensitive_values(text)
    text = str(cleaned)
    text = HOME_PATH_RE.sub("[REDACTED_HOME]", text)
    text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    if project is not None:
        project_text = str(project.resolve())
        if project_text:
            text = text.replace(project_text, "[REDACTED_PROJECT]")
    return text


def _validate_abstract_text(value: str, *, field: str) -> None:
    for pattern in PROMPT_INJECTION_PATTERNS:
        if pattern.search(value):
            raise LearningsError(f"{field} contains a prompt-injection pattern")
    for pattern in EXECUTABLE_TEXT_PATTERNS:
        if pattern.search(value):
            raise LearningsError(f"{field} contains executable instruction text")
    if RELATIVE_PATH_RE.search(value):
        raise LearningsError(f"{field} contains project-specific path content")


def _normalize_triggers(raw: Sequence[str]) -> tuple[str, ...]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise LearningsError("triggers must be a sequence")
    values: list[str] = []
    for item in raw:
        value = _scalar(
            item, name="trigger", maximum=MAX_TRIGGER_CHARS
        ).lower()
        if not SAFE_TRIGGER_RE.fullmatch(value):
            raise LearningsError(f"unsafe trigger: {value!r}")
        if value not in values:
            values.append(value)
    if not values:
        raise LearningsError("at least one trigger is required")
    if len(values) > MAX_TRIGGER_COUNT:
        raise LearningsError(
            f"triggers exceeds {MAX_TRIGGER_COUNT} entries"
        )
    return tuple(sorted(values))


def learnings_home(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = str(env.get(HOME_ENV) or "").strip()
    return Path(override).expanduser() if override else Path.home() / DEFAULT_RELATIVE_HOME


def _manifest_opt_in(project: Path | None, action: str) -> bool:
    if project is None:
        return False
    path = project / ".starforge" / "project.json"
    try:
        if path.is_symlink() or path.stat().st_size > MAX_RECORD_BYTES:
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    settings = payload.get("global_learnings") if isinstance(payload, dict) else None
    if settings is True:
        return True
    return bool(
        isinstance(settings, dict)
        and settings.get("enabled") is True
        and settings.get(action) is True
    )


def opt_in_status(
    project: Path | None,
    *,
    action: str,
    explicit: bool = False,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if action not in {"read", "write"}:
        raise LearningsError("action must be read or write")
    env = os.environ if environ is None else environ
    reason = "disabled"
    if explicit:
        reason = "explicit-action"
    elif str(env.get(OPT_IN_ENV) or "").strip().lower() in TRUTHY:
        reason = "user-environment"
    elif str(env.get(HOME_ENV) or "").strip():
        # A non-default store is an explicit user configuration and is how all
        # tests isolate themselves from the real user learnings directory.
        reason = "configured-store"
    elif _manifest_opt_in(project, action):
        reason = "project-manifest"
    return {
        "enabled": reason != "disabled",
        "action": action,
        "reason": reason,
    }


def _path_has_symlink_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(str(path)))
    current = Path(absolute.anchor)
    safe_system_aliases = {
        Path("/etc"): Path("/private/etc"),
        Path("/tmp"): Path("/private/tmp"),
        Path("/var"): Path("/private/var"),
    }
    for part in absolute.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                expected = safe_system_aliases.get(current)
                if expected is not None and current.resolve() == expected:
                    current = expected
                    continue
                return True
        except OSError:
            return True
    return False


def _safe_root(
    project: Path | None,
    *,
    action: str,
    explicit: bool,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    status = opt_in_status(
        project, action=action, explicit=explicit, environ=environ
    )
    if not status["enabled"]:
        return None, status
    root = learnings_home(environ)
    if _path_has_symlink_component(root):
        return None, {**status, "enabled": False, "reason": "unsafe-store-symlink"}
    try:
        resolved = root.resolve(strict=False)
    except OSError:
        return None, {**status, "enabled": False, "reason": "unsafe-store-path"}
    if resolved == Path(resolved.anchor):
        return None, {**status, "enabled": False, "reason": "unsafe-store-root"}
    return resolved, status


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _reject_symlink_chain(root: Path, candidate: Path) -> None:
    if not _within(root, candidate):
        raise LearningsError("learning path escapes the configured store")
    current = candidate
    while current != root:
        if current.exists() and current.is_symlink():
            raise LearningsError("learning path contains a symlink")
        current = current.parent
    if root.exists() and root.is_symlink():
        raise LearningsError("learning store must not be a symlink")


def project_identity(project: Path) -> dict[str, str]:
    resolved = project.resolve()
    manifest_path = resolved / ".starforge" / "project.json"
    manifest: dict[str, Any] = {}
    try:
        if not manifest_path.is_symlink() and manifest_path.stat().st_size <= MAX_RECORD_BYTES:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
    except (OSError, ValueError, TypeError):
        pass
    identifier = str(manifest.get("project_id") or "")
    if not SAFE_ID_RE.fullmatch(identifier):
        identifier = _stable_hash(
            {"kind": "local-project", "root": str(resolved)}
        )[:16]
    slug = _slug(
        str(manifest.get("product_slug") or resolved.name),
        fallback="project",
    )
    return {"id": identifier, "slug": slug}


def _record_payload(
    *,
    title: str,
    rule: str,
    detail: str,
    triggers: Sequence[str],
    category: str,
    confidence: str,
    source_project: Mapping[str, str],
    source_hash: str,
    timestamp: str,
    producer: str,
    origin: str,
    opt_in_reason: str,
) -> dict[str, Any]:
    base = {
        "schema": LEARNING_SCHEMA,
        "id": "",
        "title": title,
        "category": category,
        "triggers": list(triggers),
        "rule": rule,
        "detail": detail,
        "source_project": {
            "id": source_project["id"],
            "slug": source_project["slug"],
        },
        "source_hash": source_hash,
        "timestamp": timestamp,
        "producer": producer,
        "confidence": confidence,
        "provenance": {
            "kind": "local-project",
            "origin": origin,
            "opt_in": opt_in_reason,
            "trusted": True,
        },
    }
    base["id"] = _stable_hash(base)[:24]
    return base


def _frontmatter_value(value: str) -> str:
    if "\n" in value or "\r" in value or CONTROL_RE.search(value):
        raise LearningsError("record contains an unsafe scalar")
    return value


def _serialize_record(record: Mapping[str, Any]) -> str:
    provenance = json.dumps(
        record["provenance"], sort_keys=True, separators=(",", ":")
    )
    canonical = dict(record)
    record_hash = _stable_hash(canonical)
    values = {
        "schema": str(record["schema"]),
        "id": str(record["id"]),
        "title": str(record["title"]),
        "category": str(record["category"]),
        "triggers": ", ".join(record["triggers"]),
        "rule": str(record["rule"]),
        "detail": str(record["detail"]),
        "source-project-id": str(record["source_project"]["id"]),
        "source-project-slug": str(record["source_project"]["slug"]),
        "source-hash": str(record["source_hash"]),
        "timestamp": str(record["timestamp"]),
        "producer": str(record["producer"]),
        "confidence": str(record["confidence"]),
        "provenance": provenance,
        "record-hash": record_hash,
    }
    lines = ["---"]
    lines.extend(
        f"{field}: {_frontmatter_value(values[field])}"
        for field in FRONTMATTER_FIELDS
    )
    lines.extend(["---", ""])
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > MAX_RECORD_BYTES:
        raise LearningsError("serialized learning exceeds the record size limit")
    return text


def _parse_timestamp(raw: str, *, now: dt.datetime) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LearningsError("timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise LearningsError("timestamp must include a timezone")
    parsed = parsed.astimezone(dt.timezone.utc)
    if parsed > now + dt.timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        raise LearningsError("record timestamp is in the future")
    if parsed < now - dt.timedelta(days=MAX_AGE_DAYS):
        raise LearningsError("record is stale")
    return parsed


def _parse_record(text: str, *, now: dt.datetime) -> dict[str, Any]:
    if len(text.encode("utf-8")) > MAX_RECORD_BYTES:
        raise LearningsError("record exceeds the size limit")
    if CONTROL_RE.search(text):
        raise LearningsError("record contains control characters")
    lines = text.splitlines()
    if len(lines) != len(FRONTMATTER_FIELDS) + 2 or lines[0] != "---" or lines[-1] != "---":
        raise LearningsError("record must contain only bounded frontmatter")
    raw: dict[str, str] = {}
    for line in lines[1:-1]:
        if ":" not in line:
            raise LearningsError("record field is malformed")
        key, value = line.split(":", 1)
        key = key.strip()
        if key in raw:
            raise LearningsError("record contains a duplicate field")
        raw[key] = value.strip()
    if tuple(raw.keys()) != FRONTMATTER_FIELDS:
        raise LearningsError("record fields or field order do not match the schema")
    if raw["schema"] != LEARNING_SCHEMA:
        raise LearningsError("record schema is untrusted")
    try:
        provenance = json.loads(raw["provenance"])
    except (TypeError, ValueError) as exc:
        raise LearningsError("record provenance is invalid") from exc
    if not isinstance(provenance, dict) or set(provenance) != {
        "kind",
        "opt_in",
        "origin",
        "trusted",
    }:
        raise LearningsError("record provenance schema is untrusted")
    triggers = tuple(
        item.strip() for item in raw["triggers"].split(",") if item.strip()
    )
    record = {
        "schema": raw["schema"],
        "id": raw["id"],
        "title": raw["title"],
        "category": raw["category"],
        "triggers": list(triggers),
        "rule": raw["rule"],
        "detail": raw["detail"],
        "source_project": {
            "id": raw["source-project-id"],
            "slug": raw["source-project-slug"],
        },
        "source_hash": raw["source-hash"],
        "timestamp": raw["timestamp"],
        "producer": raw["producer"],
        "confidence": raw["confidence"],
        "provenance": provenance,
    }
    if raw["record-hash"] != _stable_hash(record):
        raise LearningsError("record hash does not match its content")
    _validate_record(record, now=now)
    return record


def _validate_record(record: Mapping[str, Any], *, now: dt.datetime) -> None:
    if not SAFE_ID_RE.fullmatch(str(record.get("id") or "")):
        raise LearningsError("record id is invalid")
    title = _scalar(
        record.get("title"), name="title", maximum=MAX_TITLE_CHARS
    )
    rule = _scalar(record.get("rule"), name="rule", maximum=MAX_RULE_CHARS)
    detail = _scalar(
        record.get("detail"),
        name="detail",
        maximum=MAX_DETAIL_CHARS,
        required=False,
    )
    _validate_abstract_text(title, field="title")
    _validate_abstract_text(rule, field="rule")
    _validate_abstract_text(detail, field="detail")
    for field, value in (("title", title), ("rule", rule), ("detail", detail)):
        if redact_text(value) != value:
            raise LearningsError(
                f"record {field} contains unredacted sensitive content"
            )
    _normalize_triggers(record.get("triggers") or [])
    if record.get("category") not in ALLOWED_CATEGORIES:
        raise LearningsError("record category is not allowed")
    if record.get("confidence") not in ALLOWED_CONFIDENCE:
        raise LearningsError("record confidence is not allowed")
    if record.get("producer") not in ALLOWED_PRODUCERS:
        raise LearningsError("record producer is untrusted")
    source_project = record.get("source_project")
    if not isinstance(source_project, dict) or set(source_project) != {"id", "slug"}:
        raise LearningsError("source project identity is invalid")
    if not SAFE_ID_RE.fullmatch(str(source_project.get("id") or "")):
        raise LearningsError("source project id is invalid")
    if not SAFE_ID_RE.fullmatch(str(source_project.get("slug") or "")):
        raise LearningsError("source project slug is invalid")
    if not SAFE_HASH_RE.fullmatch(str(record.get("source_hash") or "")):
        raise LearningsError("source hash is invalid")
    _parse_timestamp(str(record.get("timestamp") or ""), now=now)
    provenance = record.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("kind") != "local-project"
        or provenance.get("origin") not in ALLOWED_ORIGINS
        or provenance.get("trusted") is not True
        or provenance.get("opt_in")
        not in {
            "explicit-action",
            "user-environment",
            "configured-store",
            "project-manifest",
        }
    ):
        raise LearningsError("record provenance is untrusted")


def write_learning(
    project: Path,
    *,
    title: str,
    rule: str,
    triggers: Sequence[str],
    category: str = "general",
    detail: str = "",
    confidence: str = "medium",
    origin: str = "manual",
    producer: str = "star-forge-cli",
    source_hash: str,
    timestamp: str | None = None,
    explicit_opt_in: bool = False,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    root, opt_in = _safe_root(
        project,
        action="write",
        explicit=explicit_opt_in,
        environ=environ,
    )
    if root is None:
        raise LearningsError(
            f"global learnings write is disabled ({opt_in['reason']})"
        )
    normalized_title = _scalar(
        redact_text(title, project=project),
        name="title",
        maximum=MAX_TITLE_CHARS,
    )
    normalized_rule = _scalar(
        redact_text(rule, project=project),
        name="rule",
        maximum=MAX_RULE_CHARS,
    )
    normalized_detail = _scalar(
        redact_text(detail, project=project),
        name="detail",
        maximum=MAX_DETAIL_CHARS,
        required=False,
    )
    for field, value in (
        ("title", normalized_title),
        ("rule", normalized_rule),
        ("detail", normalized_detail),
    ):
        _validate_abstract_text(value, field=field)
    normalized_triggers = _normalize_triggers(triggers)
    normalized_category = _scalar(
        category, name="category", maximum=32
    ).lower()
    if normalized_category not in ALLOWED_CATEGORIES:
        raise LearningsError(
            "category must be one of: " + ", ".join(sorted(ALLOWED_CATEGORIES))
        )
    normalized_confidence = _scalar(
        confidence, name="confidence", maximum=16
    ).lower()
    if normalized_confidence not in ALLOWED_CONFIDENCE:
        raise LearningsError("confidence must be low, medium, or high")
    normalized_origin = _scalar(origin, name="origin", maximum=32).lower()
    if normalized_origin not in ALLOWED_ORIGINS:
        raise LearningsError(
            "origin must be one of: " + ", ".join(sorted(ALLOWED_ORIGINS))
        )
    normalized_producer = _scalar(
        producer, name="producer", maximum=MAX_PRODUCER_CHARS
    ).lower()
    if normalized_producer not in ALLOWED_PRODUCERS:
        raise LearningsError("producer is not trusted")
    normalized_hash = _scalar(
        source_hash, name="source_hash", maximum=64
    ).lower()
    if not SAFE_HASH_RE.fullmatch(normalized_hash):
        raise LearningsError("source_hash must be a SHA-256 digest")
    created_at = timestamp or dt.datetime.now(dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    _parse_timestamp(created_at, now=dt.datetime.now(dt.timezone.utc))
    record = _record_payload(
        title=normalized_title,
        rule=normalized_rule,
        detail=normalized_detail,
        triggers=normalized_triggers,
        category=normalized_category,
        confidence=normalized_confidence,
        source_project=project_identity(project),
        source_hash=normalized_hash,
        timestamp=created_at,
        producer=normalized_producer,
        origin=normalized_origin,
        opt_in_reason=str(opt_in["reason"]),
    )
    serialized = _serialize_record(record)
    category_dir = root / normalized_category
    path = category_dir / f"{_slug(normalized_title, fallback='learning')}.md"
    _reject_symlink_chain(root, category_dir)
    _reject_symlink_chain(root, path)
    root.mkdir(parents=True, exist_ok=True)
    _reject_symlink_chain(root, category_dir)
    category_dir.mkdir(parents=True, exist_ok=True)
    _reject_symlink_chain(root, path)
    if path.exists():
        if path.is_symlink():
            raise LearningsError("refusing to overwrite a symlink")
        existing = path.read_text(encoding="utf-8")
        if existing == serialized:
            return {
                "record": record,
                "path": str(path),
                "storage_relative_path": path.relative_to(root).as_posix(),
                "opt_in": opt_in,
                "created": False,
            }
        raise LearningsError("learning id collision")
    fd, temporary_name = tempfile.mkstemp(
        prefix=".learning-", suffix=".tmp", dir=str(category_dir)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        _reject_symlink_chain(root, path)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "record": record,
        "path": str(path),
        "storage_relative_path": path.relative_to(root).as_posix(),
        "opt_in": opt_in,
        "created": True,
    }


def _keyword_terms(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_+#.-]+", text)
        if 1 < len(token) <= MAX_TRIGGER_CHARS
    }


def project_keywords(
    project: Path, *, candidate_names: Sequence[str] = ()
) -> set[str]:
    terms: set[str] = set()
    blueprint = project / "Blueprint.md"
    try:
        if not blueprint.is_symlink() and blueprint.stat().st_size <= 256_000:
            terms.update(_keyword_terms(blueprint.read_text(encoding="utf-8")))
    except OSError:
        pass
    for raw in candidate_names:
        path = Path(raw)
        terms.add(path.name.lower())
        if path.suffix:
            terms.add(path.suffix.lower().lstrip("."))
    if not candidate_names:
        try:
            for path in sorted(project.iterdir(), key=lambda item: item.name)[:100]:
                terms.add(path.name.lower())
                if path.suffix:
                    terms.add(path.suffix.lower().lstrip("."))
        except OSError:
            pass
    return {term for term in terms if term}


def read_digest(
    project: Path,
    *,
    keywords: set[str],
    limit: int = 5,
    explicit_opt_in: bool = False,
    environ: Mapping[str, str] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    bounded_limit = max(0, min(int(limit), MAX_DIGEST_LIMIT))
    root, opt_in = _safe_root(
        project,
        action="read",
        explicit=explicit_opt_in,
        environ=environ,
    )
    report: dict[str, Any] = {
        "schema": DIGEST_SCHEMA,
        "enabled": bool(root is not None),
        "opt_in": opt_in,
        "limit": bounded_limit,
        "records_scanned": 0,
        "records_accepted": 0,
        "records_rejected": 0,
        "rejection_reasons": {},
        "items": [],
    }
    if root is None or bounded_limit == 0 or not root.exists():
        return report
    if not root.is_dir():
        report["enabled"] = False
        report["opt_in"] = {**opt_in, "enabled": False, "reason": "store-not-directory"}
        return report
    current_time = now or dt.datetime.now(dt.timezone.utc)
    candidates: list[Path] = []
    try:
        for path in sorted(root.rglob("*.md"), key=lambda item: item.as_posix()):
            if len(candidates) >= MAX_RECORDS_SCANNED:
                break
            candidates.append(path)
    except OSError:
        return report
    scored: list[tuple[int, str, str, dict[str, Any]]] = []
    for path in candidates:
        report["records_scanned"] += 1
        try:
            _reject_symlink_chain(root, path)
            if path.is_symlink() or not path.is_file():
                raise LearningsError("record path is unsafe")
            if path.stat().st_size > MAX_RECORD_BYTES:
                raise LearningsError("record exceeds the size limit")
            record = _parse_record(path.read_text(encoding="utf-8"), now=current_time)
        except (LearningsError, OSError, UnicodeError) as exc:
            reason = str(exc) or "invalid record"
            report["records_rejected"] += 1
            reasons = report["rejection_reasons"]
            reasons[reason] = int(reasons.get(reason) or 0) + 1
            continue
        report["records_accepted"] += 1
        matched = sorted(set(record["triggers"]) & {item.lower() for item in keywords})
        if not matched:
            continue
        score = len(matched)
        item = {
            "id": record["id"],
            "title": record["title"],
            "rule": record["rule"],
            "detail": record["detail"],
            "category": record["category"],
            "confidence": record["confidence"],
            "triggers": record["triggers"],
            "matched_triggers": matched,
            "score": score,
            "timestamp": record["timestamp"],
            "producer": record["producer"],
            "source_project": record["source_project"],
            "source_hash": record["source_hash"],
            "provenance": record["provenance"],
            "storage_relative_path": path.relative_to(root).as_posix(),
            "untrusted_data": True,
        }
        scored.append(
            (
                score,
                str(record["title"]).casefold(),
                str(record["id"]),
                item,
            )
        )
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    report["items"] = [item for _score, _title, _id, item in scored[:bounded_limit]]
    return report


__all__ = [
    "ALLOWED_CATEGORIES",
    "ALLOWED_CONFIDENCE",
    "ALLOWED_ORIGINS",
    "DIGEST_SCHEMA",
    "HOME_ENV",
    "LEARNING_SCHEMA",
    "LearningsError",
    "MAX_RECORD_BYTES",
    "OPT_IN_ENV",
    "learnings_home",
    "opt_in_status",
    "project_identity",
    "project_keywords",
    "read_digest",
    "redact_text",
    "write_learning",
]
