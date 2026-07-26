"""Read-only inspection of historical Star Forge v0.3 project artifacts."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, Mapping

from . import changes, contracts, evidence
LEGACY_PROJECT_INSPECTION_SCHEMA = "star-forge.legacy-v03-inspection.v1"
LEGACY_REVIEW_SCHEMA = "star-forge.review.v2"
LEGACY_COMPLETION_SCHEMA = "star-forge.complete-task.v1"
LEGACY_PROOF_SCHEMA = "star-forge.proof.v1"
LEGACY_STATE_SCHEMA = "star-forge.state.v3"

class LegacyMigrationError(ValueError):
    """A legacy project cannot be inspected without ambiguity."""

def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise LegacyMigrationError(f"legacy artifact escapes the project: {path}") from exc

def _file_problem(path: Path, root: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return f"{_relative(path, root)} is missing"
    except OSError as exc:
        return f"{_relative(path, root)} cannot be inspected: {exc}"
    if stat.S_ISLNK(mode):
        return f"{_relative(path, root)} must not be a symlink"
    if not stat.S_ISREG(mode):
        return f"{_relative(path, root)} must be a regular file"
    return ""

def _read_json_object(
    path: Path,
    root: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    problem = _file_problem(path, root)
    if problem:
        return None, problem
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"{_relative(path, root)} cannot be read as JSON: {exc}"
    if not isinstance(payload, dict):
        return None, f"{_relative(path, root)} must contain a JSON object"
    return payload, None

def _plan_snapshot(root: Path, problems: list[str]) -> dict[str, Any]:
    plan_path = root / contracts.PLAN_FILE
    problem = _file_problem(plan_path, root)
    if problem:
        problems.append(problem)
        return {
            "path": contracts.PLAN_FILE,
            "version": "missing",
            "tasks": [],
        }
    try:
        text = plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        problems.append(f"{contracts.PLAN_FILE} cannot be read: {exc}")
        return {
            "path": contracts.PLAN_FILE,
            "version": "unreadable",
            "tasks": [],
        }
    tasks = contracts.parse_plan_tasks_text(text)
    versions = sorted({str(task.get("plan_version") or "compatible") for task in tasks})
    version = versions[0] if len(versions) == 1 else ("mixed" if versions else "unknown")
    return {
        "path":
        contracts.PLAN_FILE,
        "version":
        version,
        "tasks": [{
            "id": task["id"],
            "status": task["status"],
            "mode": task["mode"],
            "files": task["files"],
            "depends": task["depends"],
            "acs": task["acs"],
            "proof": task["proof"],
            "verify": task["verify"],
            "evidence": task["evidence"],
            "description": task["description"],
            "line": task["line"],
            "plan_version": task["plan_version"],
        } for task in tasks],
    }

def _review_snapshot(root: Path, problems: list[str]) -> dict[str, Any]:
    reviews_root = root / ".starforge" / "reviews"
    if not reviews_root.exists():
        return {"findings": [], "merged": []}
    if reviews_root.is_symlink() or not reviews_root.is_dir():
        problems.append(".starforge/reviews must be a real directory")
        return {"findings": [], "merged": []}
    finding_records: list[dict[str, Any]] = []
    merged_records: list[dict[str, Any]] = []
    for path in sorted(reviews_root.glob("*/*.json")):
        payload, problem = _read_json_object(path, root)
        if problem:
            problems.append(problem)
            continue
        assert payload is not None
        record = {
            "path": _relative(path, root),
            "scope": path.parent.name,
            "historical": True,
            "payload": payload,
        }
        if path.name == "merged.json":
            if payload.get("schema") != LEGACY_REVIEW_SCHEMA:
                problems.append(f"{record['path']} does not use legacy review schema "
                                f"{LEGACY_REVIEW_SCHEMA}")
            record.update(
                schema=payload.get("schema"),
                source_hash=payload.get("source_hash"),
                reviewer_roles=list(payload.get("reviewer_roles") or []),
                finding_count=len(payload.get("findings") or []),
                open_finding_count=len(payload.get("fix_queue") or []),
            )
            merged_records.append(record)
            continue
        if not path.name.endswith(".findings.json"):
            continue
        findings = payload.get("findings")
        if (not isinstance(payload.get("role"), str) or not isinstance(payload.get("source_hash"), str) or not isinstance(findings, list)):
            problems.append(f"{record['path']} is not a readable v0.3 reviewer findings file")
        record.update(
            role=payload.get("role"),
            source_hash=payload.get("source_hash"),
            finding_count=len(findings) if isinstance(findings, list) else None,
        )
        finding_records.append(record)
    return {"findings": finding_records, "merged": merged_records}

def _completion_snapshot(root: Path, problems: list[str]) -> dict[str, Any]:
    state_root = root / ".starforge" / "state"
    task_records: list[dict[str, Any]] = []
    if state_root.exists() and not state_root.is_symlink() and state_root.is_dir():
        for path in sorted(state_root.glob("complete-task-*.json")):
            payload, problem = _read_json_object(path, root)
            if problem:
                problems.append(problem)
                continue
            assert payload is not None
            if payload.get("schema") != LEGACY_COMPLETION_SCHEMA:
                problems.append(f"{_relative(path, root)} does not use legacy completion schema "
                                f"{LEGACY_COMPLETION_SCHEMA}")
            snapshot = payload.get("source_snapshot")
            task_records.append({
                "path": _relative(path, root),
                "historical": True,
                "schema": payload.get("schema"),
                "task": payload.get("task"),
                "verdict": payload.get("verdict"),
                "source_hash": (snapshot.get("source_hash") if isinstance(snapshot, Mapping) else None),
                "payload": payload,
            })
    elif state_root.exists():
        problems.append(".starforge/state must be a real directory")
    proof_path = root / ".starforge" / "final" / "proof.json"
    proof: dict[str, Any] | None = None
    if proof_path.exists() or proof_path.is_symlink():
        payload, problem = _read_json_object(proof_path, root)
        if problem:
            problems.append(problem)
        elif payload is not None:
            if payload.get("schema") != LEGACY_PROOF_SCHEMA:
                problems.append(f"{_relative(proof_path, root)} does not use legacy proof schema "
                                f"{LEGACY_PROOF_SCHEMA}")
            proof = {
                "path": _relative(proof_path, root),
                "historical": True,
                "schema": payload.get("schema"),
                "source_hash": payload.get("source_hash"),
                "scope_hash": payload.get("scope_hash"),
                "verdict": payload.get("verdict"),
                "payload": payload,
            }
    return {"task_records": task_records, "final_proof": proof}

def _state_snapshot(root: Path, problems: list[str]) -> dict[str, Any] | None:
    path = root / ".starforge" / "state.json"
    if not path.exists() and not path.is_symlink():
        return None
    payload, problem = _read_json_object(path, root)
    if problem:
        problems.append(problem)
        return None
    assert payload is not None
    if payload.get("schema") != LEGACY_STATE_SCHEMA:
        problems.append(f"{_relative(path, root)} does not use legacy state schema "
                        f"{LEGACY_STATE_SCHEMA}")
    return {
        "path": _relative(path, root),
        "historical": True,
        "schema": payload.get("schema"),
        "phase": payload.get("phase"),
        "source_hash": payload.get("source_hash"),
        "scope_hash": payload.get("scope_hash"),
        "payload": payload,
    }

def _evidence_snapshot(root: Path, problems: list[str]) -> list[dict[str, Any]]:
    live_root = root / ".starforge" / "live"
    if not live_root.exists():
        return []
    if live_root.is_symlink() or not live_root.is_dir():
        problems.append(".starforge/live must be a real directory")
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(live_root.glob("*/*/manifest.json")):
        payload, problem = _read_json_object(path, root)
        if problem:
            problems.append(problem)
            continue
        assert payload is not None
        try:
            adapted = evidence.read_evidence(
                path,
                project_root=root,
                verify_artifacts=True,
            )
        except evidence.EvidenceError as exc:
            problems.append(f"{_relative(path, root)} cannot be adapted: {exc}")
            continue
        records.append({
            "path": _relative(path, root),
            "historical": True,
            "source_schema": payload.get("schema"),
            "adapted": adapted,
        })
    return records

def inspect_legacy_project(project: str | Path) -> dict[str, Any]:
    """Return a deterministic in-memory view without rewriting legacy artifacts."""
    root = Path(project).resolve()
    if not root.is_dir():
        raise LegacyMigrationError(f"legacy project is not a directory: {root}")
    problems: list[str] = []
    plan = _plan_snapshot(root, problems)
    try:
        amendments = changes.legacy_amendment_history(root)
    except changes.ChangePacketError as exc:
        problems.append(str(exc))
        amendments = []
    return {
        "schema": LEGACY_PROJECT_INSPECTION_SCHEMA,
        "project": str(root),
        "plan": plan,
        "amendments": amendments,
        "reviews": _review_snapshot(root, problems),
        "completions": _completion_snapshot(root, problems),
        "state": _state_snapshot(root, problems),
        "evidence": _evidence_snapshot(root, problems),
        "problems": problems,
    }
