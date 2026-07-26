#!/usr/bin/env python3
"""Focused tests for the Phase 1 Playwright browser collector.

Plain-python suite. Run with: python3 tests/test_live_browser_playwright.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_SCRIPT = ROOT / "scripts" / "live_collectors" / "browser_playwright.py"
SPEC = importlib.util.spec_from_file_location("browser_playwright", COLLECTOR_SCRIPT)
assert SPEC and SPEC.loader
browser_playwright = importlib.util.module_from_spec(SPEC)
sys.modules["browser_playwright"] = browser_playwright
SPEC.loader.exec_module(browser_playwright)

STAR_SCRIPT = ROOT / "scripts" / "star_forge.py"
STAR_SPEC = importlib.util.spec_from_file_location("star_forge", STAR_SCRIPT)
assert STAR_SPEC and STAR_SPEC.loader
star_forge = importlib.util.module_from_spec(STAR_SPEC)
STAR_SPEC.loader.exec_module(star_forge)

from live_collectors import common as live_common
from starforge import evidence


TASK = "SF-1"
LOCAL_URL = "http://127.0.0.1:4173"
SCENARIO_REL = "fixtures/sloppy-web-app/live-browser-scenarios.json"
FAKE_CONTEXT_OPTIONS: list[dict[str, Any]] = []


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_png(path: Path, width: int = 64, height: int = 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + ihdr + b"\x00\x00\x00\x00")
    return path


def init_project(project: Path) -> None:
    write_text(project / "src" / "app.js", "export const app = 'browser collector';\n")
    fixture_payload = json.loads((ROOT / SCENARIO_REL).read_text(encoding="utf-8"))
    write_json(project / SCENARIO_REL, fixture_payload)


def write_lease(
    project: Path,
    *,
    url: str = LOCAL_URL,
    pid: int | None = None,
    source_hash: str | None = None,
    runtime_hash: str | None = None,
    origin: str | None = None,
) -> Path:
    parsed, problems = browser_playwright.validate_url(url)
    assert not problems
    path = live_common.live_collector_dir(project, TASK, "browser") / "server-lease.json"
    payload = {
        "schema": "star-forge.server-lease.v1",
        "project": str(project),
        "origin": origin or browser_playwright.normalize_origin(parsed),
        "port": parsed.port or 80,
        "pid": os.getpid() if pid is None else pid,
        "command": "python3 -m http.server 4173",
        "source_hash": source_hash or live_common.compute_source_hash(project),
        "runtime_asset_hash": runtime_hash or live_common.compute_runtime_asset_hash(project),
    }
    return write_json(path, payload)


def success_runner(
    *,
    assertions_pass: bool = True,
    ready_pass: bool = True,
    console_payload: Any | None = None,
    malformed_console: bool = False,
    mutate_source: Callable[[], None] | None = None,
) -> Callable[[Any], Any]:
    def runner(context: Any) -> Any:
        make_png(context.paths.desktop, 1280, 800)
        make_png(context.paths.mobile, 390, 844)
        if malformed_console:
            context.paths.console.write_text("{not-json", encoding="utf-8")
        else:
            payload = console_payload
            if payload is None:
                payload = {"schema": "star-forge.browser-console.v1", "events": []}
            browser_playwright.write_json_artifact(context.paths.console, payload)
        ready = [{"viewport": item.name, "passed": ready_pass, "ready": context.scenario["ready"]} for item in context.viewports]
        assertions = [
            {
                "viewport": item.name,
                "type": "text_contains",
                "selector": "h1",
                "expected": {"text": "Lorem ipsum"},
                "actual": "Lorem ipsum" if assertions_pass else "Wrong",
                "passed": assertions_pass,
            }
            for item in context.viewports
        ]
        requests = []
        final_urls = []
        for item in context.viewports:
            request = browser_playwright.browser_url_safety_evidence(
                context.url,
                allowed_local_origins=context.allowed_local_origins,
            )
            request.update({"viewport": item.name, "method": "GET", "resource_type": "document", "navigation": True})
            requests.append(request)
            final_url = browser_playwright.browser_url_safety_evidence(
                context.url,
                allowed_local_origins=context.allowed_local_origins,
            )
            final_url["viewport"] = item.name
            final_urls.append(final_url)
        browser_playwright.write_json_artifact(
            context.paths.interaction,
            {
                "schema": "star-forge.browser-interaction.v1",
                "scenario": context.scenario_label,
                "ready": ready,
                "actions": [],
                "assertions": assertions,
                "request_safety": {
                    "schema": "star-forge.browser-request-safety.v1",
                    "service_workers": browser_playwright.SERVICE_WORKERS_MODE,
                    "connection_control": browser_playwright.BROWSER_NETWORK_CONTROL_MODE,
                    "websocket_routing": browser_playwright.WEBSOCKET_ROUTING_MODE,
                    "allowed_local_origins": list(context.allowed_local_origins),
                    "requests": requests,
                    "websockets": [],
                    "final_urls": final_urls,
                    "blocked_count": sum(1 for item in requests if item.get("allowed") is not True),
                    "websocket_blocked_count": 0,
                    "webrtc": {"mode": browser_playwright.WEBRTC_CONTROL_MODE, "init_script": True},
                },
            },
        )
        if context.trace:
            context.paths.trace.write_bytes(b"fake trace bytes")
        if mutate_source:
            mutate_source()
        return browser_playwright.BrowserExecutionResult(
            tool_versions={"playwright": "fake", "browser": "fake-chromium"},
            summary={
                "fake_runner": True,
                "service_workers": browser_playwright.SERVICE_WORKERS_MODE,
                "connection_control": browser_playwright.BROWSER_NETWORK_CONTROL_MODE,
                "websocket_routing": browser_playwright.WEBSOCKET_ROUTING_MODE,
                "webrtc_control": {"mode": browser_playwright.WEBRTC_CONTROL_MODE, "init_script": True},
                "final_urls": [context.url],
                "connected_ips": sorted({
                    str(ip)
                    for item in requests + final_urls
                    for ip in item.get("resolved_ips", []) if isinstance(item.get("resolved_ips"), list)
                }),
                "blocked_request_count": sum(1 for item in requests if item.get("allowed") is not True),
            },
        )
    return runner


def missing_dependency_runner(context: Any) -> Any:
    raise browser_playwright.BrowserDependencyError("Playwright is not installed")


def run_collector(
    project: Path,
    *,
    scenario: str = "happy",
    url: str = LOCAL_URL,
    runner: Callable[[Any], Any] | None = None,
    lease: Path | None = None,
    trace: bool = False,
    record: bool = False,
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    args = [
        "--project",
        str(project),
        "--task",
        TASK,
        "--url",
        url,
        "--scenario",
        f"{SCENARIO_REL}#{scenario}",
    ]
    if lease is not None:
        args.extend(["--server-lease", live_common.project_relative(project, lease)])
    if trace:
        args.append("--trace")
    if record:
        args.append("--record")
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = browser_playwright.main(args, runner=runner)
    payload = json.loads(stdout.getvalue())
    manifest = json.loads((project / payload["manifest"]).read_text(encoding="utf-8"))
    return code, payload, manifest


def run_star(args: list[str]) -> tuple[int, dict[str, Any]]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = star_forge.main(args)
    return code, json.loads(stdout.getvalue())


class FakeRequest:
    def __init__(self, url: str, *, resource_type: str = "document", navigation: bool = True) -> None:
        self.url = url
        self.method = "GET"
        self.resource_type = resource_type
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class FakeRoute:
    def __init__(self, request: FakeRequest) -> None:
        self.request = request
        self.continued = False
        self.aborted = False

    def continue_(self) -> None:
        self.continued = True

    def abort(self, _error_code: str = "") -> None:
        self.aborted = True


class FakeWebSocketRoute:
    def __init__(self, url: str) -> None:
        self.url = url
        self.connected = False
        self.closed = False

    def connect_to_server(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.closed = True


class FakeTracing:
    def start(self, **_kwargs: Any) -> None:
        pass

    def stop(self, *, path: str) -> None:
        Path(path).write_bytes(b"fake trace")


class FakePage:
    def __init__(self, browser_context: "FakeBrowserContext", mode: str) -> None:
        self.browser_context = browser_context
        self.mode = mode
        self.url = "about:blank"
        self.websocket_handler: Callable[[Any], None] | None = None

    def on(self, _event: str, _handler: Callable[[Any], None]) -> None:
        pass

    def route_web_socket(self, _pattern: str, handler: Callable[[Any], None]) -> None:
        if self.mode == "no-websocket-route":
            raise AttributeError("route_web_socket is unavailable")
        self.websocket_handler = handler

    def dispatch_websocket(self, url: str) -> FakeWebSocketRoute:
        route = FakeWebSocketRoute(url)
        if self.websocket_handler is not None:
            self.websocket_handler(route)
        return route

    def goto(self, url: str, **_kwargs: Any) -> None:
        if not self.browser_context.dispatch(url, resource_type="document", navigation=True):
            self.url = url
            raise RuntimeError("navigation request was blocked")
        self.url = url
        if self.mode == "redirect-loopback":
            target = "http://127.0.0.1:9/redirected"
            if not self.browser_context.dispatch(target, resource_type="document", navigation=True):
                self.url = target
                raise RuntimeError("redirect request was blocked")
            self.url = target
        elif self.mode == "subresource-private":
            self.browser_context.dispatch("http://10.0.0.5/private.css", resource_type="stylesheet", navigation=False)
        elif self.mode == "websocket-private":
            route = self.dispatch_websocket("ws://10.0.0.5/socket")
            if route.closed:
                raise RuntimeError("websocket request was blocked")

    def wait_for_selector(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def wait_for_load_state(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def wait_for_timeout(self, _ms: int) -> None:
        pass

    def click(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def fill(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def press(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def is_visible(self, *_args: Any, **_kwargs: Any) -> bool:
        return True

    def is_hidden(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def text_content(self, *_args: Any, **_kwargs: Any) -> str:
        return "Lorem ipsum"

    def locator(self, *_args: Any, **_kwargs: Any) -> Any:
        class Locator:
            def count(self) -> int:
                return 1

        return Locator()

    def screenshot(self, *, path: str, **_kwargs: Any) -> None:
        make_png(Path(path))


class FakeBrowserContext:
    def __init__(self, mode: str, options: dict[str, Any]) -> None:
        self.mode = mode
        self.options = options
        self.handler: Callable[[Any], None] | None = None
        self.init_scripts: list[str] = []
        self.tracing = FakeTracing()

    def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    def route(self, _pattern: str, handler: Callable[[Any], None]) -> None:
        self.handler = handler

    def dispatch(self, url: str, *, resource_type: str, navigation: bool) -> bool:
        if self.handler is None:
            return True
        route = FakeRoute(FakeRequest(url, resource_type=resource_type, navigation=navigation))
        self.handler(route)
        return route.continued and not route.aborted

    def new_page(self) -> FakePage:
        return FakePage(self, self.mode)

    def close(self) -> None:
        pass


class FakeBrowser:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def version(self) -> str:
        return "fake-browser"

    def new_context(self, **kwargs: Any) -> FakeBrowserContext:
        FAKE_CONTEXT_OPTIONS.append(dict(kwargs))
        return FakeBrowserContext(self.mode, dict(kwargs))

    def close(self) -> None:
        pass


class FakeBrowserType:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def launch(self, **_kwargs: Any) -> FakeBrowser:
        return FakeBrowser(self.mode)


class FakeSyncPlaywright:
    def __init__(self, mode: str) -> None:
        self.chromium = FakeBrowserType(mode)

    def sync_playwright(self) -> "FakeSyncPlaywright":
        return self

    def __enter__(self) -> "FakeSyncPlaywright":
        return self

    def __exit__(self, *_args: Any) -> None:
        pass


def with_fake_playwright(mode: str, func: Callable[[], None]) -> None:
    original = browser_playwright.load_playwright
    FAKE_CONTEXT_OPTIONS.clear()
    browser_playwright.load_playwright = lambda: (FakeSyncPlaywright(mode), "fake-playwright")
    try:
        func()
    finally:
        browser_playwright.load_playwright = original


def rules(payload: dict[str, Any]) -> set[str]:
    return {str(item.get("rule")) for item in payload.get("problems", []) if isinstance(item, dict)}


def assert_artifacts_scoped(manifest: dict[str, Any]) -> None:
    for item in manifest["artifacts"]:
        assert str(item["path"]).startswith(f".starforge/live/{TASK}/browser/"), item


def test_happy_path_writes_artifacts_manifest_and_handoff_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)
        code, payload, manifest = run_collector(project, lease=lease, runner=success_runner())
        assert code == 0, payload
        assert payload["recorded"] is False
        assert payload["degraded"] is False
        assert "--strict" in payload["browser_run_argv"]
        assert "--live-manifest" in payload["browser_run_argv"]
        assert "--server-lease" in payload["browser_run_argv"]
        assert "--degraded" not in payload["browser_run_argv"]
        assert ".starforge/runs" not in [str(path) for path in project.glob(".starforge/runs/*")]
        assert manifest["schema"] == live_common.LIVE_MANIFEST_SCHEMA
        assert manifest["collector"] == "browser"
        assert manifest["source_hash_before"] == manifest["source_hash_after"]
        envelope_path = project / payload["evidence"]
        envelope = evidence.read_envelope(
            envelope_path,
            project_root=project,
            verify_artifacts=True,
        )
        assert envelope["schema"] == evidence.EVIDENCE_SCHEMA
        assert envelope["capability"] == "local-web-qa"
        assert envelope["provider"] == "playwright-collector"
        assert envelope["runtime_asset_hash"] == manifest["runtime_asset_hash"]
        assert envelope["verdict"] == "DEGRADED"
        route = envelope["provenance"]["route"]
        assert route["preferred_provider"] == "in-app-browser"
        assert route["selected_provider"] == "playwright-collector"
        assert route["fallback"] is True
        browser_policy = envelope["provenance"]["browser_state_policy"]
        assert browser_policy["chrome"] == "reserved-for-authenticated-or-extension-dependent-state"
        assert any(
            item.get("rule") == "capability-fallback" and item.get("blocking") is False
            for item in envelope["blockers"]
        )
        expected_argv, _ = live_common.redact_sensitive_values(payload["browser_run_argv"])
        assert manifest["summary"]["browser_run_argv"] == expected_argv
        assert manifest["summary"]["capability_route"]["preferred_provider"] == "in-app-browser"
        assert manifest["summary"]["capability_route"]["selected_provider"] == "playwright-collector"
        assert manifest["summary"]["service_workers"] == "block"
        assert manifest["summary"]["network_control"] == browser_playwright.BROWSER_NETWORK_CONTROL_MODE
        assert_artifacts_scoped(manifest)
        assert (project / ".starforge" / "live" / TASK / "browser" / "desktop.png").exists()
        assert (project / ".starforge" / "live" / TASK / "browser" / "mobile.png").exists()


def test_handoff_command_uses_absolute_project_from_outside_project_cwd() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)
        old = Path.cwd()
        os.chdir(ROOT)
        try:
            code, payload, _ = run_collector(project, lease=lease, runner=success_runner())
            assert code == 0, payload
            argv = payload["browser_run_argv"]
            assert argv[argv.index("--project") + 1] == str(project)
            proof_code, proof_payload = run_star(argv[2:])
        finally:
            os.chdir(old)
        assert proof_code == 0, proof_payload
        assert proof_payload["verdict"] == "PASS", proof_payload


def test_record_uses_current_interpreter_not_path_python3() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp, "project").resolve()
        fakebin = Path(tmp, "fakebin")
        marker = Path(tmp, "path-python3-ran")
        init_project(project)
        lease = write_lease(project)
        fake_python = fakebin / "python3"
        write_text(fake_python, f"#!/bin/sh\nprintf ran > {marker}\nexit 99\n")
        fake_python.chmod(0o755)
        original_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(fakebin) + os.pathsep + original_path
        try:
            code, payload, _ = run_collector(project, lease=lease, runner=success_runner(), record=True)
        finally:
            os.environ["PATH"] = original_path
        assert not marker.exists(), payload
        assert code == 0, payload
        assert payload["recorded"] is True
        assert payload["record_result"]["returncode"] == 0, payload["record_result"]


def test_stale_source_hash_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)

        def mutate() -> None:
            write_text(project / "src" / "app.js", "export const app = 'changed during collection';\n")

        code, payload, manifest = run_collector(project, lease=lease, runner=success_runner(mutate_source=mutate))
        assert code == 1, payload
        assert "source-hash" in rules(payload)
        assert manifest["source_hash_before"] != manifest["source_hash_after"]
        assert "--degraded" in payload["browser_run_argv"]


def test_missing_playwright_dependency_writes_degraded_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)
        code, payload, manifest = run_collector(project, lease=lease, runner=missing_dependency_runner)
        assert code == 1, payload
        assert manifest["degraded"] is True
        assert "playwright-browser" in manifest["unavailable_capabilities"]
        assert "playwright-dependency" in rules(payload)
        envelope = evidence.read_envelope(project / payload["evidence"])
        assert envelope["verdict"] == "FAIL"
        assert any(item.get("rule") == "playwright-dependency" for item in envelope["blockers"])
        assert "--degraded" in payload["browser_run_argv"]


def test_bad_scenario_schema_rejects_forbidden_js() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        code, payload, manifest = run_collector(
            project,
            scenario="bad-forbidden-js",
            url="https://example.com",
            runner=success_runner(),
        )
        assert code == 1, payload
        assert "scenario-schema" in rules(payload)
        assert manifest["summary"]["blocking_problem_count"] >= 1
        assert "--degraded" in payload["browser_run_argv"]


def test_failing_visual_assertion_is_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)
        code, payload, manifest = run_collector(project, lease=lease, runner=success_runner(assertions_pass=False))
        assert code == 1, payload
        assert "visual-assertion" in rules(payload)
        assert manifest["degraded"] is False
        assert "--degraded" in payload["browser_run_argv"]


def test_dead_and_mismatched_server_lease_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        dead_lease = write_lease(project, pid=0)
        code, payload, _ = run_collector(project, lease=dead_lease, runner=success_runner())
        assert code == 1, payload
        assert "server-lease" in rules(payload)

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        bad_origin = write_lease(project, origin="http://localhost:4173")
        code, payload, _ = run_collector(project, lease=bad_origin, runner=success_runner())
        assert code == 1, payload
        assert "server-lease" in rules(payload)


def test_ready_timeout_is_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)
        code, payload, manifest = run_collector(
            project,
            scenario="ready-timeout",
            lease=lease,
            runner=success_runner(ready_pass=False),
        )
        assert code == 1, payload
        assert "ready-timeout" in rules(payload)
        assert manifest["summary"]["blocking_problem_count"] >= 1


def test_trace_opt_in_writes_trace_and_nonblocking_warning() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)
        code, payload, manifest = run_collector(project, lease=lease, trace=True, runner=success_runner())
        assert code == 0, payload
        assert "trace-redaction-warning" in rules(payload)
        trace_problem = [item for item in manifest["problems"] if item.get("rule") == "trace-redaction-warning"][0]
        assert trace_problem["blocking"] is False
        assert trace_problem["severity"] == "info"
        assert (project / ".starforge" / "live" / TASK / "browser" / "trace.zip").exists()


def test_malformed_console_evidence_is_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)
        code, payload, _ = run_collector(project, lease=lease, runner=success_runner(malformed_console=True))
        assert code == 1, payload
        assert "console-evidence" in rules(payload)
        assert "--degraded" in payload["browser_run_argv"]


def test_console_warning_after_readiness_is_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)
        console = {
            "schema": "star-forge.browser-console.v1",
            "events": [{"viewport": "desktop", "type": "warning", "text": "late warning", "phase": "after_ready"}],
        }
        code, payload, _ = run_collector(project, lease=lease, runner=success_runner(console_payload=console))
        assert code == 1, payload
        assert "console-after-ready" in rules(payload)


def test_unsafe_urls_require_rejected_or_bound_server_lease() -> None:
    unsafe_urls = [
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/",
        "http://[fe80::1]/",
        "http://metadata.google.internal/",
    ]
    for url in unsafe_urls:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            code, payload, _ = run_collector(project, url=url, runner=success_runner())
            assert code == 1, (url, payload)
            assert {"browser-url", "server-lease"} & rules(payload), payload.get("problems")


def test_public_remote_url_is_rejected_without_connection_control() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        code, payload, manifest = run_collector(project, url="http://93.184.216.34/", runner=success_runner())
        assert code == 1, payload
        assert "browser-url" in rules(payload)
        assert manifest["summary"]["blocking_problem_count"] >= 1
        assert not (project / ".starforge" / "live" / TASK / "browser" / "interaction.json").exists()


def test_hostname_resolving_to_private_address_requires_lease() -> None:
    original = browser_playwright.socket.getaddrinfo

    def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        return [(browser_playwright.socket.AF_INET, browser_playwright.socket.SOCK_STREAM, 6, "", ("10.0.0.8", 80))]

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        browser_playwright.socket.getaddrinfo = fake_getaddrinfo
        try:
            code, payload, _ = run_collector(project, url="http://private.example.test/", runner=success_runner())
        finally:
            browser_playwright.socket.getaddrinfo = original
        assert code == 1, payload
        assert "browser-url" in rules(payload)


def test_hostname_resolving_to_non_global_address_is_rejected() -> None:
    original = browser_playwright.socket.getaddrinfo

    def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        return [(browser_playwright.socket.AF_INET, browser_playwright.socket.SOCK_STREAM, 6, "", ("100.64.0.1", 80))]

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        browser_playwright.socket.getaddrinfo = fake_getaddrinfo
        try:
            code, payload, _ = run_collector(project, url="http://shared.example.test/", runner=success_runner())
        finally:
            browser_playwright.socket.getaddrinfo = original
        assert code == 1, payload
        assert "browser-url" in rules(payload)


def test_playwright_route_blocks_redirect_to_loopback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)

        def run() -> None:
            code, payload, manifest = run_collector(project, url=LOCAL_URL, lease=lease)
            assert code == 1, payload
            assert "browser-request-safety" in rules(payload)
            interaction = json.loads((project / ".starforge" / "live" / TASK / "browser" / "interaction.json").read_text(encoding="utf-8"))
            safety = interaction["request_safety"]
            assert safety["blocked_count"] >= 1
            assert any("127.0.0.1" in str(item.get("url")) for item in safety["requests"])
            assert manifest["summary"]["blocked_request_count"] >= 1

        with_fake_playwright("redirect-loopback", run)


def test_playwright_route_blocks_private_subresource() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)

        def run() -> None:
            code, payload, manifest = run_collector(project, url=LOCAL_URL, lease=lease)
            assert code == 1, payload
            assert "browser-request-safety" in rules(payload)
            interaction = json.loads((project / ".starforge" / "live" / TASK / "browser" / "interaction.json").read_text(encoding="utf-8"))
            requests = interaction["request_safety"]["requests"]
            assert any(item.get("url") == "http://10.0.0.5/private.css" and item.get("allowed") is False for item in requests)
            assert manifest["summary"]["blocked_request_count"] >= 1

        with_fake_playwright("subresource-private", run)


def test_playwright_blocks_service_workers_and_records_setting() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)

        def run() -> None:
            code, payload, _manifest = run_collector(project, lease=lease)
            assert code == 0, payload
            assert FAKE_CONTEXT_OPTIONS
            assert all(item.get("service_workers") == "block" for item in FAKE_CONTEXT_OPTIONS)
            interaction = json.loads((project / ".starforge" / "live" / TASK / "browser" / "interaction.json").read_text(encoding="utf-8"))
            assert interaction["request_safety"]["service_workers"] == "block"
            assert interaction["request_safety"]["connection_control"] == browser_playwright.BROWSER_NETWORK_CONTROL_MODE
            assert interaction["request_safety"]["websocket_routing"] == browser_playwright.WEBSOCKET_ROUTING_MODE
            assert interaction["request_safety"]["webrtc"] == {"mode": browser_playwright.WEBRTC_CONTROL_MODE, "init_script": True}

        with_fake_playwright("happy", run)


def test_playwright_fails_closed_when_websocket_routing_is_unavailable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)

        def run() -> None:
            code, payload, _manifest = run_collector(project, lease=lease)
            assert code == 1, payload
            assert "browser-websocket-safety" in rules(payload)
            interaction = json.loads((project / ".starforge" / "live" / TASK / "browser" / "interaction.json").read_text(encoding="utf-8"))
            assert interaction["request_safety"]["websocket_routing"] == "unavailable"

        with_fake_playwright("no-websocket-route", run)


def test_playwright_websocket_route_blocks_private_egress() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project)

        def run() -> None:
            code, payload, manifest = run_collector(project, lease=lease)
            assert code == 1, payload
            assert "browser-websocket-safety" in rules(payload)
            interaction = json.loads((project / ".starforge" / "live" / TASK / "browser" / "interaction.json").read_text(encoding="utf-8"))
            websockets = interaction["request_safety"]["websockets"]
            assert any(item.get("url") == "ws://10.0.0.5/socket" and item.get("allowed") is False for item in websockets)
            assert interaction["request_safety"]["websocket_blocked_count"] >= 1
            assert manifest["summary"]["blocked_websocket_count"] >= 1

        with_fake_playwright("websocket-private", run)


def test_nonliteral_loopback_host_with_server_lease_is_rejected() -> None:
    original = browser_playwright.socket.getaddrinfo

    def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        return [(browser_playwright.socket.AF_INET, browser_playwright.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 4173))]

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        browser_playwright.socket.getaddrinfo = fake_getaddrinfo
        try:
            url = "http://rebind.example.test:4173/"
            lease = write_lease(project, url=url)
            code, payload, _ = run_collector(project, url=url, lease=lease, runner=success_runner())
        finally:
            browser_playwright.socket.getaddrinfo = original
        assert code == 1, payload
        assert {"browser-url", "server-lease"} & rules(payload)


def test_request_safety_validation_requires_blocked_service_workers() -> None:
    payload = {
        "schema": "star-forge.browser-interaction.v1",
        "request_safety": {
            "schema": "star-forge.browser-request-safety.v1",
            "connection_control": browser_playwright.BROWSER_NETWORK_CONTROL_MODE,
            "allowed_local_origins": [LOCAL_URL],
            "requests": [],
            "websockets": [],
            "final_urls": [],
            "blocked_count": 0,
            "websocket_routing": browser_playwright.WEBSOCKET_ROUTING_MODE,
            "websocket_blocked_count": 0,
            "webrtc": {"mode": browser_playwright.WEBRTC_CONTROL_MODE, "init_script": True},
        },
    }
    problems = browser_playwright.validate_request_safety_payload(payload, allowed_local_origins=[LOCAL_URL])
    assert any(item.get("rule") == "browser-request-safety" and "service_workers" in str(item.get("message")) for item in problems)


def test_private_network_server_lease_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_lease(project, url="http://10.0.0.5:4173")
        code, payload, _ = run_collector(project, url="http://10.0.0.5:4173", lease=lease, runner=success_runner())
        assert code == 1, payload
        assert "browser-url" in rules(payload)
        assert "server-lease" in rules(payload)


def test_cli_server_lease_claim_matches_browser_collector_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        code, lease_payload = run_star([
            "server-lease",
            "--project", str(project),
            "--action", "claim",
            "--port", "4173",
            "--base-url", LOCAL_URL,
            "--command", "python3 -m http.server 4173",
            "--pid", str(os.getpid()),
        ])
        assert code == 0, lease_payload
        assert lease_payload["source_hash"] == live_common.compute_source_hash(project)
        assert lease_payload["runtime_asset_hash"] == live_common.compute_runtime_asset_hash(project, exclude_paths=[project / ".starforge" / "runtime" / "server.json"])
        code, payload, _ = run_collector(project, lease=project / ".starforge" / "runtime" / "server.json", runner=success_runner())
        assert code == 0, payload


def main() -> int:
    tests = [(name, func) for name, func in list(globals().items()) if name.startswith("test_") and callable(func)]
    passed = 0
    failed: list[str] = []
    for name, func in tests:
        try:
            func()
        except Exception:
            failed.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {name}")
    print(f"\ntest_live_browser_playwright.py: {passed} passed, {len(failed)} failed, {len(tests)} total")
    if failed:
        print("failed tests: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
