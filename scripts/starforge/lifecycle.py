"""Pure lifecycle contracts and gates for Star Forge foundation and delivery.

This module describes authorized lifecycle outcomes and validates evidence captured
by orchestrators or proof adapters. It intentionally performs no Git, GitHub,
filesystem, subprocess, or network mutation.
"""

from __future__ import annotations
from .policy_data import value as _policy_value

import datetime as dt
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence
from .validation import boolean_fields, flag as _flag, mapping_sections, rules
FOUNDATION_CONTRACT_SCHEMA = "star-forge.foundation-contract.v1"
FOUNDATION_EVIDENCE_SCHEMA = "star-forge.foundation-evidence.v1"
FOUNDATION_GATE_SCHEMA = "star-forge.foundation-gate.v1"
DELIVERY_CONTRACT_SCHEMA = "star-forge.delivery-contract.v1"
DELIVERY_EVIDENCE_SCHEMA = "star-forge.delivery-evidence.v1"
DELIVERY_GATE_SCHEMA = "star-forge.delivery-gate.v1"
TARGET_LIFECYCLE = _policy_value('lifecycle.TARGET_LIFECYCLE')
LEGACY_LIFECYCLE = ("plan", "build", "review", "done")
COMPATIBILITY_PHASES = frozenset({*TARGET_LIFECYCLE, *LEGACY_LIFECYCLE, "setup", "blocked", "amend"})
FOUNDATION_CONTRACT_PATH = ".starforge/foundation/contract.json"
FOUNDATION_EVIDENCE_PATH = ".starforge/foundation/evidence.json"
DELIVERY_CONTRACT_PATH = ".starforge/delivery/contract.json"
DELIVERY_EVIDENCE_PATH = ".starforge/delivery/evidence.json"
REQUIREMENT_STATES = frozenset({"requested", "not-applicable", "blocking"})
EVIDENCE_STATES = frozenset({"satisfied", "not-applicable", "blocking"})
FOUNDATION_REQUIREMENTS = _policy_value('lifecycle.FOUNDATION_REQUIREMENTS')
DELIVERY_REQUIREMENTS = _policy_value('lifecycle.DELIVERY_REQUIREMENTS')
GENERIC_DELIVERY_TARGETS = frozenset({"source-only", "private-repo", "preview", "production", "package"})
WEB_DELIVERY_TARGETS = frozenset({"preview", "production"})
WEB_DELIVERY_PROVIDERS = frozenset({"sites", "vercel"})
DELIVERY_IDENTITY_KINDS = frozenset({
    "source-handoff",
    "repository",
    "deployment",
    "package",
    "platform-release",
})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^(?!/)(?!.*(?:\.\.|//|@\{|\\|\s))(?!.*[/.]$).+$")
_SENSITIVE_FIELD_RE = re.compile(
    r"(?:^|_)(?:access_token|api_key|authorization|client_secret|credential|"
    r"password|private_key|refresh_token|token)(?:$|_)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_GITHUB_PROVIDERS = frozenset({"github-connector", "gh-cli"})
_ADOPTION_PROVIDERS = frozenset({"github-connector", "gh-readonly"})
_NAMED_TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_SITES_FIT_RE = re.compile(r"\b(?:simple|static|internal|landing|documentation|docs|portal|dashboard)\b")
_VERCEL_FIT_RE = re.compile(r"\b(?:next(?:\.js)?|react|full[- ]stack|server[- ]rendered|ssr|ai|production)\b")

class LifecycleContractError(ValueError):
    """A lifecycle contract cannot be represented safely."""

def resolve_phase(
    *,
    legacy: bool,
    setup_complete: bool,
    blocked: bool,
    intake_complete: bool,
    design_required: bool | None,
    design_complete: bool,
    plan_complete: bool,
    foundation_complete: bool,
    amendment_required: bool,
    build_complete: bool,
    review_complete: bool,
    delivery_complete: bool,
    completion_complete: bool,
) -> str:
    """Resolve one canonical phase while preserving the v0.3 phase sequence.
    Compatibility projects never acquire intake, design, foundation, or deliver
    gates retroactively. Modern projects advance only when each target lifecycle
    gate has passed. Amend remains an out-of-band re-entry after planning and
    foundation are established.
    """
    transitions = [
        ("setup", not setup_complete),
        ("blocked", blocked),
    ]
    if legacy:
        transitions.extend([
            ("plan", not plan_complete), ("amend", amendment_required),
            ("build", not build_complete), ("review", not review_complete),
            ("review", not completion_complete),
        ])
    else:
        transitions.extend([
            ("intake", not intake_complete),
            ("design", design_required is not False and not design_complete),
            ("plan", not plan_complete), ("foundation", not foundation_complete),
            ("amend", amendment_required), ("build", not build_complete),
            ("review", not review_complete), ("deliver", not delivery_complete),
        ])
    for phase, active in transitions:
        if active:
            return phase
    return "done"

@dataclass(frozen=True)
class FoundationGate:
    """Deterministic decision about whether feature work may begin."""
    status: str
    ready_for_feature_work: bool
    source_hash: str | None
    contract_sha256: str | None
    checks: Mapping[str, str]
    blockers: tuple[str, ...]
    def to_dict(self) -> dict[str, Any]:
        return _gate_dict(self, FOUNDATION_GATE_SCHEMA)

@dataclass(frozen=True)
class DeliveryGate:
    """Deterministic decision about strict completion eligibility."""
    status: str
    delivery_satisfied: bool
    ready_for_completion: bool
    source_hash: str | None
    repository_commit: str | None
    contract_sha256: str | None
    target: str | None
    provider: str | None
    checks: Mapping[str, str]
    blockers: tuple[str, ...]
    def to_dict(self) -> dict[str, Any]:
        return _gate_dict(self, DELIVERY_GATE_SCHEMA)

def _gate_dict(gate: Any, schema: str) -> dict[str, Any]:
    payload = asdict(gate)
    payload["checks"], payload["blockers"] = dict(gate.checks), list(gate.blockers)
    return {"schema": schema, **payload}

def _requirement(state: str, reason: str) -> dict[str, str]:
    return {"state": state, "reason": reason}

def _optional_requirement(required: bool, required_reason: str, absent_reason: str) -> dict[str, str]:
    return _requirement("requested" if required else "not-applicable", required_reason if required else absent_reason)

def _requirement_problems(requirements: Mapping[str, Any], names: Sequence[str]) -> list[str]:
    problems: list[str] = []
    missing = [name for name in names if name not in requirements]
    extra = sorted(str(name) for name in set(requirements) - set(names))
    _flag(problems, bool(missing), "missing requirements: " + ", ".join(missing))
    _flag(problems, bool(extra), "unknown requirements: " + ", ".join(extra))
    for name in names:
        item = requirements.get(name)
        if not isinstance(item, Mapping):
            _flag(problems, name in requirements, f"requirement {name} must be an object")
            continue
        _flag(
            problems,
            item.get("state") not in REQUIREMENT_STATES,
            f"requirement {name} has invalid state",
        )
        reason = item.get("reason")
        _flag(
            problems,
            not isinstance(reason, str) or not reason.strip(),
            f"requirement {name} requires a reason",
        )
    return problems

def make_foundation_contract(
    *,
    github_requested: bool,
    repository_mode: str = "not-applicable",
    owner: str = "",
    repository: str = "",
    visibility: str = "not-applicable",
    repository_write_authorized: bool = False,
    default_branch: str = "main",
    ci_path: str = ".github/workflows/ci.yml",
    environment_example_required: bool = True,
    dependency_audit_required: bool = True,
    security_plan_required: bool = True,
) -> dict[str, Any]:
    """Build a Foundation Contract from an approved Repository Contract.
    A missing authorization or identity is represented as a blocking state so
    the caller can present one honest foundation blocker. No operation is run.
    """
    mode = str(repository_mode or "").strip().lower()
    expected_visibility = str(visibility or "").strip().lower()
    repo_blocker = ""
    if github_requested:
        if mode not in {"create", "adopt"}:
            repo_blocker = "GitHub foundation requires create or adopt mode"
        elif not repository_write_authorized:
            repo_blocker = "approved repository write authority is missing"
        elif not owner or not repository:
            repo_blocker = "approved GitHub owner and repository identity are required"
        elif mode == "create" and expected_visibility != "private":
            repo_blocker = "new GitHub repositories must be private"
        elif mode == "adopt" and expected_visibility not in {"private", "public"}:
            repo_blocker = "adopted repository visibility must be explicit"
    repository_state = "not-applicable" if not github_requested else ("blocking" if repo_blocker else "requested")
    github_reason = "approved contract does not request GitHub" if not github_requested else (repo_blocker or "approved Repository Contract requests GitHub")
    requirements = {
        "source_scaffold": _requirement("requested", "source scaffold must exist before feature work"),
        "local_git": _requirement("requested", "local Git initialization is an automatic foundation outcome"),
        "github_repository": _requirement(repository_state, github_reason),
        "remote_origin": _requirement(repository_state, github_reason),
        "default_branch": _requirement("requested", "the initial commit must establish the approved default branch"),
        "initial_commit": _requirement("requested", "foundation artifacts must be committed before feature work"),
        "ci": _requirement("requested", "CI configuration must be installed before feature work"),
        "environment_example": _optional_requirement(environment_example_required, "an environment example is required", "the project has no configurable environment"),
        "secret_scan": _requirement("requested", "the committed foundation requires a clean secret scan"),
        "dependency_audit": _optional_requirement(dependency_audit_required, "declared dependencies require an audit", "the foundation declares no third-party dependencies"),
        "security_plan": _optional_requirement(security_plan_required, "risk flags require a threat model or security plan", "approved risk flags make a security plan not applicable"),
    }
    return {
        "schema": FOUNDATION_CONTRACT_SCHEMA,
        "repository": {
            "state": repository_state,
            "github_requested": github_requested,
            "mode": mode if github_requested else "not-applicable",
            "owner": owner if github_requested else "",
            "name": repository if github_requested else "",
            "visibility": expected_visibility if github_requested else "not-applicable",
            "write_authorized": repository_write_authorized if github_requested else False,
            "preferred_provider": "github-connector" if github_requested else "not-applicable",
            "create_fallback": "gh repo create --private" if github_requested else "not-applicable",
            "mutation_policy": "never-change-visibility",
        },
        "expectations": {
            "default_branch": default_branch,
            "ci_path": ci_path
        },
        "requirements": requirements,
    }

def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (not path.is_absolute() and value not in {".", ".."} and all(part not in {"", ".", ".."} for part in path.parts))

def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None

def _secret_problems(value: object, path: str = "evidence") -> list[str]:
    problems: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            nested_path = f"{path}.{key_text}"
            normalized_key = re.sub(r"[^a-z0-9]+", "_", key_text.lower()).strip("_")
            if _SENSITIVE_FIELD_RE.search(normalized_key):
                problems.append(f"{nested_path} must not contain secret material")
            problems.extend(_secret_problems(nested, nested_path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            problems.extend(_secret_problems(nested, f"{path}[{index}]"))
    elif isinstance(value, str):
        if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
            problems.append(f"{path} contains likely secret material")
        if re.match(r"^https?://[^/@\s]+@", value):
            problems.append(f"{path} must not embed credentials in a URL")
    return problems

def validate_foundation_contract(contract: object) -> list[str]:
    """Validate contract structure, states, authority, and mutation boundaries."""
    if not isinstance(contract, Mapping):
        return ["foundation contract must be an object"]
    problems: list[str] = []
    if contract.get("schema") != FOUNDATION_CONTRACT_SCHEMA:
        problems.append(f"schema must be {FOUNDATION_CONTRACT_SCHEMA}")
    sections = mapping_sections(contract, ("repository", "expectations", "requirements"), problems)
    repository, expectations, requirements = sections["repository"], sections["expectations"], sections["requirements"]
    problems.extend(_requirement_problems(requirements, FOUNDATION_REQUIREMENTS))
    branch = expectations.get("default_branch")
    if not isinstance(branch, str) or not _BRANCH_RE.fullmatch(branch):
        problems.append("expectations.default_branch is invalid")
    ci_path = expectations.get("ci_path")
    if (not _safe_relative_path(ci_path) or PurePosixPath(str(ci_path)).parent != PurePosixPath(".github/workflows") or
            PurePosixPath(str(ci_path)).suffix not in {".yml", ".yaml"}):
        problems.append("expectations.ci_path must be a workflow YAML path")
    github_requested = repository.get("github_requested")
    repo_state = repository.get("state")
    if not isinstance(github_requested, bool):
        problems.append("repository.github_requested must be boolean")
    if repo_state not in REQUIREMENT_STATES:
        problems.append("repository.state is invalid")
    for name in ("github_repository", "remote_origin"):
        item = requirements.get(name)
        if isinstance(item, Mapping) and item.get("state") != repo_state:
            problems.append(f"requirement {name} must match repository.state")
    if repository.get("mutation_policy") != "never-change-visibility":
        problems.append("repository visibility mutation must be forbidden")
    if github_requested:
        mode = repository.get("mode")
        visibility = repository.get("visibility")
        if mode not in {"create", "adopt"}:
            problems.append("repository.mode must be create or adopt")
        if not _OWNER_RE.fullmatch(str(repository.get("owner") or "")):
            problems.append("repository.owner is invalid")
        if not _REPOSITORY_RE.fullmatch(str(repository.get("name") or "")):
            problems.append("repository.name is invalid")
        if mode == "create" and visibility != "private":
            problems.append("new GitHub repositories must be private")
        if mode == "adopt" and visibility not in {"private", "public"}:
            problems.append("adopted repository visibility must be explicit")
        if repository.get("preferred_provider") != "github-connector":
            problems.append("GitHub connector must be the preferred provider")
        if repository.get("create_fallback") != "gh repo create --private":
            problems.append("repository creation fallback must be gh repo create --private")
        if repo_state == "requested" and repository.get("write_authorized") is not True:
            problems.append("requested GitHub foundation requires approved write authority")
    else:
        if repo_state != "not-applicable":
            problems.append("repository must be not-applicable when GitHub is not requested")
        if repository.get("write_authorized") is not False:
            problems.append("local-only foundation cannot claim repository write authority")
    problems.extend(_secret_problems(contract, "contract"))
    return problems

def foundation_contract_sha256(contract: Mapping[str, Any]) -> str:
    """Hash a valid contract using deterministic JSON encoding."""
    problems = validate_foundation_contract(contract)
    if problems:
        raise LifecycleContractError("; ".join(problems))
    serialized = json.dumps(contract, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()

def _evidence_detail(evidence: Mapping[str, Any], name: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    checks = evidence.get("checks")
    if not isinstance(checks, Mapping):
        return {}, {}
    record = checks.get(name)
    if not isinstance(record, Mapping):
        return {}, {}
    detail = record.get("detail")
    return record, detail if isinstance(detail, Mapping) else {}

def _artifact_problem(
    detail: Mapping[str, Any],
    *,
    required_path: str | None = None,
) -> str | None:
    path = detail.get("path")
    digest = detail.get("sha256")
    if not _safe_relative_path(path):
        return "requires a safe relative artifact path"
    if required_path is not None and path != required_path:
        return f"must reference {required_path}"
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        return "requires an artifact SHA-256"
    if detail.get("committed") is not True:
        return "artifact must be committed"
    return None

def _github_remote_matches(remote_url: object, owner: str, repository: str) -> bool:
    if not isinstance(remote_url, str) or not remote_url:
        return False
    if re.match(r"^https?://[^/@\s]+@", remote_url):
        return False
    escaped = re.escape(f"{owner}/{repository}")
    return bool(re.fullmatch(rf"https://github\.com/{escaped}(?:\.git)?", remote_url) or re.fullmatch(rf"git@github\.com:{escaped}(?:\.git)?", remote_url))

def _specific_evidence_problems(
    name: str,
    detail: Mapping[str, Any],
    contract: Mapping[str, Any],
    current_source_hash: str,
) -> list[str]:
    problems: list[str] = []
    repository = contract["repository"]
    expectations = contract["expectations"]
    if name == "source_scaffold":
        artifacts = detail.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            problems.append("source scaffold requires at least one committed artifact")
        else:
            for index, artifact in enumerate(artifacts):
                if not isinstance(artifact, Mapping):
                    problems.append(f"source scaffold artifact {index} must be an object")
                    continue
                artifact_problem = _artifact_problem(artifact)
                if artifact_problem:
                    problems.append(f"source scaffold artifact {index} {artifact_problem}")
    elif name == "local_git":
        if detail.get("is_repository") is not True:
            problems.append("local Git repository was not proven")
        if detail.get("method") not in {"automatic-local-init", "existing-repository"}:
            problems.append("local Git evidence must identify automatic init or existing adoption")
    elif name == "github_repository":
        mode = repository["mode"]
        provider = detail.get("provider")
        allowed = _GITHUB_PROVIDERS if mode == "create" else _ADOPTION_PROVIDERS
        if provider not in allowed:
            problems.append("GitHub evidence provider is not an allowed route")
        if detail.get("owner") != repository["owner"] or detail.get("name") != repository["name"]:
            problems.append("GitHub repository identity does not match the contract")
        if detail.get("visibility") != repository["visibility"]:
            problems.append("GitHub repository visibility does not match the contract")
        if detail.get("identity_verified") is not True:
            problems.append("GitHub repository identity was not verified")
        if detail.get("visibility_verified") is not True:
            problems.append("GitHub repository visibility was not verified")
        if mode == "create":
            if detail.get("created") is not True:
                problems.append("new GitHub repository creation was not proven")
            if provider == "gh-cli" and detail.get("fallback") != "gh repo create --private":
                problems.append("gh fallback must be exactly gh repo create --private")
        else:
            if detail.get("created") is not False or detail.get("read_only_adoption") is not True:
                problems.append("existing repository adoption must be read-only")
            if detail.get("visibility_changed") is not False:
                problems.append("existing repository visibility must not be changed")
    elif name == "remote_origin":
        if detail.get("remote") != "origin":
            problems.append("GitHub remote must be named origin")
        if not _github_remote_matches(detail.get("url"), repository["owner"], repository["name"]):
            problems.append("origin URL does not match the approved GitHub identity")
    elif name == "default_branch":
        if detail.get("name") != expectations["default_branch"]:
            problems.append("default branch does not match the contract")
        if detail.get("exists") is not True:
            problems.append("default branch existence was not proven")
        if not _COMMIT_RE.fullmatch(str(detail.get("head_commit") or "")):
            problems.append("default branch requires a valid head commit")
    elif name == "initial_commit":
        commit = str(detail.get("sha") or "")
        if not _COMMIT_RE.fullmatch(commit):
            problems.append("initial commit SHA is invalid")
        current_head = str(detail.get("current_head") or "")
        if not _COMMIT_RE.fullmatch(current_head):
            problems.append("current foundation head is invalid")
        if repository["mode"] != "adopt":
            if detail.get("parent_count") != 0:
                problems.append("new repository initial commit must have zero parents")
            if current_head != commit:
                problems.append("new repository initial commit must be the current foundation head")
        elif detail.get("exists") is not True:
            problems.append("adopted repository must prove that an initial commit exists")
        if detail.get("tree_source_hash") != current_source_hash:
            problems.append("initial commit tree is not bound to the current source hash")
    elif name == "ci":
        artifact_problem = _artifact_problem(detail, required_path=expectations["ci_path"])
        if artifact_problem:
            problems.append("CI " + artifact_problem)
    elif name == "environment_example":
        artifact_problem = _artifact_problem(detail)
        if artifact_problem:
            problems.append("environment example " + artifact_problem)
        suffixes = (".env.example", ".env.sample", "env.example", "env.sample")
        if not str(detail.get("path") or "").endswith(suffixes):
            problems.append("environment example path is not recognizable")
        if detail.get("contains_secret_values") is not False:
            problems.append("environment example must prove that it contains no secret values")
    elif name == "secret_scan":
        if detail.get("verdict") != "PASS" or detail.get("findings") != 0:
            problems.append("secret scan must pass with zero findings")
        if not str(detail.get("tool") or "").strip():
            problems.append("secret scan tool identity is required")
    elif name == "dependency_audit":
        if detail.get("verdict") != "PASS" or detail.get("unresolved_findings") != 0:
            problems.append("dependency audit must pass with zero unresolved findings")
        if not str(detail.get("tool") or "").strip():
            problems.append("dependency audit tool identity is required")
    elif name == "security_plan":
        artifact_problem = _artifact_problem(detail)
        if artifact_problem:
            problems.append("security plan " + artifact_problem)
        if detail.get("kind") not in {"threat-model", "security-plan"}:
            problems.append("security evidence must be a threat model or security plan")
    return problems

def evaluate_foundation(
    contract: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    current_source_hash: str,
) -> FoundationGate:
    """Gate feature work on complete, current, non-secret foundation evidence."""
    blockers: list[str] = []
    checks: dict[str, str] = {}
    contract_problems = validate_foundation_contract(contract)
    if contract_problems:
        blockers.extend(f"contract: {problem}" for problem in contract_problems)
        return FoundationGate(
            status="BLOCKED",
            ready_for_feature_work=False,
            source_hash=None,
            contract_sha256=None,
            checks=checks,
            blockers=tuple(blockers),
        )
    contract_hash = foundation_contract_sha256(contract)
    if not isinstance(evidence, Mapping):
        blockers.append("foundation evidence must be an object")
        evidence = {}
    if evidence.get("schema") != FOUNDATION_EVIDENCE_SCHEMA:
        blockers.append(f"evidence schema must be {FOUNDATION_EVIDENCE_SCHEMA}")
    if not _valid_timestamp(evidence.get("captured_at")):
        blockers.append("evidence captured_at must be an ISO-8601 timestamp with timezone")
    if not _SHA256_RE.fullmatch(str(current_source_hash or "")):
        blockers.append("current source hash must be a lowercase SHA-256 digest")
    if evidence.get("source_hash") != current_source_hash:
        blockers.append("foundation evidence source hash is stale")
    if evidence.get("contract_sha256") != contract_hash:
        blockers.append("foundation evidence is not bound to the current contract")
    blockers.extend(_secret_problems(evidence))
    evidence_checks = evidence.get("checks")
    if not isinstance(evidence_checks, Mapping):
        blockers.append("foundation evidence checks must be an object")
        evidence_checks = {}
    extra_checks = sorted(str(key) for key in set(evidence_checks) - set(FOUNDATION_REQUIREMENTS))
    if extra_checks:
        blockers.append("unknown foundation evidence checks: " + ", ".join(extra_checks))
    for name in FOUNDATION_REQUIREMENTS:
        requirement = contract["requirements"][name]
        required_state = requirement["state"]
        record, detail = _evidence_detail(evidence, name)
        evidence_state = record.get("state")
        if required_state == "blocking":
            checks[name] = "blocking"
            blockers.append(f"{name}: {requirement['reason']}")
            if evidence_state not in {None, "blocking"}:
                blockers.append(f"{name}: blocking contract cannot be satisfied by evidence")
            continue
        if required_state == "not-applicable":
            checks[name] = "not-applicable"
            if evidence_state != "not-applicable":
                blockers.append(f"{name}: evidence must record not-applicable")
            continue
        checks[name] = "satisfied" if evidence_state == "satisfied" else "blocking"
        if evidence_state != "satisfied":
            blockers.append(f"{name}: requested foundation evidence is missing")
            continue
        if record.get("source_hash") != current_source_hash:
            blockers.append(f"{name}: evidence is not bound to the current source hash")
        specific = _specific_evidence_problems(name, detail, contract, current_source_hash)
        blockers.extend(f"{name}: {problem}" for problem in specific)
        if specific:
            checks[name] = "blocking"
    _, branch_detail = _evidence_detail(evidence, "default_branch")
    _, commit_detail = _evidence_detail(evidence, "initial_commit")
    if (branch_detail and commit_detail and branch_detail.get("head_commit") != commit_detail.get("current_head")):
        blockers.append("default_branch: head commit does not match the foundation head")
        checks["default_branch"] = "blocking"
        checks["initial_commit"] = "blocking"
    ready = not blockers
    return FoundationGate(
        status="PASS" if ready else "BLOCKED",
        ready_for_feature_work=ready,
        source_hash=current_source_hash,
        contract_sha256=contract_hash,
        checks=checks,
        blockers=tuple(blockers),
    )

def validate_foundation_evidence(
    contract: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    current_source_hash: str,
) -> list[str]:
    """Return foundation blockers for callers that do not need the full gate."""
    return list(evaluate_foundation(
        contract,
        evidence,
        current_source_hash=current_source_hash,
    ).blockers)

def _delivery_identity_kind(target: str) -> str:
    if target == "source-only":
        return "source-handoff"
    if target == "private-repo":
        return "repository"
    if target in WEB_DELIVERY_TARGETS:
        return "deployment"
    if target == "package":
        return "package"
    return "platform-release"

def _provider_names(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values = re.split(r"[,|+]", value)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = [str(item) for item in value]
    else:
        values = []
    return tuple(dict.fromkeys(item.strip().lower() for item in values if item.strip()))

def _select_delivery_provider(
    target: str,
    project_class: str,
    selected_provider: object,
    platform_target: str,
) -> tuple[str, str]:
    providers = _provider_names(selected_provider)
    if target in WEB_DELIVERY_TARGETS:
        if len(providers) > 1:
            return "", "Sites and Vercel are mutually exclusive delivery routes"
        if providers:
            if providers[0] not in WEB_DELIVERY_PROVIDERS:
                return "", "web delivery provider must be Sites or Vercel"
            return providers[0], ""
        fit = project_class.casefold()
        if target == "production" or _VERCEL_FIT_RE.search(fit):
            return "vercel", ""
        if _SITES_FIT_RE.search(fit):
            return "sites", ""
        return "", "web delivery needs one contract-selected Sites or Vercel route"
    if len(providers) > 1:
        return "", "delivery contract must select exactly one provider"
    if providers:
        return providers[0], ""
    if target == "private-repo":
        return "github", ""
    if target == "package":
        return "package", ""
    if target not in GENERIC_DELIVERY_TARGETS:
        return platform_target or target, ""
    return "not-applicable", ""

def _authority_blocker(target: str, authority: Mapping[str, Any]) -> str:
    checks = (
        (target != "source-only" and not authority.get("external_write_authorized"), "delivery authority"),
        (authority.get("credentials_required") and not authority.get("credentials_available"), "credentials"),
        (authority.get("signing_required") and not authority.get("signing_authorized"), "signing"),
        (authority.get("billing_required") and not authority.get("billing_authorized"), "billing"),
        (target == "production" and not authority.get("production_authorized"), "production authority"),
    )
    reasons = [reason for unresolved, reason in checks if unresolved]
    return "unresolved " + ", ".join(reasons) if reasons else ""

def make_delivery_contract(
    *,
    delivery_target: str,
    project_class: str = "",
    platform_target: str = "",
    environment: str = "",
    provider: object = "",
    destination: str = "",
    live_url_required: bool | None = None,
    smoke_result_required: bool = True,
    external_write_authorized: bool = True,
    credentials_required: bool = False,
    credentials_available: bool = True,
    signing_required: bool = False,
    signing_authorized: bool = False,
    billing_required: bool = False,
    billing_authorized: bool = False,
    production_authorized: bool = False,
) -> dict[str, Any]:
    """Build an explicit Delivery Contract without performing delivery.
    Provider selection is deterministic for obvious web fits. Ambiguous web
    projects become blocked until the approved contract selects one route.
    Authority failures are combined into one honest blocker.
    """
    raw_target = str(delivery_target or "").strip().lower()
    named_platform = str(platform_target or "").strip().lower()
    if raw_target == "platform-specific":
        target = raw_target
    else:
        target = raw_target
        if target and target not in GENERIC_DELIVERY_TARGETS:
            named_platform = named_platform or target
    selected_provider, route_blocker = _select_delivery_provider(target, str(project_class or ""), provider, named_platform)
    authority = {
        "external_write_authorized": bool(external_write_authorized),
        "credentials_required": bool(credentials_required),
        "credentials_available": bool(credentials_available),
        "signing_required": bool(signing_required),
        "signing_authorized": bool(signing_authorized),
        "billing_required": bool(billing_required),
        "billing_authorized": bool(billing_authorized),
        "production_authorized": bool(production_authorized),
    }
    authority_blocker = _authority_blocker(target, authority)
    authority["blocker"] = authority_blocker
    needs_live_url = (target in WEB_DELIVERY_TARGETS if live_url_required is None else bool(live_url_required))
    identity_kind = _delivery_identity_kind(target)
    requirements = {
        "source_binding": _requirement("requested", "delivery evidence must match the current source hash"),
        "repository_commit": _requirement("requested", "delivery evidence must identify the delivered repository commit"),
        "delivery_identity": _requirement("blocking" if authority_blocker or route_blocker else "requested",
                                          authority_blocker or route_blocker or f"delivery requires a {identity_kind} identity"),
        "live_url": _optional_requirement(needs_live_url, "the approved delivery result requires a live URL", "the approved delivery result has no live URL"),
        "smoke_result": _optional_requirement(smoke_result_required, "the delivered result requires a passing smoke result",
                                              "the approved contract does not require a smoke result"),
    }
    return {
        "schema": DELIVERY_CONTRACT_SCHEMA,
        "target": {
            "kind": target,
            "platform": named_platform,
            "project_class": str(project_class or "").strip(),
            "environment": str(environment or "").strip(),
            "destination": str(destination or "").strip(),
        },
        "route": {
            "provider": selected_provider,
            "sites_selected": selected_provider == "sites",
            "vercel_selected": selected_provider == "vercel",
            "selection_blocker": route_blocker,
        },
        "result": {
            "identity_kind": identity_kind,
            "live_url_required": needs_live_url,
            "smoke_result_required": bool(smoke_result_required),
        },
        "authority": authority,
        "requirements": requirements,
    }

def validate_delivery_contract(contract: object) -> list[str]:
    """Validate delivery target, route, evidence requirements, and authority."""
    if not isinstance(contract, Mapping):
        return ["delivery contract must be an object"]
    problems: list[str] = []
    _flag(problems, contract.get("schema") != DELIVERY_CONTRACT_SCHEMA, f"schema must be {DELIVERY_CONTRACT_SCHEMA}")
    sections = mapping_sections(contract, ("target", "route", "result", "authority", "requirements"), problems)
    target, route = sections["target"], sections["route"]
    result, authority = sections["result"], sections["authority"]
    requirements = sections["requirements"]
    kind = str(target.get("kind") or "")
    platform = str(target.get("platform") or "")
    named_target = kind not in GENERIC_DELIVERY_TARGETS | {"platform-specific"}
    rules(
        problems,
        (not kind, "delivery target must be explicit"),
        (bool(kind and named_target and not _NAMED_TARGET_RE.fullmatch(kind)), "named platform delivery target is invalid"),
        (bool(kind == "platform-specific" and not _NAMED_TARGET_RE.fullmatch(platform)), "platform-specific delivery requires a named platform target"),
        (bool(named_target and platform != kind), "named delivery target must match target.platform"),
    )
    problems.extend(_requirement_problems(requirements, DELIVERY_REQUIREMENTS))
    provider = str(route.get("provider") or "")
    sites_selected = route.get("sites_selected")
    vercel_selected = route.get("vercel_selected")
    rules(
        problems,
        (not isinstance(sites_selected, bool) or not isinstance(vercel_selected, bool), "route selection flags must be boolean"),
        (bool(sites_selected and vercel_selected), "Sites and Vercel cannot both be selected"),
        (sites_selected != (provider == "sites") or vercel_selected != (provider == "vercel"), "route selection flags must match the selected provider"),
    )
    if kind in WEB_DELIVERY_TARGETS and provider not in WEB_DELIVERY_PROVIDERS:
        _flag(problems, not route.get("selection_blocker"), "web delivery requires exactly one Sites or Vercel route")
    _flag(problems, kind not in WEB_DELIVERY_TARGETS and provider in WEB_DELIVERY_PROVIDERS, "Sites or Vercel route requires preview or production delivery")
    identity_kind = result.get("identity_kind")
    _flag(problems, identity_kind not in DELIVERY_IDENTITY_KINDS, "result.identity_kind is invalid")
    _flag(problems, bool(kind and identity_kind in DELIVERY_IDENTITY_KINDS and identity_kind != _delivery_identity_kind(kind)),
          "result.identity_kind does not match the delivery target")
    needs_url = result.get("live_url_required")
    needs_smoke = result.get("smoke_result_required")
    _flag(problems, not isinstance(needs_url, bool) or not isinstance(needs_smoke, bool), "delivery result requirement flags must be boolean")
    authority_fields = (
        "external_write_authorized",
        "credentials_required",
        "credentials_available",
        "signing_required",
        "signing_authorized",
        "billing_required",
        "billing_authorized",
        "production_authorized",
    )
    boolean_fields(authority, authority_fields, problems, prefix="authority.")
    expected_blocker = _authority_blocker(kind, authority)
    _flag(problems, authority.get("blocker") != expected_blocker, "authority.blocker must aggregate every unresolved authority")
    expected_states = {
        "delivery_identity": "blocking" if expected_blocker or route.get("selection_blocker") else "requested",
        "live_url": "requested" if needs_url is True else "not-applicable",
        "smoke_result": "requested" if needs_smoke is True else "not-applicable",
    }
    for name, expected_state in expected_states.items():
        requirement = requirements.get(name)
        if isinstance(requirement, Mapping):
            _flag(problems,
                  requirement.get("state") != expected_state, f"{name} requirement does not match " + ("blockers" if name == "delivery_identity" else "the approved result"))
    problems.extend(_secret_problems(contract, "contract"))
    return problems

def delivery_contract_sha256(contract: Mapping[str, Any]) -> str:
    """Hash a valid Delivery Contract using deterministic JSON encoding."""
    problems = validate_delivery_contract(contract)
    if problems:
        raise LifecycleContractError("; ".join(problems))
    serialized = json.dumps(contract, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()

def _valid_live_url(value: object, *, production: bool) -> bool:
    if not isinstance(value, str) or not value:
        return False
    pattern = r"https://" if production else r"https?://"
    return bool(re.fullmatch(pattern + r"[^/@\s]+(?::[0-9]+)?(?:/[^\s]*)?", value))

def _delivery_specific_problems(
    name: str,
    detail: Mapping[str, Any],
    contract: Mapping[str, Any],
    current_source_hash: str,
    repository_commit: str,
) -> list[str]:
    problems: list[str] = []
    target = str(contract["target"]["kind"])
    identity_kind = str(contract["result"]["identity_kind"])
    provider = str(contract["route"]["provider"])
    if name == "source_binding":
        _flag(problems, detail.get("source_hash") != current_source_hash, "delivered source does not match the current source hash")
    elif name == "repository_commit":
        _flag(problems, detail.get("sha") != repository_commit, "repository commit does not match delivery evidence")
        _flag(problems, detail.get("tree_source_hash") != current_source_hash, "repository commit tree is not bound to the current source hash")
    elif name == "delivery_identity":
        _flag(problems, detail.get("kind") != identity_kind, f"delivery identity must be {identity_kind}")
        _flag(problems, not _IDENTITY_RE.fullmatch(str(detail.get("id") or "")), "delivery identity id is invalid")
        _flag(problems, detail.get("repository_commit") != repository_commit, "delivery identity is not bound to the repository commit")
        _flag(problems, detail.get("source_hash") != current_source_hash, "delivery identity is not bound to the current source hash")
        _flag(problems, identity_kind == "deployment" and detail.get("provider") != provider, "deployment provider does not match the selected route")
        _flag(problems, identity_kind in {"package", "platform-release"} and not _SHA256_RE.fullmatch(str(detail.get("artifact_sha256") or "")),
              "package or platform identity requires an artifact SHA-256")
    elif name == "live_url":
        _flag(problems, not _valid_live_url(detail.get("url"), production=target == "production"), "live URL is invalid")
        _flag(problems, detail.get("provider") != provider, "live URL provider does not match the selected route")
    elif name == "smoke_result":
        _flag(problems, detail.get("verdict") != "PASS", "delivery smoke result must pass")
        _flag(problems, not _valid_timestamp(detail.get("checked_at")), "delivery smoke result requires a timestamp with timezone")
        _flag(problems, detail.get("repository_commit") != repository_commit, "smoke result is not bound to the repository commit")
        _flag(problems, detail.get("source_hash") != current_source_hash, "smoke result is not bound to the current source hash")
        _flag(problems, not str(detail.get("scenario") or "").strip(), "delivery smoke result requires a scenario")
    return problems

def evaluate_delivery(
    contract: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    current_source_hash: str,
) -> DeliveryGate:
    """Gate strict completion on the exact approved delivery result."""
    blockers: list[str] = []
    checks: dict[str, str] = {}
    contract_problems = validate_delivery_contract(contract)
    if contract_problems:
        blockers.extend(f"contract: {problem}" for problem in contract_problems)
        return DeliveryGate("BLOCKED", False, False, None, None, None, None, None, checks, tuple(blockers))
    target = str(contract["target"]["kind"])
    provider = str(contract["route"]["provider"])
    contract_hash = delivery_contract_sha256(contract)
    authority_blocker = str(contract["authority"]["blocker"] or "")
    route_blocker = str(contract["route"]["selection_blocker"] or "")
    if authority_blocker or route_blocker:
        reason = authority_blocker or route_blocker
        checks["delivery_identity"] = "blocking"
        return DeliveryGate(
            "BLOCKED",
            False,
            False,
            current_source_hash,
            None,
            contract_hash,
            target,
            provider or None,
            checks,
            (f"delivery blocked: {reason}", ),
        )
    if not isinstance(evidence, Mapping):
        blockers.append("delivery evidence must be an object")
        evidence = {}
    _flag(blockers, evidence.get("schema") != DELIVERY_EVIDENCE_SCHEMA, f"evidence schema must be {DELIVERY_EVIDENCE_SCHEMA}")
    _flag(blockers, not _valid_timestamp(evidence.get("captured_at")), "evidence captured_at must be an ISO-8601 timestamp with timezone")
    _flag(blockers, not _SHA256_RE.fullmatch(str(current_source_hash or "")), "current source hash must be a lowercase SHA-256 digest")
    _flag(blockers, evidence.get("source_hash") != current_source_hash, "delivery evidence source hash is stale")
    _flag(blockers, evidence.get("contract_sha256") != contract_hash, "delivery evidence is not bound to the current contract")
    repository_commit = str(evidence.get("repository_commit") or "")
    _flag(blockers, not _COMMIT_RE.fullmatch(repository_commit), "delivery evidence requires a valid repository commit")
    _flag(blockers, evidence.get("target") != target, "delivery evidence target does not match the approved contract")
    _flag(blockers, evidence.get("provider") != provider, "delivery evidence provider does not match the selected route")
    blockers.extend(_secret_problems(evidence))
    evidence_checks = evidence.get("checks")
    if not isinstance(evidence_checks, Mapping):
        blockers.append("delivery evidence checks must be an object")
        evidence_checks = {}
    extra = sorted(str(key) for key in set(evidence_checks) - set(DELIVERY_REQUIREMENTS))
    if extra:
        blockers.append("unknown delivery evidence checks: " + ", ".join(extra))
    for name in DELIVERY_REQUIREMENTS:
        requirement = contract["requirements"][name]
        required_state = requirement["state"]
        record, detail = _evidence_detail(evidence, name)
        evidence_state = record.get("state")
        if required_state == "not-applicable":
            checks[name] = "not-applicable"
            if evidence_state != "not-applicable":
                blockers.append(f"{name}: evidence must record not-applicable")
            continue
        checks[name] = "satisfied" if evidence_state == "satisfied" else "blocking"
        if evidence_state != "satisfied":
            blockers.append(f"{name}: approved delivery evidence is missing")
            continue
        if record.get("source_hash") != current_source_hash:
            blockers.append(f"{name}: evidence is not bound to the current source hash")
            checks[name] = "blocking"
        specific = _delivery_specific_problems(name, detail, contract, current_source_hash, repository_commit)
        blockers.extend(f"{name}: {problem}" for problem in specific)
        if specific:
            checks[name] = "blocking"
    satisfied = not blockers
    return DeliveryGate(
        "PASS" if satisfied else "BLOCKED",
        satisfied,
        satisfied,
        current_source_hash,
        repository_commit or None,
        contract_hash,
        target,
        provider or None,
        checks,
        tuple(blockers),
    )

def validate_delivery_evidence(
    contract: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    current_source_hash: str,
) -> list[str]:
    """Return delivery blockers for callers that do not need the full gate."""
    return list(evaluate_delivery(contract, evidence, current_source_hash=current_source_hash).blockers)
