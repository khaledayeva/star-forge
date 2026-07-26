#!/usr/bin/env python3
"""Focused tests for Star Forge v0.4 lifecycle contracts.

Run with: python3 tests/test_v04_lifecycle.py
"""

from __future__ import annotations

import copy
import json
import sys
import traceback
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "fixtures" / "foundation"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from starforge import lifecycle


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_pair(stem: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return fixture(f"{stem}-contract.json"), fixture(f"{stem}-evidence.json")


def test_contract_builder_distinguishes_requested_not_applicable_and_blocking() -> None:
    local = lifecycle.make_foundation_contract(
        github_requested=False,
        environment_example_required=False,
        dependency_audit_required=False,
        security_plan_required=False,
    )
    assert lifecycle.validate_foundation_contract(local) == []
    assert local["requirements"]["local_git"]["state"] == "requested"
    assert local["requirements"]["github_repository"]["state"] == "not-applicable"
    assert local["requirements"]["environment_example"]["state"] == "not-applicable"

    blocked = lifecycle.make_foundation_contract(
        github_requested=True,
        repository_mode="create",
        owner="acme-labs",
        repository="fixture-app",
        visibility="private",
        repository_write_authorized=False,
    )
    assert lifecycle.validate_foundation_contract(blocked) == []
    assert blocked["repository"]["state"] == "blocking"
    assert blocked["requirements"]["github_repository"]["state"] == "blocking"
    assert "authority" in blocked["requirements"]["github_repository"]["reason"]


def test_private_new_foundation_fixture_passes_all_pre_feature_gates() -> None:
    contract, evidence = fixture_pair("private-new")
    result = lifecycle.evaluate_foundation(
        contract,
        evidence,
        current_source_hash=evidence["source_hash"],
    )

    assert result.status == "PASS", result.blockers
    assert result.ready_for_feature_work
    assert set(result.checks) == set(lifecycle.FOUNDATION_REQUIREMENTS)
    assert set(result.checks.values()) == {"satisfied"}
    assert result.contract_sha256 == evidence["contract_sha256"]
    assert result.to_dict()["schema"] == lifecycle.FOUNDATION_GATE_SCHEMA


def test_local_only_fixture_preserves_automatic_git_and_explicit_n_a_states() -> None:
    contract, evidence = fixture_pair("local-only")
    result = lifecycle.evaluate_foundation(
        contract,
        evidence,
        current_source_hash=evidence["source_hash"],
    )

    assert result.status == "PASS", result.blockers
    assert result.checks["local_git"] == "satisfied"
    assert result.checks["github_repository"] == "not-applicable"
    assert result.checks["remote_origin"] == "not-applicable"
    assert result.checks["environment_example"] == "not-applicable"
    assert result.checks["dependency_audit"] == "not-applicable"
    assert result.checks["security_plan"] == "not-applicable"


def test_every_requested_foundation_obligation_blocks_feature_work_when_missing() -> None:
    contract, evidence = fixture_pair("private-new")
    for name in lifecycle.FOUNDATION_REQUIREMENTS:
        incomplete = copy.deepcopy(evidence)
        del incomplete["checks"][name]
        result = lifecycle.evaluate_foundation(
            contract,
            incomplete,
            current_source_hash=evidence["source_hash"],
        )
        assert not result.ready_for_feature_work, name
        assert result.checks[name] == "blocking", name
        assert any(blocker.startswith(f"{name}:") for blocker in result.blockers), name


def test_foundation_evidence_is_bound_to_current_source_and_exact_contract() -> None:
    contract, evidence = fixture_pair("private-new")

    stale = lifecycle.evaluate_foundation(
        contract,
        evidence,
        current_source_hash="0" * 64,
    )
    assert not stale.ready_for_feature_work
    assert "foundation evidence source hash is stale" in stale.blockers
    assert any("current source hash" in blocker for blocker in stale.blockers)

    changed_contract = copy.deepcopy(contract)
    changed_contract["expectations"]["default_branch"] = "trunk"
    contract_drift = lifecycle.evaluate_foundation(
        changed_contract,
        evidence,
        current_source_hash=evidence["source_hash"],
    )
    assert not contract_drift.ready_for_feature_work
    assert "foundation evidence is not bound to the current contract" in contract_drift.blockers


def test_initial_commit_ci_environment_and_security_artifacts_are_hash_bound() -> None:
    contract, evidence = fixture_pair("private-new")
    cases = (
        ("initial_commit", "tree_source_hash", "0" * 64, "current source hash"),
        ("ci", "sha256", "not-a-hash", "artifact SHA-256"),
        ("environment_example", "contains_secret_values", True, "no secret values"),
        ("secret_scan", "findings", 1, "zero findings"),
        ("dependency_audit", "unresolved_findings", 1, "zero unresolved findings"),
        ("security_plan", "committed", False, "must be committed"),
    )
    for check, field, value, message in cases:
        broken = copy.deepcopy(evidence)
        broken["checks"][check]["detail"][field] = value
        blockers = lifecycle.validate_foundation_evidence(
            contract,
            broken,
            current_source_hash=evidence["source_hash"],
        )
        assert any(message in blocker for blocker in blockers), (check, blockers)


def test_connector_is_preferred_and_gh_creation_fallback_is_narrow() -> None:
    contract, evidence = fixture_pair("private-new")
    assert contract["repository"]["preferred_provider"] == "github-connector"
    assert contract["repository"]["create_fallback"] == "gh repo create --private"

    fallback = copy.deepcopy(evidence)
    detail = fallback["checks"]["github_repository"]["detail"]
    detail["provider"] = "gh-cli"
    detail["fallback"] = "gh repo create --private"
    assert lifecycle.validate_foundation_evidence(
        contract,
        fallback,
        current_source_hash=evidence["source_hash"],
    ) == []

    broad_fallback = copy.deepcopy(fallback)
    broad_fallback["checks"]["github_repository"]["detail"]["fallback"] = (
        "gh repo create --public"
    )
    blockers = lifecycle.validate_foundation_evidence(
        contract,
        broad_fallback,
        current_source_hash=evidence["source_hash"],
    )
    assert any("exactly gh repo create --private" in blocker for blocker in blockers)


def test_new_github_repository_must_be_private_and_match_origin_identity() -> None:
    contract, evidence = fixture_pair("private-new")
    public = copy.deepcopy(contract)
    public["repository"]["visibility"] = "public"
    problems = lifecycle.validate_foundation_contract(public)
    assert "new GitHub repositories must be private" in problems

    wrong_origin = copy.deepcopy(evidence)
    wrong_origin["checks"]["remote_origin"]["detail"]["url"] = (
        "https://github.com/other-owner/fixture-app.git"
    )
    result = lifecycle.evaluate_foundation(
        contract,
        wrong_origin,
        current_source_hash=evidence["source_hash"],
    )
    assert not result.ready_for_feature_work
    assert any("approved GitHub identity" in blocker for blocker in result.blockers)


def test_existing_repository_adoption_requires_read_only_identity_and_visibility_checks() -> None:
    contract, evidence = fixture_pair("adopt-existing")
    result = lifecycle.evaluate_foundation(
        contract,
        evidence,
        current_source_hash=evidence["source_hash"],
    )
    assert result.status == "PASS", result.blockers

    for field, value, message in (
        ("identity_verified", False, "identity was not verified"),
        ("visibility_verified", False, "visibility was not verified"),
        ("read_only_adoption", False, "must be read-only"),
        ("visibility_changed", True, "must not be changed"),
        ("created", True, "must be read-only"),
    ):
        mutated = copy.deepcopy(evidence)
        mutated["checks"]["github_repository"]["detail"][field] = value
        blockers = lifecycle.validate_foundation_evidence(
            contract,
            mutated,
            current_source_hash=evidence["source_hash"],
        )
        assert any(message in blocker for blocker in blockers), (field, blockers)


def test_existing_repository_visibility_mismatch_blocks_without_mutation_advice() -> None:
    contract, evidence = fixture_pair("adopt-existing")
    mismatch = copy.deepcopy(evidence)
    detail = mismatch["checks"]["github_repository"]["detail"]
    detail["visibility"] = "public"
    detail["visibility_changed"] = False

    blockers = lifecycle.validate_foundation_evidence(
        contract,
        mismatch,
        current_source_hash=evidence["source_hash"],
    )
    assert any("visibility does not match" in blocker for blocker in blockers)
    assert contract["repository"]["mutation_policy"] == "never-change-visibility"


def test_evidence_rejects_secret_fields_values_and_credentialed_urls() -> None:
    contract, evidence = fixture_pair("private-new")
    unsafe_cases = (
        ("raw-token-field", {"token": "redacted"}, "must not contain secret material"),
        (
            "secret-looking-value",
            {"note": "github_pat_abcdefghijklmnopqrstuvwxyz123456"},
            "likely secret material",
        ),
    )
    for _, payload, message in unsafe_cases:
        unsafe = copy.deepcopy(evidence)
        unsafe["diagnostic"] = payload
        blockers = lifecycle.validate_foundation_evidence(
            contract,
            unsafe,
            current_source_hash=evidence["source_hash"],
        )
        assert any(message in blocker for blocker in blockers), blockers

    credentialed = copy.deepcopy(evidence)
    credentialed["checks"]["remote_origin"]["detail"]["url"] = (
        "https://credential@github.com/acme-labs/fixture-app.git"
    )
    blockers = lifecycle.validate_foundation_evidence(
        contract,
        credentialed,
        current_source_hash=evidence["source_hash"],
    )
    assert any("embed credentials" in blocker for blocker in blockers)


def test_foundation_module_is_validation_only_and_has_no_external_write_runtime() -> None:
    source = (SCRIPTS / "starforge" / "lifecycle.py").read_text(encoding="utf-8")
    forbidden = (
        "import subprocess",
        "from subprocess",
        "import urllib",
        "import requests",
        "urlopen(",
        "write_text(",
        "write_bytes(",
        "NamedTemporaryFile",
        "os.replace",
        "git init",
        "gh repo create --public",
    )
    assert not any(token in source for token in forbidden)
    assert "gh repo create --private" in source
    assert "never-change-visibility" in source


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failures += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} lifecycle tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
