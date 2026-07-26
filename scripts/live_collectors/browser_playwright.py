#!/usr/bin/env python3
"""Local Playwright browser artifact supplier for Star Forge.

This collector writes task-scoped browser evidence under
`.starforge/live/<task-id>/browser/` and hands those files to the existing
`browser-run --strict` proof surface. Scenario files are declarative JSON only.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shlex
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parents[1]
STAR_FORGE_SCRIPT = SCRIPT_DIR / "star_forge.py"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from live_collectors import browser_safety, browser_scenario, common as live_common
from live_collectors.policy_data import policy_bindings, policy_list
from live_collectors.provider_engine import failed_checks, render_descriptor
from starforge import evidence

for _module, _names in (
    (browser_safety, policy_list("browser_playwright", "PUBLIC_SAFETY_BINDINGS")),
    (browser_scenario, policy_list("browser_playwright", "PUBLIC_SCENARIO_BINDINGS")),
):
    globals().update({name: getattr(_module, name) for name in _names})

globals().update(policy_bindings(
    "browser_playwright", "ACTION_CALLS", "ARTIFACT_FILENAMES", "COLLECTOR_BASE_ARGV",
    "CONSTANTS", "EVIDENCE_BLOCKER", "EVIDENCE_PROVENANCE", "EXECUTION_SUMMARY_TEMPLATE",
    "HANDOFF_BASE_ARGV", "INITIAL_SUMMARY_TEMPLATE", "IMAGE_ARTIFACT_CHECKS",
    "INTERACTION_TEMPLATE", "OUTPUT_TEMPLATE", "PARSER_ARGUMENTS",
    "REQUEST_METADATA_TEMPLATE", "REQUEST_SAFETY_TEMPLATE",
))
globals().update(CONSTANTS)


descriptor = render_descriptor


class BrowserDependencyError(Exception):
    """Raised when Playwright or a required browser is unavailable."""


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


problem = live_common.problem


def is_blocking(item: Mapping[str, Any]) -> bool:
    return bool(item.get("blocking")) or str(item.get("severity", "")).lower() in BLOCKING_SEVERITIES


merge_reports = live_common.merge_reports


def write_json_artifact(path: Path, payload: Any) -> dict[str, int]:
    return live_common.write_json(path, payload)[1]


read_json_file = live_common.read_json


def resolve_project(raw: str) -> Path:
    return live_common.assert_collector_project_safe(Path(raw).expanduser())


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
        spec = ACTION_CALLS.get(action_type)
        if not spec:
            observation.update({"passed": False, "error": f"unsupported action {action_type}"})
        else:
            call = descriptor(
                spec, selector=str(action.get("selector") or ""), text=str(action.get("text") or ""),
                key=str(action.get("key") or ""), state=str(action.get("state") or "visible"),
                ms=int(action.get("ms") or 0), timeout_ms=timeout_ms,
            )
            getattr(page, call["method"])(*call["args"], **call["kwargs"])
    except Exception as exc:
        observation.update({"passed": False, "error": str(exc)})
    return observation


def evaluate_assertion(page: Any, assertion: Mapping[str, Any], timeout_ms: int) -> dict[str, Any]:
    assertion_type = str(assertion.get("type"))
    observation: dict[str, Any] = {"type": assertion_type, "expected": dict(assertion), "passed": False}
    if assertion.get("selector"):
        observation["selector"] = assertion.get("selector")
    try:
        if assertion_type in {"visible", "hidden"}:
            actual = bool(getattr(page, f"is_{assertion_type}")(
                str(assertion["selector"]), timeout=timeout_ms
            ))
            observation.update({"actual": actual, "passed": actual is True})
        elif assertion_type in {"text_contains", "text_equals"}:
            actual_text = page.text_content(str(assertion["selector"]), timeout=timeout_ms) or ""
            expected = str(assertion["text"])
            passed = expected in actual_text if assertion_type == "text_contains" else actual_text.strip() == expected
            observation.update({"actual": actual_text, "passed": passed})
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
    return descriptor(
        REQUEST_METADATA_TEMPLATE,
        url=str(maybe_call(getattr(request, "url", "")) or ""),
        method=str(maybe_call(getattr(request, "method", "")) or ""),
        resource_type=str(maybe_call(getattr(request, "resource_type", "")) or ""),
        navigation=bool(maybe_call(getattr(request, "is_navigation_request", False))),
    )


def inspect_network_event(
    event: dict[str, Any],
    observations: list[dict[str, Any]],
    result: BrowserExecutionResult,
    context: BrowserExecutionContext,
    *,
    rule: str,
    blocked_prefix: str,
    failure_prefix: str,
) -> bool:
    error = ""
    try:
        event.update(browser_url_safety_evidence(
            str(event.get("url") or ""),
            allowed_local_origins=context.allowed_local_origins,
        ))
    except Exception as exc:
        error = str(exc)
        event.update({
            "allowed": False,
            "problems": [problem(f"{failure_prefix}: {exc}", rule=rule)],
        })
    observations.append(event)
    if event.get("allowed") is True:
        return True
    message = (
        f"{failure_prefix} for {event.get('viewport')}: {error}" if error else
        f"{blocked_prefix} for {event.get('viewport')}: {event.get('url')}"
    )
    result.problems.append(problem(message, rule=rule))
    return False


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
                    if inspect_network_event(
                        event, request_observations, result, context,
                        rule="browser-request-safety",
                        blocked_prefix="blocked unsafe browser request",
                        failure_prefix="browser request safety check failed",
                    ):
                        route.continue_()
                        return
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
                    if inspect_network_event(
                        event, websocket_observations, result, context,
                        rule="browser-websocket-safety",
                        blocked_prefix="blocked unsafe browser WebSocket",
                        failure_prefix="browser WebSocket safety check failed",
                    ):
                        connect = getattr(route, "connect_to_server", None)
                        if callable(connect):
                            connect()
                            return
                        result.problems.append(problem(
                            f"WebSocket route for {viewport_name} cannot connect through a controlled route",
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
    websocket_routing = WEBSOCKET_ROUTING_MODE if websocket_route_installed else "unavailable"
    blocked_requests = sum(1 for item in request_observations if item.get("allowed") is not True)
    blocked_websockets = sum(1 for item in websocket_observations if item.get("allowed") is not True)
    request_safety = descriptor(
        REQUEST_SAFETY_TEMPLATE, websocket_routing=websocket_routing,
        allowed_local_origins=list(context.allowed_local_origins),
        requests=request_observations, websockets=websocket_observations,
        final_urls=final_url_observations, blocked_count=blocked_requests,
        websocket_blocked_count=blocked_websockets, webrtc=dict(webrtc_control),
    )
    interaction = descriptor(
        INTERACTION_TEMPLATE, scenario=context.scenario_label, ready=ready_observations,
        actions=actions, assertions=assertions, request_safety=request_safety,
    )
    redaction_reports.append(write_json_artifact(context.paths.interaction, interaction))
    connected_ips = sorted({
        str(ip)
        for item in request_observations + websocket_observations + final_url_observations
        for ip in item.get("resolved_ips", []) if isinstance(item.get("resolved_ips"), list)
    })
    result.redaction_report = merge_reports(*redaction_reports)
    result.summary.update(descriptor(
        EXECUTION_SUMMARY_TEMPLATE, console_events=len(console_events), actions=len(actions),
        assertions=len(assertions), trace_recorded=context.trace and trace_written,
        websocket_routing=websocket_routing, webrtc_control=dict(webrtc_control),
        request_count=len(request_observations), blocked_request_count=blocked_requests,
        websocket_count=len(websocket_observations), blocked_websocket_count=blocked_websockets,
        final_urls=[str(item.get("url") or "") for item in final_url_observations if item.get("url")],
        connected_ips=connected_ips,
    ))
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
    for key, item_message, failure_message, rule in (
        ("assertions", "assertion observation must be an object", "visual assertion failed", "visual-assertion"),
        ("actions", "", "browser action failed", "browser-action"),
        ("ready", "", "ready condition failed", "ready-timeout"),
    ):
        for item in payload.get(key, []) if isinstance(payload.get(key), list) else []:
            if not isinstance(item, dict):
                if item_message:
                    problems.append(problem(item_message, rule="interaction-evidence", path=rel))
            elif item.get("passed") is not True:
                problems.append(problem(failure_message, rule=rule, path=rel))
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
    messages = failed_checks(record, IMAGE_ARTIFACT_CHECKS)
    return [problem(messages[0], rule="screenshot", path=rel)] if messages else []


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
    return ArtifactPaths(**{name: root / filename for name, filename in ARTIFACT_FILENAMES.items()})


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
    argv = descriptor(
        HANDOFF_BASE_ARGV, project=live_common.project_cli_arg(project), task=task,
        scenario=scenario_label, url=url,
        manifest=live_common.project_relative(project, live_manifest_path),
    )
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
    payload = live_common.run_trusted_command(
        handoff_argv, cwd=project, script_path=STAR_FORGE_SCRIPT
    )
    payload.pop("command_argv", None)
    payload["stdout"] = str(payload["stdout"])[-12000:]
    payload["stderr"] = str(payload["stderr"])[-12000:]
    try:
        payload["json"] = json.loads(payload["stdout"])
    except Exception:
        pass
    return payload


def collector_argv_from_args(args: argparse.Namespace) -> list[str]:
    argv = descriptor(
        COLLECTOR_BASE_ARGV, project=str(args.project), task=str(args.task),
        url=str(args.url), scenario=str(args.scenario),
    )
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
    envelope["provenance"].update(EVIDENCE_PROVENANCE)
    envelope["blockers"].append(EVIDENCE_BLOCKER)
    if envelope["verdict"] == "PASS":
        envelope["verdict"] = "DEGRADED"
    envelope_path = manifest_path.parent / EVIDENCE_FILENAME
    return envelope_path, evidence.write_envelope(
        envelope_path,
        envelope,
        project_root=project,
        verify_artifacts=True,
    )


def collect(args: argparse.Namespace, *, runner: BrowserRunner | None = None) -> tuple[int, dict[str, Any]]:
    project = resolve_project(args.project)
    task = str(args.task)
    root = live_common.live_collector_dir(project, task, COLLECTOR)
    paths = build_artifact_paths(root)
    problems: list[dict[str, Any]] = []
    unavailable: list[str] = []
    tool_versions: dict[str, Any] = {"collector": "browser-playwright.phase1"}
    summary = descriptor(
        INITIAL_SUMMARY_TEMPLATE, url=args.url, trace_requested=bool(args.trace)
    )
    artifact_reports: list[dict[str, int]] = []
    source_before = live_common.compute_source_hash(project)
    runtime_hash = live_common.compute_runtime_asset_hash(project)
    lease_runtime_hash = live_common.compute_runtime_asset_hash(project, exclude_paths=[project / ".starforge" / "runtime" / "server.json"])
    parsed_url, url_problems = validate_url(args.url)
    problems.extend(url_problems)

    scenario: dict[str, Any] | None = None
    scenario_label = "scenario"
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
        try:
            execution = (runner or _run_playwright_scenario)(context)
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

    artifact_map = {
        name: getattr(paths, name) for name in ("desktop", "mobile", "interaction", "console")
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

    record_result = record_browser_run(project, handoff_argv) if args.record else None

    payload = descriptor(
        OUTPUT_TEMPLATE, task=task, manifest=live_common.project_relative(project, manifest_path),
        evidence=live_common.project_relative(project, envelope_path),
        evidence_verdict=envelope["verdict"], degraded=degraded,
        unavailable=sorted(set(unavailable)), problems=problems, handoff_argv=handoff_argv,
        handoff_command=shlex.join(handoff_argv), recorded=bool(args.record),
    )
    if record_result is not None:
        payload["record_result"] = record_result
    code = 1 if any(is_blocking(item) for item in problems) or (
        record_result is not None and int(record_result.get("returncode") or 0) != 0
    ) else 0
    return code, payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect local Playwright browser evidence for Star Forge browser-run")
    for flags, kwargs in PARSER_ARGUMENTS:
        parser.add_argument(*flags, **kwargs)
    return parser


def main(argv: Sequence[str] | None = None, *, runner: BrowserRunner | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    code, payload = collect(args, runner=runner)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
