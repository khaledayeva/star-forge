#!/usr/bin/env python3
"""Local Playwright browser artifact supplier for Star Forge.

This collector writes task-scoped browser evidence under
`.starforge/live/<task-id>/browser/` and hands those files to the existing
`browser-run --strict` proof surface. Scenario files are declarative JSON only.
"""

from __future__ import annotations

import argparse
import ipaddress
import importlib.metadata
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parents[1]
STAR_FORGE_SCRIPT = SCRIPT_DIR / "star_forge.py"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from live_collectors import common as live_common
from starforge import evidence


COLLECTOR = "browser"
CAPABILITY = "local-web-qa"
PREFERRED_PROVIDER = "in-app-browser"
FALLBACK_PROVIDER = "playwright-collector"
EVIDENCE_FILENAME = "evidence.v2.json"
SCENARIO_SCHEMA = "star-forge.live-browser-scenarios.v1"
DEFAULT_TIMEOUT_MS = 5000
MAX_TIMEOUT_MS = 60000
DEFAULT_VIEWPORTS = (
    ("desktop", 1280, 800),
    ("mobile", 390, 844),
)
BLOCKING_SEVERITIES = {"critical", "high", "medium"}
METADATA_HOSTS = {"metadata", "metadata.google.internal", "169.254.169.254", "169.254.170.2"}
LOCAL_HOSTNAMES = {"localhost"}
SERVICE_WORKERS_MODE = "block"
BROWSER_NETWORK_CONTROL_MODE = "leased-loopback-only"
WEBSOCKET_ROUTING_MODE = "route-web-socket"
WEBRTC_CONTROL_MODE = "init-script-disabled"
WEBRTC_DISABLE_SCRIPT = """
(() => {
  const disabled = function StarForgeDisabledWebRTC() {
    throw new Error("WebRTC disabled by Star Forge browser collector");
  };
  for (const key of ["RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel"]) {
    try {
      Object.defineProperty(window, key, {
        value: disabled,
        configurable: false,
        writable: false
      });
    } catch (_error) {}
  }
})();
"""
FORBIDDEN_SCENARIO_KEYS = {
    "auth",
    "authorization",
    "browser_profile",
    "command",
    "cookie",
    "cookies",
    "evaluate",
    "function",
    "headers",
    "java_script",
    "javascript",
    "js",
    "local_storage",
    "localstorage",
    "profile",
    "script",
    "session_storage",
    "sessionstorage",
    "shell",
    "storage_state",
    "storagestate",
    "userdata_dir",
    "userdatadir",
}
ALLOWED_SCENARIO_KEYS = {
    "actions",
    "assertions",
    "description",
    "name",
    "ready",
    "selectors",
    "timeout_ms",
}
ALLOWED_READY_KEYS = {"selector", "state", "timeout_ms", "url_contains", "load_state"}
ALLOWED_ACTION_KEYS = {"type", "selector", "text", "key", "state", "timeout_ms", "ms"}
ALLOWED_ASSERTION_KEYS = {"type", "selector", "text", "equals"}
ALLOWED_SELECTOR_STATES = {"attached", "detached", "visible", "hidden"}
ALLOWED_LOAD_STATES = {"load", "domcontentloaded", "networkidle"}
ALLOWED_ACTIONS = {"click", "fill", "press", "wait_for_selector", "wait_for_timeout"}
ALLOWED_ASSERTIONS = {"visible", "hidden", "text_contains", "text_equals", "count", "url_contains"}


class BrowserDependencyError(Exception):
    """Raised when Playwright or a required browser is unavailable."""


class ScenarioValidationError(Exception):
    """Raised when a scenario contains unsupported or unsafe instructions."""


@dataclass(frozen=True)
class ViewportSpec:
    name: str
    width: int
    height: int
    screenshot: Path


@dataclass(frozen=True)
class ArtifactPaths:
    desktop: Path
    mobile: Path
    interaction: Path
    console: Path
    trace: Path


@dataclass(frozen=True)
class BrowserExecutionContext:
    project: Path
    root: Path
    url: str
    allowed_local_origins: tuple[str, ...]
    scenario: dict[str, Any]
    scenario_label: str
    paths: ArtifactPaths
    viewports: tuple[ViewportSpec, ...]
    browser_name: str
    trace: bool


@dataclass
class BrowserExecutionResult:
    tool_versions: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    problems: list[dict[str, Any]] = field(default_factory=list)
    unavailable_capabilities: list[str] = field(default_factory=list)
    degraded: bool = False
    redaction_report: dict[str, int] = field(default_factory=dict)


BrowserRunner = Callable[[BrowserExecutionContext], BrowserExecutionResult]


def problem(message: str, *, rule: str, severity: str = "high", path: str = "", blocking: bool = True) -> dict[str, Any]:
    item: dict[str, Any] = {
        "severity": severity,
        "rule": rule,
        "message": message,
        "blocking": blocking,
    }
    if path:
        item["path"] = path
    return item


def is_blocking(item: Mapping[str, Any]) -> bool:
    return bool(item.get("blocking")) or str(item.get("severity", "")).lower() in BLOCKING_SEVERITIES


def merge_reports(*reports: Mapping[str, Any]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for report in reports:
        for key, value in report.items():
            try:
                merged[str(key)] = merged.get(str(key), 0) + int(value)
            except (TypeError, ValueError):
                continue
    return merged


def write_json_artifact(path: Path, payload: Any) -> dict[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    redacted, report = live_common.redact_sensitive_values(payload)
    path.write_text(json.dumps(redacted, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {str(key): int(value) for key, value in report.items()}


def read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_project(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def parse_viewports(raw_items: Sequence[str] | None, paths: ArtifactPaths) -> tuple[ViewportSpec, ...]:
    if not raw_items:
        return (
            ViewportSpec("desktop", 1280, 800, paths.desktop),
            ViewportSpec("mobile", 390, 844, paths.mobile),
        )
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
        screenshot = screenshot_by_name.get(name)
        if screenshot is None:
            screenshot = paths.desktop.parent / f"{name}.png"
        parsed.append(ViewportSpec(name, width, height, screenshot))
        seen.add(name)
    required = {"desktop", "mobile"}
    missing = sorted(required - seen)
    if missing:
        raise ScenarioValidationError("browser evidence requires desktop and mobile viewports")
    return tuple(parsed)


def split_scenario_ref(raw: str) -> tuple[str, str | None]:
    path_part, sep, name = raw.partition("#")
    return path_part, name if sep else None


def scan_forbidden_keys(value: Any, path: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "", str(key).lower())
            if normalized in FORBIDDEN_SCENARIO_KEYS:
                hits.append(f"{path}.{key}")
            hits.extend(scan_forbidden_keys(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(scan_forbidden_keys(child, f"{path}[{index}]"))
    return hits


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


def validate_actions(raw: Any, selectors: Mapping[str, str], default_timeout_ms: int) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ScenarioValidationError("actions must be an array")
    actions: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ScenarioValidationError(f"actions[{index}] must be an object")
        extra = set(item) - ALLOWED_ACTION_KEYS
        if extra:
            raise ScenarioValidationError(f"actions[{index}] contains unsupported keys: " + ", ".join(sorted(extra)))
        action_type = require_string(item.get("type"), f"actions[{index}].type")
        if action_type not in ALLOWED_ACTIONS:
            raise ScenarioValidationError(f"actions[{index}].type is unsupported")
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
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ScenarioValidationError("assertions must be an array")
    assertions: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ScenarioValidationError(f"assertions[{index}] must be an object")
        extra = set(item) - ALLOWED_ASSERTION_KEYS
        if extra:
            raise ScenarioValidationError(f"assertions[{index}] contains unsupported keys: " + ", ".join(sorted(extra)))
        assertion_type = require_string(item.get("type"), f"assertions[{index}].type")
        if assertion_type not in ALLOWED_ASSERTIONS:
            raise ScenarioValidationError(f"assertions[{index}].type is unsupported")
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
    scenario = {
        "name": require_string(raw_scenario.get("name", label), "name"),
        "description": require_string(raw_scenario.get("description", ""), "description", allow_empty=True),
        "timeout_ms": timeout_ms,
        "selectors": selectors,
        "ready": ready,
        "actions": actions,
        "assertions": assertions,
    }
    return scenario, label


def load_scenario(project: Path, raw_ref: str) -> tuple[dict[str, Any], str, Path]:
    path_part, scenario_name = split_scenario_ref(raw_ref)
    path = live_common.safe_project_path(project, path_part, must_exist=True)
    payload = read_json_file(path)
    scenario, label = validate_scenario(payload, scenario_name)
    return scenario, label, path


def validate_url(raw_url: str) -> tuple[urllib.parse.ParseResult, list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        problems.append(problem("browser URL must use http or https", rule="browser-url"))
    if parsed.username or parsed.password:
        problems.append(problem("browser URL must not embed credentials", rule="browser-url"))
    if not parsed.hostname:
        problems.append(problem("browser URL must include a host", rule="browser-url"))
    elif is_metadata_host(parsed.hostname):
        problems.append(problem("browser URL must not target metadata hosts", rule="browser-url"))
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lowered = f"{key}={value}".lower()
        if any(marker in lowered for marker in ("token", "secret", "password", "api_key", "apikey", "auth")):
            problems.append(problem("browser URL query appears to contain sensitive material", rule="browser-url"))
            break
    return parsed, problems


def normalize_origin(parsed: urllib.parse.ParseResult) -> str:
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme.lower()}://{host.lower()}:{port}"


def safety_equivalent_url(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme.lower() not in {"ws", "wss"}:
        return raw_url
    scheme = "https" if parsed.scheme.lower() == "wss" else "http"
    return urllib.parse.urlunparse((scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def host_is_literal_loopback(host: str) -> bool:
    ip = parse_ip(host)
    return bool(ip and ip.is_loopback)


def is_metadata_host(host: str) -> bool:
    lowered = host.lower().strip("[]")
    return lowered in METADATA_HOSTS or lowered.endswith(".metadata.google.internal")


def parse_ip(host: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def resolve_ips(host: str, port: int | None) -> tuple[list[ipaddress._BaseAddress], str | None]:
    direct = parse_ip(host)
    if direct:
        return [direct], None
    try:
        infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        return [], f"browser URL host could not be resolved: {exc}"
    ips: list[ipaddress._BaseAddress] = []
    for info in infos:
        ip = parse_ip(info[4][0])
        if ip and ip not in ips:
            ips.append(ip)
    return ips, None


def unsafe_ip_reason(ip: ipaddress._BaseAddress) -> str | None:
    if ip.is_loopback:
        return "loopback"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_link_local:
        return "link-local"
    if ip.is_private:
        return "private network"
    if ip.is_reserved:
        return "reserved"
    if ip.is_multicast:
        return "multicast"
    if not ip.is_global:
        return "non-global"
    return None


def unsafe_url_reasons(parsed: urllib.parse.ParseResult) -> tuple[bool, list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    host = parsed.hostname or ""
    if not host:
        return False, problems
    if is_metadata_host(host):
        problems.append(problem("browser URL must not target metadata hosts", rule="browser-url"))
        return False, problems
    ips, resolve_problem = resolve_ips(host, parsed.port)
    if resolve_problem:
        problems.append(problem(resolve_problem, rule="browser-url"))
        return False, problems
    requires_lease = False
    for ip in ips:
        reason = unsafe_ip_reason(ip)
        if reason == "loopback":
            requires_lease = True
            problems.append(problem(f"browser URL resolved to unsafe address {ip}: {reason} targets require a server lease", rule="server-lease"))
        elif reason:
            problems.append(problem(f"browser URL resolved to unsafe address {ip}: {reason} targets are not allowed", rule="browser-url"))
    return requires_lease, problems


def browser_url_safety_evidence(raw_url: str, *, allowed_local_origins: Sequence[str] = ()) -> dict[str, Any]:
    safety_url = safety_equivalent_url(raw_url)
    parsed, url_problems = validate_url(safety_url)
    raw_parsed = urllib.parse.urlparse(raw_url)
    host = parsed.hostname or ""
    record: dict[str, Any] = {
        "url": raw_url,
        "scheme": raw_parsed.scheme or parsed.scheme,
        "safety_url": safety_url,
        "host": host,
        "port": parsed.port or (443 if parsed.scheme == "https" else 80),
        "resolved_ips": [],
        "requires_server_lease": False,
        "allowed_by_server_lease": False,
        "literal_loopback_host": False,
        "connection_control": BROWSER_NETWORK_CONTROL_MODE,
        "evidence_source": "",
        "allowed": False,
        "problems": [],
    }
    if parsed.scheme and host:
        try:
            record["origin"] = normalize_origin(parsed)
        except ValueError:
            record["origin"] = ""
    if url_problems:
        record["problems"] = [dict(item) for item in url_problems]
        return record
    ips, resolve_problem = resolve_ips(host, parsed.port)
    if ips:
        record["resolved_ips"] = [str(ip) for ip in ips]
    if resolve_problem:
        record["problems"] = [problem(resolve_problem, rule="browser-url")]
        return record
    if not ips:
        record["problems"] = [problem("browser URL host did not resolve to an address", rule="browser-url")]
        return record

    allowed_origin = str(record.get("origin") or "")
    allowed_local = {str(origin) for origin in allowed_local_origins if str(origin)}
    literal_loopback = host_is_literal_loopback(host)
    record["literal_loopback_host"] = literal_loopback
    problems: list[dict[str, Any]] = []
    requires_lease = False
    for ip in ips:
        reason = unsafe_ip_reason(ip)
        if reason == "loopback":
            requires_lease = True
        elif reason:
            problems.append(problem(f"browser URL resolved to unsafe address {ip}: {reason} targets are not allowed", rule="browser-url"))
    record["requires_server_lease"] = requires_lease
    if problems:
        record["problems"] = problems
        return record
    if requires_lease:
        if not literal_loopback:
            record["problems"] = [
                problem(
                    f"browser URL resolved non-literal host {host} to loopback; use a literal loopback URL or a local safety proxy",
                    rule="browser-url",
                )
            ]
            return record
        if allowed_origin in allowed_local:
            record["allowed"] = True
            record["allowed_by_server_lease"] = True
            record["evidence_source"] = "leased_loopback_literal"
            return record
        record["problems"] = [
            problem(
                f"browser URL resolved to loopback origin {allowed_origin or raw_url} without a matching server lease",
                rule="server-lease",
            )
        ]
        return record
    record["problems"] = [
        problem(
            "browser URLs must use a leased loopback origin unless browser traffic is connection-controlled",
            rule="browser-url",
        )
    ]
    return record


def request_safety_problem_records(evidence: Mapping[str, Any], *, allowed_local_origins: Sequence[str], path: str = "") -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    if evidence.get("allowed") is not True:
        message = "browser request safety evidence recorded a blocked request"
        recorded_problems = evidence.get("problems")
        if isinstance(recorded_problems, list) and recorded_problems:
            first = recorded_problems[0]
            if isinstance(first, Mapping) and first.get("message"):
                message = str(first["message"])
        problems.append(problem(message, rule="browser-request-safety", path=path))
    url = str(evidence.get("url") or "")
    if not url:
        problems.append(problem("browser request safety evidence is missing a URL", rule="browser-request-safety", path=path))
        return problems
    current = browser_url_safety_evidence(url, allowed_local_origins=allowed_local_origins)
    if current.get("allowed") is not True:
        for item in current.get("problems", []) if isinstance(current.get("problems"), list) else []:
            if isinstance(item, Mapping):
                problems.append(problem(f"browser request URL is unsafe: {item.get('message')}", rule="browser-request-safety", path=path))
        if not current.get("problems"):
            problems.append(problem("browser request URL is unsafe", rule="browser-request-safety", path=path))
    recorded_ips = evidence.get("resolved_ips")
    connection_control = str(evidence.get("connection_control") or "")
    evidence_source = str(evidence.get("evidence_source") or "")
    allowed_origin_set = {str(item) for item in allowed_local_origins}
    origin = str(evidence.get("origin") or current.get("origin") or "")
    if connection_control != BROWSER_NETWORK_CONTROL_MODE:
        problems.append(problem("browser request safety evidence must record leased loopback network control", rule="browser-request-safety", path=path))
    if (
        evidence.get("allowed_by_server_lease") is not True
        or evidence_source not in {"leased_loopback", "leased_loopback_literal"}
        or origin not in allowed_origin_set
    ):
        problems.append(problem("browser request safety evidence must come from a leased loopback origin", rule="browser-request-safety", path=path))
    if not isinstance(recorded_ips, list) or not recorded_ips:
        problems.append(problem("browser request safety evidence must include resolved IPs", rule="browser-request-safety", path=path))
    else:
        for raw_ip in recorded_ips:
            ip = parse_ip(str(raw_ip))
            if ip is None:
                problems.append(problem(f"browser request safety evidence has invalid IP {raw_ip}", rule="browser-request-safety", path=path))
                continue
            reason = unsafe_ip_reason(ip)
            if reason == "loopback" and origin in allowed_origin_set:
                if evidence.get("literal_loopback_host") is not True or current.get("literal_loopback_host") is not True:
                    problems.append(problem("browser request safety evidence must use a literal loopback URL for leased loopback traffic", rule="browser-request-safety", path=path))
                continue
            if reason:
                problems.append(problem(f"browser request resolved to unsafe address {ip}: {reason}", rule="browser-request-safety", path=path))
    return problems


def validate_request_safety_payload(payload: Mapping[str, Any], *, allowed_local_origins: Sequence[str], path: str = "") -> list[dict[str, Any]]:
    evidence = payload.get("request_safety")
    if not isinstance(evidence, Mapping):
        return [problem("interaction evidence must include browser request safety evidence", rule="browser-request-safety", path=path)]
    if evidence.get("schema") != "star-forge.browser-request-safety.v1":
        return [problem("browser request safety evidence has an unsupported schema", rule="browser-request-safety", path=path)]
    problems: list[dict[str, Any]] = []
    if evidence.get("service_workers") != SERVICE_WORKERS_MODE:
        problems.append(problem("browser request safety evidence must record service_workers=block", rule="browser-request-safety", path=path))
    if evidence.get("connection_control") != BROWSER_NETWORK_CONTROL_MODE:
        problems.append(problem("browser request safety evidence must record leased loopback network control", rule="browser-request-safety", path=path))
    if evidence.get("websocket_routing") != WEBSOCKET_ROUTING_MODE:
        problems.append(problem("browser request safety evidence must record installed WebSocket routing", rule="browser-request-safety", path=path))
    webrtc = evidence.get("webrtc")
    if not isinstance(webrtc, Mapping) or webrtc.get("mode") != WEBRTC_CONTROL_MODE or webrtc.get("init_script") is not True:
        problems.append(problem("browser request safety evidence must record disabled WebRTC", rule="browser-request-safety", path=path))
    requests = evidence.get("requests")
    final_urls = evidence.get("final_urls")
    websockets = evidence.get("websockets")
    if not isinstance(requests, list) or not requests:
        problems.append(problem("browser request safety evidence must record requests", rule="browser-request-safety", path=path))
    else:
        for item in requests:
            if not isinstance(item, Mapping):
                problems.append(problem("browser request safety entry must be an object", rule="browser-request-safety", path=path))
                continue
            problems.extend(request_safety_problem_records(item, allowed_local_origins=allowed_local_origins, path=path))
    if not isinstance(final_urls, list) or not final_urls:
        problems.append(problem("browser request safety evidence must record final URLs", rule="browser-request-safety", path=path))
    else:
        for item in final_urls:
            if not isinstance(item, Mapping):
                problems.append(problem("browser final URL safety entry must be an object", rule="browser-request-safety", path=path))
                continue
            problems.extend(request_safety_problem_records(item, allowed_local_origins=allowed_local_origins, path=path))
    if not isinstance(websockets, list):
        problems.append(problem("browser request safety evidence must record WebSocket route observations", rule="browser-request-safety", path=path))
    else:
        for item in websockets:
            if not isinstance(item, Mapping):
                problems.append(problem("browser WebSocket safety entry must be an object", rule="browser-request-safety", path=path))
                continue
            problems.extend(request_safety_problem_records(item, allowed_local_origins=allowed_local_origins, path=path))
    try:
        blocked_count = int(evidence.get("blocked_count") or 0)
    except (TypeError, ValueError):
        blocked_count = 1
    if blocked_count:
        problems.append(problem("browser request safety evidence recorded blocked requests", rule="browser-request-safety", path=path))
    try:
        websocket_blocked_count = int(evidence.get("websocket_blocked_count") or 0)
    except (TypeError, ValueError):
        websocket_blocked_count = 1
    if websocket_blocked_count:
        problems.append(problem("browser request safety evidence recorded blocked WebSocket requests", rule="browser-request-safety", path=path))
    return problems


def is_local_origin(parsed: urllib.parse.ParseResult) -> bool:
    requires_lease, _problems = unsafe_url_reasons(parsed)
    return requires_lease


def is_loopback_origin(parsed: urllib.parse.ParseResult) -> bool:
    host = parsed.hostname or ""
    if not host or is_metadata_host(host):
        return False
    ips, resolve_problem = resolve_ips(host, parsed.port)
    if resolve_problem or not ips:
        return False
    return all(ip.is_loopback for ip in ips)


def pid_is_alive(pid: Any) -> bool:
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lease_project_matches(project: Path, raw: Any) -> bool:
    if isinstance(raw, dict):
        raw = raw.get("path") or raw.get("root") or raw.get("project")
    if raw is None:
        return False
    try:
        return Path(str(raw)).expanduser().resolve() == project.resolve()
    except OSError:
        return False


def validate_server_lease(
    project: Path,
    raw_lease: str | None,
    parsed_url: urllib.parse.ParseResult,
    source_hash: str,
    runtime_hash: str,
) -> tuple[Path | None, dict[str, Any] | None, list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    requires_lease, safety_problems = unsafe_url_reasons(parsed_url)
    hard_safety_problems = [item for item in safety_problems if item.get("rule") != "server-lease"]
    lease_required_problems = [item for item in safety_problems if item.get("rule") == "server-lease"]
    problems.extend(hard_safety_problems)
    if not raw_lease and not requires_lease:
        return None, None, problems
    if not raw_lease:
        problems.extend(lease_required_problems)
    if raw_lease and not is_loopback_origin(parsed_url):
        problems.append(problem("server leases are only valid for loopback browser URLs", rule="server-lease"))
    if is_loopback_origin(parsed_url) and not host_is_literal_loopback(parsed_url.hostname or ""):
        problems.append(problem("server leases require a literal loopback browser URL to avoid DNS rebinding", rule="server-lease"))
    lease_path: Path | None = None
    if raw_lease:
        try:
            lease_path = live_common.safe_project_path(project, raw_lease, must_exist=False)
        except ValueError as exc:
            return None, None, [problem(f"server lease path is unsafe: {exc}", rule="server-lease")]
    else:
        lease_path = project / ".starforge" / "runtime" / "server.json"
    rel = live_common.project_relative(project, lease_path)
    if not lease_path.exists():
        problems.append(problem("local browser URL requires a live Star Forge server lease", rule="server-lease", path=rel))
        return lease_path, None, problems
    try:
        payload = read_json_file(lease_path)
    except Exception as exc:
        problems.append(problem(f"server lease is malformed JSON: {exc}", rule="server-lease", path=rel))
        return lease_path, None, problems
    if not isinstance(payload, dict):
        problems.append(problem("server lease must be a JSON object", rule="server-lease", path=rel))
        return lease_path, None, problems
    if payload.get("schema") != "star-forge.server-lease.v1":
        problems.append(problem("server lease schema is not star-forge.server-lease.v1", rule="server-lease", path=rel))
    if not lease_project_matches(project, payload.get("project")):
        problems.append(problem("server lease project does not match current project", rule="server-lease", path=rel))
    expected_origin = normalize_origin(parsed_url)
    lease_origin = str(payload.get("origin") or payload.get("base_url") or "")
    if lease_origin:
        try:
            lease_origin = normalize_origin(urllib.parse.urlparse(lease_origin))
        except Exception:
            pass
    if lease_origin != expected_origin:
        problems.append(problem("server lease origin does not match browser URL", rule="server-lease", path=rel))
    expected_port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
    try:
        lease_port = int(payload.get("port"))
    except (TypeError, ValueError):
        lease_port = -1
    if lease_port != expected_port:
        problems.append(problem("server lease port does not match browser URL", rule="server-lease", path=rel))
    if not payload.get("command"):
        problems.append(problem("server lease must record the server command", rule="server-lease", path=rel))
    if not pid_is_alive(payload.get("pid")):
        problems.append(problem("server lease pid is not alive", rule="server-lease", path=rel))
    if str(payload.get("source_hash") or "") != source_hash:
        problems.append(problem("server lease source_hash does not match current source", rule="server-lease", path=rel))
    if str(payload.get("runtime_asset_hash") or "") != runtime_hash:
        problems.append(problem("server lease runtime_asset_hash does not match current runtime assets", rule="server-lease", path=rel))
    return lease_path, payload, problems


def maybe_call(value: Any) -> Any:
    return value() if callable(value) else value


def load_playwright() -> tuple[Any, str]:
    try:
        from playwright import sync_api
    except Exception as exc:
        raise BrowserDependencyError(f"Playwright Python package is unavailable: {exc}") from exc
    try:
        version = importlib.metadata.version("playwright")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return sync_api, version


def wait_for_url_contains(page: Any, text: str, timeout_ms: int) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() <= deadline:
        if text in str(maybe_call(getattr(page, "url", ""))):
            return
        page.wait_for_timeout(100)
    raise TimeoutError(f"URL did not contain {text!r} within {timeout_ms}ms")


def wait_for_ready(page: Any, ready: Mapping[str, Any]) -> None:
    timeout_ms = int(ready.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    if "selector" in ready:
        page.wait_for_selector(str(ready["selector"]), state=str(ready.get("state") or "visible"), timeout=timeout_ms)
    elif "url_contains" in ready:
        wait_for_url_contains(page, str(ready["url_contains"]), timeout_ms)
    else:
        page.wait_for_load_state(str(ready.get("load_state") or "domcontentloaded"), timeout=timeout_ms)


def perform_action(page: Any, action: Mapping[str, Any]) -> dict[str, Any]:
    action_type = str(action.get("type"))
    timeout_ms = int(action.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    observation: dict[str, Any] = {"type": action_type, "passed": True}
    if action.get("selector"):
        observation["selector"] = action.get("selector")
    try:
        if action_type == "click":
            page.click(str(action["selector"]), timeout=timeout_ms)
        elif action_type == "fill":
            page.fill(str(action["selector"]), str(action.get("text", "")), timeout=timeout_ms)
        elif action_type == "press":
            page.press(str(action["selector"]), str(action["key"]), timeout=timeout_ms)
        elif action_type == "wait_for_selector":
            page.wait_for_selector(str(action["selector"]), state=str(action.get("state") or "visible"), timeout=timeout_ms)
        elif action_type == "wait_for_timeout":
            page.wait_for_timeout(int(action["ms"]))
        else:
            observation.update({"passed": False, "error": f"unsupported action {action_type}"})
    except Exception as exc:
        observation.update({"passed": False, "error": str(exc)})
    return observation


def evaluate_assertion(page: Any, assertion: Mapping[str, Any], timeout_ms: int) -> dict[str, Any]:
    assertion_type = str(assertion.get("type"))
    observation: dict[str, Any] = {"type": assertion_type, "expected": dict(assertion), "passed": False}
    if assertion.get("selector"):
        observation["selector"] = assertion.get("selector")
    try:
        if assertion_type == "visible":
            actual = bool(page.is_visible(str(assertion["selector"]), timeout=timeout_ms))
            observation.update({"actual": actual, "passed": actual is True})
        elif assertion_type == "hidden":
            actual = bool(page.is_hidden(str(assertion["selector"]), timeout=timeout_ms))
            observation.update({"actual": actual, "passed": actual is True})
        elif assertion_type == "text_contains":
            actual_text = page.text_content(str(assertion["selector"]), timeout=timeout_ms) or ""
            expected = str(assertion["text"])
            observation.update({"actual": actual_text, "passed": expected in actual_text})
        elif assertion_type == "text_equals":
            actual_text = page.text_content(str(assertion["selector"]), timeout=timeout_ms) or ""
            expected = str(assertion["text"])
            observation.update({"actual": actual_text, "passed": actual_text.strip() == expected})
        elif assertion_type == "count":
            actual_count = int(page.locator(str(assertion["selector"])).count())
            expected_count = int(assertion["equals"])
            observation.update({"actual": actual_count, "passed": actual_count == expected_count})
        elif assertion_type == "url_contains":
            actual_url = str(maybe_call(getattr(page, "url", "")))
            expected = str(assertion["text"])
            observation.update({"actual": actual_url, "passed": expected in actual_url})
        else:
            observation["error"] = f"unsupported assertion {assertion_type}"
    except Exception as exc:
        observation["error"] = str(exc)
    return observation


def request_metadata(request: Any) -> dict[str, Any]:
    return {
        "url": str(maybe_call(getattr(request, "url", "")) or ""),
        "method": str(maybe_call(getattr(request, "method", "")) or ""),
        "resource_type": str(maybe_call(getattr(request, "resource_type", "")) or ""),
        "navigation": bool(maybe_call(getattr(request, "is_navigation_request", False))),
    }


def _run_playwright_scenario(context: BrowserExecutionContext) -> BrowserExecutionResult:
    sync_api, playwright_version = load_playwright()
    result = BrowserExecutionResult(tool_versions={"playwright": playwright_version})
    console_events: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []
    ready_observations: list[dict[str, Any]] = []
    request_observations: list[dict[str, Any]] = []
    websocket_observations: list[dict[str, Any]] = []
    final_url_observations: list[dict[str, Any]] = []
    redaction_reports: list[dict[str, int]] = []
    timeout_ms = int(context.scenario.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    trace_written = False
    websocket_route_installed = False
    webrtc_control: dict[str, Any] = {"mode": WEBRTC_CONTROL_MODE, "init_script": False}

    with sync_api.sync_playwright() as playwright:
        browser_type = getattr(playwright, context.browser_name, None)
        if browser_type is None:
            raise BrowserDependencyError(f"Playwright browser `{context.browser_name}` is unavailable")
        try:
            browser = browser_type.launch(headless=True)
        except Exception as exc:
            raise BrowserDependencyError(f"Playwright browser `{context.browser_name}` cannot launch: {exc}") from exc
        result.tool_versions["browser"] = maybe_call(getattr(browser, "version", "unknown"))
        try:
            for viewport in context.viewports:
                context_options = {
                    "viewport": {"width": viewport.width, "height": viewport.height},
                    "service_workers": SERVICE_WORKERS_MODE,
                }
                page_ready = {"value": False}
                browser_context = browser.new_context(**context_options)
                add_init_script = getattr(browser_context, "add_init_script", None)
                if callable(add_init_script):
                    try:
                        add_init_script(WEBRTC_DISABLE_SCRIPT)
                        webrtc_control["init_script"] = True
                    except Exception as exc:
                        result.problems.append(problem(f"WebRTC disable init script failed for {viewport.name}: {exc}", rule="browser-webrtc-safety"))
                else:
                    result.problems.append(problem("browser context does not support WebRTC disable init scripts", rule="browser-webrtc-safety"))
                if context.trace and not trace_written:
                    browser_context.tracing.start(screenshots=True, snapshots=True, sources=False)

                def on_route(route: Any, viewport_name: str = viewport.name) -> None:
                    request = getattr(route, "request", None)
                    event = request_metadata(request)
                    event["viewport"] = viewport_name
                    try:
                        safety = browser_url_safety_evidence(
                            str(event.get("url") or ""),
                            allowed_local_origins=context.allowed_local_origins,
                        )
                        event.update(safety)
                        request_observations.append(event)
                        if safety.get("allowed") is True:
                            route.continue_()
                            return
                        result.problems.append(problem(
                            f"blocked unsafe browser request for {viewport_name}: {event.get('url')}",
                            rule="browser-request-safety",
                        ))
                    except Exception as exc:
                        event["allowed"] = False
                        event["problems"] = [problem(f"browser request safety check failed: {exc}", rule="browser-request-safety")]
                        request_observations.append(event)
                        result.problems.append(problem(
                            f"browser request safety check failed for {viewport_name}: {exc}",
                            rule="browser-request-safety",
                        ))
                    route.abort("blockedbyclient")

                browser_context.route("**/*", on_route)
                page = browser_context.new_page()

                def close_websocket_route(route: Any) -> None:
                    close = getattr(route, "close", None)
                    if callable(close):
                        close()
                        return
                    abort = getattr(route, "abort", None)
                    if callable(abort):
                        abort("blockedbyclient")

                def on_websocket_route(route: Any, viewport_name: str = viewport.name) -> None:
                    event = {
                        "url": str(maybe_call(getattr(route, "url", "")) or ""),
                        "method": "GET",
                        "resource_type": "websocket",
                        "navigation": False,
                        "viewport": viewport_name,
                    }
                    try:
                        safety = browser_url_safety_evidence(
                            str(event.get("url") or ""),
                            allowed_local_origins=context.allowed_local_origins,
                        )
                        event.update(safety)
                        websocket_observations.append(event)
                        if safety.get("allowed") is True:
                            connect = getattr(route, "connect_to_server", None)
                            if callable(connect):
                                connect()
                                return
                            result.problems.append(problem(
                                f"WebSocket route for {viewport_name} cannot connect through a controlled route",
                                rule="browser-websocket-safety",
                            ))
                        else:
                            result.problems.append(problem(
                                f"blocked unsafe browser WebSocket for {viewport_name}: {event.get('url')}",
                                rule="browser-websocket-safety",
                            ))
                    except Exception as exc:
                        event["allowed"] = False
                        event["problems"] = [problem(f"browser WebSocket safety check failed: {exc}", rule="browser-websocket-safety")]
                        websocket_observations.append(event)
                        result.problems.append(problem(
                            f"browser WebSocket safety check failed for {viewport_name}: {exc}",
                            rule="browser-websocket-safety",
                        ))
                    close_websocket_route(route)

                route_web_socket = getattr(page, "route_web_socket", None) or getattr(browser_context, "route_web_socket", None)
                if callable(route_web_socket):
                    try:
                        route_web_socket("**/*", on_websocket_route)
                        websocket_route_installed = True
                    except Exception as exc:
                        result.problems.append(problem(f"browser WebSocket routing is unavailable: {exc}", rule="browser-websocket-safety"))
                else:
                    result.problems.append(problem("browser WebSocket routing is unavailable", rule="browser-websocket-safety"))

                def on_console(message: Any, viewport_name: str = viewport.name) -> None:
                    event = {
                        "viewport": viewport_name,
                        "type": str(maybe_call(getattr(message, "type", "")) or ""),
                        "text": str(maybe_call(getattr(message, "text", "")) or ""),
                        "phase": "after_ready" if page_ready["value"] else "before_ready",
                    }
                    location = maybe_call(getattr(message, "location", None))
                    if isinstance(location, dict):
                        event["location"] = {
                            key: value for key, value in location.items()
                            if key in {"url", "lineNumber", "columnNumber"}
                        }
                    console_events.append(event)

                page.on("console", on_console)
                ready_record = {"viewport": viewport.name, "passed": True, "ready": context.scenario["ready"]}
                try:
                    page.goto(context.url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception as exc:
                    ready_record.update({"passed": False, "error": str(exc)})
                    result.problems.append(problem(f"navigation failed for {viewport.name}: {exc}", rule="browser-navigation"))
                try:
                    if ready_record["passed"]:
                        wait_for_ready(page, context.scenario["ready"])
                        page_ready["value"] = True
                except Exception as exc:
                    ready_record.update({"passed": False, "error": str(exc)})
                    result.problems.append(problem(f"ready condition timed out for {viewport.name}: {exc}", rule="ready-timeout"))
                ready_observations.append(ready_record)
                if ready_record["passed"]:
                    for action in context.scenario.get("actions", []):
                        observed = perform_action(page, action)
                        observed["viewport"] = viewport.name
                        actions.append(observed)
                        if not observed.get("passed"):
                            result.problems.append(problem(f"action failed for {viewport.name}: {observed.get('error')}", rule="browser-action"))
                    for assertion in context.scenario.get("assertions", []):
                        observed = evaluate_assertion(page, assertion, timeout_ms)
                        observed["viewport"] = viewport.name
                        assertions.append(observed)
                        if not observed.get("passed"):
                            result.problems.append(problem(f"assertion failed for {viewport.name}: {observed}", rule="visual-assertion"))
                final_url = str(maybe_call(getattr(page, "url", "")) or context.url)
                final_safety = browser_url_safety_evidence(final_url, allowed_local_origins=context.allowed_local_origins)
                final_safety["viewport"] = viewport.name
                final_url_observations.append(final_safety)
                if final_safety.get("allowed") is not True:
                    result.problems.append(problem(f"unsafe final browser URL for {viewport.name}: {final_url}", rule="browser-request-safety"))
                page.screenshot(path=str(viewport.screenshot), full_page=True)
                if context.trace and not trace_written:
                    browser_context.tracing.stop(path=str(context.paths.trace))
                    trace_written = True
                browser_context.close()
        finally:
            browser.close()

    redaction_reports.append(write_json_artifact(context.paths.console, {"schema": "star-forge.browser-console.v1", "events": console_events}))
    redaction_reports.append(write_json_artifact(
        context.paths.interaction,
        {
            "schema": "star-forge.browser-interaction.v1",
            "scenario": context.scenario_label,
            "ready": ready_observations,
            "actions": actions,
            "assertions": assertions,
            "request_safety": {
                "schema": "star-forge.browser-request-safety.v1",
                "service_workers": SERVICE_WORKERS_MODE,
                "connection_control": BROWSER_NETWORK_CONTROL_MODE,
                "websocket_routing": WEBSOCKET_ROUTING_MODE if websocket_route_installed else "unavailable",
                "allowed_local_origins": list(context.allowed_local_origins),
                "requests": request_observations,
                "websockets": websocket_observations,
                "final_urls": final_url_observations,
                "blocked_count": sum(1 for item in request_observations if item.get("allowed") is not True),
                "websocket_blocked_count": sum(1 for item in websocket_observations if item.get("allowed") is not True),
                "webrtc": dict(webrtc_control),
            },
        },
    ))
    connected_ips = sorted({
        str(ip)
        for item in request_observations + websocket_observations + final_url_observations
        for ip in item.get("resolved_ips", []) if isinstance(item.get("resolved_ips"), list)
    })
    result.redaction_report = merge_reports(*redaction_reports)
    result.summary.update(
        {
            "console_events": len(console_events),
            "actions": len(actions),
            "assertions": len(assertions),
            "ephemeral_context": True,
            "trace_recorded": context.trace and trace_written,
            "service_workers": SERVICE_WORKERS_MODE,
            "connection_control": BROWSER_NETWORK_CONTROL_MODE,
            "websocket_routing": WEBSOCKET_ROUTING_MODE if websocket_route_installed else "unavailable",
            "webrtc_control": dict(webrtc_control),
            "request_count": len(request_observations),
            "blocked_request_count": sum(1 for item in request_observations if item.get("allowed") is not True),
            "websocket_count": len(websocket_observations),
            "blocked_websocket_count": sum(1 for item in websocket_observations if item.get("allowed") is not True),
            "final_urls": [str(item.get("url") or "") for item in final_url_observations if item.get("url")],
            "connected_ips": connected_ips,
        }
    )
    return result


def validate_console_artifact(path: Path, project: Path) -> list[dict[str, Any]]:
    rel = live_common.project_relative(project, path)
    try:
        payload = read_json_file(path)
    except Exception as exc:
        return [problem(f"console evidence is malformed JSON: {exc}", rule="console-evidence", path=rel)]
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return [problem("console evidence must be an object with an events array", rule="console-evidence", path=rel)]
    problems: list[dict[str, Any]] = []
    for index, event in enumerate(payload["events"]):
        if not isinstance(event, dict):
            problems.append(problem(f"console event {index} must be an object", rule="console-evidence", path=rel))
            continue
        level = str(event.get("type") or "").lower()
        phase = str(event.get("phase") or "")
        if phase == "after_ready" and level in {"warning", "error"}:
            problems.append(problem(f"console {level} after readiness: {event.get('text', '')}", rule="console-after-ready", path=rel))
    return problems


def validate_interaction_artifact(path: Path, project: Path) -> list[dict[str, Any]]:
    rel = live_common.project_relative(project, path)
    try:
        payload = read_json_file(path)
    except Exception as exc:
        return [problem(f"interaction evidence is malformed JSON: {exc}", rule="interaction-evidence", path=rel)]
    if not isinstance(payload, dict):
        return [problem("interaction evidence must be a JSON object", rule="interaction-evidence", path=rel)]
    problems: list[dict[str, Any]] = []
    for key in ("ready", "actions", "assertions"):
        if not isinstance(payload.get(key), list):
            problems.append(problem(f"interaction evidence `{key}` must be an array", rule="interaction-evidence", path=rel))
    for assertion in payload.get("assertions", []) if isinstance(payload.get("assertions"), list) else []:
        if not isinstance(assertion, dict):
            problems.append(problem("assertion observation must be an object", rule="interaction-evidence", path=rel))
            continue
        if assertion.get("passed") is not True:
            problems.append(problem("visual assertion failed", rule="visual-assertion", path=rel))
    for action in payload.get("actions", []) if isinstance(payload.get("actions"), list) else []:
        if isinstance(action, dict) and action.get("passed") is not True:
            problems.append(problem("browser action failed", rule="browser-action", path=rel))
    for ready in payload.get("ready", []) if isinstance(payload.get("ready"), list) else []:
        if isinstance(ready, dict) and ready.get("passed") is not True:
            problems.append(problem("ready condition failed", rule="ready-timeout", path=rel))
    return problems


def validate_request_safety_artifact(path: Path, project: Path, *, allowed_local_origins: Sequence[str] = ()) -> list[dict[str, Any]]:
    rel = live_common.project_relative(project, path)
    try:
        payload = read_json_file(path)
    except Exception as exc:
        return [problem(f"interaction evidence is malformed JSON: {exc}", rule="interaction-evidence", path=rel)]
    if not isinstance(payload, Mapping):
        return [problem("interaction evidence must be a JSON object", rule="interaction-evidence", path=rel)]
    return validate_request_safety_payload(payload, allowed_local_origins=allowed_local_origins, path=rel)


def validate_image_artifact(path: Path, project: Path) -> list[dict[str, Any]]:
    rel = live_common.project_relative(project, path)
    record = live_common.artifact_record(project, path, kind="screenshot")
    if not record.get("exists"):
        return [problem("viewport screenshot is missing", rule="screenshot", path=rel)]
    if not record.get("valid_image"):
        return [problem("viewport screenshot is not a decodable PNG/JPEG image", rule="screenshot", path=rel)]
    if int(record.get("bytes") or 0) <= 0:
        return [problem("viewport screenshot is empty", rule="screenshot", path=rel)]
    return []


def validate_output_artifacts(context: BrowserExecutionContext) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for viewport in context.viewports:
        problems.extend(validate_image_artifact(viewport.screenshot, context.project))
    problems.extend(validate_console_artifact(context.paths.console, context.project))
    problems.extend(validate_interaction_artifact(context.paths.interaction, context.project))
    problems.extend(validate_request_safety_artifact(
        context.paths.interaction,
        context.project,
        allowed_local_origins=context.allowed_local_origins,
    ))
    if context.trace and not context.paths.trace.exists():
        problems.append(problem("trace was requested but trace.zip was not written", rule="trace", path=live_common.project_relative(context.project, context.paths.trace)))
    return problems


def build_artifact_paths(root: Path) -> ArtifactPaths:
    return ArtifactPaths(
        desktop=root / "desktop.png",
        mobile=root / "mobile.png",
        interaction=root / "interaction.json",
        console=root / "console.json",
        trace=root / "trace.zip",
    )


def build_handoff_argv(
    project: Path,
    task: str,
    url: str,
    scenario_label: str,
    viewports: Sequence[ViewportSpec],
    paths: ArtifactPaths,
    *,
    require_server_lease: bool,
    server_lease_path: Path | None,
    live_manifest_path: Path,
    handoff_degraded: bool,
) -> list[str]:
    argv = [
        "python3",
        "scripts/star_forge.py",
        "browser-run",
        "--project",
        live_common.project_cli_arg(project),
        "--task",
        task,
        "--scenario",
        scenario_label,
        "--url",
        url,
        "--live-manifest",
        live_common.project_relative(project, live_manifest_path),
    ]
    if require_server_lease:
        if server_lease_path is not None:
            argv.extend(["--server-lease", live_common.project_relative(project, server_lease_path)])
        else:
            argv.append("--server-lease")
        argv.append("--require-server-lease")
    for viewport in viewports:
        rel = live_common.project_relative(project, viewport.screenshot)
        argv.extend(["--viewport", f"{viewport.name}={viewport.width}x{viewport.height}:{rel}"])
    argv.extend(["--interaction-evidence", live_common.project_relative(project, paths.interaction)])
    argv.extend(["--console-evidence", live_common.project_relative(project, paths.console)])
    if handoff_degraded:
        argv.append("--degraded")
    argv.append("--strict")
    return argv


def record_browser_run(project: Path, handoff_argv: Sequence[str]) -> dict[str, Any]:
    actual = list(handoff_argv)
    if actual and Path(actual[0]).name in {"python", "python3"}:
        actual[0] = sys.executable
    actual[1] = str(STAR_FORGE_SCRIPT)
    proc = subprocess.run(actual, cwd=str(project), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    payload: dict[str, Any] = {
        "returncode": proc.returncode,
        "stdout": proc.stdout[-12000:],
        "stderr": proc.stderr[-12000:],
    }
    try:
        payload["json"] = json.loads(proc.stdout)
    except Exception:
        pass
    return payload


def collector_argv_from_args(args: argparse.Namespace) -> list[str]:
    argv = [
        "python3",
        "scripts/live_collectors/browser_playwright.py",
        "--project",
        str(args.project),
        "--task",
        str(args.task),
        "--url",
        str(args.url),
        "--scenario",
        str(args.scenario),
    ]
    for viewport in args.viewport or []:
        argv.extend(["--viewport", str(viewport)])
    if args.server_lease:
        argv.extend(["--server-lease", str(args.server_lease)])
    if args.browser:
        argv.extend(["--browser", str(args.browser)])
    if args.trace:
        argv.append("--trace")
    if args.record:
        argv.append("--record")
    return argv


def write_evidence_envelope(project: Path, manifest_path: Path) -> tuple[Path, dict[str, Any]]:
    """Adapt the compatibility manifest to source-bound v2 fallback evidence."""

    manifest = read_json_file(manifest_path)
    envelope = evidence.adapt_v1_manifest(
        manifest,
        capability=CAPABILITY,
        provider=FALLBACK_PROVIDER,
    )
    provenance = dict(envelope["provenance"])
    provenance["route"] = {
        "preferred_provider": PREFERRED_PROVIDER,
        "selected_provider": FALLBACK_PROVIDER,
        "fallback": True,
        "reason": "Playwright collector was explicitly invoked for CI or headless local QA",
    }
    provenance["browser_state_policy"] = {
        "ambient_profile": False,
        "authenticated_state": False,
        "extension_dependent_state": False,
        "chrome": "reserved-for-authenticated-or-extension-dependent-state",
    }
    envelope["provenance"] = provenance
    envelope["blockers"].append(
        {
            "rule": "capability-fallback",
            "message": "In-app Browser proof was not supplied; Playwright collector evidence is the explicit fallback",
            "capability": CAPABILITY,
            "preferred_provider": PREFERRED_PROVIDER,
            "selected_provider": FALLBACK_PROVIDER,
            "blocking": False,
        }
    )
    if envelope["verdict"] == "PASS":
        envelope["verdict"] = "DEGRADED"
    envelope_path = manifest_path.parent / EVIDENCE_FILENAME
    written = evidence.write_envelope(
        envelope_path,
        envelope,
        project_root=project,
        verify_artifacts=True,
    )
    return envelope_path, written


def collect(args: argparse.Namespace, *, runner: BrowserRunner | None = None) -> tuple[int, dict[str, Any]]:
    project = resolve_project(args.project)
    task = str(args.task)
    root = live_common.live_collector_dir(project, task, COLLECTOR)
    paths = build_artifact_paths(root)
    problems: list[dict[str, Any]] = []
    unavailable: list[str] = []
    tool_versions: dict[str, Any] = {"collector": "browser-playwright.phase1"}
    summary: dict[str, Any] = {
        "url": args.url,
        "trace_requested": bool(args.trace),
        "ephemeral_context": True,
        "ambient_profile": False,
        "capability_route": {
            "capability": CAPABILITY,
            "preferred_provider": PREFERRED_PROVIDER,
            "selected_provider": FALLBACK_PROVIDER,
            "fallback": True,
            "chrome_policy": "reserved-for-authenticated-or-extension-dependent-state",
        },
    }
    artifact_reports: list[dict[str, int]] = []
    source_before = live_common.compute_source_hash(project)
    runtime_hash = live_common.compute_runtime_asset_hash(project)
    lease_runtime_hash = live_common.compute_runtime_asset_hash(project, exclude_paths=[project / ".starforge" / "runtime" / "server.json"])
    parsed_url, url_problems = validate_url(args.url)
    problems.extend(url_problems)

    scenario: dict[str, Any] | None = None
    scenario_label = "scenario"
    scenario_path: Path | None = None
    try:
        scenario, scenario_label, scenario_path = load_scenario(project, args.scenario)
        summary["scenario"] = scenario_label
        summary["scenario_path"] = live_common.project_relative(project, scenario_path)
    except Exception as exc:
        problems.append(problem(f"scenario validation failed: {exc}", rule="scenario-schema"))

    lease_path: Path | None = None
    lease_payload: dict[str, Any] | None = None
    allowed_local_origins: tuple[str, ...] = ()
    if not url_problems:
        lease_path, lease_payload, lease_problems = validate_server_lease(
            project,
            args.server_lease,
            parsed_url,
            source_before,
            lease_runtime_hash,
        )
        problems.extend(lease_problems)
        if lease_path is not None:
            summary["server_lease_path"] = live_common.project_relative(project, lease_path)
            if lease_path.exists() and lease_path.is_file():
                summary["server_lease_sha256"] = live_common.file_sha256(lease_path)
        if lease_payload is not None:
            summary["server_lease_origin"] = lease_payload.get("origin") or lease_payload.get("base_url")
            allowed_local_origins = (normalize_origin(parsed_url),)
        initial_safety = browser_url_safety_evidence(args.url, allowed_local_origins=allowed_local_origins)
        summary["network_control"] = BROWSER_NETWORK_CONTROL_MODE
        summary["service_workers"] = SERVICE_WORKERS_MODE
        summary["initial_request_safety"] = initial_safety
        if initial_safety.get("allowed") is not True:
            for item in initial_safety.get("problems", []) if isinstance(initial_safety.get("problems"), list) else []:
                if isinstance(item, Mapping):
                    problems.append(dict(item))
            if not initial_safety.get("problems"):
                problems.append(problem("browser URL is not allowed by the browser network control policy", rule="browser-url"))

    try:
        viewports = parse_viewports(args.viewport, paths)
    except ScenarioValidationError as exc:
        viewports = (
            ViewportSpec("desktop", 1280, 800, paths.desktop),
            ViewportSpec("mobile", 390, 844, paths.mobile),
        )
        problems.append(problem(f"viewport validation failed: {exc}", rule="viewport"))

    if args.trace:
        problems.append(problem(
            "Playwright traces can contain DOM content, network metadata, and typed text; review trace.zip before sharing.",
            rule="trace-redaction-warning",
            severity="info",
            blocking=False,
            path=live_common.project_relative(project, paths.trace),
        ))

    if scenario is not None and not any(is_blocking(item) for item in problems):
        context = BrowserExecutionContext(
            project=project,
            root=root,
            url=args.url,
            allowed_local_origins=allowed_local_origins,
            scenario=scenario,
            scenario_label=scenario_label,
            paths=paths,
            viewports=viewports,
            browser_name=str(args.browser),
            trace=bool(args.trace),
        )
        active_runner = runner or _run_playwright_scenario
        try:
            execution = active_runner(context)
        except BrowserDependencyError as exc:
            execution = BrowserExecutionResult(
                tool_versions={},
                problems=[problem(str(exc), rule="playwright-dependency")],
                unavailable_capabilities=["playwright-browser"],
                degraded=True,
            )
        except Exception as exc:
            execution = BrowserExecutionResult(problems=[problem(f"browser collection failed: {exc}", rule="browser-collection")])
        tool_versions.update(execution.tool_versions)
        summary.update(execution.summary)
        artifact_reports.append(execution.redaction_report)
        unavailable.extend(execution.unavailable_capabilities)
        problems.extend(execution.problems)
        if not execution.degraded:
            problems.extend(validate_output_artifacts(context))
        else:
            unavailable.extend(capability for capability in execution.unavailable_capabilities if capability not in unavailable)

    source_after = live_common.compute_source_hash(project)
    if source_after != source_before:
        problems.append(problem("source changed during browser collection", rule="source-hash"))

    degraded = bool(unavailable)
    handoff_degraded = degraded or any(is_blocking(item) for item in problems)
    manifest_target = root / "manifest.json"
    handoff_argv = build_handoff_argv(
        project,
        task,
        args.url,
        scenario_label,
        viewports,
        paths,
        require_server_lease=is_local_origin(parsed_url),
        server_lease_path=lease_path,
        live_manifest_path=manifest_target,
        handoff_degraded=handoff_degraded,
    )
    summary["browser_run_command"] = shlex.join(handoff_argv)
    summary["browser_run_argv"] = handoff_argv
    summary["blocking_problem_count"] = sum(1 for item in problems if is_blocking(item))
    if artifact_reports:
        summary["artifact_redaction_report"] = merge_reports(*artifact_reports)

    artifact_map: dict[str, Path] = {
        "desktop": paths.desktop,
        "mobile": paths.mobile,
        "interaction": paths.interaction,
        "console": paths.console,
    }
    if args.trace:
        artifact_map["trace"] = paths.trace

    manifest_path = live_common.write_live_manifest(
        project,
        task=task,
        collector=COLLECTOR,
        command_argv=collector_argv_from_args(args),
        tool_versions=tool_versions,
        artifacts=artifact_map,
        summary=summary,
        degraded=degraded,
        unavailable_capabilities=sorted(set(unavailable)),
        problems=problems,
        source_hash_before=source_before,
        source_hash_after=source_after,
        runtime_asset_hash=runtime_hash,
    )
    envelope_path, envelope = write_evidence_envelope(project, manifest_path)

    record_result: dict[str, Any] | None = None
    if args.record:
        record_result = record_browser_run(project, handoff_argv)

    payload: dict[str, Any] = {
        "schema": "star-forge.live-browser-collector-result.v1",
        "collector": COLLECTOR,
        "task": task,
        "manifest": live_common.project_relative(project, manifest_path),
        "evidence": live_common.project_relative(project, envelope_path),
        "evidence_verdict": envelope["verdict"],
        "capability": CAPABILITY,
        "preferred_provider": PREFERRED_PROVIDER,
        "provider": FALLBACK_PROVIDER,
        "fallback": True,
        "degraded": degraded,
        "unavailable_capabilities": sorted(set(unavailable)),
        "problems": problems,
        "browser_run_argv": handoff_argv,
        "browser_run_command": shlex.join(handoff_argv),
        "recorded": bool(args.record),
    }
    if record_result is not None:
        payload["record_result"] = record_result
    blocking = any(is_blocking(item) for item in problems)
    code = 1 if blocking or (record_result is not None and int(record_result.get("returncode") or 0) != 0) else 0
    return code, payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect local Playwright browser evidence for Star Forge browser-run")
    parser.add_argument("--project", default=".")
    parser.add_argument("--task", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--scenario", required=True, help="Project-relative scenario JSON path, optionally path#scenario-name")
    parser.add_argument("--viewport", action="append", help="NAME=WIDTHxHEIGHT. Desktop and mobile are required.")
    parser.add_argument("--server-lease", default="", help="Project-relative Star Forge server lease JSON for local URLs")
    parser.add_argument("--browser", default="chromium", choices=["chromium", "firefox", "webkit"])
    parser.add_argument("--trace", action="store_true", help="Opt in to Playwright trace.zip artifact")
    parser.add_argument("--record", action="store_true", help="Invoke browser-run --strict after writing artifacts")
    return parser


def main(argv: Sequence[str] | None = None, *, runner: BrowserRunner | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    code, payload = collect(args, runner=runner)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
