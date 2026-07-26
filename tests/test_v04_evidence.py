#!/usr/bin/env python3
"""Focused tests for evidence envelope v2 and v1 compatibility."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from starforge import evidence


FIXTURES = ROOT / "fixtures" / "evidence-v2"


def fixture(name: str = "pass.json") -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def raises(message: str, function, *args, **kwargs) -> None:
    try:
        function(*args, **kwargs)
    except evidence.EvidenceError as exc:
        assert message in str(exc), exc
    else:
        raise AssertionError(f"expected EvidenceError containing {message!r}")


def test_v2_fixture_validates_and_reader_returns_the_envelope() -> None:
    payload = fixture()
    evidence.validate_envelope(payload)
    assert evidence.read_envelope(FIXTURES / "pass.json") == payload


def test_schema_requires_identity_hash_time_artifact_verdict_and_blocker_fields() -> None:
    payload = fixture()
    for field in evidence.REQUIRED_FIELDS:
        broken = copy.deepcopy(payload)
        del broken[field]
        raises(field, evidence.validate_envelope, broken)

    for field in ("kind", "task", "capability", "provider"):
        broken = copy.deepcopy(payload)
        broken[field] = ""
        raises(field, evidence.validate_envelope, broken)

    broken = copy.deepcopy(payload)
    broken["provenance"] = {}
    raises("provenance", evidence.validate_envelope, broken)
    broken = copy.deepcopy(payload)
    broken["source_hash"] = "ABC"
    raises("source_hash", evidence.validate_envelope, broken)
    broken = copy.deepcopy(payload)
    broken["runtime_asset_hash"] = "f" * 63
    raises("runtime_asset_hash", evidence.validate_envelope, broken)
    broken = copy.deepcopy(payload)
    broken["finished_at"] = "2026-07-25T19:59:59Z"
    raises("finished_at", evidence.validate_envelope, broken)
    broken = copy.deepcopy(payload)
    broken["verdict"] = "OK"
    raises("verdict", evidence.validate_envelope, broken)
    broken = copy.deepcopy(payload)
    broken["blockers"] = {}
    raises("blockers", evidence.validate_envelope, broken)


def test_artifact_references_are_relative_normalized_unique_and_hash_bound() -> None:
    for unsafe in (
        "/tmp/proof.json",
        "../proof.json",
        "proof/../../escape.json",
        r"C:\proof.json",
        r"proof\result.json",
        "https://example.com/proof.json",
        "./proof.json",
    ):
        broken = copy.deepcopy(fixture())
        broken["artifacts"][0]["path"] = unsafe
        raises("artifact path", evidence.validate_envelope, broken)

    broken = copy.deepcopy(fixture())
    broken["artifacts"][0]["sha256"] = "not-a-hash"
    raises("sha256", evidence.validate_envelope, broken)
    broken = copy.deepcopy(fixture())
    broken["artifacts"].append(copy.deepcopy(broken["artifacts"][0]))
    raises("more than once", evidence.validate_envelope, broken)


def test_artifact_verification_checks_file_hash_size_and_symlink_escape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        artifact = project / "proof.txt"
        artifact.write_bytes(b"abc")
        payload = fixture()
        payload["artifacts"] = [
            {
                "path": "proof.txt",
                "kind": "transcript",
                "sha256": hashlib.sha256(b"abc").hexdigest(),
                "bytes": 3,
            }
        ]
        evidence.validate_envelope(payload, project_root=project, verify_artifacts=True)
        payload["artifacts"][0]["sha256"] = "f" * 64
        raises(
            "hash does not match",
            evidence.validate_envelope,
            payload,
            project_root=project,
            verify_artifacts=True,
        )

        outside = Path(tmp) / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        link = project / "link.txt"
        link.symlink_to(outside)
        payload["artifacts"][0].update(
            path="link.txt",
            sha256=hashlib.sha256(b"private").hexdigest(),
            bytes=7,
        )
        raises("escapes", evidence.validate_envelope, payload, project_root=project)


def test_secret_material_is_rejected_anywhere_in_the_envelope() -> None:
    payload = fixture()
    payload["provenance"]["authorization"] = "Bearer actual-secret-value"
    raises("secret material", evidence.validate_envelope, payload)
    payload = fixture()
    payload["blockers"] = ["collector leaked sk-abcdefghijklmnopqrstuvwxyz"]
    raises("secret material", evidence.validate_envelope, payload)


def test_writer_is_atomic_validated_and_leaves_no_temporary_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "nested" / "proof.json"
        written = evidence.write_envelope(target, fixture())
        assert json.loads(target.read_text(encoding="utf-8")) == written
        assert not list(target.parent.glob(f".{target.name}.*.tmp"))

        original = target.read_bytes()
        broken = fixture()
        broken["source_hash"] = "invalid"
        raises("source_hash", evidence.write_envelope, target, broken)
        assert target.read_bytes() == original


def test_v1_reader_adapts_without_rewriting_the_legacy_manifest() -> None:
    source = FIXTURES / "live-manifest-v1.json"
    original = source.read_bytes()
    adapted = evidence.read_evidence(source)
    assert source.read_bytes() == original
    assert adapted["schema"] == evidence.EVIDENCE_SCHEMA
    assert adapted["kind"] == "browser"
    assert adapted["capability"] == "local-web-qa"
    assert adapted["provider"] == "browser"
    assert adapted["provenance"]["adapter"] == evidence.LEGACY_LIVE_MANIFEST_SCHEMA
    assert adapted["source_hash"] == "a" * 64
    assert adapted["runtime_asset_hash"] == "b" * 64
    assert adapted["verdict"] == "PASS"
    assert adapted["blockers"] == []
    assert adapted["artifacts"][0]["sha256"] == hashlib.sha256(b"abc").hexdigest()
    raises("not allowed", evidence.read_envelope, source, allow_v1=False)


def test_v1_degradation_and_blocking_problems_map_to_honest_verdicts() -> None:
    legacy = fixture("live-manifest-v1.json")
    legacy["degraded"] = True
    legacy["unavailable_capabilities"] = ["in-app-browser"]
    adapted = evidence.adapt_live_manifest_v1(legacy)
    assert adapted["verdict"] == "DEGRADED"
    assert adapted["blockers"][0]["blocking"] is False

    legacy["problems"] = [
        {
            "severity": "high",
            "rule": "browser-console",
            "message": "console error",
            "blocking": True,
        }
    ]
    adapted = evidence.adapt_v1_manifest(legacy)
    assert adapted["verdict"] == "FAIL"
    assert any(blocker.get("rule") == "browser-console" for blocker in adapted["blockers"])

    legacy["problems"][0]["blocking"] = False
    adapted = evidence.adapt_v1_manifest(legacy)
    assert adapted["verdict"] == "DEGRADED"


def test_v1_adapter_rejects_unsafe_paths_and_secret_bearing_provenance() -> None:
    legacy = fixture("live-manifest-v1.json")
    legacy["artifacts"][0]["path"] = "../../outside.json"
    legacy["raw_artifact_hashes"] = {}
    adapted = evidence.adapt_v1_manifest(legacy)
    assert adapted["verdict"] == "FAIL"
    assert adapted["artifacts"] == []
    assert "project-relative" in adapted["blockers"][0]["message"] or "escapes" in adapted["blockers"][0]["message"]

    legacy = fixture("live-manifest-v1.json")
    legacy["command_argv"].append("GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz")
    raises("secret material", evidence.adapt_v1_manifest, legacy)


def test_expo_uses_standard_task_evidence_plus_v2_delivery_proof() -> None:
    task = fixture()
    task.update(
        kind="task",
        task="SF-EXPO-001",
        capability="expo",
        provider="expo-plugin",
        provenance={
            "evidence_source": "coordinator-recorded-plan-verify",
            "route": "expo",
            "selected": "expo-plugin",
            "status": "available",
        },
        artifacts=[],
    )
    evidence.validate_envelope(task)

    delivery = copy.deepcopy(task)
    delivery.update(
        kind="delivery",
        capability="expo-platform-delivery",
        provenance={
            "evidence_source": "coordinator-recorded-delivery",
            "route": "expo-platform-delivery",
            "selected": "expo-plugin",
            "status": "available",
            "delivery_contract": "expo",
        },
    )
    evidence.validate_envelope(delivery)
    assert delivery["schema"] == evidence.EVIDENCE_SCHEMA
    assert delivery["source_hash"] == task["source_hash"]
    assert delivery["runtime_asset_hash"] == task["runtime_asset_hash"]
    assert delivery["kind"] == "delivery"
    assert delivery["capability"] == "expo-platform-delivery"


def test_expo_fallback_and_unavailable_delivery_proof_are_not_misrepresented() -> None:
    fallback = fixture()
    fallback.update(
        kind="delivery",
        task="SF-EXPO-002",
        capability="expo-platform-delivery",
        provider="expo-cli",
        provenance={
            "evidence_source": "coordinator-recorded-delivery",
            "route": "expo-platform-delivery",
            "selected": "expo-cli",
            "status": "degraded",
            "fallback_used": True,
            "unavailable": ["expo-plugin"],
        },
        artifacts=[],
    )
    evidence.validate_envelope(fallback)
    assert fallback["provider"] == "expo-cli"
    assert fallback["provenance"]["fallback_used"] is True

    unavailable = copy.deepcopy(fallback)
    unavailable.update(
        provider="expo-platform-delivery-unavailable",
        verdict="FAIL",
        blockers=[
            {
                "message": "official Expo plugin and repository-native Expo CLI are unavailable",
                "blocking": True,
            }
        ],
    )
    unavailable["provenance"].update(
        selected="expo-platform-delivery-unavailable",
        status="blocked",
    )
    evidence.validate_envelope(unavailable)
    assert unavailable["verdict"] == "FAIL"
    assert unavailable["blockers"][0]["blocking"] is True


def test_expo_contract_does_not_add_a_live_collector() -> None:
    collectors = ROOT / "scripts" / "live_collectors"
    assert not list(collectors.glob("*expo*"))
    assert not list(collectors.glob("*react_native*"))

    reference = (
        ROOT / "skills" / "forge" / "references" / "capability-routing.md"
    ).read_text(encoding="utf-8")
    assert "There is no Expo-specific live collector." in reference
    assert "normal coordinator-recorded Plan Verify evidence" in reference
    assert "`star-forge.evidence-envelope.v2`" in reference
    assert "`capability` set to `expo-platform-delivery`" in reference


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} tests passed")
