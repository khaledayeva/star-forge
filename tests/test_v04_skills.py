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
