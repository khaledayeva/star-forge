"""Cohesive Star Forge runtime extracted from the CLI facade."""

from __future__ import annotations
import argparse
import re
from pathlib import Path
from typing import Any, Mapping
from live_collectors import common as live_common
from .policy_data import value as _policy_value
from .runtime_support import BLOCKING_SEVERITIES, FINDING_SEVERITIES, dirty_paths_missing_from_source_snapshot, file_sha256, git_head, tree_clean_for_commit_binding
from .runtime_project import ensure_state_dirs, resolve_project
from .runtime_preview import append_artifact_once, collector_for_profile, current_live_source_hash, flag_live_problem, is_task_scoped_live_path, live_manifest_summary, live_problem, live_rel, load_and_validate_live_manifest, require_raw_hash_for_artifact, task_from_scoped_live_path, validate_artifact_arg, validate_manifest_artifact_scopes, validate_manifest_bound_artifact_arg, validate_raw_artifact_hashes, write_live_proof_record

SECURITY_POLICY = _policy_value("runtime_security.POLICY")

def validate_security_findings_payload(payload: Any, path: str) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    raw = payload.get("findings") if isinstance(payload, dict) else payload
    problems: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return None, problems
    findings: list[dict[str, Any]] = []
    required = SECURITY_POLICY["finding_required_fields"]
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(live_problem(f"security finding {idx + 1} must be a JSON object", rule="security-findings", path=path))
            continue
        missing = [key for key in required if not item.get(key)]
        if missing:
            problems.append(live_problem(f"security finding {idx + 1} is missing normalized fields: " + ", ".join(missing), rule="security-findings", path=path))
        fingerprint = str(item.get("fingerprint") or "")
        if fingerprint and not fingerprint.startswith("sfsec-"):
            problems.append(live_problem(f"security finding {idx + 1} fingerprint is not deterministic", rule="security-findings", path=path))
        findings.append(item)
    return findings, problems
SECURITY_HANDOFF_INPUT_SCHEMA = SECURITY_POLICY["schemas"]["handoff_input"]
SECURITY_INPUT_HASH_SCHEMA = SECURITY_POLICY["schemas"]["input_hash"]
SECURITY_FINDINGS_SCHEMA = SECURITY_POLICY["schemas"]["findings"]
SECURITY_REDACTION_SCHEMA = SECURITY_POLICY["schemas"]["redaction"]
TRUSTED_SECURITY_SCHEMA_FAMILIES = set(SECURITY_POLICY["trusted_schema_families"])

def valid_sha256(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", str(value or "")))

def security_clean_problem(message: str, *, path: str = "") -> dict[str, Any]:
    return live_problem(message, rule="security-clean-proof", path=path)

def validate_required_match(
    problems: list[dict[str, Any]], actual: Any, expected: Any, missing: str, mismatch: str,
    *, required: bool = True, rule: str = "github-live-provenance", path: str = "",
) -> None:
    if required and not actual:
        problems.append(live_problem(missing, rule=rule, path=path))
    elif expected and actual != expected:
        problems.append(live_problem(mismatch, rule=rule, path=path))

def source_binding_is_fresh(project: Path, binding: Mapping[str, Any], *, current_source_hash: str | None = None) -> bool:
    has_source_hash = bool(str(binding.get("source_hash") or ""))
    if current_source_hash is not None and str(binding.get("source_hash") or "") == current_source_hash:
        return not dirty_paths_missing_from_source_snapshot(project)
    commit_sha, head = str(binding.get("commit_sha") or ""), git_head(project)
    if not commit_sha or not head or commit_sha != head:
        return current_source_hash is None and has_source_hash
    return tree_clean_for_commit_binding(project)

def finish_security_command(
    project: Path, args: argparse.Namespace, kind: str, task: str | None, problems: list[dict[str, Any]],
    manifest: dict[str, Any] | None, manifest_path: Path | None, artifacts: list[dict[str, Any]], summary: str,
) -> int:
    return write_live_proof_record(
        project, kind=kind, task=task, strict=args.strict, inputs=vars(args), problems=problems,
        manifest_path=manifest_path, manifest=manifest, artifacts=artifacts, summary=summary,
    )

def security_command_state(args: argparse.Namespace) -> tuple[Path, list[dict[str, Any]]]:
    project = resolve_project(args.project)
    ensure_state_dirs(project)
    return project, []

def load_security_artifact(
    project: Path, raw_path: str, label: str, problems: list[dict[str, Any]], artifacts: list[dict[str, Any]],
    manifest: dict[str, Any] | None, *, rule: str, task: str | None, collector: str,
    require_object: bool = False, require_json: bool = True,
) -> tuple[dict[str, Any] | None, Any]:
    result = validate_manifest_bound_artifact_arg(
        project, raw_path, label, problems, manifest=manifest, raw_hash_rule=rule, task=task,
        collector=collector, require_scoped=True, require_json=require_json, require_object=require_object,
    )
    append_artifact_once(artifacts, result[0])
    return result

def validate_clean_security_artifacts(
    project: Path,
    args: argparse.Namespace,
    manifest: dict[str, Any] | None,
    manifest_path: Path | None,
    findings_entry: dict[str, Any] | None,
    findings_payload: Any,
    artifacts: list[dict[str, Any]],
    problems: list[dict[str, Any]],
) -> None:
    if manifest_path is None or not isinstance(manifest, dict):
        problems.append(security_clean_problem("clean security proof requires a scoped security manifest"))
        return
    root = manifest_path.parent
    task = str(args.task or "")
    payloads: dict[str, Any] = {}
    entries: dict[str, dict[str, Any]] = {}
    for label, filename in SECURITY_POLICY["clean_artifacts"].items():
        entry, payload = load_security_artifact(project, str(root / filename), label, problems, artifacts, manifest, rule="security-clean-proof", task=task, collector="security", require_object=True)
        if entry:
            entries[label] = entry
        payloads[label] = payload
        flag_live_problem(problems, not entry or not entry.get("exists"), f"clean security proof requires scoped {filename}", rule="security-clean-proof", path=live_rel(project, root / filename))
    normalized_entry = entries.get("normalized-findings")
    flag_live_problem(problems, findings_entry and normalized_entry and findings_entry.get("path") != normalized_entry.get("path"), "security proof findings argument must match the manifest normalized findings artifact", rule="security-findings", path=str(findings_entry.get("path") or "") if findings_entry else "")
    handoff = payloads.get("handoff-input")
    input_hash = payloads.get("input-hash")
    normalized = payloads.get("normalized-findings")
    redaction = payloads.get("redaction-report")
    summary = live_manifest_summary(manifest)
    current_source = current_live_source_hash(project, problems)
    if isinstance(normalized, dict):
        normalized_path = str(normalized_entry.get("path") if normalized_entry else "")
        flag_live_problem(problems, normalized.get("schema") != SECURITY_FINDINGS_SCHEMA, "normalized findings schema is invalid", rule="security-clean-proof", path=normalized_path)
        flag_live_problem(problems, normalized.get("task") and str(normalized.get("task")) != task, "normalized findings task does not match proof task", rule="security-task", path=normalized_path)
        flag_live_problem(problems, normalized.get("profile") and str(normalized.get("profile")) != str(args.profile), "normalized findings profile does not match proof profile", rule="security-profile", path=normalized_path)
        normalized_findings = normalized.get("findings")
        flag_live_problem(problems, not isinstance(normalized_findings, list), "normalized findings artifact must contain a findings array", rule="security-findings", path=normalized_path)
        flag_live_problem(problems, findings_payload != normalized, "security proof findings payload does not match the manifest normalized findings artifact", rule="security-findings", path=normalized_path)
    normalized_artifact_path = str(normalized_entry.get("path") if normalized_entry else "")
    normalized_artifact_sha = str(normalized_entry.get("sha256") if normalized_entry else "").lower()
    actual_input_hash = ""
    if isinstance(input_hash, dict):
        input_hash_path = str(entries.get("input-hash", {}).get("path") or "")
        flag_live_problem(problems, input_hash.get("schema") != SECURITY_INPUT_HASH_SCHEMA, "input hash schema is invalid", rule="security-clean-proof", path=input_hash_path)
        declared = str(input_hash.get("declared_sha256") or "").lower()
        actual = str(input_hash.get("actual_sha256") or "").lower()
        actual_input_hash = actual
        flag_live_problem(problems, not valid_sha256(declared) or not valid_sha256(actual) or declared != actual or input_hash.get("matches") is not True, "security input hash artifact does not prove matching input bytes", rule="security-input-hash", path=input_hash_path)
        raw_input_path = str(input_hash.get("input_path") or "")
        if not raw_input_path:
            problems.append(live_problem("security input hash artifact is missing input_path", rule="security-input-hash", path=input_hash_path))
        else:
            try:
                resolved_input = live_common.safe_project_path(project, raw_input_path, must_exist=True)
            except ValueError as exc:
                problems.append(live_problem(f"security input hash path is unsafe or missing: {exc}", rule="security-input-hash", path=raw_input_path))
            else:
                flag_live_problem(problems, file_sha256(resolved_input).lower() != actual, "security input hash does not match current input bytes", rule="security-input-hash", path=raw_input_path)
    if isinstance(handoff, dict):
        handoff_path = str(entries.get("handoff-input", {}).get("path") or "")
        flag_live_problem(problems, handoff.get("schema") != SECURITY_HANDOFF_INPUT_SCHEMA, "handoff input schema is invalid", rule="security-clean-proof", path=handoff_path)
        flag_live_problem(problems, str(handoff.get("task") or "") != task, "handoff input task does not match proof task", rule="security-task", path=handoff_path)
        flag_live_problem(problems, str(handoff.get("profile") or "") != str(args.profile), "handoff input profile does not match proof profile", rule="security-profile", path=handoff_path)
        handoff_kind = str(handoff.get("kind") or "")
        expected_kind = str(getattr(args, "kind", "") or "")
        validate_required_match(problems, handoff_kind, expected_kind, "handoff input requires kind", "handoff input kind does not match proof kind", rule="security-kind", path=handoff_path)
        provenance = handoff.get("provenance")
        if not isinstance(provenance, dict):
            problems.append(live_problem("handoff input requires scanner provenance", rule="security-provenance", path=handoff_path))
            provenance = {}
        schema_family = str(provenance.get("schema_family") or "")
        trusted_schema = provenance.get("trusted_schema") is True and schema_family in TRUSTED_SECURITY_SCHEMA_FAMILIES
        flag_live_problem(problems, not trusted_schema, "security proof requires trusted scanner schema provenance", rule="security-provenance", path=handoff_path)
        scanner = str(handoff.get("scanner") or provenance.get("scanner") or "")
        scanner_version = str(handoff.get("scanner_version") or provenance.get("scanner_version") or "")
        scanner_ready = bool(scanner and scanner_version)
        flag_live_problem(problems, not scanner_ready, "handoff input requires scanner and scanner_version", rule="security-provenance", path=handoff_path)
        flag_live_problem(problems, scanner_ready and (scanner != str(args.scanner or "") or scanner_version != str(args.scanner_version or "")), "handoff scanner provenance does not match proof arguments", rule="security-provenance", path=handoff_path)
        flag_live_problem(problems, not handoff.get("ruleset"), "security proof requires ruleset provenance", rule="security-ruleset", path=handoff_path)
        flag_live_problem(problems, not handoff.get("scan_scope"), "security proof requires scan scope", rule="security-scope", path=handoff_path)
        source_binding = handoff.get("source_binding")
        flag_live_problem(problems, not isinstance(source_binding, dict) or not source_binding_is_fresh(project, source_binding, current_source_hash=current_source), "security proof requires a fresh source_hash or commit binding", rule="security-source-binding", path=handoff_path)
        handoff_input_hash = handoff.get("input_hash")
        if isinstance(handoff_input_hash, dict) and isinstance(input_hash, dict):
            flag_live_problem(problems, str(handoff_input_hash.get("actual_sha256") or "").lower() != str(input_hash.get("actual_sha256") or "").lower(), "handoff input hash does not match input-hash artifact", rule="security-input-hash", path=handoff_path)
        else:
            problems.append(live_problem("handoff input must include input_hash details", rule="security-input-hash", path=handoff_path))
        handoff_findings = handoff.get("normalized_findings")
        if isinstance(handoff_findings, dict):
            declared_path = str(handoff_findings.get("path") or "")
            declared_hash = str(handoff_findings.get("sha256") or "").lower()
            flag_live_problem(problems, declared_path and normalized_artifact_path and declared_path != normalized_artifact_path, "handoff normalized findings path does not match artifact", rule="security-findings", path=handoff_path)
            flag_live_problem(problems, not valid_sha256(declared_hash) or (normalized_artifact_sha and declared_hash != normalized_artifact_sha), "handoff normalized findings hash does not match artifact", rule="security-findings", path=handoff_path)
            if isinstance(normalized, dict):
                findings_array = normalized.get("findings")
                if isinstance(findings_array, list) and "finding_count" in handoff_findings:
                    try:
                        declared_count = int(handoff_findings.get("finding_count"))
                    except (TypeError, ValueError):
                        declared_count = -1
                    flag_live_problem(problems, declared_count != len(findings_array), "handoff normalized findings count does not match artifact", rule="security-findings", path=handoff_path)
        else:
            problems.append(live_problem("handoff input must include normalized findings hash", rule="security-findings", path=handoff_path))
    if isinstance(redaction, dict):
        redaction_path = str(entries.get("redaction-report", {}).get("path") or "")
        flag_live_problem(problems, redaction.get("schema") != SECURITY_REDACTION_SCHEMA, "redaction report schema is invalid", rule="security-clean-proof", path=redaction_path)
        flag_live_problem(problems, not isinstance(redaction.get("counts"), dict), "redaction report must include counts", rule="security-clean-proof", path=redaction_path)
    flag_live_problem(problems, summary.get("trusted_provenance") is not True, "manifest summary must mark trusted_provenance true", rule="security-clean-proof")
    flag_live_problem(problems, not summary.get("ruleset"), "clean security proof is missing ruleset metadata", rule="security-ruleset")
    flag_live_problem(problems, not summary.get("scan_scope"), "clean security proof is missing scan scope", rule="security-scope")
    flag_live_problem(problems, actual_input_hash and str(summary.get("input_hash") or "").lower() != actual_input_hash, "manifest summary input_hash does not match input-hash artifact", rule="security-input-hash")

def cmd_security_handoff_packet(args: argparse.Namespace) -> int:
    project, problems = security_command_state(args)
    artifacts: list[dict[str, Any]] = []
    input_entry, input_payload = validate_artifact_arg(project, args.input, "handoff input", problems, require_json=True, require_object=True, require_scoped=False)
    if input_entry:
        artifacts.append(input_entry)
    task = str(getattr(args, "task", "") or "")
    input_path: Path | None = None
    if input_entry and input_entry.get("path"):
        try:
            input_path = live_common.safe_project_path(project, str(input_entry["path"]), must_exist=False)
        except ValueError as exc:
            problems.append(live_problem(f"handoff input path is unsafe: {exc}", rule="artifact-path", path=str(input_entry.get("path") or "")))
        if not task and isinstance(input_payload, dict):
            task = str(input_payload.get("task") or "")
        if not task and input_path is not None:
            task = task_from_scoped_live_path(project, input_path, "security") or ""
        if input_path is not None and not is_task_scoped_live_path(project, input_path, task or None, "security"):
            problems.append(live_problem("handoff input must be under .starforge/live/<task>/security/", rule="artifact-scope", path=input_entry.get("path", "")))
    flag_live_problem(problems, isinstance(input_payload, dict) and input_payload.get("task") and task and str(input_payload.get("task")) != task, "handoff input task does not match scoped task", rule="security-task", path=input_entry.get("path", "") if input_entry else "")
    manifest = None
    manifest_path = None
    if input_path is not None:
        manifest_candidate = input_path.parent / "manifest.json"
        manifest, manifest_path = load_and_validate_live_manifest(project, manifest_candidate, problems, task=task or None, collector="security", require_scoped=True)
        require_raw_hash_for_artifact(project, manifest, input_path, problems, label="handoff input", rule="security-clean-proof", attested_entry=input_entry)
    flag_live_problem(problems, not str(args.kind or "").strip(), "security handoff packet requires --kind", rule="security-kind")
    profile = ""
    scanner = ""
    scanner_version = ""
    if isinstance(input_payload, dict):
        profile = str(input_payload.get("profile") or "")
        handoff_kind = str(input_payload.get("kind") or "")
        provenance = input_payload.get("provenance") if isinstance(input_payload.get("provenance"), dict) else {}
        scanner = str(input_payload.get("scanner") or provenance.get("scanner") or "")
        scanner_version = str(input_payload.get("scanner_version") or provenance.get("scanner_version") or "")
        input_entry_path = input_entry.get("path", "") if input_entry else ""
        flag_live_problem(problems, not task, "security handoff input must include task", rule="security-task", path=input_entry_path)
        flag_live_problem(problems, not profile, "security handoff input must include profile", rule="security-profile", path=input_entry_path)
        if not handoff_kind:
            problems.append(live_problem("security handoff input must include kind", rule="security-kind", path=input_entry.get("path", "") if input_entry else ""))
        elif args.kind and handoff_kind != str(args.kind):
            problems.append(live_problem("security handoff input kind does not match --kind", rule="security-kind", path=input_entry.get("path", "") if input_entry else ""))
        flag_live_problem(problems, not scanner or not scanner_version, "security handoff input must include scanner and scanner_version", rule="security-provenance", path=input_entry_path)
    findings_entry: dict[str, Any] | None = None
    findings_payload: Any = None
    if manifest_path is not None:
        findings_entry, findings_payload = load_security_artifact(project, str(manifest_path.parent / "normalized-findings.json"), "normalized findings", problems, artifacts, manifest, rule="security-clean-proof", task=task or None, collector="security", require_object=True)
    validation_args = argparse.Namespace(
        task=task,
        profile=profile,
        kind=str(args.kind or ""),
        scanner=scanner,
        scanner_version=scanner_version,
    )
    validate_clean_security_artifacts(project, validation_args, manifest, manifest_path, findings_entry, findings_payload, artifacts, problems)
    return finish_security_command(project, args, "security-handoff-packet", task or None, problems, manifest, manifest_path, artifacts, "security handoff packet")

def cmd_security_proof(args: argparse.Namespace) -> int:
    project, problems = security_command_state(args)
    artifacts: list[dict[str, Any]] = []
    flag_live_problem(problems, args.profile not in SECURITY_POLICY["security_profiles"], "security proof profile is invalid", rule="security-profile")
    flag_live_problem(problems, not str(args.scanner or "").strip(), "security proof requires --scanner", rule="security-scanner")
    flag_live_problem(problems, not str(args.scanner_version or "").strip(), "security proof requires --scanner-version", rule="security-scanner")
    manifest, manifest_path = load_and_validate_live_manifest(project, args.artifact, problems, task=args.task, collector="security")
    findings_entry, findings_payload = load_security_artifact(project, args.findings, "normalized findings", problems, artifacts, manifest, rule="security-clean-proof", task=args.task, collector="security")
    findings, finding_shape_problems = validate_security_findings_payload(findings_payload, findings_entry.get("path", "") if findings_entry else "")
    problems.extend(finding_shape_problems)
    if findings is None:
        problems.append(
            live_problem("normalized findings must be an array or contain a findings array",
                         rule="security-findings",
                         path=findings_entry.get("path", "") if findings_entry else ""))
    else:
        for item in findings:
            severity = str(item.get("severity") or "").lower()
            if severity not in FINDING_SEVERITIES:
                problems.append(live_problem("security finding has unknown severity", rule="security-severity", path=findings_entry.get("path", "") if findings_entry else ""))
            elif severity in BLOCKING_SEVERITIES:
                problems.append(
                    live_problem(f"security finding is blocking severity: {severity}",
                                 rule="security-finding",
                                 path=findings_entry.get("path", "") if findings_entry else "",
                                 severity=severity))
    summary = live_manifest_summary(manifest)
    required = SECURITY_POLICY["security_summary_fields"]
    missing = [key for key in required if not summary.get(key)]
    flag_live_problem(problems, missing, "security proof is missing trusted provenance fields: " + ", ".join(missing), rule="security-clean-proof")
    validate_clean_security_artifacts(project, args, manifest, manifest_path, findings_entry, findings_payload, artifacts, problems)
    return finish_security_command(project, args, "security-proof", args.task, problems, manifest, manifest_path, artifacts, f"profile={args.profile}")

def github_live_source_marker(value: Any) -> bool:
    return str(value or "") in SECURITY_POLICY["github_live_source_markers"]

def github_fixture_marker(value: Any) -> bool:
    return (text := str(value or "").lower()) in SECURITY_POLICY["github_fixture_markers"] or "fixture" in text

def validate_github_operation_transcript(
    project: Path,
    manifest: dict[str, Any] | None,
    transcript_path: Path,
    transcript_payload: Any,
    summary: dict[str, Any],
    problems: list[dict[str, Any]],
    *,
    path: str,
    attested_entry: Mapping[str, Any] | None = None,
    check_runs_payload: Any = None,
    pr_payload: Any = None,
) -> str:
    actual_hash = require_raw_hash_for_artifact(
        project,
        manifest,
        transcript_path,
        problems,
        label="GitHub operation transcript",
        rule="github-live-provenance",
        attested_entry=attested_entry,
    )
    if not isinstance(transcript_payload, dict):
        problems.append(live_problem("GitHub operation transcript must be a JSON object", rule="github-live-provenance", path=path))
        return actual_hash
    flag_live_problem(problems, transcript_payload.get("schema") != SECURITY_POLICY["schemas"]["github_transcript"], "GitHub operation transcript schema is invalid", rule="github-live-provenance", path=path)
    transcript_source = str(transcript_payload.get("source") or "")
    flag_live_problem(problems, not github_live_source_marker(transcript_source), "GitHub operation transcript requires live source provenance", rule="github-live-provenance", path=path)
    flag_live_problem(problems, str(transcript_payload.get("repo") or "") != str(summary.get("repo") or ""), "GitHub operation transcript repo does not match the manifest summary", rule="github-live-provenance", path=path)
    flag_live_problem(problems, str(transcript_payload.get("pr") or "") != str(summary.get("pr") or ""), "GitHub operation transcript PR does not match the manifest summary", rule="github-live-provenance", path=path)
    flag_live_problem(problems, not str(transcript_payload.get("collected_at") or transcript_payload.get("captured_at") or "").strip(), "GitHub operation transcript requires a collection timestamp", rule="github-live-provenance", path=path)
    refs = transcript_payload.get("refs")
    if not isinstance(refs, dict):
        problems.append(live_problem("GitHub operation transcript requires freshness refs", rule="github-live-provenance", path=path))
        refs = {}
    for field in SECURITY_POLICY["github_freshness_fields"]:
        flag_live_problem(problems, str(refs.get(field) or "") != str(summary.get(field) or ""), f"GitHub operation transcript {field} does not match the manifest summary", rule="github-live-provenance", path=path)
    permission_state = transcript_payload.get("permission_state")
    pagination_state = transcript_payload.get("pagination_state")
    if not isinstance(permission_state, dict):
        problems.append(live_problem("GitHub operation transcript requires permission state", rule="github-live-provenance", path=path))
        permission_state = {}
    if not isinstance(pagination_state, dict):
        problems.append(live_problem("GitHub operation transcript requires pagination state", rule="github-live-provenance", path=path))
        pagination_state = {}
    flag_live_problem(problems, permission_state.get("partial_permissions"), "GitHub operation transcript reports partial permissions", rule="github-permissions", path=path)
    flag_live_problem(problems, pagination_state.get("pagination_incomplete"), "GitHub operation transcript reports incomplete pagination", rule="github-pagination", path=path)
    operations = transcript_payload.get("operations") if isinstance(transcript_payload.get("operations"), list) else []
    commands = transcript_payload.get("commands") if isinstance(transcript_payload.get("commands"), list) else []
    flag_live_problem(problems, not operations and not commands, "GitHub operation transcript requires read-only operations", rule="github-live-provenance", path=path)
    try:
        from live_collectors import github_pr
    except Exception as exc:
        problems.append(live_problem(f"GitHub operation validators are unavailable: {exc}", rule="github-live-provenance", path=path))
    else:
        github_host, host_messages = github_pr.validate_transcript_github_host_evidence(
            transcript_payload=transcript_payload,
            summary=summary,
            pr_payload=pr_payload,
            operations=operations,
        )
        for message in host_messages:
            problems.append(live_problem(message, rule="github-live-provenance", path=path))
        validation_context = {
            "repo": str(summary.get("repo") or ""), "pr_number": str(summary.get("pr") or ""),
            "check_runs": check_runs_payload, "captured_head": str(summary.get("captured_head_sha") or ""),
            "github_host": github_host,
        }
        for command in commands:
            parsed = github_pr.shell_argv(command)
            if not parsed:
                problems.append(live_problem("GitHub operation transcript command is malformed", rule="github-command", path=path))
                continue
            problems.extend(github_pr.validate_gh_command(parsed, **validation_context))
        for operation in operations:
            problems.extend(github_pr.validate_connector_operation(operation, **validation_context, require_identity=True))
    return actual_hash

def _github_payload_text(*values: Any) -> str:
    return next((text for value in values if value is not None and (text := str(value).strip())), "")

def _github_payload_ref(payload: Mapping[str, Any], *keys: str) -> str:
    return _github_payload_text(*(payload.get(key) for key in keys))

def validate_github_pr_payload(
    pr_payload: Any,
    summary: Mapping[str, Any],
    transcript_payload: Any,
    problems: list[dict[str, Any]],
    *,
    path: str,
) -> None:
    if not isinstance(pr_payload, Mapping):
        problems.append(live_problem("GitHub PR artifact must be a JSON object", rule="github-live-provenance", path=path))
        return
    try:
        from live_collectors import github_pr
    except Exception as exc:
        problems.append(live_problem(f"GitHub PR validators are unavailable: {exc}", rule="github-live-provenance", path=path))
        return
    expected_repo = _github_payload_text(summary.get("repo"))
    expected_pr = _github_payload_text(summary.get("pr"))
    payload_repo = github_pr.payload_repository_identity(pr_payload)
    payload_pr = _github_payload_text(
        pr_payload.get("number"),
        pr_payload.get("pr"),
        pr_payload.get("pull_request"),
        pr_payload.get("pullRequestNumber"),
    )
    validate_required_match(problems, payload_repo, expected_repo, "GitHub PR artifact requires repository identity", "GitHub PR artifact repository does not match the manifest summary", required=bool(expected_repo), path=path)
    validate_required_match(problems, payload_pr, expected_pr, "GitHub PR artifact requires PR identity", "GitHub PR artifact number does not match the manifest summary", required=bool(expected_pr), path=path)
    present_pr_urls = [(key, text) for key in SECURITY_POLICY["github_pr_url_fields"] if (text := _github_payload_text(pr_payload.get(key)))]
    flag_live_problem(problems, not present_pr_urls, "GitHub PR artifact requires PR URL identity", rule="github-live-provenance", path=path)
    for key, pr_url in present_pr_urls:
        label = f"GitHub PR artifact {key}"
        for message in github_pr.github_url_identity_messages(pr_url, label, require_url=True):
            problems.append(live_problem(message, rule="github-live-provenance", path=path))
        url_repo = github_pr.repo_from_url(pr_url)
        url_pr = github_pr.pr_from_url(pr_url)
        validate_required_match(problems, url_repo, expected_repo, f"{label} must include repository identity", f"{label} repository does not match the manifest summary", required=bool(expected_repo), path=path)
        validate_required_match(problems, url_pr, expected_pr, f"{label} must include PR identity", f"{label} PR does not match the manifest summary", required=bool(expected_pr), path=path)
    pr_refs = {
        "captured_base_sha": github_pr.extract_base_sha(pr_payload), "captured_head_sha": github_pr.extract_head_sha(pr_payload),
        **{field: _github_payload_ref(pr_payload, *aliases) for field, aliases in SECURITY_POLICY["github_ref_aliases"].items()},
    }
    transcript_refs = transcript_payload.get("refs") if isinstance(transcript_payload, Mapping) and isinstance(transcript_payload.get("refs"), Mapping) else {}
    for field, actual in pr_refs.items():
        expected = _github_payload_text(summary.get(field))
        validate_required_match(problems, actual, expected, f"GitHub PR artifact requires {field}", f"GitHub PR artifact {field} does not match the manifest summary", path=path)
        transcript_value = _github_payload_text(transcript_refs.get(field)) if isinstance(transcript_refs, Mapping) else ""
        flag_live_problem(problems, actual and transcript_value and actual != transcript_value, f"GitHub PR artifact {field} does not match the operation transcript", rule="github-live-provenance", path=path)

def validate_source_packet_manifest(project: Path, manifest: dict[str, Any] | None, manifest_path: Path | None, problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    if manifest_path is None:
        return artifacts
    root = manifest_path.parent
    required = SECURITY_POLICY["github_packet_artifacts"]
    summary = live_manifest_summary(manifest)
    check_runs_payload: Any = None
    pr_payload: Any = None
    task = str(manifest.get("task") or "") if isinstance(manifest, dict) else None
    for filename in required:
        require_json = filename.endswith(".json")
        entry, payload = load_security_artifact(project, str(root / filename), filename, problems, artifacts, manifest, rule="github-live-provenance", task=task, collector="github", require_json=require_json)
        if filename == "pr.json" and payload is not None:
            pr_payload = payload
        if filename == "check-runs.json" and payload is not None:
            check_runs_payload = payload
            validate_check_runs(payload, problems, entry.get("path", "") if entry else "", captured_head=str(summary.get("captured_head_sha") or ""))
    logs_path = root / "ci-log-excerpts.json"
    if logs_path.exists() or summary.get("logs_included"):
        entry, log_payload = load_security_artifact(project, str(logs_path), "ci log excerpts", problems, artifacts, manifest, rule="github-live-provenance", task=task, collector="github", require_object=True)
        try:
            from live_collectors import github_pr
        except Exception as exc:
            problems.append(
                live_problem(f"GitHub CI log validators are unavailable: {exc}",
                             rule="github-live-provenance",
                             path=entry.get("path", "") if entry else live_rel(project, logs_path)))
        else:
            problems.extend(
                github_pr.validate_ci_log_excerpt_payload(
                    log_payload,
                    repo=str(summary.get("repo") or ""),
                    pr_number=str(summary.get("pr") or ""),
                    captured_head=str(summary.get("captured_head_sha") or ""),
                    check_runs=check_runs_payload,
                    path=entry.get("path", "") if entry else live_rel(project, logs_path),
                ))
    transcript_entry, transcript_payload = validate_artifact_arg(
        project,
        str(root / "operation-transcript.json"),
        "operation transcript",
        problems,
        task=task,
        collector="github",
        require_scoped=True,
        require_json=True,
        require_object=True,
    )
    if transcript_entry:
        artifacts.append(transcript_entry)
    transcript_actual_hash = ""
    transcript_path = root / "operation-transcript.json"
    if transcript_entry and transcript_entry.get("exists"):
        transcript_actual_hash = validate_github_operation_transcript(
            project,
            manifest,
            transcript_path,
            transcript_payload,
            summary,
            problems,
            path=transcript_entry.get("path", ""),
            attested_entry=transcript_entry,
            check_runs_payload=check_runs_payload,
            pr_payload=pr_payload,
        )
        validate_github_pr_payload(
            pr_payload,
            summary,
            transcript_payload,
            problems,
            path=next((str(item.get("path") or "") for item in artifacts if str(item.get("path") or "").endswith("/pr.json")), live_rel(project, root / "pr.json")),
        )
    else:
        problems.append(
            live_problem("GitHub PR source packet requires a scoped operation transcript artifact", rule="github-live-provenance", path=live_rel(project, transcript_path)))
    for left, right, label in (
        ("captured_base_sha", "current_base_sha", "base SHA"),
        ("captured_head_sha", "current_head_sha", "head SHA"),
    ):
        flag_live_problem(problems, summary.get(left) and summary.get(right) and summary.get(left) != summary.get(right), f"GitHub PR {label} changed after capture", rule="github-freshness")
    flag_live_problem(problems, summary.get("missing_refs"), "GitHub PR evidence reports missing refs", rule="github-refs")
    flag_live_problem(problems, summary.get("partial_permissions"), "GitHub PR evidence reports partial permissions", rule="github-permissions")
    flag_live_problem(problems, summary.get("pagination_incomplete"), "GitHub PR evidence reports incomplete pagination", rule="github-pagination")
    tool_versions = manifest.get("tool_versions") if isinstance(manifest, dict) else {}
    source_markers = {
        str(summary.get("source") or ""),
        str(tool_versions.get("source") or "") if isinstance(tool_versions, dict) else "",
    }
    flag_live_problem(problems, any(github_fixture_marker(item) for item in source_markers), "GitHub PR fixture evidence is not production-review proof", rule="github-fixture-provenance")
    normalized_sources = {item for item in source_markers if item}
    flag_live_problem(problems, not normalized_sources or not any(github_live_source_marker(item) for item in normalized_sources), "GitHub PR source packet requires positive live GitHub provenance", rule="github-live-provenance")
    flag_live_problem(problems, not isinstance(tool_versions, dict) or not tool_versions or any(github_fixture_marker(key) or github_fixture_marker(value) for key, value in tool_versions.items()), "GitHub PR source packet requires collector tool versions", rule="github-live-provenance")
    provenance = summary.get("live_provenance") or summary.get("github_provenance")
    if not isinstance(provenance, dict):
        problems.append(live_problem("GitHub PR source packet requires live provenance details", rule="github-live-provenance"))
        provenance = {}
    summary_repo = str(summary.get("repo") or "").strip()
    summary_pr = str(summary.get("pr") or "").strip()
    provenance_repo = str(provenance.get("repo") or provenance.get("repository") or "").strip()
    provenance_pr = str(provenance.get("pr") or provenance.get("pull_request") or provenance.get("number") or "").strip()
    flag_live_problem(problems, not summary_repo, "GitHub PR source packet requires repository identity", rule="github-live-provenance")
    flag_live_problem(problems, not summary_pr, "GitHub PR source packet requires PR identity", rule="github-live-provenance")
    validate_required_match(problems, provenance_repo, summary_repo, "GitHub PR source packet requires provenance repository identity", "GitHub PR source packet provenance repository does not match the summary")
    validate_required_match(problems, provenance_pr, summary_pr, "GitHub PR source packet requires provenance PR identity", "GitHub PR source packet provenance PR does not match the summary")
    flag_live_problem(problems, not str(provenance.get("collected_at") or provenance.get("captured_at") or summary.get("captured_at") or "").strip(), "GitHub PR source packet requires a collection timestamp", rule="github-live-provenance")
    freshness_fields = SECURITY_POLICY["github_freshness_fields"]
    missing_freshness = [field for field in freshness_fields if not str(summary.get(field) or provenance.get(field) or "").strip()]
    flag_live_problem(problems, missing_freshness, "GitHub PR source packet is missing freshness refs: " + ", ".join(missing_freshness), rule="github-live-provenance")
    read_only_commands = summary.get("read_only_commands") if isinstance(summary.get("read_only_commands"), list) else []
    read_only_operations = summary.get("read_only_operations") if isinstance(summary.get("read_only_operations"), list) else []
    provenance_transcript_hash = str(provenance.get("operation_transcript_sha256") or provenance.get("read_only_transcript_sha256") or "")
    summary_transcript_hash = str(summary.get("read_only_transcript_sha256") or "")
    claimed_transcript_hash = provenance_transcript_hash or summary_transcript_hash
    flag_live_problem(problems, not valid_sha256(claimed_transcript_hash), "GitHub PR source packet requires a hashed read-only operation transcript", rule="github-live-provenance")
    flag_live_problem(problems, transcript_actual_hash and summary_transcript_hash != transcript_actual_hash, "GitHub operation transcript hash does not match the manifest summary", rule="github-live-provenance")
    flag_live_problem(problems, transcript_actual_hash and provenance_transcript_hash != transcript_actual_hash, "GitHub operation transcript hash does not match live provenance", rule="github-live-provenance")
    flag_live_problem(problems, not read_only_commands and not read_only_operations and not provenance.get("read_only_operations"), "GitHub PR source packet requires read-only GitHub operations", rule="github-live-provenance")
    return artifacts

def validate_check_runs(payload: Any, problems: list[dict[str, Any]], path: str, *, captured_head: str = "") -> None:
    completed_statuses = SECURITY_POLICY["check_completed_statuses"]
    successful_conclusions = SECURITY_POLICY["check_successful_conclusions"]
    pending_statuses = SECURITY_POLICY["check_pending_statuses"]
    if isinstance(payload, dict):
        flag_live_problem(problems, payload.get("partial_permissions"), "check runs are permission-partial", rule="github-checks", path=path)
        flag_live_problem(problems, payload.get("pagination_incomplete"), "check runs pagination is incomplete", rule="github-checks", path=path)
        raw = payload.get("check_runs") or payload.get("checks") or payload.get("runs")
    else:
        raw = payload
    if not isinstance(raw, list) or not raw:
        problems.append(live_problem("check runs must contain at least one check", rule="github-checks", path=path))
        return
    for idx, check in enumerate(raw):
        if not isinstance(check, dict):
            problems.append(live_problem(f"check run {idx + 1} is malformed", rule="github-checks", path=path))
            continue
        status = str(check.get("status") or "").lower()
        conclusion = str(check.get("conclusion") or "").lower()
        status_message = f"check run {idx + 1} is missing status" if not status else f"check run {idx + 1} is pending: {status}" if status in pending_statuses else f"check run {idx + 1} is not complete: {status}" if status not in completed_statuses else ""
        conclusion_message = f"check run {idx + 1} is missing conclusion" if not conclusion else f"check run {idx + 1} conclusion is {conclusion}" if conclusion not in successful_conclusions else ""
        flag_live_problem(problems, status_message, status_message, rule="github-checks", path=path)
        flag_live_problem(problems, conclusion_message, conclusion_message, rule="github-checks", path=path)
        commit = check.get("commit")
        commit_sha = str(commit.get("sha") or "") if isinstance(commit, dict) else ""
        run_head = str(check.get("head_sha") or check.get("headSha") or commit_sha)
        validate_required_match(
            problems, run_head, captured_head, f"check run {idx + 1} is missing head SHA binding",
            f"check run {idx + 1} is bound to a different head SHA", required=bool(captured_head), rule="github-checks", path=path,
        )

def cmd_source_packet_proof(args: argparse.Namespace) -> int:
    project, problems = security_command_state(args)
    profile = str(args.profile or "").strip()
    github_source_profiles = SECURITY_POLICY["github_source_profiles"]
    collector = collector_for_profile(profile) if profile in github_source_profiles else None
    manifest, manifest_path = load_and_validate_live_manifest(project, args.input, problems, task=args.task, collector=collector, require_scoped=collector is not None)
    if not profile:
        problems.append(live_problem(SECURITY_POLICY["source_packet_messages"]["missing_profile"], rule="source-profile"))
    elif profile not in github_source_profiles:
        problems.append(live_problem(SECURITY_POLICY["source_packet_messages"]["unsupported_profile"].format(profile=profile), rule="source-profile"))
    artifacts = validate_source_packet_manifest(project, manifest, manifest_path, problems) if profile in github_source_profiles else []
    return finish_security_command(project, args, "source-packet-proof", args.task, problems, manifest, manifest_path, artifacts, f"profile={args.profile}")

def cmd_source_packet_github_pr_review(args: argparse.Namespace) -> int:
    project, problems = security_command_state(args)
    manifest, manifest_path = load_and_validate_live_manifest(project, args.input, problems, collector="github", require_scoped=False)
    task = str(manifest.get("task") or "") if isinstance(manifest, dict) else None
    if manifest_path is not None:
        if not is_task_scoped_live_path(project, manifest_path, task, "github"):
            problems.append(live_problem(SECURITY_POLICY["source_packet_messages"]["manifest_scope"], rule="manifest-scope", path=live_rel(project, manifest_path)))
        if isinstance(manifest, dict):
            validate_manifest_artifact_scopes(project, manifest, problems, task=task, collector="github", require_scoped=True)
            validate_raw_artifact_hashes(project, manifest, problems, task=task, collector="github", require_scoped=True)
    artifacts = validate_source_packet_manifest(project, manifest, manifest_path, problems)
    return finish_security_command(project, args, "source-packet-github-pr-review", task, problems, manifest, manifest_path, artifacts, "GitHub PR source packet proof")

__all__ = tuple(name for name in globals() if not name.startswith("__"))
