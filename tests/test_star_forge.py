#!/usr/bin/env python3
"""Tests for the Star Forge v0.3 "Forge Loop" helper (scripts/star_forge.py).

Plain-python suite (no pytest). Run with: python3 tests/test_star_forge.py

Every test builds an isolated temp project. The suite never reads or writes
the user's real ~/.star-forge: STAR_FORGE_LEARNINGS_HOME is pointed at a
throwaway temp directory before any command runs.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "star_forge.py"
SPEC = importlib.util.spec_from_file_location("star_forge", SCRIPT)
assert SPEC and SPEC.loader
star_forge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(star_forge)

from live_collectors import browser_playwright
from live_collectors import common as live_common
from starforge import runtime_orchestration
from starforge import runtime_preview
from starforge import runtime_records
from starforge import runtime_review
from starforge import runtime_security
from starforge import runtime_support

# Isolate learnings from the real home for the whole suite (item: setup detail).
os.environ["STAR_FORGE_LEARNINGS_HOME"] = tempfile.mkdtemp(prefix="star-forge-test-learnings-")

# Real-looking secret material, assembled by concatenation so this test file
# itself never contains a literal the tree scanner would flag.
REAL_SK_KEY = "sk-" + "abc123def456ghi789jkl012mno345"
REAL_GHP_KEY = "ghp_" + "AbCdEf123456789012345678"
REAL_AKIA_KEY = "AKIA" + "IOSFODNN7EXAMPL3"
REAL_DB_CRED_LINE = "DATABASE_URL=" + "postgres://user:" + "realpassword123" + "@host/db"

APPROVED_BLUEPRINT = """# Blueprint.md

Status: approved
Owner: project team
Last approved: 2026-06-12

## Product Summary

Deliver a small command line greeter with verified output.

## Acceptance Criteria

- AC-1: greeting output is proven by an automated verification command.
"""

PLAN_HEADER = (
    "| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |\n"
    "|------|-------------|--------|------|-------|---------|--------|----------|\n"
)


# ------------------------------------------------------------------- harness


def run_cli(args: list[str], stdin_payload: dict | None = None) -> tuple[int, str, str]:
    """Run star_forge.main() in-process, capturing stdout/stderr (and feeding stdin)."""
    old_stdin = sys.stdin
    stdout = io.StringIO()
    stderr = io.StringIO()
    if stdin_payload is not None:
        sys.stdin = io.StringIO(json.dumps(stdin_payload))
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = star_forge.main(args)
    finally:
        sys.stdin = old_stdin
    return code, stdout.getvalue(), stderr.getvalue()


def init_project(project: Path, profile: str = "standard") -> None:
    args = ["init", "--project", str(project), "--no-agents"]
    if profile != "standard":
        args.extend(["--profile", profile])
    code, out, err = run_cli(args)
    assert code == 0, err or out


def write_blueprint(project: Path, text: str = APPROVED_BLUEPRINT) -> None:
    (project / "Blueprint.md").write_text(text, encoding="utf-8")


def write_plan(project: Path, rows: list[str]) -> None:
    body = "# Plan.md\n\n" + PLAN_HEADER + "".join(row.rstrip("\n") + "\n" for row in rows)
    (project / "Plan.md").write_text(body, encoding="utf-8")


def commit_all(project: Path, message: str = "test state") -> None:
    star_forge.ensure_git_repo(project)
    code, _, err = star_forge.run_git(["add", "."], project)
    assert code == 0, err
    code, _, err = star_forge.run_git(
        ["-c", "user.name=Star Forge Test", "-c", "user.email=starforge@example.com", "commit", "-m", message],
        project,
    )
    assert code == 0, err


def make_test_png(path: Path, width: int = 1280, height: int = 800) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + ihdr + b"\x00\x00\x00\x00")


# A non-trivial command that still passes deterministically in a temp project with
# no deps. `echo`/`true` are now rejected as trivially-passing no-ops, so every
# Verify cell and recorded verify run uses this exact string (they must match,
# since fresh_passing_verify binds the run to the task's declared Verify cell).
REAL_VERIFY = "python3 -c \"print('ok')\""


def record_verify(project: Path, task: str, command: str = REAL_VERIFY) -> dict:
    code, out, err = run_cli(["verify", "--project", str(project), "--task", task, "--command", command, "--strict"])
    assert code == 0, err or out
    return json.loads(out)


def record_passing_browser_run(project: Path, task: str) -> dict:
    shots = star_forge.live_common.live_collector_dir(project, task, "browser")
    url = "http://127.0.0.1:4173/"
    parsed_url, url_problems = browser_playwright.validate_url(url)
    assert not url_problems
    allowed_origin = browser_playwright.normalize_origin(parsed_url)
    desktop = shots / "desktop.png"
    mobile = shots / "mobile.png"
    make_test_png(desktop, 1280, 800)
    make_test_png(mobile, 390, 844)
    interaction = shots / "interaction.json"
    request = browser_playwright.browser_url_safety_evidence(url, allowed_local_origins=[allowed_origin])
    request.update({
        "method": "GET",
        "resource_type": "document",
        "navigation": True,
    })
    interaction.write_text(json.dumps({
        "ready": [{"passed": True}],
        "actions": [],
        "assertions": [],
        "request_safety": {
            "schema": "star-forge.browser-request-safety.v1",
            "service_workers": browser_playwright.SERVICE_WORKERS_MODE,
            "connection_control": browser_playwright.BROWSER_NETWORK_CONTROL_MODE,
            "websocket_routing": browser_playwright.WEBSOCKET_ROUTING_MODE,
            "allowed_local_origins": [allowed_origin],
            "requests": [request],
            "websockets": [],
            "final_urls": [request],
            "blocked_count": 0,
            "websocket_blocked_count": 0,
            "webrtc": {"mode": browser_playwright.WEBRTC_CONTROL_MODE, "init_script": True},
        },
    }) + "\n", encoding="utf-8")
    console = shots / "console.json"
    console.write_text(json.dumps({"events": []}) + "\n", encoding="utf-8")
    lease = shots / "server-lease.json"
    lease.write_text(json.dumps({
        "schema": "star-forge.server-lease.v1",
        "project": str(project),
        "origin": allowed_origin,
        "port": 4173,
        "pid": os.getpid(),
        "command": "python3 -m http.server 4173",
        "source_hash": star_forge.source_hash(project),
        "runtime_asset_hash": star_forge.live_common.compute_runtime_asset_hash(project, exclude_paths=[project / ".starforge" / "runtime" / "server.json"]),
    }) + "\n", encoding="utf-8")
    current_source = star_forge.source_hash(project)
    manifest = star_forge.live_common.write_live_manifest(
        project,
        task=task,
        collector="browser",
        command_argv=["test-browser-collector"],
        tool_versions={"test": "1"},
        artifacts={"desktop": desktop, "mobile": mobile, "interaction": interaction, "console": console},
        summary={"url": url},
        source_hash_before=current_source,
        source_hash_after=current_source,
        runtime_asset_hash=star_forge.live_common.compute_runtime_asset_hash(project),
    )
    args = argparse.Namespace(
        project=str(project),
        task=task,
        url=url,
        scenario="smoke",
        viewport=[
            f"desktop=1280x800:{desktop}",
            f"mobile=390x844:{mobile}",
        ],
        screenshot=None,
        interaction_evidence=[str(interaction)],
        console_evidence=[str(console)],
        live_manifest=str(manifest),
        require_viewports=True,
        require_interaction=True,
        require_console=True,
        server_lease=str(lease),
        require_server_lease=False,
        degraded=False,
        strict=True,
        summary="",
    )
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = star_forge.cmd_browser_run(args)
    payload = json.loads(stdout.getvalue())
    assert code == 0, payload
    assert payload["verdict"] == "PASS"
    return payload


def write_reviewer_findings(project: Path, role: str, findings: list[dict], source_hash: str | None = None, agent_id: str | None = None) -> Path:
    """Write a reviewer findings file. By default it attests the CURRENT source
    hash (a fresh review); pass source_hash explicitly to simulate a stale review."""
    scope = star_forge.scope_hash(project) or "noscope"
    path = star_forge.reviews_scope_dir(project, scope) / f"{role}.findings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {"role": role, "findings": findings}
    payload["source_hash"] = star_forge.source_hash(project) if source_hash is None else source_hash
    if agent_id is not None:
        payload["agent_id"] = agent_id
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_clean_review_wave(project: Path, source_hash: str | None = None) -> None:
    for role in star_forge.required_review_roles(project):
        write_reviewer_findings(project, role, [], source_hash=source_hash)


def write_merged_review_cache(project: Path, **overrides: object) -> Path:
    scope = star_forge.scope_hash(project) or "noscope"
    path = star_forge.reviews_scope_dir(project, scope) / "merged.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    roles = star_forge.required_review_roles(project)
    payload: dict[str, object] = {
        "schema": "star-forge.review.v2",
        "project": str(project),
        "scope": scope,
        "source_hash": star_forge.source_hash(project),
        "reviewer_roles": roles,
        "reviewer_count": len(roles),
        "required_review_roles": roles,
        "required_reviewer_count": len(roles),
        "missing_review_roles": [],
        "stale_roles": [],
        "reviewers_witnessed": False,
        "findings": [],
        "fix_queue": [],
        "waived": [],
        "file_problems": [],
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def tamper_manifest_profile(project: Path, profile: str) -> None:
    manifest_path = project / ".starforge" / "project.json"
    manifest = star_forge.read_json(manifest_path)
    manifest["profile"] = profile
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def write_raw_source_profile(project: Path, profile: str = "fast-mvp") -> Path:
    path = project / star_forge.SOURCE_PROFILE_FILE
    payload = {
        "schema": star_forge.SOURCE_PROFILE_SCHEMA,
        "profile": profile,
        "review_roles": star_forge.review_roles_for_profile(profile),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def reviewable_project(project: Path, profile: str = "standard") -> list[dict]:
    """Approved blueprint + one complete solo task; returns the parsed tasks."""
    init_project(project, profile=profile)
    if profile == "fast-mvp":
        commit_all(project, "fast mvp init before gates")
    write_blueprint(project)
    write_plan(project, [f"| SF-1 | Build the greeter module | complete | solo | src/hello.py | - | {REAL_VERIFY} | src/hello.py |"])
    src = project / "src" / "hello.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("print('hello forge')\n", encoding="utf-8")
    return star_forge.parse_tasks(project / "Plan.md")


def build_completed_project(
    project: Path,
    *,
    owned_files: str = "src/hello.py",
) -> None:
    """Full happy path up to (but not including) `done`: approve, build, verify, complete, review, commit."""
    init_project(project)
    write_blueprint(project)
    write_plan(project, [f"| SF-1 | Build the greeter module | ready | solo | {owned_files} | - | {REAL_VERIFY} | - |"])
    src = project / "src" / "hello.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("print('hello forge')\n", encoding="utf-8")
    record_verify(project, "SF-1")
    code, out, err = run_cli(["complete-task", "--project", str(project), "--task", "SF-1", "--changed-file", "src/hello.py"])
    assert code == 0, err or out
    record_verify(project, "SF-1")
    write_clean_review_wave(project)
    code, out, err = run_cli(["review", "--project", str(project), "--strict"])
    assert code == 0, err or out
    commit_all(project)


def run_done(project: Path) -> tuple[int, dict]:
    code, out, err = run_cli(["done", "--project", str(project), "--strict"])
    assert out.strip(), err
    return code, json.loads(out)


# ----------------------------------------------------------- 1. plan parsing


def test_parse_tasks_reads_forge_loop_columns() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plan = Path(tmp) / "Plan.md"
        plan.write_text(
            "# Plan\n\n" + PLAN_HEADER
            + "| SF-1 | Build the parser | ready | solo | src/a.py | - | pytest -q | - |\n",
            encoding="utf-8",
        )
        tasks = star_forge.parse_tasks(plan)
        assert len(tasks) == 1
        task = tasks[0]
        assert task["id"] == "SF-1"
        assert task["description"] == "Build the parser"
        assert task["status"] == "ready"
        assert task["mode"] == "solo"
        assert task["files"] == "src/a.py"
        assert task["depends"] == "-"
        assert task["verify"] == "pytest -q"
        assert task["evidence"] == "-"


def test_parse_tasks_mode_defaults_to_delegate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plan = Path(tmp) / "Plan.md"
        # Empty Mode cell and a table without a Mode column must both default.
        plan.write_text(
            "# Plan\n\n" + PLAN_HEADER
            + "| SF-1 | Build a slice | queued |  | src/a.py | - | pytest -q | - |\n"
            + "\n"
            + "| Task | Description | Status | Verify | Evidence |\n"
            + "|------|-------------|--------|--------|----------|\n"
            + "| SF-2 | Another slice | queued | pytest -q | - |\n",
            encoding="utf-8",
        )
        tasks = {task["id"]: task for task in star_forge.parse_tasks(plan)}
        assert tasks["SF-1"]["mode"] == "delegate"
        assert tasks["SF-2"]["mode"] == "delegate"
        assert star_forge.task_requires_real_workers(tasks["SF-1"])


def test_task_files_splits_on_commas_and_semicolons() -> None:
    assert star_forge.task_files({"files": "src/a.py, src/b.py; src/c.py"}) == ["src/a.py", "src/b.py", "src/c.py"]
    assert star_forge.task_files({"files": "-"}) == []
    assert star_forge.task_files({"files": ""}) == []
    assert star_forge.task_files({"files": "n/a"}) == []


def test_validate_tasks_flags_invalid_status_and_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plan = Path(tmp) / "Plan.md"
        plan.write_text(
            "# Plan\n\n" + PLAN_HEADER
            + "| T-1 | Wrong status | wip | solo | - | - | echo verified-ok | - |\n"
            + "| T-2 | Wrong mode | queued | yolo | - | - | echo verified-ok | - |\n",
            encoding="utf-8",
        )
        problems = star_forge.validate_tasks(star_forge.parse_tasks(plan))
        messages = " | ".join(item["message"] for item in problems)
        assert "invalid status `wip`" in messages
        assert "invalid mode `yolo`" in messages


def test_validate_tasks_requires_verify_and_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plan = Path(tmp) / "Plan.md"
        plan.write_text(
            "# Plan\n\n" + PLAN_HEADER
            + "| T-1 | Ready without verify | ready | solo | - | - |  | - |\n"
            + "| T-2 | Complete without evidence | complete | solo | - | - | echo verified-ok | - |\n"
            + "| T-3 | Docs task may noop | ready | docs | README.md | - |  | - |\n",
            encoding="utf-8",
        )
        problems = star_forge.validate_tasks(star_forge.parse_tasks(plan))
        by_task = {}
        for item in problems:
            by_task.setdefault(item["task"], []).append(item["message"])
        assert any("missing verify command" in msg for msg in by_task.get("T-1", []))
        assert any("requires evidence" in msg for msg in by_task.get("T-2", []))
        # docs mode tolerates a no-op verify cell.
        assert "T-3" not in by_task


def test_validate_tasks_flags_unknown_dependency() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plan = Path(tmp) / "Plan.md"
        plan.write_text(
            "# Plan\n\n" + PLAN_HEADER
            + "| T-1 | Depends on a ghost | queued | solo | - | T-404 | echo verified-ok | - |\n",
            encoding="utf-8",
        )
        problems = star_forge.validate_tasks(star_forge.parse_tasks(plan))
        assert any("unknown dependency `T-404`" in item["message"] for item in problems)


def test_ready_tasks_gates_on_completed_dependencies() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plan = Path(tmp) / "Plan.md"
        plan.write_text(
            "# Plan\n\n" + PLAN_HEADER
            + "| A | Done already | complete | solo | - | - | echo verified-ok | logs |\n"
            + "| B | Ready, dep met | ready | solo | - | A | echo verified-ok | - |\n"
            + "| C | Queued, dep unmet | queued | solo | - | B | echo verified-ok | - |\n"
            + "| D | In progress, never ready-listed | in_progress | solo | - | - | echo verified-ok | - |\n"
            + "| E | Queued, dep met | queued | solo | - | A | echo verified-ok | - |\n",
            encoding="utf-8",
        )
        ready = [task["id"] for task in star_forge.ready_tasks(star_forge.parse_tasks(plan))]
        assert ready == ["B", "E"]


def test_plan_parse_problem_flags_malformed_table() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plan = Path(tmp) / "Plan.md"
        # Task-like header but no |---| separator row: parses to zero tasks.
        plan.write_text("# Plan\n\n| Task | Status |\n| T-1 | ready |\n", encoding="utf-8")
        tasks = star_forge.parse_tasks(plan)
        assert tasks == []
        problem = star_forge.plan_parse_problem(plan, tasks)
        assert problem is not None and "did not parse" in problem
        # A well-formed plan reports no parse problem.
        plan.write_text("# Plan\n\n" + PLAN_HEADER + "| T-1 | Fine | ready | solo | - | - | echo verified-ok | - |\n", encoding="utf-8")
        tasks = star_forge.parse_tasks(plan)
        assert len(tasks) == 1
        assert star_forge.plan_parse_problem(plan, tasks) is None


# ------------------------------------------------------ 2. blueprint approval


def test_blueprint_approved_by_status_line_including_bold() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        write_blueprint(project, "# Blueprint\n\nStatus: approved\n")
        assert star_forge.blueprint_is_approved(project)
        write_blueprint(project, "# Blueprint\n\n**Status:** approved\n")
        assert star_forge.blueprint_is_approved(project)


def test_blueprint_last_approved_date_counts_as_approval() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        write_blueprint(project, "# Blueprint\n\nStatus: draft\nLast approved: 2026-06-12\n")
        assert star_forge.blueprint_is_approved(project)


def test_blueprint_not_approved_for_draft_or_placeholder_dates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        assert not star_forge.blueprint_is_approved(project)  # missing file
        write_blueprint(project, "# Blueprint\n\nStatus: draft\nLast approved: TBD\n")
        assert not star_forge.blueprint_is_approved(project)
        write_blueprint(project, "# Blueprint\n\nStatus: draft\nLast approved: not approved yet\n")
        assert not star_forge.blueprint_is_approved(project)


def test_scope_hash_requires_approval() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        write_blueprint(project, "# Blueprint\n\nStatus: draft\nLast approved: not approved yet\n")
        assert star_forge.scope_hash(project) is None
        write_blueprint(project)
        scope = star_forge.scope_hash(project)
        assert isinstance(scope, str) and len(scope) == 16


# ------------------------------------------------------- 3. SECRET_RE matrix


def test_secret_re_allows_placeholder_and_prose_values() -> None:
    allowed = [
        "OPENAI_API_KEY=your-key-here",
        "OPENAI_API_KEY=sk-your-key-here",
        "OPENAI_API_KEY=sk-proj-placeholder123",
        "OPENAI_API_KEY=<your-openai-api-key>",
        "OPENAI_API_KEY=${OPENAI_API_KEY}",
        "DATABASE_URL=postgres://localhost:5432/dev_db",
        "Set the OPENAI_API_KEY environment variable before starting the server.",
        "task-management",  # Boss Fight false positive: `sk-` inside a word.
    ]
    for sample in allowed:
        assert not star_forge.SECRET_RE.search(sample), f"false positive: {sample!r}"


def test_secret_re_denies_real_secret_material() -> None:
    denied = [
        "OPENAI_API_KEY=" + REAL_SK_KEY,
        REAL_SK_KEY,
        REAL_GHP_KEY,
        REAL_AKIA_KEY,
        REAL_DB_CRED_LINE,
    ]
    for sample in denied:
        assert star_forge.SECRET_RE.search(sample), f"missed secret: {sample!r}"


def test_scan_paths_flags_real_key_but_not_placeholder_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        hot = project / "config.py"
        hot.write_text("TOKEN = '" + REAL_SK_KEY + "'\n", encoding="utf-8")
        findings = star_forge.scan_paths([hot], project)
        assert any(item["rule"] == "secret-material" for item in findings)
        cool = project / "README.md"
        cool.write_text(
            "# Setup\n\nExport OPENAI_API_KEY=your-key-here before running.\n"
            "OPENAI_API_KEY=sk-proj-placeholder123 is only an example value.\n",
            encoding="utf-8",
        )
        assert star_forge.scan_paths([cool], project) == []


def test_secret_scan_catches_real_secret_in_dotenv_file() -> None:
    # Regression: is_text_file now treats literal `.env`/`.env.*` and key files as
    # scannable, so a real secret committed to a `.env` file is caught (it was
    # previously missed because Path('.env').suffix is '').
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        env_file = project / ".env"
        env_file.write_text("OPENAI_API_KEY=" + REAL_SK_KEY + "\n", encoding="utf-8")
        assert star_forge.is_text_file(env_file) is True
        findings = star_forge.scan_paths([env_file], project)
        assert any(item["rule"] == "secret-material" for item in findings)
        # The whole-tree scan used by the review wave catches it too.
        tree = star_forge.secret_scan_findings(project)
        assert any(item.get("role") == "tree-scan" and item.get("file") == ".env" for item in tree)


def test_source_hash_includes_clean_build_and_dependency_config_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_blueprint(project)
        write_plan(project, [f"| T1 | code | ready | solo | app.py | - | {REAL_VERIFY} | - |"])
        (project / "app.py").write_text("print('ok')\n", encoding="utf-8")
        cases = [
            ("Dockerfile", "FROM scratch\n", "FROM scratch\nLABEL version=2\n"),
            ("Makefile", "build:\n\t@echo one\n", "build:\n\t@echo two\n"),
            ("build.gradle.kts", "plugins { id(\"com.android.application\") version \"1.0\" }\n", "plugins { id(\"com.android.application\") version \"2.0\" }\n"),
            ("settings.gradle", "pluginManagement { repositories { google() } }\n", "pluginManagement { repositories { mavenCentral() } }\n"),
            ("gradle.properties", "org.gradle.jvmargs=-Xmx2g\n", "org.gradle.jvmargs=-Xmx4g\n"),
            ("go.work", "go 1.22\nuse ./app\n", "go 1.22\nuse ./app\nuse ./tools\n"),
            ("Pipfile", "[packages]\nrequests = \"==2.31.0\"\n", "[packages]\nrequests = \"==2.32.0\"\n"),
            ("poetry.lock", "package = []\nmetadata = { lock-version = \"2.0\" }\n", "package = []\nmetadata = { lock-version = \"2.1\" }\n"),
            ("uv.lock", "version = 1\n", "version = 2\n"),
            ("Gemfile", "source \"https://rubygems.org\"\ngem \"rack\", \"3.0.0\"\n", "source \"https://rubygems.org\"\ngem \"rack\", \"3.1.0\"\n"),
            ("Podfile", "platform :ios, \"17.0\"\npod \"AFNetworking\", \"4.0.0\"\n", "platform :ios, \"18.0\"\npod \"AFNetworking\", \"4.0.1\"\n"),
        ]
        for rel_path, before, after in cases:
            path = project / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(before, encoding="utf-8")
            commit_all(project, f"{rel_path} v1")
            hash_v1 = star_forge.source_hash(project)
            assert star_forge.live_common.compute_source_hash(project) == hash_v1

            path.write_text(after, encoding="utf-8")
            commit_all(project, f"{rel_path} v2")
            hash_v2 = star_forge.source_hash(project)
            assert hash_v2 != hash_v1, rel_path
            assert star_forge.live_common.compute_source_hash(project) == hash_v2


def test_source_hash_binds_executable_mode_in_dirty_and_clean_states() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        tool = project / "tool.sh"
        tool.write_text("#!/bin/sh\nprintf safe\n", encoding="utf-8")
        tool.chmod(0o644)
        commit_all(project, "regular tool")
        regular_hash = star_forge.source_hash(project)
        tool.chmod(0o755)
        executable_dirty_hash = star_forge.source_hash(project)
        assert executable_dirty_hash != regular_hash
        commit_all(project, "executable tool")
        assert star_forge.source_hash(project) != regular_hash


def test_source_hash_binds_gitlink_object_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        source = project / "app.py"
        source.write_text("print('one')\n", encoding="utf-8")
        commit_all(project, "dependency commit one")
        first_commit = star_forge.git_head(project)
        source.write_text("print('two')\n", encoding="utf-8")
        commit_all(project, "dependency commit two")
        second_commit = star_forge.git_head(project)
        first_hash = ""
        for commit in (first_commit, second_commit):
            code, _, error = star_forge.run_git(
                ["update-index", "--add", "--cacheinfo",
                 f"160000,{commit},vendor/dependency"], project)
            assert code == 0, error
            code, _, error = star_forge.run_git(
                ["-c", "user.name=Star Forge Test",
                 "-c", "user.email=starforge@example.com",
                 "commit", "-m", f"gitlink {commit[:8]}"], project)
            assert code == 0, error
            current = star_forge.source_hash(project)
            if commit == first_commit:
                first_hash = current
        assert current != first_hash


def test_source_hash_includes_tracked_files_under_ignored_generated_dirs() -> None:
    cases = [
        ("build/tracked.js", "console.log('build v1')\n", "console.log('build v2')\n"),
        ("dist/tracked.js", "console.log('dist v1')\n", "console.log('dist v2')\n"),
        ("target/tracked.rs", "fn main() { println!(\"target v1\"); }\n", "fn main() { println!(\"target v2\"); }\n"),
    ]
    for rel_path, before, after in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            write_blueprint(project)
            write_plan(project, [f"| T1 | code | ready | solo | app.py | - | {REAL_VERIFY} | - |"])
            (project / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (project / ".gitignore").write_text("build/\ndist/\ntarget/\n", encoding="utf-8")
            tracked = project / rel_path
            tracked.parent.mkdir(parents=True, exist_ok=True)
            tracked.write_text(before, encoding="utf-8")
            code, _, err = star_forge.run_git(["add", "-f", rel_path], project)
            assert code == 0, err
            commit_all(project, f"{rel_path} v1")
            hash_v1 = star_forge.source_hash(project)
            assert live_common.compute_source_hash(project) == hash_v1

            tracked.write_text(after, encoding="utf-8")
            commit_all(project, f"{rel_path} v2")
            hash_v2 = star_forge.source_hash(project)
            assert hash_v2 != hash_v1, rel_path
            assert live_common.compute_source_hash(project) == hash_v2


def test_git_path_consumers_preserve_special_filenames() -> None:
    names = (
        "café.py",
        "line\nbreak.py",
        "tab\tname.py",
        'quote"name.py',
        "back\\slash.py",
    )
    for name in names:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            special = project / name
            special.write_text("print('untracked v1')\n", encoding="utf-8")

            for status in (
                live_common.git_status(project),
                runtime_support.git_status(project),
            ):
                assert name in {
                    live_common.git_status_path(entry) for entry in status
                }, (name, status)

            untracked_v1 = star_forge.source_hash(project)
            special.write_text("print('untracked v2')\n", encoding="utf-8")
            untracked_v2 = star_forge.source_hash(project)
            assert untracked_v2 != untracked_v1, name
            assert name in {
                live_common.project_relative(project, path)
                for path in live_common.snapshot_file_candidates(project)
            }

            commit_all(project, f"track special path {name!r}")
            tracked_v1 = star_forge.source_hash(project)
            special.write_text("print('tracked v2')\n", encoding="utf-8")
            assert special in runtime_support.git_changed_files(project)
            assert name in {
                live_common.git_status_path(entry)
                for entry in runtime_support.git_status(project)
            }
            commit_all(project, f"change special path {name!r}")
            tracked_v2 = star_forge.source_hash(project)
            assert tracked_v2 != tracked_v1, name
            assert live_common.compute_source_hash(project) == tracked_v2


def test_git_path_enumeration_failures_do_not_produce_partial_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        commit_all(project)
        original_git_path_records = live_common.run_git_path_records

        def fail_source_listing(
            command: list[str], target: Path,
        ) -> tuple[int, list[str], str]:
            if command[:2] == ["ls-files", "-z"]:
                return 1, [], "simulated path listing failure"
            return original_git_path_records(command, target)

        with mock.patch.object(
            live_common, "run_git_path_records", side_effect=fail_source_listing,
        ):
            try:
                star_forge.source_hash(project)
            except ValueError as exc:
                assert "cannot enumerate Git paths" in str(exc)
            else:
                raise AssertionError("source hash accepted a failed Git path listing")

        def fail_changed_listing(
            command: list[str], target: Path,
        ) -> tuple[int, list[str], str]:
            if command[:3] == ["diff", "--name-only", "-z"]:
                return 1, [], "simulated changed path failure"
            return original_git_path_records(command, target)

        with mock.patch.object(
            live_common, "run_git_path_records", side_effect=fail_changed_listing,
        ):
            try:
                runtime_support.git_changed_files(project)
            except star_forge.ForgeError as exc:
                assert "Cannot enumerate changed Git paths" in str(exc)
            else:
                raise AssertionError("changed-file scan accepted a failed Git listing")

        with mock.patch.object(
            live_common, "git_status",
            side_effect=ValueError("Git status unavailable: simulated"),
        ):
            try:
                runtime_support.git_status(project)
            except ValueError as exc:
                assert "Git status unavailable" in str(exc)
            else:
                raise AssertionError("unavailable Git status was accepted")


def test_nested_state_named_sources_invalidate_review_and_completion_proof() -> None:
    nested_sources = (
        "src/.starforge/app.py",
        "packages/the-loop/index.ts",
    )
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_blueprint(project)
        write_plan(project, [
            f"| SF-1 | Build nested sources | ready | solo | src/, packages/ | - | {REAL_VERIFY} | - |",
        ])
        for name in nested_sources:
            path = project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("version one\n", encoding="utf-8")

        record_verify(project, "SF-1")
        code, payload = complete_task(
            project, "SF-1", changed=nested_sources[0],
        )
        assert code == 0, payload
        record_verify(project, "SF-1")
        write_clean_review_wave(project)
        code, out, err = run_cli([
            "review", "--project", str(project), "--strict",
        ])
        assert code == 0, err or out
        commit_all(project, "track nested state named sources")
        code, proof = run_done(project)
        assert code == 0, proof
        original_hash = proof["snapshot"]["source_hash"]
        assert {
            live_common.project_relative(project, path)
            for path in live_common.snapshot_file_candidates(project)
        }.issuperset(nested_sources)

        for name in nested_sources:
            (project / name).write_text("version two\n", encoding="utf-8")

        dirty = set(star_forge.source_dirty_entries(
            star_forge.git_status(project),
        ))
        assert dirty.issuperset(nested_sources)
        assert star_forge.source_hash(project) != original_hash
        tasks = star_forge.parse_tasks(project / "Plan.md")
        assert [
            item["rule"]
            for item in star_forge.review_findings_for_done(project, tasks)
        ] == ["review-stale"]
        assert runtime_review.proof_is_current(project, proof) is False


# --------------------------------------------------------------- 4. verify


def test_verify_pass_records_stdout_tail() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        # A non-trivial command (bare `echo` is now rejected) that still emits a
        # unique marker on stdout so we can assert the tail is captured.
        payload = record_verify(project, "SF-1", "python3 -c \"print('tail-marker-stdout-123')\"")
        assert payload["verdict"] == "PASS"
        assert "tail-marker-stdout-123" in payload["stdout_tail"]
        artifact = project / payload["artifact"]
        assert artifact.exists()
        assert json.loads(artifact.read_text(encoding="utf-8"))["kind"] == "verify-run"


def test_verify_redacts_terminal_and_stored_payload_while_command_digest_matches() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        command_secret = "commandvalue123456789"
        stdout_secret = REAL_SK_KEY
        summary_secret = REAL_AKIA_KEY
        signed_value = "amzsig123456789"
        stderr_text = (
            f"https://cdn.example.test/build?X-Amz-Signature={signed_value} "
            f"path={Path.home() / 'private' / 'token.txt'}"
        )
        script = (
            f"import sys; print({stdout_secret!r}); "
            f"print({stderr_text!r}, file=sys.stderr)"
        )
        command = (
            f"GITHUB_TOKEN={command_secret} python3 -c {shlex.quote(script)}"
        )
        write_plan(project, [
            f"| SF-1 | Verify secret handling | ready | solo | src/hello.py | - | {command} | - |"
        ])
        source = project / "src" / "hello.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("print('safe')\n", encoding="utf-8")

        code, out, err = run_cli([
            "verify", "--project", str(project), "--task", "SF-1",
            "--command", command, "--summary", f"summary {summary_secret}",
            "--strict",
        ])
        assert code == 0, err or out
        payload = json.loads(out)
        artifact = project / payload["artifact"]
        stored = json.loads(artifact.read_text(encoding="utf-8"))
        terminal_blob = out + err
        stored_blob = artifact.read_text(encoding="utf-8")
        state_blob = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (project / ".starforge").rglob("*") if path.is_file()
        )
        for secret in (
            command_secret, stdout_secret, summary_secret, signed_value,
            str(Path.home()),
        ):
            assert secret not in terminal_blob
            assert secret not in stored_blob
            assert secret not in state_blob
        assert stored == payload
        assert payload["redaction_report"]["secret_values"] >= 3
        assert payload["redaction_report"]["home_paths"] >= 1
        assert payload["command_digest"] == runtime_records.command_identity_digest(command)
        task = star_forge.parse_tasks(project / "Plan.md")[0]
        assert star_forge.fresh_passing_verify(project, task) is True


def test_verify_redacts_problem_before_terminal_and_storage() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        problem_secret = REAL_SK_KEY
        problem = {
            "severity": "high",
            "rule": "source-snapshot",
            "message": f"api-key={problem_secret}",
            "blocking": True,
        }
        snapshot = {"source_hash": star_forge.source_hash(project)}
        with mock.patch.object(
            runtime_records, "safe_release_snapshot",
            return_value=(snapshot, problem),
        ):
            code, out, err = run_cli([
                "verify", "--project", str(project), "--task", "SF-1",
                "--command", REAL_VERIFY, "--strict",
            ])
        assert code == 1, err or out
        payload = json.loads(out)
        stored = (project / payload["artifact"]).read_text(encoding="utf-8")
        assert problem_secret not in out + err
        assert problem_secret not in stored
        assert all(
            problem_secret not in path.read_text(
                encoding="utf-8", errors="replace"
            )
            for path in (project / ".starforge").rglob("*") if path.is_file()
        )
        assert "[REDACTED_SECRET]" in payload["problems"][0]["message"]
        assert payload["redaction_report"]["secret_values"] >= 1
        assert json.loads(stored) == payload


def test_verify_fail_returns_nonzero_in_strict_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        code, out, err = run_cli(["verify", "--project", str(project), "--task", "SF-1", "--command", "exit 7", "--strict"])
        assert code == 1, err or out
        payload = json.loads(out)
        assert payload["verdict"] == "FAIL"
        assert payload["returncode"] == 7


def test_verify_source_hash_unavailable_profile_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        (project / star_forge.SOURCE_PROFILE_FILE).write_text("{", encoding="utf-8")

        code, out, err = run_cli(["verify", "--project", str(project), "--task", "SF-1", "--command", REAL_VERIFY, "--strict"])
        assert code == 1, err or out
        payload = json.loads(out)
        assert payload["verdict"] == "FAIL"
        assert payload["returncode"] == 0
        assert payload["source_snapshot"]["source_hash_unavailable"] is True
        assert payload["problems"][0]["rule"] == "source-hash-unavailable"
        assert "not valid JSON" in payload["problems"][0]["message"]

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        profile_path = project / star_forge.SOURCE_PROFILE_FILE
        profile_path.write_text(json.dumps({"schema": star_forge.SOURCE_PROFILE_SCHEMA, "profile": "standard"}), encoding="utf-8")
        profile_path.chmod(0)
        try:
            code, out, err = run_cli(["verify", "--project", str(project), "--task", "SF-1", "--command", REAL_VERIFY, "--strict"])
            assert code == 1, err or out
            payload = json.loads(out)
            assert payload["verdict"] == "FAIL"
            assert payload["source_snapshot"]["source_hash_unavailable"] is True
            assert "not readable" in payload["problems"][0]["message"]
        finally:
            profile_path.chmod(0o644)


def test_verify_normalizes_snapshot_refusals_without_a_traceback() -> None:
    def assert_blocked(project: Path) -> None:
        code, out, err = run_cli([
            "verify", "--project", str(project), "--task", "SF-1",
            "--command", REAL_VERIFY, "--strict",
        ])
        assert code == 1, err or out
        payload = json.loads(out)
        assert payload["verdict"] == "FAIL"
        assert payload["returncode"] == 0
        assert payload["source_snapshot"]["source_hash_unavailable"] is True
        assert payload["problems"][0]["rule"] == "source-hash-unavailable"
        assert "Traceback" not in err

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        target = project / "target.py"
        target.write_text("value = 1\n", encoding="utf-8")
        (project / "linked.py").symlink_to(target.name)
        assert_blocked(project)

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        original_run_git = live_common.run_git

        def malformed_git_paths(
            args: list[str], cwd: Path,
        ) -> tuple[int, str, str]:
            if args[:2] == ["ls-files", "-z"]:
                return 0, "path-without-nul", ""
            return original_run_git(args, cwd)

        with mock.patch.object(
            live_common, "run_git", side_effect=malformed_git_paths,
        ):
            assert_blocked(project)


def test_verify_noop_refused_for_non_docs_task() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_plan(project, ["| SF-1 | Build the greeter module | ready | solo | src/hello.py | - | pytest -q | - |"])
        code, out, err = run_cli(["verify", "--project", str(project), "--task", "SF-1", "--noop"])
        assert code == 1
        assert "not eligible for no-op verification" in err


def test_verify_noop_allowed_for_docs_task() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_plan(project, ["| DOC-1 | Write the user guide | ready | docs | README.md | - | noop | - |"])
        code, out, err = run_cli(["verify", "--project", str(project), "--task", "DOC-1", "--noop", "--summary", "guide reviewed", "--strict"])
        assert code == 0, err or out
        payload = json.loads(out)
        assert payload["verdict"] == "PASS" and payload["noop"] is True
        assert star_forge.has_noop_verify(project, "DOC-1")


def test_noop_verify_becomes_stale_after_source_change() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_plan(
            project,
            [
                "| DOC-1 | Write the user guide | ready | docs | "
                "README.md | - | noop | - |"
            ],
        )
        readme = project / "README.md"
        readme.write_text("# Guide\n", encoding="utf-8")
        code, out, err = run_cli(
            [
                "verify",
                "--project",
                str(project),
                "--task",
                "DOC-1",
                "--noop",
                "--summary",
                "guide reviewed",
                "--strict",
            ]
        )
        assert code == 0, err or out
        assert star_forge.has_noop_verify(project, "DOC-1")

        readme.write_text("# Changed guide\n", encoding="utf-8")
        assert not star_forge.has_noop_verify(project, "DOC-1")
        code, payload = complete_task(project, "DOC-1", changed="README.md")
        assert code == 1
        assert any(
            item["rule"] == "verify-noop-missing"
            for item in payload["findings"]
        )


def test_fresh_passing_verify_flips_when_source_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_plan(project, [f"| SF-1 | Build the greeter module | ready | solo | src/hello.py | - | {REAL_VERIFY} | - |"])
        src = project / "src" / "hello.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("print('hello forge')\n", encoding="utf-8")
        # The recorded verify command must match the task's declared Verify cell;
        # record_verify uses REAL_VERIFY, the same string the row carries.
        record_verify(project, "SF-1")
        task = next(t for t in star_forge.parse_tasks(project / "Plan.md") if t["id"] == "SF-1")
        assert star_forge.fresh_passing_verify(project, task)
        (project / "src" / "extra.py").write_text("print('drift')\n", encoding="utf-8")
        assert not star_forge.fresh_passing_verify(project, task)


def test_verify_command_matching_preserves_case_and_quoted_whitespace() -> None:
    variants = (
        (
            "python3 -c \"assert 'A' == 'a'\"",
            "python3 -c \"assert 'a' == 'a'\"",
        ),
        (
            "python3 -c \"assert 'two  spaces' == 'two spaces'\"",
            "python3 -c \"assert 'two spaces' == 'two spaces'\"",
        ),
    )
    for declared, executed in variants:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            write_plan(project, [f"| SF-1 | Build greeter | ready | solo | src/hello.py | - | {declared} | - |"])
            source = project / "src" / "hello.py"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("print('hello forge')\n", encoding="utf-8")
            record_verify(project, "SF-1", executed)
            task = star_forge.parse_tasks(project / "Plan.md")[0]
            assert not star_forge.fresh_passing_verify(project, task)
            code, payload = complete_task(project, "SF-1")
            assert code == 1
            assert any(item["rule"] == "verify-stale" for item in payload["findings"])


# ----------------------------------------------------------- 5. complete-task


def complete_task(project: Path, task: str, changed: str = "src/hello.py") -> tuple[int, dict]:
    code, out, err = run_cli(["complete-task", "--project", str(project), "--task", task, "--changed-file", changed])
    assert out.strip(), err
    return code, json.loads(out)


def test_complete_task_refuses_without_fresh_verify() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_blueprint(project)
        write_plan(project, ["| SF-1 | Build the greeter module | ready | solo | src/hello.py | - | echo verified-ok | - |"])
        code, payload = complete_task(project, "SF-1")
        assert code == 1
        assert payload["verdict"] == "REFUSED" and payload["updated"] is False
        assert any(item["rule"] == "verify-stale" for item in payload["findings"])


def test_complete_task_refuses_non_completable_status() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_plan(project, ["| SF-1 | Build the greeter module | queued | solo | src/hello.py | - | echo verified-ok | - |"])
        code, payload = complete_task(project, "SF-1")
        assert code == 1
        assert any(item["rule"] == "task-status-not-completable" for item in payload["findings"])


def test_complete_task_refuses_unmet_dependencies() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_plan(
            project,
            [
                f"| SF-A | Build module a | ready | solo | src/a.py | - | {REAL_VERIFY} | - |",
                f"| SF-B | Build module b | ready | solo | src/b.py | SF-A | {REAL_VERIFY} | - |",
            ],
        )
        record_verify(project, "SF-B")
        code, payload = complete_task(project, "SF-B", changed="src/b.py")
        assert code == 1
        rules = {item["rule"] for item in payload["findings"]}
        assert "task-dependencies-incomplete" in rules


def test_complete_task_updates_plan_row_status_and_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_plan(project, [f"| SF-1 | Build the greeter module | ready | solo | src/hello.py | - | {REAL_VERIFY} | - |"])
        src = project / "src" / "hello.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("print('hello forge')\n", encoding="utf-8")
        record_verify(project, "SF-1")
        code, payload = complete_task(project, "SF-1")
        assert code == 0, payload
        assert payload["verdict"] == "COMPLETE" and payload["updated"] is True
        completion = json.loads(
            (project / ".starforge" / "state" / "complete-task-SF-1.json").read_text(
                encoding="utf-8"
            )
        )
        assert set(completion) == {
            "changed_files",
            "created_at",
            "schema",
            "source_snapshot",
            "summary",
            "task",
            "verdict",
        }
        assert completion["schema"] == "star-forge.complete-task.v1"
        assert completion["source_snapshot"]["source_hash"] == star_forge.source_hash(
            project
        )
        task = star_forge.parse_tasks(project / "Plan.md")[0]
        assert task["status"] == "complete"
        assert json.loads(task["evidence"]) == ["src/hello.py"]


def test_complete_task_preserves_exact_evidence_and_every_plan_row() -> None:
    changed_files = (
        "src/line\nbreak.py",
        "src/carriage\rreturn.py",
        os.fsdecode(b"src/nonutf8-\xff.py"),
    )
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_blueprint(project)
        write_plan(project, [
            f"| SF-1 | Build special paths | ready | solo | src/ | - | {REAL_VERIFY} | - |",
            f"| SF-2 | Keep the dependent row | ready | solo | tests/ | SF-1 | {REAL_VERIFY} | - |",
        ])
        for name in changed_files:
            if name == os.fsdecode(b"src/nonutf8-\xff.py"):
                continue
            path = project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("exact path evidence\n", encoding="utf-8")
        dependent = project / "tests" / "dependent.py"
        dependent.parent.mkdir(parents=True, exist_ok=True)
        dependent.write_text("print('dependent')\n", encoding="utf-8")

        record_verify(project, "SF-1")
        args = [
            "complete-task", "--project", str(project), "--task", "SF-1",
        ]
        for name in changed_files:
            args.extend(["--changed-file", name])
        code, out, err = run_cli(args)
        assert code == 0, err or out

        tasks = star_forge.parse_tasks(project / "Plan.md")
        assert [task["id"] for task in tasks] == ["SF-1", "SF-2"]
        assert json.loads(tasks[0]["evidence"]) == list(changed_files)
        assert tasks[1]["status"] == "ready"
        plan_bytes = (project / "Plan.md").read_bytes()
        assert b"\r" not in plan_bytes
        assert b"\xff" not in plan_bytes

        record_verify(project, "SF-2")
        code, payload = complete_task(
            project, "SF-2", changed="tests/dependent.py",
        )
        assert code == 0, payload
        record_verify(project, "SF-1")
        record_verify(project, "SF-2")
        write_clean_review_wave(project)
        code, out, err = run_cli([
            "review", "--project", str(project), "--strict",
        ])
        assert code == 0, err or out
        commit_all(project, "complete exact path evidence")
        code, proof = run_done(project)
        assert code == 0, proof
        assert proof["snapshot"]["source_hash"] == star_forge.source_hash(project)


def test_complete_task_visual_requires_passing_browser_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_plan(project, [f"| SF-2 | Build the dashboard UI page | ready | solo | src/page.html | - | {REAL_VERIFY} | - |"])
        page = project / "src" / "page.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("<html><body>dashboard</body></html>\n", encoding="utf-8")
        record_verify(project, "SF-2")
        code, payload = complete_task(project, "SF-2", changed="src/page.html")
        assert code == 1
        assert any(item["rule"] == "browser-run-missing" for item in payload["findings"])
        run = record_passing_browser_run(project, "SF-2")
        assert run["viewports"]["desktop"]["valid_image"] is True
        assert run["viewports"]["mobile"]["decoded_width"] == 390
        code, payload = complete_task(project, "SF-2", changed="src/page.html")
        assert code == 0, payload


def test_browser_proof_becomes_stale_after_visual_source_change() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_plan(
            project,
            [
                f"| SF-2 | Build the dashboard UI page | ready | solo | "
                f"src/page.html | - | {REAL_VERIFY} | - |"
            ],
        )
        page = project / "src" / "page.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("<html><body>first</body></html>\n", encoding="utf-8")
        record_verify(project, "SF-2")
        record_passing_browser_run(project, "SF-2")
        assert star_forge.passing_browser_runs(project, "SF-2")

        page.write_text("<html><body>changed</body></html>\n", encoding="utf-8")
        record_verify(project, "SF-2")
        assert star_forge.passing_browser_runs(project, "SF-2") == []
        code, payload = complete_task(project, "SF-2", changed="src/page.html")
        assert code == 1
        assert any(
            item["rule"] == "browser-run-missing"
            for item in payload["findings"]
        )


def test_complete_task_python_control_plane_does_not_require_browser_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_plan(
            project,
            [
                (
                    "| SF-3 | Enforce UX and browser review-policy floors | "
                    "ready | solo | scripts/starforge/review_policy.py, "
                    "scripts/star_forge.py, tests/test_v04_review_policy.py | "
                    f"- | {REAL_VERIFY} | - |"
                )
            ],
        )
        policy = project / "scripts" / "starforge" / "review_policy.py"
        policy.parent.mkdir(parents=True)
        policy.write_text("REVIEW_FLOOR = True\n", encoding="utf-8")
        cli = project / "scripts" / "star_forge.py"
        cli.write_text("from starforge import review_policy\n", encoding="utf-8")
        test = project / "tests" / "test_v04_review_policy.py"
        test.parent.mkdir(parents=True)
        test.write_text("def test_floor(): pass\n", encoding="utf-8")
        record_verify(project, "SF-3")
        code, payload = complete_task(
            project,
            "SF-3",
            changed="scripts/starforge/review_policy.py",
        )
        assert code == 0, payload
        assert payload["verdict"] == "COMPLETE"


def test_task_visual_classifier_exempts_browser_collector_infrastructure() -> None:
    task = {
        "description": "Adapt browser and preview proof with viewport tests",
        "files": (
            "scripts/live_collectors/browser_playwright.py, "
            "scripts/live_collectors/preview.py, "
            "tests/test_live_browser_playwright.py, tests/test_live_preview.py"
        ),
        "verify": "python3 tests/test_live_browser_playwright.py",
        "evidence": "scripts/live_collectors/browser_playwright.py",
        "plan_version": "legacy",
        "proof": "",
    }
    assert star_forge.task_files_are_infrastructure(task)
    assert not star_forge.task_is_visual(task)


def test_task_visual_classifier_ignores_ux_prose_for_python_control_plane() -> None:
    task = {
        "description": (
            "Enforce review floors so Fast MVP cannot omit UX or browser review"
        ),
        "files": (
            "scripts/starforge/review_policy.py, scripts/star_forge.py, "
            "tests/test_v04_review_policy.py"
        ),
        "verify": "python3 tests/test_v04_review_policy.py",
        "evidence": "scripts/starforge/review_policy.py",
        "plan_version": "legacy",
        "proof": "",
    }
    assert not star_forge.task_files_are_infrastructure(task)
    assert not star_forge.task_owns_visual_source(task)
    assert star_forge.task_files_are_python_control_plane(task)
    assert not star_forge.task_is_visual(task)


def test_task_visual_classifier_keeps_python_ui_source_visual() -> None:
    task = {
        "description": "Build a Python UI view",
        "files": "scripts/ui/dashboard.py, tests/test_dashboard.py",
        "verify": "python3 tests/test_dashboard.py",
        "evidence": "-",
        "plan_version": "legacy",
        "proof": "",
    }
    assert star_forge.task_owns_visual_source(task)
    assert not star_forge.task_files_are_python_control_plane(task)
    assert star_forge.task_is_visual(task)


def test_task_visual_classifier_keeps_legacy_app_prose_fallback() -> None:
    task = {
        "description": "Build the dashboard UX",
        "files": "src/app.py, tests/test_app.py",
        "verify": "python3 tests/test_app.py",
        "evidence": "-",
        "plan_version": "legacy",
        "proof": "",
    }
    assert not star_forge.task_owns_visual_source(task)
    assert not star_forge.task_files_are_python_control_plane(task)
    assert star_forge.task_is_visual(task)


def test_task_visual_classifier_keeps_mixed_app_ui_ownership_visual() -> None:
    task = {
        "description": "Update browser collector and dashboard",
        "files": "scripts/live_collectors/browser_playwright.py, frontend/pages/dashboard.tsx",
        "verify": "python3 tests/test_dashboard.py",
        "evidence": "-",
        "plan_version": "legacy",
        "proof": "",
    }
    assert not star_forge.task_files_are_infrastructure(task)
    assert star_forge.task_owns_visual_source(task)
    assert star_forge.task_is_visual(task)


def test_task_visual_classifier_prefers_plan_v2_browser_proof() -> None:
    task = {
        "description": "Exercise the release workflow",
        "files": "scripts/release_check.py",
        "verify": "python3 tests/test_release.py",
        "evidence": "-",
        "plan_version": "v2",
        "proof": "unit, browser",
    }
    assert star_forge.task_is_visual(task)


def test_task_visual_classifier_uses_plan_v2_nonbrowser_proof_for_non_ui_code() -> None:
    task = {
        "description": "Validate browser terminology in lifecycle output",
        "files": "scripts/starforge/lifecycle.py",
        "verify": "python3 tests/test_lifecycle.py",
        "evidence": "-",
        "plan_version": "v2",
        "proof": "unit, integration",
    }
    assert not star_forge.task_files_are_infrastructure(task)
    assert not star_forge.task_owns_visual_source(task)
    assert not star_forge.task_is_visual(task)


def test_browser_run_cli_accepts_summary_argument() -> None:
    # Regression: the `browser-run` subparser once lacked --summary while
    # cmd_browser_run read args.summary, so every CLI invocation crashed.
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        code, out, err = run_cli(
            ["browser-run", "--project", str(project), "--task", "SF-1", "--scenario", "smoke", "--summary", "cli smoke", "--no-require-viewports", "--no-require-interaction", "--no-require-console"]
        )
        assert code == 0, err or out
        payload = json.loads(out)
        assert payload["verdict"] == "PASS"
        assert payload["summary"] == "cli smoke"


def test_strict_browser_run_rejects_invalid_screenshot_arguments_even_with_manifest_hashes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        shots = star_forge.live_common.live_collector_dir(project, "SF-1", "browser")
        url = "http://127.0.0.1:4173/"
        parsed_url, url_problems = browser_playwright.validate_url(url)
        assert not url_problems
        allowed_origin = browser_playwright.normalize_origin(parsed_url)
        desktop = shots / "desktop.png"
        mobile = shots / "mobile.png"
        desktop.write_bytes(b"this is not a png")
        mobile.write_bytes(b"nor is this")
        lease = shots / "server-lease.json"
        lease.write_text(json.dumps({
            "schema": "star-forge.server-lease.v1",
            "project": str(project),
            "origin": allowed_origin,
            "port": 4173,
            "pid": os.getpid(),
            "command": "python3 -m http.server 4173",
            "source_hash": star_forge.source_hash(project),
            "runtime_asset_hash": star_forge.live_common.compute_runtime_asset_hash(project, exclude_paths=[project / ".starforge" / "runtime" / "server.json"]),
        }) + "\n", encoding="utf-8")
        current_source = star_forge.source_hash(project)
        manifest = star_forge.live_common.write_live_manifest(
            project,
            task="SF-1",
            collector="browser",
            command_argv=["test-browser-collector"],
            tool_versions={"test": "1"},
            artifacts={"desktop": desktop, "mobile": mobile},
            summary={"url": url},
            source_hash_before=current_source,
            source_hash_after=current_source,
            runtime_asset_hash=star_forge.live_common.compute_runtime_asset_hash(project),
        )
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        assert manifest_payload["raw_artifact_hashes"][star_forge.relative_to_project(desktop, project)]["sha256"] == star_forge.file_sha256(desktop)
        assert manifest_payload["raw_artifact_hashes"][star_forge.relative_to_project(mobile, project)]["sha256"] == star_forge.file_sha256(mobile)

        args = argparse.Namespace(
            project=str(project),
            task="SF-1",
            url=url,
            scenario="smoke",
            viewport=None,
            screenshot=[str(desktop), str(mobile)],
            interaction_evidence=[],
            console_evidence=[],
            live_manifest=str(manifest),
            require_viewports=True,
            require_interaction=False,
            require_console=False,
            server_lease=str(lease),
            require_server_lease=False,
            degraded=False,
            strict=True,
            summary="",
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = star_forge.cmd_browser_run(args)
        payload = json.loads(stdout.getvalue())
        assert code == 1
        assert payload["verdict"] == "FAIL"
        screenshot_problems = [item for item in payload["problems"] if item["rule"] == "screenshot"]
        assert len(screenshot_problems) == 2
        assert all("not a decodable PNG/JPEG image" in item["message"] for item in screenshot_problems)


def test_command_is_noop_canonicalizer() -> None:
    # Adversarial no-op detection: these unconditionally-succeed commands must be
    # rejected as verifications, and real commands must pass through untouched.
    noops = [
        "true", "(true)", "{ true; }", "true && true", "true; :", "/usr/bin/true",
        "exit 0", "return 0", "echo done", "printf x", "cat /dev/null", "sleep 0",
        "true #comment", "command -v node", "command true", "type node",
        "which python3", "hash node", "[[ 1 = 1 ]]", "[[ -e . ]]", "CI=true true",
        "eval true", "bash -c 'exit 0'", "sh -c :", "test 1 -eq 1", 'test -z ""',
        "(true && true) || true", "builtin true",
    ]
    reals = [
        "npm test", "npm ci && npm test", "pytest -q", "go test ./...", "make test",
        "test -f dist/app.js", "[ -f dist/app.js ]", "test -d node_modules",
        "./gradlew test", "cargo test", "tsc --noEmit && npm test", "python3 -m pytest",
        "npm run build && node dist/index.js --selftest", "true && pytest",
    ]
    missed = [c for c in noops if not star_forge.command_is_noop(c)]
    false_pos = [c for c in reals if star_forge.command_is_noop(c)]
    assert not missed, f"no-ops not caught: {missed}"
    assert not false_pos, f"real commands wrongly flagged: {false_pos}"


def test_empty_or_noop_verify_cell_blocks_completion() -> None:
    # A non-docs task with an empty or no-op Verify cell is not completable, even
    # if some passing verify run was recorded with an unrelated command.
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_blueprint(project)
        write_plan(project, ["| T1 | code | ready | solo | app.py | - |  | - |"])
        (project / "app.py").write_text("print(1)\n", encoding="utf-8")
        run_cli(["verify", "--project", str(project), "--task", "T1", "--command", "python3 -c \"print('x')\""])
        code, out, err = run_cli(["complete-task", "--project", str(project), "--task", "T1", "--changed-file", "app.py"])
        assert code == 1
        assert json.loads(out)["verdict"] == "REFUSED"
        # validate-plan flags the empty cell as high.
        code, out, err = run_cli(["validate-plan", "--project", str(project), "--strict"])
        assert code == 1
        assert any("verify" in p["message"].lower() for p in json.loads(out)["problems"])


def test_corrupt_fix_queue_fails_closed() -> None:
    # A hand-corrupted merged.json with non-dict fix_queue entries must fail closed
    # by recomputing reviewer evidence, not by trusting the cache.
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        scope = star_forge.scope_hash(project) or "noscope"
        merged = star_forge.reviews_scope_dir(project, scope) / "merged.json"
        merged.parent.mkdir(parents=True, exist_ok=True)
        merged.write_text(json.dumps({
            "scope": scope, "source_hash": star_forge.source_hash(project),
            "reviewer_roles": ["c"], "stale_roles": [], "file_problems": [],
            "fix_queue": ["corrupt-string-entry", None], "findings": [], "waived": [],
        }), encoding="utf-8")
        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["review-empty"]


# ------------------------------------------------------------ 6. review wave


def test_review_not_performed_blocks_done_gate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        findings = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in findings] == ["review-not-performed"]


def test_standard_review_strict_requires_all_profile_reviewers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        write_reviewer_findings(project, "correctness", [])
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        merged = json.loads(out)
        assert merged["required_review_roles"] == ["correctness", "security", "architecture"]
        assert merged["required_reviewer_count"] == 3
        assert merged["missing_review_roles"] == ["security", "architecture"]
        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["reviewer-count-insufficient"]


def test_fast_mvp_review_strict_accepts_single_reviewer() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project, profile="fast-mvp")
        assert (project / star_forge.SOURCE_PROFILE_FILE).exists()
        assert star_forge.project_profile(project) == "fast-mvp"
        assert star_forge.read_source_profile(project) == "fast-mvp"
        assert star_forge.review_profile(project) == "fast-mvp"
        write_reviewer_findings(project, "correctness", [])
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 0, err or out
        merged = json.loads(out)
        assert merged["review_profile"] == "fast-mvp"
        assert merged["required_review_roles"] == ["correctness"]
        assert merged["required_reviewer_count"] == 1
        assert merged["missing_review_roles"] == []
        assert star_forge.review_findings_for_done(project, tasks) == []


def test_committed_fast_mvp_source_profile_before_gates_keeps_fast_mvp_review_roles() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project, profile="fast-mvp")
        commit_all(project, "fast mvp init before gates")
        write_blueprint(project)
        write_plan(project, [f"| SF-1 | Build the greeter module | complete | solo | src/hello.py | - | {REAL_VERIFY} | src/hello.py |"])
        src = project / "src" / "hello.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("print('hello forge')\n", encoding="utf-8")
        (project / ".starforge" / "ledger.jsonl").write_text("", encoding="utf-8")

        assert star_forge.project_profile(project) == "fast-mvp"
        assert star_forge.read_source_profile(project) == "fast-mvp"
        assert star_forge.review_profile(project) == "fast-mvp"
        assert star_forge.required_review_roles(project) == ["correctness"]


def test_explicit_fast_mvp_rerun_writes_blocked_profile_lock_after_gates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        code, out, err = run_cli(["run", "--project", str(project), "--fast-mvp", "--objective", "Build the greeter", "--no-hooks"])
        assert code == 0, err or out
        initial = star_forge.read_json(project / ".starforge" / "state.json")
        assert initial["phase"] == "intake"
        assert initial["profile_lock"]["status"] == "pending"
        assert initial["profile_lock"]["effective_review_profile"] == "standard"
        assert "Commit StarForge.profile.json" in initial["profile_lock"]["next_action"]

        write_blueprint(project)
        write_plan(project, [f"| SF-1 | Build the greeter module | ready | solo | src/hello.py | - | {REAL_VERIFY} | - |"])

        code, out, err = run_cli(["run", "--project", str(project), "--fast-mvp", "--strict", "--no-hooks"])
        assert code == 1, out
        assert "Refusing to downgrade review profile from standard to fast-mvp" not in err
        state = star_forge.read_json(project / ".starforge" / "state.json")
        lock = state["profile_lock"]
        assert state["phase"] == "blocked"
        assert lock["status"] == "blocked"
        assert initial["profile_lock"]["status"] == "pending"
        assert initial["profile_lock"]["next_action"] != lock["next_action"]
        assert lock["manifest_profile"] == "fast-mvp"
        assert lock["source_profile"] == "fast-mvp"
        assert lock["effective_review_profile"] == "standard"
        assert "Blueprint.md is approved" in lock["gate_reasons"]
        assert "Plan.md has real tasks" in lock["gate_reasons"]
        assert "Standard review remains required" in lock["message"]
        assert "Commit only StarForge.profile.json" in lock["next_action"]
        assert "PROFILE: Fast-mvp was selected before gates" in out
        assert "Standard review remains required" in out
        assert star_forge.required_review_roles(project) == ["correctness", "security", "architecture"]

        code, status_out, err = run_cli(["status", "--project", str(project)])
        assert code == 0, err or status_out
        status = json.loads(status_out)
        assert status["profile_lock"]["status"] == "blocked"
        assert status["profile_lock"]["effective_review_profile"] == "standard"


def test_missing_fast_mvp_source_profile_rerun_writes_current_profile_lock_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        code, out, err = run_cli(["run", "--project", str(project), "--fast-mvp", "--objective", "Build the greeter", "--no-hooks"])
        assert code == 0, err or out
        initial = star_forge.read_json(project / ".starforge" / "state.json")
        assert initial["profile_lock"]["status"] == "pending"
        assert initial["profile_lock"]["effective_review_profile"] == "standard"

        (project / star_forge.SOURCE_PROFILE_FILE).unlink()
        write_blueprint(project)
        write_plan(project, [f"| SF-1 | Build the greeter module | ready | solo | src/hello.py | - | {REAL_VERIFY} | - |"])

        code, out, err = run_cli(["run", "--project", str(project), "--fast-mvp", "--strict", "--no-hooks"])
        assert code == 1, out
        assert "Refusing to downgrade review profile from standard to fast-mvp" not in err
        state = star_forge.read_json(project / ".starforge" / "state.json")
        lock = state["profile_lock"]
        assert lock["status"] == "standard-required"
        assert lock["status"] != initial["profile_lock"]["status"]
        assert lock["manifest_profile"] == "fast-mvp"
        assert lock["source_profile"] is None
        assert lock["effective_review_profile"] == "standard"
        assert lock["selected_before_gates"] is False
        assert "Blueprint.md is approved" in lock["gate_reasons"]
        assert "Plan.md has real tasks" in lock["gate_reasons"]
        assert f"{star_forge.SOURCE_PROFILE_FILE} is missing" in lock["problems"]
        assert "Standard review remains required" in lock["message"]
        assert "PROFILE: Fast-mvp is recorded without durable pre-gate proof" in out
        assert star_forge.required_review_roles(project) == ["correctness", "security", "architecture"]


def test_unreadable_fast_mvp_source_profile_review_run_writes_profile_lock_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        reviewable_project(project, profile="fast-mvp")
        profile_path = project / star_forge.SOURCE_PROFILE_FILE
        profile_path.chmod(0)
        try:
            code, out, err = run_cli(["run", "--project", str(project), "--strict", "--no-hooks"])
            assert code == 1, out
            assert "PermissionError" not in err
            state_path = project / ".starforge" / "state.json"
            assert state_path.exists()
            state = star_forge.read_json(state_path)
            lock = state["profile_lock"]
            assert state["phase"] == "blocked"
            assert state["fast_mvp"] is False
            assert state["spawn_plan"] == []
            assert state["drift"]["source_hash_unavailable"] is True
            assert state["review"]["source_hash_unavailable"] is True
            assert lock["status"] == "blocked"
            assert lock["manifest_profile"] == "fast-mvp"
            assert lock["source_profile"] is None
            assert lock["effective_review_profile"] == "standard"
            assert f"{star_forge.SOURCE_PROFILE_FILE} is not readable" in lock["problems"]
            assert "Standard review remains required" in lock["message"]
            assert "Restore read permission" in lock["next_action"]
            assert "PROFILE: Fast-mvp cannot be proven" in out
            assert star_forge.required_review_roles(project) == ["correctness", "security", "architecture"]
        finally:
            profile_path.chmod(0o644)


def test_standard_run_strict_damaged_profile_blocks_before_review_spawn() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        reviewable_project(project)
        (project / star_forge.SOURCE_PROFILE_FILE).write_text("{", encoding="utf-8")

        code, out, err = run_cli(["run", "--project", str(project), "--strict", "--no-hooks"])
        assert code == 1, err or out
        assert "spawn_agent starforge-reviewer" not in out
        state = star_forge.read_json(project / ".starforge" / "state.json")
        assert state["phase"] == "blocked"
        assert state["spawn_plan"] == []
        assert state["source_hash_unavailable"] is True
        assert state["source_hash_problems"][0]["rule"] == "source-hash-unavailable"
        assert "not valid JSON" in state["source_hash_problems"][0]["message"]
        assert state["drift"]["source_hash_unavailable"] is True
        assert state["review"]["source_hash_unavailable"] is True


def test_fast_mvp_source_profile_write_rejects_symlink_escape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        project = root / "project"
        project.mkdir()
        init_project(project)
        outside = root / "outside-profile.json"
        outside.write_text("outside\n", encoding="utf-8")
        profile_path = project / star_forge.SOURCE_PROFILE_FILE
        profile_path.symlink_to(outside)

        code, out, err = run_cli(["run", "--project", str(project), "--fast-mvp", "--no-hooks"])
        assert code == 1, out
        payload = json.loads(err)
        assert payload["schema"] == "star-forge.error.v1"
        assert "Refusing to write" in payload["error"]
        assert "symlink" in payload["error"]
        assert outside.read_text(encoding="utf-8") == "outside\n"


def test_review_strict_unreadable_fast_mvp_source_profile_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        reviewable_project(project, profile="fast-mvp")
        write_reviewer_findings(project, "correctness", [])
        profile_path = project / star_forge.SOURCE_PROFILE_FILE
        profile_path.chmod(0)
        try:
            code, out, err = run_cli(["review", "--project", str(project), "--strict"])
            assert code == 1, out
            assert "PermissionError" not in err
            payload = json.loads(out)
            assert payload["source_hash_unavailable"] is True
            assert payload["review_profile"] == "standard"
            assert payload["profile_lock"]["status"] == "blocked"
            assert payload["fix_queue"][0]["rule"] == "source-hash-unavailable"
            assert star_forge.SOURCE_PROFILE_FILE in payload["fix_queue"][0]["message"]

            code, status_out, err = run_cli(["status", "--project", str(project)])
            assert code == 0, err or status_out
            status = json.loads(status_out)
            assert status["source_hash_unavailable"] is True
            assert status["review"]["source_hash_unavailable"] is True
            assert status["profile_lock"]["effective_review_profile"] == "standard"
        finally:
            profile_path.chmod(0o644)


def test_review_strict_invalid_fast_mvp_source_profile_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        reviewable_project(project, profile="fast-mvp")
        write_reviewer_findings(project, "correctness", [])
        (project / star_forge.SOURCE_PROFILE_FILE).write_text("{", encoding="utf-8")

        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        payload = json.loads(out)
        assert payload["source_hash_unavailable"] is True
        assert payload["review_profile"] == "standard"
        assert payload["profile_lock"]["status"] == "blocked"
        assert "not valid JSON" in payload["fix_queue"][0]["message"]


def test_done_strict_unreadable_fast_mvp_source_profile_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        reviewable_project(project, profile="fast-mvp")
        write_clean_review_wave(project)
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 0, err or out
        profile_path = project / star_forge.SOURCE_PROFILE_FILE
        profile_path.chmod(0)
        try:
            code, out, err = run_cli(["done", "--project", str(project), "--strict"])
            assert code == 1, out
            assert "PermissionError" not in err
            payload = json.loads(out)
            assert payload["verdict"] == "NEEDS_CHANGES"
            assert payload["source_hash_unavailable"] is True
            assert payload["snapshot"]["source_hash_unavailable"] is True
            assert payload["drift"]["source_hash_unavailable"] is True
            assert any(star_forge.SOURCE_PROFILE_FILE in item["message"] for item in payload["problems"])
        finally:
            profile_path.chmod(0o644)


def test_standard_approved_project_rejects_fast_mvp_downgrade_via_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        reviewable_project(project)
        manifest_path = project / ".starforge" / "project.json"
        assert star_forge.read_json(manifest_path)["profile"] == "standard"

        code, out, err = run_cli(["run", "--project", str(project), "--fast-mvp", "--no-hooks"])
        assert code == 1, out
        assert "Refusing to downgrade review profile from standard to fast-mvp" in err
        assert "Blueprint.md is approved" in err
        assert "Plan.md exists" in err
        assert star_forge.read_json(manifest_path)["profile"] == "standard"
        assert not (project / star_forge.SOURCE_PROFILE_FILE).exists()


def test_standard_project_manifest_tamper_cannot_bypass_security_or_architecture_review_roles() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        manifest_path = project / ".starforge" / "project.json"
        manifest = star_forge.read_json(manifest_path)
        manifest["profile"] = "fast-mvp"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        assert star_forge.project_profile(project) == "fast-mvp"
        assert star_forge.read_source_profile(project) == ""
        assert star_forge.review_profile(project) == "standard"

        write_reviewer_findings(project, "correctness", [])
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        merged = json.loads(out)
        assert merged["manifest_profile"] == "fast-mvp"
        assert merged["source_profile"] is None
        assert merged["review_profile"] == "standard"
        assert merged["required_review_roles"] == ["correctness", "security", "architecture"]
        assert merged["missing_review_roles"] == ["security", "architecture"]

        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["reviewer-count-insufficient"]
        assert "security" in gate[0]["message"]
        assert "architecture" in gate[0]["message"]


def test_manifest_tamper_then_run_fast_mvp_cannot_seed_source_profile_or_drop_review_roles() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        manifest_path = project / ".starforge" / "project.json"
        manifest = star_forge.read_json(manifest_path)
        manifest["profile"] = "fast-mvp"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        code, out, err = run_cli(["run", "--project", str(project), "--fast-mvp", "--no-hooks"])
        assert code == 1, out
        assert "Refusing to downgrade review profile from standard to fast-mvp" in err
        assert "Blueprint.md is approved" in err
        assert "Plan.md exists" in err
        assert not (project / star_forge.SOURCE_PROFILE_FILE).exists()
        assert star_forge.project_profile(project) == "fast-mvp"
        assert star_forge.read_source_profile(project) == ""
        assert star_forge.review_profile(project) == "standard"

        write_reviewer_findings(project, "correctness", [])
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        merged = json.loads(out)
        assert merged["manifest_profile"] == "fast-mvp"
        assert merged["source_profile"] is None
        assert merged["review_profile"] == "standard"
        assert merged["required_review_roles"] == ["correctness", "security", "architecture"]
        assert merged["missing_review_roles"] == ["security", "architecture"]

        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["reviewer-count-insufficient"]
        assert "security" in gate[0]["message"]
        assert "architecture" in gate[0]["message"]


def test_manifest_and_source_profile_tamper_then_run_fast_mvp_cannot_drop_review_roles() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        manifest_path = project / ".starforge" / "project.json"
        manifest = star_forge.read_json(manifest_path)
        manifest["profile"] = "fast-mvp"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        star_forge.ensure_source_profile(project, "fast-mvp")
        gate_time = max((project / "Blueprint.md").stat().st_mtime, (project / "Plan.md").stat().st_mtime)
        os.utime(project / star_forge.SOURCE_PROFILE_FILE, (gate_time + 1, gate_time + 1))

        assert star_forge.project_profile(project) == "fast-mvp"
        assert star_forge.read_source_profile(project) == "fast-mvp"
        assert star_forge.review_profile(project) == "standard"

        code, out, err = run_cli(["run", "--project", str(project), "--fast-mvp", "--no-hooks"])
        assert code == 1, out
        assert "Refusing to downgrade review profile from standard to fast-mvp" in err
        assert "Blueprint.md is approved" in err
        assert "Plan.md exists" in err
        assert star_forge.project_profile(project) == "fast-mvp"
        assert star_forge.read_source_profile(project) == "fast-mvp"
        assert star_forge.review_profile(project) == "standard"

        write_reviewer_findings(project, "correctness", [])
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        merged = json.loads(out)
        assert merged["manifest_profile"] == "fast-mvp"
        assert merged["source_profile"] == "fast-mvp"
        assert merged["review_profile"] == "standard"
        assert merged["required_review_roles"] == ["correctness", "security", "architecture"]
        assert merged["missing_review_roles"] == ["security", "architecture"]

        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["reviewer-count-insufficient"]
        assert "security" in gate[0]["message"]
        assert "architecture" in gate[0]["message"]


def test_forged_setup_ledger_cannot_prove_fast_mvp_before_gates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        tamper_manifest_profile(project, "fast-mvp")
        profile_path = write_raw_source_profile(project, "fast-mvp")
        profile_payload = star_forge.read_json(profile_path)
        profile_payload["selected_before_gates"] = True
        profile_path.write_text(json.dumps(profile_payload), encoding="utf-8")
        ledger_record = {
            "schema": "star-forge.ledger.v1",
            "timestamp": "2026-01-01T00:00:00Z",
            "event": "setup",
            "summary": "Forged local setup",
            "profile": "fast-mvp",
            "profile_selected_before_gates": True,
            "source_profile_sha256": star_forge.file_sha256(profile_path),
            "artifacts": ["Blueprint.md", "Plan.md", ".starforge/ledger.jsonl"],
        }
        (project / ".starforge" / "ledger.jsonl").write_text(json.dumps(ledger_record) + "\n", encoding="utf-8")

        assert star_forge.project_profile(project) == "fast-mvp"
        assert star_forge.read_source_profile(project) == "fast-mvp"
        assert star_forge.review_profile(project) == "standard"
        assert star_forge.required_review_roles(project) == ["correctness", "security", "architecture"]

        write_reviewer_findings(project, "correctness", [])
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        merged = json.loads(out)
        assert merged["manifest_profile"] == "fast-mvp"
        assert merged["source_profile"] == "fast-mvp"
        assert merged["review_profile"] == "standard"
        assert merged["required_review_roles"] == ["correctness", "security", "architecture"]
        assert merged["missing_review_roles"] == ["security", "architecture"]

        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["reviewer-count-insufficient"]
        assert "security" in gate[0]["message"]
        assert "architecture" in gate[0]["message"]


def test_backdated_untracked_source_profile_cannot_forge_fast_mvp_review_roles() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        tamper_manifest_profile(project, "fast-mvp")
        profile_path = write_raw_source_profile(project, "fast-mvp")
        gate_time = min((project / "Blueprint.md").stat().st_mtime, (project / "Plan.md").stat().st_mtime)
        os.utime(profile_path, (gate_time - 60, gate_time - 60))

        assert star_forge.project_profile(project) == "fast-mvp"
        assert star_forge.read_source_profile(project) == "fast-mvp"
        assert star_forge.review_profile(project) == "standard"
        assert star_forge.required_review_roles(project) == ["correctness", "security", "architecture"]

        write_reviewer_findings(project, "correctness", [])
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        merged = json.loads(out)
        assert merged["manifest_profile"] == "fast-mvp"
        assert merged["source_profile"] == "fast-mvp"
        assert merged["review_profile"] == "standard"
        assert merged["required_review_roles"] == ["correctness", "security", "architecture"]
        assert merged["missing_review_roles"] == ["security", "architecture"]

        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["reviewer-count-insufficient"]
        assert "security" in gate[0]["message"]
        assert "architecture" in gate[0]["message"]


def test_late_committed_source_profile_cannot_forge_fast_mvp_review_roles() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        commit_all(project, "standard gated project")
        tamper_manifest_profile(project, "fast-mvp")
        write_raw_source_profile(project, "fast-mvp")
        commit_all(project, "late fast mvp profile")

        assert star_forge.project_profile(project) == "fast-mvp"
        assert star_forge.read_source_profile(project) == "fast-mvp"
        assert star_forge.review_profile(project) == "standard"
        assert star_forge.required_review_roles(project) == ["correctness", "security", "architecture"]

        write_reviewer_findings(project, "correctness", [])
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        merged = json.loads(out)
        assert merged["manifest_profile"] == "fast-mvp"
        assert merged["source_profile"] == "fast-mvp"
        assert merged["review_profile"] == "standard"
        assert merged["required_review_roles"] == ["correctness", "security", "architecture"]
        assert merged["missing_review_roles"] == ["security", "architecture"]

        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["reviewer-count-insufficient"]
        assert "security" in gate[0]["message"]
        assert "architecture" in gate[0]["message"]


def test_temporary_gate_removal_cannot_forge_fast_mvp_review_roles() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_blueprint(project)
        write_plan(project, [f"| SF-1 | Build the greeter module | complete | solo | src/hello.py | - | {REAL_VERIFY} | src/hello.py |"])
        src = project / "src" / "hello.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("print('hello forge')\n", encoding="utf-8")
        commit_all(project, "standard gates")

        write_blueprint(project, "# Blueprint.md\n\nStatus: draft\n")
        write_plan(project, [])
        write_raw_source_profile(project, "fast-mvp")
        commit_all(project, "temporarily remove gates and add fast mvp")

        write_blueprint(project)
        write_plan(project, [f"| SF-1 | Build the greeter module | complete | solo | src/hello.py | - | {REAL_VERIFY} | src/hello.py |"])
        tamper_manifest_profile(project, "fast-mvp")
        commit_all(project, "restore gates with fast mvp manifest")
        tasks = star_forge.parse_tasks(project / "Plan.md")

        assert star_forge.project_profile(project) == "fast-mvp"
        assert star_forge.read_source_profile(project) == "fast-mvp"
        assert star_forge.git_history_has_fast_mvp_before_gates(project) is False
        assert star_forge.review_profile(project) == "standard"
        assert star_forge.required_review_roles(project) == ["correctness", "security", "architecture"]

        write_reviewer_findings(project, "correctness", [])
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        merged = json.loads(out)
        assert merged["review_profile"] == "standard"
        assert merged["required_review_roles"] == ["correctness", "security", "architecture"]
        assert merged["missing_review_roles"] == ["security", "architecture"]

        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["reviewer-count-insufficient"]
        assert "security" in gate[0]["message"]
        assert "architecture" in gate[0]["message"]


def test_done_gate_recomputes_review_roles_after_fast_mvp_profile_switch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project, profile="fast-mvp")
        write_reviewer_findings(project, "correctness", [])
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 0, err or out
        merged = json.loads(out)
        assert merged["required_review_roles"] == ["correctness"]
        assert star_forge.review_findings_for_done(project, tasks) == []

        star_forge.ensure_project_manifest(project, profile="standard")
        assert star_forge.review_profile(project) == "standard"
        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["review-stale"]

        write_clean_review_wave(project)
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 0, err or out
        merged = json.loads(out)
        assert merged["required_review_roles"] == ["correctness", "security", "architecture"]
        assert merged["missing_review_roles"] == []
        assert star_forge.review_findings_for_done(project, tasks) == []


def test_review_with_empty_findings_counts_as_performed() -> None:
    # Regression: a clean review (empty findings array) must NOT fire review-empty.
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        write_clean_review_wave(project)
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 0, err or out
        merged = json.loads(out)
        assert merged["reviewer_roles"] == ["architecture", "correctness", "security"]
        assert merged["fix_queue"] == []
        assert star_forge.review_findings_for_done(project, tasks) == []


def test_blocking_finding_opens_fix_queue_and_done_needs_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        write_reviewer_findings(
            project,
            "correctness",
            [{"severity": "high", "file": "src/hello.py", "line": 1, "title": "Greeting text is wrong", "detail": "Output mismatch"}],
        )
        write_reviewer_findings(project, "security", [])
        write_reviewer_findings(project, "architecture", [])
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["review-fix-queue-open"]
        done_code, payload = run_done(project)
        assert done_code == 1
        assert payload["verdict"] == "NEEDS_CHANGES"
        assert any("review-fix-queue-open" in str(item.get("message")) for item in payload["problems"])


def test_waive_with_reason_clears_fix_queue() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        write_reviewer_findings(
            project,
            "correctness",
            [{"severity": "high", "file": "src/hello.py", "line": 1, "title": "Greeting text is wrong", "detail": "Output mismatch"}],
        )
        write_reviewer_findings(project, "security", [])
        write_reviewer_findings(project, "architecture", [])
        run_cli(["review", "--project", str(project)])
        code, out, err = run_cli(["waive", "--project", str(project), "--finding", "F-1", "--reason", "intended copy per Blueprint AC-1"])
        assert code == 0, err or out
        assert json.loads(out)["open_findings"] == 0
        assert star_forge.review_findings_for_done(project, tasks) == []
        incidents = (project / ".starforge" / "state" / "incidents.jsonl").read_text(encoding="utf-8")
        assert '"kind": "waive"' in incidents


def test_waiver_cannot_follow_positional_id_to_unrelated_finding() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        reviewable_project(project)
        original = {
            "severity": "high",
            "file": "src/hello.py",
            "line": 1,
            "title": "Greeting text is wrong",
            "detail": "Output mismatch",
        }
        write_reviewer_findings(project, "correctness", [original])
        write_reviewer_findings(project, "security", [])
        write_reviewer_findings(project, "architecture", [])
        code, out, err = run_cli(["review", "--project", str(project)])
        assert code == 0, err or out
        first = json.loads(out)["fix_queue"][0]
        code, out, err = run_cli(
            [
                "waive",
                "--project",
                str(project),
                "--finding",
                first["id"],
                "--reason",
                "accepted wording",
            ]
        )
        assert code == 0, err or out
        waiver = json.loads(
            (project / star_forge.WAIVES_FILE).read_text(encoding="utf-8")
        )
        assert waiver["fingerprint"] == first["fingerprint"]
        assert waiver["source_hash"] == first.get(
            "reviewed_source_hash",
            star_forge.source_hash(project),
        )

        source = project / "src" / "hello.py"
        source.write_text("print('new behavior')\n", encoding="utf-8")
        unrelated = {
            "severity": "high",
            "file": "src/other.py",
            "line": 40,
            "title": "Unrelated crash in worker",
            "detail": "The worker raises on empty input",
        }
        write_reviewer_findings(project, "correctness", [unrelated])
        write_reviewer_findings(project, "security", [])
        write_reviewer_findings(project, "architecture", [])
        scope = star_forge.scope_hash(project) or "noscope"
        merged = star_forge.merge_review(project, scope)
        replacement = next(
            item
            for item in merged["findings"]
            if item["title"] == unrelated["title"]
        )
        assert replacement["id"] == first["id"]
        assert replacement["waived"] is False
        assert replacement in merged["fix_queue"]


def test_waiver_identity_cannot_collide_across_exact_locations_and_details() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        reviewable_project(project)
        original = {
            "severity": "high",
            "file": "src/hello.py",
            "line": 1,
            "title": "Hardcoded secret material",
            "detail": "A token is embedded in the source",
        }
        replacement = {
            "severity": "high",
            "file": "src/hello.py",
            "line": 2,
            "title": "Hardcoded credential material",
            "detail": "A password is embedded in the source",
        }
        write_reviewer_findings(project, "correctness", [original])
        write_reviewer_findings(project, "security", [])
        write_reviewer_findings(project, "architecture", [])
        code, out, err = run_cli(["review", "--project", str(project)])
        assert code == 0, err or out
        first = json.loads(out)["fix_queue"][0]
        code, out, err = run_cli([
            "waive", "--project", str(project), "--finding", first["id"],
            "--reason", "accepted exact occurrence",
        ])
        assert code == 0, err or out
        write_reviewer_findings(project, "correctness", [replacement])
        merged = star_forge.merge_review(project, star_forge.scope_hash(project) or "noscope")
        second = next(item for item in merged["findings"] if item["title"] == replacement["title"])
        assert first["fingerprint"] != second["fingerprint"]
        assert second["waived"] is False
        assert second in merged["fix_queue"]


def test_review_goes_stale_after_source_change() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        write_clean_review_wave(project)
        run_cli(["review", "--project", str(project)])
        assert star_forge.review_findings_for_done(project, tasks) == []
        (project / "src" / "extra.py").write_text("print('post-review drift')\n", encoding="utf-8")
        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["review-stale"]


def test_review_freshness_attested_survives_ledger_reset_and_no_livelock() -> None:
    # Review freshness is keyed on the source_hash each reviewer ATTESTS in its own
    # file. After a source edit, a stale reviewer file (declaring the old hash) is
    # not refreshed by re-running `review` OR by deleting merged.json (the old
    # content-ledger reset bypass); and a clean re-review at the new hash is fresh
    # even with byte-identical findings (no livelock).
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        v1 = star_forge.source_hash(project)
        write_clean_review_wave(project, source_hash=v1)
        run_cli(["review", "--project", str(project)])  # source v1: passes
        assert star_forge.review_findings_for_done(project, tasks) == []
        # Edit a source file -> the attested hash (v1) no longer matches the tree.
        (project / "src" / "extra.py").write_text("print('drift')\n", encoding="utf-8")
        assert [item["rule"] for item in star_forge.review_findings_for_done(project, tasks)] == ["review-stale"]
        # The ledger-reset bypass: delete merged.json, re-run review. STILL stale,
        # because the attestation lives in the reviewer file, not merged.json.
        scope = star_forge.scope_hash(project) or "noscope"
        (star_forge.reviews_scope_dir(project, scope) / "merged.json").unlink()
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        merged = json.loads(out)
        assert merged["reviewer_roles"] == []
        assert merged["stale_roles"] == ["architecture", "correctness", "security"]
        assert [item["rule"] for item in star_forge.review_findings_for_done(project, tasks)] == ["review-stale"]
        # A clean re-review attesting the NEW hash is fresh even though its findings
        # are byte-identical-in-spirit (still empty) -> no livelock.
        v2 = star_forge.source_hash(project)
        assert v2 != v1
        write_clean_review_wave(project, source_hash=v2)
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 0, err or out
        assert json.loads(out)["reviewer_roles"] == ["architecture", "correctness", "security"]
        assert star_forge.review_findings_for_done(project, tasks) == []


def test_done_gate_recomputes_reviewer_files_despite_edited_merged_cache() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        scope = star_forge.scope_hash(project) or "noscope"
        write_reviewer_findings(project, "correctness", [], source_hash="old-source-hash")
        write_merged_review_cache(project)

        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["review-stale"]

        write_reviewer_findings(project, "correctness", [])
        write_merged_review_cache(project)
        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["reviewer-count-insufficient"]
        assert "security" in gate[0]["message"]
        assert "architecture" in gate[0]["message"]

        scope_dir = star_forge.reviews_scope_dir(project, scope)
        (scope_dir / "security.findings.json").write_text(
            json.dumps({"role": "security", "findings": []}), encoding="utf-8"
        )
        write_merged_review_cache(project)
        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["review-findings-invalid"]
        assert "source_hash" in gate[0]["message"]


def test_findings_file_without_source_hash_is_rejected_clearly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        scope = star_forge.scope_hash(project) or "noscope"
        scope_dir = star_forge.reviews_scope_dir(project, scope)
        scope_dir.mkdir(parents=True, exist_ok=True)
        (scope_dir / "correctness.findings.json").write_text(
            json.dumps({"role": "correctness", "findings": []}), encoding="utf-8"
        )

        files, problems = star_forge.load_review_findings(project, scope)
        assert files == []
        assert [item["rule"] for item in problems] == ["review-findings-shape"]
        assert "source_hash" in problems[0]["message"]

        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        merged = json.loads(out)
        assert merged["reviewer_roles"] == []
        assert "source_hash" in merged["file_problems"][0]["message"]
        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["review-findings-invalid"]
        assert "source_hash" in gate[0]["message"]


def test_findings_file_role_contract_mismatches_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        scope = star_forge.scope_hash(project) or "noscope"
        scope_dir = star_forge.reviews_scope_dir(project, scope)
        write_clean_review_wave(project)
        current = star_forge.source_hash(project)
        (scope_dir / "security.findings.json").write_text(
            json.dumps({"role": "correctness", "source_hash": current, "findings": []}), encoding="utf-8"
        )
        (scope_dir / "bogus.findings.json").write_text(
            json.dumps({"role": "qa", "source_hash": current, "findings": []}), encoding="utf-8"
        )

        files, problems = star_forge.load_review_findings(project, scope)
        assert {item["role"] for item in files} == {"architecture", "correctness"}
        messages = [item["message"] for item in problems]
        assert any("must match payload role" in message for message in messages)
        assert any("not a known review role" in message for message in messages)

        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        merged = json.loads(out)
        assert merged["file_problems"]
        assert "security" in merged["missing_review_roles"]
        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["review-findings-invalid"]


def test_malformed_findings_files_report_problems() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        scope = star_forge.scope_hash(project) or "noscope"
        scope_dir = star_forge.reviews_scope_dir(project, scope)
        scope_dir.mkdir(parents=True, exist_ok=True)
        (scope_dir / "broken.findings.json").write_text("{not json", encoding="utf-8")
        (scope_dir / "shapeless.findings.json").write_text(json.dumps({"role": "shapeless", "findings": "nope"}), encoding="utf-8")
        # load_review_findings now returns (files, problems): the malformed files
        # contribute no usable findings and are surfaced as file_problems instead.
        files, problems = star_forge.load_review_findings(project, scope)
        assert files == []
        rules = {item["rule"] for item in problems}
        assert rules == {"review-findings-invalid", "review-findings-shape"}
        # The problems must BLOCK, not be silently dropped: a strict review exits
        # nonzero and the done gate reports review-findings-invalid.
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        merged = json.loads(out)
        assert merged["file_problems"], "malformed files must be recorded as file_problems"
        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["review-findings-invalid"]
        commit_all(project)
        done_code, payload = run_done(project)
        assert done_code == 1
        assert any("review-findings-invalid" in str(item.get("message")) for item in payload["problems"])


def test_malformed_dict_finding_does_not_pass_done() -> None:
    # Regression: a findings file whose `findings` array contains a non-object
    # entry (dict-shaped but a string here) must register a file_problem and
    # block done because the malformed entry cannot be smuggled past the gate.
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        scope = star_forge.scope_hash(project) or "noscope"
        scope_dir = star_forge.reviews_scope_dir(project, scope)
        scope_dir.mkdir(parents=True, exist_ok=True)
        # A real reviewer file alongside one malformed entry (a non-object finding).
        write_reviewer_findings(project, "correctness", [])
        (scope_dir / "bad.findings.json").write_text(
            json.dumps({"role": "security", "findings": ["not-an-object"]}), encoding="utf-8"
        )
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 1, err or out
        gate = star_forge.review_findings_for_done(project, tasks)
        assert [item["rule"] for item in gate] == ["review-findings-invalid"]
        commit_all(project)
        done_code, payload = run_done(project)
        assert done_code == 1
        assert any("review-findings-invalid" in str(item.get("message")) for item in payload["problems"])


def test_reviewers_agreeing_findings_dedup_into_one() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        reviewable_project(project)
        shared = {"severity": "high", "file": "src/hello.py", "line": 12, "title": "Greeting crashes on empty name", "detail": "IndexError"}
        write_reviewer_findings(project, "correctness", [shared])
        write_reviewer_findings(project, "security", [shared])
        scope = star_forge.scope_hash(project) or "noscope"
        merged = star_forge.merge_review(project, scope)
        matches = [item for item in merged["findings"] if item["title"] == shared["title"]]
        assert len(matches) == 1
        assert matches[0]["agreed_by"] == ["correctness", "security"]
        assert len(merged["fix_queue"]) == 1


def test_role_specific_duplicate_variants_share_a_presentation_group() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        reviewable_project(project)
        variants = {
            "correctness": {
                "severity": "high",
                "file": "src/hello.py",
                "line": 12,
                "title": "Greeting crashes when user is missing",
                "detail": "Null user path raises AttributeError before rendering.",
            },
            "architecture": {
                "severity": "high",
                "file": "src/hello.py",
                "line": 13,
                "title": "Guard clause is missing around greeting input",
                "detail": "Null user path raises AttributeError before rendering.",
            },
            "security": {
                "severity": "high",
                "file": "src/hello.py",
                "line": 12,
                "title": "Missing validation exposes the greeting flow",
                "detail": "Null user path raises AttributeError before rendering.",
            },
        }
        for role, finding in variants.items():
            write_reviewer_findings(project, role, [finding])
        scope = star_forge.scope_hash(project) or "noscope"
        merged = star_forge.merge_review(project, scope)
        assert len(merged["findings"]) == 3
        assert len(merged["fix_queue"]) == 3
        assert len({item["fingerprint"] for item in merged["findings"]}) == 3
        assert len({item["presentation_group"] for item in merged["findings"]}) == 1


def test_review_dedupe_preserves_max_severity_for_later_blocking_duplicate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        reviewable_project(project)
        low = {
            "severity": "low",
            "file": "src/hello.py",
            "line": 12,
            "title": "Greeting empty user guard is missing",
            "detail": "Null user path raises AttributeError before rendering.",
        }
        high = {
            "severity": "high",
            "file": "src/hello.py",
            "line": 12,
            "title": "Greeting empty user guard is missing",
            "detail": "Null user path raises AttributeError before rendering.",
        }
        write_reviewer_findings(project, "correctness", [low], agent_id="review-low")
        write_reviewer_findings(project, "security", [high], agent_id="review-high")
        scope = star_forge.scope_hash(project) or "noscope"
        merged = star_forge.merge_review(project, scope)
        assert len(merged["findings"]) == 1
        finding = merged["findings"][0]
        assert finding["severity"] == "high"
        assert finding["agreed_by"] == ["correctness", "security"]
        assert {"role": "correctness", "agent_id": "review-low", "severity": "low"} in finding["role_details"]
        assert {"role": "security", "agent_id": "review-high", "severity": "high"} in finding["role_details"]
        assert len(merged["fix_queue"]) == 1
        assert merged["fix_queue"][0]["severity"] == "high"


def test_simultaneous_fuzzy_findings_require_independent_waivers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        reviewable_project(project)
        findings = [
            {
                "severity": "high",
                "file": "src/hello.py",
                "line": 1,
                "title": "Hardcoded API token",
                "detail": "A production token is embedded in the request client",
            },
            {
                "severity": "high",
                "file": "src/hello.py",
                "line": 2,
                "title": "Credential exposed in debug output",
                "detail": "A password is printed by the debug logger",
            },
        ]
        write_reviewer_findings(project, "correctness", findings)
        write_reviewer_findings(project, "security", [])
        write_reviewer_findings(project, "architecture", [])
        code, out, err = run_cli([
            "review", "--project", str(project), "--strict",
        ])
        assert code == 1, err or out
        merged = json.loads(out)
        assert len(merged["fix_queue"]) == 2
        assert len({item["fingerprint"] for item in merged["fix_queue"]}) == 2
        assert len({item["presentation_group"] for item in merged["fix_queue"]}) == 1

        first = next(
            item for item in merged["fix_queue"]
            if item["title"] == findings[0]["title"]
        )
        code, out, err = run_cli([
            "waive", "--project", str(project), "--finding", first["id"],
            "--reason", "accept API test token only",
        ])
        assert code == 0, err or out
        assert json.loads(out)["open_findings"] == 1
        after = star_forge.merge_review(
            project, star_forge.scope_hash(project) or "noscope"
        )
        remaining = [
            item for item in after["fix_queue"]
            if item["title"] == findings[1]["title"]
        ]
        assert len(remaining) == 1
        assert remaining[0]["waived"] is False


# ----------------------------------------------- 7. done + proof + amend loop


def test_plan_v2_cannot_cover_drift_with_new_completed_amend_row() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        plan = project / "Plan.md"
        header = (
            "# Plan.md\n\n"
            "| Task | Description | Status | Mode | Files | Depends | ACs | Proof | Verify | Evidence |\n"
            "|------|-------------|--------|------|-------|---------|-----|-------|--------|----------|\n"
        )
        root = f"| SF-1 | Build greeter | complete | solo | src/hello.py | - | AC-1 | automated-test | {REAL_VERIFY} | verified |\n"
        plan.write_text(header + root, encoding="utf-8")
        source = project / "src" / "hello.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("print('hello forge')\n", encoding="utf-8")
        commit_all(project, "complete modern plan before drift")
        proof_head = star_forge.git_head(project)
        star_forge.write_json(project / star_forge.PROOF_FILE, {
            "schema": "star-forge.proof.v1",
            "created_at": "2026-07-26T00:00:00+00:00",
            "head": proof_head,
            "source_hash": star_forge.source_hash(project),
            "scope_hash": "noscope",
            "verdict": "COMPLETE",
        })
        amend = f"| AMEND-1 | Cover drift | complete | solo | src/hello.py | SF-1 | AC-1 | automated-test | {REAL_VERIFY} | verified |\n"
        plan.write_text(header + root + amend, encoding="utf-8")
        tasks = star_forge.parse_tasks(plan)
        assert all(task["plan_version"] == "v2" for task in tasks)
        current = star_forge.source_hash(project)
        star_forge.write_json(
            project / ".starforge" / "state" / "complete-task-amend-1.json",
            {
                "verdict": "COMPLETE",
                "task": "AMEND-1",
                "source_snapshot": {"source_hash": current},
            },
        )
        covered = runtime_review.completed_amendment_covering_drift(
            project, tasks, {"detected": True}
        )
        assert covered is None


def test_done_happy_path_writes_complete_proof() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        build_completed_project(project)
        code, payload = run_done(project)
        assert code == 0, payload
        assert payload["verdict"].startswith("COMPLETE")
        assert payload["is_complete"] is True
        proof = json.loads((project / ".starforge" / "final" / "proof.json").read_text(encoding="utf-8"))
        assert proof["schema"] == "star-forge.proof.v1"
        assert proof["head"] == star_forge.git_head(project)
        assert proof["source_hash"] == star_forge.source_hash(project)
        assert proof["scope_hash"] == star_forge.scope_hash(project)
        assert set(proof) == {
            "created_at",
            "head",
            "schema",
            "scope_hash",
            "source_hash",
            "verdict",
        }
        assert set(payload) == {
            "counts",
            "created_at",
            "delivery",
            "drift",
            "enforcement",
            "foundation",
            "is_complete",
            "problems",
            "project",
            "schema",
            "snapshot",
            "source_hash_unavailable",
            "task_count",
            "verdict",
            "witness",
        }


def test_git_status_failure_is_not_a_clean_commit_or_proof_binding() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        commit_all(project)
        with mock.patch.object(
            live_common, "git_status",
            side_effect=ValueError("Git status unavailable: simulated"),
        ):
            head = star_forge.git_head(project)
            assert head
            try:
                runtime_support.git_status(project)
            except ValueError as exc:
                assert "Git status unavailable" in str(exc)
            else:
                raise AssertionError("unavailable Git status was accepted")
            assert runtime_support.stable_clean_commit_binding(project, head) is False
            assert not runtime_preview.deployment_bound_to_current(
                project, {"commit_sha": head}
            )
            assert not runtime_security.source_binding_is_fresh(
                project, {"commit_sha": head}
            )


def test_commit_binding_consumers_reject_repository_loss_during_probe() -> None:
    consumers = (
        lambda project, head: runtime_preview.deployment_bound_to_current(
            project, {"commit_sha": head}
        ),
        lambda project, head: runtime_security.source_binding_is_fresh(
            project, {"commit_sha": head}
        ),
    )
    for consumer in consumers:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            commit_all(project)
            head = star_forge.git_head(project)
            original_status = live_common.git_status
            def remove_repository_after_status(target: Path) -> list[str]:
                entries = original_status(target)
                shutil.rmtree(target / ".git")
                return entries
            with mock.patch.object(
                live_common, "git_status",
                side_effect=remove_repository_after_status,
            ):
                assert not consumer(project, head)


def test_commit_binding_consumers_reject_source_or_head_change_between_probes() -> None:
    consumers = (
        lambda project, head: runtime_preview.deployment_bound_to_current(
            project, {"commit_sha": head}
        ),
        lambda project, head: runtime_security.source_binding_is_fresh(
            project, {"commit_sha": head}
        ),
    )
    for consumer in consumers:
        for change_head in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp).resolve()
                init_project(project)
                source = project / "source.py"
                source.write_text("before\n", encoding="utf-8")
                commit_all(project)
                head = star_forge.git_head(project)
                original_is_repo = live_common.is_git_repo
                probes = 0
                def change_before_second_probe(target: Path) -> bool:
                    nonlocal probes
                    probes += 1
                    if probes == 4:
                        if change_head:
                            code, _, error = star_forge.run_git([
                                "-c", "user.name=Star Forge Test",
                                "-c", "user.email=starforge@example.com",
                                "commit", "--allow-empty", "-m", "binding race",
                            ], target)
                            assert code == 0, error
                        else:
                            source.write_text("after\n", encoding="utf-8")
                    return original_is_repo(target)
                with mock.patch.object(
                    live_common, "is_git_repo",
                    side_effect=change_before_second_probe,
                ):
                    assert not consumer(project, head)
                assert probes >= 4


def test_done_fails_closed_when_git_status_is_unavailable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        build_completed_project(project)
        with mock.patch.object(
            live_common, "git_status",
            side_effect=ValueError("Git status unavailable: simulated"),
        ):
            assert star_forge.git_head(project)
            code, payload = run_done(project)

        assert code == 1, payload
        assert payload["is_complete"] is False
        assert any(
            "Git status unavailable" in str(problem)
            for problem in payload["problems"]
        )


def test_done_fails_closed_when_repository_disappears_during_status_and_preserves_prior_proof() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        build_completed_project(project)
        code, payload = run_done(project)
        assert code == 0, payload
        proof_path = project / star_forge.PROOF_FILE
        prior_proof = proof_path.read_bytes()
        original_status = runtime_review.git_status
        removed = False

        def remove_repository_before_status(target: Path) -> list[str]:
            nonlocal removed
            if not removed:
                shutil.rmtree(target / ".git")
                removed = True
            return original_status(target)

        with mock.patch.object(
            runtime_review, "git_status",
            side_effect=remove_repository_before_status,
        ):
            code, payload = run_done(project)

        assert code == 1, payload
        assert payload["is_complete"] is False
        assert any(
            problem.get("rule") == "git-repository-required"
            for problem in payload["problems"]
        )
        assert proof_path.read_bytes() == prior_proof


def test_done_rejects_commits_during_final_source_hash_and_preserves_prior_proof() -> None:
    for empty_commit in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            build_completed_project(project)
            code, payload = run_done(project)
            assert code == 0, payload
            proof_path = project / star_forge.PROOF_FILE
            prior_proof = proof_path.read_bytes()
            original_done_payload = runtime_review.done_payload
            original_try_source_hash = runtime_review.try_source_hash
            armed = raced = False

            def arm_after_gates(target: Path) -> dict:
                nonlocal armed
                result = original_done_payload(target)
                armed = True
                return result

            def commit_at_final_hash(
                target: Path,
            ) -> tuple[str | None, dict | None]:
                nonlocal raced
                if armed and not raced:
                    if empty_commit:
                        code, _, error = star_forge.run_git([
                            "-c", "user.name=Star Forge Test",
                            "-c", "user.email=starforge@example.com",
                            "commit", "--allow-empty", "-m",
                            "empty race during proof publication",
                        ], target)
                        assert code == 0, error
                    else:
                        (target / "src" / "hello.py").write_text(
                            "print('changed during proof publication')\n",
                            encoding="utf-8",
                        )
                        commit_all(target, "race during proof publication")
                    raced = True
                return original_try_source_hash(target)

            with (
                mock.patch.object(
                    runtime_review, "done_payload", side_effect=arm_after_gates,
                ),
                mock.patch.object(
                    runtime_review, "try_source_hash",
                    side_effect=commit_at_final_hash,
                ),
            ):
                code, payload = run_done(project)

            assert raced is True
            assert code == 1, payload
            assert payload["is_complete"] is False
            assert any(
                problem.get("rule") == "git-proof-binding"
                for problem in payload["problems"]
            )
            assert proof_path.read_bytes() == prior_proof
            proof = json.loads(prior_proof)
            assert (
                proof["source_hash"] == star_forge.source_hash(project)
            ) is empty_commit
            assert proof["head"] != star_forge.git_head(project)


def test_done_rejects_commits_during_proof_setup_without_publication() -> None:
    for empty_commit, keep_prior in ((False, True), (True, False)):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            build_completed_project(project)
            proof_path = project / star_forge.PROOF_FILE
            prior_proof = None
            if keep_prior:
                code, payload = run_done(project)
                assert code == 0, payload
                prior_proof = proof_path.read_bytes()

            original_ensure_state_dirs = runtime_review.ensure_state_dirs
            raced = False

            def commit_during_publication(target: Path) -> None:
                nonlocal raced
                original_ensure_state_dirs(target)
                if raced:
                    return
                if empty_commit:
                    code, _, error = star_forge.run_git([
                        "-c", "user.name=Star Forge Test",
                        "-c", "user.email=starforge@example.com",
                        "commit", "--allow-empty", "-m",
                        "empty commit at proof publication boundary",
                    ], target)
                    assert code == 0, error
                else:
                    (target / "src" / "hello.py").write_text(
                        "print('changed at proof publication boundary')\n",
                        encoding="utf-8",
                    )
                    commit_all(target, "source commit at proof publication boundary")
                raced = True

            with mock.patch.object(
                runtime_review, "ensure_state_dirs",
                side_effect=commit_during_publication,
            ):
                code, payload = run_done(project)

            assert raced is True
            assert code == 1, payload
            assert payload["is_complete"] is False
            assert any(
                problem.get("rule") == "git-proof-binding"
                for problem in payload["problems"]
            )
            if prior_proof is not None:
                assert proof_path.read_bytes() == prior_proof
            else:
                assert not proof_path.exists()


def test_interruption_before_terminal_publication_preserves_proof_state() -> None:
    for keep_prior in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            build_completed_project(project)
            proof_path = project / star_forge.PROOF_FILE
            prior_proof = None
            if keep_prior:
                code, payload = run_done(project)
                assert code == 0, payload
                prior_proof = proof_path.read_bytes()

            interrupted = False
            try:
                with mock.patch.object(
                    runtime_review, "proof_binding_matches",
                    side_effect=KeyboardInterrupt(
                        "interrupted before terminal proof publication"
                    ),
                ):
                    run_done(project)
            except KeyboardInterrupt:
                interrupted = True

            assert interrupted is True
            if prior_proof is None:
                assert not proof_path.exists()
            else:
                assert proof_path.read_bytes() == prior_proof


def test_done_rejects_commits_during_atomic_proof_publication() -> None:
    for empty_commit in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            build_completed_project(project)
            proof_path = project / star_forge.PROOF_FILE
            original_write_json = runtime_review.write_json
            raced = False

            def commit_during_publication(path: Path, value: object) -> None:
                nonlocal raced
                if path == proof_path and not raced:
                    if empty_commit:
                        code, _, error = star_forge.run_git([
                            "-c", "user.name=Star Forge Test",
                            "-c", "user.email=starforge@example.com",
                            "commit", "--allow-empty", "-m",
                            "empty commit during proof publication",
                        ], project)
                        assert code == 0, error
                    else:
                        (project / "src" / "hello.py").write_text(
                            "print('changed during atomic publication')\n",
                            encoding="utf-8",
                        )
                        commit_all(project, "source commit during proof publication")
                    raced = True
                original_write_json(path, value)

            with mock.patch.object(
                runtime_review, "write_json",
                side_effect=commit_during_publication,
            ):
                code, payload = run_done(project)

            assert raced is True
            assert code == 1, payload
            assert payload["is_complete"] is False
            assert any(
                problem.get("rule") == "git-proof-binding"
                for problem in payload["problems"]
            )
            historical = star_forge.read_json(proof_path)
            assert runtime_review.load_current_proof(project) is None
            drift = runtime_review.detect_drift(project, historical)
            assert drift["detected"] is True
            assert drift["head_changed"] is True
            assert drift["source_changed"] is not empty_commit
            assert runtime_orchestration.canonical_state_payload(project)["proof"] is None


def test_interruption_after_proof_publication_leaves_only_stale_history() -> None:
    for empty_commit in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            build_completed_project(project)
            proof_path = project / star_forge.PROOF_FILE
            original_write_json = runtime_review.write_json

            def publish_race_then_interrupt(path: Path, value: object) -> None:
                original_write_json(path, value)
                if path != proof_path:
                    return
                if empty_commit:
                    code, _, error = star_forge.run_git([
                        "-c", "user.name=Star Forge Test",
                        "-c", "user.email=starforge@example.com",
                        "commit", "--allow-empty", "-m",
                        "empty commit after atomic publication",
                    ], project)
                    assert code == 0, error
                else:
                    (project / "src" / "hello.py").write_text(
                        "print('changed after atomic publication')\n",
                        encoding="utf-8",
                    )
                    commit_all(project, "source commit after atomic publication")
                raise KeyboardInterrupt("interrupted after atomic publication")

            interrupted = False
            try:
                with mock.patch.object(
                    runtime_review, "write_json",
                    side_effect=publish_race_then_interrupt,
                ):
                    run_done(project)
            except KeyboardInterrupt:
                interrupted = True

            assert interrupted is True
            assert proof_path.exists()
            assert runtime_review.load_current_proof(project) is None


def test_completion_proof_consumers_require_full_current_binding() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        build_completed_project(project)
        code, payload = run_done(project)
        assert code == 0, payload
        proof = star_forge.read_json(project / star_forge.PROOF_FILE)
        assert runtime_review.proof_is_current(project, proof) is True
        for field, value in (
            ("head", "0" * 40),
            ("source_hash", "0" * 64),
            ("scope_hash", "wrong-scope"),
        ):
            assert runtime_review.proof_is_current(
                project, dict(proof, **{field: value}),
            ) is False

        (project / "src" / "hello.py").write_text(
            "print('dirty but forged current values')\n",
            encoding="utf-8",
        )
        forged = dict(
            proof,
            head=star_forge.git_head(project),
            source_hash=star_forge.source_hash(project),
            scope_hash=star_forge.scope_hash(project) or "noscope",
        )
        assert runtime_review.proof_is_current(project, forged) is False
        assert runtime_review.load_current_proof(project) is None
        assert runtime_orchestration.canonical_state_payload(project)["proof"] is None


def test_done_requires_change_packet_for_post_proof_drift_without_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        build_completed_project(project)
        code, payload = run_done(project)
        assert code == 0, payload

        source = project / "src" / "hello.py"
        source.write_text("print('changed after proof')\n", encoding="utf-8")
        record_verify(project, "SF-1")
        write_clean_review_wave(project)
        code, out, err = run_cli(
            ["review", "--project", str(project), "--strict"]
        )
        assert code == 0, err or out
        commit_all(project, "post proof drift without run")

        code, payload = run_done(project)
        assert code == 1, payload
        assert payload["drift"]["detected"] is True
        assert payload["drift"]["actionable"] is True
        assert any(
            item.get("rule") == "post-proof-change-packet-required"
            for item in payload["problems"]
        )


def test_done_rejects_reusing_historical_legacy_amend_for_new_drift() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_blueprint(project)
        write_plan(project, [
            f"| SF-1 | Build the greeter module | ready | solo | src/hello.py | - | {REAL_VERIFY} | - |",
            f"| AMEND-1 | Historical greeting fix | ready | solo | src/hello.py | SF-1 | {REAL_VERIFY} | - |",
        ])
        source = project / "src" / "hello.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("print('hello forge')\n", encoding="utf-8")

        record_verify(project, "SF-1")
        code, out, err = run_cli([
            "complete-task", "--project", str(project), "--task", "SF-1",
            "--changed-file", "src/hello.py",
        ])
        assert code == 0, err or out
        record_verify(project, "AMEND-1")
        code, out, err = run_cli([
            "complete-task", "--project", str(project), "--task", "AMEND-1",
            "--changed-file", "src/hello.py",
        ])
        assert code == 0, err or out
        for task_id in ("SF-1", "AMEND-1"):
            record_verify(project, task_id)
        write_clean_review_wave(project)
        code, out, err = run_cli([
            "review", "--project", str(project), "--strict",
        ])
        assert code == 0, err or out
        commit_all(project, "complete legacy project with historical amendment")
        code, payload = run_done(project)
        assert code == 0, payload
        assert payload["is_complete"] is True
        proof = star_forge.read_json(project / star_forge.PROOF_FILE)
        history_code, historical_plan, history_error = star_forge.run_git(
            ["show", f"{proof['head']}:{star_forge.PLAN_FILE}"], project
        )
        assert history_code == 0, history_error
        assert "AMEND-1" in historical_plan
        assert "AMEND-1" in {
            task["id"] for task in star_forge.parse_tasks(project / "Plan.md")
        }

        source.write_text("print('changed after historical amendment')\n", encoding="utf-8")
        plan = project / "Plan.md"
        plan.write_text(
            plan.read_text(encoding="utf-8").replace(
                "| AMEND-1 | Historical greeting fix | complete |",
                "| AMEND-1 | Historical greeting fix | ready |",
            ),
            encoding="utf-8",
        )
        record_verify(project, "AMEND-1")
        code, out, err = run_cli([
            "complete-task", "--project", str(project), "--task", "AMEND-1",
            "--changed-file", "src/hello.py",
        ])
        assert code == 0, err or out
        for task_id in ("SF-1", "AMEND-1"):
            record_verify(project, task_id)
        write_clean_review_wave(project)
        code, out, err = run_cli([
            "review", "--project", str(project), "--strict",
        ])
        assert code == 0, err or out
        commit_all(project, "attempt to reuse historical amendment")

        code, payload = run_done(project)
        assert code == 1, payload
        assert payload["is_complete"] is False
        assert payload["drift"]["detected"] is True
        assert payload["drift"]["covered_by_completed_amendment"] is None
        assert payload["drift"]["covered_by_completed_change_packet"] is None
        assert payload["drift"]["actionable"] is True
        assert any(
            item.get("rule") == "post-proof-change-packet-required"
            for item in payload["problems"]
        )


def test_post_done_drift_creates_single_draft_change_packet() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        build_completed_project(project)
        code, _ = run_done(project)
        assert code == 0
        (project / "src" / "hello.py").write_text("print('hello forge, drifted')\n", encoding="utf-8")
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["phase"] == "plan"
        assert state["blueprint"]["status"] == "legacy-approved"
        assert state["drift"]["detected"] is True
        assert "AMEND-1" not in (project / "Plan.md").read_text(encoding="utf-8")
        code, out, err = run_cli(["approve-blueprint", "--project", str(project)])
        assert code == 0, err or out
        assert json.loads(out)["status"] == "locked"
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["phase"] == "amend"
        assert state["drift"]["detected"] is True
        assert state["change_packet"]["change_id"] == "CHANGE-1"
        assert state["change_packet"]["approval_state"] == "draft"
        assert state["change_packet"]["scope_delta"] == ["src/hello.py"]
        assert state["plan"]["ready"] == []
        assert state["spawn_plan"] == []
        plan_text = (project / "Plan.md").read_text(encoding="utf-8")
        assert "| AMEND-" not in plan_text
        packet_tasks = star_forge.project_changes.change_plan_tasks(project, "CHANGE-1")
        assert len(packet_tasks) == 1
        assert packet_tasks[0]["status"] == "queued"
        assert packet_tasks[0]["mode"] == "delegate"
        assert packet_tasks[0]["verify"] == REAL_VERIFY
        assert "delivery" in star_forge.task_proof_kinds(packet_tasks[0])
        incidents = (project / ".starforge" / "state" / "incidents.jsonl").read_text(encoding="utf-8")
        assert '"kind": "post-done-drift"' in incidents
        assert '"change_packet": "CHANGE-1"' in incidents

        # Identical drift reuses the same draft and still cannot yield work.
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        packets = star_forge.project_changes.list_change_packets(project)
        assert [packet["change_id"] for packet in packets] == ["CHANGE-1"]
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["plan"]["ready"] == []
        assert state["spawn_plan"] == []

        code, out, err = run_cli(
            ["approve-change", "--project", str(project), "--change", "CHANGE-1"]
        )
        assert code == 0, err or out
        assert json.loads(out)["ready"] == ["CHANGE-1-T1"]
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["change_packet"]["approval_state"] == "approved"
        assert state["plan"]["ready"] == ["CHANGE-1-T1"]
        assert state["spawn_plan"][0]["task"] == "CHANGE-1-T1"


def test_post_done_special_paths_round_trip_through_one_reused_packet() -> None:
    names = (
        "back\\slash.py",
        'quote"name.py',
        " ",
        "-",
        " leading.py",
        "trailing.py ",
        "left -> right.py",
        "line\nbreak.py",
        "tab\tname.py",
        "café.py",
        "comma,name.py",
        "semi;name.py",
        "pipe|name.py",
        "M  status-like.py",
        "?? status-like.py",
    )
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        build_completed_project(project)
        code, payload = run_done(project)
        assert code == 0, payload
        for name in names:
            (project / name).write_text("special path drift\n", encoding="utf-8")

        expected = star_forge.source_dirty_entries(star_forge.git_status(project))
        assert set(expected) == set(names)
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        code, out, err = run_cli(["approve-blueprint", "--project", str(project)])
        assert code == 0, err or out
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out

        packets = star_forge.project_changes.list_change_packets(project)
        assert len(packets) == 1
        packet = packets[0]
        assert packet["scope_delta"] == expected
        assert star_forge.project_changes.read_change_packet(
            project, packet["change_id"]
        )["scope_delta"] == expected
        tasks = star_forge.project_changes.change_plan_tasks(
            project, packet["change_id"]
        )
        assert len(tasks) == 1
        assert star_forge.task_files(tasks[0]) == expected
        assert str(tasks[0]["files"]).startswith("[")

        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        reused = star_forge.project_changes.list_change_packets(project)
        assert [item["change_id"] for item in reused] == [packet["change_id"]]
        assert reused[0]["scope_delta"] == expected


def test_active_change_task_preserves_special_paths_through_fresh_proof() -> None:
    names = (
        "src/back\\slash.py",
        "src/ leading.py",
        "src/trailing.py ",
        "src/line\nbreak.py",
        "src/carriage\rreturn.py",
        "src/tab\tname.py",
        "src/comma,name.py",
        "src/semi;name.py",
        "src/pipe|name.py",
        "src/literal*.py",
        "src/literal?.py",
        "src/literal[ab].py",
        "src/literal].py",
        "C:\\absolute",
        "\\\\server\\share",
    )
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        owned_files = json.dumps([
            "src/", "C:\\absolute", "\\\\server\\share",
        ])
        build_completed_project(project, owned_files=owned_files)
        code, initial_done = run_done(project)
        assert code == 0, initial_done
        initial_proof = star_forge.read_json(project / star_forge.PROOF_FILE)
        for name in names:
            (project / name).write_text("special path drift\n", encoding="utf-8")

        expected = star_forge.source_dirty_entries(star_forge.git_status(project))
        assert set(expected) == set(names)
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        code, out, err = run_cli([
            "approve-blueprint", "--project", str(project),
        ])
        assert code == 0, err or out
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out

        packet = star_forge.project_changes.list_change_packets(project)[0]
        assert packet["scope_delta"] == expected
        change_id = packet["change_id"]
        code, out, err = run_cli([
            "approve-change", "--project", str(project),
            "--change", change_id,
        ])
        assert code == 0, err or out
        task = star_forge.project_changes.change_plan_tasks(
            project, change_id,
        )[0]
        assert task["status"] == "ready"
        assert star_forge.task_files(task) == expected

        record_verify(project, task["id"])
        complete_args = [
            "complete-task", "--project", str(project), "--task", task["id"],
        ]
        for name in expected:
            complete_args.extend(["--changed-file", name])
        code, out, err = run_cli(complete_args)
        assert code == 0, err or out
        completed = star_forge.project_changes.change_plan_tasks(
            project, change_id,
        )[0]
        assert completed["status"] == "complete"
        assert json.loads(completed["evidence"]) == expected
        assert star_forge.task_files(completed) == expected

        record_verify(project, task["id"])
        record_verify(project, "SF-1")
        commit_all(project, "complete special path amendment")
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        state = json.loads(
            (project / ".starforge" / "state.json").read_text(encoding="utf-8")
        )
        assert state["phase"] == "review"
        assert state["drift"]["covered_by_completed_change_packet"] == change_id
        assert len(star_forge.project_changes.list_change_packets(project)) == 1

        write_clean_review_wave(project)
        code, out, err = run_cli([
            "review", "--project", str(project), "--strict",
        ])
        assert code == 0, err or out
        code, final_done = run_done(project)
        assert code == 0, final_done
        final_proof = star_forge.read_json(project / star_forge.PROOF_FILE)
        assert final_proof["source_hash"] == star_forge.source_hash(project)
        assert final_proof["source_hash"] != initial_proof["source_hash"]


def test_scaffold_amend_preserves_complete_long_drift_file_list() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_blueprint(project)
        write_plan(project, [f"| SF-1 | Build the greeter module | complete | solo | src/hello.py | - | {REAL_VERIFY} | src/hello.py |"])
        changed = [
            *[f"src/feature_{idx:02d}/component_{idx:02d}.py" for idx in range(16)],
            "src/pipe|amend.py",
            "scripts/star_forge.py",
            "skills/forge-review/SKILL.md",
            "tests/test_star_forge.py",
        ]
        amend_id = star_forge.scaffold_amend(project, {"changed_files": changed})
        assert amend_id == "AMEND-1"
        amend = next(task for task in star_forge.parse_tasks(project / "Plan.md") if task["id"] == "AMEND-1")
        assert len(amend["files"]) > 120
        assert star_forge.task_files(amend) == changed
        assert "scrip" not in star_forge.task_files(amend)


def test_completed_change_packet_is_reused_then_new_drift_gets_new_packet() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        build_completed_project(project)
        code, _ = run_done(project)
        assert code == 0

        (project / "src" / "hello.py").write_text("print('hello forge, drifted')\n", encoding="utf-8")
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["phase"] == "plan"
        assert state["blueprint"]["status"] == "legacy-approved"
        assert "AMEND-1" not in (project / "Plan.md").read_text(encoding="utf-8")
        code, out, err = run_cli(["approve-blueprint", "--project", str(project)])
        assert code == 0, err or out
        assert json.loads(out)["status"] == "locked"
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        code, out, err = run_cli(
            ["approve-change", "--project", str(project), "--change", "CHANGE-1"]
        )
        assert code == 0, err or out
        record_verify(project, "CHANGE-1-T1")
        code, payload = complete_task(project, "CHANGE-1-T1")
        assert code == 0, payload

        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["phase"] == "review"
        assert state["drift"]["covered_by_completed_change_packet"] == "CHANGE-1"
        assert state["drift"]["actionable"] is False
        plan_text = (project / "Plan.md").read_text(encoding="utf-8")
        assert "| AMEND-" not in plan_text
        assert len(star_forge.project_changes.list_change_packets(project)) == 1

        (project / "src" / "hello.py").write_text("print('hello forge, drifted again')\n", encoding="utf-8")
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["phase"] == "amend"
        assert state["change_packet"]["change_id"] == "CHANGE-2"
        assert state["change_packet"]["approval_state"] == "draft"
        assert state["plan"]["ready"] == []
        assert [
            packet["change_id"]
            for packet in star_forge.project_changes.list_change_packets(project)
        ] == ["CHANGE-1", "CHANGE-2"]
        assert "| AMEND-" not in (project / "Plan.md").read_text(encoding="utf-8")


def test_change_packet_loop_closes_and_new_proof_supersedes_old_proof() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        build_completed_project(project)
        code, _ = run_done(project)
        assert code == 0
        proof_path = project / ".starforge" / "final" / "proof.json"
        old_proof = json.loads(proof_path.read_text(encoding="utf-8"))
        (project / "src" / "hello.py").write_text("print('hello forge, drifted')\n", encoding="utf-8")
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["phase"] == "plan"
        assert state["blueprint"]["status"] == "legacy-approved"
        assert "AMEND-1" not in (project / "Plan.md").read_text(encoding="utf-8")
        code, out, err = run_cli(["approve-blueprint", "--project", str(project)])
        assert code == 0, err or out
        assert json.loads(out)["status"] == "locked"
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        assert "| AMEND-" not in (project / "Plan.md").read_text(encoding="utf-8")
        assert json.loads(
            (project / ".starforge" / "state.json").read_text(encoding="utf-8")
        )["change_packet"]["approval_state"] == "draft"
        code, out, err = run_cli(
            ["approve-change", "--project", str(project), "--change", "CHANGE-1"]
        )
        assert code == 0, err or out
        # Close the loop: re-verify both tasks at the new tree, complete the packet,
        # refresh the review, commit, then done passes and the proof supersedes.
        record_verify(project, "SF-1")
        record_verify(project, "CHANGE-1-T1")
        code, payload = complete_task(project, "CHANGE-1-T1")
        assert code == 0, payload
        record_verify(project, "SF-1")
        record_verify(project, "CHANGE-1-T1")
        # The source changed, so the prior review is stale: a real re-review must
        # re-attest the new source hash before review passes.
        write_clean_review_wave(project)
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 0, err or out
        commit_all(project, "amend loop closed")
        code, payload = run_done(project)
        assert code == 0, payload
        new_proof = json.loads(proof_path.read_text(encoding="utf-8"))
        assert new_proof["source_hash"] != old_proof["source_hash"]
        assert new_proof["source_hash"] == star_forge.source_hash(project)
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["phase"] == "done"
        assert state["change_packet"] is None


# -------------------------------------------------------------- 8. isolation


def make_foreign_root(root: Path) -> None:
    (root / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "README.md").write_text("# Existing app\n", encoding="utf-8")


def test_run_blocks_foreign_root_without_isolation_flags() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        make_foreign_root(root)
        code, out, err = run_cli(["run", "--project", str(root), "--strict", "--no-hooks"])
        assert code == 1, err or out
        payload = json.loads(out)
        assert payload["phase"] == "blocked:isolation-required"
        assert "--product-slug" in payload["required_next_action"]
        assert not (root / "work").exists()
        assert not (root / ".starforge").exists()


def test_init_product_slug_isolates_under_work_dir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        make_foreign_root(root)
        code, out, err = run_cli(["init", "--project", str(root), "--product-slug", "galaxy-map", "--no-agents"])
        assert code == 0, err or out
        project = root / "work" / "galaxy-map"
        assert (project / ".git").exists()  # its own repo, not the parent's
        assert (project / "Blueprint.md").exists() and (project / "Plan.md").exists()
        redirect = json.loads((root / ".starforge" / "project.json").read_text(encoding="utf-8"))
        assert redirect["schema"] == "star-forge.project-redirect.v1"
        assert Path(redirect["project_root"]) == project
        manifest = json.loads((project / ".starforge" / "project.json").read_text(encoding="utf-8"))
        assert manifest["schema"] == "star-forge.project.v1"
        # resolve_project follows the redirect from the contaminated root.
        assert star_forge.resolve_project(str(root)) == project
        code, out, err = run_cli(["run", "--project", str(root), "--no-hooks", "--objective", "Build the galaxy map"])
        assert code == 0, err or out
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["project"] == str(project)
        assert not (root / "Blueprint.md").exists()  # root stays clean


def test_run_product_slug_on_fresh_foreign_root_isolates_cleanly() -> None:
    # Regression: resolve_isolation once wrote the root redirect before the
    # nested manifest existed, so follow_project_redirect bounced auto-init back
    # to the foreign root and scaffolded .git/.starforge into the user's repo.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        make_foreign_root(root)
        code, out, err = run_cli(["run", "--project", str(root), "--product-slug", "galaxy-map", "--no-hooks"])
        assert code == 0, err or out
        redirect = json.loads((root / ".starforge" / "project.json").read_text(encoding="utf-8"))
        assert redirect["schema"] == "star-forge.project-redirect.v1"
        nested = root / "work" / "galaxy-map"
        manifest = json.loads((nested / ".starforge" / "project.json").read_text(encoding="utf-8"))
        assert manifest["schema"] == "star-forge.project.v1"
        assert (nested / ".git").exists()
        assert (nested / "Blueprint.md").exists()
        # The foreign root stays clean: no scaffolding outside the redirect file.
        assert not (root / "Blueprint.md").exists()
        assert not (root / "the-loop").exists()
        assert not (root / ".git").exists()
        state = json.loads((nested / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["project"] == str(nested)


def test_project_redirect_rejects_absolute_sibling_project_escape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        selected = root / "selected"
        sibling = root / "sibling"
        selected.mkdir()
        init_project(sibling)
        (selected / ".starforge").mkdir()
        (selected / ".starforge" / "project.json").write_text(
            json.dumps({
                "schema": "star-forge.project-redirect.v1",
                "project_root": str(sibling),
            }) + "\n",
            encoding="utf-8",
        )
        code, out, err = run_cli(["status", "--project", str(selected)])
        assert code == 1, out
        assert "redirect" in err.lower()
        assert str(sibling) in err


def test_project_redirect_rejects_symlinked_managed_target() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        selected = root / "selected"
        external = root / "external"
        selected.mkdir()
        init_project(external)
        (selected / "work").mkdir()
        (selected / "work" / "app").symlink_to(external, target_is_directory=True)
        (selected / ".starforge").mkdir()
        (selected / ".starforge" / "project.json").write_text(
            json.dumps({
                "schema": "star-forge.project-redirect.v1",
                "project_root": str(selected / "work" / "app"),
            }) + "\n",
            encoding="utf-8",
        )
        code, out, err = run_cli(["status", "--project", str(selected)])
        assert code == 1, out
        assert "symlink" in err.lower()


def test_state_commands_reject_symlinked_starforge_root_without_external_write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        project = root / "project"
        external = root / "external-state"
        project.mkdir()
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("unchanged\n", encoding="utf-8")
        (project / ".starforge").symlink_to(external, target_is_directory=True)
        code, out, err = run_cli(["init", "--project", str(project), "--no-agents"])
        assert code == 1, out
        assert "symlink" in err.lower()
        assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
        assert sorted(path.name for path in external.iterdir()) == ["sentinel.txt"]


def test_state_commands_reject_nested_state_symlink_without_external_write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        project = root / "project"
        external = root / "external-runs"
        init_project(project)
        runs = project / ".starforge" / "runs"
        runs.rmdir()
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("unchanged\n", encoding="utf-8")
        runs.symlink_to(external, target_is_directory=True)
        code, out, err = run_cli(["status", "--project", str(project)])
        assert code == 1, out
        assert "symlink" in err.lower()
        assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
        assert sorted(path.name for path in external.iterdir()) == ["sentinel.txt"]


def test_init_no_hooks_and_no_agents_control_distinct_surfaces() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        hooks_disabled = root / "hooks-disabled"
        code, out, err = run_cli(["init", "--project", str(hooks_disabled), "--no-hooks"])
        assert code == 0, err or out
        assert (hooks_disabled / ".codex" / "agents" / "starforge-builder.toml").exists()

        agents_disabled = root / "agents-disabled"
        code, out, err = run_cli(["init", "--project", str(agents_disabled), "--no-agents"])
        assert code == 0, err or out
        assert not (agents_disabled / ".codex" / "agents").exists()


def test_run_no_hooks_and_no_agents_control_distinct_auto_init_surfaces() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        hooks_disabled = root / "hooks-disabled"
        code, out, err = run_cli(["run", "--project", str(hooks_disabled), "--no-hooks"])
        assert code == 0, err or out
        assert (hooks_disabled / ".codex" / "agents" / "starforge-builder.toml").exists()
        assert not any(line.startswith("HOOKS:") for line in out.splitlines())
        assert not (hooks_disabled / star_forge.HOOK_TRUST_NOTICE_FILE).exists()
        state = json.loads((hooks_disabled / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["hook_trust_notice"]["show"] is False

        agents_disabled = root / "agents-disabled"
        code, out, err = run_cli(["run", "--project", str(agents_disabled), "--no-agents"])
        assert code == 0, err or out
        assert not (agents_disabled / ".codex" / "agents").exists()
        assert "HOOKS: Optional observer hooks are not trusted yet." in out
        assert (agents_disabled / star_forge.HOOK_TRUST_NOTICE_FILE).exists()
        state = json.loads((agents_disabled / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["hook_trust_notice"]["show"] is True


def test_run_adopt_root_records_adopted_root_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        make_foreign_root(root)
        code, out, err = run_cli(["run", "--project", str(root), "--adopt-root", "--no-hooks"])
        assert code == 0, err or out
        manifest = json.loads((root / ".starforge" / "project.json").read_text(encoding="utf-8"))
        assert manifest["schema"] == "star-forge.project.v1"
        assert manifest["root_mode"] == "adopted-root"
        assert (root / "Blueprint.md").exists()  # built in place, deliberately
        assert (root / ".codex" / "agents" / "starforge-builder.toml").exists()
        assert not any(line.startswith("HOOKS:") for line in out.splitlines())


def test_run_adopt_root_no_agents_preserves_hook_behavior() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        make_foreign_root(root)
        code, out, err = run_cli(["run", "--project", str(root), "--adopt-root", "--no-agents"])
        assert code == 0, err or out
        manifest = json.loads((root / ".starforge" / "project.json").read_text(encoding="utf-8"))
        assert manifest["root_mode"] == "adopted-root"
        assert not (root / ".codex" / "agents").exists()
        assert "HOOKS: Optional observer hooks are not trusted yet." in out
        assert (root / star_forge.HOOK_TRUST_NOTICE_FILE).exists()


def test_init_obeys_isolation_guard() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        make_foreign_root(root)
        code, out, err = run_cli(["init", "--project", str(root), "--no-agents"])
        assert code == 1, err or out
        payload = json.loads(out)
        assert payload["phase"] == "blocked:isolation-required"
        assert not (root / "Blueprint.md").exists()
        assert not (root / "work").exists()


# ------------------------------------------------------------------ 9. hooks


HOOK_COMMANDS = [
    "hook",
    "post-hook",
    "prompt-hook",
    "session-start-hook",
    "subagent-start-hook",
    "subagent-stop-hook",
    "stop-hook",
    "pre-compact-hook",
]


def test_plugin_package_includes_standard_hooks_without_unsupported_manifest_field() -> None:
    manifest_path = ROOT / ".codex-plugin" / "plugin.json"
    hooks_path = ROOT / "hooks" / "hooks.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert hooks_path.is_file()
    assert "hooks" not in manifest
    assert manifest["interface"]["displayName"] == "Star Forge"


def test_packaged_contract_text_does_not_claim_local_hooks_witness_completion() -> None:
    targets = [
        ROOT / "docs" / "hooks.md",
        ROOT / "docs" / "forge-loop.md",
        ROOT / "docs" / "workflow.md",
        ROOT / "skills" / "forge" / "SKILL.md",
        ROOT / "skills" / "forge-review" / "SKILL.md",
        ROOT / "agents" / "reviewer" / "agent.md",
        ROOT / "scripts" / "star_forge.py",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in targets)
    normalized = " ".join(text.split())
    forbidden = [
        "To earn an unqualified `COMPLETE`, hooks must be trusted",
        "instead of a witnessed `COMPLETE`",
        "hook-observed sub-agent thread ids",
        "done` reads it to decide whether a completion is *witnessed*",
        "review counts as witnessed",
        "ABSENT (advisory",
        "run /hooks in Codex",
        "+ /hooks",
    ]
    for phrase in forbidden:
        assert phrase not in normalized
    docs_hooks = (ROOT / "docs" / "hooks.md").read_text(encoding="utf-8")
    assert "observer diagnostics and continuity" in docs_hooks
    assert "no supported host-controlled witness source" in docs_hooks
    manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert "diagnostic rather than trusted completion witnesses" in manifest["interface"]["longDescription"]


def test_packaged_reviewer_role_contract_requires_source_hash() -> None:
    text = (ROOT / "agents" / "reviewer" / "agent.md").read_text(encoding="utf-8")
    assert '"source_hash": "<exact source_hash value from your spawn prompt>"' in text
    assert "The `source_hash` field is required." in text
    assert "exactly match the `source_hash` in your spawn prompt" in text


def test_packaged_reviewer_role_contract_allows_run_reanchor_only() -> None:
    text = (ROOT / "agents" / "reviewer" / "agent.md").read_text(encoding="utf-8")
    assert "You may run one read/state re-anchor command" in text
    assert "python3 scripts/star_forge.py run --project ." in text
    assert "current phase, scope, profile lock, and source_hash" in text
    assert "Never run mutating or proof Star Forge commands" in text
    for forbidden in ("verify", "browser-run", "complete-task", "review", "waive", "done"):
        assert f"`{forbidden}`" in text
    assert "or any Star Forge CLI command" not in text


def test_hooks_noop_outside_star_forge_projects() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        foreign = Path(tmp).resolve()
        star_forge.ensure_git_repo(foreign)
        (foreign / "README.md").write_text("# Some other repo\n", encoding="utf-8")
        for subcmd in HOOK_COMMANDS:
            event = {"cwd": str(foreign), "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, "prompt": "hello"}
            code, out, err = run_cli([subcmd], stdin_payload=event)
            assert code == 0, f"{subcmd}: {err or out}"
        assert not (foreign / ".starforge").exists()  # no scaffolding side effects


def test_post_hook_appends_changed_files_with_session_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        event = {"cwd": str(project), "hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": "src/app.py"}, "session_id": "sess-123"}
        code, out, err = run_cli(["post-hook"], stdin_payload=event)
        assert code == 0, err or out
        lines = [json.loads(line) for line in (project / ".starforge" / "state" / "changed-files.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        assert lines[0]["file"] == "src/app.py"
        assert lines[0]["session_id"] == "sess-123"


def test_prompt_hook_emits_banner_and_resets_budget() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        run_cli(["run", "--project", str(project), "--no-hooks"])  # phase=intake state exists
        counter = project / ".starforge" / "state" / "auto-continue.json"
        star_forge.write_json(counter, {"count": 2, "signature": "stale"})
        event = {"cwd": str(project), "prompt": "keep going"}
        code, out, err = run_cli(["prompt-hook"], stdin_payload=event)
        assert code == 0, err or out
        payload = json.loads(out)
        banner = payload["hookSpecificOutput"]["additionalContext"]
        assert banner.startswith("[star-forge] phase=intake")
        assert "next:" in banner
        assert not counter.exists()  # budget reset on every user prompt


def test_stop_hook_writes_handoff_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        run_cli(["run", "--project", str(project), "--mode", "sync", "--no-hooks"])
        event = {"cwd": str(project), "hook_event_name": "Stop"}
        code, out, err = run_cli(["stop-hook"], stdin_payload=event)
        assert code == 0, err or out
        assert "saved continuity state" in out
        handoff = json.loads((project / ".starforge" / "state" / "handoff-artifact.json").read_text(encoding="utf-8"))
        assert handoff["schema"] == "star-forge.handoff.v1"
        assert handoff["phase"] == "intake"
        assert handoff["complete"] is False


def test_stop_hook_flags_completion_contradiction() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)  # blueprint unapproved: predicate is false
        event = {"cwd": str(project), "hook_event_name": "Stop", "summary": {"complete": True}}
        code, out, err = run_cli(["stop-hook"], stdin_payload=event)
        assert code == 0, err or out
        payload = json.loads(out)
        assert "contradicts the computed predicate" in payload["systemMessage"]
        incidents = (project / ".starforge" / "state" / "incidents.jsonl").read_text(encoding="utf-8")
        assert '"kind": "completion-contradiction"' in incidents


def test_subagent_hooks_append_events() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        base = {"cwd": str(project), "agent_id": "agent-7", "agent_type": "starforge-builder", "session_id": "sess-9"}
        code, _, err = run_cli(["subagent-start-hook"], stdin_payload=base)
        assert code == 0, err
        code, _, err = run_cli(["subagent-stop-hook"], stdin_payload=base)
        assert code == 0, err
        lines = [json.loads(line) for line in (project / ".starforge" / "state" / "subagent-events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        assert [item["event"] for item in lines] == ["SubagentStart", "SubagentStop"]
        assert all(item["agent_id"] == "agent-7" for item in lines)


def test_should_block_stop_requires_cruise_and_active_phase() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        handoff = {"next_action": "approve the blueprint"}
        # sync mode never auto-continues.
        run_cli(["run", "--project", str(project), "--mode", "sync", "--no-hooks"])
        assert star_forge.should_block_stop(project, {}, handoff) is None
        # cruise + intake phase does.
        run_cli(["run", "--project", str(project), "--mode", "cruise", "--no-hooks"])
        assert star_forge.should_block_stop(project, {}, handoff)
        # an already-active stop hook is never re-blocked.
        assert star_forge.should_block_stop(project, {"stop_hook_active": True}, handoff) is None
    with tempfile.TemporaryDirectory() as tmp:
        # cruise + setup phase is outside the keep-going set.
        project = Path(tmp).resolve()
        code, out, err = run_cli(["run", "--project", str(project), "--no-auto-init", "--no-hooks"])
        assert code == 0, err or out
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["phase"] == "setup" and state["mode"] == "cruise"
        assert star_forge.should_block_stop(project, {}, {"next_action": "init"}) is None


def test_should_block_stop_bounded_with_signature_reset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        run_cli(["run", "--project", str(project), "--no-hooks"])  # cruise + intake
        handoff = {"next_action": "approve the blueprint"}
        results = [star_forge.should_block_stop(project, {}, handoff) for _ in range(star_forge.MAX_AUTO_CONTINUES + 1)]
        assert all(isinstance(item, str) for item in results[: star_forge.MAX_AUTO_CONTINUES])
        assert results[star_forge.MAX_AUTO_CONTINUES] is None  # budget exhausted
        # A different next-action signature resets the budget.
        assert star_forge.should_block_stop(project, {}, {"next_action": "write the plan"})


# ------------------------------------------------- 10. operating card + state


def test_run_prints_operating_card_first() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        code, out, err = run_cli(["run", "--project", str(project), "--objective", "Build the greeter", "--no-hooks"])
        assert code == 0, err or out
        lines = out.splitlines()
        assert lines[0].startswith(f"star-forge {star_forge.SF_VERSION} | hooks: ADVISORY (no trusted witness source)")
        assert "| phase: intake" in lines[0]
        assert "/hooks" not in lines[0]
        assert "witnessed" not in lines[0]
        assert lines[1].startswith("NEXT:")
        assert any(line.startswith("RULES:") for line in lines[:10])


def test_operating_card_reports_local_hooks_as_diagnostics_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        event = {"cwd": str(project), "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}
        code, _, err = run_cli(["hook"], stdin_payload=event)
        assert code == 0, err
        code, out, err = run_cli(["run", "--project", str(project), "--objective", "Build the greeter", "--no-hooks"])
        assert code == 0, err or out
        first = out.splitlines()[0]
        assert "hooks: ADVISORY (local hook diagnostics observed)" in first
        assert "/hooks" not in first
        assert "witnessed" not in first


def test_run_prints_hook_trust_notice_once_until_local_hooks_observed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        code, out, err = run_cli(["run", "--project", str(project), "--objective", "Build the greeter"])
        assert code == 0, err or out
        assert "HOOKS: Optional observer hooks are not trusted yet." in out
        marker = project / star_forge.HOOK_TRUST_NOTICE_FILE
        assert marker.exists()
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["hook_trust_notice"]["show"] is True

        code, out, err = run_cli(["run", "--project", str(project), "--objective", "Build the greeter"])
        assert code == 0, err or out
        assert "HOOKS: Optional observer hooks are not trusted yet." not in out
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["hook_trust_notice"]["show"] is False

        marker.unlink()
        event = {"cwd": str(project), "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}
        code, _, err = run_cli(["hook"], stdin_payload=event)
        assert code == 0, err
        code, out, err = run_cli(["run", "--project", str(project), "--objective", "Build the greeter"])
        assert code == 0, err or out
        assert "HOOKS: Optional observer hooks are not trusted yet." not in out


def test_review_spawn_prompt_treats_agent_id_as_provenance_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        reviewable_project(project)
        prompt = star_forge.reviewer_spawn_prompt(project, star_forge.scope_hash(project) or "noscope")
        assert "review counts as witnessed" not in prompt
        assert "agent_id for provenance diagnostics only" in prompt
        assert "local ids do not create unqualified COMPLETE" in prompt


def test_standard_review_spawn_plan_has_distinct_profile_reviewers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project)
        scope = star_forge.scope_hash(project) or "noscope"
        plan = star_forge.spawn_plan(project, tasks, "review")
        assert [entry["role"] for entry in plan] == ["correctness", "security", "architecture"]
        for entry in plan:
            role = entry["role"]
            findings_file = star_forge.relative_to_project(
                star_forge.reviews_scope_dir(project, scope) / f"{role}.findings.json",
                project,
            )
            assert entry["agent"] == "starforge-reviewer"
            assert entry["tag"] == f"SF:review:{role}"
            assert entry["findings_file"] == findings_file
            assert findings_file in entry["spawn"]
            assert f"[SF:review:{role}]" in entry["spawn"]
            assert f'\\"role\\":\\"{role}\\"' in entry["spawn"]


def test_fast_mvp_review_spawn_plan_has_one_reviewer() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        tasks = reviewable_project(project, profile="fast-mvp")
        plan = star_forge.spawn_plan(project, tasks, "review")
        assert len(plan) == 1
        assert plan[0]["role"] == "correctness"
        assert plan[0]["findings_file"].endswith("correctness.findings.json")


def test_manifest_version_build_metadata_matches_semantic_core() -> None:
    assert star_forge.version_core(star_forge.SF_VERSION + "+codex.20260618020929") == star_forge.SF_VERSION
    assert star_forge.version_key("0.3.0+codex.20260618020929") == star_forge.version_key("0.3.0")


def test_state_spawn_plan_has_paste_ready_builder_prompt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_blueprint(project)
        write_plan(project, ["| SF-1 | Build the greeter module | ready | delegate | src/hello.py | - | echo verified-ok | - |"])
        code, out, err = run_cli(["run", "--project", str(project), "--no-hooks"])
        assert code == 0, err or out
        state = json.loads((project / ".starforge" / "state.json").read_text(encoding="utf-8"))
        assert state["phase"] == "build"
        spawn = state["spawn_plan"]
        assert spawn and spawn[0]["task"] == "SF-1"
        assert spawn[0]["agent"] == "starforge-builder"
        assert "spawn_agent starforge-builder" in spawn[0]["spawn"]
        assert "SF-1" in spawn[0]["spawn"] and "src/hello.py" in spawn[0]["spawn"]
        assert "SPAWN (paste as-is):" in state["operating_card"]


def test_learn_writes_under_env_home() -> None:
    old_home = os.environ["STAR_FORGE_LEARNINGS_HOME"]
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["STAR_FORGE_LEARNINGS_HOME"] = tmp
        try:
            code, out, err = run_cli(
                ["learn", "--title", "Pin the dev server port", "--rule", "Claim a server lease before any browser run", "--trigger", "py", "--category", "process"]
            )
            assert code == 0, err or out
            path = Path(tmp) / "process" / "pin-the-dev-server-port.md"
            assert path.exists()
            text = path.read_text(encoding="utf-8")
            assert "triggers: py" in text
            assert "Claim a server lease" in text
        finally:
            os.environ["STAR_FORGE_LEARNINGS_HOME"] = old_home


def test_learnings_digest_surfaces_matching_triggers() -> None:
    old_home = os.environ["STAR_FORGE_LEARNINGS_HOME"]
    with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_project:
        os.environ["STAR_FORGE_LEARNINGS_HOME"] = tmp_home
        try:
            project = Path(tmp_project).resolve()
            init_project(project)
            src = project / "src" / "hello.py"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("print('hello forge')\n", encoding="utf-8")
            run_cli(["learn", "--title", "Python verify commands", "--rule", "Use the interpreter to verify modules", "--trigger", "py", "--category", "verification"])
            run_cli(["learn", "--title", "Cargo workspace pitfalls", "--rule", "Pin workspace members explicitly", "--trigger", "rustlang", "--category", "verification"])
            titles = [item["title"] for item in star_forge.learnings_digest(project)]
            assert "Python verify commands" in titles
            assert "Cargo workspace pitfalls" not in titles
        finally:
            os.environ["STAR_FORGE_LEARNINGS_HOME"] = old_home


# -------------------------------------------------------- 11. enforcement mode


def test_enforcement_mode_keeps_project_local_hooks_advisory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        assert star_forge.enforcement_mode(project) == "advisory"
        event = {"cwd": str(project), "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}
        code, _, err = run_cli(["hook"], stdin_payload=event)
        assert code == 0, err
        assert (project / ".starforge" / "state" / "hook-events.jsonl").exists()
        liveness = star_forge.hooks_liveness(project)
        assert liveness["local_events_observed"] is True
        assert liveness["events_observed"] is False
        assert star_forge.enforcement_mode(project) == "advisory"


def test_env_selected_witness_root_does_not_upgrade_or_receive_mirrors() -> None:
    old_root = os.environ.get("STAR_FORGE_TRUSTED_WITNESS_ROOT")
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as witness_tmp:
        try:
            os.environ["STAR_FORGE_TRUSTED_WITNESS_ROOT"] = witness_tmp
            project = Path(tmp).resolve()
            reviewable_project(project)
            hook_event = {"cwd": str(project), "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}
            code, _, err = run_cli(["hook"], stdin_payload=hook_event)
            assert code == 0, err
            subagent_event = {"cwd": str(project), "agent_id": "reviewer-7", "agent_type": "starforge-reviewer", "session_id": "sess-7"}
            code, _, err = run_cli(["subagent-start-hook"], stdin_payload=subagent_event)
            assert code == 0, err
            assert not any(Path(witness_tmp).rglob("*.jsonl"))
            assert star_forge.hooks_liveness(project)["local_events_observed"] is True
            assert star_forge.hooks_liveness(project)["events_observed"] is False
            assert star_forge.enforcement_mode(project) == "advisory"
            assert star_forge.local_subagent_ids(project) == {"reviewer-7"}
            assert star_forge.known_subagent_ids(project) == set()
            write_reviewer_findings(project, "correctness", [], agent_id="reviewer-7")
            write_reviewer_findings(project, "security", [])
            write_reviewer_findings(project, "architecture", [])
            code, out, err = run_cli(["review", "--project", str(project), "--strict"])
            assert code == 0, err or out
            merged = json.loads(out)
            assert merged["reviewers_witnessed"] is False
        finally:
            if old_root is None:
                os.environ.pop("STAR_FORGE_TRUSTED_WITNESS_ROOT", None)
            else:
                os.environ["STAR_FORGE_TRUSTED_WITNESS_ROOT"] = old_root


def test_done_verdict_carries_advisory_suffix_without_hooks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        build_completed_project(project)
        code, payload = run_done(project)
        assert code == 0, payload
        assert payload["enforcement"] == "advisory"
        # With no trusted hook witness source, the verdict is COMPLETE but
        # advisory. Project-local hook events are surfaced separately.
        assert payload["verdict"].startswith("COMPLETE")
        assert "advisory" in payload["verdict"]
        assert "no trusted hook witness source is enabled in this version" in payload["verdict"]
        assert payload["witness"]["hooks_live"] is False
        # Project-local hook events are still reported, but they cannot upgrade
        # enforcement or remove the advisory suffix.
        event = {"cwd": str(project), "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}
        run_cli(["hook"], stdin_payload=event)
        code, payload = run_done(project)
        assert code == 0, payload
        assert payload["enforcement"] == "advisory"
        assert payload["witness"]["hooks_live"] is False
        assert payload["witness"]["local_hooks_observed"] is True
        assert payload["verdict"].startswith("COMPLETE")
        assert "no trusted hook witness source is enabled in this version" in payload["verdict"]


def test_done_advisory_flags_delegate_task_without_observed_subagent() -> None:
    # A delegate-mode task that completed with no trusted sub-agent witness earns
    # an advisory reason. Local sub-agent event files are diagnostics only.
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_blueprint(project)
        write_plan(project, [f"| SF-1 | Build the greeter module | ready | delegate | src/hello.py | - | {REAL_VERIFY} | - |"])
        src = project / "src" / "hello.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("print('hello forge')\n", encoding="utf-8")
        record_verify(project, "SF-1")
        code, out, err = run_cli(["complete-task", "--project", str(project), "--task", "SF-1", "--changed-file", "src/hello.py"])
        assert code == 0, err or out
        record_verify(project, "SF-1")
        write_clean_review_wave(project)
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 0, err or out
        commit_all(project)
        code, payload = run_done(project)
        assert code == 0, payload
        assert payload["witness"]["delegated_complete"] is True
        assert payload["witness"]["subagent_observed"] is False
        assert payload["verdict"].startswith("COMPLETE")
        assert "delegated tasks lack a trusted sub-agent witness" in payload["verdict"]


def test_done_keeps_forged_local_hook_and_subagent_ledgers_advisory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_blueprint(project)
        write_plan(project, [f"| SF-1 | Build the greeter module | ready | delegate | src/hello.py | - | {REAL_VERIFY} | - |"])
        src = project / "src" / "hello.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("print('hello forge')\n", encoding="utf-8")
        record_verify(project, "SF-1")
        code, out, err = run_cli(["complete-task", "--project", str(project), "--task", "SF-1", "--changed-file", "src/hello.py"])
        assert code == 0, err or out
        record_verify(project, "SF-1")
        state_dir = project / ".starforge" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        forged_hook = {"schema": "star-forge.hook-event.v1", "timestamp": "2026-01-01T00:00:00Z", "event": "PreToolUse", "tool": "Bash"}
        forged_subagent = {"schema": "star-forge.subagent-event.v1", "timestamp": "2026-01-01T00:00:01Z", "event": "SubagentStart", "agent_id": "reviewer-7", "agent_type": "starforge-reviewer"}
        (state_dir / "hook-events.jsonl").write_text(json.dumps(forged_hook) + "\n", encoding="utf-8")
        (state_dir / "subagent-events.jsonl").write_text(json.dumps(forged_subagent) + "\n", encoding="utf-8")
        write_reviewer_findings(project, "correctness", [], agent_id="reviewer-7")
        write_reviewer_findings(project, "security", [])
        write_reviewer_findings(project, "architecture", [])
        code, out, err = run_cli(["review", "--project", str(project), "--strict"])
        assert code == 0, err or out
        commit_all(project)
        code, payload = run_done(project)
        assert code == 0, payload
        assert payload["enforcement"] == "advisory"
        assert payload["witness"]["local_hooks_observed"] is True
        assert payload["witness"]["local_subagent_observed"] is True
        assert payload["witness"]["trusted_hooks_observed"] is False
        assert payload["witness"]["trusted_subagent_observed"] is False
        assert payload["witness"]["review_witnessed"] is False
        assert payload["verdict"].startswith("COMPLETE (advisory:")


# -------------------------------------------------------------- 12. redaction


def test_redact_masks_secret_material_and_sensitive_keys() -> None:
    raw = {"prompt": "anything at all", "note": "key=" + REAL_SK_KEY, "nested": ["value " + REAL_GHP_KEY]}
    clean = star_forge.redact(raw)
    assert clean["prompt"] == "[REDACTED]"
    assert "[REDACTED_SECRET]" in clean["note"]
    assert REAL_SK_KEY not in clean["note"]
    assert REAL_GHP_KEY not in clean["nested"][0]


# ----------------------------------------------------- 13. CLI characterization


def test_public_cli_commands_and_options_remain_compatible() -> None:
    """Lock the public parser surface before the runtime is split into modules."""
    parser = star_forge.build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    actual = {
        name: sorted(
            option
            for action in command_parser._actions
            for option in action.option_strings
        )
        for name, command_parser in subparsers.choices.items()
    }
    expected = {}
    for line in """
run|--adopt-root,--fast-mvp,--global-learnings,--help,--mode,--no-agents,--no-auto-init,--no-hooks,--objective,--product-slug,--profile,--project,--strict,-h
init|--adopt-root,--fast-mvp,--force,--help,--no-agents,--no-hooks,--product-slug,--profile,--project,-h
approve-blueprint|--help,--project,-h
approve-change|--change,--help,--project,-h
validate-plan|--file,--help,--project,--strict,-h
migrate-plan|--file,--help,--output,--project,-h
status|--help,--project,-h
doctor|--active-plugin-root,--codex-home,--help,--plugin-root,--source-root,--strict,-h
quality|--help,--include-files,--project,--strict,-h
verify|--command,--help,--noop,--project,--strict,--summary,--task,--timeout,-h
browser-run|--console-evidence,--degraded,--help,--interaction-evidence,--live-manifest,--no-require-console,--no-require-interaction,--no-require-viewports,--project,--require-console,--require-interaction,--require-server-lease,--require-viewports,--scenario,--screenshot,--server-lease,--strict,--summary,--task,--url,--viewport,-h
preview-proof|--deployment-metadata,--expect-status,--help,--project,--smoke-checks,--strict,--task,--url,-h
proof-run|--artifact,--help,--profile,--project,--strict,--task,-h
native-ios-proof|--build-result,--help,--launch-result,--project,--scheme,--screenshot,--simulator,--strict,--task,--test-result,--ui-snapshot,-h
native-macos-proof|--app-bundle,--app-name,--build-result,--bundle-id,--help,--packaging-note,--project,--run-result,--screenshot,--signing-note,--strict,--task,--test-result,-h
security-handoff-packet|--help,--input,--kind,--project,--strict,-h
security-proof|--artifact,--findings,--help,--profile,--project,--scanner,--scanner-version,--strict,--task,-h
source-packet-proof|--help,--input,--profile,--project,--strict,--task,-h
source-packet-github-pr-review|--help,--input,--project,--strict,-h
server-lease|--action,--base-url,--command,--help,--owner,--pid,--port,--project,-h
review|--help,--project,--strict,-h
waive|--finding,--help,--project,--reason,-h
complete-task|--changed-file,--help,--project,--summary,--task,-h
done|--help,--project,--strict,--write-summary,-h
learn|--category,--confidence,--detail,--global-learnings,--help,--project,--rule,--source,--title,--trigger,-h
agents-install|--help,--project,-h
self-test|--help,--strict,-h
hook|--help,-h
post-hook|--help,-h
prompt-hook|--help,-h
session-start-hook|--help,-h
subagent-start-hook|--help,-h
subagent-stop-hook|--help,-h
stop-hook|--help,-h
pre-compact-hook|--help,-h
""".strip().splitlines():
        command, options = line.split("|", 1)
        expected[command] = options.split(",")
    assert actual == expected


# --------------------------------------------------------------------- runner


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
    print(f"\ntest_star_forge.py: {passed} passed, {len(failed)} failed, {len(tests)} total")
    if failed:
        print("failed tests: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
