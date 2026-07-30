#!/usr/bin/env python3
"""Collect baseline native macOS artifacts for Star Forge.

This collector is a local artifact supplier only. It runs explicit structured
argv commands, writes task-scoped evidence under
`.starforge/live/<task-id>/native-macos/`, and prints the strict
`native-macos-proof` handoff command.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence

SCRIPTS_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from live_collectors import common, native_argv, native_transcript as native
from live_collectors.policy_data import policy_dict, policy_list, policy_set, policy_tuple
from live_collectors.provider_engine import render_descriptor

MappingLike = dict[str, Any]

SCREENSHOT_PLACEHOLDER = "{screenshot}"
BUILD_MACOS_PROVIDER = "build-macos-apps"
COMPUTER_USE_PROVIDER = "computer-use"
PROJECT_WORKFLOW_PROVIDER = "macos-project-workflow"
CONTRACT_CAPABILITIES = {"ui-automation", "signing", "packaging", "test"}

FORBIDDEN_EXECUTABLES = policy_set("native_macos", "FORBIDDEN_EXECUTABLES")
FORBIDDEN_SUBCOMMANDS = policy_set("native_macos", "FORBIDDEN_SUBCOMMANDS")
SHELL_EXECUTABLES = policy_set("native_macos", "SHELL_EXECUTABLES")
IGNORED_DISCOVERY_PARTS = policy_set("native_macos", "IGNORED_DISCOVERY_PARTS")
SCREENSHOT_PERMISSION_MARKERS = policy_tuple("native_macos", "SCREENSHOT_PERMISSION_MARKERS")
APP_METADATA_TEMPLATE = policy_dict("native_macos", "APP_METADATA_TEMPLATE")
NOTE_PAYLOADS = policy_dict("native_macos", "NOTE_PAYLOADS")
ENVELOPE_PROVENANCE_TEMPLATE = policy_dict("native_macos", "ENVELOPE_PROVENANCE_TEMPLATE")
NATIVE_FINALIZE = policy_dict("native_macos", "NATIVE_FINALIZE")
COMMAND_SPECS = policy_list("native_macos", "COMMAND_SPECS")
REQUIREMENT_CHECKS = policy_list("native_macos", "REQUIREMENT_CHECKS")
ROUTE_CHECKS = policy_list("native_macos", "ROUTE_CHECKS")
UNAVAILABLE_BINDINGS = policy_list("native_macos", "UNAVAILABLE_BINDINGS")
CAPABILITY, ROUTE = NATIVE_FINALIZE["capability"], NATIVE_FINALIZE["route"]
STAR_FORGE = SCRIPTS_ROOT / "star_forge.py"
PLIST_FIELDS = policy_dict("native_macos", "PLIST_FIELDS")
RESULT_TEMPLATE = policy_dict("native_macos", "RESULT_TEMPLATE")
RUN_MISSING_TEMPLATE = policy_dict("native_macos", "RUN_MISSING_TEMPLATE")
RUN_RESULT_TEMPLATE = policy_dict("native_macos", "RUN_RESULT_TEMPLATE")
RUN_SKIPPED_TEMPLATE = policy_dict("native_macos", "RUN_SKIPPED_TEMPLATE")
ROUTE_STATUS_TEMPLATE = policy_dict("native_macos", "ROUTE_STATUS_TEMPLATE")
SUMMARY_TEMPLATE = policy_dict("native_macos", "SUMMARY_TEMPLATE")
OUTPUT_TEMPLATE = policy_dict("native_macos", "OUTPUT_TEMPLATE")
PARSER_ARGUMENTS = policy_list("native_macos", "PARSER_ARGUMENTS")

descriptor = render_descriptor

def result_payload(kind: str, argv: Sequence[str], **fields: Any) -> MappingLike:
    return {**descriptor(RESULT_TEMPLATE, kind=kind, command_argv=list(argv)), **fields}

now = common.now_utc
rel = common.project_relative

write_json = native.write_json
write_text = native.write_text

tree_sha256 = common.tree_sha256

problem = common.blocking_problem

def parse_argv_json(raw: str | None, label: str, *, required: bool) -> tuple[list[str] | None, list[MappingLike]]:
    return native_argv.parse_argv_json(
        raw, label, required=required, validate=validate_argv,
        make_problem=lambda message: problem(message, rule=f"native-macos-{label}-argv"),
    )

executable_name = native_argv.executable_name

def validate_env_wrapper(argv: Sequence[str], label: str) -> list[MappingLike]:
    return native_argv.validate_env_wrapper(
        argv, label, shell_names=SHELL_EXECUTABLES,
        make_problem=lambda message: problem(message, rule="native-macos-shell"),
    )

def validate_argv(argv: Sequence[str], label: str) -> list[MappingLike]:
    problems: list[MappingLike] = []
    if not argv:
        return [problem(f"{label} argv is empty", rule=f"native-macos-{label}-argv")]
    first = executable_name(argv[0])
    if first in SHELL_EXECUTABLES:
        problems.append(problem(f"{label} argv must not invoke a shell", rule="native-macos-shell"))
    problems.extend(validate_env_wrapper(argv, label))
    if first in FORBIDDEN_EXECUTABLES:
        problems.append(problem(f"{label} argv uses forbidden executable `{first}`", rule="native-macos-forbidden-command"))
    for item in argv:
        name = executable_name(item)
        if name in FORBIDDEN_EXECUTABLES or name in FORBIDDEN_SUBCOMMANDS:
            problems.append(problem(f"{label} argv contains forbidden tool `{name}`", rule="native-macos-forbidden-command"))
            break
    return problems

def resolve_executable(project: Path, argv: Sequence[str]) -> str:
    if not argv:
        return ""
    raw = argv[0]
    candidate = Path(raw)
    if not candidate.is_absolute() and "/" in raw:
        candidate = (project / candidate).resolve()
    return str(candidate) if candidate.is_absolute() else shutil.which(raw) or raw

def exit_details(returncode: int | None) -> tuple[int | None, int | None]:
    if returncode is None:
        return None, None
    return (None, abs(returncode)) if returncode < 0 else (returncode, None)

def run_command(
    project: Path,
    out_dir: Path,
    label: str,
    argv: Sequence[str],
    *,
    timeout: float,
) -> tuple[Path, MappingLike]:
    stdout_path = out_dir / f"{label}-stdout.txt"
    stderr_path = out_dir / f"{label}-stderr.txt"
    started = time.monotonic()
    started_at = now()
    stdout = ""
    stderr = ""
    timed_out = False
    returncode: int | None = None
    error = ""
    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(project),
            shell=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
    except OSError as exc:
        error = str(exc)
        stderr = error
    ended_at = now()
    duration = max(0.0, time.monotonic() - started)
    write_text(stdout_path, stdout)
    write_text(stderr_path, stderr)
    exit_code, sig = exit_details(returncode)
    payload = {
        **result_payload(label, argv),
        "cwd": ".",
        "executable_path": resolve_executable(project, argv),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(duration, 3),
        "timeout_seconds": timeout,
        "timed_out": timed_out,
        "returncode": returncode,
        "exit_code": exit_code,
        "signal": sig,
        "stdout_artifact": rel(project, stdout_path),
        "stderr_artifact": rel(project, stderr_path),
        "stdout_bytes": stdout_path.stat().st_size,
        "stderr_bytes": stderr_path.stat().st_size,
        "success": bool(returncode == 0 and not timed_out and not error),
    }
    if error:
        payload["error"] = error
    result_path = write_json(out_dir / f"{label}.json", payload)
    return result_path, payload

def terminate_process(proc: subprocess.Popen[str], *, timeout: float) -> MappingLike:
    result: MappingLike = {
        "attempted": True,
        "method": "terminate",
        "success": False,
        "returncode": None,
    }
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
        result["success"] = proc.poll() is not None
        result["returncode"] = proc.poll()
        return result
    except subprocess.TimeoutExpired:
        result["method"] = "kill"
        try:
            proc.kill()
            proc.wait(timeout=timeout)
            result["success"] = proc.poll() is not None
            result["returncode"] = proc.poll()
        except subprocess.TimeoutExpired:
            result["success"] = False
        return result
    except OSError as exc:
        result["error"] = str(exc)
        return result

def observe_runtime(
    project: Path,
    out_dir: Path,
    argv: Sequence[str] | None,
    *,
    timeout: float,
    readiness_text: str,
    observe_seconds: float,
    cleanup_timeout: float,
) -> tuple[Path, MappingLike]:
    stdout_path = out_dir / "stdout.txt"
    stderr_path = out_dir / "stderr.txt"
    started = time.monotonic()
    started_at = now()
    if not argv:
        write_text(stdout_path, "")
        write_text(stderr_path, "run argv was not provided\n")
        payload = descriptor(
            RUN_MISSING_TEMPLATE, command_argv=[], executable_path="", timeout=timeout,
            readiness_status="missing_command", stdout_artifact=rel(project, stdout_path),
            stderr_artifact=rel(project, stderr_path), cleanup_success=True,
            cleanup_failed=False,
        )
        return write_json(out_dir / "run.json", payload), payload

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    readiness: MappingLike = {"status": "not_requested"} if not readiness_text else {"status": "pending", "text": readiness_text}
    readiness_event = threading.Event()
    stream_lock = threading.Lock()

    def reader(stream: Any, chunks: list[str], stream_name: str) -> None:
        while True:
            piece = stream.readline()
            if piece == "":
                break
            with stream_lock:
                chunks.append(piece)
            if readiness_text and readiness_text in piece and not readiness_event.is_set():
                readiness.update({
                    "status": "observed",
                    "stream": stream_name,
                    "elapsed_seconds": round(max(0.0, time.monotonic() - started), 3),
                })
                readiness_event.set()

    proc: subprocess.Popen[str] | None = None
    launch_error = ""
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=str(project),
            shell=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
    except OSError as exc:
        launch_error = str(exc)

    if proc is None:
        write_text(stdout_path, "")
        write_text(stderr_path, launch_error + "\n")
        payload = descriptor(
            RUN_MISSING_TEMPLATE, command_argv=list(argv),
            executable_path=resolve_executable(project, argv), timeout=timeout,
            readiness_status="launch_failed", stdout_artifact=rel(project, stdout_path),
            stderr_artifact=rel(project, stderr_path), cleanup_success=False,
            cleanup_failed=True,
        )
        payload["error"] = launch_error
        return write_json(out_dir / "run.json", payload), payload

    threads: list[threading.Thread] = []
    if proc.stdout is not None:
        threads.append(threading.Thread(target=reader, args=(proc.stdout, stdout_chunks, "stdout"), daemon=True))
    if proc.stderr is not None:
        threads.append(threading.Thread(target=reader, args=(proc.stderr, stderr_chunks, "stderr"), daemon=True))
    for thread in threads:
        thread.start()

    timed_out = False
    deadline = started + timeout
    stop_reason = "process_exit"
    observed_until = started + max(0.0, observe_seconds)
    while True:
        returncode = proc.poll()
        current = time.monotonic()
        if returncode is not None:
            break
        if readiness_text:
            if readiness_event.is_set():
                stop_reason = "readiness_observed"
                break
        elif current >= observed_until:
            stop_reason = "observed_without_readiness"
            break
        if current >= deadline:
            timed_out = True
            stop_reason = "timeout"
            break
        time.sleep(0.02)

    termination: MappingLike = {"attempted": False}
    if proc.poll() is None:
        termination = terminate_process(proc, timeout=cleanup_timeout)

    for thread in threads:
        thread.join(timeout=cleanup_timeout)
    with stream_lock:
        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
    write_text(stdout_path, stdout)
    write_text(stderr_path, stderr)

    returncode = proc.poll()
    exit_code, sig = exit_details(returncode)
    if readiness_text and readiness.get("status") == "pending":
        readiness["status"] = "missing"
    cleanup = {
        "attempted": True,
        "success": proc.poll() is not None,
        "pid": proc.pid,
    }
    termination_failed = bool(termination.get("attempted") and not termination.get("success"))
    cleanup_failed = bool(not cleanup["success"] or termination_failed)
    intentional_stop = stop_reason in {"readiness_observed", "observed_without_readiness"}
    gui_launch_failed = bool(returncode not in (0, None) and not timed_out and not intentional_stop)
    readiness_ok = readiness.get("status") in {"observed", "not_requested"} or (readiness.get("status") == "missing" and returncode == 0 and not readiness_text)
    success = bool(not timed_out and not cleanup_failed and not gui_launch_failed and readiness_ok)
    payload = descriptor(
        RUN_RESULT_TEMPLATE, command_argv=list(argv), pid=proc.pid,
        executable_path=resolve_executable(project, argv), started_at=started_at,
        ended_at=now(), duration=round(max(0.0, time.monotonic() - started), 3),
        timeout=timeout, timed_out=timed_out, signal=sig, returncode=returncode,
        exit_code=exit_code, readiness=readiness,
        stdout_artifact=rel(project, stdout_path), stderr_artifact=rel(project, stderr_path),
        stdout_bytes=stdout_path.stat().st_size, stderr_bytes=stderr_path.stat().st_size,
        termination=termination, cleanup=cleanup, cleanup_failed=cleanup_failed,
        gui_launch_failed=gui_launch_failed, stop_reason=stop_reason, success=success,
    )
    if not intentional_stop or timed_out or cleanup_failed or gui_launch_failed:
        payload["returncode"] = returncode
        payload["exit_code"] = exit_code
    return write_json(out_dir / "run.json", payload), payload

def app_bundle_candidates(project: Path, app_name: str, bundle_id: str) -> list[Path]:
    candidates: list[Path] = []
    for root, dirs, _files in os.walk(project):
        root_path = Path(root)
        dirs[:] = sorted(
            name for name in dirs
            if name not in IGNORED_DISCOVERY_PARTS and not (root_path / name).is_symlink()
        )
        for name in list(dirs):
            if name.endswith(".app"):
                path = root_path / name
                metadata, _ = read_app_bundle_metadata(project, path, app_name="", bundle_id="", validate_identity=False)
                if metadata_matches(metadata, path, app_name, bundle_id):
                    candidates.append(path)
                dirs.remove(name)
    return sorted(candidates)

def metadata_matches(metadata: MappingLike, path: Path, app_name: str, bundle_id: str) -> bool:
    if bundle_id and metadata.get("bundle_id") == bundle_id:
        return True
    names = {str(metadata.get(key) or "") for key in ("app_name", "display_name")}
    return bool(app_name and app_name in {*names, path.stem})

def read_app_bundle_metadata(
    project: Path,
    raw_path: str | Path,
    *,
    app_name: str,
    bundle_id: str,
    validate_identity: bool = True,
) -> tuple[MappingLike, list[MappingLike]]:
    problems: list[MappingLike] = []
    metadata = dict(APP_METADATA_TEMPLATE)
    try:
        bundle_path = common.safe_project_path(project, raw_path, must_exist=True)
    except ValueError as exc:
        problems.append(problem(f"app bundle path is invalid: {exc}", rule="native-macos-bundle-metadata"))
        return metadata, problems
    metadata["app_bundle"] = rel(project, bundle_path)
    if not bundle_path.is_dir() or bundle_path.suffix != ".app":
        problems.append(problem("app bundle must be an existing .app directory", rule="native-macos-bundle-metadata", path=rel(project, bundle_path)))
        return metadata, problems
    info_plist = bundle_path / "Contents" / "Info.plist"
    metadata["info_plist"] = rel(project, info_plist)
    if not info_plist.exists():
        problems.append(problem("app bundle metadata is missing Contents/Info.plist", rule="native-macos-bundle-metadata", path=rel(project, info_plist)))
        return metadata, problems
    try:
        with info_plist.open("rb") as handle:
            plist = plistlib.load(handle)
    except Exception as exc:
        problems.append(problem(f"app bundle Info.plist is malformed: {exc}", rule="native-macos-bundle-metadata", path=rel(project, info_plist)))
        return metadata, problems
    if not isinstance(plist, dict):
        problems.append(problem("app bundle Info.plist must be a dictionary", rule="native-macos-bundle-metadata", path=rel(project, info_plist)))
        return metadata, problems
    metadata.update({
        field: str(plist.get(plist_key) or (bundle_path.stem if field == "app_name" else ""))
        for field, plist_key in PLIST_FIELDS.items()
    })
    executable_name_value = str(metadata.get("executable_name") or "")
    if executable_name_value:
        executable = bundle_path / "Contents" / "MacOS" / executable_name_value
        metadata["executable_path"] = rel(project, executable)
        metadata["executable_exists"] = executable.exists() and executable.is_file()
    if not metadata["bundle_id"]:
        problems.append(problem("app bundle metadata is missing CFBundleIdentifier", rule="native-macos-bundle-metadata", path=rel(project, info_plist)))
    if not metadata["executable_name"]:
        problems.append(problem("app bundle metadata is missing CFBundleExecutable", rule="native-macos-bundle-metadata", path=rel(project, info_plist)))
    if validate_identity:
        if bundle_id and metadata["bundle_id"] != bundle_id:
            problems.append(problem("app bundle identifier does not match requested bundle id", rule="native-macos-app-identity", path=rel(project, info_plist)))
        if app_name:
            valid_names = {str(metadata.get("app_name") or ""), str(metadata.get("display_name") or ""), bundle_path.stem}
            if app_name not in valid_names:
                problems.append(problem("app bundle name does not match requested app name", rule="native-macos-app-identity", path=rel(project, info_plist)))
    metadata["valid"] = not problems
    return metadata, problems

def resolve_app_bundle(
    project: Path,
    *,
    app_bundle: str,
    app_name: str,
    bundle_id: str,
) -> tuple[Path | None, MappingLike, list[MappingLike]]:
    if app_bundle:
        try:
            bundle_path = common.safe_project_path(project, app_bundle, must_exist=True)
        except ValueError as exc:
            metadata = dict(APP_METADATA_TEMPLATE)
            return None, metadata, [problem(f"app bundle path is invalid: {exc}", rule="native-macos-bundle-metadata")]
        metadata, problems = read_app_bundle_metadata(project, bundle_path, app_name=app_name, bundle_id=bundle_id)
        return bundle_path, metadata, problems

    candidates = app_bundle_candidates(project, app_name, bundle_id)
    if not candidates:
        metadata = dict(APP_METADATA_TEMPLATE)
        return None, metadata, [problem("no app bundle matched the requested app identity", rule="native-macos-bundle-metadata")]
    if len(candidates) > 1:
        metadata = {**APP_METADATA_TEMPLATE, "candidates": [rel(project, item) for item in candidates]}
        return None, metadata, [problem("app bundle discovery is ambiguous", rule="native-macos-app-discovery")]
    metadata, problems = read_app_bundle_metadata(project, candidates[0], app_name=app_name, bundle_id=bundle_id)
    return candidates[0], metadata, problems

def write_notes(out_dir: Path) -> tuple[Path, Path]:
    return tuple(
        write_json(out_dir / f"{kind}-note.json", NOTE_PAYLOADS[kind])
        for kind in ("signing", "packaging")
    )

def image_is_valid(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        head = path.read_bytes()[:26]
    except OSError:
        return False
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24 and head[12:16] == b"IHDR":
        width = int.from_bytes(head[16:20], "big")
        height = int.from_bytes(head[20:24], "big")
        return width > 0 and height > 0
    return head.startswith(b"\xff\xd8\xff")

def screenshot_permission_failed(stderr: str) -> bool:
    return any(marker in stderr.lower() for marker in SCREENSHOT_PERMISSION_MARKERS)

def run_screenshot_command(
    project: Path,
    out_dir: Path,
    argv: Sequence[str] | None,
    *,
    timeout: float,
) -> tuple[Path | None, Path | None, MappingLike | None, list[MappingLike]]:
    if not argv:
        return None, None, None, []
    screenshot_path = out_dir / "screenshot.png"
    if not any(SCREENSHOT_PLACEHOLDER in item for item in argv):
        result = {
            **result_payload("screenshot", argv),
            "success": False,
            "screenshot_permission_failed": False,
            "error": f"screenshot argv must include {SCREENSHOT_PLACEHOLDER}",
        }
        result_path = write_json(out_dir / "screenshot-result.json", result)
        return None, result_path, result, [problem("screenshot argv must include the screenshot output placeholder", rule="native-macos-screenshot")]
    resolved = [item.replace(SCREENSHOT_PLACEHOLDER, str(screenshot_path)) for item in argv]
    result_path, payload = run_command(project, out_dir, "screenshot-result", resolved, timeout=timeout)
    stderr_path = project / str(payload.get("stderr_artifact") or "")
    stderr = stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else ""
    failed_permission = screenshot_permission_failed(stderr)
    success = bool(payload.get("success") and image_is_valid(screenshot_path))
    payload.update({
        "kind": "screenshot",
        "screenshot": rel(project, screenshot_path),
        "screenshot_permission_failed": failed_permission,
        "success": success,
    })
    write_json(result_path, payload)
    problems: list[MappingLike] = []
    if failed_permission:
        problems.append(problem("screenshot command failed because screen recording permission is unavailable", rule="native-macos-screenshot-permission", path=rel(project, screenshot_path)))
    elif not success:
        problems.append(problem("screenshot command did not produce a valid image", rule="native-macos-screenshot", path=rel(project, screenshot_path)))
    return screenshot_path if success else None, result_path, payload, problems

def result_problems(project: Path, label: str, result_path: Path, payload: MappingLike) -> list[MappingLike]:
    failures = {
        "build": [(not payload.get("success"), "build command failed", "native-macos-build")],
        "test": [(not payload.get("success"), "test command failed", "native-macos-test")],
        "run": [
            (payload.get("timed_out") is True, "run command timed out", "native-macos-run-timeout"),
            (payload.get("gui_launch_failed") is True, "run command failed to launch the macOS app", "native-macos-gui-launch"),
            (payload.get("cleanup_failed") is True, "runtime cleanup failed", "native-macos-cleanup"),
            (payload.get("readiness", {}).get("status") == "missing", "runtime readiness signal was not observed", "native-macos-readiness"),
        ],
    }
    return [
        problem(message, rule=rule, path=rel(project, result_path))
        for failed, message, rule in failures.get(label, []) if failed
    ]

def add_result_stream_artifacts(project: Path, artifacts: dict[str, Path], prefix: str, payload: MappingLike) -> None:
    for stream in ("stdout", "stderr"):
        raw = payload.get(f"{stream}_artifact")
        if not raw:
            continue
        try:
            path = common.safe_project_path(project, str(raw), must_exist=True)
        except ValueError:
            continue
        artifacts[f"{prefix}_{stream}"] = path

def collect(args: argparse.Namespace, command_argv: Sequence[str]) -> tuple[int, MappingLike]:
    project = common.assert_collector_project_safe(Path(args.project))
    out_dir = common.live_collector_dir(project, args.task, NATIVE_FINALIZE["collector"])
    source_hash_before = common.compute_source_hash(project)
    problems: list[MappingLike] = []
    unavailable: list[str] = []

    app_name = str(args.app_name or "").strip()
    bundle_id = str(args.bundle_id or "").strip()
    if not app_name and not bundle_id:
        problems.append(problem("native macOS collection requires --app-name or --bundle-id", rule="native-macos-app-identity"))

    commands: dict[str, list[str] | None] = {}
    for label, field, required in COMMAND_SPECS:
        commands[label], parse_problems = parse_argv_json(
            getattr(args, field), label, required=required
        )
        problems.extend(parse_problems)
    build_argv, run_argv, test_argv, screenshot_argv = (
        commands[name] for name in ("build", "run", "test", "screenshot")
    )
    required_capabilities = set(args.required_capability or [])
    requirement_checks = descriptor(
        REQUIREMENT_CHECKS, missing_test=not test_argv, missing_ui=not screenshot_argv,
    )
    problems.extend(
        problem(f"the macOS contract requires {message}", rule=f"native-macos-required-{capability}")
        for capability, missing, message in requirement_checks
        if capability in required_capabilities and missing
    )
    unavailable.extend(provider for field, provider in UNAVAILABLE_BINDINGS if getattr(args, field))
    route_checks = descriptor(
        ROUTE_CHECKS,
        build_conflict=args.build_provider == BUILD_MACOS_PROVIDER and args.build_macos_apps_unavailable,
        ui_conflict=args.ui_provider == COMPUTER_USE_PROVIDER and args.computer_use_unavailable,
        ui_evidence_missing=args.ui_provider == COMPUTER_USE_PROVIDER and not screenshot_argv,
    )
    problems.extend(problem(message, rule="native-macos-capability-route")
                    for failed, message in route_checks if failed)

    app_bundle_path, metadata, metadata_problems = resolve_app_bundle(
        project,
        app_bundle=str(args.app_bundle or ""),
        app_name=app_name,
        bundle_id=bundle_id,
    )
    problems.extend(metadata_problems)
    metadata_path = write_json(out_dir / "app-bundle-metadata.json", metadata)
    signing_path, packaging_path = write_notes(out_dir)

    artifacts: dict[str, Path] = {
        "app_bundle_metadata": metadata_path,
        "signing_note": signing_path,
        "packaging_note": packaging_path,
    }
    build_payload: MappingLike
    if build_argv:
        build_path, build_payload = run_command(project, out_dir, "build", build_argv, timeout=float(args.command_timeout))
    else:
        build_payload = result_payload("build", [], success=False)
        build_path = write_json(out_dir / "build.json", build_payload)
    artifacts["build"] = build_path
    add_result_stream_artifacts(project, artifacts, "build", build_payload)
    problems.extend(result_problems(project, "build", build_path, build_payload))

    if build_payload.get("success"):
        run_path, run_payload = observe_runtime(
            project,
            out_dir,
            run_argv,
            timeout=float(args.run_timeout),
            readiness_text=str(args.readiness_text or ""),
            observe_seconds=float(args.observe_seconds),
            cleanup_timeout=float(args.cleanup_timeout),
        )
    else:
        write_text(out_dir / "stdout.txt", "")
        write_text(out_dir / "stderr.txt", "")
        run_payload = descriptor(
            RUN_SKIPPED_TEMPLATE, command_argv=list(run_argv or []),
            executable_path=resolve_executable(project, run_argv or []),
            timeout=float(args.run_timeout), stdout_artifact=rel(project, out_dir / "stdout.txt"),
            stderr_artifact=rel(project, out_dir / "stderr.txt"),
        )
        run_path = write_json(out_dir / "run.json", run_payload)
    artifacts["run"] = run_path
    artifacts["stdout"] = out_dir / "stdout.txt"
    artifacts["stderr"] = out_dir / "stderr.txt"
    problems.extend(result_problems(project, "run", run_path, run_payload))

    if test_argv:
        if build_payload.get("success"):
            test_path, test_payload = run_command(project, out_dir, "test", test_argv, timeout=float(args.command_timeout))
        else:
            test_payload = result_payload("test", test_argv, success=False, status="skipped", reason="build_failed")
            test_path = write_json(out_dir / "test.json", test_payload)
        artifacts["test"] = test_path
        add_result_stream_artifacts(project, artifacts, "test", test_payload)
        problems.extend(result_problems(project, "test", test_path, test_payload))

    screenshot_path, screenshot_result_path, screenshot_payload, screenshot_problems = run_screenshot_command(
        project,
        out_dir,
        screenshot_argv,
        timeout=float(args.screenshot_timeout),
    )
    if screenshot_result_path:
        artifacts["screenshot_result"] = screenshot_result_path
        if screenshot_payload is not None:
            add_result_stream_artifacts(project, artifacts, "screenshot_result", screenshot_payload)
    if screenshot_path:
        artifacts["screenshot"] = screenshot_path
    if screenshot_problems:
        problems.extend(screenshot_problems)
        unavailable.append("screenshot")

    test_status = ("passed" if test_argv and test_payload.get("success") else
                   "failed" if test_argv else
                   "not-required" if "test" not in required_capabilities else "missing")
    route_status = descriptor(
        ROUTE_STATUS_TEMPLATE, required=sorted(required_capabilities),
        build_provider=args.build_provider, build_tool=resolve_executable(project, build_argv or []),
        build_status="passed" if build_payload.get("success") else "failed",
        ui_provider=args.ui_provider, run_tool=resolve_executable(project, run_argv or []),
        run_status="passed" if run_payload.get("success") else "failed",
        ui_status="passed" if screenshot_path else "not-collected",
        test_provider=args.test_provider, test_status=test_status,
        signing_provider=args.signing_provider,
        signing_status="blocked-no-authority" if "signing" in required_capabilities else "not-required",
        packaging_provider=args.packaging_provider,
        packaging_status="blocked-no-mutation" if "packaging" in required_capabilities else "not-required",
        build_macos_apps="unavailable" if args.build_macos_apps_unavailable else "availability-not-reported",
        computer_use=("used" if args.ui_provider == COMPUTER_USE_PROVIDER else
                      "unavailable" if args.computer_use_unavailable else "availability-not-reported"),
    )
    summary = descriptor(
        SUMMARY_TEMPLATE, app_name=app_name or metadata.get("app_name") or "",
        bundle_id=bundle_id or metadata.get("bundle_id") or "",
        app_bundle=rel(project, app_bundle_path) if app_bundle_path else "",
        app_bundle_metadata=rel(project, metadata_path), pid=run_payload.get("pid"),
        executable_path=run_payload.get("executable_path"),
        readiness=run_payload.get("readiness"), termination=run_payload.get("termination"),
        cleanup=run_payload.get("cleanup"), app_bundle_hash=tree_sha256(app_bundle_path),
        capability_route={"id": ROUTE, **route_status},
    )
    runtime_asset_hash = common.compute_runtime_asset_hash(project)
    provider = str(args.build_provider)
    provenance = descriptor(
        ENVELOPE_PROVENANCE_TEMPLATE, provider=provider, capabilities=dict(route_status),
        build_status=("used" if provider == BUILD_MACOS_PROVIDER else
                      "unavailable" if route_status.get("build_macos_apps") == "unavailable"
                      else "not-selected"),
        computer_use_status=str(route_status.get("computer_use") or "not-selected"),
        project_workflow_status="used" if provider == PROJECT_WORKFLOW_PROVIDER else "not-selected",
    )
    return native.finalize_collection(
        project, out_dir, task=args.task, command_argv=command_argv, artifacts=artifacts,
        summary=summary, problems=problems, unavailable=unavailable,
        source_hash_before=source_hash_before, runtime_asset_hash=runtime_asset_hash,
        tool_versions={"python": sys.version.split()[0], "platform": sys.platform},
        provider=provider, provenance=provenance, config=NATIVE_FINALIZE,
        output_template=OUTPUT_TEMPLATE,
        values={"app_name": app_name,
                "bundle_id": bundle_id or str(metadata.get("bundle_id") or ""),
                "app_bundle_path": app_bundle_path},
        record=args.record, script_path=STAR_FORGE,
    )

def build_parser() -> argparse.ArgumentParser:
    arguments = descriptor(
        PARSER_ARGUMENTS, contract_capabilities=sorted(CONTRACT_CAPABILITIES),
        build_providers=[BUILD_MACOS_PROVIDER, PROJECT_WORKFLOW_PROVIDER],
        ui_providers=[COMPUTER_USE_PROVIDER, PROJECT_WORKFLOW_PROVIDER],
        project_provider=PROJECT_WORKFLOW_PROVIDER, build_provider=BUILD_MACOS_PROVIDER,
    )
    return native.build_parser("Collect native macOS baseline artifacts for Star Forge", arguments)

def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    args = build_parser().parse_args(raw_argv)
    code, output = collect(args, ["python3", "scripts/live_collectors/native_macos.py", *raw_argv])
    print(json.dumps(output, indent=2, sort_keys=True))
    return code

if __name__ == "__main__":
    raise SystemExit(main())
