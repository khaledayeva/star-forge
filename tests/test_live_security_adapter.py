#!/usr/bin/env python3
"""Focused tests for the Phase 5 security scanner handoff adapter.

Run with: python3 tests/test_live_security_adapter.py
"""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import tempfile
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STAR_FORGE_SCRIPT = ROOT / "scripts" / "star_forge.py"
ADAPTER_SCRIPT = ROOT / "scripts" / "live_collectors" / "security_adapter.py"
FIXTURES = ROOT / "fixtures" / "security-reports"

STAR_SPEC = importlib.util.spec_from_file_location("star_forge", STAR_FORGE_SCRIPT)
assert STAR_SPEC and STAR_SPEC.loader
star_forge = importlib.util.module_from_spec(STAR_SPEC)
STAR_SPEC.loader.exec_module(star_forge)

ADAPTER_SPEC = importlib.util.spec_from_file_location("security_adapter", ADAPTER_SCRIPT)
assert ADAPTER_SPEC and ADAPTER_SPEC.loader
security_adapter = importlib.util.module_from_spec(ADAPTER_SPEC)
ADAPTER_SPEC.loader.exec_module(security_adapter)

from live_collectors import common as live_common

os.environ["STAR_FORGE_LEARNINGS_HOME"] = tempfile.mkdtemp(prefix="star-forge-security-test-learnings-")

PLAN_HEADER = (
    "| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |\n"
    "|------|-------------|--------|------|-------|---------|--------|----------|\n"
)
REAL_VERIFY = "python3 -c \"print('ok')\""


def run_star_forge(args: list[str]) -> tuple[int, dict[str, Any], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = star_forge.main(args)
    out = stdout.getvalue()
    err = stderr.getvalue()
    payload = json.loads(out) if out.strip().startswith("{") else {}
    return code, payload, err


def run_adapter(args: list[str]) -> tuple[int, dict[str, Any]]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = security_adapter.main(args)
    payload = json.loads(stdout.getvalue())
    return code, payload


def init_project(project: Path) -> None:
    code, payload, err = run_star_forge(["init", "--project", str(project), "--no-agents"])
    assert code == 0, err or payload
    (project / "src").mkdir(exist_ok=True)
    (project / "src" / "app.py").write_text("print('hello security adapter')\n", encoding="utf-8")
    (project / "package.json").write_text('{"dependencies":{"example-lib":"1.0.0"}}\n', encoding="utf-8")
    (project / "Plan.md").write_text(
        "# Plan.md\n\n" + PLAN_HEADER
        + f"| SF-1 | Build security adapter test app | ready | solo | src/app.py | - | {REAL_VERIFY} | - |\n",
        encoding="utf-8",
    )


def read_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def write_report(project: Path, payload: dict[str, Any], name: str = "report.json") -> Path:
    path = project / "scanner-reports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def adapter_args(project: Path, report: Path, *, profile: str = "security-diff", source_hash: str | None = None, extra: list[str] | None = None) -> list[str]:
    args = [
        "--project", str(project),
        "--task", "SF-1",
        "--profile", profile,
        "--input", str(report),
        "--input-hash", live_common.file_sha256(report),
        "--source-hash", source_hash if source_hash is not None else live_common.compute_source_hash(project),
    ]
    if extra:
        args.extend(extra)
    return args


def problem_rules(payload: dict[str, Any]) -> set[str]:
    return {str(item.get("rule")) for item in payload.get("problems", []) if isinstance(item, dict)}


def assert_adapter_pass(code: int, payload: dict[str, Any]) -> None:
    assert code == 0, payload
    assert payload["verdict"] == "PASS", payload
    assert "security_handoff_packet" in payload["commands"], payload
    assert "security_proof" in payload["commands"], payload


def assert_adapter_fail(code: int, payload: dict[str, Any], rule: str) -> None:
    assert code == 1, payload
    assert payload["verdict"] == "FAIL", payload
    assert rule in problem_rules(payload), payload.get("problems")


def load_artifact(project: Path, payload: dict[str, Any], key: str) -> Any:
    path = project / payload["artifacts"][key]
    return json.loads(path.read_text(encoding="utf-8"))


def assert_star_fail(code: int, payload: dict[str, Any], rule: str) -> None:
    assert code == 1, payload
    assert payload["verdict"] == "FAIL", payload
    rules = {str(item.get("rule")) for item in payload.get("problems", []) if isinstance(item, dict)}
    assert rule in rules, payload.get("problems")


def assert_star_pass(code: int, payload: dict[str, Any]) -> None:
    assert code == 0, payload
    assert payload["verdict"] == "PASS", payload


def test_codex_security_input_normalizes_and_hands_to_core() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        report = write_report(project, read_fixture("codex-security-report.json"))
        code, payload = run_adapter(adapter_args(project, report))
        assert_adapter_pass(code, payload)

        findings = load_artifact(project, payload, "normalized_findings")
        item = findings["findings"][0]
        assert item["raw_id"] == "CSEC-001"
        assert item["raw_severity"] == "HIGH"
        assert item["severity"] == "high"
        assert item["fingerprint"].startswith("sfsec-")
        assert "[REDACTED" in json.dumps(item)

        handoff = project / payload["artifacts"]["handoff_input"]
        normalized = project / payload["artifacts"]["normalized_findings"]
        manifest = project / payload["artifacts"]["manifest"]
        code, handoff_payload, _ = run_star_forge([
            "security-handoff-packet", "--project", str(project), "--kind", "security-diff",
            "--input", str(handoff), "--strict",
        ])
        assert_star_pass(code, handoff_payload)

        code, proof_payload, _ = run_star_forge([
            "security-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "security-diff", "--scanner", "codex-security",
            "--scanner-version", "1.2.3", "--findings", str(normalized),
            "--artifact", str(manifest), "--strict",
        ])
        assert_star_fail(code, proof_payload, "security-finding")


def test_free_text_token_redaction_reaches_normalized_security_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        basic = "dXNlcjpzdXBlci1zZWNyZXQtcGFzc3dvcmQ="
        access = "access_live_1234567890abcdef"
        report_payload = read_fixture("codex-security-report.json")
        report_payload["findings"][0]["message"] = f"Authorization: Bearer {jwt} access_token={access}"
        report_payload["findings"][0]["evidence"]["snippet"] = f"Authorization: Basic {basic}"
        report = write_report(project, report_payload, "free-text-secrets.json")

        code, payload = run_adapter(adapter_args(project, report))
        assert_adapter_pass(code, payload)
        findings = load_artifact(project, payload, "normalized_findings")
        blob = json.dumps(findings)
        assert jwt not in blob
        assert basic not in blob
        assert access not in blob
        assert "[REDACTED_SECRET]" in blob
        redaction = load_artifact(project, payload, "redaction_report")
        assert redaction["counts"].get("secret_values", 0) >= 3


def test_common_redacts_url_userinfo_and_provider_signed_url_keys() -> None:
    password = "supersecretpassword"
    raw = {
        "url": (
            f"https://reader:{password}@cdn.example.test/pkg.tgz?"
            "X-Amz-Signature=amzsig123&X-Amz-Credential=amzcred123&AWSAccessKeyId=accessid123&safe=ok"
        )
    }

    clean, report = live_common.redact_sensitive_values(raw)
    blob = json.dumps(clean)
    assert f"reader:{password}@" not in blob
    assert password not in blob
    assert "amzsig123" not in blob
    assert "amzcred123" not in blob
    assert "accessid123" not in blob
    assert "safe=ok" in blob
    assert report["secret_values"] >= 4


def test_signed_url_and_hyphenated_api_key_redaction_reaches_security_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        password = "supersecretpassword"
        signed_url = (
            f"https://reader:{password}@cdn.example.test/pkg.tgz?"
            "token=tok123&X-Amz-Signature=amzsig123&X-Amz-Credential=amzcred123&AWSAccessKeyId=accessid123&safe=ok"
            "#access_token=frag123"
        )
        api_key_value = "hyphen-api-key-value"
        x_api_key_value = "x-hyphen-api-key-value"
        report_payload = read_fixture("codex-security-report.json")
        finding = report_payload["findings"][0]
        finding["message"] = f"Signed artifact URL leaked: {signed_url}"
        finding["evidence"]["snippet"] = f"curl '{signed_url}'"
        finding["evidence"]["api-key"] = api_key_value
        finding["evidence"]["x-api-key"] = x_api_key_value
        report = write_report(project, report_payload, "signed-url-secrets.json")

        code, payload = run_adapter(adapter_args(project, report))
        assert_adapter_pass(code, payload)
        findings = load_artifact(project, payload, "normalized_findings")
        blob = json.dumps(findings)
        assert "tok123" not in blob
        assert password not in blob
        assert "reader:" not in blob
        assert "amzsig123" not in blob
        assert "amzcred123" not in blob
        assert "accessid123" not in blob
        assert "frag123" not in blob
        assert api_key_value not in blob
        assert x_api_key_value not in blob
        assert "REDACTED_SECRET" in blob
        assert "[REDACTED]" in blob


def test_star_forge_schema_input_and_dependency_manifest_capture() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        report = write_report(project, read_fixture("star-forge-report.json"))
        code, payload = run_adapter(adapter_args(project, report, profile="dependency-audit"))
        assert_adapter_pass(code, payload)

        handoff = load_artifact(project, payload, "handoff_input")
        assert handoff["kind"] == "dependency-audit"
        assert handoff["scan_scope"] == "full-project"
        assert handoff["dependency_manifests"][0]["path"] == "package.json"
        findings = load_artifact(project, payload, "normalized_findings")
        assert findings["findings"][0]["severity"] == "medium"
        assert findings["findings"][0]["raw_severity"] == "moderate"


def test_unsupported_generic_input_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        report = write_report(project, read_fixture("unsupported-generic-report.json"))
        code, payload = run_adapter(adapter_args(project, report, extra=[
            "--scanner", "generic-json-scanner",
            "--scanner-version", "1.0",
            "--ruleset", "generic",
            "--scan-scope", "full-project",
        ]))
        assert_adapter_fail(code, payload, "security-schema")


def test_missing_provenance_scope_and_stale_source_binding_fail() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        missing_provenance = read_fixture("star-forge-report.json")
        missing_provenance.pop("scanner", None)
        missing_provenance.pop("provenance", None)
        report = write_report(project, missing_provenance, "missing-provenance.json")
        code, payload = run_adapter(adapter_args(project, report))
        assert_adapter_fail(code, payload, "security-provenance")

        missing_scope = read_fixture("star-forge-report.json")
        missing_scope.pop("scan_scope", None)
        report = write_report(project, missing_scope, "missing-scope.json")
        code, payload = run_adapter(adapter_args(project, report))
        assert_adapter_fail(code, payload, "security-scope")

        stale = read_fixture("star-forge-report.json")
        report = write_report(project, stale, "stale.json")
        code, payload = run_adapter(adapter_args(project, report, source_hash="stale-source-hash"))
        assert_adapter_fail(code, payload, "security-source-binding")


def test_unknown_severity_blocks_and_is_preserved() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        report_payload = read_fixture("star-forge-report.json")
        report_payload["findings"][0]["severity"] = "cosmic"
        report = write_report(project, report_payload)
        code, payload = run_adapter(adapter_args(project, report))
        assert_adapter_fail(code, payload, "security-severity")

        findings = load_artifact(project, payload, "normalized_findings")
        item = findings["findings"][0]
        assert item["raw_severity"] == "cosmic"
        assert item["severity"] == "unknown"
        assert item["severity_known"] is False


def test_redacts_absolute_home_token_env_and_snippet_values() -> None:
    openai_key = "sk-" + "abc123def456ghi789jkl012mno345"
    github_token = "ghp_" + "abcd1234abcd1234abcd1234abcd1234"
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        report_payload = read_fixture("codex-security-report.json")
        finding = report_payload["findings"][0]
        finding["severity"] = "low"
        finding["evidence"] = {
            "file": str(project / "src" / "app.py"),
            "home_note": str(Path.home() / "secret-notes.txt"),
            "snippet": (
                "OPENAI_API_KEY=" + openai_key + " "
                "GITHUB_TOKEN=" + github_token
            ),
            "token": github_token
        }
        report = write_report(project, report_payload)
        code, payload = run_adapter(adapter_args(project, report))
        assert_adapter_pass(code, payload)

        findings = load_artifact(project, payload, "normalized_findings")
        text = json.dumps(findings)
        assert str(project) not in text
        assert str(Path.home()) not in text
        assert "sk-abc123" not in text
        assert "ghp_abcd" not in text
        assert "OPENAI_API_KEY=sk-" not in text
        assert findings["findings"][0]["evidence"]["file"] == "src/app.py"
        assert "[REDACTED" in text

        report_payload = load_artifact(project, payload, "redaction_report")
        counts = report_payload["counts"]
        assert counts["secret_values"] >= 1
        assert counts["home_paths"] >= 1


def test_clean_trusted_report_passes_security_handoff_and_proof() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        clean = copy.deepcopy(read_fixture("codex-security-report.json"))
        clean["findings"] = []
        report = write_report(project, clean, "clean.json")
        code, payload = run_adapter(adapter_args(project, report, profile="security-deep"))
        assert_adapter_pass(code, payload)

        handoff = project / payload["artifacts"]["handoff_input"]
        normalized = project / payload["artifacts"]["normalized_findings"]
        manifest = project / payload["artifacts"]["manifest"]
        code, handoff_payload, _ = run_star_forge([
            "security-handoff-packet", "--project", str(project), "--kind", "security-deep",
            "--input", str(handoff), "--strict",
        ])
        assert_star_pass(code, handoff_payload)
        code, proof_payload, _ = run_star_forge([
            "security-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "security-deep", "--scanner", "codex-security",
            "--scanner-version", "1.2.3", "--findings", str(normalized),
            "--artifact", str(manifest), "--strict",
        ])
        assert_star_pass(code, proof_payload)


def main() -> int:
    tests = [(name, func) for name, func in list(globals().items()) if name.startswith("test_") and callable(func)]
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
    print(f"\ntest_live_security_adapter.py: {passed} passed, {len(failed)} failed, {len(tests)} total")
    if failed:
        print("failed tests: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
