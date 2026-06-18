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
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
STAR_FORGE = SCRIPTS_DIR / "star_forge.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from live_collectors import common as live_common


STAR_FORGE_REPORT_SCHEMA = "star-forge.security-report.v1"
CODEX_SECURITY_SCHEMAS = {
    "codex.security.report.v1",
    "codex-security.report.v1",
    "codex.security.v1",
}
NORMALIZED_FINDINGS_SCHEMA = "star-forge.normalized-security-findings.v1"
HANDOFF_INPUT_SCHEMA = "star-forge.security-handoff-input.v1"
INPUT_HASH_SCHEMA = "star-forge.security-input-hash.v1"
RESULT_SCHEMA = "star-forge.security-adapter-result.v1"
VALID_PROFILES = {"dependency-audit", "security-deep", "security-diff", "vulnerability-fix"}
KIND_BY_PROFILE = {
    "dependency-audit": "dependency-audit",
    "security-deep": "security-deep",
    "security-diff": "security-diff",
    "vulnerability-fix": "vulnerability-fix",
}
KNOWN_SEVERITIES = {"critical", "high", "medium", "low", "info"}
PATH_KEYS = {"path", "file", "filepath", "file_path", "filename", "uri", "source", "target"}
SENSITIVE_TEXT_KEYS = {"snippet", "code", "line_text", "context", "evidence", "message", "description"}


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    return live_common.file_sha256(path)


def stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def merge_reports(*reports: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for report in reports:
        for key, value in report.items():
            if isinstance(value, int):
                merged[key] = int(merged.get(key, 0)) + value
            else:
                merged[key] = value
    return merged


def add_problem(problems: list[dict[str, Any]], message: str, *, rule: str, path: str = "", severity: str = "high") -> None:
    problems.append(live_common.blocking_problem(message, rule=rule, path=path, severity=severity))


def dotted_get(payload: Any, dotted: str) -> Any:
    current = payload
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def first_value(payload: Any, paths: Sequence[str]) -> Any:
    for path in paths:
        value = dotted_get(payload, path) if "." in path else payload.get(path) if isinstance(payload, Mapping) else None
        if value not in (None, ""):
            return value
    return None


def first_text(payload: Any, paths: Sequence[str]) -> str:
    value = first_value(payload, paths)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value).strip()


def normalize_severity(raw: Any) -> tuple[str, bool]:
    if raw is None or raw == "":
        return "unknown", False
    if isinstance(raw, (int, float)):
        score = float(raw)
        if score >= 9.0:
            return "critical", True
        if score >= 7.0:
            return "high", True
        if score >= 4.0:
            return "medium", True
        if score > 0:
            return "low", True
        return "info", True
    text = str(raw).strip().lower().replace("_", "-")
    text = re.sub(r"\s+", "-", text)
    mapping = {
        "blocker": "critical",
        "severe": "critical",
        "critical": "critical",
        "crit": "critical",
        "high": "high",
        "error": "high",
        "major": "high",
        "medium": "medium",
        "moderate": "medium",
        "warning": "medium",
        "warn": "medium",
        "low": "low",
        "minor": "low",
        "info": "info",
        "informational": "info",
        "notice": "info",
        "note": "info",
        "none": "info",
        "negligible": "info",
    }
    if text in mapping:
        return mapping[text], True
    cvss = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)
    if cvss:
        return normalize_severity(float(cvss.group(1)))
    return "unknown", False


def normalize_confidence(raw: Any) -> str:
    if raw is None or raw == "":
        return "unknown"
    if isinstance(raw, (int, float)):
        score = float(raw)
        if score >= 0.8:
            return "high"
        if score >= 0.4:
            return "medium"
        return "low"
    text = str(raw).strip().lower().replace("_", "-")
    mapping = {
        "certain": "high",
        "confirmed": "high",
        "high": "high",
        "likely": "high",
        "medium": "medium",
        "moderate": "medium",
        "possible": "medium",
        "low": "low",
        "tentative": "low",
        "unknown": "unknown",
    }
    return mapping.get(text, "unknown")


def sanitize_path_text(project: Path, raw: str) -> str:
    text = str(raw)
    if not text:
        return ""
    project_root = str(project.resolve())
    if text == project_root:
        return "."
    if text.startswith(project_root + "/"):
        try:
            return str(Path(text).resolve().relative_to(project.resolve()))
        except (OSError, ValueError):
            return text.replace(project_root, ".")
    if text.startswith("/"):
        return live_common.sanitize_external_path(Path(text))
    return text


def redact_and_sanitize(value: Any, project: Path, report: dict[str, Any], key_hint: str = "") -> Any:
    key_norm = re.sub(r"[^a-z0-9_]+", "", key_hint.lower())
    if isinstance(value, Mapping):
        return {str(key): redact_and_sanitize(item, project, report, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_and_sanitize(item, project, report, key_hint) for item in value]
    if isinstance(value, str):
        text = sanitize_path_text(project, value) if key_norm in PATH_KEYS else value
        if key_norm in SENSITIVE_TEXT_KEYS:
            text = text.replace(str(project.resolve()), ".")
        cleaned, local_report = live_common.redact_sensitive_values(text)
        report.update(merge_reports(report, local_report))
        return cleaned
    return value


def redact_payload(payload: Any, project: Path) -> tuple[Any, dict[str, Any]]:
    report: dict[str, Any] = {"secret_values": 0, "sensitive_keys": 0, "home_paths": 0, "temp_paths": 0, "env_values": 0}
    cleaned = redact_and_sanitize(payload, project, report)
    cleaned, final_report = live_common.redact_sensitive_values(cleaned)
    return cleaned, merge_reports(report, final_report)


def detect_schema(payload: Any) -> tuple[str, str, bool]:
    if not isinstance(payload, Mapping):
        return "", "unsupported", False
    schema = str(payload.get("schema") or payload.get("report_schema") or payload.get("format") or "").strip()
    if schema == STAR_FORGE_REPORT_SCHEMA:
        return schema, "star-forge", True
    if schema in CODEX_SECURITY_SCHEMAS:
        return schema, "codex-security", True
    report_type = str(payload.get("report_type") or payload.get("type") or "").strip().lower()
    scanner = first_text(payload, ["scanner.name", "scanner", "tool.name", "tool", "provenance.scanner", "provenance.tool"])
    if report_type in {"codex-security", "codex_security"} and scanner.lower() in {"codex-security", "codex security"}:
        return schema or "codex-security.report.v1", "codex-security", True
    return schema, "unsupported", False


def extract_findings(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    raw = payload.get("findings")
    if raw is None:
        raw = payload.get("results")
    if raw is None:
        raw = dotted_get(payload, "report.findings")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def scanner_name(payload: Any, args: argparse.Namespace, schema_family: str) -> str:
    return (
        args.scanner
        or first_text(payload, ["scanner.name", "scanner", "tool.name", "tool", "provenance.scanner", "provenance.tool"])
        or ("codex-security" if schema_family == "codex-security" else "")
    ).strip()


def scanner_version(payload: Any, args: argparse.Namespace) -> str:
    return (
        args.scanner_version
        or first_text(payload, ["scanner.version", "tool.version", "provenance.scanner_version", "provenance.version"])
    ).strip()


def ruleset_metadata(payload: Any, args: argparse.Namespace) -> Any:
    if args.ruleset or args.ruleset_version:
        data: dict[str, Any] = {}
        if args.ruleset:
            data["name"] = args.ruleset
        if args.ruleset_version:
            data["version"] = args.ruleset_version
        return data
    return first_value(payload, ["ruleset", "ruleset_metadata", "scan.ruleset", "metadata.ruleset"])


def scan_scope(payload: Any, args: argparse.Namespace) -> Any:
    if args.scan_scope:
        return args.scan_scope
    return first_value(payload, ["scan_scope", "scope", "scan.scope", "metadata.scan_scope"])


def source_binding(payload: Any, args: argparse.Namespace) -> dict[str, Any]:
    binding: dict[str, Any] = {}
    source_hash = args.source_hash or first_text(payload, ["source_hash", "source.source_hash", "source.sourceHash", "metadata.source_hash"])
    commit_sha = args.commit_sha or first_text(payload, ["commit_sha", "commit", "source.commit_sha", "source.commit", "source.git_head", "git_head"])
    base_sha = args.base_sha or first_text(payload, ["base_sha", "source.base_sha"])
    head_sha = args.head_sha or first_text(payload, ["head_sha", "source.head_sha"])
    if source_hash:
        binding["source_hash"] = source_hash
    if commit_sha:
        binding["commit_sha"] = commit_sha
    if base_sha:
        binding["base_sha"] = base_sha
    if head_sha:
        binding["head_sha"] = head_sha
    return binding


def git_head(project: Path) -> str:
    code, out, _ = live_common.run_git(["rev-parse", "HEAD"], project)
    return out.strip() if code == 0 else ""


def git_status_path(line: str) -> str:
    path = line[3:] if len(line) > 3 else line.strip()
    path = path.strip().strip('"')
    if " -> " in path:
        path = path.split(" -> ", 1)[1].strip().strip('"')
    return path


def source_dirty_entries(project: Path) -> list[str]:
    code, out, _ = live_common.run_git(["status", "--short", "--untracked-files=all", "--", "."], project)
    if code != 0:
        return ["?? <git status unavailable>"]
    dirty: list[str] = []
    for line in out.splitlines():
        path = git_status_path(line)
        parts = Path(path).parts
        if any(part in live_common.IGNORED_PARTS or part == ".starforge" for part in parts):
            continue
        dirty.append(line)
    return dirty


def source_snapshot_rel_paths(project: Path) -> set[str]:
    return {live_common.project_relative(project, path) for path in live_common.snapshot_file_candidates(project)}


def dirty_paths_missing_from_source_snapshot(project: Path) -> list[str]:
    snapshot_paths = source_snapshot_rel_paths(project)
    missing: list[str] = []
    for line in source_dirty_entries(project):
        rel = git_status_path(line)
        if not rel or rel in snapshot_paths:
            continue
        missing.append(line)
    return missing


def source_tree_clean_at_head(project: Path) -> bool:
    return bool(git_head(project)) and not source_dirty_entries(project)


def validate_source_binding(project: Path, binding: Mapping[str, Any], problems: list[dict[str, Any]]) -> bool:
    current_source = live_common.compute_source_hash(project)
    source_hash = str(binding.get("source_hash") or "")
    commit_sha = str(binding.get("commit_sha") or "")
    source_matches = bool(source_hash and source_hash == current_source)
    source_ok = source_matches
    if source_matches:
        missing_dirty = dirty_paths_missing_from_source_snapshot(project)
        if missing_dirty:
            source_ok = False
            add_problem(
                problems,
                "security report source_hash does not cover dirty source paths: "
                + ", ".join(git_status_path(item) for item in missing_dirty[:5]),
                rule="security-source-binding",
            )
    commit_ok = False
    head = ""
    if commit_sha:
        head = git_head(project)
        clean = source_tree_clean_at_head(project)
        commit_ok = bool(head and commit_sha == head and clean)
        if not head:
            add_problem(problems, "commit binding was provided but the project has no git HEAD", rule="security-source-binding")
        elif commit_sha == head and not clean:
            add_problem(problems, "security report commit binding requires a clean source tree at HEAD", rule="security-source-binding")
    if source_hash and not source_matches:
        add_problem(problems, "security report source_hash is stale", rule="security-source-binding")
    if commit_sha and head and commit_sha != head:
        add_problem(problems, "security report commit binding is stale", rule="security-source-binding")
    if not source_hash and not commit_sha:
        add_problem(problems, "security report requires source_hash or commit binding", rule="security-source-binding")
    return source_ok or commit_ok


def declared_input_hash(payload: Any, args: argparse.Namespace) -> str:
    return (
        args.input_hash
        or first_text(payload, ["input_hash", "input.sha256", "input.input_hash", "metadata.input_hash", "provenance.input_hash"])
    ).strip().lower()


def dependency_manifest_records(project: Path, payload: Any, args: argparse.Namespace, problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_items: list[Any] = list(args.dependency_manifest or [])
    report_items = first_value(payload, ["dependency_manifests", "dependencies.manifests", "scan.dependency_manifests"])
    if isinstance(report_items, list):
        raw_items.extend(report_items)
    elif report_items:
        raw_items.append(report_items)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
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
        if rel in seen:
            continue
        seen.add(rel)
        records.append({"path": rel, "sha256": file_sha256(path), "bytes": path.stat().st_size})
    return records


def finding_path(raw: Mapping[str, Any]) -> str:
    value = first_value(raw, ["file", "path", "file_path", "location.file", "location.path", "primary_location.file", "primary_location.path"])
    if isinstance(value, Mapping):
        value = first_value(value, ["path", "file"])
    return str(value or "")


def finding_line(raw: Mapping[str, Any]) -> Any:
    value = first_value(raw, ["line", "start_line", "location.line", "location.start_line", "primary_location.line"])
    return value


def build_evidence(raw: Mapping[str, Any]) -> dict[str, Any]:
    evidence = raw.get("evidence")
    if isinstance(evidence, Mapping):
        out = dict(evidence)
    elif evidence not in (None, ""):
        out = {"value": evidence}
    else:
        out = {}
    for key in ("file", "path", "line", "start_line", "end_line", "snippet", "message", "package", "dependency", "vulnerability", "url"):
        if key in raw and key not in out:
            out[key] = raw[key]
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
    raw_id = first_text(raw, ["id", "finding_id", "findingId", "issue_id", "uuid", "fingerprint"]) or f"finding-{index + 1}"
    rule_id = first_text(raw, ["rule_id", "ruleId", "rule", "check_id", "checkId", "cwe", "cve"]) or "unknown-rule"
    raw_severity = first_value(raw, ["severity", "level", "priority", "risk", "impact", "cvss_score", "cvss.score"])
    severity, known = normalize_severity(raw_severity)
    raw_confidence = first_value(raw, ["confidence", "certainty", "likelihood"])
    confidence = normalize_confidence(raw_confidence)
    title = first_text(raw, ["title", "name", "summary"])
    message = first_text(raw, ["message", "description", "details"])
    remediation = first_value(raw, ["remediation", "recommendation", "fix", "fix_text", "help"])
    evidence = build_evidence(raw)
    redacted_evidence, evidence_report = redact_payload(evidence, project)
    redacted_remediation, remediation_report = redact_payload(remediation or "", project)
    redacted_title, title_report = redact_payload(title, project)
    redacted_message, message_report = redact_payload(message, project)
    redaction_totals.update(merge_reports(redaction_totals, evidence_report, remediation_report, title_report, message_report))
    raw_path = finding_path(raw)
    path = sanitize_path_text(project, raw_path) if raw_path else ""
    line = finding_line(raw)
    fingerprint_basis = {
        "scanner": scanner,
        "rule_id": rule_id,
        "raw_id": raw_id,
        "path": path,
        "line": line,
        "severity": severity,
        "message": redacted_message or redacted_title,
    }
    normalized: dict[str, Any] = {
        "schema": "star-forge.normalized-security-finding.v1",
        "id": raw_id,
        "raw_id": raw_id,
        "scanner": scanner,
        "scanner_version": version,
        "rule_id": rule_id,
        "raw_severity": raw_severity if raw_severity is not None else "",
        "severity": severity,
        "normalized_severity": severity,
        "severity_known": known,
        "confidence": confidence,
        "raw_confidence": raw_confidence if raw_confidence is not None else "",
        "title": redacted_title,
        "message": redacted_message,
        "evidence": redacted_evidence,
        "remediation": redacted_remediation,
        "fingerprint": "sfsec-" + stable_hash(fingerprint_basis)[:32],
        "source_schema": source_schema,
    }
    if path:
        normalized["path"] = path
    if line not in (None, ""):
        normalized["line"] = line
    return normalized


def severity_rank(severity: str) -> int:
    return {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1, "unknown": 6}.get(severity, 0)


def summarize_findings(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in findings:
        severity = str(item.get("severity") or "unknown")
        counts[severity] = counts.get(severity, 0) + 1
    max_severity = "info"
    if findings:
        max_severity = max((str(item.get("severity") or "unknown") for item in findings), key=severity_rank)
    return {
        "finding_count": len(findings),
        "severity_counts": counts,
        "max_severity": max_severity,
        "blocking_finding_count": sum(1 for item in findings if str(item.get("severity") or "") in live_common.BLOCKING_SEVERITIES),
        "unknown_severity_count": sum(1 for item in findings if str(item.get("severity") or "") not in KNOWN_SEVERITIES),
    }


def validate_required_metadata(
    *,
    trusted_schema: bool,
    scanner: str,
    version: str,
    ruleset: Any,
    scope: Any,
    input_hash_ok: bool,
    source_binding_ok: bool,
    problems: list[dict[str, Any]],
) -> None:
    if not trusted_schema:
        add_problem(problems, "unsupported security report schema", rule="security-schema")
    if not scanner:
        add_problem(problems, "security report requires scanner provenance", rule="security-provenance")
    if not version:
        add_problem(problems, "security report requires scanner version", rule="security-provenance")
    if ruleset in (None, "", {}, []):
        add_problem(problems, "security report requires ruleset metadata", rule="security-ruleset")
    if scope in (None, "", {}, []):
        add_problem(problems, "security report requires scan scope", rule="security-scope")
    if not input_hash_ok:
        add_problem(problems, "security report requires a declared input hash matching the input bytes", rule="security-input-hash")
    if not source_binding_ok:
        add_problem(problems, "security report requires a fresh source hash or commit binding", rule="security-source-binding")


def build_command_argvs(project_arg: str, task: str, profile: str, kind: str, scanner: str, version: str, root: Path, project: Path) -> dict[str, list[str]]:
    handoff = live_common.project_relative(project, root / "handoff-input.json")
    findings = live_common.project_relative(project, root / "normalized-findings.json")
    manifest = live_common.project_relative(project, root / "manifest.json")
    base = ["python3", "scripts/star_forge.py"]
    handoff_cmd = base + ["security-handoff-packet", "--project", project_arg, "--kind", kind, "--input", handoff, "--strict"]
    proof_cmd = base + [
        "security-proof",
        "--project",
        project_arg,
        "--task",
        task,
        "--profile",
        profile,
        "--scanner",
        scanner,
        "--scanner-version",
        version,
        "--findings",
        findings,
        "--artifact",
        manifest,
        "--strict",
    ]
    return {
        "security_handoff_packet": handoff_cmd,
        "security_proof": proof_cmd,
    }


def display_command(command: Sequence[str]) -> str:
    return shlex.join([str(item) for item in command])


def build_commands(project_arg: str, task: str, profile: str, kind: str, scanner: str, version: str, root: Path, project: Path) -> dict[str, str]:
    return {
        name: display_command(command)
        for name, command in build_command_argvs(project_arg, task, profile, kind, scanner, version, root, project).items()
    }


def trusted_record_command(command: Sequence[str]) -> list[str]:
    actual = [str(item) for item in command]
    if actual and actual[0] == "python3":
        actual[0] = sys.executable
    if len(actual) > 1 and actual[1] == "scripts/star_forge.py":
        actual[1] = str(STAR_FORGE)
    return actual


def maybe_record(commands: Mapping[str, Sequence[str]], cwd: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name, command in commands.items():
        actual = trusted_record_command(command)
        proc = subprocess.run(actual, cwd=str(cwd), shell=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        results[name] = {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "command_argv": actual,
        }
    return results


def adapt(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    project = Path(args.project).resolve()
    root = live_common.live_collector_dir(project, args.task, "security")
    problems: list[dict[str, Any]] = []
    command_argv = ["security_adapter.py", *getattr(args, "_command_argv", sys.argv[1:])]

    if args.profile not in VALID_PROFILES:
        add_problem(problems, f"security profile is invalid: {args.profile}", rule="security-profile")
    kind = KIND_BY_PROFILE.get(args.profile, args.profile or "security")

    try:
        input_path = live_common.safe_project_path(project, args.input, must_exist=True)
    except ValueError as exc:
        input_path = project / "__missing_security_input__.json"
        add_problem(problems, f"security input path is unsafe or missing: {exc}", rule="security-input")

    payload: Any = {}
    if input_path.exists():
        try:
            payload = read_json(input_path)
        except Exception as exc:
            add_problem(problems, f"security input is malformed JSON: {exc}", rule="security-input-json", path=live_common.project_relative(project, input_path))
    else:
        add_problem(problems, "security input does not exist", rule="security-input")

    source_schema, schema_family, trusted_schema = detect_schema(payload)
    scanner = scanner_name(payload, args, schema_family)
    version = scanner_version(payload, args)
    ruleset = ruleset_metadata(payload, args)
    scope = scan_scope(payload, args)
    binding = source_binding(payload, args)
    source_binding_ok = validate_source_binding(project, binding, problems)
    actual_input_hash = file_sha256(input_path) if input_path.exists() else ""
    declared_hash = declared_input_hash(payload, args)
    input_hash_ok = bool(declared_hash and actual_input_hash and declared_hash.lower() == actual_input_hash.lower())

    dependency_manifests = dependency_manifest_records(project, payload, args, problems)
    validate_required_metadata(
        trusted_schema=trusted_schema,
        scanner=scanner,
        version=version,
        ruleset=ruleset,
        scope=scope,
        input_hash_ok=input_hash_ok,
        source_binding_ok=source_binding_ok,
        problems=problems,
    )

    redaction_totals: dict[str, Any] = {"secret_values": 0, "sensitive_keys": 0, "home_paths": 0, "temp_paths": 0, "env_values": 0}
    normalized: list[dict[str, Any]] = []
    if trusted_schema:
        for index, raw in enumerate(extract_findings(payload)):
            finding = normalized_finding(
                project,
                raw,
                index=index,
                scanner=scanner,
                version=version,
                source_schema=source_schema,
                redaction_totals=redaction_totals,
            )
            normalized.append(finding)
            if not finding.get("severity_known"):
                add_problem(problems, f"security finding {finding['id']} has unknown severity", rule="security-severity")

    findings_summary = summarize_findings(normalized)
    normalized_payload = {
        "schema": NORMALIZED_FINDINGS_SCHEMA,
        "task": args.task,
        "profile": args.profile,
        "source_schema": source_schema,
        "scanner": scanner,
        "scanner_version": version,
        "findings": normalized,
        "summary": findings_summary,
    }
    normalized_payload, normalized_report = redact_payload(normalized_payload, project)
    redaction_totals = merge_reports(redaction_totals, normalized_report)
    findings_path = write_json(root / "normalized-findings.json", normalized_payload)

    input_hash_payload = {
        "schema": INPUT_HASH_SCHEMA,
        "input_path": live_common.project_relative(project, input_path) if input_path.exists() else "",
        "declared_sha256": declared_hash,
        "actual_sha256": actual_input_hash,
        "matches": input_hash_ok,
    }
    input_hash_path = write_json(root / "input-hash.json", input_hash_payload)

    provenance = {
        "scanner": scanner,
        "scanner_version": version,
        "source_schema": source_schema,
        "schema_family": schema_family,
        "trusted_schema": trusted_schema,
        "report_provenance": payload.get("provenance") if isinstance(payload, Mapping) else {},
    }
    provenance, provenance_report = redact_payload(provenance, project)
    redaction_totals = merge_reports(redaction_totals, provenance_report)
    handoff_payload = {
        "schema": HANDOFF_INPUT_SCHEMA,
        "task": args.task,
        "profile": args.profile,
        "kind": kind,
        "provenance": provenance,
        "scanner": scanner,
        "scanner_version": version,
        "ruleset": ruleset,
        "scan_scope": scope,
        "source_binding": binding,
        "input_hash": input_hash_payload,
        "dependency_manifests": dependency_manifests,
        "normalized_findings": {
            "path": live_common.project_relative(project, findings_path),
            "sha256": file_sha256(findings_path),
            **findings_summary,
        },
        "problems": problems,
    }
    handoff_payload, handoff_report = redact_payload(handoff_payload, project)
    redaction_totals = merge_reports(redaction_totals, handoff_report)
    handoff_path = write_json(root / "handoff-input.json", handoff_payload)

    redaction_report_payload = {
        "schema": "star-forge.security-redaction-report.v1",
        "counts": redaction_totals,
        "notes": [
            "Secrets, sensitive keys, home paths, temp paths, env values, and scanner snippets are redacted before handoff."
        ],
    }
    redaction_path = write_json(root / "redaction-report.json", redaction_report_payload)

    artifacts: dict[str, Path] = {
        "normalized-findings": findings_path,
        "handoff-input": handoff_path,
        "input-hash": input_hash_path,
        "redaction-report": redaction_path,
    }
    for index, record in enumerate(dependency_manifests):
        artifacts[f"dependency-manifest-{index + 1}"] = project / record["path"]

    summary = {
        "trusted_provenance": bool(trusted_schema and scanner and version),
        "scanner": scanner,
        "scanner_version": version,
        "source_schema": source_schema,
        "schema_family": schema_family,
        "ruleset": ruleset if ruleset not in (None, "", {}, []) else "",
        "scan_scope": scope if scope not in (None, "", {}, []) else "",
        "input_hash": declared_hash if input_hash_ok else "",
        "source_binding": {**binding, "fresh": source_binding_ok},
        "dependency_manifests": dependency_manifests,
        **findings_summary,
    }
    manifest_path = live_common.write_live_manifest(
        project,
        task=args.task,
        collector="security",
        command_argv=command_argv,
        tool_versions={scanner or "security-adapter": version or "unknown"},
        artifacts=artifacts,
        summary=summary,
        degraded=bool(problems),
        problems=problems,
    )

    project_arg = "."
    try:
        if Path(args.project).resolve() != Path.cwd().resolve():
            project_arg = str(project)
    except OSError:
        project_arg = str(project)
    command_argvs = build_command_argvs(project_arg, args.task, args.profile, kind, scanner, version, root, project)
    commands = {name: display_command(command) for name, command in command_argvs.items()}

    record_results = maybe_record(command_argvs, project) if args.record else {}
    record_failed = False
    for name, record in record_results.items():
        if int(record.get("returncode") or 0) != 0:
            record_failed = True
            add_problem(
                problems,
                f"security record command {name} failed with exit code {record.get('returncode')}",
                rule="security-record",
            )
    verdict = "FAIL" if problems else "PASS"
    result = {
        "schema": RESULT_SCHEMA,
        "verdict": verdict,
        "task": args.task,
        "profile": args.profile,
        "kind": kind,
        "artifacts": {
            "normalized_findings": live_common.project_relative(project, findings_path),
            "handoff_input": live_common.project_relative(project, handoff_path),
            "input_hash": live_common.project_relative(project, input_hash_path),
            "redaction_report": live_common.project_relative(project, redaction_path),
            "manifest": live_common.project_relative(project, manifest_path),
        },
        "commands": commands,
        "problems": problems,
    }
    if record_results:
        result["record_results"] = record_results
    return (1 if record_failed or (args.strict and problems) else 0), result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize trusted security scanner reports for Star Forge")
    parser.add_argument("--project", default=".")
    parser.add_argument("--task", required=True)
    parser.add_argument("--profile", required=True, choices=sorted(VALID_PROFILES))
    parser.add_argument("--input", required=True)
    parser.add_argument("--scanner", default="")
    parser.add_argument("--scanner-version", default="")
    parser.add_argument("--ruleset", default="")
    parser.add_argument("--ruleset-version", default="")
    parser.add_argument("--scan-scope", default="")
    parser.add_argument("--input-hash", default="")
    parser.add_argument("--source-hash", default="")
    parser.add_argument("--commit-sha", default="")
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--dependency-manifest", action="append", default=[])
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--strict", action="store_true", default=True)
    parser.add_argument("--no-strict", action="store_false", dest="strict")
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
