"""Scope-driven task, review, proof, and delivery derivation for change packets."""

from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from .contracts import PLAN_PROOF_KINDS
from .review_policy import RISK_FLAG_ORDER, select_review_policy


CHANGE_IMPACT_SCHEMA = "star-forge.change-impact.v1"
_AC_ID_RE = re.compile(r"AC-[1-9][0-9]*")
_CODE_SUFFIXES = frozenset(
    {
        ".c", ".cc", ".cpp", ".cs", ".dart", ".ex", ".exs", ".go", ".h",
        ".html", ".java", ".js", ".jsx", ".kt", ".lua", ".m", ".mjs", ".mm",
        ".php", ".pl", ".py", ".r", ".rb", ".rs", ".scala", ".sh", ".sql",
        ".swift", ".tf", ".ts", ".tsx", ".vue", ".yaml", ".yml", ".zsh",
    }
)
_CODE_FILENAMES = frozenset(
    {
        "dockerfile", "gemfile", "justfile", "makefile", "procfile", "rakefile",
        "vagrantfile",
    }
)
_DOC_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".txt"})
_DELIVERY_PROOFS_BY_TARGET = {
    "package": ("delivery", "package"),
    "preview": ("delivery", "preview"),
    "private-repo": ("delivery", "github"),
    "production": ("delivery",),
    "source-only": ("delivery",),
}


class ChangeDerivationError(ValueError):
    """The changed scope cannot produce a safe implementation plan."""


def _normalize_scope_path(raw_path: str) -> str:
    raw_value = str(raw_path or "").rstrip()
    status = raw_value[:2]
    if (
        len(raw_value) >= 4
        and raw_value[2] == " "
        and status.strip() in {"M", "A", "D", "R", "C", "U", "??"}
    ):
        value = raw_value[3:].strip()
    else:
        value = raw_value.strip()
    value = value.strip('"')
    if " -> " in value:
        value = value.split(" -> ", 1)[1].strip().strip('"')
    value = value.replace("\\", "/")
    candidate = PurePosixPath(value)
    if (
        not value
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ChangeDerivationError(
            "changed file paths must be normalized project-relative paths"
        )
    return candidate.as_posix()


def normalize_changed_files(changed_files: Sequence[str]) -> list[str]:
    """Normalize Git status paths into a stable, de-duplicated scope."""
    if isinstance(changed_files, (str, bytes)) or not isinstance(
        changed_files, Sequence
    ):
        raise ChangeDerivationError("changed_files must be a non-empty sequence")
    normalized: list[str] = []
    for raw_path in changed_files:
        if not isinstance(raw_path, str):
            raise ChangeDerivationError("changed file paths must be strings")
        path = _normalize_scope_path(raw_path)
        if path not in normalized:
            normalized.append(path)
    if not normalized:
        raise ChangeDerivationError("changed_files must not be empty")
    return normalized


def _values(raw_value: Any) -> list[str]:
    if isinstance(raw_value, str):
        values = re.split(r"[,;]", raw_value)
    elif isinstance(raw_value, Sequence):
        values = [str(value) for value in raw_value]
    else:
        values = []
    return [
        value.strip()
        for value in values
        if value.strip()
        and value.strip().casefold() not in {"-", "none", "n/a", "na"}
    ]


def _task_paths(task: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    for value in _values(task.get("files")):
        try:
            normalized = _normalize_scope_path(value)
        except ChangeDerivationError:
            continue
        if normalized not in paths:
            paths.append(normalized)
    return paths


def _path_matches_owner(changed_path: str, owner: str) -> bool:
    if any(char in owner for char in "*?["):
        return fnmatch.fnmatchcase(changed_path, owner)
    return (
        changed_path == owner
        or changed_path.startswith(owner.rstrip("/") + "/")
        or owner.startswith(changed_path.rstrip("/") + "/")
    )


def _is_code_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    return (
        candidate.suffix.casefold() in _CODE_SUFFIXES
        or candidate.name.casefold() in _CODE_FILENAMES
    )


def _derived_mode(source_mode: str, paths: Sequence[str]) -> str:
    if paths and all(
        PurePosixPath(path).suffix.casefold() in _DOC_SUFFIXES for path in paths
    ):
        return "docs"
    if any(_is_code_path(path) for path in paths):
        return "delegate"
    return "solo" if source_mode.casefold() == "solo" else "delegate"


def _safe_verify_command(task: Mapping[str, Any], *, mode: str) -> str:
    command = str(task.get("verify") or "").strip()
    if mode == "docs":
        return "noop"
    if (
        not command
        or command.casefold() in {"-", "none", "noop", "review_required"}
        or re.search(
            r"\b(?:replace|set)\b.*\b(?:command|verification)\b",
            command,
            re.IGNORECASE,
        )
    ):
        return "REVIEW_REQUIRED"
    return command


def _proof_values(task: Mapping[str, Any]) -> list[str]:
    return sorted(
        {
            value.casefold()
            for value in _values(task.get("proof"))
            if value.casefold() in PLAN_PROOF_KINDS
        }
    )


def _ac_sort_key(value: str) -> int:
    return int(value.split("-", 1)[1])


def _ac_values(task: Mapping[str, Any]) -> list[str]:
    return sorted(
        {value for value in _values(task.get("acs")) if _AC_ID_RE.fullmatch(value)},
        key=_ac_sort_key,
    )


def _delivery_impact(
    delivery_contract: Mapping[str, Any] | None,
) -> tuple[str, list[str], dict[str, Any]]:
    contract = delivery_contract if isinstance(delivery_contract, Mapping) else {}
    target = contract.get("target")
    target_mapping = target if isinstance(target, Mapping) else {}
    target_kind = str(target_mapping.get("kind") or "source-only").strip().casefold()
    proof_kinds = list(_DELIVERY_PROOFS_BY_TARGET.get(target_kind, ("delivery",)))
    destination = str(target_mapping.get("destination") or "").strip()
    route = contract.get("route")
    provider = (
        str(route.get("provider") or "").strip()
        if isinstance(route, Mapping)
        else ""
    )
    detail = f" ({destination})" if destination else ""
    return (
        f"{target_kind} delivery must be revalidated{detail}",
        proof_kinds,
        {
            "required": True,
            "target": target_kind,
            "provider": provider or "not-specified",
            "proof_kinds": proof_kinds,
            "reason": "delivery evidence is source-hash bound and the source changed",
        },
    )


def derive_change_impact(
    *,
    changed_files: Sequence[str],
    root_tasks: Sequence[Mapping[str, Any]],
    blueprint_text: str = "",
    delivery_contract: Mapping[str, Any] | None = None,
    profile: str = "standard",
) -> dict[str, Any]:
    """Derive packet tasks, risk policy, and proof from the actual changed scope."""
    scope = normalize_changed_files(changed_files)
    task_matches: list[dict[str, Any]] = []
    covered: set[str] = set()
    affected_source_tasks: list[Mapping[str, Any]] = []
    for task in root_tasks:
        owners = _task_paths(task)
        matched = [
            path
            for path in scope
            if any(_path_matches_owner(path, owner) for owner in owners)
        ]
        if not matched:
            continue
        covered.update(matched)
        affected_source_tasks.append(task)
        mode = _derived_mode(str(task.get("mode") or "delegate"), matched)
        task_matches.append(
            {
                "source_task": str(task.get("id") or ""),
                "description": (
                    "Revalidate affected root task "
                    + str(task.get("id") or "unknown")
                    + " for the changed scope."
                ),
                "mode": mode,
                "files": matched,
                "acs": _ac_values(task),
                "proof_kinds": _proof_values(task),
                "verify": _safe_verify_command(task, mode=mode),
            }
        )
    unmatched = [path for path in scope if path not in covered]
    if unmatched:
        mode = _derived_mode("delegate", unmatched)
        task_matches.append(
            {
                "source_task": None,
                "description": "Plan and revalidate changed files with no root task owner.",
                "mode": mode,
                "files": unmatched,
                "acs": [],
                "proof_kinds": [],
                "verify": "noop" if mode == "docs" else "REVIEW_REQUIRED",
            }
        )
    fallback_acs = sorted(set(_AC_ID_RE.findall(blueprint_text)), key=_ac_sort_key)
    for match in task_matches:
        if not match["acs"]:
            match["acs"] = fallback_acs
    all_acs = sorted(
        {value for match in task_matches for value in match["acs"]},
        key=_ac_sort_key,
    )
    if not all_acs:
        raise ChangeDerivationError(
            "changed scope has no affected AC mapping; update Plan v2 before approval"
        )
    delivery_summary, delivery_proofs, delivery_revalidation = _delivery_impact(
        delivery_contract
    )
    for match in task_matches:
        match["proof_kinds"] = sorted(set(match["proof_kinds"]).union(delivery_proofs))
    policy = select_review_policy(
        blueprint_text,
        affected_source_tasks,
        profile=profile,
        delivery_contract=delivery_contract,
    )
    unresolved = [
        match["source_task"] or ", ".join(match["files"])
        for match in task_matches
        if match["verify"] == "REVIEW_REQUIRED"
    ]
    return {
        "schema": CHANGE_IMPACT_SCHEMA,
        "scope_delta": scope,
        "affected_tasks": task_matches,
        "affected_task_ids": [
            str(match["source_task"]) for match in task_matches if match["source_task"]
        ],
        "affected_acs": all_acs,
        "risk_flags": {flag.name: flag.to_dict() for flag in policy.risk_flags},
        "risk_flag_order": list(RISK_FLAG_ORDER),
        "review_roles": list(policy.roles),
        "review_lenses": {
            selection.role: list(selection.lenses)
            for selection in policy.selections
        },
        "proof_kinds": sorted(
            {proof for match in task_matches for proof in match["proof_kinds"]}
        ),
        "delivery_impact": delivery_summary,
        "delivery_revalidation": delivery_revalidation,
        "delegation_required": any(
            match["mode"] == "delegate" for match in task_matches
        ),
        "unmatched_files": unmatched,
        "approval_blockers": [
            f"derive a real verification command for {value}" for value in unresolved
        ],
    }


__all__ = [
    "CHANGE_IMPACT_SCHEMA",
    "ChangeDerivationError",
    "derive_change_impact",
    "normalize_changed_files",
]
