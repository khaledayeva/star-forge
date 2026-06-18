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


def live_dir(project: Path, collector: str, task: str = "SF-1") -> Path:
    return live_common.live_collector_dir(project, task, collector)


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
        root = live_dir(project, "github")
        artifact = write_text(root / "proof.json", "{}\n")
        manifest = write_manifest(project, "github", {"proof": artifact})
        code, payload, err = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "github-pr-review", "--artifact", str(manifest), "--strict"])
        assert err == ""
        assert_pass(code, payload)

        degraded = write_manifest(project, "github", {"proof": artifact}, degraded=True)
        code, payload, _ = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "github-pr-review", "--artifact", str(degraded), "--strict"])
        assert_fail(code, payload, "manifest-degraded")

        stale = write_manifest(project, "github", {"proof": artifact}, source_hash="stale")
        code, payload, _ = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "github-pr-review", "--artifact", str(stale), "--strict"])
        assert_fail(code, payload, "manifest-source")

        fresh = write_manifest(project, "github", {"proof": artifact})
        write_text(project / ".starforge" / "runtime" / "server.json", "{}\n")
        code, payload, _ = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "github-pr-review", "--artifact", str(fresh), "--strict"])
        assert_fail(code, payload, "manifest-runtime")


def test_proof_run_strict_rejects_malformed_and_out_of_scope_manifest_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "github")
        artifact = write_text(root / "proof.json", "{}\n")
        manifest = write_manifest(project, "github", {"proof": artifact})
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["artifacts"] = "not-artifact-records"
        manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        code, payload, _ = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "github-pr-review", "--artifact", str(manifest), "--strict"])
        assert_fail(code, payload, "manifest-shape")

        external_artifact = write_text(project / "fixtures" / "github-proof.json", "{}\n")
        manifest = write_manifest(project, "github", {"proof": external_artifact})
        code, payload, _ = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "github-pr-review", "--artifact", str(manifest), "--strict"])
        assert_fail(code, payload, "artifact-scope")

        manifest = write_manifest(project, "github", {"proof": artifact})
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["raw_artifact_hashes"][star_forge.relative_to_project(external_artifact, project)] = {
            "path": star_forge.relative_to_project(external_artifact, project),
            "sha256": star_forge.file_sha256(external_artifact),
        }
        manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        code, payload, _ = run_cli(["proof-run", "--project", str(project), "--task", "SF-1", "--profile", "github-pr-review", "--artifact", str(manifest), "--strict"])
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
        interaction = write_json(root / "interaction.json", {"ready": [{"passed": True}], "actions": [], "assertions": []})
        console = write_json(root / "console.json", {"events": []})
        manifest = write_manifest(
            project,
            "browser",
            {"desktop": desktop, "mobile": mobile, "interaction": interaction, "console": console},
            summary={"url": "https://example.com"},
        )
        base_args = [
            "browser-run", "--project", str(project), "--task", "SF-1",
            "--scenario", "happy", "--url", "https://example.com",
            "--viewport", f"desktop=1280x800:{desktop}",
            "--viewport", f"mobile=390x844:{mobile}",
            "--interaction-evidence", str(interaction),
            "--console-evidence", str(console),
            "--strict",
        ]
        code, payload, _ = run_cli(base_args)
        assert_fail(code, payload, "manifest-missing")

        code, payload, _ = run_cli(base_args[:-1] + ["--live-manifest", str(manifest), "--strict"])
        assert_pass(code, payload)

        rewrite_manifest(manifest, lambda payload: payload.update({"raw_artifact_hashes": {}}))
        code, payload, _ = run_cli(base_args[:-1] + ["--live-manifest", str(manifest), "--strict"])
        assert_fail(code, payload, "artifact-hash")

        console.write_text("not json\n", encoding="utf-8")
        write_manifest(
            project,
            "browser",
            {"desktop": desktop, "mobile": mobile, "interaction": interaction, "console": console},
            summary={"url": "https://example.com"},
        )
        code, payload, _ = run_cli(base_args[:-1] + ["--live-manifest", str(manifest), "--strict"])
        assert_fail(code, payload, "console-evidence")


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
        return [(live_preview.socket.AF_INET, live_preview.socket.SOCK_STREAM, 6, "", ("10.0.0.9", 80))]

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
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "preview")
        http = write_json(root / "http.json", {
            "status": 200,
            "final_url": "http://93.184.216.34/",
            "connected_ips": ["127.0.0.1"],
        })
        deployment = write_json(root / "deployment.json", {"source_hash": star_forge.source_hash(project), "deployment_id": "dep-1"})
        smoke = write_json(root / "smoke.json", {"checks": [{"name": "home", "passed": True}]})
        write_manifest(project, "preview", {"http": http, "deployment": deployment, "smoke": smoke}, summary={"url": "http://93.184.216.34/"})
        code, payload, _ = run_cli([
            "preview-proof", "--project", str(project), "--task", "SF-1",
            "--url", "http://93.184.216.34/", "--expect-status", "200",
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
        build = write_json(root / "build.json", {"success": True})
        launch = write_json(root / "launch.json", {"success": True})
        test = write_json(root / "test.json", {"success": True})
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
        build = write_json(root / "build.json", {"success": True})
        launch = write_json(root / "launch.json", {"success": True})
        test = write_json(root / "test.json", {"success": True})
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


def test_native_macos_requires_identity_then_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project, "native-macos")
        build = write_json(root / "build.json", {"success": True})
        stdout = write_text(root / "stdout.txt", "READY\n")
        stderr = write_text(root / "stderr.txt", "")
        run = write_json(root / "run.json", {
            "success": True,
            "pid": os.getpid(),
            "timed_out": False,
            "gui_launch_failed": False,
            "cleanup_failed": False,
            "readiness": {"status": "observed"},
            "termination": {"attempted": True, "success": True},
            "cleanup": {"attempted": True, "success": True},
            "stdout_artifact": star_forge.relative_to_project(stdout, project),
            "stderr_artifact": star_forge.relative_to_project(stderr, project),
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
        write_manifest(project, "native-macos", {"build": build, "run": run, "stdout": stdout, "stderr": stderr, "metadata": metadata, "signing": signing, "packaging": packaging}, summary={"app_bundle_metadata": star_forge.relative_to_project(metadata, project)})

        base_args = [
            "native-macos-proof", "--project", str(project), "--task", "SF-1",
            "--build-result", str(build), "--run-result", str(run), "--app-bundle", str(app),
            "--signing-note", str(signing), "--packaging-note", str(packaging), "--strict",
        ]
        code, payload, _ = run_cli(base_args)
        assert_fail(code, payload, "native-macos-app-identity")

        code, payload, _ = run_cli(base_args[:-1] + ["--app-name", "Test", "--bundle-id", "com.example.Test", "--strict"])
        assert_pass(code, payload)


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
) -> Path:
    root = live_dir(project, "github")
    pr = write_json(root / "pr.json", {"number": 7, "base_sha": "base1", "head_sha": "head1"})
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
    current_head = "head2" if head_changed else "head1"
    operations = [{"action": "read", "operation": "pull_request"}, {"action": "read", "operation": "check_runs"}]
    artifacts = {"pr": pr, "diff": diff, "reviews": reviews, "comments": comments, "checks": checks, "annotations": annotations}
    summary_extra: dict[str, Any] = {}
    if ci_log_identity is not None:
        log_identity = dict(ci_log_identity)
        operations.append({"action": "read", "operation": "logs", **log_identity})
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
    transcript = write_json(root / "operation-transcript.json", {
        "schema": "star-forge.github-operation-transcript.v1",
        "source": "github-connector-live",
        "repo": "example/repo",
        "pr": "7",
        "collected_at": "2026-06-18T00:00:00Z",
        "refs": {
            "captured_base_sha": "base1",
            "current_base_sha": "base1",
            "captured_head_sha": "head1",
            "current_head_sha": current_head,
        },
        "permission_state": {"partial_permissions": False},
        "pagination_state": {"pagination_incomplete": False},
        "operations": operations,
        "commands": [],
    })
    transcript_hash = star_forge.file_sha256(transcript)
    artifacts["operation-transcript"] = transcript
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
            "read_only_operations": operations,
            "read_only_transcript_sha256": transcript_hash,
            **summary_extra,
            "live_provenance": {
                "source": "github-connector-live",
                "repo": "example/repo",
                "pr": "7",
                "collected_at": "2026-06-18T00:00:00Z",
                "operation_transcript_sha256": transcript_hash,
            },
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
