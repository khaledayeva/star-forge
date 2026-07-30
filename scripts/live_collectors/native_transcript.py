"""Reject shell and simulator fallbacks recorded in native tool transcripts."""

from __future__ import annotations

from live_collectors.policy_data import policy_set

import argparse
import re
import shlex
from pathlib import Path
from typing import Any, Mapping, Sequence

from live_collectors import common, native_argv
from live_collectors.provider_engine import render_descriptor
from starforge import evidence


SHELL_FALLBACK_TOOLS = policy_set("native_transcript", "SHELL_FALLBACK_TOOLS")
SHELL_FALLBACK_TOOL_SUFFIXES = tuple(f"_{name}" for name in sorted(SHELL_FALLBACK_TOOLS))
DIRECT_SHELL_FALLBACK_TOOLS = SHELL_FALLBACK_TOOLS | {"open"}
SHELL_FALLBACK_COMMAND_RE = re.compile(
    r"\b(xcodebuild|xcrun|simctl|osascript)\b|com\.apple\.iphonesimulator|Simulator\.app",
    re.IGNORECASE,
)
SIMULATOR_BUNDLE_ID = "com.apple.iphonesimulator"
COMMAND_FIELD_NAMES = policy_set("native_transcript", "COMMAND_FIELD_NAMES")
COMMAND_EXECUTION_FIELD_NAMES = policy_set("native_transcript", "COMMAND_EXECUTION_FIELD_NAMES")
SHELL_COMMAND_FIELD_NAMES = policy_set("native_transcript", "SHELL_COMMAND_FIELD_NAMES")
ARGV_EXECUTABLE_FIELD_NAMES = policy_set("native_transcript", "ARGV_EXECUTABLE_FIELD_NAMES")
SHELL_EXECUTABLE_NAMES = policy_set("native_transcript", "SHELL_EXECUTABLE_NAMES")
MappingLike = dict[str, Any]


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
    if isinstance(value, dict):
        return [
            entry for key, child in value.items()
            for entry in (
                ([(normalize_command_field_name(key), child)]
                 if command_field_is_candidate(normalize_command_field_name(key)) else [])
                + command_field_entries(child)
            )
        ]
    return [entry for child in value for entry in command_field_entries(child)] if isinstance(value, list) else []


def command_parts_and_text(command: Any) -> tuple[list[str], str]:
    if isinstance(command, list):
        parts = [str(item) for item in command]
        return parts, " ".join(parts)
    text = str(command)
    try:
        return shlex.split(text), text
    except ValueError:
        return text.split(), text


executable_name = native_argv.executable_name


def env_tail_uses_shell_fallback(tokens: Sequence[str], *, depth: int = 0) -> bool:
    target, error = native_argv.unwrap_env_command(tokens, depth=depth)
    return bool(error or not target or executable_name(target[0]) in SHELL_EXECUTABLE_NAMES)


def env_wrapper_uses_shell_fallback(command: Any) -> bool:
    parts, _text = command_parts_and_text(command)
    return bool(parts and executable_name(parts[0]) == "env"
                and env_tail_uses_shell_fallback(parts))


def argv_executable_is_shell(command: Any) -> bool:
    parts, _text = command_parts_and_text(command)
    return bool(parts and (
        executable_name(parts[0]) in SHELL_EXECUTABLE_NAMES or env_wrapper_uses_shell_fallback(parts)
    ))


def command_uses_open_simulator_fallback(command: Any) -> bool:
    parts, text = command_parts_and_text(command)
    lowered_parts = [part.strip().strip("'\"").lower() for part in parts]
    return bool(parts and (
        executable_name(parts[0]) == "open"
        or SIMULATOR_BUNDLE_ID in lowered_parts
        or any(part.endswith("simulator.app") for part in lowered_parts)
        or re.search(r"(?i)(^|\s|/|')Simulator\.app(\s|/|'|$)", text)
    ))


def command_value_uses_shell_fallback(command: Any) -> bool:
    if isinstance(command, dict):
        return any(command_value_uses_shell_fallback(child) for child in command.values())
    if not isinstance(command, (list, str)):
        return False
    text = " ".join(str(item) for item in command) if isinstance(command, list) else command
    return bool(
        env_wrapper_uses_shell_fallback(command)
        or command_uses_open_simulator_fallback(command)
        or SHELL_FALLBACK_COMMAND_RE.search(text)
        or isinstance(command, list)
        and any(command_value_uses_shell_fallback(child) for child in command)
    )


def command_field_uses_shell_fallback(field_name: str, command: Any) -> bool:
    return bool(
        field_name in SHELL_COMMAND_FIELD_NAMES
        or command_field_name_contains(field_name, "shell", "command")
        or field_name in COMMAND_EXECUTION_FIELD_NAMES
        or command_field_name_contains(field_name, "command", "line")
        or compact_command_field_name(field_name) == "cmdline"
        or field_name in ARGV_EXECUTABLE_FIELD_NAMES and argv_executable_is_shell(command)
        or command_value_uses_shell_fallback(command)
    )


def call_uses_shell_fallback(call: Mapping[str, Any]) -> bool:
    tool = str(call.get("tool") or "")
    return bool(tool in DIRECT_SHELL_FALLBACK_TOOLS
        or tool.endswith(SHELL_FALLBACK_TOOL_SUFFIXES) or any(
        command_field_uses_shell_fallback(*entry)
        for name in ("args", "result")
        if isinstance((payload := call.get(name)), (dict, list))
        for entry in command_field_entries(payload)
    ))


def build_parser(description: str, arguments: Sequence[Sequence[Any]]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    for flags, kwargs in arguments:
        if kind := kwargs.get("type"):
            kwargs["type"] = {"float": float, "int": int}.get(kind, kind)
        parser.add_argument(*flags, **kwargs)
    return parser


write_json = lambda path, payload: common.write_json(path, payload)[0]
write_text = lambda path, text: common.write_text(path, text)[0]


def _proof_argv(
    project: Path, task: str, artifacts: Mapping[str, Path],
    config: Mapping[str, Any], values: Mapping[str, Any],
) -> list[str]:
    argv = [
        "python3", "scripts/star_forge.py", config["proof_command"],
        "--project", common.project_cli_arg(project), "--task", task,
    ]
    fields = (
        [(flag, values.get(key), False) for flag, key in config["proof_value_flags"]]
        + [(flag, artifacts.get(key), True) for key, flag in config["proof_artifact_flags"]]
        + [(flag, values.get(key), True) for flag, key in config["proof_path_flags"]]
    )
    for flag, value, is_path in fields:
        if value:
            argv.extend([flag, common.project_relative(project, value) if is_path else str(value)])
    return [*argv, "--strict"]


def finalize_collection(
    project: Path,
    out_dir: Path,
    *,
    task: str,
    command_argv: Sequence[str],
    artifacts: Mapping[str, Path],
    summary: Mapping[str, Any],
    problems: list[MappingLike],
    unavailable: Sequence[str],
    source_hash_before: str,
    runtime_asset_hash: str,
    tool_versions: Mapping[str, Any],
    provider: str,
    provenance: Mapping[str, Any],
    config: Mapping[str, Any],
    output_template: Mapping[str, Any],
    values: Mapping[str, Any],
    record: bool,
    script_path: Path,
) -> tuple[int, MappingLike]:
    unavailable = sorted(set(unavailable))
    degraded = bool(unavailable) if config["manifest_degraded"] == "unavailable" else bool(problems or unavailable)
    manifest_path = common.write_live_manifest(
        project, task=task, collector=config["collector"], command_argv=list(command_argv),
        tool_versions=tool_versions, artifacts=artifacts, summary=summary, degraded=degraded,
        unavailable_capabilities=unavailable, problems=problems,
        source_hash_before=source_hash_before, source_hash_after=common.compute_source_hash(project),
        runtime_asset_hash=runtime_asset_hash,
    )
    envelope = evidence.adapt_v1_manifest(
        common.read_json(manifest_path, {}), capability=config["capability"], provider=provider,
    )
    envelope["provenance"].update(provenance)
    envelope_path = out_dir / common.LIVE_EVIDENCE_FILENAME
    envelope = evidence.write_envelope(
        envelope_path, envelope, project_root=project, verify_artifacts=True,
    )
    proof_argv = _proof_argv(project, task, artifacts, config, values)
    output_values = {
        **values, "task": task, "artifact_dir": common.project_relative(project, out_dir),
        "manifest": common.project_relative(project, manifest_path),
        "evidence": common.project_relative(project, envelope_path),
        "evidence_schema": envelope["schema"], "evidence_verdict": envelope["verdict"],
        "degraded": degraded, "unavailable": unavailable, "problems": problems,
        "handoff_ready": not bool(problems or unavailable), "proof_argv": proof_argv,
        "proof_command": shlex.join(proof_argv),
    }
    output_values["artifacts"] = {
        name: common.project_relative(project, path) for name, path in artifacts.items()
    }
    output = render_descriptor(output_template, output_values)
    record_failed = False
    if record:
        result = common.run_trusted_command(proof_argv, cwd=project, script_path=script_path)
        if config["omit_record_command"]:
            result.pop("command_argv", None)
        if key := config["recorded_key"]:
            output[key] = True
        output[config["record_key"]] = result
        record_failed = int(result.get("returncode") or 0) != 0
        if record_failed and config["record_failure_rule"]:
            problems.append(common.blocking_problem(
                config["record_failure_message"].format(returncode=result.get("returncode")),
                rule=config["record_failure_rule"],
            ))
    return (1 if problems or unavailable or record_failed else 0), output
