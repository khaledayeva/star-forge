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
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from live_collectors import common as live_common


COLLECTOR = "github"
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


@dataclass
class CollectionResult:
    manifest_path: Path
    commands: list[str]
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
    path_cleaned, path_report = normalize_abs_paths(value)
    secret_cleaned, secret_report = live_common.redact_sensitive_values(path_cleaned)
    return secret_cleaned, merge_reports(path_report, secret_report)


def update_manifest_redaction_report(manifest_path: Path, report: Mapping[str, int]) -> None:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing = payload.get("redaction_report")
    merged = merge_reports(existing if isinstance(existing, dict) else {}, report)
    payload["redaction_report"] = merged
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def shell_argv(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return shlex.split(raw)
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


def gh_api_endpoint(tokens: Sequence[str]) -> str:
    skip_next = False
    for token in [str(item) for item in tokens[2:]]:
        if skip_next:
            skip_next = False
            continue
        if token in GH_API_VALUE_FLAGS:
            skip_next = True
            continue
        if token.startswith("-X") and len(token) > 2:
            continue
        if token in GH_API_FLAG_ONLY or token.startswith(GH_API_VALUE_PREFIXES):
            continue
        if token.startswith("-"):
            continue
        return token
    return ""


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


def validate_gh_command(
    argv: Sequence[str],
    *,
    repo: str = "",
    pr_number: str = "",
    check_runs: Any = None,
    captured_head: str = "",
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    tokens = [str(item) for item in argv]
    if not tokens or tokens[0] != "gh":
        return [command_problem("fixture command must be a gh argv array")]
    if len(tokens) < 2:
        return [command_problem("gh command is missing a subcommand")]
    top = tokens[1]
    sub = tokens[2] if len(tokens) > 2 else ""
    if top == "pr":
        if sub != "view":
            reason = "gh pr checkout is forbidden" if sub == "checkout" else f"gh pr {sub or '<missing>'} is not read-only allowlisted"
            problems.append(command_problem(reason))
        if "--web" in tokens:
            problems.append(command_problem("gh pr view --web is not allowed for fixture evidence"))
        if sub == "view":
            command_pr, command_repo = gh_pr_view_identity(tokens)
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
            if token in {"-H", "--header"} or token.startswith("--header="):
                problems.append(command_problem("gh api fixture commands must not include headers"))
            if token in {"-f", "-F", "--field", "--raw-field"} or token.startswith(("--field=", "--raw-field=")):
                has_field_arg = True
            if token == "--input" or token.startswith("--input="):
                has_input_arg = True
        if method != "GET":
            problems.append(command_problem(f"gh api --method {method} is forbidden"))
        if has_input_arg:
            problems.append(command_problem("gh api fixture commands must not send input bodies"))
        if has_field_arg and not method_explicit:
            problems.append(command_problem("gh api field arguments require an explicit GET method"))
        if any("mutation" in token.lower() for token in tokens):
            problems.append(command_problem("gh api GraphQL mutations are forbidden"))
        endpoint = gh_api_endpoint(tokens)
        if not endpoint:
            problems.append(command_problem("gh api command is missing an endpoint"))
        elif not gh_api_endpoint_allowed(
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
            run_id, command_repo = gh_run_view_identity(tokens)
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


def validate_connector_operation(
    operation: Any,
    *,
    repo: str = "",
    pr_number: str = "",
    check_runs: Any = None,
    captured_head: str = "",
) -> list[dict[str, Any]]:
    if isinstance(operation, str):
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
    if normalized in {"logs", "ci_logs"}:
        if not isinstance(operation, Mapping):
            return [command_problem(f"connector operation {name or '<missing>'} must be structured with repo, PR, head SHA, and CI identity")]
        return validate_ci_log_identity(
            operation,
            repo=repo,
            pr_number=pr_number,
            captured_head=captured_head,
            check_runs=check_runs,
            label=f"connector operation {name or '<missing>'}",
            rule="github-command",
        )
    return []


def validate_read_only(raw: RawEvidence, *, repo: str, pr_number: str, captured_head: str = "") -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for command in raw.commands:
        problems.extend(
            validate_gh_command(
                command,
                repo=repo,
                pr_number=str(pr_number),
                check_runs=raw.check_runs,
                captured_head=captured_head,
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
    )


def load_connector_input(path: Path) -> RawEvidence:
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        payload = {}
    pr_payload = payload.get("pr") or payload.get("pull_request") or {}
    final_pr = payload.get("final_pr") or payload.get("freshness") or pr_payload
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
    raw.live_provenance = provenance if isinstance(provenance, dict) else {}
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
    if not isinstance(value, str) or not value.strip():
        return ""
    parsed = urllib.parse.urlsplit(value.strip())
    parts = [urllib.parse.unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if len(parts) >= 3 and parts[0] == "repos":
        return f"{parts[1]}/{parts[2]}"
    if len(parts) >= 2 and parts[0] not in {"pull", "pulls", "issues"}:
        return f"{parts[0]}/{parts[1]}"
    return ""


def pr_from_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    parsed = urllib.parse.urlsplit(value.strip())
    parts = [urllib.parse.unquote(part) for part in parsed.path.strip("/").split("/") if part]
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
    if raw.final_pr is not raw.pr:
        problems.extend(validate_payload_identity(raw.final_pr, repo=repo, pr_number=str(pr_number), label="live GitHub final PR metadata"))
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
        },
    }


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

    initial_base = extract_base_sha(raw.pr)
    initial_head = extract_head_sha(raw.pr)
    captured_base = first_text(args.base, initial_base)
    captured_head = first_text(args.head, initial_head)
    problems.extend(validate_read_only(raw, repo=args.repo, pr_number=str(args.pr), captured_head=captured_head))
    current_base = extract_base_sha(raw.final_pr) or initial_base
    current_head = extract_head_sha(raw.final_pr) or initial_head
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
    summary_live_provenance = dict(raw.live_provenance)
    summary_live_provenance["operation_transcript_sha256"] = transcript_sha256
    summary = {
        "adapter": "github-pr",
        "source": raw.source,
        "repo": args.repo,
        "pr": str(args.pr),
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
        "read_only_commands": raw.commands,
        "read_only_transcript_sha256": transcript_sha256,
        "captured_at": first_text(raw.live_provenance.get("collected_at"), raw.live_provenance.get("captured_at")),
        "live_provenance": summary_live_provenance,
    }
    tool_versions = {"adapter": "github-pr.v1", "source": raw.source}
    tool_versions.update(raw.tool_versions)
    manifest_path = live_common.write_live_manifest(
        project,
        task=args.task,
        collector=COLLECTOR,
        command_argv=args.command_argv,
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
    manifest_rel = live_common.project_relative(project, manifest_path)
    project_arg = "." if project == Path.cwd().resolve() else str(project)
    fixture_sources = {"connector-fixture", "gh-fixture", "missing-fixture"}
    commands = []
    if raw.source not in fixture_sources:
        commands = [
            f"python3 scripts/star_forge.py source-packet-github-pr-review --project {shlex.quote(project_arg)} --input {shlex.quote(manifest_rel)} --strict",
            f"python3 scripts/star_forge.py source-packet-proof --project {shlex.quote(project_arg)} --task {shlex.quote(args.task)} --profile production-review --input {shlex.quote(manifest_rel)} --strict",
        ]
    return CollectionResult(manifest_path=manifest_path, commands=commands, problems=problems)


def record_proof_commands(result: CollectionResult, project: Path) -> int:
    script = SCRIPT_DIR / "star_forge.py"
    manifest_rel = live_common.project_relative(project, result.manifest_path)
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "source-packet-github-pr-review",
            "--project",
            str(project),
            "--input",
            manifest_rel,
            "--strict",
        ],
        text=True,
        check=False,
    )
    return proc.returncode


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
    if result.commands:
        print("Source packet proof commands:")
        for command in result.commands:
            print(command)
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
