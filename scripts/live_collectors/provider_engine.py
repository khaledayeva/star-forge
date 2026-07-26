"""Declarative transition and artifact primitives for live collectors."""

from __future__ import annotations

from functools import reduce
from typing import Any, Mapping, Sequence
import re


def nested_value(mapping: Any, *keys: str) -> Any:
    return reduce(
        lambda current, key: current.get(key) if isinstance(current, Mapping) else None,
        keys, mapping,
    )


def first_candidate(payload: Any, paths: Sequence[str]) -> Any:
    return next((
        current for path in paths
        if (current := nested_value(payload, *path.split("."))) not in (None, "")
    ), None)


def candidate_text(payload: Any, paths: Sequence[str]) -> str:
    value = first_candidate(payload, paths)
    if isinstance(value, (dict, list)):
        import json
        return json.dumps(value, sort_keys=True)
    return "" if value is None else str(value).strip()


def normalize_alias(
    raw: Any, descriptor: Mapping[str, Any], *, missing: str = "unknown"
) -> tuple[str, bool]:
    if raw is None or raw == "":
        return missing, False
    if isinstance(raw, (int, float)):
        for threshold, label in descriptor.get("thresholds", []):
            if float(raw) >= float(threshold):
                return str(label), True
    text = re.sub(r"\s+", "-", str(raw).strip().lower().replace("_", "-"))
    for label, aliases in descriptor.get("aliases", {}).items():
        if text in aliases:
            return str(label), True
    if numeric := re.search(r"([0-9]+(?:\.[0-9]+)?)", text):
        return normalize_alias(float(numeric.group(1)), descriptor, missing=missing)
    return missing, False


def render_descriptor(
    template: Any, values: Mapping[str, Any] | None = None, **fields: Any
) -> Any:
    """Render JSON policy templates whose `$name` leaves reference trusted values."""

    values = {**dict(values or {}), **fields}
    if isinstance(template, str) and template.startswith("$"):
        key = template[1:]
        if key not in values:
            raise ValueError(f"collector payload descriptor references unknown value: {key}")
        return values[key]
    if isinstance(template, Mapping):
        return {str(key): render_descriptor(value, values) for key, value in template.items()}
    if isinstance(template, list):
        return [render_descriptor(value, values) for value in template]
    return template


def failed_checks(payload: Mapping[str, Any], checks: Sequence[Sequence[Any]]) -> list[str]:
    """Return messages for failed declarative field checks."""

    types = {"array": list, "boolean": bool, "object": dict}
    failures: list[str] = []
    for field, operator, expected, message in checks:
        actual = first_candidate(payload, [str(field)])
        try:
            passed = (
                actual == expected if operator == "equals"
                else str(actual or "") == str(expected) if operator == "string_equals"
                else isinstance(actual, types[str(expected)]) if operator == "type"
                else bool(actual) if operator == "truthy"
                else int(actual or 0) == 0 if operator == "zero_int"
                else actual in expected if operator == "one_of"
                else bool(re.fullmatch(str(expected), str(actual or ""))) if operator == "regex"
                else str(actual).startswith(".github/workflows/") and str(actual).endswith((".yml", ".yaml"))
                if operator == "workflow_path" else False
            )
        except (KeyError, TypeError, ValueError):
            passed = False
        if not passed and str(message) not in failures:
            failures.append(str(message))
    return failures
