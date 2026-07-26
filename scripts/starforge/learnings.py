"""Safe, opt-in storage for cross-project Star Forge learnings.

Learning records are local data, not instructions.  This module deliberately
accepts a small scalar schema, hashes every record, and returns only validated
abstract rules.  Callers must never execute text obtained from this store.
"""

from __future__ import annotations
from .policy_data import mapping as policy_mapping, value as _policy_value

import datetime as dt
import hashlib
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Mapping, Sequence

from live_collectors import common as live_common
from . import safe_io
LEARNING_POLICY = _policy_value("learnings.POLICY")
globals().update(LEARNING_POLICY["exports"])
DEFAULT_RELATIVE_HOME = Path(".star-forge") / "learnings"
globals().update(LEARNING_POLICY["constants"])
ALLOWED_CATEGORIES = _policy_value('learnings.ALLOWED_CATEGORIES')
globals().update({name: frozenset(LEARNING_POLICY[key]) for name, key in LEARNING_POLICY["set_exports"].items()})
for _regex_name, (_pattern, _ignore_case) in LEARNING_POLICY["regexes"].items():
    globals()[_regex_name] = re.compile(_pattern, re.IGNORECASE if _ignore_case else 0)
PROMPT_INJECTION_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in LEARNING_POLICY["prompt_injection_patterns"])
EXECUTABLE_TEXT_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in LEARNING_POLICY["executable_text_patterns"])
FRONTMATTER_FIELDS = _policy_value('learnings.FRONTMATTER_FIELDS')

class LearningsError(ValueError):
    """A learning request or record violates the safety contract."""

def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
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
    if normalized in {"localhost"} or normalized.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        import ipaddress
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return bool(parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_reserved)

def _redact_urls(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        trimmed = raw.rstrip(".,);]}")
        suffix, raw = raw[len(trimmed):], trimmed
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
            query = urllib.parse.urlencode([("redacted-key", "[REDACTED_QUERY]") for _key, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)])
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, query, "")) + suffix
    return URL_RE.sub(replace, text)

def redact_text(raw: str, *, project: Path | None = None) -> str:
    """Redact secrets and project-specific identifiers before persistence."""
    text = _redact_urls(raw)
    cleaned, _report = live_common.redact_sensitive_values(text)
    text = str(cleaned)
    text = HOME_PATH_RE.sub("[REDACTED_HOME]", text)
    text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    if project is not None and (project_text := str(project.resolve())):
        text = text.replace(project_text, "[REDACTED_PROJECT]")
    return text

def _validate_abstract_text(value: str, *, field: str) -> None:
    pattern_groups = ((PROMPT_INJECTION_PATTERNS, "prompt-injection pattern"), (EXECUTABLE_TEXT_PATTERNS, "executable instruction text"))
    for patterns, message in pattern_groups:
        if any(pattern.search(value) for pattern in patterns):
            raise LearningsError(f"{field} contains a {message}")
    if RELATIVE_PATH_RE.search(value):
        raise LearningsError(f"{field} contains project-specific path content")

def _abstract_fields(values: Mapping[str, Any], *, project: Path | None = None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for field, descriptor in LEARNING_POLICY["text_fields"].items():
        raw = redact_text(values.get(field), project=project) if project is not None else values.get(field)
        normalized[field] = _scalar(raw, name=field, maximum=descriptor["maximum"], required=descriptor["required"])
        _validate_abstract_text(normalized[field], field=field)
    return normalized

def _enum_value(raw: Any, field: str, *, record: bool = False) -> str:
    descriptor = LEARNING_POLICY["enum_fields"][field]
    value = str(raw or "") if record else _scalar(raw, name=field, maximum=descriptor["maximum"]).lower()
    if value not in descriptor["allowed"]:
        raise LearningsError(descriptor["record_error" if record else "write_error"])
    return value

def _normalize_triggers(raw: Sequence[str]) -> tuple[str, ...]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise LearningsError("triggers must be a sequence")
    values = list(dict.fromkeys(_scalar(item, name="trigger", maximum=MAX_TRIGGER_CHARS).lower() for item in raw))
    unsafe = next((value for value in values if not SAFE_TRIGGER_RE.fullmatch(value)), None)
    if unsafe:
        raise LearningsError(f"unsafe trigger: {unsafe!r}")
    if not values:
        raise LearningsError("at least one trigger is required")
    if len(values) > MAX_TRIGGER_COUNT:
        raise LearningsError(f"triggers exceeds {MAX_TRIGGER_COUNT} entries")
    return tuple(sorted(values))

def learnings_home(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = str(env.get(HOME_ENV) or "").strip()
    return Path(override).expanduser() if override else Path.home() / DEFAULT_RELATIVE_HOME

def _bounded_json(path: Path, *, root: Path | None = None) -> dict[str, Any]:
    try:
        content, _digest, _size = safe_io.read_snapshot(
            root or safe_io.infer_root(path), path, max_bytes=MAX_RECORD_BYTES)
        payload = json.loads(content.decode("utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}

def opt_in_status(
    project: Path | None,
    *,
    action: str,
    explicit: bool = False,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if action not in LEARNING_POLICY["allowed_actions"]:
        raise LearningsError("action must be read or write")
    env = os.environ if environ is None else environ
    reasons = (
        (explicit, "explicit-action"),
        (str(env.get(OPT_IN_ENV) or "").strip().lower() in TRUTHY, "user-environment"),
        (bool(str(env.get(HOME_ENV) or "").strip()), "configured-store"),
    )
    reason = next((value for enabled, value in reasons if enabled), "disabled")
    return policy_mapping("learning_opt_in", enabled=reason != "disabled", action=action, reason=reason)

def _path_has_symlink_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(str(path)))
    current = Path(absolute.anchor)
    safe_system_aliases = {Path(alias): Path(target) for alias, target in LEARNING_POLICY["safe_system_aliases"].items()}
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
    status = opt_in_status(project, action=action, explicit=explicit, environ=environ)
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

def project_identity(project: Path) -> dict[str, str]:
    resolved = project.resolve()
    manifest = _bounded_json(resolved / ".starforge" / "project.json", root=resolved)
    identifier = str(manifest.get("project_id") or "")
    if not SAFE_ID_RE.fullmatch(identifier):
        identifier = _stable_hash({"kind": "local-project", "root": str(resolved)})[:16]
    slug = _slug(str(manifest.get("product_slug") or resolved.name), fallback="project")
    return policy_mapping("learning_source_project", id=identifier, slug=slug)

def _record_payload(*, source_project: Mapping[str, str], origin: str, opt_in_reason: str, **fields: Any) -> dict[str, Any]:
    fields["triggers"] = list(fields["triggers"])
    base = policy_mapping(
        "learning_record", **fields,
        source_project=policy_mapping("learning_source_project", id=source_project["id"], slug=source_project["slug"]),
        provenance=policy_mapping("learning_provenance", origin=origin, opt_in=opt_in_reason),
    )
    return base | {"id": _stable_hash(base)[:24]}

def _frontmatter_value(value: str) -> str:
    if "\n" in value or "\r" in value or CONTROL_RE.search(value):
        raise LearningsError("record contains an unsafe scalar")
    return value

def _serialize_record(record: Mapping[str, Any]) -> str:
    provenance = json.dumps(record["provenance"], sort_keys=True, separators=(",", ":"))
    record_hash = _stable_hash(dict(record))
    values = {output: str(record[source]) for output, source in LEARNING_POLICY["frontmatter_record_fields"].items()} | {
        "triggers": ", ".join(record["triggers"]),
        "source-project-id": str(record["source_project"]["id"]),
        "source-project-slug": str(record["source_project"]["slug"]),
        "provenance": provenance,
        "record-hash": record_hash,
    }
    lines = ["---"]
    lines.extend(f"{field}: {_frontmatter_value(values[field])}" for field in FRONTMATTER_FIELDS)
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
    if not isinstance(provenance, dict) or set(provenance) != set(LEARNING_POLICY["provenance_fields"]):
        raise LearningsError("record provenance schema is untrusted")
    record = policy_mapping(
        "learning_record", schema=raw["schema"], id=raw["id"], title=raw["title"], category=raw["category"],
        triggers=[item.strip() for item in raw["triggers"].split(",") if item.strip()], rule=raw["rule"], detail=raw["detail"],
        source_project=policy_mapping("learning_source_project", id=raw["source-project-id"], slug=raw["source-project-slug"]),
        source_hash=raw["source-hash"], timestamp=raw["timestamp"], producer=raw["producer"],
        confidence=raw["confidence"], provenance=provenance,
    )
    if raw["record-hash"] != _stable_hash(record):
        raise LearningsError("record hash does not match its content")
    _validate_record(record, now=now)
    return record

def _validate_record(record: Mapping[str, Any], *, now: dt.datetime) -> None:
    if not SAFE_ID_RE.fullmatch(str(record.get("id") or "")):
        raise LearningsError("record id is invalid")
    abstract = _abstract_fields(record)
    for field, value in abstract.items():
        if redact_text(value) != value:
            raise LearningsError(f"record {field} contains unredacted sensitive content")
    _normalize_triggers(record.get("triggers") or [])
    for field in ("category", "confidence", "producer"):
        _enum_value(record.get(field), field, record=True)
    source_project = record.get("source_project")
    if not isinstance(source_project, dict) or set(source_project) != set(LEARNING_POLICY["source_project_fields"]):
        raise LearningsError("source project identity is invalid")
    for field in LEARNING_POLICY["source_project_fields"]:
        if not SAFE_ID_RE.fullmatch(str(source_project.get(field) or "")):
            raise LearningsError(f"source project {field} is invalid")
    if not SAFE_HASH_RE.fullmatch(str(record.get("source_hash") or "")):
        raise LearningsError("source hash is invalid")
    _parse_timestamp(str(record.get("timestamp") or ""), now=now)
    provenance = record.get("provenance")
    if (not isinstance(provenance, dict) or provenance.get("kind") != "local-project" or provenance.get("origin") not in ALLOWED_ORIGINS or provenance.get("trusted") is not True or
            provenance.get("opt_in") not in LEARNING_POLICY["opt_in_reasons"]):
        raise LearningsError("record provenance is untrusted")

def _write_result(record: dict[str, Any], path: Path, root: Path, opt_in: dict[str, Any], *, created: bool) -> dict[str, Any]:
    return policy_mapping(
        "learning_write_result", record=record, path=str(path),
        storage_relative_path=path.relative_to(root).as_posix(), opt_in=opt_in, created=created,
    )

def _record_relative(root: Path, path: Path) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise LearningsError("record path escapes the configured store") from exc
    if (len(relative.parts) != 2 or relative.parts[0] not in ALLOWED_CATEGORIES
            or relative.suffix != ".md"):
        raise LearningsError("record path does not match the learning layout")
    return relative

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
        raise LearningsError(f"global learnings write is disabled ({opt_in['reason']})")
    normalized_text = _abstract_fields({"title": title, "rule": rule, "detail": detail}, project=project)
    normalized_triggers = _normalize_triggers(triggers)
    normalized_category = _enum_value(category, "category")
    normalized_confidence = _enum_value(confidence, "confidence")
    normalized_origin = _enum_value(origin, "origin")
    normalized_producer = _enum_value(producer, "producer")
    normalized_hash = _scalar(source_hash, name="source_hash", maximum=64).lower()
    if not SAFE_HASH_RE.fullmatch(normalized_hash):
        raise LearningsError("source_hash must be a SHA-256 digest")
    created_at = timestamp or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    _parse_timestamp(created_at, now=dt.datetime.now(dt.timezone.utc))
    record = _record_payload(
        **normalized_text,
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
    path = category_dir / f"{_slug(normalized_text['title'], fallback='learning')}.md"
    io_root = safe_io.infer_root(root)
    try:
        existing, _digest, _size = safe_io.read_snapshot(
            io_root, path, max_bytes=MAX_RECORD_BYTES)
    except FileNotFoundError:
        try:
            safe_io.create_text_exclusive(io_root, path, serialized)
        except OSError as exc:
            raise LearningsError(f"learning path cannot be created safely: {exc}") from exc
    except OSError as exc:
        raise LearningsError(f"learning path cannot be read safely: {exc}") from exc
    else:
        if existing == serialized.encode("utf-8"):
            return _write_result(record, path, root, opt_in, created=False)
        raise LearningsError("learning id collision")
    return _write_result(record, path, root, opt_in, created=True)

def _keyword_terms(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[A-Za-z0-9_+#.-]+", text) if 1 < len(token) <= MAX_TRIGGER_CHARS}

def _path_keywords(path: Path) -> set[str]:
    return {path.name.lower(), path.suffix.lower().lstrip(".")} - {""}

def project_keywords(project: Path, *, candidate_names: Sequence[str] = ()) -> set[str]:
    terms: set[str] = set()
    blueprint = project / "Blueprint.md"
    try:
        if not blueprint.is_symlink() and blueprint.stat().st_size <= 256_000:
            terms.update(_keyword_terms(blueprint.read_text(encoding="utf-8")))
    except OSError:
        pass
    for raw in candidate_names:
        terms.update(_path_keywords(Path(raw)))
    if not candidate_names:
        try:
            for path in sorted(project.iterdir(), key=lambda item: item.name)[:100]:
                terms.update(_path_keywords(path))
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
    report = policy_mapping("learning_digest", enabled=bool(root is not None), opt_in=opt_in, limit=bounded_limit)
    if root is None or bounded_limit == 0:
        return report
    io_root = safe_io.infer_root(root)
    try:
        store_exists = safe_io.directory_exists(io_root, root)
    except OSError:
        report.update(enabled=False, opt_in={**opt_in, "enabled": False, "reason": "store-not-directory"})
        return report
    if not store_exists:
        return report
    current_time = now or dt.datetime.now(dt.timezone.utc)
    try:
        candidates = sorted(root.rglob("*.md"), key=lambda item: item.as_posix())[:MAX_RECORDS_SCANNED]
    except OSError:
        return report
    scored: list[tuple[int, str, str, dict[str, Any]]] = []
    for path in candidates:
        report["records_scanned"] += 1
        try:
            relative = _record_relative(root, path)
            content, _digest, _size = safe_io.read_snapshot(
                io_root, path, max_bytes=MAX_RECORD_BYTES)
            record = _parse_record(content.decode("utf-8"), now=current_time)
        except (LearningsError, OSError, UnicodeError) as exc:
            reason = str(exc) or "invalid record"
            report["records_rejected"] += 1
            report["rejection_reasons"][reason] = int(report["rejection_reasons"].get(reason) or 0) + 1
            continue
        report["records_accepted"] += 1
        matched = sorted(set(record["triggers"]) & {item.lower() for item in keywords})
        if not matched:
            continue
        item = policy_mapping(
            "learning_digest_item", **{field: record[field] for field in ("id", "title", "rule", "detail", "category", "confidence", "triggers", "timestamp", "producer", "source_project", "source_hash", "provenance")},
            matched_triggers=matched, score=(score := len(matched)), storage_relative_path=relative.as_posix(),
        )
        scored.append((score, str(record["title"]).casefold(), str(record["id"]), item))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    report["items"] = [item for _score, _title, _id, item in scored[:bounded_limit]]
    return report
