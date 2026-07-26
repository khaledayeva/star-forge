#!/usr/bin/env python3
"""Focused tests for the Star Forge v0.4 Mobbin registration decision.

Run with: python3 tests/test_v04_mobbin.py
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "mobbin"
DECISION_DOC = ROOT / "docs" / "decisions" / "mobbin-integration.md"
PLUGIN_MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
APP_MANIFEST = ROOT / ".app.json"
MCP_MANIFEST = ROOT / ".mcp.json"
CAPABILITY_ROUTING = ROOT / "config" / "capability-routing.json"
FORGE_PLAN_SKILL = ROOT / "skills" / "forge-plan" / "SKILL.md"
BUILDER_AGENT = ROOT / "agents" / "builder" / "agent.md"
REVIEWER_AGENT = ROOT / "agents" / "reviewer" / "agent.md"

APP_ID = "asdk_app_69fdb9081018819193707354f21b366e"
MCP_URL = "https://api.mobbin.com/mcp"


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), path
    return payload


def test_decision_selects_registered_app_and_oauth_only() -> None:
    decision = load_json(FIXTURES / "decision.json")
    assert decision["schema"] == "star-forge.mobbin-integration-decision.v1"
    assert decision["acceptance_criterion"] == "AC-10"
    assert decision["selected_surface"] == ".app.json"
    assert decision["rejected_surface"] == ".mcp.json"
    assert decision["registered_app_id"] == APP_ID
    assert decision["app_required"] is False
    assert decision["mcp"] == {
        "url": MCP_URL,
        "transport": "streamable-http",
        "authorization": "oauth",
        "repository_api_key_allowed": False,
        "rest_fallback_allowed": False,
    }
    assert decision["codex_app"] == {
        "credential_source": "chatgpt-app-connection",
        "setup_surface": "ChatGPT",
        "plugin_adds_separate_mcp_registration": False,
    }


def test_registered_app_snapshot_matches_public_mapping() -> None:
    evidence = load_json(FIXTURES / "registered-app-evidence.json")
    assert evidence["schema"] == "star-forge.registered-app-evidence.v1"
    assert evidence["source"] == {
        "product": "Codex Desktop",
        "version": "0.139.0",
        "method": "app/list",
        "force_refetch": True,
    }
    record = evidence["record"]
    assert record["id"] == APP_ID
    assert record["name"] == "Mobbin"
    assert record["distribution_channel"] == "ECOSYSTEM_DIRECTORY"
    assert record["categories"] == ["DESIGN"]
    assert record["install_url"].endswith(f"/mobbin/{APP_ID}")
    assert record["is_enabled"] is True
    assert record["accessibility_is_user_specific"] is True


def test_expected_app_manifest_is_minimal_and_optional() -> None:
    expected = load_json(FIXTURES / "expected-app-manifest.json")
    assert expected == {"apps": {"mobbin": {"id": APP_ID}}}
    assert "required" not in expected["apps"]["mobbin"]
    serialized = json.dumps(expected).lower()
    assert "api_key" not in serialized
    assert "bearer" not in serialized
    assert MCP_URL not in serialized


def test_package_uses_only_the_selected_surface() -> None:
    expected = load_json(FIXTURES / "expected-app-manifest.json")
    manifest = load_json(PLUGIN_MANIFEST)

    assert not MCP_MANIFEST.exists(), "Mobbin must not be packaged through .mcp.json"
    assert "mcpServers" not in manifest
    assert load_json(APP_MANIFEST) == expected
    assert manifest.get("apps") == "./.app.json"


def test_capability_route_is_mobbin_first_and_oauth_only() -> None:
    catalog = load_json(CAPABILITY_ROUTING)
    route = next(
        item for item in catalog["routes"] if item["id"] == "ui-pattern-discovery"
    )
    mobbin = route["options"][0]
    assert mobbin["id"] == "mobbin-mcp"
    assert mobbin["kind"] == "mcp"
    assert mobbin["auth"] == "oauth"
    assert mobbin["connection"] == {
        "package_surface": ".app.json",
        "app_id": APP_ID,
        "desktop_credentials": "chatgpt-app-connection",
        "cli_add_command": f"codex mcp add mobbin --url {MCP_URL}",
        "cli_login_command": "codex mcp login mobbin",
        "repository_credentials_allowed": False,
        "rest_fallback_allowed": False,
    }
    assert mobbin["research"] == {
        "priority": "preferred",
        "query_order": [
            "product-platform-primary-flow",
            "interaction-pattern-and-constraints",
            "broader-adjacent-flow-if-needed",
        ],
        "candidate_minimum": 3,
        "candidate_maximum": 5,
    }
    assert [item["id"] for item in route["options"][1:]] == [
        "figma-plugin",
        "imagegen",
        "supplied-design-references",
        "design-research-unavailable",
    ]


def test_plan_skill_uses_mobbin_first_queries_and_normalized_candidates() -> None:
    text = FORGE_PLAN_SKILL.read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    assert "#### Mobbin-first research" in text
    assert "research there before using another source" in normalized
    for query_input in (
        "product domain",
        "target platform",
        "primary user job",
        "target flow",
        "interaction pattern",
        "material constraints",
    ):
        assert query_input in normalized, query_input
    assert "three to five grounded research candidates" in normalized
    assert "Do not pad the set with duplicates" in text
    for candidate_field in (
        "candidate id and source type",
        "stable reference",
        "product class, platform, and observed flow",
        "observed interaction or information-architecture pattern",
        "why the pattern is relevant",
        "`Borrow`",
        "`Avoid`",
        "product-specific design and verification constraints",
    ):
        assert candidate_field in text, candidate_field
    assert "The normalized fields are the provider-neutral research record" in text


def test_plan_skill_gives_supported_oauth_setup_and_explicit_failure_states() -> None:
    text = FORGE_PLAN_SKILL.read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    assert "connect the Mobbin App in ChatGPT" in normalized
    assert f"codex mcp add mobbin --url {MCP_URL}" in text
    assert "codex mcp login mobbin" in text
    for forbidden in (
        "request, store, or commit a Mobbin API key",
        "repository `.mcp.json`",
        "undocumented REST fallback",
    ):
        assert forbidden in text, forbidden
    for state in (
        "authentication failure",
        "permission failure",
        "transport failure",
        "empty results",
        "rate limits",
    ):
        assert state in normalized, state
    assert "do not fabricate missing candidates" in normalized
    assert "Otherwise try accepted fallbacks in router order" in text
    assert "Never imply that unavailable research ran" in text


def test_originality_constraints_reach_planners_builders_and_reviewers() -> None:
    plan = FORGE_PLAN_SKILL.read_text(encoding="utf-8")
    builder = BUILDER_AGENT.read_text(encoding="utf-8")
    reviewer = REVIEWER_AGENT.read_text(encoding="utf-8")
    combined = "\n".join((plan, builder, reviewer))
    for token in (
        "`Borrow`",
        "`Avoid`",
        "source branding",
        "trade dress",
        "assets",
        "copy",
        "proprietary content",
        "distinctive composition",
        "screen-level layout",
    ):
        assert token in combined, token
    assert "principle, not as a screen to reproduce" in builder
    assert "contract gap instead of guessing" in builder
    assert "Research should contribute reusable behavior and constraints" in reviewer
    assert "Do not accept claims that unavailable research ran" in reviewer
    assert "single complete Blueprint approval" in plan
    assert "never a second\napproval gate" in plan


def test_plugin_scope_evidence_captures_duplicate_registration_risk() -> None:
    evidence = load_json(FIXTURES / "plugin-scope-evidence.json")
    assert evidence["schema"] == "star-forge.mobbin-plugin-scope-evidence.v1"
    rules = {item["case"]: item for item in evidence["observed_rules"]}
    assert rules["registered-app-binding"]["mcp_registrations_added"] == 0
    assert rules["plugin-mcp-only"]["result"] == "separate-plugin-registration"
    assert rules["plugin-and-config-same-name"]["winner"] == "config"
    assert rules["plugin-and-config-same-name"]["result"] == (
        "plugin-registration-shadowed"
    )
    assert rules["plugin-and-config-different-names"]["active_registrations"] == 2
    assert rules["plugin-and-config-different-names"]["result"] == (
        "duplicate-remote-connection-risk"
    )
    assert all(
        url.startswith("https://github.com/openai/codex/")
        for url in evidence["official_codex_source"].values()
    )


def test_cli_path_is_user_scoped_oauth_not_plugin_packaging() -> None:
    cli = load_json(FIXTURES / "decision.json")["codex_cli"]
    assert cli == {
        "add_command": f"codex mcp add mobbin --url {MCP_URL}",
        "login_command": "codex mcp login mobbin",
        "scope": "user-configured",
        "packaged_by_plugin": False,
    }


def test_decision_doc_records_implementation_contract_and_sources() -> None:
    text = DECISION_DOC.read_text(encoding="utf-8")
    required = (
        "Star Forge will package `.app.json`",
        "It will not package `.mcp.json`",
        APP_ID,
        MCP_URL,
        "Streamable HTTP",
        "OAuth",
        "codex mcp add mobbin --url",
        "codex mcp login mobbin",
        "https://docs.mobbin.com/mcp/clients/codex-app",
        "https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/catalog.rs",
    )
    assert all(token in text for token in required)
    assert "undocumented REST" in text
    assert "stores no Mobbin secret" in text


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
