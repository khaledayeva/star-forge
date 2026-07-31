#!/usr/bin/env python3
"""Provider-neutral preview URL evidence collector for Star Forge.

This collector performs read-only HTTP checks for an existing preview URL,
writes task-scoped artifacts, and prints the strict proof command that should
consume those artifacts. It never deploys, calls provider CLIs, or writes to
remote services.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import shlex
import socket
import sys
import time
import urllib.parse
from functools import partial
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
PLUGIN_ROOT = SCRIPTS_ROOT.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from live_collectors import browser_safety, common
from live_collectors.policy_data import policy_dict, policy_list
from live_collectors.provider_engine import render_descriptor
from starforge import evidence

COLLECTOR = "preview"
CAPABILITY = "preview-verification"
EVIDENCE_FILENAME = "evidence.v2.json"
STAR_FORGE = PLUGIN_ROOT / "scripts" / "star_forge.py"
SETTINGS = policy_dict("preview", "SETTINGS")
DEFAULT_USER_AGENT = SETTINGS["default_user_agent"]
LOCAL_HOSTNAMES = set(SETTINGS["local_hostnames"])
TEMPLATES = policy_dict("preview", "PAYLOAD_TEMPLATES")

git_head = common.git_head
git_status_path = common.git_status_path
source_tree_clean_at_head = common.source_tree_clean_at_head
dirty_paths_missing_from_source_snapshot = common.dirty_paths_missing_from_source_snapshot

evidence_provider = partial(common.sanitize_segment, fallback="provider-neutral")

def write_evidence_envelope(
    project: Path,
    manifest_path: Path,
    *,
    provider: str,
) -> tuple[Path, dict[str, Any]]:
    manifest = common.read_json(manifest_path)
    provider_id = evidence_provider(provider)
    envelope = evidence.adapt_v1_manifest(
        manifest, capability=CAPABILITY, provider=provider_id,
    )
    envelope["provenance"] = {
        **dict(envelope["provenance"]),
        **render_descriptor(TEMPLATES["evidence_provenance"], {"provider": provider_id}),
    }
    envelope_path = manifest_path.parent / EVIDENCE_FILENAME
    written = evidence.write_envelope(
        envelope_path, envelope, project_root=project, verify_artifacts=True,
    )
    return envelope_path, written

problem = partial(common.problem, include_empty_path=True)

sensitive_name = common.sensitive_key_name

def sensitive_value(value: str) -> bool:
    lowered = value.lower()
    return bool(common.SECRET_RE.search(value)) or lowered.startswith(
        ("bearer ", "basic ")
    ) or "authorization:" in lowered

def safe_url_for_artifact(url: str) -> str:
    parsed = urllib.parse.urlparse(url or "")
    if not parsed.scheme:
        return "[invalid-url]"
    cleaned, _ = common.redact_sensitive_values(url)
    return str(cleaned).replace("[REDACTED_SECRET]", "[REDACTED]").replace("%5BREDACTED_SECRET%5D", "[REDACTED]")

parse_ip = browser_safety.parse_ip

def is_blocked_ip(ip: ipaddress._BaseAddress, *, explicit_local_allowed: bool) -> str | None:
    if ip.is_loopback:
        if explicit_local_allowed:
            return None
        return "loopback targets require --server-lease or --local-preview-mode"
    reason = next((
        message for attribute, message in policy_list("preview", "BLOCKED_IP_RULES")
        if getattr(ip, attribute)
    ), None)
    return reason or ("non-global IP targets are not allowed" if not ip.is_global else None)

def _preview_lease_problems(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**item, "rule": "preview-localhost"} if item.get("rule") == "server-lease"
        else dict(item) for item in items
    ]

def validate_server_lease(project: Path, raw_lease: str, url: str, *, source_hash: str, runtime_hash: str) -> tuple[bool, list[dict[str, Any]]]:
    if not raw_lease:
        return False, []
    try:
        parsed_url, url_problems = browser_safety.validate_url(url)
        problems = _preview_lease_problems(url_problems)
        if url_problems:
            return False, problems
        _lease_path, payload, lease_problems = browser_safety.validate_server_lease(
            project, raw_lease, parsed_url, source_hash, runtime_hash,
        )
        problems.extend(_preview_lease_problems(lease_problems))
    except Exception as exc:
        return False, [problem(f"server lease is invalid: {exc}", rule="preview-localhost", path=str(raw_lease))]
    return payload is not None and not problems, problems

def validate_url_safety_with_ips(url: str, *, allow_local: bool) -> tuple[list[dict[str, Any]], set[str]]:
    problems: list[dict[str, Any]] = []
    try:
        parsed = urllib.parse.urlparse(url or "")
    except ValueError:
        return [problem("preview URL is malformed", rule="preview-url")], set()
    if parsed.scheme not in {"http", "https"}:
        return [problem("preview URL must use http or https", rule="preview-url")], set()
    if parsed.username or parsed.password:
        problems.append(problem("preview URL must not include credentials", rule="preview-url"))
    host = parsed.hostname or ""
    if not host:
        problems.append(problem("preview URL must include a host", rule="preview-url"))
        return problems, set()
    if browser_safety.is_metadata_host(host):
        problems.append(problem("preview URL must not target metadata hosts", rule="preview-url"))
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if sensitive_name(key) or sensitive_value(value):
            problems.append(problem("preview URL query appears to contain sensitive material", rule="preview-url"))
            break
    explicit_local = host.lower() in LOCAL_HOSTNAMES or bool(parse_ip(host) and parse_ip(host).is_loopback)
    if explicit_local and not allow_local:
        problems.append(problem("localhost preview URLs require --server-lease or --local-preview-mode", rule="preview-localhost"))
    try:
        port = parsed.port
    except ValueError:
        problems.append(problem("preview URL has an invalid port", rule="preview-url"))
        return problems, set()
    ips, resolve_problem = browser_safety.resolve_ips(host, port)
    if resolve_problem:
        problems.append(problem(
            resolve_problem.replace("browser URL", "preview URL"), rule="preview-url",
        ))
        return problems, set()
    for ip in ips:
        blocked = is_blocked_ip(ip, explicit_local_allowed=explicit_local and allow_local)
        if blocked:
            problems.append(problem(f"preview URL resolved to unsafe address {ip}: {blocked}", rule="preview-url"))
    return problems, {str(ip) for ip in ips}

def validate_url_safety(url: str, *, allow_local: bool) -> list[dict[str, Any]]:
    return validate_url_safety_with_ips(url, allow_local=allow_local)[0]

def host_header_for_url(parsed: urllib.parse.ParseResult) -> str:
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    return f"{host}:{parsed.port}" if parsed.port and parsed.port != default_port else host

def pinned_http_get(
    url: str, headers: Mapping[str, str], validated_ips: set[str],
    args: argparse.Namespace,
) -> tuple[int | None, dict[str, str], bytes, bool, str, list[dict[str, Any]]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return None, {}, b"", False, "", [
            problem("HTTPS preview fetch requires connection-time IP pinning and is fail-closed until SNI-safe pinning is available", rule="preview-url")
        ]
    if parsed.scheme != "http":
        return None, {}, b"", False, "", [problem("preview URL must use http or https", rule="preview-url")]
    try:
        connected_ip = str(min(
            map(ipaddress.ip_address, validated_ips),
            key=lambda ip: (ip.version, int(ip)),
        ))
    except ValueError:
        return None, {}, b"", False, "", [problem("no validated IP address is available for preview connection", rule="preview-url")]
    port = parsed.port or 80
    request_headers = {**headers, "Host": host_header_for_url(parsed)}
    conn = http.client.HTTPConnection(connected_ip, port, timeout=args.timeout)
    try:
        target = (parsed.path or "/") + (f";{parsed.params}" if parsed.params else "") + (f"?{parsed.query}" if parsed.query else "")
        conn.request("GET", target, headers=request_headers)
        response = conn.getresponse()
        raw_body = response.read(args.max_body_bytes + 1)
        truncated = len(raw_body) > args.max_body_bytes
        raw_body = raw_body[:args.max_body_bytes] if truncated else raw_body
        return int(response.status), {str(key): str(value) for key, value in response.getheaders()}, raw_body, truncated, connected_ip, []
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        return None, {}, b"", False, connected_ip, [problem(f"preview HTTP request failed: {exc}", rule="preview-http")]
    finally:
        conn.close()

def parse_headers(raw_headers: Sequence[str]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    problems: list[dict[str, Any]] = []
    for raw in raw_headers:
        name, sep, value = _header_parts(raw)
        if not sep or not name:
            problems.append(problem(f"header must use Name: value or Name=value: {raw}", rule="preview-header"))
            continue
        if sensitive_name(name) or sensitive_value(value):
            problems.append(problem(f"request header `{name}` appears to contain sensitive material", rule="preview-header"))
            continue
        headers[name] = value
    return headers, problems

def _header_parts(raw: str) -> tuple[str, str, str]:
    name, sep, value = raw.partition(":")
    if not sep:
        name, sep, value = raw.partition("=")
    return name.strip(), sep, value.strip()

def sanitize_headers(headers: Mapping[str, str]) -> tuple[dict[str, str], dict[str, int]]:
    ordered = sorted(headers.items(), key=lambda item: item[0].lower())
    pre_redactions = sum(
        sensitive_value(str(value)) and not sensitive_name(key) for key, value in ordered
    )
    prepared = {
        key: "[REDACTED]" if sensitive_value(str(value)) else str(value)[:500]
        for key, value in ordered
    }
    safe, report = common.redact_sensitive_values(prepared)
    redactions = pre_redactions + int(report.get("secret_values") or 0) + int(report.get("sensitive_keys") or 0)
    return safe, {"header_values": redactions}

def safe_header_arg(raw: str) -> str:
    name, sep, value = _header_parts(raw)
    if not sep:
        return "[REDACTED_HEADER]"
    if sensitive_name(name) or sensitive_value(value):
        return f"{name}=[REDACTED]"
    return raw

def safe_command_argv(raw_argv: Sequence[str]) -> list[str]:
    safe = list(raw_argv)
    for index, item in enumerate(safe):
        previous = safe[index - 1] if index else ""
        if previous == "--url":
            safe[index] = safe_url_for_artifact(item)
        elif previous == "--header":
            safe[index] = safe_header_arg(item)
        elif item.startswith("--url="):
            safe[index] = "--url=" + safe_url_for_artifact(item.split("=", 1)[1])
        elif item.startswith("--header="):
            safe[index] = "--header=" + safe_header_arg(item.split("=", 1)[1])
    return safe

def skipped_http_payload(
    args: argparse.Namespace, headers: Mapping[str, str],
    problems: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    expect_status = args.expect_status
    safe_url = safe_final_url = safe_url_for_artifact(args.url)
    request_headers, response_headers = sanitize_headers(headers)[0], {}
    problem_records = [dict(item) for item in problems]
    values = {**locals(), "problems": problem_records}
    return (
        render_descriptor(TEMPLATES["skipped_http"], values),
        render_descriptor(TEMPLATES["headers"], values), "",
    )

def fetch_http(
    args: argparse.Namespace, headers: Mapping[str, str], allow_local: bool,
) -> tuple[dict[str, Any], dict[str, Any], str, list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    url, expect_status = args.url, args.expect_status
    current, started = url, int(time.time() * 1000)
    redirects, connected_ips = [], []
    response_headers: dict[str, str] = {}
    body_text = body_sha = ""
    body_bytes_read, body_truncated = 0, False
    status, attempted = None, False

    initial_problems, validated_ips = validate_url_safety_with_ips(current, allow_local=allow_local)
    if initial_problems:
        problems.extend(initial_problems)
        http_payload, headers_payload, _ = skipped_http_payload(
            args, {}, problems,
        )
        headers_payload.pop("final_url", None)
        headers_payload.pop("request_headers", None)
        return http_payload, headers_payload, body_text, problems

    for _ in range(args.max_redirects + 1):
        status, response_headers, raw_body, body_truncated, connected_ip, request_problems = pinned_http_get(
            current, headers, validated_ips, args,
        )
        if connected_ip:
            connected_ips.append(connected_ip)
        if request_problems:
            problems.extend(request_problems)
            break
        attempted = True
        if status is not None and 300 <= status < 400 and response_headers.get("Location"):
            location = response_headers.get("Location", "")
            next_url = urllib.parse.urljoin(current, location)
            redirect_problems, validated_ips = validate_url_safety_with_ips(next_url, allow_local=allow_local)
            redirects.append({"status": status, "url": safe_url_for_artifact(next_url), "from": safe_url_for_artifact(current)})
            if redirect_problems:
                problems.extend({**item, "rule": "preview-redirect"} for item in redirect_problems)
                break
            current = next_url
            continue
        body_sha = hashlib.sha256(raw_body).hexdigest()
        body_text = raw_body.decode("utf-8", errors="replace")
        body_bytes_read = len(raw_body)
        break
    else:
        problems.append(problem("preview redirect limit was exceeded", rule="preview-redirect"))

    if attempted and status != expect_status:
        problems.append(problem(f"http status {status} did not match expected {expect_status}", rule="preview-status"))

    safe_response_headers, header_redaction = sanitize_headers(response_headers)
    if header_redaction.get("header_values"):
        problems.append(problem(
            "response headers contained sensitive values and were redacted",
            rule="preview-header-redacted", severity="low", blocking=False,
        ))
    safe_url = safe_url_for_artifact(url)
    safe_final_url = safe_url_for_artifact(current)
    http_ok = status == expect_status and not any(
        item.get("blocking", True) for item in problems
        if item.get("rule") != "preview-header-redacted"
    )
    elapsed_ms = int(time.time() * 1000) - started
    request_headers = sanitize_headers(headers)[0]
    values = {
        **locals(), "redirects": redirects, "body_sha": body_sha,
        "connected_ips": connected_ips, "response_headers": safe_response_headers,
    }
    http_payload = render_descriptor(TEMPLATES["http"], values)
    headers_payload = render_descriptor(TEMPLATES["headers"], values)
    return http_payload, headers_payload, body_text, problems

def run_smoke_checks(
    args: argparse.Namespace, body: str, headers_payload: Mapping[str, Any],
    http_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    expect_status = args.expect_status
    checks = list(args.smoke_check) or [f"status:{expect_status}"]
    results: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    headers = headers_payload.get("response_headers", {})
    headers = headers if isinstance(headers, Mapping) else {}
    final_url = str(http_payload.get("final_url") or "")
    status_value = http_payload.get("status")
    status = status_value if isinstance(status_value, int) else None
    header_lookup = {key.lower(): value for key, value in headers.items()}
    for raw in checks:
        kind, sep, value = raw.partition(":")
        kind, value = (kind.strip().lower(), value) if sep else ("contains", raw)
        name = raw
        passed = False
        observed = ""
        if kind in {"contains", "not-contains"}:
            contains = value in body
            passed = contains if kind == "contains" else not contains
            key = kind.replace("-", "_") + ("_pass" if passed else "_fail")
            observed = policy_dict("preview", "SMOKE_MESSAGES")[key]
        elif kind == "header":
            header_name, sep, expected = value.partition("=")
            actual = header_lookup.get(header_name.strip().lower(), "")
            passed = bool(sep) and actual == expected
            observed = f"{header_name.strip()}={actual[:120]}"
        elif kind == "url-contains":
            passed = value in final_url
            observed = safe_url_for_artifact(final_url)
        elif kind == "status":
            try:
                expected = int(value)
            except ValueError:
                expected = expect_status
            passed = status == expected
            observed = str(status)
        else:
            observed = f"unknown smoke check kind {kind}"
        results.append({"name": name, "kind": kind, "passed": passed, "observed": observed})
    problems.extend(
        problem(f"smoke check failed: {item['name']}", rule="preview-smoke")
        for item in results if not item["passed"]
    )
    payload = render_descriptor(TEMPLATES["smoke"], {
        "smoke_passed": all(item["passed"] for item in results), "checks": results,
    })
    return payload, problems

def build_deployment_payload(
    args: argparse.Namespace,
    *,
    project: Path,
    current_source: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    payload: dict[str, Any] = render_descriptor(TEMPLATES["deployment"], {
        "provider": args.provider or SETTINGS["default_provider"],
        "url": safe_url_for_artifact(args.url),
    })
    if args.deployment_id:
        payload["deployment_id"] = args.deployment_id
    if args.deployment_source_hash:
        payload["source_hash"] = args.deployment_source_hash
        if args.deployment_source_hash != current_source:
            problems.append(problem("deployment source hash is not bound to the current source", rule="preview-source-binding"))
        else:
            missing_dirty = dirty_paths_missing_from_source_snapshot(project)
            if missing_dirty:
                problems.append(problem(
                    "deployment source hash does not cover dirty source paths: "
                    + ", ".join(git_status_path(item) for item in missing_dirty[:5]),
                    rule="preview-source-binding",
                ))
    if args.deployment_commit_sha:
        payload["commit_sha"] = args.deployment_commit_sha
        head = git_head(project)
        payload["current_git_head"] = head
        if not head:
            problems.append(problem("deployment commit SHA cannot be checked because the project has no git HEAD", rule="preview-source-binding"))
        elif args.deployment_commit_sha != head:
            problems.append(problem("deployment commit SHA is not bound to the current git HEAD", rule="preview-source-binding"))
        elif not source_tree_clean_at_head(project):
            problems.append(problem("deployment commit SHA requires a clean source tree at git HEAD", rule="preview-source-binding"))
    if args.local_build_artifact:
        try:
            artifact = common.safe_project_path(project, args.local_build_artifact, must_exist=True)
            payload["local_build_artifact"] = {
                "path": common.project_relative(project, artifact),
                "sha256": common.file_sha256(artifact),
                "linked_preview_url_sha256": hashlib.sha256(args.url.encode("utf-8")).hexdigest(),
                "diagnostic_only": True,
            }
        except ValueError as exc:
            problems.append(problem(f"local build artifact path is unsafe: {exc}", rule="preview-source-binding", path=args.local_build_artifact))
    has_source_binding = bool(args.deployment_source_hash or args.deployment_commit_sha)
    if not has_source_binding:
        problems.append(problem("source-bound deployment identity is required for strict preview proof", rule="preview-source-binding"))
        if args.local_build_artifact:
            problems.append(problem("local build artifact alone cannot prove the remote preview", rule="preview-source-binding"))
    return payload, problems

def proof_command_for_project(
    args: argparse.Namespace, project: Path, project_arg: str,
    artifacts: Mapping[str, Path],
) -> list[str]:
    values = {
        "project": project_arg, "task": args.task, "url": safe_url_for_artifact(args.url),
        "expect_status": args.expect_status,
        "deployment": common.project_relative(project, artifacts["deployment"]),
        "smoke": common.project_relative(project, artifacts["smoke"]),
    }
    return [
        str(token).format_map(values)
        for token in policy_list("preview", "COMMAND_TEMPLATE")
    ]

def run_record_command(command: Sequence[str]) -> int:
    result = common.run_trusted_command(command, cwd=PLUGIN_ROOT, script_path=STAR_FORGE)
    if result["stdout"]:
        print(result["stdout"], end="")
    if result["stderr"]:
        print(result["stderr"], end="", file=sys.stderr)
    return int(result["returncode"])

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect provider-neutral preview URL evidence")
    types = {"int": int, "float": float}
    for option in policy_list("preview", "PARSER_OPTIONS"):
        kwargs = {key: value for key, value in option.items() if key != "name"}
        if isinstance(kwargs.get("type"), str):
            kwargs["type"] = types[kwargs["type"]]
        parser.add_argument(option["name"], **kwargs)
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    project = common.assert_collector_project_safe(Path(args.project))
    root = common.live_collector_dir(project, args.task, COLLECTOR)
    source_before, runtime_hash = (
        common.compute_source_hash(project), common.compute_runtime_asset_hash(project),
    )
    artifact_reports, problems = [], []

    lease_runtime_hash = runtime_hash
    if args.server_lease:
        try:
            lease_source_path = common.safe_project_path(project, args.server_lease, must_exist=False)
            lease_runtime_hash = common.compute_runtime_asset_hash(project, exclude_paths=[lease_source_path])
        except ValueError:
            lease_runtime_hash = runtime_hash
    lease_valid, lease_problems = validate_server_lease(
        project,
        args.server_lease,
        args.url,
        source_hash=source_before,
        runtime_hash=lease_runtime_hash,
    )
    problems.extend(lease_problems)
    if args.local_preview_mode and not lease_valid:
        problems.append(problem("--local-preview-mode does not authorize loopback requests without a valid server lease", rule="preview-localhost"))
    lease_artifact_path: Path | None = None

    headers, header_problems = parse_headers(args.header)
    deployment_payload, deployment_problems = build_deployment_payload(
        args, project=project, current_source=source_before,
    )
    problems.extend([*header_problems, *deployment_problems])
    if any(item.get("blocking", True) for item in header_problems):
        url_problems = validate_url_safety(args.url, allow_local=lease_valid)
        http_payload, headers_payload, body = skipped_http_payload(
            args, headers, [*header_problems, *url_problems],
        )
        http_problems = url_problems
    else:
        http_payload, headers_payload, body, http_problems = fetch_http(
            args, headers, lease_valid,
        )
    problems.extend(http_problems)
    smoke_payload, smoke_problems = run_smoke_checks(
        args, body, headers_payload, http_payload,
    )
    problems.extend(smoke_problems)
    if args.diagnostic_screenshot:
        try:
            screenshot = common.safe_project_path(project, args.diagnostic_screenshot, must_exist=True)
            deployment_payload["diagnostic_screenshot"] = {
                "path": common.project_relative(project, screenshot),
                "diagnostic_only": True,
            }
        except ValueError as exc:
            problems.append(problem(f"diagnostic screenshot path is unsafe: {exc}", rule="preview-screenshot", path=args.diagnostic_screenshot))
    payloads = {
        "http": http_payload, "deployment": deployment_payload,
        "smoke": smoke_payload, "headers": headers_payload,
    }
    artifact_paths = {
        name: root / filename for name, filename
        in policy_dict("preview", "ARTIFACT_FILES").items()
    }
    artifact_reports += [
        common.write_json(artifact_paths[name], payload)[1] for name, payload in payloads.items()
    ]
    if args.server_lease and lease_valid:
        try:
            source_lease_path = common.safe_project_path(project, args.server_lease, must_exist=True)
            lease_payload = common.read_json(source_lease_path)
        except Exception as exc:
            problems.append(problem(f"server lease artifact could not be copied: {exc}", rule="preview-localhost", path=str(args.server_lease)))
        else:
            lease_artifact_path = root / "server-lease.json"
            artifact_reports.append(common.write_json(
                lease_artifact_path, lease_payload, preserve_fields=("project",))[1])

    degraded = any(item.get("blocking", True) for item in problems)
    manifest_artifacts = {
        **artifact_paths,
        **({"server_lease": lease_artifact_path} if lease_artifact_path else {}),
    }
    values = {
        "url": safe_url_for_artifact(args.url),
        "provider": evidence_provider(args.provider),
        "status": http_payload.get("status"), "expect_status": args.expect_status,
        "smoke_passed": smoke_payload.get("passed"),
        "source_bound": bool(args.deployment_source_hash or args.deployment_commit_sha),
        "local_preview_mode": bool(args.local_preview_mode),
        "server_lease": bool(args.server_lease),
        "server_lease_artifact": (
            common.project_relative(project, lease_artifact_path)
            if lease_artifact_path is not None else ""
        ),
        "redacted_artifacts": common.merge_reports(*artifact_reports),
    }
    manifest = common.write_live_manifest(
        project,
        task=args.task,
        collector=COLLECTOR,
        command_argv=safe_command_argv([
            "python3", "scripts/live_collectors/preview.py",
            *(list(argv) if argv is not None else sys.argv[1:]),
        ]),
        tool_versions={"python": sys.version.split()[0], "urllib": "stdlib"},
        artifacts=manifest_artifacts,
        summary=render_descriptor(TEMPLATES["summary"], values),
        degraded=degraded,
        problems=problems,
        source_hash_before=source_before,
        source_hash_after=common.compute_source_hash(project),
        runtime_asset_hash=runtime_hash,
    )
    envelope_path, envelope = write_evidence_envelope(project, manifest, provider=args.provider)

    command = proof_command_for_project(args, project, args.project, artifact_paths)
    record_command = proof_command_for_project(args, project, str(project), artifact_paths)
    values.update({
        "manifest": common.project_relative(project, manifest),
        "evidence": common.project_relative(project, envelope_path),
        "evidence_verdict": envelope["verdict"], "degraded": degraded,
        "problems": problems, "proof_command": command,
        "proof_command_shell": shlex.join(command),
        "artifacts": {
            name: common.project_relative(project, path)
            for name, path in artifact_paths.items()
        },
    })
    output, _ = common.redact_sensitive_values(
        render_descriptor(TEMPLATES["result"], values)
    )
    output["proof_command"] = command
    output["proof_command_shell"] = shlex.join(command)
    print(json.dumps(output, indent=2, sort_keys=True))
    return run_record_command(record_command) if args.record else 1 if degraded else 0

if __name__ == "__main__":
    raise SystemExit(main())
