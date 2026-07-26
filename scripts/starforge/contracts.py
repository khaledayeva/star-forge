"""Versioned project contracts used by the Star Forge lifecycle."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


BLUEPRINT_FILE = "Blueprint.md"
BLUEPRINT_LOCK_FILE = "Blueprint.lock.json"
BLUEPRINT_LOCK_SCHEMA = "star-forge.blueprint-lock.v1"
BLUEPRINT_CONTRACT_VERSION = 1
PLAN_FILE = "Plan.md"
PLAN_MIGRATION_SCHEMA = "star-forge.plan-migration.v1"
PLAN_LEGACY_COLUMNS = (
    "Task",
    "Description",
    "Status",
    "Mode",
    "Files",
    "Depends",
    "Verify",
    "Evidence",
)
PLAN_V2_COLUMNS = (
    "Task",
    "Description",
    "Status",
    "Mode",
    "Files",
    "Depends",
    "ACs",
    "Proof",
    "Verify",
    "Evidence",
)
PLAN_REVIEW_REQUIRED = "REVIEW_REQUIRED"

_LOCK_FIELDS = frozenset(
    {
        "schema",
        "blueprint_sha256",
        "approved_at",
        "contract_version",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    """A project contract could not be read or safely updated."""


def split_plan_row(line: str) -> list[str]:
    """Split a Markdown table row while preserving escaped pipe characters."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]

    cells: list[str] = []
    cell: list[str] = []
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if char == "\\" and index + 1 < len(stripped):
            escaped = stripped[index + 1]
            if escaped == "|":
                cell.append(escaped)
                index += 2
                continue
        if char == "|":
            cells.append("".join(cell).strip())
            cell = []
        else:
            cell.append(char)
        index += 1
    cells.append("".join(cell).strip())
    return cells


def _is_plan_separator(line: str) -> bool:
    cells = split_plan_row(line)
    return bool(cells) and all(
        re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells
    )


def _plan_tables(
    lines: Sequence[str],
) -> list[tuple[int, list[str], int, int]]:
    tables: list[tuple[int, list[str], int, int]] = []
    for header_index, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        headers = split_plan_row(line)
        lowered = [header.lower() for header in headers]
        if "task" not in lowered or "status" not in lowered:
            continue
        if (
            header_index + 1 >= len(lines)
            or not _is_plan_separator(lines[header_index + 1])
        ):
            continue
        end = header_index + 2
        while end < len(lines) and lines[end].strip().startswith("|"):
            end += 1
        tables.append((header_index, headers, header_index + 2, end))
    return tables


def plan_table_version(headers: Sequence[str]) -> str:
    """Classify an exact task-table header as legacy, v2, or compatible."""
    normalized = tuple(header.strip().lower() for header in headers)
    if normalized == tuple(column.lower() for column in PLAN_LEGACY_COLUMNS):
        return "legacy"
    if normalized == tuple(column.lower() for column in PLAN_V2_COLUMNS):
        return "v2"
    return "compatible"


def parse_plan_tasks_text(text: str) -> list[dict[str, Any]]:
    """Read both eight-column legacy tasks and ten-column Plan v2 tasks."""
    lines = text.splitlines()
    tasks: list[dict[str, Any]] = []
    for header_index, headers, start, end in _plan_tables(lines):
        index = {name.lower(): position for position, name in enumerate(headers)}
        version = plan_table_version(headers)
        for line_index in range(start, end):
            cells = split_plan_row(lines[line_index])
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            task_id = cells[index["task"]].strip()
            if not task_id:
                continue

            def cell(name: str, fallback: str = "") -> str:
                position = index.get(name.lower())
                return fallback if position is None else cells[position].strip()

            tasks.append(
                {
                    "id": task_id,
                    "status": cell("status"),
                    "mode": cell("mode").lower() or "delegate",
                    "files": cell("files"),
                    "depends": cell("depends"),
                    "acs": cell("acs"),
                    "proof": cell("proof"),
                    "verify": cell("verify"),
                    "evidence": cell("evidence"),
                    "description": cell("description", cell("task", task_id)),
                    "line": line_index + 1,
                    "headers": list(headers),
                    "header_line": header_index + 1,
                    "plan_version": version,
                }
            )
    return tasks


def _escape_plan_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", r"\|")


def serialize_plan_tasks(
    tasks: Sequence[Mapping[str, Any]],
    *,
    version: str = "v2",
) -> str:
    """Serialize task mappings as an exact legacy or Plan v2 Markdown table."""
    if version == "v2":
        columns = PLAN_V2_COLUMNS
    elif version == "legacy":
        columns = PLAN_LEGACY_COLUMNS
    else:
        raise ContractError("Plan version must be `legacy` or `v2`")

    key_for_column = {
        "Task": "id",
        "Description": "description",
        "Status": "status",
        "Mode": "mode",
        "Files": "files",
        "Depends": "depends",
        "ACs": "acs",
        "Proof": "proof",
        "Verify": "verify",
        "Evidence": "evidence",
    }
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join("-" * max(3, len(column)) for column in columns) + "|",
    ]
    for task in tasks:
        values: list[str] = []
        for column in columns:
            key = key_for_column[column]
            value = task.get(key)
            if column == "Task" and value is None:
                value = task.get("task", "")
            values.append(_escape_plan_cell(value))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"


def migrate_plan_text(text: str) -> tuple[str, dict[str, int]]:
    """Convert every exact legacy task table to v2 without guessing mappings."""
    lines = text.splitlines()
    trailing_newline = text.endswith(("\n", "\r"))
    tables = _plan_tables(lines)
    legacy_tables = [
        table for table in tables if plan_table_version(table[1]) == "legacy"
    ]
    if not legacy_tables:
        if any(plan_table_version(table[1]) == "v2" for table in tables):
            raise ContractError("Plan already uses the v2 task-table schema")
        raise ContractError(
            "Plan has no exact eight-column legacy task table to migrate"
        )

    migrated_rows = 0
    for header_index, headers, start, end in reversed(legacy_tables):
        index = {name.lower(): position for position, name in enumerate(headers)}
        migrated_tasks: list[dict[str, str]] = []
        for line_index in range(start, end):
            cells = split_plan_row(lines[line_index])
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))

            def cell(name: str) -> str:
                return cells[index[name.lower()]].strip()

            task_id = cell("task")
            if not task_id:
                continue
            migrated_tasks.append(
                {
                    "id": task_id,
                    "description": cell("description"),
                    "status": cell("status"),
                    "mode": cell("mode"),
                    "files": cell("files"),
                    "depends": cell("depends"),
                    "acs": PLAN_REVIEW_REQUIRED,
                    "proof": PLAN_REVIEW_REQUIRED,
                    "verify": cell("verify"),
                    "evidence": cell("evidence"),
                }
            )
        replacement = serialize_plan_tasks(migrated_tasks, version="v2").splitlines()
        lines[header_index:end] = replacement
        migrated_rows += len(migrated_tasks)

    migrated = "\n".join(lines)
    if trailing_newline:
        migrated += "\n"
    return migrated, {
        "legacy_tables_migrated": len(legacy_tables),
        "task_rows_migrated": migrated_rows,
    }


def write_plan_v2_migration(source: Path, output: Path) -> dict[str, Any]:
    """Atomically create a separate, reviewable Plan v2 draft."""
    source = Path(os.path.abspath(source))
    output = Path(os.path.abspath(output))
    source_problem = _regular_file_problem(source, required=True)
    if source_problem:
        raise ContractError(source_problem)
    if source == output:
        raise ContractError(
            "Migration output must be separate from the legacy Plan"
        )
    if output.exists() or output.is_symlink():
        raise ContractError(f"{output.name} already exists; choose a new output path")

    try:
        source_bytes = source.read_bytes()
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"{source.name} is not valid UTF-8") from exc
    except OSError as exc:
        raise ContractError(f"{source.name} cannot be read: {exc}") from exc

    migrated, summary = migrate_plan_text(source_text)
    parsed = parse_plan_tasks_text(migrated)
    if not parsed or any(task["plan_version"] != "v2" for task in parsed):
        raise ContractError("Migrated Plan v2 draft failed its schema check")

    output.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(migrated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, output)
    except OSError as exc:
        raise ContractError(f"Could not write {output}: {exc}") from exc
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass

    try:
        source_unchanged = source.read_bytes() == source_bytes
    except OSError:
        source_unchanged = False
    if not source_unchanged:
        try:
            output.unlink()
        except OSError:
            pass
        raise ContractError("Legacy Plan changed during migration; draft removed")

    return {
        "schema": PLAN_MIGRATION_SCHEMA,
        "source": str(source),
        "output": str(output),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "output_sha256": hashlib.sha256(migrated.encode("utf-8")).hexdigest(),
        "source_preserved": True,
        "review_required": True,
        **summary,
    }


def _now_utc() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _regular_file_problem(path: Path, *, required: bool) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return f"{path.name} is missing" if required else ""
    except OSError:
        return f"{path.name} cannot be inspected"
    if stat.S_ISLNK(mode):
        return f"{path.name} must not be a symlink"
    if not stat.S_ISREG(mode):
        return f"{path.name} must be a regular file"
    if mode & 0o444 == 0:
        return f"{path.name} is not readable"
    return ""


def blueprint_sha256(path: Path) -> str:
    """Hash the exact Blueprint bytes without normalizing its content."""
    problem = _regular_file_problem(path, required=True)
    if problem:
        raise ContractError(problem)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ContractError(f"{path.name} cannot be read: {exc}") from exc
    return digest.hexdigest()


def blueprint_text_has_legacy_approval(text: str) -> bool:
    """Recognize the mutable v0.3 approval sentinel for compatibility only."""
    normalized = re.sub(r"\*\*|__", "", text)
    if re.search(
        r"^\s*Status\s*[:\-—]\s*approved\b",
        normalized,
        re.IGNORECASE | re.MULTILINE,
    ):
        return True
    match = re.search(
        r"^\s*Last approved\s*[:\-—]\s*(.+)$",
        normalized,
        re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return False
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}\b", match.group(1).strip()))


def _valid_iso8601(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(
            candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
        )
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_blueprint_lock(payload: Any) -> list[str]:
    """Return deterministic validation problems for a v1 lock payload."""
    if not isinstance(payload, Mapping):
        return [f"{BLUEPRINT_LOCK_FILE} must contain a JSON object"]
    problems: list[str] = []
    payload_fields = {str(key) for key in payload}
    missing = sorted(_LOCK_FIELDS - payload_fields)
    extra = sorted(payload_fields - _LOCK_FIELDS)
    if missing:
        problems.append("missing fields: " + ", ".join(missing))
    if extra:
        problems.append("unexpected fields: " + ", ".join(extra))
    if payload.get("schema") != BLUEPRINT_LOCK_SCHEMA:
        problems.append(f"schema must be {BLUEPRINT_LOCK_SCHEMA}")
    digest = payload.get("blueprint_sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        problems.append("blueprint_sha256 must be a lowercase SHA-256 digest")
    if not _valid_iso8601(payload.get("approved_at")):
        problems.append("approved_at must be an ISO-8601 timestamp with a timezone")
    version = payload.get("contract_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != BLUEPRINT_CONTRACT_VERSION
    ):
        problems.append(
            f"contract_version must be {BLUEPRINT_CONTRACT_VERSION}"
        )
    return problems


def _read_lock(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    problem = _regular_file_problem(path, required=False)
    if problem:
        return None, [problem]
    if not path.exists():
        return None, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return None, [f"{BLUEPRINT_LOCK_FILE} is not valid UTF-8"]
    except json.JSONDecodeError:
        return None, [f"{BLUEPRINT_LOCK_FILE} is not valid JSON"]
    except OSError:
        return None, [f"{BLUEPRINT_LOCK_FILE} cannot be read"]
    problems = validate_blueprint_lock(payload)
    return (dict(payload) if isinstance(payload, Mapping) else None), problems


def blueprint_lock_state(project: Path) -> dict[str, Any]:
    """Describe v0.4 lock validity and legacy approval without conflating them."""
    root = project.resolve()
    blueprint_path = root / BLUEPRINT_FILE
    lock_path = root / BLUEPRINT_LOCK_FILE
    blueprint_problem = _regular_file_problem(blueprint_path, required=True)
    lock, lock_problems = _read_lock(lock_path)

    current_sha256: str | None = None
    legacy_approved = False
    if not blueprint_problem:
        try:
            text = blueprint_path.read_text(encoding="utf-8")
            current_sha256 = blueprint_sha256(blueprint_path)
            legacy_approved = blueprint_text_has_legacy_approval(text)
        except UnicodeDecodeError:
            blueprint_problem = f"{BLUEPRINT_FILE} is not valid UTF-8"
        except ContractError as exc:
            blueprint_problem = str(exc)
        except OSError:
            blueprint_problem = f"{BLUEPRINT_FILE} cannot be read"

    problems = ([blueprint_problem] if blueprint_problem else []) + lock_problems
    lock_exists = lock_path.exists() or bool(lock_problems)
    status: str
    if blueprint_problem:
        status = "invalid" if lock_exists else "missing"
    elif lock_problems:
        status = "invalid"
    elif lock is not None and lock.get("blueprint_sha256") != current_sha256:
        status = "drifted"
        problems.append(
            f"{BLUEPRINT_FILE} does not match {BLUEPRINT_LOCK_FILE}"
        )
    elif lock is not None:
        status = "locked"
    elif legacy_approved:
        status = "legacy-approved"
    else:
        status = "draft"

    locked = status == "locked"
    return {
        "schema": "star-forge.blueprint-lock-state.v1",
        "path": BLUEPRINT_FILE,
        "lock_path": BLUEPRINT_LOCK_FILE,
        "status": status,
        "approved": locked or status == "legacy-approved",
        "locked": locked,
        "legacy_approved": status == "legacy-approved",
        "current_sha256": current_sha256,
        "locked_sha256": lock.get("blueprint_sha256") if lock else None,
        "approved_at": lock.get("approved_at") if lock else None,
        "contract_version": lock.get("contract_version") if lock else None,
        "problems": problems,
    }


def write_blueprint_lock(
    project: Path,
    *,
    approved_at: str | None = None,
) -> dict[str, Any]:
    """Atomically approve the current Blueprint without editing its source."""
    root = project.resolve()
    blueprint_path = root / BLUEPRINT_FILE
    lock_path = root / BLUEPRINT_LOCK_FILE
    lock_problem = _regular_file_problem(lock_path, required=False)
    if lock_problem:
        raise ContractError(lock_problem)
    timestamp = approved_at or _now_utc()
    payload: dict[str, Any] = {
        "schema": BLUEPRINT_LOCK_SCHEMA,
        "blueprint_sha256": blueprint_sha256(blueprint_path),
        "approved_at": timestamp,
        "contract_version": BLUEPRINT_CONTRACT_VERSION,
    }
    problems = validate_blueprint_lock(payload)
    if problems:
        raise ContractError("; ".join(problems))

    root.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=root,
            prefix=".Blueprint.lock.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, lock_path)
    except OSError as exc:
        raise ContractError(
            f"Could not write {BLUEPRINT_LOCK_FILE}: {exc}"
        ) from exc
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
    return payload
