"""Isolated v0.4 change packets and read-only amendment history."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from . import change_derivation
from .contracts import parse_plan_tasks_text, serialize_plan_tasks
from .policy_data import value as _policy_value

_POLICY = _policy_value("changes.POLICY")
CHANGE_ROOT = Path(".starforge") / "changes"
CHANGE_FILE = "change.md"
CHANGE_PLAN_FILE = "Plan.md"
CHANGE_EVIDENCE_DIR = "evidence"
CHANGE_REVIEW_DIR = "review"
CHANGE_IMPACT_FILE = "impact.json"
CHANGE_SCHEMA = "star-forge.change-packet.v1"
CHANGE_IMPACT_SCHEMA = change_derivation.CHANGE_IMPACT_SCHEMA
CHANGE_APPROVAL_STATES = frozenset(_POLICY["approval_states"])
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
    return (dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
def _valid_iso8601(value: str) -> bool:
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None

def validate_change_id(change_id: str) -> str:
    """Return a safe packet identifier or raise ``ChangePacketError``."""
    if not isinstance(change_id, str) or not _CHANGE_ID_RE.fullmatch(change_id):
        raise ChangePacketError(_POLICY["messages"]["change_id"])
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
    problems = ((stat.S_ISLNK(mode), "symlink"),
                (kind == "directory" and not stat.S_ISDIR(mode), "directory"),
                (kind == "file" and not stat.S_ISREG(mode), "regular file"))
    for invalid, expected in problems:
        if invalid:
            return f"{path} must not be a symlink" if expected == "symlink" else f"{path} must be a {expected}"
    return ""
def _require_path(path: Path, kind: str, missing: str) -> None:
    problem = _lstat_problem(path, kind=kind)
    if problem:
        raise ChangePacketError(problem)
    if not path.exists():
        raise ChangePacketError(missing)
def _validate_packet_relative_path(raw_path: str, *, label: str) -> str:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ChangePacketError(f"{label} must be a non-empty packet-relative path")
    if "\\" in raw_path:
        raise ChangePacketError(f"{label} must use '/' separators")
    candidate = PurePosixPath(raw_path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ChangePacketError(f"{label} must be a normalized path inside the change packet")
    normalized = candidate.as_posix()
    if normalized != raw_path:
        raise ChangePacketError(f"{label} must be normalized")
    return normalized
def _section(text: str, title: str) -> str:
    matches = list(_SECTION_RE.finditer(text))
    for index, match in enumerate(matches):
        if match.group("title").strip().casefold() != title.casefold():
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
        if match:
            values.append(match.group(1).strip())
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
def _clean_affected_acs(affected_acs: Sequence[str]) -> list[str]:
    if isinstance(affected_acs, (str, bytes)) or not isinstance(affected_acs, Sequence):
        raise ChangePacketError("affected_acs must be a non-empty sequence")
    cleaned: set[str] = set()
    for item in affected_acs:
        if not isinstance(item, str) or not _AC_ID_RE.fullmatch(item.strip()):
            raise ChangePacketError("affected_acs entries must use the AC-n form")
        cleaned.add(item.strip())
    if not cleaned:
        raise ChangePacketError("affected_acs must not be empty")
    return sorted(cleaned, key=lambda value: int(value.split("-", 1)[1]))
def _validate_source_hash(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ChangePacketError(_POLICY["messages"]["source_hash"])
    return value
def _validate_delivery_impact(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChangePacketError("delivery_impact must be a non-empty string")
    cleaned = value.strip()
    if "\n" in cleaned or "\r" in cleaned:
        raise ChangePacketError("delivery_impact must be a single-line string")
    return cleaned
def _read_template(path: Path) -> str:
    _require_path(path, "file", f"template is missing: {path}")
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
        raise ChangePacketError("template has unresolved placeholders: " + ", ".join(unresolved))
    return rendered.rstrip() + "\n"
def normalize_changed_files(changed_files: Sequence[str]) -> list[str]:
    """Normalize Git status paths into a stable, de-duplicated scope."""
    try:
        return change_derivation.normalize_changed_files(changed_files)
    except change_derivation.ChangeDerivationError as exc:
        raise ChangePacketError(str(exc)) from exc

def derive_change_impact(
    *,
    changed_files: Sequence[str],
    root_tasks: Sequence[Mapping[str, Any]],
    blueprint_text: str = "",
    delivery_contract: Mapping[str, Any] | None = None,
    profile: str = "standard",
) -> dict[str, Any]:
    """Derive packet tasks, risk policy, and proof from the actual changed scope."""
    try:
        return change_derivation.derive_change_impact(
            changed_files=changed_files,
            root_tasks=root_tasks,
            blueprint_text=blueprint_text,
            delivery_contract=delivery_contract,
            profile=profile,
        )
    except change_derivation.ChangeDerivationError as exc:
        raise ChangePacketError(str(exc)) from exc

def derive_change_impact_for_project(
    project: Path,
    changed_files: Sequence[str],
    *,
    profile: str = "standard",
) -> dict[str, Any]:
    """Load the root contracts and derive one change impact without mutation."""
    root = project.resolve()
    try:
        plan_text = (root / "Plan.md").read_text(encoding="utf-8")
        blueprint_text = (root / "Blueprint.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ChangePacketError(f"root contracts cannot be read: {exc}") from exc
    delivery_contract: dict[str, Any] = {}
    delivery_path = root / ".starforge" / "contracts" / "delivery.json"
    if delivery_path.exists():
        try:
            parsed = json.loads(delivery_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ChangePacketError(f"delivery contract cannot be read: {exc}") from exc
        if isinstance(parsed, dict):
            delivery_contract = parsed
    root_tasks = parse_plan_tasks_text(plan_text)
    blueprint_acs = set(_AC_ID_RE.findall(blueprint_text))
    unmapped = []
    for task in root_tasks:
        legacy_with_one_possible_ac = (task.get("plan_version") == "legacy" and len(blueprint_acs) == 1)
        explicit_v2_mapping = (task.get("plan_version") == "v2" and _AC_ID_RE.search(str(task.get("acs") or "")) is not None and bool(str(task.get("proof") or "").strip()) and
                               str(task.get("proof") or "").strip() != "REVIEW_REQUIRED")
        if not legacy_with_one_possible_ac and not explicit_v2_mapping:
            unmapped.append(str(task.get("id") or "unknown"))
    if unmapped:
        raise ChangePacketError(_POLICY["messages"]["unmapped_plan"] + ", ".join(unmapped))
    return derive_change_impact(
        changed_files=changed_files,
        root_tasks=root_tasks,
        blueprint_text=blueprint_text,
        delivery_contract=delivery_contract,
        profile=profile,
    )

def validate_change_packet(payload: Any) -> list[str]:
    """Return deterministic schema problems for a parsed change packet."""
    if not isinstance(payload, Mapping):
        return ["change packet must be a mapping"]
    problems: list[str] = []
    missing = sorted(set(_POLICY["packet_fields"]) - {str(key) for key in payload})
    if missing:
        problems.append("missing fields: " + ", ".join(missing))
    if payload.get("schema") != CHANGE_SCHEMA:
        problems.append(f"schema must be {CHANGE_SCHEMA}")
    for field, validator in (
        ("change_id", validate_change_id),
        ("original_completed_source_hash", _validate_source_hash),
        ("scope_delta", _clean_scope_delta),
        ("affected_acs", _clean_affected_acs),
        ("delivery_impact", _validate_delivery_impact),
    ):
        try:
            validator(payload.get(field))  # type: ignore[arg-type]
        except ChangePacketError as exc:
            problems.append(str(exc))
    if not isinstance(payload.get("created_at"), str) or not _valid_iso8601(payload.get("created_at")):  # type: ignore[arg-type]
        problems.append(_POLICY["messages"]["created_at"])
    approval_state = payload.get("approval_state")
    if approval_state not in CHANGE_APPROVAL_STATES:
        problems.append(_POLICY["messages"]["approval_state"])
    approved_at = payload.get("approved_at")
    if approval_state == "draft" and approved_at is not None:
        problems.append("draft packets must not have approved_at")
    if approval_state == "approved" and (not isinstance(approved_at, str) or not _valid_iso8601(approved_at)):
        problems.append("approved packets require an ISO-8601 approved_at timestamp with a timezone")
    for field, expected, _kind in _POLICY["packet_paths"]:
        try:
            value = _validate_packet_relative_path(payload.get(field), label=field)  # type: ignore[arg-type]
            if value != expected:
                problems.append(f"{field} must be {expected}")
        except ChangePacketError as exc:
            problems.append(str(exc))
    return problems
def _parsed_value(text: str, fields: Mapping[str, str], kind: str, label: str) -> Any:
    if kind == "section":
        return _bullet_values(_section(text, label))
    value = fields.get(label, "-" if kind == "approved" else "")
    if kind == "state":
        return value.casefold()
    return None if kind == "approved" and value in _POLICY["parser"]["empty_approved_at"] else value

def parse_change_text(text: str) -> dict[str, Any]:
    """Parse a v0.4 ``change.md`` document without accepting ambiguous fields."""
    if not isinstance(text, str):
        raise ChangePacketError("change.md content must be text")
    fields = _fields(text)
    payload = {key: _parsed_value(text, fields, kind, label)
               for key, kind, label in _POLICY["parsed_fields"]}
    problems = validate_change_packet(payload)
    if problems:
        raise ChangePacketError("; ".join(problems))
    payload["scope_delta"] = _clean_scope_delta(payload["scope_delta"])
    payload["affected_acs"] = _clean_affected_acs(payload["affected_acs"])
    return payload
def _assert_packet_contents(packet: Path, payload: Mapping[str, Any]) -> None:
    resolved_packet = packet.resolve()
    for field, _expected, kind in _POLICY["packet_paths"]:
        relative = _validate_packet_relative_path(str(payload[field]), label=field)
        path = packet / relative
        _require_path(path, kind, f"{field} is missing: {relative}")
        try:
            path.resolve().relative_to(resolved_packet)
        except ValueError as exc:
            raise ChangePacketError(f"{field} escapes the change packet") from exc

def read_change_packet(project: Path, change_id: str) -> dict[str, Any]:
    """Read and validate one packet, including its packet-local paths."""
    packet = _packet_root(project, change_id)
    _require_path(packet, "directory", f"change packet does not exist: {change_id}")
    change_path = packet / CHANGE_FILE
    _require_path(change_path, "file", f"{CHANGE_FILE} is missing for {change_id}")
    try:
        text = change_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ChangePacketError(f"{CHANGE_FILE} is not valid UTF-8") from exc
    except OSError as exc:
        raise ChangePacketError(f"{CHANGE_FILE} cannot be read: {exc}") from exc
    payload = parse_change_text(text)
    if payload["change_id"] != change_id:
        raise ChangePacketError(f"packet directory {change_id} does not match change.md id "
                                f"{payload['change_id']}")
    _assert_packet_contents(packet, payload)
    record = {
        **payload,
        "kind": "change-packet",
        "path": str(packet.relative_to(project.resolve())),
    }
    impact_path = packet / CHANGE_IMPACT_FILE
    if impact_path.exists() or impact_path.is_symlink():
        problem = _lstat_problem(impact_path, kind="file")
        if problem:
            raise ChangePacketError(problem)
        try:
            impact = json.loads(impact_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ChangePacketError(f"{CHANGE_IMPACT_FILE} cannot be read: {exc}") from exc
        if not isinstance(impact, dict) or impact.get("schema") != CHANGE_IMPACT_SCHEMA:
            raise ChangePacketError(f"{CHANGE_IMPACT_FILE} must use schema {CHANGE_IMPACT_SCHEMA}")
        if impact.get("change_id") != change_id:
            raise ChangePacketError(f"{CHANGE_IMPACT_FILE} does not match packet {change_id}")
        record["impact"] = impact
    return record

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
        raise ChangePacketError("created_at must be an ISO-8601 timestamp with a timezone")
    changes_root = root / CHANGE_ROOT
    for directory in (root / ".starforge", changes_root):
        problem = _lstat_problem(directory, kind="directory")
        if problem:
            raise ChangePacketError(problem)
    if packet.exists() or packet.is_symlink():
        raise ChangePacketError(f"change packet already exists: {change_id}")
    templates = (template_dir.resolve() if template_dir is not None
                 else Path(__file__).resolve().parents[2] / "templates")
    change_template = _read_template(templates / CHANGE_TEMPLATE_FILE)
    plan_template = _read_template(templates / CHANGE_PLAN_TEMPLATE_FILE)
    values = {
        **_POLICY["template_defaults"],
        "CHANGE_ID": change_id,
        "CREATED_AT": timestamp,
        "ORIGINAL_COMPLETED_SOURCE_HASH": source_hash,
        "SCOPE_DELTA": "\n".join(f"- {value}" for value in scope),
        "AFFECTED_ACS": "\n".join(f"- {value}" for value in acs),
        "DELIVERY_IMPACT": impact,
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
        temp_path = Path(tempfile.mkdtemp(prefix=f".{change_id}.", suffix=".tmp", dir=changes_root))
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
def _write_atomic_text(path: Path, text: str) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass
def _read_packet_plan(project: Path, change_id: str) -> tuple[Path, str]:
    path = _packet_root(project, change_id) / CHANGE_PLAN_FILE
    try:
        return path, path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ChangePacketError(f"packet Plan.md cannot be read: {exc}") from exc
def _replace_plan_table(text: str, tasks: Sequence[Mapping[str, Any]]) -> str:
    lines = text.splitlines()
    header = "| " + " | ".join(_POLICY["plan_columns"]) + " |"
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == header)
    except StopIteration as exc:
        raise ChangePacketError("packet Plan.md has no exact Plan v2 task table") from exc
    end = start + 2
    while end < len(lines) and lines[end].strip().startswith("|"):
        end += 1
    replacement = serialize_plan_tasks(tasks, version="v2").rstrip().splitlines()
    lines[start:end] = replacement
    return "\n".join(lines).rstrip() + "\n"
def _packet_plan_tasks(impact: Mapping[str, Any], change_id: str) -> list[dict[str, str]]:
    tasks: list[dict[str, str]] = []
    for index, affected in enumerate(impact.get("affected_tasks") or [], start=1):
        if not isinstance(affected, Mapping):
            continue
        values = {
            **_POLICY["task_defaults"],
            "id": f"{change_id}-T{index}",
            "description": str(affected.get("description") or _POLICY["task_defaults"]["description"]),
            "mode": str(affected.get("mode") or _POLICY["task_defaults"]["mode"]),
            "files": ", ".join(str(value) for value in affected.get("files") or []),
            "acs": ", ".join(str(value) for value in affected.get("acs") or []),
            "proof": ", ".join(str(value) for value in affected.get("proof_kinds") or []),
            "verify": str(affected.get("verify") or _POLICY["task_defaults"]["verify"]),
        }
        tasks.append({field: values[field] for field in _POLICY["task_fields"]})
    if not tasks:
        raise ChangePacketError("derived change impact has no affected tasks")
    return tasks

def plan_change_packet(
    project: Path,
    change_id: str,
    impact: Mapping[str, Any],
) -> dict[str, Any]:
    """Write derived impact and packet-local Plan v2 rows for one draft."""
    packet = read_change_packet(project, change_id)
    if packet["approval_state"] != "draft":
        raise ChangePacketError("only draft change packets can be planned")
    if impact.get("schema") != CHANGE_IMPACT_SCHEMA:
        raise ChangePacketError(f"impact must use schema {CHANGE_IMPACT_SCHEMA}")
    if list(impact.get("scope_delta") or []) != packet["scope_delta"]:
        raise ChangePacketError("derived impact scope does not match change.md")
    if list(impact.get("affected_acs") or []) != packet["affected_acs"]:
        raise ChangePacketError("derived impact ACs do not match change.md")
    packet_root = _packet_root(project, change_id)
    plan_path, original = _read_packet_plan(project, change_id)
    tasks = _packet_plan_tasks(impact, change_id)
    planned = _replace_plan_table(original, tasks)
    impact_payload = {**dict(impact), "change_id": change_id}
    _write_atomic_text(plan_path, planned)
    _write_atomic_text(packet_root / CHANGE_IMPACT_FILE,
                       json.dumps(impact_payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return read_change_packet(project, change_id)

def activate_change_plan(project: Path, change_id: str) -> list[dict[str, Any]]:
    """Make an approved packet's dependency-free task rows ready."""
    packet = read_change_packet(project, change_id)
    if packet["approval_state"] != "approved":
        raise ChangePacketError("change packet must be approved before activation")
    plan_path, original = _read_packet_plan(project, change_id)
    parsed = parse_plan_tasks_text(original)
    if not parsed:
        raise ChangePacketError("approved change packet has no planned tasks")
    updated_tasks: list[dict[str, Any]] = []
    for task in parsed:
        updated_tasks.append({
            **task,
            "status": "ready" if task["status"] == "queued" else task["status"],
        })
    updated = _replace_plan_table(original, updated_tasks)
    updated, count = re.subn(*_POLICY["transitions"]["plan_status"], updated)
    if count != 1 and "Status: active" not in updated:
        raise ChangePacketError("packet Plan.md status is missing or ambiguous")
    _write_atomic_text(plan_path, updated)
    return parse_plan_tasks_text(updated)

def change_plan_tasks(project: Path, change_id: str) -> list[dict[str, Any]]:
    """Read packet-local Plan v2 tasks without consulting the root Plan."""
    read_change_packet(project, change_id)
    return parse_plan_tasks_text(_read_packet_plan(project, change_id)[1])

def next_change_id(project: Path) -> str:
    """Return the next collision-free numeric v0.4 packet id."""
    highest = 0
    for packet in list_change_packets(project):
        match = re.fullmatch(r"CHANGE-([1-9][0-9]*)", packet["change_id"])
        if match:
            highest = max(highest, int(match.group(1)))
    return f"CHANGE-{highest + 1}"

def create_or_select_change_packet(
    project: Path,
    *,
    original_completed_source_hash: str,
    changed_files: Sequence[str],
    profile: str = "standard",
    created_at: str | None = None,
    template_dir: Path | None = None,
) -> dict[str, Any]:
    """Select the exact open scope or create and derive one isolated draft."""
    scope = normalize_changed_files(changed_files)
    existing: list[dict[str, Any]] = []
    binding = {"original_completed_source_hash": original_completed_source_hash, "scope_delta": scope}
    for packet in list_change_packets(project):
        if any(packet[field] != binding[field] for field in _POLICY["packet_selection"]["binding_fields"]):
            continue
        planned = change_plan_tasks(project, packet["change_id"])
        if planned and all(task.get("status") == _POLICY["packet_selection"]["terminal_task_status"] for task in planned):
            continue
        existing.append(packet)
    if existing:
        return existing[-1]
    impact = derive_change_impact_for_project(
        project,
        scope,
        profile=profile,
    )
    packet = create_change_packet(
        project,
        change_id=next_change_id(project),
        original_completed_source_hash=original_completed_source_hash,
        scope_delta=scope,
        affected_acs=impact["affected_acs"],
        delivery_impact=impact["delivery_impact"],
        created_at=created_at,
        template_dir=template_dir,
    )
    return plan_change_packet(project, packet["change_id"], impact)

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
    blockers = list((current.get("impact") or {}).get("approval_blockers") or [])
    if blockers:
        raise ChangePacketError("change packet cannot be approved: " + "; ".join(str(item) for item in blockers))
    timestamp = approved_at or _now_utc()
    if not _valid_iso8601(timestamp):
        raise ChangePacketError("approved_at must be an ISO-8601 timestamp with a timezone")
    packet = _packet_root(project, change_id)
    path = packet / CHANGE_FILE
    original = path.read_text(encoding="utf-8")
    updated, state_count = re.subn(*_POLICY["transitions"]["approval_state"], original)
    approved_transition = _POLICY["transitions"]["approved_at"]
    updated, approved_count = re.subn(
        approved_transition[0],
        approved_transition[1].format(timestamp=timestamp),
        updated,
    )
    if state_count != 1 or approved_count != 1:
        raise ChangePacketError("change.md approval fields are missing or ambiguous")
    parsed = parse_change_text(updated)
    if parsed["approval_state"] != "approved":
        raise ChangePacketError("change.md could not be moved to approved state")
    try:
        _write_atomic_text(path, updated)
    except OSError as exc:
        raise ChangePacketError(f"could not approve change packet {change_id}: {exc}") from exc
    return read_change_packet(project, change_id)

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
    return sorted(records, key=lambda record: (
        str(record.get("created_at") or ""), str(record.get("change_id") or "")))

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
        raise ChangePacketError(_POLICY["messages"]["affected_ac"])
    if approval_state is not None and approval_state not in CHANGE_APPROVAL_STATES:
        raise ChangePacketError(_POLICY["messages"]["approval_state"])
    records = list_change_packets(project)
    return [record for record in records
            if (original_completed_source_hash is None or record["original_completed_source_hash"] == original_completed_source_hash)
            and (affected_ac is None or affected_ac in record["affected_acs"])
            and (approval_state is None or record["approval_state"] == approval_state)]

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
        record = {
            "kind": "legacy-amendment",
            "change_id": task["id"],
            "legacy_number": int(match.group("number")),
            **{field: task[field] for field in _POLICY["legacy_fields"]},
            "path": "Plan.md"}
        records.append(record)
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
    if not _LEGACY_AMEND_RE.fullmatch(change_id):
        return None
    return next((record for record in legacy_amendment_history(project)
                 if record["change_id"].casefold() == change_id.casefold()), None)
