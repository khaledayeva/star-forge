"""Load immutable runtime policy tables from one package-owned data file."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
_POLICY_TYPES = {"dict": dict, "frozenset": frozenset, "set": set, "tuple": tuple, "list": list}
def _decode(item: Any) -> Any:
    if isinstance(item, list):
        return [_decode(value) for value in item]
    if not isinstance(item, dict):
        return item
    if "$type" not in item:
        return {key: _decode(value) for key, value in item.items()}
    try:
        return _POLICY_TYPES[item["$type"]](_decode(value) for value in item["$items"])
    except KeyError as exc:
        raise ValueError(f"unsupported runtime policy type: {item['$type']}") from exc

@lru_cache(maxsize=1)
def _tables() -> dict[str, Any]:
    payload = json.loads(Path(__file__).with_name("runtime_policy.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("runtime policy data must contain a JSON object")
    return payload

def value(name: str) -> Any:
    try:
        return _decode(_tables()[name])
    except KeyError as exc:
        raise ValueError(f"missing runtime policy table: {name}") from exc

def project(name: str, /, *sources: dict[str, Any], **values: Any) -> dict[str, Any]:
    record_fields = value(f"records.{name}")["fields"]
    inherited = {field: source[field] for source in reversed(sources) for field in record_fields if field in source}
    return record(name, **{**inherited, **values})
def _ordered(label: str, descriptor: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    fields = descriptor["fields"]
    missing, extra = [field for field in fields if field not in values], [field for field in values if field not in fields]
    if missing or extra:
        raise ValueError(f"{label} fields mismatch; missing={missing}, extra={extra}")
    return {field: values[field] for field in fields}

def mapping(name: str, /, **values: Any) -> dict[str, Any]:
    descriptor = value(f"mappings.{name}")
    return _ordered(f"mapping {name}", descriptor, {**descriptor["defaults"], **values})

def record(name: str, /, **values: Any) -> dict[str, Any]:
    descriptor = value(f"records.{name}")
    return {"schema": descriptor["schema"], **_ordered(f"record {name}", descriptor, values)}
for _name, _doc in value("policy_data.DOCS").items():
    globals()[_name].__doc__ = _doc
