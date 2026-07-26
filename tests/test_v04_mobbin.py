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


def test_package_uses_at_most_the_selected_surface() -> None:
    expected = load_json(FIXTURES / "expected-app-manifest.json")
    manifest = load_json(PLUGIN_MANIFEST)

    assert not MCP_MANIFEST.exists(), "Mobbin must not be packaged through .mcp.json"
    assert "mcpServers" not in manifest

    if APP_MANIFEST.exists():
        assert load_json(APP_MANIFEST) == expected
        assert manifest.get("apps") == "./.app.json"
    else:
        assert "apps" not in manifest, (
            "plugin.json must not advertise the app surface before .app.json exists"
        )


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
