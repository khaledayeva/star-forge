#!/usr/bin/env python3
"""GitHub identity, source binding, command redaction, and CI log kernels."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from live_collectors.policy_data import (
    first_path_text, first_text, flatten_paginated, nested,
    gh_api_endpoint, github_host_evidence_for_raw, github_host_from_payload,
    github_host_from_provenance, option_value, option_values, parse_commands,
    pagination_flags, policy_dict, policy_set, policy_tuple, shell_argv, trusted_proof_command, unwrap_edges,
    validate_transcript_github_host,
)
parse_gh_commands = parse_commands

SCRIPT_DIR = Path(__file__).resolve().parents[1]
STAR_FORGE_SCRIPT = SCRIPT_DIR / "star_forge.py"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from live_collectors import common as live_common
from starforge import evidence

globals().update(policy_dict("github_identity", "CONSTANTS"))
BLOCKING_CONCLUSIONS = policy_set("github_identity", "BLOCKING_CONCLUSIONS")
CONNECTOR_READ_OPERATIONS = policy_set("github_identity", "CONNECTOR_READ_OPERATIONS")
GH_TOP_LEVEL_MUTATIONS = policy_set("github_identity", "GH_TOP_LEVEL_MUTATIONS")
GH_API_VALUE_FLAGS = policy_set("github_identity", "GH_API_VALUE_FLAGS")
GH_API_VALUE_PREFIXES = policy_tuple("github_identity", "GH_API_VALUE_PREFIXES")
GH_COMMAND_SHELL_CONTROL_RE = re.compile(
    r"(?:;|&&|\|\||\||[\r\n]|\$\(|`|<\(|>\(|\d?>{1,2}|&>|>\||\d?<{1,2})"
)
_GH_CONFIG = policy_dict("github_identity", "GH_COMMAND_CONFIG")
GH_RUN_MUTATIONS = set(_GH_CONFIG["run_mutations"])
GH_API_FLAG_ONLY = set(_GH_CONFIG["api_allowed_flag_only"])
GH_API_FIELD_FLAGS = set(_GH_CONFIG["api_field_flags"])
GH_API_FIELD_PREFIXES = tuple(_GH_CONFIG["api_field_prefixes"])
GH_PR_VIEW_VALUE_FLAGS = set(_GH_CONFIG["pr_view_value_flags"])
GH_PR_VIEW_VALUE_PREFIXES = tuple(_GH_CONFIG["pr_view_value_prefixes"])
GH_RUN_VIEW_VALUE_FLAGS = GH_PR_VIEW_VALUE_FLAGS | {"--attempt"}
GH_RUN_VIEW_VALUE_PREFIXES = (*GH_PR_VIEW_VALUE_PREFIXES, "--attempt=")
GH_RUN_VIEW_FLAG_ONLY = set(_GH_CONFIG["run_view_flag_only"])
GH_API_ALLOWED_VALUE_FLAGS = set(_GH_CONFIG["api_allowed_value_flags"])
GH_API_ALLOWED_VALUE_PREFIXES = tuple(_GH_CONFIG["api_allowed_value_prefixes"])
GH_API_ALLOWED_FLAG_ONLY = GH_API_FLAG_ONLY
GH_API_FORBIDDEN_VALUE_FLAGS = set(_GH_CONFIG["api_forbidden_value_flags"])
ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9:/])/(?:Users|home|private|tmp|var|Volumes|opt)/[^\s\"'<>]+")
WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:Users|Temp|Windows|Program Files)\\[^\s\"'<>]+")

@dataclass
class RawEvidence:
    source: str
    pr: Mapping[str, Any]
    final_pr: Mapping[str, Any]
    diff: str
    files: Any
    reviews: Any
    comments: Any
    check_runs: Any
    annotations: Any
    logs: Any
    commands: list[list[str]]
    operations: list[Any]
    tool_versions: dict[str, Any]
    live_provenance: dict[str, Any] = field(default_factory=dict)
    foundation_provenance: dict[str, Any] = field(default_factory=dict)

@dataclass
class CollectionResult:
    manifest_path: Path
    evidence_path: Path
    commands: list[list[str]]
    problems: list[dict[str, Any]]

def is_live_source(source: str) -> bool:
    return str(source).endswith("-live")

def check_head_sha(item: Mapping[str, Any], top_head_sha: str) -> str:
    return first_text(
        item.get("head_sha"), item.get("headSha"), nested(item, "commit", "sha"),
        nested(item, "check_suite", "head_sha"), nested(item, "checkSuite", "headSha"),
        top_head_sha,
    )

read_json = live_common.read_json
read_text = live_common.read_text
merge_reports = live_common.merge_reports

def write_json(path: Path, payload: Any) -> Path:
    return live_common.write_json(path, payload, redact=False)[0]

def write_text(path: Path, text: str) -> Path:
    return live_common.write_text(path, text, redact=False)[0]

def blocking_problem(message: str, *, rule: str, path: str = "") -> dict[str, Any]:
    return live_common.blocking_problem(message, rule=rule, path=path)

def command_problem(message: str) -> dict[str, Any]:
    return blocking_problem(message, rule="github-command")

def normalize_abs_paths(value: Any) -> tuple[Any, dict[str, int]]:
    report = {"absolute_paths": 0}

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            def replace(match: re.Match[str]) -> str:
                report["absolute_paths"] += 1
                return live_common.sanitize_external_path(Path(match.group(0)))
            return WINDOWS_PATH_RE.sub(replace, ABS_PATH_RE.sub(replace, item))
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, dict):
            return {str(key): clean(child) for key, child in item.items()}
        return item
    return clean(value), report

def redact_artifact_payload(value: Any) -> tuple[Any, dict[str, int]]:
    value, command_report = redact_gh_api_command_query_values(value)
    value, path_report = normalize_abs_paths(value)
    value, secret_report = live_common.redact_sensitive_values(value)
    return value, merge_reports(command_report, path_report, secret_report)

def update_manifest_redaction_report(manifest_path: Path, report: Mapping[str, int]) -> None:
    payload = read_json(manifest_path, {})
    current = payload.get("redaction_report")
    payload["redaction_report"] = merge_reports(current if isinstance(current, dict) else {}, report)
    write_json(manifest_path, payload)

def is_url_like(value: Any) -> bool:
    return isinstance(value, str) and ("://" in value.strip() or value.strip().startswith("//"))

def canonical_github_host(raw: Any) -> str:
    text = str(raw or "").strip().lower().rstrip(".")
    if not text or not is_url_like(text) and ("/" in text or "\\" in text):
        return ""
    host = (urllib.parse.urlsplit(text if is_url_like(text) else f"//{text}").hostname or "")
    host = host.lower().rstrip(".")
    return "github.com" if host == "api.github.com" else host

def github_host_from_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    return canonical_github_host(urllib.parse.urlsplit(value.strip()).hostname or "")

def github_host_evidence_from_value(
    value: Any, label: str, *, require_url_like: bool = False
) -> list[tuple[str, str]]:
    if require_url_like and not is_url_like(value):
        return []
    host = github_host_from_url(value) if is_url_like(value) else canonical_github_host(value)
    return [(label, host)] if host else []

def github_host_evidence_from_provenance(provenance: Any, label: str) -> list[tuple[str, str]]:
    if not isinstance(provenance, Mapping):
        return []
    return [
        item for key in _HOST_KEYS
        for item in github_host_evidence_from_value(provenance.get(key), f"{label}.{key}")
    ]

def github_host_evidence_from_payload(
    payload: Any, label: str, _seen: set[int] | None = None
) -> list[tuple[str, str]]:
    if not isinstance(payload, Mapping):
        return []
    seen = _seen if _seen is not None else set()
    if id(payload) in seen:
        return []
    seen.add(id(payload))
    evidence = [
        item for key in GITHUB_URL_KEYS
        for item in github_host_evidence_from_value(
            payload.get(key), f"{label}.{key}", require_url_like=True
        )
    ]
    for key in GITHUB_IDENTITY_URL_KEYS:
        value = payload.get(key)
        evidence += github_host_evidence_from_value(
            value, f"{label}.{key}", require_url_like=True
        )
        evidence += github_host_evidence_from_payload(value, f"{label}.{key}", seen)
    return evidence

def github_host_provenance_evidence_for_raw(raw: RawEvidence) -> list[tuple[str, str]]:
    return github_host_evidence_from_provenance(raw.live_provenance, "live_provenance")

def github_host_payload_evidence_for_raw(raw: RawEvidence) -> list[tuple[str, str]]:
    return [
        item for payload, label in ((raw.pr, "pr"), (raw.final_pr, "final_pr"))
        for item in github_host_evidence_from_payload(payload, label)
    ]

def github_host_evidence_from_operations(operations: Any, label: str) -> list[tuple[str, str]]:
    if not isinstance(operations, list):
        return []
    return [
        item for index, operation in enumerate(operations, 1)
        if isinstance(operation, Mapping)
        for item in (
            github_host_evidence_from_provenance(operation, f"{label}[{index}]")
            + github_host_evidence_from_payload(operation, f"{label}[{index}]")
        )
    ]

def resolve_github_host(
    evidence: Sequence[tuple[str, str]], *, default_host: str = ""
) -> str:
    hosts = {host for _, raw in evidence if (host := canonical_github_host(raw))}
    return next(iter(hosts)) if len(hosts) == 1 else "" if hosts else canonical_github_host(default_host)

def github_host_policy_messages(
    evidence: Sequence[tuple[str, str]], *, require_host: bool, context: str
) -> list[str]:
    entries = list(dict.fromkeys(
        (str(label), host) for label, raw in evidence if (host := canonical_github_host(raw))
    ))
    messages = [f"{context} requires approved GitHub host provenance"] if require_host and not entries else []
    messages += [
        f"{label} {host} is not an approved GitHub host"
        for label, host in entries if host not in APPROVED_GITHUB_HOSTS
    ]
    hosts = sorted({host for _, host in entries})
    if len(hosts) > 1:
        messages.append(f"{context} has conflicting GitHub hosts: {', '.join(hosts)}")
    return messages

def github_host_for_raw(raw: RawEvidence) -> str:
    provenance = github_host_provenance_evidence_for_raw(raw)
    evidence = provenance if is_live_source(raw.source) else provenance + github_host_payload_evidence_for_raw(raw)
    default = "" if is_live_source(raw.source) else "github.com"
    return resolve_github_host(evidence, default_host=default)

def _payload_host_messages(
    evidence: list[tuple[str, str]], approved_host: str, context: str
) -> list[str]:
    messages = github_host_policy_messages(evidence, require_host=False, context=context)
    messages += [
        f"{label} host does not match approved GitHub provenance"
        for label, raw in evidence
        if approved_host and (host := canonical_github_host(raw)) and host != approved_host
    ]
    return messages

def validate_live_github_host(raw: RawEvidence) -> list[dict[str, Any]]:
    provenance = github_host_provenance_evidence_for_raw(raw)
    messages = github_host_policy_messages(
        provenance, require_host=True, context="live GitHub import"
    )
    payload = github_host_payload_evidence_for_raw(raw)
    payload += github_host_evidence_from_operations(raw.operations, "operation")
    messages += _payload_host_messages(payload, resolve_github_host(provenance), "live GitHub payload")
    return [blocking_problem(message, rule="github-live-provenance") for message in messages]

def _record_host_evidence(payload: Mapping[str, Any], label: str) -> list[tuple[str, str]]:
    return github_host_evidence_from_value(
        payload.get("github_host"), f"{label}.github_host"
    ) + github_host_evidence_from_provenance(
        payload.get("live_provenance"), f"{label}.live_provenance"
    )

def validate_transcript_github_host_evidence(
    *, transcript_payload: Mapping[str, Any], summary: Mapping[str, Any],
    pr_payload: Any = None, operations: Any = None,
) -> tuple[str, list[str]]:
    transcript = _record_host_evidence(transcript_payload, "operation_transcript")
    combined = transcript + _record_host_evidence(summary, "summary")
    messages = github_host_policy_messages(
        combined, require_host=True, context="GitHub operation transcript"
    )
    if not any(canonical_github_host(host) for _, host in transcript):
        messages.append("GitHub operation transcript requires approved GitHub host provenance")
    github_host = resolve_github_host(combined)
    payload = github_host_evidence_from_payload(pr_payload, "pr")
    payload += github_host_evidence_from_operations(
        operations, "operation_transcript.operations"
    )
    messages += _payload_host_messages(payload, github_host, "GitHub PR payload")
    return github_host, messages

def gh_api_endpoint_is_absolute(endpoint: str) -> bool:
    parsed = urllib.parse.urlsplit(str(endpoint or "").strip())
    return bool(parsed.scheme or parsed.netloc)

def validate_gh_hostname(
    tokens: Sequence[str], *, github_host: str = ""
) -> list[dict[str, Any]]:
    hostnames = option_values(tokens, {"--hostname"})
    problems = [command_problem("gh --hostname must be provided at most once")] if len(hostnames) > 1 else []
    expected = canonical_github_host(github_host)
    for hostname in hostnames:
        host = canonical_github_host(hostname)
        message = (
            "gh --hostname must name a GitHub host" if not host else
            f"gh --hostname {hostname} is not an approved GitHub host"
            if host not in APPROVED_GITHUB_HOSTS else
            "gh --hostname does not match recorded GitHub provenance"
            if expected and host != expected else ""
        )
        problems += [command_problem(message)] if message else []
    return problems

def gh_api_endpoint_index(tokens: Sequence[str]) -> int:
    skip = False
    for index, token in enumerate(map(str, tokens[2:]), 2):
        if skip:
            skip = False
        elif token in GH_API_VALUE_FLAGS:
            skip = True
        elif (
            token.startswith(("-H", "-f", "-F", "-X")) and len(token) > 2
            or token in GH_API_FLAG_ONLY or token.startswith(GH_API_VALUE_PREFIXES)
            or token.startswith("-")
        ):
            continue
        else:
            return index
    return -1

def gh_api_endpoint_query_problems(endpoint: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlsplit(str(endpoint or "").strip()).query
    if not query:
        return []
    problems = [command_problem(
        "gh api endpoint query parameter <empty> is not allowlisted"
    )] if any(part == "" for part in query.split("&")) else []
    for key, value in urllib.parse.parse_qsl(query, keep_blank_values=True):
        key = str(key or "").strip()
        if live_common.sensitive_key_name(key):
            message = f"gh api endpoint query parameter {key or '<empty>'} is sensitive"
        elif key not in SAFE_GH_API_QUERY_PARAMS:
            message = f"gh api endpoint query parameter {key or '<empty>'} is not allowlisted"
        elif not re.fullmatch(r"\d+", str(value or "").strip()):
            message = f"gh api endpoint query parameter {key} must be a decimal integer"
        else:
            lower, upper = SAFE_GH_API_QUERY_BOUNDS[key]
            numeric = int(value)
            message = (
                f"gh api endpoint query parameter {key} must be between {lower} and {upper}"
                if not lower <= numeric <= upper else ""
            )
        if message:
            problems.append(command_problem(message))
    return problems

def redact_gh_api_field_value(raw: str) -> str:
    text = str(raw or "")
    return f"{text.split('=', 1)[0]}=[REDACTED_SECRET]" if "=" in text else "[REDACTED_SECRET]"

def redact_gh_api_field_arguments(tokens: Sequence[str]) -> tuple[list[str], dict[str, int]]:
    output, count, index = list(map(str, tokens)), 0, 0
    while index < len(output):
        token = output[index]
        if token in GH_API_FIELD_FLAGS and index + 1 < len(output):
            output[index + 1] = redact_gh_api_field_value(output[index + 1])
            count += 1
            index += 2
            continue
        if token.startswith(("--field=", "--raw-field=")):
            flag, value = token.split("=", 1)
            output[index] = f"{flag}={redact_gh_api_field_value(value)}"
            count += 1
        elif token.startswith(("-f", "-F")) and len(token) > 2:
            output[index] = token[:2] + redact_gh_api_field_value(token[2:])
            count += 1
        index += 1
    return output, {"gh_api_field_values": count}

def redact_gh_api_endpoint_query_values(endpoint: str) -> tuple[str, dict[str, int]]:
    parsed = urllib.parse.urlsplit(str(endpoint or ""))
    if not parsed.query:
        return str(endpoint or ""), {"gh_api_query_values": 0}
    count, pairs = 0, []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        safe = key in SAFE_GH_API_QUERY_PARAMS and not live_common.sensitive_key_name(key)
        pairs.append((key, value if safe else "[REDACTED_SECRET]"))
        count += not safe
    query = urllib.parse.urlencode(pairs, doseq=True)
    return urllib.parse.urlunsplit((*parsed[:3], query, parsed.fragment)), {
        "gh_api_query_values": count
    }

def redact_gh_api_command_query_values(value: Any) -> tuple[Any, dict[str, int]]:
    report: dict[str, int] = {"gh_api_query_values": 0}

    def clean(item: Any) -> Any:
        if isinstance(item, list):
            tokens = list(map(str, item))
            if tokens[:2] != ["gh", "api"]:
                return [clean(child) for child in item]
            index = gh_api_endpoint_index(tokens)
            if index >= 0:
                tokens[index], local = redact_gh_api_endpoint_query_values(tokens[index])
                report.update(merge_reports(report, local))
            tokens, local = redact_gh_api_field_arguments(tokens)
            report.update(merge_reports(report, local))
            return tokens
        if isinstance(item, dict):
            return {str(key): clean(child) for key, child in item.items()}
        return item
    return clean(value), report

def approved_github_url_parts(value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    parsed = urllib.parse.urlsplit(value.strip())
    if (
        parsed.scheme.lower() != "https" or not parsed.netloc
        or canonical_github_host(parsed.hostname or "") not in APPROVED_GITHUB_HOSTS
    ):
        return []
    return [urllib.parse.unquote(part) for part in parsed.path.strip("/").split("/") if part]

def github_url_identity_messages(
    value: Any, label: str, *, require_url: bool = False
) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    if not require_url and not is_url_like(value):
        return []
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return [f"{label} must be an absolute https GitHub URL"]
    host = canonical_github_host(parsed.hostname or "")
    return [f"{label} {host or '<missing>'} is not an approved GitHub host"] if (
        host not in APPROVED_GITHUB_HOSTS
    ) else []

def display_command(command: Sequence[str]) -> str:
    return shlex.join(list(map(str, command)))

def normalize_gh_api_endpoint(endpoint: str) -> str:
    raw = str(endpoint or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    raw = parsed.path if parsed.scheme and parsed.netloc else raw.split("?", 1)[0]
    return urllib.parse.unquote(raw).strip("/")

def id_values(*values: Any) -> set[str]:
    return {
        text for value in values if value is not None and (text := str(value).strip())
        and text.lower() not in {"none", "null"}
    }

def actions_ids_from_url(value: Any) -> tuple[set[str], set[str]]:
    if not isinstance(value, str) or not value.strip():
        return set(), set()
    parts = [
        urllib.parse.unquote(part)
        for part in urllib.parse.urlsplit(value.strip()).path.strip("/").split("/") if part
    ]
    runs = {parts[index + 1] for index, part in enumerate(parts[:-1]) if part == "runs"}
    jobs = {
        parts[index + 1] for index, part in enumerate(parts[:-1])
        if part in {"job", "jobs"}
    }
    return runs, jobs

_CI_ID_PATHS = policy_dict("github_identity", "CI_ID_PATHS")
_CI_BINDING_PATHS = policy_dict("github_identity", "CI_BINDING_PATHS")

def ci_log_identity_fields(item: Any) -> dict[str, str]:
    if not isinstance(item, Mapping):
        return {field: "" for field in _CI_ID_PATHS}
    identity = item.get("identity")
    sources = (item, identity if isinstance(identity, Mapping) else {})
    return {
        field: first_text(*(nested(source, *path) for source in sources for path in paths))
        for field, paths in _CI_ID_PATHS.items()
    }

def pr_bound_ci_ids(check_runs: Any, *, captured_head: str) -> dict[str, set[str]]:
    ids = {"check_runs": set(), "runs": set(), "jobs": set()}
    captured = str(captured_head or "").strip()
    if not captured:
        return ids
    top = first_text(check_runs.get("head_sha"), check_runs.get("headSha")) if (
        isinstance(check_runs, Mapping)
    ) else ""
    items, _, _ = flatten_paginated(check_runs, ("check_runs", "checks", "runs", "nodes"))
    for item in items:
        if not isinstance(item, Mapping) or check_head_sha(item, top) != captured:
            continue
        for bucket, paths in _CI_BINDING_PATHS.items():
            ids[bucket].update(id_values(*(nested(item, *path) for path in paths)))
        for key in ("url", "html_url", "htmlUrl", "details_url", "detailsUrl"):
            runs, jobs = actions_ids_from_url(item.get(key))
            ids["runs"].update(runs)
            ids["jobs"].update(jobs)
    return ids

def ci_log_problem(message: str, *, rule: str, path: str = "") -> dict[str, Any]:
    return command_problem(message) if rule == "github-command" else blocking_problem(
        message, rule=rule, path=path
    )

def validate_ci_log_identity(
    item: Any, *, repo: str, pr_number: str, captured_head: str, check_runs: Any,
    label: str = "CI log entry", rule: str = "github-logs", path: str = "",
) -> list[dict[str, Any]]:
    problem = lambda message: ci_log_problem(message, rule=rule, path=path)
    if not isinstance(item, Mapping):
        return [problem(f"{label} must be a structured object")]
    identity = ci_log_identity_fields(item)
    problems: list[dict[str, Any]] = []
    checks = (
        ("repo", str(repo or "").strip(), "repository"),
        ("pr", str(pr_number or "").strip(), "PR"),
        ("captured_head_sha", str(captured_head or "").strip(), "captured head SHA"),
    )
    for field, expected, human in checks:
        actual = identity[field]
        if field == "captured_head_sha" and not expected:
            problems.append(problem(f"{label} cannot be bound without a captured head SHA"))
        if not actual:
            problems.append(problem(f"{label} requires {human} identity"))
        elif expected and actual != expected:
            problems.append(problem(f"{label} {human} does not match the requested PR"))
    supplied = {field: identity[field] for field in ("check_run_id", "run_id", "job_id")}
    if not any(supplied.values()):
        problems.append(problem(f"{label} requires a check_run_id, run_id, or job_id"))
        return problems
    bound = pr_bound_ci_ids(check_runs, captured_head=str(captured_head or "").strip())
    bindings = {
        "check_run_id": ("check_runs", "check run id"),
        "run_id": ("runs", "workflow run id"),
        "job_id": ("jobs", "workflow job id"),
    }
    problems += [
        problem(f"{label} {human} is not bound to the requested PR head SHA")
        for field, value in supplied.items() if value
        for bucket, human in (bindings[field],) if value not in bound[bucket]
    ]
    return problems

def validate_ci_log_excerpt_payload(
    payload: Any, *, repo: str, pr_number: str, captured_head: str,
    check_runs: Any, path: str = "",
) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return [blocking_problem(
            "CI log excerpts must be a JSON object", rule="github-logs", path=path
        )]
    logs = payload.get("logs")
    if not isinstance(logs, list) or not logs:
        return [blocking_problem(
            "CI log excerpts must contain at least one log entry",
            rule="github-logs", path=path,
        )]
    return [
        problem for index, entry in enumerate(logs, 1)
        for problem in validate_ci_log_identity(
            entry, repo=repo, pr_number=pr_number, captured_head=captured_head,
            check_runs=check_runs, label=f"CI log entry {index}",
            rule="github-logs", path=path,
        )
    ]
