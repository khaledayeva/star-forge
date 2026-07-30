#!/usr/bin/env python3
"""Offline end-to-end tests for the Star Forge v0.4 project matrix.

Run with: python3 tests/test_v04_e2e.py
"""

from __future__ import annotations

import copy
import hashlib
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
SCRIPT = ROOT / "scripts" / "star_forge.py"
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "fixtures" / "v04-projects"
DOGFOOD_FIXTURE = ROOT / "fixtures" / "v04-dogfood"
FOUNDATION_FIXTURES = ROOT / "fixtures" / "foundation"
LEGACY_FIXTURES = ROOT / "fixtures" / "legacy-v03"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import star_forge
from live_collectors import browser_playwright
from starforge import changes, contracts, evidence, lifecycle, review_policy, routing


SCENARIOS = ("web", "ios", "macos", "expo", "cli", "fast-mvp")
EXPECTED_PHASES = (
    "foundation",
    "build",
    "review",
    "deliver",
    "done",
)


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    expected: int = 0,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=cwd or ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTHONPYCACHEPREFIX": str(Path(tempfile.gettempdir()) / "star-forge-v04-e2e-pycache")},
    )
    assert result.returncode == expected, (
        f"command returned {result.returncode}, expected {expected}: {argv}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def cli(project: Path, *args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
    return run([sys.executable, str(SCRIPT), *args, "--project", str(project)], expected=expected)


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def copy_project(root: Path, name: str) -> tuple[Path, dict[str, Any]]:
    project = root / name
    shutil.copytree(FIXTURES / name, project)
    return project, load_json(project / "scenario.json")


def commit_all(project: Path, message: str) -> str:
    source_paths = [
        str(path.relative_to(project))
        for path in star_forge.snapshot_file_candidates(project)
    ]
    assert source_paths
    run(["git", "add", "--", *source_paths], cwd=project)
    run(
        [
            "git",
            "-c",
            "user.name=Star Forge E2E",
            "-c",
            "user.email=star-forge-e2e@example.invalid",
            "commit",
            "-m",
            message,
        ],
        cwd=project,
    )
    return run(["git", "rev-parse", "HEAD"], cwd=project).stdout.strip()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def replace_value(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return new if value == old else value
    if isinstance(value, list):
        return [replace_value(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: replace_value(item, old, new) for key, item in value.items()}
    return value


def install_foundation_evidence(project: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    stem = "private-new" if scenario["github_requested"] else "local-only"
    contract = load_json(FOUNDATION_FIXTURES / f"{stem}-contract.json")
    fixture_evidence = load_json(FOUNDATION_FIXTURES / f"{stem}-evidence.json")
    source_hash = star_forge.source_hash(project)
    old_hash = str(fixture_evidence["source_hash"])
    replay = replace_value(fixture_evidence, old_hash, source_hash)
    replay["contract_sha256"] = lifecycle.foundation_contract_sha256(contract)
    write_json(project / lifecycle.FOUNDATION_CONTRACT_PATH, contract)
    write_json(project / lifecycle.FOUNDATION_EVIDENCE_PATH, replay)
    gate = lifecycle.evaluate_foundation(contract, replay, current_source_hash=source_hash)
    assert gate.status == "PASS", gate.blockers
    return gate.to_dict()


def install_dogfood_foundation_evidence(
    project: Path,
    blueprint_contract: dict[str, Any],
) -> dict[str, Any]:
    assert blueprint_contract["delivery_target"] == "source-only"
    contract = lifecycle.make_foundation_contract(
        github_requested=False,
        environment_example_required=False,
        dependency_audit_required=False,
        security_plan_required=False,
    )
    source_hash = star_forge.source_hash(project)
    commit = star_forge.git_head(project)
    branch = run(["git", "branch", "--show-current"], cwd=project).stdout.strip()
    parent_fields = run(
        ["git", "rev-list", "--parents", "-n", "1", "HEAD"],
        cwd=project,
    ).stdout.split()
    assert len(parent_fields) == 1
    source = project / "scripts" / "starforge" / "dogfood_status.py"
    ci = project / ".github" / "workflows" / "ci.yml"
    scanned = b"\n".join(
        path.read_bytes() for path in star_forge.snapshot_file_candidates(project)
    )
    forbidden = (
        b"github" + b"_pat_",
        b"g" + b"hp_",
        b"BEGIN PRIVATE" + b" KEY",
    )
    assert not any(marker in scanned for marker in forbidden)
    details = {
        "source_scaffold": {
            "artifacts": [
                {
                    "path": str(source.relative_to(project)),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "committed": True,
                }
            ]
        },
        "local_git": {
            "is_repository": True,
            "method": "automatic-local-init",
        },
        "default_branch": {
            "name": branch,
            "exists": True,
            "head_commit": commit,
        },
        "initial_commit": {
            "sha": commit,
            "parent_count": 0,
            "current_head": commit,
            "tree_source_hash": source_hash,
        },
        "ci": {
            "path": str(ci.relative_to(project)),
            "sha256": hashlib.sha256(ci.read_bytes()).hexdigest(),
            "committed": True,
        },
        "secret_scan": {
            "tool": "deterministic-fixture-byte-scan",
            "verdict": "PASS",
            "findings": 0,
        },
    }
    checks: dict[str, Any] = {}
    for name in lifecycle.FOUNDATION_REQUIREMENTS:
        requirement = contract["requirements"][name]
        if requirement["state"] == "not-applicable":
            checks[name] = {"state": "not-applicable"}
        else:
            checks[name] = {
                "state": "satisfied",
                "source_hash": source_hash,
                "detail": details[name],
            }
    replay = {
        "schema": lifecycle.FOUNDATION_EVIDENCE_SCHEMA,
        "captured_at": "2026-07-26T12:00:00Z",
        "source_hash": source_hash,
        "contract_sha256": lifecycle.foundation_contract_sha256(contract),
        "checks": checks,
    }
    write_json(project / lifecycle.FOUNDATION_CONTRACT_PATH, contract)
    write_json(project / lifecycle.FOUNDATION_EVIDENCE_PATH, replay)
    gate = lifecycle.evaluate_foundation(
        contract,
        replay,
        current_source_hash=source_hash,
    )
    assert gate.status == "PASS", gate.blockers
    return gate.to_dict()


def delivery_contract_for(scenario: dict[str, Any]) -> dict[str, Any]:
    target = str(scenario["delivery_target"])
    platform = str(scenario["platform_target"])
    return lifecycle.make_delivery_contract(
        delivery_target=target,
        project_class=str(scenario["project_class"]),
        platform_target=platform,
        environment="fixture",
        destination=f"{scenario['name']}-delivery",
    )


def install_delivery_evidence(project: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    contract = delivery_contract_for(scenario)
    source_hash = star_forge.source_hash(project)
    commit = star_forge.git_head(project)
    target = str(contract["target"]["kind"])
    provider = str(contract["route"]["provider"])
    identity_kind = str(contract["result"]["identity_kind"])
    checks: dict[str, Any] = {}
    details = {
        "source_binding": {"source_hash": source_hash},
        "repository_commit": {"sha": commit, "tree_source_hash": source_hash},
        "delivery_identity": {
            "kind": identity_kind,
            "id": f"{scenario['name']}-delivery-1",
            "repository_commit": commit,
            "source_hash": source_hash,
        },
        "live_url": {
            "url": f"https://{scenario['name']}.example.invalid/preview",
            "provider": provider,
        },
        "smoke_result": {
            "verdict": "PASS",
            "checked_at": "2026-07-26T12:00:00Z",
            "scenario": "fixture project tests pass from delivered source",
            "repository_commit": commit,
            "source_hash": source_hash,
        },
    }
    if identity_kind == "deployment":
        details["delivery_identity"]["provider"] = provider
    if identity_kind in {"package", "platform-release"}:
        details["delivery_identity"]["artifact_sha256"] = hashlib.sha256(
            f"{scenario['name']}-artifact".encode("utf-8")
        ).hexdigest()
    for name in lifecycle.DELIVERY_REQUIREMENTS:
        state = contract["requirements"][name]["state"]
        if state == "not-applicable":
            checks[name] = {"state": "not-applicable"}
        else:
            checks[name] = {
                "state": "satisfied",
                "source_hash": source_hash,
                "detail": details[name],
            }
    replay = {
        "schema": lifecycle.DELIVERY_EVIDENCE_SCHEMA,
        "captured_at": "2026-07-26T12:00:00Z",
        "source_hash": source_hash,
        "contract_sha256": lifecycle.delivery_contract_sha256(contract),
        "repository_commit": commit,
        "target": target,
        "provider": provider,
        "checks": checks,
    }
    write_json(project / lifecycle.DELIVERY_CONTRACT_PATH, contract)
    write_json(project / lifecycle.DELIVERY_EVIDENCE_PATH, replay)
    gate = lifecycle.evaluate_delivery(contract, replay, current_source_hash=source_hash)
    assert gate.status == "PASS", gate.blockers
    return gate.to_dict()


def write_evidence_v2(project: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    source = project / str(scenario["source_file"])
    source_hash = star_forge.source_hash(project)
    proof_kinds = list(scenario["proofs"])
    kind = next(
        (
            item
            for item in ("browser", "native-ios", "native-macos", "security", "delivery")
            if item in proof_kinds
        ),
        "unit",
    )
    envelope = {
        "schema": evidence.EVIDENCE_SCHEMA,
        "kind": kind,
        "task": "SF-1",
        "capability": f"{scenario['name']}-verification",
        "provider": "offline-fixture-replay",
        "provenance": {
            "fixture": scenario["name"],
            "command": scenario["verify_command"],
        },
        "source_hash": source_hash,
        "runtime_asset_hash": source_hash,
        "started_at": "2026-07-26T12:00:00Z",
        "finished_at": "2026-07-26T12:00:01Z",
        "artifacts": [
            {
                "path": str(scenario["source_file"]),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "bytes": source.stat().st_size,
                "kind": "source",
            }
        ],
        "verdict": "PASS",
        "blockers": [],
    }
    path = project / ".starforge" / "evidence" / "SF-1" / f"{scenario['name']}.json"
    written = evidence.write_envelope(
        path,
        envelope,
        project_root=project,
        verify_artifacts=True,
    )
    return evidence.read_envelope(
        path,
        project_root=project,
        verify_artifacts=True,
    ) | {"written": written}


def make_png(path: Path, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + ihdr
        + b"\x00\x00\x00\x00"
    )


def record_browser_proof(project: Path) -> None:
    root = project / ".starforge" / "live" / "SF-1" / "browser"
    desktop = root / "desktop.png"
    mobile = root / "mobile.png"
    interaction = root / "interaction.json"
    console = root / "console.json"
    make_png(desktop, 1280, 800)
    make_png(mobile, 390, 844)
    url = "http://127.0.0.1:4173/"
    parsed, problems = browser_playwright.validate_url(url)
    assert not problems
    origin = browser_playwright.normalize_origin(parsed)
    request = browser_playwright.browser_url_safety_evidence(
        url, allowed_local_origins=[origin]
    )
    request.update({"method": "GET", "resource_type": "document", "navigation": True})
    write_json(
        interaction,
        {
            "ready": [{"passed": True}],
            "actions": [{"type": "click", "target": "Refresh", "passed": True}],
            "assertions": [{"name": "status visible", "passed": True}],
            "request_safety": {
                "schema": "star-forge.browser-request-safety.v1",
                "service_workers": browser_playwright.SERVICE_WORKERS_MODE,
                "connection_control": browser_playwright.BROWSER_NETWORK_CONTROL_MODE,
                "websocket_routing": browser_playwright.WEBSOCKET_ROUTING_MODE,
                "allowed_local_origins": [origin],
                "requests": [request],
                "websockets": [],
                "final_urls": [request],
                "blocked_count": 0,
                "websocket_blocked_count": 0,
                "webrtc": {
                    "mode": browser_playwright.WEBRTC_CONTROL_MODE,
                    "init_script": True,
                },
            },
        },
    )
    write_json(console, {"events": []})
    cli(
        project,
        "server-lease",
        "--action",
        "claim",
        "--port",
        "4173",
        "--base-url",
        url,
        "--command",
        "python3 -m http.server 4173",
        "--pid",
        str(os.getpid()),
    )
    lease = project / star_forge.SERVER_LEASE
    current = star_forge.source_hash(project)
    manifest = star_forge.live_common.write_live_manifest(
        project,
        task="SF-1",
        collector="browser",
        command_argv=["offline-browser-fixture"],
        tool_versions={"fixture": "1"},
        artifacts={
            "desktop": desktop,
            "mobile": mobile,
            "interaction": interaction,
            "console": console,
        },
        summary={"url": url},
        source_hash_before=current,
        source_hash_after=current,
        runtime_asset_hash=star_forge.live_common.compute_runtime_asset_hash(project),
    )
    browser_playwright.write_evidence_envelope(project, manifest)
    cli(
        project,
        "browser-run",
        "--task",
        "SF-1",
        "--url",
        url,
        "--scenario",
        "refresh deployment status",
        "--viewport",
        f"desktop=1280x800:{desktop}",
        "--viewport",
        f"mobile=390x844:{mobile}",
        "--interaction-evidence",
        str(interaction),
        "--console-evidence",
        str(console),
        "--live-manifest",
        str(manifest),
        "--server-lease",
        str(lease),
        "--require-server-lease",
        "--strict",
    )


def write_clean_review(project: Path) -> list[str]:
    roles = star_forge.required_review_roles(project)
    scope = star_forge.scope_hash(project) or "noscope"
    review_root = star_forge.reviews_scope_dir(project, scope)
    review_root.mkdir(parents=True, exist_ok=True)
    source_hash = star_forge.source_hash(project)
    for role in roles:
        write_json(
            review_root / f"{role}.findings.json",
            {
                "role": role,
                "agent_id": f"offline-{role}",
                "source_hash": source_hash,
                "findings": [],
            },
        )
    result = cli(project, "review", "--strict")
    payload = json.loads(result.stdout)
    assert payload["fix_queue"] == []
    return roles


def state(project: Path) -> dict[str, Any]:
    return load_json(project / ".starforge" / "state.json")


def run_state(project: Path) -> dict[str, Any]:
    cli(project, "run", "--no-hooks", "--no-agents")
    return state(project)


def exercise_project(project: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    profile_args = ["--profile", str(scenario["profile"])]
    deferred_contracts: dict[Path, bytes] = {}
    if scenario["profile"] == "fast-mvp":
        for name in ("Blueprint.md", "Plan.md"):
            path = project / name
            deferred_contracts[path] = path.read_bytes()
            path.unlink()
    cli(
        project,
        "run",
        "--adopt-root",
        "--no-hooks",
        "--no-agents",
        *profile_args,
    )
    if scenario["profile"] == "fast-mvp":
        run(["git", "add", "--", star_forge.SOURCE_PROFILE_FILE], cwd=project)
        run(
            [
                "git",
                "-c",
                "user.name=Star Forge E2E",
                "-c",
                "user.email=star-forge-e2e@example.invalid",
                "commit",
                "-m",
                "Select Fast MVP before project gates",
            ],
            cwd=project,
        )
    for path, content in deferred_contracts.items():
        path.write_bytes(content)
    commit_all(project, "Initialize fixture through one Forge invocation")
    cli(project, "approve-blueprint")
    commit_all(project, "Lock approved fixture contract")
    validate = cli(project, "validate-plan", "--strict")
    assert json.loads(validate.stdout)["plan_version"] == "v2"

    phase_sequence = [run_state(project)["phase"]]
    assert phase_sequence == ["foundation"]
    foundation = install_foundation_evidence(project, scenario)
    phase_sequence.append(run_state(project)["phase"])
    assert phase_sequence[-1] == "build"

    route_result = routing.resolve_routes(
        project_class=str(scenario["routing_project_class"]),
        blueprint_flags={
            "ui": "User-facing UI" in scenario["risk_flags"],
            "security_sensitive": bool(
                set(scenario["risk_flags"])
                & {
                    "Authentication or authorization",
                    "User, sensitive, or regulated data",
                    "Network access or external input",
                }
            ),
        },
        proof_kinds=scenario["proofs"],
        delivery_target=[
            str(scenario["delivery_target"]),
            str(scenario["platform_target"]),
        ],
        delivery_provider=scenario.get("delivery_provider"),
        available_capabilities=scenario["available_capabilities"],
    )
    selected = {
        decision.need: str((decision.selected or {}).get("id"))
        for decision in route_result.decisions
    }
    for need, provider in scenario["expected_routes"].items():
        assert selected[need] == provider, (scenario["name"], need, selected)

    verify = cli(
        project,
        "verify",
        "--task",
        "SF-1",
        "--command",
        str(scenario["verify_command"]),
        "--strict",
    )
    assert json.loads(verify.stdout)["verdict"] == "PASS"
    envelope = write_evidence_v2(project, scenario)
    if scenario["name"] == "web":
        record_browser_proof(project)
    complete = cli(
        project,
        "complete-task",
        "--task",
        "SF-1",
        "--changed-file",
        str(scenario["source_file"]),
    )
    assert json.loads(complete.stdout)["verdict"] == "COMPLETE"
    cli(
        project,
        "verify",
        "--task",
        "SF-1",
        "--command",
        str(scenario["verify_command"]),
        "--strict",
    )
    if scenario["name"] == "web":
        record_browser_proof(project)
    commit_all(project, "Complete verified fixture task")
    phase_sequence.append(run_state(project)["phase"])
    assert phase_sequence[-1] == "review"

    roles = write_clean_review(project)
    phase_sequence.append(run_state(project)["phase"])
    assert phase_sequence[-1] == "deliver"

    delivery = install_delivery_evidence(project, scenario)
    phase_sequence.append(run_state(project)["phase"])
    assert phase_sequence[-1] == "done"
    done = cli(project, "done", "--strict")
    done_payload = json.loads(done.stdout)
    assert done_payload["is_complete"] is True
    final_state = run_state(project)
    assert final_state["phase"] == "done"

    return {
        "foundation": foundation,
        "delivery": delivery,
        "envelope": envelope,
        "routes": selected,
        "roles": roles,
        "phases": phase_sequence,
        "done": done_payload,
    }


def test_fixture_matrix_drives_real_cli_from_one_initial_invocation_to_done() -> None:
    with tempfile.TemporaryDirectory(prefix="star-forge-v04-e2e-") as tmp:
        root = Path(tmp)
        results: dict[str, dict[str, Any]] = {}
        for name in SCENARIOS:
            project, scenario = copy_project(root, name)
            results[name] = exercise_project(project, scenario)

        for name, result in results.items():
            assert result["phases"] == list(EXPECTED_PHASES), (name, result["phases"])
            assert result["foundation"]["status"] == "PASS"
            assert result["delivery"]["status"] == "PASS"
            assert result["envelope"]["schema"] == evidence.EVIDENCE_SCHEMA
            assert result["done"]["verdict"].startswith("COMPLETE")


def test_platform_profiles_and_fast_mvp_risk_floor_are_not_labels_only() -> None:
    selected_by_name: dict[str, dict[str, str]] = {}
    for name in ("ios", "macos", "expo"):
        scenario = load_json(FIXTURES / name / "scenario.json")
        result = routing.resolve_routes(
            project_class=scenario["routing_project_class"],
            proof_kinds=scenario["proofs"],
            delivery_target=[
                scenario["delivery_target"],
                scenario["platform_target"],
            ],
            available_capabilities=scenario["available_capabilities"],
        )
        selected_by_name[name] = {
            decision.need: str((decision.selected or {}).get("id"))
            for decision in result.decisions
        }
        assert result.blocked is False
    assert selected_by_name["ios"]["ios-verification"] == "xcodebuildmcp"
    assert selected_by_name["macos"]["macos-implementation"] == "build-macos-apps"
    assert selected_by_name["expo"]["expo"] == "expo-plugin"
    fallback = routing.resolve_route(
        "expo", available_capabilities=["repository-native expo cli"]
    )
    assert fallback.status == "degraded"
    assert fallback.selected["id"] == "expo-cli"

    fast = load_json(FIXTURES / "fast-mvp" / "scenario.json")
    fast_blueprint = (FIXTURES / "fast-mvp" / "Blueprint.md").read_text(
        encoding="utf-8"
    )
    fast_tasks = star_forge.parse_tasks(FIXTURES / "fast-mvp" / "Plan.md")
    policy = review_policy.select_review_policy(
        fast_blueprint,
        fast_tasks,
        profile="fast-mvp",
        source_hash="a" * 64,
    )
    assert "security" in policy.roles
    assert len(policy.roles) <= review_policy.MAX_REVIEW_AGENTS
    assert "security" in fast["proofs"]
    assert delivery_contract_for(fast)["target"]["kind"] == "private-repo"


def test_replayed_foundation_and_delivery_evidence_fail_closed_on_tamper() -> None:
    with tempfile.TemporaryDirectory(prefix="star-forge-v04-gates-") as tmp:
        project, scenario = copy_project(Path(tmp), "web")
        cli(
            project,
            "run",
            "--adopt-root",
            "--no-hooks",
            "--no-agents",
            "--profile",
            "standard",
        )
        commit_all(project, "Initialize gate fixture")
        cli(project, "approve-blueprint")
        commit_all(project, "Lock gate fixture")
        install_foundation_evidence(project, scenario)
        foundation_contract = load_json(project / lifecycle.FOUNDATION_CONTRACT_PATH)
        foundation_evidence = load_json(project / lifecycle.FOUNDATION_EVIDENCE_PATH)
        broken_foundation = copy.deepcopy(foundation_evidence)
        broken_foundation["checks"]["github_repository"]["detail"]["visibility"] = "public"
        gate = lifecycle.evaluate_foundation(
            foundation_contract,
            broken_foundation,
            current_source_hash=foundation_evidence["source_hash"],
        )
        assert gate.status == "BLOCKED"
        assert any("visibility" in blocker for blocker in gate.blockers)

        commit = star_forge.git_head(project)
        assert len(commit) == 40
        delivery_contract = delivery_contract_for(scenario)
        source_hash = star_forge.source_hash(project)
        install_delivery_evidence(project, scenario)
        delivery_evidence = load_json(project / lifecycle.DELIVERY_EVIDENCE_PATH)
        stale_delivery = copy.deepcopy(delivery_evidence)
        stale_delivery["source_hash"] = "0" * 64
        delivery_gate = lifecycle.evaluate_delivery(
            delivery_contract,
            stale_delivery,
            current_source_hash=source_hash,
        )
        assert delivery_gate.status == "BLOCKED"
        assert "delivery evidence source hash is stale" in delivery_gate.blockers


def test_post_completion_change_packet_repeats_affected_gates_and_completes() -> None:
    with tempfile.TemporaryDirectory(prefix="star-forge-v04-change-") as tmp:
        project, scenario = copy_project(Path(tmp), "amendment")
        initial = exercise_project(project, scenario)
        root_plan_before = (project / "Plan.md").read_bytes()
        proof_before = load_json(project / ".starforge" / "final" / "proof.json")

        source = project / "src" / "calculator.py"
        source.write_text(
            source.read_text(encoding="utf-8")
            + "\n\ndef subtract(left: int, right: int) -> int:\n    return left - right\n",
            encoding="utf-8",
        )
        amended_test = project / "tests" / "test_fixture.py"
        amended_test.write_text(
            amended_test.read_text(encoding="utf-8").replace(
                "from calculator import add",
                "from calculator import add, subtract",
            ).replace(
                "self.assertEqual(add(2, 3), 5)",
                "self.assertEqual(add(2, 3), 5)\n        self.assertEqual(subtract(5, 3), 2)",
            ),
            encoding="utf-8",
        )
        drift_state = run_state(project)
        assert drift_state["phase"] == "amend"
        packet = drift_state["change_packet"]
        assert packet["approval_state"] == "draft"
        assert packet["original_completed_source_hash"] == proof_before["source_hash"]
        assert set(packet["scope_delta"]) == {
            "src/calculator.py",
            "tests/test_fixture.py",
        }
        assert (project / "Plan.md").read_bytes() == root_plan_before
        assert b"AMEND-" not in root_plan_before

        change_id = str(packet["change_id"])
        cli(project, "approve-change", "--change", change_id)
        approved_state = run_state(project)
        assert approved_state["phase"] == "amend"
        assert approved_state["change_packet"]["approval_state"] == "approved"
        change_plan = project / str(packet["path"]) / str(packet["plan_path"])
        tasks = star_forge.parse_tasks(change_plan)
        assert len(tasks) == 1
        task = tasks[0]
        assert task["mode"] == "delegate"
        assert "unit" in task["proof"]
        assert task["verify"] == scenario["verify_command"]

        cli(
            project,
            "verify",
            "--task",
            task["id"],
            "--command",
            task["verify"],
            "--strict",
        )
        cli(
            project,
            "complete-task",
            "--task",
            task["id"],
            "--changed-file",
            "src/calculator.py",
            "--changed-file",
            "tests/test_fixture.py",
        )
        cli(
            project,
            "verify",
            "--task",
            task["id"],
            "--command",
            task["verify"],
            "--strict",
        )
        cli(
            project,
            "verify",
            "--task",
            "SF-1",
            "--command",
            str(scenario["verify_command"]),
            "--strict",
        )
        commit_all(project, "Complete scoped calculator change")
        assert run_state(project)["phase"] == "review"
        write_clean_review(project)
        install_delivery_evidence(project, scenario)
        assert run_state(project)["phase"] == "done"
        completed = cli(project, "done", "--strict")
        payload = json.loads(completed.stdout)
        assert payload["is_complete"] is True
        final_state = run_state(project)
        assert final_state["drift"]["detected"] is False
        final_proof = load_json(project / ".starforge" / "final" / "proof.json")
        assert final_proof["source_hash"] == star_forge.source_hash(project)
        assert changes.lookup_change_history(project, change_id)["kind"] == "change-packet"
        assert initial["done"]["is_complete"] is True


def test_modern_plan_amend_row_cannot_replace_an_approved_change_packet() -> None:
    with tempfile.TemporaryDirectory(prefix="star-forge-v04-amend-exploit-") as tmp:
        project, scenario = copy_project(Path(tmp), "amendment")
        exercise_project(project, scenario)
        source = project / "src" / "calculator.py"
        source.write_text(
            source.read_text(encoding="utf-8")
            + "\n\ndef subtract(left: int, right: int) -> int:\n    return left - right\n",
            encoding="utf-8",
        )
        plan = project / "Plan.md"
        plan.write_text(
            plan.read_text(encoding="utf-8")
            + "| AMEND-1 | Cover unapproved drift | ready | delegate | "
            "src/calculator.py | SF-1 | AC-1 | unit, delivery | "
            f"{scenario['verify_command']} | - |\n",
            encoding="utf-8",
        )
        cli(
            project,
            "verify",
            "--task",
            "AMEND-1",
            "--command",
            scenario["verify_command"],
            "--strict",
        )
        completed = cli(
            project,
            "complete-task",
            "--task",
            "AMEND-1",
            "--changed-file",
            "src/calculator.py",
        )
        assert json.loads(completed.stdout)["verdict"] == "COMPLETE"
        for task in ("SF-1", "AMEND-1"):
            cli(
                project,
                "verify",
                "--task",
                task,
                "--command",
                scenario["verify_command"],
                "--strict",
            )
        commit_all(project, "Attempt unapproved root Plan amendment")
        write_clean_review(project)
        install_delivery_evidence(project, scenario)
        refused = cli(project, "done", "--strict", expected=1)
        payload = json.loads(refused.stdout)
        assert payload["is_complete"] is False
        assert payload["drift"]["detected"] is True
        assert payload["drift"]["covered_by_completed_amendment"] is None
        assert payload["drift"]["covered_by_completed_change_packet"] is None
        assert payload["drift"]["actionable"] is True
        assert any(
            item.get("rule") == "post-proof-change-packet-required"
            for item in payload["problems"]
        )
        assert changes.list_change_packets(project) == []


def test_star_forge_dogfood_runs_from_intake_through_change_completion() -> None:
    with tempfile.TemporaryDirectory(prefix="star-forge-v04-self-dogfood-") as tmp:
        workspace = Path(tmp) / "dogfood"
        shutil.copytree(DOGFOOD_FIXTURE, workspace)
        project = workspace / "project"
        scenario = load_json(workspace / "scenario.json")

        initial = cli(
            project,
            "run",
            "--adopt-root",
            "--no-hooks",
            "--no-agents",
            "--objective",
            "Dogfood Star Forge v0.4 on its own lifecycle status control plane",
        )
        assert "phase: intake" in initial.stdout
        assert state(project)["phase"] == "intake"

        shutil.copy2(workspace / "contracts" / "Blueprint.md", project / "Blueprint.md")
        shutil.copy2(workspace / "contracts" / "Plan.md", project / "Plan.md")
        planning = run_state(project)
        assert planning["phase"] == "plan"
        lifecycle_contract = contracts.parse_blueprint_lifecycle_contract(
            (project / "Blueprint.md").read_text(encoding="utf-8")
        )
        blueprint_contract = contracts.parse_blueprint_plan_contract(
            (project / "Blueprint.md").read_text(encoding="utf-8")
        )
        assert lifecycle_contract["intake"]["complete"] is True
        assert lifecycle_contract["design"] == {
            "present": True,
            "required": False,
            "complete": True,
            "direction_selected": False,
            "unavailable_recorded": False,
        }
        assert blueprint_contract["delivery_target"] == "source-only"

        cli(project, "approve-blueprint")
        validated = cli(project, "validate-plan", "--strict")
        assert json.loads(validated.stdout)["plan_version"] == "v2"
        commit_all(project, "Approve the Star Forge self-dogfood contract")
        assert run_state(project)["phase"] == "foundation"

        foundation = install_dogfood_foundation_evidence(project, blueprint_contract)
        assert foundation["status"] == "PASS"
        assert run_state(project)["phase"] == "build"

        routes = routing.resolve_routes(
            project_class=str(scenario["routing_project_class"]),
            blueprint_flags={"performance_sensitive": True},
            proof_kinds=scenario["proofs"],
            delivery_target=[scenario["delivery_target"]],
            available_capabilities=scenario["available_capabilities"],
        )
        assert routes.blocked is False
        assert routes.decisions == ()

        verified = cli(
            project,
            "verify",
            "--task",
            "SF-1",
            "--command",
            scenario["verify_command"],
            "--strict",
        )
        assert json.loads(verified.stdout)["verdict"] == "PASS"
        envelope = write_evidence_v2(project, scenario)
        assert envelope["provider"] == "offline-fixture-replay"
        completed_task = cli(
            project,
            "complete-task",
            "--task",
            "SF-1",
            "--changed-file",
            scenario["source_file"],
        )
        assert json.loads(completed_task.stdout)["verdict"] == "COMPLETE"
        cli(
            project,
            "verify",
            "--task",
            "SF-1",
            "--command",
            scenario["verify_command"],
            "--strict",
        )
        commit_all(project, "Complete the initial Star Forge dogfood slice")
        assert run_state(project)["phase"] == "review"

        initial_roles = write_clean_review(project)
        assert initial_roles == [
            "correctness",
            "ux-accessibility",
            "performance-reliability",
        ]
        assert run_state(project)["phase"] == "deliver"
        initial_delivery = install_delivery_evidence(project, scenario)
        assert initial_delivery["status"] == "PASS"
        assert initial_delivery["provider"] == "not-applicable"
        assert run_state(project)["phase"] == "done"
        initial_done = json.loads(cli(project, "done", "--strict").stdout)
        assert initial_done["is_complete"] is True
        initial_proof = load_json(project / ".starforge" / "final" / "proof.json")

        source = project / scenario["source_file"]
        source.write_text(
            source.read_text(encoding="utf-8")
            + "\n\ndef completion_is_fresh(proof_hash: str, source_hash: str) -> bool:\n"
            + "    \"\"\"Return whether completion proof matches current source.\"\"\"\n"
            + "    return proof_hash == source_hash\n",
            encoding="utf-8",
        )
        fixture_test = project / "tests" / "test_fixture.py"
        fixture_test.write_text(
            fixture_test.read_text(encoding="utf-8")
            .replace(
                "from starforge.dogfood_status import format_status",
                "from starforge.dogfood_status import completion_is_fresh, format_status",
            )
            .replace(
                "\n\nif __name__ == \"__main__\":",
                "\n\n    def test_completion_freshness_uses_exact_source_hash(self) -> None:\n"
                "        self.assertTrue(completion_is_fresh(\"a\" * 64, \"a\" * 64))\n"
                "        self.assertFalse(completion_is_fresh(\"a\" * 64, \"b\" * 64))\n"
                "\n\nif __name__ == \"__main__\":",
            ),
            encoding="utf-8",
        )

        amended = run_state(project)
        assert amended["phase"] == "amend"
        packet = amended["change_packet"]
        assert packet["approval_state"] == "draft"
        assert packet["original_completed_source_hash"] == initial_proof["source_hash"]
        assert set(packet["scope_delta"]) == {
            "scripts/starforge/dogfood_status.py",
            "tests/test_fixture.py",
        }
        change_id = str(packet["change_id"])
        cli(project, "approve-change", "--change", change_id)
        approved = run_state(project)
        assert approved["change_packet"]["approval_state"] == "approved"
        change_plan = project / str(packet["path"]) / str(packet["plan_path"])
        task = star_forge.parse_tasks(change_plan)[0]
        assert task["mode"] == "delegate"
        assert task["verify"] == scenario["verify_command"]

        cli(
            project,
            "verify",
            "--task",
            task["id"],
            "--command",
            task["verify"],
            "--strict",
        )
        cli(
            project,
            "complete-task",
            "--task",
            task["id"],
            "--changed-file",
            "scripts/starforge/dogfood_status.py",
            "--changed-file",
            "tests/test_fixture.py",
        )
        cli(
            project,
            "verify",
            "--task",
            task["id"],
            "--command",
            task["verify"],
            "--strict",
        )
        cli(
            project,
            "verify",
            "--task",
            "SF-1",
            "--command",
            scenario["verify_command"],
            "--strict",
        )
        commit_all(project, "Complete the approved dogfood freshness change")
        assert run_state(project)["phase"] == "review"

        changed_roles = write_clean_review(project)
        assert changed_roles == initial_roles
        assert run_state(project)["phase"] == "deliver"
        changed_delivery = install_delivery_evidence(project, scenario)
        assert changed_delivery["status"] == "PASS"
        assert run_state(project)["phase"] == "done"
        changed_done = json.loads(cli(project, "done", "--strict").stdout)
        assert changed_done["is_complete"] is True
        final_state = run_state(project)
        assert final_state["phase"] == "done"
        assert final_state["drift"]["detected"] is False
        final_proof = load_json(project / ".starforge" / "final" / "proof.json")
        assert final_proof["source_hash"] == star_forge.source_hash(project)
        assert final_proof["source_hash"] != initial_proof["source_hash"]
        history = changes.lookup_change_history(project, change_id)
        assert history["kind"] == "change-packet"


def test_v03_completion_amendments_and_evidence_remain_readable() -> None:
    fixture = LEGACY_FIXTURES / "completed-amended"
    plan_before = (fixture / "Plan.md").read_bytes()
    history = changes.change_history(fixture)
    assert history["legacy_amendment_count"] == 2
    assert [item["change_id"] for item in history["legacy_amendments"]] == [
        "AMEND-1",
        "AMEND-2",
    ]
    legacy_manifest = (
        fixture
        / "dot-starforge"
        / "live"
        / "SF-002"
        / "browser"
        / "manifest.json"
    )
    adapted = evidence.read_envelope(legacy_manifest)
    assert adapted["schema"] == evidence.EVIDENCE_SCHEMA
    assert adapted["provenance"]["adapter"] == evidence.LEGACY_LIVE_MANIFEST_SCHEMA
    assert (fixture / "Plan.md").read_bytes() == plan_before


def test_fixture_matrix_contains_no_credentials_or_provider_writes() -> None:
    fixture_files = [path for path in FIXTURES.rglob("*") if path.is_file()]
    assert fixture_files
    joined = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore") for path in fixture_files
    )
    forbidden = (
        "github" + "_pat_",
        "g" + "hp_",
        "BEGIN PRIVATE" + " KEY",
        "gh repo create --public",
        "vercel deploy",
        "sites deploy",
    )
    assert not any(value in joined for value in forbidden)
    assert all(".starforge" not in path.parts for path in fixture_files)


def main() -> int:
    tests = [
        (name, value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures: list[str] = []
    for name, test in tests:
        try:
            test()
        except Exception:
            failures.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"PASS {name}")
    passed = len(tests) - len(failures)
    print(
        f"\ntest_v04_e2e.py: {passed} passed, "
        f"{len(failures)} failed, {len(tests)} total"
    )
    if failures:
        print("failed tests: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
