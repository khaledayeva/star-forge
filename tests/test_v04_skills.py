#!/usr/bin/env python3
"""Focused tests for Star Forge v0.4 intake and planning instructions.

Run with: python3 tests/test_v04_skills.py
"""

from __future__ import annotations

import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_TEMPLATE = ROOT / "templates" / "Blueprint.md"
FORGE_PLAN_SKILL = ROOT / "skills" / "forge-plan" / "SKILL.md"
FORGE_SKILL = ROOT / "skills" / "forge" / "SKILL.md"
FORGE_WORK_SKILL = ROOT / "skills" / "forge-work" / "SKILL.md"
FORGE_REVIEW_SKILL = ROOT / "skills" / "forge-review" / "SKILL.md"
CAPABILITY_ROUTING = (
    ROOT / "skills" / "forge" / "references" / "capability-routing.md"
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_blueprint_records_complete_intake_and_explicit_assumptions() -> None:
    text = read(BLUEPRINT_TEMPLATE)
    assert "## Intake Decision Record" in text
    for topic in (
        "Users and their primary need",
        "Core flows and success conditions",
        "Target platforms",
        "Data created, read, stored, or shared",
        "Authentication and authorization",
        "Payments, billing, or financial behavior",
        "External integrations and network access",
        "Design applicability and supplied references",
        "Delivery outcome",
        "operational constraints",
    ):
        assert topic in text, topic
    assert "## Explicit Assumptions" in text
    assert "| ID | Assumption | Basis | Impact if wrong |" in text


def test_blueprint_defines_provider_neutral_toolchain_and_risk_flags() -> None:
    text = read(BLUEPRINT_TEMPLATE)
    assert "## Toolchain" in text
    for field in (
        "Project class",
        "Target platforms",
        "Required capabilities",
        "Preferred route",
        "Accepted fallback",
        "Availability or blocker",
    ):
        assert field in text, field
    assert "## Risk Flags" in text
    for flag in (
        "User-facing UI",
        "Authentication or authorization",
        "Payments or financial data",
        "Secrets or privileged operations",
        "Network access or external input",
        "User, sensitive, or regulated data",
        "Privacy obligations",
        "Security-sensitive behavior",
        "Meaningful dependency exposure",
        "Multiple services or high coupling",
        "Migrations or complex persistence",
        "Performance or reliability constraints",
        "Destructive operations",
    ):
        assert flag in text, flag
    assert "yes`, `no`, or `not applicable" in text
    assert "no named provider is required unless the product" in text


def test_blueprint_supports_grounded_design_options_and_unavailable_state() -> None:
    text = read(BLUEPRINT_TEMPLATE)
    normalized = " ".join(text.split())
    assert "## Design Direction" in text
    assert "Keep this contract provider-neutral" in text
    for source in ("Mobbin", "Figma", "ImageGen", "user-supplied material"):
        assert source in text, source
    assert text.count("### Candidate Direction") == 3
    assert "must present two or three candidate directions" in text
    assert "Grounded by:" in text
    assert "### Documented Unavailable State" in text
    assert "Selected direction:" in text
    assert "not a separate approval checkpoint" in normalized


def test_blueprint_supports_every_delivery_target_and_authority_boundary() -> None:
    text = read(BLUEPRINT_TEMPLATE)
    assert "## Delivery Contract" in text
    for target in (
        "`source-only`",
        "`private-repo`",
        "`preview`",
        "`production`",
        "`package`",
        "platform-specific target",
    ):
        assert target in text, target
    for field in (
        "Environment:",
        "Release intent:",
        "GitHub requested:",
        "Owner:",
        "Repository:",
        "Visibility:",
        "Existing repository adoption:",
    ):
        assert field in text, field
    assert "Approval authorizes only the non-destructive external writes" in text


def test_skill_asks_only_material_unanswered_decisions() -> None:
    text = read(FORGE_PLAN_SKILL)
    assert "### Adaptive Interview" in text
    assert (
        "A question is material only when its answer could change\n"
        "scope, architecture, design, security, or delivery."
    ) in text
    assert "only the material unanswered decisions" in text
    assert "Skip\nconfirmed and inapplicable topics" in text
    assert "record it under `Explicit Assumptions`" in text
    assert "Never hide an assumption in prose" in text
    assert "Follow up only when an answer reveals a new material branch" in text


def test_skill_keeps_design_research_provider_neutral_and_mobbin_first() -> None:
    text = read(FORGE_PLAN_SKILL)
    assert "follow the capability router's preferred route" in text
    assert "Keep the Blueprint provider-neutral" in text
    assert "may prefer Mobbin for real-world interaction" in text
    assert "two or three\nmaterially distinct, grounded directions" in text
    assert "documented unavailable state" in text
    assert "Never imply that unavailable research ran" in text


def test_skill_uses_one_complete_approval_for_design_and_delivery() -> None:
    text = read(FORGE_PLAN_SKILL)
    assert "### Delivery Contract" in text
    assert "### One Complete Approval" in text
    assert "single complete Blueprint approval" in text
    assert "never a second\napproval gate" in text
    assert "approve-blueprint --project ." in text
    assert "Do not create the lock before approval" in text
    assert "new v0.4 contracts use `Blueprint.lock.json`" in text


def test_four_skill_topology_stays_lean() -> None:
    skill_roots = {
        path.parent.relative_to(ROOT / "skills").as_posix()
        for path in (ROOT / "skills").glob("*/SKILL.md")
    }
    assert skill_roots == {"forge", "forge-plan", "forge-work", "forge-review"}
    assert CAPABILITY_ROUTING.is_file()


def test_entry_skill_owns_one_continuous_lifecycle() -> None:
    text = read(FORGE_SKILL)
    assert "intake -> design -> plan -> foundation -> build -> review -> deliver -> done" in text
    assert "One `$forge` invocation owns that whole lifecycle" in text
    assert "A phase transition is a reason to continue" in text
    assert "Start every turn" in text
    assert ".starforge/state.json" in text
    assert "`required_next_action`" in text
    assert "`spawn_plan`" in text
    assert "After every material change" in text
    for phase in (
        "`setup`",
        "`intake`",
        "`design`",
        "`plan`",
        "`foundation`",
        "`build`",
        "`review`",
        "`deliver`",
        "`done`",
        "`amend`",
        "`blocked`",
    ):
        assert phase in text, phase


def test_phase_skills_return_control_without_a_second_invocation() -> None:
    for path in (FORGE_PLAN_SKILL, FORGE_WORK_SKILL, FORGE_REVIEW_SKILL):
        text = read(path)
        assert "one `$forge` invocation" in text, path
        assert "return control to `$forge`" in text, path
        assert "rerun `run`" in text.lower(), path


def test_skills_use_data_driven_capability_router() -> None:
    entry = read(FORGE_SKILL)
    plan = read(FORGE_PLAN_SKILL)
    work = read(FORGE_WORK_SKILL)
    review = read(FORGE_REVIEW_SKILL)
    reference = read(CAPABILITY_ROUTING)
    for text in (entry, plan, work, review, reference):
        assert "starforge.routing.resolve_routes" in text
        assert "config/capability-routing.json" in text
    for token in (
        "project class",
        "Blueprint",
        "Plan v2",
        "Delivery Contract target",
        "host-discovered capabilities",
        "catalog order",
        "`selected`",
        "`fallback_used`",
        "`unavailable`",
        "`install_suggestion`",
        "suggestion-only",
        "requires user action",
    ):
        assert token in reference, token
    assert "Changing a provider alias or adding a route normally changes" in reference
    assert "not these skills or the\nlifecycle state machine" in reference


def test_plan_skill_requires_traceable_plan_v2() -> None:
    text = read(FORGE_PLAN_SKILL)
    assert (
        "| Task | Description | Status | Mode | Files | Depends | ACs | Proof | "
        "Verify | Evidence |"
    ) in text
    assert "**ACs**" in text
    assert "Every Blueprint criterion must be covered" in text
    assert "**Proof**" in text
    for proof in (
        "`unit`",
        "`integration`",
        "`browser`",
        "`preview`",
        "`native-ios`",
        "`native-macos`",
        "`security`",
        "`github`",
        "`package`",
        "`delivery`",
    ):
        assert proof in text, proof
    assert "Evidence is coordinator-owned" in text
    assert "leave `-`" in text


def test_routing_policy_covers_platform_specific_build_and_proof() -> None:
    work = read(FORGE_WORK_SKILL)
    reference = read(CAPABILITY_ROUTING)
    for token in (
        "Build Web Apps",
        "in-app Browser",
        "Playwright",
        "authenticated Chrome state",
        "Build iOS Apps",
        "XcodeBuildMCP",
        "Simulator",
        "Build macOS Apps",
        "Expo plugin",
        "Codex Security",
    ):
        assert token in work, token
    assert "`authenticated-browser-state` is the only ordinary reason to prefer Chrome" in reference
    assert "Never report a\ndedicated provider as used when only its fallback ran" in reference


def test_foundation_is_private_verified_and_authority_bounded() -> None:
    entry = read(FORGE_SKILL)
    reference = read(CAPABILITY_ROUTING)
    for token in (
        "create a private repository before feature work",
        "`origin`",
        "default branch",
        "initial commit",
        "install CI",
        "GitHub connector",
        "`gh repo create --private`",
        "approved write authority",
        "verify owner, name, remote identity, visibility",
        "Do not overwrite it, change visibility",
        "source-bound foundation evidence",
    ):
        assert token in entry, token
    assert "Adopt existing repositories read-only first" in reference
    assert "Never create a\npublic repository" in reference


def test_delivery_selects_one_provider_and_requires_exact_proof() -> None:
    plan = read(FORGE_PLAN_SKILL)
    review = read(FORGE_REVIEW_SKILL)
    reference = read(CAPABILITY_ROUTING)
    for text in (plan, review, reference):
        assert "Sites" in text
        assert "Vercel" in text
        assert "both" in text
    for token in (
        "`source-only`",
        "`private-repo`",
        "`preview`",
        "`production`",
        "`package`",
        "source hash",
        "repository commit",
        "live URL",
        "smoke result",
        "one honest delivery blocker",
    ):
        assert token in review, token
    assert "exact approved Delivery Contract result" in review


def test_coordinator_owns_evidence_and_completion() -> None:
    entry = read(FORGE_SKILL)
    work = read(FORGE_WORK_SKILL)
    review = read(FORGE_REVIEW_SKILL)
    reference = read(CAPABILITY_ROUTING)
    assert "The coordinator alone runs and records task verification" in entry
    assert "Builder output and self-reports never count as evidence" in work
    assert "Do not\nwrite a reviewer's findings file on its behalf" in review
    for token in (
        "run and record the exact Plan Verify command",
        "capture live browser, native, security, or preview proof",
        "record source-bound Foundation Contract evidence",
        "merge reviewer findings",
        "record source-bound Delivery Contract evidence",
        "run and report `done --strict`",
    ):
        assert token in reference, token


def test_skill_sources_do_not_use_em_dashes() -> None:
    for path in (
        FORGE_SKILL,
        FORGE_PLAN_SKILL,
        FORGE_WORK_SKILL,
        FORGE_REVIEW_SKILL,
        CAPABILITY_ROUTING,
    ):
        assert chr(0x2014) not in read(path), path


def main() -> int:
    tests = [
        (name, func)
        for name, func in list(globals().items())
        if name.startswith("test_") and callable(func)
    ]
    passed = 0
    failed: list[str] = []
    for name, func in tests:
        try:
            func()
        except Exception:
            failed.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {name}")

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        print("Failed tests: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
