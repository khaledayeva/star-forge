#!/usr/bin/env python3
"""Focused tests for the GitHub PR live evidence adapter.

Plain-python suite. Run with: python3 tests/test_live_github_pr.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "star_forge.py"
GITHUB_PR_SCRIPT = ROOT / "scripts" / "live_collectors" / "github_pr.py"
FIXTURES = ROOT / "fixtures" / "github-pr"

SPEC = importlib.util.spec_from_file_location("star_forge", SCRIPT)
assert SPEC and SPEC.loader
star_forge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(star_forge)

GH_SPEC = importlib.util.spec_from_file_location("github_pr", GITHUB_PR_SCRIPT)
assert GH_SPEC and GH_SPEC.loader
github_pr = importlib.util.module_from_spec(GH_SPEC)
sys.modules["github_pr"] = github_pr
GH_SPEC.loader.exec_module(github_pr)

from starforge import evidence

os.environ["STAR_FORGE_LEARNINGS_HOME"] = tempfile.mkdtemp(prefix="star-forge-github-pr-test-learnings-")

PLAN_HEADER = (
    "| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |\n"
    "|------|-------------|--------|------|-------|---------|--------|----------|\n"
)
REAL_VERIFY = "python3 -c \"print('ok')\""
TASK = "SF-005"
REPO = "star-forge/tools"
PR = "42"


def run_star_forge(args: list[str]) -> tuple[int, dict[str, Any], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = star_forge.main(args)
    out = stdout.getvalue()
    err = stderr.getvalue()
    payload = json.loads(out) if out.strip().startswith("{") else {}
    return code, payload, err


def run_collector(args: list[str]) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = github_pr.main(args)
    return code, stdout.getvalue()


def init_project(project: Path) -> None:
    code, payload, err = run_star_forge(["init", "--project", str(project), "--no-agents"])
    assert code == 0, err or payload
    src = project / "src" / "app.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("print('hello github pr')\n", encoding="utf-8")
    (project / "Plan.md").write_text(
        "# Plan.md\n\n" + PLAN_HEADER
        + f"| {TASK} | Build GitHub PR evidence adapter | ready | solo | src/app.py | - | {REAL_VERIFY} | - |\n",
        encoding="utf-8",
    )


def manifest_path(project: Path) -> Path:
    return project / ".starforge" / "live" / TASK / "github" / "manifest.json"


def evidence_path(project: Path) -> Path:
    return project / ".starforge" / "live" / TASK / "github" / github_pr.EVIDENCE_FILENAME


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_connector_payload() -> dict[str, Any]:
    return load_json(FIXTURES / "connector-happy.json")


def connector_fixture(project: Path, mutate: Callable[[dict[str, Any]], None] | None = None) -> Path:
    payload = load_connector_payload()
    if mutate:
        mutate(payload)
    return write_json(project / "connector-fixture.json", payload)


def connector_input(project: Path, mutate: Callable[[dict[str, Any]], None] | None = None) -> Path:
    payload = load_connector_payload()
    payload["pr"]["url"] = f"https://github.com/{REPO}/pull/{PR}"
    for operation in payload.get("operations", []):
        if isinstance(operation, dict):
            operation.setdefault("repo", REPO)
            operation.setdefault("pr", PR)
            operation.setdefault("github_host", "github.com")
    payload["tool_versions"] = {"github_connector": "1.4.0", "github_api": "2022-11-28"}
    payload["live_provenance"] = {
        "source": "github-connector-live",
        "repo": REPO,
        "pr": PR,
        "github_host": "github.com",
        "collected_at": "2026-06-18T12:06:00Z",
        "collector": "codex-github-connector",
    }
    if mutate:
        mutate(payload)
    return write_json(project / "connector-input.json", payload)


def gh_fixture_dir(project: Path, mutate: Callable[[Path], None] | None = None) -> Path:
    target = project / "gh-fixture"
    shutil.copytree(FIXTURES / "gh-readonly", target)
    if mutate:
        mutate(target)
    return target


def gh_readonly_dir(project: Path, mutate: Callable[[Path], None] | None = None) -> Path:
    target = project / "gh-readonly-live"
    shutil.copytree(FIXTURES / "gh-readonly", target)
    for filename in ("pr-view.json", "final-pr-view.json"):
        payload = load_json(target / filename)
        payload["url"] = f"https://github.com/{REPO}/pull/{PR}"
        write_json(target / filename, payload)
    write_json(target / "tool-versions.json", {"gh": "2.75.0", "github_api": "2022-11-28"})
    write_json(
        target / "provenance.json",
        {
            "source": "gh-readonly-live",
            "repo": REPO,
            "pr": PR,
            "github_host": "github.com",
            "collected_at": "2026-06-18T12:07:00Z",
            "collector": "gh-cli-readonly",
        },
    )
    if mutate:
        mutate(target)
    return target


def collect_connector(project: Path, fixture: Path, extra: list[str] | None = None) -> tuple[int, str, Path]:
    args = [
        "--project", str(project),
        "--task", TASK,
        "--repo", REPO,
        "--pr", PR,
        "--connector-fixture", str(fixture),
    ]
    if extra:
        args.extend(extra)
    code, out = run_collector(args)
    return code, out, manifest_path(project)


def collect_connector_input(project: Path, input_path: Path, extra: list[str] | None = None) -> tuple[int, str, Path]:
    args = [
        "--project", str(project),
        "--task", TASK,
        "--repo", REPO,
        "--pr", PR,
        "--connector-input", str(input_path),
    ]
    if extra:
        args.extend(extra)
    code, out = run_collector(args)
    return code, out, manifest_path(project)


def collect_gh(project: Path, fixture_dir: Path, extra: list[str] | None = None) -> tuple[int, str, Path]:
    args = [
        "--project", str(project),
        "--task", TASK,
        "--repo", REPO,
        "--pr", PR,
        "--gh-fixture-dir", str(fixture_dir),
    ]
    if extra:
        args.extend(extra)
    code, out = run_collector(args)
    return code, out, manifest_path(project)


def collect_gh_readonly(project: Path, fixture_dir: Path, extra: list[str] | None = None) -> tuple[int, str, Path]:
    args = [
        "--project", str(project),
        "--task", TASK,
        "--repo", REPO,
        "--pr", PR,
        "--gh-readonly-dir", str(fixture_dir),
    ]
    if extra:
        args.extend(extra)
    code, out = run_collector(args)
    return code, out, manifest_path(project)


def rules_from_manifest(path: Path) -> set[str]:
    payload = load_json(path)
    return {str(item.get("rule")) for item in payload.get("problems", []) if isinstance(item, dict)}


def rules_from_payload(payload: dict[str, Any]) -> set[str]:
    return {str(item.get("rule")) for item in payload.get("problems", []) if isinstance(item, dict)}


def assert_core_passes(project: Path, manifest: Path) -> None:
    code, payload, err = run_star_forge([
        "source-packet-github-pr-review",
        "--project", str(project),
        "--input", str(manifest),
        "--strict",
    ])
    assert err == ""
    assert code == 0, payload
    assert payload["verdict"] == "PASS", payload
    code, payload, err = run_star_forge([
        "source-packet-proof",
        "--project", str(project),
        "--task", TASK,
        "--profile", "production-review",
        "--input", str(manifest),
        "--strict",
    ])
    assert err == ""
    assert code == 0, payload
    assert payload["verdict"] == "PASS", payload


def assert_core_fails(project: Path, manifest: Path, rule: str) -> None:
    code, payload, _ = run_star_forge([
        "source-packet-github-pr-review",
        "--project", str(project),
        "--input", str(manifest),
        "--strict",
    ])
    assert code == 1, payload
    assert payload["verdict"] == "FAIL", payload
    assert rule in rules_from_payload(payload), payload.get("problems")


def assert_production_proof_fails(project: Path, manifest: Path, rule: str) -> None:
    code, payload, _ = run_star_forge([
        "source-packet-proof",
        "--project", str(project),
        "--task", TASK,
        "--profile", "production-review",
        "--input", str(manifest),
        "--strict",
    ])
    assert code == 1, payload
    assert payload["verdict"] == "FAIL", payload
    assert rule in rules_from_payload(payload), payload.get("problems")


def refresh_manifest_artifact_hash(project: Path, manifest: Path, artifact: Path) -> None:
    payload = load_json(manifest)
    rel = str(artifact.relative_to(project))
    digest = star_forge.file_sha256(artifact)
    for item in payload.get("artifacts", []):
        if isinstance(item, dict) and item.get("path") == rel:
            item["sha256"] = digest
            item["bytes"] = artifact.stat().st_size
    raw_hashes = payload.get("raw_artifact_hashes")
    if isinstance(raw_hashes, dict) and isinstance(raw_hashes.get(rel), dict):
        raw_hashes[rel]["sha256"] = digest
        raw_hashes[rel]["bytes"] = artifact.stat().st_size
    write_json(manifest, payload)


def test_connector_fixture_writes_packet_without_production_proof_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        fixture = connector_fixture(project)
        code, out, manifest = collect_connector(project, fixture)
        assert code == 0, out
        assert "production proof commands were not emitted" in out
        assert "source-packet-github-pr-review" not in out
        assert "source-packet-proof" not in out
        payload = load_json(manifest)
        assert payload["collector"] == "github"
        assert payload["summary"]["source"] == "connector-fixture"
        assert payload["summary"]["captured_head_sha"] == "2222222222222222222222222222222222222222"
        assert_core_fails(project, manifest, "github-fixture-provenance")
        assert_production_proof_fails(project, manifest, "github-fixture-provenance")


def test_gh_read_only_path_writes_source_packet_and_core_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        fixture_dir = gh_fixture_dir(project)
        code, out, manifest = collect_gh(project, fixture_dir)
        assert code == 0, out
        assert "production proof commands were not emitted" in out
        assert "source-packet-proof" not in out
        payload = load_json(manifest)
        assert payload["summary"]["source"] == "gh-fixture"
        assert payload["summary"]["captured_base_sha"] == "3333333333333333333333333333333333333333"
        assert payload["summary"]["read_only_commands"]
        assert_core_fails(project, manifest, "github-fixture-provenance")
        assert_production_proof_fails(project, manifest, "github-fixture-provenance")


def test_connector_input_writes_degraded_packet_without_provider_receipt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        input_path = connector_input(project)
        code, out, manifest = collect_connector_input(project, input_path)
        assert code == 1, out
        assert "source-packet-github-pr-review" in out
        assert "source-packet-proof" in out
        payload = load_json(manifest)
        assert payload["summary"]["source"] == "github-import-live"
        assert payload["degraded"] is True
        transcript = project / ".starforge" / "live" / TASK / "github" / "operation-transcript.json"
        assert transcript.exists()
        transcript_hash = github_pr.live_common.file_sha256(transcript)
        assert payload["summary"]["read_only_transcript_sha256"] == transcript_hash
        assert payload["summary"]["live_provenance"]["operation_transcript_sha256"] == transcript_hash
        assert payload["raw_artifact_hashes"][str(transcript.relative_to(project))]["sha256"] == transcript_hash
        envelope = evidence.read_envelope(
            evidence_path(project),
            project_root=project,
            verify_artifacts=True,
        )
        assert envelope["capability"] == github_pr.CAPABILITY
        assert envelope["provider"] == "github-unavailable"
        assert envelope["verdict"] == "FAIL"
        assert envelope["provenance"]["route"]["fallback"] is True
        assert envelope["provenance"]["repository"]["full_name"] == REPO
        assert envelope["provenance"]["pull_request"]["number"] == PR
        assert envelope["provenance"]["source_binding"]["source_hash"] == payload["source_hash_after"]
        assert_core_fails(project, manifest, "github-provider-receipt")


def test_self_authored_connector_json_cannot_claim_live_github_provenance() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        input_path = connector_input(project)
        payload = load_json(input_path)
        payload["live_provenance"]["receipt"] = "caller-authored"
        payload["live_provenance"]["trusted"] = True
        write_json(input_path, payload)
        code, out, manifest = collect_connector_input(project, input_path)
        assert code == 1, out
        manifest_payload = load_json(manifest)
        assert manifest_payload["degraded"] is True
        assert "github-provider-receipt" in rules_from_manifest(manifest)
        assert manifest_payload["summary"]["source"] == "github-import-live"
        assert_core_fails(project, manifest, "github-provider-receipt")
        assert_production_proof_fails(project, manifest, "github-provider-receipt")


def test_direct_connector_receipt_binds_import_to_exact_payload() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        input_path = connector_input(project)
        receipt = {
            "source": "github-connector-direct",
            "input_sha256": github_pr.live_common.file_sha256(input_path),
            "operation_id": "connector-read-42",
            "collected_at": "2026-06-18T12:06:00Z",
        }
        assert github_pr.load_connector_input(
            input_path, provider_receipt=receipt,
        ).source == "github-connector-live"
        receipt["input_sha256"] = "0" * 64
        assert github_pr.load_connector_input(
            input_path, provider_receipt=receipt,
        ).source == "github-import-live"


def test_connector_input_records_source_bound_foundation_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        input_path = connector_input(project)
        source_hash = github_pr.live_common.compute_source_hash(project)
        head = "2" * 40
        foundation_path = project / ".starforge" / "foundation" / "evidence.json"
        write_json(
            foundation_path,
            {
                "schema": "star-forge.foundation-evidence.v1",
                "source_hash": source_hash,
                "checks": {
                    "github_repository": {
                        "state": "satisfied",
                        "detail": {
                            "provider": "github-connector",
                            "owner": "star-forge",
                            "name": "tools",
                            "visibility": "private",
                            "identity_verified": True,
                            "visibility_verified": True,
                            "created": True,
                        },
                    },
                    "remote_origin": {
                        "state": "satisfied",
                        "detail": {
                            "remote": "origin",
                            "url": f"https://github.com/{REPO}.git",
                        },
                    },
                    "default_branch": {
                        "state": "satisfied",
                        "detail": {
                            "name": "main",
                            "exists": True,
                            "head_commit": head,
                        },
                    },
                    "initial_commit": {
                        "state": "satisfied",
                        "detail": {
                            "sha": head,
                            "current_head": head,
                            "tree_source_hash": source_hash,
                        },
                    },
                    "ci": {
                        "state": "satisfied",
                        "detail": {
                            "path": ".github/workflows/ci.yml",
                            "sha256": "d" * 64,
                            "committed": True,
                        },
                    },
                },
            },
        )
        code, out, manifest = collect_connector_input(
            project,
            input_path,
            ["--foundation-evidence", str(foundation_path)],
        )
        assert code == 1, out
        envelope = evidence.read_envelope(
            evidence_path(project),
            project_root=project,
            verify_artifacts=True,
        )
        foundation = envelope["provenance"]["foundation"]
        assert foundation["applicable"] is True
        assert foundation["repository"]["full_name"] == REPO
        assert foundation["repository"]["visibility"] == "private"
        assert foundation["remote"]["name"] == "origin"
        assert foundation["default_branch"]["name"] == "main"
        assert foundation["initial_commit"]["tree_source_hash"] == source_hash
        assert foundation["ci"]["path"] == ".github/workflows/ci.yml"
        assert foundation["provider_route"]["preferred_provider"] == "github-connector"
        assert_core_fails(project, manifest, "github-provider-receipt")


def test_foundation_gh_creation_requires_the_exact_private_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        input_path = connector_input(project)
        source_hash = github_pr.live_common.compute_source_hash(project)
        head = "3" * 40
        raw = github_pr.load_connector_input(input_path)
        raw.foundation_provenance = {
            "schema": "star-forge.foundation-evidence.v1",
            "source_hash": source_hash,
            "github_repository": {
                "provider": "gh-cli",
                "fallback": "gh repo create --private",
                "owner": "star-forge",
                "name": "tools",
                "visibility": "private",
                "identity_verified": True,
                "visibility_verified": True,
                "created": True,
            },
            "remote_origin": {
                "remote": "origin",
                "url": f"https://github.com/{REPO}.git",
            },
            "default_branch": {
                "name": "main",
                "exists": True,
                "head_commit": head,
            },
            "initial_commit": {
                "sha": head,
                "current_head": head,
                "tree_source_hash": source_hash,
            },
            "ci": {
                "path": ".github/workflows/ci.yml",
                "sha256": "e" * 64,
                "committed": True,
            },
        }
        problems: list[dict[str, Any]] = []
        normalized = github_pr.normalize_foundation_provenance(
            raw,
            repo=REPO,
            current_source_hash=source_hash,
            problems=problems,
        )
        assert problems == []
        assert normalized["provider_route"]["recorded_fallback"] == github_pr.GH_CREATE_FALLBACK

        raw.foundation_provenance["github_repository"]["fallback"] = "gh repo create --public"
        problems = []
        github_pr.normalize_foundation_provenance(
            raw,
            repo=REPO,
            current_source_hash=source_hash,
            problems=problems,
        )
        assert any(
            item.get("rule") == "github-foundation-provenance"
            and github_pr.GH_CREATE_FALLBACK in str(item.get("message"))
            for item in problems
        )


def test_connector_input_requires_explicit_final_freshness_without_initial_ref_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload.pop("final_pr", None)
            payload.pop("freshness", None)

        input_path = connector_input(project, mutate)
        code, _, manifest = collect_connector_input(project, input_path)
        assert code == 1
        assert "github-live-provenance" in rules_from_manifest(manifest)

        payload = load_json(manifest)
        summary = payload["summary"]
        assert summary["captured_base_sha"] == "1111111111111111111111111111111111111111"
        assert summary["captured_head_sha"] == "2222222222222222222222222222222222222222"
        assert summary["current_base_sha"] == ""
        assert summary["current_head_sha"] == ""

        pr_payload = load_json(manifest.parent / "pr.json")
        assert pr_payload["base_sha"] == "1111111111111111111111111111111111111111"
        assert pr_payload["head_sha"] == "2222222222222222222222222222222222222222"
        assert pr_payload["current_base_sha"] == ""
        assert pr_payload["current_head_sha"] == ""
        assert_core_fails(project, manifest, "github-live-provenance")


def test_connector_input_record_runs_both_strict_proof_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        input_path = connector_input(project)
        code, out, _ = collect_connector_input(project, input_path, ["--record"])
        assert code == 1, out
        assert "source-packet-github-pr-review" in out
        assert "source-packet-proof" in out
        github_records = star_forge.load_run_records(project, kind="source-packet-github-pr-review", task=TASK)
        production_records = star_forge.load_run_records(project, kind="source-packet-proof", task=TASK)
        assert github_records and github_records[-1]["verdict"] == "FAIL"
        assert production_records == []


def test_connector_input_record_uses_bundled_star_forge_from_unrelated_cwd() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        project = root / "project"
        init_project(project)
        input_path = connector_input(project)
        caller = root / "caller"
        sentinel_dir = caller / "scripts"
        sentinel_dir.mkdir(parents=True)
        marker = root / "sentinel-ran.txt"
        (sentinel_dir / "star_forge.py").write_text(
            "import pathlib\n"
            "import sys\n"
            f"pathlib.Path({str(marker)!r}).write_text('ran\\n', encoding='utf-8')\n"
            "sys.exit(64)\n",
            encoding="utf-8",
        )

        proc = subprocess.run(
            [
                sys.executable,
                str(GITHUB_PR_SCRIPT),
                "--project", str(project),
                "--task", TASK,
                "--repo", REPO,
                "--pr", PR,
                "--connector-input", str(input_path),
                "--record",
            ],
            cwd=str(caller),
            text=True,
            capture_output=True,
            check=False,
        )

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert not marker.exists()
        assert "source-packet-github-pr-review" in proc.stdout
        assert "source-packet-proof" in proc.stdout
        github_records = star_forge.load_run_records(project, kind="source-packet-github-pr-review", task=TASK)
        production_records = star_forge.load_run_records(project, kind="source-packet-proof", task=TASK)
        assert github_records and github_records[-1]["verdict"] == "FAIL"
        assert production_records == []


def test_connector_input_rejects_unbound_ci_log_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            identity = {
                "repo": REPO,
                "pr": PR,
                "captured_head_sha": "2222222222222222222222222222222222222222",
                "run_id": "999",
            }
            payload["operations"].append({"action": "read", "operation": "logs", **identity})
            payload["logs"] = [
                {
                    "name": "unbound-run-999",
                    "text": "failing log excerpt\n",
                    **identity,
                }
            ]

        input_path = connector_input(project, mutate)
        code, _, manifest = collect_connector_input(project, input_path, ["--include-ci-logs", "--max-log-bytes", "128"])
        assert code == 1
        rules = rules_from_manifest(manifest)
        assert "github-command" in rules
        assert "github-logs" in rules
        assert not (project / ".starforge" / "live" / TASK / "github" / "ci-log-excerpts.json").exists()
        assert_core_fails(project, manifest, "github-command")
        assert_production_proof_fails(project, manifest, "github-command")


def test_connector_input_accepts_pr_bound_ci_log_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            identity = {
                "repo": REPO,
                "pr": PR,
                "github_host": "github.com",
                "captured_head_sha": "2222222222222222222222222222222222222222",
                "check_run_id": "501",
            }
            payload["operations"].append({"action": "read", "operation": "logs", **identity})
            payload["logs"] = [
                {
                    "name": "unit",
                    "text": "passing log excerpt\n",
                    **identity,
                }
            ]

        input_path = connector_input(project, mutate)
        code, _, manifest = collect_connector_input(project, input_path, ["--include-ci-logs", "--max-log-bytes", "128"])
        assert code == 1
        assert "github-command" not in rules_from_manifest(manifest)
        assert "github-logs" not in rules_from_manifest(manifest)
        logs = load_json(project / ".starforge" / "live" / TASK / "github" / "ci-log-excerpts.json")
        assert logs["logs"][0]["check_run_id"] == "501"
        assert logs["logs"][0]["captured_head_sha"] == "2222222222222222222222222222222222222222"
        assert_core_fails(project, manifest, "github-provider-receipt")


def test_connector_input_requires_provenance_repo_and_pr_without_synthesis() -> None:
    for missing_key in ("repo", "pr"):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(payload: dict[str, Any]) -> None:
                payload["live_provenance"].pop(missing_key, None)

            input_path = connector_input(project, mutate)
            code, _, manifest = collect_connector_input(project, input_path)
            assert code == 1
            assert "github-live-provenance" in rules_from_manifest(manifest)
            payload = load_json(manifest)
            assert missing_key not in payload["summary"]["live_provenance"]
            assert_core_fails(project, manifest, "github-live-provenance")


def test_connector_input_requires_explicit_live_provenance_host_without_url_synthesis() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload["live_provenance"].pop("github_host", None)
            payload["pr"]["url"] = f"https://github.com/{REPO}/pull/{PR}"

        input_path = connector_input(project, mutate)
        code, _, manifest = collect_connector_input(project, input_path)
        assert code == 1
        assert "github-live-provenance" in rules_from_manifest(manifest)
        payload = load_json(manifest)
        assert "github_host" not in payload["summary"]["live_provenance"]
        assert payload["summary"]["github_host"] == ""
        assert_core_fails(project, manifest, "github-live-provenance")


def test_connector_input_rejects_off_host_live_provenance_with_connector_operations() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload["live_provenance"]["github_host"] = "evil.example"

        input_path = connector_input(project, mutate)
        code, _, manifest = collect_connector_input(project, input_path)
        assert code == 1
        assert "github-live-provenance" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-live-provenance")


def test_connector_input_rejects_wrong_repo_or_pr_connector_operation_identity() -> None:
    bad_operations = [
        {"action": "read", "operation": "pull_request", "repo": "other/repo", "pr": PR, "github_host": "github.com"},
        {"action": "read", "operation": "pull_request", "repository": "other/repo", "pr": PR, "github_host": "github.com"},
        {"action": "read", "operation": "pull_request", "repository": f"https://evil.example/{REPO}", "pr": PR, "github_host": "github.com"},
        {"action": "read", "operation": "pull_request", "repository": f"https://github.com/{REPO}", "pull_request": f"https://evil.example/{REPO}/pull/{PR}", "github_host": "github.com"},
        {"action": "read", "operation": "pull_request", "repo": REPO, "pullRequest": f"//evil.example/{REPO}/pull/{PR}", "github_host": "github.com"},
        {"action": "read", "operation": "pull_request", "repo": REPO, "pr": PR, "baseRef": {"url": f"https://evil.example/{REPO}"}},
        {"action": "read", "operation": "pull_request", "repo": REPO, "pr": "99", "github_host": "github.com"},
        {"action": "read", "operation": "pull_request", "repo": REPO, "pull_request": "99", "github_host": "github.com"},
    ]
    for bad_operation in bad_operations:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(payload: dict[str, Any], bad_operation: dict[str, Any] = bad_operation) -> None:
                payload["operations"][0] = bad_operation

            input_path = connector_input(project, mutate)
            code, _, manifest = collect_connector_input(project, input_path)
            assert code == 1
            assert "github-command" in rules_from_manifest(manifest)
            assert_core_fails(project, manifest, "github-command")


def test_connector_input_rejects_missing_live_connector_operation_identity() -> None:
    bad_operations: list[Any] = [
        "pull_request",
        {"action": "read", "operation": "pull_request", "pr": PR, "github_host": "github.com"},
        {"action": "read", "operation": "pull_request", "repo": REPO, "github_host": "github.com"},
        {"action": "read", "operation": "pull_request", "repo": REPO, "pr": PR},
    ]
    for bad_operation in bad_operations:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(payload: dict[str, Any], bad_operation: Any = bad_operation) -> None:
                payload["operations"][0] = bad_operation

            input_path = connector_input(project, mutate)
            code, _, manifest = collect_connector_input(project, input_path)
            assert code == 1
            assert "github-command" in rules_from_manifest(manifest)
            assert_core_fails(project, manifest, "github-command")


def test_connector_input_rejects_off_host_connector_operation_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload["operations"][0] = {
                "action": "read",
                "operation": "pull_request",
                "url": f"https://evil.example/{REPO}/pull/{PR}",
            }

        input_path = connector_input(project, mutate)
        code, _, manifest = collect_connector_input(project, input_path)
        assert code == 1
        assert "github-command" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-command")


def test_connector_input_rejects_scheme_relative_connector_operation_url() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload["operations"][0] = {
                "action": "read",
                "operation": "pull_request",
                "repo": REPO,
                "pr": PR,
                "url": f"//evil.example/{REPO}/pull/{PR}",
            }

        input_path = connector_input(project, mutate)
        code, _, manifest = collect_connector_input(project, input_path)
        assert code == 1
        assert "github-command" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-command")


def test_connector_input_rejects_pr_url_identity_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload["pr"]["url"] = "https://github.example/other/repo/pull/99"

        input_path = connector_input(project, mutate)
        code, _, manifest = collect_connector_input(project, input_path)
        assert code == 1
        assert "github-live-provenance" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-live-provenance")


def test_core_rejects_contradictory_secondary_pr_url_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        input_path = connector_input(project)
        code, _, manifest = collect_connector_input(project, input_path)
        assert code == 1
        pr_path = project / ".starforge" / "live" / TASK / "github" / "pr.json"
        pr_payload = load_json(pr_path)
        pr_payload["html_url"] = f"https://github.com/{REPO}/pull/99"
        write_json(pr_path, pr_payload)
        refresh_manifest_artifact_hash(project, manifest, pr_path)
        assert_core_fails(project, manifest, "github-live-provenance")


def test_gh_readonly_requires_matching_provenance_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(path: Path) -> None:
            provenance = load_json(path / "provenance.json")
            provenance["repo"] = "other/repo"
            write_json(path / "provenance.json", provenance)

        fixture_dir = gh_readonly_dir(project, mutate)
        code, _, manifest = collect_gh_readonly(project, fixture_dir)
        assert code == 1
        assert "github-live-provenance" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-live-provenance")


def test_gh_readonly_accepts_pr_scoped_api_endpoints() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(path: Path) -> None:
            write_json(
                path / "commands.json",
                [
                    ["gh", "pr", "view", PR, "--repo", REPO, "--json", "number,title,state,baseRefOid,headRefOid,url"],
                    ["gh", "api", f"/repos/{REPO}/pulls/{PR}"],
                    ["gh", "api", f"repos/{REPO}/pulls/{PR}/files", "--paginate"],
                    ["gh", "api", f"repos/{REPO}/issues/{PR}/comments"],
                    ["gh", "api", f"repos/{REPO}/commits/4444444444444444444444444444444444444444/check-runs"],
                    ["gh", "api", f"repos/{REPO}/check-runs/701/annotations"],
                    ["gh", "api", f"repos/{REPO}/actions/runs/701/logs"],
                    ["gh", "api", f"repos/{REPO}/actions/jobs/1701/logs"],
                    ["gh", "run", "view", "701", "--repo", REPO, "--log"],
                ],
            )

        fixture_dir = gh_readonly_dir(project, mutate)
        code, out, manifest = collect_gh_readonly(project, fixture_dir)
        assert code == 0, out
        assert "source-packet-github-pr-review" in out
        envelope = evidence.read_envelope(
            evidence_path(project),
            project_root=project,
            verify_artifacts=True,
        )
        assert envelope["provider"] == github_pr.GH_READONLY_PROVIDER
        assert envelope["verdict"] == "DEGRADED"
        assert envelope["provenance"]["route"]["create_fallback"] == github_pr.GH_CREATE_FALLBACK
        assert any(
            item.get("rule") == "github-capability-fallback"
            and item.get("blocking") is False
            for item in envelope["blockers"]
        )
        assert_core_passes(project, manifest)


def test_gh_readonly_accepts_safe_path_style_api_query_params() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(path: Path) -> None:
            write_json(path / "commands.json", [["gh", "api", f"repos/{REPO}/pulls/{PR}/files?per_page=100&page=2"]])

        fixture_dir = gh_readonly_dir(project, mutate)
        code, out, manifest = collect_gh_readonly(project, fixture_dir)
        assert code == 0, out
        assert_core_passes(project, manifest)


def test_collector_rejects_shell_substitution_in_allowed_gh_option_values() -> None:
    bad_commands = [
        ["gh", "pr", "view", PR, "--repo", REPO, "--jq", "$(echo forged)"],
        ["gh", "pr", "view", PR, "--repo", REPO, "--template", "{{.title}} `echo forged`"],
        ["gh", "api", f"repos/{REPO}/pulls/{PR}", "--jq=$(echo forged)"],
        ["gh", "api", f"repos/{REPO}/pulls/{PR}", "--template", "{{.title}} $(echo forged)"],
    ]
    for command in bad_commands:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(payload: dict[str, Any], command: list[str] = command) -> None:
                payload["commands"] = [command]

            input_path = connector_input(project, mutate)
            code, _, manifest = collect_connector_input(project, input_path)
            assert code == 1
            assert "github-command" in rules_from_manifest(manifest)
            assert_core_fails(project, manifest, "github-command")
            assert_production_proof_fails(project, manifest, "github-command")


def test_collector_rejects_embedded_ampersands_in_gh_commands() -> None:
    bad_commands: list[Any] = [
        ["gh", "pr", "view", PR, "--repo", REPO, "--jq", ".title&echo forged"],
        ["gh", "pr", "view", PR, "--repo", REPO, "--template", "{{.title}}&echo forged"],
        ["gh", "api", f"repos/{REPO}/pulls/{PR}", "--method", "GET&echo forged"],
        ["gh", "api", "--hostname", "github.com&evil.example", f"repos/{REPO}/pulls/{PR}"],
        f"gh pr view {PR} --repo {REPO} --jq '.title&echo forged'",
    ]
    for command in bad_commands:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(payload: dict[str, Any], command: Any = command) -> None:
                payload["commands"] = [command]

            input_path = connector_input(project, mutate)
            code, _, manifest = collect_connector_input(project, input_path)
            assert code == 1
            assert "github-command" in rules_from_manifest(manifest)
            assert_core_fails(project, manifest, "github-command")
            assert_production_proof_fails(project, manifest, "github-command")


def test_gh_readonly_rejects_unbounded_path_style_api_query_params() -> None:
    commands = [
        ["gh", "api", f"repos/{REPO}/pulls/{PR}/files?page=nonnumeric"],
        ["gh", "api", f"repos/{REPO}/pulls/{PR}/files?page=0"],
        ["gh", "api", f"repos/{REPO}/pulls/{PR}/files?per_page=101"],
    ]
    for command in commands:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(path: Path, command: list[str] = command) -> None:
                write_json(path / "commands.json", [command])

            fixture_dir = gh_readonly_dir(project, mutate)
            code, _, manifest = collect_gh_readonly(project, fixture_dir)
            assert code == 1
            assert "github-command" in rules_from_manifest(manifest)
            assert_core_fails(project, manifest, "github-command")


def test_gh_readonly_rejects_sensitive_path_style_api_query_params_and_redacts_values() -> None:
    query_cases = [
        ("api-key", "collectorsecret0"),
        ("X-Amz-Signature", "collectorsecret1"),
        ("authorization", "collectorsecret2"),
        ("access_token", "collectorsecret3"),
    ]
    for key, secret in query_cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(path: Path, key: str = key, secret: str = secret) -> None:
                write_json(path / "commands.json", [["gh", "api", f"repos/{REPO}/pulls/{PR}?{key}={secret}"]])

            fixture_dir = gh_readonly_dir(project, mutate)
            code, _, manifest = collect_gh_readonly(project, fixture_dir)
            assert code == 1
            assert "github-command" in rules_from_manifest(manifest)
            assert_core_fails(project, manifest, "github-command")
            transcript = project / ".starforge" / "live" / TASK / "github" / "operation-transcript.json"
            artifact_text = manifest.read_text(encoding="utf-8") + transcript.read_text(encoding="utf-8")
            assert secret not in artifact_text
            assert "REDACTED_SECRET" in artifact_text


def test_gh_api_rejects_absolute_off_host_api_endpoint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(path: Path) -> None:
            write_json(path / "commands.json", [["gh", "api", f"https://evil.example/repos/{REPO}/pulls/{PR}"]])

        fixture_dir = gh_readonly_dir(project, mutate)
        code, _, manifest = collect_gh_readonly(project, fixture_dir)
        assert code == 1
        assert "github-command" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-command")


def test_gh_api_rejects_scheme_relative_api_endpoints() -> None:
    bad_commands = [
        ["gh", "api", f"//repos/{REPO}/pulls/{PR}"],
        ["gh", "api", f"//api.github.com/repos/{REPO}/pulls/{PR}"],
    ]
    for command in bad_commands:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(path: Path, command: list[str] = command) -> None:
                write_json(path / "commands.json", [command])

            fixture_dir = gh_readonly_dir(project, mutate)
            code, _, manifest = collect_gh_readonly(project, fixture_dir)
            assert code == 1
            assert "github-command" in rules_from_manifest(manifest)
            assert_core_fails(project, manifest, "github-command")


def test_gh_api_rejects_off_host_hostname_flag() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(path: Path) -> None:
            write_json(path / "commands.json", [["gh", "api", "--hostname", "evil.example", f"repos/{REPO}/pulls/{PR}"]])

        fixture_dir = gh_readonly_dir(project, mutate)
        code, _, manifest = collect_gh_readonly(project, fixture_dir)
        assert code == 1
        assert "github-command" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-command")


def test_gh_api_rejects_path_style_command_with_off_host_live_provenance() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(path: Path) -> None:
            provenance = load_json(path / "provenance.json")
            provenance["github_host"] = "evil.example"
            write_json(path / "provenance.json", provenance)
            write_json(path / "commands.json", [["gh", "api", f"repos/{REPO}/pulls/{PR}"]])

        fixture_dir = gh_readonly_dir(project, mutate)
        code, _, manifest = collect_gh_readonly(project, fixture_dir)
        assert code == 1
        assert "github-live-provenance" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-live-provenance")


def test_gh_api_rejects_attached_value_short_flags() -> None:
    bad_commands = [
        ["gh", "api", "-HAuthorization:Bearer token", f"repos/{REPO}/pulls/{PR}"],
        ["gh", "api", "-ffield=value", f"repos/{REPO}/issues/{PR}/comments"],
        ["gh", "api", "-Ffield=value", f"repos/{REPO}/issues/{PR}/comments"],
    ]
    for command in bad_commands:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(path: Path, command: list[str] = command) -> None:
                write_json(path / "commands.json", [command])

            fixture_dir = gh_readonly_dir(project, mutate)
            code, _, manifest = collect_gh_readonly(project, fixture_dir)
            assert code == 1
            assert "github-command" in rules_from_manifest(manifest)
            assert_core_fails(project, manifest, "github-command")


def test_gh_api_rejects_field_arguments_and_redacts_values() -> None:
    endpoint = f"repos/{REPO}/pulls/{PR}"
    field_cases = [
        (["gh", "api", "--method", "GET", "-f", "access_token=collectorsecretfield0", endpoint], "collectorsecretfield0"),
        (["gh", "api", "--method", "GET", "-F", "foo=collectorsecretfield1", endpoint], "collectorsecretfield1"),
        (["gh", "api", "--method", "GET", "--field", "page=nonnumeric", endpoint], "nonnumeric"),
        (["gh", "api", "--method", "GET", "--raw-field", "per_page=collectorsecretfield3", endpoint], "collectorsecretfield3"),
    ]
    for command, rejected_value in field_cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(path: Path, command: list[str] = command) -> None:
                write_json(path / "commands.json", [command])

            fixture_dir = gh_readonly_dir(project, mutate)
            code, _, manifest = collect_gh_readonly(project, fixture_dir)
            assert code == 1
            assert "github-command" in rules_from_manifest(manifest)
            assert_core_fails(project, manifest, "github-command")
            transcript = project / ".starforge" / "live" / TASK / "github" / "operation-transcript.json"
            artifact_text = manifest.read_text(encoding="utf-8") + transcript.read_text(encoding="utf-8")
            assert rejected_value not in artifact_text
            assert "REDACTED_SECRET" in artifact_text


def test_gh_api_rejects_unscoped_account_endpoints() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(path: Path) -> None:
            write_json(path / "commands.json", [["gh", "api", "/user/emails"]])

        fixture_dir = gh_readonly_dir(project, mutate)
        code, _, manifest = collect_gh_readonly(project, fixture_dir)
        assert code == 1
        assert "github-command" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-command")


def test_gh_run_view_rejects_wrong_repo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(path: Path) -> None:
            write_json(path / "commands.json", [["gh", "run", "view", "701", "--repo", "other/repo", "--log"]])

        fixture_dir = gh_readonly_dir(project, mutate)
        code, _, manifest = collect_gh_readonly(project, fixture_dir)
        assert code == 1
        assert "github-command" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-command")


def test_gh_api_rejects_unbound_actions_log_endpoints() -> None:
    endpoints = [
        f"repos/{REPO}/actions/runs/999/logs",
        f"repos/{REPO}/actions/jobs/999/logs",
    ]
    for endpoint in endpoints:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(path: Path, endpoint: str = endpoint) -> None:
                write_json(path / "commands.json", [["gh", "api", endpoint]])

            fixture_dir = gh_readonly_dir(project, mutate)
            code, _, manifest = collect_gh_readonly(project, fixture_dir)
            assert code == 1
            assert "github-command" in rules_from_manifest(manifest)
            assert_core_fails(project, manifest, "github-command")


def test_forbidden_gh_command_guardrails_block_checkout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(path: Path) -> None:
            write_json(path / "commands.json", [["gh", "pr", "checkout", PR]])

        fixture_dir = gh_fixture_dir(project, mutate)
        code, _, manifest = collect_gh(project, fixture_dir)
        assert code == 1
        assert "github-command" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-command")


def test_gh_readonly_rejects_trailing_shell_commands_in_string_and_argv_forms() -> None:
    bad_commands: list[Any] = [
        f"gh pr view {PR} --repo {REPO} ; gh pr checkout {PR}",
        ["gh", "pr", "view", PR, "--repo", REPO, ";", "gh", "pr", "checkout", PR],
        "gh run view 701 --repo " + REPO + " --log && gh run rerun 701",
        ["gh", "run", "view", "701", "--repo", REPO, "--log", "&&", "gh", "run", "rerun", "701"],
        f"gh api repos/{REPO}/pulls/{PR}\ngh api repos/{REPO}/actions/runs/701/rerun",
        ["gh", "api", f"repos/{REPO}/pulls/{PR}", "gh", "repo", "delete", REPO],
    ]
    for command in bad_commands:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)

            def mutate(path: Path, command: Any = command) -> None:
                write_json(path / "commands.json", [command])

            fixture_dir = gh_readonly_dir(project, mutate)
            code, _, manifest = collect_gh_readonly(project, fixture_dir)
            assert code == 1
            assert "github-command" in rules_from_manifest(manifest)
            assert_core_fails(project, manifest, "github-command")


def test_changed_base_sha_is_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload["final_pr"]["base"]["sha"] = "9999999999999999999999999999999999999999"

        fixture = connector_fixture(project, mutate)
        code, _, manifest = collect_connector(project, fixture)
        assert code == 1
        assert "github-freshness" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-freshness")


def test_changed_head_sha_is_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload["final_pr"]["head"]["sha"] = "8888888888888888888888888888888888888888"

        fixture = connector_fixture(project, mutate)
        code, _, manifest = collect_connector(project, fixture)
        assert code == 1
        assert "github-freshness" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-freshness")


def test_missing_refs_are_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload["pr"].pop("merge_base_sha", None)
            payload["pr"].pop("mergeBase", None)

        fixture = connector_fixture(project, mutate)
        code, _, manifest = collect_connector(project, fixture)
        assert code == 1
        assert "github-refs" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-refs")


def test_failing_checks_are_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload["check_runs"]["check_runs"][0]["conclusion"] = "failure"

        fixture = connector_fixture(project, mutate)
        code, _, manifest = collect_connector(project, fixture)
        assert code == 1
        assert "github-checks" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-checks")


def test_pending_checks_are_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload["check_runs"]["check_runs"][0]["status"] = "queued"
            payload["check_runs"]["check_runs"][0]["conclusion"] = ""

        fixture = connector_fixture(project, mutate)
        code, _, manifest = collect_connector(project, fixture)
        assert code == 1
        assert "github-checks" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-checks")


def test_skipped_checks_are_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload["check_runs"]["check_runs"][0]["conclusion"] = "skipped"

        fixture = connector_fixture(project, mutate)
        code, _, manifest = collect_connector(project, fixture)
        assert code == 1
        assert "github-checks" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-checks")


def test_partial_permissions_are_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload["check_runs"]["partial_permissions"] = True

        fixture = connector_fixture(project, mutate)
        code, _, manifest = collect_connector(project, fixture)
        assert code == 1
        assert "github-permissions" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-permissions")


def test_pagination_incomplete_is_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)

        def mutate(payload: dict[str, Any]) -> None:
            payload["check_runs"]["pagination"] = {"has_next_page": True}

        fixture = connector_fixture(project, mutate)
        code, _, manifest = collect_connector(project, fixture)
        assert code == 1
        assert "github-pagination" in rules_from_manifest(manifest)
        assert_core_fails(project, manifest, "github-pagination")


def test_bounded_log_redaction_hashes_without_raw_log_embedding() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        secret = "ghp_" + "A" * 24
        jwt = "eyJabcdefgh.ijklmnopq.rstuvwxyz"
        refresh = "refreshabc123"
        home_path = str(Path.home() / "private" / "token.txt")

        def mutate(payload: dict[str, Any]) -> None:
            identity = {
                "repo": REPO,
                "pr": PR,
                "captured_head_sha": "2222222222222222222222222222222222222222",
                "check_run_id": "501",
            }
            payload["operations"].append({"action": "read", "operation": "logs", **identity})
            payload["logs"] = [
                {
                    "name": "unit",
                    **identity,
                    "text": f"token={secret} path={home_path}\n" + ("x" * 1000),
                },
                {
                    "name": "integration",
                    **identity,
                    "text": f"Authorization: Bearer {jwt} refresh_token={refresh}\n" + ("x" * 1000),
                }
            ]

        fixture = connector_fixture(project, mutate)
        code, _, manifest = collect_connector(project, fixture, ["--include-ci-logs", "--max-log-bytes", "128"])
        assert code == 0
        logs = load_json(project / ".starforge" / "live" / TASK / "github" / "ci-log-excerpts.json")
        blob = json.dumps(logs)
        assert secret not in blob
        assert jwt not in blob
        assert refresh not in blob
        assert str(Path.home()) not in blob
        assert "[REDACTED_SECRET]" in blob
        entry = logs["logs"][0]
        assert entry["repo"] == REPO
        assert entry["pr"] == PR
        assert entry["check_run_id"] == "501"
        assert entry["original_sha256"]
        assert entry["excerpt_sha256"]
        assert entry["captured_bytes"] <= 128
        assert entry["truncated"] is True
        report = load_json(manifest)["redaction_report"]
        assert report.get("secret_values", 0) >= 3
        assert report.get("absolute_paths", 0) >= 1


def test_connector_redacts_signed_urls_and_hyphenated_api_key_fields() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        password = "supersecretpassword"
        signed_url = (
            f"https://reader:{password}@assets.example.test/build.zip?"
            "api-key=shortkey&X-Amz-Signature=shortsig&X-Amz-Credential=shortcred&AWSAccessKeyId=shortaccess&safe=ok"
            "#oauth_token=shortoauth"
        )
        api_key_value = "github-hyphen-api-key-value"

        def mutate(payload: dict[str, Any]) -> None:
            comment = payload["comments"]["comments"][0]
            comment["body"] = f"Debug artifact: {signed_url}"
            comment["x-api-key"] = api_key_value

        fixture = connector_fixture(project, mutate)
        code, _, _ = collect_connector(project, fixture)
        assert code == 0
        comments = load_json(project / ".starforge" / "live" / TASK / "github" / "comments.json")
        blob = json.dumps(comments)
        assert password not in blob
        assert "reader:" not in blob
        assert "shortkey" not in blob
        assert "shortsig" not in blob
        assert "shortcred" not in blob
        assert "shortaccess" not in blob
        assert "shortoauth" not in blob
        assert api_key_value not in blob
        assert "REDACTED_SECRET" in blob
        assert "[REDACTED]" in blob
        envelope_blob = evidence_path(project).read_text(encoding="utf-8")
        assert password not in envelope_blob
        assert "shortsig" not in envelope_blob
        assert api_key_value not in envelope_blob
        evidence.read_envelope(
            evidence_path(project),
            project_root=project,
            verify_artifacts=True,
        )


def test_absolute_paths_are_normalized_in_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        absolute = str(Path.home() / "workspace" / "src" / "app.py")

        def mutate(payload: dict[str, Any]) -> None:
            payload["files"][0]["filename"] = absolute
            payload["comments"]["comments"][0]["body"] = f"See {absolute}"
            payload["annotations"]["annotations"][0]["path"] = absolute

        fixture = connector_fixture(project, mutate)
        code, _, _ = collect_connector(project, fixture)
        assert code == 0
        artifact_blob = (
            (project / ".starforge" / "live" / TASK / "github" / "pr.json").read_text(encoding="utf-8")
            + (project / ".starforge" / "live" / TASK / "github" / "comments.json").read_text(encoding="utf-8")
            + (project / ".starforge" / "live" / TASK / "github" / "annotations.json").read_text(encoding="utf-8")
        )
        assert str(Path.home()) not in artifact_blob
        assert "[external]" in artifact_blob


def test_raw_hash_preservation_detects_artifact_mutation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        fixture = connector_fixture(project)
        code, _, manifest = collect_connector(project, fixture)
        assert code == 0
        payload = load_json(manifest)
        raw_hashes = payload["raw_artifact_hashes"]
        assert any(path.endswith("diff.patch") for path in raw_hashes), raw_hashes
        diff = project / ".starforge" / "live" / TASK / "github" / "diff.patch"
        diff.write_text("tampered\n", encoding="utf-8")
        assert_core_fails(project, manifest, "artifact-hash")


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
    print(f"\ntest_live_github_pr.py: {passed} passed, {len(failed)} failed, {len(tests)} total")
    if failed:
        print("failed tests: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
