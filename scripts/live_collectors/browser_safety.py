"""Network and lease safety policy for live browser collection."""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse
from pathlib import Path
from typing import Any, Mapping, Sequence

from live_collectors import common
from live_collectors.policy_data import policy_bindings
from live_collectors.provider_engine import failed_checks, render_descriptor

live_common = common


globals().update(policy_bindings(
    "browser_safety", "CONSTANTS", "METADATA_HOSTS", "LEASE_VALUE_CHECKS",
    "REQUEST_CONTROL_CHECKS", "REQUEST_ENTRY_SPECS", "URL_SAFETY_TEMPLATE",
))
globals().update(CONSTANTS)


descriptor = render_descriptor


problem = common.problem
read_json_file = common.read_json


def validate_url(raw_url: str) -> tuple[urllib.parse.ParseResult, list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        problems.append(problem("browser URL must use http or https", rule="browser-url"))
    if parsed.username or parsed.password:
        problems.append(problem("browser URL must not embed credentials", rule="browser-url"))
    if not parsed.hostname:
        problems.append(problem("browser URL must include a host", rule="browser-url"))
    elif is_metadata_host(parsed.hostname):
        problems.append(problem("browser URL must not target metadata hosts", rule="browser-url"))
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(marker in f"{key}={value}".lower() for key, value in query_pairs
           for marker in ("token", "secret", "password", "api_key", "apikey", "auth")):
        problems.append(problem("browser URL query appears to contain sensitive material", rule="browser-url"))
    return parsed, problems


def normalize_origin(parsed: urllib.parse.ParseResult) -> str:
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme.lower()}://{host.lower()}:{port}"


def safety_equivalent_url(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme.lower() not in {"ws", "wss"}:
        return raw_url
    scheme = "https" if parsed.scheme.lower() == "wss" else "http"
    return urllib.parse.urlunparse((scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def host_is_literal_loopback(host: str) -> bool:
    ip = parse_ip(host)
    return bool(ip and ip.is_loopback)


def is_metadata_host(host: str) -> bool:
    lowered = host.lower().strip("[]")
    return lowered in METADATA_HOSTS or lowered.endswith(".metadata.google.internal")


def parse_ip(host: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def resolve_ips(host: str, port: int | None) -> tuple[list[ipaddress._BaseAddress], str | None]:
    direct = parse_ip(host)
    if direct:
        return [direct], None
    try:
        infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        return [], f"browser URL host could not be resolved: {exc}"
    ips = list(dict.fromkeys(
        ip for info in infos if (ip := parse_ip(info[4][0]))
    ))
    return ips, None


def unsafe_ip_reason(ip: ipaddress._BaseAddress) -> str | None:
    reasons = (
        (ip.is_loopback, "loopback"), (ip.is_unspecified, "unspecified"),
        (ip.is_link_local, "link-local"), (ip.is_private, "private network"),
        (ip.is_reserved, "reserved"), (ip.is_multicast, "multicast"),
        (not ip.is_global, "non-global"),
    )
    return next((reason for applies, reason in reasons if applies), None)


def unsafe_url_reasons(parsed: urllib.parse.ParseResult) -> tuple[bool, list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    host = parsed.hostname or ""
    if not host:
        return False, problems
    if is_metadata_host(host):
        problems.append(problem("browser URL must not target metadata hosts", rule="browser-url"))
        return False, problems
    ips, resolve_problem = resolve_ips(host, parsed.port)
    if resolve_problem:
        problems.append(problem(resolve_problem, rule="browser-url"))
        return False, problems
    requires_lease = False
    for ip in ips:
        reason = unsafe_ip_reason(ip)
        if reason == "loopback":
            requires_lease = True
            problems.append(problem(f"browser URL resolved to unsafe address {ip}: {reason} targets require a server lease", rule="server-lease"))
        elif reason:
            problems.append(problem(f"browser URL resolved to unsafe address {ip}: {reason} targets are not allowed", rule="browser-url"))
    return requires_lease, problems


def reject_safety(
    record: dict[str, Any], issue: str | Sequence[Mapping[str, Any]], rule: str = "browser-url"
) -> dict[str, Any]:
    record["problems"] = (
        [dict(item) for item in issue] if not isinstance(issue, str)
        else [problem(issue, rule=rule)]
    )
    return record


def browser_url_safety_evidence(raw_url: str, *, allowed_local_origins: Sequence[str] = ()) -> dict[str, Any]:
    safety_url = safety_equivalent_url(raw_url)
    parsed, url_problems = validate_url(safety_url)
    raw_parsed = urllib.parse.urlparse(raw_url)
    host = parsed.hostname or ""
    record = descriptor(
        URL_SAFETY_TEMPLATE, url=raw_url, scheme=raw_parsed.scheme or parsed.scheme,
        safety_url=safety_url, host=host,
        port=parsed.port or (443 if parsed.scheme == "https" else 80),
    )
    if parsed.scheme and host:
        try:
            record["origin"] = normalize_origin(parsed)
        except ValueError:
            record["origin"] = ""
    if url_problems:
        return reject_safety(record, url_problems)
    ips, resolve_problem = resolve_ips(host, parsed.port)
    if ips:
        record["resolved_ips"] = [str(ip) for ip in ips]
    if resolve_problem:
        return reject_safety(record, resolve_problem)
    if not ips:
        return reject_safety(record, "browser URL host did not resolve to an address")

    allowed_origin = str(record.get("origin") or "")
    allowed_local = {str(origin) for origin in allowed_local_origins if str(origin)}
    literal_loopback = host_is_literal_loopback(host)
    record["literal_loopback_host"] = literal_loopback
    problems: list[dict[str, Any]] = []
    requires_lease = False
    for ip in ips:
        reason = unsafe_ip_reason(ip)
        if reason == "loopback":
            requires_lease = True
        elif reason:
            problems.append(problem(f"browser URL resolved to unsafe address {ip}: {reason} targets are not allowed", rule="browser-url"))
    record["requires_server_lease"] = requires_lease
    if problems:
        return reject_safety(record, problems)
    if requires_lease:
        if not literal_loopback:
            return reject_safety(
                record,
                f"browser URL resolved non-literal host {host} to loopback; use a literal loopback URL or a local safety proxy",
            )
        if allowed_origin in allowed_local:
            record["allowed"] = True
            record["allowed_by_server_lease"] = True
            record["evidence_source"] = "leased_loopback_literal"
            return record
        return reject_safety(
            record,
            f"browser URL resolved to loopback origin {allowed_origin or raw_url} without a matching server lease",
            "server-lease",
        )
    return reject_safety(
        record,
        "browser URLs must use a leased loopback origin unless browser traffic is connection-controlled",
    )


def request_safety_problem_records(evidence: Mapping[str, Any], *, allowed_local_origins: Sequence[str], path: str = "") -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    if evidence.get("allowed") is not True:
        message = "browser request safety evidence recorded a blocked request"
        recorded_problems = evidence.get("problems")
        if isinstance(recorded_problems, list) and recorded_problems:
            first = recorded_problems[0]
            if isinstance(first, Mapping) and first.get("message"):
                message = str(first["message"])
        problems.append(problem(message, rule="browser-request-safety", path=path))
    url = str(evidence.get("url") or "")
    if not url:
        problems.append(problem("browser request safety evidence is missing a URL", rule="browser-request-safety", path=path))
        return problems
    current = browser_url_safety_evidence(url, allowed_local_origins=allowed_local_origins)
    if current.get("allowed") is not True:
        for item in current.get("problems", []) if isinstance(current.get("problems"), list) else []:
            if isinstance(item, Mapping):
                problems.append(problem(f"browser request URL is unsafe: {item.get('message')}", rule="browser-request-safety", path=path))
        if not current.get("problems"):
            problems.append(problem("browser request URL is unsafe", rule="browser-request-safety", path=path))
    recorded_ips = evidence.get("resolved_ips")
    connection_control = str(evidence.get("connection_control") or "")
    evidence_source = str(evidence.get("evidence_source") or "")
    allowed_origin_set = {str(item) for item in allowed_local_origins}
    origin = str(evidence.get("origin") or current.get("origin") or "")
    if connection_control != BROWSER_NETWORK_CONTROL_MODE:
        problems.append(problem("browser request safety evidence must record leased loopback network control", rule="browser-request-safety", path=path))
    if (
        evidence.get("allowed_by_server_lease") is not True
        or evidence_source not in {"leased_loopback", "leased_loopback_literal"}
        or origin not in allowed_origin_set
    ):
        problems.append(problem("browser request safety evidence must come from a leased loopback origin", rule="browser-request-safety", path=path))
    if not isinstance(recorded_ips, list) or not recorded_ips:
        problems.append(problem("browser request safety evidence must include resolved IPs", rule="browser-request-safety", path=path))
    else:
        for raw_ip in recorded_ips:
            ip = parse_ip(str(raw_ip))
            if ip is None:
                problems.append(problem(f"browser request safety evidence has invalid IP {raw_ip}", rule="browser-request-safety", path=path))
                continue
            reason = unsafe_ip_reason(ip)
            if reason == "loopback" and origin in allowed_origin_set:
                if evidence.get("literal_loopback_host") is not True or current.get("literal_loopback_host") is not True:
                    problems.append(problem("browser request safety evidence must use a literal loopback URL for leased loopback traffic", rule="browser-request-safety", path=path))
                continue
            if reason:
                problems.append(problem(f"browser request resolved to unsafe address {ip}: {reason}", rule="browser-request-safety", path=path))
    return problems


def validate_safety_entries(
    items: Any,
    *,
    allowed_local_origins: Sequence[str],
    path: str,
    missing_message: str,
    entry_message: str,
    require_nonempty: bool,
) -> list[dict[str, Any]]:
    if not isinstance(items, list) or require_nonempty and not items:
        return [problem(missing_message, rule="browser-request-safety", path=path)]
    problems: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            problems.append(problem(entry_message, rule="browser-request-safety", path=path))
        else:
            problems.extend(request_safety_problem_records(
                item, allowed_local_origins=allowed_local_origins, path=path
            ))
    return problems


def validate_request_safety_payload(payload: Mapping[str, Any], *, allowed_local_origins: Sequence[str], path: str = "") -> list[dict[str, Any]]:
    evidence = payload.get("request_safety")
    if not isinstance(evidence, Mapping):
        return [problem("interaction evidence must include browser request safety evidence", rule="browser-request-safety", path=path)]
    if evidence.get("schema") != "star-forge.browser-request-safety.v1":
        return [problem("browser request safety evidence has an unsupported schema", rule="browser-request-safety", path=path)]
    problems: list[dict[str, Any]] = []
    problems.extend(
        problem(message, rule="browser-request-safety", path=path)
        for message in failed_checks(evidence, REQUEST_CONTROL_CHECKS)
    )
    for field, missing_message, entry_message, require_nonempty in REQUEST_ENTRY_SPECS:
        problems.extend(validate_safety_entries(
            evidence.get(field), allowed_local_origins=allowed_local_origins, path=path,
            missing_message=missing_message, entry_message=entry_message,
            require_nonempty=require_nonempty,
        ))
    return problems


def is_local_origin(parsed: urllib.parse.ParseResult) -> bool:
    requires_lease, _problems = unsafe_url_reasons(parsed)
    return requires_lease


def is_loopback_origin(parsed: urllib.parse.ParseResult) -> bool:
    host = parsed.hostname or ""
    if not host or is_metadata_host(host):
        return False
    ips, resolve_problem = resolve_ips(host, parsed.port)
    return not resolve_problem and bool(ips) and all(ip.is_loopback for ip in ips)


def pid_is_alive(pid: Any) -> bool:
    try:
        number = int(pid)
        if number <= 0:
            return False
        os.kill(number, 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def lease_project_matches(project: Path, raw: Any) -> bool:
    raw = raw.get("path") or raw.get("root") or raw.get("project") if isinstance(raw, dict) else raw
    if raw is None:
        return False
    try:
        return Path(str(raw)).expanduser().resolve() == project.resolve()
    except OSError:
        return False


def validate_server_lease(
    project: Path,
    raw_lease: str | None,
    parsed_url: urllib.parse.ParseResult,
    source_hash: str,
    runtime_hash: str,
) -> tuple[Path | None, dict[str, Any] | None, list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    requires_lease, safety_problems = unsafe_url_reasons(parsed_url)
    hard_safety_problems = [item for item in safety_problems if item.get("rule") != "server-lease"]
    lease_required_problems = [item for item in safety_problems if item.get("rule") == "server-lease"]
    problems.extend(hard_safety_problems)
    if not raw_lease and not requires_lease:
        return None, None, problems
    if not raw_lease:
        problems.extend(lease_required_problems)
    if raw_lease and not is_loopback_origin(parsed_url):
        problems.append(problem("server leases are only valid for loopback browser URLs", rule="server-lease"))
    if is_loopback_origin(parsed_url) and not host_is_literal_loopback(parsed_url.hostname or ""):
        problems.append(problem("server leases require a literal loopback browser URL to avoid DNS rebinding", rule="server-lease"))
    lease_path: Path | None = None
    if raw_lease:
        try:
            lease_path = live_common.safe_project_path(project, raw_lease, must_exist=False)
        except ValueError as exc:
            return None, None, [problem(f"server lease path is unsafe: {exc}", rule="server-lease")]
    else:
        lease_path = project / ".starforge" / "runtime" / "server.json"
    rel = live_common.project_relative(project, lease_path)
    if not lease_path.exists():
        problems.append(problem("local browser URL requires a live Star Forge server lease", rule="server-lease", path=rel))
        return lease_path, None, problems
    try:
        payload = read_json_file(lease_path)
    except Exception as exc:
        problems.append(problem(f"server lease is malformed JSON: {exc}", rule="server-lease", path=rel))
        return lease_path, None, problems
    if not isinstance(payload, dict):
        problems.append(problem("server lease must be a JSON object", rule="server-lease", path=rel))
        return lease_path, None, problems
    expected_origin = normalize_origin(parsed_url)
    lease_origin = str(payload.get("origin") or payload.get("base_url") or "")
    if lease_origin:
        try:
            lease_origin = normalize_origin(urllib.parse.urlparse(lease_origin))
        except Exception:
            pass
    expected_port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
    try:
        lease_port = int(payload.get("port"))
    except (TypeError, ValueError):
        lease_port = -1
    problems.extend(
        problem(message, rule="server-lease", path=rel)
        for message in failed_checks(payload, descriptor(
            LEASE_VALUE_CHECKS, source_hash=source_hash, runtime_hash=runtime_hash
        ))
    )
    checks = (
        (not lease_project_matches(project, payload.get("project")), "server lease project does not match current project"),
        (lease_origin != expected_origin, "server lease origin does not match browser URL"),
        (lease_port != expected_port, "server lease port does not match browser URL"),
        (not pid_is_alive(payload.get("pid")), "server lease pid is not alive"),
    )
    problems.extend(problem(message, rule="server-lease", path=rel) for failed, message in checks if failed)
    return lease_path, payload, problems
