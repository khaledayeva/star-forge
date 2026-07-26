#!/usr/bin/env python3
"""Focused v0.4 change packet tests."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from starforge import changes


TEMPLATES = ROOT / "templates"
SOURCE_HASH = hashlib.sha256(b"completed source").hexdigest()


class ChangePacketTests(unittest.TestCase):
    def create_packet(
        self,
        project: Path,
        change_id: str = "CHANGE-1",
        *,
        created_at: str = "2026-07-25T18:30:00Z",
    ) -> dict[str, object]:
        return changes.create_change_packet(
            project,
            change_id=change_id,
            original_completed_source_hash=SOURCE_HASH,
            scope_delta=["src/app.py", "tests/test_app.py"],
            affected_acs=["AC-51", "AC-49", "AC-49"],
            delivery_impact="source-only handoff must be repeated",
            created_at=created_at,
            template_dir=TEMPLATES,
        )

    def test_create_packet_has_complete_schema_and_preserves_root_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            root_plan = project / "Plan.md"
            root_plan.write_text("# Historical Plan\n\ncomplete\n", encoding="utf-8")
            original_plan = root_plan.read_bytes()

            packet = self.create_packet(project)

            self.assertEqual(root_plan.read_bytes(), original_plan)
            self.assertEqual(packet["schema"], changes.CHANGE_SCHEMA)
            self.assertEqual(packet["change_id"], "CHANGE-1")
            self.assertEqual(packet["original_completed_source_hash"], SOURCE_HASH)
            self.assertEqual(packet["scope_delta"], ["src/app.py", "tests/test_app.py"])
            self.assertEqual(packet["affected_acs"], ["AC-49", "AC-51"])
            self.assertEqual(packet["approval_state"], "draft")
            self.assertIsNone(packet["approved_at"])
            packet_root = project / ".starforge" / "changes" / "CHANGE-1"
            self.assertTrue((packet_root / "change.md").is_file())
            self.assertTrue((packet_root / "Plan.md").is_file())
            self.assertTrue((packet_root / "evidence").is_dir())
            self.assertTrue((packet_root / "review").is_dir())
            self.assertIn(
                "original completed source hash",
                (packet_root / "change.md").read_text(encoding="utf-8").lower(),
            )
            self.assertIn(
                "root Plan",
                (packet_root / "Plan.md").read_text(encoding="utf-8"),
            )

    def test_approval_is_atomic_state_transition_and_root_sources_are_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "src" / "app.py"
            source.parent.mkdir(parents=True)
            source.write_text("print('historical')\n", encoding="utf-8")
            root_plan = project / "Plan.md"
            root_plan.write_text("# Plan\n", encoding="utf-8")
            source_before = source.read_bytes()
            plan_before = root_plan.read_bytes()
            self.create_packet(project)

            packet = changes.approve_change_packet(
                project,
                "CHANGE-1",
                approved_at="2026-07-25T19:00:00+00:00",
            )

            self.assertEqual(packet["approval_state"], "approved")
            self.assertEqual(packet["approved_at"], "2026-07-25T19:00:00+00:00")
            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual(root_plan.read_bytes(), plan_before)
            packet_root = project / ".starforge" / "changes" / "CHANGE-1"
            self.assertEqual(list(packet_root.glob(".change.*.tmp")), [])
            with self.assertRaises(changes.ChangePacketError):
                changes.approve_change_packet(project, "CHANGE-1")

    def test_safe_ids_hashes_and_packet_paths_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            for unsafe in ("../escape", "/absolute", ".hidden", "bad id", "x..y"):
                with self.subTest(unsafe=unsafe):
                    with self.assertRaises(changes.ChangePacketError):
                        changes.create_change_packet(
                            project,
                            change_id=unsafe,
                            original_completed_source_hash=SOURCE_HASH,
                            scope_delta=["src/app.py"],
                            affected_acs=["AC-49"],
                            delivery_impact="none",
                            template_dir=TEMPLATES,
                        )
            with self.assertRaises(changes.ChangePacketError):
                changes.create_change_packet(
                    project,
                    change_id="CHANGE-1",
                    original_completed_source_hash="not-a-hash",
                    scope_delta=["src/app.py"],
                    affected_acs=["AC-49"],
                    delivery_impact="none",
                    template_dir=TEMPLATES,
                )

            self.create_packet(project)
            packet_root = project / ".starforge" / "changes" / "CHANGE-1"
            change_path = packet_root / "change.md"
            text = change_path.read_text(encoding="utf-8")
            change_path.write_text(
                text.replace("**Evidence**: evidence", "**Evidence**: ../evidence"),
                encoding="utf-8",
            )
            with self.assertRaises(changes.ChangePacketError):
                changes.read_change_packet(project, "CHANGE-1")

    def test_duplicate_creation_never_overwrites_existing_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.create_packet(project)
            change_path = project / ".starforge" / "changes" / "CHANGE-1" / "change.md"
            original = change_path.read_bytes()

            with self.assertRaises(changes.ChangePacketError):
                self.create_packet(project)

            self.assertEqual(change_path.read_bytes(), original)

    def test_lookup_and_filter_order_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.create_packet(
                project,
                "CHANGE-20",
                created_at="2026-07-25T20:00:00Z",
            )
            self.create_packet(
                project,
                "CHANGE-3",
                created_at="2026-07-25T19:00:00Z",
            )
            changes.approve_change_packet(
                project,
                "CHANGE-20",
                approved_at="2026-07-25T21:00:00Z",
            )

            self.assertEqual(
                [item["change_id"] for item in changes.list_change_packets(project)],
                ["CHANGE-3", "CHANGE-20"],
            )
            self.assertEqual(
                changes.lookup_change_packet(project, "CHANGE-3")["change_id"],
                "CHANGE-3",
            )
            self.assertIsNone(changes.lookup_change_packet(project, "CHANGE-99"))
            self.assertEqual(
                [
                    item["change_id"]
                    for item in changes.find_change_packets(
                        project,
                        affected_ac="AC-49",
                        approval_state="draft",
                    )
                ],
                ["CHANGE-3"],
            )

    def test_legacy_amend_rows_remain_byte_identical_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            plan = project / "Plan.md"
            plan.write_text(
                "# Plan\n\n"
                "| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |\n"
                "|---|---|---|---|---|---|---|---|\n"
                "| SF-1 | Original | complete | delegate | src/app.py | - | pytest | proof.json |\n"
                "| AMEND-2 | Second old change | complete | solo | src/b.py | SF-1 | pytest -q | old-2.json |\n"
                "| AMEND-1 | First old change | complete | solo | src/a.py | SF-1 | pytest -q | old-1.json |\n",
                encoding="utf-8",
            )
            original = plan.read_bytes()
            self.create_packet(project)

            history = changes.change_history(project)

            self.assertEqual(plan.read_bytes(), original)
            self.assertEqual(history["packet_count"], 1)
            self.assertEqual(history["legacy_amendment_count"], 2)
            self.assertEqual(
                [item["change_id"] for item in history["entries"]],
                ["AMEND-1", "AMEND-2", "CHANGE-1"],
            )
            self.assertEqual(
                [item["change_id"] for item in history["legacy_amendments"]],
                ["AMEND-1", "AMEND-2"],
            )
            self.assertEqual(
                changes.lookup_change_history(project, "AMEND-2")["description"],
                "Second old change",
            )
            self.assertEqual(
                changes.lookup_change_history(project, "CHANGE-1")["kind"],
                "change-packet",
            )
            self.assertTrue(
                all(
                    item["plan_version"] == "legacy"
                    for item in history["legacy_amendments"]
                )
            )

    def test_symlinked_packet_content_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.create_packet(project)
            packet = project / ".starforge" / "changes" / "CHANGE-1"
            outside = project / "outside"
            outside.mkdir()
            (packet / "evidence").rmdir()
            (packet / "evidence").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(changes.ChangePacketError):
                changes.read_change_packet(project, "CHANGE-1")


if __name__ == "__main__":
    unittest.main()
