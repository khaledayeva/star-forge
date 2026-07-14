#!/usr/bin/env python3
"""Normalize agent-exported XcodeBuildMCP iOS evidence for Star Forge.

This adapter does not call Xcode, simctl, xcrun, or any local shell fallback.
It accepts an MCP transcript and local artifacts produced by an agent-mediated
XcodeBuildMCP workflow, writes task-scoped evidence, and prints the strict
native iOS proof command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
PLUGIN_ROOT = SCRIPTS_ROOT.parent
STAR_FORGE = PLUGIN_ROOT / "scripts" / "star_forge.py"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from live_collectors import common


COLLECTOR = "native-ios"
RESULT_SCHEMA = "star-forge.native-ios.result.v1"
SESSION_SCHEMA = "star-forge.native-ios.session-defaults.v1"
TRANSCRIPT_SCHEMA = "star-forge.native-ios.mcp-transcript.v1"
APP_BUNDLE_SCHEMA = "star-forge.native-ios.app-bundle.v1"

SESSION_TOOLS = {"session_show_defaults"}
BUILD_TOOLS = {
    "build_run_sim",
    "build_sim",
    "build_ios_sim",
    "build_for_sim",
    "build_app_sim",
}
LAUNCH_TOOLS = {
    "build_run_sim",
    "run_sim",
    "run_app_sim",
    "launch_app",
    "launch_app_sim",
    "install_launch_sim",
}
TEST_TOOLS = {
    "test_sim",
    "test_ios_sim",
    "run_tests_sim",
    "build_test_sim",
    "test",
}
SCREENSHOT_TOOLS = {
    "screenshot",
    "screenshot_sim",
    "capture_screenshot",
    "capture_sim_screenshot",
    "sim_screenshot",
}
UI_SNAPSHOT_TOOLS = {
    "ui_snapshot",
    "snapshot_ui",
    "capture_ui_snapshot",
    "get_ui_snapshot",
    "inspect_ui",
    "describe_ui",
    "accessibility_snapshot",
}
LOG_TOOLS = {
    "log",
    "logs",
    "log_stream",
    "stream_logs",
    "get_sim_logs",
    "capture_logs",
    "start_log_capture",
    "stop_log_capture",
}
NATIVE_ACTION_TOOLS = BUILD_TOOLS | LAUNCH_TOOLS | TEST_TOOLS | SCREENSHOT_TOOLS | UI_SNAPSHOT_TOOLS | LOG_TOOLS
DISCOVERY_TOOLS = {
    "discover_projs",
    "discover_projects",
    "find_projects",
    "list_schemes",
    "list_sims",
    "list_simulators",
    "show_build_settings",
}
PREBOOT_OPEN_TOOLS = {"boot_sim", "open_sim", "preboot_sim", "open_simulator"}
SHELL_FALLBACK_TOOLS = {
    "cmd",
    "command",
    "exec_command",
    "execute_command",
    "executecommand",
    "run_command",
    "runcommand",
    "shell",
    "terminal",
    "subprocess",
    "xcodebuild",
    "xcrun",
    "simctl",
    "osascript",
}
SHELL_FALLBACK_TOOL_SUFFIXES = tuple(f"_{name}" for name in sorted(SHELL_FALLBACK_TOOLS))
DIRECT_SHELL_FALLBACK_TOOLS = SHELL_FALLBACK_TOOLS | {"open"}
SHELL_FALLBACK_COMMAND_RE = re.compile(
    r"\b(xcodebuild|xcrun|simctl|osascript)\b|com\.apple\.iphonesimulator|Simulator\.app",
    re.IGNORECASE,
)
SIMULATOR_BUNDLE_ID = "com.apple.iphonesimulator"
COMMAND_FIELD_NAMES = {
    "argv",
    "cmd",
    "cmd_line",
    "cmdline",
    "command",
    "command_args",
    "commandargs",
    "command_argv",
    "commandargv",
    "command_line",
    "commandline",
    "shell_cmd",
    "shellcmd",
    "shell_command",
    "shellcommand",
}
COMMAND_EXECUTION_FIELD_NAMES = {
    "cmd",
    "cmd_line",
    "cmdline",
    "command",
    "command_args",
    "commandargs",
    "command_line",
    "commandline",
}
SHELL_COMMAND_FIELD_NAMES = {
    "shell_cmd",
    "shellcmd",
    "shell_command",
    "shellcommand",
}
ARGV_EXECUTABLE_FIELD_NAMES = {
    "argv",
    "command_argv",
    "commandargv",
}
SHELL_EXECUTABLE_NAMES = {
    "bash",
    "fish",
    "powershell",
    "pwsh",
    "sh",
    "zsh",
}
ENV_NO_OPERAND_OPTIONS = {
    "-i",
    "--ignore-environment",
    "-0",
    "--null",
    "-v",
    "--debug",
}
ENV_OPERAND_OPTIONS = {
    "-u",
    "--unset",
    "-C",
    "--chdir",
    "-P",
    "--path",
}
ENV_OPERAND_PREFIXES = (
    "--unset=",
    "--chdir=",
    "--path=",
)
ENV_SPLIT_OPTIONS = {"-S", "--split-string"}
MAX_ENV_WRAPPER_DEPTH = 16


MappingLike = dict[str, Any]


def rel(project: Path, path: Path) -> str:
    return common.project_relative(project, path)


def problem(message: str, *, rule: str, path: str = "", severity: str = "high", blocking: bool = True) -> MappingLike:
    return {
        "severity": severity,
        "rule": rule,
        "message": message,
        "path": path,
        "blocking": blocking,
    }


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    redacted, _report = common.redact_sensitive_values(payload)
    path.write_text(json.dumps(redacted, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    redacted, _report = common.redact_sensitive_values(text)
    path.write_text(str(redacted), encoding="utf-8")
    return path


def input_display_path(project: Path, path: Path | str) -> str:
    candidate = Path(str(path))
    try:
        resolved = candidate.resolve()
    except OSError:
        return common.sanitize_external_path(candidate)
    try:
        return rel(project, resolved)
    except ValueError:
        return common.sanitize_external_path(resolved)


def resolve_input_path(project: Path, raw_path: str, label: str, problems: list[MappingLike], *, rule: str) -> Path | None:
    raw = str(raw_path or "").strip()
    if not raw:
        problems.append(problem(f"{label} is required", rule=rule))
        return None
    if "\0" in raw:
        problems.append(problem(f"{label} path contains a null byte", rule="native-ios-input-path"))
        return None
    if raw.startswith("~"):
        problems.append(problem(f"{label} path must not be home-relative", rule="native-ios-input-path", path="[home]"))
        return None
    try:
        resolved = common.safe_project_path(project, raw, must_exist=False)
    except ValueError:
        problems.append(problem(f"{label} path must stay inside the project", rule=rule, path=common.sanitize_external_path(Path(raw))))
        return None
    if not resolved.exists():
        problems.append(problem(f"{label} does not exist", rule=rule, path=input_display_path(project, resolved)))
        return None
    if not resolved.is_file():
        problems.append(problem(f"{label} must be a file", rule=rule, path=input_display_path(project, resolved)))
        return None
    return resolved


def copy_json_artifact(
    project: Path,
    out_dir: Path,
    raw_path: str,
    dest_name: str,
    label: str,
    problems: list[MappingLike],
    *,
    required: bool,
    missing_rule: str,
    require_object: bool = True,
    fallback: Any | None = None,
) -> tuple[Path, Any]:
    dest = out_dir / dest_name
    if not str(raw_path or "").strip():
        if required:
            problems.append(problem(f"{label} is required", rule=missing_rule))
        payload = fallback if fallback is not None else {"schema": RESULT_SCHEMA, "kind": label, "success": False, "status": "missing"}
        write_json(dest, payload)
        return dest, payload
    src = resolve_input_path(project, raw_path, label, problems, rule=missing_rule)
    if src is None:
        payload = fallback if fallback is not None else {"schema": RESULT_SCHEMA, "kind": label, "success": False, "status": "missing"}
        write_json(dest, payload)
        return dest, payload
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except Exception as exc:
        problems.append(problem(f"{label} is malformed JSON: {exc}", rule="native-ios-json", path=input_display_path(project, src)))
        payload = {"schema": RESULT_SCHEMA, "kind": label, "success": False, "status": "malformed_json", "error": str(exc)}
    if require_object and not isinstance(payload, dict):
        problems.append(problem(f"{label} must be a JSON object", rule="native-ios-artifact-shape", path=input_display_path(project, src)))
        payload = {"schema": RESULT_SCHEMA, "kind": label, "success": False, "status": "wrong_shape"}
    write_json(dest, payload)
    return dest, payload


def copy_image_artifact(
    project: Path,
    out_dir: Path,
    raw_path: str,
    problems: list[MappingLike],
) -> Path | None:
    if not str(raw_path or "").strip():
        return None
    src = resolve_input_path(project, raw_path, "screenshot", problems, rule="native-ios-screenshot")
    dest = out_dir / "screenshot.png"
    if src is None:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copyfile(src, dest)
    record = common.artifact_record(project, dest, kind="screenshot", must_exist=True)
    if not record.get("valid_image") or int(record.get("bytes") or 0) <= 0:
        problems.append(problem("screenshot must be a decodable non-empty PNG or JPEG", rule="native-ios-screenshot", path=record.get("path", rel(project, dest))))
    return dest


def copy_log_artifact(project: Path, out_dir: Path, raw_path: str, max_bytes: int, problems: list[MappingLike]) -> Path | None:
    if not str(raw_path or "").strip():
        return None
    src = resolve_input_path(project, raw_path, "log", problems, rule="native-ios-log")
    if src is None:
        return None
    data = src.read_bytes()
    truncated = len(data) > max_bytes
    text = data[:max_bytes].decode("utf-8", "replace")
    if truncated:
        text += "\n[TRUNCATED_BY_STAR_FORGE_NATIVE_IOS_ADAPTER]\n"
    return write_text(out_dir / "log.txt", text)


def normalize_tool_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not name:
        return ""
    name = name.replace("-", "_").replace(".", "_")
    lowered = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower()
    for marker in ("mcp__xcodebuildmcp__", "xcodebuildmcp__", "mcp_xcodebuildmcp_"):
        if marker in lowered:
            lowered = lowered.split(marker, 1)[1]
    return lowered


def first_present(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def raw_call_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("calls", "tool_calls", "events", "transcript", "entries"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    messages = payload.get("messages")
    if isinstance(messages, list):
        return messages
    return []


def normalize_call(item: Any, index: int) -> MappingLike:
    if isinstance(item, str):
        return {"index": index, "tool": normalize_tool_name(item), "raw_tool": item, "args": {}, "result": {}}
    if not isinstance(item, dict):
        return {"index": index, "tool": "", "raw_tool": "", "args": {}, "result": {}}
    raw_tool = first_present(item, ("tool", "tool_name", "name", "function", "mcp_tool", "action")) or ""
    tool = normalize_tool_name(raw_tool)
    args = first_present(item, ("arguments", "args", "input", "parameters")) or {}
    result = first_present(item, ("result", "output", "response")) or {}
    if not isinstance(args, dict):
        args = {"value": args}
    if not isinstance(result, dict):
        result = {"value": result}
    return {
        "index": index,
        "tool": tool,
        "raw_tool": raw_tool,
        "args": args,
        "result": result,
        "parallel": bool(item.get("parallel") or item.get("parallel_tool_call")),
        "parallel_group": item.get("parallel_group") or item.get("batch_id"),
        "type": item.get("type") or item.get("kind") or "",
    }


def transcript_calls(payload: Any) -> list[MappingLike]:
    return [normalize_call(item, index) for index, item in enumerate(raw_call_items(payload))]


def extract_transcript_provenance(payload: Any, args: argparse.Namespace) -> MappingLike:
    if not isinstance(payload, dict):
        return {
            "tool_surface": "mcp",
            "server": args.mcp_server,
            "server_version": args.mcp_version,
            "exported_by": str(args.agent_id or ""),
            "explicit": False,
        }
    mcp = payload.get("mcp") if isinstance(payload.get("mcp"), dict) else {}
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
    explicit = bool(mcp or provenance or payload.get("tool_surface") or payload.get("server") or payload.get("exported_by"))
    return {
        "tool_surface": str(mcp.get("tool_surface") or provenance.get("tool_surface") or payload.get("tool_surface") or args.mcp_server and "mcp" or ""),
        "server": str(mcp.get("server") or provenance.get("server") or payload.get("server") or args.mcp_server),
        "server_version": str(mcp.get("version") or provenance.get("version") or payload.get("version") or args.mcp_version),
        "exported_by": str(payload.get("exported_by") or provenance.get("exported_by") or args.agent_id or ""),
        "explicit": explicit,
    }


def merge_manifest_provenance(provenance: MappingLike, args: argparse.Namespace) -> MappingLike:
    manifest_provenance = getattr(args, "manifest_mcp_provenance", None)
    if provenance.get("explicit") or not isinstance(manifest_provenance, dict):
        return provenance
    return {
        "tool_surface": str(manifest_provenance.get("tool_surface") or ""),
        "server": str(manifest_provenance.get("server") or ""),
        "server_version": str(manifest_provenance.get("version") or manifest_provenance.get("server_version") or ""),
        "exported_by": str(manifest_provenance.get("exported_by") or manifest_provenance.get("agent_id") or ""),
        "explicit": True,
        "source": "manifest-summary",
    }


def transcript_source_hash(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("source_hash", "source_hash_before", "source_hash_after"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    provenance = payload.get("provenance")
    if isinstance(provenance, dict):
        value = provenance.get("source_hash")
        if isinstance(value, str):
            return value
    return ""


def extract_capabilities(payload: Any) -> set[str] | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("capabilities") or payload.get("available_tools")
    mcp = payload.get("mcp")
    if raw is None and isinstance(mcp, dict):
        raw = mcp.get("capabilities") or mcp.get("available_tools")
    if raw is None:
        return None
    if not isinstance(raw, list):
        return set()
    return {normalize_tool_name(item) for item in raw if normalize_tool_name(item)}


def mcp_marked_unavailable(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    mcp = payload.get("mcp")
    if isinstance(mcp, dict) and mcp.get("available") is False:
        return True
    if payload.get("available") is False or payload.get("xcodebuildmcp_available") is False:
        return True
    unavailable = payload.get("unavailable_capabilities")
    return isinstance(unavailable, list) and any("xcodebuild" in str(item).lower() or "mcp" in str(item).lower() for item in unavailable)


def extract_session_defaults(transcript: Any) -> MappingLike | None:
    for call in transcript_calls(transcript):
        if call.get("tool") not in SESSION_TOOLS:
            continue
        result = call.get("result")
        if isinstance(result, dict) and result:
            return dict(result)
        args = call.get("args")
        if isinstance(args, dict) and args:
            return dict(args)
    return None


def nested_get(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for inner_key in ("name", "id", "udid", "runtime", "scheme"):
                inner = value.get(inner_key)
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
    return ""


def session_scheme(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return nested_get(payload, ("scheme", "active_scheme", "default_scheme", "configured_scheme"))


def session_simulator(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return nested_get(payload, ("simulator", "device", "destination", "active_simulator", "default_simulator"))


def session_runtime(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return nested_get(payload, ("runtime", "simulator_runtime", "os", "os_version", "platform_runtime"))


def session_udid(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return nested_get(payload, ("udid", "simulator_udid", "device_udid"))


def session_needs_discovery(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return True
    if payload.get("needs_discovery") is True or payload.get("missing_defaults") is True:
        return True
    missing = payload.get("missing")
    if isinstance(missing, list) and missing:
        return True
    return not (session_scheme(payload) and session_simulator(payload))


def bool_success(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    for key in ("success", "ok", "passed"):
        if key in payload:
            return bool(payload.get(key))
    status = str(payload.get("status") or payload.get("conclusion") or "").lower()
    if status in {"success", "succeeded", "passed", "pass", "ok"}:
        return True
    return False


def result_failure(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "result must be a JSON object"
    for key in ("timed_out", "timeout", "crashed"):
        if payload.get(key) is True:
            return f"{key} is true"
    for key in ("success", "ok", "passed"):
        if key in payload and payload.get(key) is False:
            return f"{key} is false"
    for key in ("returncode", "exit_code"):
        if key in payload:
            try:
                if int(payload.get(key)) != 0:
                    return f"{key} is nonzero"
            except (TypeError, ValueError):
                return f"{key} is not numeric"
    status = str(payload.get("status") or payload.get("conclusion") or "").lower()
    if status in {"failed", "failure", "error", "timed_out", "timeout", "cancelled", "crashed", "skipped"}:
        return f"status is {status}"
    if not bool_success(payload):
        return "success was not recorded"
    return None


def result_runtime(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("simulator_runtime") or payload.get("runtime") or payload.get("os_version") or "").strip()


def result_udid(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("simulator_udid") or payload.get("udid") or payload.get("device_udid") or "").strip()


def result_provenance(payload: Any) -> MappingLike | None:
    if not isinstance(payload, dict):
        return None
    provenance = payload.get("mcp_provenance") if isinstance(payload.get("mcp_provenance"), dict) else payload.get("provenance")
    return dict(provenance) if isinstance(provenance, dict) else None


def validate_result_artifact_contract(
    label: str,
    expected_kind: str,
    payload: Any,
    problems: list[MappingLike],
    *,
    path: str = "",
    expected_runtime: str = "",
    expected_udid: str = "",
) -> None:
    if not isinstance(payload, dict):
        problems.append(problem(f"{label} must be a JSON object", rule="native-ios-result", path=path))
        return
    if payload.get("schema") != RESULT_SCHEMA:
        problems.append(problem(f"{label} must use schema {RESULT_SCHEMA}", rule="native-ios-result", path=path))
    if str(payload.get("kind") or "") != expected_kind:
        problems.append(problem(f"{label} kind must be {expected_kind}", rule="native-ios-result", path=path))
    if payload.get("success") is not True:
        problems.append(problem(f"{label} must include success true", rule="native-ios-result", path=path))
    if payload.get("shell") is True:
        problems.append(problem(f"{label} must not record shell execution", rule="native-ios-result", path=path))

    command_entries = command_field_entries(payload)
    if any(command_field_uses_shell_fallback(field_name, value) for field_name, value in command_entries):
        problems.append(problem(f"{label} must not include shell fallback command evidence", rule="native-ios-shell-fallback", path=path))

    runtime = result_runtime(payload)
    udid = result_udid(payload)
    provenance = result_provenance(payload)
    if not runtime and not udid and provenance is None:
        problems.append(problem(f"{label} must include simulator runtime, simulator UDID, or MCP provenance", rule="native-ios-result", path=path))
    if runtime and expected_runtime and runtime != expected_runtime:
        problems.append(problem(f"{label} runtime does not match manifest summary", rule="native-ios-result", path=path))
    if udid and expected_udid and udid != expected_udid:
        problems.append(problem(f"{label} simulator UDID does not match manifest summary", rule="native-ios-result", path=path))
    if provenance is not None:
        tool_surface = str(provenance.get("tool_surface") or "").strip().lower()
        server = str(provenance.get("server") or "").strip()
        if tool_surface != "mcp":
            problems.append(problem(f"{label} provenance must use MCP tool surface", rule="native-ios-result", path=path))
        if server != "XcodeBuildMCP":
            problems.append(problem(f"{label} provenance server must be XcodeBuildMCP", rule="native-ios-result", path=path))


def validate_result(label: str, payload: Any, problems: list[MappingLike], path: Path, project: Path) -> None:
    failure = result_failure(payload)
    if failure:
        problems.append(problem(f"{label} failed: {failure}", rule=f"native-ios-{label}", path=rel(project, path)))


def validate_ui_snapshot(payload: Any, path: Path, project: Path, problems: list[MappingLike]) -> bool:
    if not isinstance(payload, dict):
        problems.append(problem("UI snapshot must be a JSON object", rule="native-ios-ui-snapshot", path=rel(project, path)))
        return False
    markers = ("tree", "elements", "children", "windows", "screens", "app", "snapshot")
    if not any(key in payload for key in markers):
        problems.append(problem("UI snapshot must contain inspectable UI structure", rule="native-ios-ui-snapshot", path=rel(project, path)))
        return False
    return True


def app_bundle_hash(project: Path, raw_path: str, problems: list[MappingLike]) -> MappingLike:
    if not str(raw_path or "").strip():
        return {"available": False}
    try:
        bundle = common.safe_project_path(project, raw_path, must_exist=True)
    except ValueError as exc:
        problems.append(problem(f"app bundle path is invalid: {exc}", rule="native-ios-app-bundle", path=common.sanitize_external_path(Path(str(raw_path)))))
        return {"available": False, "error": str(exc)}
    digest = hashlib.sha256()
    if bundle.is_file():
        return {"available": True, "path": rel(project, bundle), "sha256": common.file_sha256(bundle), "kind": "file"}
    if not bundle.is_dir():
        problems.append(problem("app bundle must be a file or directory", rule="native-ios-app-bundle", path=rel(project, bundle)))
        return {"available": False, "path": rel(project, bundle)}
    files = sorted(path for path in bundle.glob("**/*") if path.is_file() and not path.is_symlink())
    for path in files:
        bundle_rel = str(path.relative_to(bundle))
        digest.update(bundle_rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(common.file_sha256(path).encode("ascii"))
        digest.update(b"\0")
    return {"available": True, "path": rel(project, bundle), "sha256": digest.hexdigest(), "kind": "directory", "files": len(files)}


def normalize_command_field_name(raw: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(raw or "")).strip("_").lower()


def compact_command_field_name(field_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", field_name.lower())


def command_field_name_contains(field_name: str, *needles: str) -> bool:
    compact = compact_command_field_name(field_name)
    return all(needle in compact for needle in needles)


def command_field_is_candidate(field_name: str) -> bool:
    return (
        field_name in COMMAND_FIELD_NAMES
        or command_field_name_contains(field_name, "command", "line")
        or command_field_name_contains(field_name, "shell", "command")
        or compact_command_field_name(field_name) == "cmdline"
    )


def command_field_entries(value: Any) -> list[tuple[str, Any]]:
    entries: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = normalize_command_field_name(key)
            if command_field_is_candidate(normalized):
                entries.append((normalized, child))
            entries.extend(command_field_entries(child))
    elif isinstance(value, list):
        for child in value:
            entries.extend(command_field_entries(child))
    return entries


def command_field_values(value: Any) -> list[Any]:
    return [child for _field_name, child in command_field_entries(value)]


def command_parts_and_text(command: Any) -> tuple[list[str], str]:
    if isinstance(command, list):
        parts = [str(item) for item in command]
        return parts, " ".join(parts)
    text = str(command)
    try:
        return shlex.split(text), text
    except ValueError:
        return text.split(), text


def executable_name(raw: Any) -> str:
    name = Path(str(raw or "").strip().strip("'\"")).name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def is_env_assignment(value: str) -> bool:
    return bool(value) and "=" in value and not value.startswith("-") and value.split("=", 1)[0].isidentifier()


def split_env_string(raw: str) -> list[str] | None:
    try:
        return shlex.split(raw)
    except ValueError:
        return None


def env_tail_uses_shell_fallback(tokens: Sequence[str], *, depth: int = 0) -> bool:
    if depth > MAX_ENV_WRAPPER_DEPTH:
        return True
    idx = 1 if tokens and executable_name(tokens[0]) == "env" else 0
    while idx < len(tokens):
        item = str(tokens[idx])
        if item == "--":
            idx += 1
            break
        if is_env_assignment(item):
            idx += 1
            continue
        if item in ENV_SPLIT_OPTIONS:
            if idx + 1 >= len(tokens) or idx + 2 < len(tokens):
                return True
            split_tokens = split_env_string(str(tokens[idx + 1]))
            return not split_tokens or env_tail_uses_shell_fallback(split_tokens, depth=depth + 1)
        if item.startswith("--split-string="):
            if idx + 1 < len(tokens):
                return True
            split_tokens = split_env_string(item.split("=", 1)[1])
            return not split_tokens or env_tail_uses_shell_fallback(split_tokens, depth=depth + 1)
        if item.startswith("-S") and item != "-S":
            if idx + 1 < len(tokens):
                return True
            split_tokens = split_env_string(item[2:])
            return not split_tokens or env_tail_uses_shell_fallback(split_tokens, depth=depth + 1)
        if item in ENV_OPERAND_OPTIONS:
            if idx + 1 >= len(tokens):
                return True
            idx += 2
            continue
        if item.startswith(ENV_OPERAND_PREFIXES):
            idx += 1
            continue
        if item.startswith("-P") and item != "-P":
            idx += 1
            continue
        if item.startswith("-u") and item != "-u":
            idx += 1
            continue
        if item.startswith("-C") and item != "-C":
            idx += 1
            continue
        if item.startswith("-"):
            if item in ENV_NO_OPERAND_OPTIONS:
                idx += 1
                continue
            return True
        break
    if idx >= len(tokens):
        return True
    target_tokens = [str(item) for item in tokens[idx:]]
    target_name = executable_name(target_tokens[0])
    if target_name in SHELL_EXECUTABLE_NAMES:
        return True
    if target_name == "env":
        return env_tail_uses_shell_fallback(target_tokens, depth=depth + 1)
    return False


def env_wrapper_uses_shell_fallback(command: Any) -> bool:
    parts, _text = command_parts_and_text(command)
    if not parts or executable_name(parts[0]) != "env":
        return False
    return env_tail_uses_shell_fallback(parts)


def argv_executable_is_shell(command: Any) -> bool:
    parts, _text = command_parts_and_text(command)
    if not parts:
        return False
    return executable_name(parts[0]) in SHELL_EXECUTABLE_NAMES or env_wrapper_uses_shell_fallback(parts)


def command_uses_open_simulator_fallback(command: Any) -> bool:
    parts, text = command_parts_and_text(command)
    if not parts:
        return False
    executable = executable_name(parts[0])
    if executable == "open":
        return True
    lowered_parts = [part.strip().strip("'\"").lower() for part in parts]
    if any(part == SIMULATOR_BUNDLE_ID for part in lowered_parts):
        return True
    if any(part.endswith("simulator.app") for part in lowered_parts):
        return True
    return bool(re.search(r"(?i)(^|\s|/|')Simulator\.app(\s|/|'|$)", text))


def command_value_uses_shell_fallback(command: Any) -> bool:
    if isinstance(command, dict):
        return any(command_value_uses_shell_fallback(child) for child in command.values())
    if isinstance(command, list):
        if env_wrapper_uses_shell_fallback(command):
            return True
        if command_uses_open_simulator_fallback(command):
            return True
        combined = " ".join(str(item) for item in command)
        if SHELL_FALLBACK_COMMAND_RE.search(combined):
            return True
        return any(command_value_uses_shell_fallback(child) for child in command)
    if isinstance(command, str):
        if env_wrapper_uses_shell_fallback(command):
            return True
        if SHELL_FALLBACK_COMMAND_RE.search(command):
            return True
        return command_uses_open_simulator_fallback(command)
    return False


def command_field_uses_shell_fallback(field_name: str, command: Any) -> bool:
    if field_name in SHELL_COMMAND_FIELD_NAMES or command_field_name_contains(field_name, "shell", "command"):
        return True
    if (
        field_name in COMMAND_EXECUTION_FIELD_NAMES
        or command_field_name_contains(field_name, "command", "line")
        or compact_command_field_name(field_name) == "cmdline"
    ):
        return True
    if field_name in ARGV_EXECUTABLE_FIELD_NAMES and argv_executable_is_shell(command):
        return True
    return command_value_uses_shell_fallback(command)


def call_uses_shell_fallback(call: Mapping[str, Any]) -> bool:
    tool = str(call.get("tool") or "")
    if tool in DIRECT_SHELL_FALLBACK_TOOLS or tool.endswith(SHELL_FALLBACK_TOOL_SUFFIXES):
        return True
    commands: list[tuple[str, Any]] = []
    for payload_name in ("args", "result"):
        payload = call.get(payload_name)
        if isinstance(payload, (dict, list)):
            commands.extend(command_field_entries(payload))
    if not commands:
        return False
    return any(command_field_uses_shell_fallback(field_name, command) for field_name, command in commands)


def validate_transcript(
    transcript: Any,
    session: Any,
    *,
    scheme: str,
    simulator: str,
    current_source_hash: str,
    has_screenshot: bool,
    has_ui_snapshot: bool,
    args: argparse.Namespace,
) -> tuple[MappingLike, list[MappingLike], list[str]]:
    problems: list[MappingLike] = []
    unavailable: list[str] = []
    calls = transcript_calls(transcript)
    provenance = merge_manifest_provenance(extract_transcript_provenance(transcript, args), args)
    if not provenance.get("explicit"):
        problems.append(problem("XcodeBuildMCP transcript requires explicit MCP provenance", rule="native-ios-mcp-provenance"))
    if str(provenance.get("tool_surface") or "").lower() != "mcp":
        problems.append(problem("XcodeBuildMCP evidence must come from an MCP tool surface", rule="native-ios-mcp-provenance"))
    if str(provenance.get("server") or "") != "XcodeBuildMCP":
        problems.append(problem("XcodeBuildMCP transcript must name server XcodeBuildMCP", rule="native-ios-mcp-provenance"))
    if not str(provenance.get("exported_by") or "").strip():
        problems.append(problem("XcodeBuildMCP transcript requires exported_by or agent id", rule="native-ios-mcp-provenance"))
    if args.mcp_unavailable or mcp_marked_unavailable(transcript):
        unavailable.append("xcodebuildmcp")
        problems.append(problem("XcodeBuildMCP is unavailable", rule="native-ios-mcp-unavailable"))
    if not calls:
        unavailable.append("mcp-transcript")
        problems.append(problem("MCP transcript is missing tool calls", rule="native-ios-mcp-transcript"))

    source = transcript_source_hash(transcript) or str(getattr(args, "manifest_source_hash", "") or "")
    if not source:
        problems.append(problem("MCP transcript requires a source hash bound to the current project", rule="native-ios-source"))
    elif source != current_source_hash:
        problems.append(problem("MCP transcript source hash does not match current source", rule="native-ios-source"))

    capabilities = extract_capabilities(transcript)
    if capabilities is not None:
        required = set(SESSION_TOOLS)
        for call in calls:
            tool = str(call.get("tool") or "")
            if tool in NATIVE_ACTION_TOOLS:
                required.add(tool)
        missing = sorted(name for name in required if name not in capabilities)
        if missing:
            unavailable.extend(f"xcodebuildmcp:{name}" for name in missing)
            problems.append(problem("XcodeBuildMCP transcript is missing required capabilities: " + ", ".join(missing), rule="native-ios-mcp-capability"))

    session_indexes = [int(call["index"]) for call in calls if call.get("tool") in SESSION_TOOLS]
    if not session_indexes:
        problems.append(problem("MCP transcript must include session_show_defaults", rule="native-ios-session-defaults"))
        first_session = None
    else:
        first_session = min(session_indexes)

    categories = {
        "build": False,
        "launch": False,
        "test": False,
        "screenshot": False,
        "ui_snapshot": False,
        "log": False,
    }
    for call in calls:
        tool = str(call.get("tool") or "")
        idx = int(call.get("index") or 0)
        if call_uses_shell_fallback(call):
            problems.append(problem("iOS evidence must not use xcodebuild, xcrun, simctl, shell, or exec fallback", rule="native-ios-shell-fallback"))
        if tool in PREBOOT_OPEN_TOOLS:
            problems.append(problem("transcript must not pre-boot or pre-open the simulator", rule="native-ios-preboot"))
        if tool in DISCOVERY_TOOLS:
            if call.get("parallel") or call.get("parallel_group"):
                problems.append(problem("discovery must not be run in parallel", rule="native-ios-parallel-discovery"))
            if first_session is None or idx < first_session:
                problems.append(problem("discovery must not run before session_show_defaults", rule="native-ios-discovery"))
            elif not session_needs_discovery(session):
                problems.append(problem("discovery appears speculative because session defaults were already configured", rule="native-ios-discovery"))
        if tool in NATIVE_ACTION_TOOLS:
            if first_session is None or idx < first_session:
                problems.append(problem("session_show_defaults must appear before native build, run, test, screenshot, UI snapshot, or log actions", rule="native-ios-tool-order"))
        if tool in BUILD_TOOLS:
            categories["build"] = True
        if tool in LAUNCH_TOOLS:
            categories["launch"] = True
        if tool in TEST_TOOLS:
            categories["test"] = True
        if tool in SCREENSHOT_TOOLS:
            categories["screenshot"] = True
        if tool in UI_SNAPSHOT_TOOLS:
            categories["ui_snapshot"] = True
        if tool in LOG_TOOLS:
            categories["log"] = True
        call_args = call.get("args") if isinstance(call.get("args"), dict) else {}
        call_scheme = str(call_args.get("scheme") or call_args.get("scheme_name") or "").strip()
        if call_scheme and call_scheme != scheme:
            problems.append(problem("transcript tool call used a different scheme than the requested scheme", rule="native-ios-scheme"))
        call_sim = str(call_args.get("simulator") or call_args.get("device") or call_args.get("destination") or "").strip()
        if call_sim and simulator and simulator not in call_sim and call_sim not in simulator:
            problems.append(problem("transcript tool call used a different simulator than the requested simulator", rule="native-ios-simulator"))

    if not categories["build"]:
        problems.append(problem("MCP transcript must include a build action", rule="native-ios-transcript-build"))
    if not categories["launch"]:
        problems.append(problem("MCP transcript must include a launch or run action", rule="native-ios-transcript-launch"))
    if not categories["test"]:
        problems.append(problem("MCP transcript must include a test action", rule="native-ios-transcript-test"))
    if has_screenshot and not categories["screenshot"]:
        problems.append(problem("screenshot evidence requires a matching MCP screenshot action", rule="native-ios-transcript-ui"))
    if has_ui_snapshot and not categories["ui_snapshot"]:
        problems.append(problem("UI snapshot evidence requires a matching MCP UI snapshot action", rule="native-ios-transcript-ui"))

    return {
        "provenance": provenance,
        "tool_call_count": len(calls),
        "categories": categories,
        "capabilities_declared": capabilities is not None,
    }, problems, sorted(set(unavailable))


def validate_session_defaults(session: Any, *, scheme: str, simulator: str, runtime: str, udid: str, problems: list[MappingLike]) -> MappingLike:
    if not isinstance(session, dict):
        problems.append(problem("session-defaults.json must be a JSON object", rule="native-ios-session-defaults"))
        return {"scheme": "", "simulator": "", "runtime": runtime, "udid": udid}
    found_scheme = session_scheme(session)
    found_simulator = session_simulator(session)
    found_runtime = runtime or session_runtime(session)
    found_udid = udid or session_udid(session)
    if found_scheme and found_scheme != scheme:
        problems.append(problem("session defaults scheme does not match requested scheme", rule="native-ios-scheme"))
    if found_simulator and simulator and simulator not in found_simulator and found_simulator not in simulator:
        problems.append(problem("session defaults simulator does not match requested simulator", rule="native-ios-simulator"))
    if not found_runtime and not found_udid:
        problems.append(problem("session defaults must include simulator runtime or UDID", rule="native-ios-runtime"))
    return {
        "scheme": found_scheme,
        "simulator": found_simulator,
        "runtime": found_runtime,
        "udid": found_udid,
    }


def proof_command_argv(*, task: str, scheme: str, simulator: str, project: Path, artifacts: Mapping[str, Path]) -> list[str]:
    argv = [
        "python3",
        "scripts/star_forge.py",
        "native-ios-proof",
        "--project",
        common.project_cli_arg(project),
        "--task",
        task,
        "--scheme",
        scheme,
        "--simulator",
        simulator,
        "--build-result",
        rel(project, artifacts["build"]),
        "--launch-result",
        rel(project, artifacts["launch"]),
        "--test-result",
        rel(project, artifacts["test"]),
    ]
    if "screenshot" in artifacts:
        argv.extend(["--screenshot", rel(project, artifacts["screenshot"])])
    if "ui_snapshot" in artifacts:
        argv.extend(["--ui-snapshot", rel(project, artifacts["ui_snapshot"])])
    argv.append("--strict")
    return argv


def record_proof(project: Path, command: Sequence[str]) -> MappingLike:
    actual = [
        sys.executable if item == "python3" else str(STAR_FORGE) if item == "scripts/star_forge.py" else item
        for item in command
    ]
    proc = subprocess.run(actual, cwd=str(project), shell=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def collect(args: argparse.Namespace, command_argv: Sequence[str]) -> tuple[int, MappingLike]:
    project = Path(args.project).resolve()
    out_dir = common.live_collector_dir(project, args.task, COLLECTOR)
    source_before = common.compute_source_hash(project)
    runtime_hash = common.compute_runtime_asset_hash(project)
    problems: list[MappingLike] = []
    unavailable: list[str] = []

    scheme = str(args.scheme or "").strip()
    simulator = str(args.simulator or "").strip()
    app_identity = str(args.app_identity or args.bundle_id or "").strip()
    if not scheme:
        problems.append(problem("native iOS collection requires --scheme", rule="native-ios-scheme"))
    if not simulator:
        problems.append(problem("native iOS collection requires --simulator", rule="native-ios-simulator"))
    if not app_identity:
        problems.append(problem("native iOS collection requires --app-identity or --bundle-id", rule="native-ios-app-identity"))

    transcript_path, transcript_payload = copy_json_artifact(
        project,
        out_dir,
        args.mcp_transcript,
        "mcp-transcript.json",
        "MCP transcript",
        problems,
        required=True,
        missing_rule="native-ios-mcp-transcript",
        require_object=True,
        fallback={"schema": TRANSCRIPT_SCHEMA, "calls": [], "status": "missing"},
    )
    if not str(args.mcp_transcript or "").strip():
        unavailable.append("mcp-transcript")

    derived_session = extract_session_defaults(transcript_payload)
    session_fallback = derived_session or {"schema": SESSION_SCHEMA, "status": "missing"}
    session_path, session_payload = copy_json_artifact(
        project,
        out_dir,
        args.session_defaults,
        "session-defaults.json",
        "session defaults",
        problems,
        required=derived_session is None,
        missing_rule="native-ios-session-defaults",
        require_object=True,
        fallback=session_fallback,
    )

    build_path, build_payload = copy_json_artifact(
        project,
        out_dir,
        args.build_result,
        "build.json",
        "build result",
        problems,
        required=True,
        missing_rule="native-ios-build",
        require_object=True,
    )
    launch_path, launch_payload = copy_json_artifact(
        project,
        out_dir,
        args.launch_result,
        "launch.json",
        "launch result",
        problems,
        required=True,
        missing_rule="native-ios-launch",
        require_object=True,
    )
    test_path, test_payload = copy_json_artifact(
        project,
        out_dir,
        args.test_result,
        "test.json",
        "test result",
        problems,
        required=True,
        missing_rule="native-ios-test",
        require_object=True,
    )
    artifacts: dict[str, Path] = {
        "session_defaults": session_path,
        "mcp_transcript": transcript_path,
        "build": build_path,
        "launch": launch_path,
        "test": test_path,
    }

    validate_result("build", build_payload, problems, build_path, project)
    validate_result("launch", launch_payload, problems, launch_path, project)
    validate_result("test", test_payload, problems, test_path, project)

    screenshot_path = copy_image_artifact(project, out_dir, args.screenshot, problems)
    if screenshot_path is not None:
        artifacts["screenshot"] = screenshot_path

    ui_snapshot_payload: Any | None = None
    if str(args.ui_snapshot or "").strip():
        ui_snapshot_path, ui_snapshot_payload = copy_json_artifact(
            project,
            out_dir,
            args.ui_snapshot,
            "ui-snapshot.json",
            "UI snapshot",
            problems,
            required=True,
            missing_rule="native-ios-ui-snapshot",
            require_object=True,
        )
        artifacts["ui_snapshot"] = ui_snapshot_path
        validate_ui_snapshot(ui_snapshot_payload, ui_snapshot_path, project, problems)

    log_path = copy_log_artifact(project, out_dir, args.log, int(args.max_log_bytes), problems)
    if log_path is not None:
        artifacts["log"] = log_path

    has_ui_proof = screenshot_path is not None or ui_snapshot_payload is not None
    if not has_ui_proof:
        problems.append(problem("native iOS UI proof requires screenshot or UI snapshot evidence; log evidence alone is not enough", rule="native-ios-ui"))

    session_info = validate_session_defaults(
        session_payload,
        scheme=scheme,
        simulator=simulator,
        runtime=str(args.simulator_runtime or "").strip(),
        udid=str(args.simulator_udid or "").strip(),
        problems=problems,
    )
    transcript_summary, transcript_problems, transcript_unavailable = validate_transcript(
        transcript_payload,
        session_payload,
        scheme=scheme,
        simulator=simulator,
        current_source_hash=source_before,
        has_screenshot=screenshot_path is not None,
        has_ui_snapshot=ui_snapshot_payload is not None,
        args=args,
    )
    problems.extend(transcript_problems)
    unavailable.extend(transcript_unavailable)

    for label, expected_kind, payload, path in (
        ("build result", "build", build_payload, build_path),
        ("launch result", "launch", launch_payload, launch_path),
        ("test result", "test", test_payload, test_path),
    ):
        validate_result_artifact_contract(
            label,
            expected_kind,
            payload,
            problems,
            path=rel(project, path),
            expected_runtime=str(session_info.get("runtime") or ""),
            expected_udid=str(session_info.get("udid") or ""),
        )

    bundle_record = app_bundle_hash(project, str(args.app_bundle or ""), problems)
    bundle_path = write_json(out_dir / "app-bundle.json", {"schema": APP_BUNDLE_SCHEMA, **bundle_record})
    artifacts["app_bundle"] = bundle_path

    source_after = common.compute_source_hash(project)
    degraded = bool(problems or unavailable)
    summary: MappingLike = {
        "mcp_provenance": transcript_summary["provenance"],
        "mcp_tool_call_count": transcript_summary["tool_call_count"],
        "mcp_categories": transcript_summary["categories"],
        "scheme": scheme,
        "simulator": {
            "name": simulator,
            "runtime": session_info.get("runtime") or "",
            "udid": session_info.get("udid") or "",
        },
        "simulator_runtime": session_info.get("runtime") or "",
        "simulator_udid": session_info.get("udid") or "",
        "app_identity": app_identity,
        "app_bundle_hash": bundle_record.get("sha256", ""),
        "source_hash": source_before,
        "runtime_asset_hash": runtime_hash,
        "artifact_semantics": {
            "build_success": result_failure(build_payload) is None,
            "launch_success": result_failure(launch_payload) is None,
            "test_success": result_failure(test_payload) is None,
            "ui_proof": "screenshot" if screenshot_path is not None else "ui_snapshot" if ui_snapshot_payload is not None else "",
            "log_is_diagnostic_only": log_path is not None,
            "strict_proof_handoff_ready": not degraded,
        },
    }
    manifest_path = common.write_live_manifest(
        project,
        task=args.task,
        collector=COLLECTOR,
        command_argv=list(command_argv),
        tool_versions={
            "python": sys.version.split()[0],
            "xcodebuildmcp": str(args.mcp_version or "agent-exported"),
        },
        artifacts=artifacts,
        summary=summary,
        degraded=degraded,
        unavailable_capabilities=sorted(set(unavailable)),
        problems=problems,
        source_hash_before=source_before,
        source_hash_after=source_after,
        runtime_asset_hash=runtime_hash,
    )
    proof_argv = proof_command_argv(task=args.task, scheme=scheme, simulator=simulator, project=project, artifacts=artifacts)
    output: MappingLike = {
        "schema": "star-forge.native-ios-collector.v1",
        "collector": COLLECTOR,
        "task": args.task,
        "artifact_dir": rel(project, out_dir),
        "manifest": rel(project, manifest_path),
        "artifacts": {name: rel(project, path) for name, path in artifacts.items()},
        "degraded": degraded,
        "unavailable_capabilities": sorted(set(unavailable)),
        "problems": problems,
        "handoff_ready": not degraded,
        "proof_command_argv": proof_argv,
        "proof_command": shlex.join(proof_argv),
        "native_ios_proof_command": shlex.join(proof_argv),
        "recorded": False,
    }
    if args.record:
        output["recorded"] = True
        output["record"] = record_proof(project, proof_argv)
    return (1 if degraded or (output.get("record", {}).get("returncode", 0) not in (0, None)) else 0), output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and normalize agent-exported XcodeBuildMCP iOS evidence")
    parser.add_argument("--project", default=".")
    parser.add_argument("--task", required=True)
    parser.add_argument("--scheme", default="")
    parser.add_argument("--simulator", default="")
    parser.add_argument("--simulator-runtime", default="")
    parser.add_argument("--simulator-udid", default="")
    parser.add_argument("--app-identity", default="")
    parser.add_argument("--bundle-id", default="")
    parser.add_argument("--mcp-transcript", default="")
    parser.add_argument("--session-defaults", default="")
    parser.add_argument("--build-result", default="")
    parser.add_argument("--launch-result", default="")
    parser.add_argument("--test-result", default="")
    parser.add_argument("--screenshot", default="")
    parser.add_argument("--ui-snapshot", default="")
    parser.add_argument("--log", default="")
    parser.add_argument("--app-bundle", default="")
    parser.add_argument("--mcp-server", default="XcodeBuildMCP")
    parser.add_argument("--mcp-version", default="")
    parser.add_argument("--agent-id", default="")
    parser.add_argument("--mcp-unavailable", action="store_true")
    parser.add_argument("--max-log-bytes", type=int, default=65536)
    parser.add_argument("--record", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    args = build_parser().parse_args(raw_argv)
    code, output = collect(args, ["python3", "scripts/live_collectors/native_ios.py", *raw_argv])
    redacted, _report = common.redact_sensitive_values(output)
    if isinstance(redacted, dict):
        for field in ("proof_command_argv", "proof_command", "native_ios_proof_command"):
            redacted[field] = output[field]
    print(json.dumps(redacted, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
