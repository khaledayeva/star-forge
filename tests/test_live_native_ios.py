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
from unittest import mock

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
from starforge import evidence

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


def envelope_payload(project: Path) -> dict[str, Any]:
    return evidence.read_envelope(
        live_dir(project) / "evidence.json",
        project_root=project,
        verify_artifacts=True,
    )


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


def replace_arg(args: list[str], flag: str, value: Path | str) -> list[str]:
    out = list(args)
    idx = out.index(flag)
    out[idx + 1] = str(value)
    return out


def base_inputs(
    project: Path,
    *,
    transcript_name: str = "mcp-transcript-happy.json",
    mutate_transcript: Callable[[dict[str, Any]], None] | None = None,
    include_screenshot: bool = True,
    include_ui_snapshot: bool = True,
    include_log: bool = False,
    build_success: bool = True,
    launch_success: bool = True,
    test_success: bool = True,
) -> dict[str, Path]:
    root = project / ".starforge" / "native-ios-inputs"
    transcript = fixture_payload(transcript_name)
    transcript.setdefault("source_hash", star_forge.source_hash(project))
    if include_ui_snapshot:
        transcript.setdefault("capabilities", []).append("ui_snapshot")
        transcript.setdefault("calls", []).append(
            {
                "tool": "ui_snapshot",
                "arguments": {"simulator": "iPhone 16"},
                "result": {"success": True},
            }
        )
    if mutate_transcript is not None:
        mutate_transcript(transcript)
    def result_payload(kind: str, success: bool) -> dict[str, Any]:
        return {
            "schema": native_ios.RESULT_SCHEMA,
            "kind": kind,
            "success": success,
            "simulator_runtime": "iOS 18.4",
            "simulator_udid": "00000000-0000-0000-0000-000000000001",
        }
    paths = {
        "transcript": write_json(root / "mcp-transcript.json", transcript),
        "build": write_json(root / "build.json", result_payload("build", build_success)),
        "launch": write_json(root / "launch.json", result_payload("launch", launch_success)),
        "test": write_json(root / "test.json", result_payload("test", test_success)),
    }
    if include_screenshot:
        paths["screenshot"] = make_png(root / "screenshot.png")
    if include_ui_snapshot:
        paths["ui_snapshot"] = write_json(
            root / "ui-snapshot.json",
            {"app": "TestApp", "tree": {"role": "window", "children": []}},
        )
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
    if "ui_snapshot" in paths:
        args.extend(["--ui-snapshot", str(paths["ui_snapshot"])])
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


def test_absolute_outside_mcp_transcript_is_rejected_before_reading() -> None:
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside_tmp:
        project = Path(tmp).resolve()
        outside = Path(outside_tmp).resolve()
        init_project(project)
        paths = base_inputs(project)
        transcript = fixture_payload("mcp-transcript-happy.json")
        transcript.setdefault("source_hash", star_forge.source_hash(project))
        transcript["outside_marker"] = "OUTSIDE_TRANSCRIPT_SECRET"
        outside_transcript = write_json(outside / "mcp-transcript.json", transcript)
        args = replace_arg(base_args(project, paths), "--mcp-transcript", outside_transcript)

        code, output, _ = run_collector(args)

        assert code == 1
        assert "mcp_transcript" in output["artifacts"]
        assert_collector_rule(project, output, "native-ios-mcp-transcript")
        copied = live_dir(project) / "mcp-transcript.json"
        assert copied.exists()
        assert "OUTSIDE_TRANSCRIPT_SECRET" not in copied.read_text(encoding="utf-8")


def test_absolute_outside_log_is_rejected_before_copying() -> None:
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside_tmp:
        project = Path(tmp).resolve()
        outside = Path(outside_tmp).resolve()
        init_project(project)
        paths = base_inputs(project, include_log=True)
        outside_log = write_text(outside / "device.log", "OUTSIDE_LOG_SECRET\n")
        args = replace_arg(base_args(project, paths), "--log", outside_log)

        code, output, _ = run_collector(args)

        assert code == 1
        assert "log" not in output["artifacts"]
        assert_collector_rule(project, output, "native-ios-log")
        copied = live_dir(project) / "log.txt"
        assert not copied.exists()


def test_absolute_outside_screenshot_is_rejected_before_copying() -> None:
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside_tmp:
        project = Path(tmp).resolve()
        outside = Path(outside_tmp).resolve()
        init_project(project)
        paths = base_inputs(project)
        outside_screenshot = make_png(outside / "screenshot.png")
        args = replace_arg(base_args(project, paths), "--screenshot", outside_screenshot)

        code, output, _ = run_collector(args)

        assert code == 1
        assert "screenshot" not in output["artifacts"]
        assert_collector_rule(project, output, "native-ios-screenshot")
        copied = live_dir(project) / "screenshot.png"
        assert not copied.exists()


def test_missing_session_show_defaults_blocks_ordered_proof() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project, transcript_name="mcp-transcript-missing-defaults.json")
        code, output, _ = run_collector(base_args(project, paths))
        assert code == 1
        assert_collector_rule(project, output, "native-ios-session-defaults")
        assert_collector_rule(project, output, "native-ios-tool-order")


def test_shell_fallback_tools_and_simulator_shell_commands_are_rejected() -> None:
    cases = [
        {"tool": "functions.exec_command", "arguments": {"cmd": "echo shell fallback"}},
        {"tool": "mcp__utility__exec_command", "arguments": {"cmd": "echo mcp shell fallback"}},
        {"tool": "run_command", "arguments": {"command": "echo forged evidence"}},
        {"tool": "run_command", "arguments": {"cmd": "sh -c echo forged"}},
        {"tool": "run_command", "arguments": {"command_args": ["sh", "-c", "echo forged"]}},
        {"tool": "run_command", "arguments": {"command": "open -a Simulator"}},
        {"tool": "run_command", "arguments": {"command": "open -a Simulator.app"}},
        {"tool": "run_command", "arguments": {"command": ["open", "-b", "com.apple.iphonesimulator"]}},
        {"tool": "run_command", "arguments": {"command": "/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app"}},
        {"tool": "run_command", "arguments": {"nested": {"command": "open -a Simulator.app"}}},
        {"tool": "run_command", "arguments": {"command": "osascript -e 'tell application \"Simulator\" to activate'"}},
        {"tool": "run_command", "arguments": {"command_argv": ["xcrun", "simctl", "list"]}},
        {"tool": "run_command", "arguments": {"command-argv": ["xcodebuild", "-scheme", "TestApp"]}},
        {"tool": "run_command", "arguments": {"command_args": ["env", "xcrun", "simctl", "boot", "DEVICE"]}},
        {"tool": "build_run_sim", "arguments": {"scheme": "TestApp", "simulator": "iPhone 16", "commandLine": "xcrun simctl list"}},
        {"tool": "test_sim", "arguments": {"scheme": "TestApp", "simulator": "iPhone 16", "command_line": ["xcrun", "simctl", "list"]}},
        {"tool": "screenshot", "arguments": {"simulator": "iPhone 16", "cmdLine": "sh -c echo forged"}},
        {"tool": "ui_snapshot", "arguments": {"simulator": "iPhone 16", "cmd_line": "xcrun simctl list"}},
        {"tool": "run_command", "arguments": {"shell_command": "open -a Simulator.app"}},
        {"tool": "run_command", "arguments": {"shell_command": "echo forged evidence"}},
        {"tool": "run_command", "arguments": {"shellCommand": "osascript -e 'tell application \"Simulator\" to activate'"}},
        {"tool": "run_command", "arguments": {"shellCmd": "echo forged evidence"}},
        {"tool": "run_command", "arguments": {"command_argv": ["sh", "-c", "echo forged"]}},
        {"tool": "build_run_sim", "arguments": {"scheme": "TestApp", "simulator": "iPhone 16", "argv": ["env", "sh", "-c", "echo forged"]}},
        {"tool": "build_run_sim", "arguments": {"scheme": "TestApp", "simulator": "iPhone 16", "command_argv": ["/usr/bin/env", "env", "bash", "-c", "echo forged"]}},
        {"tool": "launch_app", "arguments": {"simulator": "iPhone 16", "command_argv": ["env"]}},
        {"tool": "test_sim", "arguments": {"scheme": "TestApp", "simulator": "iPhone 16", "argv": ["env", "-S", "zsh -c 'echo forged'"]}},
        {"tool": "test_sim", "arguments": {"scheme": "TestApp", "simulator": "iPhone 16", "command_argv": ["env", "--split-string=pwsh -Command echo forged"]}},
    ]
    for shell_call in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(transcript: dict[str, Any]) -> None:
                transcript.setdefault("calls", []).append(shell_call)

            paths = base_inputs(project, mutate_transcript=mutate)
            code, output, _ = run_collector(base_args(project, paths))
            assert code == 1
            assert_collector_rule(project, output, "native-ios-shell-fallback")


def test_allowed_tool_result_command_fields_are_rejected() -> None:
    cases = [
        {"tool": "build_run_sim", "args": {"scheme": "TestApp", "simulator": "iPhone 16"}, "result": {"command_argv": ["xcrun", "simctl", "list"]}},
        {"tool": "build_run_sim", "args": {"scheme": "TestApp", "simulator": "iPhone 16"}, "result": {"details": {"commandLine": "xcrun simctl list"}}},
        {"tool": "test_sim", "args": {"scheme": "TestApp", "simulator": "iPhone 16"}, "result": {"shell_command": "open -a Simulator.app"}},
        {"tool": "test_sim", "args": {"scheme": "TestApp", "simulator": "iPhone 16"}, "result": {"cmdline": "osascript -e 'tell application \"Simulator\" to activate'"}},
        {"tool": "screenshot", "args": {"simulator": "iPhone 16"}, "result": {"argv": ["env", "sh", "-c", "echo forged"]}},
        {"tool": "ui_snapshot", "args": {"simulator": "iPhone 16"}, "result": {"nested": {"command_argv": ["env", "-S", "zsh -c 'echo forged'"]}}},
    ]
    for result_call in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(transcript: dict[str, Any]) -> None:
                transcript.setdefault("calls", []).append(result_call)

            paths = base_inputs(project, mutate_transcript=mutate)
            code, output, _ = run_collector(base_args(project, paths))
            assert code == 1
            assert_collector_rule(project, output, "native-ios-shell-fallback")


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


def test_schema_less_result_artifacts_block_handoff() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project)
        write_json(paths["build"], {"success": True})
        write_json(paths["launch"], {"success": True})
        write_json(paths["test"], {"success": True})
        code, output, _ = run_collector(base_args(project, paths))
        assert code == 1
        assert output["handoff_ready"] is False
        assert_collector_rule(project, output, "native-ios-result")


def test_log_only_ui_proof_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(
            project,
            include_screenshot=False,
            include_ui_snapshot=False,
            include_log=True,
        )
        code, output, _ = run_collector(base_args(project, paths))
        assert code == 1
        assert "log" in output["artifacts"]
        assert "screenshot" not in output["artifacts"]
        assert_collector_rule(project, output, "native-ios-ui")
        assert_collector_rule(project, output, "native-ios-ui-snapshot")


def test_each_visual_artifact_is_required() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project, include_screenshot=False)
        code, output, _ = run_collector(base_args(project, paths))
        assert code == 1
        assert_collector_rule(project, output, "native-ios-ui")

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project, include_ui_snapshot=False)
        code, output, _ = run_collector(base_args(project, paths))
        assert code == 1
        assert_collector_rule(project, output, "native-ios-ui-snapshot")


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
        assert manifest["summary"]["artifact_semantics"]["ui_proof"] == ["ui_snapshot", "screenshot"]
        assert manifest["source_hash_after"] == star_forge.source_hash(project)
        assert "runtime_asset_hash" in manifest
        assert output["evidence"].endswith("/evidence.json")
        envelope = envelope_payload(project)
        assert envelope["schema"] == evidence.EVIDENCE_SCHEMA
        assert envelope["capability"] == native_ios.CAPABILITY
        assert envelope["provider"] == native_ios.PRIMARY_PROVIDER
        assert envelope["source_hash"] == manifest["source_hash_after"]
        assert envelope["runtime_asset_hash"] == manifest["runtime_asset_hash"]
        assert envelope["verdict"] == "PASS"
        assert envelope["provenance"]["route"] == native_ios.ROUTE
        assert envelope["provenance"]["tool"] == "XcodeBuildMCP"
        assert envelope["provenance"]["tool_categories"]["ui_snapshot"] is True
        assert {
            item["id"]: item["status"]
            for item in envelope["provenance"]["providers"]
        }[native_ios.SIMULATOR_BROWSER_PROVIDER] == "availability-not-reported"

        with chdir(project):
            proof_args = output["proof_command_argv"][2:]
            proof_code, proof_payload, proof_err = run_star_cli(proof_args)
        assert proof_err == ""
        assert proof_code == 0, proof_payload
        assert proof_payload["verdict"] == "PASS", proof_payload


def test_proof_command_uses_absolute_project_from_outside_project_cwd() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project)
        with chdir(ROOT):
            code, output, err = run_collector(base_args(project, paths) + ["--mcp-version", "fixture"])
            assert err == ""
            assert code == 0, output
            proof_argv = output["proof_command_argv"]
            assert proof_argv[proof_argv.index("--project") + 1] == str(project)
            proof_code, proof_payload, proof_err = run_star_cli(proof_argv[2:])
        assert proof_err == ""
        assert proof_code == 0, proof_payload
        assert proof_payload["verdict"] == "PASS", proof_payload


def test_record_invokes_strict_proof_even_when_collector_degraded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(
            project,
            include_screenshot=False,
            include_ui_snapshot=False,
            include_log=True,
        )
        code, output, err = run_collector(base_args(project, paths) + ["--record"])
        assert err == ""
        assert code == 1, output
        assert output["degraded"] is True
        assert output["recorded"] is True
        assert output["record"]["returncode"] == 1, output["record"]
        assert "skipped" not in output["record"]
        records = star_forge.load_run_records(project, kind="native-ios-proof", task=TASK)
        assert records, output["record"]
        assert records[-1]["verdict"] == "FAIL", records[-1]
        assert_collector_rule(project, output, "native-ios-ui")


def test_simulator_browser_route_is_recorded_or_degraded_honestly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project)
        code, output, _ = run_collector(
            base_args(project, paths) + ["--simulator-browser-used"]
        )
        assert code == 0, output
        providers = {
            item["id"]: item["status"]
            for item in envelope_payload(project)["provenance"]["providers"]
        }
        assert providers[native_ios.SIMULATOR_BROWSER_PROVIDER] == "used"

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        paths = base_inputs(project)
        code, output, _ = run_collector(
            base_args(project, paths) + ["--simulator-browser-unavailable"]
        )
        assert code == 1
        assert output["evidence_verdict"] == "DEGRADED"
        assert native_ios.SIMULATOR_BROWSER_PROVIDER in output["unavailable_capabilities"]
        envelope = envelope_payload(project)
        assert envelope["verdict"] == "DEGRADED"
        assert any(
            blocker.get("capability") == native_ios.SIMULATOR_BROWSER_PROVIDER
            for blocker in envelope["blockers"]
        )

def test_json_source_swap_never_reads_external_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside_tmp:
        project = Path(tmp).resolve()
        out_dir = project / ".starforge" / "live" / TASK / "native-ios"
        out_dir.mkdir(parents=True)
        source = write_json(project / "input.json", {"trusted": True})
        outside = write_json(Path(outside_tmp) / "secret.json", {"secret": "outside-value"})
        real_read = native_ios.safe_io.read_snapshot
        swapped = False
        def swap_before_read(root: Path, path: Path, **kwargs: Any) -> tuple[bytes, str, int]:
            nonlocal swapped
            if not swapped:
                source.unlink()
                source.symlink_to(outside)
                swapped = True
            return real_read(root, path, **kwargs)
        problems: list[dict[str, Any]] = []
        with mock.patch.object(native_ios.safe_io, "read_snapshot", side_effect=swap_before_read):
            _path, payload = native_ios.copy_json_artifact(
                project, out_dir, str(source), "copied.json", "input", problems,
                missing_rule="native-ios-input")
        assert problems
        assert "outside-value" not in json.dumps(payload)

def test_static_in_project_symlink_source_is_not_followed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        target = write_json(project / "target.json", {"secret": "linked-value"})
        source = project / "input.json"
        source.symlink_to(target)
        problems: list[dict[str, Any]] = []
        resolved = native_ios.resolve_input_path(
            project, str(source), "input", problems, rule="native-ios-input")
        assert resolved is None
        assert problems

def test_image_destination_parent_swap_cannot_write_outside_project() -> None:
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside_tmp:
        project = Path(tmp).resolve()
        out_dir = project / ".starforge" / "live" / TASK / "native-ios"
        out_dir.mkdir(parents=True)
        source = make_png(project / "input.png")
        outside = Path(outside_tmp).resolve()
        parked = project / "parked-native-ios"
        real_read = native_ios.safe_io.read_snapshot
        def read_then_swap(root: Path, path: Path, **kwargs: Any) -> tuple[bytes, str, int]:
            result = real_read(root, path, **kwargs)
            out_dir.rename(parked)
            out_dir.symlink_to(outside, target_is_directory=True)
            return result
        problems: list[dict[str, Any]] = []
        with mock.patch.object(native_ios.safe_io, "read_snapshot", side_effect=read_then_swap):
            result = native_ios.copy_image_artifact(project, out_dir, str(source), problems)
        assert result is None
        assert problems
        assert not (outside / "screenshot.png").exists()


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
