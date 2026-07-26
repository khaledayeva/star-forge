#!/usr/bin/env python3
"""Focused tests for Star Forge v0.4 adaptive review selection.

Run with: python3 tests/test_v04_review_policy.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from starforge import review_policy


FLAG_NAMES = review_policy.RISK_FLAG_ORDER


def blueprint(
    enabled: set[str] | None = None,
    *,
    project_class: str = "cli",
    target_platforms: str = "linux",
    delivery_target: str = "source-only",
    summary: str = "A local utility.",
    rows: list[tuple[str, str, str]] | None = None,
) -> str:
    selected = enabled or set()
    flag_rows = rows or [
        (
            name,
            "yes" if name in selected else "no",
            f"handling for {name}",
        )
        for name in FLAG_NAMES
    ]
    rendered_rows = "\n".join(
        f"| {name} | {value} | {reason} |"
        for name, value, reason in flag_rows
    )
    return f"""# Blueprint

Status: approved

## Product Summary

{summary}

## Toolchain

- Project class: {project_class}
- Target platforms: {target_platforms}

## Delivery Contract

- Delivery target: {delivery_target}

## Risk Flags

| Flag | Value | Reason and required handling |
|---|---|---|
{rendered_rows}
"""


def roles(
    text: str,
    tasks: list[dict[str, str]] | None = None,
    *,
    profile: str = "standard",
) -> list[str]:
    return review_policy.select_review_roles(
        text,
        tasks or [],
        profile=profile,
        source_hash="a" * 64,
    )


def load_star_forge():
    spec = importlib.util.spec_from_file_location(
        "star_forge_review_policy_test",
        SCRIPTS / "star_forge.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_correctness_is_always_first_and_is_the_only_baseline_role() -> None:
    result = review_policy.select_review_policy(
        blueprint(),
        source_hash="1" * 64,
    )
    assert result.roles == ("correctness",)
    assert result.source_hash == "1" * 64
    assert result.selections[0].reasons == (
        "Correctness review is required for every project",
    )


def test_targeted_risk_roles_follow_exact_canonical_flags() -> None:
    text = blueprint(
        {
            "User-facing UI",
            "Privacy obligations",
            "Migrations or complex persistence",
            "Performance or reliability constraints",
        }
    )
    assert roles(text) == [
        "correctness",
        "ux-accessibility",
        "security",
        "architecture-performance-reliability",
    ]
    result = review_policy.select_review_policy(text)
    assert result.combined is True
    combined = result.selections[-1]
    assert combined.lenses == ("architecture", "performance", "reliability")
    assert any("Migrations or complex persistence" in item for item in combined.reasons)
    assert any(
        "Performance or reliability constraints" in item
        for item in combined.reasons
    )


def test_every_security_and_privacy_flag_selects_security() -> None:
    for flag in review_policy.SECURITY_PRIVACY_FLAGS:
        selected = roles(blueprint({flag}))
        assert selected == ["correctness", "security"], flag


def test_every_architecture_flag_selects_architecture() -> None:
    for flag in review_policy.ARCHITECTURE_FLAGS:
        selected = roles(blueprint({flag}))
        assert selected == ["correctness", "architecture"], flag


def test_project_surfaces_and_plan_proofs_establish_applicability() -> None:
    text = blueprint(
        project_class="cli",
        target_platforms="linux",
    )
    tasks = [
        {"proof": "unit, browser"},
        {"proof": "native-ios"},
        {"proof": "browser, unit"},
    ]
    result = review_policy.select_review_policy(text, tasks)
    assert result.roles == (
        "correctness",
        "ux-accessibility",
        "performance-reliability",
    )
    assert result.project_surfaces.proof_kinds == (
        "browser",
        "native-ios",
        "unit",
    )
    assert result.project_surfaces.surfaces == (
        "cli",
        "native-mobile",
        "ui",
        "web",
    )
    assert any(
        "Plan proof contract establishes a user-facing interface" in reason
        for reason in result.reasons_for("ux-accessibility")
    )


def test_production_delivery_contract_establishes_reliability_review() -> None:
    text = blueprint().replace(
        "- Delivery target: source-only",
        "- Delivery target: production",
    )
    result = review_policy.select_review_policy(text)
    assert result.roles == ("correctness", "performance-reliability")
    assert result.reasons_for("performance-reliability") == (
        "Structured delivery contract establishes operational reliability risk: production",
    )


def test_arbitrary_product_prose_does_not_select_targeted_roles() -> None:
    text = blueprint(
        summary=(
            "The documentation discusses OAuth, payment secrets, migrations, "
            "high coupling, latency, privacy, and screen readers."
        )
    )
    assert roles(text, [{"proof": "unit, security"}]) == ["correctness"]


def test_selection_is_deduplicated_stable_and_capped_at_four() -> None:
    enabled = {
        "User-facing UI",
        "Authentication or authorization",
        "Privacy obligations",
        "Multiple services or high coupling",
        "Migrations or complex persistence",
        "Performance or reliability constraints",
    }
    first = review_policy.select_review_policy(
        blueprint(enabled),
        [{"proof": "security, browser"}, {"proof": "browser, security"}],
    )
    reversed_rows = [
        (
            name,
            "yes" if name in enabled else "no",
            f"handling for {name}",
        )
        for name in reversed(FLAG_NAMES)
    ]
    second = review_policy.select_review_policy(
        blueprint(rows=reversed_rows),
        [{"proof": "browser, security"}],
    )
    expected = (
        "correctness",
        "ux-accessibility",
        "security",
        "architecture-performance-reliability",
    )
    assert first.roles == expected
    assert second.roles == expected
    assert len(first.roles) == review_policy.MAX_REVIEW_AGENTS
    assert len(set(first.roles)) == len(first.roles)
    assert first.to_dict()["max_agents"] == 4


def test_legacy_blueprints_preserve_profile_behavior() -> None:
    legacy = """# Blueprint

Status: approved

## Product Summary

A legacy project with no structured risk table.
"""
    assert roles(legacy, profile="standard") == [
        "correctness",
        "security",
        "architecture",
    ]
    assert roles(legacy, profile="fast-mvp") == ["correctness"]
    assert review_policy.select_review_policy(legacy).legacy is True


def test_legacy_fast_mvp_adds_only_structured_surface_floors() -> None:
    legacy_with_surfaces = """# Blueprint

Status: approved

## Toolchain

- Project class: web-app
- Target platforms: web

## Delivery Contract

- Delivery target: package
"""
    result = review_policy.select_review_policy(
        legacy_with_surfaces,
        profile="fast-mvp",
    )
    assert result.legacy is True
    assert result.roles == (
        "correctness",
        "ux-accessibility",
        "performance-reliability",
    )
    assert result.selections[-1].lenses == (
        "delivery",
        "performance",
        "reliability",
    )
    standard = review_policy.select_review_policy(
        legacy_with_surfaces,
        profile="standard",
    )
    assert standard.roles == (
        "correctness",
        "ux-accessibility",
        "security",
        "architecture-performance-reliability",
    )
    assert standard.combined is True
    assert len(standard.roles) == review_policy.MAX_REVIEW_AGENTS


def test_modern_fast_mvp_still_selects_applicable_roles() -> None:
    text = blueprint(
        {
            "User-facing UI",
            "Authentication or authorization",
            "Multiple services or high coupling",
        }
    )
    assert roles(text, profile="fast-mvp") == [
        "correctness",
        "ux-accessibility",
        "security",
        "architecture",
    ]


def test_fast_mvp_preserves_ui_floor_from_structured_surfaces() -> None:
    text = blueprint(
        project_class="web-app",
        target_platforms="web",
    )
    result = review_policy.select_review_policy(
        text,
        profile="fast-mvp",
    )
    assert result.roles == ("correctness", "ux-accessibility")
    assert result.reasons_for("ux-accessibility") == (
        "Structured project class establishes a user-facing interface: web-app",
    )


def test_fast_mvp_preserves_every_security_and_privacy_floor() -> None:
    for flag in review_policy.SECURITY_PRIVACY_FLAGS:
        result = review_policy.select_review_policy(
            blueprint({flag}),
            profile="fast-mvp",
        )
        assert result.roles == ("correctness", "security"), flag
        assert result.selections[-1].lenses == ("security", "privacy")


def test_fast_mvp_preserves_delivery_review_floors() -> None:
    expected_reasons = {
        "private-repo": (
            "Structured delivery contract requires delivery review: private-repo",
        ),
        "preview": (
            "Structured delivery contract requires delivery review: preview",
        ),
        "production": (
            "Structured delivery contract establishes operational reliability risk: production",
        ),
        "package": (
            "Structured delivery contract requires delivery review: package",
        ),
        "ios-release": (
            "Structured delivery contract requires delivery review: ios-release",
        ),
    }
    for target, expected in expected_reasons.items():
        result = review_policy.select_review_policy(
            blueprint(delivery_target=target),
            profile="fast-mvp",
        )
        assert result.roles == (
            "correctness",
            "performance-reliability",
        ), target
        assert "delivery" in result.selections[-1].lenses
        assert result.reasons_for("performance-reliability") == expected

    source_only = review_policy.select_review_policy(
        blueprint(delivery_target="source-only"),
        profile="fast-mvp",
    )
    assert source_only.roles == ("correctness",)


def test_delivery_plan_proof_establishes_review_floor() -> None:
    result = review_policy.select_review_policy(
        blueprint(),
        [{"proof": "unit, delivery, package"}],
        profile="fast-mvp",
    )
    assert result.roles == ("correctness", "performance-reliability")
    assert result.reasons_for("performance-reliability") == (
        "Plan proof contract requires delivery review: delivery, package",
    )


def test_lifecycle_delivery_contract_establishes_review_floor() -> None:
    contract = {
        "schema": "star-forge.delivery-contract.v1",
        "target": {
            "kind": "platform-specific",
            "platform": "ios",
        },
    }
    result = review_policy.select_review_policy(
        blueprint(),
        profile="fast-mvp",
        delivery_contract=contract,
    )
    assert result.project_surfaces.delivery_targets == (
        "source-only",
        "platform-specific",
    )
    assert result.project_surfaces.target_platforms == ("linux", "ios")
    assert result.roles == (
        "correctness",
        "ux-accessibility",
        "performance-reliability",
    )
    assert (
        "Structured target platform establishes a user-facing interface: ios"
        in result.reasons_for("ux-accessibility")
    )
    assert (
        "Structured delivery contract requires delivery review: platform-specific"
        in result.reasons_for("performance-reliability")
    )


def test_duplicate_flag_rows_cannot_duplicate_roles_or_reasons() -> None:
    rows = [
        ("Authentication or authorization", "yes", "required auth"),
        ("Authentication or authorization", "yes", "required auth"),
        ("Privacy obligations", "yes", "privacy handling"),
    ]
    result = review_policy.select_review_policy(blueprint(rows=rows))
    assert result.roles == ("correctness", "security")
    assert result.reasons_for("security") == (
        "Risk flag `Authentication or authorization` is yes: required auth",
        "Risk flag `Privacy obligations` is yes: privacy handling",
    )


def test_cli_policy_is_source_bound_and_spawn_plan_carries_reasons() -> None:
    star_forge = load_star_forge()
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        (project / "Blueprint.md").write_text(
            blueprint(
                {
                    "User-facing UI",
                    "Authentication or authorization",
                }
            ),
            encoding="utf-8",
        )
        (project / "Plan.md").write_text(
            """# Plan

| Task | Description | Status | Mode | Files | Depends | ACs | Proof | Verify | Evidence |
|---|---|---|---|---|---|---|---|---|---|
| SF-1 | Build | complete | solo | src/app.py | - | AC-1 | browser, security | python3 -m compileall src | src/app.py |
""",
            encoding="utf-8",
        )
        source = project / "src" / "app.py"
        source.parent.mkdir(parents=True)
        source.write_text("print('ok')\n", encoding="utf-8")
        policy = star_forge.required_review_policy(project)
        assert policy.source_hash == star_forge.source_hash(project)
        assert policy.roles == (
            "correctness",
            "ux-accessibility",
            "security",
        )
        tasks = star_forge.parse_tasks(project / "Plan.md")
        spawned = star_forge.spawn_plan(project, tasks, "review")
        assert [item["role"] for item in spawned] == list(policy.roles)
        assert all(item["reasons"] for item in spawned)
        assert all(policy.source_hash in item["spawn"] for item in spawned)


def test_cli_policy_reads_structured_delivery_lifecycle_state() -> None:
    star_forge = load_star_forge()
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        (project / "Blueprint.md").write_text(
            blueprint(),
            encoding="utf-8",
        )
        delivery_path = (
            project / star_forge.project_lifecycle.DELIVERY_CONTRACT_PATH
        )
        delivery_path.parent.mkdir(parents=True)
        delivery_path.write_text(
            json.dumps(
                star_forge.project_lifecycle.make_delivery_contract(
                    delivery_target="package",
                )
            ),
            encoding="utf-8",
        )
        policy = star_forge.required_review_policy(
            project,
            bind_source_hash=False,
        )
        assert policy.roles == (
            "correctness",
            "performance-reliability",
        )
        assert policy.reasons_for("performance-reliability") == (
            "Structured delivery contract requires delivery review: package",
        )


def test_new_roles_are_accepted_by_findings_loader() -> None:
    star_forge = load_star_forge()
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        (project / "Blueprint.md").write_text(
            blueprint({"User-facing UI"}),
            encoding="utf-8",
        )
        source_hash = star_forge.source_hash(project)
        scope = "policy-test"
        root = star_forge.reviews_scope_dir(project, scope)
        root.mkdir(parents=True)
        path = root / "ux-accessibility.findings.json"
        path.write_text(
            json.dumps(
                {
                    "role": "ux-accessibility",
                    "source_hash": source_hash,
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        files, problems = star_forge.load_review_findings(project, scope)
        assert problems == []
        assert [item["role"] for item in files] == ["ux-accessibility"]


def test_operating_card_exposes_all_four_adaptive_review_spawns() -> None:
    star_forge = load_star_forge()
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        state = {
            "versions": {},
            "phase": "review",
            "required_next_action": "Review.",
            "profile_lock": {},
            "hook_trust_notice": {},
            "spawn_plan": [
                {"spawn": f"spawn review-{index}"}
                for index in range(4)
            ],
        }
        card = star_forge.operating_card(project, state)
        assert all(f"spawn review-{index}" in card for index in range(4))


def test_canonical_reviewer_prompt_documents_adaptive_roles() -> None:
    canonical = (
        ROOT / "agents" / "reviewer" / "agent.md"
    ).read_text(encoding="utf-8")
    generated = (
        ROOT / ".codex" / "agents" / "starforge-reviewer.toml"
    ).read_text(encoding="utf-8")
    for value in (
        "## Adaptive role contract",
        "`ux-accessibility`",
        "`performance-reliability`",
        "`architecture-performance-reliability`",
        "Use that exact role in the findings file",
    ):
        assert value in canonical
        assert value in generated


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
    print(
        f"\n{len(tests) - failures}/{len(tests)} "
        "adaptive review policy tests passed"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
