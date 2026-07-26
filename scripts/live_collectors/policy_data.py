"""Typed access to package-owned declarative collector policy data."""

from __future__ import annotations

import json
import re
import shlex
from functools import lru_cache, partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence
from live_collectors.provider_engine import failed_checks, nested_value, render_descriptor


@lru_cache(maxsize=1)
def _policies() -> dict[str, Any]:
    path = Path(__file__).with_name("collector_policy.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _value(section: str, name: str, *, kind: str, cast: Callable[[Any], Any]) -> Any:
    record = _policies()[section][name]
    if record.get("kind") != kind:
        raise ValueError(f"collector policy {section}.{name} must be {kind}")
    return cast(record["value"])


POLICY_CASTS = {"dict": dict, "list": list, "set": set, "tuple": tuple}
for _kind, _cast in POLICY_CASTS.items():
    globals()[f"policy_{_kind}"] = partial(_value, kind=_kind, cast=_cast)


def policy_bindings(section: str, *names: str) -> dict[str, Any]:
    records = _policies()[section]
    return {
        name: POLICY_CASTS[records[name]["kind"]](records[name]["value"])
        for name in names
    }


def _github() -> Any:
    from live_collectors import github_identity
    return github_identity


def first_text(*values: Any) -> str:
    return next((
        value.strip() if isinstance(value, str) else str(value)
        for value in values if isinstance(value, str) and value.strip()
        or isinstance(value, (int, float)) and not isinstance(value, bool)
    ), "")


nested = nested_value


def first_path_text(mapping: Mapping[str, Any], *paths: str | Sequence[str]) -> str:
    return first_text(*(
        nested(mapping, path) if isinstance(path, str) else nested(mapping, *path)
        for path in paths
    ))


def _page_items(payload: Any, keys: Sequence[str]) -> list[Any]:
    if not isinstance(payload, Mapping):
        return list(payload) if isinstance(payload, list) else []
    if key := next((name for name in keys if name in payload), ""):
        return _page_items(payload.get(key), ("nodes", "edges"))
    values = payload.get("nodes")
    if isinstance(values, list):
        return list(values)
    edges = payload.get("edges")
    return [
        edge.get("node") if isinstance(edge, Mapping) and "node" in edge else edge
        for edge in edges
    ] if isinstance(edges, list) else []


def _page_flags(payload: Mapping[str, Any], count: int) -> tuple[bool, bool]:
    raw_info = payload.get("page_info") or payload.get("pageInfo") or payload.get("pagination")
    info = raw_info if isinstance(raw_info, Mapping) else {}
    partial = any(payload.get(key) for key in ("partial_permissions", "permission_partial", "partial"))
    incomplete = any(payload.get(key) for key in ("incomplete_results", "pagination_incomplete"))
    incomplete |= any(info.get(key) for key in ("has_next_page", "hasNextPage", "incomplete", "pagination_incomplete"))
    expected = (info.get("expected_total_count") or info.get("total_count")
                or payload.get("expected_total_count") or payload.get("total_count"))
    return bool(partial), bool(incomplete or isinstance(expected, int) and expected > count)

unwrap_edges = lambda value: _page_items(value, ("nodes", "edges"))
def pagination_flags(payload: Any, item_count: int) -> tuple[bool, bool]:
    return _page_flags(payload, item_count) if isinstance(payload, Mapping) else (False, False)

def flatten_paginated(payload: Any, keys: Sequence[str]) -> tuple[list[Any], bool, bool]:
    if not isinstance(payload, Mapping):
        return (list(payload), False, False) if isinstance(payload, list) else ([], False, False)
    pages = payload.get("pages")
    if not isinstance(pages, list):
        items = _page_items(payload, keys)
        return items, *_page_flags(payload, len(items))
    page_items = [(_page_items(page, keys), page) for page in pages]
    items = [item for values, _page in page_items for item in values]
    flags = [_page_flags(payload, 0), *(
        _page_flags(page, len(values)) for values, page in page_items
        if isinstance(page, Mapping)
    )]
    partial, incomplete = (any(flag[index] for flag in flags) for index in (0, 1))
    expected = payload.get("expected_total_count") or payload.get("total_count")
    return items, partial, incomplete or isinstance(expected, int) and expected > len(items)

def shell_argv(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            tokens = shlex.split(raw)
        except ValueError:
            return ["<malformed-gh-command>"]
        return [*tokens, "\n"] if "\n" in raw or "\r" in raw else tokens
    if isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        return list(map(str, raw))
    return []


def parse_commands(raw: Any) -> list[list[str]]:
    items = [] if raw is None else raw if isinstance(raw, list) else [raw]
    argvs = (
        item.get("argv") or item.get("command") or item.get("cmd")
        if isinstance(item, dict) else item for item in items
    )
    return [parsed for argv in argvs if (parsed := shell_argv(argv))]


def option_values(tokens: Sequence[str], names: set[str]) -> list[str]:
    return [
        str(tokens[index + 1]) for index, token in enumerate(tokens[:-1]) if token in names
    ] + [
        str(token).split("=", 1)[1] for token in tokens
        for name in names if str(token).startswith(f"{name}=")
    ]


def option_value(tokens: Sequence[str], names: set[str]) -> str:
    return next(iter(option_values(tokens, names)), "")


def _attached_value(
    token: str, prefixes: Sequence[str], short_flags: Sequence[str],
) -> tuple[str, str]:
    prefixed = next((
        (item[:-1], token.split("=", 1)[1]) for item in prefixes if token.startswith(item)
    ), None)
    return prefixed or next((
        (item, token[len(item):]) for item in short_flags
        if token.startswith(item) and len(token) > len(item)
    ), ("", ""))


def parse_option_grammar(
    command_name: str,
    tokens: Sequence[str],
    *,
    value_flags: set[str],
    value_prefixes: Sequence[str],
    flag_only: set[str],
    problem: Callable[[str], Any],
    attached_short_value_flags: Sequence[str] = (),
    forbidden_value_flags: set[str] | None = None,
    forbidden_value_prefixes: Sequence[str] = (),
    forbidden_attached_short_value_flags: Sequence[str] = (),
) -> tuple[list[str], dict[str, list[str]], list[Any]]:
    """Parse an argv suffix using declarative value and flag allowlists."""

    positionals, values, problems = [], {}, []
    items, forbidden = iter(map(str, tokens)), forbidden_value_flags or set()
    for token in items:
        bad_flag, _ = _attached_value(token, forbidden_value_prefixes, forbidden_attached_short_value_flags)
        if token in forbidden or bad_flag:
            flag = token if token in forbidden else bad_flag
            problems.append(problem(f"{command_name} flag {flag} is not read-only allowlisted"))
            if token in forbidden:
                next(items, None)
        elif token in value_flags:
            value = next(items, None)
            if value is None:
                problems.append(problem(f"{command_name} flag {token} requires a value"))
            else:
                values.setdefault(token, []).append(value)
        else:
            key, value = _attached_value(token, value_prefixes, attached_short_value_flags)
            if key:
                values.setdefault(key, []).append(value)
            elif token not in flag_only:
                if token.startswith("-"):
                    problems.append(problem(f"{command_name} flag {token} is not read-only allowlisted"))
                else:
                    positionals.append(token)
    return positionals, values, problems


def foundation_check_detail(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    detail = nested(payload, "checks", name, "detail")
    if isinstance(detail, Mapping):
        return detail
    direct = payload.get(name)
    detail = direct.get("detail") if isinstance(direct, Mapping) else None
    return detail if isinstance(detail, Mapping) else direct if isinstance(direct, Mapping) else {}


def github_remote_matches(remote_url: Any, repo: str) -> bool:
    if not isinstance(remote_url, str) or not remote_url.strip() or re.match(r"^https?://[^/@\s]+@", remote_url):
        return False
    escaped = re.escape(repo)
    return any(re.fullmatch(pattern, remote_url) for pattern in (
        rf"https://github\.com/{escaped}(?:\.git)?",
        rf"git@github\.com:{escaped}(?:\.git)?",
    ))


def normalize_github_foundation(
    raw: Any, *, repo: str, current_source_hash: str,
    problems: list[dict[str, Any]],
) -> dict[str, Any]:
    """Normalize the declarative GitHub Foundation policy record."""

    gh = _github()
    payload = raw.foundation_provenance
    if not payload:
        return policy_dict("github_adapter", "FOUNDATION_NOT_APPLICABLE")
    sections = {"payload": payload, **{
        section: foundation_check_detail(payload, name)
        for section, name in policy_dict("github_adapter", "FOUNDATION_SECTIONS").items()
    }}
    repository, remote, branch, initial, ci = (
        sections[name] for name in ("repository", "remote", "branch", "initial", "ci")
    )
    extracted = {
        field: first_text(
            sections[section].get(key), *(payload.get(name) for name in fallbacks),
            payload.get(field),
        )
        for field, (section, key, *fallbacks) in policy_dict(
            "github_adapter", "FOUNDATION_FIELDS"
        ).items()
    }
    state = SimpleNamespace(**extracted)
    selected_provider = github_provider(raw)
    if (
        selected_provider == "github-unavailable"
        and raw.source not in {"connector-fixture", "gh-fixture"}
    ):
        extracted["provider"] = selected_provider
        state.provider = selected_provider
        state.fallback = ""
    owner, _, name = repo.partition("/")
    state.visibility = state.visibility.lower()
    tree_hash = first_text(
        initial.get("tree_source_hash"), payload.get("tree_source_hash"), state.source_hash
    )
    created = repository.get("created", payload.get("created"))
    validation = {
        **extracted, "visibility": state.visibility, "tree_hash": tree_hash,
        "schema": first_text(payload.get("schema")) or "star-forge.foundation-provenance.v1",
        "provider_route": {**provider_route(state.provider), "recorded_fallback": state.fallback},
        "repo": repo, "created": created if isinstance(created, bool) else None,
        "create_fallback_valid": state.provider != gh.GH_CREATE_PROVIDER or state.fallback == gh.GH_CREATE_FALLBACK,
        "created_private_valid": created is not True or state.visibility == "private",
        "readonly_create_valid": state.provider != gh.GH_READONLY_PROVIDER or created is not True,
        "adoption_visibility_valid": created is not False or repository.get("visibility_changed") is False,
        "identity_verified": repository.get("identity_verified") is True,
        "visibility_verified": repository.get("visibility_verified") is True,
        "remote_valid": github_remote_matches(state.remote_url, repo),
        "branch_exists": branch.get("exists") is True,
        "heads_match": not state.head_commit or not state.current_head or state.head_commit == state.current_head,
        "ci_committed": ci.get("committed") is True,
    }
    problems += [
        gh.blocking_problem(message, rule="github-foundation-provenance")
        for message in failed_checks(validation, render_descriptor(
            policy_list("github_adapter", "FOUNDATION_CHECKS"),
            current_source_hash=current_source_hash, owner=owner, name=name,
        ))
    ]
    return render_descriptor(
        policy_dict("github_adapter", "FOUNDATION_OUTPUT_TEMPLATE"), validation
    )


def github_provider(raw: Any) -> str:
    return "github-unavailable"


def provider_route(provider: str) -> dict[str, Any]:
    return render_descriptor(
        policy_dict("github_adapter", "PROVIDER_ROUTE_TEMPLATE"),
        provider=provider, fallback=provider != _github().PREFERRED_PROVIDER,
    )


def write_github_evidence_envelope(
    project: Path, manifest_path: Path, *, raw: Any, repo: str, pr_number: str,
    github_host: str, pr_url: str, captured_base: str, captured_head: str,
    current_base: str, current_head: str, foundation: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Write the policy-bound GitHub v2 evidence envelope."""

    gh = _github()
    provider = github_provider(raw)
    envelope = gh.evidence.adapt_v1_manifest(
        gh.read_json(manifest_path, {}), capability=gh.CAPABILITY, provider=provider
    )
    owner, _, name = repo.partition("/")
    values = {
        **locals(), "route": provider_route(provider), "source": raw.source,
        "pr_number": str(pr_number), "source_hash": envelope["source_hash"],
        "runtime_asset_hash": envelope["runtime_asset_hash"], "foundation": dict(foundation),
    }
    envelope["provenance"].update(
        render_descriptor(policy_dict("github_adapter", "ENVELOPE_PROVENANCE_TEMPLATE"), values)
    )
    envelope["provenance"], _ = gh.redact_artifact_payload(envelope["provenance"])
    blockers = policy_dict("github_adapter", "ENVELOPE_BLOCKERS")
    if provider != gh.PREFERRED_PROVIDER:
        envelope["blockers"].append({**blockers["fallback"], "selected_provider": provider})
        if envelope["verdict"] == "PASS":
            envelope["verdict"] = "DEGRADED"
    if raw.source in {"connector-fixture", "gh-fixture", "missing-fixture"}:
        envelope["blockers"].append(blockers["fixture"])
        envelope["verdict"] = "FAIL"
    envelope_path = manifest_path.parent / gh.EVIDENCE_FILENAME
    return envelope_path, gh.evidence.write_envelope(
        envelope_path, envelope, project_root=project, verify_artifacts=True,
    )


def _host_from(value: Any, method: str, label: str) -> str:
    evidence = getattr(_github(), method)(value, label)
    return evidence[0][1] if evidence else ""


def github_host_evidence_for_raw(raw: Any) -> list[tuple[str, str]]:
    gh = _github()
    return gh.github_host_provenance_evidence_for_raw(raw) + gh.github_host_payload_evidence_for_raw(raw)


def validate_transcript_github_host(**kwargs: Any) -> tuple[str, list[dict[str, Any]]]:
    gh = _github()
    host, messages = gh.validate_transcript_github_host_evidence(**kwargs)
    return host, [gh.blocking_problem(message, rule="github-live-provenance") for message in messages]


def gh_api_endpoint(tokens: Sequence[str]) -> str:
    index = _github().gh_api_endpoint_index(tokens)
    return str(tokens[index]) if index >= 0 else ""


def trusted_proof_command(command: Sequence[str]) -> list[str]:
    gh = _github()
    return gh.live_common.trusted_python_command(
        command, script_path=gh.STAR_FORGE_SCRIPT,
    )


def _gh_view_identity(tokens: Sequence[str], run: bool) -> tuple[str, str]:
    from live_collectors import github_policy as gh

    flags = gh.GH_RUN_VIEW_VALUE_FLAGS if run else gh.GH_PR_VIEW_VALUE_FLAGS
    prefixes = gh.GH_RUN_VIEW_VALUE_PREFIXES if run else gh.GH_PR_VIEW_VALUE_PREFIXES
    positionals, _, _ = gh.parse_gh_option_grammar(
        "gh view", tokens[3:], value_flags=flags, value_prefixes=prefixes,
        flag_only=gh.GH_RUN_VIEW_FLAG_ONLY if run else set(),
    )
    return (positionals[0] if positionals else ""), gh.option_value(tokens, {"--repo", "-R"})


for _name, (_method, _label) in policy_dict("github_adapter", "HOST_BINDINGS").items():
    globals()[_name] = partial(_host_from, method=_method, label=_label)
gh_pr_view_identity = partial(_gh_view_identity, run=False)
gh_run_view_identity = partial(_gh_view_identity, run=True)
