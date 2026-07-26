"""Acyclic persistence primitives shared by runtime proof commands."""

from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any
from .policy_data import mapping as policy_mapping, value as policy_value
from .runtime_support import LEDGER_FILE, RUNS_DIR, append_jsonl, now_utc, read_json, redact, relative_to_project, slugify, stable_json_hash, timestamp_slug, write_text
from .runtime_project import ensure_state_dirs

STORE_POLICY = policy_value("runtime_records.POLICY")

def write_run_record(project: Path, payload: dict[str, Any], *,
                     sanitized: bool = False) -> Path:
    ensure_state_dirs(project)
    record = payload if sanitized else dict(payload)
    record.setdefault("created_at", now_utc())
    record.setdefault("recorded_ns", time.time_ns())
    record.setdefault("project", str(project))
    output = record if sanitized else redact(record)
    kind = str(output.get("kind") or output.get("schema") or STORE_POLICY["kinds"]["run_default"])
    task = str(output.get("task") or "") or None
    digest = stable_json_hash({key: value for key, value in output.items() if key != "artifact"})
    parts = [timestamp_slug(), slugify(kind)]
    parts.extend(slugify(item) for item in (task, digest[:12]) if item)
    path = project / RUNS_DIR / ("-".join(parts) + ".json")
    if sanitized:
        output["artifact"] = relative_to_project(path, project)
    write_text(path, json.dumps(output, indent=2, sort_keys=True) + "\n")
    append_jsonl(project / LEDGER_FILE, policy_mapping(
        "ledger_event", schema=STORE_POLICY["schemas"]["ledger"],
        timestamp=now_utc(), event=kind, task=task,
        verdict=output.get("verdict"), summary=output.get("summary") or "",
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
