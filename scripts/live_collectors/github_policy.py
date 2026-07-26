"""Read-only command and connector policy for the GitHub collector."""

from __future__ import annotations

from live_collectors.github_identity import *  # noqa: F401,F403
from live_collectors.policy_data import (
    gh_pr_view_identity, gh_run_view_identity, parse_option_grammar,
)

def repository_identity(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        return text if re.fullmatch(r"[^/\s]+/[^/\s]+", text) else ""
    if not isinstance(value, Mapping):
        return ""
    keys = ("full_name", "fullName", "nameWithOwner", "name_with_owner",
            "repo", "repository")
    return first_text(*(value.get(key) for key in keys))
def repo_from_url(value: Any) -> str:
    parts = approved_github_url_parts(value)
    if len(parts) >= 3 and parts[0] == "repos":
        return f"{parts[1]}/{parts[2]}"
    blocked = {"pull", "pulls", "issues"}
    return f"{parts[0]}/{parts[1]}" if len(parts) >= 2 and parts[0] not in blocked else ""
def pr_from_url(value: Any) -> str:
    parts = approved_github_url_parts(value)
    if len(parts) >= 5 and parts[0] == "repos" and parts[3] in {"pull", "pulls"}:
        return parts[4]
    return parts[3] if len(parts) >= 4 and parts[2] in {"pull", "pulls"} else ""
def gh_api_endpoint_allowed(
    endpoint: str, *, repo: str, pr_number: str,
    check_runs: Any = None, captured_head: str = "",
) -> bool:
    normalized = normalize_gh_api_endpoint(endpoint)
    repo_prefix = f"repos/{str(repo or '').strip().strip('/')}"
    pr_number = str(pr_number or "").strip()
    if not normalized or repo_prefix == "repos/" or not pr_number:
        return False
    pull = f"{repo_prefix}/pulls/{pr_number}"
    if normalized in {pull, *(f"{pull}/{part}" for part in
                            ("files", "reviews", "comments", "commits"))}:
        return True
    if normalized == f"{repo_prefix}/issues/{pr_number}/comments":
        return True
    escaped = re.escape(repo_prefix)
    match = re.fullmatch(rf"{escaped}/commits/([^/]+)/check-runs", normalized)
    if match:
        return bool(captured_head and match.group(1) == captured_head)
    bound = pr_bound_ci_ids(check_runs, captured_head=captured_head)
    patterns = (
        (rf"{escaped}/check-runs/([^/]+)/annotations", "check_runs"),
        (rf"{escaped}/actions/runs/([^/]+)/(?:logs|jobs)", "runs"),
        (rf"{escaped}/actions/jobs/([^/]+)/logs", "jobs"),
    )
    return any(
        match.group(1) in bound[bucket]
        for pattern, bucket in patterns
        if (match := re.fullmatch(pattern, normalized))
    )
def gh_api_endpoint_allows_ampersand(tokens: Sequence[str], token_index: int) -> bool:
    if list(tokens[:2]) != ["gh", "api"] or gh_api_endpoint_index(tokens) != token_index:
        return False
    endpoint = str(tokens[token_index])
    parsed = urllib.parse.urlsplit(endpoint.strip())
    return bool(
        parsed.query and "&" in parsed.query
        and not gh_api_endpoint_is_absolute(endpoint)
        and not gh_api_endpoint_query_problems(endpoint)
    )
def validate_no_shell_control(tokens: Sequence[str]) -> list[dict[str, Any]]:
    items = list(map(str, tokens))
    unsafe = any(
        GH_COMMAND_SHELL_CONTROL_RE.search(token)
        or "&" in token and not gh_api_endpoint_allows_ampersand(items, index)
        for index, token in enumerate(items)
    )
    return [command_problem("gh command must not contain shell control tokens")] if unsafe else []

def parse_gh_option_grammar(
    command_name: str, tokens: Sequence[str], *, value_flags: set[str],
    value_prefixes: Sequence[str], flag_only: set[str],
    attached_short_value_flags: Sequence[str] = (),
    forbidden_value_flags: set[str] | None = None,
    forbidden_value_prefixes: Sequence[str] = (),
    forbidden_attached_short_value_flags: Sequence[str] = (),
) -> tuple[list[str], dict[str, list[str]], list[dict[str, Any]]]:
    return parse_option_grammar(
        command_name, tokens, value_flags=value_flags,
        value_prefixes=value_prefixes, flag_only=flag_only,
        problem=command_problem,
        attached_short_value_flags=attached_short_value_flags,
        forbidden_value_flags=forbidden_value_flags,
        forbidden_value_prefixes=forbidden_value_prefixes,
        forbidden_attached_short_value_flags=forbidden_attached_short_value_flags,
    )

def require_exactly_one_positional(
    command_name: str, positionals: Sequence[str], subject: str
) -> list[dict[str, Any]]:
    message = (
        f"{command_name} must name {subject}" if not positionals else
        f"{command_name} has extra positional arguments after {subject}"
        if len(positionals) > 1 else ""
    )
    return [command_problem(message)] if message else []

def _validate_view(
    tokens: list[str], *, kind: str, repo: str, pr_number: str = "",
    check_runs: Any = None, captured_head: str = "",
) -> list[dict[str, Any]]:
    name = f"gh {kind} view"
    sub = tokens[2] if len(tokens) > 2 else ""
    if sub != "view":
        forbidden = kind == "pr" and sub == "checkout" or kind == "run" and sub in GH_RUN_MUTATIONS
        reason = (f"gh {kind} {sub} is forbidden" if forbidden else
                  f"gh {kind} {sub or '<missing>'} is not read-only allowlisted")
        return [command_problem(reason)]
    is_run = kind == "run"
    flags = GH_RUN_VIEW_VALUE_FLAGS if is_run else GH_PR_VIEW_VALUE_FLAGS
    prefixes = GH_RUN_VIEW_VALUE_PREFIXES if is_run else GH_PR_VIEW_VALUE_PREFIXES
    positionals, _, problems = parse_gh_option_grammar(
        name, tokens[3:], value_flags=flags, value_prefixes=prefixes,
        flag_only=GH_RUN_VIEW_FLAG_ONLY if is_run else set(),
    )
    subject = "a workflow run id" if is_run else "the requested PR"
    problems += require_exactly_one_positional(name, positionals, subject)
    value = positionals[0] if positionals else ""
    command_repo = option_value(tokens, {"--repo", "-R"})
    run_bound = pr_bound_ci_ids(check_runs, captured_head=captured_head)["runs"] if is_run else set()
    checks = (
        ("--web" in tokens, f"{name} --web is not allowed for fixture evidence"),
        (bool(repo and not command_repo), f"{name} must name the requested repo with --repo"),
        (bool(repo and command_repo and command_repo != repo), f"{name} repo does not match --repo"),
        (is_run and not value, f"{name} must name a workflow run id"),
        (bool(is_run and value and not re.fullmatch(r"\d+", value)), f"{name} must use a numeric workflow run id"),
        (bool(is_run and value and re.fullmatch(r"\d+", value) and value not in run_bound),
         f"{name} run id is not bound to the requested PR head SHA"),
        (bool(not is_run and pr_number and not value), f"{name} must name the requested PR"),
        (bool(not is_run and pr_number and value and value != pr_number), f"{name} PR does not match --pr"),
    )
    problems.extend(command_problem(message) for failed, message in checks if failed)
    return problems

def _api_flags(tokens: list[str]) -> tuple[str, bool, bool, list[dict[str, Any]]]:
    method, has_field, has_input = "GET", False, False
    problems: list[dict[str, Any]] = []
    for index, token in enumerate(tokens):
        if token in {"--method", "-X"} and index + 1 < len(tokens):
            method = tokens[index + 1].upper()
        elif token.startswith("--method="):
            method = token.split("=", 1)[1].upper()
        elif token.startswith("-X") and len(token) > 2:
            method = token[2:].upper()
        if token in {"-H", "--header"} or token.startswith(("--header=", "-H")):
            problems.append(command_problem("gh api fixture commands must not include headers"))
        has_field |= token in GH_API_FIELD_FLAGS or token.startswith(GH_API_FIELD_PREFIXES)
        has_input |= token == "--input" or token.startswith("--input=")
    return method, has_field, has_input, problems

def _validate_api(
    tokens: list[str], repo: str, pr_number: str,
    check_runs: Any, captured_head: str,
) -> list[dict[str, Any]]:
    method, has_field, has_input, problems = _api_flags(tokens)
    positionals, _, grammar = parse_gh_option_grammar(
        "gh api", tokens[2:], value_flags=GH_API_ALLOWED_VALUE_FLAGS,
        value_prefixes=GH_API_ALLOWED_VALUE_PREFIXES,
        flag_only=GH_API_ALLOWED_FLAG_ONLY, attached_short_value_flags=("-X",),
        forbidden_value_flags=GH_API_FORBIDDEN_VALUE_FLAGS,
        forbidden_value_prefixes=("--field=", "--header=", "--input=", "--raw-field="),
        forbidden_attached_short_value_flags=("-F", "-H", "-f"),
    )
    problems += grammar
    problems += require_exactly_one_positional(
        "gh api", positionals, "one PR-scoped endpoint"
    )
    checks = (
        (method != "GET", f"gh api --method {method} is forbidden"),
        (has_input, "gh api fixture commands must not send input bodies"),
        (has_field, "gh api field arguments are not allowed for live evidence"),
        (any("mutation" in token.lower() for token in tokens), "gh api GraphQL mutations are forbidden"),
    )
    problems.extend(command_problem(message) for failed, message in checks if failed)
    endpoint = positionals[0] if positionals else ""
    if not endpoint:
        problems.append(command_problem("gh api command is missing an endpoint"))
    elif gh_api_endpoint_is_absolute(endpoint):
        problems.append(command_problem(
            "gh api fixture commands must use path-style endpoints, not absolute URLs"
        ))
    else:
        problems += gh_api_endpoint_query_problems(endpoint)
        if not gh_api_endpoint_allowed(
            endpoint, repo=repo, pr_number=pr_number,
            check_runs=check_runs, captured_head=captured_head,
        ):
            problems.append(command_problem(
                f"gh api endpoint {endpoint} is not PR-scoped for the requested repo and PR"
            ))
    return problems

def validate_gh_command(
    argv: Sequence[str], *, repo: str = "", pr_number: str = "",
    check_runs: Any = None, captured_head: str = "", github_host: str = "",
) -> list[dict[str, Any]]:
    tokens = list(map(str, argv))
    if not tokens or tokens[0] != "gh":
        return [command_problem("fixture command must be a gh argv array")]
    if len(tokens) < 2:
        return [command_problem("gh command is missing a subcommand")]
    problems = validate_no_shell_control(tokens)
    problems += validate_gh_hostname(tokens, github_host=github_host)
    top = tokens[1]
    if top == "pr":
        return problems + _validate_view(
            tokens, kind="pr", repo=repo, pr_number=str(pr_number)
        )
    if top == "api":
        return problems + _validate_api(tokens, repo, str(pr_number), check_runs, captured_head)
    if top == "run":
        return problems + _validate_view(
            tokens, kind="run", repo=repo, check_runs=check_runs,
            captured_head=captured_head,
        )
    qualifier = "not read-only allowlisted" if top in GH_TOP_LEVEL_MUTATIONS else (
        "not in the read-only allowlist"
    )
    return problems + [command_problem(f"gh {top} is {qualifier}")]

def connector_operation_repo_identity(operation: Mapping[str, Any]) -> str:
    repository = operation.get("repository")
    return first_text(
        operation.get("repo"), repository_identity(repository),
        repo_from_url(repository if isinstance(repository, str) else ""),
    )

def connector_operation_pr_identity(operation: Mapping[str, Any]) -> str:
    values = (operation.get("pull_request"), operation.get("pullRequest"))
    maps = [value if isinstance(value, Mapping) else {} for value in values]
    return first_text(
        operation.get("pr"), operation.get("pull_request_number"),
        operation.get("pullRequestNumber"),
        *(nested(value, "number") for value in maps),
        *(pr_from_url(value) for value in values if isinstance(value, str)),
        *(value for value in values
          if isinstance(value, (int, str)) and not pr_from_url(value)),
    )

def connector_operation_host_evidence(
    operation: Any, label: str
) -> list[tuple[str, str]]:
    if not isinstance(operation, Mapping):
        return []
    evidence = [
        item for key in ("host", "github_host")
        for item in github_host_evidence_from_value(operation.get(key), f"{label}.{key}")
    ]
    for key in (*GITHUB_URL_KEYS, *GITHUB_IDENTITY_URL_KEYS):
        value = operation.get(key)
        evidence += github_host_evidence_from_value(
            value, f"{label}.{key}", require_url_like=True
        )
        evidence += github_host_evidence_from_payload(value, f"{label}.{key}")
    return evidence

def connector_operation_url_identity_items(
    operation: Mapping[str, Any]
) -> list[tuple[str, str, bool]]:
    items: list[tuple[str, str, bool]] = []
    for key in (*GITHUB_URL_KEYS, *GITHUB_IDENTITY_URL_KEYS):
        value = operation.get(key)
        if isinstance(value, str) and value.strip():
            items.append((key, value, key in GITHUB_URL_KEYS))
        elif isinstance(value, Mapping):
            items += [
                (f"{key}.{nested_key}", nested_value, True)
                for nested_key in GITHUB_URL_KEYS
                if isinstance((nested_value := value.get(nested_key)), str)
                and nested_value.strip()
            ]
    return items

def validate_connector_operation_identity(
    operation: Any, *, repo: str, pr_number: str, github_host: str = "",
    label: str = "connector operation", require_identity: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(operation, Mapping):
        return []
    problems: list[dict[str, Any]] = []
    expected_repo, expected_pr = str(repo or "").strip(), str(pr_number or "").strip()
    checks = (
        (expected_repo, connector_operation_repo_identity(operation), "repository"),
        (expected_pr, connector_operation_pr_identity(operation), "PR"),
    )
    for expected, declared, subject in checks:
        if require_identity and expected and not declared:
            problems.append(command_problem(f"{label} must declare the requested {subject}"))
        if declared and expected and declared != expected:
            problems.append(command_problem(
                f"{label} {subject} does not match the requested PR"
            ))
    for key, url, require_url in connector_operation_url_identity_items(operation):
        problems += [
            command_problem(message)
            for message in github_url_identity_messages(
                url, f"{label} {key}", require_url=require_url
            )
        ]
        url_repo, url_pr = repo_from_url(url), pr_from_url(url)
        if url_repo and expected_repo and url_repo != expected_repo:
            problems.append(command_problem(
                f"{label} URL repository does not match the requested PR"
            ))
        if url_pr and expected_pr and url_pr != expected_pr:
            problems.append(command_problem(f"{label} URL PR does not match the requested PR"))
    expected_host, approved = canonical_github_host(github_host), False
    for evidence_label, raw_host in connector_operation_host_evidence(operation, label):
        host = canonical_github_host(raw_host)
        if not host:
            continue
        if host not in APPROVED_GITHUB_HOSTS:
            problems.append(command_problem(
                f"{evidence_label} {host} is not an approved GitHub host"
            ))
        elif expected_host and host != expected_host:
            problems.append(command_problem(
                f"{evidence_label} does not match recorded GitHub provenance"
            ))
        else:
            approved = True
    if require_identity and not approved:
        problems.append(command_problem(
            f"{label} must include approved GitHub host or URL evidence"
        ))
    return problems

def validate_connector_operation(
    operation: Any, *, repo: str = "", pr_number: str = "",
    check_runs: Any = None, captured_head: str = "", github_host: str = "",
    require_identity: bool = False,
) -> list[dict[str, Any]]:
    if isinstance(operation, str):
        if require_identity:
            return [command_problem(
                "connector operation must be a structured object with repo, PR, and host evidence"
            )]
        name, action = operation, "read"
    elif isinstance(operation, Mapping):
        name = str(operation.get("operation") or operation.get("name")
                   or operation.get("kind") or "")
        action = str(operation.get("action") or operation.get("mode") or "read")
    else:
        return [command_problem("connector operation must be a string or object")]
    normalized = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    reason = (
        "is not read-only" if action.lower() not in {"read", "get", "list"} else
        "is not read-only allowlisted" if normalized not in CONNECTOR_READ_OPERATIONS else ""
    )
    if reason:
        return [command_problem(f"connector operation {name or '<missing>'} {reason}")]
    label = f"connector operation {name or '<missing>'}"
    problems = validate_connector_operation_identity(
        operation, repo=repo, pr_number=pr_number, github_host=github_host,
        label=label, require_identity=require_identity,
    )
    if normalized not in {"logs", "ci_logs"}:
        return problems
    if not isinstance(operation, Mapping):
        return [command_problem(
            f"{label} must be structured with repo, PR, head SHA, and CI identity"
        )]
    return problems + validate_ci_log_identity(
        operation, repo=repo, pr_number=pr_number, captured_head=captured_head,
        check_runs=check_runs, label=label, rule="github-command",
    )

def validate_read_only(
    raw: RawEvidence, *, repo: str, pr_number: str,
    captured_head: str = "", github_host: str = "",
) -> list[dict[str, Any]]:
    return [
        problem for command in raw.commands
        for problem in validate_gh_command(
            command, repo=repo, pr_number=str(pr_number),
            check_runs=raw.check_runs, captured_head=captured_head,
            github_host=github_host,
        )
    ] + [
        problem for operation in raw.operations
        for problem in validate_connector_operation(
            operation, repo=repo, pr_number=str(pr_number),
            check_runs=raw.check_runs, captured_head=captured_head,
            github_host=github_host, require_identity=is_live_source(raw.source),
        )
    ]
