#!/usr/bin/env python3
"""Focused tests for the Star Forge v0.4 capability router.

Run with: python3 tests/test_v04_routing.py
"""

from __future__ import annotations

import copy
import json
import sys
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from starforge import routing


def decisions_by_need(result: routing.RoutingResult) -> dict[str, routing.RouteDecision]:
    return {decision.need: decision for decision in result.decisions}


def test_catalog_is_versioned_complete_and_policy_only() -> None:
    catalog = routing.load_catalog()
    assert catalog["schema"] == routing.CATALOG_SCHEMA
    assert catalog["policy"] == {
        "installation": "suggest-only",
        "requires_user_action": True,
        "unknown_capability": "unavailable",
        "missing_capability": "report-with-fallback",
    }
    expected_purposes = {
        "Requirements and OpenAI API work",
        "UI pattern discovery",
        "Existing design implementation",
        "Original visual concepts",
        "Web implementation",
        "React and Next.js quality",
        "Component systems",
        "Payments",
        "Postgres and backend data",
        "Local web QA",
        "Authenticated browser state",
        "General GUI QA",
        "iOS implementation",
        "iOS verification",
        "macOS implementation and verification",
        "React Native implementation",
        "Security review",
        "GitHub lifecycle",
        "Simple or internal hosting",
        "Production web hosting",
        "AI SDK applications",
        "ChatGPT applications",
    }
    assert {route["purpose"] for route in catalog["routes"]} == expected_purposes
    source = (ROOT / "scripts" / "starforge" / "routing.py").read_text(encoding="utf-8")
    assert "mobbin-mcp" not in source
    assert "figma-plugin" not in source
    assert "build-web-apps" not in source


def test_project_flags_proofs_and_delivery_derive_needs_in_catalog_order() -> None:
    catalog = routing.load_catalog()
    request = routing.make_request(
        project_class="nextjs",
        blueprint_flags={
            "ui": True,
            "payments": True,
            "security_sensitive": True,
            "ignored_false_flag": False,
        },
        proof_kinds=["browser", "security", "github"],
        delivery_target="vercel",
    )
    assert routing.required_needs(catalog, request) == (
        "ui-pattern-discovery",
        "web-implementation",
        "react-next-quality",
        "payments",
        "local-web-qa",
        "security",
        "github-lifecycle",
        "vercel-delivery",
    )


def test_dedicated_capabilities_win_and_aliases_are_discovery_data() -> None:
    result = routing.resolve_routes(
        project_class="nextjs",
        blueprint_flags=["ui", "payments"],
        proof_kinds=["browser"],
        delivery_target="vercel",
        available_capabilities=[
            "mobbin",
            "build web apps",
            "react best practices",
            "stripe",
            "browser plugin",
            "vercel plugin",
            "playwright",
        ],
    )
    decisions = decisions_by_need(result)
    assert decisions["ui-pattern-discovery"].selected["id"] == "mobbin-mcp"
    assert decisions["web-implementation"].selected["id"] == "build-web-apps"
    assert decisions["react-next-quality"].selected["id"] == "react-best-practices"
    assert decisions["payments"].selected["id"] == "stripe-guidance"
    assert decisions["local-web-qa"].selected["id"] == "in-app-browser"
    assert decisions["vercel-delivery"].selected["id"] == "vercel"
    assert not result.blocked
    assert not result.degraded


def test_missing_preferred_capability_reports_selected_fallback() -> None:
    result = routing.resolve_routes(
        project_class="web",
        blueprint_flags=["ui"],
        proof_kinds=["browser"],
        available_capabilities=["image generation", "playwright"],
    )
    decisions = decisions_by_need(result)

    design = decisions["ui-pattern-discovery"]
    assert design.status == "degraded"
    assert design.fallback_used
    assert design.selected["id"] == "imagegen"
    assert [item["id"] for item in design.unavailable[:2]] == ["mobbin-mcp", "figma-plugin"]

    web = decisions["web-implementation"]
    assert web.status == "degraded"
    assert web.selected["id"] == "repository-web-guidance"
    assert web.selected["kind"] == "shell"
    assert web.selected["safe"] is True
    assert [item["id"] for item in web.unavailable] == ["build-web-apps"]

    qa = decisions["local-web-qa"]
    assert qa.status == "degraded"
    assert qa.selected["id"] == "playwright-collector"
    assert [item["id"] for item in qa.unavailable] == ["in-app-browser"]


def test_unavailable_required_capability_is_an_explicit_blocker() -> None:
    decision = routing.resolve_route("ios-verification", available_capabilities=[])
    assert decision.status == "blocked"
    assert decision.fallback_used
    assert decision.selected == {
        "id": "ios-verification-unavailable",
        "label": "iOS verification unavailable blocker",
        "kind": "blocker",
    }
    assert [item["id"] for item in decision.unavailable] == [
        "xcodebuildmcp",
        "ios-simulator-browser",
    ]


def test_optional_install_suggestions_require_material_dependency_and_user_action() -> None:
    ordinary = routing.resolve_route(
        "existing-design-implementation",
        available_capabilities=[],
    )
    assert ordinary.install_suggestion is None

    material = routing.resolve_route(
        "existing-design-implementation",
        available_capabilities=[],
        material=True,
    )
    assert material.install_suggestion == {
        "need": "existing-design-implementation",
        "capability": "figma-plugin",
        "plugin_id": "figma@openai-curated-remote",
        "reason": (
            "Existing design implementation materially depends on the preferred "
            "Figma plugin capability."
        ),
        "requires_user_action": True,
        "action": "request-plugin-install",
    }
    module_source = (ROOT / "scripts" / "starforge" / "routing.py").read_text(encoding="utf-8")
    forbidden_mutations = ("request_plugin_install(", "suggest_plugins(", "codex plugin install")
    assert not any(token in module_source for token in forbidden_mutations)


def test_alias_change_requires_only_catalog_data() -> None:
    catalog = routing.load_catalog()
    changed = copy.deepcopy(catalog)
    route = next(item for item in changed["routes"] if item["id"] == "web-implementation")
    route["options"][0]["aliases"] = ["renamed web factory plugin"]

    result = routing.resolve_routes(
        catalog=changed,
        project_class="web",
        available_capabilities=["renamed web factory plugin"],
    )
    assert decisions_by_need(result)["web-implementation"].selected["id"] == "build-web-apps"


def test_deterministic_output_ignores_discovery_order_and_duplicate_inputs() -> None:
    kwargs = {
        "project_class": ["web", "web"],
        "blueprint_flags": ["ui", "ui"],
        "proof_kinds": ["browser", "browser"],
        "delivery_target": ["sites", "sites"],
    }
    first = routing.resolve_routes(
        **kwargs,
        available_capabilities=["sites", "browser", "mobbin", "build-web-apps"],
    )
    second = routing.resolve_routes(
        **kwargs,
        available_capabilities=["build-web-apps", "mobbin", "browser", "sites"],
    )
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(second.to_dict(), sort_keys=True)


def test_catalog_validation_rejects_unsafe_or_out_of_order_fallbacks() -> None:
    catalog = routing.load_catalog()

    unsafe = copy.deepcopy(catalog)
    route = next(item for item in unsafe["routes"] if item["id"] == "web-implementation")
    route["options"][1].pop("safe")
    try:
        routing.validate_catalog(unsafe)
    except routing.RoutingError as exc:
        assert "unsafe shell fallback" in str(exc)
    else:
        raise AssertionError("unsafe automatic shell fallback was accepted")

    out_of_order = copy.deepcopy(catalog)
    route = next(item for item in out_of_order["routes"] if item["id"] == "general-gui-qa")
    route["options"].insert(
        0,
        {
            "id": "unsafe-shell-first",
            "label": "Unsafe shell first",
            "kind": "shell",
            "aliases": [],
        },
    )
    try:
        routing.validate_catalog(out_of_order)
    except routing.RoutingError as exc:
        assert "preference_order" in str(exc)
    else:
        raise AssertionError("out-of-order route was accepted")


def test_invalid_request_never_silently_drops_unknown_needs() -> None:
    try:
        routing.resolve_route("renamed-or-missing-need", available_capabilities=[])
    except routing.RoutingError as exc:
        assert "unknown required needs" in str(exc)
    else:
        raise AssertionError("unknown required need was silently ignored")


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
    print(f"\ntest_v04_routing.py: {passed} passed, {len(failed)} failed, {len(tests)} total")
    if failed:
        print("failed tests: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
