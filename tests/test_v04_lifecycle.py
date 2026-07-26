#!/usr/bin/env python3
"""Focused tests for Star Forge v0.4 lifecycle contracts.

Run with: python3 tests/test_v04_lifecycle.py
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FOUNDATION_FIXTURES = ROOT / "fixtures" / "foundation"
DELIVERY_FIXTURES = ROOT / "fixtures" / "delivery"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from starforge import contracts, lifecycle, runtime_plan


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FOUNDATION_FIXTURES / name).read_text(encoding="utf-8"))


def fixture_pair(stem: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return fixture(f"{stem}-contract.json"), fixture(f"{stem}-evidence.json")


def delivery_fixture_pair(stem: str) -> tuple[dict[str, Any], dict[str, Any]]:
    contract = json.loads(
        (DELIVERY_FIXTURES / f"{stem}-contract.json").read_text(encoding="utf-8")
    )
    evidence = json.loads(
        (DELIVERY_FIXTURES / f"{stem}-evidence.json").read_text(encoding="utf-8")
    )
    return contract, evidence


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


def test_runtime_foundation_gate_requires_current_source_or_exact_prior_transition() -> None:
    contract, evidence = fixture_pair("private-new")
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        contract_path = project / ".starforge" / "foundation" / "contract.json"
        evidence_path = project / ".starforge" / "foundation" / "evidence.json"
        contract_path.parent.mkdir(parents=True)
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        current = runtime_plan.lifecycle_gate_state(
            project,
            kind="foundation",
            required=True,
            current_source_hash=evidence["source_hash"],
        )
        assert current["status"] == "PASS"
        assert current["satisfied"] is True
        stale = runtime_plan.lifecycle_gate_state(
            project,
            kind="foundation",
            required=True,
            current_source_hash="0" * 64,
        )
        assert stale["status"] == "BLOCKED"
        assert stale["satisfied"] is False
        state_path = project / ".starforge" / "state.json"
        for damaged_text in ("{damaged", "[]"):
            state_path.write_text(damaged_text, encoding="utf-8")
            damaged = runtime_plan.lifecycle_gate_state(
                project,
                kind="foundation",
                required=True,
                current_source_hash="0" * 64,
            )
            assert damaged["status"] == "BLOCKED"
        valid_state = {
            "schema": "star-forge.state.v3",
            "project": str(project),
            "phase": "build",
            "foundation": current,
        }
        state_path.write_text(json.dumps(valid_state), encoding="utf-8")
        transitioned = runtime_plan.lifecycle_gate_state(
            project,
            kind="foundation",
            required=True,
            current_source_hash="0" * 64,
        )
        assert transitioned["status"] == "PASS"
        assert transitioned["satisfied"] is True
        invalid_states = []
        for path, value in (
            (("project",), str(project.parent / "other")),
            (("phase",), "foundation"),
            (("foundation", "source_hash"), "f" * 64),
            (("foundation", "contract_sha256"), "f" * 64),
            (("foundation", "status"), "BLOCKED"),
        ):
            invalid = copy.deepcopy(valid_state)
            target = invalid
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            invalid_states.append(invalid)
        for invalid in invalid_states:
            state_path.write_text(json.dumps(invalid), encoding="utf-8")
            refused = runtime_plan.lifecycle_gate_state(
                project,
                kind="foundation",
                required=True,
                current_source_hash="0" * 64,
            )
            assert refused["status"] == "BLOCKED"


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
            {"note": "github_" + "pat_" + "abcdefghijklmnopqrstuvwxyz123456"},
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


def test_delivery_builder_supports_every_generic_and_named_platform_target() -> None:
    contracts = (
        lifecycle.make_delivery_contract(delivery_target="source-only"),
        lifecycle.make_delivery_contract(delivery_target="private-repo"),
        lifecycle.make_delivery_contract(
            delivery_target="preview", project_class="simple internal portal"
        ),
        lifecycle.make_delivery_contract(
            delivery_target="production",
            project_class="Next.js application",
            production_authorized=True,
        ),
        lifecycle.make_delivery_contract(delivery_target="package"),
        lifecycle.make_delivery_contract(
            delivery_target="platform-specific",
            platform_target="ios-app-store",
            signing_required=True,
            signing_authorized=True,
        ),
        lifecycle.make_delivery_contract(delivery_target="macos-notarized"),
    )
    assert all(lifecycle.validate_delivery_contract(item) == [] for item in contracts)
    assert [item["target"]["kind"] for item in contracts[:5]] == [
        "source-only",
        "private-repo",
        "preview",
        "production",
        "package",
    ]
    assert contracts[5]["target"]["platform"] == "ios-app-store"
    assert contracts[6]["target"]["platform"] == "macos-notarized"


def test_sites_and_vercel_routes_are_selected_by_fit_and_never_together() -> None:
    simple = lifecycle.make_delivery_contract(
        delivery_target="preview", project_class="simple internal dashboard"
    )
    production = lifecycle.make_delivery_contract(
        delivery_target="production",
        project_class="full-stack React application",
        production_authorized=True,
    )
    assert simple["route"]["provider"] == "sites"
    assert simple["route"]["sites_selected"] is True
    assert simple["route"]["vercel_selected"] is False
    assert production["route"]["provider"] == "vercel"
    assert production["route"]["sites_selected"] is False
    assert production["route"]["vercel_selected"] is True

    conflicted = lifecycle.make_delivery_contract(
        delivery_target="preview",
        project_class="internal app",
        provider=("sites", "vercel"),
    )
    assert lifecycle.validate_delivery_contract(conflicted) == []
    gate = lifecycle.evaluate_delivery(
        conflicted, {}, current_source_hash="a" * 64
    )
    assert gate.status == "BLOCKED"
    assert len(gate.blockers) == 1
    assert "mutually exclusive" in gate.blockers[0]


def test_delivery_fixtures_satisfy_the_exact_approved_result() -> None:
    for stem in ("source-only", "preview-sites", "package"):
        contract, evidence = delivery_fixture_pair(stem)
        result = lifecycle.evaluate_delivery(
            contract,
            evidence,
            current_source_hash=evidence["source_hash"],
        )
        assert result.status == "PASS", (stem, result.blockers)
        assert result.delivery_satisfied
        assert result.ready_for_completion
        assert result.repository_commit == evidence["repository_commit"]
        assert set(result.checks) == set(lifecycle.DELIVERY_REQUIREMENTS)
        assert result.to_dict()["schema"] == lifecycle.DELIVERY_GATE_SCHEMA


def test_delivery_requires_source_commit_identity_live_url_and_smoke_result() -> None:
    contract, evidence = delivery_fixture_pair("preview-sites")
    for check in lifecycle.DELIVERY_REQUIREMENTS:
        incomplete = copy.deepcopy(evidence)
        del incomplete["checks"][check]
        result = lifecycle.evaluate_delivery(
            contract,
            incomplete,
            current_source_hash=evidence["source_hash"],
        )
        assert not result.ready_for_completion, check
        assert result.checks[check] == "blocking"
        assert any(blocker.startswith(f"{check}:") for blocker in result.blockers)


def test_delivery_proof_is_bound_to_source_contract_and_repository_commit() -> None:
    contract, evidence = delivery_fixture_pair("preview-sites")
    stale = lifecycle.evaluate_delivery(
        contract, evidence, current_source_hash="0" * 64
    )
    assert not stale.ready_for_completion
    assert "delivery evidence source hash is stale" in stale.blockers

    changed_contract = copy.deepcopy(contract)
    changed_contract["target"]["destination"] = "customer preview"
    drifted = lifecycle.evaluate_delivery(
        changed_contract,
        evidence,
        current_source_hash=evidence["source_hash"],
    )
    assert "delivery evidence is not bound to the current contract" in drifted.blockers

    wrong_commit = copy.deepcopy(evidence)
    wrong_commit["checks"]["delivery_identity"]["detail"]["repository_commit"] = "c" * 40
    wrong_commit["checks"]["smoke_result"]["detail"]["repository_commit"] = "c" * 40
    blockers = lifecycle.validate_delivery_evidence(
        contract,
        wrong_commit,
        current_source_hash=evidence["source_hash"],
    )
    assert any("repository commit" in blocker for blocker in blockers)


def test_package_identity_requires_artifact_hash_and_web_requires_live_url() -> None:
    package_contract, package_evidence = delivery_fixture_pair("package")
    missing_artifact = copy.deepcopy(package_evidence)
    del missing_artifact["checks"]["delivery_identity"]["detail"]["artifact_sha256"]
    blockers = lifecycle.validate_delivery_evidence(
        package_contract,
        missing_artifact,
        current_source_hash=package_evidence["source_hash"],
    )
    assert any("artifact SHA-256" in blocker for blocker in blockers)

    preview_contract, preview_evidence = delivery_fixture_pair("preview-sites")
    bad_url = copy.deepcopy(preview_evidence)
    bad_url["checks"]["live_url"]["detail"]["url"] = "not-live"
    blockers = lifecycle.validate_delivery_evidence(
        preview_contract,
        bad_url,
        current_source_hash=preview_evidence["source_hash"],
    )
    assert any("live URL is invalid" in blocker for blocker in blockers)


def test_unresolved_delivery_authority_collapses_to_one_honest_blocker() -> None:
    contract = lifecycle.make_delivery_contract(
        delivery_target="production",
        project_class="production web application",
        external_write_authorized=False,
        credentials_required=True,
        credentials_available=False,
        signing_required=True,
        signing_authorized=False,
        billing_required=True,
        billing_authorized=False,
        production_authorized=False,
    )
    assert lifecycle.validate_delivery_contract(contract) == []
    blocker = contract["authority"]["blocker"]
    for reason in (
        "delivery authority",
        "credentials",
        "signing",
        "billing",
        "production authority",
    ):
        assert reason in blocker
    result = lifecycle.evaluate_delivery(
        contract, {}, current_source_hash="a" * 64
    )
    assert result.status == "BLOCKED"
    assert not result.ready_for_completion
    assert result.blockers == (f"delivery blocked: {blocker}",)


def test_target_lifecycle_advances_through_every_v04_gate() -> None:
    assert lifecycle.TARGET_LIFECYCLE == (
        "intake",
        "design",
        "plan",
        "foundation",
        "build",
        "review",
        "deliver",
        "done",
    )
    facts = {
        "legacy": False,
        "setup_complete": True,
        "blocked": False,
        "intake_complete": False,
        "design_required": True,
        "design_complete": False,
        "plan_complete": False,
        "foundation_complete": False,
        "amendment_required": False,
        "build_complete": False,
        "review_complete": False,
        "delivery_complete": False,
        "completion_complete": False,
    }
    sequence = []
    sequence.append(lifecycle.resolve_phase(**facts))
    facts["intake_complete"] = True
    sequence.append(lifecycle.resolve_phase(**facts))
    facts["design_complete"] = True
    sequence.append(lifecycle.resolve_phase(**facts))
    facts["plan_complete"] = True
    sequence.append(lifecycle.resolve_phase(**facts))
    facts["foundation_complete"] = True
    sequence.append(lifecycle.resolve_phase(**facts))
    facts["build_complete"] = True
    sequence.append(lifecycle.resolve_phase(**facts))
    facts["review_complete"] = True
    sequence.append(lifecycle.resolve_phase(**facts))
    facts["delivery_complete"] = True
    sequence.append(lifecycle.resolve_phase(**facts))
    assert tuple(sequence) == lifecycle.TARGET_LIFECYCLE

    facts["build_complete"] = False
    facts["review_complete"] = False
    facts["delivery_complete"] = False
    facts["amendment_required"] = True
    assert lifecycle.resolve_phase(**facts) == "amend"


def test_non_ui_design_skips_without_removing_other_v04_gates() -> None:
    phase = lifecycle.resolve_phase(
        legacy=False,
        setup_complete=True,
        blocked=False,
        intake_complete=True,
        design_required=False,
        design_complete=True,
        plan_complete=False,
        foundation_complete=False,
        amendment_required=False,
        build_complete=False,
        review_complete=False,
        delivery_complete=False,
        completion_complete=False,
    )
    assert phase == "plan"


def test_legacy_phase_compatibility_ignores_new_v04_gates() -> None:
    facts = {
        "legacy": True,
        "setup_complete": True,
        "blocked": False,
        "intake_complete": False,
        "design_required": True,
        "design_complete": False,
        "plan_complete": True,
        "foundation_complete": False,
        "amendment_required": False,
        "build_complete": False,
        "review_complete": False,
        "delivery_complete": False,
        "completion_complete": False,
    }
    assert lifecycle.resolve_phase(**facts) == "build"
    facts["amendment_required"] = True
    assert lifecycle.resolve_phase(**facts) == "amend"
    facts["amendment_required"] = False
    facts["build_complete"] = True
    assert lifecycle.resolve_phase(**facts) == "review"
    facts["review_complete"] = True
    assert lifecycle.resolve_phase(**facts) == "review"
    facts["completion_complete"] = True
    assert lifecycle.resolve_phase(**facts) == "done"


def test_blueprint_lifecycle_contract_resolves_intake_design_and_legacy_defaults() -> None:
    intake_lines = "\n".join(
        f"- {name}: {'not applicable' if index % 2 else 'resolved decision'}"
        for index, name in enumerate(contracts.INTAKE_DECISION_FIELDS)
    )
    modern = contracts.parse_blueprint_lifecycle_contract(
        f"""
## Intake Decision Record
{intake_lines}

## Design Direction
- Applicability: applicable, user-facing web interface
- Research availability: available
- Selected direction: Direction 2
- Selection rationale: Best fit for the core workflow
- Selected design constraints: Dense, keyboard-accessible workspace

## Delivery Contract
- Delivery target: preview
"""
    )
    assert modern["legacy"] is False
    assert modern["intake"]["complete"] is True
    assert modern["design"]["required"] is True
    assert modern["design"]["complete"] is True
    assert modern["delivery"]["target"] == "preview"

    draft = contracts.parse_blueprint_lifecycle_contract(
        """
## Intake Decision Record
- Users and their primary need:

## Design Direction
- Applicability: <applicable/not applicable, with reason>
"""
    )
    assert draft["intake"]["complete"] is False
    assert "Core flows and success conditions" in draft["intake"]["unresolved"]
    assert draft["design"]["required"] is None
    assert draft["design"]["complete"] is False

    legacy = contracts.parse_blueprint_lifecycle_contract(
        "# Blueprint.md\n\nStatus: approved\n\n## Product Goal\nBuild a CLI.\n"
    )
    assert legacy["legacy"] is True
    assert legacy["delivery"] == {
        "present": False,
        "target": "source-only",
        "legacy_default": True,
    }


def test_run_enters_intake_for_a_new_v04_project() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "star_forge.py"),
                "run",
                "--project",
                str(project),
                "--objective",
                "Build a focused test project",
                "--no-hooks",
                "--no-agents",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        state = json.loads(
            (project / ".starforge" / "state.json").read_text(encoding="utf-8")
        )
        assert state["phase"] == "intake"
        assert state["lifecycle"]["legacy"] is False
        assert state["foundation"]["status"] == "MISSING"
        assert state["delivery"]["status"] == "MISSING"


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
    assert "Sites and Vercel are mutually exclusive" in source
    assert len(source.splitlines()) < 1200


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
