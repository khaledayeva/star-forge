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
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
PLUGIN_ROOT = SCRIPTS_ROOT.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from live_collectors import common


COLLECTOR = "preview"
STAR_FORGE = PLUGIN_ROOT / "scripts" / "star_forge.py"
DEFAULT_USER_AGENT = "star-forge-preview-collector/1 read-only"
METADATA_HOSTS = {"metadata.google.internal", "metadata", "169.254.169.254", "169.254.170.2"}
LOCAL_HOSTNAMES = {"localhost"}
def now_ms() -> int:
    return int(time.time() * 1000)


def git_head(project: Path) -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(project),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        return None
    head = proc.stdout.strip()
    return head or None


def git_status(project: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all", "--", "."],
        cwd=str(project),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        return ["?? <git status unavailable>"]
    return [line for line in proc.stdout.splitlines() if line.strip()]


def git_status_path(line: str) -> str:
    path = line[3:] if len(line) > 3 else line.strip()
    path = path.strip().strip('"')
    if " -> " in path:
        path = path.split(" -> ", 1)[1].strip().strip('"')
    return path


def source_dirty_entries(project: Path) -> list[str]:
    return common.source_hash_dirty_entries(project, git_status(project))


def source_tree_clean_at_head(project: Path) -> bool:
    return bool(git_head(project)) and not source_dirty_entries(project)


def source_snapshot_rel_paths(project: Path) -> set[str]:
    return {common.project_relative(project, path) for path in common.snapshot_file_candidates(project)}


def dirty_paths_missing_from_source_snapshot(project: Path) -> list[str]:
    snapshot_paths = source_snapshot_rel_paths(project)
    missing: list[str] = []
    for line in source_dirty_entries(project):
        rel = git_status_path(line)
        if not rel or rel in snapshot_paths:
            continue
        missing.append(line)
    return missing


def json_write(path: Path, payload: Any) -> dict[str, Any]:
    redacted, report = common.redact_sensitive_values(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redacted, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def merge_reports(reports: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for report in reports:
        for key, value in report.items():
            if isinstance(value, int):
                merged[key] = merged.get(key, 0) + value
    return merged


def problem(message: str, *, rule: str, severity: str = "high", path: str = "", blocking: bool = True) -> dict[str, Any]:
    return {
        "severity": severity,
        "rule": rule,
        "message": message,
        "path": path,
        "blocking": blocking,
    }


def sensitive_name(name: str) -> bool:
    return common.sensitive_key_name(name)


def sensitive_value(value: str) -> bool:
    lowered = value.lower()
    return (
        bool(common.SECRET_RE.search(value))
        or lowered.startswith("bearer ")
        or lowered.startswith("basic ")
        or "authorization:" in lowered
    )


def sensitive_query_pair(key: str, value: str) -> bool:
    return sensitive_name(key) or sensitive_value(value)


def safe_url_for_artifact(url: str) -> str:
    parsed = urllib.parse.urlparse(url or "")
    if not parsed.scheme:
        return "[invalid-url]"
    host = parsed.hostname or ""
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    pairs: list[tuple[str, str]] = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        pairs.append((key, "[REDACTED]" if sensitive_query_pair(key, value) else value))
    query = urllib.parse.urlencode(pairs, doseq=True)
    fragment = "[REDACTED]" if sensitive_query_pair("fragment", parsed.fragment) else parsed.fragment
    return urllib.parse.urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, query, fragment))


def is_metadata_host(host: str) -> bool:
    lowered = host.lower().strip("[]")
    return lowered in METADATA_HOSTS or lowered.endswith(".metadata.google.internal")


def parse_ip(host: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def is_loopback_ip(ip: ipaddress._BaseAddress) -> bool:
    return ip.is_loopback


def is_blocked_ip(ip: ipaddress._BaseAddress, *, explicit_local_allowed: bool) -> str | None:
    if is_loopback_ip(ip):
        if explicit_local_allowed:
            return None
        return "loopback targets require --server-lease or --local-preview-mode"
    if ip.is_unspecified:
        return "unspecified IP targets are not allowed"
    if ip.is_link_local:
        return "link-local targets are not allowed"
    if ip.is_private:
        return "private network targets are not allowed"
    if ip.is_reserved:
        return "reserved IP targets are not allowed"
    if ip.is_multicast:
        return "multicast targets are not allowed"
    if not ip.is_global:
        return "non-global IP targets are not allowed"
    return None


def resolve_ips(host: str, port: int | None) -> tuple[list[ipaddress._BaseAddress], str | None]:
    direct = parse_ip(host)
    if direct:
        return [direct], None
    try:
        infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        return [], f"preview URL host could not be resolved: {exc}"
    ips: list[ipaddress._BaseAddress] = []
    for info in infos:
        address = info[4][0]
        ip = parse_ip(address)
        if ip and ip not in ips:
            ips.append(ip)
    return ips, None


def is_local_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url or "")
    host = parsed.hostname or ""
    ip = parse_ip(host)
    return host.lower() in LOCAL_HOSTNAMES or bool(ip and ip.is_loopback)


def validate_server_lease(project: Path, raw_lease: str, url: str, *, source_hash: str, runtime_hash: str) -> tuple[bool, list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    if not raw_lease:
        return False, problems
    try:
        from live_collectors import browser_playwright
        parsed_url, url_problems = browser_playwright.validate_url(url)
        for item in url_problems:
            mapped = dict(item)
            if mapped.get("rule") == "server-lease":
                mapped["rule"] = "preview-localhost"
            problems.append(mapped)
        if url_problems:
            return False, problems
        _lease_path, payload, lease_problems = browser_playwright.validate_server_lease(
            project,
            raw_lease,
            parsed_url,
            source_hash,
            runtime_hash,
        )
    except Exception as exc:
        return False, [problem(f"server lease is invalid: {exc}", rule="preview-localhost", path=str(raw_lease))]
    for item in lease_problems:
        mapped = dict(item)
        if mapped.get("rule") == "server-lease":
            mapped["rule"] = "preview-localhost"
        problems.append(mapped)
    return payload is not None and not problems, problems


def validate_url_safety(url: str, *, allow_local: bool) -> list[dict[str, Any]]:
    problems, _ips = validate_url_safety_with_ips(url, allow_local=allow_local)
    return problems


def validate_url_safety_with_ips(url: str, *, allow_local: bool) -> tuple[list[dict[str, Any]], set[str]]:
    problems: list[dict[str, Any]] = []
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in {"http", "https"}:
        return [problem("preview URL must use http or https", rule="preview-url")], set()
    if parsed.username or parsed.password:
        problems.append(problem("preview URL must not include credentials", rule="preview-url"))
    host = parsed.hostname or ""
    if not host:
        problems.append(problem("preview URL must include a host", rule="preview-url"))
        return problems, set()
    if is_metadata_host(host):
        problems.append(problem("preview URL must not target metadata hosts", rule="preview-url"))
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if sensitive_query_pair(key, value):
            problems.append(problem("preview URL query appears to contain sensitive material", rule="preview-url"))
            break
    explicit_local = host.lower() in LOCAL_HOSTNAMES or bool(parse_ip(host) and parse_ip(host).is_loopback)
    if explicit_local and not allow_local:
        problems.append(problem("localhost preview URLs require --server-lease or --local-preview-mode", rule="preview-localhost"))
    ips, resolve_problem = resolve_ips(host, parsed.port)
    if resolve_problem:
        problems.append(problem(resolve_problem, rule="preview-url"))
        return problems, set()
    for ip in ips:
        blocked = is_blocked_ip(ip, explicit_local_allowed=explicit_local and allow_local)
        if blocked:
            problems.append(problem(f"preview URL resolved to unsafe address {ip}: {blocked}", rule="preview-url"))
    return problems, {str(ip) for ip in ips}


def validate_dns_stability_for_connection(url: str, *, allow_local: bool, validated_ips: set[str]) -> tuple[list[dict[str, Any]], set[str]]:
    problems, current_ips = validate_url_safety_with_ips(url, allow_local=allow_local)
    if problems:
        return problems, current_ips
    if validated_ips and current_ips != validated_ips:
        return [
            problem(
                "preview URL DNS resolution changed between validation and connection setup",
                rule="preview-url",
            )
        ], current_ips
    return [], current_ips


def host_header_for_url(parsed: urllib.parse.ParseResult) -> str:
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port and parsed.port != default_port:
        return f"{host}:{parsed.port}"
    return host


def request_target_for_url(parsed: urllib.parse.ParseResult) -> str:
    path = parsed.path or "/"
    if parsed.params:
        path = f"{path};{parsed.params}"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


def choose_connection_ip(validated_ips: set[str]) -> str:
    parsed_ips = sorted(
        (ipaddress.ip_address(raw) for raw in validated_ips),
        key=lambda ip: (ip.version, int(ip)),
    )
    if not parsed_ips:
        raise ValueError("no validated IP address is available for preview connection")
    return str(parsed_ips[0])


def pinned_http_get(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    max_body_bytes: int,
    validated_ips: set[str],
) -> tuple[int | None, dict[str, str], bytes, bool, str, list[dict[str, Any]]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return None, {}, b"", False, "", [
            problem("HTTPS preview fetch requires connection-time IP pinning and is fail-closed until SNI-safe pinning is available", rule="preview-url")
        ]
    if parsed.scheme != "http":
        return None, {}, b"", False, "", [problem("preview URL must use http or https", rule="preview-url")]
    try:
        connected_ip = choose_connection_ip(validated_ips)
    except ValueError as exc:
        return None, {}, b"", False, "", [problem(str(exc), rule="preview-url")]
    port = parsed.port or 80
    request_headers = dict(headers)
    request_headers["Host"] = host_header_for_url(parsed)
    conn = http.client.HTTPConnection(connected_ip, port, timeout=timeout)
    try:
        conn.request("GET", request_target_for_url(parsed), headers=request_headers)
        response = conn.getresponse()
        raw_body = response.read(max_body_bytes + 1)
        truncated = len(raw_body) > max_body_bytes
        if truncated:
            raw_body = raw_body[:max_body_bytes]
        return int(response.status), {str(key): str(value) for key, value in response.getheaders()}, raw_body, truncated, connected_ip, []
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        return None, {}, b"", False, connected_ip, [problem(f"preview HTTP request failed: {exc}", rule="preview-http")]
    finally:
        conn.close()


def parse_headers(raw_headers: Sequence[str]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    problems: list[dict[str, Any]] = []
    for raw in raw_headers:
        name, sep, value = raw.partition(":")
        if not sep:
            name, sep, value = raw.partition("=")
        name = name.strip()
        value = value.strip()
        if not sep or not name:
            problems.append(problem(f"header must use Name: value or Name=value: {raw}", rule="preview-header"))
            continue
        if sensitive_name(name) or sensitive_value(value):
            problems.append(problem(f"request header `{name}` appears to contain sensitive material", rule="preview-header"))
            continue
        headers[name] = value
    return headers, problems


def sanitize_headers(headers: Mapping[str, str]) -> tuple[dict[str, str], dict[str, int]]:
    safe: dict[str, str] = {}
    redactions = 0
    for key, value in sorted(headers.items(), key=lambda item: item[0].lower()):
        if sensitive_name(key) or sensitive_value(str(value)):
            safe[key] = "[REDACTED]"
            redactions += 1
            continue
        cleaned, report = common.redact_sensitive_values(str(value)[:500])
        if report.get("secret_values") or report.get("sensitive_keys"):
            redactions += int(report.get("secret_values") or 0) + int(report.get("sensitive_keys") or 0)
        safe[key] = str(cleaned)
    return safe, {"header_values": redactions}


def safe_header_arg(raw: str) -> str:
    name, sep, value = raw.partition(":")
    if not sep:
        name, sep, value = raw.partition("=")
    if not sep:
        return "[REDACTED_HEADER]"
    name = name.strip()
    value = value.strip()
    if sensitive_name(name) or sensitive_value(value):
        return f"{name}=[REDACTED]"
    return raw


def safe_command_argv(raw_argv: Sequence[str]) -> list[str]:
    safe: list[str] = []
    redact_next = ""
    for item in raw_argv:
        if redact_next == "url":
            safe.append(safe_url_for_artifact(item))
            redact_next = ""
            continue
        if redact_next == "header":
            safe.append(safe_header_arg(item))
            redact_next = ""
            continue
        if item == "--url":
            safe.append(item)
            redact_next = "url"
            continue
        if item == "--header":
            safe.append(item)
            redact_next = "header"
            continue
        if item.startswith("--url="):
            safe.append("--url=" + safe_url_for_artifact(item.split("=", 1)[1]))
            continue
        if item.startswith("--header="):
            safe.append("--header=" + safe_header_arg(item.split("=", 1)[1]))
            continue
        safe.append(item)
    return safe


def skipped_http_payload(
    url: str,
    *,
    headers: Mapping[str, str],
    expect_status: int,
    problems: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    http_payload = {
        "schema": "star-forge.preview-http.v1",
        "attempted": False,
        "method": "GET",
        "url": safe_url_for_artifact(url),
        "expected_status": expect_status,
        "ok": False,
        "problems": [dict(item) for item in problems],
    }
    headers_payload = {
        "schema": "star-forge.preview-headers.v1",
        "final_url": safe_url_for_artifact(url),
        "response_headers": {},
        "request_headers": sanitize_headers(headers)[0],
    }
    return http_payload, headers_payload, ""


def fetch_http(
    url: str,
    *,
    headers: Mapping[str, str],
    expect_status: int,
    timeout: float,
    max_redirects: int,
    max_body_bytes: int,
    allow_local: bool,
) -> tuple[dict[str, Any], dict[str, Any], str, list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    current = url
    started = now_ms()
    redirects: list[dict[str, Any]] = []
    response_headers: dict[str, str] = {}
    body_text = ""
    body_sha = ""
    body_bytes_read = 0
    body_truncated = False
    status: int | None = None
    attempted = False
    connected_ips: list[str] = []

    initial_problems, validated_ips = validate_url_safety_with_ips(current, allow_local=allow_local)
    if initial_problems:
        problems.extend(initial_problems)
        http_payload = {
            "schema": "star-forge.preview-http.v1",
            "attempted": False,
            "method": "GET",
            "url": safe_url_for_artifact(url),
            "expected_status": expect_status,
            "ok": False,
            "problems": problems,
        }
        return http_payload, {"schema": "star-forge.preview-headers.v1", "response_headers": {}}, body_text, problems

    for _ in range(max_redirects + 1):
        status, response_headers, raw_body, body_truncated, connected_ip, request_problems = pinned_http_get(
            current,
            headers=headers,
            timeout=timeout,
            max_body_bytes=max_body_bytes,
            validated_ips=validated_ips,
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
                for item in redirect_problems:
                    item = dict(item)
                    item["rule"] = "preview-redirect"
                    problems.append(item)
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
        problems.append(
            problem(
                "response headers contained sensitive values and were redacted",
                rule="preview-header-redacted",
                severity="low",
                blocking=False,
            )
        )
    elapsed_ms = now_ms() - started
    http_payload = {
        "schema": "star-forge.preview-http.v1",
        "attempted": attempted,
        "method": "GET",
        "url": safe_url_for_artifact(url),
        "final_url": safe_url_for_artifact(current),
        "status": status,
        "expected_status": expect_status,
        "ok": status == expect_status and not any(item.get("blocking", True) for item in problems if item.get("rule") != "preview-header-redacted"),
        "redirect_chain": redirects,
        "elapsed_ms": elapsed_ms,
        "body_sha256": body_sha,
        "body_bytes_read": body_bytes_read,
        "body_truncated": body_truncated,
        "connected_ips": connected_ips,
        "connection_pinning": {
            "strategy": "http-connect-vetted-ip",
            "https": "fail-closed",
        },
    }
    headers_payload = {
        "schema": "star-forge.preview-headers.v1",
        "final_url": safe_url_for_artifact(current),
        "response_headers": safe_response_headers,
        "request_headers": sanitize_headers(headers)[0],
    }
    return http_payload, headers_payload, body_text, problems


def parse_smoke(raw: str) -> tuple[str, str, str]:
    kind, sep, rest = raw.partition(":")
    if not sep:
        return "contains", raw, raw
    return kind.strip().lower(), rest, raw


def run_smoke_checks(
    raw_checks: Sequence[str],
    *,
    body: str,
    headers: Mapping[str, str],
    final_url: str,
    status: int | None,
    expect_status: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    checks = list(raw_checks) or [f"status:{expect_status}"]
    results: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    header_lookup = {key.lower(): value for key, value in headers.items()}
    for raw in checks:
        kind, value, name = parse_smoke(raw)
        passed = False
        observed = ""
        if kind == "contains":
            passed = value in body
            observed = "body contained expected text" if passed else "body did not contain expected text"
        elif kind == "not-contains":
            passed = value not in body
            observed = "body omitted forbidden text" if passed else "body contained forbidden text"
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
            passed = False
            observed = f"unknown smoke check kind {kind}"
        item = {"name": name, "kind": kind, "passed": passed, "observed": observed}
        results.append(item)
        if not passed:
            problems.append(problem(f"smoke check failed: {name}", rule="preview-smoke"))
    payload = {"schema": "star-forge.preview-smoke.v1", "passed": all(item["passed"] for item in results), "checks": results}
    return payload, problems


def build_deployment_payload(
    args: argparse.Namespace,
    *,
    project: Path,
    current_source: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    payload: dict[str, Any] = {
        "schema": "star-forge.preview-deployment.v1",
        "provider": args.provider or "provider-neutral",
        "url": safe_url_for_artifact(args.url),
    }
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
    *,
    project: Path,
    project_arg: str,
    task: str,
    url: str,
    expect_status: int,
    manifest: Path,
    deployment: Path,
    smoke: Path,
) -> list[str]:
    deployment_rel = common.project_relative(project, deployment)
    smoke_rel = common.project_relative(project, smoke)
    return [
        "python3",
        "scripts/star_forge.py",
        "preview-proof",
        "--project",
        project_arg,
        "--task",
        task,
        "--url",
        url,
        "--expect-status",
        str(expect_status),
        "--deployment-metadata",
        deployment_rel,
        "--smoke-checks",
        smoke_rel,
        "--strict",
    ]


def run_record_command(command: Sequence[str]) -> int:
    argv = [sys.executable if item == "python3" else str(STAR_FORGE) if item == "scripts/star_forge.py" else item for item in command]
    proc = subprocess.run(argv, cwd=str(PLUGIN_ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect provider-neutral preview URL evidence")
    parser.add_argument("--project", default=".")
    parser.add_argument("--task", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--expect-status", type=int, default=200)
    parser.add_argument("--smoke-check", action="append", default=[])
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-redirects", type=int, default=5)
    parser.add_argument("--max-body-bytes", type=int, default=256 * 1024)
    parser.add_argument("--server-lease", default="")
    parser.add_argument("--local-preview-mode", action="store_true")
    parser.add_argument("--provider", default="provider-neutral")
    parser.add_argument("--deployment-id", default="")
    parser.add_argument("--deployment-source-hash", default="")
    parser.add_argument("--deployment-commit-sha", default="")
    parser.add_argument("--local-build-artifact", default="")
    parser.add_argument("--diagnostic-screenshot", default="")
    parser.add_argument("--record", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    project = Path(args.project).resolve()
    root = common.live_collector_dir(project, args.task, COLLECTOR)
    source_before = common.compute_source_hash(project)
    runtime_hash = common.compute_runtime_asset_hash(project)
    artifact_reports: list[Mapping[str, Any]] = []
    problems: list[dict[str, Any]] = []

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
    allow_local = lease_valid
    lease_artifact_path: Path | None = None

    headers, header_problems = parse_headers(args.header)
    problems.extend(header_problems)

    deployment_payload, deployment_problems = build_deployment_payload(args, project=project, current_source=source_before)
    problems.extend(deployment_problems)

    pre_request_blockers = [item for item in header_problems if item.get("blocking", True)]
    if pre_request_blockers:
        url_problems = validate_url_safety(args.url, allow_local=allow_local)
        http_payload, headers_payload, body = skipped_http_payload(
            args.url,
            headers=headers,
            expect_status=args.expect_status,
            problems=[*header_problems, *url_problems],
        )
        http_problems = url_problems
    else:
        http_payload, headers_payload, body, http_problems = fetch_http(
            args.url,
            headers=headers,
            expect_status=args.expect_status,
            timeout=args.timeout,
            max_redirects=args.max_redirects,
            max_body_bytes=args.max_body_bytes,
            allow_local=allow_local,
        )
    problems.extend(http_problems)

    smoke_payload, smoke_problems = run_smoke_checks(
        args.smoke_check,
        body=body,
        headers=headers_payload.get("response_headers", {}),
        final_url=str(http_payload.get("final_url") or ""),
        status=http_payload.get("status") if isinstance(http_payload.get("status"), int) else None,
        expect_status=args.expect_status,
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

    http_path = root / "http.json"
    deployment_path = root / "deployment.json"
    smoke_path = root / "smoke.json"
    headers_path = root / "headers.json"
    artifact_reports.append(json_write(http_path, http_payload))
    artifact_reports.append(json_write(deployment_path, deployment_payload))
    artifact_reports.append(json_write(smoke_path, smoke_payload))
    artifact_reports.append(json_write(headers_path, headers_payload))
    if args.server_lease and lease_valid:
        try:
            source_lease_path = common.safe_project_path(project, args.server_lease, must_exist=True)
            lease_payload = json.loads(source_lease_path.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append(problem(f"server lease artifact could not be copied: {exc}", rule="preview-localhost", path=str(args.server_lease)))
        else:
            lease_artifact_path = root / "server-lease.json"
            artifact_reports.append(json_write(lease_artifact_path, lease_payload))

    source_after = common.compute_source_hash(project)
    degraded = any(item.get("blocking", True) for item in problems)
    command_argv = safe_command_argv(["python3", "scripts/live_collectors/preview.py", *(list(argv) if argv is not None else sys.argv[1:])])
    manifest_artifacts: dict[str, Path] = {"http": http_path, "deployment": deployment_path, "smoke": smoke_path, "headers": headers_path}
    if lease_artifact_path is not None:
        manifest_artifacts["server_lease"] = lease_artifact_path
    manifest = common.write_live_manifest(
        project,
        task=args.task,
        collector=COLLECTOR,
        command_argv=command_argv,
        tool_versions={"python": sys.version.split()[0], "urllib": "stdlib"},
        artifacts=manifest_artifacts,
        summary={
            "url": safe_url_for_artifact(args.url),
            "status": http_payload.get("status"),
            "expected_status": args.expect_status,
            "smoke_passed": smoke_payload.get("passed"),
            "source_bound": bool(args.deployment_source_hash or args.deployment_commit_sha),
            "local_preview_mode": bool(args.local_preview_mode),
            "server_lease": bool(args.server_lease),
            "server_lease_artifact": common.project_relative(project, lease_artifact_path) if lease_artifact_path is not None else "",
            "redacted_artifacts": merge_reports(artifact_reports),
        },
        degraded=degraded,
        problems=problems,
        source_hash_before=source_before,
        source_hash_after=source_after,
        runtime_asset_hash=runtime_hash,
    )

    command = proof_command_for_project(
        project=project,
        project_arg=args.project,
        task=args.task,
        url=safe_url_for_artifact(args.url),
        expect_status=args.expect_status,
        manifest=manifest,
        deployment=deployment_path,
        smoke=smoke_path,
    )
    record_command = proof_command_for_project(
        project=project,
        project_arg=str(project),
        task=args.task,
        url=safe_url_for_artifact(args.url),
        expect_status=args.expect_status,
        manifest=manifest,
        deployment=deployment_path,
        smoke=smoke_path,
    )
    result = {
        "schema": "star-forge.preview-collector.v1",
        "collector": COLLECTOR,
        "manifest": common.project_relative(project, manifest),
        "artifacts": {
            "http": common.project_relative(project, http_path),
            "deployment": common.project_relative(project, deployment_path),
            "smoke": common.project_relative(project, smoke_path),
            "headers": common.project_relative(project, headers_path),
        },
        "degraded": degraded,
        "problems": problems,
        "proof_command": command,
        "proof_command_shell": shlex.join(command),
        "recorded": False,
    }
    print(json.dumps(common.redact_sensitive_values(result)[0], indent=2, sort_keys=True))
    if args.record:
        return run_record_command(record_command)
    return 1 if degraded else 0


if __name__ == "__main__":
    raise SystemExit(main())
