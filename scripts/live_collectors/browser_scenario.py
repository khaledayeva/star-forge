"""Declarative scenario validation for the Playwright live collector."""

from __future__ import annotations

from live_collectors.policy_data import policy_bindings, policy_list

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from live_collectors import common
from live_collectors.provider_engine import render_descriptor

live_common = common


read_json_file = common.read_json


SCENARIO_SCHEMA = "star-forge.live-browser-scenarios.v1"
DEFAULT_TIMEOUT_MS = 5000
MAX_TIMEOUT_MS = 60000
WEBRTC_DISABLE_SCRIPT = policy_list("browser_scenario", "WEBRTC_DISABLE_SCRIPT")[0]
globals().update(policy_bindings("browser_scenario",
    "FORBIDDEN_SCENARIO_KEYS", "ALLOWED_SCENARIO_KEYS", "ALLOWED_READY_KEYS",
    "ALLOWED_ACTION_KEYS", "ALLOWED_ASSERTION_KEYS", "ALLOWED_SELECTOR_STATES",
    "ALLOWED_LOAD_STATES", "ALLOWED_ACTIONS", "ALLOWED_ASSERTIONS",
    "SCENARIO_TEMPLATE",
))


class ScenarioValidationError(Exception):
    """Raised when a scenario contains unsupported or unsafe instructions."""


@dataclass(frozen=True)
class ViewportSpec:
    name: str
    width: int
    height: int
    screenshot: Path



def parse_viewports(raw_items: Sequence[str] | None, paths: Any) -> tuple[ViewportSpec, ...]:
    if not raw_items:
        return tuple(ViewportSpec(name, width, height, getattr(paths, name))
                     for name, width, height in (("desktop", 1280, 800), ("mobile", 390, 844)))
    parsed: list[ViewportSpec] = []
    seen: set[str] = set()
    screenshot_by_name = {"desktop": paths.desktop, "mobile": paths.mobile}
    for raw in raw_items:
        name_part, sep, size_part = raw.partition("=")
        if not sep:
            raise ScenarioValidationError("viewport must use NAME=WIDTHxHEIGHT")
        name = live_common.sanitize_segment(name_part, fallback="viewport").lower()
        match = re.fullmatch(r"(\d+)x(\d+)", size_part.strip())
        if not match:
            raise ScenarioValidationError("viewport size must use WIDTHxHEIGHT")
        if name in seen:
            raise ScenarioValidationError(f"duplicate viewport `{name}`")
        width = int(match.group(1))
        height = int(match.group(2))
        if width < 100 or height < 100 or width > 4096 or height > 4096:
            raise ScenarioValidationError("viewport dimensions must be between 100 and 4096")
        screenshot = screenshot_by_name.get(name, paths.desktop.parent / f"{name}.png")
        parsed.append(ViewportSpec(name, width, height, screenshot))
        seen.add(name)
    if {"desktop", "mobile"} - seen:
        raise ScenarioValidationError("browser evidence requires desktop and mobile viewports")
    return tuple(parsed)


def split_scenario_ref(raw: str) -> tuple[str, str | None]:
    path_part, sep, name = raw.partition("#")
    return path_part, name if sep else None


def scan_forbidden_keys(value: Any, path: str = "$") -> list[str]:
    if isinstance(value, dict):
        return [
            hit for key, child in value.items()
            for hit in (
                ([f"{path}.{key}"] if re.sub(r"[^a-z0-9]+", "", str(key).lower())
                 in FORBIDDEN_SCENARIO_KEYS else [])
                + scan_forbidden_keys(child, f"{path}.{key}")
            )
        ]
    if isinstance(value, list):
        return [hit for index, child in enumerate(value)
                for hit in scan_forbidden_keys(child, f"{path}[{index}]")]
    return []


def require_string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ScenarioValidationError(f"{label} must be a string")
    text = value.strip()
    if not allow_empty and not text:
        raise ScenarioValidationError(f"{label} must not be empty")
    if "\0" in text:
        raise ScenarioValidationError(f"{label} contains a null byte")
    return text


def require_timeout(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ScenarioValidationError(f"{label} must be an integer")
    if value < 1 or value > MAX_TIMEOUT_MS:
        raise ScenarioValidationError(f"{label} must be between 1 and {MAX_TIMEOUT_MS}")
    return value


def validate_selector(raw: Any, selectors: Mapping[str, str], label: str) -> str:
    selector = require_string(raw, label)
    if selector.startswith("@"):
        key = selector[1:]
        if key not in selectors:
            raise ScenarioValidationError(f"{label} references unknown selector `{selector}`")
        selector = selectors[key]
    if re.search(r"\bjavascript\s*:", selector, re.IGNORECASE):
        raise ScenarioValidationError(f"{label} must not contain javascript URLs")
    return selector


def validate_ready(raw: Any, selectors: Mapping[str, str], default_timeout_ms: int) -> dict[str, Any]:
    if raw is None:
        return {"selector": "body", "state": "visible", "timeout_ms": default_timeout_ms}
    if not isinstance(raw, dict):
        raise ScenarioValidationError("ready must be an object")
    extra = set(raw) - ALLOWED_READY_KEYS
    if extra:
        raise ScenarioValidationError("ready contains unsupported keys: " + ", ".join(sorted(extra)))
    ready: dict[str, Any] = {"timeout_ms": default_timeout_ms}
    if "timeout_ms" in raw:
        ready["timeout_ms"] = require_timeout(raw["timeout_ms"], "ready.timeout_ms")
    modes = [key for key in ("selector", "url_contains", "load_state") if key in raw]
    if len(modes) != 1:
        raise ScenarioValidationError("ready must use exactly one of selector, url_contains, or load_state")
    if "selector" in raw:
        ready["selector"] = validate_selector(raw["selector"], selectors, "ready.selector")
        ready["state"] = require_string(raw.get("state", "visible"), "ready.state")
        if ready["state"] not in ALLOWED_SELECTOR_STATES:
            raise ScenarioValidationError("ready.state is unsupported")
    elif "url_contains" in raw:
        ready["url_contains"] = require_string(raw["url_contains"], "ready.url_contains")
    else:
        ready["load_state"] = require_string(raw["load_state"], "ready.load_state")
        if ready["load_state"] not in ALLOWED_LOAD_STATES:
            raise ScenarioValidationError("ready.load_state is unsupported")
    return ready


def validated_items(
    raw: Any, label: str, allowed_keys: set[str], allowed_types: set[str]
) -> list[tuple[int, Mapping[str, Any], str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ScenarioValidationError(f"{label} must be an array")
    output: list[tuple[int, Mapping[str, Any], str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ScenarioValidationError(f"{label}[{index}] must be an object")
        extra = set(item) - allowed_keys
        if extra:
            raise ScenarioValidationError(
                f"{label}[{index}] contains unsupported keys: " + ", ".join(sorted(extra))
            )
        item_type = require_string(item.get("type"), f"{label}[{index}].type")
        if item_type not in allowed_types:
            raise ScenarioValidationError(f"{label}[{index}].type is unsupported")
        output.append((index, item, item_type))
    return output


def validate_actions(raw: Any, selectors: Mapping[str, str], default_timeout_ms: int) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for index, item, action_type in validated_items(
        raw, "actions", ALLOWED_ACTION_KEYS, ALLOWED_ACTIONS
    ):
        action: dict[str, Any] = {"type": action_type, "timeout_ms": default_timeout_ms}
        if "timeout_ms" in item:
            action["timeout_ms"] = require_timeout(item["timeout_ms"], f"actions[{index}].timeout_ms")
        if action_type in {"click", "fill", "press", "wait_for_selector"}:
            action["selector"] = validate_selector(item.get("selector"), selectors, f"actions[{index}].selector")
        if action_type == "fill":
            action["text"] = require_string(item.get("text", ""), f"actions[{index}].text", allow_empty=True)
        if action_type == "press":
            action["key"] = require_string(item.get("key"), f"actions[{index}].key")
        if action_type == "wait_for_selector":
            state = require_string(item.get("state", "visible"), f"actions[{index}].state")
            if state not in ALLOWED_SELECTOR_STATES:
                raise ScenarioValidationError(f"actions[{index}].state is unsupported")
            action["state"] = state
        if action_type == "wait_for_timeout":
            action["ms"] = require_timeout(item.get("ms"), f"actions[{index}].ms")
        actions.append(action)
    return actions


def validate_assertions(raw: Any, selectors: Mapping[str, str]) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    for index, item, assertion_type in validated_items(
        raw, "assertions", ALLOWED_ASSERTION_KEYS, ALLOWED_ASSERTIONS
    ):
        assertion: dict[str, Any] = {"type": assertion_type}
        if assertion_type in {"visible", "hidden", "text_contains", "text_equals", "count"}:
            assertion["selector"] = validate_selector(item.get("selector"), selectors, f"assertions[{index}].selector")
        if assertion_type in {"text_contains", "text_equals", "url_contains"}:
            assertion["text"] = require_string(item.get("text"), f"assertions[{index}].text")
        if assertion_type == "count":
            equals = item.get("equals")
            if not isinstance(equals, int) or isinstance(equals, bool) or equals < 0:
                raise ScenarioValidationError(f"assertions[{index}].equals must be a nonnegative integer")
            assertion["equals"] = equals
        assertions.append(assertion)
    return assertions


def validate_scenario(raw: Any, scenario_name: str | None) -> tuple[dict[str, Any], str]:
    if not isinstance(raw, dict):
        raise ScenarioValidationError("scenario file must contain a JSON object")
    if "scenarios" in raw:
        wrapper_extra = set(raw) - {"schema", "scenarios"}
        if wrapper_extra:
            raise ScenarioValidationError("scenario wrapper contains unsupported keys: " + ", ".join(sorted(wrapper_extra)))
        if raw.get("schema") != SCENARIO_SCHEMA:
            raise ScenarioValidationError(f"scenario wrapper schema must be {SCENARIO_SCHEMA}")
        scenarios = raw.get("scenarios")
        if not isinstance(scenarios, dict) or not scenarios:
            raise ScenarioValidationError("scenarios must be a nonempty object")
        selected = scenario_name
        if not selected:
            if len(scenarios) != 1:
                raise ScenarioValidationError("scenario name is required when the file contains multiple scenarios")
            selected = next(iter(scenarios))
        if selected not in scenarios:
            raise ScenarioValidationError(f"scenario `{selected}` was not found")
        raw_scenario = scenarios[selected]
        label = selected
    else:
        raw_scenario = raw
        label = scenario_name or str(raw.get("name") or "scenario")
    if not isinstance(raw_scenario, dict):
        raise ScenarioValidationError("selected scenario must be an object")
    forbidden = scan_forbidden_keys(raw_scenario)
    if forbidden:
        raise ScenarioValidationError("scenario contains forbidden keys: " + ", ".join(forbidden[:5]))
    extra = set(raw_scenario) - ALLOWED_SCENARIO_KEYS
    if extra:
        raise ScenarioValidationError("scenario contains unsupported keys: " + ", ".join(sorted(extra)))
    timeout_ms = require_timeout(raw_scenario.get("timeout_ms", DEFAULT_TIMEOUT_MS), "timeout_ms")
    selectors_raw = raw_scenario.get("selectors", {})
    if not isinstance(selectors_raw, dict):
        raise ScenarioValidationError("selectors must be an object")
    selectors: dict[str, str] = {}
    for key, value in selectors_raw.items():
        name = require_string(key, "selector name")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ScenarioValidationError(f"selector name `{name}` contains unsupported characters")
        selectors[name] = require_string(value, f"selectors.{name}")
    ready = validate_ready(raw_scenario.get("ready"), selectors, timeout_ms)
    actions = validate_actions(raw_scenario.get("actions", []), selectors, timeout_ms)
    assertions = validate_assertions(raw_scenario.get("assertions", []), selectors)
    scenario = render_descriptor(SCENARIO_TEMPLATE, {
        "name": require_string(raw_scenario.get("name", label), "name"),
        "description": require_string(raw_scenario.get("description", ""), "description", allow_empty=True),
        "timeout_ms": timeout_ms, "selectors": selectors, "ready": ready,
        "actions": actions, "assertions": assertions,
    })
    return scenario, label


def load_scenario(project: Path, raw_ref: str) -> tuple[dict[str, Any], str, Path]:
    path_part, scenario_name = split_scenario_ref(raw_ref)
    path = live_common.safe_project_path(project, path_part, must_exist=True)
    payload = read_json_file(path)
    scenario, label = validate_scenario(payload, scenario_name)
    return scenario, label, path
