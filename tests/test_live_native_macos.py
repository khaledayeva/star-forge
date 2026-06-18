#!/usr/bin/env python3
"""Focused tests for the native macOS live collector.

Run with: python3 tests/test_live_native_macos.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import plistlib
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SCRIPT = SCRIPTS / "star_forge.py"
SPEC = importlib.util.spec_from_file_location("star_forge", SCRIPT)
assert SPEC and SPEC.loader
star_forge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(star_forge)

from live_collectors import common as live_common
from live_collectors import native_macos

os.environ["STAR_FORGE_LEARNINGS_HOME"] = tempfile.mkdtemp(prefix="star-forge-native-macos-learnings-")

PLAN_HEADER = (
    "| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |\n"
    "|------|-------------|--------|------|-------|---------|--------|----------|\n"
)
REAL_VERIFY = "python3 -c \"print('ok')\""
PNG_CODE = (
    "from pathlib import Path; import sys; "
    "p=Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); "
    "ihdr=(32).to_bytes(4,'big')+(32).to_bytes(4,'big')+b'\\x08\\x06\\x00\\x00\\x00'; "
    "p.write_bytes(b'\\x89PNG\\r\\n\\x1a\\n'+(13).to_bytes(4,'big')+b'IHDR'+ihdr+b'\\x00\\x00\\x00\\x00')"
)


@contextlib.contextmanager
def chdir(path: Path) -> Iterator[None]:
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def run_star_cli(args: list[str]) -> tuple[int, dict[str, Any], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = star_forge.main(args)
    out = stdout.getvalue()
    err = stderr.getvalue()
    payload = json.loads(out) if out.strip().startswith("{") else {}
    return code, payload, err


def run_collector(args: list[str]) -> tuple[int, dict[str, Any], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = native_macos.main(args)
    out = stdout.getvalue()
    err = stderr.getvalue()
    payload = json.loads(out) if out.strip().startswith("{") else {}
    return code, payload, err


def init_project(project: Path) -> None:
    code, payload, err = run_star_cli(["init", "--project", str(project), "--no-agents"])
    assert code == 0, err or payload
    (project / "src").mkdir(exist_ok=True)
    (project / "src" / "app.swift").write_text("print(\"hello native mac\")\n", encoding="utf-8")
    (project / "Plan.md").write_text(
        "# Plan.md\n\n" + PLAN_HEADER
        + f"| SF-1 | Build native macOS artifact | ready | solo | src/app.swift | - | {REAL_VERIFY} | - |\n",
        encoding="utf-8",
    )


def make_app(
    project: Path,
    *,
    name: str = "TestApp",
    bundle_id: str = "com.example.TestApp",
    dirname: str | None = None,
    include_plist: bool = True,
) -> Path:
    app = project / "BuildProducts" / f"{dirname or name}.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    exe = macos / name
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    if include_plist:
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleIdentifier": bundle_id,
                    "CFBundleName": name,
                    "CFBundleDisplayName": name,
                    "CFBundleExecutable": name,
                    "CFBundleShortVersionString": "1.0",
                    "CFBundleVersion": "1",
                },
                handle,
            )
    return app


def argv(items: list[str]) -> str:
    return json.dumps(items)


def py_cmd(code: str, *extra: str) -> list[str]:
    return [sys.executable, "-c", code, *extra]


def base_args(project: Path, app: Path | None = None) -> list[str]:
    args = [
        "--project", str(project),
        "--task", "SF-1",
        "--app-name", "TestApp",
        "--bundle-id", "com.example.TestApp",
        "--build-command", argv(py_cmd("print('build ok')")),
        "--run-command", argv(py_cmd("import time; print('READY', flush=True); time.sleep(5)")),
        "--readiness-text", "READY",
        "--run-timeout", "2",
        "--cleanup-timeout", "1",
    ]
    if app is not None:
        args.extend(["--app-bundle", str(app)])
    return args


def manifest_payload(project: Path) -> dict[str, Any]:
    path = live_common.live_collector_dir(project, "SF-1", "native-macos", create=False) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def live_path(project: Path, name: str) -> Path:
    return live_common.live_collector_dir(project, "SF-1", "native-macos", create=False) / name


def rules(payload: dict[str, Any]) -> set[str]:
    return {str(item.get("rule")) for item in payload.get("problems", []) if isinstance(item, dict)}


def assert_rule(payload: dict[str, Any], rule: str) -> None:
    assert rule in rules(payload), payload.get("problems")


def assert_collector_rule(project: Path, output: dict[str, Any], rule: str) -> None:
    assert_rule(output, rule)
    assert_rule(manifest_payload(project), rule)


def test_missing_app_identity_blocks_collection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        app = make_app(project)
        args = base_args(project, app)
        args.remove("--app-name")
        args.remove("TestApp")
        args.remove("--bundle-id")
        args.remove("com.example.TestApp")
        code, output, err = run_collector(args)
        assert err == ""
        assert code == 1
        assert_collector_rule(project, output, "native-macos-app-identity")


def test_failed_build_writes_blocking_result_and_skips_runtime() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        app = make_app(project)
        args = base_args(project, app)
        args[args.index("--build-command") + 1] = argv(py_cmd("import sys; print('bad build'); sys.exit(3)"))
        code, output, _ = run_collector(args)
        assert code == 1
        assert_collector_rule(project, output, "native-macos-build")
        build = json.loads(live_path(project, "build.json").read_text(encoding="utf-8"))
        run = json.loads(live_path(project, "run.json").read_text(encoding="utf-8"))
        assert build["success"] is False
        assert run["status"] == "skipped"


def test_run_timeout_blocks_collection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        app = make_app(project)
        args = base_args(project, app)
        args[args.index("--run-command") + 1] = argv(py_cmd("import time; time.sleep(3)"))
        args[args.index("--run-timeout") + 1] = "0.2"
        code, output, _ = run_collector(args)
        assert code == 1
        assert_collector_rule(project, output, "native-macos-run-timeout")
        run = json.loads(live_path(project, "run.json").read_text(encoding="utf-8"))
        assert run["pid"]
        assert run["timed_out"] is True
        assert run["readiness"]["status"] == "missing"


def test_gui_launch_failure_blocks_collection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        app = make_app(project)
        args = base_args(project, app)
        args[args.index("--run-command") + 1] = argv(py_cmd("import sys; sys.exit(7)"))
        args[args.index("--readiness-text") + 1] = ""
        code, output, _ = run_collector(args)
        assert code == 1
        assert_collector_rule(project, output, "native-macos-gui-launch")
        run = json.loads(live_path(project, "run.json").read_text(encoding="utf-8"))
        assert run["gui_launch_failed"] is True


def test_screenshot_permission_failure_degrades_and_blocks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        app = make_app(project)
        screenshot = py_cmd("import sys; print('screen recording permission denied', file=sys.stderr); sys.exit(1)", native_macos.SCREENSHOT_PLACEHOLDER)
        args = base_args(project, app) + ["--screenshot-command", argv(screenshot)]
        code, output, _ = run_collector(args)
        assert code == 1
        assert output["degraded"] is True
        assert_collector_rule(project, output, "native-macos-screenshot-permission")
        manifest = manifest_payload(project)
        assert manifest["degraded"] is True
        assert "screenshot" in manifest["unavailable_capabilities"]


def test_missing_bundle_metadata_blocks_collection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        app = make_app(project, include_plist=False)
        code, output, _ = run_collector(base_args(project, app))
        assert code == 1
        assert_collector_rule(project, output, "native-macos-bundle-metadata")


def test_ambiguous_app_discovery_blocks_collection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        make_app(project, dirname="First")
        make_app(project, dirname="Second")
        code, output, _ = run_collector(base_args(project, app=None))
        assert code == 1
        assert_collector_rule(project, output, "native-macos-app-discovery")


def test_cleanup_failure_blocks_collection(monkeypatch: Any = None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        app = make_app(project)
        original = native_macos.terminate_process

        def fake_terminate(proc: Any, *, timeout: float) -> dict[str, Any]:
            proc.kill()
            proc.wait(timeout=1)
            return {"attempted": True, "method": "fake", "success": False, "returncode": proc.poll()}

        native_macos.terminate_process = fake_terminate
        try:
            code, output, _ = run_collector(base_args(project, app))
        finally:
            native_macos.terminate_process = original
        assert code == 1
        assert_collector_rule(project, output, "native-macos-cleanup")
        run = json.loads(live_path(project, "run.json").read_text(encoding="utf-8"))
        assert run["cleanup_failed"] is True


def test_structured_argv_capture_and_forbidden_shell() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        app = make_app(project)
        args = base_args(project, app)
        literal = "*.swift"
        args[args.index("--build-command") + 1] = argv(py_cmd("import sys; print(sys.argv[1])", literal))
        code, output, _ = run_collector(args)
        assert code == 0, output
        build = json.loads(live_path(project, "build.json").read_text(encoding="utf-8"))
        assert build["shell"] is False
        assert build["command_argv"][-1] == literal
        proof_argv = output["proof_command_argv"]
        assert proof_argv[proof_argv.index("--app-bundle") + 1].endswith("TestApp.app")
        assert "app-bundle-metadata.json" not in proof_argv[proof_argv.index("--app-bundle") + 1]

        shell_args = base_args(project, app)
        shell_args[shell_args.index("--build-command") + 1] = argv(["sh", "-c", "echo unsafe"])
        code, output, _ = run_collector(shell_args)
        assert code == 1
        assert_collector_rule(project, output, "native-macos-shell")


def test_happy_path_prints_native_macos_proof_command_and_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        app = make_app(project)
        screenshot = py_cmd(PNG_CODE, native_macos.SCREENSHOT_PLACEHOLDER)
        args = base_args(project, app) + [
            "--test-command", argv(py_cmd("print('tests ok')")),
            "--screenshot-command", argv(screenshot),
        ]
        code, output, err = run_collector(args)
        assert err == ""
        assert code == 0, output
        assert output["artifact_dir"].endswith(".starforge/live/SF-1/native-macos")
        assert "native-macos-proof" in output["proof_command"]
        assert "--signing-note .starforge/live/SF-1/native-macos/signing-note.json" in output["proof_command"]
        assert "--packaging-note .starforge/live/SF-1/native-macos/packaging-note.json" in output["proof_command"]

        manifest = manifest_payload(project)
        assert manifest["summary"]["app_bundle_metadata"].endswith("app-bundle-metadata.json")
        assert manifest["summary"]["signing_note"] == "not_checked"
        assert manifest["summary"]["packaging_note"] == "not_checked"
        run = json.loads(live_path(project, "run.json").read_text(encoding="utf-8"))
        assert run["pid"]
        assert run["executable_path"]
        assert run["readiness"]["status"] == "observed"
        assert run["stdout_artifact"].endswith("stdout.txt")
        assert run["stderr_artifact"].endswith("stderr.txt")
        assert run["termination"]["attempted"] is True
        assert run["cleanup_failed"] is False

        with chdir(project):
            proof_args = output["proof_command_argv"][2:]
            proof_code, proof_payload, proof_err = run_star_cli(proof_args)
        assert proof_err == ""
        assert proof_code == 0, proof_payload
        assert proof_payload["verdict"] == "PASS", proof_payload


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
    print(f"\ntest_live_native_macos.py: {passed} passed, {len(failed)} failed, {len(tests)} total")
    if failed:
        print("failed tests: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
