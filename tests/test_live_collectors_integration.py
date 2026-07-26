#!/usr/bin/env python3
"""Phase 7 integration checks for live collector release gates.

Run with: python3 tests/test_live_collectors_integration.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "star_forge.py"
CHECK_SH = ROOT / "scripts" / "check.sh"

SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SPEC = importlib.util.spec_from_file_location("star_forge", SCRIPT)
assert SPEC and SPEC.loader
star_forge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(star_forge)

from live_collectors import common as live_common

os.environ["STAR_FORGE_LEARNINGS_HOME"] = tempfile.mkdtemp(prefix="star-forge-live-integration-learnings-")

PLAN_HEADER = (
    "| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |\n"
    "|------|-------------|--------|------|-------|---------|--------|----------|\n"
)
REAL_VERIFY = "python3 -c \"print('ok')\""
TASK = "SF-1"

REQUIRED_CHECK_TESTS = {
    "tests/test_star_forge.py",
    "tests/test_live_proof_commands.py",
    "tests/test_live_browser_playwright.py",
    "tests/test_live_preview.py",
    "tests/test_live_native_ios.py",
    "tests/test_live_native_macos.py",
    "tests/test_live_security_adapter.py",
    "tests/test_live_github_pr.py",
    "tests/test_live_collectors_integration.py",
}

REQUIRED_CHECK_COMPILES = {
    "scripts/star_forge.py",
    "scripts/live_collectors/__init__.py",
    "scripts/live_collectors/common.py",
    "scripts/live_collectors/browser_playwright.py",
    "scripts/live_collectors/preview.py",
    "scripts/live_collectors/native_ios.py",
    "scripts/live_collectors/native_macos.py",
    "scripts/live_collectors/security_adapter.py",
    "scripts/live_collectors/github_pr.py",
}


def run_star(args: list[str]) -> tuple[int, dict[str, Any], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = star_forge.main(args)
    out = stdout.getvalue()
    err = stderr.getvalue()
    payload = json.loads(out) if out.strip().startswith("{") else {}
    return code, payload, err


def init_project(project: Path) -> None:
    code, payload, err = run_star(["init", "--project", str(project), "--no-agents"])
    assert code == 0, err or payload
    src = project / "src" / "app.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("print('hello live integration')\n", encoding="utf-8")
    (project / "Plan.md").write_text(
        "# Plan.md\n\n"
        + PLAN_HEADER
        + f"| {TASK} | Build live integration test app | ready | solo | src/app.py | - | {REAL_VERIFY} | - |\n",
        encoding="utf-8",
    )


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_clean_manifest(project: Path, collector: str = "preview") -> Path:
    root = live_common.live_collector_dir(project, TASK, collector)
    artifact = write_text(root / "evidence.txt", "release gate evidence\n")
    current_source = star_forge.source_hash(project)
    return live_common.write_live_manifest(
        project,
        task=TASK,
        collector=collector,
        command_argv=["integration-test", collector],
        tool_versions={"integration": "1"},
        artifacts={"evidence": artifact},
        summary={"purpose": "release gate"},
        source_hash_before=current_source,
        source_hash_after=current_source,
        runtime_asset_hash=live_common.compute_runtime_asset_hash(project),
    )


def rules(payload: dict[str, Any]) -> set[str]:
    return {str(item.get("rule")) for item in payload.get("problems", []) if isinstance(item, dict)}


def assert_pass(code: int, payload: dict[str, Any]) -> None:
    assert code == 0, payload
    assert payload["verdict"] == "PASS", payload


def assert_fail(code: int, payload: dict[str, Any], expected_rule: str) -> None:
    assert code == 1, payload
    assert payload["verdict"] == "FAIL", payload
    assert expected_rule in rules(payload), payload.get("problems")


def test_release_check_wrapper_includes_core_and_all_live_suites() -> None:
    text = CHECK_SH.read_text(encoding="utf-8")
    assert "python3 -m json.tool .codex-plugin/plugin.json" in text
    assert "python3 -m json.tool hooks/hooks.json" in text
    assert "sh -n scripts/install-codex.sh" in text
    assert "sh -n scripts/release-check.sh" in text
    compile_patterns = ("scripts/star_forge.py", "scripts/starforge/*.py", "scripts/live_collectors/*.py")
    assert all(any(Path(path).match(pattern) for pattern in compile_patterns)
               for path in REQUIRED_CHECK_COMPILES)
    assert "for suite in tests/test_*.py" in text
    assert all(Path(path).match("tests/test_*.py") for path in REQUIRED_CHECK_TESTS)


def test_strict_proof_rejects_domain_profiles_without_dedicated_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        manifest = write_clean_manifest(project, collector="security")
        code, payload, err = run_star([
            "proof-run",
            "--project", str(project),
            "--task", TASK,
            "--profile", "security",
            "--artifact", str(manifest),
            "--strict",
        ])
        assert err == ""
        assert_fail(code, payload, "proof-profile")

        outside = project / "fixtures" / "manifest.json"
        outside.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(manifest, outside)
        code, payload, _ = run_star([
            "proof-run",
            "--project", str(project),
            "--task", TASK,
            "--profile", "security",
            "--artifact", str(outside),
            "--strict",
        ])
        assert_fail(code, payload, "manifest-scope")

        stale = write_clean_manifest(project, collector="security")
        write_text(project / "src" / "app.py", "print('changed after collection')\n")
        code, payload, _ = run_star([
            "proof-run",
            "--project", str(project),
            "--task", TASK,
            "--profile", "security",
            "--artifact", str(stale),
            "--strict",
        ])
        assert_fail(code, payload, "manifest-source")


def test_repository_fixtures_cannot_be_used_directly_as_release_proof() -> None:
    fixture_sources = [
        ROOT / "fixtures" / "github-pr" / "connector-happy.json",
        ROOT / "fixtures" / "native-ios" / "mcp-transcript-happy.json",
        ROOT / "fixtures" / "security-reports" / "codex-security-report.json",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        for source in fixture_sources:
            target = project / "fixtures" / source.relative_to(ROOT / "fixtures")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            code, payload, _ = run_star([
                "proof-run",
                "--project", str(project),
                "--task", TASK,
                "--profile", "preview",
                "--artifact", str(target),
                "--strict",
            ])
            assert_fail(code, payload, "manifest-scope")
            assert "manifest-field" in rules(payload), payload.get("problems")


def test_docs_capture_dogfood_release_boundaries() -> None:
    live_tools = (ROOT / "docs" / "live-tools.md").read_text(encoding="utf-8")
    dogfood = (ROOT / "docs" / "live-tools-dogfood.md").read_text(encoding="utf-8")
    combined = live_tools + "\n" + dogfood
    required_phrases = [
        "artifact suppliers",
        ".starforge/live/<task-id>/<collector>/",
        "Strict proof fails closed",
        "Fixtures under `fixtures/` are not release proof",
        "Do not run `complete-task`",
        "scripts/check.sh",
    ]
    for phrase in required_phrases:
        assert phrase in combined, phrase


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
    print(f"\ntest_live_collectors_integration.py: {passed} passed, {len(failed)} failed, {len(tests)} total")
    if failed:
        print("failed tests: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
