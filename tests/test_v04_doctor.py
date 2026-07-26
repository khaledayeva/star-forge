#!/usr/bin/env python3
"""Focused tests for the read-only Star Forge installation doctor.

Run with: python3 tests/test_v04_doctor.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SCRIPT = SCRIPTS / "star_forge.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from starforge import doctor


SPEC = importlib.util.spec_from_file_location("star_forge_doctor_cli", SCRIPT)
assert SPEC and SPEC.loader
star_forge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(star_forge)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def install_fixture(
    codex_home: Path,
    marketplace: str,
    version: str,
    *,
    runtime_matches_source: bool,
    disabled: bool = False,
) -> Path:
    marketplace_name = marketplace + (".disabled" if disabled else "")
    root = codex_home / "plugins" / "cache" / marketplace_name / "star-forge" / version
    write_json(
        root / ".codex-plugin" / "plugin.json",
        {"name": "star-forge", "version": version},
    )
    runtime = root / "scripts" / "star_forge.py"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    if runtime_matches_source:
        shutil.copyfile(SCRIPT, runtime)
    else:
        runtime.write_text("#!/usr/bin/env python3\nprint('legacy')\n", encoding="utf-8")
    hooks = root / "hooks" / "hooks.json"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "hooks" / "hooks.json", hooks)
    return root


def canonical_config(codex_home: Path) -> None:
    (codex_home / "config.toml").write_text(
        """
[marketplaces.star-forge]
source_type = "git"
source = "https://github.com/khaledayeva/star-forge"
""".lstrip(),
        encoding="utf-8",
    )


def snapshot_tree(root: Path) -> dict[str, tuple[bytes, int, int]]:
    result: dict[str, tuple[bytes, int, int]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        stat = path.stat()
        result[str(path.relative_to(root))] = (
            path.read_bytes(),
            stat.st_mode,
            stat.st_mtime_ns,
        )
    return result


def findings_by_rule(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for finding in payload["findings"]:
        result.setdefault(finding["rule"], []).append(finding)
    return result


def source_version() -> str:
    return json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )["version"]


def test_clean_canonical_install_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        canonical_config(codex_home)
        active = install_fixture(
            codex_home,
            "star-forge",
            source_version(),
            runtime_matches_source=True,
        )
        payload = doctor.diagnose_installation(
            codex_home=codex_home,
            source_root=ROOT,
            runtime_version=star_forge.SF_VERSION,
            active_plugin_root=active,
        )
        assert payload["schema"] == doctor.DOCTOR_SCHEMA
        assert payload["read_only"] is True
        assert payload["verdict"] == "PASS", payload
        assert payload["findings"] == []
        assert {check["id"] for check in payload["checks"]} == set(doctor.CHECK_RULES)
        assert all(check["status"] == "pass" for check in payload["checks"])


def test_doctor_detects_every_ac5_installation_problem() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        legacy_source = Path(tmp) / "missing-local-marketplace"
        (codex_home / "config.toml").write_text(
            f"""
[marketplaces.star-forge-v3]
source_type = "local"
source = "{legacy_source}"

[mcp_servers.mobbin]
url = "https://example.invalid/mobbin"
""".lstrip(),
            encoding="utf-8",
        )
        old = install_fixture(
            codex_home,
            "star-forge-v3",
            "0.3.0",
            runtime_matches_source=False,
        )
        install_fixture(
            codex_home,
            "star-forge-v3",
            source_version(),
            runtime_matches_source=True,
            disabled=True,
        )
        write_json(
            codex_home / "hook-trust.json",
            {
                "trusted_hooks": {
                    "star-forge": {
                        "plugin": "star-forge",
                        "plugin_version": "0.3.0",
                        "plugin_root": str(Path(tmp) / "removed-star-forge"),
                    }
                }
            },
        )
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
        payload = doctor.diagnose_installation(
            codex_home=codex_home,
            source_root=ROOT,
            runtime_version=star_forge.SF_VERSION,
        )
        after = snapshot_tree(codex_home)

        assert before == after
        assert payload["verdict"] == "ATTENTION"
        by_rule = findings_by_rule(payload)
        assert set(by_rule) == set(doctor.CHECK_RULES), by_rule
        assert "no longer exists" in by_rule[doctor.RULE_STALE_MARKETPLACE][0]["message"]
        assert len(by_rule[doctor.RULE_DUPLICATE_INSTALL][0]["paths"]) == 2
        assert "active version" in by_rule[doctor.RULE_ACTIVE_VERSION_DRIFT][0]["message"]
        assert "trusted version" in by_rule[doctor.RULE_STALE_HOOK_TRUST][0]["message"]
        assert len(by_rule[doctor.RULE_DUPLICATE_MOBBIN][0]["paths"]) == 2
        assert all(finding["remediation"] for finding in payload["findings"])


def test_cli_default_is_advisory_and_strict_reports_attention() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        active = install_fixture(
            codex_home,
            "legacy-star-forge",
            "0.3.0",
            runtime_matches_source=False,
        )
        (codex_home / "config.toml").write_text(
            f"""
[marketplaces.legacy-star-forge]
source_type = "local"
source = "{Path(tmp) / 'gone'}"
""".lstrip(),
            encoding="utf-8",
        )
        args = [
            "doctor",
            "--codex-home",
            str(codex_home),
            "--source-root",
            str(ROOT),
            "--active-plugin-root",
            str(active),
        ]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = star_forge.main(args)
        payload = json.loads(stdout.getvalue())
        assert code == 0
        assert payload["verdict"] == "ATTENTION"

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            strict_code = star_forge.main(args + ["--strict"])
        strict_payload = json.loads(stdout.getvalue())
        assert strict_code == 1
        assert strict_payload == payload


def test_mobbin_single_connection_is_not_a_duplicate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        codex_home = Path(tmp) / ".codex"
        codex_home.mkdir()
        active = install_fixture(
            codex_home,
            "star-forge",
            source_version(),
            runtime_matches_source=True,
        )
        (codex_home / "config.toml").write_text(
            """
[marketplaces.star-forge]
source_type = "git"
source = "https://github.com/khaledayeva/star-forge"

[mcp_servers.mobbin]
url = "https://example.invalid/mobbin"
""".lstrip(),
            encoding="utf-8",
        )
        payload = doctor.diagnose_installation(
            codex_home=codex_home,
            source_root=ROOT,
            runtime_version=star_forge.SF_VERSION,
            active_plugin_root=active,
        )
        assert doctor.RULE_DUPLICATE_MOBBIN not in findings_by_rule(payload)
        check = next(
            item for item in payload["checks"] if item["id"] == doctor.RULE_DUPLICATE_MOBBIN
        )
        assert check["status"] == "pass"
        assert len(check["details"]) == 1


def main() -> int:
    tests = [
        (name, value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures: list[tuple[str, str]] = []
    for name, test in tests:
        try:
            test()
            print(f"PASS {name}")
        except Exception:
            failures.append((name, traceback.format_exc()))
            print(f"FAIL {name}")
    if failures:
        for name, detail in failures:
            print(f"\n{name}\n{detail}", file=sys.stderr)
        return 1
    print(f"\n{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
