#!/usr/bin/env python3
"""Star Forge v0.4 packaging and release metadata checks.

Run with: python3 tests/test_v04_release.py
"""

from __future__ import annotations

import ast
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MANIFEST_PATH = ROOT / ".codex-plugin" / "plugin.json"
MARKETPLACE_PATH = ROOT / ".agents" / "plugins" / "marketplace.json"
RELEASE_CHECK = ROOT / "scripts" / "release-check.sh"
INSTALL_CODEX = ROOT / "scripts" / "install-codex.sh"
MOBBIN_APP_ID = "asdk_app_69fdb9081018819193707354f21b366e"
RC_TMP_PARENT = os.environ.get("STAR_FORGE_RC_TMPDIR")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from starforge import contracts, doctor, lifecycle, migration, routing
import star_forge


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), path
    return payload


def isolated_temp_directory(prefix: str) -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix=prefix, dir=RC_TMP_PARENT)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


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


def test_local_installer_uses_canonical_checkout_without_outside_write() -> None:
    with isolated_temp_directory("star-forge-install-symlink-") as tmp:
        fixture = Path(tmp).resolve()
        marketplace = fixture / "marketplace"
        outside = fixture / "outside"
        fake_bin = fixture / "bin"
        command_log = fixture / "codex-commands.log"
        marketplace.mkdir()
        (outside / "star-forge").mkdir(parents=True)
        fake_bin.mkdir()
        sentinel = outside / "star-forge" / "outside-sentinel"
        sentinel.write_text("preserve\n", encoding="utf-8")
        (marketplace / "plugins").symlink_to(outside, target_is_directory=True)

        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CODEX_LOG"\n',
            encoding="utf-8",
        )
        fake_codex.chmod(0o700)
        result = run(
            ["sh", str(INSTALL_CODEX)],
            cwd=ROOT,
            env={
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "CODEX_LOG": str(command_log),
                "STAR_FORGE_MARKETPLACE_ROOT": str(marketplace),
            },
        )

        assert result.returncode == 0, (result.stdout, result.stderr)
        assert command_log.read_text(encoding="utf-8").splitlines() == [
            f"plugin marketplace add {ROOT}",
            "plugin add star-forge@star-forge",
        ]
        assert sentinel.read_text(encoding="utf-8") == "preserve\n"
        assert list((outside / "star-forge").iterdir()) == [sentinel]


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


def test_isolated_clean_install_and_mobbin_duplicate_doctor_are_read_only() -> None:
    with isolated_temp_directory("star-forge-rc-install-") as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        version = str(load_json(MANIFEST_PATH)["version"])
        active = (
            codex_home
            / "plugins"
            / "cache"
            / "star-forge"
            / "star-forge"
            / version
        )
        write_json(
            active / ".codex-plugin" / "plugin.json",
            {"name": "star-forge", "version": version},
        )
        active_runtime = active / "scripts" / "star_forge.py"
        active_runtime.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts" / "star_forge.py", active_runtime)
        active_hooks = active / "hooks" / "hooks.json"
        active_hooks.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "hooks" / "hooks.json", active_hooks)
        shutil.copyfile(ROOT / ".app.json", active / ".app.json")
        (codex_home / "config.toml").write_text(
            """
[marketplaces.star-forge]
source_type = "git"
source = "https://github.com/khaledayeva/star-forge"
""".lstrip(),
            encoding="utf-8",
        )

        app_manifest = load_json(ROOT / ".app.json")
        assert app_manifest == {"apps": {"mobbin": {"id": MOBBIN_APP_ID}}}
        serialized_app = json.dumps(app_manifest).casefold()
        assert all(
            credential not in serialized_app
            for credential in ("access_token", "api_key", "password", "secret")
        )
        clean = doctor.diagnose_installation(
            codex_home=codex_home,
            source_root=ROOT,
            runtime_version=version.split("+", 1)[0],
            active_plugin_root=active,
        )
        assert clean["verdict"] == "PASS", clean

        write_json(
            codex_home
            / "plugins"
            / "cache"
            / "legacy-design"
            / "design-tools"
            / "1.0.0"
            / ".app.json",
            {"name": "mobbin", "oauth": {"provider": "mobbin"}},
        )
        before = snapshot_tree(codex_home)
        duplicate = doctor.diagnose_installation(
            codex_home=codex_home,
            source_root=ROOT,
            runtime_version=version.split("+", 1)[0],
            active_plugin_root=active,
        )
        assert snapshot_tree(codex_home) == before
        matching = [
            item
            for item in duplicate["findings"]
            if item["rule"] == doctor.RULE_DUPLICATE_MOBBIN
        ]
        assert len(matching) == 1
        assert len(matching[0]["paths"]) == 2


def test_legacy_v03_migration_is_separate_and_non_destructive() -> None:
    fixture_root = ROOT / "fixtures" / "legacy-v03" / "completed-amended"
    with isolated_temp_directory("star-forge-rc-legacy-") as tmp:
        project = Path(tmp) / "legacy"
        shutil.copytree(fixture_root, project)
        if not (project / ".starforge").is_dir():
            (project / "dot-starforge").rename(project / ".starforge")
        before = snapshot_tree(project)

        inspection = migration.inspect_legacy_project(project)
        assert inspection["schema"] == migration.LEGACY_PROJECT_INSPECTION_SCHEMA
        assert inspection["problems"] == []
        assert snapshot_tree(project) == before

        draft = project / "Plan.v2.draft.md"
        result = contracts.write_plan_v2_migration(project / "Plan.md", draft)
        assert result["source_preserved"] is True
        assert result["review_required"] is True
        after = snapshot_tree(project)
        assert {path: after[path] for path in before} == before
        assert "REVIEW_REQUIRED" in draft.read_text(encoding="utf-8")


def test_private_foundation_fixture_is_source_bound_and_fails_closed() -> None:
    contract_path = ROOT / "fixtures" / "foundation" / "private-new-contract.json"
    evidence_path = ROOT / "fixtures" / "foundation" / "private-new-evidence.json"
    before = (contract_path.read_bytes(), evidence_path.read_bytes())
    contract = load_json(contract_path)
    fixture_evidence = load_json(evidence_path)
    source_hash = str(fixture_evidence["source_hash"])

    passing = lifecycle.evaluate_foundation(
        contract,
        fixture_evidence,
        current_source_hash=source_hash,
    )
    assert passing.status == "PASS", passing.blockers

    stale = lifecycle.evaluate_foundation(
        contract,
        fixture_evidence,
        current_source_hash="0" * 64,
    )
    assert stale.status == "BLOCKED"
    assert any("stale" in blocker for blocker in stale.blockers)

    unsafe_contract = copy.deepcopy(contract)
    unsafe_contract["repository"]["write_authorized"] = False
    unsafe = lifecycle.evaluate_foundation(
        unsafe_contract,
        fixture_evidence,
        current_source_hash=source_hash,
    )
    assert unsafe.status == "BLOCKED"
    assert unsafe.ready_for_feature_work is False
    assert (contract_path.read_bytes(), evidence_path.read_bytes()) == before


def test_platform_release_routes_and_plan_proofs_are_complete() -> None:
    fixtures = ROOT / "fixtures" / "v04-projects"
    required_proofs = {
        "web": {"browser", "preview", "github", "delivery"},
        "ios": {"native-ios", "delivery"},
        "macos": {"native-macos", "package", "delivery"},
        "expo": {"native-ios", "delivery"},
        "cli": {"unit", "delivery"},
    }
    for name, proofs in required_proofs.items():
        project = fixtures / name
        scenario = load_json(project / "scenario.json")
        tasks = contracts.parse_plan_tasks_text(
            (project / "Plan.md").read_text(encoding="utf-8")
        )
        problems = contracts.validate_plan_v2_contract(
            (project / "Blueprint.md").read_text(encoding="utf-8"),
            tasks,
        )
        assert problems == [], (name, problems)
        assert proofs.issubset(set(scenario["proofs"])), name
        assert (project / scenario["source_file"]).is_file(), name

        result = routing.resolve_routes(
            project_class=scenario["routing_project_class"],
            proof_kinds=scenario["proofs"],
            delivery_target=[
                scenario["delivery_target"],
                scenario["platform_target"],
            ],
            available_capabilities=scenario["available_capabilities"],
        )
        assert result.blocked is False, name
        selected = {
            decision.need: str(decision.selected["id"])
            for decision in result.decisions
        }
        for need, provider in scenario["expected_routes"].items():
            assert selected.get(need) == provider, (name, selected)


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
    with isolated_temp_directory("star-forge-rc-agents-") as tmp:
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
    with isolated_temp_directory("star-forge-rc-version-") as tmp:
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
    with isolated_temp_directory("star-forge-rc-cachebuster-") as tmp:
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
    with isolated_temp_directory("star-forge-rc-untracked-") as tmp:
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


def test_release_gate_preserves_special_git_paths() -> None:
    names = (
        "café.txt",
        "line\nbreak.txt",
        "tab\tname.txt",
        'quote"name.txt',
        "back\\slash.txt",
    )
    with isolated_temp_directory("star-forge-rc-special-paths-") as tmp:
        fixture = Path(tmp).resolve()
        base = init_release_fixture(fixture)
        for name in names:
            (fixture / name).write_text("new\n", encoding="utf-8")
        result = run(
            ["sh", "scripts/release-check.sh", "--version-only"],
            cwd=fixture,
            env={"STAR_FORGE_RELEASE_BASE": base},
        )
        assert result.returncode == 1
        assert "without a new plugin version or cachebuster" in result.stderr
        for name in names:
            assert name in result.stderr, (name, result.stderr)


def test_release_gate_fails_without_a_trustworthy_base() -> None:
    with isolated_temp_directory("star-forge-rc-no-base-") as tmp:
        fixture = Path(tmp).resolve()
        init_release_fixture(fixture)
        result = run(
            ["sh", "scripts/release-check.sh", "--version-only"],
            cwd=fixture,
        )
        assert result.returncode == 1
        assert "no trustworthy comparison revision" in result.stderr


def test_release_gate_rejects_a_base_at_current_head() -> None:
    with isolated_temp_directory("star-forge-rc-head-base-") as tmp:
        fixture = Path(tmp).resolve()
        init_release_fixture(fixture)
        result = run(
            ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
            cwd=fixture,
        )
        assert result.returncode == 0, result.stderr
        result = run(
            ["sh", "scripts/release-check.sh", "--version-only"],
            cwd=fixture,
        )
        assert result.returncode == 1
        assert "comparison revision resolves to current HEAD" in result.stderr


def test_release_gate_allows_only_an_explicit_first_commit_release() -> None:
    with isolated_temp_directory("star-forge-rc-initial-") as tmp:
        fixture = Path(tmp).resolve()
        init_release_fixture(fixture)
        result = run(
            ["sh", "scripts/release-check.sh", "--version-only"],
            cwd=fixture,
            env={"STAR_FORGE_INITIAL_RELEASE": "1"},
        )
        assert result.returncode == 0, result.stderr
        (fixture / "second.txt").write_text("second\n", encoding="utf-8")
        for command in (
            ["git", "add", "second.txt"],
            ["git", "commit", "-qm", "second"],
        ):
            result = run(command, cwd=fixture)
            assert result.returncode == 0, result.stderr
        result = run(
            ["sh", "scripts/release-check.sh", "--version-only"],
            cwd=fixture,
            env={"STAR_FORGE_INITIAL_RELEASE": "1"},
        )
        assert result.returncode == 1
        assert "valid only for a repository's first commit" in result.stderr


def test_repository_metadata_passes_version_only_release_gate() -> None:
    result = run(
        ["sh", "scripts/release-check.sh", "--version-only"],
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_release_gate_discovers_every_test_and_runtime_module() -> None:
    script = RELEASE_CHECK.parent.joinpath("check.sh").read_text(encoding="utf-8")
    assert "for suite in tests/test_*.py" in script
    assert (
        "scripts/star_forge.py scripts/starforge/*.py "
        "scripts/live_collectors/*.py"
    ) in script
    assert "TEST_SUITES=" not in script
    assert "PYTHON_FILES=" not in script


def test_runtime_modules_have_explicit_acyclic_dependencies() -> None:
    runtime_paths = sorted((SCRIPTS / "starforge").glob("runtime_*.py"))
    graph: dict[str, set[str]] = {}
    for path in runtime_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        dependencies: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            assert all(alias.name != "*" for alias in node.names), path
            module = str(node.module or "")
            if node.level == 1 and module.startswith("runtime_"):
                dependencies.add(module)
        graph[path.stem] = dependencies

    def visit(name: str, visiting: set[str], visited: set[str]) -> None:
        assert name not in visiting, f"runtime dependency cycle at {name}"
        if name in visited:
            return
        visiting.add(name)
        for dependency in graph[name]:
            visit(dependency, visiting, visited)
        visiting.remove(name)
        visited.add(name)

    visited: set[str] = set()
    for module_name in graph:
        visit(module_name, set(), visited)

    facade = (SCRIPTS / "star_forge.py").read_text(encoding="utf-8")
    assert "vars(_module).update" not in facade
    assert "globals().update(_RUNTIME_NAMESPACE)" not in facade
    assert "_RUNTIME_NAMESPACE" not in facade
    assert "__all__" in facade

    for module_name in graph:
        process = run(
            [sys.executable, "-c", f"import starforge.{module_name}"],
            cwd=ROOT,
            env={"PYTHONPATH": str(SCRIPTS)},
        )
        assert process.returncode == 0, process.stderr


def test_runtime_compatibility_exports_are_keyed_and_resolvable() -> None:
    facade = (SCRIPTS / "star_forge.py").read_text(encoding="utf-8")
    support = (SCRIPTS / "starforge" / "runtime_support.py").read_text(
        encoding="utf-8"
    )
    assert "_COMPATIBILITY_EXPORT_GROUPS" in facade
    assert "_COMPATIBILITY_EXPORTS" in facade
    assert "_POLICY_EXPORT_GROUPS" in support
    assert "globals().update" not in facade
    assert "globals().update" not in support

    support_tree = ast.parse(support)
    positional_targets = [
        target
        for node in support_tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, (ast.Tuple, ast.List))
    ]
    assert positional_targets == []

    assert len(star_forge.__all__) == len(set(star_forge.__all__))
    for name in star_forge.__all__:
        assert getattr(star_forge, name) is not None, name


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
