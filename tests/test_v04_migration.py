#!/usr/bin/env python3
"""Project-level migration coverage for immutable Star Forge v0.3 fixtures."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import star_forge
from starforge import changes, contracts, evidence, migration


FIXTURES = ROOT / "fixtures" / "legacy-v03"
TEMPLATES = ROOT / "templates"
COMPLETED = FIXTURES / "completed-amended"
DEGRADED = FIXTURES / "degraded-preview"
COMPLETED_SOURCE_HASH = "1" * 64


def tree_digest(root: Path) -> str:
    """Hash paths and bytes so a read-only check detects any fixture rewrite."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"symlink:")
            digest.update(path.readlink().as_posix().encode("utf-8"))
        elif path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def copy_fixture(source: Path, target: Path) -> Path:
    project = target / source.name
    shutil.copytree(source, project)
    stored_state = project / "dot-starforge"
    if stored_state.exists():
        materialized_state = project / ".starforge"
        if materialized_state.exists():
            shutil.rmtree(materialized_state)
        stored_state.rename(materialized_state)
    return project


class LegacyProjectInspectionTests(unittest.TestCase):
    def test_completed_fixture_is_readable_across_every_v03_surface(self) -> None:
        original = tree_digest(COMPLETED)

        with tempfile.TemporaryDirectory() as tmp:
            project = copy_fixture(COMPLETED, Path(tmp))
            snapshot = migration.inspect_legacy_project(project)

        self.assertEqual(tree_digest(COMPLETED), original)
        self.assertEqual(snapshot["schema"], migration.LEGACY_PROJECT_INSPECTION_SCHEMA)
        self.assertEqual(snapshot["problems"], [])
        self.assertEqual(snapshot["plan"]["version"], "legacy")
        self.assertEqual(
            [task["id"] for task in snapshot["plan"]["tasks"]],
            ["SF-001", "SF-002", "AMEND-1", "AMEND-2"],
        )
        self.assertTrue(
            all(
                task["acs"] == "" and task["proof"] == ""
                for task in snapshot["plan"]["tasks"]
            )
        )
        self.assertEqual(
            [item["change_id"] for item in snapshot["amendments"]],
            ["AMEND-1", "AMEND-2"],
        )
        self.assertEqual(len(snapshot["reviews"]["findings"]), 3)
        self.assertEqual(len(snapshot["reviews"]["merged"]), 1)
        merged = snapshot["reviews"]["merged"][0]
        self.assertTrue(merged["historical"])
        self.assertEqual(merged["finding_count"], 1)
        self.assertEqual(merged["open_finding_count"], 0)
        self.assertEqual(
            {item["task"] for item in snapshot["completions"]["task_records"]},
            {"SF-001", "SF-002", "AMEND-1", "AMEND-2"},
        )
        proof = snapshot["completions"]["final_proof"]
        self.assertIsNotNone(proof)
        assert proof is not None
        self.assertTrue(proof["historical"])
        self.assertEqual(proof["schema"], migration.LEGACY_PROOF_SCHEMA)
        self.assertEqual(proof["source_hash"], COMPLETED_SOURCE_HASH)
        self.assertEqual(snapshot["state"]["phase"], "done")
        self.assertTrue(snapshot["state"]["historical"])
        adapted = snapshot["evidence"][0]["adapted"]
        self.assertEqual(snapshot["evidence"][0]["source_schema"], evidence.LEGACY_LIVE_MANIFEST_SCHEMA)
        self.assertEqual(adapted["schema"], evidence.EVIDENCE_SCHEMA)
        self.assertEqual(adapted["verdict"], "PASS")
        self.assertEqual(adapted["source_hash"], COMPLETED_SOURCE_HASH)
        self.assertEqual(adapted["provenance"]["adapter"], evidence.LEGACY_LIVE_MANIFEST_SCHEMA)

    def test_degraded_v1_preview_remains_degraded_and_nonblocking(self) -> None:
        original = tree_digest(DEGRADED)

        with tempfile.TemporaryDirectory() as tmp:
            project = copy_fixture(DEGRADED, Path(tmp))
            snapshot = migration.inspect_legacy_project(project)

        self.assertEqual(tree_digest(DEGRADED), original)
        self.assertEqual(snapshot["problems"], [])
        self.assertEqual(
            [item["change_id"] for item in snapshot["amendments"]],
            ["AMEND-1"],
        )
        adapted = snapshot["evidence"][0]["adapted"]
        self.assertEqual(adapted["verdict"], "DEGRADED")
        self.assertEqual(adapted["provider"], "preview")
        self.assertEqual(len(adapted["blockers"]), 2)
        self.assertTrue(
            all(blocker.get("blocking") is False for blocker in adapted["blockers"])
        )
        self.assertTrue(
            any(
                blocker.get("rule") == "preview-provider-unavailable"
                for blocker in adapted["blockers"]
            )
        )
        self.assertTrue(
            any(
                blocker.get("capability") == "hosted-preview"
                for blocker in adapted["blockers"]
            )
        )


class LegacyPlanMigrationTests(unittest.TestCase):
    def test_explicit_plan_draft_preserves_source_and_invents_no_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = copy_fixture(COMPLETED, Path(tmp))
            source = project / "Plan.md"
            output = project / "drafts" / "Plan.v2.md"
            source_before = source.read_bytes()
            historical_before = {
                path.relative_to(project).as_posix(): path.read_bytes()
                for path in project.rglob("*")
                if path.is_file()
            }

            result = contracts.write_plan_v2_migration(source, output)

            self.assertTrue(result["source_preserved"])
            self.assertTrue(result["review_required"])
            self.assertEqual(result["legacy_tables_migrated"], 1)
            self.assertEqual(result["task_rows_migrated"], 4)
            self.assertEqual(source.read_bytes(), source_before)
            for relative, content in historical_before.items():
                self.assertEqual((project / relative).read_bytes(), content)
            tasks = contracts.parse_plan_tasks_text(output.read_text(encoding="utf-8"))
            self.assertEqual(
                [task["id"] for task in tasks],
                ["SF-001", "SF-002", "AMEND-1", "AMEND-2"],
            )
            self.assertTrue(
                all(
                    task["acs"] == contracts.PLAN_REVIEW_REQUIRED
                    and task["proof"] == contracts.PLAN_REVIEW_REQUIRED
                    for task in tasks
                )
            )
            self.assertEqual(
                [item["change_id"] for item in changes.legacy_amendment_history(project)],
                ["AMEND-1", "AMEND-2"],
            )

    def test_cli_rejects_final_and_intermediate_source_symlinks(self) -> None:
        if not hasattr(Path, "symlink_to"):
            self.skipTest("symlinks unavailable")
        for intermediate in (False, True):
            with self.subTest(intermediate=intermediate), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = copy_fixture(COMPLETED, root)
                outside = root / "outside"
                outside.mkdir()
                (outside / "Plan.md").write_bytes((project / "Plan.md").read_bytes())
                source = project / "input"
                if intermediate:
                    source.symlink_to(outside, target_is_directory=True)
                    source_arg = "input/Plan.md"
                else:
                    source = source.with_suffix(".md")
                    source.symlink_to(outside / "Plan.md")
                    source_arg = source.name
                with self.assertRaises(star_forge.ForgeError):
                    star_forge.cmd_migrate_plan(argparse.Namespace(
                        project=str(project), file=source_arg,
                        output="drafts/Plan.v2.md"))
                self.assertFalse((project / "drafts" / "Plan.v2.md").exists())

    def test_cli_rejects_intermediate_and_dangling_output_symlinks(self) -> None:
        if not hasattr(Path, "symlink_to"):
            self.skipTest("symlinks unavailable")
        for intermediate in (False, True):
            with self.subTest(intermediate=intermediate), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = copy_fixture(COMPLETED, root)
                outside = root / "outside"
                outside.mkdir()
                drafts = project / "drafts"
                if intermediate:
                    drafts.symlink_to(outside, target_is_directory=True)
                else:
                    drafts.mkdir()
                    (drafts / "Plan.v2.md").symlink_to(
                        outside / "missing-plan.md")
                with self.assertRaises(star_forge.ForgeError):
                    star_forge.cmd_migrate_plan(argparse.Namespace(
                        project=str(project), file="Plan.md",
                        output="drafts/Plan.v2.md"))
                self.assertFalse((outside / "Plan.v2.md").exists())
                self.assertFalse((outside / "missing-plan.md").exists())

    def test_cli_rejects_absolute_source_and_output_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = copy_fixture(COMPLETED, Path(tmp))
            cases = (
                (str(project / "Plan.md"), "drafts/Plan.v2.md"),
                ("Plan.md", str(project / "drafts" / "Plan.v2.md")),
            )
            for source, output in cases:
                with self.subTest(source=source, output=output):
                    with self.assertRaisesRegex(
                            star_forge.ForgeError, "project-relative"):
                        star_forge.cmd_migrate_plan(argparse.Namespace(
                            project=str(project), file=source, output=output))

    def test_automatic_packet_derivation_refuses_unmapped_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = copy_fixture(COMPLETED, Path(tmp))
            before = tree_digest(project)

            with self.assertRaisesRegex(
                changes.ChangePacketError,
                "requires reviewed Plan v2 AC and Proof mappings",
            ):
                changes.create_or_select_change_packet(
                    project,
                    original_completed_source_hash=COMPLETED_SOURCE_HASH,
                    changed_files=["M  src/app.py"],
                    template_dir=TEMPLATES,
                )

            self.assertEqual(tree_digest(project), before)
            self.assertFalse((project / changes.CHANGE_ROOT).exists())


class LegacyChangeTransitionTests(unittest.TestCase):
    def test_explicit_packet_preserves_root_history_and_uses_only_supplied_ac(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = copy_fixture(COMPLETED, Path(tmp))
            plan_before = (project / "Plan.md").read_bytes()
            proof_before = (project / ".starforge" / "final" / "proof.json").read_bytes()
            review_before = (
                project
                / ".starforge"
                / "reviews"
                / "ab12cd34ef56ab78"
                / "merged.json"
            ).read_bytes()

            packet = changes.create_change_packet(
                project,
                change_id="CHANGE-1",
                original_completed_source_hash=COMPLETED_SOURCE_HASH,
                scope_delta=["src/app.py"],
                affected_acs=["AC-2"],
                delivery_impact="source-only handoff must be repeated",
                created_at="2026-06-22T12:00:00Z",
                template_dir=TEMPLATES,
            )
            approved = changes.approve_change_packet(
                project,
                "CHANGE-1",
                approved_at="2026-06-22T12:05:00Z",
            )
            history = changes.change_history(project)

            self.assertEqual((project / "Plan.md").read_bytes(), plan_before)
            self.assertEqual(
                (project / ".starforge" / "final" / "proof.json").read_bytes(),
                proof_before,
            )
            self.assertEqual(
                (
                    project
                    / ".starforge"
                    / "reviews"
                    / "ab12cd34ef56ab78"
                    / "merged.json"
                ).read_bytes(),
                review_before,
            )
            self.assertEqual(packet["affected_acs"], ["AC-2"])
            self.assertEqual(approved["approval_state"], "approved")
            self.assertEqual(
                [item["change_id"] for item in history["entries"]],
                ["AMEND-1", "AMEND-2", "CHANGE-1"],
            )
            self.assertEqual(history["legacy_amendment_count"], 2)
            self.assertEqual(history["packet_count"], 1)
            packet_plan = (
                project / changes.CHANGE_ROOT / "CHANGE-1" / changes.CHANGE_PLAN_FILE
            ).read_text(encoding="utf-8")
            self.assertNotIn("| CHANGE-1-", packet_plan)
            self.assertNotIn("unit", packet_plan)
            self.assertNotIn("integration", packet_plan)


class LegacyEvidenceHonestyTests(unittest.TestCase):
    def test_source_drift_in_v1_manifest_adapts_to_fail_without_writing(self) -> None:
        fixture_before = tree_digest(COMPLETED)
        with tempfile.TemporaryDirectory() as tmp:
            project = copy_fixture(COMPLETED, Path(tmp))
            source = (
                project
                / ".starforge"
                / "live"
                / "SF-002"
                / "browser"
                / "manifest.json"
            )
            source_before = source.read_bytes()
            payload = copy.deepcopy(json.loads(source.read_text(encoding="utf-8")))
            payload["source_hash_after"] = "9" * 64

            adapted = evidence.adapt_v1_manifest(payload)

            self.assertEqual(source.read_bytes(), source_before)
        self.assertEqual(tree_digest(COMPLETED), fixture_before)
        self.assertEqual(adapted["verdict"], "FAIL")
        self.assertEqual(adapted["source_hash"], "9" * 64)
        self.assertTrue(
            any(
                blocker.get("rule") == "source-hash-changed"
                and blocker.get("blocking") is True
                for blocker in adapted["blockers"]
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
