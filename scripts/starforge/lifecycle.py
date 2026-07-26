"""Pure lifecycle contracts and gates for Star Forge foundation work.

This module describes authorized foundation outcomes and validates evidence
captured by orchestrators or proof adapters. It intentionally performs no Git,
GitHub, filesystem, subprocess, or network mutation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


FOUNDATION_CONTRACT_SCHEMA = "star-forge.foundation-contract.v1"
FOUNDATION_EVIDENCE_SCHEMA = "star-forge.foundation-evidence.v1"
FOUNDATION_GATE_SCHEMA = "star-forge.foundation-gate.v1"

REQUIREMENT_STATES = frozenset({"requested", "not-applicable", "blocking"})
EVIDENCE_STATES = frozenset({"satisfied", "not-applicable", "blocking"})
FOUNDATION_REQUIREMENTS = (
    "source_scaffold",
    "local_git",
    "github_repository",
    "remote_origin",
    "default_branch",
    "initial_commit",
    "ci",
    "environment_example",
    "secret_scan",
    "dependency_audit",
    "security_plan",
)

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


class LifecycleContractError(ValueError):
    """A lifecycle contract cannot be represented safely."""


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
        return {
            "schema": FOUNDATION_GATE_SCHEMA,
            "status": self.status,
            "ready_for_feature_work": self.ready_for_feature_work,
            "source_hash": self.source_hash,
            "contract_sha256": self.contract_sha256,
            "checks": dict(self.checks),
            "blockers": list(self.blockers),
        }


def _requirement(state: str, reason: str) -> dict[str, str]:
    return {"state": state, "reason": reason}


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

    repository_state = (
        "not-applicable"
        if not github_requested
        else ("blocking" if repo_blocker else "requested")
    )
    github_reason = (
        "approved contract does not request GitHub"
        if not github_requested
        else (repo_blocker or "approved Repository Contract requests GitHub")
    )
    requirements = {
        "source_scaffold": _requirement(
            "requested", "source scaffold must exist before feature work"
        ),
        "local_git": _requirement(
            "requested", "local Git initialization is an automatic foundation outcome"
        ),
        "github_repository": _requirement(repository_state, github_reason),
        "remote_origin": _requirement(repository_state, github_reason),
        "default_branch": _requirement(
            "requested", "the initial commit must establish the approved default branch"
        ),
        "initial_commit": _requirement(
            "requested", "foundation artifacts must be committed before feature work"
        ),
        "ci": _requirement(
            "requested", "CI configuration must be installed before feature work"
        ),
        "environment_example": _requirement(
            "requested" if environment_example_required else "not-applicable",
            (
                "an environment example is required"
                if environment_example_required
                else "the project has no configurable environment"
            ),
        ),
        "secret_scan": _requirement(
            "requested", "the committed foundation requires a clean secret scan"
        ),
        "dependency_audit": _requirement(
            "requested" if dependency_audit_required else "not-applicable",
            (
                "declared dependencies require an audit"
                if dependency_audit_required
                else "the foundation declares no third-party dependencies"
            ),
        ),
        "security_plan": _requirement(
            "requested" if security_plan_required else "not-applicable",
            (
                "risk flags require a threat model or security plan"
                if security_plan_required
                else "approved risk flags make a security plan not applicable"
            ),
        ),
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
            "preferred_provider": (
                "github-connector" if github_requested else "not-applicable"
            ),
            "create_fallback": (
                "gh repo create --private" if github_requested else "not-applicable"
            ),
            "mutation_policy": "never-change-visibility",
        },
        "expectations": {
            "default_branch": default_branch,
            "ci_path": ci_path,
        },
        "requirements": requirements,
    }


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and value not in {".", ".."}
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(
            candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
        )
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

    repository = contract.get("repository")
    expectations = contract.get("expectations")
    requirements = contract.get("requirements")
    if not isinstance(repository, Mapping):
        problems.append("repository must be an object")
        repository = {}
    if not isinstance(expectations, Mapping):
        problems.append("expectations must be an object")
        expectations = {}
    if not isinstance(requirements, Mapping):
        problems.append("requirements must be an object")
        requirements = {}

    missing = [name for name in FOUNDATION_REQUIREMENTS if name not in requirements]
    extra = sorted(str(name) for name in set(requirements) - set(FOUNDATION_REQUIREMENTS))
    if missing:
        problems.append("missing requirements: " + ", ".join(missing))
    if extra:
        problems.append("unknown requirements: " + ", ".join(extra))
    for name in FOUNDATION_REQUIREMENTS:
        item = requirements.get(name)
        if not isinstance(item, Mapping):
            if name in requirements:
                problems.append(f"requirement {name} must be an object")
            continue
        state = item.get("state")
        if state not in REQUIREMENT_STATES:
            problems.append(f"requirement {name} has invalid state")
        if not isinstance(item.get("reason"), str) or not item.get("reason", "").strip():
            problems.append(f"requirement {name} requires a reason")

    branch = expectations.get("default_branch")
    if not isinstance(branch, str) or not _BRANCH_RE.fullmatch(branch):
        problems.append("expectations.default_branch is invalid")
    ci_path = expectations.get("ci_path")
    if (
        not _safe_relative_path(ci_path)
        or PurePosixPath(str(ci_path)).parent != PurePosixPath(".github/workflows")
        or PurePosixPath(str(ci_path)).suffix not in {".yml", ".yaml"}
    ):
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
    serialized = json.dumps(
        contract, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _evidence_detail(
    evidence: Mapping[str, Any], name: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
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
    return bool(
        re.fullmatch(rf"https://github\.com/{escaped}(?:\.git)?", remote_url)
        or re.fullmatch(rf"git@github\.com:{escaped}(?:\.git)?", remote_url)
    )


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
        if not _github_remote_matches(
            detail.get("url"), repository["owner"], repository["name"]
        ):
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
        artifact_problem = _artifact_problem(
            detail, required_path=expectations["ci_path"]
        )
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
        specific = _specific_evidence_problems(
            name, detail, contract, current_source_hash
        )
        blockers.extend(f"{name}: {problem}" for problem in specific)
        if specific:
            checks[name] = "blocking"

    _, branch_detail = _evidence_detail(evidence, "default_branch")
    _, commit_detail = _evidence_detail(evidence, "initial_commit")
    if (
        branch_detail
        and commit_detail
        and branch_detail.get("head_commit") != commit_detail.get("current_head")
    ):
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

    return list(
        evaluate_foundation(
            contract,
            evidence,
            current_source_hash=current_source_hash,
        ).blockers
    )
