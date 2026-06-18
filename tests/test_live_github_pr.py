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
    payload["tool_versions"] = {"github_connector": "1.4.0", "github_api": "2022-11-28"}
    payload["live_provenance"] = {
        "source": "github-connector-live",
        "repo": REPO,
        "pr": PR,
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
    write_json(target / "tool-versions.json", {"gh": "2.75.0", "github_api": "2022-11-28"})
    write_json(
        target / "provenance.json",
        {
            "source": "gh-readonly-live",
            "repo": REPO,
            "pr": PR,
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


def test_connector_input_emits_production_proof_commands_and_core_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        input_path = connector_input(project)
        code, out, manifest = collect_connector_input(project, input_path)
        assert code == 0, out
        assert "source-packet-github-pr-review" in out
        assert "source-packet-proof" in out
        payload = load_json(manifest)
        assert payload["summary"]["source"] == "github-connector-live"
        transcript = project / ".starforge" / "live" / TASK / "github" / "operation-transcript.json"
        assert transcript.exists()
        transcript_hash = github_pr.live_common.file_sha256(transcript)
        assert payload["summary"]["read_only_transcript_sha256"] == transcript_hash
        assert payload["summary"]["live_provenance"]["operation_transcript_sha256"] == transcript_hash
        assert payload["raw_artifact_hashes"][str(transcript.relative_to(project))]["sha256"] == transcript_hash
        assert_core_passes(project, manifest)


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
        assert code == 0
        logs = load_json(project / ".starforge" / "live" / TASK / "github" / "ci-log-excerpts.json")
        assert logs["logs"][0]["check_run_id"] == "501"
        assert logs["logs"][0]["captured_head_sha"] == "2222222222222222222222222222222222222222"
        assert_core_passes(project, manifest)


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
        assert_core_passes(project, manifest)


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
