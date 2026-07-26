#!/usr/bin/env python3
"""Normalize agent-exported XcodeBuildMCP iOS evidence for Star Forge.
This adapter does not call Xcode, simctl, xcrun, or any local shell fallback.
It accepts an MCP transcript and local artifacts produced by an agent-mediated
XcodeBuildMCP workflow, writes task-scoped evidence, and prints the strict
native iOS proof command.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from functools import partial
from pathlib import Path
from typing import Any, Mapping, Sequence
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
from live_collectors import common, native_argv, native_transcript as native
from live_collectors.native_transcript import *  # noqa: F401,F403
from live_collectors.policy_data import policy_dict, policy_list, policy_set, policy_tuple
from live_collectors.provider_engine import candidate_text, first_candidate, render_descriptor
from starforge import safe_io
RESULT_SCHEMA = "star-forge.native-ios.result.v1"
SESSION_SCHEMA = "star-forge.native-ios.session-defaults.v1"
TRANSCRIPT_SCHEMA = "star-forge.native-ios.mcp-transcript.v1"
APP_BUNDLE_SCHEMA = "star-forge.native-ios.app-bundle.v1"
PRIMARY_PROVIDER = "xcodebuildmcp"
SIMULATOR_BROWSER_PROVIDER = "ios-simulator-browser"
SESSION_TOOLS = policy_set("native_ios", "SESSION_TOOLS")
BUILD_TOOLS = policy_set("native_ios", "BUILD_TOOLS")
LAUNCH_TOOLS = policy_set("native_ios", "LAUNCH_TOOLS")
TEST_TOOLS = policy_set("native_ios", "TEST_TOOLS")
SCREENSHOT_TOOLS = policy_set("native_ios", "SCREENSHOT_TOOLS")
UI_SNAPSHOT_TOOLS = policy_set("native_ios", "UI_SNAPSHOT_TOOLS")
LOG_TOOLS = policy_set("native_ios", "LOG_TOOLS")
NATIVE_ACTION_TOOLS = BUILD_TOOLS | LAUNCH_TOOLS | TEST_TOOLS | SCREENSHOT_TOOLS | UI_SNAPSHOT_TOOLS | LOG_TOOLS
DISCOVERY_TOOLS = policy_set("native_ios", "DISCOVERY_TOOLS")
PREBOOT_OPEN_TOOLS = policy_set("native_ios", "PREBOOT_OPEN_TOOLS")
CALL_FIELD_KEYS = policy_dict("native_ios", "CALL_FIELD_KEYS")
CALL_LIST_KEYS = policy_tuple("native_ios", "CALL_LIST_KEYS")
CALL_TEMPLATE = policy_dict("native_ios", "CALL_TEMPLATE")
CATEGORY_TOOLS = policy_dict("native_ios", "CATEGORY_TOOLS")
ENVELOPE_PROVENANCE_TEMPLATE = policy_dict("native_ios", "ENVELOPE_PROVENANCE_TEMPLATE")
NATIVE_FINALIZE = policy_dict("native_ios", "NATIVE_FINALIZE")
COLLECT_CHECKS = policy_list("native_ios", "COLLECT_CHECKS")
CAPABILITY, ROUTE = NATIVE_FINALIZE["capability"], "ios-verification"
OUTPUT_TEMPLATE = policy_dict("native_ios", "OUTPUT_TEMPLATE")
PARSER_ARGUMENTS = policy_list("native_ios", "PARSER_ARGUMENTS")
PROVENANCE_TEMPLATE = policy_dict("native_ios", "PROVENANCE_TEMPLATE")
RESULT_FALLBACK_TEMPLATE = policy_dict("native_ios", "RESULT_FALLBACK_TEMPLATE")
MAX_JSON_INPUT_BYTES = 16 * 1024 * 1024
MAX_IMAGE_INPUT_BYTES = 64 * 1024 * 1024
RESULT_RUNTIME_PATHS = policy_tuple("native_ios", "RESULT_RUNTIME_PATHS")
RESULT_UDID_PATHS = policy_tuple("native_ios", "RESULT_UDID_PATHS")
SESSION_INFO_TEMPLATE = policy_dict("native_ios", "SESSION_INFO_TEMPLATE")
SESSION_FIELD_KEYS = policy_dict("native_ios", "SESSION_FIELD_KEYS")
SESSION_INNER_KEYS = policy_tuple("native_ios", "SESSION_INNER_KEYS")
SUMMARY_TEMPLATE = policy_dict("native_ios", "SUMMARY_TEMPLATE")
TOOL_NAME_MARKERS = policy_tuple("native_ios", "TOOL_NAME_MARKERS")
TRANSCRIPT_SUMMARY_TEMPLATE = policy_dict("native_ios", "TRANSCRIPT_SUMMARY_TEMPLATE")
TRANSCRIPT_CATEGORY_REQUIREMENTS = policy_list("native_ios", "TRANSCRIPT_CATEGORY_REQUIREMENTS")
UI_SNAPSHOT_MARKERS = policy_tuple("native_ios", "UI_SNAPSHOT_MARKERS")
MappingLike = dict[str, Any]
descriptor = render_descriptor
rel = common.project_relative
def problem(message: str, *, rule: str, path: str = "", severity: str = "high", blocking: bool = True) -> MappingLike:
    payload = common.blocking_problem(message, rule=rule, path=path, severity=severity)
    payload["blocking"] = blocking
    return payload
write_json = native.write_json
write_text = native.write_text
def input_display_path(project: Path, path: Path | str) -> str:
    candidate = Path(str(path))
    try:
        return rel(project, candidate.resolve())
    except OSError:
        return common.sanitize_external_path(candidate)
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
    candidate = Path(raw)
    try:
        project_root = project.resolve()
        resolved = (candidate if candidate.is_absolute() else project_root / candidate).absolute()
        resolved.relative_to(project_root)
    except ValueError:
        problems.append(problem(f"{label} path must stay inside the project", rule=rule, path=common.sanitize_external_path(Path(raw))))
        return None
    try:
        safe_io.read_bytes(project, resolved, limit=0)
    except FileNotFoundError:
        problems.append(problem(f"{label} does not exist", rule=rule, path=input_display_path(project, resolved)))
        return None
    except OSError as exc:
        problems.append(problem(f"{label} must be a safe regular file: {exc}", rule=rule, path=input_display_path(project, resolved)))
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
    missing_rule: str,
    required: bool = True,
    require_object: bool = True,
    fallback: Any | None = None,
) -> tuple[Path, Any]:
    dest = out_dir / dest_name
    raw = str(raw_path or "").strip()
    src: Path | None = None
    if not raw:
        if required:
            problems.append(problem(f"{label} is required", rule=missing_rule))
    else:
        src = resolve_input_path(project, raw, label, problems, rule=missing_rule)
    if src is None:
        payload = fallback if fallback is not None else descriptor(
            RESULT_FALLBACK_TEMPLATE, kind=label, status="missing"
        )
    else:
        try:
            content, _digest, _size = safe_io.read_snapshot(
                project, src, max_bytes=MAX_JSON_INPUT_BYTES)
            payload = json.loads(content)
        except Exception as exc:
            problems.append(problem(f"{label} is malformed JSON: {exc}", rule="native-ios-json", path=input_display_path(project, src)))
            payload = {
                **descriptor(RESULT_FALLBACK_TEMPLATE, kind=label, status="malformed_json"),
                "error": str(exc),
            }
        if require_object and not isinstance(payload, dict):
            problems.append(problem(f"{label} must be a JSON object", rule="native-ios-artifact-shape", path=input_display_path(project, src)))
            payload = descriptor(RESULT_FALLBACK_TEMPLATE, kind=label, status="wrong_shape")
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
    try:
        content, _digest, _size = safe_io.read_snapshot(
            project, src, max_bytes=MAX_IMAGE_INPUT_BYTES)
        safe_io.atomic_write_bytes(project, dest, content)
    except OSError as exc:
        problems.append(problem(f"screenshot cannot be copied safely: {exc}", rule="native-ios-screenshot", path=input_display_path(project, src)))
        return None
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
    bounded = max(0, max_bytes)
    try:
        data = safe_io.read_bytes(project, src, limit=bounded + 1)
    except OSError as exc:
        problems.append(problem(f"log cannot be read safely: {exc}", rule="native-ios-log", path=input_display_path(project, src)))
        return None
    truncated = len(data) > bounded
    text = data[:bounded].decode("utf-8", "replace")
    if truncated:
        text += "\n[TRUNCATED_BY_STAR_FORGE_NATIVE_IOS_ADAPTER]\n"
    return write_text(out_dir / "log.txt", text)
def normalize_tool_name(raw: Any) -> str:
    cleaned = str(raw or "").strip().replace("-", "_").replace(".", "_")
    lowered = re.sub(r"[^A-Za-z0-9_]+", "_", cleaned).strip("_").lower()
    for marker in TOOL_NAME_MARKERS:
        if marker in lowered:
            lowered = lowered.split(marker, 1)[1]
    return lowered
def raw_call_items(payload: Any) -> list[Any]:
    value = first_candidate(payload, CALL_LIST_KEYS) if isinstance(payload, dict) else None
    return payload if isinstance(payload, list) else value if isinstance(value, list) else []
def normalize_call(item: Any, index: int) -> MappingLike:
    if isinstance(item, str):
        return {"index": index, "tool": normalize_tool_name(item), "raw_tool": item, "args": {}, "result": {}}
    if not isinstance(item, dict):
        return {"index": index, "tool": "", "raw_tool": "", "args": {}, "result": {}}
    raw_tool = first_candidate(item, CALL_FIELD_KEYS["tool"]) or ""
    args = first_candidate(item, CALL_FIELD_KEYS["args"]) or {}
    result = first_candidate(item, CALL_FIELD_KEYS["result"]) or {}
    if not isinstance(args, dict):
        args = {"value": args}
    if not isinstance(result, dict):
        result = {"value": result}
    return descriptor(
        CALL_TEMPLATE, index=index, tool=normalize_tool_name(raw_tool), raw_tool=raw_tool,
        args=args, result=result,
        parallel=bool(item.get("parallel") or item.get("parallel_tool_call")),
        parallel_group=item.get("parallel_group") or item.get("batch_id"),
        type=item.get("type") or item.get("kind") or "",
    )
def transcript_calls(payload: Any) -> list[MappingLike]:
    return [normalize_call(item, index) for index, item in enumerate(raw_call_items(payload))]
def extract_transcript_provenance(payload: Any, args: argparse.Namespace) -> MappingLike:
    if not isinstance(payload, dict):
        return descriptor(
            PROVENANCE_TEMPLATE, tool_surface="mcp", server=args.mcp_server,
            server_version=args.mcp_version, exported_by=str(args.agent_id or ""),
            explicit=False,
        )
    return descriptor(
        PROVENANCE_TEMPLATE,
        tool_surface=candidate_text(payload, ("mcp.tool_surface", "provenance.tool_surface", "tool_surface"))
        or ("mcp" if args.mcp_server else ""),
        server=candidate_text(payload, ("mcp.server", "provenance.server", "server")) or args.mcp_server,
        server_version=candidate_text(payload, ("mcp.version", "provenance.version", "version")) or args.mcp_version,
        exported_by=candidate_text(payload, ("exported_by", "provenance.exported_by")) or str(args.agent_id or ""),
        explicit=bool(any(isinstance(payload.get(key), dict) and payload.get(key)
                          for key in ("mcp", "provenance"))
                      or payload.get("tool_surface") or payload.get("server") or payload.get("exported_by")),
    )
def merge_manifest_provenance(provenance: MappingLike, args: argparse.Namespace) -> MappingLike:
    manifest_provenance = getattr(args, "manifest_mcp_provenance", None)
    if provenance.get("explicit") or not isinstance(manifest_provenance, dict):
        return provenance
    merged = descriptor(
        PROVENANCE_TEMPLATE,
        tool_surface=candidate_text(manifest_provenance, ("tool_surface",)),
        server=candidate_text(manifest_provenance, ("server",)),
        server_version=candidate_text(manifest_provenance, ("version", "server_version")),
        exported_by=candidate_text(manifest_provenance, ("exported_by", "agent_id")),
        explicit=True,
    )
    return {**merged, "source": "manifest-summary"}
def transcript_source_hash(payload: Any) -> str:
    return candidate_text(
        payload, ("source_hash", "source_hash_before", "source_hash_after",
                  "provenance.source_hash")
    )
def extract_capabilities(payload: Any) -> set[str] | None:
    if not isinstance(payload, dict):
        return None
    raw = first_candidate(
        payload, ("capabilities", "available_tools", "mcp.capabilities", "mcp.available_tools")
    )
    if raw is None:
        return None
    return (
        {normalize_tool_name(item) for item in raw if normalize_tool_name(item)}
        if isinstance(raw, list) else set()
    )
def mcp_marked_unavailable(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if any(first_candidate(payload, (path,)) is False for path in
           ("mcp.available", "available", "xcodebuildmcp_available")):
        return True
    unavailable = payload.get("unavailable_capabilities")
    return isinstance(unavailable, list) and any(
        "xcodebuild" in str(item).lower() or "mcp" in str(item).lower()
        for item in unavailable
    )
def extract_session_defaults(transcript: Any) -> MappingLike | None:
    return next((
        dict(value) for call in transcript_calls(transcript)
        if call.get("tool") in SESSION_TOOLS for field in ("result", "args")
        if isinstance((value := call.get(field)), dict) and value
    ), None)
def nested_get(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
    return next((
        candidate.strip() for key in keys
        for candidate in (
            [payload[key].get(name) for name in SESSION_INNER_KEYS]
            if isinstance(payload.get(key), dict) else [payload.get(key)]
        )
        if isinstance(candidate, str) and candidate.strip()
    ), "")
def session_field(payload: Any, field: str) -> str:
    return nested_get(payload, SESSION_FIELD_KEYS[field]) if isinstance(payload, dict) else ""
for _field in ("scheme", "simulator", "runtime", "udid"):
    globals()[f"session_{_field}"] = partial(session_field, field=_field)
def session_needs_discovery(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return True
    return bool(
        payload.get("needs_discovery") is True or payload.get("missing_defaults") is True
        or isinstance(payload.get("missing"), list) and payload.get("missing")
        or not (session_scheme(payload) and session_simulator(payload))
    )
def bool_success(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    key = next((name for name in ("success", "ok", "passed") if name in payload), "")
    if key:
        return bool(payload.get(key))
    return candidate_text(payload, ("status", "conclusion")).lower() in {
        "success", "succeeded", "passed", "pass", "ok"
    }
def result_failure(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "result must be a JSON object"
    true_key = next((key for key in ("timed_out", "timeout", "crashed") if payload.get(key) is True), "")
    if true_key:
        return f"{true_key} is true"
    false_key = next((key for key in ("success", "ok", "passed")
                      if key in payload and payload.get(key) is False), "")
    if false_key:
        return f"{false_key} is false"
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
result_runtime = partial(candidate_text, paths=RESULT_RUNTIME_PATHS)
result_udid = partial(candidate_text, paths=RESULT_UDID_PATHS)
def result_provenance(payload: Any) -> MappingLike | None:
    provenance = first_candidate(payload, ("mcp_provenance", "provenance"))
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
    command_entries = command_field_entries(payload)
    runtime = result_runtime(payload)
    udid = result_udid(payload)
    provenance = result_provenance(payload)
    checks = [
        (payload.get("schema") != RESULT_SCHEMA, f"{label} must use schema {RESULT_SCHEMA}", "native-ios-result"),
        (str(payload.get("kind") or "") != expected_kind, f"{label} kind must be {expected_kind}", "native-ios-result"),
        (payload.get("success") is not True, f"{label} must include success true", "native-ios-result"),
        (payload.get("shell") is True, f"{label} must not record shell execution", "native-ios-result"),
        (any(command_field_uses_shell_fallback(name, value) for name, value in command_entries),
         f"{label} must not include shell fallback command evidence", "native-ios-shell-fallback"),
        (not runtime and not udid and provenance is None,
         f"{label} must include simulator runtime, simulator UDID, or MCP provenance", "native-ios-result"),
        (bool(runtime and expected_runtime and runtime != expected_runtime),
         f"{label} runtime does not match manifest summary", "native-ios-result"),
        (bool(udid and expected_udid and udid != expected_udid),
         f"{label} simulator UDID does not match manifest summary", "native-ios-result"),
    ]
    if provenance is not None:
        tool_surface = str(provenance.get("tool_surface") or "").strip().lower()
        server = str(provenance.get("server") or "").strip()
        checks += [
            (tool_surface != "mcp", f"{label} provenance must use MCP tool surface", "native-ios-result"),
            (server != "XcodeBuildMCP", f"{label} provenance server must be XcodeBuildMCP", "native-ios-result"),
        ]
    problems.extend(problem(message, rule=rule, path=path) for failed, message, rule in checks if failed)
def validate_result(label: str, payload: Any, problems: list[MappingLike], path: Path, project: Path) -> None:
    failure = result_failure(payload)
    if failure:
        problems.append(problem(f"{label} failed: {failure}", rule=f"native-ios-{label}", path=rel(project, path)))
def validate_ui_snapshot(payload: Any, path: Path, project: Path, problems: list[MappingLike]) -> bool:
    if not isinstance(payload, dict):
        problems.append(problem("UI snapshot must be a JSON object", rule="native-ios-ui-snapshot", path=rel(project, path)))
        return False
    if not any(key in payload for key in UI_SNAPSHOT_MARKERS):
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
    if bundle.is_file():
        return {"available": True, "path": rel(project, bundle), "sha256": common.file_sha256(bundle), "kind": "file"}
    if not bundle.is_dir():
        problems.append(problem("app bundle must be a file or directory", rule="native-ios-app-bundle", path=rel(project, bundle)))
        return {"available": False, "path": rel(project, bundle)}
    return {
        "available": True,
        "path": rel(project, bundle),
        "sha256": common.tree_sha256(bundle),
        "kind": "directory",
        "files": sum(1 for path in bundle.glob("**/*") if path.is_file() and not path.is_symlink()),
    }
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
    provenance_checks = (
        (not provenance.get("explicit"), "XcodeBuildMCP transcript requires explicit MCP provenance"),
        (str(provenance.get("tool_surface") or "").lower() != "mcp", "XcodeBuildMCP evidence must come from an MCP tool surface"),
        (str(provenance.get("server") or "") != "XcodeBuildMCP", "XcodeBuildMCP transcript must name server XcodeBuildMCP"),
        (not str(provenance.get("exported_by") or "").strip(), "XcodeBuildMCP transcript requires exported_by or agent id"),
    )
    problems.extend(problem(message, rule="native-ios-mcp-provenance") for failed, message in provenance_checks if failed)
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
        required = SESSION_TOOLS | {
            str(call.get("tool") or "") for call in calls
            if call.get("tool") in NATIVE_ACTION_TOOLS
        }
        missing = sorted(name for name in required if name not in capabilities)
        if missing:
            unavailable.extend(f"xcodebuildmcp:{name}" for name in missing)
            problems.append(problem("XcodeBuildMCP transcript is missing required capabilities: " + ", ".join(missing), rule="native-ios-mcp-capability"))
    first_session = min(
        (int(call["index"]) for call in calls if call.get("tool") in SESSION_TOOLS),
        default=None,
    )
    if first_session is None:
        problems.append(problem("MCP transcript must include session_show_defaults", rule="native-ios-session-defaults"))
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
        if tool in NATIVE_ACTION_TOOLS and (first_session is None or idx < first_session):
            problems.append(problem("session_show_defaults must appear before native build, run, test, screenshot, UI snapshot, or log actions", rule="native-ios-tool-order"))
        call_args = call.get("args") if isinstance(call.get("args"), dict) else {}
        call_scheme = candidate_text(call_args, ("scheme", "scheme_name"))
        if call_scheme and call_scheme != scheme:
            problems.append(problem("transcript tool call used a different scheme than the requested scheme", rule="native-ios-scheme"))
        call_sim = candidate_text(call_args, ("simulator", "device", "destination"))
        if call_sim and simulator and simulator not in call_sim and call_sim not in simulator:
            problems.append(problem("transcript tool call used a different simulator than the requested simulator", rule="native-ios-simulator"))
    categories = {
        category: any(call.get("tool") in tools for call in calls)
        for category, tools in CATEGORY_TOOLS.items()
    }
    required_categories = {
        "always": True, "has_screenshot": has_screenshot,
        "has_ui_snapshot": has_ui_snapshot,
    }
    for category, condition, message, rule in TRANSCRIPT_CATEGORY_REQUIREMENTS:
        if required_categories[condition] and not categories[category]:
            problems.append(problem(message, rule=rule))
    summary = descriptor(
        TRANSCRIPT_SUMMARY_TEMPLATE, provenance=provenance, tool_call_count=len(calls),
        categories=categories, capabilities_declared=capabilities is not None,
    )
    return summary, problems, sorted(set(unavailable))
def validate_session_defaults(session: Any, *, scheme: str, simulator: str, runtime: str, udid: str, problems: list[MappingLike]) -> MappingLike:
    if not isinstance(session, dict):
        problems.append(problem("session-defaults.json must be a JSON object", rule="native-ios-session-defaults"))
        return descriptor(SESSION_INFO_TEMPLATE, scheme="", simulator="", runtime=runtime, udid=udid)
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
    return descriptor(
        SESSION_INFO_TEMPLATE, scheme=found_scheme, simulator=found_simulator,
        runtime=found_runtime, udid=found_udid,
    )
def collect(args: argparse.Namespace, command_argv: Sequence[str]) -> tuple[int, MappingLike]:
    project = common.assert_collector_project_safe(Path(args.project))
    out_dir = common.live_collector_dir(project, args.task, NATIVE_FINALIZE["collector"])
    source_before = common.compute_source_hash(project)
    runtime_hash = common.compute_runtime_asset_hash(project)
    problems: list[MappingLike] = []
    unavailable: list[str] = []
    scheme = str(args.scheme or "").strip()
    simulator = str(args.simulator or "").strip()
    app_identity = str(args.app_identity or args.bundle_id or "").strip()
    collect_checks = descriptor(
        COLLECT_CHECKS, missing_scheme=not scheme, missing_simulator=not simulator,
        missing_identity=not app_identity, missing_screenshot=False, missing_ui_snapshot=False,
        browser_conflict=False, browser_artifacts_missing=False,
    )
    problems.extend(problem(message, rule=rule) for failed, message, rule in collect_checks if failed)
    transcript_path, transcript_payload = copy_json_artifact(
        project,
        out_dir,
        args.mcp_transcript,
        "mcp-transcript.json",
        "MCP transcript",
        problems,
        missing_rule="native-ios-mcp-transcript",
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
        fallback=session_fallback,
    )
    artifacts: dict[str, Path] = {
        "session_defaults": session_path, "mcp_transcript": transcript_path,
    }
    result_artifacts: dict[str, tuple[Path, Any]] = {}
    for kind in ("build", "launch", "test"):
        path, payload = copy_json_artifact(
            project,
            out_dir,
            getattr(args, f"{kind}_result"),
            f"{kind}.json",
            f"{kind} result",
            problems,
            missing_rule=f"native-ios-{kind}",
        )
        result_artifacts[kind] = path, payload
        artifacts[kind] = path
        validate_result(kind, payload, problems, path, project)
    build_path, build_payload = result_artifacts["build"]
    launch_path, launch_payload = result_artifacts["launch"]
    test_path, test_payload = result_artifacts["test"]
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
            missing_rule="native-ios-ui-snapshot",
        )
        artifacts["ui_snapshot"] = ui_snapshot_path
        validate_ui_snapshot(ui_snapshot_payload, ui_snapshot_path, project, problems)
    log_path = copy_log_artifact(project, out_dir, args.log, int(args.max_log_bytes), problems)
    if log_path is not None:
        artifacts["log"] = log_path
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
    collect_checks = descriptor(
        COLLECT_CHECKS, missing_scheme=False, missing_simulator=False, missing_identity=False,
        missing_screenshot=screenshot_path is None, missing_ui_snapshot=ui_snapshot_payload is None,
        browser_conflict=args.simulator_browser_used and args.simulator_browser_unavailable,
        browser_artifacts_missing=args.simulator_browser_used
        and (screenshot_path is None or ui_snapshot_payload is None),
    )
    problems.extend(problem(message, rule=rule) for failed, message, rule in collect_checks if failed)
    if args.simulator_browser_unavailable:
        unavailable.append(SIMULATOR_BROWSER_PROVIDER)
    for expected_kind, (path, payload) in result_artifacts.items():
        validate_result_artifact_contract(
            f"{expected_kind} result",
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
    simulator_browser_status = (
        "used" if args.simulator_browser_used else
        "unavailable" if args.simulator_browser_unavailable else "availability-not-reported"
    )
    summary = descriptor(
        SUMMARY_TEMPLATE, provenance=transcript_summary["provenance"],
        tool_call_count=transcript_summary["tool_call_count"],
        categories=transcript_summary["categories"], scheme=scheme, simulator=simulator,
        runtime=session_info.get("runtime") or "", udid=session_info.get("udid") or "",
        app_identity=app_identity, app_bundle_hash=bundle_record.get("sha256", ""),
        source_hash=source_before, runtime_asset_hash=runtime_hash,
        build_success=result_failure(build_payload) is None,
        launch_success=result_failure(launch_payload) is None,
        test_success=result_failure(test_payload) is None,
        ui_proof=[name for name, present in (
            ("ui_snapshot", ui_snapshot_payload is not None),
            ("screenshot", screenshot_path is not None),
        ) if present],
        has_log=log_path is not None, handoff_ready=not bool(problems or unavailable),
        simulator_browser=simulator_browser_status,
    )
    provenance = descriptor(
        ENVELOPE_PROVENANCE_TEMPLATE,
        tool_details=dict(transcript_summary.get("provenance") or {}),
        tool_call_count=int(transcript_summary.get("tool_call_count") or 0),
        tool_categories=dict(transcript_summary.get("categories") or {}),
        simulator_browser_status=simulator_browser_status,
    )
    return native.finalize_collection(
        project, out_dir, task=args.task, command_argv=command_argv, artifacts=artifacts,
        summary=summary, problems=problems, unavailable=unavailable,
        source_hash_before=source_before, runtime_asset_hash=runtime_hash,
        tool_versions={"python": sys.version.split()[0],
                       "xcodebuildmcp": str(args.mcp_version or "agent-exported")},
        provider=PRIMARY_PROVIDER, provenance=provenance, config=NATIVE_FINALIZE,
        output_template=OUTPUT_TEMPLATE, values={"scheme": scheme, "simulator": simulator},
        record=args.record, script_path=SCRIPTS_ROOT / "star_forge.py",
    )
def build_parser() -> argparse.ArgumentParser:
    return native.build_parser(
        "Validate and normalize agent-exported XcodeBuildMCP iOS evidence", PARSER_ARGUMENTS
    )
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
