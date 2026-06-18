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


TASK = "SF-1"
LOCAL_URL = "http://127.0.0.1:4173"
SCENARIO_REL = "fixtures/sloppy-web-app/live-browser-scenarios.json"


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
        browser_playwright.write_json_artifact(
            context.paths.interaction,
            {
                "schema": "star-forge.browser-interaction.v1",
                "scenario": context.scenario_label,
                "ready": ready,
                "actions": [],
                "assertions": assertions,
            },
        )
        if context.trace:
            context.paths.trace.write_bytes(b"fake trace bytes")
        if mutate_source:
            mutate_source()
        return browser_playwright.BrowserExecutionResult(
            tool_versions={"playwright": "fake", "browser": "fake-chromium"},
            summary={"fake_runner": True},
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
        assert manifest["summary"]["browser_run_argv"] == payload["browser_run_argv"]
        assert_artifacts_scoped(manifest)
        assert (project / ".starforge" / "live" / TASK / "browser" / "desktop.png").exists()
        assert (project / ".starforge" / "live" / TASK / "browser" / "mobile.png").exists()


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
