#!/usr/bin/env python3
"""Focused tests for Star Forge live proof command surfaces.

Plain-python suite. Run with: python3 tests/test_live_proof_commands.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "star_forge.py"
SPEC = importlib.util.spec_from_file_location("star_forge", SCRIPT)
assert SPEC and SPEC.loader
star_forge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(star_forge)

from live_collectors import common as live_common
from live_collectors import browser_playwright
from live_collectors import preview as live_preview

os.environ["STAR_FORGE_LEARNINGS_HOME"] = tempfile.mkdtemp(prefix="star-forge-live-test-learnings-")

PLAN_HEADER = (
    "| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |\n"
    "|------|-------------|--------|------|-------|---------|--------|----------|\n"
)
REAL_VERIFY = "python3 -c \"print('ok')\""


def run_cli(args: list[str]) -> tuple[int, dict[str, Any], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = star_forge.main(args)
    out = stdout.getvalue()
    err = stderr.getvalue()
    payload = json.loads(out) if out.strip().startswith("{") else {}
    return code, payload, err


def init_project(project: Path) -> None:
    code, payload, err = run_cli(["init", "--project", str(project), "--no-agents"])
    assert code == 0, err or payload
    (project / "src").mkdir(exist_ok=True)
    (project / "src" / "app.py").write_text("print('hello live proof')\n", encoding="utf-8")
    (project / "Plan.md").write_text(
        "# Plan.md\n\n" + PLAN_HEADER
        + f"| SF-1 | Build live proof test app | ready | solo | src/app.py | - | {REAL_VERIFY} | - |\n",
        encoding="utf-8",
    )


def commit_all(project: Path, message: str = "snapshot") -> str:
    subprocess.run(["git", "add", "."], cwd=str(project), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(
        ["git", "-c", "user.name=Star Forge Test", "-c", "user.email=star-forge@example.invalid", "commit", "-m", message],
        cwd=str(project),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(project), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc.stdout.strip()


def force_add(project: Path, rel_path: str) -> None:
    subprocess.run(["git", "add", "-f", rel_path], cwd=str(project), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_png(path: Path, width: int = 64, height: int = 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + ihdr + b"\x00\x00\x00\x00")
    return path


def write_native_macos_result(
    project: Path,
    root: Path,
    kind: str,
    *,
    name: str | None = None,
    stdout_name: str | None = None,
    stderr_name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[Path, Path, Path]:
    result_name = name or kind
    stdout = write_text(root / (stdout_name or f"{result_name}-stdout.txt"), "READY\n" if kind == "run" else "")
    stderr = write_text(root / (stderr_name or f"{result_name}-stderr.txt"), "")
    payload: dict[str, Any] = {
        "schema": "star-forge.native-macos.result.v1",
        "kind": kind,
        "command_argv": [sys.executable, "-c", "print('ok')"],
        "shell": False,
        "cwd": ".",
        "executable_path": sys.executable,
        "started_at": "2026-06-18T00:00:00Z",
        "ended_at": "2026-06-18T00:00:01Z",
        "duration_seconds": 0.01,
        "timeout_seconds": 5,
        "timed_out": False,
        "returncode": 0,
        "exit_code": 0,
        "signal": None,
        "stdout_artifact": star_forge.relative_to_project(stdout, project),
        "stderr_artifact": star_forge.relative_to_project(stderr, project),
        "stdout_bytes": stdout.stat().st_size,
        "stderr_bytes": stderr.stat().st_size,
        "success": True,
    }
    if extra:
        payload.update(extra)
    return write_json(root / f"{result_name}.json", payload), stdout, stderr


def write_native_ios_result(root: Path, kind: str, *, extra: dict[str, Any] | None = None) -> Path:
    payload: dict[str, Any] = {
        "schema": "star-forge.native-ios.result.v1",
        "kind": kind,
        "success": True,
        "simulator_runtime": "iOS 18",
    }
    if extra:
        payload.update(extra)
    return write_json(root / f"{kind}.json", payload)


def write_native_macos_identity_artifacts(project: Path, root: Path, *, app_name: str = "Test", bundle_id: str = "com.example.Test") -> tuple[Path, Path, Path, Path]:
    import plistlib

    app = project / "build" / f"{app_name}.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / app_name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (macos / app_name).chmod(0o755)
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleName": app_name, "CFBundleExecutable": app_name}, handle)
    metadata = write_json(root / "app-bundle-metadata.json", {
        "schema": "star-forge.native-macos.app-bundle-metadata.v1",
        "metadata_only": True,
        "valid": True,
        "app_bundle": star_forge.relative_to_project(app, project),
        "bundle_id": bundle_id,
        "app_name": app_name,
        "display_name": app_name,
        "executable_name": app_name,
        "executable_exists": True,
    })
    signing = write_json(root / "signing-note.json", {"schema": "star-forge.native-macos.note.v1", "kind": "signing", "status": "not_checked", "metadata_only": True})
    packaging = write_json(root / "packaging-note.json", {"schema": "star-forge.native-macos.note.v1", "kind": "packaging", "status": "not_checked", "metadata_only": True})
    return app, metadata, signing, packaging


def live_dir(project: Path, collector: str, task: str = "SF-1") -> Path:
    return live_common.live_collector_dir(project, task, collector)


def browser_interaction_payload(url: str = "http://127.0.0.1:4173/") -> dict[str, Any]:
    parsed, url_problems = browser_playwright.validate_url(url)
    assert not url_problems
    allowed_origin = browser_playwright.normalize_origin(parsed)
    request = browser_playwright.browser_url_safety_evidence(url, allowed_local_origins=[allowed_origin])
    request.update({
        "method": "GET",
        "resource_type": "document",
        "navigation": True,
    })
    return {
        "schema": "star-forge.browser-interaction.v1",
        "ready": [{"viewport": "desktop", "passed": True}, {"viewport": "mobile", "passed": True}],
        "actions": [],
        "assertions": [],
        "request_safety": {
            "schema": "star-forge.browser-request-safety.v1",
            "service_workers": browser_playwright.SERVICE_WORKERS_MODE,
            "connection_control": browser_playwright.BROWSER_NETWORK_CONTROL_MODE,
            "websocket_routing": browser_playwright.WEBSOCKET_ROUTING_MODE,
            "allowed_local_origins": [allowed_origin],
            "requests": [dict(request, viewport="desktop"), dict(request, viewport="mobile")],
            "websockets": [],
            "final_urls": [dict(request, viewport="desktop"), dict(request, viewport="mobile")],
            "blocked_count": 0,
            "websocket_blocked_count": 0,
            "webrtc": {"mode": browser_playwright.WEBRTC_CONTROL_MODE, "init_script": True},
        },
    }


def write_manifest(
    project: Path,
    collector: str,
    artifacts: dict[str, Path],
    *,
    task: str = "SF-1",
    summary: dict[str, Any] | None = None,
    degraded: bool = False,
    source_hash: str | None = None,
    runtime_hash: str | None = None,
) -> Path:
    current_source = source_hash if source_hash is not None else star_forge.source_hash(project)
    current_runtime = runtime_hash if runtime_hash is not None else live_common.compute_runtime_asset_hash(project)
    return live_common.write_live_manifest(
        project,
        task=task,
        collector=collector,
        command_argv=["test-collector", collector],
        tool_versions={"test": "1"},
        artifacts=artifacts,
        summary=summary or {},
        degraded=degraded,
        source_hash_before=current_source,
        source_hash_after=current_source,
        runtime_asset_hash=current_runtime,
    )


def valid_preview_http_payload(url: str = "http://93.184.216.34/", *, expected_status: int = 200) -> dict[str, Any]:
    return {
        "schema": "star-forge.preview-http.v1",
        "attempted": True,
        "method": "GET",
        "url": url,
        "final_url": url,
        "status": expected_status,
        "expected_status": expected_status,
        "ok": True,
        "redirect_chain": [],
        "connected_ips": ["93.184.216.34"],
        "connection_pinning": {
            "strategy": "http-connect-vetted-ip",
            "https": "fail-closed",
        },
    }


def write_clean_security_fixture(project: Path, *, source_binding: dict[str, Any] | None = None) -> tuple[Path, Path]:
    root = live_dir(project, "security")
    scanner_input = write_json(root / "scanner-input.json", {"schema": "codex.security.report.v1", "findings": []})
    scanner_hash = star_forge.file_sha256(scanner_input)
    input_hash_payload = {
        "schema": "star-forge.security-input-hash.v1",
        "input_path": star_forge.relative_to_project(scanner_input, project),
        "declared_sha256": scanner_hash,
        "actual_sha256": scanner_hash,
        "matches": True,
    }
    input_hash = write_json(root / "input-hash.json", input_hash_payload)
    findings = write_json(root / "normalized-findings.json", {
        "schema": "star-forge.normalized-security-findings.v1",
        "task": "SF-1",
        "profile": "security-diff",
        "source_schema": "codex.security.report.v1",
        "scanner": "codex-security",
        "scanner_version": "1.0",
        "findings": [],
        "summary": {"finding_count": 0},
    })
    handoff = write_json(root / "handoff-input.json", {
        "schema": "star-forge.security-handoff-input.v1",
        "task": "SF-1",
        "profile": "security-diff",
        "kind": "security-diff",
        "provenance": {
            "scanner": "codex-security",
            "scanner_version": "1.0",
            "source_schema": "codex.security.report.v1",
            "schema_family": "codex-security",
            "trusted_schema": True,
        },
        "scanner": "codex-security",
        "scanner_version": "1.0",
        "ruleset": {"name": "default"},
        "scan_scope": "changed-files",
        "source_binding": source_binding or {"source_hash": star_forge.source_hash(project)},
        "input_hash": input_hash_payload,
        "normalized_findings": {
            "path": star_forge.relative_to_project(findings, project),
            "sha256": star_forge.file_sha256(findings),
            "finding_count": 0,
        },
        "problems": [],
    })
    redaction = write_json(root / "redaction-report.json", {
        "schema": "star-forge.security-redaction-report.v1",
        "counts": {"secret_values": 0, "sensitive_keys": 0, "home_paths": 0, "temp_paths": 0, "env_values": 0},
    })
    manifest = write_manifest(
        project,
        "security",
        {"handoff": handoff, "findings": findings, "input-hash": input_hash, "redaction-report": redaction},
        summary={
            "trusted_provenance": True,
            "ruleset": {"name": "default"},
            "scan_scope": "changed-files",
            "input_hash": scanner_hash,
        },
    )
    return manifest, findings


def problem_rules(payload: dict[str, Any]) -> set[str]:
    return {str(item.get("rule")) for item in payload.get("problems", []) if isinstance(item, dict)}


def assert_pass(code: int, payload: dict[str, Any]) -> None:
    assert code == 0, payload
    assert payload["verdict"] == "PASS", payload
    assert payload.get("artifact"), payload


def assert_fail(code: int, payload: dict[str, Any], rule: str) -> None:
    assert code == 1, payload
    assert payload["verdict"] == "FAIL", payload
    assert rule in problem_rules(payload), payload.get("problems")


def rewrite_manifest(path: Path, mutator: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutator(payload)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def refresh_manifest_artifact_hash(manifest: Path, project: Path, artifact: Path) -> None:
    rel = star_forge.relative_to_project(artifact, project)
    digest = star_forge.file_sha256(artifact)
    size = artifact.stat().st_size

    def update(payload: dict[str, Any]) -> None:
        for record in payload.get("artifacts", []):
            if isinstance(record, dict) and str(record.get("path") or "") == rel:
                record["sha256"] = digest
                record["bytes"] = size
        payload.setdefault("raw_artifact_hashes", {})[rel] = {"sha256": digest, "bytes": size}

    rewrite_manifest(manifest, update)


def refresh_github_transcript_hashes(manifest: Path, project: Path, transcript: Path, commands: list[Any]) -> None:
    rel = star_forge.relative_to_project(transcript, project)
    digest = star_forge.file_sha256(transcript)
    size = transcript.stat().st_size

    def update(payload: dict[str, Any]) -> None:
        for record in payload.get("artifacts", []):
            if isinstance(record, dict) and str(record.get("path") or "") == rel:
                record["sha256"] = digest
                record["bytes"] = size
        payload.setdefault("raw_artifact_hashes", {})[rel] = {"sha256": digest, "bytes": size}
        summary = payload.setdefault("summary", {})
        if isinstance(summary, dict):
            summary["read_only_commands"] = commands
            summary["read_only_transcript_sha256"] = digest
            provenance = summary.setdefault("live_provenance", {})
            if isinstance(provenance, dict):
                provenance["operation_transcript_sha256"] = digest

    rewrite_manifest(manifest, update)


def test_common_manifest_fields_and_redaction() -> None:
    secret = "sk-" + "abc123def456ghi789jkl012mno345"
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "preview")
        artifact = write_text(root / "note.txt", "hello\n")
        manifest = write_manifest(
            project,
            "preview",
            {"note": artifact},
            summary={"note": "token=" + secret, "path": str(Path.home() / "private.txt")},
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        for field in live_common.REQUIRED_MANIFEST_FIELDS:
            assert field in payload
        assert payload["schema"] == live_common.LIVE_MANIFEST_SCHEMA
        assert payload["collector"] == "preview"
        assert payload["source_hash_after"] == star_forge.source_hash(project)
        assert secret not in json.dumps(payload)
        assert "[REDACTED_SECRET]" in json.dumps(payload)
        assert payload["redaction_report"]["home_paths"] >= 1


def test_proof_run_strict_rejects_degraded_source_and_runtime_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "security")
        artifact = write_text(root / "proof.json", "{}\n")
        manifest = write_manifest(project, "security", {"proof": artifact})
        code, payload, err = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "security", "--artifact", str(manifest), "--strict"])
        assert err == ""
        assert_fail(code, payload, "proof-profile")

        degraded = write_manifest(project, "security", {"proof": artifact}, degraded=True)
        code, payload, _ = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "security", "--artifact", str(degraded), "--strict"])
        assert_fail(code, payload, "manifest-degraded")

        stale = write_manifest(project, "security", {"proof": artifact}, source_hash="stale")
        code, payload, _ = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "security", "--artifact", str(stale), "--strict"])
        assert_fail(code, payload, "manifest-source")

        fresh = write_manifest(project, "security", {"proof": artifact})
        write_text(project / ".starforge" / "runtime" / "server.json", "{}\n")
        code, payload, _ = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "security", "--artifact", str(fresh), "--strict"])
        assert_fail(code, payload, "manifest-runtime")


def test_proof_run_strict_rejects_profiles_with_dedicated_proof_commands() -> None:
    cases = [
        ("security", "security", "security-proof"),
        ("security", "security-diff", "security-proof"),
        ("native-ios", "native-ios", "native-ios-proof"),
        ("native-macos", "native-macos", "native-macos-proof"),
    ]
    for collector, profile, command_name in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            root = live_dir(project, collector)
            artifact = write_text(root / "forged-proof.json", "{}\n")
            manifest = write_manifest(project, collector, {"proof": artifact})
            code, payload, _ = run_cli([
                "proof-run", "--project", str(project), "--task", "SF-1",
                "--profile", profile, "--artifact", str(manifest), "--strict",
            ])
            assert_fail(code, payload, "proof-profile")
            messages = " ".join(str(item.get("message") or "") for item in payload.get("problems", []) if isinstance(item, dict))
            assert command_name in messages, payload.get("problems")


def test_proof_run_rejects_github_source_packet_profiles() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "github")
        artifact = write_text(root / "proof.json", "{}\n")
        manifest = write_manifest(project, "github", {"proof": artifact})
        for profile in ("github-pr-review", "production-review"):
            code, payload, _ = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", profile, "--artifact", str(manifest), "--strict"])
            assert_fail(code, payload, "proof-profile")
            messages = " ".join(str(item.get("message") or "") for item in payload.get("problems", []) if isinstance(item, dict))
            assert "source-packet-proof" in messages
            assert "source-packet-github-pr-review" in messages


def test_proof_run_strict_rejects_malformed_and_out_of_scope_manifest_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "security")
        artifact = write_text(root / "proof.json", "{}\n")
        manifest = write_manifest(project, "security", {"proof": artifact})
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["artifacts"] = "not-artifact-records"
        manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        code, payload, _ = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "security", "--artifact", str(manifest), "--strict"])
        assert_fail(code, payload, "manifest-shape")

        external_artifact = write_text(project / "fixtures" / "github-proof.json", "{}\n")
        manifest = write_manifest(project, "security", {"proof": external_artifact})
        code, payload, _ = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "security", "--artifact", str(manifest), "--strict"])
        assert_fail(code, payload, "artifact-scope")

        manifest = write_manifest(project, "security", {"proof": artifact})
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["raw_artifact_hashes"][star_forge.relative_to_project(external_artifact, project)] = {
            "path": star_forge.relative_to_project(external_artifact, project),
            "sha256": star_forge.file_sha256(external_artifact),
        }
        manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        code, payload, _ = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "security", "--artifact", str(manifest), "--strict"])
        assert_fail(code, payload, "artifact-scope")


def test_preview_proof_happy_path_and_unsafe_url() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "preview")
        url = "http://93.184.216.34/"
        http = write_json(root / "http.json", valid_preview_http_payload(url))
        deployment = write_json(root / "deployment.json", {"source_hash": star_forge.source_hash(project), "deployment_id": "dep-1"})
        smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})
        write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": url})

        code, payload, _ = run_cli([
            "preview-proof", "--project", str(project), "--task", "SF-1",
            "--url", url, "--expect-status", "200",
            "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
        ])
        assert_pass(code, payload)

        code, payload, _ = run_cli([
            "preview-proof", "--project", str(project), "--task", "SF-1",
            "--url", "file:///tmp/preview", "--deployment-metadata", str(deployment),
            "--smoke-checks", str(smoke), "--strict",
        ])
        assert_fail(code, payload, "preview-url")


def test_preview_proof_commit_binding_rejects_uncommitted_source_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        head = commit_all(project)
        write_text(project / "src" / "app.py", "print('dirty preview source')\n")
        root = live_dir(project, "preview")
        url = "http://93.184.216.34/"
        http = write_json(root / "http.json", valid_preview_http_payload(url))
        deployment = write_json(root / "deployment.json", {"commit_sha": head, "deployment_id": "dep-1"})
        smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})
        write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": url})
        code, payload, _ = run_cli([
            "preview-proof", "--project", str(project), "--task", "SF-1",
            "--url", url, "--expect-status", "200",
            "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
        ])
        assert_fail(code, payload, "preview-source-binding")


def test_preview_source_hash_binding_rejects_dirty_build_inputs_after_deployment_metadata() -> None:
    cases = [
        ("modified Makefile", "Makefile", "build:\n\t@echo before\n", "build:\n\t@echo after\n"),
        ("untracked Dockerfile variant", "Dockerfile.preview", None, "FROM scratch\n"),
    ]
    for _name, rel_path, before, after in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            if before is not None:
                write_text(project / rel_path, before)
            root = live_dir(project, "preview")
            url = "http://93.184.216.34/"
            http = write_json(root / "http.json", valid_preview_http_payload(url))
            deployment = write_json(root / "deployment.json", {"source_hash": star_forge.source_hash(project), "deployment_id": "dep-1"})
            smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})
            write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": url})
            write_text(project / rel_path, after)

            code, payload, _ = run_cli([
                "preview-proof", "--project", str(project), "--task", "SF-1",
                "--url", url, "--expect-status", "200",
                "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
            ])
            assert_fail(code, payload, "preview-source-binding")


def test_preview_source_hash_binding_rejects_clean_committed_build_config_after_deployment_metadata() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_text(project / "build.gradle.kts", "plugins { id(\"com.android.application\") version \"1.0\" }\n")
        commit_all(project, "gradle config v1")
        old_source = star_forge.source_hash(project)
        root = live_dir(project, "preview")
        url = "http://93.184.216.34/"
        http = write_json(root / "http.json", valid_preview_http_payload(url))
        deployment = write_json(root / "deployment.json", {"source_hash": old_source, "deployment_id": "dep-1"})
        smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})
        write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": url}, source_hash=old_source)

        write_text(project / "build.gradle.kts", "plugins { id(\"com.android.application\") version \"2.0\" }\n")
        commit_all(project, "gradle config v2")
        assert star_forge.source_hash(project) != old_source
        assert not star_forge.source_dirty_entries(star_forge.git_status(project))

        code, payload, _ = run_cli([
            "preview-proof", "--project", str(project), "--task", "SF-1",
            "--url", url, "--expect-status", "200",
            "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
        ])
        assert_fail(code, payload, "preview-source-binding")


def test_preview_source_hash_binding_rejects_tracked_generated_dir_changes_after_deployment_metadata() -> None:
    cases = [
        ("build/proof-preview.js", "console.log('preview proof build v1')\n", "console.log('preview proof build v2')\n"),
        ("dist/proof-preview.js", "console.log('preview proof dist v1')\n", "console.log('preview proof dist v2')\n"),
        ("target/proof-preview.txt", "target preview proof v1\n", "target preview proof v2\n"),
    ]
    for rel_path, before, after in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            (project / ".gitignore").write_text("build/\ndist/\ntarget/\n", encoding="utf-8")
            tracked = project / rel_path
            tracked.parent.mkdir(parents=True, exist_ok=True)
            tracked.write_text(before, encoding="utf-8")
            force_add(project, rel_path)
            commit_all(project, f"{rel_path} v1")
            old_source = star_forge.source_hash(project)
            root = live_dir(project, "preview")
            url = "http://93.184.216.34/"
            http = write_json(root / "http.json", valid_preview_http_payload(url))
            deployment = write_json(root / "deployment.json", {"source_hash": old_source, "deployment_id": "dep-1"})
            smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})
            write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": url}, source_hash=old_source)

            tracked.write_text(after, encoding="utf-8")
            commit_all(project, f"{rel_path} v2")
            assert star_forge.source_hash(project) != old_source
            assert not star_forge.source_dirty_entries(star_forge.git_status(project))

            code, payload, _ = run_cli([
                "preview-proof", "--project", str(project), "--task", "SF-1",
                "--url", url, "--expect-status", "200",
                "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
            ])
            assert_fail(code, payload, "preview-source-binding")


def test_preview_proof_strict_rejects_weak_http_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "preview")
        url = "http://93.184.216.34/"
        deployment = write_json(root / "deployment.json", {"source_hash": star_forge.source_hash(project), "deployment_id": "dep-1"})
        smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})

        def run_with_http(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            http = write_json(root / "http.json", payload)
            write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": url})
            code, proof_payload, _ = run_cli([
                "preview-proof", "--project", str(project), "--task", "SF-1",
                "--url", url, "--expect-status", "200",
                "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
            ])
            return code, proof_payload

        code, payload = run_with_http({"status": 200})
        assert_fail(code, payload, "preview-http")

        missing_ips = valid_preview_http_payload(url)
        missing_ips.pop("connected_ips")
        code, payload = run_with_http(missing_ips)
        assert_fail(code, payload, "preview-url")

        missing_pinning = valid_preview_http_payload(url)
        missing_pinning.pop("connection_pinning")
        code, payload = run_with_http(missing_pinning)
        assert_fail(code, payload, "preview-http")

        mismatch = valid_preview_http_payload(url)
        mismatch["url"] = "http://93.184.216.35/"
        code, payload = run_with_http(mismatch)
        assert_fail(code, payload, "preview-url")


def test_preview_proof_strict_rejects_https_without_sni_safe_pinning() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "preview")
        url = "https://93.184.216.34/"
        http_payload = valid_preview_http_payload(url)
        http_payload["connection_pinning"] = {
            "strategy": "http-connect-vetted-ip",
            "https": "fail-closed",
        }
        http = write_json(root / "http.json", http_payload)
        deployment = write_json(root / "deployment.json", {"source_hash": star_forge.source_hash(project), "deployment_id": "dep-1"})
        smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})
        write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": url})
        code, payload, _ = run_cli([
            "preview-proof", "--project", str(project), "--task", "SF-1",
            "--url", url, "--expect-status", "200",
            "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
        ])
        assert_fail(code, payload, "preview-http")


def test_preview_proof_strict_rejects_forged_https_sni_pinning_after_hash_refresh() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "preview")
        url = "https://93.184.216.34/"
        http_payload = valid_preview_http_payload(url)
        http_payload["connection_pinning"] = {
            "strategy": "https-connect-vetted-ip-sni-safe",
            "sni_safe": True,
            "server_hostname": "93.184.216.34",
        }
        http = write_json(root / "http.json", http_payload)
        deployment = write_json(root / "deployment.json", {"source_hash": star_forge.source_hash(project), "deployment_id": "dep-1"})
        smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})
        manifest = write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": url})
        refresh_manifest_artifact_hash(manifest, project, http)
        code, payload, _ = run_cli([
            "preview-proof", "--project", str(project), "--task", "SF-1",
            "--url", url, "--expect-status", "200",
            "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
        ])
        assert_fail(code, payload, "preview-http")


def test_preview_proof_requires_manifest_records_raw_hashes_and_current_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "preview")
        http = write_json(root / "http.json", {"status": 200})
        deployment = write_json(root / "deployment.json", {"source_hash": star_forge.source_hash(project), "deployment_id": "dep-1"})
        smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})
        args = [
            "preview-proof", "--project", str(project), "--task", "SF-1",
            "--url", "https://93.184.216.34", "--expect-status", "200",
            "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
        ]

        manifest = write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": "https://93.184.216.34"})
        rewrite_manifest(manifest, lambda payload: payload.update({"artifacts": [], "raw_artifact_hashes": {}}))
        code, payload, _ = run_cli(args)
        assert_fail(code, payload, "artifact-hash")

        manifest = write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": "https://93.184.216.34"})
        smoke_rel = star_forge.relative_to_project(smoke, project)
        rewrite_manifest(manifest, lambda payload: payload["raw_artifact_hashes"].pop(smoke_rel, None))
        code, payload, _ = run_cli(args)
        assert_fail(code, payload, "artifact-hash")

        write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": "https://93.184.216.34"})
        write_json(deployment, {"source_hash": star_forge.source_hash(project), "deployment_id": "dep-1", "mutated": True})
        code, payload, _ = run_cli(args)
        assert_fail(code, payload, "artifact-hash")


def test_preview_proof_rejects_loopback_summary_flag_without_lease_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "preview")
        url = "http://127.0.0.1:4173"
        http = write_json(root / "http.json", {"status": 200, "final_url": url})
        deployment = write_json(root / "deployment.json", {"source_hash": star_forge.source_hash(project), "deployment_id": "dep-1"})
        smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})
        write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": url, "server_lease": True, "local_preview_mode": True})
        code, payload, _ = run_cli([
            "preview-proof", "--project", str(project), "--task", "SF-1",
            "--url", url, "--expect-status", "200",
            "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
        ])
        assert_fail(code, payload, "preview-localhost")


def test_browser_run_strict_requires_live_manifest_and_json_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "browser")
        desktop = make_png(root / "desktop.png", 1280, 800)
        mobile = make_png(root / "mobile.png", 390, 844)
        url = "http://127.0.0.1:4173/"
        interaction = write_json(root / "interaction.json", browser_interaction_payload(url))
        console = write_json(root / "console.json", {"events": []})
        lease = write_json(root / "server-lease.json", {
            "schema": "star-forge.server-lease.v1",
            "project": str(project),
            "origin": "http://127.0.0.1:4173",
            "port": 4173,
            "pid": os.getpid(),
            "command": "python3 -m http.server 4173",
            "source_hash": star_forge.source_hash(project),
            "runtime_asset_hash": live_common.compute_runtime_asset_hash(project, exclude_paths=[project / ".starforge" / "runtime" / "server.json"]),
        })
        manifest = write_manifest(
            project,
            "browser",
            {"desktop": desktop, "mobile": mobile, "interaction": interaction, "console": console},
            summary={"url": url},
        )
        base_args = [
            "browser-run", "--project", str(project), "--task", "SF-1",
            "--scenario", "happy", "--url", url,
            "--viewport", f"desktop=1280x800:{desktop}",
            "--viewport", f"mobile=390x844:{mobile}",
            "--interaction-evidence", str(interaction),
            "--console-evidence", str(console),
            "--server-lease", str(lease),
            "--strict",
        ]
        code, payload, _ = run_cli(base_args)
        assert_fail(code, payload, "manifest-missing")

        code, payload, _ = run_cli(base_args[:-1] + ["--live-manifest", str(manifest), "--strict"])
        assert_pass(code, payload)

        interaction_payload = browser_interaction_payload(url)
        interaction_payload["request_safety"].pop("service_workers", None)
        write_json(interaction, interaction_payload)
        refresh_manifest_artifact_hash(manifest, project, interaction)
        code, payload, _ = run_cli(base_args[:-1] + ["--live-manifest", str(manifest), "--strict"])
        assert_fail(code, payload, "browser-request-safety")

        write_json(interaction, browser_interaction_payload(url))
        refresh_manifest_artifact_hash(manifest, project, interaction)
        rewrite_manifest(manifest, lambda payload: payload.update({"raw_artifact_hashes": {}}))
        code, payload, _ = run_cli(base_args[:-1] + ["--live-manifest", str(manifest), "--strict"])
        assert_fail(code, payload, "artifact-hash")

        console.write_text("not json\n", encoding="utf-8")
        write_manifest(
            project,
            "browser",
            {"desktop": desktop, "mobile": mobile, "interaction": interaction, "console": console},
            summary={"url": url},
        )
        code, payload, _ = run_cli(base_args[:-1] + ["--live-manifest", str(manifest), "--strict"])
        assert_fail(code, payload, "console-evidence")


def test_browser_run_strict_rejects_non_global_recorded_ip_evidence_after_hash_refresh() -> None:
    for evidence_key in ("requests", "final_urls"):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            root = live_dir(project, "browser")
            url = "http://127.0.0.1:4173/"
            desktop = make_png(root / "desktop.png", 1280, 800)
            mobile = make_png(root / "mobile.png", 390, 844)
            interaction_payload = browser_interaction_payload(url)
            for entry in interaction_payload["request_safety"][evidence_key]:
                entry["resolved_ips"] = ["100.64.0.1"]
                entry["allowed"] = True
            interaction = write_json(root / "interaction.json", interaction_payload)
            console = write_json(root / "console.json", {"events": []})
            lease = write_json(root / "server-lease.json", {
                "schema": "star-forge.server-lease.v1",
                "project": str(project),
                "origin": "http://127.0.0.1:4173",
                "port": 4173,
                "pid": os.getpid(),
                "command": "python3 -m http.server 4173",
                "source_hash": star_forge.source_hash(project),
                "runtime_asset_hash": live_common.compute_runtime_asset_hash(project, exclude_paths=[project / ".starforge" / "runtime" / "server.json"]),
            })
            manifest = write_manifest(
                project,
                "browser",
                {"desktop": desktop, "mobile": mobile, "interaction": interaction, "console": console},
                summary={"url": url},
            )
            refresh_manifest_artifact_hash(manifest, project, interaction)
            code, payload, _ = run_cli([
                "browser-run", "--project", str(project), "--task", "SF-1",
                "--scenario", "forged-ip", "--url", url,
                "--viewport", f"desktop=1280x800:{desktop}",
                "--viewport", f"mobile=390x844:{mobile}",
                "--interaction-evidence", str(interaction),
                "--console-evidence", str(console),
                "--live-manifest", str(manifest),
                "--server-lease", str(lease),
                "--strict",
            ])
            assert_fail(code, payload, "browser-request-safety")


def test_browser_run_strict_rejects_private_url_even_with_lease() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "browser")
        desktop = make_png(root / "desktop.png", 1280, 800)
        mobile = make_png(root / "mobile.png", 390, 844)
        interaction = write_json(root / "interaction.json", {"ready": [{"passed": True}], "actions": [], "assertions": []})
        console = write_json(root / "console.json", {"events": []})
        manifest = write_manifest(
            project,
            "browser",
            {"desktop": desktop, "mobile": mobile, "interaction": interaction, "console": console},
            summary={"url": "http://10.0.0.5:8080"},
        )
        lease = write_json(root / "server-lease.json", {
            "schema": "star-forge.server-lease.v1",
            "project": str(project),
            "origin": "http://10.0.0.5:8080",
            "port": 8080,
            "pid": os.getpid(),
            "command": "python3 -m http.server 8080",
            "source_hash": star_forge.source_hash(project),
            "runtime_asset_hash": live_common.compute_runtime_asset_hash(project, exclude_paths=[project / ".starforge" / "runtime" / "server.json"]),
        })
        code, payload, _ = run_cli([
            "browser-run", "--project", str(project), "--task", "SF-1",
            "--scenario", "private", "--url", "http://10.0.0.5:8080",
            "--viewport", f"desktop=1280x800:{desktop}",
            "--viewport", f"mobile=390x844:{mobile}",
            "--interaction-evidence", str(interaction),
            "--console-evidence", str(console),
            "--live-manifest", str(manifest),
            "--server-lease", str(lease),
            "--strict",
        ])
        assert_fail(code, payload, "browser-url")


def test_preview_proof_uses_dns_aware_url_safety() -> None:
    original = live_preview.socket.getaddrinfo

    def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        return [(live_preview.socket.AF_INET, live_preview.socket.SOCK_STREAM, 6, "", ("100.64.0.1", 80))]

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "preview")
        http = write_json(root / "http.json", {"status": 200, "final_url": "http://private.example.test/"})
        deployment = write_json(root / "deployment.json", {"source_hash": star_forge.source_hash(project), "deployment_id": "dep-1"})
        smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})
        write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": "http://private.example.test/"})
        live_preview.socket.getaddrinfo = fake_getaddrinfo
        try:
            code, payload, _ = run_cli([
                "preview-proof", "--project", str(project), "--task", "SF-1",
                "--url", "http://private.example.test/", "--expect-status", "200",
                "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
            ])
        finally:
            live_preview.socket.getaddrinfo = original
        assert_fail(code, payload, "preview-url")


def test_preview_proof_rejects_unsafe_connected_ip_evidence() -> None:
    for connected_ip in ("127.0.0.1", "100.64.0.1"):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            root = live_dir(project, "preview")
            http_payload = valid_preview_http_payload("http://93.184.216.34/")
            http_payload["connected_ips"] = [connected_ip]
            http = write_json(root / "http.json", http_payload)
            deployment = write_json(root / "deployment.json", {"source_hash": star_forge.source_hash(project), "deployment_id": "dep-1"})
            smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})
            write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": "http://93.184.216.34/"})
            code, payload, _ = run_cli([
                "preview-proof", "--project", str(project), "--task", "SF-1",
                "--url", "http://93.184.216.34/", "--expect-status", "200",
                "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
            ])
            assert_fail(code, payload, "preview-url")


def test_preview_proof_rejects_direct_shared_address_space_url() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "preview")
        url = "http://100.64.0.1/"
        http = write_json(root / "http.json", valid_preview_http_payload(url))
        deployment = write_json(root / "deployment.json", {"source_hash": star_forge.source_hash(project), "deployment_id": "dep-1"})
        smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})
        write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": url})
        code, payload, _ = run_cli([
            "preview-proof", "--project", str(project), "--task", "SF-1",
            "--url", url, "--expect-status", "200",
            "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
        ])
        assert_fail(code, payload, "preview-url")


def test_native_ios_requires_mcp_ui_and_then_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "native-ios")
        session = write_json(root / "session-defaults.json", {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"})
        transcript = write_json(root / "mcp-transcript.json", {"calls": [
            {"tool": "session_show_defaults", "result": {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"}},
            {"tool": "build_run_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
            {"tool": "test_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
            {"tool": "screenshot", "args": {"simulator": "iPhone 16"}},
        ]})
        build = write_native_ios_result(root, "build")
        launch = write_native_ios_result(root, "launch")
        test = write_native_ios_result(root, "test")
        write_manifest(
            project,
            "native-ios",
            {"session": session, "transcript": transcript, "build": build, "launch": launch, "test": test},
            summary={"app_identity": "com.example.App"},
        )

        base_args = [
            "native-ios-proof", "--project", str(project), "--task", "SF-1",
            "--scheme", "App", "--simulator", "iPhone 16",
            "--build-result", str(build), "--launch-result", str(launch), "--test-result", str(test),
            "--strict",
        ]
        code, payload, _ = run_cli(base_args)
        assert_fail(code, payload, "native-ios-ui")

        screenshot = make_png(root / "screenshot.png")
        write_manifest(
            project,
            "native-ios",
            {"session": session, "transcript": transcript, "build": build, "launch": launch, "test": test, "screenshot": screenshot},
            summary={"app_identity": "com.example.App"},
        )
        code, payload, _ = run_cli(base_args[:-1] + ["--screenshot", str(screenshot), "--strict"])
        assert_fail(code, payload, "native-ios-mcp-provenance")

        transcript = write_json(root / "mcp-transcript.json", {
            "source_hash": star_forge.source_hash(project),
            "exported_by": "test-agent",
            "mcp": {"tool_surface": "mcp", "server": "XcodeBuildMCP", "version": "test"},
            "calls": [
                {"tool": "session_show_defaults", "result": {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"}},
                {"tool": "build_run_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                {"tool": "test_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                {"tool": "screenshot", "args": {"simulator": "iPhone 16"}},
            ],
        })
        write_manifest(
            project,
            "native-ios",
            {"session": session, "transcript": transcript, "build": build, "launch": launch, "test": test, "screenshot": screenshot},
            summary={"app_identity": "com.example.App"},
        )
        code, payload, _ = run_cli(base_args[:-1] + ["--screenshot", str(screenshot), "--strict"])
        assert_pass(code, payload)

        transcript = write_json(root / "mcp-transcript.json", {"calls": [{"tool": "session_show_defaults"}]})
        write_manifest(
            project,
            "native-ios",
            {"session": session, "transcript": transcript, "build": build, "launch": launch, "test": test, "screenshot": screenshot},
            summary={"app_identity": "com.example.App"},
        )
        code, payload, _ = run_cli(base_args[:-1] + ["--screenshot", str(screenshot), "--strict"])
        assert_fail(code, payload, "native-ios-transcript-build")


def test_native_ios_strict_rejects_qualified_exec_fallback_tools() -> None:
    cases = [
        {"tool": "functions.exec_command", "arguments": {"cmd": "echo shell fallback"}},
        {"tool": "mcp__utility__exec_command", "arguments": {"cmd": "echo mcp shell fallback"}},
        {"tool": "run_command", "arguments": {"command": "echo forged evidence"}},
        {"tool": "run_command", "arguments": {"cmd": "sh -c echo forged"}},
        {"tool": "run_command", "arguments": {"command_args": ["sh", "-c", "echo forged"]}},
        {"tool": "run_command", "arguments": {"command": "open -a Simulator.app"}},
        {"tool": "run_command", "arguments": {"command": ["open", "-b", "com.apple.iphonesimulator"]}},
        {"tool": "run_command", "arguments": {"command": "/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app"}},
        {"tool": "run_command", "arguments": {"nested": {"command": "open -a Simulator.app"}}},
        {"tool": "run_command", "arguments": {"command_argv": ["xcrun", "simctl", "list"]}},
        {"tool": "run_command", "arguments": {"command-argv": ["xcodebuild", "-scheme", "App"]}},
        {"tool": "run_command", "arguments": {"command_args": ["env", "xcrun", "simctl", "list"]}},
        {"tool": "build_run_sim", "arguments": {"scheme": "App", "simulator": "iPhone 16", "commandLine": "xcrun simctl list"}},
        {"tool": "test_sim", "arguments": {"scheme": "App", "simulator": "iPhone 16", "command_line": ["xcrun", "simctl", "list"]}},
        {"tool": "screenshot", "arguments": {"simulator": "iPhone 16", "cmdLine": "sh -c echo forged"}},
        {"tool": "ui_snapshot", "arguments": {"simulator": "iPhone 16", "cmd_line": "xcrun simctl list"}},
        {"tool": "run_command", "arguments": {"shell_command": "open -a Simulator.app"}},
        {"tool": "run_command", "arguments": {"shell_command": "echo forged evidence"}},
        {"tool": "run_command", "arguments": {"shellCommand": "osascript -e 'tell application \"Simulator\" to activate'"}},
        {"tool": "run_command", "arguments": {"shellCmd": "echo forged evidence"}},
        {"tool": "run_command", "arguments": {"command_argv": ["sh", "-c", "echo forged"]}},
        {"tool": "build_run_sim", "arguments": {"scheme": "App", "simulator": "iPhone 16", "argv": ["env", "sh", "-c", "echo forged"]}},
        {"tool": "build_run_sim", "arguments": {"scheme": "App", "simulator": "iPhone 16", "command_argv": ["/usr/bin/env", "env", "bash", "-c", "echo forged"]}},
        {"tool": "launch_app", "arguments": {"simulator": "iPhone 16", "command_argv": ["env"]}},
        {"tool": "test_sim", "arguments": {"scheme": "App", "simulator": "iPhone 16", "argv": ["env", "-S", "zsh -c 'echo forged'"]}},
        {"tool": "test_sim", "arguments": {"scheme": "App", "simulator": "iPhone 16", "command_argv": ["env", "--split-string=pwsh -Command echo forged"]}},
    ]
    for shell_call in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            root = live_dir(project, "native-ios")
            session = write_json(root / "session-defaults.json", {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"})
            transcript = write_json(root / "mcp-transcript.json", {
                "source_hash": star_forge.source_hash(project),
                "exported_by": "test-agent",
                "mcp": {"tool_surface": "mcp", "server": "XcodeBuildMCP", "version": "test"},
                "calls": [
                    {"tool": "session_show_defaults", "result": {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"}},
                    {"tool": "build_run_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                    {"tool": "test_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                    {"tool": "screenshot", "args": {"simulator": "iPhone 16"}},
                    shell_call,
                ],
            })
            build = write_native_ios_result(root, "build")
            launch = write_native_ios_result(root, "launch")
            test = write_native_ios_result(root, "test")
            screenshot = make_png(root / "screenshot.png")
            write_manifest(
                project,
                "native-ios",
                {"session": session, "transcript": transcript, "build": build, "launch": launch, "test": test, "screenshot": screenshot},
                summary={"app_identity": "com.example.App"},
            )
            code, payload, _ = run_cli([
                "native-ios-proof", "--project", str(project), "--task", "SF-1",
                "--scheme", "App", "--simulator", "iPhone 16",
                "--build-result", str(build), "--launch-result", str(launch), "--test-result", str(test),
                "--screenshot", str(screenshot), "--strict",
            ])
            assert_fail(code, payload, "native-ios-shell-fallback")


def test_native_ios_strict_rejects_generic_shell_fallback_after_manifest_hash_refresh() -> None:
    cases = [
        {"tool": "run_command", "arguments": {"command": "echo forged evidence"}},
        {"tool": "run_command", "arguments": {"cmd": "sh -c echo forged"}},
        {"tool": "run_command", "arguments": {"command_args": ["sh", "-c", "echo forged"]}},
        {"tool": "run_command", "arguments": {"shell_command": "echo forged evidence"}},
        {"tool": "run_command", "arguments": {"command_argv": ["sh", "-c", "echo forged"]}},
        {"tool": "build_run_sim", "arguments": {"scheme": "App", "simulator": "iPhone 16", "argv": ["env", "sh", "-c", "echo forged"]}},
        {"tool": "build_run_sim", "arguments": {"scheme": "App", "simulator": "iPhone 16", "command_argv": ["/usr/bin/env", "env", "bash", "-c", "echo forged"]}},
        {"tool": "launch_app", "arguments": {"simulator": "iPhone 16", "command_argv": ["env"]}},
        {"tool": "test_sim", "arguments": {"scheme": "App", "simulator": "iPhone 16", "argv": ["env", "-S", "zsh -c 'echo forged'"]}},
        {"tool": "test_sim", "arguments": {"scheme": "App", "simulator": "iPhone 16", "command_argv": ["env", "--split-string=powershell -Command echo forged"]}},
    ]
    for shell_call in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            root = live_dir(project, "native-ios")
            session = write_json(root / "session-defaults.json", {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"})
            transcript_payload = {
                "source_hash": star_forge.source_hash(project),
                "exported_by": "test-agent",
                "mcp": {"tool_surface": "mcp", "server": "XcodeBuildMCP", "version": "test"},
                "calls": [
                    {"tool": "session_show_defaults", "result": {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"}},
                    {"tool": "build_run_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                    {"tool": "test_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                    {"tool": "screenshot", "args": {"simulator": "iPhone 16"}},
                ],
            }
            transcript = write_json(root / "mcp-transcript.json", transcript_payload)
            build = write_native_ios_result(root, "build")
            launch = write_native_ios_result(root, "launch")
            test = write_native_ios_result(root, "test")
            screenshot = make_png(root / "screenshot.png")
            manifest = write_manifest(
                project,
                "native-ios",
                {"session": session, "transcript": transcript, "build": build, "launch": launch, "test": test, "screenshot": screenshot},
                summary={"app_identity": "com.example.App"},
            )
            transcript_payload["calls"].append(shell_call)
            write_json(transcript, transcript_payload)
            refresh_manifest_artifact_hash(manifest, project, transcript)

            code, payload, _ = run_cli([
                "native-ios-proof", "--project", str(project), "--task", "SF-1",
                "--scheme", "App", "--simulator", "iPhone 16",
                "--build-result", str(build), "--launch-result", str(launch), "--test-result", str(test),
                "--screenshot", str(screenshot), "--strict",
            ])
            assert_fail(code, payload, "native-ios-shell-fallback")


def test_native_ios_strict_rejects_result_payload_shell_fallback_after_manifest_hash_refresh() -> None:
    cases = [
        {"tool": "build_run_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}, "result": {"command_argv": ["xcrun", "simctl", "list"]}},
        {"tool": "build_run_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}, "result": {"nested": {"commandLine": "xcrun simctl list"}}},
        {"tool": "test_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}, "result": {"shell_command": "open -a Simulator.app"}},
        {"tool": "test_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}, "result": {"cmdline": "osascript -e 'tell application \"Simulator\" to activate'"}},
        {"tool": "screenshot", "args": {"simulator": "iPhone 16"}, "result": {"argv": ["env", "sh", "-c", "echo forged"]}},
        {"tool": "ui_snapshot", "args": {"simulator": "iPhone 16"}, "result": {"details": {"command_argv": ["env", "-S", "zsh -c 'echo forged'"]}}},
        {"tool": "build_run_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}, "result": {"command_argv": ["/usr/bin/env", "--split-string=pwsh -Command echo forged"]}},
    ]
    for result_call in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            root = live_dir(project, "native-ios")
            session = write_json(root / "session-defaults.json", {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"})
            transcript_payload = {
                "source_hash": star_forge.source_hash(project),
                "exported_by": "test-agent",
                "mcp": {"tool_surface": "mcp", "server": "XcodeBuildMCP", "version": "test"},
                "calls": [
                    {"tool": "session_show_defaults", "result": {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"}},
                    {"tool": "build_run_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                    {"tool": "test_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                    {"tool": "screenshot", "args": {"simulator": "iPhone 16"}},
                ],
            }
            transcript = write_json(root / "mcp-transcript.json", transcript_payload)
            build = write_native_ios_result(root, "build")
            launch = write_native_ios_result(root, "launch")
            test = write_native_ios_result(root, "test")
            screenshot = make_png(root / "screenshot.png")
            manifest = write_manifest(
                project,
                "native-ios",
                {"session": session, "transcript": transcript, "build": build, "launch": launch, "test": test, "screenshot": screenshot},
                summary={"app_identity": "com.example.App"},
            )
            transcript_payload["calls"].append(result_call)
            write_json(transcript, transcript_payload)
            refresh_manifest_artifact_hash(manifest, project, transcript)

            code, payload, _ = run_cli([
                "native-ios-proof", "--project", str(project), "--task", "SF-1",
                "--scheme", "App", "--simulator", "iPhone 16",
                "--build-result", str(build), "--launch-result", str(launch), "--test-result", str(test),
                "--screenshot", str(screenshot), "--strict",
            ])
            assert_fail(code, payload, "native-ios-shell-fallback")


def test_native_ios_strict_requires_transcript_owned_mcp_provenance_and_source() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "native-ios")
        session = write_json(root / "session-defaults.json", {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"})
        transcript = write_json(root / "mcp-transcript.json", {"calls": [
            {"tool": "session_show_defaults", "result": {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"}},
            {"tool": "build_run_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
            {"tool": "test_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
            {"tool": "screenshot", "args": {"simulator": "iPhone 16"}},
        ]})
        build = write_native_ios_result(root, "build")
        launch = write_native_ios_result(root, "launch")
        test = write_native_ios_result(root, "test")
        screenshot = make_png(root / "screenshot.png")
        write_manifest(
            project,
            "native-ios",
            {"session": session, "transcript": transcript, "build": build, "launch": launch, "test": test, "screenshot": screenshot},
            summary={
                "app_identity": "com.example.App",
                "mcp_provenance": {
                    "tool_surface": "mcp",
                    "server": "XcodeBuildMCP",
                    "version": "test",
                    "exported_by": "manifest-only",
                },
                "source_hash": star_forge.source_hash(project),
            },
        )
        code, payload, _ = run_cli([
            "native-ios-proof", "--project", str(project), "--task", "SF-1",
            "--scheme", "App", "--simulator", "iPhone 16",
            "--build-result", str(build), "--launch-result", str(launch), "--test-result", str(test),
            "--screenshot", str(screenshot), "--strict",
        ])
        assert_fail(code, payload, "native-ios-mcp-provenance")
        assert "native-ios-source" in problem_rules(payload)


def test_native_ios_strict_rejects_schema_less_result_artifacts_after_manifest_hash_refresh() -> None:
    for target in ("build", "launch", "test"):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            root = live_dir(project, "native-ios")
            session = write_json(root / "session-defaults.json", {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"})
            transcript = write_json(root / "mcp-transcript.json", {
                "source_hash": star_forge.source_hash(project),
                "exported_by": "test-agent",
                "mcp": {"tool_surface": "mcp", "server": "XcodeBuildMCP", "version": "test"},
                "calls": [
                    {"tool": "session_show_defaults", "result": {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"}},
                    {"tool": "build_run_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                    {"tool": "test_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                    {"tool": "screenshot", "args": {"simulator": "iPhone 16"}},
                ],
            })
            build = write_native_ios_result(root, "build")
            launch = write_native_ios_result(root, "launch")
            test = write_native_ios_result(root, "test")
            screenshot = make_png(root / "screenshot.png")
            manifest = write_manifest(
                project,
                "native-ios",
                {"session": session, "transcript": transcript, "build": build, "launch": launch, "test": test, "screenshot": screenshot},
                summary={
                    "app_identity": "com.example.App",
                    "simulator": {"name": "iPhone 16", "runtime": "iOS 18"},
                    "simulator_runtime": "iOS 18",
                },
            )
            target_path = {"build": build, "launch": launch, "test": test}[target]
            write_json(target_path, {"success": True})
            refresh_manifest_artifact_hash(manifest, project, target_path)

            code, payload, _ = run_cli([
                "native-ios-proof", "--project", str(project), "--task", "SF-1",
                "--scheme", "App", "--simulator", "iPhone 16",
                "--build-result", str(build), "--launch-result", str(launch), "--test-result", str(test),
                "--screenshot", str(screenshot), "--strict",
            ])
            assert_fail(code, payload, "native-ios-result")


def test_native_ios_strict_rejects_result_command_line_aliases_after_manifest_hash_refresh() -> None:
    cases: list[tuple[str, dict[str, Any]]] = [
        ("build", {"commandLine": "xcrun simctl list"}),
        ("launch", {"command_line": ["xcrun", "simctl", "list"]}),
        ("test", {"details": {"cmdLine": "sh -c echo forged"}}),
        ("test", {"details": {"cmd_line": "xcrun simctl list"}}),
    ]
    for target, extra in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            root = live_dir(project, "native-ios")
            session = write_json(root / "session-defaults.json", {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"})
            transcript = write_json(root / "mcp-transcript.json", {
                "source_hash": star_forge.source_hash(project),
                "exported_by": "test-agent",
                "mcp": {"tool_surface": "mcp", "server": "XcodeBuildMCP", "version": "test"},
                "calls": [
                    {"tool": "session_show_defaults", "result": {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"}},
                    {"tool": "build_run_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                    {"tool": "test_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                    {"tool": "screenshot", "args": {"simulator": "iPhone 16"}},
                ],
            })
            build = write_native_ios_result(root, "build")
            launch = write_native_ios_result(root, "launch")
            test = write_native_ios_result(root, "test")
            screenshot = make_png(root / "screenshot.png")
            manifest = write_manifest(
                project,
                "native-ios",
                {"session": session, "transcript": transcript, "build": build, "launch": launch, "test": test, "screenshot": screenshot},
                summary={
                    "app_identity": "com.example.App",
                    "simulator": {"name": "iPhone 16", "runtime": "iOS 18"},
                    "simulator_runtime": "iOS 18",
                },
            )
            target_path = {"build": build, "launch": launch, "test": test}[target]
            payload = json.loads(target_path.read_text(encoding="utf-8"))
            payload.update(extra)
            write_json(target_path, payload)
            refresh_manifest_artifact_hash(manifest, project, target_path)

            code, payload, _ = run_cli([
                "native-ios-proof", "--project", str(project), "--task", "SF-1",
                "--scheme", "App", "--simulator", "iPhone 16",
                "--build-result", str(build), "--launch-result", str(launch), "--test-result", str(test),
                "--screenshot", str(screenshot), "--strict",
            ])
            assert_fail(code, payload, "native-ios-shell-fallback")


def test_native_ios_strict_rejects_result_env_wrapped_shells_after_manifest_hash_refresh() -> None:
    cases: list[tuple[str, list[str]]] = [
        ("build", ["env", "sh", "-c", "echo forged"]),
        ("build", ["env"]),
        ("launch", ["/usr/bin/env", "env", "bash", "-c", "echo forged"]),
        ("launch", ["env", "--split-string", "fish -c 'echo forged'"]),
        ("test", ["env", "-S", "zsh -c 'echo forged'"]),
        ("test", ["/usr/bin/env", "--split-string=pwsh -Command echo forged"]),
        ("test", ["env", "-S", "powershell -Command echo forged"]),
    ]
    for target, command_argv in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            root = live_dir(project, "native-ios")
            session = write_json(root / "session-defaults.json", {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"})
            transcript = write_json(root / "mcp-transcript.json", {
                "source_hash": star_forge.source_hash(project),
                "exported_by": "test-agent",
                "mcp": {"tool_surface": "mcp", "server": "XcodeBuildMCP", "version": "test"},
                "calls": [
                    {"tool": "session_show_defaults", "result": {"scheme": "App", "simulator": "iPhone 16", "runtime": "iOS 18"}},
                    {"tool": "build_run_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                    {"tool": "test_sim", "args": {"scheme": "App", "simulator": "iPhone 16"}},
                    {"tool": "screenshot", "args": {"simulator": "iPhone 16"}},
                ],
            })
            build = write_native_ios_result(root, "build")
            launch = write_native_ios_result(root, "launch")
            test = write_native_ios_result(root, "test")
            screenshot = make_png(root / "screenshot.png")
            manifest = write_manifest(
                project,
                "native-ios",
                {"session": session, "transcript": transcript, "build": build, "launch": launch, "test": test, "screenshot": screenshot},
                summary={
                    "app_identity": "com.example.App",
                    "simulator": {"name": "iPhone 16", "runtime": "iOS 18"},
                    "simulator_runtime": "iOS 18",
                },
            )
            target_path = {"build": build, "launch": launch, "test": test}[target]
            payload = json.loads(target_path.read_text(encoding="utf-8"))
            payload["command_argv"] = command_argv
            write_json(target_path, payload)
            refresh_manifest_artifact_hash(manifest, project, target_path)

            code, payload, _ = run_cli([
                "native-ios-proof", "--project", str(project), "--task", "SF-1",
                "--scheme", "App", "--simulator", "iPhone 16",
                "--build-result", str(build), "--launch-result", str(launch), "--test-result", str(test),
                "--screenshot", str(screenshot), "--strict",
            ])
            assert_fail(code, payload, "native-ios-shell-fallback")


def test_native_macos_requires_identity_then_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "native-macos")
        build, build_stdout, build_stderr = write_native_macos_result(project, root, "build")
        run, stdout, stderr = write_native_macos_result(project, root, "run", stdout_name="stdout.txt", stderr_name="stderr.txt", extra={
            "pid": os.getpid(),
            "gui_launch_failed": False,
            "cleanup_failed": False,
            "readiness": {"status": "observed"},
            "termination": {"attempted": True, "success": True},
            "cleanup": {"attempted": True, "success": True},
        })
        app = project / "build" / "Test.app"
        macos = app / "Contents" / "MacOS"
        macos.mkdir(parents=True)
        (macos / "Test").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (macos / "Test").chmod(0o755)
        import plistlib
        with (app / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleIdentifier": "com.example.Test", "CFBundleName": "Test", "CFBundleExecutable": "Test"}, handle)
        metadata = write_json(root / "app-bundle-metadata.json", {
            "schema": "star-forge.native-macos.app-bundle-metadata.v1",
            "metadata_only": True,
            "valid": True,
            "app_bundle": star_forge.relative_to_project(app, project),
            "bundle_id": "com.example.Test",
            "app_name": "Test",
            "display_name": "Test",
            "executable_name": "Test",
            "executable_exists": True,
        })
        signing = write_json(root / "signing-note.json", {"schema": "star-forge.native-macos.note.v1", "kind": "signing", "status": "not_checked", "metadata_only": True})
        packaging = write_json(root / "packaging-note.json", {"schema": "star-forge.native-macos.note.v1", "kind": "packaging", "status": "not_checked", "metadata_only": True})
        write_manifest(
            project,
            "native-macos",
            {
                "build": build,
                "build_stdout": build_stdout,
                "build_stderr": build_stderr,
                "run": run,
                "stdout": stdout,
                "stderr": stderr,
                "metadata": metadata,
                "signing": signing,
                "packaging": packaging,
            },
            summary={"app_bundle_metadata": star_forge.relative_to_project(metadata, project)},
        )

        base_args = [
            "native-macos-proof", "--project", str(project), "--task", "SF-1",
            "--build-result", str(build), "--run-result", str(run), "--app-bundle", str(app),
            "--signing-note", str(signing), "--packaging-note", str(packaging), "--strict",
        ]
        code, payload, _ = run_cli(base_args)
        assert_fail(code, payload, "native-macos-app-identity")

        code, payload, _ = run_cli(base_args[:-1] + ["--app-name", "Test", "--bundle-id", "com.example.Test", "--strict"])
        assert_pass(code, payload)


def test_native_macos_strict_rejects_unstructured_result_artifacts() -> None:
    cases = {
        "shell true": lambda payload: payload.update({"shell": True}),
        "missing argv": lambda payload: payload.pop("command_argv", None),
        "minimal success": lambda payload: (payload.clear(), payload.update({"success": True})),
    }
    for name, mutate in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            root = live_dir(project, "native-macos")
            build, build_stdout, build_stderr = write_native_macos_result(project, root, "build")
            build_payload = json.loads(build.read_text(encoding="utf-8"))
            mutate(build_payload)
            write_json(build, build_payload)
            run, stdout, stderr = write_native_macos_result(project, root, "run", stdout_name="stdout.txt", stderr_name="stderr.txt", extra={
                "pid": os.getpid(),
                "gui_launch_failed": False,
                "cleanup_failed": False,
                "readiness": {"status": "observed"},
                "termination": {"attempted": True, "success": True},
                "cleanup": {"attempted": True, "success": True},
            })
            app = project / "build" / f"{name.replace(' ', '-')}.app"
            macos = app / "Contents" / "MacOS"
            macos.mkdir(parents=True)
            (macos / "Test").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (macos / "Test").chmod(0o755)
            import plistlib
            with (app / "Contents" / "Info.plist").open("wb") as handle:
                plistlib.dump({"CFBundleIdentifier": "com.example.Test", "CFBundleName": "Test", "CFBundleExecutable": "Test"}, handle)
            metadata = write_json(root / "app-bundle-metadata.json", {
                "schema": "star-forge.native-macos.app-bundle-metadata.v1",
                "metadata_only": True,
                "valid": True,
                "app_bundle": star_forge.relative_to_project(app, project),
                "bundle_id": "com.example.Test",
                "app_name": "Test",
                "display_name": "Test",
                "executable_name": "Test",
                "executable_exists": True,
            })
            signing = write_json(root / "signing-note.json", {"schema": "star-forge.native-macos.note.v1", "kind": "signing", "status": "not_checked", "metadata_only": True})
            packaging = write_json(root / "packaging-note.json", {"schema": "star-forge.native-macos.note.v1", "kind": "packaging", "status": "not_checked", "metadata_only": True})
            write_manifest(
                project,
                "native-macos",
                {
                    "build": build,
                    "build_stdout": build_stdout,
                    "build_stderr": build_stderr,
                    "run": run,
                    "stdout": stdout,
                    "stderr": stderr,
                    "metadata": metadata,
                    "signing": signing,
                    "packaging": packaging,
                },
                summary={"app_bundle_metadata": star_forge.relative_to_project(metadata, project)},
            )
            code, payload, _ = run_cli([
                "native-macos-proof", "--project", str(project), "--task", "SF-1",
                "--app-name", "Test", "--bundle-id", "com.example.Test",
                "--build-result", str(build), "--run-result", str(run), "--app-bundle", str(app),
                "--signing-note", str(signing), "--packaging-note", str(packaging), "--strict",
            ])
            assert_fail(code, payload, "native-macos-result")


def test_native_macos_strict_rejects_shell_and_forbidden_argv_after_manifest_hash_refresh() -> None:
    cases = [
        ("build direct shell", "build", ["sh", "-c", "echo build"], "native-macos-shell"),
        ("run env path shell", "run", ["/usr/bin/env", "-P", "/bin:/usr/bin", "sh", "-c", "echo run"], "native-macos-shell"),
        ("test env split shell", "test", ["/usr/bin/env", "-S", "bash -c 'echo test'"], "native-macos-shell"),
        ("screenshot result split shell", "screenshot-result", ["env", "--split-string", "sh -c 'echo shot'"], "native-macos-shell"),
        ("build unknown env option", "build", ["/usr/bin/env", "--unknown-env-option", "python3", "-c", "print('build')"], "native-macos-shell"),
        ("run forbidden executable", "run", ["sudo", "xcodebuild"], "native-macos-forbidden-command"),
        ("build nested env shell", "build", ["/usr/bin/env", "env", "sh", "-c", "echo build"], "native-macos-shell"),
        ("run split nested env shell", "run", ["/usr/bin/env", "-S", "env bash -c 'echo run'"], "native-macos-shell"),
        ("test nested env shell", "test", ["/usr/bin/env", "CI=1", "env", "TEAM=1", "zsh", "-c", "echo test"], "native-macos-shell"),
        ("screenshot result nested env shell", "screenshot-result", ["/usr/bin/env", "env", "bash", "-c", "echo shot"], "native-macos-shell"),
        ("build env without target", "build", ["/usr/bin/env", "CI=1"], "native-macos-shell"),
    ]
    for name, target, bad_argv, expected_rule in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            root = live_dir(project, "native-macos")
            build, build_stdout, build_stderr = write_native_macos_result(project, root, "build")
            run, stdout, stderr = write_native_macos_result(project, root, "run", stdout_name="stdout.txt", stderr_name="stderr.txt", extra={
                "pid": os.getpid(),
                "gui_launch_failed": False,
                "cleanup_failed": False,
                "readiness": {"status": "observed"},
                "termination": {"attempted": True, "success": True},
                "cleanup": {"attempted": True, "success": True},
            })
            test, test_stdout, test_stderr = write_native_macos_result(project, root, "test")
            screenshot = make_png(root / "screenshot.png")
            screenshot_result, screenshot_stdout, screenshot_stderr = write_native_macos_result(project, root, "screenshot", name="screenshot-result")
            app, metadata, signing, packaging = write_native_macos_identity_artifacts(project, root)
            manifest = write_manifest(
                project,
                "native-macos",
                {
                    "build": build,
                    "build_stdout": build_stdout,
                    "build_stderr": build_stderr,
                    "run": run,
                    "stdout": stdout,
                    "stderr": stderr,
                    "test": test,
                    "test_stdout": test_stdout,
                    "test_stderr": test_stderr,
                    "screenshot": screenshot,
                    "screenshot_result": screenshot_result,
                    "screenshot_result_stdout": screenshot_stdout,
                    "screenshot_result_stderr": screenshot_stderr,
                    "metadata": metadata,
                    "signing": signing,
                    "packaging": packaging,
                },
                summary={"app_bundle_metadata": star_forge.relative_to_project(metadata, project)},
            )
            target_path = {
                "build": build,
                "run": run,
                "test": test,
                "screenshot-result": screenshot_result,
            }[target]
            payload = json.loads(target_path.read_text(encoding="utf-8"))
            payload["command_argv"] = bad_argv
            payload["executable_path"] = bad_argv[0]
            write_json(target_path, payload)
            refresh_manifest_artifact_hash(manifest, project, target_path)

            code, proof_payload, _ = run_cli([
                "native-macos-proof", "--project", str(project), "--task", "SF-1",
                "--app-name", "Test", "--bundle-id", "com.example.Test",
                "--build-result", str(build), "--run-result", str(run), "--test-result", str(test),
                "--screenshot", str(screenshot), "--app-bundle", str(app),
                "--signing-note", str(signing), "--packaging-note", str(packaging), "--strict",
            ])
            assert_fail(code, proof_payload, expected_rule)


def test_security_handoff_and_proof_strictness() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "security")
        handoff = write_json(root / "handoff-input.json", {"task": "SF-1", "provenance": "codex-security", "scan_scope": "changed-files"})
        findings = write_json(root / "normalized-findings.json", {"findings": [{
            "schema": "star-forge.normalized-security-finding.v1",
            "id": "F1",
            "scanner": "codex-security",
            "scanner_version": "1.0",
            "rule_id": "example-rule",
            "severity": "mystery",
            "fingerprint": "sfsec-1234567890abcdef",
        }]})
        summary = {"trusted_provenance": True, "ruleset": "default", "scan_scope": "changed-files", "input_hash": "abc123"}
        manifest = write_manifest(project, "security", {"handoff": handoff, "findings": findings}, summary=summary)

        code, payload, _ = run_cli([
            "security-handoff-packet", "--project", str(project), "--kind", "security-diff",
            "--input", str(handoff), "--strict",
        ])
        assert_fail(code, payload, "security-clean-proof")

        outside_handoff = write_json(project / "fixtures" / "handoff-input.json", {"task": "SF-1", "provenance": "codex-security", "scan_scope": "changed-files"})
        code, payload, _ = run_cli([
            "security-handoff-packet", "--project", str(project), "--kind", "security-diff",
            "--input", str(outside_handoff), "--strict",
        ])
        assert_fail(code, payload, "artifact-scope")

        code, payload, _ = run_cli([
            "security-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "security-diff", "--scanner", "codex-security",
            "--scanner-version", "1.0", "--findings", str(findings),
            "--artifact", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "security-severity")

        findings = write_json(root / "normalized-findings.json", {"findings": ["not-an-object"]})
        manifest = write_manifest(project, "security", {"handoff": handoff, "findings": findings}, summary=summary)
        code, payload, _ = run_cli([
            "security-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "security-diff", "--scanner", "codex-security",
            "--scanner-version", "1.0", "--findings", str(findings),
            "--artifact", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "security-findings")

        scanner_input = write_json(project / "scanner-reports" / "clean.json", {"schema": "codex.security.report.v1", "findings": []})
        scanner_hash = star_forge.file_sha256(scanner_input)
        input_hash_payload = {
            "schema": "star-forge.security-input-hash.v1",
            "input_path": star_forge.relative_to_project(scanner_input, project),
            "declared_sha256": scanner_hash,
            "actual_sha256": scanner_hash,
            "matches": True,
        }
        input_hash = write_json(root / "input-hash.json", input_hash_payload)
        findings = write_json(root / "normalized-findings.json", {
            "schema": "star-forge.normalized-security-findings.v1",
            "task": "SF-1",
            "profile": "security-diff",
            "source_schema": "codex.security.report.v1",
            "scanner": "codex-security",
            "scanner_version": "1.0",
            "findings": [],
            "summary": {"finding_count": 0},
        })
        handoff = write_json(root / "handoff-input.json", {
            "schema": "star-forge.security-handoff-input.v1",
            "task": "SF-1",
            "profile": "security-diff",
            "kind": "security-diff",
            "provenance": {
                "scanner": "codex-security",
                "scanner_version": "1.0",
                "source_schema": "codex.security.report.v1",
                "schema_family": "codex-security",
                "trusted_schema": True,
            },
            "scanner": "codex-security",
            "scanner_version": "1.0",
            "ruleset": {"name": "default"},
            "scan_scope": "changed-files",
            "source_binding": {"source_hash": star_forge.source_hash(project)},
            "input_hash": input_hash_payload,
            "normalized_findings": {
                "path": star_forge.relative_to_project(findings, project),
                "sha256": star_forge.file_sha256(findings),
                "finding_count": 0,
            },
            "problems": [],
        })
        redaction = write_json(root / "redaction-report.json", {
            "schema": "star-forge.security-redaction-report.v1",
            "counts": {"secret_values": 0, "sensitive_keys": 0, "home_paths": 0, "temp_paths": 0, "env_values": 0},
        })
        summary = {
            "trusted_provenance": True,
            "ruleset": {"name": "default"},
            "scan_scope": "changed-files",
            "input_hash": scanner_hash,
        }
        manifest = write_manifest(project, "security", {"handoff": handoff, "findings": findings, "input-hash": input_hash, "redaction-report": redaction}, summary=summary)
        code, payload, _ = run_cli([
            "security-handoff-packet", "--project", str(project), "--kind", "security-diff",
            "--input", str(handoff), "--strict",
        ])
        assert_pass(code, payload)

        code, payload, _ = run_cli([
            "security-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "security-diff", "--scanner", "codex-security",
            "--scanner-version", "1.0", "--findings", str(findings),
            "--artifact", str(manifest), "--strict",
        ])
        assert_pass(code, payload)

        write_text(project / "Dockerfile", "FROM scratch\n")
        code, payload, _ = run_cli([
            "security-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "security-diff", "--scanner", "codex-security",
            "--scanner-version", "1.0", "--findings", str(findings),
            "--artifact", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "security-source-binding")


def test_security_source_hash_binding_rejects_uncovered_dirty_paths_in_strict_proof() -> None:
    cases = {
        ".env.local": "LOCAL_ONLY=1\n",
        ".npmrc": "audit=false\n",
        "settings.local": "enabled=true\n",
    }
    for rel_path, contents in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            manifest, findings = write_clean_security_fixture(project)
            write_text(project / rel_path, contents)
            code, payload, _ = run_cli([
                "security-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "security-diff", "--scanner", "codex-security",
                "--scanner-version", "1.0", "--findings", str(findings),
                "--artifact", str(manifest), "--strict",
            ])
            assert_fail(code, payload, "security-source-binding")


def test_security_source_hash_binding_rejects_clean_committed_dependency_config_in_strict_proof() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        write_text(project / "Pipfile", "[packages]\nrequests = \"==2.31.0\"\n")
        commit_all(project, "pipfile v1")
        old_source = star_forge.source_hash(project)
        manifest, findings = write_clean_security_fixture(project, source_binding={"source_hash": old_source})

        write_text(project / "Pipfile", "[packages]\nrequests = \"==2.32.0\"\n")
        commit_all(project, "pipfile v2")
        assert star_forge.source_hash(project) != old_source
        assert not star_forge.source_dirty_entries(star_forge.git_status(project))

        code, payload, _ = run_cli([
            "security-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "security-diff", "--scanner", "codex-security",
            "--scanner-version", "1.0", "--findings", str(findings),
            "--artifact", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "security-source-binding")


def test_security_source_hash_binding_rejects_tracked_generated_dir_changes_in_strict_proof() -> None:
    cases = [
        ("build/proof-security.js", "console.log('security proof build v1')\n", "console.log('security proof build v2')\n"),
        ("dist/proof-security.js", "console.log('security proof dist v1')\n", "console.log('security proof dist v2')\n"),
        ("target/proof-security.txt", "target security proof v1\n", "target security proof v2\n"),
    ]
    for rel_path, before, after in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            (project / ".gitignore").write_text("build/\ndist/\ntarget/\n", encoding="utf-8")
            tracked = project / rel_path
            tracked.parent.mkdir(parents=True, exist_ok=True)
            tracked.write_text(before, encoding="utf-8")
            force_add(project, rel_path)
            commit_all(project, f"{rel_path} v1")
            old_source = star_forge.source_hash(project)
            manifest, findings = write_clean_security_fixture(project, source_binding={"source_hash": old_source})

            tracked.write_text(after, encoding="utf-8")
            commit_all(project, f"{rel_path} v2")
            assert star_forge.source_hash(project) != old_source
            assert not star_forge.source_dirty_entries(star_forge.git_status(project))

            code, payload, _ = run_cli([
                "security-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "security-diff", "--scanner", "codex-security",
                "--scanner-version", "1.0", "--findings", str(findings),
                "--artifact", str(manifest), "--strict",
            ])
            assert_fail(code, payload, "security-source-binding")


def test_commit_bound_clean_security_proof_rejects_uncommitted_source_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        head = commit_all(project)
        root = live_dir(project, "security")
        scanner_input = write_json(root / "scanner-input.json", {"schema": "codex.security.report.v1", "findings": []})
        scanner_hash = star_forge.file_sha256(scanner_input)
        input_hash_payload = {
            "schema": "star-forge.security-input-hash.v1",
            "input_path": star_forge.relative_to_project(scanner_input, project),
            "declared_sha256": scanner_hash,
            "actual_sha256": scanner_hash,
            "matches": True,
        }
        input_hash = write_json(root / "input-hash.json", input_hash_payload)
        findings = write_json(root / "normalized-findings.json", {
            "schema": "star-forge.normalized-security-findings.v1",
            "task": "SF-1",
            "profile": "security-diff",
            "source_schema": "codex.security.report.v1",
            "scanner": "codex-security",
            "scanner_version": "1.0",
            "findings": [],
            "summary": {"finding_count": 0},
        })
        handoff = write_json(root / "handoff-input.json", {
            "schema": "star-forge.security-handoff-input.v1",
            "task": "SF-1",
            "profile": "security-diff",
            "kind": "security-diff",
            "provenance": {
                "scanner": "codex-security",
                "scanner_version": "1.0",
                "source_schema": "codex.security.report.v1",
                "schema_family": "codex-security",
                "trusted_schema": True,
            },
            "scanner": "codex-security",
            "scanner_version": "1.0",
            "ruleset": {"name": "default"},
            "scan_scope": "changed-files",
            "source_binding": {"commit_sha": head},
            "input_hash": input_hash_payload,
            "normalized_findings": {
                "path": star_forge.relative_to_project(findings, project),
                "sha256": star_forge.file_sha256(findings),
                "finding_count": 0,
            },
            "problems": [],
        })
        redaction = write_json(root / "redaction-report.json", {
            "schema": "star-forge.security-redaction-report.v1",
            "counts": {"secret_values": 0, "sensitive_keys": 0, "home_paths": 0, "temp_paths": 0, "env_values": 0},
        })
        manifest = write_manifest(
            project,
            "security",
            {"handoff": handoff, "findings": findings, "input-hash": input_hash, "redaction-report": redaction},
            summary={
                "trusted_provenance": True,
                "ruleset": {"name": "default"},
                "scan_scope": "changed-files",
                "input_hash": scanner_hash,
            },
        )
        write_text(project / "src" / "uncommitted.py", "print('changed after report')\n")
        code, payload, _ = run_cli([
            "security-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "security-diff", "--scanner", "codex-security",
            "--scanner-version", "1.0", "--findings", str(findings),
            "--artifact", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "security-source-binding")


def test_clean_security_proof_rejects_summary_only_claims() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "security")
        handoff = write_json(root / "handoff-input.json", {"task": "SF-1", "provenance": "claimed", "scan_scope": "changed-files"})
        findings = write_json(root / "normalized-findings.json", {"findings": []})
        manifest = write_manifest(
            project,
            "security",
            {"handoff": handoff, "findings": findings},
            summary={
                "trusted_provenance": True,
                "ruleset": "default",
                "scan_scope": "changed-files",
                "input_hash": "0" * 64,
            },
        )
        code, payload, _ = run_cli([
            "security-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "security-diff", "--scanner", "codex-security",
            "--scanner-version", "1.0", "--findings", str(findings),
            "--artifact", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "security-clean-proof")


def test_security_proof_validates_bundle_for_nonblocking_findings() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "security")
        findings = write_json(root / "normalized-findings.json", {
            "schema": "star-forge.normalized-security-findings.v1",
            "task": "SF-1",
            "profile": "security-diff",
            "scanner": "codex-security",
            "scanner_version": "1.0",
            "findings": [{
                "schema": "star-forge.normalized-security-finding.v1",
                "id": "F-low",
                "scanner": "codex-security",
                "scanner_version": "1.0",
                "rule_id": "example-low",
                "severity": "low",
                "fingerprint": "sfsec-low1234567890",
            }],
        })
        manifest = write_manifest(
            project,
            "security",
            {"findings": findings},
            summary={
                "trusted_provenance": True,
                "ruleset": {"name": "default"},
                "scan_scope": "changed-files",
                "input_hash": "0" * 64,
            },
        )
        code, payload, _ = run_cli([
            "security-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "security-diff", "--scanner", "codex-security",
            "--scanner-version", "1.0", "--findings", str(findings),
            "--artifact", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "security-clean-proof")


def write_github_packet(
    project: Path,
    *,
    head_changed: bool = False,
    check_conclusion: str = "success",
    ci_log_identity: dict[str, Any] | None = None,
    commands: list[list[str]] | None = None,
    operations: list[Any] | None = None,
    github_host: str | None = "github.com",
) -> Path:
    root = live_dir(project, "github")
    current_head = "head2" if head_changed else "head1"
    pr = write_json(root / "pr.json", {
        "repo": "example/repo",
        "number": 7,
        "base_sha": "base1",
        "head_sha": "head1",
        "current_base_sha": "base1",
        "current_head_sha": current_head,
        "merge_base_sha": "merge1",
        "url": "https://github.com/example/repo/pull/7",
    })
    diff = write_text(root / "diff.patch", "diff --git a/src/app.py b/src/app.py\n")
    reviews = write_json(root / "reviews.json", [])
    comments = write_json(root / "comments.json", [])
    checks = write_json(root / "check-runs.json", {
        "check_runs": [
            {
                "id": "501",
                "run_id": "701",
                "job_id": "1701",
                "name": "test",
                "status": "completed",
                "conclusion": check_conclusion,
                "head_sha": "head1",
            }
        ],
        "head_sha": "head1",
    })
    annotations = write_json(root / "annotations.json", [])
    default_operations: list[dict[str, Any]] = [
        {"action": "read", "operation": "pull_request", "repo": "example/repo", "pr": "7"},
        {"action": "read", "operation": "check_runs", "repo": "example/repo", "pr": "7"},
    ]
    if github_host is not None:
        for operation in default_operations:
            operation["github_host"] = github_host
    operation_list = list(operations) if operations is not None else default_operations
    command_list = list(commands or [])
    artifacts = {"pr": pr, "diff": diff, "reviews": reviews, "comments": comments, "checks": checks, "annotations": annotations}
    summary_extra: dict[str, Any] = {}
    if ci_log_identity is not None:
        log_identity = dict(ci_log_identity)
        if github_host is not None:
            log_identity.setdefault("github_host", github_host)
        operation_list.append({"action": "read", "operation": "logs", **log_identity})
        logs = write_json(root / "ci-log-excerpts.json", {
            "max_log_bytes": 128,
            "logs": [
                {
                    "name": "unit",
                    "original_sha256": "0" * 64,
                    "original_bytes": 18,
                    "captured_bytes": 18,
                    "excerpt_sha256": "1" * 64,
                    "excerpt_bytes": 18,
                    "max_log_bytes": 128,
                    "truncated": False,
                    "text": "unit log excerpt\n",
                    **log_identity,
                }
            ],
        })
        artifacts["ci-log-excerpts"] = logs
        summary_extra = {"logs_included": True, "ci_log_excerpt_count": 1}
    transcript_live_provenance = {
        "source": "github-connector-live",
        "repo": "example/repo",
        "pr": "7",
        "collected_at": "2026-06-18T00:00:00Z",
    }
    summary_live_provenance = {
        "source": "github-connector-live",
        "repo": "example/repo",
        "pr": "7",
        "collected_at": "2026-06-18T00:00:00Z",
    }
    transcript_host_fields: dict[str, Any] = {}
    summary_host_fields: dict[str, Any] = {}
    if github_host is not None:
        transcript_host_fields["github_host"] = github_host
        summary_host_fields["github_host"] = github_host
        transcript_live_provenance["github_host"] = github_host
        summary_live_provenance["github_host"] = github_host
    transcript = write_json(root / "operation-transcript.json", {
        "schema": "star-forge.github-operation-transcript.v1",
        "source": "github-connector-live",
        "repo": "example/repo",
        "pr": "7",
        **transcript_host_fields,
        "collected_at": "2026-06-18T00:00:00Z",
        "live_provenance": transcript_live_provenance,
        "refs": {
            "captured_base_sha": "base1",
            "current_base_sha": "base1",
            "captured_head_sha": "head1",
            "current_head_sha": current_head,
            "merge_base_sha": "merge1",
        },
        "permission_state": {"partial_permissions": False},
        "pagination_state": {"pagination_incomplete": False},
        "operations": operation_list,
        "commands": command_list,
    })
    transcript_hash = star_forge.file_sha256(transcript)
    artifacts["operation-transcript"] = transcript
    summary_live_provenance["operation_transcript_sha256"] = transcript_hash
    return write_manifest(
        project,
        "github",
        artifacts,
        summary={
            "source": "github-connector-live",
            "repo": "example/repo",
            "pr": "7",
            "captured_at": "2026-06-18T00:00:00Z",
            "captured_base_sha": "base1",
            "current_base_sha": "base1",
            "captured_head_sha": "head1",
            "current_head_sha": current_head,
            "merge_base_sha": "merge1",
            **summary_host_fields,
            "read_only_operations": operation_list,
            "read_only_commands": command_list,
            "read_only_transcript_sha256": transcript_hash,
            **summary_extra,
            "live_provenance": summary_live_provenance,
        },
    )


def test_github_source_packet_freshness_checks_and_proof_paths() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        stale = write_github_packet(project, head_changed=True)
        code, payload, _ = run_cli(["source-packet-github-pr-review", "--project", str(project), "--input", str(stale), "--strict"])
        assert_fail(code, payload, "github-freshness")

        failing = write_github_packet(project, check_conclusion="failure")
        code, payload, _ = run_cli(["source-packet-github-pr-review", "--project", str(project), "--input", str(failing), "--strict"])
        assert_fail(code, payload, "github-checks")

        fresh = write_github_packet(project)
        code, payload, _ = run_cli(["source-packet-github-pr-review", "--project", str(project), "--input", str(fresh), "--strict"])
        assert_pass(code, payload)
        code, payload, _ = run_cli([
            "source-packet-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "production-review", "--input", str(fresh), "--strict",
        ])
        assert_pass(code, payload)
        code, payload, _ = run_cli([
            "source-packet-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "github-pr-review", "--input", str(fresh), "--strict",
        ])
        assert_pass(code, payload)


def test_github_source_packet_rejects_missing_final_freshness_without_initial_ref_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        manifest = write_github_packet(project)
        root = live_dir(project, "github")

        pr_path = root / "pr.json"
        pr_payload = json.loads(pr_path.read_text(encoding="utf-8"))
        pr_payload["current_base_sha"] = ""
        pr_payload["current_head_sha"] = ""
        write_json(pr_path, pr_payload)
        refresh_manifest_artifact_hash(manifest, project, pr_path)

        transcript_path = root / "operation-transcript.json"
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        transcript["refs"]["current_base_sha"] = ""
        transcript["refs"]["current_head_sha"] = ""
        write_json(transcript_path, transcript)
        refresh_github_transcript_hashes(manifest, project, transcript_path, [])

        def clear_summary_current_refs(payload: dict[str, Any]) -> None:
            summary = payload.setdefault("summary", {})
            summary["current_base_sha"] = ""
            summary["current_head_sha"] = ""

        rewrite_manifest(manifest, clear_summary_current_refs)

        code, payload, _ = run_cli([
            "source-packet-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "production-review", "--input", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "github-live-provenance")


def test_source_packet_proof_profiles_fail_closed_for_missing_github_packet() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "github")
        evidence = write_text(root / "evidence.txt", "not a GitHub source packet\n")
        manifest = write_manifest(
            project,
            "github",
            {"evidence": evidence},
            summary={
                "source": "github-connector-live",
                "repo": "example/repo",
                "pr": "7",
            },
        )
        code, payload, _ = run_cli([
            "source-packet-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "github-pr-review", "--input", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "artifact-missing")

        code, payload, _ = run_cli([
            "source-packet-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "unknown-profile", "--input", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "source-profile")


def test_github_source_packet_rejects_pr_json_identity_mutations_after_hash_refresh() -> None:
    mutations: list[tuple[str, Any]] = [
        ("repo", lambda payload: payload.update({"repo": "other/repo"})),
        ("number", lambda payload: payload.update({"number": 99})),
        ("url_repo", lambda payload: payload.update({"url": "https://github.com/other/repo/pull/7"})),
        ("url_pr", lambda payload: payload.update({"url": "https://github.com/example/repo/pull/99"})),
        ("url_scheme_relative", lambda payload: payload.update({"url": "//evil.example/example/repo/pull/7"})),
        ("base", lambda payload: payload.update({"base_sha": "base2"})),
        ("head", lambda payload: payload.update({"head_sha": "head2"})),
        ("current_base", lambda payload: payload.update({"current_base_sha": "base2"})),
        ("current_head", lambda payload: payload.update({"current_head_sha": "head2"})),
        ("merge_base", lambda payload: payload.update({"merge_base_sha": "merge2"})),
    ]
    for _, mutate in mutations:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            manifest = write_github_packet(project)
            pr_path = live_dir(project, "github") / "pr.json"
            payload = json.loads(pr_path.read_text(encoding="utf-8"))
            mutate(payload)
            write_json(pr_path, payload)
            refresh_manifest_artifact_hash(manifest, project, pr_path)
            code, proof, _ = run_cli([
                "source-packet-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "production-review", "--input", str(manifest), "--strict",
            ])
            assert_fail(code, proof, "github-live-provenance")


def test_github_source_packet_rejects_non_passing_check_runs_after_hash_refresh() -> None:
    mutations: list[Any] = [
        lambda check: check.pop("conclusion", None),
        lambda check: check.pop("status", None),
        lambda check: check.update({"conclusion": "mystery"}),
    ]
    for mutate in mutations:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            manifest = write_github_packet(project)
            checks_path = live_dir(project, "github") / "check-runs.json"
            payload = json.loads(checks_path.read_text(encoding="utf-8"))
            mutate(payload["check_runs"][0])
            write_json(checks_path, payload)
            refresh_manifest_artifact_hash(manifest, project, checks_path)
            code, proof, _ = run_cli([
                "source-packet-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "production-review", "--input", str(manifest), "--strict",
            ])
            assert_fail(code, proof, "github-checks")


def test_github_source_packet_rejects_unbound_ci_log_excerpt_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        manifest = write_github_packet(
            project,
            ci_log_identity={
                "repo": "example/repo",
                "pr": "7",
                "captured_head_sha": "head1",
                "run_id": "999",
            },
        )
        code, payload, _ = run_cli([
            "source-packet-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "production-review", "--input", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "github-logs")


def test_github_source_packet_rejects_off_host_gh_api_commands() -> None:
    bad_commands = [
        ["gh", "api", "https://evil.example/repos/example/repo/pulls/7"],
        ["gh", "api", "//repos/example/repo/pulls/7"],
        ["gh", "api", "//api.github.com/repos/example/repo/pulls/7"],
        ["gh", "api", "--hostname", "evil.example", "repos/example/repo/pulls/7"],
    ]
    for command in bad_commands:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            manifest = write_github_packet(project, commands=[command])
            code, payload, _ = run_cli([
                "source-packet-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "production-review", "--input", str(manifest), "--strict",
            ])
            assert_fail(code, payload, "github-command")


def test_github_source_packet_rejects_attached_gh_api_value_flags() -> None:
    bad_commands = [
        ["gh", "api", "-HAuthorization:Bearer token", "repos/example/repo/pulls/7"],
        ["gh", "api", "-ffield=value", "repos/example/repo/issues/7/comments"],
        ["gh", "api", "-Ffield=value", "repos/example/repo/issues/7/comments"],
    ]
    for command in bad_commands:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            manifest = write_github_packet(project, commands=[command])
            code, payload, _ = run_cli([
                "source-packet-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "production-review", "--input", str(manifest), "--strict",
            ])
            assert_fail(code, payload, "github-command")


def test_github_source_packet_rejects_sensitive_path_style_gh_api_query_params() -> None:
    bad_queries = [
        ("api-key", "proofsecret0"),
        ("X-Amz-Signature", "proofsecret1"),
        ("authorization", "proofsecret2"),
        ("access_token", "proofsecret3"),
    ]
    for key, secret in bad_queries:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            manifest = write_github_packet(project, commands=[["gh", "api", f"repos/example/repo/pulls/7?{key}={secret}"]])
            code, payload, _ = run_cli([
                "source-packet-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "production-review", "--input", str(manifest), "--strict",
            ])
            assert_fail(code, payload, "github-command")


def test_github_source_packet_rejects_unbounded_path_style_gh_api_query_params() -> None:
    bad_commands = [
        ["gh", "api", "repos/example/repo/pulls/7/files?page=nonnumeric"],
        ["gh", "api", "repos/example/repo/pulls/7/files?page=0"],
        ["gh", "api", "repos/example/repo/pulls/7/files?per_page=101"],
    ]
    for command in bad_commands:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            manifest = write_github_packet(project, commands=[command])
            code, payload, _ = run_cli([
                "source-packet-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "production-review", "--input", str(manifest), "--strict",
            ])
            assert_fail(code, payload, "github-command")


def test_github_source_packet_rejects_gh_api_field_arguments() -> None:
    endpoint = "repos/example/repo/pulls/7"
    bad_commands = [
        ["gh", "api", "--method", "GET", "-f", "access_token=proofsecretfield0", endpoint],
        ["gh", "api", "--method", "GET", "-F", "foo=proofsecretfield1", endpoint],
        ["gh", "api", "--method", "GET", "--field", "page=nonnumeric", endpoint],
        ["gh", "api", "--method", "GET", "--raw-field", "per_page=proofsecretfield3", endpoint],
    ]
    for command in bad_commands:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            manifest = write_github_packet(project, commands=[command])
            code, payload, _ = run_cli([
                "source-packet-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "production-review", "--input", str(manifest), "--strict",
            ])
            assert_fail(code, payload, "github-command")


def test_github_source_packet_rejects_trailing_gh_commands_after_hash_refresh() -> None:
    bad_commands: list[Any] = [
        "gh pr view 7 --repo example/repo ; gh pr checkout 7",
        ["gh", "pr", "view", "7", "--repo", "example/repo", ";", "gh", "pr", "checkout", "7"],
        "gh run view 701 --repo example/repo --log && gh run rerun 701",
        ["gh", "run", "view", "701", "--repo", "example/repo", "--log", "&&", "gh", "run", "rerun", "701"],
        "gh api repos/example/repo/pulls/7\ngh api repos/example/repo/actions/runs/701/rerun",
        ["gh", "api", "repos/example/repo/pulls/7", "gh", "repo", "delete", "example/repo"],
    ]
    for command in bad_commands:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            manifest = write_github_packet(project)
            transcript_path = live_dir(project, "github") / "operation-transcript.json"
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            transcript["commands"] = [command]
            write_json(transcript_path, transcript)
            refresh_github_transcript_hashes(manifest, project, transcript_path, [command])

            code, payload, _ = run_cli([
                "source-packet-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "production-review", "--input", str(manifest), "--strict",
            ])
            assert_fail(code, payload, "github-command")


def test_github_source_packet_rejects_allowed_option_shell_substitution_after_hash_refresh() -> None:
    bad_commands: list[Any] = [
        ["gh", "pr", "view", "7", "--repo", "example/repo", "--jq", "$(echo forged)"],
        ["gh", "pr", "view", "7", "--repo", "example/repo", "--template", "{{.title}} `echo forged`"],
        ["gh", "api", "repos/example/repo/pulls/7", "--jq=$(echo forged)"],
        ["gh", "api", "repos/example/repo/pulls/7", "--template", "{{.title}} $(echo forged)"],
    ]
    for command in bad_commands:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            manifest = write_github_packet(project)
            transcript_path = live_dir(project, "github") / "operation-transcript.json"
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            transcript["commands"] = [command]
            write_json(transcript_path, transcript)
            refresh_github_transcript_hashes(manifest, project, transcript_path, [command])

            code, payload, _ = run_cli([
                "source-packet-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "production-review", "--input", str(manifest), "--strict",
            ])
            assert_fail(code, payload, "github-command")


def test_github_source_packet_rejects_embedded_ampersands_after_transcript_hash_refresh() -> None:
    bad_commands: list[Any] = [
        ["gh", "pr", "view", "7", "--repo", "example/repo", "--jq", ".title&echo forged"],
        ["gh", "pr", "view", "7", "--repo", "example/repo", "--template", "{{.title}}&echo forged"],
        ["gh", "api", "repos/example/repo/pulls/7", "--method", "GET&echo forged"],
        ["gh", "api", "--hostname", "github.com&evil.example", "repos/example/repo/pulls/7"],
        "gh pr view 7 --repo example/repo --jq '.title&echo forged'",
    ]
    for command in bad_commands:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            manifest = write_github_packet(project)
            transcript_path = live_dir(project, "github") / "operation-transcript.json"
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            transcript["commands"] = [command]
            write_json(transcript_path, transcript)
            refresh_github_transcript_hashes(manifest, project, transcript_path, [command])

            code, payload, _ = run_cli([
                "source-packet-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "production-review", "--input", str(manifest), "--strict",
            ])
            assert_fail(code, payload, "github-command")


def test_github_source_packet_rejects_missing_explicit_host_even_with_github_pr_url() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        manifest = write_github_packet(project, github_host=None)
        code, payload, _ = run_cli([
            "source-packet-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "production-review", "--input", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "github-live-provenance")


def test_github_source_packet_rejects_off_host_live_provenance_with_connector_operations() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        manifest = write_github_packet(project, github_host="evil.example")
        code, payload, _ = run_cli([
            "source-packet-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "production-review", "--input", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "github-live-provenance")


def test_github_source_packet_rejects_path_style_command_with_off_host_live_provenance() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        manifest = write_github_packet(
            project,
            github_host="evil.example",
            commands=[["gh", "api", "repos/example/repo/pulls/7"]],
        )
        code, payload, _ = run_cli([
            "source-packet-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "production-review", "--input", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "github-live-provenance")


def test_github_source_packet_rejects_wrong_connector_operation_identity() -> None:
    bad_operations = [
        [{"action": "read", "operation": "pull_request", "repo": "other/repo", "pr": "7", "github_host": "github.com"}],
        [{"action": "read", "operation": "pull_request", "repository": "other/repo", "pr": "7", "github_host": "github.com"}],
        [{"action": "read", "operation": "pull_request", "repository": "https://evil.example/example/repo", "pr": "7", "github_host": "github.com"}],
        [{"action": "read", "operation": "pull_request", "repository": "https://github.com/example/repo", "pull_request": "https://evil.example/example/repo/pull/7", "github_host": "github.com"}],
        [{"action": "read", "operation": "pull_request", "repo": "example/repo", "pullRequest": "//evil.example/example/repo/pull/7", "github_host": "github.com"}],
        [{"action": "read", "operation": "pull_request", "repo": "example/repo", "pr": "7", "headRef": {"url": "https://evil.example/example/repo"}}],
        [{"action": "read", "operation": "pull_request", "repo": "example/repo", "pr": "99", "github_host": "github.com"}],
        [{"action": "read", "operation": "pull_request", "repo": "example/repo", "pull_request": "99", "github_host": "github.com"}],
    ]
    for operations in bad_operations:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            manifest = write_github_packet(project, operations=operations)
            code, payload, _ = run_cli([
                "source-packet-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "production-review", "--input", str(manifest), "--strict",
            ])
            assert_fail(code, payload, "github-command")


def test_github_source_packet_rejects_missing_connector_operation_identity() -> None:
    bad_operations: list[list[Any]] = [
        ["pull_request"],
        [{"action": "read", "operation": "pull_request", "pr": "7", "github_host": "github.com"}],
        [{"action": "read", "operation": "pull_request", "repo": "example/repo", "github_host": "github.com"}],
        [{"action": "read", "operation": "pull_request", "repo": "example/repo", "pr": "7"}],
    ]
    for operations in bad_operations:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_project(project)
            manifest = write_github_packet(project, operations=operations)
            code, payload, _ = run_cli([
                "source-packet-proof", "--project", str(project), "--task", "SF-1",
                "--profile", "production-review", "--input", str(manifest), "--strict",
            ])
            assert_fail(code, payload, "github-command")


def test_github_source_packet_rejects_off_host_connector_operation_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        manifest = write_github_packet(
            project,
            operations=[{"action": "read", "operation": "pull_request", "url": "https://evil.example/example/repo/pull/7"}],
        )
        code, payload, _ = run_cli([
            "source-packet-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "production-review", "--input", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "github-command")


def test_github_source_packet_rejects_scheme_relative_connector_operation_url() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        manifest = write_github_packet(
            project,
            operations=[{"action": "read", "operation": "pull_request", "repo": "example/repo", "pr": "7", "url": "//evil.example/example/repo/pull/7"}],
        )
        code, payload, _ = run_cli([
            "source-packet-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "production-review", "--input", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "github-command")


def test_github_source_packet_accepts_pr_bound_ci_log_excerpt_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        manifest = write_github_packet(
            project,
            ci_log_identity={
                "repo": "example/repo",
                "pr": "7",
                "captured_head_sha": "head1",
                "run_id": "701",
            },
        )
        code, payload, _ = run_cli([
            "source-packet-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "production-review", "--input", str(manifest), "--strict",
        ])
        assert_pass(code, payload)


def test_github_source_packet_rejects_summary_only_live_provenance() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "github")
        pr = write_json(root / "pr.json", {"number": 7, "base_sha": "base1", "head_sha": "head1"})
        diff = write_text(root / "diff.patch", "diff --git a/src/app.py b/src/app.py\n")
        reviews = write_json(root / "reviews.json", [])
        comments = write_json(root / "comments.json", [])
        checks = write_json(root / "check-runs.json", {"check_runs": [{"name": "test", "status": "completed", "conclusion": "success", "head_sha": "head1"}]})
        annotations = write_json(root / "annotations.json", [])
        fake_hash = "0" * 64
        manifest = write_manifest(
            project,
            "github",
            {"pr": pr, "diff": diff, "reviews": reviews, "comments": comments, "checks": checks, "annotations": annotations},
            summary={
                "source": "github-connector-live",
                "repo": "example/repo",
                "pr": "7",
                "captured_at": "2026-06-18T00:00:00Z",
                "captured_base_sha": "base1",
                "current_base_sha": "base1",
                "captured_head_sha": "head1",
                "current_head_sha": "head1",
                "read_only_operations": [{"action": "read", "operation": "pull_request"}],
                "read_only_transcript_sha256": fake_hash,
                "live_provenance": {
                    "source": "github-connector-live",
                    "repo": "example/repo",
                    "pr": "7",
                    "collected_at": "2026-06-18T00:00:00Z",
                    "operation_transcript_sha256": fake_hash,
                },
            },
        )
        code, payload, _ = run_cli(["source-packet-github-pr-review", "--project", str(project), "--input", str(manifest), "--strict"])
        assert_fail(code, payload, "github-live-provenance")


def test_github_source_packet_requires_check_run_raw_hash() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        manifest = write_github_packet(project)
        checks = live_dir(project, "github") / "check-runs.json"
        checks_rel = star_forge.relative_to_project(checks, project)
        rewrite_manifest(manifest, lambda payload: payload["raw_artifact_hashes"].pop(checks_rel, None))
        code, payload, _ = run_cli([
            "source-packet-proof", "--project", str(project), "--task", "SF-1",
            "--profile", "production-review", "--input", str(manifest), "--strict",
        ])
        assert_fail(code, payload, "github-live-provenance")


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
    print(f"\ntest_live_proof_commands.py: {passed} passed, {len(failed)} failed, {len(tests)} total")
    if failed:
        print("failed tests: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
