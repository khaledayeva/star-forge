"""Versioned project contracts used by the Star Forge lifecycle."""

from __future__ import annotations
from .policy_data import value as _policy_value

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
PLAN_LEGACY_COLUMNS = _policy_value('contracts.PLAN_LEGACY_COLUMNS')
PLAN_V2_COLUMNS = _policy_value('contracts.PLAN_V2_COLUMNS')
PLAN_REVIEW_REQUIRED = "REVIEW_REQUIRED"
PLAN_MAINTENANCE_EXEMPTION = "maintenance"
INTAKE_DECISION_FIELDS = _policy_value('contracts.INTAKE_DECISION_FIELDS')
PLAN_PROOF_KINDS = _policy_value('contracts.PLAN_PROOF_KINDS')
_LOCK_FIELDS = _policy_value('contracts._LOCK_FIELDS')
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AC_ID_RE = re.compile(r"AC-[1-9][0-9]*")
_DOCUMENT_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".txt"})
_DOCUMENT_FILENAMES = frozenset({"changelog", "license", "readme"})
_GENERIC_DELIVERY_TARGETS = frozenset({"source-only", "private-repo", "preview", "production", "package"})

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
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)

def _plan_tables(lines: Sequence[str], ) -> list[tuple[int, list[str], int, int]]:
    tables: list[tuple[int, list[str], int, int]] = []
    for header_index, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        headers = split_plan_row(line)
        lowered = [header.lower() for header in headers]
        if "task" not in lowered or "status" not in lowered:
            continue
        if (header_index + 1 >= len(lines) or not _is_plan_separator(lines[header_index + 1])):
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
            tasks.append({
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
            })
    return tasks

def _markdown_section(text: str, title: str) -> str:
    """Return one Markdown heading section without consuming peer sections."""
    lines = text.splitlines()
    wanted = title.strip().casefold()
    start = -1
    level = 0
    for index, line in enumerate(lines):
        match = re.match(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match and match.group(2).strip().casefold() == wanted:
            start = index + 1
            level = len(match.group(1))
            break
    if start < 0:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        match = re.match(r"^\s*(#{1,6})\s+", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end])

def _blueprint_field(text: str, name: str) -> str:
    matches = re.finditer(
        rf"^\s*[-*]\s*(?:\*\*)?{re.escape(name)}(?:\*\*)?\s*:\s*(.*?)\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    for match in matches:
        value = match.group(1).strip().strip("`").strip()
        if (not value or re.fullmatch(r"<[^>]+>", value) or value.casefold() in {"-", "n/a", "na", "none", "not applicable", "unresolved"}):
            continue
        return value
    return ""

def _blueprint_lifecycle_field(text: str, name: str) -> tuple[bool, str]:
    """Return whether a lifecycle field exists and its resolved value, if any."""
    match = re.search(
        rf"^\s*[-*]\s*(?:\*\*)?{re.escape(name)}(?:\*\*)?\s*:\s*(.*?)\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return False, ""
    value = match.group(1).strip().strip("`").strip()
    unresolved = (not value or bool(re.search(r"<[^>]+>", value)) or value.casefold() in {"-", "tbd", "todo", "unknown", "unresolved"})
    return True, "" if unresolved else value

def parse_blueprint_lifecycle_contract(text: str) -> dict[str, Any]:
    """Describe v0.4 intake, design, delivery, and legacy lifecycle defaults."""
    intake_text = _markdown_section(text, "Intake Decision Record")
    intake_values: dict[str, str] = {}
    missing_intake: list[str] = []
    if intake_text.strip():
        for name in INTAKE_DECISION_FIELDS:
            present, value = _blueprint_lifecycle_field(intake_text, name)
            if present and value:
                intake_values[name] = value
            else:
                missing_intake.append(name)
    design_text = _markdown_section(text, "Design Direction")
    _, applicability = _blueprint_lifecycle_field(design_text, "Applicability")
    applicability_key = applicability.casefold()
    design_required: bool | None
    if applicability_key.startswith("not applicable"):
        design_required = False
    elif applicability_key.startswith("applicable"):
        design_required = True
    else:
        design_required = None
    _, selected_direction = _blueprint_lifecycle_field(design_text, "Selected direction")
    _, selection_rationale = _blueprint_lifecycle_field(design_text, "Selection rationale")
    _, selected_constraints = _blueprint_lifecycle_field(design_text, "Selected design constraints")
    unavailable_text = _markdown_section(text, "Documented Unavailable State")
    unavailable_fields = (
        "Capabilities checked",
        "Why unavailable",
        "Written constraints used instead",
        "Effect on confidence or verification",
    )
    unavailable_values = [_blueprint_lifecycle_field(unavailable_text, name)[1] for name in unavailable_fields]
    unavailable_recorded = bool(unavailable_text.strip()) and all(unavailable_values)
    direction_selected = bool(selected_direction and not selected_direction.casefold().startswith("not applicable") and selection_rationale and selected_constraints)
    design_complete = (design_required is False or (design_required is True and (direction_selected or unavailable_recorded)))
    delivery = _markdown_section(text, "Delivery Contract")
    delivery_target = _blueprint_field(delivery, "Delivery target").casefold()
    legacy = not bool(intake_text.strip())
    return {
        "schema": "star-forge.blueprint-lifecycle.v1",
        "legacy": legacy,
        "intake": {
            "present": bool(intake_text.strip()),
            "complete": bool(intake_text.strip()) and not missing_intake,
            "decisions": intake_values,
            "unresolved": missing_intake,
        },
        "design": {
            "present": bool(design_text.strip()),
            "required": design_required,
            "complete": bool(design_complete),
            "direction_selected": direction_selected,
            "unavailable_recorded": unavailable_recorded,
        },
        "delivery": {
            "present": bool(delivery.strip()),
            "target": delivery_target or "source-only",
            "legacy_default": not bool(delivery.strip()),
        },
    }

def parse_blueprint_plan_contract(text: str) -> dict[str, Any]:
    """Extract the Blueprint fields needed for Plan v2 validation."""
    acceptance = _markdown_section(text, "Acceptance Criteria")
    ac_mentions = re.findall(
        r"^\s*[-*]\s*(?:\*\*)?(AC-[1-9][0-9]*)(?:\*\*)?\s*:",
        acceptance,
        re.MULTILINE,
    )
    counts: dict[str, int] = {}
    for ac_id in ac_mentions:
        counts[ac_id] = counts.get(ac_id, 0) + 1
    delivery = _markdown_section(text, "Delivery Contract")
    delivery_target = _blueprint_field(delivery, "Delivery target")
    platform_target = _blueprint_field(
        delivery,
        "Platform-specific target, when selected",
    )
    project_class = _blueprint_field(text, "Project class")
    target_platforms = _blueprint_field(text, "Target platforms")
    github_requested = _blueprint_field(delivery, "GitHub requested").casefold()
    return {
        "ac_ids": tuple(counts),
        "duplicate_ac_ids": tuple(sorted(ac_id for ac_id, count in counts.items() if count > 1)),
        "has_acceptance_criteria": bool(acceptance.strip()),
        "has_delivery_contract": bool(delivery.strip()),
        "delivery_target": delivery_target.casefold(),
        "platform_target": platform_target.casefold(),
        "project_class": project_class.casefold(),
        "target_platforms": target_platforms.casefold(),
        "github_requested": github_requested,
    }

def _comma_values(raw: Any) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]
def task_file_owners(raw: Any) -> list[tuple[str, str]]:
    text = str(raw or "")
    try:
        values = json.loads(text) if text.startswith("[") else None
    except json.JSONDecodeError:
        values = None
    if isinstance(values, list) and all(isinstance(value, str) for value in values):
        return [(value, "exact") for value in values]
    legacy = (item.strip() for item in re.split(r"[,;]", text))
    return [(value, "glob" if any(char in value for char in "*?[") else "exact")
            for value in legacy if value]
def _maintenance_task_owns_non_docs(task: Mapping[str, Any]) -> bool:
    raw = task.get("files")
    encoded = str(raw or "").startswith("[")
    for value, _match_kind in task_file_owners(raw):
        if not value or not encoded and value.casefold() in {"-", "n/a", "na", "none"}:
            continue
        path = Path(value)
        if (path.suffix.casefold() not in _DOCUMENT_SUFFIXES and path.name.casefold() not in _DOCUMENT_FILENAMES):
            return True
    return False

def _plan_problem(
    message: str,
    *,
    rule: str,
    task: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "severity": "high",
        "rule": rule,
        "task": task.get("id") if task else None,
        "line": int(task.get("line") or 0) if task else 0,
        "message": message,
    }

def _platform_tokens(contract: Mapping[str, Any]) -> str:
    return " ".join(str(contract.get(field) or "") for field in (
        "project_class",
        "target_platforms",
        "delivery_target",
        "platform_target",
    ))

def validate_plan_v2_contract(
    blueprint_text: str,
    tasks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate Plan v2 traceability while leaving legacy Plans readable."""
    if not tasks:
        return []
    versions = {str(task.get("plan_version") or "compatible") for task in tasks}
    has_v2 = "v2" in versions
    compatible_v2 = any(
        task.get("plan_version") == "compatible" and {"acs", "proof"}.issubset({str(header).strip().casefold()
                                                                                for header in task.get("headers") or []}) for task in tasks)
    if not has_v2 and not compatible_v2:
        return []
    problems: list[dict[str, Any]] = []
    if versions != {"v2"}:
        problems.append(
            _plan_problem(
                "Plan v2 validation requires every task table to use the exact "
                "ten-column v2 schema; legacy and v2 tables cannot be mixed",
                rule="plan-v2-schema",
            ))
    v2_tasks = [task for task in tasks if task.get("plan_version") == "v2"]
    if not v2_tasks:
        return problems
    contract = parse_blueprint_plan_contract(blueprint_text)
    blueprint_acs = set(contract["ac_ids"])
    if not contract["has_acceptance_criteria"] or not blueprint_acs:
        problems.append(_plan_problem(
            "Plan v2 requires a Blueprint Acceptance Criteria section with "
            "at least one `AC-n` criterion",
            rule="blueprint-acs-missing",
        ))
    for ac_id in contract["duplicate_ac_ids"]:
        problems.append(_plan_problem(
            f"Blueprint criterion `{ac_id}` is defined more than once",
            rule="blueprint-ac-duplicate",
        ))
    covered: set[str] = set()
    plan_proofs: set[str] = set()
    for task in v2_tasks:
        ac_values = _comma_values(task.get("acs"))
        proof_values = _comma_values(task.get("proof"))
        maintenance_values = [value for value in ac_values if value.casefold() == PLAN_MAINTENANCE_EXEMPTION]
        maintenance = bool(maintenance_values)
        if any(value == PLAN_REVIEW_REQUIRED for value in ac_values + proof_values):
            problems.append(
                _plan_problem(
                    "migrated Plan v2 fields marked REVIEW_REQUIRED must be "
                    "resolved before strict validation",
                    rule="plan-v2-review-required",
                    task=task,
                ))
        if maintenance:
            if len(ac_values) != 1:
                problems.append(
                    _plan_problem(
                        "a maintenance exemption must be the only value in ACs "
                        "and cannot claim acceptance-criterion coverage",
                        rule="maintenance-coverage",
                        task=task,
                    ))
            if str(task.get("mode") or "").casefold() != "docs":
                problems.append(_plan_problem(
                    "maintenance exemptions are limited to docs-mode tasks",
                    rule="maintenance-mode",
                    task=task,
                ))
            if _maintenance_task_owns_non_docs(task):
                problems.append(_plan_problem(
                    "a maintenance-exempt task cannot own code or "
                    "configuration files",
                    rule="maintenance-files",
                    task=task,
                ))
        elif not ac_values:
            problems.append(
                _plan_problem(
                    "Plan v2 task must reference at least one Blueprint criterion "
                    f"or `{PLAN_MAINTENANCE_EXEMPTION}`",
                    rule="task-acs-missing",
                    task=task,
                ))
        for ac_id in ac_values:
            if ac_id.casefold() == PLAN_MAINTENANCE_EXEMPTION:
                continue
            if not _AC_ID_RE.fullmatch(ac_id) or ac_id not in blueprint_acs:
                problems.append(_plan_problem(
                    f"unknown Blueprint criterion `{ac_id}`",
                    rule="task-ac-unknown",
                    task=task,
                ))
            elif not maintenance:
                covered.add(ac_id)
        if not proof_values and not maintenance:
            problems.append(_plan_problem(
                "substantive Plan v2 task must name at least one Proof kind",
                rule="task-proof-missing",
                task=task,
            ))
        seen_proofs: set[str] = set()
        for proof in proof_values:
            if proof not in PLAN_PROOF_KINDS:
                problems.append(_plan_problem(
                    f"unknown Proof kind `{proof}`; allowed values are " + ", ".join(sorted(PLAN_PROOF_KINDS)),
                    rule="task-proof-unknown",
                    task=task,
                ))
                continue
            if proof in seen_proofs:
                problems.append(_plan_problem(
                    f"duplicate Proof kind `{proof}`",
                    rule="task-proof-duplicate",
                    task=task,
                ))
            seen_proofs.add(proof)
            if not maintenance:
                plan_proofs.add(proof)
    for ac_id in sorted(blueprint_acs - covered, key=lambda value: int(value[3:])):
        problems.append(_plan_problem(
            f"Blueprint criterion `{ac_id}` is not covered by a substantive task",
            rule="blueprint-ac-uncovered",
        ))
    delivery_target = str(contract["delivery_target"])
    if not contract["has_delivery_contract"] or not delivery_target:
        problems.append(_plan_problem(
            "Plan v2 requires an explicit Blueprint Delivery Contract target",
            rule="delivery-target-missing",
        ))
        return problems
    required_proofs = {"delivery"}
    if delivery_target == "preview":
        required_proofs.add("preview")
    elif delivery_target == "private-repo":
        required_proofs.add("github")
    elif delivery_target == "package":
        required_proofs.add("package")
    elif delivery_target == "platform-specific":
        if not contract["platform_target"]:
            problems.append(_plan_problem(
                "platform-specific delivery requires a named platform target",
                rule="delivery-platform-missing",
            ))
    platforms = _platform_tokens(contract)
    has_ios = bool(re.search(r"\bios\b|iphone|ipad|app store", platforms))
    mac_platform = bool(re.search(r"\bmacos\b|\bmac\b|mac app", platforms))
    native_app = bool(
        re.search(
            r"\b(app|application|desktop|gui|native)\b",
            " ".join([
                str(contract["project_class"]),
                str(contract["delivery_target"]),
                str(contract["platform_target"]),
            ]),
        ))
    has_macos = mac_platform and native_app
    if has_ios:
        required_proofs.add("native-ios")
    if has_macos:
        required_proofs.add("native-macos")
    if contract["github_requested"] == "yes":
        required_proofs.add("github")
    for proof in sorted(required_proofs - plan_proofs):
        message = ("Plan v2 has no substantive delivery task with `delivery` proof" if proof == "delivery" else f"Blueprint contract requires Proof kind `{proof}`")
        problems.append(_plan_problem(
            message,
            rule="delivery-task-missing" if proof == "delivery" else "blueprint-proof-missing",
        ))
    if delivery_target not in {"preview", "production"} and "preview" in plan_proofs:
        problems.append(_plan_problem(
            f"Proof kind `preview` contradicts delivery target `{delivery_target}`",
            rule="delivery-proof-contradiction",
        ))
    platform_delivery = (delivery_target == "platform-specific" or delivery_target not in _GENERIC_DELIVERY_TARGETS)
    if (delivery_target != "package" and not platform_delivery and "package" in plan_proofs):
        problems.append(_plan_problem(
            f"Proof kind `package` contradicts delivery target `{delivery_target}`",
            rule="delivery-proof-contradiction",
        ))
    if "native-ios" in plan_proofs and not has_ios:
        problems.append(_plan_problem(
            "Proof kind `native-ios` is not supported by the Blueprint platforms",
            rule="blueprint-proof-contradiction",
        ))
    if "native-macos" in plan_proofs and not has_macos:
        problems.append(_plan_problem(
            "Proof kind `native-macos` is not supported by the Blueprint platforms",
            rule="blueprint-proof-contradiction",
        ))
    return problems

def encode_plan_cell(value: Any) -> str:
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
        values: list[Any] = []
        for column in columns:
            key = key_for_column[column]
            value = task.get(key)
            if column == "Task" and value is None:
                value = task.get("task", "")
            values.append(encode_plan_cell(value))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"

def migrate_plan_text(text: str) -> tuple[str, dict[str, int]]:
    """Convert every exact legacy task table to v2 without guessing mappings."""
    lines = text.splitlines()
    trailing_newline = text.endswith(("\n", "\r"))
    tables = _plan_tables(lines)
    legacy_tables = [table for table in tables if plan_table_version(table[1]) == "legacy"]
    if not legacy_tables:
        if any(plan_table_version(table[1]) == "v2" for table in tables):
            raise ContractError("Plan already uses the v2 task-table schema")
        raise ContractError("Plan has no exact eight-column legacy task table to migrate")
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
            migrated_tasks.append({
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
            })
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
        raise ContractError("Migration output must be separate from the legacy Plan")
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
    return (dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))

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
        parsed = dt.datetime.fromisoformat(candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate)
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
    if (isinstance(version, bool) or not isinstance(version, int) or version != BLUEPRINT_CONTRACT_VERSION):
        problems.append(f"contract_version must be {BLUEPRINT_CONTRACT_VERSION}")
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
        problems.append(f"{BLUEPRINT_FILE} does not match {BLUEPRINT_LOCK_FILE}")
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
        raise ContractError(f"Could not write {BLUEPRINT_LOCK_FILE}: {exc}") from exc
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
    return payload
