#!/usr/bin/env python3
"""Focused tests for the native iOS XcodeBuildMCP evidence adapter.

Run with: python3 tests/test_live_native_ios.py
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
from typing import Any, Callable, Iterator

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
from live_collectors import native_ios

os.environ["STAR_FORGE_LEARNINGS_HOME"] = tempfile.mkdtemp(prefix="star-forge-native-ios-learnings-")

PLAN_HEADER = (
    "| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |\n"
    "|------|-------------|--------|------|-------|---------|--------|----------|\n"
)
REAL_VERIFY = "python3 -c \"print('ok')\""
TASK = "SF-1"
FIXTURES = ROOT / "fixtures" / "native-ios"


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
        code = native_ios.main(args)
    out = stdout.getvalue()
    err = stderr.getvalue()
    payload = json.loads(out) if out.strip().startswith("{") else {}
    return code, payload, err


def init_project(project: Path) -> None:
    code, payload, err = run_star_cli(["init", "--project", str(project), "--no-agents"])
    assert code == 0, err or payload
    (project / "src").mkdir(exist_ok=True)
    (project / "src" / "App.swift").write_text("print(\"hello native ios\")\n", encoding="utf-8")
    (project / "Plan.md").write_text(
        "# Plan.md\n\n"
        + PLAN_HEADER
        + f"| {TASK} | Build native iOS proof artifact | ready | solo | src/App.swift | - | {REAL_VERIFY} | - |\n",
        encoding="utf-8",
    )


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_png(path: Path, width: int = 48, height: int = 48) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + ihdr + b"\x00\x00\x00\x00")
    return path


def fixture_payload(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def live_dir(project: Path) -> Path:
    return live_common.live_collector_dir(project, TASK, "native-ios", create=False)


def manifest_payload(project: Path) -> dict[str, Any]:
    return json.loads((live_dir(project) / "manifest.json").read_text(encoding="utf-8"))


def rules(payload: dict[str, Any]) -> set[str]:
    return {str(item.get("rule")) for item in payload.get("problems", []) if isinstance(item, dict)}


def assert_rule(payload: dict[str, Any], rule: str) -> None:
    assert rule in rules(payload), payload.get("problems")


def assert_collector_rule(project: Path, output: dict[str, Any], rule: str) -> None:
    assert_rule(output, rule)
    assert_rule(manifest_payload(project), rule)


def remove_arg(args: list[str], flag: str) -> list[str]:
    out = list(args)
    idx = out.index(flag)
    del out[idx:idx + 2]
    return out


def base_inputs(
    project: Path,
    *,
    transcript_name: str = "mcp-transcript-happy.json",
    mutate_transcript: Callable[[dict[str, Any]], None] | None = None,
    include_screenshot: bool = True,
    include_log: bool = False,
    build_success: bool = True,
    launch_success: bool = True,
    test_success: bool = True,
) -> dict[str, Path]:
    root = project / ".starforge" / "native-ios-inputs"
    transcript = fixture_payload(transcript_name)
    transcript.setdefault("source_hash", star_forge.source_hash(project))
    if mutate_transcript is not None:
        mutate_transcript(transcript)
    paths = {
        "transcript": write_json(root / "mcp-transcript.json", transcript),
        "build": write_json(root / "build.json", {"schema": native_ios.RESULT_SCHEMA, "kind": "build", "success": build_success}),
        "launch": write_json(root / "launch.json", {"schema": native_ios.RESULT_SCHEMA, "kind": "launch", "success": launch_success}),
        "test": write_json(root / "test.json", {"schema": native_ios.RESULT_SCHEMA, "kind": "test", "success": test_success}),
    }
    if include_screenshot:
        paths["screenshot"] = make_png(root / "screenshot.png")
    if include_log:
        paths["log"] = write_text(root / "log.txt", "SpringBoard log line\n")
    return paths


def base_args(project: Path, paths: dict[str, Path]) -> list[str]:
    args = [
        "--project", str(project),
        "--task", TASK,
        "--scheme", "TestApp",
        "--simulator", "iPhone 16",
        "--app-identity", "com.example.TestApp",
        "--mcp-transcript", str(paths["transcript"]),
        "--build-result", str(paths["build"]),
        "--launch-result", str(paths["launch"]),
        "--test-result", str(paths["test"]),
    ]
    if "screenshot" in paths:
        args.extend(["--screenshot", str(paths["screenshot"])])
    if "log" in paths:
        args.extend(["--log", str(paths["log"])])
    return args


def test_missing_mcp_transcript_degrades_and_blocks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project)
        args = remove_arg(base_args(project, paths), "--mcp-transcript")
        code, output, err = run_collector(args)
        assert err == ""
        assert code == 1
        assert output["degraded"] is True
        assert "mcp-transcript" in output["unavailable_capabilities"]
        assert_collector_rule(project, output, "native-ios-mcp-transcript")


def test_missing_session_show_defaults_blocks_ordered_proof() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project, transcript_name="mcp-transcript-missing-defaults.json")
        code, output, _ = run_collector(base_args(project, paths))
        assert code == 1
        assert_collector_rule(project, output, "native-ios-session-defaults")
        assert_collector_rule(project, output, "native-ios-tool-order")


def test_failed_build_blocks_handoff() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project, build_success=False)
        code, output, _ = run_collector(base_args(project, paths))
        assert code == 1
        assert output["handoff_ready"] is False
        assert_collector_rule(project, output, "native-ios-build")


def test_failed_launch_blocks_handoff() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project, launch_success=False)
        code, output, _ = run_collector(base_args(project, paths))
        assert code == 1
        assert output["handoff_ready"] is False
        assert_collector_rule(project, output, "native-ios-launch")


def test_failed_test_blocks_handoff() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project, test_success=False)
        code, output, _ = run_collector(base_args(project, paths))
        assert code == 1
        assert output["handoff_ready"] is False
        assert_collector_rule(project, output, "native-ios-test")


def test_log_only_ui_proof_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project, include_screenshot=False, include_log=True)
        code, output, _ = run_collector(base_args(project, paths))
        assert code == 1
        assert "log" in output["artifacts"]
        assert "screenshot" not in output["artifacts"]
        assert_collector_rule(project, output, "native-ios-ui")


def test_missing_app_identity_blocks_collection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project)
        args = remove_arg(base_args(project, paths), "--app-identity")
        code, output, _ = run_collector(args)
        assert code == 1
        assert_collector_rule(project, output, "native-ios-app-identity")


def test_stale_transcript_source_hash_blocks_collection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def stale(payload: dict[str, Any]) -> None:
            payload["source_hash"] = "stale-source-hash"

        paths = base_inputs(project, mutate_transcript=stale)
        code, output, _ = run_collector(base_args(project, paths))
        assert code == 1
        assert_collector_rule(project, output, "native-ios-source")


def test_unavailable_mcp_degrades_and_blocks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project)
        code, output, _ = run_collector(base_args(project, paths) + ["--mcp-unavailable"])
        assert code == 1
        assert output["degraded"] is True
        assert "xcodebuildmcp" in output["unavailable_capabilities"]
        assert_collector_rule(project, output, "native-ios-mcp-unavailable")


def test_happy_path_prints_native_ios_proof_command_and_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project)
        code, output, err = run_collector(base_args(project, paths) + ["--mcp-version", "fixture"])
        assert err == ""
        assert code == 0, output
        assert output["handoff_ready"] is True
        assert output["artifact_dir"].endswith(".starforge/live/SF-1/native-ios")
        assert "native-ios-proof" in output["proof_command"]
        assert output["native_ios_proof_command"] == output["proof_command"]

        manifest = manifest_payload(project)
        assert manifest["degraded"] is False
        assert manifest["summary"]["mcp_provenance"]["server"] == "XcodeBuildMCP"
        assert manifest["summary"]["simulator"]["runtime"] == "iOS 18.4"
        assert manifest["summary"]["simulator"]["udid"].endswith("0001")
        assert manifest["summary"]["app_identity"] == "com.example.TestApp"
        assert manifest["summary"]["artifact_semantics"]["ui_proof"] == "screenshot"
        assert manifest["source_hash_after"] == star_forge.source_hash(project)
        assert "runtime_asset_hash" in manifest

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
    print(f"\ntest_live_native_ios.py: {passed} passed, {len(failed)} failed, {len(tests)} total")
    if failed:
        print("failed tests: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
