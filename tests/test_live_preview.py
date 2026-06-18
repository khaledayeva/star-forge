#!/usr/bin/env python3
"""Focused tests for the provider-neutral preview URL collector.

Run with: python3 tests/test_live_preview.py
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
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

STAR_FORGE_SCRIPT = ROOT / "scripts" / "star_forge.py"
STAR_FORGE_SPEC = importlib.util.spec_from_file_location("star_forge", STAR_FORGE_SCRIPT)
assert STAR_FORGE_SPEC and STAR_FORGE_SPEC.loader
star_forge = importlib.util.module_from_spec(STAR_FORGE_SPEC)
STAR_FORGE_SPEC.loader.exec_module(star_forge)

PREVIEW_SCRIPT = ROOT / "scripts" / "live_collectors" / "preview.py"
PREVIEW_SPEC = importlib.util.spec_from_file_location("preview_collector", PREVIEW_SCRIPT)
assert PREVIEW_SPEC and PREVIEW_SPEC.loader
preview = importlib.util.module_from_spec(PREVIEW_SPEC)
PREVIEW_SPEC.loader.exec_module(preview)

from live_collectors import common as live_common

os.environ["STAR_FORGE_LEARNINGS_HOME"] = tempfile.mkdtemp(prefix="star-forge-preview-test-learnings-")

PLAN_HEADER = (
    "| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |\n"
    "|------|-------------|--------|------|-------|---------|--------|----------|\n"
)
REAL_VERIFY = "python3 -c \"print('ok')\""
TASK = "SF-1"


def run_star_cli(args: list[str]) -> tuple[int, dict[str, Any], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = star_forge.main(args)
    out = stdout.getvalue()
    err = stderr.getvalue()
    payload = json.loads(out) if out.strip().startswith("{") else {}
    return code, payload, err


def run_preview(args: list[str]) -> tuple[int, dict[str, Any], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = preview.main(args)
    out = stdout.getvalue()
    err = stderr.getvalue()
    payload = json.loads(out) if out.strip().startswith("{") else {}
    return code, payload, err


def init_project(project: Path) -> None:
    code, payload, err = run_star_cli(["init", "--project", str(project), "--no-agents"])
    assert code == 0, err or payload
    (project / "src").mkdir(exist_ok=True)
    (project / "src" / "app.py").write_text("print('preview test')\n", encoding="utf-8")
    (project / "Plan.md").write_text(
        "# Plan.md\n\n"
        + PLAN_HEADER
        + f"| {TASK} | Build preview test app | ready | solo | src/app.py | - | {REAL_VERIFY} | - |\n",
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


def source_hash(project: Path) -> str:
    return star_forge.source_hash(project)


def live_dir(project: Path) -> Path:
    return live_common.live_collector_dir(project, TASK, "preview", create=False)


def write_server_lease(project: Path, url: str) -> Path:
    parsed = preview.urllib.parse.urlparse(url)
    path = project / ".starforge" / "runtime" / "server.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    origin = f"{parsed.scheme}://{parsed.hostname}:{port}"
    payload = {
        "schema": "star-forge.server-lease.v1",
        "project": str(project),
        "origin": origin,
        "base_url": origin,
        "port": port,
        "pid": os.getpid(),
        "command": "python3 -m http.server",
        "source_hash": source_hash(project),
        "runtime_asset_hash": live_common.compute_runtime_asset_hash(project, exclude_paths=[path]),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def rules(payload: dict[str, Any]) -> set[str]:
    return {str(item.get("rule")) for item in payload.get("problems", []) if isinstance(item, dict)}


def load_manifest(project: Path) -> dict[str, Any]:
    return json.loads((live_dir(project) / "manifest.json").read_text(encoding="utf-8"))


class TestServer:
    def __init__(self, routes: dict[str, Callable[[BaseHTTPRequestHandler], None]]) -> None:
        self.routes = routes
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "TestServer":
        routes = self.routes

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                route = routes.get(self.path.split("?", 1)[0])
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"missing")
                    return
                route(self)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        assert self.server is not None
        self.server.shutdown()
        self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2)

    def url(self, path: str = "/") -> str:
        assert self.server is not None
        host, port = self.server.server_address
        return f"http://{host}:{port}{path}"


def route(status: int = 200, body: str = "", headers: dict[str, str] | None = None) -> Callable[[BaseHTTPRequestHandler], None]:
    def handle(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(status)
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(body.encode("utf-8"))

    return handle


def redirect(location: str) -> Callable[[BaseHTTPRequestHandler], None]:
    return route(302, "", {"Location": location})


def collector_args(project: Path, url: str, *extra: str) -> list[str]:
    return ["--project", str(project), "--task", TASK, "--url", url, *extra]


@contextlib.contextmanager
def chdir(path: Path) -> Any:
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def assert_failed_with(code: int, payload: dict[str, Any], rule: str) -> None:
    assert code == 1, payload
    assert payload.get("degraded") is True, payload
    assert rule in rules(payload), payload.get("problems")


def test_happy_path_collects_artifacts_and_strict_proof_run_passes() -> None:
    body = (ROOT / "fixtures" / "live-preview" / "ok.html").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": route(200, body)}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_server_lease(project, server.url("/"))
        code, payload, err = run_preview(
            collector_args(
                project,
                server.url("/"),
                "--server-lease",
                str(lease),
                "--deployment-id",
                "dep-happy",
                "--deployment-source-hash",
                source_hash(project),
                "--smoke-check",
                "contains:Star Forge Preview OK",
            )
        )
        assert err == ""
        assert code == 0, payload
        assert payload["degraded"] is False, payload
        assert payload["proof_command"][2] == "preview-proof", payload["proof_command"]
        for name in ("http", "deployment", "smoke", "headers"):
            assert (project / payload["artifacts"][name]).exists(), name

        proof_code, proof_payload, proof_err = run_star_cli(payload["proof_command"][2:])
        assert proof_err == ""
        assert proof_code == 0, proof_payload
        assert proof_payload["verdict"] == "PASS", proof_payload


def test_record_uses_absolute_project_path_for_relative_project_arg() -> None:
    body = (ROOT / "fixtures" / "live-preview" / "ok.html").read_text(encoding="utf-8")
    captured: dict[str, list[str]] = {}
    original = preview.run_record_command

    def fake_record(command: list[str]) -> int:
        captured["command"] = list(command)
        return 0

    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": route(200, body)}) as server:
        workspace = Path(tmp).resolve()
        project = workspace / "app"
        init_project(project)
        lease = write_server_lease(project, server.url("/"))
        preview.run_record_command = fake_record
        try:
            with chdir(workspace):
                code, payload, err = run_preview(
                    collector_args(
                        Path("app"),
                        server.url("/"),
                        "--server-lease",
                        str(lease),
                        "--deployment-id",
                        "dep-record",
                        "--deployment-source-hash",
                        source_hash(project),
                        "--smoke-check",
                        "contains:Star Forge Preview OK",
                        "--record",
                    )
                )
        finally:
            preview.run_record_command = original
        assert err == ""
        assert code == 0, payload
        command = captured["command"]
        assert command[command.index("--project") + 1] == str(project)
        assert payload["proof_command"][payload["proof_command"].index("--project") + 1] == "app"


def test_preview_proof_rejects_tampered_bad_http_without_manifest_problem() -> None:
    body = (ROOT / "fixtures" / "live-preview" / "ok.html").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": route(200, body)}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_server_lease(project, server.url("/"))
        code, payload, _ = run_preview(
            collector_args(
                project,
                server.url("/"),
                "--server-lease",
                str(lease),
                "--deployment-id",
                "dep-happy",
                "--deployment-source-hash",
                source_hash(project),
                "--smoke-check",
                "contains:Star Forge Preview OK",
            )
        )
        assert code == 0, payload
        http = live_dir(project) / "http.json"
        http.write_text(json.dumps({"status": 503, "final_url": server.url("/")}, indent=2) + "\n", encoding="utf-8")
        proof_code, proof_payload, _ = run_star_cli(payload["proof_command"][2:])
        assert proof_code == 1, proof_payload
        assert "preview-status" in rules(proof_payload), proof_payload.get("problems")


def test_strict_preview_proof_rejects_forged_https_sni_safe_pinning() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        root = live_dir(project)
        root.mkdir(parents=True, exist_ok=True)
        url = "https://93.184.216.34/"
        http = root / "http.json"
        http.write_text(json.dumps({
            "schema": "star-forge.preview-http.v1",
            "attempted": True,
            "method": "GET",
            "url": url,
            "final_url": url,
            "status": 200,
            "expected_status": 200,
            "ok": True,
            "redirect_chain": [],
            "connected_ips": ["93.184.216.34"],
            "connection_pinning": {
                "strategy": "https-connect-vetted-ip-sni-safe",
                "sni_safe": True,
                "server_hostname": "93.184.216.34",
            },
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        deployment = root / "deployment.json"
        deployment.write_text(json.dumps({"source_hash": source_hash(project), "deployment_id": "dep-forged-https"}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        smoke = root / "smoke.json"
        smoke.write_text(json.dumps({"checks": [{"name": "home", "passed": True}]}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        live_common.write_live_manifest(
            project,
            task=TASK,
            collector="preview",
            command_argv=["test-preview-forged-https"],
            tool_versions={"test": "1"},
            artifacts={"http": http, "deployment": deployment, "smoke": smoke},
            summary={"url": url},
        )
        code, payload, _ = run_star_cli([
            "preview-proof", "--project", str(project), "--task", TASK,
            "--url", url, "--expect-status", "200",
            "--deployment-metadata", str(deployment), "--smoke-checks", str(smoke), "--strict",
        ])
        assert code == 1, payload
        assert payload["verdict"] == "FAIL", payload
        assert "preview-http" in rules(payload), payload.get("problems")


def test_unsafe_url_rejection_writes_degraded_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        code, payload, _ = run_preview(
            collector_args(project, "file:///tmp/preview.html", "--deployment-source-hash", source_hash(project))
        )
        assert_failed_with(code, payload, "preview-url")
        manifest = load_manifest(project)
        assert manifest["degraded"] is True
        proof_code, proof_payload, _ = run_star_cli(payload["proof_command"][2:])
        assert proof_code == 1, proof_payload
        assert "manifest-degraded" in {item.get("rule") for item in proof_payload["problems"]}


def test_unsafe_redirect_rejection() -> None:
    with tempfile.TemporaryDirectory() as tmp, TestServer({"/redirect": redirect("http://169.254.169.254/latest/meta-data")}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_server_lease(project, server.url("/redirect"))
        code, payload, _ = run_preview(
            collector_args(
                project,
                server.url("/redirect"),
                "--server-lease",
                str(lease),
                "--deployment-source-hash",
                source_hash(project),
            )
        )
        assert_failed_with(code, payload, "preview-redirect")


def test_localhost_without_lease_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": route(200, "ok")}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        code, payload, _ = run_preview(
            collector_args(project, server.url("/"), "--deployment-source-hash", source_hash(project))
        )
        assert_failed_with(code, payload, "preview-localhost")


def test_incomplete_server_lease_blocks_before_request() -> None:
    hits: list[str] = []

    def counted(handler: BaseHTTPRequestHandler) -> None:
        hits.append("hit")
        route(200, "ok")(handler)

    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": counted}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        parsed = preview.urllib.parse.urlparse(server.url("/"))
        lease = project / ".starforge" / "runtime" / "server.json"
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_text(json.dumps({
            "schema": "star-forge.server-lease.v1",
            "port": parsed.port,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        code, payload, _ = run_preview(
            collector_args(
                project,
                server.url("/"),
                "--server-lease",
                str(lease),
                "--deployment-source-hash",
                source_hash(project),
            )
        )
        assert_failed_with(code, payload, "preview-localhost")
        assert hits == []


def test_stale_server_lease_blocks_before_request() -> None:
    hits: list[str] = []

    def counted(handler: BaseHTTPRequestHandler) -> None:
        hits.append("hit")
        route(200, "ok")(handler)

    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": counted}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_server_lease(project, server.url("/"))
        payload = json.loads(lease.read_text(encoding="utf-8"))
        payload["source_hash"] = "stale-source"
        lease.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        code, payload, _ = run_preview(
            collector_args(
                project,
                server.url("/"),
                "--server-lease",
                str(lease),
                "--deployment-source-hash",
                source_hash(project),
            )
        )
        assert_failed_with(code, payload, "preview-localhost")
        assert hits == []


def test_missing_source_bound_identity_is_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": route(200, "ok")}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_server_lease(project, server.url("/"))
        code, payload, _ = run_preview(
            collector_args(project, server.url("/"), "--server-lease", str(lease))
        )
        assert_failed_with(code, payload, "preview-source-binding")
        deployment = json.loads((live_dir(project) / "deployment.json").read_text(encoding="utf-8"))
        assert "source_hash" not in deployment


def test_stale_source_binding_is_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": route(200, "ok")}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_server_lease(project, server.url("/"))
        code, payload, _ = run_preview(
            collector_args(
                project,
                server.url("/"),
                "--server-lease",
                str(lease),
                "--deployment-source-hash",
                "stale-source-hash",
            )
        )
        assert_failed_with(code, payload, "preview-source-binding")


def test_commit_only_source_binding_rejects_uncommitted_source_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": route(200, "ok")}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        head = commit_all(project)
        (project / "src" / "app.py").write_text("print('dirty preview source')\n", encoding="utf-8")
        lease = write_server_lease(project, server.url("/"))
        code, payload, _ = run_preview(
            collector_args(
                project,
                server.url("/"),
                "--server-lease",
                str(lease),
                "--deployment-commit-sha",
                head,
            )
        )
        assert_failed_with(code, payload, "preview-source-binding")


def test_source_hash_binding_rejects_clean_committed_gradle_config_change() -> None:
    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": route(200, "ok")}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        (project / "settings.gradle").write_text("pluginManagement { repositories { google() } }\n", encoding="utf-8")
        commit_all(project, "gradle settings v1")
        old_source = source_hash(project)
        (project / "settings.gradle").write_text("pluginManagement { repositories { mavenCentral() } }\n", encoding="utf-8")
        commit_all(project, "gradle settings v2")
        assert source_hash(project) != old_source
        assert not star_forge.source_dirty_entries(star_forge.git_status(project))
        lease = write_server_lease(project, server.url("/"))
        code, payload, _ = run_preview(
            collector_args(
                project,
                server.url("/"),
                "--server-lease",
                str(lease),
                "--deployment-source-hash",
                old_source,
            )
        )
        assert_failed_with(code, payload, "preview-source-binding")


def test_source_hash_binding_rejects_tracked_generated_dir_changes() -> None:
    cases = [
        ("build/preview.js", "console.log('preview build v1')\n", "console.log('preview build v2')\n"),
        ("dist/preview.js", "console.log('preview dist v1')\n", "console.log('preview dist v2')\n"),
        ("target/preview.txt", "target preview v1\n", "target preview v2\n"),
    ]
    for rel_path, before, after in cases:
        with tempfile.TemporaryDirectory() as tmp, TestServer({"/": route(200, "ok")}) as server:
            project = Path(tmp).resolve()
            init_project(project)
            (project / ".gitignore").write_text("build/\ndist/\ntarget/\n", encoding="utf-8")
            tracked = project / rel_path
            tracked.parent.mkdir(parents=True, exist_ok=True)
            tracked.write_text(before, encoding="utf-8")
            force_add(project, rel_path)
            commit_all(project, f"{rel_path} v1")
            old_source = source_hash(project)

            tracked.write_text(after, encoding="utf-8")
            commit_all(project, f"{rel_path} v2")
            assert source_hash(project) != old_source
            assert not star_forge.source_dirty_entries(star_forge.git_status(project))
            lease = write_server_lease(project, server.url("/"))

            code, payload, _ = run_preview(
                collector_args(
                    project,
                    server.url("/"),
                    "--server-lease",
                    str(lease),
                    "--deployment-source-hash",
                    old_source,
                )
            )
            assert_failed_with(code, payload, "preview-source-binding")


def test_shared_address_space_preview_urls_are_rejected_direct_and_dns() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        code, payload, _ = run_preview(
            collector_args(project, "http://100.64.0.1/", "--deployment-source-hash", source_hash(project))
        )
        assert_failed_with(code, payload, "preview-url")

    original = preview.socket.getaddrinfo

    def fake_getaddrinfo(host: str, port: int | None = None, *args: Any, **kwargs: Any) -> list[Any]:
        if host == "shared.example.test":
            return [(preview.socket.AF_INET, preview.socket.SOCK_STREAM, 6, "", ("100.64.0.1", port or 80))]
        return original(host, port, *args, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        preview.socket.getaddrinfo = fake_getaddrinfo
        try:
            code, payload, _ = run_preview(
                collector_args(project, "http://shared.example.test/", "--deployment-source-hash", source_hash(project))
            )
        finally:
            preview.socket.getaddrinfo = original
        assert_failed_with(code, payload, "preview-url")


def test_failed_http_status_is_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": route(503, "down")}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_server_lease(project, server.url("/"))
        code, payload, _ = run_preview(
            collector_args(
                project,
                server.url("/"),
                "--server-lease",
                str(lease),
                "--deployment-source-hash",
                source_hash(project),
                "--expect-status",
                "200",
            )
        )
        assert_failed_with(code, payload, "preview-status")


def test_failed_smoke_check_is_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": route(200, "plain ok")}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_server_lease(project, server.url("/"))
        code, payload, _ = run_preview(
            collector_args(
                project,
                server.url("/"),
                "--server-lease",
                str(lease),
                "--deployment-source-hash",
                source_hash(project),
                "--smoke-check",
                "contains:missing text",
            )
        )
        assert_failed_with(code, payload, "preview-smoke")


def test_token_redaction_covers_query_headers_manifest_and_artifacts() -> None:
    secret = "preview-secret-1234567890"
    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": route(200, "ok")}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        code, payload, _ = run_preview(
            collector_args(
                project,
                server.url(f"/?token={secret}"),
                "--local-preview-mode",
                "--deployment-source-hash",
                source_hash(project),
                "--header",
                f"Authorization=Bearer {secret}",
            )
        )
        assert_failed_with(code, payload, "preview-url")
        artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(live_dir(project).glob("*.json")))
        assert secret not in artifact_text
        assert "Bearer" not in artifact_text
        assert "[REDACTED]" in artifact_text


def test_provider_signed_preview_urls_are_blocked_and_redacted_before_request() -> None:
    cases = [
        (
            "s3",
            "X-Amz-Signature=amzsig123&X-Amz-Credential=amzcred123&AWSAccessKeyId=accessid123",
            ("amzsig123", "amzcred123", "accessid123"),
        ),
        (
            "gcs",
            "X-Goog-Signature=googsig123&X-Goog-Credential=googcred123&signedheaders=hostsecret123",
            ("googsig123", "googcred123", "hostsecret123"),
        ),
        (
            "azure",
            "sig=azsig123&sp=permsecret123&se=expirysecret123&sv=versionsecret123",
            ("azsig123", "permsecret123", "expirysecret123", "versionsecret123"),
        ),
    ]
    for provider, query, secrets in cases:
        hits: list[str] = []

        def counted(handler: BaseHTTPRequestHandler) -> None:
            hits.append(provider)
            route(200, "ok")(handler)

        with tempfile.TemporaryDirectory() as tmp, TestServer({"/": counted}) as server:
            project = Path(tmp).resolve()
            init_project(project)
            code, payload, _ = run_preview(
                collector_args(
                    project,
                    server.url(f"/?{query}"),
                    "--deployment-source-hash",
                    source_hash(project),
                )
            )
            assert_failed_with(code, payload, "preview-url")
            assert hits == []
            artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(live_dir(project).glob("*.json")))
            for secret in secrets:
                assert secret not in artifact_text
            assert "[REDACTED]" in artifact_text


def test_provider_signed_headers_block_before_request() -> None:
    hits: list[str] = []
    secret = "amzheadersecret123"

    def counted(handler: BaseHTTPRequestHandler) -> None:
        hits.append("hit")
        route(200, "ok")(handler)

    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": counted}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_server_lease(project, server.url("/"))
        code, payload, _ = run_preview(
            collector_args(
                project,
                server.url("/"),
                "--server-lease",
                str(lease),
                "--deployment-source-hash",
                source_hash(project),
                "--header",
                f"X-Amz-Signature={secret}",
            )
        )
        assert_failed_with(code, payload, "preview-header")
        assert hits == []
        artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(live_dir(project).glob("*.json")))
        assert secret not in artifact_text
        assert "X-Amz-Signature=[REDACTED]" in artifact_text


def test_preview_pins_http_connection_to_vetted_ip_and_records_it() -> None:
    original = preview.socket.getaddrinfo
    original_connection = preview.http.client.HTTPConnection
    calls: list[str] = []
    connections: list[dict[str, Any]] = []

    def fake_getaddrinfo(host: str, port: int | None = None, *args: Any, **kwargs: Any) -> list[Any]:
        if host == "rebind.example.test":
            address = "93.184.216.34" if not calls else "127.0.0.1"
            calls.append(address)
            return [(preview.socket.AF_INET, preview.socket.SOCK_STREAM, 6, "", (address, port or 80))]
        return original(host, port, *args, **kwargs)

    class FakeResponse:
        status = 200

        def getheaders(self) -> list[tuple[str, str]]:
            return [("Content-Type", "text/plain")]

        def read(self, size: int) -> bytes:
            return b"Star Forge Preview OK"

    class FakeConnection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            assert host != "rebind.example.test"
            self.record = {"host": host, "port": port, "timeout": timeout}
            connections.append(self.record)

        def request(self, method: str, target: str, headers: dict[str, str]) -> None:
            self.record.update({"method": method, "target": target, "headers": dict(headers)})

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            self.record["closed"] = True

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp).resolve()
        init_project(project)
        preview.socket.getaddrinfo = fake_getaddrinfo
        preview.http.client.HTTPConnection = FakeConnection
        try:
            code, payload, _ = run_preview(
                collector_args(
                    project,
                    "http://rebind.example.test/",
                    "--deployment-source-hash",
                    source_hash(project),
                )
            )
        finally:
            preview.socket.getaddrinfo = original
            preview.http.client.HTTPConnection = original_connection
        assert code == 0, payload
        assert calls == ["93.184.216.34"]
        assert connections[0]["host"] == "93.184.216.34"
        assert connections[0]["headers"]["Host"] == "rebind.example.test"
        http_payload = json.loads((live_dir(project) / "http.json").read_text(encoding="utf-8"))
        assert http_payload["connected_ips"] == ["93.184.216.34"]


def test_local_build_artifact_alone_cannot_prove_preview() -> None:
    with tempfile.TemporaryDirectory() as tmp, TestServer({"/": route(200, "ok")}) as server:
        project = Path(tmp).resolve()
        init_project(project)
        lease = write_server_lease(project, server.url("/"))
        build_artifact = project / "dist" / "bundle.js"
        build_artifact.parent.mkdir()
        build_artifact.write_text("console.log('built')\n", encoding="utf-8")
        code, payload, _ = run_preview(
            collector_args(
                project,
                server.url("/"),
                "--server-lease",
                str(lease),
                "--local-build-artifact",
                str(build_artifact),
            )
        )
        assert_failed_with(code, payload, "preview-source-binding")
        messages = " ".join(str(item.get("message")) for item in payload["problems"])
        assert "local build artifact alone" in messages


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
    print(f"\ntest_live_preview.py: {passed} passed, {len(failed)} failed, {len(tests)} total")
    if failed:
        print("failed tests: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
