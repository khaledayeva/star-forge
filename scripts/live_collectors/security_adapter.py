#!/usr/bin/env python3
"""Normalize trusted security scanner reports for Star Forge handoff.

The adapter imports scanner output only. It never runs a scanner, approves work,
or decides completion. Generated artifacts are handed to the existing
security-handoff-packet and security-proof commands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
STAR_FORGE = SCRIPTS_DIR / "star_forge.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from live_collectors import common as live_common
from live_collectors.policy_data import policy_dict, policy_list, policy_set, policy_tuple
from live_collectors.provider_engine import (
    candidate_text as first_text,
    first_candidate as first_value,
    normalize_alias, nested_value,
    render_descriptor,
)
from starforge import evidence


globals().update(policy_dict("security_adapter", "CONSTANTS"))
for _name in ("CODEX_SECURITY_SCHEMAS", "VALID_PROFILES", "PATH_KEYS", "SENSITIVE_TEXT_KEYS"):
    globals()[_name] = policy_set("security_adapter", _name)
for _name in (
    "KIND_BY_PROFILE", "CANDIDATES", "SEVERITY_NORMALIZATION", "CONFIDENCE_NORMALIZATION",
    "FINGERPRINT_TEMPLATE", "PAYLOAD_TEMPLATES", "COMMAND_TEMPLATES", "EVIDENCE_ROUTES",
    "ARTIFACT_FILES", "EVIDENCE_PROVENANCE_TEMPLATE", "FALLBACK_BLOCKER", "REDACTION_COUNTS",
):
    globals()[_name] = policy_dict("security_adapter", _name)
KNOWN_SEVERITIES = {"critical", "high", "medium", "low", "info"}
EVIDENCE_KEYS = policy_tuple("security_adapter", "EVIDENCE_KEYS")
REQUIRED_METADATA = policy_list("security_adapter", "REQUIRED_METADATA")
SOURCE_BINDING_MESSAGES = policy_tuple("security_adapter", "SOURCE_BINDING_MESSAGES")
PARSER_OPTIONS = policy_list("security_adapter", "PARSER_OPTIONS")


write_json = lambda path, payload: live_common.write_json(path, payload, redact=False)[0]


for _name in (
    "read_json", "file_sha256", "merge_reports", "git_head", "git_status_path",
    "source_dirty_entries", "source_snapshot_rel_paths",
    "dirty_paths_missing_from_source_snapshot", "source_tree_clean_at_head",
):
    globals()[_name] = getattr(live_common, _name)


stable_hash = lambda payload: hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


add_problem = lambda problems, message, *, rule, path="", severity="high": problems.append(live_common.blocking_problem(message, rule=rule, path=path, severity=severity))


normalize_severity = lambda raw: normalize_alias(raw, SEVERITY_NORMALIZATION)


normalize_confidence = lambda raw: normalize_alias(raw, CONFIDENCE_NORMALIZATION)[0]


def sanitize_path_text(project: Path, raw: str) -> str:
    text = str(raw)
    return live_common.project_relative(project, Path(text)) if text.startswith("/") else text


def redact_and_sanitize(value: Any, project: Path, report: dict[str, Any], key_hint: str = "") -> Any:
    key_norm = re.sub(r"[^a-z0-9_]+", "", key_hint.lower())
    if isinstance(value, Mapping):
        return {str(key): redact_and_sanitize(item, project, report, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_and_sanitize(item, project, report, key_hint) for item in value]
    if not isinstance(value, str):
        return value
    text = sanitize_path_text(project, value) if key_norm in PATH_KEYS else value
    return text.replace(str(project.resolve()), ".") if key_norm in SENSITIVE_TEXT_KEYS else text


def redact_payload(payload: Any, project: Path) -> tuple[Any, dict[str, Any]]:
    report = dict(REDACTION_COUNTS)
    cleaned = redact_and_sanitize(payload, project, report)
    cleaned, final = live_common.redact_sensitive_values(cleaned)
    return cleaned, merge_reports(report, final)


def detect_schema(payload: Any) -> tuple[str, str, bool]:
    if not isinstance(payload, Mapping):
        return "", "unsupported", False
    schema = str(payload.get("schema") or payload.get("report_schema") or payload.get("format") or "").strip()
    family = "star-forge" if schema == STAR_FORGE_REPORT_SCHEMA else (
        "codex-security" if schema in CODEX_SECURITY_SCHEMAS else ""
    )
    if family:
        return schema, family, True
    codex = (
        str(payload.get("report_type") or payload.get("type") or "").strip().lower()
        in {"codex-security", "codex_security"}
        and first_text(payload, CANDIDATES["scanner"]).lower() in {"codex-security", "codex security"}
    )
    return (schema or "codex-security.report.v1", "codex-security", True) if codex else (
        schema, "unsupported", False
    )


extract_findings = lambda payload: (lambda raw: [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else [])(first_value(payload, ["findings", "results", "report.findings"]) if isinstance(payload, Mapping) else [])


_metadata_value = lambda payload, args, field: getattr(args, field, "") or first_value(payload, CANDIDATES[field])


scanner_name = lambda payload, args, schema_family: str(_metadata_value(payload, args, "scanner") or ("codex-security" if schema_family == "codex-security" else "")).strip()


scanner_version = lambda payload, args: str(_metadata_value(payload, args, "scanner_version") or "").strip()


def ruleset_metadata(payload: Any, args: argparse.Namespace) -> Any:
    override = {"name": args.ruleset, "version": args.ruleset_version}
    return {key: value for key, value in override.items() if value} or _metadata_value(
        payload, args, "ruleset")


scan_scope = lambda payload, args: _metadata_value(payload, args, "scan_scope")


source_binding = lambda payload, args: {
    field: value for field in ("source_hash", "commit_sha", "base_sha", "head_sha")
    if (value := getattr(args, field) or first_text(payload, CANDIDATES[field]))}


def validate_source_binding(project: Path, binding: Mapping[str, Any], problems: list[dict[str, Any]]) -> bool:
    source_hash = str(binding.get("source_hash") or "")
    commit_sha = str(binding.get("commit_sha") or "")
    source_matches = bool(source_hash and source_hash == live_common.compute_source_hash(project))
    missing_dirty = dirty_paths_missing_from_source_snapshot(project) if source_matches else []
    if missing_dirty:
        add_problem(
            problems, "security report source_hash does not cover dirty source paths: "
            + ", ".join(git_status_path(item) for item in missing_dirty[:5]),
            rule="security-source-binding",
        )
    head = git_head(project) if commit_sha else ""
    clean = source_tree_clean_at_head(project) if commit_sha else False
    failed = (
        bool(commit_sha and not head),
        bool(commit_sha and head and commit_sha == head and not clean),
        bool(source_hash and not source_matches),
        bool(commit_sha and head and commit_sha != head),
        not source_hash and not commit_sha,
    )
    problems.extend(
        live_common.blocking_problem(message, rule="security-source-binding")
        for condition, message in zip(failed, SOURCE_BINDING_MESSAGES) if condition
    )
    return source_matches and not missing_dirty or bool(head and commit_sha == head and clean)


declared_input_hash = lambda payload, args: (args.input_hash or first_text(payload, CANDIDATES["input_hash"])).strip().lower()


def dependency_manifest_records(project: Path, payload: Any, args: argparse.Namespace, problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_items: list[Any] = list(args.dependency_manifest or [])
    report_items = first_value(payload, CANDIDATES["dependency_manifests"])
    raw_items += report_items if isinstance(report_items, list) else [report_items] if report_items else []
    records, seen = [], set()
    for raw in raw_items:
        path_text = str(raw.get("path") if isinstance(raw, Mapping) else raw)
        if not path_text:
            continue
        try:
            path = live_common.safe_project_path(project, path_text, must_exist=True)
        except ValueError as exc:
            add_problem(problems, f"dependency manifest path is unsafe or missing: {exc}", rule="security-dependency-manifest", path=path_text)
            continue
        rel = live_common.project_relative(project, path)
        if rel not in seen:
            seen.add(rel)
            records.append({"path": rel, "sha256": file_sha256(path), "bytes": path.stat().st_size})
    return records


finding_path = lambda raw: str(first_value(value, ["path", "file"]) if isinstance(
    (value := first_value(raw, CANDIDATES["finding_path"])), Mapping) else value or "")
finding_line = lambda raw: first_value(raw, CANDIDATES["finding_line"])
dotted_get = lambda payload, dotted: nested_value(payload, *dotted.split("."))


def build_evidence(raw: Mapping[str, Any]) -> dict[str, Any]:
    evidence = raw.get("evidence")
    out = dict(evidence) if isinstance(evidence, Mapping) else {"value": evidence} if evidence not in (None, "") else {}
    out |= {
        key: raw[key] for key in EVIDENCE_KEYS
        if key in raw and key not in out
    }
    location = raw.get("location") or raw.get("primary_location")
    if isinstance(location, Mapping):
        out.setdefault("location", dict(location))
    return out


def normalized_finding(
    project: Path,
    raw: Mapping[str, Any],
    *,
    index: int,
    scanner: str,
    version: str,
    source_schema: str,
    redaction_totals: dict[str, Any],
) -> dict[str, Any]:
    raw_id = first_text(raw, CANDIDATES["finding_id"]) or f"finding-{index + 1}"
    rule_id = first_text(raw, CANDIDATES["rule_id"]) or "unknown-rule"
    raw_severity = first_value(raw, CANDIDATES["severity"])
    severity, known = normalize_severity(raw_severity)
    raw_confidence = first_value(raw, CANDIDATES["confidence"])
    confidence = normalize_confidence(raw_confidence)
    redacted: dict[str, Any] = {}
    for field, value in (
        ("evidence", build_evidence(raw)),
        ("remediation", first_value(raw, CANDIDATES["remediation"]) or ""),
        ("title", first_text(raw, CANDIDATES["title"])),
        ("message", first_text(raw, CANDIDATES["message"])),
    ):
        redacted[field], report = redact_payload(value, project)
        redaction_totals.update(merge_reports(redaction_totals, report))
    raw_path = finding_path(raw)
    path = sanitize_path_text(project, raw_path) if raw_path else ""
    line = first_value(raw, CANDIDATES["finding_line"])
    fingerprint_basis = render_descriptor(
        FINGERPRINT_TEMPLATE, scanner=scanner, rule_id=rule_id, raw_id=raw_id,
        path=path, line=line, severity=severity,
        message=redacted["message"] or redacted["title"],
    )
    values = {
        "raw_id": raw_id, "scanner": scanner, "version": version, "rule_id": rule_id,
        "raw_severity_output": raw_severity if raw_severity is not None else "",
        "severity": severity, "severity_known": known, "confidence": confidence,
        "raw_confidence_output": raw_confidence if raw_confidence is not None else "",
        **redacted, "fingerprint": "sfsec-" + stable_hash(fingerprint_basis)[:32],
        "source_schema": source_schema,
    }
    normalized: dict[str, Any] = render_descriptor(PAYLOAD_TEMPLATES["finding"], values)
    normalized.update({
        key: value for key, value in (("path", path), ("line", line))
        if value not in (None, "")
    })
    return normalized


def summarize_findings(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    severities = [str(item.get("severity") or "unknown") for item in findings]
    return {
        "finding_count": len(findings),
        "severity_counts": dict(Counter(severities)),
        "max_severity": max(severities, key=severity_rank, default="info"),
        "blocking_finding_count": sum(item in live_common.BLOCKING_SEVERITIES for item in severities),
        "unknown_severity_count": sum(item not in KNOWN_SEVERITIES for item in severities),
    }


def validate_required_metadata(
    *, problems: list[dict[str, Any]], **values: Any,
) -> None:
    missing = (None, "", {}, [])
    problems.extend(
        live_common.blocking_problem(item["message"], rule=item["rule"])
        for item in REQUIRED_METADATA
        if (values[item["field"]] in missing if item["field"] in {"ruleset", "scope"}
            else not values[item["field"]])
    )


def build_command_argvs(project_arg: str, task: str, profile: str, kind: str, scanner: str, version: str, root: Path, project: Path) -> dict[str, list[str]]:
    values = {
        "project": project_arg, "task": task, "profile": profile, "kind": kind,
        "scanner": scanner, "version": version,
        "handoff": live_common.project_relative(project, root / "handoff-input.json"),
        "findings": live_common.project_relative(project, root / "normalized-findings.json"),
        "manifest": live_common.project_relative(project, root / "manifest.json"),
    }
    return render_descriptor(COMMAND_TEMPLATES, values)


display_command = lambda command: shlex.join(list(map(str, command)))
build_commands = lambda *args: {name: display_command(command) for name, command in build_command_argvs(*args).items()}
trusted_record_command = lambda command: live_common.trusted_python_command(command, script_path=STAR_FORGE)
severity_rank = lambda severity: {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1, "unknown": 6}.get(severity, 0)


maybe_record = lambda commands, cwd: {name: live_common.run_trusted_command(
    command, cwd=cwd, script_path=STAR_FORGE) for name, command in commands.items()}


security_provider = lambda schema_family: str(EVIDENCE_ROUTES["codex-security" if schema_family == "codex-security" else "fallback"]["provider"])


def write_evidence_envelope(
    project: Path,
    manifest_path: Path,
    *,
    schema_family: str,
    scanner: str,
    scanner_version_value: str,
    source_schema: str,
    source_binding_value: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Adapt the v1 manifest and record the selected security capability route."""

    route = dict(EVIDENCE_ROUTES[
        "codex-security" if schema_family == "codex-security" else "fallback"])
    provider = str(route.pop("provider"))
    envelope = evidence.adapt_v1_manifest(
        read_json(manifest_path), capability=CAPABILITY, provider=provider,
    )
    values = {
        "route": {**route, "selected_provider": provider},
        "provider": provider, "scanner": scanner, "version": scanner_version_value,
        "source_schema": source_schema, "schema_family": schema_family,
        "source_binding": dict(source_binding_value),
    }
    provenance = {**dict(envelope["provenance"]), **render_descriptor(
        EVIDENCE_PROVENANCE_TEMPLATE, values)}
    envelope["provenance"], _ = redact_payload(provenance, project)
    if route["fallback"]:
        envelope["blockers"].append(render_descriptor(FALLBACK_BLOCKER, provider=provider))
        envelope["verdict"] = "DEGRADED" if envelope["verdict"] == "PASS" else envelope["verdict"]
    envelope_path = manifest_path.parent / EVIDENCE_FILENAME
    return envelope_path, evidence.write_envelope(
        envelope_path, envelope, project_root=project, verify_artifacts=True)


def adapt(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    project = Path(args.project).resolve()
    root = live_common.live_collector_dir(project, args.task, "security")
    problems: list[dict[str, Any]] = []
    command_argv = ["security_adapter.py", *getattr(args, "_command_argv", sys.argv[1:])]
    if args.profile not in VALID_PROFILES:
        add_problem(problems, f"security profile is invalid: {args.profile}", rule="security-profile")
    kind = KIND_BY_PROFILE.get(args.profile, args.profile or "security")
    payload: Any = {}
    try:
        input_path = live_common.safe_project_path(project, args.input, must_exist=True)
        payload = read_json(input_path)
    except ValueError as exc:
        input_path = project / "__missing_security_input__.json"
        add_problem(problems, f"security input path is unsafe or missing: {exc}", rule="security-input")
        add_problem(problems, "security input does not exist", rule="security-input")
    except Exception as exc:
        add_problem(
            problems, f"security input is malformed JSON: {exc}",
            rule="security-input-json", path=live_common.project_relative(project, input_path),
        )
    source_schema, schema_family, trusted_schema = detect_schema(payload)
    scanner, version = scanner_name(payload, args, schema_family), scanner_version(payload, args)
    ruleset, scope = ruleset_metadata(payload, args), scan_scope(payload, args)
    binding = source_binding(payload, args)
    source_binding_ok = validate_source_binding(project, binding, problems)
    actual_input_hash = file_sha256(input_path) if input_path.exists() else ""
    declared_hash = declared_input_hash(payload, args)
    input_hash_ok = bool(declared_hash and actual_input_hash and declared_hash.lower() == actual_input_hash.lower())
    dependency_manifests = dependency_manifest_records(project, payload, args, problems)
    validate_required_metadata(
        trusted_schema=trusted_schema, scanner=scanner, version=version,
        ruleset=ruleset, scope=scope, input_hash_ok=input_hash_ok,
        source_binding_ok=source_binding_ok, problems=problems,
    )
    redaction_totals: dict[str, Any] = dict(REDACTION_COUNTS)
    normalized = [
        normalized_finding(
            project, raw, index=index, scanner=scanner, version=version,
            source_schema=source_schema, redaction_totals=redaction_totals,
        )
        for index, raw in enumerate(extract_findings(payload))
    ] if trusted_schema else []
    problems.extend(
        live_common.blocking_problem(
            f"security finding {finding['id']} has unknown severity",
            rule="security-severity",
        )
        for finding in normalized if not finding.get("severity_known")
    )
    findings_summary = summarize_findings(normalized)
    values: dict[str, Any] = {
        **vars(args), **locals(), "version": version, "findings": normalized,
        "input_path": live_common.project_relative(project, input_path) if input_path.exists() else "",
        "report_provenance": payload.get("provenance") if isinstance(payload, Mapping) else {},
    }
    def clean(value: Any) -> Any:
        nonlocal redaction_totals
        cleaned, report = redact_payload(value, project)
        redaction_totals = merge_reports(redaction_totals, report)
        return cleaned

    normalized_payload = clean(render_descriptor(PAYLOAD_TEMPLATES["normalized_findings"], values))
    findings_path = write_json(root / ARTIFACT_FILES["normalized_findings"], normalized_payload)
    input_hash_payload = render_descriptor(PAYLOAD_TEMPLATES["input_hash"], values)
    input_hash_path = write_json(root / ARTIFACT_FILES["input_hash"], input_hash_payload)
    provenance = clean(render_descriptor(PAYLOAD_TEMPLATES["provenance"], values))
    values.update({
        "provenance": provenance, "input_hash_payload": input_hash_payload,
        "normalized_findings_record": {
            "path": live_common.project_relative(project, findings_path),
            "sha256": file_sha256(findings_path), **findings_summary,
        },
    })
    handoff_payload = clean(render_descriptor(PAYLOAD_TEMPLATES["handoff"], values))
    handoff_path = write_json(root / ARTIFACT_FILES["handoff_input"], handoff_payload)
    values["redaction_totals"] = redaction_totals
    redaction_path = write_json(
        root / ARTIFACT_FILES["redaction_report"],
        render_descriptor(PAYLOAD_TEMPLATES["redaction_report"], values),
    )
    artifacts: dict[str, Path] = {
        "normalized-findings": findings_path,
        "handoff-input": handoff_path,
        "input-hash": input_hash_path,
        "redaction-report": redaction_path,
    }
    for index, record in enumerate(dependency_manifests):
        artifacts[f"dependency-manifest-{index + 1}"] = project / record["path"]

    values.update({
        "trusted_provenance": bool(trusted_schema and scanner and version),
        "summary_ruleset": ruleset if ruleset not in (None, "", {}, []) else "",
        "summary_scope": scope if scope not in (None, "", {}, []) else "",
        "summary_input_hash": declared_hash if input_hash_ok else "",
        "fresh_binding": {**binding, "fresh": source_binding_ok},
    })
    summary = render_descriptor(PAYLOAD_TEMPLATES["summary"], values)
    summary.update(findings_summary)
    safe_command_argv = clean(command_argv)
    manifest_path = live_common.write_live_manifest(
        project, task=args.task, collector="security", command_argv=safe_command_argv,
        tool_versions={scanner or "security-adapter": version or "unknown"},
        artifacts=artifacts, summary=summary, degraded=bool(problems), problems=problems,
    )
    envelope_path, envelope = write_evidence_envelope(
        project, manifest_path, schema_family=schema_family, scanner=scanner,
        scanner_version_value=version,
        source_schema=source_schema, source_binding_value={**binding, "fresh": source_binding_ok},
    )
    project_arg = live_common.project_cli_arg(project)
    command_argvs = build_command_argvs(project_arg, args.task, args.profile, kind, scanner, version, root, project)
    commands = {name: display_command(command) for name, command in command_argvs.items()}
    record_results = maybe_record(command_argvs, project) if args.record else {}
    record_failures = {
        name: record for name, record in record_results.items()
        if int(record.get("returncode") or 0) != 0
    }
    problems.extend(
        live_common.blocking_problem(
            f"security record command {name} failed with exit code {record.get('returncode')}",
            rule="security-record",
        )
        for name, record in record_failures.items()
    )
    record_failed = bool(record_failures)
    if record_failed:
        envelope["blockers"].extend(
            dict(item) for item in problems if item.get("rule") == "security-record"
        )
        envelope["verdict"] = "FAIL"
        evidence.write_envelope(
            envelope_path, envelope, project_root=project, verify_artifacts=True,
        )
    result_artifacts = {
        name.replace("-", "_"): live_common.project_relative(project, path)
        for name, path in artifacts.items() if not name.startswith("dependency-")
    } | {"manifest": live_common.project_relative(project, manifest_path)}
    values.update({**locals(),
        "verdict": "FAIL" if problems else "PASS", "result_artifacts": result_artifacts,
        "evidence_path": live_common.project_relative(project, envelope_path),
        "evidence_schema": envelope["schema"], "evidence_verdict": envelope["verdict"],
        "provider": security_provider(schema_family), "commands": commands,
    })
    result = render_descriptor(PAYLOAD_TEMPLATES["result"], values)
    if record_results:
        result["record_results"] = record_results
    return (1 if record_failed or (args.strict and problems) else 0), result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize trusted security scanner reports for Star Forge")
    for option in PARSER_OPTIONS:
        kwargs = {key: value for key, value in option.items() if key != "name"}
        kwargs.update(choices=sorted(VALID_PROFILES)) if kwargs.get("choices") == "profiles" else None
        parser.add_argument(option["name"], **kwargs)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(raw_argv)
    args._command_argv = raw_argv
    code, result = adapt(args)
    print(json.dumps(result, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
