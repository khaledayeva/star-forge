#!/usr/bin/env python3
"""Star Forge v0.4 packaging and release metadata checks.

Run with: python3 tests/test_v04_release.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / ".codex-plugin" / "plugin.json"
MARKETPLACE_PATH = ROOT / ".agents" / "plugins" / "marketplace.json"
RELEASE_CHECK = ROOT / "scripts" / "release-check.sh"
MOBBIN_APP_ID = "asdk_app_69fdb9081018819193707354f21b366e"


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), path
    return payload


def run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    return subprocess.run(
        args,
        cwd=cwd,
        env=process_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_marketplace_is_canonical_repo_root_package() -> None:
    marketplace = load_json(MARKETPLACE_PATH)
    assert marketplace == {
        "name": "star-forge",
        "interface": {"displayName": "Star Forge"},
        "plugins": [
            {
                "name": "star-forge",
                "source": {"source": "local", "path": "./"},
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_USE",
                },
                "category": "Productivity",
            }
        ],
    }
    assert (ROOT / ".codex-plugin" / "plugin.json").is_file()
    assert (ROOT / "skills" / "forge" / "SKILL.md").is_file()


def test_manifest_has_complete_publisher_and_interface_metadata() -> None:
    manifest = load_json(MANIFEST_PATH)
    assert manifest["name"] == "star-forge"
    assert manifest["repository"] == "https://github.com/khaledayeva/star-forge"
    assert manifest["homepage"] == "https://github.com/khaledayeva/star-forge#readme"
    assert manifest["author"] == {
        "name": "Khaled Ayeva",
        "url": "https://github.com/khaledayeva",
    }
    assert manifest["license"] == "MIT"
    assert manifest["skills"] == "./skills/"
    assert "hooks" not in manifest
    assert manifest["apps"] == "./.app.json"
    assert "mcpServers" not in manifest
    app_manifest_path = (ROOT / manifest["apps"]).resolve()
    assert app_manifest_path == (ROOT / ".app.json").resolve()
    assert load_json(app_manifest_path) == {
        "apps": {"mobbin": {"id": MOBBIN_APP_ID}}
    }

    interface = manifest["interface"]
    for field in (
        "displayName",
        "shortDescription",
        "longDescription",
        "developerName",
        "category",
        "capabilities",
        "websiteURL",
        "defaultPrompt",
        "brandColor",
        "composerIcon",
        "logo",
        "screenshots",
    ):
        assert field in interface, field
    assert interface["websiteURL"] == "https://github.com/khaledayeva/star-forge"
    assert interface["screenshots"] == []
    assert len(interface["defaultPrompt"]) <= 3
    assert all(len(prompt) <= 128 for prompt in interface["defaultPrompt"])


def test_manifest_visual_assets_are_safe_and_present() -> None:
    interface = load_json(MANIFEST_PATH)["interface"]
    for field in ("composerIcon", "logo"):
        relative = interface[field]
        assert isinstance(relative, str) and relative.startswith("./assets/")
        resolved = (ROOT / relative).resolve()
        assert resolved.is_relative_to(ROOT)
        assert resolved.is_file(), relative


def init_agent_fixture(root: Path) -> None:
    shutil.copytree(
        ROOT / "scripts",
        root / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(ROOT / "agents", root / "agents")
    shutil.copytree(ROOT / ".codex" / "agents", root / ".codex" / "agents")


def test_generated_agents_exactly_match_canonical_prompts() -> None:
    result = run(
        ["sh", "scripts/release-check.sh", "--agents-only"],
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_release_gate_rejects_generated_agent_prompt_drift() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp).resolve()
        init_agent_fixture(fixture)
        prompt = fixture / "agents" / "builder" / "agent.md"
        prompt.write_text(
            prompt.read_text(encoding="utf-8") + "\nCanonical prompt changed.\n",
            encoding="utf-8",
        )
        result = run(
            ["sh", "scripts/release-check.sh", "--agents-only"],
            cwd=fixture,
        )
        assert result.returncode == 1
        assert "does not match its canonical prompt" in result.stderr
        assert "starforge-builder.toml != agents/builder/agent.md" in result.stderr


def init_release_fixture(root: Path) -> str:
    (root / ".codex-plugin").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copyfile(RELEASE_CHECK, root / "scripts" / "release-check.sh")
    shutil.copyfile(MANIFEST_PATH, root / ".codex-plugin" / "plugin.json")
    (root / "package.txt").write_text("first\n", encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "release-test@example.invalid"],
        ["git", "config", "user.name", "Release Test"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "baseline"],
    ):
        result = run(command, cwd=root)
        assert result.returncode == 0, result.stderr
    return run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()


def test_release_gate_rejects_unchanged_version_for_publishable_diff() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp).resolve()
        base = init_release_fixture(fixture)
        (fixture / "package.txt").write_text("changed\n", encoding="utf-8")
        result = run(
            ["sh", "scripts/release-check.sh", "--version-only"],
            cwd=fixture,
            env={"STAR_FORGE_RELEASE_BASE": base},
        )
        assert result.returncode == 1
        assert "without a new plugin version or cachebuster" in result.stderr


def test_release_gate_accepts_new_cachebuster_for_publishable_diff() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp).resolve()
        base = init_release_fixture(fixture)
        (fixture / "package.txt").write_text("changed\n", encoding="utf-8")
        manifest = load_json(fixture / ".codex-plugin" / "plugin.json")
        core = manifest["version"].split("+", 1)[0]
        manifest["version"] = f"{core}+codex.release-test"
        (fixture / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        result = run(
            ["sh", "scripts/release-check.sh", "--version-only"],
            cwd=fixture,
            env={"STAR_FORGE_RELEASE_BASE": base},
        )
        assert result.returncode == 0, result.stderr


def test_release_gate_counts_untracked_package_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp).resolve()
        base = init_release_fixture(fixture)
        (fixture / "new-package-file.txt").write_text("new\n", encoding="utf-8")
        result = run(
            ["sh", "scripts/release-check.sh", "--version-only"],
            cwd=fixture,
            env={"STAR_FORGE_RELEASE_BASE": base},
        )
        assert result.returncode == 1
        assert "new-package-file.txt" in result.stderr


def test_repository_metadata_passes_version_only_release_gate() -> None:
    result = run(
        ["sh", "scripts/release-check.sh", "--version-only"],
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr


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
    print(
        f"\ntest_v04_release.py: "
        f"{passed} passed, {len(failed)} failed, {len(tests)} total"
    )
    if failed:
        print("failed tests: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
