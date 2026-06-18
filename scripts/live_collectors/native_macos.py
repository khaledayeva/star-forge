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
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from live_collectors import common


MappingLike = dict[str, Any]

COLLECTOR = "native-macos"
SCREENSHOT_PLACEHOLDER = "{screenshot}"
RESULT_SCHEMA = "star-forge.native-macos.result.v1"
NOTE_SCHEMA = "star-forge.native-macos.note.v1"
APP_METADATA_SCHEMA = "star-forge.native-macos.app-bundle-metadata.v1"

FORBIDDEN_EXECUTABLES = {
    "sudo",
    "codesign",
    "pkgbuild",
    "productbuild",
    "notarytool",
    "altool",
    "stapler",
    "osascript",
    "automator",
    "cliclick",
}
FORBIDDEN_SUBCOMMANDS = {"notarytool", "notarize", "stapler"}
SHELL_EXECUTABLES = {"sh", "bash", "zsh", "fish", "csh", "tcsh", "dash", "ksh"}
IGNORED_DISCOVERY_PARTS = {
    ".git",
    ".starforge",
    ".venv",
    "__pycache__",
    "node_modules",
    "the-loop",
}


def now() -> str:
    return common.now_utc()


def rel(project: Path, path: Path) -> str:
    return common.project_relative(project, path)


def write_json(path: Path, payload: MappingLike) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    redacted, _ = common.redact_sensitive_values(payload)
    path.write_text(json.dumps(redacted, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    redacted, _ = common.redact_sensitive_values(text)
    path.write_text(str(redacted), encoding="utf-8")
    return path


def problem(message: str, *, rule: str, path: str = "", severity: str = "high") -> MappingLike:
    return common.blocking_problem(message, rule=rule, path=path, severity=severity)


def parse_argv_json(raw: str | None, label: str, *, required: bool) -> tuple[list[str] | None, list[MappingLike]]:
    problems: list[MappingLike] = []
    text = str(raw or "").strip()
    if not text:
        if required:
            problems.append(problem(f"{label} argv is required", rule=f"native-macos-{label}-argv"))
        return None, problems
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        problems.append(problem(f"{label} argv must be a JSON array: {exc}", rule=f"native-macos-{label}-argv"))
        return None, problems
    if not isinstance(parsed, list):
        problems.append(problem(f"{label} argv must be a JSON array", rule=f"native-macos-{label}-argv"))
        return None, problems
    argv: list[str] = []
    for idx, item in enumerate(parsed):
        if not isinstance(item, str) or not item:
            problems.append(problem(f"{label} argv item {idx + 1} must be a non-empty string", rule=f"native-macos-{label}-argv"))
            return None, problems
        if "\0" in item:
            problems.append(problem(f"{label} argv item {idx + 1} contains a null byte", rule=f"native-macos-{label}-argv"))
            return None, problems
        argv.append(item)
    problems.extend(validate_argv(argv, label))
    return argv if not problems else None, problems


def executable_name(value: str) -> str:
    return Path(value).name.lower()


def validate_argv(argv: Sequence[str], label: str) -> list[MappingLike]:
    problems: list[MappingLike] = []
    if not argv:
        return [problem(f"{label} argv is empty", rule=f"native-macos-{label}-argv")]
    first = executable_name(argv[0])
    if first in SHELL_EXECUTABLES:
        problems.append(problem(f"{label} argv must not invoke a shell", rule="native-macos-shell"))
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
    if candidate.is_absolute():
        return str(candidate)
    if "/" in raw:
        return str((project / candidate).resolve())
    found = shutil.which(raw)
    return found or raw


def exit_details(returncode: int | None) -> tuple[int | None, int | None]:
    if returncode is None:
        return None, None
    if returncode < 0:
        return None, abs(returncode)
    return returncode, None


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
    payload: MappingLike = {
        "schema": RESULT_SCHEMA,
        "kind": label,
        "command_argv": list(argv),
        "shell": False,
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
        payload: MappingLike = {
            "schema": RESULT_SCHEMA,
            "kind": "run",
            "command_argv": [],
            "shell": False,
            "cwd": ".",
            "pid": None,
            "executable_path": "",
            "timeout_seconds": timeout,
            "timed_out": False,
            "returncode": None,
            "exit_code": None,
            "signal": None,
            "readiness": {"status": "missing_command"},
            "stdout_artifact": rel(project, stdout_path),
            "stderr_artifact": rel(project, stderr_path),
            "termination": {"attempted": False},
            "cleanup": {"attempted": False, "success": True},
            "cleanup_failed": False,
            "gui_launch_failed": True,
            "success": False,
        }
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
        payload = {
            "schema": RESULT_SCHEMA,
            "kind": "run",
            "command_argv": list(argv),
            "shell": False,
            "cwd": ".",
            "pid": None,
            "executable_path": resolve_executable(project, argv),
            "timeout_seconds": timeout,
            "timed_out": False,
            "returncode": None,
            "exit_code": None,
            "signal": None,
            "readiness": {"status": "launch_failed"},
            "stdout_artifact": rel(project, stdout_path),
            "stderr_artifact": rel(project, stderr_path),
            "termination": {"attempted": False},
            "cleanup": {"attempted": False, "success": False},
            "cleanup_failed": True,
            "gui_launch_failed": True,
            "success": False,
            "error": launch_error,
        }
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
    payload = {
        "schema": RESULT_SCHEMA,
        "kind": "run",
        "command_argv": list(argv),
        "shell": False,
        "cwd": ".",
        "pid": proc.pid,
        "executable_path": resolve_executable(project, argv),
        "started_at": started_at,
        "ended_at": now(),
        "duration_seconds": round(max(0.0, time.monotonic() - started), 3),
        "timeout_seconds": timeout,
        "timed_out": timed_out,
        "signal": sig,
        "process_returncode": returncode,
        "process_exit_code": exit_code,
        "readiness": readiness,
        "stdout_artifact": rel(project, stdout_path),
        "stderr_artifact": rel(project, stderr_path),
        "stdout_bytes": stdout_path.stat().st_size,
        "stderr_bytes": stderr_path.stat().st_size,
        "termination": termination,
        "cleanup": cleanup,
        "cleanup_failed": cleanup_failed,
        "gui_launch_failed": gui_launch_failed,
        "stop_reason": stop_reason,
        "success": success,
    }
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
    if app_name:
        names = {
            str(metadata.get("app_name") or ""),
            str(metadata.get("display_name") or ""),
            path.stem,
        }
        return app_name in names
    return False


def read_app_bundle_metadata(
    project: Path,
    raw_path: str | Path,
    *,
    app_name: str,
    bundle_id: str,
    validate_identity: bool = True,
) -> tuple[MappingLike, list[MappingLike]]:
    problems: list[MappingLike] = []
    metadata: MappingLike = {
        "schema": APP_METADATA_SCHEMA,
        "metadata_only": True,
        "app_bundle": "",
        "info_plist": "",
        "bundle_id": "",
        "app_name": "",
        "display_name": "",
        "executable_name": "",
        "executable_path": "",
        "executable_exists": False,
        "valid": False,
    }
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
        "bundle_id": str(plist.get("CFBundleIdentifier") or ""),
        "app_name": str(plist.get("CFBundleName") or bundle_path.stem),
        "display_name": str(plist.get("CFBundleDisplayName") or ""),
        "executable_name": str(plist.get("CFBundleExecutable") or ""),
        "short_version": str(plist.get("CFBundleShortVersionString") or ""),
        "bundle_version": str(plist.get("CFBundleVersion") or ""),
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
            metadata = {"schema": APP_METADATA_SCHEMA, "metadata_only": True, "valid": False}
            return None, metadata, [problem(f"app bundle path is invalid: {exc}", rule="native-macos-bundle-metadata")]
        metadata, problems = read_app_bundle_metadata(project, bundle_path, app_name=app_name, bundle_id=bundle_id)
        return bundle_path, metadata, problems

    candidates = app_bundle_candidates(project, app_name, bundle_id)
    if not candidates:
        metadata = {"schema": APP_METADATA_SCHEMA, "metadata_only": True, "valid": False}
        return None, metadata, [problem("no app bundle matched the requested app identity", rule="native-macos-bundle-metadata")]
    if len(candidates) > 1:
        metadata = {
            "schema": APP_METADATA_SCHEMA,
            "metadata_only": True,
            "valid": False,
            "candidates": [rel(project, item) for item in candidates],
        }
        return None, metadata, [problem("app bundle discovery is ambiguous", rule="native-macos-app-discovery")]
    metadata, problems = read_app_bundle_metadata(project, candidates[0], app_name=app_name, bundle_id=bundle_id)
    return candidates[0], metadata, problems


def write_notes(out_dir: Path) -> tuple[Path, Path]:
    signing = {
        "schema": NOTE_SCHEMA,
        "kind": "signing",
        "status": "not_checked",
        "metadata_only": True,
        "satisfies_macos_signing_proof": False,
        "message": "Signing was not checked by the native macOS baseline collector.",
    }
    packaging = {
        "schema": NOTE_SCHEMA,
        "kind": "packaging",
        "status": "not_checked",
        "metadata_only": True,
        "satisfies_macos_notarization_or_packaging_proof": False,
        "message": "Packaging and notarization were not checked by the native macOS baseline collector.",
    }
    return write_json(out_dir / "signing-note.json", signing), write_json(out_dir / "packaging-note.json", packaging)


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
    lowered = stderr.lower()
    return any(marker in lowered for marker in ("permission", "not authorized", "privacy", "screen recording"))


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
            "schema": RESULT_SCHEMA,
            "kind": "screenshot",
            "command_argv": list(argv),
            "shell": False,
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
    out: list[MappingLike] = []
    result_path_text = rel(project, result_path)
    if label == "build" and not payload.get("success"):
        out.append(problem("build command failed", rule="native-macos-build", path=result_path_text))
    if label == "test" and not payload.get("success"):
        out.append(problem("test command failed", rule="native-macos-test", path=result_path_text))
    if label == "run":
        if payload.get("timed_out") is True:
            out.append(problem("run command timed out", rule="native-macos-run-timeout", path=result_path_text))
        if payload.get("gui_launch_failed") is True:
            out.append(problem("run command failed to launch the macOS app", rule="native-macos-gui-launch", path=result_path_text))
        if payload.get("cleanup_failed") is True:
            out.append(problem("runtime cleanup failed", rule="native-macos-cleanup", path=result_path_text))
        if payload.get("readiness", {}).get("status") == "missing":
            out.append(problem("runtime readiness signal was not observed", rule="native-macos-readiness", path=result_path_text))
    return out


def proof_command_argv(
    *,
    app_name: str,
    bundle_id: str,
    app_bundle: Path | None,
    project: Path,
    task: str,
    artifacts: dict[str, Path],
) -> list[str]:
    argv = [
        "python3",
        "scripts/star_forge.py",
        "native-macos-proof",
        "--project",
        ".",
        "--task",
        task,
    ]
    if app_name:
        argv.extend(["--app-name", app_name])
    if bundle_id:
        argv.extend(["--bundle-id", bundle_id])
    argv.extend(["--build-result", rel(project, artifacts["build"])])
    argv.extend(["--run-result", rel(project, artifacts["run"])])
    if "test" in artifacts:
        argv.extend(["--test-result", rel(project, artifacts["test"])])
    if "screenshot" in artifacts:
        argv.extend(["--screenshot", rel(project, artifacts["screenshot"])])
    if app_bundle is not None:
        argv.extend(["--app-bundle", rel(project, app_bundle)])
    argv.extend(["--signing-note", rel(project, artifacts["signing_note"])])
    argv.extend(["--packaging-note", rel(project, artifacts["packaging_note"])])
    argv.append("--strict")
    return argv


def collect(args: argparse.Namespace, command_argv: Sequence[str]) -> tuple[int, MappingLike]:
    project = Path(args.project).resolve()
    out_dir = common.live_collector_dir(project, args.task, COLLECTOR)
    source_hash_before = common.compute_source_hash(project)
    problems: list[MappingLike] = []
    unavailable: list[str] = []

    app_name = str(args.app_name or "").strip()
    bundle_id = str(args.bundle_id or "").strip()
    if not app_name and not bundle_id:
        problems.append(problem("native macOS collection requires --app-name or --bundle-id", rule="native-macos-app-identity"))

    build_argv, parse_problems = parse_argv_json(args.build_command, "build", required=True)
    problems.extend(parse_problems)
    run_argv, parse_problems = parse_argv_json(args.run_command, "run", required=False)
    problems.extend(parse_problems)
    test_argv, parse_problems = parse_argv_json(args.test_command, "test", required=False)
    problems.extend(parse_problems)
    screenshot_argv, parse_problems = parse_argv_json(args.screenshot_command, "screenshot", required=False)
    problems.extend(parse_problems)

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
        build_payload = {"success": False, "schema": RESULT_SCHEMA, "kind": "build", "command_argv": []}
        build_path = write_json(out_dir / "build.json", build_payload)
    artifacts["build"] = build_path
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
        run_payload = {
            "schema": RESULT_SCHEMA,
            "kind": "run",
            "command_argv": list(run_argv or []),
            "shell": False,
            "success": False,
            "status": "skipped",
            "reason": "build_failed",
            "pid": None,
            "executable_path": resolve_executable(project, run_argv or []),
            "timeout_seconds": float(args.run_timeout),
            "timed_out": False,
            "returncode": None,
            "exit_code": None,
            "signal": None,
            "readiness": {"status": "not_run"},
            "termination": {"attempted": False},
            "cleanup": {"attempted": False, "success": True},
            "cleanup_failed": False,
            "gui_launch_failed": False,
        }
        write_text(out_dir / "stdout.txt", "")
        write_text(out_dir / "stderr.txt", "")
        run_payload["stdout_artifact"] = rel(project, out_dir / "stdout.txt")
        run_payload["stderr_artifact"] = rel(project, out_dir / "stderr.txt")
        run_path = write_json(out_dir / "run.json", run_payload)
    artifacts["run"] = run_path
    artifacts["stdout"] = out_dir / "stdout.txt"
    artifacts["stderr"] = out_dir / "stderr.txt"
    problems.extend(result_problems(project, "run", run_path, run_payload))

    if test_argv:
        if build_payload.get("success"):
            test_path, test_payload = run_command(project, out_dir, "test", test_argv, timeout=float(args.command_timeout))
        else:
            test_payload = {"success": False, "schema": RESULT_SCHEMA, "kind": "test", "command_argv": list(test_argv), "status": "skipped", "reason": "build_failed"}
            test_path = write_json(out_dir / "test.json", test_payload)
        artifacts["test"] = test_path
        problems.extend(result_problems(project, "test", test_path, test_payload))

    screenshot_path, screenshot_result_path, _screenshot_payload, screenshot_problems = run_screenshot_command(
        project,
        out_dir,
        screenshot_argv,
        timeout=float(args.screenshot_timeout),
    )
    if screenshot_result_path:
        artifacts["screenshot_result"] = screenshot_result_path
    if screenshot_path:
        artifacts["screenshot"] = screenshot_path
    if screenshot_problems:
        problems.extend(screenshot_problems)
        unavailable.append("screenshot")

    source_hash_after = common.compute_source_hash(project)
    summary: MappingLike = {
        "app_identity": {
            "app_name": app_name or metadata.get("app_name") or "",
            "bundle_id": bundle_id or metadata.get("bundle_id") or "",
        },
        "app_bundle": rel(project, app_bundle_path) if app_bundle_path else "",
        "app_bundle_metadata": rel(project, metadata_path),
        "runtime_observation": {
            "pid": run_payload.get("pid"),
            "executable_path": run_payload.get("executable_path"),
            "readiness": run_payload.get("readiness"),
            "termination": run_payload.get("termination"),
            "cleanup": run_payload.get("cleanup"),
        },
        "signing_note": "not_checked",
        "packaging_note": "not_checked",
    }
    manifest_path = common.write_live_manifest(
        project,
        task=args.task,
        collector=COLLECTOR,
        command_argv=list(command_argv),
        tool_versions={"python": sys.version.split()[0], "platform": sys.platform},
        artifacts=artifacts,
        summary=summary,
        degraded=bool(unavailable),
        unavailable_capabilities=unavailable,
        problems=problems,
        source_hash_before=source_hash_before,
        source_hash_after=source_hash_after,
        runtime_asset_hash=common.compute_runtime_asset_hash(project),
    )
    proof_argv = proof_command_argv(
        app_name=app_name,
        bundle_id=bundle_id or str(metadata.get("bundle_id") or ""),
        app_bundle=app_bundle_path,
        project=project,
        task=args.task,
        artifacts=artifacts,
    )
    output = {
        "schema": "star-forge.native-macos-collector.v1",
        "collector": COLLECTOR,
        "task": args.task,
        "artifact_dir": rel(project, out_dir),
        "manifest": rel(project, manifest_path),
        "degraded": bool(unavailable),
        "problems": problems,
        "proof_command_argv": proof_argv,
        "proof_command": shlex.join(proof_argv),
    }
    if args.record:
        proc = subprocess.run(proof_argv, cwd=str(project), shell=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        output["record"] = {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    return (1 if problems else 0), output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect native macOS baseline artifacts for Star Forge")
    parser.add_argument("--project", default=".")
    parser.add_argument("--task", required=True)
    parser.add_argument("--app-name", default="")
    parser.add_argument("--bundle-id", default="")
    parser.add_argument("--build-command", "--build-argv", dest="build_command", required=True)
    parser.add_argument("--run-command", "--run-argv", dest="run_command", default="")
    parser.add_argument("--test-command", "--test-argv", dest="test_command", default="")
    parser.add_argument("--screenshot-command", "--screenshot-argv", dest="screenshot_command", default="")
    parser.add_argument("--app-bundle", default="")
    parser.add_argument("--readiness-text", default="")
    parser.add_argument("--command-timeout", type=float, default=60.0)
    parser.add_argument("--run-timeout", type=float, default=15.0)
    parser.add_argument("--screenshot-timeout", type=float, default=10.0)
    parser.add_argument("--observe-seconds", type=float, default=0.25)
    parser.add_argument("--cleanup-timeout", type=float, default=2.0)
    parser.add_argument("--record", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    args = build_parser().parse_args(raw_argv)
    code, output = collect(args, ["python3", "scripts/live_collectors/native_macos.py", *raw_argv])
    print(json.dumps(output, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
