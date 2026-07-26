"""Cohesive Star Forge runtime extracted from the CLI facade."""

from __future__ import annotations
import argparse
import plistlib
from functools import partial
from pathlib import Path
from typing import Any
from live_collectors import common as live_common
from live_collectors import native_macos as native_macos_collector
from .policy_data import value as _policy_value
from .runtime_support import relative_to_project
from .runtime_project import ensure_state_dirs, resolve_project
from .runtime_preview import append_artifact_once, current_live_source_hash, default_live_manifest_path, live_manifest_summary, live_problem, load_and_validate_live_manifest, manifest_artifact_path_for_kind, require_raw_hash_for_artifact, result_indicates_failure, validate_artifact_arg, validate_manifest_bound_artifact_arg, write_live_proof_record

_NATIVE_POLICY = _policy_value("runtime_native.POLICY")
_PLATFORMS = _NATIVE_POLICY["platforms"]
NATIVE_MACOS_RESULT_SCHEMA = _NATIVE_POLICY["schemas"]["macos_result"]
SECURITY_PROFILES = set(_NATIVE_POLICY["security_profiles"])

def _native_problem(name: str, path: str = "", **values: object) -> dict[str, Any]:
    message, rule = _NATIVE_POLICY["problems"][name]
    return live_problem(message.format(**values), rule=rule, path=path)

def _report_if(condition: bool, problems: list[dict[str, Any]], name: str,
               path: str = "", **values: object) -> None:
    if condition:
        problems.append(_native_problem(name, path, **values))

def _bound_artifact(
    project: Path, raw: str, label: str, problems: list[dict[str, Any]], *,
    manifest: dict[str, Any] | None, task: str, collector: str,
    artifacts: list[dict[str, Any]], **requirements: bool,
) -> tuple[dict[str, Any] | None, Any]:
    entry, payload = validate_manifest_bound_artifact_arg(
        project, raw, label, problems, manifest=manifest, task=task,
        collector=collector, **requirements)
    append_artifact_once(artifacts, entry)
    return entry, payload

def _write_native_record(
    args: argparse.Namespace, project: Path, platform: str,
    manifest_path: Path, manifest: dict[str, Any] | None,
    problems: list[dict[str, Any]], artifacts: list[dict[str, Any]],
) -> int:
    descriptor = _PLATFORMS[platform]
    return write_live_proof_record(
        project, kind=descriptor["proof_kind"], task=args.task, strict=args.strict,
        inputs=vars(args), problems=problems, manifest_path=manifest_path,
        manifest=manifest, artifacts=artifacts, summary=descriptor["summary"])

def validate_native_ios_result_artifact(
    project: Path,
    manifest: dict[str, Any] | None,
    payload: Any,
    entry: dict[str, Any] | None,
    *,
    expected_kind: str,
    label: str,
    scheme: str,
    simulator: str,
    problems: list[dict[str, Any]],
) -> None:
    path = entry.get("path", "") if entry else ""
    if not isinstance(payload, dict):
        return
    summary = live_manifest_summary(manifest)
    simulator_summary = summary.get("simulator") if isinstance(summary.get("simulator"), dict) else {}
    expected_runtime = str(summary.get("simulator_runtime") or simulator_summary.get("runtime") or "").strip()
    expected_udid = str(summary.get("simulator_udid") or simulator_summary.get("udid") or "").strip()
    try:
        from live_collectors import native_ios
        native_ios.validate_result_artifact_contract(
            label, expected_kind, payload, problems, path=path,
            expected_runtime=expected_runtime, expected_udid=expected_udid)
    except Exception as exc:
        problems.append(_native_problem("ios_result_validation", path, label=label, error=exc))
    result_scheme = str(payload.get("scheme") or payload.get("scheme_name") or "").strip()
    _report_if(bool(result_scheme and result_scheme != scheme), problems, "ios_scheme_mismatch", path, label=label)
    result_simulator = str(payload.get("simulator") or payload.get("device") or payload.get("destination") or "").strip()
    mismatch = bool(result_simulator and simulator and simulator not in result_simulator
                    and result_simulator not in simulator)
    _report_if(mismatch, problems, "ios_simulator_mismatch", path, label=label)

def cmd_native_ios_proof(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    problems: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    policy = _PLATFORMS["ios"]
    collector = policy["collector"]
    for attribute, problem in policy["required_args"]:
        _report_if(not str(getattr(args, attribute) or "").strip(), problems, problem)
    manifest_path = default_live_manifest_path(project, args.task, collector)
    manifest, manifest_resolved = load_and_validate_live_manifest(
        project, manifest_path, problems, task=args.task, collector=collector)
    bound = partial(
        _bound_artifact, project, problems=problems, manifest=manifest,
        task=args.task, collector=collector, artifacts=artifacts)
    summary = live_manifest_summary(manifest)
    session_artifacts = {}
    for filename, name in policy["session_artifacts"]:
        _, session_artifacts[name] = bound(
            str(manifest_path.parent / filename), filename,
            require_json=True, require_object=True)
    for label, attribute in policy["result_artifacts"]:
        entry, payload = bound(
            getattr(args, attribute), label,
            require_json=True, require_object=True)
        if args.strict:
            validate_native_ios_result_artifact(
                project, manifest, payload, entry, expected_kind=label.split()[0],
                label=label, scheme=args.scheme, simulator=args.simulator,
                problems=problems)
        if failure := result_indicates_failure(payload):
            path = entry.get("path", "") if entry else ""
            problems.append(_native_problem(
                "ios_result_failed", path, label=label, failure=failure))
    visual_entries = {}
    for label, attribute, kind in policy["visual_artifacts"]:
        raw = getattr(args, attribute)
        visual_entries[attribute] = None
        if raw:
            requirements = ({"require_image": True} if kind == "image"
                            else {"require_json": True, "require_object": True})
            visual_entries[attribute], _ = bound(raw, label, **requirements)
    screenshot_entry = visual_entries["screenshot"]
    snapshot_entry = visual_entries["ui_snapshot"]
    _report_if(not screenshot_entry and not snapshot_entry, problems, "ios_ui_required")
    session_payload = session_artifacts["session"]
    transcript_payload = session_artifacts["transcript"]
    if session_payload is not None and transcript_payload is not None:
        try:
            from live_collectors import native_ios
            validator_args = argparse.Namespace(**policy["transcript_args"])
            native_ios.validate_session_defaults(
                session_payload, scheme=args.scheme, simulator=args.simulator,
                problems=problems, **policy["session_validation_defaults"])
            current_source = current_live_source_hash(project, problems)
            if current_source is not None:
                _transcript_summary, transcript_problems, _unavailable = native_ios.validate_transcript(
                    transcript_payload, session_payload, scheme=args.scheme,
                    simulator=args.simulator,
                    current_source_hash=current_source,
                    has_screenshot=screenshot_entry is not None,
                    has_ui_snapshot=snapshot_entry is not None,
                    args=validator_args)
                problems.extend(dict(item) for item in transcript_problems)
        except Exception as exc:
            problems.append(_native_problem("ios_transcript_validation", error=exc))
    _report_if(not summary.get("app_identity"), problems, "ios_app_identity")
    return _write_native_record(
        args, project, "ios", manifest_resolved, manifest, problems, artifacts)

def _validate_output_artifacts(
    project: Path, manifest: dict[str, Any] | None, payload: dict[str, Any],
    problems: list[dict[str, Any]], *, prefix: str, label: str = "",
) -> None:
    rule = _NATIVE_POLICY["problems"][f"{prefix}_artifact_invalid"][1]
    for key in _NATIVE_POLICY["macos_result"]["artifact_keys"]:
        raw = payload.get(key)
        if not raw:
            continue
        try:
            artifact = live_common.safe_project_path(project, raw, must_exist=True)
        except ValueError as exc:
            problems.append(_native_problem(
                f"{prefix}_artifact_invalid", str(raw), label=label,
                key=key, error=exc))
            continue
        if not artifact.is_file():
            problems.append(_native_problem(
                f"{prefix}_artifact_file", relative_to_project(artifact, project),
                label=label, key=key))
            continue
        artifact_label = f"{label} {key}".strip()
        require_raw_hash_for_artifact(
            project, manifest, artifact, problems, label=artifact_label, rule=rule)

def validate_native_macos_runtime(project: Path, manifest: dict[str, Any] | None, payload: Any, entry: dict[str, Any] | None, problems: list[dict[str, Any]]) -> None:
    path = entry.get("path", "") if entry else ""
    if not isinstance(payload, dict):
        return
    policy = _NATIVE_POLICY["macos_result"]
    required = policy["runtime_required_fields"]
    missing = [key for key in required if key not in payload]
    _report_if(bool(missing), problems, "macos_runtime_missing", path, fields=", ".join(missing))
    _report_if(payload.get("success") is not True, problems, "macos_runtime_success", path)
    for field, problem in policy["failure_flags"]:
        _report_if(payload.get(field) is True, problems, problem, path)
    readiness = payload.get("readiness")
    _report_if(not isinstance(readiness, dict), problems, "macos_readiness_object", path)
    _report_if(
        isinstance(readiness, dict)
        and str(readiness.get("status") or "") not in policy["readiness_statuses"],
        problems, "macos_readiness_status", path)
    cleanup = payload.get("cleanup")
    _report_if(not isinstance(cleanup, dict) or cleanup.get("success") is not True, problems, "macos_cleanup_success", path)
    termination = payload.get("termination")
    _report_if(not isinstance(termination, dict), problems, "macos_termination_object", path)
    _validate_output_artifacts(
        project, manifest, payload, problems, prefix="macos_runtime")

def numeric_field(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    return (float(value) if isinstance(value, (int, float))
            and not isinstance(value, bool) else None)

def validate_native_macos_result_artifact(
    project: Path,
    manifest: dict[str, Any] | None,
    payload: Any,
    entry: dict[str, Any] | None,
    *,
    expected_kind: str,
    label: str,
    problems: list[dict[str, Any]],
) -> None:
    path = entry.get("path", "") if entry else ""
    if not isinstance(payload, dict):
        return
    policy = _NATIVE_POLICY["macos_result"]
    _report_if(payload.get("schema") != NATIVE_MACOS_RESULT_SCHEMA, problems, "macos_result_schema", path, label=label, schema=NATIVE_MACOS_RESULT_SCHEMA)
    _report_if(str(payload.get("kind") or "") != expected_kind, problems, "macos_result_kind", path, label=label, kind=expected_kind)
    command_argv = payload.get("command_argv")
    valid_command_argv: list[str] | None = None
    if (isinstance(command_argv, list) and command_argv and all(isinstance(item, str) and item and "\0" not in item for item in command_argv)):
        valid_command_argv = list(command_argv)
    else:
        problems.append(_native_problem("macos_result_argv", path, label=label))
    if valid_command_argv is not None:
        for issue in native_macos_collector.validate_argv(valid_command_argv, label):
            issue = dict(issue)
            if path and not issue.get("path"):
                issue["path"] = path
            problems.append(issue)
    for field, expected, problem in policy["fixed_fields"]:
        _report_if(payload.get(field) != expected, problems, problem, path, label=label)
    _report_if(valid_command_argv is not None and not str(payload.get("executable_path") or ""), problems, "macos_result_executable", path, label=label)
    timeout = numeric_field(payload, "timeout_seconds")
    _report_if(timeout is None or timeout <= 0 or timeout > policy["timeout_max_seconds"], problems, "macos_result_timeout", path, label=label)
    duration = numeric_field(payload, "duration_seconds")
    invalid_duration = duration is None or duration < 0
    _report_if(invalid_duration, problems, "macos_result_duration", path, label=label)
    _report_if(not invalid_duration and timeout is not None and duration > timeout + policy["duration_slack_seconds"], problems, "macos_result_duration_timeout", path, label=label)
    artifact_keys = policy["artifact_keys"]
    _report_if(valid_command_argv is not None and not any(payload.get(key) for key in artifact_keys), problems, "macos_result_artifact_required", path, label=label)
    _validate_output_artifacts(
        project, manifest, payload, problems,
        prefix="macos_result", label=label)

def validate_native_macos_note(payload: Any, kind: str, entry: dict[str, Any] | None, problems: list[dict[str, Any]]) -> None:
    path = entry.get("path", "") if entry else ""
    if not isinstance(payload, dict):
        return
    _report_if(payload.get("schema") != _NATIVE_POLICY["schemas"]["macos_note"], problems, "macos_note_schema", path, kind=kind)
    _report_if(payload.get("kind") != kind, problems, "macos_note_kind", path, kind=kind)
    for field, expected, problem in _NATIVE_POLICY["macos_note"]["required_fields"]:
        _report_if(payload.get(field) != expected, problems, problem, path, kind=kind)

def validate_native_macos_metadata(payload: Any, app_name: str, bundle_id: str, app_bundle_entry: dict[str, Any] | None, entry: dict[str, Any] | None,
                                   problems: list[dict[str, Any]]) -> None:
    path = entry.get("path", "") if entry else ""
    if not isinstance(payload, dict):
        return
    _report_if(payload.get("schema") != _NATIVE_POLICY["schemas"]["macos_metadata"], problems, "macos_metadata_schema", path)
    for field, problem in _NATIVE_POLICY["macos_metadata"]["required_true"]:
        _report_if(payload.get(field) is not True, problems, problem, path)
    _report_if(bool(bundle_id and payload.get("bundle_id") != bundle_id), problems, "macos_metadata_bundle_id", path)
    if app_name:
        valid_names = {str(payload.get("app_name") or ""), str(payload.get("display_name") or "")}
        bundle_path = str(payload.get("app_bundle") or "")
        if bundle_path:
            valid_names.add(Path(bundle_path).stem)
        _report_if(app_name not in valid_names, problems, "macos_metadata_app_name", path)
    metadata_path_mismatch = bool(
        app_bundle_entry and app_bundle_entry.get("path")
        and payload.get("app_bundle")
        and str(app_bundle_entry.get("path")) != str(payload.get("app_bundle")))
    _report_if(metadata_path_mismatch, problems, "macos_metadata_path", path)

def validate_native_macos_app_bundle(project: Path, raw_path: str, app_name: str, bundle_id: str, entry: dict[str, Any] | None, problems: list[dict[str, Any]]) -> None:
    path_text = entry.get("path", "") if entry else str(raw_path or "")
    policy = _NATIVE_POLICY["app_bundle"]
    if not raw_path:
        problems.append(_native_problem("macos_bundle_required"))
        return
    try:
        bundle = live_common.safe_project_path(project, raw_path, must_exist=True)
    except ValueError as exc:
        problems.append(_native_problem("macos_bundle_invalid", path_text, error=exc))
        return
    if not bundle.is_dir() or bundle.suffix != policy["extension"]:
        problems.append(_native_problem("macos_bundle_directory", path_text))
        return
    info_plist = bundle.joinpath(*policy["info_plist"])
    plist_path = relative_to_project(info_plist, project)
    if not info_plist.exists():
        problems.append(_native_problem("macos_bundle_plist_missing", plist_path))
        return
    try:
        with info_plist.open("rb") as handle:
            plist = plistlib.load(handle)
    except Exception as exc:
        problems.append(_native_problem(
            "macos_bundle_plist_malformed", plist_path, error=exc))
        return
    if not isinstance(plist, dict):
        problems.append(_native_problem("macos_bundle_plist_object", plist_path))
        return
    keys = policy["plist_keys"]
    found_bundle_id = str(plist.get(keys["bundle_id"]) or "")
    found_executable = str(plist.get(keys["executable"]) or "")
    found_name = str(plist.get(keys["name"]) or bundle.stem)
    found_display_name = str(plist.get(keys["display_name"]) or "")
    if bundle_id and found_bundle_id != bundle_id:
        problems.append(_native_problem("macos_bundle_id_mismatch", plist_path))
    if app_name and app_name not in {found_name, found_display_name, bundle.stem}:
        problems.append(_native_problem("macos_bundle_name_mismatch", plist_path))
    if not found_bundle_id:
        problems.append(_native_problem("macos_bundle_id_missing", plist_path))
    if not found_executable:
        problems.append(_native_problem("macos_bundle_executable_missing", plist_path))
    else:
        executable = bundle.joinpath(*policy["executable_path"], found_executable)
        if not executable.is_file():
            problems.append(_native_problem(
                "macos_bundle_executable_file",
                relative_to_project(executable, project)))

def cmd_native_macos_proof(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    problems: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    policy = _PLATFORMS["macos"]
    collector = policy["collector"]
    missing_identity = not str(args.app_name or "").strip() and not str(args.bundle_id or "").strip()
    _report_if(missing_identity, problems, "macos_identity_required")
    manifest_path = default_live_manifest_path(project, args.task, collector)
    manifest, manifest_resolved = load_and_validate_live_manifest(
        project, manifest_path, problems, task=args.task, collector=collector)
    bound = partial(
        _bound_artifact, project, problems=problems, manifest=manifest,
        task=args.task, collector=collector, artifacts=artifacts)
    for label, attribute, optional in policy["result_artifacts"]:
        expected_kind = label.split()[0]
        entry, payload = bound(
            getattr(args, attribute), label,
            require_json=True, require_object=True, optional=optional)
        if payload is not None:
            if args.strict:
                validate_native_macos_result_artifact(
                    project, manifest, payload, entry, expected_kind=expected_kind,
                    label=label, problems=problems)
            if failure := result_indicates_failure(payload):
                path = entry.get("path", "") if entry else ""
                problems.append(_native_problem(
                    "macos_result_failed", path, label=label, failure=failure))
            if label == "run result":
                validate_native_macos_runtime(project, manifest, payload, entry, problems)
    if args.screenshot:
        bound(args.screenshot, "screenshot", require_image=True)
    if args.strict:
        screenshot_result = manifest_artifact_path_for_kind(project, manifest, "screenshot-result", "screenshot_result")
        if screenshot_result is not None:
            entry, payload = bound(
                screenshot_result, "screenshot result",
                require_json=True, require_object=True)
            validate_native_macos_result_artifact(
                project, manifest, payload, entry, expected_kind="screenshot",
                label="screenshot result", problems=problems)
    app_bundle_entry = None
    if args.app_bundle:
        entry, _ = validate_artifact_arg(project, args.app_bundle, "app bundle", problems, task=args.task, collector="native-macos", require_scoped=False, require_dir=True)
        if entry:
            artifacts.append(entry)
            app_bundle_entry = entry
        _report_if(not str(args.app_bundle).endswith(".app"), problems, "macos_bundle_extension", entry.get("path", "") if entry else "")
    validate_native_macos_app_bundle(project, args.app_bundle, args.app_name, args.bundle_id, app_bundle_entry, problems)
    metadata_entry, metadata_payload = bound(
        str(manifest_path.parent / "app-bundle-metadata.json"),
        "app bundle metadata", require_json=True, require_object=True)
    validate_native_macos_metadata(metadata_payload, args.app_name, args.bundle_id, app_bundle_entry, metadata_entry, problems)
    for kind, attribute in policy["note_artifacts"]:
        entry, payload = bound(
            getattr(args, attribute), f"{kind} note",
            require_json=True, require_object=True)
        validate_native_macos_note(payload, kind, entry, problems)
    summary = live_manifest_summary(manifest)
    _report_if(not summary.get("app_bundle_metadata"), problems, "macos_manifest_metadata")
    return _write_native_record(
        args, project, "macos", manifest_resolved, manifest, problems, artifacts)

__all__ = tuple(name for name in globals() if not name.startswith("__"))
