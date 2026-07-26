#!/usr/bin/env python3
"""Read-only GitHub PR evidence adapter for Star Forge source packets."""

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

SCRIPT_DIR = Path(__file__).resolve().parents[1]
STAR_FORGE_SCRIPT = SCRIPT_DIR / "star_forge.py"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from live_collectors import common as live_common
from starforge import evidence


COLLECTOR = "github"
CAPABILITY = "github-lifecycle"
PREFERRED_PROVIDER = "github-connector"
GH_READONLY_PROVIDER = "gh-readonly"
GH_CREATE_PROVIDER = "gh-cli"
GH_CREATE_FALLBACK = "gh repo create --private"
EVIDENCE_FILENAME = "evidence.v2.json"
DEFAULT_MAX_LOG_BYTES = 20_000
HARD_MAX_LOG_BYTES = 64 * 1024
SUCCESSFUL_CONCLUSIONS = {"success"}
BLOCKING_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "failed",
    "neutral",
    "skipped",
    "startup_failure",
    "timed_out",
}
COMPLETED_STATUSES = {"completed", "success"}
PENDING_STATUSES = {"expected", "in_progress", "pending", "queued", "requested", "waiting"}
CONNECTOR_READ_OPERATIONS = {
    "pull_request",
    "pull_request_metadata",
    "pr",
    "pr_metadata",
    "files",
    "changed_files",
    "diff",
    "reviews",
    "review_comments",
    "comments",
    "check_runs",
    "checks",
    "annotations",
    "logs",
    "ci_logs",
}
GH_RUN_MUTATIONS = {"cancel", "delete", "download", "rerun", "watch"}
GH_TOP_LEVEL_MUTATIONS = {
    "auth",
    "browse",
    "codespace",
    "config",
    "extension",
    "gist",
    "gpg-key",
    "issue",
    "label",
    "project",
    "release",
    "repo",
    "secret",
    "ssh-key",
    "status",
    "variable",
    "workflow",
}
GH_API_VALUE_FLAGS = {
    "--cache",
    "--field",
    "--header",
    "--hostname",
    "--input",
    "--jq",
    "--method",
    "--preview",
    "--raw-field",
    "--template",
    "-F",
    "-H",
    "-X",
    "-f",
    "-q",
}
GH_API_FLAG_ONLY = {"--include", "--paginate", "--silent", "--slurp", "-i"}
GH_API_VALUE_PREFIXES = (
    "--cache=",
    "--field=",
    "--header=",
    "--hostname=",
    "--input=",
    "--jq=",
    "--method=",
    "--preview=",
    "--raw-field=",
    "--template=",
)
APPROVED_GITHUB_HOSTS = {"github.com"}
SAFE_GH_API_QUERY_PARAMS = {"page", "per_page"}
SAFE_GH_API_QUERY_BOUNDS = {"page": (1, 1000), "per_page": (1, 100)}
GH_API_FIELD_FLAGS = {"-f", "-F", "--field", "--raw-field"}
GH_API_FIELD_PREFIXES = ("--field=", "--raw-field=", "-f", "-F")
GITHUB_URL_KEYS = ("url", "html_url", "api_url", "web_url", "pull_request_url", "pullRequestUrl")
GITHUB_IDENTITY_URL_KEYS = ("repository", "pull_request", "pullRequest", "base", "head", "baseRef", "headRef")
GH_COMMAND_SHELL_CONTROL_RE = re.compile(
    r"(?:"
    r";|&&|\|\||\||[\r\n]|"
    r"\$\(|`|<\(|>\(|"
    r"\d?>{1,2}|&>|>\||\d?<{1,2}"
    r")"
)
GH_PR_VIEW_VALUE_FLAGS = {"--repo", "-R", "--json", "--jq", "--template", "--hostname"}
GH_PR_VIEW_VALUE_PREFIXES = ("--repo=", "--json=", "--jq=", "--template=", "--hostname=")
GH_RUN_VIEW_VALUE_FLAGS = {"--repo", "-R", "--json", "--jq", "--template", "--hostname", "--attempt"}
GH_RUN_VIEW_VALUE_PREFIXES = ("--repo=", "--json=", "--jq=", "--template=", "--hostname=", "--attempt=")
GH_RUN_VIEW_FLAG_ONLY = {"--log"}
GH_API_ALLOWED_VALUE_FLAGS = {"--cache", "--hostname", "--jq", "--method", "--preview", "--template", "-X", "-q"}
GH_API_ALLOWED_VALUE_PREFIXES = ("--cache=", "--hostname=", "--jq=", "--method=", "--preview=", "--template=")
GH_API_ALLOWED_FLAG_ONLY = {"--include", "--paginate", "--silent", "--slurp", "-i"}
GH_API_FORBIDDEN_VALUE_FLAGS = {"--field", "--header", "--input", "--raw-field", "-F", "-H", "-f"}
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


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_text(path: Path, default: str = "") -> str:
    if not path.exists():
        return default
    return path.read_text(encoding="utf-8", errors="replace")


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def merge_reports(*reports: Mapping[str, Any]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for report in reports:
        for key, value in report.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                merged[str(key)] = merged.get(str(key), 0) + value
    return merged


def blocking_problem(message: str, *, rule: str, path: str = "") -> dict[str, Any]:
    return live_common.blocking_problem(message, rule=rule, path=path)


def command_problem(message: str) -> dict[str, Any]:
    return blocking_problem(message, rule="github-command")


def normalize_abs_paths(value: Any) -> tuple[Any, dict[str, int]]:
    report = {"absolute_paths": 0}

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            def repl(match: re.Match[str]) -> str:
                report["absolute_paths"] += 1
                return live_common.sanitize_external_path(Path(match.group(0)))

            out = ABS_PATH_RE.sub(repl, item)
            out = WINDOWS_PATH_RE.sub(lambda match: repl(match), out)
            return out
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, dict):
            return {str(key): clean(child) for key, child in item.items()}
        return item

    return clean(value), report


def redact_artifact_payload(value: Any) -> tuple[Any, dict[str, int]]:
    command_cleaned, command_report = redact_gh_api_command_query_values(value)
    path_cleaned, path_report = normalize_abs_paths(command_cleaned)
    secret_cleaned, secret_report = live_common.redact_sensitive_values(path_cleaned)
    return secret_cleaned, merge_reports(command_report, path_report, secret_report)


def update_manifest_redaction_report(manifest_path: Path, report: Mapping[str, int]) -> None:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing = payload.get("redaction_report")
    merged = merge_reports(existing if isinstance(existing, dict) else {}, report)
    payload["redaction_report"] = merged
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def shell_argv(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            tokens = shlex.split(raw)
        except ValueError:
            return ["<malformed-gh-command>"]
        if "\n" in raw or "\r" in raw:
            tokens.append("\n")
        return tokens
    if isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        return [str(item) for item in raw]
    return []


def parse_gh_commands(raw: Any) -> list[list[str]]:
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    commands: list[list[str]] = []
    for item in items:
        if isinstance(item, dict):
            argv = item.get("argv") or item.get("command") or item.get("cmd")
        else:
            argv = item
        parsed = shell_argv(argv)
        if parsed:
            commands.append(parsed)
    return commands


def option_value(tokens: Sequence[str], names: set[str]) -> str:
    for idx, token in enumerate(tokens):
        if token in names and idx + 1 < len(tokens):
            return str(tokens[idx + 1])
        for name in names:
            if token.startswith(f"{name}="):
                return token.split("=", 1)[1]
    return ""


def option_values(tokens: Sequence[str], names: set[str]) -> list[str]:
    values: list[str] = []
    for idx, token in enumerate(tokens):
        if token in names and idx + 1 < len(tokens):
            values.append(str(tokens[idx + 1]))
        for name in names:
            if token.startswith(f"{name}="):
                values.append(token.split("=", 1)[1])
    return values


def is_url_like(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return "://" in text or text.startswith("//")


def canonical_github_host(raw: Any) -> str:
    text = str(raw or "").strip().lower().rstrip(".")
    if not text:
        return ""
    if not is_url_like(text) and ("/" in text or "\\" in text):
        return ""
    parsed = urllib.parse.urlsplit(text if is_url_like(text) else f"//{text}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return ""
    if host == "api.github.com":
        return "github.com"
    return host


def github_host_from_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    parsed = urllib.parse.urlsplit(value.strip())
    return canonical_github_host(parsed.hostname or "")


def github_host_from_provenance(provenance: Any) -> str:
    if not isinstance(provenance, Mapping):
        return ""
    for key in ("github_host", "host", "hostname", "gh_hostname", "server_url", "api_url", "html_url", "url"):
        value = provenance.get(key)
        host = github_host_from_url(value) if isinstance(value, str) and "://" in value else canonical_github_host(value)
        if host:
            return host
    return ""


def github_host_from_payload(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    for key in GITHUB_URL_KEYS:
        host = github_host_from_url(payload.get(key))
        if host:
            return host
    for key in GITHUB_IDENTITY_URL_KEYS:
        value = payload.get(key)
        host = github_host_from_url(value) if is_url_like(value) else github_host_from_payload(value)
        if host:
            return host
    return ""


def github_host_evidence_from_value(value: Any, label: str, *, require_url_like: bool = False) -> list[tuple[str, str]]:
    if require_url_like and not is_url_like(value):
        return []
    host = github_host_from_url(value) if is_url_like(value) else canonical_github_host(value)
    return [(label, host)] if host else []


def github_host_evidence_from_provenance(provenance: Any, label: str) -> list[tuple[str, str]]:
    if not isinstance(provenance, Mapping):
        return []
    evidence: list[tuple[str, str]] = []
    for key in ("github_host", "host", "hostname", "gh_hostname", "server_url", "api_url", "html_url", "url"):
        evidence.extend(github_host_evidence_from_value(provenance.get(key), f"{label}.{key}"))
    return evidence


def github_host_evidence_from_payload(payload: Any, label: str, _seen: set[int] | None = None) -> list[tuple[str, str]]:
    if not isinstance(payload, Mapping):
        return []
    if _seen is None:
        _seen = set()
    identity = id(payload)
    if identity in _seen:
        return []
    _seen.add(identity)
    evidence: list[tuple[str, str]] = []
    for key in GITHUB_URL_KEYS:
        evidence.extend(github_host_evidence_from_value(payload.get(key), f"{label}.{key}", require_url_like=True))
    for key in GITHUB_IDENTITY_URL_KEYS:
        value = payload.get(key)
        evidence.extend(github_host_evidence_from_value(value, f"{label}.{key}", require_url_like=True))
        evidence.extend(github_host_evidence_from_payload(value, f"{label}.{key}", _seen))
    return evidence


def github_host_evidence_for_raw(raw: RawEvidence) -> list[tuple[str, str]]:
    evidence: list[tuple[str, str]] = []
    evidence.extend(github_host_evidence_from_provenance(raw.live_provenance, "live_provenance"))
    evidence.extend(github_host_evidence_from_payload(raw.pr, "pr"))
    evidence.extend(github_host_evidence_from_payload(raw.final_pr, "final_pr"))
    return evidence


def github_host_provenance_evidence_for_raw(raw: RawEvidence) -> list[tuple[str, str]]:
    return github_host_evidence_from_provenance(raw.live_provenance, "live_provenance")


def github_host_payload_evidence_for_raw(raw: RawEvidence) -> list[tuple[str, str]]:
    evidence: list[tuple[str, str]] = []
    evidence.extend(github_host_evidence_from_payload(raw.pr, "pr"))
    evidence.extend(github_host_evidence_from_payload(raw.final_pr, "final_pr"))
    return evidence


def github_host_evidence_from_operations(operations: Any, label: str) -> list[tuple[str, str]]:
    if not isinstance(operations, list):
        return []
    evidence: list[tuple[str, str]] = []
    for idx, operation in enumerate(operations):
        if not isinstance(operation, Mapping):
            continue
        operation_label = f"{label}[{idx + 1}]"
        for key in ("host", "github_host"):
            evidence.extend(github_host_evidence_from_value(operation.get(key), f"{operation_label}.{key}"))
        for key in (*GITHUB_URL_KEYS, *GITHUB_IDENTITY_URL_KEYS):
            value = operation.get(key)
            evidence.extend(github_host_evidence_from_value(value, f"{operation_label}.{key}", require_url_like=True))
            evidence.extend(github_host_evidence_from_payload(value, f"{operation_label}.{key}"))
    return evidence


def resolve_github_host(evidence: Sequence[tuple[str, str]], *, default_host: str = "") -> str:
    hosts = {canonical_github_host(host) for _, host in evidence if canonical_github_host(host)}
    if len(hosts) == 1:
        return next(iter(hosts))
    if hosts:
        return ""
    return canonical_github_host(default_host)


def github_host_policy_messages(
    evidence: Sequence[tuple[str, str]],
    *,
    require_host: bool,
    context: str,
) -> list[str]:
    entries: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for label, raw_host in evidence:
        host = canonical_github_host(raw_host)
        if not host:
            continue
        item = (str(label), host)
        if item not in seen:
            seen.add(item)
            entries.append(item)
    messages: list[str] = []
    if require_host and not entries:
        messages.append(f"{context} requires approved GitHub host provenance")
    for label, host in entries:
        if host not in APPROVED_GITHUB_HOSTS:
            messages.append(f"{label} {host} is not an approved GitHub host")
    hosts = sorted({host for _, host in entries})
    if len(hosts) > 1:
        messages.append(f"{context} has conflicting GitHub hosts: {', '.join(hosts)}")
    return messages


def github_host_for_raw(raw: RawEvidence) -> str:
    default_host = "" if is_live_source(raw.source) else "github.com"
    provenance_evidence = github_host_provenance_evidence_for_raw(raw)
    if is_live_source(raw.source):
        return resolve_github_host(provenance_evidence, default_host=default_host)
    return resolve_github_host([*provenance_evidence, *github_host_payload_evidence_for_raw(raw)], default_host=default_host)


def validate_live_github_host(raw: RawEvidence) -> list[dict[str, Any]]:
    messages = github_host_policy_messages(
        github_host_provenance_evidence_for_raw(raw),
        require_host=True,
        context="live GitHub import",
    )
    approved_host = resolve_github_host(github_host_provenance_evidence_for_raw(raw))
    payload_evidence = [*github_host_payload_evidence_for_raw(raw), *github_host_evidence_from_operations(raw.operations, "operation")]
    for message in github_host_policy_messages(
        payload_evidence,
        require_host=False,
        context="live GitHub payload",
    ):
        messages.append(message)
    for label, raw_host in payload_evidence:
        payload_host = canonical_github_host(raw_host)
        if approved_host and payload_host and payload_host != approved_host:
            messages.append(f"{label} host does not match approved GitHub provenance")
    return [
        blocking_problem(message, rule="github-live-provenance")
        for message in messages
    ]


def validate_transcript_github_host_evidence(
    *,
    transcript_payload: Mapping[str, Any],
    summary: Mapping[str, Any],
    pr_payload: Any = None,
    operations: Any = None,
) -> tuple[str, list[str]]:
    transcript_provenance = transcript_payload.get("live_provenance") if isinstance(transcript_payload.get("live_provenance"), Mapping) else {}
    summary_provenance = summary.get("live_provenance") if isinstance(summary.get("live_provenance"), Mapping) else {}
    transcript_evidence: list[tuple[str, str]] = []
    transcript_evidence.extend(github_host_evidence_from_value(transcript_payload.get("github_host"), "operation_transcript.github_host"))
    transcript_evidence.extend(github_host_evidence_from_provenance(transcript_provenance, "operation_transcript.live_provenance"))
    evidence: list[tuple[str, str]] = list(transcript_evidence)
    evidence.extend(github_host_evidence_from_value(summary.get("github_host"), "summary.github_host"))
    evidence.extend(github_host_evidence_from_provenance(summary_provenance, "summary.live_provenance"))
    messages = github_host_policy_messages(evidence, require_host=True, context="GitHub operation transcript")
    if not any(canonical_github_host(host) for _, host in transcript_evidence):
        messages.append("GitHub operation transcript requires approved GitHub host provenance")
    github_host = resolve_github_host(evidence)
    payload_evidence = [
        *github_host_evidence_from_payload(pr_payload, "pr"),
        *github_host_evidence_from_operations(operations, "operation_transcript.operations"),
    ]
    for message in github_host_policy_messages(
        payload_evidence,
        require_host=False,
        context="GitHub PR payload",
    ):
        messages.append(message)
    for label, raw_host in payload_evidence:
        payload_host = canonical_github_host(raw_host)
        if github_host and payload_host and payload_host != github_host:
            messages.append(f"{label} host does not match approved GitHub provenance")
    return github_host, messages


def validate_transcript_github_host(
    *,
    transcript_payload: Mapping[str, Any],
    summary: Mapping[str, Any],
    pr_payload: Any = None,
    operations: Any = None,
) -> tuple[str, list[dict[str, Any]]]:
    github_host, messages = validate_transcript_github_host_evidence(
        transcript_payload=transcript_payload,
        summary=summary,
        pr_payload=pr_payload,
        operations=operations,
    )
    return github_host, [blocking_problem(message, rule="github-live-provenance") for message in messages]


def gh_api_endpoint_is_absolute(endpoint: str) -> bool:
    parsed = urllib.parse.urlsplit(str(endpoint or "").strip())
    return bool(parsed.scheme or parsed.netloc)


def validate_gh_hostname(tokens: Sequence[str], *, github_host: str = "") -> list[dict[str, Any]]:
    hostnames = option_values(tokens, {"--hostname"})
    if not hostnames:
        return []
    expected_host = canonical_github_host(github_host)
    problems: list[dict[str, Any]] = []
    if len(hostnames) > 1:
        problems.append(command_problem("gh --hostname must be provided at most once"))
    for hostname in hostnames:
        host = canonical_github_host(hostname)
        if not host:
            problems.append(command_problem("gh --hostname must name a GitHub host"))
        elif host not in APPROVED_GITHUB_HOSTS:
            problems.append(command_problem(f"gh --hostname {hostname} is not an approved GitHub host"))
        elif expected_host and host != expected_host:
            problems.append(command_problem("gh --hostname does not match recorded GitHub provenance"))
    return problems


def gh_api_endpoint_index(tokens: Sequence[str]) -> int:
    skip_next = False
    for idx, token in enumerate([str(item) for item in tokens[2:]], start=2):
        if skip_next:
            skip_next = False
            continue
        if token in GH_API_VALUE_FLAGS:
            skip_next = True
            continue
        if token.startswith(("-H", "-f", "-F", "-X")) and len(token) > 2:
            continue
        if token in GH_API_FLAG_ONLY or token.startswith(GH_API_VALUE_PREFIXES):
            continue
        if token.startswith("-"):
            continue
        return idx
    return -1


def gh_api_endpoint(tokens: Sequence[str]) -> str:
    idx = gh_api_endpoint_index(tokens)
    if idx < 0:
        return ""
    return str(tokens[idx])


def gh_api_endpoint_query_problems(endpoint: str) -> list[dict[str, Any]]:
    parsed = urllib.parse.urlsplit(str(endpoint or "").strip())
    if not parsed.query:
        return []
    problems: list[dict[str, Any]] = []
    if any(part == "" for part in parsed.query.split("&")):
        problems.append(command_problem("gh api endpoint query parameter <empty> is not allowlisted"))
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        key_text = str(key or "").strip()
        if live_common.sensitive_key_name(key_text):
            problems.append(command_problem(f"gh api endpoint query parameter {key_text or '<empty>'} is sensitive"))
        elif key_text not in SAFE_GH_API_QUERY_PARAMS:
            problems.append(command_problem(f"gh api endpoint query parameter {key_text or '<empty>'} is not allowlisted"))
        else:
            value_text = str(value or "").strip()
            lower, upper = SAFE_GH_API_QUERY_BOUNDS[key_text]
            if not re.fullmatch(r"\d+", value_text):
                problems.append(command_problem(f"gh api endpoint query parameter {key_text} must be a decimal integer"))
            else:
                numeric = int(value_text)
                if numeric < lower or numeric > upper:
                    problems.append(command_problem(f"gh api endpoint query parameter {key_text} must be between {lower} and {upper}"))
    return problems


def redact_gh_api_field_value(raw: str) -> str:
    text = str(raw or "")
    if "=" not in text:
        return "[REDACTED_SECRET]"
    key, _value = text.split("=", 1)
    return f"{key}=[REDACTED_SECRET]"


def redact_gh_api_field_arguments(tokens: Sequence[str]) -> tuple[list[str], dict[str, int]]:
    report = {"gh_api_field_values": 0}
    out = [str(token) for token in tokens]
    idx = 0
    while idx < len(out):
        token = out[idx]
        if token in GH_API_FIELD_FLAGS and idx + 1 < len(out):
            out[idx + 1] = redact_gh_api_field_value(out[idx + 1])
            report["gh_api_field_values"] += 1
            idx += 2
            continue
        if token.startswith(("--field=", "--raw-field=")):
            flag, value = token.split("=", 1)
            out[idx] = f"{flag}={redact_gh_api_field_value(value)}"
            report["gh_api_field_values"] += 1
        elif token.startswith(("-f", "-F")) and len(token) > 2:
            out[idx] = f"{token[:2]}{redact_gh_api_field_value(token[2:])}"
            report["gh_api_field_values"] += 1
        idx += 1
    return out, report


def redact_gh_api_endpoint_query_values(endpoint: str) -> tuple[str, dict[str, int]]:
    report = {"gh_api_query_values": 0}
    raw = str(endpoint or "")
    parsed = urllib.parse.urlsplit(raw)
    if not parsed.query:
        return raw, report
    pairs: list[tuple[str, str]] = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        key_text = str(key or "")
        if key_text in SAFE_GH_API_QUERY_PARAMS and not live_common.sensitive_key_name(key_text):
            pairs.append((key_text, value))
        else:
            pairs.append((key_text, "[REDACTED_SECRET]"))
            report["gh_api_query_values"] += 1
    query = urllib.parse.urlencode(pairs, doseq=True)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment)), report


def redact_gh_api_command_query_values(value: Any) -> tuple[Any, dict[str, int]]:
    report = {"gh_api_query_values": 0}

    def clean(item: Any) -> Any:
        if isinstance(item, list):
            tokens = [str(child) for child in item]
            if len(tokens) >= 2 and tokens[0] == "gh" and tokens[1] == "api":
                idx = gh_api_endpoint_index(tokens)
                if idx >= 0:
                    tokens[idx], local_report = redact_gh_api_endpoint_query_values(tokens[idx])
                    report["gh_api_query_values"] += int(local_report.get("gh_api_query_values") or 0)
                tokens, field_report = redact_gh_api_field_arguments(tokens)
                report["gh_api_field_values"] = report.get("gh_api_field_values", 0) + int(field_report.get("gh_api_field_values") or 0)
                return tokens
            return [clean(child) for child in item]
        if isinstance(item, dict):
            return {str(key): clean(child) for key, child in item.items()}
        return item

    return clean(value), report


def approved_github_url_parts(value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return []
    if canonical_github_host(parsed.hostname or "") not in APPROVED_GITHUB_HOSTS:
        return []
    return [urllib.parse.unquote(part) for part in parsed.path.strip("/").split("/") if part]


def github_url_identity_messages(value: Any, label: str, *, require_url: bool = False) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    text = value.strip()
    if not require_url and not is_url_like(text):
        return []
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return [f"{label} must be an absolute https GitHub URL"]
    host = canonical_github_host(parsed.hostname or "")
    if host not in APPROVED_GITHUB_HOSTS:
        return [f"{label} {host or '<missing>'} is not an approved GitHub host"]
    return []


def display_command(command: Sequence[str]) -> str:
    return shlex.join([str(item) for item in command])


def trusted_proof_command(command: Sequence[str]) -> list[str]:
    actual = [str(item) for item in command]
    if actual and actual[0] == "python3":
        actual[0] = sys.executable
    if len(actual) > 1 and actual[1] == "scripts/star_forge.py":
        actual[1] = str(STAR_FORGE_SCRIPT)
    return actual


def normalize_gh_api_endpoint(endpoint: str) -> str:
    raw = str(endpoint or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme and parsed.netloc:
        raw = parsed.path
    else:
        raw = raw.split("?", 1)[0]
    return urllib.parse.unquote(raw).strip("/")


def id_values(*values: Any) -> set[str]:
    out: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"none", "null"}:
            out.add(text)
    return out


def actions_ids_from_url(value: Any) -> tuple[set[str], set[str]]:
    if not isinstance(value, str) or not value.strip():
        return set(), set()
    parsed = urllib.parse.urlsplit(value.strip())
    parts = [urllib.parse.unquote(part) for part in parsed.path.strip("/").split("/") if part]
    run_ids: set[str] = set()
    job_ids: set[str] = set()
    for idx, part in enumerate(parts):
        if part == "runs" and idx + 1 < len(parts):
            run_ids.add(parts[idx + 1])
        if part in {"job", "jobs"} and idx + 1 < len(parts):
            job_ids.add(parts[idx + 1])
    return run_ids, job_ids


def pr_bound_ci_ids(check_runs: Any, *, captured_head: str) -> dict[str, set[str]]:
    ids = {"check_runs": set(), "runs": set(), "jobs": set()}
    captured = str(captured_head or "").strip()
    if not captured:
        return ids
    top_head = first_text(check_runs.get("head_sha"), check_runs.get("headSha")) if isinstance(check_runs, Mapping) else ""
    items, _, _ = flatten_paginated(check_runs, ("check_runs", "checks", "runs", "nodes"))
    for item in items:
        if not isinstance(item, Mapping):
            continue
        run_head = check_head_sha(item, top_head)
        if run_head != captured:
            continue
        ids["check_runs"].update(id_values(item.get("id"), item.get("databaseId"), item.get("check_run_id"), item.get("checkRunId")))
        ids["runs"].update(
            id_values(
                item.get("run_id"),
                item.get("runId"),
                item.get("workflow_run_id"),
                item.get("workflowRunId"),
                item.get("workflow_run_database_id"),
                item.get("workflowRunDatabaseId"),
                nested(item, "run", "id"),
                nested(item, "workflow_run", "id"),
                nested(item, "workflowRun", "id"),
                nested(item, "workflowRun", "databaseId"),
            )
        )
        ids["jobs"].update(
            id_values(
                item.get("job_id"),
                item.get("jobId"),
                item.get("workflow_job_id"),
                item.get("workflowJobId"),
                nested(item, "job", "id"),
                nested(item, "workflow_job", "id"),
                nested(item, "workflowJob", "id"),
                nested(item, "workflowJob", "databaseId"),
            )
        )
        for key in ("url", "html_url", "htmlUrl", "details_url", "detailsUrl"):
            run_ids, job_ids = actions_ids_from_url(item.get(key))
            ids["runs"].update(run_ids)
            ids["jobs"].update(job_ids)
    return ids


def ci_log_identity_fields(item: Any) -> dict[str, str]:
    if not isinstance(item, Mapping):
        return {
            "repo": "",
            "pr": "",
            "captured_head_sha": "",
            "check_run_id": "",
            "run_id": "",
            "job_id": "",
        }
    identity = item.get("identity")
    identity_map = identity if isinstance(identity, Mapping) else {}
    return {
        "repo": first_text(
            item.get("repo"),
            item.get("repository"),
            nested(item, "repository", "full_name"),
            nested(item, "repository", "nameWithOwner"),
            identity_map.get("repo"),
            identity_map.get("repository"),
            nested(identity_map, "repository", "full_name"),
            nested(identity_map, "repository", "nameWithOwner"),
        ),
        "pr": first_text(
            item.get("pr"),
            item.get("pull_request_number"),
            nested(item, "pull_request", "number"),
            nested(item, "pullRequest", "number"),
            identity_map.get("pr"),
            identity_map.get("pull_request_number"),
            nested(identity_map, "pull_request", "number"),
            nested(identity_map, "pullRequest", "number"),
        ),
        "captured_head_sha": first_text(
            item.get("captured_head_sha"),
            item.get("head_sha"),
            item.get("headSha"),
            nested(item, "commit", "sha"),
            identity_map.get("captured_head_sha"),
            identity_map.get("head_sha"),
            identity_map.get("headSha"),
            nested(identity_map, "commit", "sha"),
        ),
        "check_run_id": first_text(
            item.get("check_run_id"),
            item.get("checkRunId"),
            nested(item, "check_run", "id"),
            nested(item, "checkRun", "id"),
            identity_map.get("check_run_id"),
            identity_map.get("checkRunId"),
            nested(identity_map, "check_run", "id"),
            nested(identity_map, "checkRun", "id"),
        ),
        "run_id": first_text(
            item.get("run_id"),
            item.get("runId"),
            item.get("workflow_run_id"),
            item.get("workflowRunId"),
            nested(item, "run", "id"),
            nested(item, "workflow_run", "id"),
            nested(item, "workflowRun", "id"),
            identity_map.get("run_id"),
            identity_map.get("runId"),
            identity_map.get("workflow_run_id"),
            identity_map.get("workflowRunId"),
            nested(identity_map, "run", "id"),
            nested(identity_map, "workflow_run", "id"),
            nested(identity_map, "workflowRun", "id"),
        ),
        "job_id": first_text(
            item.get("job_id"),
            item.get("jobId"),
            item.get("workflow_job_id"),
            item.get("workflowJobId"),
            nested(item, "job", "id"),
            nested(item, "workflow_job", "id"),
            nested(item, "workflowJob", "id"),
            identity_map.get("job_id"),
            identity_map.get("jobId"),
            identity_map.get("workflow_job_id"),
            identity_map.get("workflowJobId"),
            nested(identity_map, "job", "id"),
            nested(identity_map, "workflow_job", "id"),
            nested(identity_map, "workflowJob", "id"),
        ),
    }


def ci_log_problem(message: str, *, rule: str, path: str = "") -> dict[str, Any]:
    if rule == "github-command":
        return command_problem(message)
    return blocking_problem(message, rule=rule, path=path)


def validate_ci_log_identity(
    item: Any,
    *,
    repo: str,
    pr_number: str,
    captured_head: str,
    check_runs: Any,
    label: str = "CI log entry",
    rule: str = "github-logs",
    path: str = "",
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    if not isinstance(item, Mapping):
        return [ci_log_problem(f"{label} must be a structured object", rule=rule, path=path)]

    identity = ci_log_identity_fields(item)
    expected_repo = str(repo or "").strip()
    expected_pr = str(pr_number or "").strip()
    expected_head = str(captured_head or "").strip()
    actual_repo = identity["repo"]
    actual_pr = identity["pr"]
    actual_head = identity["captured_head_sha"]

    if not actual_repo:
        problems.append(ci_log_problem(f"{label} requires repository identity", rule=rule, path=path))
    elif expected_repo and actual_repo != expected_repo:
        problems.append(ci_log_problem(f"{label} repository does not match the requested PR", rule=rule, path=path))
    if not actual_pr:
        problems.append(ci_log_problem(f"{label} requires PR identity", rule=rule, path=path))
    elif expected_pr and actual_pr != expected_pr:
        problems.append(ci_log_problem(f"{label} PR does not match the requested PR", rule=rule, path=path))
    if not expected_head:
        problems.append(ci_log_problem(f"{label} cannot be bound without a captured head SHA", rule=rule, path=path))
    if not actual_head:
        problems.append(ci_log_problem(f"{label} requires captured head SHA identity", rule=rule, path=path))
    elif expected_head and actual_head != expected_head:
        problems.append(ci_log_problem(f"{label} captured head SHA does not match the requested PR", rule=rule, path=path))

    supplied_ids = {
        "check_run_id": identity["check_run_id"],
        "run_id": identity["run_id"],
        "job_id": identity["job_id"],
    }
    if not any(supplied_ids.values()):
        problems.append(ci_log_problem(f"{label} requires a check_run_id, run_id, or job_id", rule=rule, path=path))
        return problems

    bound_ids = pr_bound_ci_ids(check_runs, captured_head=expected_head)
    checks = {
        "check_run_id": ("check_runs", "check run id"),
        "run_id": ("runs", "workflow run id"),
        "job_id": ("jobs", "workflow job id"),
    }
    for field, value in supplied_ids.items():
        if not value:
            continue
        bucket, human = checks[field]
        if value not in bound_ids[bucket]:
            problems.append(ci_log_problem(f"{label} {human} is not bound to the requested PR head SHA", rule=rule, path=path))
    return problems


def validate_ci_log_excerpt_payload(
    payload: Any,
    *,
    repo: str,
    pr_number: str,
    captured_head: str,
    check_runs: Any,
    path: str = "",
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    if not isinstance(payload, Mapping):
        return [blocking_problem("CI log excerpts must be a JSON object", rule="github-logs", path=path)]
    logs = payload.get("logs")
    if not isinstance(logs, list) or not logs:
        return [blocking_problem("CI log excerpts must contain at least one log entry", rule="github-logs", path=path)]
    for idx, entry in enumerate(logs):
        problems.extend(
            validate_ci_log_identity(
                entry,
                repo=repo,
                pr_number=pr_number,
                captured_head=captured_head,
                check_runs=check_runs,
                label=f"CI log entry {idx + 1}",
                rule="github-logs",
                path=path,
            )
        )
    return problems


def gh_api_endpoint_allowed(endpoint: str, *, repo: str, pr_number: str, check_runs: Any = None, captured_head: str = "") -> bool:
    normalized = normalize_gh_api_endpoint(endpoint)
    repo_norm = str(repo or "").strip().strip("/")
    pr_norm = str(pr_number or "").strip()
    if not normalized or not repo_norm or not pr_norm:
        return False
    repo_prefix = f"repos/{repo_norm}"
    pull_prefix = f"{repo_prefix}/pulls/{pr_norm}"
    if normalized == pull_prefix:
        return True
    if normalized in {f"{pull_prefix}/{suffix}" for suffix in ("files", "reviews", "comments", "commits")}:
        return True
    if normalized == f"{repo_prefix}/issues/{pr_norm}/comments":
        return True
    escaped_repo = re.escape(repo_prefix)
    commit_match = re.fullmatch(rf"{escaped_repo}/commits/([^/]+)/check-runs", normalized)
    if commit_match:
        return bool(captured_head and commit_match.group(1) == captured_head)
    bound_ids = pr_bound_ci_ids(check_runs, captured_head=captured_head)
    check_match = re.fullmatch(rf"{escaped_repo}/check-runs/([^/]+)/annotations", normalized)
    if check_match:
        return check_match.group(1) in bound_ids["check_runs"]
    run_match = re.fullmatch(rf"{escaped_repo}/actions/runs/([^/]+)/(?:logs|jobs)", normalized)
    if run_match:
        return run_match.group(1) in bound_ids["runs"]
    job_match = re.fullmatch(rf"{escaped_repo}/actions/jobs/([^/]+)/logs", normalized)
    if job_match:
        return job_match.group(1) in bound_ids["jobs"]
    return False


def gh_pr_view_identity(tokens: Sequence[str]) -> tuple[str, str]:
    command_pr = ""
    skip_next = False
    value_flags = {"--repo", "-R", "--json", "--jq", "--template", "--hostname"}
    for token in [str(item) for item in tokens[3:]]:
        if skip_next:
            skip_next = False
            continue
        if token in value_flags:
            skip_next = True
            continue
        if token.startswith(("--repo=", "--json=", "--jq=", "--template=", "--hostname=")):
            continue
        if token.startswith("-"):
            continue
        command_pr = token
        break
    return command_pr, option_value(tokens, {"--repo", "-R"})


def gh_run_view_identity(tokens: Sequence[str]) -> tuple[str, str]:
    run_id = ""
    skip_next = False
    value_flags = {"--repo", "-R", "--json", "--jq", "--template", "--hostname", "--attempt"}
    value_prefixes = ("--repo=", "--json=", "--jq=", "--template=", "--hostname=", "--attempt=")
    for token in [str(item) for item in tokens[3:]]:
        if skip_next:
            skip_next = False
            continue
        if token in value_flags:
            skip_next = True
            continue
        if token.startswith(value_prefixes):
            continue
        if token.startswith("-"):
            continue
        run_id = token
        break
    return run_id, option_value(tokens, {"--repo", "-R"})


def gh_api_endpoint_allows_ampersand(tokens: Sequence[str], token_index: int) -> bool:
    if len(tokens) < 2 or tokens[0] != "gh" or tokens[1] != "api":
        return False
    endpoint_index = gh_api_endpoint_index(tokens)
    if endpoint_index != token_index:
        return False
    endpoint = str(tokens[token_index])
    parsed = urllib.parse.urlsplit(endpoint.strip())
    if not parsed.query or "&" not in parsed.query:
        return False
    if gh_api_endpoint_is_absolute(endpoint):
        return False
    return not gh_api_endpoint_query_problems(endpoint)


def validate_no_shell_control(tokens: Sequence[str]) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    items = [str(token) for token in tokens]
    for idx, token in enumerate(items):
        has_control = bool(GH_COMMAND_SHELL_CONTROL_RE.search(token))
        has_unsafe_ampersand = "&" in token and not gh_api_endpoint_allows_ampersand(items, idx)
        if has_control or has_unsafe_ampersand:
            problems.append(command_problem("gh command must not contain shell control tokens"))
            break
    return problems


def parse_gh_option_grammar(
    command_name: str,
    tokens: Sequence[str],
    *,
    value_flags: set[str],
    value_prefixes: Sequence[str],
    flag_only: set[str],
    attached_short_value_flags: Sequence[str] = (),
    forbidden_value_flags: set[str] | None = None,
    forbidden_value_prefixes: Sequence[str] = (),
    forbidden_attached_short_value_flags: Sequence[str] = (),
) -> tuple[list[str], dict[str, list[str]], list[dict[str, Any]]]:
    positionals: list[str] = []
    values: dict[str, list[str]] = {}
    problems: list[dict[str, Any]] = []
    items = [str(item) for item in tokens]
    forbidden_flags = forbidden_value_flags or set()
    idx = 0
    while idx < len(items):
        token = items[idx]
        if token in forbidden_flags:
            problems.append(command_problem(f"{command_name} flag {token} is not read-only allowlisted"))
            idx += 2 if idx + 1 < len(items) else 1
            continue
        matched_forbidden_prefix = next((prefix for prefix in forbidden_value_prefixes if token.startswith(prefix)), "")
        if matched_forbidden_prefix:
            problems.append(command_problem(f"{command_name} flag {matched_forbidden_prefix[:-1]} is not read-only allowlisted"))
            idx += 1
            continue
        matched_forbidden_short = next((flag for flag in forbidden_attached_short_value_flags if token.startswith(flag) and len(token) > len(flag)), "")
        if matched_forbidden_short:
            problems.append(command_problem(f"{command_name} flag {matched_forbidden_short} is not read-only allowlisted"))
            idx += 1
            continue
        if token in value_flags:
            if idx + 1 >= len(items):
                problems.append(command_problem(f"{command_name} flag {token} requires a value"))
                idx += 1
                continue
            values.setdefault(token, []).append(items[idx + 1])
            idx += 2
            continue
        matched_prefix = next((prefix for prefix in value_prefixes if token.startswith(prefix)), "")
        if matched_prefix:
            values.setdefault(matched_prefix[:-1], []).append(token.split("=", 1)[1])
            idx += 1
            continue
        matched_short = next((flag for flag in attached_short_value_flags if token.startswith(flag) and len(token) > len(flag)), "")
        if matched_short:
            values.setdefault(matched_short, []).append(token[len(matched_short):])
            idx += 1
            continue
        if token in flag_only:
            idx += 1
            continue
        if token.startswith("-"):
            problems.append(command_problem(f"{command_name} flag {token} is not read-only allowlisted"))
            idx += 1
            continue
        positionals.append(token)
        idx += 1
    return positionals, values, problems


def require_exactly_one_positional(command_name: str, positionals: Sequence[str], subject: str) -> list[dict[str, Any]]:
    if not positionals:
        return [command_problem(f"{command_name} must name {subject}")]
    if len(positionals) > 1:
        return [command_problem(f"{command_name} has extra positional arguments after {subject}")]
    return []


def validate_gh_command(
    argv: Sequence[str],
    *,
    repo: str = "",
    pr_number: str = "",
    check_runs: Any = None,
    captured_head: str = "",
    github_host: str = "",
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    tokens = [str(item) for item in argv]
    if not tokens or tokens[0] != "gh":
        return [command_problem("fixture command must be a gh argv array")]
    if len(tokens) < 2:
        return [command_problem("gh command is missing a subcommand")]
    problems.extend(validate_no_shell_control(tokens))
    top = tokens[1]
    sub = tokens[2] if len(tokens) > 2 else ""
    problems.extend(validate_gh_hostname(tokens, github_host=github_host))
    if top == "pr":
        if sub != "view":
            reason = "gh pr checkout is forbidden" if sub == "checkout" else f"gh pr {sub or '<missing>'} is not read-only allowlisted"
            problems.append(command_problem(reason))
        if "--web" in tokens:
            problems.append(command_problem("gh pr view --web is not allowed for fixture evidence"))
        if sub == "view":
            positionals, _values, grammar_problems = parse_gh_option_grammar(
                "gh pr view",
                tokens[3:],
                value_flags=GH_PR_VIEW_VALUE_FLAGS,
                value_prefixes=GH_PR_VIEW_VALUE_PREFIXES,
                flag_only=set(),
            )
            problems.extend(grammar_problems)
            problems.extend(require_exactly_one_positional("gh pr view", positionals, "the requested PR"))
            command_pr = positionals[0] if positionals else ""
            command_repo = option_value(tokens, {"--repo", "-R"})
            if pr_number and not command_pr:
                problems.append(command_problem("gh pr view must name the requested PR"))
            elif pr_number and command_pr != str(pr_number):
                problems.append(command_problem("gh pr view PR does not match --pr"))
            if repo and not command_repo:
                problems.append(command_problem("gh pr view must name the requested repo with --repo"))
            elif repo and command_repo != repo:
                problems.append(command_problem("gh pr view repo does not match --repo"))
        return problems
    if top == "api":
        method = "GET"
        method_explicit = False
        has_field_arg = False
        has_input_arg = False
        for idx, token in enumerate(tokens):
            if token in {"--method", "-X"} and idx + 1 < len(tokens):
                method = tokens[idx + 1].upper()
                method_explicit = True
            elif token.startswith("--method="):
                method = token.split("=", 1)[1].upper()
                method_explicit = True
            elif token.startswith("-X") and len(token) > 2:
                method = token[2:].upper()
                method_explicit = True
            if token in {"-H", "--header"} or token.startswith(("--header=", "-H")):
                problems.append(command_problem("gh api fixture commands must not include headers"))
            if token in GH_API_FIELD_FLAGS or token.startswith(GH_API_FIELD_PREFIXES):
                has_field_arg = True
            if token == "--input" or token.startswith("--input="):
                has_input_arg = True
        positionals, _values, grammar_problems = parse_gh_option_grammar(
            "gh api",
            tokens[2:],
            value_flags=GH_API_ALLOWED_VALUE_FLAGS,
            value_prefixes=GH_API_ALLOWED_VALUE_PREFIXES,
            flag_only=GH_API_ALLOWED_FLAG_ONLY,
            attached_short_value_flags=("-X",),
            forbidden_value_flags=GH_API_FORBIDDEN_VALUE_FLAGS,
            forbidden_value_prefixes=("--field=", "--header=", "--input=", "--raw-field="),
            forbidden_attached_short_value_flags=("-F", "-H", "-f"),
        )
        problems.extend(grammar_problems)
        problems.extend(require_exactly_one_positional("gh api", positionals, "one PR-scoped endpoint"))
        if method != "GET":
            problems.append(command_problem(f"gh api --method {method} is forbidden"))
        if has_input_arg:
            problems.append(command_problem("gh api fixture commands must not send input bodies"))
        if has_field_arg:
            problems.append(command_problem("gh api field arguments are not allowed for live evidence"))
        if any("mutation" in token.lower() for token in tokens):
            problems.append(command_problem("gh api GraphQL mutations are forbidden"))
        endpoint = positionals[0] if positionals else ""
        if not endpoint:
            problems.append(command_problem("gh api command is missing an endpoint"))
        elif gh_api_endpoint_is_absolute(endpoint):
            problems.append(command_problem("gh api fixture commands must use path-style endpoints, not absolute URLs"))
        else:
            problems.extend(gh_api_endpoint_query_problems(endpoint))
            if not gh_api_endpoint_allowed(
                endpoint,
                repo=repo,
                pr_number=str(pr_number),
                check_runs=check_runs,
                captured_head=captured_head,
            ):
                problems.append(command_problem(f"gh api endpoint {endpoint} is not PR-scoped for the requested repo and PR"))
        return problems
    if top == "run":
        if sub != "view":
            reason = f"gh run {sub or '<missing>'} is not read-only allowlisted"
            if sub in GH_RUN_MUTATIONS:
                reason = f"gh run {sub} is forbidden"
            problems.append(command_problem(reason))
        else:
            positionals, _values, grammar_problems = parse_gh_option_grammar(
                "gh run view",
                tokens[3:],
                value_flags=GH_RUN_VIEW_VALUE_FLAGS,
                value_prefixes=GH_RUN_VIEW_VALUE_PREFIXES,
                flag_only=GH_RUN_VIEW_FLAG_ONLY,
            )
            problems.extend(grammar_problems)
            problems.extend(require_exactly_one_positional("gh run view", positionals, "a workflow run id"))
            run_id = positionals[0] if positionals else ""
            command_repo = option_value(tokens, {"--repo", "-R"})
            if "--web" in tokens:
                problems.append(command_problem("gh run view --web is not allowed for fixture evidence"))
            if repo and not command_repo:
                problems.append(command_problem("gh run view must name the requested repo with --repo"))
            elif repo and command_repo != repo:
                problems.append(command_problem("gh run view repo does not match --repo"))
            if not run_id:
                problems.append(command_problem("gh run view must name a workflow run id"))
            elif not re.fullmatch(r"\d+", run_id):
                problems.append(command_problem("gh run view must use a numeric workflow run id"))
            else:
                bound_ids = pr_bound_ci_ids(check_runs, captured_head=captured_head)
                if run_id not in bound_ids["runs"]:
                    problems.append(command_problem("gh run view run id is not bound to the requested PR head SHA"))
        return problems
    if top in GH_TOP_LEVEL_MUTATIONS:
        problems.append(command_problem(f"gh {top} is not read-only allowlisted"))
    else:
        problems.append(command_problem(f"gh {top} is not in the read-only allowlist"))
    return problems


def connector_operation_repo_identity(operation: Mapping[str, Any]) -> str:
    repository = operation.get("repository")
    return first_text(
        operation.get("repo"),
        repository_identity(repository),
        repo_from_url(repository if isinstance(repository, str) else ""),
    )


def connector_operation_pr_identity(operation: Mapping[str, Any]) -> str:
    pull_request = operation.get("pull_request")
    pull_request_camel = operation.get("pullRequest")
    pull_request_map = pull_request if isinstance(pull_request, Mapping) else {}
    pull_request_camel_map = pull_request_camel if isinstance(pull_request_camel, Mapping) else {}
    pull_request_url = pull_request if isinstance(pull_request, str) else ""
    pull_request_camel_url = pull_request_camel if isinstance(pull_request_camel, str) else ""
    pull_request_text = pull_request if isinstance(pull_request, (int, str)) and not pr_from_url(pull_request) else ""
    pull_request_camel_text = pull_request_camel if isinstance(pull_request_camel, (int, str)) and not pr_from_url(pull_request_camel) else ""
    return first_text(
        operation.get("pr"),
        operation.get("pull_request_number"),
        operation.get("pullRequestNumber"),
        nested(pull_request_map, "number"),
        nested(pull_request_camel_map, "number"),
        pr_from_url(pull_request_url),
        pr_from_url(pull_request_camel_url),
        pull_request_text,
        pull_request_camel_text,
    )


def connector_operation_host_evidence(operation: Any, label: str) -> list[tuple[str, str]]:
    if not isinstance(operation, Mapping):
        return []
    evidence: list[tuple[str, str]] = []
    for key in ("host", "github_host"):
        evidence.extend(github_host_evidence_from_value(operation.get(key), f"{label}.{key}"))
    for key in (*GITHUB_URL_KEYS, *GITHUB_IDENTITY_URL_KEYS):
        value = operation.get(key)
        evidence.extend(github_host_evidence_from_value(value, f"{label}.{key}", require_url_like=True))
        evidence.extend(github_host_evidence_from_payload(value, f"{label}.{key}"))
    return evidence


def connector_operation_url_identity_items(operation: Mapping[str, Any]) -> list[tuple[str, str, bool]]:
    items: list[tuple[str, str, bool]] = []
    for key in (*GITHUB_URL_KEYS, *GITHUB_IDENTITY_URL_KEYS):
        value = operation.get(key)
        if isinstance(value, str) and value.strip():
            items.append((key, value, key in GITHUB_URL_KEYS))
        elif isinstance(value, Mapping):
            for nested_key in GITHUB_URL_KEYS:
                nested_value = value.get(nested_key)
                if isinstance(nested_value, str) and nested_value.strip():
                    items.append((f"{key}.{nested_key}", nested_value, True))
    return items


def validate_connector_operation_identity(
    operation: Any,
    *,
    repo: str,
    pr_number: str,
    github_host: str = "",
    label: str = "connector operation",
    require_identity: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(operation, Mapping):
        return []
    problems: list[dict[str, Any]] = []
    expected_repo = str(repo or "").strip()
    expected_pr = str(pr_number or "").strip()
    expected_host = canonical_github_host(github_host)
    declared_repo = connector_operation_repo_identity(operation)
    declared_pr = connector_operation_pr_identity(operation)
    if require_identity and expected_repo and not declared_repo:
        problems.append(command_problem(f"{label} must declare the requested repository"))
    if declared_repo and expected_repo and declared_repo != expected_repo:
        problems.append(command_problem(f"{label} repository does not match the requested PR"))
    if require_identity and expected_pr and not declared_pr:
        problems.append(command_problem(f"{label} must declare the requested PR"))
    if declared_pr and expected_pr and declared_pr != expected_pr:
        problems.append(command_problem(f"{label} PR does not match the requested PR"))
    for key, raw_url, require_url in connector_operation_url_identity_items(operation):
        for message in github_url_identity_messages(raw_url, f"{label} {key}", require_url=require_url):
            problems.append(command_problem(message))
        url_repo = repo_from_url(raw_url)
        url_pr = pr_from_url(raw_url)
        if url_repo and expected_repo and url_repo != expected_repo:
            problems.append(command_problem(f"{label} URL repository does not match the requested PR"))
        if url_pr and expected_pr and url_pr != expected_pr:
            problems.append(command_problem(f"{label} URL PR does not match the requested PR"))
    approved_host_evidence = False
    for evidence_label, raw_host in connector_operation_host_evidence(operation, label):
        host = canonical_github_host(raw_host)
        if not host:
            continue
        if host not in APPROVED_GITHUB_HOSTS:
            problems.append(command_problem(f"{evidence_label} {host} is not an approved GitHub host"))
        elif expected_host and host != expected_host:
            problems.append(command_problem(f"{evidence_label} does not match recorded GitHub provenance"))
        else:
            approved_host_evidence = True
    if require_identity and not approved_host_evidence:
        problems.append(command_problem(f"{label} must include approved GitHub host or URL evidence"))
    return problems


def validate_connector_operation(
    operation: Any,
    *,
    repo: str = "",
    pr_number: str = "",
    check_runs: Any = None,
    captured_head: str = "",
    github_host: str = "",
    require_identity: bool = False,
) -> list[dict[str, Any]]:
    if isinstance(operation, str):
        if require_identity:
            return [command_problem("connector operation must be a structured object with repo, PR, and host evidence")]
        name = operation
        action = "read"
    elif isinstance(operation, Mapping):
        name = str(operation.get("operation") or operation.get("name") or operation.get("kind") or "")
        action = str(operation.get("action") or operation.get("mode") or "read")
    else:
        return [command_problem("connector operation must be a string or object")]
    normalized = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    action_norm = action.lower()
    if action_norm not in {"read", "get", "list"}:
        return [command_problem(f"connector operation {name or '<missing>'} is not read-only")]
    if normalized not in CONNECTOR_READ_OPERATIONS:
        return [command_problem(f"connector operation {name or '<missing>'} is not read-only allowlisted")]
    identity_problems = validate_connector_operation_identity(
        operation,
        repo=repo,
        pr_number=pr_number,
        github_host=github_host,
        label=f"connector operation {name or '<missing>'}",
        require_identity=require_identity,
    )
    if normalized in {"logs", "ci_logs"}:
        if not isinstance(operation, Mapping):
            return [command_problem(f"connector operation {name or '<missing>'} must be structured with repo, PR, head SHA, and CI identity")]
        identity_problems.extend(validate_ci_log_identity(
            operation,
            repo=repo,
            pr_number=pr_number,
            captured_head=captured_head,
            check_runs=check_runs,
            label=f"connector operation {name or '<missing>'}",
            rule="github-command",
        ))
    return identity_problems


def validate_read_only(raw: RawEvidence, *, repo: str, pr_number: str, captured_head: str = "", github_host: str = "") -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    require_operation_identity = is_live_source(raw.source)
    for command in raw.commands:
        problems.extend(
            validate_gh_command(
                command,
                repo=repo,
                pr_number=str(pr_number),
                check_runs=raw.check_runs,
                captured_head=captured_head,
                github_host=github_host,
            )
        )
    for operation in raw.operations:
        problems.extend(
            validate_connector_operation(
                operation,
                repo=repo,
                pr_number=str(pr_number),
                check_runs=raw.check_runs,
                captured_head=captured_head,
                github_host=github_host,
                require_identity=require_operation_identity,
            )
        )
    return problems


def load_connector_fixture(path: Path) -> RawEvidence:
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        payload = {}
    pr_payload = payload.get("pr") or payload.get("pull_request") or {}
    final_pr = payload.get("final_pr") or payload.get("freshness") or pr_payload
    return RawEvidence(
        source="connector-fixture",
        pr=pr_payload if isinstance(pr_payload, Mapping) else {},
        final_pr=final_pr if isinstance(final_pr, Mapping) else {},
        diff=str(payload.get("diff") or ""),
        files=payload.get("files"),
        reviews=payload.get("reviews"),
        comments=payload.get("comments"),
        check_runs=payload.get("check_runs") or payload.get("checks"),
        annotations=payload.get("annotations"),
        logs=payload.get("logs") or payload.get("ci_logs"),
        commands=[],
        operations=list(payload.get("operations") or []),
        tool_versions=dict(payload.get("tool_versions") or {}),
        foundation_provenance=(
            dict(payload.get("foundation") or payload.get("foundation_provenance") or {})
            if isinstance(
                payload.get("foundation") or payload.get("foundation_provenance") or {},
                Mapping,
            )
            else {}
        ),
    )


def load_connector_input(path: Path) -> RawEvidence:
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        payload = {}
    pr_payload = payload.get("pr") or payload.get("pull_request") or {}
    final_pr = payload.get("final_pr") or payload.get("freshness") or {}
    provenance = payload.get("live_provenance") or payload.get("github_provenance") or payload.get("provenance") or {}
    return RawEvidence(
        source="github-connector-live",
        pr=pr_payload if isinstance(pr_payload, Mapping) else {},
        final_pr=final_pr if isinstance(final_pr, Mapping) else {},
        diff=str(payload.get("diff") or ""),
        files=payload.get("files"),
        reviews=payload.get("reviews"),
        comments=payload.get("comments"),
        check_runs=payload.get("check_runs") or payload.get("checks"),
        annotations=payload.get("annotations"),
        logs=payload.get("logs") or payload.get("ci_logs"),
        commands=parse_gh_commands(payload.get("commands")),
        operations=list(payload.get("operations") or []),
        tool_versions=dict(payload.get("tool_versions") or {}),
        live_provenance=provenance if isinstance(provenance, dict) else {},
        foundation_provenance=(
            dict(payload.get("foundation") or payload.get("foundation_provenance") or {})
            if isinstance(
                payload.get("foundation") or payload.get("foundation_provenance") or {},
                Mapping,
            )
            else {}
        ),
    )


def load_gh_fixture_dir(path: Path) -> RawEvidence:
    pr_payload = read_json(path / "pr-view.json", {})
    final_pr = read_json(path / "final-pr-view.json", pr_payload)
    logs = read_json(path / "ci-logs.json", None)
    if logs is None:
        log_dir = path / "logs"
        if log_dir.exists():
            logs = [{"name": item.name, "text": read_text(item)} for item in sorted(log_dir.glob("*.txt"))]
    tool_versions = read_json(path / "tool-versions.json", {})
    return RawEvidence(
        source="gh-fixture",
        pr=pr_payload if isinstance(pr_payload, Mapping) else {},
        final_pr=final_pr if isinstance(final_pr, Mapping) else {},
        diff=read_text(path / "diff.patch"),
        files=read_json(path / "files.json", None),
        reviews=read_json(path / "reviews.json", []),
        comments=read_json(path / "comments.json", []),
        check_runs=read_json(path / "check-runs.json", {}),
        annotations=read_json(path / "annotations.json", []),
        logs=logs,
        commands=parse_gh_commands(read_json(path / "commands.json", [])),
        operations=[],
        tool_versions=tool_versions if isinstance(tool_versions, dict) else {},
    )


def load_gh_readonly_dir(path: Path) -> RawEvidence:
    raw = load_gh_fixture_dir(path)
    provenance = read_json(path / "provenance.json", {})
    raw.source = "gh-readonly-live"
    if not (path / "final-pr-view.json").exists():
        raw.final_pr = {}
    raw.live_provenance = provenance if isinstance(provenance, dict) else {}
    foundation = read_json(path / "foundation.json", {})
    if not foundation and isinstance(provenance, Mapping):
        foundation = provenance.get("foundation") or provenance.get("foundation_provenance") or {}
    raw.foundation_provenance = dict(foundation) if isinstance(foundation, Mapping) else {}
    return raw


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int):
            return str(value)
    return ""


def nested(mapping: Mapping[str, Any], *keys: str) -> Any:
    item: Any = mapping
    for key in keys:
        if not isinstance(item, Mapping):
            return None
        item = item.get(key)
    return item


def extract_ref_sha(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, Mapping):
        return ""
    return first_text(
        value.get("sha"),
        value.get("oid"),
        value.get("id"),
        nested(value, "target", "oid"),
        nested(value, "target", "sha"),
        nested(value, "commit", "oid"),
        nested(value, "commit", "sha"),
    )


def extract_base_sha(pr_payload: Mapping[str, Any]) -> str:
    return first_text(
        pr_payload.get("base_sha"),
        pr_payload.get("baseRefOid"),
        nested(pr_payload, "base", "sha"),
        nested(pr_payload, "base", "oid"),
        nested(pr_payload, "baseRef", "target", "oid"),
        nested(pr_payload, "baseRef", "target", "sha"),
        extract_ref_sha(pr_payload.get("base")),
        extract_ref_sha(pr_payload.get("baseRef")),
    )


def extract_head_sha(pr_payload: Mapping[str, Any]) -> str:
    return first_text(
        pr_payload.get("head_sha"),
        pr_payload.get("headRefOid"),
        nested(pr_payload, "head", "sha"),
        nested(pr_payload, "head", "oid"),
        nested(pr_payload, "headRef", "target", "oid"),
        nested(pr_payload, "headRef", "target", "sha"),
        extract_ref_sha(pr_payload.get("head")),
        extract_ref_sha(pr_payload.get("headRef")),
    )


def extract_current_base_sha(pr_payload: Mapping[str, Any]) -> str:
    return first_text(
        pr_payload.get("current_base_sha"),
        pr_payload.get("currentBaseSha"),
        extract_base_sha(pr_payload),
    )


def extract_current_head_sha(pr_payload: Mapping[str, Any]) -> str:
    return first_text(
        pr_payload.get("current_head_sha"),
        pr_payload.get("currentHeadSha"),
        extract_head_sha(pr_payload),
    )


def extract_merge_base(raw: RawEvidence, pr_payload: Mapping[str, Any]) -> str:
    return first_text(
        pr_payload.get("merge_base_sha"),
        pr_payload.get("mergeBaseOid"),
        nested(pr_payload, "mergeBaseCommit", "oid"),
        nested(pr_payload, "merge_base", "sha"),
        nested(pr_payload, "mergeBase", "sha"),
        nested(raw.pr, "merge_base", "sha"),
        nested(raw.pr, "mergeBase", "sha"),
    )


def unwrap_edges(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, Mapping):
        return []
    if isinstance(value.get("nodes"), list):
        return list(value["nodes"])
    if isinstance(value.get("edges"), list):
        out: list[Any] = []
        for edge in value["edges"]:
            if isinstance(edge, Mapping) and "node" in edge:
                out.append(edge["node"])
            else:
                out.append(edge)
        return out
    return []


def pagination_flags(payload: Any, item_count: int) -> tuple[bool, bool]:
    partial = False
    incomplete = False
    if not isinstance(payload, Mapping):
        return partial, incomplete
    partial = bool(payload.get("partial_permissions") or payload.get("permission_partial") or payload.get("partial"))
    page_info = payload.get("page_info") or payload.get("pageInfo") or payload.get("pagination") or {}
    if isinstance(page_info, Mapping):
        incomplete = bool(
            page_info.get("has_next_page")
            or page_info.get("hasNextPage")
            or page_info.get("incomplete")
            or page_info.get("pagination_incomplete")
        )
        expected = page_info.get("expected_total_count") or page_info.get("total_count") or payload.get("total_count")
        if isinstance(expected, int) and expected > item_count:
            incomplete = True
    if payload.get("incomplete_results") or payload.get("pagination_incomplete"):
        incomplete = True
    return partial, incomplete


def flatten_paginated(payload: Any, keys: Sequence[str]) -> tuple[list[Any], bool, bool]:
    if payload is None:
        return [], False, False
    if isinstance(payload, list):
        return list(payload), False, False
    if not isinstance(payload, Mapping):
        return [], False, False
    pages = payload.get("pages")
    if isinstance(pages, list):
        items: list[Any] = []
        partial = bool(payload.get("partial_permissions") or payload.get("permission_partial"))
        incomplete = bool(payload.get("pagination_incomplete"))
        for idx, page in enumerate(pages):
            page_items = []
            if isinstance(page, Mapping):
                for key in keys:
                    if key in page:
                        page_items = unwrap_edges(page.get(key))
                        break
                if not page_items:
                    page_items = unwrap_edges(page)
                page_partial = bool(page.get("partial_permissions") or page.get("permission_partial") or page.get("partial"))
                page_incomplete = bool(page.get("pagination_incomplete") or page.get("incomplete_results"))
            else:
                page_items, page_partial, page_incomplete = flatten_paginated(page, keys)
            items.extend(page_items)
            partial = partial or page_partial
            incomplete = incomplete or page_incomplete
            if idx == len(pages) - 1 and isinstance(page, Mapping):
                page_info = page.get("page_info") or page.get("pageInfo") or {}
                if isinstance(page_info, Mapping) and (page_info.get("has_next_page") or page_info.get("hasNextPage")):
                    incomplete = True
        expected = payload.get("expected_total_count") or payload.get("total_count")
        if isinstance(expected, int) and expected > len(items):
            incomplete = True
        return items, partial, incomplete
    for key in keys:
        if key in payload:
            raw_items = unwrap_edges(payload.get(key))
            partial, incomplete = pagination_flags(payload, len(raw_items))
            return raw_items, partial, incomplete
    raw_items = unwrap_edges(payload)
    partial, incomplete = pagination_flags(payload, len(raw_items))
    return raw_items, partial, incomplete


def normalize_file_item(item: Any) -> dict[str, Any]:
    if isinstance(item, str):
        return {"filename": item}
    if not isinstance(item, Mapping):
        return {"filename": ""}
    return {
        "filename": first_text(item.get("filename"), item.get("path"), item.get("file")),
        "status": first_text(item.get("status"), item.get("changeType")),
        "additions": item.get("additions"),
        "deletions": item.get("deletions"),
        "changes": item.get("changes"),
    }


def normalize_files(raw: RawEvidence, pr_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = raw.files if raw.files is not None else pr_payload.get("files")
    items, _, _ = flatten_paginated(payload, ("files", "changed_files", "nodes"))
    return [normalize_file_item(item) for item in items]


def normalize_simple_list(payload: Any, keys: Sequence[str]) -> tuple[list[Any], bool, bool]:
    items, partial, incomplete = flatten_paginated(payload, keys)
    normalized: list[Any] = []
    for item in items:
        normalized.append(item if isinstance(item, Mapping) else {"body": str(item)})
    return normalized, partial, incomplete


def check_head_sha(item: Mapping[str, Any], top_head_sha: str) -> str:
    return first_text(
        item.get("head_sha"),
        item.get("headSha"),
        nested(item, "commit", "sha"),
        nested(item, "check_suite", "head_sha"),
        nested(item, "checkSuite", "headSha"),
        top_head_sha,
    )


def normalize_check_runs(payload: Any, captured_head: str, problems: list[dict[str, Any]]) -> tuple[dict[str, Any], bool, bool]:
    top_head = first_text(payload.get("head_sha"), payload.get("headSha")) if isinstance(payload, Mapping) else ""
    items, partial, incomplete = flatten_paginated(payload, ("check_runs", "checks", "runs", "nodes"))
    normalized: list[dict[str, Any]] = []
    if not items:
        problems.append(blocking_problem("GitHub PR evidence is missing check runs", rule="github-checks"))
    for idx, item in enumerate(items):
        if not isinstance(item, Mapping):
            problems.append(blocking_problem(f"check run {idx + 1} is malformed", rule="github-checks"))
            continue
        status = first_text(item.get("status")).lower()
        conclusion = first_text(item.get("conclusion")).lower()
        run_head = check_head_sha(item, top_head)
        if not run_head:
            problems.append(blocking_problem(f"check run {idx + 1} is missing head SHA binding", rule="github-checks"))
        elif captured_head and run_head != captured_head:
            problems.append(blocking_problem(f"check run {idx + 1} is bound to a different head SHA", rule="github-checks"))
        if not status:
            problems.append(blocking_problem(f"check run {idx + 1} is missing status", rule="github-checks"))
        elif status not in COMPLETED_STATUSES:
            problems.append(blocking_problem(f"check run {idx + 1} is not complete: {status}", rule="github-checks"))
        if status in COMPLETED_STATUSES and not conclusion:
            problems.append(blocking_problem(f"check run {idx + 1} is missing conclusion", rule="github-checks"))
        elif conclusion in BLOCKING_CONCLUSIONS or (conclusion and conclusion not in SUCCESSFUL_CONCLUSIONS):
            problems.append(blocking_problem(f"check run {idx + 1} conclusion is {conclusion}", rule="github-checks"))
        elif status in PENDING_STATUSES:
            problems.append(blocking_problem(f"check run {idx + 1} is pending", rule="github-checks"))
        normalized.append(
            {
                "id": item.get("id") or item.get("databaseId"),
                "run_id": first_text(
                    item.get("run_id"),
                    item.get("runId"),
                    item.get("workflow_run_id"),
                    item.get("workflowRunId"),
                    nested(item, "workflow_run", "id"),
                    nested(item, "workflowRun", "id"),
                    nested(item, "workflowRun", "databaseId"),
                ),
                "job_id": first_text(
                    item.get("job_id"),
                    item.get("jobId"),
                    item.get("workflow_job_id"),
                    item.get("workflowJobId"),
                    nested(item, "workflow_job", "id"),
                    nested(item, "workflowJob", "id"),
                    nested(item, "workflowJob", "databaseId"),
                ),
                "name": first_text(item.get("name"), item.get("displayName"), item.get("workflowName")),
                "status": status,
                "conclusion": conclusion,
                "head_sha": run_head,
                "started_at": first_text(item.get("started_at"), item.get("startedAt")),
                "completed_at": first_text(item.get("completed_at"), item.get("completedAt")),
                "url": first_text(item.get("url"), item.get("details_url"), item.get("detailsUrl")),
            }
        )
    if partial:
        problems.append(blocking_problem("check runs are permission-partial", rule="github-permissions"))
    if incomplete:
        problems.append(blocking_problem("check runs pagination is incomplete", rule="github-pagination"))
    return {"head_sha": top_head or captured_head, "partial_permissions": partial, "pagination_incomplete": incomplete, "check_runs": normalized}, partial, incomplete


def normalize_logs(logs: Any, *, include: bool, max_log_bytes: int, problems: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, int]]:
    report: dict[str, int] = {}
    if not include:
        return None, report
    limit = max(0, min(int(max_log_bytes), HARD_MAX_LOG_BYTES))
    if limit <= 0:
        problems.append(blocking_problem("max log bytes must be greater than zero", rule="github-logs"))
        return {"max_log_bytes": limit, "logs": []}, report
    entries: list[Any]
    if logs is None:
        entries = []
    elif isinstance(logs, list):
        entries = logs
    else:
        entries = [logs]
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(entries):
        if isinstance(item, Mapping):
            name = first_text(item.get("name"), item.get("check_name"), f"log-{idx + 1}")
            text = str(item.get("text") or item.get("log") or item.get("body") or "")
            identity = ci_log_identity_fields(item)
        else:
            name = f"log-{idx + 1}"
            text = str(item)
            identity = {}
        raw = text.encode("utf-8", errors="replace")
        clipped = raw[:limit].decode("utf-8", errors="replace")
        redacted, redaction_report = redact_artifact_payload(clipped)
        report = merge_reports(report, redaction_report)
        excerpt_text = str(redacted)
        excerpt_bytes = excerpt_text.encode("utf-8", errors="replace")
        entry = {
            "name": name,
            "original_sha256": hashlib.sha256(raw).hexdigest(),
            "original_bytes": len(raw),
            "captured_bytes": len(raw[:limit]),
            "excerpt_sha256": hashlib.sha256(excerpt_bytes).hexdigest(),
            "excerpt_bytes": len(excerpt_bytes),
            "max_log_bytes": limit,
            "truncated": len(raw) > limit,
            "text": excerpt_text,
        }
        for field in ("repo", "pr", "captured_head_sha", "check_run_id", "run_id", "job_id"):
            value = str(identity.get(field) or "").strip()
            if value:
                entry[field] = value
        out.append(entry)
    if not out:
        problems.append(blocking_problem("CI logs were requested but no log fixture was available", rule="github-logs"))
    return {"max_log_bytes": limit, "logs": out}, report


def normalize_pr_payload(
    *,
    raw: RawEvidence,
    repo: str,
    pr_number: str,
    captured_base: str,
    captured_head: str,
    current_base: str,
    current_head: str,
    merge_base: str,
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    source = raw.pr
    return {
        "repo": repo,
        "number": pr_number,
        "title": first_text(source.get("title")),
        "state": first_text(source.get("state")),
        "author": source.get("author"),
        "url": first_text(source.get("url"), source.get("html_url")),
        "base_sha": captured_base,
        "head_sha": captured_head,
        "current_base_sha": current_base,
        "current_head_sha": current_head,
        "merge_base_sha": merge_base,
        "changed_files": files,
    }


def artifact_write_json(path: Path, payload: Any) -> tuple[Path, dict[str, int]]:
    redacted, report = redact_artifact_payload(payload)
    return write_json(path, redacted), report


def artifact_write_text(path: Path, text: str) -> tuple[Path, dict[str, int]]:
    redacted, report = redact_artifact_payload(text)
    return write_text(path, str(redacted)), report


def has_fixture_marker(value: Any) -> bool:
    text = str(value or "").lower()
    return "fixture" in text or text in {"connector-fixture", "gh-fixture", "missing-fixture"}


def is_live_source(source: str) -> bool:
    return source in {"github-live", "github-cli-live", "github-connector-live", "gh-readonly-live", "connector-readonly-live"}


def repository_identity(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        return text if re.fullmatch(r"[^/\s]+/[^/\s]+", text) else ""
    if not isinstance(value, Mapping):
        return ""
    return first_text(
        value.get("full_name"),
        value.get("fullName"),
        value.get("nameWithOwner"),
        value.get("name_with_owner"),
        value.get("repo"),
        value.get("repository"),
    )


def payload_repository_identity(payload: Mapping[str, Any]) -> str:
    return first_text(
        payload.get("repo"),
        payload.get("repository_full_name"),
        payload.get("repositoryFullName"),
        payload.get("nameWithOwner"),
        repository_identity(payload.get("repository")),
        repository_identity(nested(payload, "base", "repo")),
    )


def repo_from_url(value: Any) -> str:
    parts = approved_github_url_parts(value)
    if len(parts) >= 3 and parts[0] == "repos":
        return f"{parts[1]}/{parts[2]}"
    if len(parts) >= 2 and parts[0] not in {"pull", "pulls", "issues"}:
        return f"{parts[0]}/{parts[1]}"
    return ""


def pr_from_url(value: Any) -> str:
    parts = approved_github_url_parts(value)
    if len(parts) >= 5 and parts[0] == "repos" and parts[3] in {"pull", "pulls"}:
        return parts[4]
    if len(parts) >= 4 and parts[2] in {"pull", "pulls"}:
        return parts[3]
    return ""


def validate_payload_identity(payload: Mapping[str, Any], *, repo: str, pr_number: str, label: str) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    payload_repo = payload_repository_identity(payload)
    if payload_repo and payload_repo != repo:
        problems.append(blocking_problem(f"{label} repository identity does not match --repo", rule="github-live-provenance"))
    payload_pr = first_text(payload.get("number"), payload.get("pr"), payload.get("pull_request"))
    if payload_pr and payload_pr != str(pr_number):
        problems.append(blocking_problem(f"{label} PR metadata does not match --pr", rule="github-live-provenance"))
    for key in ("url", "html_url", "web_url", "pull_request_url", "pullRequestUrl"):
        raw_url = payload.get(key)
        for message in github_url_identity_messages(raw_url, f"{label} {key}", require_url=True):
            problems.append(blocking_problem(message, rule="github-live-provenance"))
        url_repo = repo_from_url(raw_url)
        url_pr = pr_from_url(raw_url)
        if url_repo and url_repo != repo:
            problems.append(blocking_problem(f"{label} URL repository does not match --repo", rule="github-live-provenance"))
        if url_pr and url_pr != str(pr_number):
            problems.append(blocking_problem(f"{label} URL PR does not match --pr", rule="github-live-provenance"))
    return problems


def validate_live_import(
    raw: RawEvidence,
    *,
    repo: str,
    pr_number: str,
    captured_base: str,
    captured_head: str,
    current_base: str,
    current_head: str,
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    if not is_live_source(raw.source):
        return problems
    if not raw.tool_versions or any(has_fixture_marker(key) or has_fixture_marker(value) for key, value in raw.tool_versions.items()):
        problems.append(blocking_problem("live GitHub import requires non-fixture tool versions", rule="github-live-provenance"))
    provenance = raw.live_provenance
    if not provenance:
        problems.append(blocking_problem("live GitHub import requires provenance metadata", rule="github-live-provenance"))
    problems.extend(validate_live_github_host(raw))
    collected_at = first_text(provenance.get("collected_at"), provenance.get("captured_at"))
    if not collected_at:
        problems.append(blocking_problem("live GitHub import requires a collection timestamp", rule="github-live-provenance"))
    source = first_text(provenance.get("source"))
    if not source:
        problems.append(blocking_problem("live GitHub provenance requires a source", rule="github-live-provenance"))
    elif source != raw.source:
        problems.append(blocking_problem("live GitHub provenance source does not match the import mode", rule="github-live-provenance"))
    provenance_repo = first_text(provenance.get("repo"), provenance.get("repository"))
    if not provenance_repo:
        problems.append(blocking_problem("live GitHub provenance requires repo matching --repo", rule="github-live-provenance"))
    elif provenance_repo != repo:
        problems.append(blocking_problem("live GitHub provenance repo does not match --repo", rule="github-live-provenance"))
    provenance_pr = first_text(provenance.get("pr"), provenance.get("pull_request"), provenance.get("number"))
    if not provenance_pr:
        problems.append(blocking_problem("live GitHub provenance requires PR matching --pr", rule="github-live-provenance"))
    elif provenance_pr != str(pr_number):
        problems.append(blocking_problem("live GitHub provenance PR does not match --pr", rule="github-live-provenance"))
    problems.extend(validate_payload_identity(raw.pr, repo=repo, pr_number=str(pr_number), label="live GitHub PR metadata"))
    if not raw.final_pr:
        problems.append(blocking_problem("live GitHub import requires explicit final PR or freshness payload", rule="github-live-provenance"))
    else:
        problems.extend(validate_payload_identity(raw.final_pr, repo=repo, pr_number=str(pr_number), label="live GitHub final PR metadata"))
        if not extract_current_base_sha(raw.final_pr) or not extract_current_head_sha(raw.final_pr):
            problems.append(blocking_problem("live GitHub import requires final freshness base and head refs", rule="github-live-provenance"))
    if not raw.commands and not raw.operations:
        problems.append(blocking_problem("live GitHub import requires read-only command or connector operations", rule="github-live-provenance"))
    if not all([captured_base, captured_head, current_base, current_head]):
        problems.append(blocking_problem("live GitHub import requires base and head freshness refs", rule="github-live-provenance"))
    return problems


def operation_transcript_payload(
    *,
    raw: RawEvidence,
    repo: str,
    pr_number: str,
    github_host: str,
    captured_base: str,
    captured_head: str,
    current_base: str,
    current_head: str,
    merge_base: str,
    partial_permissions: bool,
    pagination_incomplete: bool,
) -> dict[str, Any]:
    provenance = dict(raw.live_provenance)
    return {
        "schema": "star-forge.github-operation-transcript.v1",
        "source": raw.source,
        "repo": repo,
        "pr": str(pr_number),
        "github_host": github_host,
        "collected_at": first_text(provenance.get("collected_at"), provenance.get("captured_at")),
        "imported_at": live_common.now_utc(),
        "live_provenance": provenance,
        "refs": {
            "captured_base_sha": captured_base,
            "current_base_sha": current_base,
            "captured_head_sha": captured_head,
            "current_head_sha": current_head,
            "merge_base_sha": merge_base,
        },
        "permission_state": {
            "partial_permissions": bool(partial_permissions),
        },
        "pagination_state": {
            "pagination_incomplete": bool(pagination_incomplete),
        },
        "operations": raw.operations,
        "commands": raw.commands,
        "allowlist": {
            "connector_operations": sorted(CONNECTOR_READ_OPERATIONS),
            "gh_top_level": ["api", "pr", "run"],
            "github_hosts": sorted(APPROVED_GITHUB_HOSTS),
        },
    }


def github_provider(raw: RawEvidence) -> str:
    """Identify the GitHub capability route that actually supplied the evidence."""

    provider = first_text(
        raw.foundation_provenance.get("provider"),
        nested(raw.foundation_provenance, "github_repository", "provider"),
    )
    if provider in {PREFERRED_PROVIDER, GH_READONLY_PROVIDER, GH_CREATE_PROVIDER}:
        return provider
    if raw.source.startswith("github-connector") or raw.source == "connector-fixture":
        return PREFERRED_PROVIDER
    if raw.source.startswith("gh-"):
        return GH_READONLY_PROVIDER
    return "github-unavailable"


def foundation_check_detail(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    checks = payload.get("checks")
    if isinstance(checks, Mapping):
        record = checks.get(name)
        if isinstance(record, Mapping):
            detail = record.get("detail")
            if isinstance(detail, Mapping):
                return detail
    direct = payload.get(name)
    if isinstance(direct, Mapping):
        detail = direct.get("detail")
        return detail if isinstance(detail, Mapping) else direct
    return {}


def github_remote_matches(remote_url: Any, repo: str) -> bool:
    if not isinstance(remote_url, str) or not remote_url.strip():
        return False
    if re.match(r"^https?://[^/@\s]+@", remote_url):
        return False
    escaped = re.escape(repo)
    return bool(
        re.fullmatch(rf"https://github\.com/{escaped}(?:\.git)?", remote_url)
        or re.fullmatch(rf"git@github\.com:{escaped}(?:\.git)?", remote_url)
    )


def normalize_foundation_provenance(
    raw: RawEvidence,
    *,
    repo: str,
    current_source_hash: str,
    problems: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return allowlisted Foundation identity and append honest blockers when supplied."""

    payload = raw.foundation_provenance
    if not payload:
        return {
            "applicable": False,
            "reason": "pull-request proof does not include Foundation evidence",
        }

    repository = foundation_check_detail(payload, "github_repository")
    remote = foundation_check_detail(payload, "remote_origin")
    branch = foundation_check_detail(payload, "default_branch")
    initial = foundation_check_detail(payload, "initial_commit")
    ci = foundation_check_detail(payload, "ci")
    source_hash = first_text(payload.get("source_hash"), payload.get("tree_source_hash"))
    provider = first_text(repository.get("provider"), payload.get("provider"))
    fallback = first_text(repository.get("fallback"), payload.get("fallback"))
    owner, _, name = repo.partition("/")
    repository_owner = first_text(repository.get("owner"), payload.get("owner"))
    repository_name = first_text(repository.get("name"), payload.get("name"))
    visibility = first_text(repository.get("visibility"), payload.get("visibility")).lower()
    remote_url = first_text(remote.get("url"), payload.get("remote_url"))
    default_branch = first_text(branch.get("name"), payload.get("default_branch"))
    head_commit = first_text(branch.get("head_commit"), payload.get("head_commit"))
    initial_commit = first_text(initial.get("sha"), payload.get("initial_commit"))
    current_head = first_text(initial.get("current_head"), payload.get("current_head"))
    tree_source_hash = first_text(
        initial.get("tree_source_hash"),
        payload.get("tree_source_hash"),
        source_hash,
    )
    ci_path = first_text(ci.get("path"), payload.get("ci_path"))
    ci_sha256 = first_text(ci.get("sha256"), payload.get("ci_sha256"))
    created = repository.get("created", payload.get("created"))

    def foundation_problem(message: str) -> None:
        problems.append(
            blocking_problem(message, rule="github-foundation-provenance")
        )

    if source_hash != current_source_hash:
        foundation_problem("Foundation evidence source hash does not match the current source")
    if provider not in {PREFERRED_PROVIDER, GH_READONLY_PROVIDER, GH_CREATE_PROVIDER}:
        foundation_problem("Foundation evidence does not identify an allowed GitHub provider")
    if provider == GH_CREATE_PROVIDER and fallback != GH_CREATE_FALLBACK:
        foundation_problem(
            "Foundation gh repository creation provenance must be exactly gh repo create --private"
        )
    if repository_owner != owner or repository_name != name:
        foundation_problem("Foundation repository identity does not match --repo")
    if visibility not in {"private", "public"}:
        foundation_problem("Foundation evidence requires verified repository visibility")
    if created is True and visibility != "private":
        foundation_problem("New GitHub Foundation repositories must be private")
    if provider == GH_READONLY_PROVIDER and created is True:
        foundation_problem("Read-only gh provenance cannot claim repository creation")
    if created is False and repository.get("visibility_changed") is not False:
        foundation_problem("Adopted repository provenance must prove visibility was unchanged")
    if repository.get("identity_verified") is not True:
        foundation_problem("Foundation evidence must verify repository identity")
    if repository.get("visibility_verified") is not True:
        foundation_problem("Foundation evidence must verify repository visibility")
    if not github_remote_matches(remote_url, repo):
        foundation_problem("Foundation origin URL does not match --repo")
    if first_text(remote.get("remote"), payload.get("remote")) != "origin":
        foundation_problem("Foundation GitHub remote must be named origin")
    if not default_branch or branch.get("exists") is not True:
        foundation_problem("Foundation evidence must prove the default branch")
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", head_commit):
        foundation_problem("Foundation default branch head commit is invalid")
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", initial_commit):
        foundation_problem("Foundation initial commit is invalid")
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", current_head):
        foundation_problem("Foundation current head is invalid")
    if head_commit and current_head and head_commit != current_head:
        foundation_problem("Foundation default branch head does not match current head")
    if tree_source_hash != current_source_hash:
        foundation_problem("Foundation initial commit is not bound to the current source")
    if not ci_path.startswith(".github/workflows/") or not ci_path.endswith((".yml", ".yaml")):
        foundation_problem("Foundation CI path is not a GitHub workflow YAML path")
    if not re.fullmatch(r"[0-9a-f]{64}", ci_sha256):
        foundation_problem("Foundation CI artifact SHA-256 is invalid")
    if ci.get("committed") is not True:
        foundation_problem("Foundation CI artifact must be committed")

    return {
        "applicable": True,
        "schema": first_text(payload.get("schema")) or "star-forge.foundation-provenance.v1",
        "source_hash": source_hash,
        "provider": provider,
        "provider_route": {
            "preferred_provider": PREFERRED_PROVIDER,
            "selected_provider": provider,
            "fallback": provider != PREFERRED_PROVIDER,
            "create_fallback": GH_CREATE_FALLBACK,
            "recorded_fallback": fallback,
        },
        "repository": {
            "owner": repository_owner,
            "name": repository_name,
            "full_name": repo,
            "visibility": visibility,
            "identity_verified": repository.get("identity_verified") is True,
            "visibility_verified": repository.get("visibility_verified") is True,
            "created": created if isinstance(created, bool) else None,
        },
        "remote": {
            "name": first_text(remote.get("remote"), payload.get("remote")),
            "url": remote_url,
        },
        "default_branch": {
            "name": default_branch,
            "exists": branch.get("exists") is True,
            "head_commit": head_commit,
        },
        "initial_commit": {
            "sha": initial_commit,
            "current_head": current_head,
            "tree_source_hash": tree_source_hash,
        },
        "ci": {
            "path": ci_path,
            "sha256": ci_sha256,
            "committed": ci.get("committed") is True,
        },
    }


def write_evidence_envelope(
    project: Path,
    manifest_path: Path,
    *,
    raw: RawEvidence,
    repo: str,
    pr_number: str,
    github_host: str,
    pr_url: str,
    captured_base: str,
    captured_head: str,
    current_base: str,
    current_head: str,
    foundation: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Adapt the v1 packet while preserving connector, PR, and Foundation identity."""

    manifest = read_json(manifest_path, {})
    provider = github_provider(raw)
    envelope = evidence.adapt_v1_manifest(
        manifest,
        capability=CAPABILITY,
        provider=provider,
    )
    provenance_payload = {
        **dict(envelope["provenance"]),
        "route": {
            "preferred_provider": PREFERRED_PROVIDER,
            "selected_provider": provider,
            "fallback": provider != PREFERRED_PROVIDER,
            "create_fallback": GH_CREATE_FALLBACK,
        },
        "provider": provider,
        "source": raw.source,
        "repository": {
            "full_name": repo,
            "owner": repo.partition("/")[0],
            "name": repo.partition("/")[2],
            "github_host": github_host,
        },
        "pull_request": {
            "number": str(pr_number),
            "url": pr_url,
            "captured_base_sha": captured_base,
            "current_base_sha": current_base,
            "captured_head_sha": captured_head,
            "current_head_sha": current_head,
        },
        "source_binding": {
            "source_hash": envelope["source_hash"],
            "runtime_asset_hash": envelope["runtime_asset_hash"],
        },
        "foundation": dict(foundation),
    }
    safe_provenance, _provenance_redactions = redact_artifact_payload(
        provenance_payload
    )
    envelope["provenance"] = safe_provenance
    if provider != PREFERRED_PROVIDER:
        envelope["blockers"].append(
            {
                "rule": "github-capability-fallback",
                "message": (
                    "GitHub connector evidence was not supplied; the recorded gh route "
                    "is read-only except for the separately authorized exact private-repository fallback"
                ),
                "capability": CAPABILITY,
                "preferred_provider": PREFERRED_PROVIDER,
                "selected_provider": provider,
                "allowed_create_fallback": GH_CREATE_FALLBACK,
                "blocking": False,
            }
        )
        if envelope["verdict"] == "PASS":
            envelope["verdict"] = "DEGRADED"
    if raw.source in {"connector-fixture", "gh-fixture", "missing-fixture"}:
        envelope["blockers"].append(
            {
                "rule": "github-fixture-provenance",
                "message": "Fixture GitHub evidence cannot satisfy live production proof",
                "blocking": True,
            }
        )
        envelope["verdict"] = "FAIL"
    envelope_path = manifest_path.parent / EVIDENCE_FILENAME
    written = evidence.write_envelope(
        envelope_path,
        envelope,
        project_root=project,
        verify_artifacts=True,
    )
    return envelope_path, written


def collect(args: argparse.Namespace) -> CollectionResult:
    project = Path(args.project).resolve()
    source_hash_before = live_common.compute_source_hash(project)
    problems: list[dict[str, Any]] = []
    input_modes = [
        bool(args.connector_fixture),
        bool(args.gh_fixture_dir),
        bool(args.connector_input),
        bool(args.gh_readonly_dir),
    ]
    if sum(1 for item in input_modes if item) != 1:
        problems.append(command_problem("provide exactly one of --connector-fixture, --gh-fixture-dir, --connector-input, or --gh-readonly-dir"))
        raw = RawEvidence("missing-fixture", {}, {}, "", None, None, None, None, None, None, [], [], {})
    elif args.connector_fixture:
        raw = load_connector_fixture(Path(args.connector_fixture))
    elif args.gh_fixture_dir:
        raw = load_gh_fixture_dir(Path(args.gh_fixture_dir))
    elif args.connector_input:
        raw = load_connector_input(Path(args.connector_input))
    else:
        raw = load_gh_readonly_dir(Path(args.gh_readonly_dir))
    if args.foundation_evidence:
        try:
            foundation_path = live_common.safe_project_path(
                project,
                args.foundation_evidence,
                must_exist=True,
            )
            foundation_payload = read_json(foundation_path, {})
            if not isinstance(foundation_payload, Mapping):
                raise ValueError("Foundation evidence must be a JSON object")
            raw.foundation_provenance = dict(foundation_payload)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            problems.append(
                blocking_problem(
                    f"Foundation evidence could not be imported safely: {exc}",
                    rule="github-foundation-provenance",
                )
            )

    initial_base = extract_base_sha(raw.pr)
    initial_head = extract_head_sha(raw.pr)
    captured_base = first_text(args.base, initial_base)
    captured_head = first_text(args.head, initial_head)
    github_host = github_host_for_raw(raw)
    problems.extend(validate_read_only(raw, repo=args.repo, pr_number=str(args.pr), captured_head=captured_head, github_host=github_host))
    if is_live_source(raw.source):
        current_base = extract_current_base_sha(raw.final_pr)
        current_head = extract_current_head_sha(raw.final_pr)
    else:
        current_base = extract_current_base_sha(raw.final_pr) or initial_base
        current_head = extract_current_head_sha(raw.final_pr) or initial_head
    merge_base = extract_merge_base(raw, raw.pr)

    if args.base and initial_base and args.base != initial_base:
        problems.append(blocking_problem("captured base SHA does not match PR metadata", rule="github-refs"))
    if args.head and initial_head and args.head != initial_head:
        problems.append(blocking_problem("captured head SHA does not match PR metadata", rule="github-refs"))
    missing_refs = not (captured_base and captured_head and current_base and current_head and merge_base)
    if missing_refs:
        problems.append(blocking_problem("GitHub PR evidence is missing base, head, current, or merge-base refs", rule="github-refs"))
    if captured_base and current_base and captured_base != current_base:
        problems.append(blocking_problem("GitHub PR base SHA changed after capture", rule="github-freshness"))
    if captured_head and current_head and captured_head != current_head:
        problems.append(blocking_problem("GitHub PR head SHA changed after capture", rule="github-freshness"))

    files = normalize_files(raw, raw.pr)
    reviews, reviews_partial, reviews_incomplete = normalize_simple_list(raw.reviews, ("reviews", "nodes"))
    comments, comments_partial, comments_incomplete = normalize_simple_list(raw.comments, ("comments", "review_comments", "nodes"))
    annotations, annotations_partial, annotations_incomplete = normalize_simple_list(raw.annotations, ("annotations", "nodes"))
    checks_payload, checks_partial, checks_incomplete = normalize_check_runs(raw.check_runs, captured_head, problems)
    partial_permissions = checks_partial or reviews_partial or comments_partial or annotations_partial
    pagination_incomplete = checks_incomplete or reviews_incomplete or comments_incomplete or annotations_incomplete
    if reviews_partial or comments_partial or annotations_partial:
        problems.append(blocking_problem("GitHub PR evidence reports partial permissions", rule="github-permissions"))
    if reviews_incomplete or comments_incomplete or annotations_incomplete:
        problems.append(blocking_problem("GitHub PR evidence reports incomplete pagination", rule="github-pagination"))
    problems.extend(
        validate_live_import(
            raw,
            repo=args.repo,
            pr_number=str(args.pr),
            captured_base=captured_base,
            captured_head=captured_head,
            current_base=current_base,
            current_head=current_head,
        )
    )

    log_payload, log_report = normalize_logs(
        raw.logs,
        include=bool(args.include_ci_logs),
        max_log_bytes=int(args.max_log_bytes),
        problems=problems,
    )
    log_identity_problems: list[dict[str, Any]] = []
    if log_payload is not None:
        log_identity_problems = validate_ci_log_excerpt_payload(
            log_payload,
            repo=args.repo,
            pr_number=str(args.pr),
            captured_head=captured_head,
            check_runs=checks_payload,
            path="ci-log-excerpts.json",
        )
        problems.extend(log_identity_problems)
        if log_identity_problems:
            log_payload = None

    root = live_common.live_collector_dir(project, args.task, COLLECTOR)
    redaction_report: dict[str, int] = dict(log_report)
    artifacts: dict[str, Path] = {}
    safe_read_only_commands, command_report = redact_gh_api_command_query_values(raw.commands)
    redaction_report = merge_reports(redaction_report, command_report)

    pr_payload = normalize_pr_payload(
        raw=raw,
        repo=args.repo,
        pr_number=str(args.pr),
        captured_base=captured_base,
        captured_head=captured_head,
        current_base=current_base,
        current_head=current_head,
        merge_base=merge_base,
        files=files,
    )
    pr_path, report = artifact_write_json(root / "pr.json", pr_payload)
    artifacts["pr"] = pr_path
    redaction_report = merge_reports(redaction_report, report)
    diff_path, report = artifact_write_text(root / "diff.patch", raw.diff or "")
    artifacts["diff"] = diff_path
    redaction_report = merge_reports(redaction_report, report)
    reviews_path, report = artifact_write_json(root / "reviews.json", {"reviews": reviews})
    artifacts["reviews"] = reviews_path
    redaction_report = merge_reports(redaction_report, report)
    comments_path, report = artifact_write_json(root / "comments.json", {"comments": comments})
    artifacts["comments"] = comments_path
    redaction_report = merge_reports(redaction_report, report)
    checks_path, report = artifact_write_json(root / "check-runs.json", checks_payload)
    artifacts["check-runs"] = checks_path
    redaction_report = merge_reports(redaction_report, report)
    annotations_path, report = artifact_write_json(root / "annotations.json", {"annotations": annotations})
    artifacts["annotations"] = annotations_path
    redaction_report = merge_reports(redaction_report, report)
    if log_payload is not None:
        logs_path, report = artifact_write_json(root / "ci-log-excerpts.json", log_payload)
        artifacts["ci-log-excerpts"] = logs_path
        redaction_report = merge_reports(redaction_report, report)
    transcript_payload = operation_transcript_payload(
        raw=raw,
        repo=args.repo,
        pr_number=str(args.pr),
        github_host=github_host,
        captured_base=captured_base,
        captured_head=captured_head,
        current_base=current_base,
        current_head=current_head,
        merge_base=merge_base,
        partial_permissions=partial_permissions,
        pagination_incomplete=pagination_incomplete,
    )
    transcript_path, report = artifact_write_json(root / "operation-transcript.json", transcript_payload)
    artifacts["operation-transcript"] = transcript_path
    redaction_report = merge_reports(redaction_report, report)
    transcript_sha256 = live_common.file_sha256(transcript_path)

    source_hash_after = live_common.compute_source_hash(project)
    foundation = normalize_foundation_provenance(
        raw,
        repo=args.repo,
        current_source_hash=source_hash_after,
        problems=problems,
    )
    summary_live_provenance = dict(raw.live_provenance)
    if github_host and github_host_provenance_evidence_for_raw(raw):
        summary_live_provenance.setdefault("github_host", github_host)
    summary_live_provenance["operation_transcript_sha256"] = transcript_sha256
    summary_live_provenance, provenance_report = redact_artifact_payload(
        summary_live_provenance
    )
    redaction_report = merge_reports(redaction_report, provenance_report)
    foundation, foundation_report = redact_artifact_payload(foundation)
    redaction_report = merge_reports(redaction_report, foundation_report)
    summary = {
        "adapter": "github-pr",
        "source": raw.source,
        "repo": args.repo,
        "pr": str(args.pr),
        "github_host": github_host,
        "captured_base_sha": captured_base,
        "current_base_sha": current_base,
        "captured_head_sha": captured_head,
        "current_head_sha": current_head,
        "merge_base_sha": merge_base,
        "changed_files_count": len(files),
        "review_count": len(reviews),
        "comment_count": len(comments),
        "check_run_count": len(checks_payload.get("check_runs", [])),
        "annotation_count": len(annotations),
        "ci_log_excerpt_count": len(log_payload.get("logs", [])) if isinstance(log_payload, dict) else 0,
        "logs_included": bool(log_payload),
        "missing_refs": missing_refs,
        "partial_permissions": partial_permissions,
        "pagination_incomplete": pagination_incomplete,
        "checks_bound_to_head": not any(item.get("rule") == "github-checks" and "head SHA" in str(item.get("message")) for item in problems),
        "read_only_operations": raw.operations,
        "read_only_commands": safe_read_only_commands,
        "read_only_transcript_sha256": transcript_sha256,
        "captured_at": first_text(raw.live_provenance.get("collected_at"), raw.live_provenance.get("captured_at")),
        "live_provenance": summary_live_provenance,
        "foundation": foundation,
    }
    tool_versions = {"adapter": "github-pr.v1", "source": raw.source}
    tool_versions.update(raw.tool_versions)
    safe_command_argv, argv_report = redact_artifact_payload(args.command_argv)
    redaction_report = merge_reports(redaction_report, argv_report)
    manifest_path = live_common.write_live_manifest(
        project,
        task=args.task,
        collector=COLLECTOR,
        command_argv=safe_command_argv,
        tool_versions=tool_versions,
        artifacts=artifacts,
        summary=summary,
        degraded=False,
        unavailable_capabilities=[],
        problems=problems,
        source_hash_before=source_hash_before,
        source_hash_after=source_hash_after,
        runtime_asset_hash=live_common.compute_runtime_asset_hash(project),
    )
    update_manifest_redaction_report(manifest_path, redaction_report)
    pr_url = first_text(
        raw.final_pr.get("url"),
        raw.final_pr.get("html_url"),
        raw.pr.get("url"),
        raw.pr.get("html_url"),
    )
    envelope_path, _envelope = write_evidence_envelope(
        project,
        manifest_path,
        raw=raw,
        repo=args.repo,
        pr_number=str(args.pr),
        github_host=github_host,
        pr_url=pr_url,
        captured_base=captured_base,
        captured_head=captured_head,
        current_base=current_base,
        current_head=current_head,
        foundation=foundation,
    )
    manifest_rel = live_common.project_relative(project, manifest_path)
    project_arg = live_common.project_cli_arg(project)
    fixture_sources = {"connector-fixture", "gh-fixture", "missing-fixture"}
    commands: list[list[str]] = []
    if raw.source not in fixture_sources:
        commands = [
            [
                "python3",
                "scripts/star_forge.py",
                "source-packet-github-pr-review",
                "--project",
                project_arg,
                "--input",
                manifest_rel,
                "--strict",
            ],
            [
                "python3",
                "scripts/star_forge.py",
                "source-packet-proof",
                "--project",
                project_arg,
                "--task",
                str(args.task),
                "--profile",
                "production-review",
                "--input",
                manifest_rel,
                "--strict",
            ],
        ]
    return CollectionResult(
        manifest_path=manifest_path,
        evidence_path=envelope_path,
        commands=commands,
        problems=problems,
    )


def record_proof_commands(result: CollectionResult, project: Path) -> int:
    for command in result.commands:
        proc = subprocess.run(
            trusted_proof_command(command),
            cwd=str(project),
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.stdout:
            print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="", file=sys.stderr)
        if proc.returncode != 0:
            return proc.returncode
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect read-only GitHub PR evidence for a Star Forge source packet")
    parser.add_argument("--project", default=".")
    parser.add_argument("--task", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--connector-fixture", default="")
    parser.add_argument("--gh-fixture-dir", default="")
    parser.add_argument("--connector-input", default="")
    parser.add_argument("--gh-readonly-dir", default="")
    parser.add_argument("--foundation-evidence", default="")
    parser.add_argument("--include-ci-logs", action="store_true")
    parser.add_argument("--max-log-bytes", type=int, default=DEFAULT_MAX_LOG_BYTES)
    parser.add_argument("--record", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    args = parser.parse_args(raw_argv)
    args.command_argv = ["github_pr.py", *raw_argv]
    result = collect(args)
    print("Wrote GitHub PR source packet manifest:")
    print(live_common.project_relative(Path(args.project).resolve(), result.manifest_path))
    print("Wrote GitHub evidence envelope:")
    print(live_common.project_relative(Path(args.project).resolve(), result.evidence_path))
    if result.commands:
        print("Source packet proof commands:")
        for command in result.commands:
            print(display_command(command))
    else:
        print("Fixture-only evidence was written; production proof commands were not emitted.")
    if args.record:
        if not result.commands:
            print("Record skipped because fixture-only evidence cannot satisfy production proof.", file=sys.stderr)
            return 1
        return record_proof_commands(result, Path(args.project).resolve())
    return 1 if result.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
