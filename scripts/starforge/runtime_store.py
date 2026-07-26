"""Acyclic persistence primitives shared by runtime proof commands."""

from __future__ import annotations
import time
from pathlib import Path
from typing import Any
from .policy_data import mapping as policy_mapping, value as policy_value
from .runtime_support import LEDGER_FILE, RUNS_DIR, append_jsonl, now_utc, read_json, redact, relative_to_project, slugify, stable_json_hash, timestamp_slug, write_json
from .runtime_project import ensure_state_dirs

STORE_POLICY = policy_value("runtime_records.POLICY")

def run_record_path(project: Path, *, kind: str, task: str | None = None,
                    digest: str | None = None) -> Path:
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
    kind = str(payload.get("kind") or payload.get("schema") or STORE_POLICY["kinds"]["run_default"])
    task = str(payload.get("task") or "") or None
    digest = stable_json_hash(redact({key: value for key, value in payload.items() if key != "artifact"}))
    path = run_record_path(project, kind=kind, task=task, digest=digest)
    write_json(path, payload)
    append_jsonl(project / LEDGER_FILE, policy_mapping(
        "ledger_event", schema=STORE_POLICY["schemas"]["ledger"],
        timestamp=now_utc(), event=kind, task=task,
        verdict=payload.get("verdict"), summary=payload.get("summary") or "",
        artifacts=[relative_to_project(path, project)]))
    return path

def load_run_records(project: Path, *, kind: str, task: str | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((project / RUNS_DIR).glob("*.json")):
        try:
            payload = read_json(path)
        except Exception:
            continue
        if payload.get("kind") == kind and (task is None or payload.get("task") == task):
            payload["_artifact"] = relative_to_project(path, project)
            records.append(payload)
    return records
