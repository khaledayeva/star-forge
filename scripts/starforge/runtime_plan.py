"""Cohesive Star Forge runtime extracted from the CLI facade."""

from __future__ import annotations
from .policy_data import value as _policy_value
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from starforge import changes as project_changes
from starforge import contracts as project_contracts
from starforge import lifecycle as project_lifecycle
from .runtime_support import BLUEPRINT_FILE, CANONICAL_STATE, PLAN_FILE, ForgeError, read_json, read_text, write_text

PLAN_POLICY = _policy_value("runtime_plan.POLICY")
VALID_STATUSES, VALID_MODES = (set(PLAN_POLICY[key]) for key in ("statuses", "modes"))
CODE_SUFFIXES = _policy_value("runtime_plan.CODE_SUFFIXES")
CODE_FILENAMES = set(PLAN_POLICY["code_filenames"])
NOOP_COMMANDS, PRESENCE_HEADS, ALWAYS_PRESENT_PATHS = (set(PLAN_POLICY["noop"][key]) for key in ("commands", "presence_heads", "always_present_paths"))
INFRASTRUCTURE_TASK_PARTS, INFRASTRUCTURE_DOCUMENT_SUFFIXES, VISUAL_SOURCE_SUFFIXES, VISUAL_SOURCE_PARTS, PYTHON_CONTROL_PLANE_PARTS = (frozenset(PLAN_POLICY["visual"][key]) for key in ("infrastructure_parts", "infrastructure_document_suffixes", "visual_source_suffixes", "visual_source_parts", "python_control_plane_parts"))
VISUAL_TASK_RE = re.compile(PLAN_POLICY["visual"]["prose_pattern"], re.IGNORECASE)

split_row = project_contracts.split_plan_row
is_separator_row = project_contracts._is_plan_separator
task_tables = project_contracts._plan_tables
parse_tasks_from_text = project_contracts.parse_plan_tasks_text

def parse_tasks(plan_path: Path) -> list[dict[str, Any]]:
    if not plan_path.exists():
        raise ForgeError(PLAN_POLICY["errors"]["missing_plan"].format(path=plan_path))
    return parse_tasks_from_text(read_text(plan_path))

def task_plan(project: Path, task_id: str) -> tuple[Path, list[dict[str, Any]]]:
    """Resolve a task in the historical root Plan or one packet-local Plan."""
    root_plan = project / PLAN_FILE
    root_tasks = parse_tasks(root_plan) if root_plan.exists() else []
    if any(task.get("id") == task_id for task in root_tasks):
        return root_plan, root_tasks
    try:
        for packet in reversed(project_changes.list_change_packets(project)):
            packet_plan = project / packet["path"] / packet["plan_path"]
            packet_tasks = parse_tasks(packet_plan)
            if any(task.get("id") == task_id for task in packet_tasks):
                return packet_plan, packet_tasks
    except project_changes.ChangePacketError as exc:
        raise ForgeError(str(exc)) from exc
    return root_plan, root_tasks

def plan_parse_problem(plan_path: Path, tasks: Sequence[dict[str, Any]]) -> str | None:
    if tasks or not plan_path.exists():
        return None
    try:
        lines = read_text(plan_path).splitlines()
    except OSError:
        return None
    malformed = any(
        line.strip().startswith("|") and not is_separator_row(line)
        and "task" in line.strip().lower() for line in lines)
    return PLAN_POLICY["errors"]["unparsed_task_table"] if malformed else None

def _split_items(raw: object, kind: str) -> list[str]:
    value = str(raw or "")
    return [] if value.strip() in PLAN_POLICY[f"{kind}_empty"] else [item.strip() for item in re.split(PLAN_POLICY[f"{kind}_separator"], value) if item.strip()]

def parse_depends(raw: str) -> list[str]:
    return _split_items(raw, "dependency")

def task_files(task: dict[str, Any]) -> list[str]:
    raw = task.get("files")
    values = project_contracts.task_file_values(raw)
    return (values if str(raw or "").startswith("[") or
            not (len(values) == 1 and values[0].strip() in PLAN_POLICY["file_empty"]) else [])

def task_requires_real_workers(task: dict[str, Any]) -> bool:
    return str(task.get("mode") or PLAN_POLICY["default_mode"]).lower() == PLAN_POLICY["default_mode"]

def _clause_is_noop(clause: str, depth: int) -> bool:
    tokens = clause.split()
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return True
    head = tokens[0].split("/")[-1].lower()
    rest = tokens[1:]
    policy = PLAN_POLICY["noop"]
    if (head in NOOP_COMMANDS or head in PRESENCE_HEADS
            or head in policy["zero_exit_heads"] and (not rest or rest[0] == "0")
            or head == "cat" and rest == ["/dev/null"]):
        return True
    if head in {"command", "builtin"}:
        if head == "command" and rest and rest[0] in policy["command_presence_flags"]:
            return True
        if depth < policy["max_wrapper_depth"] and rest:
            return command_is_noop(" ".join(rest), depth + 1)
    if depth < policy["max_wrapper_depth"]:
        if head == "eval":
            inner = clause[clause.lower().find("eval") + 4:].strip().strip("'\"")
            return command_is_noop(inner, depth + 1)
        if head in policy["shell_heads"] and "-c" in rest:
            inner = " ".join(rest[rest.index("-c") + 1:]).strip().strip("'\"")
            return command_is_noop(inner, depth + 1)
    if head in policy["test_heads"]:
        args = [item for item in rest if item not in policy["test_closers"]]
        return (
            len(args) == 3 and args[1] in policy["equal_operators"] and args[0] == args[2]
            or len(args) == 3 and args[1] == "-ne" and args[0] != args[2]
            or len(args) == 2 and args[0] == "-z" and args[1] in {"", "''", '""'}
            or len(args) == 2 and args[0] in policy["present_path_operators"] and args[1].lower() in ALWAYS_PRESENT_PATHS
            or len(args) == 1 and "$" not in args[0] and args[0] not in {"", "''", '""'})
    return False

def _split_top_level(text: str) -> list[str]:
    """Split command clauses while preserving nested expression groups."""
    clauses: list[str] = []
    depth = start = i = 0
    while i < len(text):
        two = text[i:i + 2]
        char = text[i]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and (two in {"&&", "||"} or char in ";&"):
            clauses.append(text[start:i])
            i += 2 if two in {"&&", "||"} else 1
            start = i
            continue
        i += 1
    clauses.append(text[start:])
    return [clause.strip() for clause in clauses if clause.strip()]

def command_is_noop(command: str, depth: int = 0) -> bool:
    text = str(command or "").strip()
    if not text:
        return True
    if "'" not in text and '"' not in text:
        text = re.sub(r"\s#.*$", "", text).strip()
    for _ in range(PLAN_POLICY["noop"]["max_peels"]):
        stripped = text.strip().strip(";").strip()
        m = re.fullmatch(r"\((.*)\)", stripped, re.DOTALL) or re.fullmatch(r"\{(.*)\}", stripped, re.DOTALL)
        if m:
            text = m.group(1)
            continue
        text = stripped
        break
    clauses = _split_top_level(text)
    max_depth = PLAN_POLICY["noop"]["max_group_depth"]
    return not clauses or all(command_is_noop(clause, depth + 1)
        if depth < max_depth and (clause.startswith(("(", "{"))
                                  or _split_top_level(clause) != [clause])
        else _clause_is_noop(clause, depth) for clause in clauses)

def task_owns_code(task: dict[str, Any]) -> bool:
    return any(Path(rel).suffix.lower() in CODE_SUFFIXES
               or Path(rel).name.lower() in CODE_FILENAMES
               for rel in task_files(task))

def task_allows_noop_verification(task: dict[str, Any]) -> bool:
    # No-op verification is for genuine documentation tasks only. The old "Verify
    # cell says noop" path let the model self-grant a no-op on a code task, so it
    # is gone: a docs task that owns code files still needs a real verify.
    return str(task.get("mode") or "").lower() == "docs" and not task_owns_code(task)

def normalize_command(text: str) -> str:
    return str(text or "").strip()

def task_verify_command(task: dict[str, Any]) -> str:
    return str(task.get("verify") or "").strip()

def task_proof_kinds(task: Mapping[str, Any]) -> set[str]:
    return {item.strip().casefold() for item in str(task.get("proof") or "").split(",") if item.strip() and item.strip() != "-"}

def task_file_is_infrastructure(raw_path: str) -> bool:
    if not raw_path:
        return False
    path = PurePosixPath(raw_path)
    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    visual = PLAN_POLICY["visual"]
    return bool(parts & INFRASTRUCTURE_TASK_PARTS
        or path.suffix.casefold() in INFRASTRUCTURE_DOCUMENT_SUFFIXES
        or name.startswith("test_")
        or any(marker in name for marker in visual["infrastructure_name_markers"])
        or name in visual["infrastructure_names"])

def task_files_are_infrastructure(task: Mapping[str, Any]) -> bool:
    files = task_files(dict(task))
    return bool(files) and all(task_file_is_infrastructure(path) for path in files)

def _task_source_paths(task: Mapping[str, Any]) -> list[PurePosixPath]:
    return [PurePosixPath(raw) for raw in task_files(dict(task)) if not task_file_is_infrastructure(raw)]

def task_owns_visual_source(task: Mapping[str, Any]) -> bool:
    swift_suffixes = PLAN_POLICY["visual"]["swift_view_suffixes"]
    def is_visual(path: PurePosixPath) -> bool:
        suffix = path.suffix.casefold()
        parts = {part.casefold() for part in path.parts[:-1]}
        return bool(suffix in VISUAL_SOURCE_SUFFIXES
            or parts & VISUAL_SOURCE_PARTS
            and (suffix in CODE_SUFFIXES or path.name.casefold() in CODE_FILENAMES)
            or suffix == ".swift"
            and any(path.stem.casefold().endswith(item) for item in swift_suffixes))
    return any(is_visual(path) for path in _task_source_paths(task))

def task_files_are_python_control_plane(task: Mapping[str, Any]) -> bool:
    """Identify nonvisual Python tooling without consulting task prose."""
    owned = _task_source_paths(task)
    def is_control_plane(path: Path) -> bool:
        parts = {part.casefold() for part in path.parts[:-1]}
        return (path.suffix.casefold() == ".py" and bool(parts & PYTHON_CONTROL_PLANE_PARTS)
                and not bool(parts & VISUAL_SOURCE_PARTS))
    return bool(owned) and all(is_control_plane(path) for path in owned)

def task_is_visual(task: dict[str, Any]) -> bool:
    proof_kinds = task_proof_kinds(task)
    visual = PLAN_POLICY["visual"]
    text = " ".join(str(task.get(key, "")) for key in visual["prose_fields"])
    establishes_visual = visual["browser_proof"] in proof_kinds or task_owns_visual_source(task)
    establishes_nonvisual = (
        task_files_are_infrastructure(task)
        or str(task.get("plan_version") or "").casefold() == visual["plan_v2"]
        and bool(proof_kinds)
        or task_files_are_python_control_plane(task))
    return True if establishes_visual else False if establishes_nonvisual else bool(VISUAL_TASK_RE.search(text))

def _task_problem(task: Mapping[str, Any], name: str, **values: object) -> dict[str, Any]:
    severity, message = PLAN_POLICY["validation"]["problems"][name]
    return {"severity": severity, "task": task["id"], "line": task["line"],
            "message": message.format(**values)}

def validate_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    ids = {task["id"] for task in tasks}
    policy = PLAN_POLICY["validation"]
    for task in tasks:
        status = task["status"]
        if status not in VALID_STATUSES:
            problems.append(_task_problem(task, "invalid_status", status=status))
        mode = str(task.get("mode") or "").lower()
        if mode not in VALID_MODES:
            problems.append(_task_problem(task, "invalid_mode", mode=mode))
        if status in policy["verify_required_statuses"] and not task_allows_noop_verification(task):
            verify_cell = str(task.get("verify") or "").strip()
            if not verify_cell:
                problems.append(_task_problem(task, "missing_verify"))
            elif command_is_noop(verify_cell):
                problems.append(_task_problem(task, "noop_verify", verify=verify_cell))
        missing_evidence = not task["evidence"] or task["evidence"] == "-"
        evidence_problem = policy["evidence_required_statuses"].get(status)
        if evidence_problem and missing_evidence:
            problems.append(_task_problem(task, evidence_problem))
        for dep in parse_depends(task["depends"]):
            if dep not in ids:
                problems.append(_task_problem(
                    task, "unknown_dependency", dependency=dep))
    return problems

def plan_contract_mode(tasks: Sequence[dict[str, Any]]) -> str:
    modes = PLAN_POLICY["contract_modes"]
    versions = {str(task.get("plan_version") or modes["default_version"]) for task in tasks}
    if len(versions) != 1:
        return modes["empty"] if not versions else modes["multiple"]
    version = next(iter(versions))
    return modes.get(version, version)

def _optional_text(path: Path) -> str:
    try:
        return read_text(path) if path.exists() else ""
    except (OSError, UnicodeError):
        return ""

def validate_project_plan_contract(project: Path, tasks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply strict traceability only when Plan v2 is present."""
    return project_contracts.validate_plan_v2_contract(
        _optional_text(project / BLUEPRINT_FILE), tasks)

def ready_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    policy = PLAN_POLICY["readiness"]
    complete = {task["id"] for task in tasks if task["status"] == policy["complete_status"]}
    return [task for task in tasks if task["status"] in policy["candidate_statuses"]
            and all(dep in complete for dep in parse_depends(task["depends"]))]

def all_tasks_complete(tasks: Sequence[dict[str, Any]]) -> bool:
    return bool(tasks) and all(task.get("status") == "complete" for task in tasks)

def plan_is_placeholder(tasks: list[dict[str, Any]]) -> bool:
    if len(tasks) != 1:
        return not tasks
    task, policy = tasks[0], PLAN_POLICY["placeholder"]
    return task["id"] == policy["task_id"] and policy["description_marker"] in task["description"].lower()

def task_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    return {status: sum(task["status"] == status for task in tasks) for status in dict.fromkeys(task["status"] for task in tasks)}

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
            lines[line_idx] = "| " + " | ".join(cells[:len(headers)]) + " |"
            write_text(plan_path, "\n".join(lines) + "\n")
            return
    raise ForgeError(PLAN_POLICY["errors"]["task_not_found"].format(task=task_id, path=plan_path))

def append_plan_task(plan_path: Path, row: dict[str, str]) -> bool:
    """Append a task row to the first task table. Returns False if no table."""
    lines = read_text(plan_path).splitlines()
    tables = list(task_tables(lines))
    if not tables:
        return False
    _, headers, _, end = tables[0]
    cells = [row.get(name.lower(), "") for name in headers]
    lines.insert(end, "| " + " | ".join(cells) + " |")
    write_text(plan_path, "\n".join(lines) + "\n")
    return True

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
    return project_contracts.parse_blueprint_lifecycle_contract(_optional_text(project / BLUEPRINT_FILE))

def _blocked_gate(base: dict[str, Any], status: str, blockers: Sequence[str]) -> dict[str, Any]:
    return base | {"status": status, "satisfied": False, "blockers": list(blockers)}

def lifecycle_gate_state(project: Path, *, kind: str, required: bool,
                         current_source_hash: str | None,
                         expected_delivery_target: str = "") -> dict[str, Any]:
    """Load and evaluate one lifecycle gate without mutating its proof files."""
    policy = PLAN_POLICY["lifecycle"]
    descriptor = policy["kinds"][kind]
    contract_rel, evidence_rel = descriptor["contract"], descriptor["evidence"]
    base = {"required": required, "contract_path": contract_rel, "evidence_path": evidence_rel}
    if not required:
        return base | dict(policy["compatible_result"])
    paths = {rel: project / rel for rel in (contract_rel, evidence_rel)}
    missing = [rel for rel, path in paths.items() if not path.exists()]
    if missing:
        blockers = [policy["missing_message"].format(path=item) for item in missing]
        return _blocked_gate(base, policy["missing_status"], blockers)
    try:
        contract, evidence = (read_json(paths[rel]) for rel in (contract_rel, evidence_rel))
    except Exception as exc:
        message = policy["unreadable_message"].format(kind=kind, error=exc)
        return _blocked_gate(base, policy["blocked_status"], [message])
    evaluator = getattr(project_lifecycle, f"evaluate_{kind}")
    gate = evaluator(contract, evidence, current_source_hash=str(current_source_hash or ""))
    if kind == "foundation" and not getattr(gate, descriptor["satisfied_field"]):
        try:
            prior = read_json(project / CANONICAL_STATE) if (project / CANONICAL_STATE).exists() else {}
        except Exception:
            prior = {}
        prior_gate = prior.get("foundation") if isinstance(prior, dict) else {}
        evidence_source = str(evidence.get("source_hash") or "") if isinstance(evidence, Mapping) else ""
        historical = evaluator(contract, evidence, current_source_hash=evidence_source)
        transition_bound = (
            isinstance(prior, dict)
            and prior.get("schema") == "star-forge.state.v3"
            and prior.get("project") == str(project.resolve())
            and prior.get("phase") in {"build", "review", "deliver", "done", "amend"}
            and isinstance(prior_gate, dict)
            and prior_gate == base | historical.to_dict() | {"satisfied": True}
        )
        if transition_bound and getattr(historical, descriptor["satisfied_field"]):
            gate = historical
    payload = gate.to_dict()
    satisfied = getattr(gate, descriptor["satisfied_field"])
    if kind == "delivery":
        actual_target = str((contract.get("target") or {}).get("kind") or "")
        if expected_delivery_target and actual_target != expected_delivery_target:
            payload["status"] = policy["blocked_status"]
            payload.setdefault("blockers", []).append(policy["target_mismatch"])
            satisfied = False
    return base | payload | {"satisfied": satisfied}

def scope_hash(project: Path) -> str | None:
    state = blueprint_lock_state(project)
    digest = str(state.get("current_sha256") or "") if state.get("approved") else ""
    return digest[:16] if len(digest) == 64 else None

__all__ = tuple(name for name in globals() if not name.startswith("__"))
