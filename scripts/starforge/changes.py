"""Isolated v0.4 change packets and read-only amendment history."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .contracts import parse_plan_tasks_text


CHANGE_ROOT = Path(".starforge") / "changes"
CHANGE_FILE = "change.md"
CHANGE_PLAN_FILE = "Plan.md"
CHANGE_EVIDENCE_DIR = "evidence"
CHANGE_REVIEW_DIR = "review"
CHANGE_SCHEMA = "star-forge.change-packet.v1"
CHANGE_APPROVAL_STATES = frozenset({"draft", "approved"})
CHANGE_TEMPLATE_FILE = "Change.md"
CHANGE_PLAN_TEMPLATE_FILE = "ChangePlan.md"

_CHANGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AC_ID_RE = re.compile(r"AC-[1-9][0-9]*")
_FIELD_RE = re.compile(
    r"^\s*-\s+\*\*(?P<name>[^*]+)\*\*:\s*(?P<value>.*?)\s*$",
    re.MULTILINE,
)
_SECTION_RE = re.compile(r"^\s*##\s+(?P<title>.+?)\s*$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
_LEGACY_AMEND_RE = re.compile(r"^AMEND-(?P<number>[1-9][0-9]*)$", re.IGNORECASE)


class ChangePacketError(ValueError):
    """A change packet could not be read or safely updated."""


def _now_utc() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _valid_iso8601(value: str) -> bool:
    try:
        parsed = dt.datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_change_id(change_id: str) -> str:
    """Return a safe packet identifier or raise ``ChangePacketError``."""
    if not isinstance(change_id, str) or not _CHANGE_ID_RE.fullmatch(change_id):
        raise ChangePacketError(
            "change_id must be 1 to 80 safe ASCII letters, digits, dots, "
            "underscores, or hyphens, and must start with a letter or digit"
        )
    if change_id in {".", ".."} or ".." in change_id:
        raise ChangePacketError("change_id must not contain '..'")
    return change_id


def _packet_root(project: Path, change_id: str) -> Path:
    root = project.resolve()
    validated = validate_change_id(change_id)
    packet = root / CHANGE_ROOT / validated
    try:
        packet.relative_to(root)
    except ValueError as exc:
        raise ChangePacketError("change packet escapes the project") from exc
    return packet


def _lstat_problem(path: Path, *, kind: str) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"{path} cannot be inspected: {exc}"
    if stat.S_ISLNK(mode):
        return f"{path} must not be a symlink"
    if kind == "directory" and not stat.S_ISDIR(mode):
        return f"{path} must be a directory"
    if kind == "file" and not stat.S_ISREG(mode):
        return f"{path} must be a regular file"
    return ""


def _validate_packet_relative_path(raw_path: str, *, label: str) -> str:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ChangePacketError(f"{label} must be a non-empty packet-relative path")
    if "\\" in raw_path:
        raise ChangePacketError(f"{label} must use '/' separators")
    candidate = PurePosixPath(raw_path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ChangePacketError(
            f"{label} must be a normalized path inside the change packet"
        )
    normalized = candidate.as_posix()
    if normalized != raw_path:
        raise ChangePacketError(f"{label} must be normalized")
    return normalized


def _section(text: str, title: str) -> str:
    matches = list(_SECTION_RE.finditer(text))
    wanted = title.casefold()
    for index, match in enumerate(matches):
        if match.group("title").strip().casefold() != wanted:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        return text[start:end].strip()
    return ""


def _fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in _FIELD_RE.finditer(text):
        name = match.group("name").strip().casefold()
        if name in result:
            raise ChangePacketError(f"change.md contains duplicate field: {name}")
        result[name] = match.group("value").strip()
    return result


def _bullet_values(section: str) -> list[str]:
    values: list[str] = []
    for line in section.splitlines():
        match = re.match(r"^\s*-\s+(.+?)\s*$", line)
        if not match:
            continue
        value = match.group(1).strip()
        values.append(value)
    return values


def _clean_scope_delta(scope_delta: Sequence[str]) -> list[str]:
    if isinstance(scope_delta, (str, bytes)) or not isinstance(scope_delta, Sequence):
        raise ChangePacketError("scope_delta must be a non-empty sequence of strings")
    cleaned: list[str] = []
    for item in scope_delta:
        if not isinstance(item, str) or not item.strip():
            raise ChangePacketError("scope_delta entries must be non-empty strings")
        value = item.strip()
        if "\n" in value or "\r" in value:
            raise ChangePacketError("scope_delta entries must be single-line strings")
        if value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        raise ChangePacketError("scope_delta must not be empty")
    return cleaned


def _ac_sort_key(value: str) -> int:
    return int(value.split("-", 1)[1])


def _clean_affected_acs(affected_acs: Sequence[str]) -> list[str]:
    if isinstance(affected_acs, (str, bytes)) or not isinstance(
        affected_acs, Sequence
    ):
        raise ChangePacketError("affected_acs must be a non-empty sequence")
    cleaned: set[str] = set()
    for item in affected_acs:
        if not isinstance(item, str) or not _AC_ID_RE.fullmatch(item.strip()):
            raise ChangePacketError("affected_acs entries must use the AC-n form")
        cleaned.add(item.strip())
    if not cleaned:
        raise ChangePacketError("affected_acs must not be empty")
    return sorted(cleaned, key=_ac_sort_key)


def _validate_source_hash(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ChangePacketError(
            "original_completed_source_hash must be a lowercase SHA-256 digest"
        )
    return value


def _validate_delivery_impact(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChangePacketError("delivery_impact must be a non-empty string")
    cleaned = value.strip()
    if "\n" in cleaned or "\r" in cleaned:
        raise ChangePacketError("delivery_impact must be a single-line string")
    return cleaned


def _template_dir(template_dir: Path | None) -> Path:
    if template_dir is not None:
        return template_dir.resolve()
    return Path(__file__).resolve().parents[2] / "templates"


def _read_template(path: Path) -> str:
    problem = _lstat_problem(path, kind="file")
    if problem:
        raise ChangePacketError(problem)
    if not path.exists():
        raise ChangePacketError(f"template is missing: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ChangePacketError(f"template is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise ChangePacketError(f"template cannot be read: {path}: {exc}") from exc


def _render(template: str, values: Mapping[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    unresolved = sorted(set(_PLACEHOLDER_RE.findall(rendered)))
    if unresolved:
        raise ChangePacketError(
            "template has unresolved placeholders: " + ", ".join(unresolved)
        )
    return rendered.rstrip() + "\n"


def _markdown_bullets(values: Iterable[str]) -> str:
    return "\n".join(f"- {value}" for value in values)


def validate_change_packet(payload: Any) -> list[str]:
    """Return deterministic schema problems for a parsed change packet."""
    if not isinstance(payload, Mapping):
        return ["change packet must be a mapping"]
    problems: list[str] = []
    required = {
        "schema",
        "change_id",
        "created_at",
        "original_completed_source_hash",
        "scope_delta",
        "affected_acs",
        "delivery_impact",
        "approval_state",
        "approved_at",
        "plan_path",
        "evidence_path",
        "review_path",
    }
    missing = sorted(required - {str(key) for key in payload})
    if missing:
        problems.append("missing fields: " + ", ".join(missing))

    if payload.get("schema") != CHANGE_SCHEMA:
        problems.append(f"schema must be {CHANGE_SCHEMA}")
    try:
        validate_change_id(payload.get("change_id"))  # type: ignore[arg-type]
    except ChangePacketError as exc:
        problems.append(str(exc))
    if not isinstance(payload.get("created_at"), str) or not _valid_iso8601(
        payload.get("created_at")  # type: ignore[arg-type]
    ):
        problems.append("created_at must be an ISO-8601 timestamp with a timezone")
    try:
        _validate_source_hash(payload.get("original_completed_source_hash"))  # type: ignore[arg-type]
    except ChangePacketError as exc:
        problems.append(str(exc))
    try:
        _clean_scope_delta(payload.get("scope_delta"))  # type: ignore[arg-type]
    except ChangePacketError as exc:
        problems.append(str(exc))
    try:
        _clean_affected_acs(payload.get("affected_acs"))  # type: ignore[arg-type]
    except ChangePacketError as exc:
        problems.append(str(exc))
    try:
        _validate_delivery_impact(payload.get("delivery_impact"))  # type: ignore[arg-type]
    except ChangePacketError as exc:
        problems.append(str(exc))

    approval_state = payload.get("approval_state")
    if approval_state not in CHANGE_APPROVAL_STATES:
        problems.append("approval_state must be draft or approved")
    approved_at = payload.get("approved_at")
    if approval_state == "draft" and approved_at is not None:
        problems.append("draft packets must not have approved_at")
    if approval_state == "approved" and (
        not isinstance(approved_at, str) or not _valid_iso8601(approved_at)
    ):
        problems.append(
            "approved packets require an ISO-8601 approved_at timestamp with a timezone"
        )

    expected_paths = {
        "plan_path": CHANGE_PLAN_FILE,
        "evidence_path": CHANGE_EVIDENCE_DIR,
        "review_path": CHANGE_REVIEW_DIR,
    }
    for field, expected in expected_paths.items():
        try:
            value = _validate_packet_relative_path(
                payload.get(field),  # type: ignore[arg-type]
                label=field,
            )
            if value != expected:
                problems.append(f"{field} must be {expected}")
        except ChangePacketError as exc:
            problems.append(str(exc))
    return problems


def parse_change_text(text: str) -> dict[str, Any]:
    """Parse a v0.4 ``change.md`` document without accepting ambiguous fields."""
    if not isinstance(text, str):
        raise ChangePacketError("change.md content must be text")
    fields = _fields(text)
    scope_delta = _bullet_values(_section(text, "Scope Delta"))
    affected_acs = _bullet_values(_section(text, "Affected Acceptance Criteria"))
    approved_raw = fields.get("approved at", "-")
    payload: dict[str, Any] = {
        "schema": fields.get("schema", ""),
        "change_id": fields.get("change id", ""),
        "created_at": fields.get("created at", ""),
        "original_completed_source_hash": fields.get(
            "original completed source hash", ""
        ),
        "scope_delta": scope_delta,
        "affected_acs": affected_acs,
        "delivery_impact": fields.get("delivery impact", ""),
        "approval_state": fields.get("state", "").casefold(),
        "approved_at": None if approved_raw in {"", "-"} else approved_raw,
        "plan_path": fields.get("plan", ""),
        "evidence_path": fields.get("evidence", ""),
        "review_path": fields.get("review", ""),
    }
    problems = validate_change_packet(payload)
    if problems:
        raise ChangePacketError("; ".join(problems))
    payload["scope_delta"] = _clean_scope_delta(payload["scope_delta"])
    payload["affected_acs"] = _clean_affected_acs(payload["affected_acs"])
    return payload


def _assert_packet_contents(packet: Path, payload: Mapping[str, Any]) -> None:
    expected = (
        ("plan_path", "file"),
        ("evidence_path", "directory"),
        ("review_path", "directory"),
    )
    resolved_packet = packet.resolve()
    for field, kind in expected:
        relative = _validate_packet_relative_path(str(payload[field]), label=field)
        path = packet / relative
        problem = _lstat_problem(path, kind=kind)
        if problem:
            raise ChangePacketError(problem)
        if not path.exists():
            raise ChangePacketError(f"{field} is missing: {relative}")
        try:
            path.resolve().relative_to(resolved_packet)
        except ValueError as exc:
            raise ChangePacketError(f"{field} escapes the change packet") from exc


def read_change_packet(project: Path, change_id: str) -> dict[str, Any]:
    """Read and validate one packet, including its packet-local paths."""
    packet = _packet_root(project, change_id)
    problem = _lstat_problem(packet, kind="directory")
    if problem:
        raise ChangePacketError(problem)
    if not packet.exists():
        raise ChangePacketError(f"change packet does not exist: {change_id}")
    change_path = packet / CHANGE_FILE
    problem = _lstat_problem(change_path, kind="file")
    if problem:
        raise ChangePacketError(problem)
    if not change_path.exists():
        raise ChangePacketError(f"{CHANGE_FILE} is missing for {change_id}")
    try:
        text = change_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ChangePacketError(f"{CHANGE_FILE} is not valid UTF-8") from exc
    except OSError as exc:
        raise ChangePacketError(f"{CHANGE_FILE} cannot be read: {exc}") from exc
    payload = parse_change_text(text)
    if payload["change_id"] != change_id:
        raise ChangePacketError(
            f"packet directory {change_id} does not match change.md id "
            f"{payload['change_id']}"
        )
    _assert_packet_contents(packet, payload)
    return {
        **payload,
        "kind": "change-packet",
        "path": str(packet.relative_to(project.resolve())),
    }


def create_change_packet(
    project: Path,
    *,
    change_id: str,
    original_completed_source_hash: str,
    scope_delta: Sequence[str],
    affected_acs: Sequence[str],
    delivery_impact: str,
    created_at: str | None = None,
    template_dir: Path | None = None,
) -> dict[str, Any]:
    """Atomically create one draft packet without changing root project sources."""
    root = project.resolve()
    packet = _packet_root(root, change_id)
    source_hash = _validate_source_hash(original_completed_source_hash)
    scope = _clean_scope_delta(scope_delta)
    acs = _clean_affected_acs(affected_acs)
    impact = _validate_delivery_impact(delivery_impact)
    timestamp = created_at or _now_utc()
    if not _valid_iso8601(timestamp):
        raise ChangePacketError(
            "created_at must be an ISO-8601 timestamp with a timezone"
        )

    changes_root = root / CHANGE_ROOT
    for directory in (root / ".starforge", changes_root):
        problem = _lstat_problem(directory, kind="directory")
        if problem:
            raise ChangePacketError(problem)
    if packet.exists() or packet.is_symlink():
        raise ChangePacketError(f"change packet already exists: {change_id}")

    templates = _template_dir(template_dir)
    change_template = _read_template(templates / CHANGE_TEMPLATE_FILE)
    plan_template = _read_template(templates / CHANGE_PLAN_TEMPLATE_FILE)
    values = {
        "CHANGE_ID": change_id,
        "CREATED_AT": timestamp,
        "ORIGINAL_COMPLETED_SOURCE_HASH": source_hash,
        "SCOPE_DELTA": _markdown_bullets(scope),
        "AFFECTED_ACS": _markdown_bullets(acs),
        "DELIVERY_IMPACT": impact,
        "APPROVAL_STATE": "draft",
        "APPROVED_AT": "-",
        "PLAN_PATH": CHANGE_PLAN_FILE,
        "EVIDENCE_PATH": CHANGE_EVIDENCE_DIR,
        "REVIEW_PATH": CHANGE_REVIEW_DIR,
    }
    change_text = _render(change_template, values)
    plan_text = _render(plan_template, values)
    parsed = parse_change_text(change_text)
    if parsed["change_id"] != change_id:
        raise ChangePacketError("rendered template changed the packet id")

    try:
        changes_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ChangePacketError(f"could not create {CHANGE_ROOT}: {exc}") from exc

    temp_path: Path | None = None
    try:
        temp_path = Path(
            tempfile.mkdtemp(prefix=f".{change_id}.", suffix=".tmp", dir=changes_root)
        )
        (temp_path / CHANGE_EVIDENCE_DIR).mkdir()
        (temp_path / CHANGE_REVIEW_DIR).mkdir()
        _write_new_file(temp_path / CHANGE_FILE, change_text)
        _write_new_file(temp_path / CHANGE_PLAN_FILE, plan_text)
        _fsync_directory(temp_path)
        os.replace(temp_path, packet)
        temp_path = None
        _fsync_directory(changes_root)
    except OSError as exc:
        raise ChangePacketError(f"could not create change packet {change_id}: {exc}") from exc
    finally:
        if temp_path is not None:
            shutil.rmtree(temp_path, ignore_errors=True)
    return read_change_packet(root, change_id)


def _write_new_file(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def approve_change_packet(
    project: Path,
    change_id: str,
    *,
    approved_at: str | None = None,
) -> dict[str, Any]:
    """Atomically move one draft packet to its approved state."""
    current = read_change_packet(project, change_id)
    if current["approval_state"] == "approved":
        raise ChangePacketError(f"change packet is already approved: {change_id}")
    timestamp = approved_at or _now_utc()
    if not _valid_iso8601(timestamp):
        raise ChangePacketError(
            "approved_at must be an ISO-8601 timestamp with a timezone"
        )

    packet = _packet_root(project, change_id)
    path = packet / CHANGE_FILE
    original = path.read_text(encoding="utf-8")
    updated, state_count = re.subn(
        r"(?m)^(\s*-\s+\*\*State\*\*:\s*)draft\s*$",
        rf"\g<1>approved",
        original,
    )
    updated, approved_count = re.subn(
        r"(?m)^(\s*-\s+\*\*Approved at\*\*:\s*)-\s*$",
        rf"\g<1>{timestamp}",
        updated,
    )
    if state_count != 1 or approved_count != 1:
        raise ChangePacketError("change.md approval fields are missing or ambiguous")
    parsed = parse_change_text(updated)
    if parsed["approval_state"] != "approved":
        raise ChangePacketError("change.md could not be moved to approved state")

    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=packet,
            prefix=".change.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
        _fsync_directory(packet)
    except OSError as exc:
        raise ChangePacketError(
            f"could not approve change packet {change_id}: {exc}"
        ) from exc
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
    return read_change_packet(project, change_id)


def _change_sort_key(record: Mapping[str, Any]) -> tuple[str, str]:
    return (str(record.get("created_at") or ""), str(record.get("change_id") or ""))


def list_change_packets(project: Path) -> list[dict[str, Any]]:
    """Return all valid packet records in deterministic chronological order."""
    root = project.resolve()
    changes_root = root / CHANGE_ROOT
    problem = _lstat_problem(changes_root, kind="directory")
    if problem:
        raise ChangePacketError(problem)
    if not changes_root.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        entries = sorted(changes_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ChangePacketError(f"could not list {CHANGE_ROOT}: {exc}") from exc
    for entry in entries:
        if entry.name.startswith("."):
            continue
        problem = _lstat_problem(entry, kind="directory")
        if problem:
            raise ChangePacketError(problem)
        if not entry.is_dir():
            raise ChangePacketError(f"unexpected entry in {CHANGE_ROOT}: {entry.name}")
        validate_change_id(entry.name)
        records.append(read_change_packet(root, entry.name))
    return sorted(records, key=_change_sort_key)


def lookup_change_packet(project: Path, change_id: str) -> dict[str, Any] | None:
    """Look up one packet safely, returning ``None`` when it does not exist."""
    packet = _packet_root(project, change_id)
    if not packet.exists():
        return None
    return read_change_packet(project, change_id)


def find_change_packets(
    project: Path,
    *,
    original_completed_source_hash: str | None = None,
    affected_ac: str | None = None,
    approval_state: str | None = None,
) -> list[dict[str, Any]]:
    """Filter packet history without changing its deterministic ordering."""
    if original_completed_source_hash is not None:
        _validate_source_hash(original_completed_source_hash)
    if affected_ac is not None and not _AC_ID_RE.fullmatch(affected_ac):
        raise ChangePacketError("affected_ac must use the AC-n form")
    if approval_state is not None and approval_state not in CHANGE_APPROVAL_STATES:
        raise ChangePacketError("approval_state must be draft or approved")
    records = list_change_packets(project)
    return [
        record
        for record in records
        if (
            original_completed_source_hash is None
            or record["original_completed_source_hash"]
            == original_completed_source_hash
        )
        and (affected_ac is None or affected_ac in record["affected_acs"])
        and (
            approval_state is None
            or record["approval_state"] == approval_state
        )
    ]


def legacy_amendment_history(project: Path) -> list[dict[str, Any]]:
    """Read v0.3 AMEND rows from root Plan.md without rewriting them."""
    plan = project.resolve() / "Plan.md"
    problem = _lstat_problem(plan, kind="file")
    if problem:
        raise ChangePacketError(problem)
    if not plan.exists():
        return []
    try:
        text = plan.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ChangePacketError("root Plan.md is not valid UTF-8") from exc
    except OSError as exc:
        raise ChangePacketError(f"root Plan.md cannot be read: {exc}") from exc
    records: list[dict[str, Any]] = []
    for task in parse_plan_tasks_text(text):
        match = _LEGACY_AMEND_RE.fullmatch(str(task.get("id") or ""))
        if not match:
            continue
        records.append(
            {
                "kind": "legacy-amendment",
                "change_id": task["id"],
                "legacy_number": int(match.group("number")),
                "status": task["status"],
                "mode": task["mode"],
                "files": task["files"],
                "depends": task["depends"],
                "verify": task["verify"],
                "evidence": task["evidence"],
                "description": task["description"],
                "line": task["line"],
                "plan_version": task["plan_version"],
                "path": "Plan.md",
            }
        )
    return sorted(records, key=lambda item: (item["legacy_number"], item["line"]))


def change_history(project: Path) -> dict[str, Any]:
    """Return deterministic v0.4 packet history plus preserved v0.3 rows."""
    packets = list_change_packets(project)
    legacy = legacy_amendment_history(project)
    return {
        "schema": "star-forge.change-history.v1",
        "entries": [*legacy, *packets],
        "packets": packets,
        "legacy_amendments": legacy,
        "packet_count": len(packets),
        "legacy_amendment_count": len(legacy),
    }


def lookup_change_history(project: Path, change_id: str) -> dict[str, Any] | None:
    """Look up either a v0.4 packet or one preserved v0.3 amendment row."""
    packet = lookup_change_packet(project, change_id)
    if packet is not None:
        return packet
    match = _LEGACY_AMEND_RE.fullmatch(change_id)
    if not match:
        return None
    for record in legacy_amendment_history(project):
        if record["change_id"].casefold() == change_id.casefold():
            return record
    return None


__all__ = [
    "CHANGE_APPROVAL_STATES",
    "CHANGE_EVIDENCE_DIR",
    "CHANGE_FILE",
    "CHANGE_PLAN_FILE",
    "CHANGE_REVIEW_DIR",
    "CHANGE_ROOT",
    "CHANGE_SCHEMA",
    "ChangePacketError",
    "approve_change_packet",
    "change_history",
    "create_change_packet",
    "find_change_packets",
    "legacy_amendment_history",
    "list_change_packets",
    "lookup_change_history",
    "lookup_change_packet",
    "parse_change_text",
    "read_change_packet",
    "validate_change_id",
    "validate_change_packet",
]
