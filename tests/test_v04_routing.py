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
        "Expo implementation",
        "React Native and Expo platform delivery",
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
        delivery_target="preview",
        delivery_provider="vercel",
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
        delivery_target="preview",
        delivery_provider="vercel",
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


def test_web_delivery_routes_follow_the_contract_provider_not_target_aliases() -> None:
    cases = (
        ("preview", "sites", "sites-delivery", "sites"),
        ("preview", "vercel", "vercel-delivery", "vercel"),
        ("production", "sites", "sites-delivery", "sites"),
        ("production", "vercel", "vercel-delivery", "vercel"),
    )
    for target, provider, need, selected in cases:
        result = routing.resolve_routes(
            delivery_target=target,
            delivery_provider=provider,
            available_capabilities=[provider],
        )
        decisions = decisions_by_need(result)
        assert tuple(decisions) == (need,)
        assert decisions[need].selected["id"] == selected
        assert decisions[need].required_by == (f"delivery_provider:{provider}",)

    legacy = routing.resolve_routes(
        delivery_target="sites",
        available_capabilities=["sites"],
    )
    assert decisions_by_need(legacy)["sites-delivery"].selected["id"] == "sites"


def test_conflicting_delivery_selectors_fail_closed() -> None:
    for target, provider in (("sites", "vercel"), ("vercel", "sites")):
        try:
            routing.resolve_routes(
                delivery_target=target, delivery_provider=provider,
                available_capabilities=["sites", "vercel"])
        except routing.RoutingError as exc:
            assert "resolve to one provider" in str(exc)
        else:
            raise AssertionError("conflicting delivery selectors were accepted")


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


def test_react_native_and_expo_routes_prefer_the_official_plugin() -> None:
    react_native = routing.resolve_routes(
        project_class="react-native",
        available_capabilities=["official expo plugin", "expo cli"],
    )
    react_native_decisions = decisions_by_need(react_native)
    assert tuple(react_native_decisions) == ("react-native",)
    selected = react_native_decisions["react-native"]
    assert selected.status == "available"
    assert selected.fallback_used is False
    assert selected.selected == {
        "id": "expo-plugin",
        "label": "Official Expo plugin",
        "kind": "plugin",
    }

    expo = routing.resolve_routes(
        project_class="expo",
        available_capabilities=["expo cli", "expo plugin"],
    )
    expo_decisions = decisions_by_need(expo)
    assert tuple(expo_decisions) == ("expo",)
    assert expo_decisions["expo"].selected["id"] == "expo-plugin"
    assert expo_decisions["expo"].status == "available"


def test_react_native_and_expo_fallbacks_are_degraded_or_blocked_honestly() -> None:
    fallback = routing.resolve_route(
        "react-native",
        available_capabilities=["repository-native react native"],
    )
    assert fallback.status == "degraded"
    assert fallback.fallback_used
    assert fallback.selected == {
        "id": "expo-cli",
        "label": "Repository-native React Native or Expo CLI workflow",
        "kind": "shell",
        "safe": True,
    }
    assert [item["id"] for item in fallback.unavailable] == ["expo-plugin"]

    blocked = routing.resolve_route("expo", available_capabilities=[])
    assert blocked.status == "blocked"
    assert blocked.fallback_used
    assert blocked.selected["id"] == "expo-unavailable"
    assert [item["id"] for item in blocked.unavailable] == [
        "expo-plugin",
        "expo-cli",
    ]


def test_named_expo_platform_delivery_has_its_own_deterministic_route() -> None:
    preferred = routing.resolve_routes(
        project_class="expo",
        delivery_target=["platform-specific", "expo"],
        available_capabilities=["expo plugin", "expo cli"],
    )
    decisions = decisions_by_need(preferred)
    assert tuple(decisions) == ("expo", "expo-platform-delivery")
    delivery = decisions["expo-platform-delivery"]
    assert delivery.required_by == ("delivery:expo",)
    assert delivery.selected["id"] == "expo-plugin"
    assert delivery.status == "available"

    fallback = routing.resolve_route(
        "expo-platform-delivery",
        available_capabilities=["repository-native expo cli"],
    )
    assert fallback.status == "degraded"
    assert fallback.selected["id"] == "expo-cli"
    assert [item["id"] for item in fallback.unavailable] == ["expo-plugin"]

    blocked = routing.resolve_route(
        "expo-platform-delivery",
        available_capabilities=[],
    )
    assert blocked.status == "blocked"
    assert blocked.selected["id"] == "expo-platform-delivery-unavailable"
    assert [item["id"] for item in blocked.unavailable] == [
        "expo-plugin",
        "expo-cli",
    ]


def test_expo_aliases_and_installation_policy_remain_data_only() -> None:
    catalog = routing.load_catalog()
    changed = copy.deepcopy(catalog)
    route = next(item for item in changed["routes"] if item["id"] == "expo")
    route["options"][0]["aliases"] = ["renamed official expo capability"]

    result = routing.resolve_routes(
        catalog=changed,
        project_class="expo",
        available_capabilities=["renamed official expo capability"],
    )
    assert decisions_by_need(result)["expo"].selected["id"] == "expo-plugin"

    for route_id in ("react-native", "expo", "expo-platform-delivery"):
        route = next(item for item in catalog["routes"] if item["id"] == route_id)
        assert "install" not in route["options"][0]
        assert route["options"][1]["safe"] is True
        assert route["options"][1].get("always_available") is not True


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
    preferred = routing.resolve_route(
        "ui-pattern-discovery",
        available_capabilities=["mobbin"],
        material=True,
    )
    assert preferred.status == "available"
    assert preferred.install_suggestion is None

    fallback = routing.resolve_route(
        "ui-pattern-discovery",
        available_capabilities=["imagegen"],
        material=True,
    )
    assert fallback.status == "degraded"
    assert fallback.install_suggestion is None

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
