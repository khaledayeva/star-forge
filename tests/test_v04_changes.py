#!/usr/bin/env python3
"""Focused v0.4 change packet tests."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from starforge import changes, lifecycle
from starforge import runtime_plan
from starforge import runtime_review
import star_forge


TEMPLATES = ROOT / "templates"
SOURCE_HASH = hashlib.sha256(b"completed source").hexdigest()
SPECIAL_PATHS = (
    "back\\slash.py",
    "C:\\absolute",
    "\\\\server\\share",
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


class ChangePacketTests(unittest.TestCase):
    def write_modern_project(self, project: Path) -> bytes:
        blueprint = """# Blueprint

Status: approved

- **Project class**: web-app
- **Delivery target**: preview

## Risk Flags

| Flag | Value | Reason |
|---|---|---|
| User-facing UI | no | backend-only delta |
| Authentication or authorization | yes | signed-in API |
| Multiple services or high coupling | no | one service |

## Acceptance Criteria

- AC-1: Authentication remains correct.
- AC-2: The dashboard remains usable.
"""
        plan = """# Plan

Status: active

## Task Ledger

| Task | Description | Status | Mode | Files | Depends | ACs | Proof | Verify | Evidence |
|---|---|---|---|---|---|---|---|---|---|
| SF-1 | Build API | complete | solo | src/api.py, tests/test_api.py | - | AC-1 | unit, security | python3 tests/test_api.py | proof-api.json |
| SF-2 | Build dashboard | complete | delegate | web/ | - | AC-2 | browser | npm test | proof-web.json |
"""
        (project / "Blueprint.md").write_text(blueprint, encoding="utf-8")
        root_plan = project / "Plan.md"
        root_plan.write_text(plan, encoding="utf-8")
        contract_path = project / lifecycle.DELIVERY_CONTRACT_PATH
        contract_path.parent.mkdir(parents=True)
        contract_path.write_text(
            json.dumps(lifecycle.make_delivery_contract(
                delivery_target="preview",
                project_class="web-app",
                provider="sites",
                destination="team preview",
            )),
            encoding="utf-8",
        )
        return root_plan.read_bytes()

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
            change_path = packet_root / "change.md"
            legacy_text = change_path.read_text(encoding="utf-8")
            for path in ("src/app.py", "tests/test_app.py"):
                legacy_text = legacy_text.replace(
                    "- " + json.dumps(path), "- " + path
                )
            change_path.write_text(legacy_text, encoding="utf-8")
            self.assertEqual(
                changes.read_change_packet(project, "CHANGE-1")["scope_delta"],
                ["src/app.py", "tests/test_app.py"],
            )

    def test_special_scope_paths_round_trip_and_unsafe_forms_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            packet = changes.create_change_packet(
                project,
                change_id="CHANGE-1",
                original_completed_source_hash=SOURCE_HASH,
                scope_delta=SPECIAL_PATHS,
                affected_acs=["AC-49"],
                delivery_impact="source-only",
                template_dir=TEMPLATES,
            )
            self.assertEqual(packet["scope_delta"], list(SPECIAL_PATHS))
            self.assertEqual(
                changes.read_change_packet(project, "CHANGE-1")["scope_delta"],
                list(SPECIAL_PATHS),
            )
            text = (
                project / ".starforge" / "changes" / "CHANGE-1" / "change.md"
            ).read_text(encoding="utf-8")
            for path in SPECIAL_PATHS:
                self.assertIn("- " + json.dumps(path), text)

        for unsafe in (
            "", "\0", "/absolute", "a//b", "./a", "a/./b",
            "a/../b", "a/", ".", "..",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(changes.ChangePacketError):
                    changes.normalize_changed_files([unsafe])

    def test_raw_git_bytes_round_trip_through_packet_plan_reuse_and_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.write_modern_project(project)
            plan_path = project / "Plan.md"
            plan_path.write_text(
                plan_path.read_text(encoding="utf-8").replace(
                    "src/api.py, tests/test_api.py",
                    "src/",
                ),
                encoding="utf-8",
            )
            raw_path = b"src/nonutf8-\xff-\\|-[x],; -> name.py"
            decoded_path = os.fsdecode(raw_path)
            raw_status = os.fsdecode(b"?? " + raw_path + b"\0")
            with mock.patch.object(
                star_forge.live_common,
                "run_git",
                return_value=(0, raw_status, ""),
            ):
                self.assertEqual(
                    star_forge.live_common.git_status(project),
                    [decoded_path],
                )

            packet = changes.create_or_select_change_packet(
                project,
                original_completed_source_hash=SOURCE_HASH,
                changed_files=[decoded_path],
                template_dir=TEMPLATES,
            )
            reused = changes.create_or_select_change_packet(
                project,
                original_completed_source_hash=SOURCE_HASH,
                changed_files=[decoded_path],
                template_dir=TEMPLATES,
            )
            self.assertEqual(reused["change_id"], packet["change_id"])
            self.assertEqual(packet["scope_delta"], [decoded_path])
            self.assertEqual(packet["impact"]["scope_delta"], [decoded_path])
            task = changes.change_plan_tasks(project, packet["change_id"])[0]
            self.assertEqual(star_forge.task_files(task), [decoded_path])

            packet_root = project / changes.CHANGE_ROOT / packet["change_id"]
            for artifact in ("change.md", "impact.json", "Plan.md"):
                self.assertIn(
                    b"\\udcff",
                    (packet_root / artifact).read_bytes(),
                    artifact,
                )

            changes.approve_change_packet(project, packet["change_id"])
            active = changes.activate_change_plan(project, packet["change_id"])[0]
            self.assertEqual(active["status"], "ready")
            self.assertEqual(star_forge.task_files(active), [decoded_path])
            runtime_plan.update_plan_task_row(
                packet_root / "Plan.md",
                active["id"],
                {
                    "status": "complete",
                    "evidence": json.dumps(
                        [decoded_path, "proof|with-pipe.json"],
                        separators=(",", ":"),
                    ),
                },
            )
            completed = changes.change_plan_tasks(project, packet["change_id"])[0]
            self.assertEqual(completed["status"], "complete")
            self.assertEqual(star_forge.task_files(completed), [decoded_path])
            self.assertEqual(
                json.loads(completed["evidence"]),
                [decoded_path, "proof|with-pipe.json"],
            )

            finding = json.loads(json.dumps({
                "file": decoded_path,
                "line": 7,
                "title": "Raw path finding",
                "detail": "The path must remain reversible",
            }))
            first = runtime_review.finding_fingerprint(finding)
            second = runtime_review.finding_fingerprint(dict(finding))
            self.assertEqual(first, second)
            self.assertTrue(first.startswith("sha256:"))

    def test_json_file_owners_are_exact_while_legacy_globs_remain_patterns(self) -> None:
        literal_paths = [
            "src/literal*.py",
            "src/literal?.py",
            "src/literal[ab].py",
            "src/literal].py",
        ]
        unrelated_paths = [
            "src/literal-match.py",
            "src/literalQ.py",
            "src/literala.py",
            "src/literal.py",
        ]
        exact_task = {
            "id": "SF-1",
            "mode": "delegate",
            "files": json.dumps(literal_paths, separators=(",", ":")),
            "acs": "AC-1",
            "proof": "unit",
            "verify": "python3 tests/test_api.py",
        }
        exact = changes.derive_change_impact(
            changed_files=[*literal_paths, *unrelated_paths],
            root_tasks=[exact_task],
            blueprint_text="- AC-1: Literal paths remain exact.",
        )
        owned = next(
            task for task in exact["affected_tasks"]
            if task["source_task"] == "SF-1"
        )
        unowned = next(
            task for task in exact["affected_tasks"]
            if task["source_task"] is None
        )
        self.assertEqual(owned["files"], literal_paths)
        self.assertEqual(unowned["files"], unrelated_paths)

        legacy_task = {
            **exact_task,
            "files": "src/literal*.py",
        }
        legacy = changes.derive_change_impact(
            changed_files=["src/literal*.py", "src/literal-match.py"],
            root_tasks=[legacy_task],
            blueprint_text="- AC-1: Legacy patterns remain compatible.",
        )
        self.assertEqual(
            legacy["affected_tasks"][0]["files"],
            ["src/literal*.py", "src/literal-match.py"],
        )
        self.assertEqual(legacy["unmatched_files"], [])

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
            self.assertFalse(packet["approval_identity_bound"])
            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual(root_plan.read_bytes(), plan_before)
            packet_root = project / ".starforge" / "changes" / "CHANGE-1"
            self.assertEqual(list(packet_root.glob(".change.*.tmp")), [])
            with self.assertRaises(changes.ChangePacketError):
                changes.approve_change_packet(project, "CHANGE-1")

    def test_approval_rederives_scope_impact_and_packet_plan(self) -> None:
        for tamper in ("impact", "profile", "plan"):
            with self.subTest(tamper=tamper):
                with tempfile.TemporaryDirectory() as tmp:
                    project = Path(tmp)
                    self.write_modern_project(project)
                    packet = changes.create_or_select_change_packet(
                        project, original_completed_source_hash=SOURCE_HASH,
                        changed_files=["src/api.py"], template_dir=TEMPLATES)
                    root = project / packet["path"]
                    if tamper in {"impact", "profile"}:
                        path = root / changes.CHANGE_IMPACT_FILE
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        if tamper == "impact":
                            payload["review_roles"] = []
                        else:
                            payload["profile"] = "fast-mvp"
                        path.write_text(json.dumps(payload), encoding="utf-8")
                    else:
                        path = root / changes.CHANGE_PLAN_FILE
                        path.write_text(
                            path.read_text(encoding="utf-8").replace(
                                "src/api.py", "README.md"), encoding="utf-8")
                    with self.assertRaisesRegex(
                            changes.ChangePacketError, "scope, impact, and Plan"):
                        changes.approve_change_packet(project, packet["change_id"])

    def test_approval_identity_survives_status_evidence_and_rejects_contract_tamper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.write_modern_project(project)
            packet = changes.create_or_select_change_packet(
                project, original_completed_source_hash=SOURCE_HASH,
                changed_files=["src/api.py"], template_dir=TEMPLATES)
            approved = changes.approve_change_packet(project, packet["change_id"])
            self.assertTrue(approved["approval_identity_bound"])
            root = project / packet["path"]
            self.assertTrue((root / changes.CHANGE_APPROVAL_FILE).is_file())
            changes.activate_change_plan(project, packet["change_id"])
            runtime_plan.update_plan_task_row(
                root / changes.CHANGE_PLAN_FILE, "CHANGE-1-T1",
                {"status": "complete", "evidence": "proof.json"})
            self.assertTrue(
                changes.read_change_packet(
                    project, packet["change_id"])["approval_identity_bound"])
            plan = root / changes.CHANGE_PLAN_FILE
            plan.write_text(
                plan.read_text(encoding="utf-8").replace(
                    "Revalidate affected root task", "Trust unrelated root task"),
                encoding="utf-8")
            with self.assertRaisesRegex(changes.ChangePacketError, "approval identity"):
                changes.read_change_packet(project, packet["change_id"])

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

    def test_scope_derivation_promotes_code_and_selects_risk_proof_delivery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.write_modern_project(project)

            impact = changes.derive_change_impact_for_project(
                project,
                ["src/api.py"],
                profile="fast-mvp",
            )

            self.assertEqual(impact["scope_delta"], ["src/api.py"])
            self.assertEqual(impact["affected_task_ids"], ["SF-1"])
            self.assertEqual(impact["affected_acs"], ["AC-1"])
            self.assertTrue(impact["delegation_required"])
            task = impact["affected_tasks"][0]
            self.assertEqual(task["mode"], "delegate")
            self.assertEqual(task["verify"], "python3 tests/test_api.py")
            self.assertEqual(
                task["proof_kinds"],
                ["delivery", "preview", "security", "unit"],
            )
            self.assertEqual(
                impact["delivery_revalidation"]["target"],
                "preview",
            )
            self.assertIn("security", impact["review_roles"])
            self.assertIn("correctness", impact["review_lenses"])
            self.assertNotIn("browser", impact["proof_kinds"])

    def test_canonical_delivery_contract_drives_each_amendment_target(self) -> None:
        cases = (
            ("preview", "sites", {"delivery", "preview"}),
            ("private-repo", "", {"delivery", "github"}),
            ("package", "", {"delivery", "package"}),
            ("production", "sites", {"delivery"}),
            ("ios", "", {"delivery"}),
        )
        for target, provider, expected in cases:
            with self.subTest(target=target):
                with tempfile.TemporaryDirectory() as tmp:
                    project = Path(tmp)
                    self.write_modern_project(project)
                    contract = lifecycle.make_delivery_contract(
                        delivery_target=target,
                        project_class="web-app",
                        provider=provider,
                        production_authorized=target == "production",
                    )
                    path = project / lifecycle.DELIVERY_CONTRACT_PATH
                    path.write_text(json.dumps(contract), encoding="utf-8")

                    impact = changes.derive_change_impact_for_project(
                        project, ["src/api.py"],
                    )

                    self.assertEqual(
                        impact["delivery_revalidation"]["target"], target,
                    )
                    self.assertTrue(
                        expected.issubset(impact["proof_kinds"]),
                        (target, impact["proof_kinds"]),
                    )

    def test_invalid_canonical_delivery_contract_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.write_modern_project(project)
            path = project / lifecycle.DELIVERY_CONTRACT_PATH
            path.write_text(
                json.dumps({
                    "schema": lifecycle.DELIVERY_CONTRACT_SCHEMA,
                    "target": {"kind": "preview"},
                }),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                changes.ChangePacketError, "delivery contract is invalid",
            ):
                changes.derive_change_impact_for_project(
                    project, ["src/api.py"],
                )

    def test_create_or_select_packet_keeps_root_historical_until_approval(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            root_plan_before = self.write_modern_project(project)

            first = changes.create_or_select_change_packet(
                project,
                original_completed_source_hash=SOURCE_HASH,
                changed_files=["src/api.py"],
                created_at="2026-07-25T20:00:00Z",
                template_dir=TEMPLATES,
            )
            second = changes.create_or_select_change_packet(
                project,
                original_completed_source_hash=SOURCE_HASH,
                changed_files=["src/api.py"],
                template_dir=TEMPLATES,
            )

            self.assertEqual(first["change_id"], "CHANGE-1")
            self.assertEqual(second["change_id"], "CHANGE-1")
            self.assertEqual((project / "Plan.md").read_bytes(), root_plan_before)
            self.assertNotIn("AMEND-", (project / "Plan.md").read_text())
            draft_tasks = changes.change_plan_tasks(project, "CHANGE-1")
            self.assertEqual([task["status"] for task in draft_tasks], ["queued"])
            self.assertEqual(draft_tasks[0]["mode"], "delegate")
            self.assertEqual(
                draft_tasks[0]["verify"],
                "python3 tests/test_api.py",
            )

            approved = changes.approve_change_packet(
                project,
                "CHANGE-1",
                approved_at="2026-07-25T21:00:00Z",
            )
            ready = changes.activate_change_plan(project, "CHANGE-1")

            self.assertEqual(approved["approval_state"], "approved")
            self.assertEqual([task["status"] for task in ready], ["ready"])
            self.assertEqual((project / "Plan.md").read_bytes(), root_plan_before)

    def test_legacy_cross_plan_task_id_ambiguity_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.write_modern_project(project)
            packet = changes.create_or_select_change_packet(
                project,
                original_completed_source_hash=SOURCE_HASH,
                changed_files=["src/api.py"],
                template_dir=TEMPLATES,
            )
            packet_plan = (
                project / packet["path"] / packet["plan_path"]
            )
            packet_plan.write_text(
                packet_plan.read_text(encoding="utf-8").replace(
                    "CHANGE-1-T1", "SF-1",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                star_forge.ForgeError,
                r"Task SF-1 is ambiguous across project Plans: .*Plan.md",
            ):
                runtime_plan.task_plan(project, "SF-1")
            with self.assertRaisesRegex(
                changes.ChangePacketError,
                "ambiguous task IDs across project Plans",
            ):
                changes.approve_change_packet(project, "CHANGE-1")

    def test_unowned_code_never_inherits_unrelated_verification_or_approves(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.write_modern_project(project)

            packet = changes.create_or_select_change_packet(
                project,
                original_completed_source_hash=SOURCE_HASH,
                changed_files=["scripts/new_worker.py"],
                template_dir=TEMPLATES,
            )

            task = changes.change_plan_tasks(project, packet["change_id"])[0]
            self.assertEqual(task["mode"], "delegate")
            self.assertEqual(task["verify"], "REVIEW_REQUIRED")
            self.assertNotEqual(task["verify"], "python3 tests/test_api.py")
            self.assertTrue(packet["impact"]["approval_blockers"])
            with self.assertRaises(changes.ChangePacketError):
                changes.approve_change_packet(project, packet["change_id"])

    def test_approve_change_cli_preserves_command_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.write_modern_project(project)
            changes.create_or_select_change_packet(
                project,
                original_completed_source_hash=SOURCE_HASH,
                changed_files=["src/api.py"],
                template_dir=TEMPLATES,
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = star_forge.main(
                    [
                        "approve-change",
                        "--project",
                        str(project),
                        "--change",
                        "CHANGE-1",
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["approval_state"], "approved")
            self.assertEqual(payload["ready"], ["CHANGE-1-T1"])


if __name__ == "__main__":
    unittest.main()
