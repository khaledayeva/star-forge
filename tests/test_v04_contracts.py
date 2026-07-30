#!/usr/bin/env python3
"""Focused v0.4 contract tests."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "star_forge.py"
SPEC = importlib.util.spec_from_file_location("star_forge_v04_contracts", SCRIPT)
assert SPEC and SPEC.loader
star_forge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(star_forge)

from starforge import contracts


BLUEPRINT = """# Blueprint

Status: draft
Last approved: not approved yet

## Product Goal

Ship the approved behavior.

## Acceptance Criteria

- AC-1: The behavior is observable.
"""


class BlueprintLockTests(unittest.TestCase):
    def test_approval_writes_exact_source_bound_schema_without_editing_blueprint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            blueprint_path = project / "Blueprint.md"
            blueprint_path.write_text(BLUEPRINT, encoding="utf-8")
            original = blueprint_path.read_bytes()

            payload = contracts.write_blueprint_lock(
                project,
                approved_at="2026-07-25T18:30:00Z",
            )

            expected_hash = hashlib.sha256(original).hexdigest()
            self.assertEqual(
                set(payload),
                {
                    "schema",
                    "blueprint_sha256",
                    "approved_at",
                    "contract_version",
                },
            )
            self.assertEqual(payload["schema"], "star-forge.blueprint-lock.v1")
            self.assertEqual(payload["blueprint_sha256"], expected_hash)
            self.assertEqual(payload["contract_version"], 1)
            self.assertEqual(blueprint_path.read_bytes(), original)
            self.assertEqual(
                json.loads((project / "Blueprint.lock.json").read_text()),
                payload,
            )
            state = contracts.blueprint_lock_state(project)
            self.assertEqual(state["status"], "locked")
            self.assertTrue(state["approved"])
            self.assertTrue(state["locked"])
            self.assertFalse(state["legacy_approved"])

    def test_blueprint_edit_invalidates_lock_until_revision_is_approved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            blueprint_path = project / "Blueprint.md"
            blueprint_path.write_text(BLUEPRINT, encoding="utf-8")
            first = contracts.write_blueprint_lock(
                project,
                approved_at="2026-07-25T18:30:00+00:00",
            )

            blueprint_path.write_text(
                BLUEPRINT.replace("Ship the approved behavior.", "Ship revision two."),
                encoding="utf-8",
            )
            drifted = contracts.blueprint_lock_state(project)
            self.assertEqual(drifted["status"], "drifted")
            self.assertFalse(drifted["approved"])
            self.assertFalse(star_forge.blueprint_is_approved(project))
            self.assertIsNone(star_forge.scope_hash(project))
            self.assertNotEqual(
                drifted["current_sha256"],
                first["blueprint_sha256"],
            )

            second = contracts.write_blueprint_lock(
                project,
                approved_at="2026-07-25T19:00:00Z",
            )
            self.assertNotEqual(
                second["blueprint_sha256"],
                first["blueprint_sha256"],
            )
            self.assertEqual(
                contracts.blueprint_lock_state(project)["status"],
                "locked",
            )

    def test_legacy_approval_is_readable_but_not_a_v04_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "Blueprint.md").write_text(
                BLUEPRINT.replace("Status: draft", "Status: approved"),
                encoding="utf-8",
            )

            state = contracts.blueprint_lock_state(project)

            self.assertEqual(state["status"], "legacy-approved")
            self.assertTrue(state["approved"])
            self.assertTrue(state["legacy_approved"])
            self.assertFalse(state["locked"])
            self.assertTrue(star_forge.blueprint_is_approved(project))
            self.assertFalse(star_forge.blueprint_has_valid_lock(project))

    def test_invalid_lock_never_falls_back_to_legacy_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "Blueprint.md").write_text(
                BLUEPRINT.replace("Status: draft", "Status: approved"),
                encoding="utf-8",
            )
            (project / "Blueprint.lock.json").write_text(
                '{"schema": "star-forge.blueprint-lock.v0"}\n',
                encoding="utf-8",
            )

            state = contracts.blueprint_lock_state(project)

            self.assertEqual(state["status"], "invalid")
            self.assertFalse(state["approved"])
            self.assertFalse(state["legacy_approved"])
            self.assertFalse(star_forge.blueprint_is_approved(project))
            self.assertTrue(state["problems"])

    def test_legacy_completed_project_requires_lock_before_amendment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            star_forge.ensure_git_repo(project)
            star_forge.ensure_state_dirs(project)
            star_forge.ensure_project_manifest(project)
            (project / "Blueprint.md").write_text(
                BLUEPRINT.replace("Status: draft", "Status: approved"),
                encoding="utf-8",
            )
            (project / "Plan.md").write_text(
                "# Plan\n\n"
                "| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |\n"
                "|---|---|---|---|---|---|---|---|\n"
                "| SF-1 | Ship behavior | complete | solo | src/app.py | - | "
                "python3 -c \"print('ok')\" | .starforge/runs/verify.json |\n",
                encoding="utf-8",
            )
            root_plan_before = (project / "Plan.md").read_bytes()
            source = project / "src" / "app.py"
            source.parent.mkdir()
            source.write_text("print('one')\n", encoding="utf-8")
            star_forge.append_jsonl(
                project / star_forge.LEDGER_FILE,
                {
                    "schema": "star-forge.ledger.v1",
                    "timestamp": "2026-07-25T18:30:00Z",
                    "event": "setup",
                },
            )
            code, _, error = star_forge.run_git(["add", "."], project)
            self.assertEqual(code, 0, error)
            code, _, error = star_forge.run_git(
                [
                    "-c",
                    "user.name=Star Forge Test",
                    "-c",
                    "user.email=starforge@example.com",
                    "commit",
                    "-m",
                    "legacy completed state",
                ],
                project,
            )
            self.assertEqual(code, 0, error)
            star_forge.write_json(
                project / star_forge.PROOF_FILE,
                {
                    "schema": "star-forge.proof.v1",
                    "created_at": "2026-07-25T18:30:00Z",
                    "head": star_forge.git_head(project),
                    "source_hash": star_forge.source_hash(project),
                    "scope_hash": star_forge.scope_hash(project),
                    "verdict": "COMPLETE",
                },
            )
            source.write_text("print('two')\n", encoding="utf-8")
            run_args = argparse.Namespace(
                project=str(project),
                objective="",
                mode="cruise",
                fast_mvp=False,
                profile="",
                product_slug="",
                adopt_root=False,
                strict=False,
                no_auto_init=True,
                no_hooks=True,
                no_agents=False,
            )

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(star_forge.cmd_run(run_args), 0)
            state = star_forge.read_json(project / star_forge.CANONICAL_STATE)
            self.assertEqual(state["phase"], "plan")
            self.assertEqual(state["blueprint"]["status"], "legacy-approved")
            self.assertNotIn("AMEND-1", (project / "Plan.md").read_text())

            contracts.write_blueprint_lock(
                project,
                approved_at="2026-07-25T19:00:00Z",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(star_forge.cmd_run(run_args), 0)
            state = star_forge.read_json(project / star_forge.CANONICAL_STATE)
            self.assertEqual(state["phase"], "amend")
            self.assertEqual(state["blueprint"]["status"], "locked")
            self.assertEqual((project / "Plan.md").read_bytes(), root_plan_before)
            self.assertEqual(state["change_packet"]["change_id"], "CHANGE-1")
            self.assertEqual(state["change_packet"]["approval_state"], "draft")
            self.assertEqual(state["plan"]["ready"], [])
            self.assertEqual(state["spawn_plan"], [])
            self.assertEqual(
                [
                    packet["change_id"]
                    for packet in star_forge.project_changes.list_change_packets(
                        project
                    )
                ],
                ["CHANGE-1"],
            )

    def test_approval_command_is_explicit_and_reports_the_locked_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            blueprint_path = project / "Blueprint.md"
            blueprint_path.write_text(BLUEPRINT, encoding="utf-8")
            before = blueprint_path.read_bytes()
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = star_forge.cmd_approve_blueprint(
                    argparse.Namespace(project=str(project))
                )

            self.assertEqual(code, 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["schema"], "star-forge.blueprint-approval.v1")
            self.assertEqual(result["status"], "locked")
            self.assertEqual(
                result["blueprint_sha256"],
                hashlib.sha256(before).hexdigest(),
            )
            self.assertEqual(blueprint_path.read_bytes(), before)
            parser = star_forge.build_parser()
            parsed = parser.parse_args(
                ["approve-blueprint", "--project", str(project)]
            )
            self.assertIs(parsed.func, star_forge.cmd_approve_blueprint)

    def test_missing_or_symlinked_blueprint_is_not_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            with self.assertRaises(contracts.ContractError):
                contracts.write_blueprint_lock(project)
            self.assertFalse((project / "Blueprint.lock.json").exists())

            source = project / "outside.md"
            source.write_text(BLUEPRINT, encoding="utf-8")
            (project / "Blueprint.md").symlink_to(source)
            with self.assertRaises(contracts.ContractError):
                contracts.write_blueprint_lock(project)
            self.assertFalse((project / "Blueprint.lock.json").exists())

    def test_blueprint_state_uses_one_descriptor_snapshot_per_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            blueprint = project / "Blueprint.md"
            blueprint.write_text(BLUEPRINT, encoding="utf-8")
            locked = contracts.write_blueprint_lock(project)
            original_read = contracts.safe_io.read_snapshot
            calls: list[str] = []

            def read_then_swap(root: Path, path: Path, **kwargs):
                result = original_read(root, path, **kwargs)
                calls.append(Path(path).name)
                if Path(path).name == "Blueprint.md":
                    blueprint.write_text(BLUEPRINT + "\nchanged\n", encoding="utf-8")
                return result

            with mock.patch.object(
                contracts.safe_io, "read_snapshot", side_effect=read_then_swap,
            ):
                state = contracts.blueprint_lock_state(project)
            self.assertEqual(state["status"], "locked")
            self.assertEqual(state["current_sha256"], locked["blueprint_sha256"])
            self.assertEqual(calls.count("Blueprint.md"), 1)
            self.assertEqual(calls.count("Blueprint.lock.json"), 1)

    def test_blueprint_swap_to_symlink_and_symlinked_lock_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            blueprint = project / "Blueprint.md"
            blueprint.write_text(BLUEPRINT, encoding="utf-8")
            outside = root / "outside.md"
            outside.write_text("outside secret", encoding="utf-8")
            original_read = contracts.safe_io.read_snapshot

            def swap_before_open(boundary: Path, path: Path, **kwargs):
                if Path(path).name == "Blueprint.md":
                    blueprint.unlink()
                    blueprint.symlink_to(outside)
                return original_read(boundary, path, **kwargs)

            with mock.patch.object(
                contracts.safe_io, "read_snapshot", side_effect=swap_before_open,
            ):
                state = contracts.blueprint_lock_state(project)
            self.assertEqual(state["status"], "missing")
            self.assertIsNone(state["current_sha256"])

            blueprint.unlink()
            blueprint.write_text(BLUEPRINT, encoding="utf-8")
            outside_lock = root / "outside-lock.json"
            outside_lock.write_text("preserve me\n", encoding="utf-8")
            (project / "Blueprint.lock.json").symlink_to(outside_lock)
            with self.assertRaises(contracts.ContractError):
                contracts.write_blueprint_lock(project)
            self.assertEqual(outside_lock.read_text(encoding="utf-8"), "preserve me\n")

    def test_lock_validation_rejects_ambiguous_or_unversioned_payloads(self) -> None:
        valid = {
            "schema": contracts.BLUEPRINT_LOCK_SCHEMA,
            "blueprint_sha256": "a" * 64,
            "approved_at": "2026-07-25T18:30:00Z",
            "contract_version": 1,
        }
        self.assertEqual(contracts.validate_blueprint_lock(valid), [])
        self.assertTrue(
            contracts.validate_blueprint_lock(
                {**valid, "contract_version": True, "extra": "ambiguous"}
            )
        )
        self.assertTrue(
            contracts.validate_blueprint_lock(
                {**valid, "approved_at": "2026-07-25"}
            )
        )


class PlanV2ContractTests(unittest.TestCase):
    LEGACY = (
        "# Plan\n\n"
        "Keep this note unchanged.\n\n"
        "| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| SF-1 | Parse a legacy plan | ready | solo | src/a.py | - | "
        "python3 -c \"print('ok')\" | - |\n"
    )

    def test_dual_reader_exposes_v2_fields_without_breaking_legacy(self) -> None:
        legacy = contracts.parse_plan_tasks_text(self.LEGACY)
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0]["plan_version"], "legacy")
        self.assertEqual(legacy[0]["acs"], "")
        self.assertEqual(legacy[0]["proof"], "")
        self.assertEqual(star_forge.parse_tasks_from_text(self.LEGACY)[0]["id"], "SF-1")

        v2 = contracts.serialize_plan_tasks(
            [
                {
                    **legacy[0],
                    "acs": "AC-14, AC-18",
                    "proof": "unit, integration",
                    "description": "Round trip an escaped | character",
                }
            ]
        )
        self.assertEqual(
            v2.splitlines()[0],
            "| Task | Description | Status | Mode | Files | Depends | ACs | Proof | Verify | Evidence |",
        )
        parsed = contracts.parse_plan_tasks_text(v2)
        self.assertEqual(parsed[0]["plan_version"], "v2")
        self.assertEqual(parsed[0]["acs"], "AC-14, AC-18")
        self.assertEqual(parsed[0]["proof"], "unit, integration")
        self.assertEqual(
            parsed[0]["description"],
            "Round trip an escaped | character",
        )
        self.assertEqual(
            contracts.split_plan_row(r"| SF-2 | C:\tmp\plan.py |"),
            ["SF-2", r"C:\tmp\plan.py"],
        )

    def test_plan_cell_serializer_rejects_physical_line_breaks(self) -> None:
        for value in ("line\nbreak", "carriage\rreturn"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    contracts.ContractError, "physical line breaks",
                ):
                    contracts.encode_plan_cell(value)

    def test_migration_creates_reviewable_v2_draft_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "Plan.md"
            output = project / "Plan.v2.md"
            source.write_text(self.LEGACY, encoding="utf-8")
            original = source.read_bytes()

            result = contracts.write_plan_v2_migration(source, output)

            self.assertEqual(result["schema"], "star-forge.plan-migration.v1")
            self.assertTrue(result["source_preserved"])
            self.assertTrue(result["review_required"])
            self.assertEqual(result["legacy_tables_migrated"], 1)
            self.assertEqual(result["task_rows_migrated"], 1)
            self.assertEqual(source.read_bytes(), original)
            migrated = output.read_text(encoding="utf-8")
            self.assertIn("Keep this note unchanged.", migrated)
            task = contracts.parse_plan_tasks_text(migrated)[0]
            self.assertEqual(task["plan_version"], "v2")
            self.assertEqual(task["acs"], contracts.PLAN_REVIEW_REQUIRED)
            self.assertEqual(task["proof"], contracts.PLAN_REVIEW_REQUIRED)
            self.assertEqual(task["verify"], "python3 -c \"print('ok')\"")

    def test_migration_refuses_source_overwrite_and_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "Plan.md"
            output = project / "Plan.v2.md"
            source.write_text(self.LEGACY, encoding="utf-8")
            output.write_text("do not overwrite\n", encoding="utf-8")

            with self.assertRaises(contracts.ContractError):
                contracts.write_plan_v2_migration(source, source)
            with self.assertRaises(contracts.ContractError):
                contracts.write_plan_v2_migration(source, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "do not overwrite\n")

    def test_cli_requires_explicit_output_and_reports_separate_draft(self) -> None:
        parser = star_forge.build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["migrate-plan"])

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "Plan.md"
            source.write_text(self.LEGACY, encoding="utf-8")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = star_forge.cmd_migrate_plan(
                    argparse.Namespace(
                        project=str(project),
                        file="Plan.md",
                        output="drafts/Plan.v2.md",
                    )
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["schema"], "star-forge.plan-migration.v1")
            self.assertTrue((project / "drafts" / "Plan.v2.md").exists())
            self.assertEqual(source.read_text(encoding="utf-8"), self.LEGACY)


class PlanV2TraceabilityTests(unittest.TestCase):
    BLUEPRINT = """# Blueprint

Status: draft

## Toolchain

- Project class: command-line tool
- Target platforms: Linux and macOS terminals

## Delivery Contract

- Delivery target: source-only
- Platform-specific target, when selected:
- GitHub requested: no

## Acceptance Criteria

- AC-1: The command produces output.
- AC-2: The source handoff is verified.
"""

    def plan(self, tasks: list[dict[str, str]]) -> list[dict[str, object]]:
        return contracts.parse_plan_tasks_text(
            contracts.serialize_plan_tasks(tasks)
        )

    def messages(
        self,
        blueprint: str,
        tasks: list[dict[str, str]],
    ) -> list[str]:
        return [
            item["message"]
            for item in contracts.validate_plan_v2_contract(
                blueprint,
                self.plan(tasks),
            )
        ]

    def test_closed_proof_vocabulary_and_complete_traceability_pass(self) -> None:
        self.assertEqual(
            contracts.PLAN_PROOF_KINDS,
            {
                "unit",
                "integration",
                "browser",
                "preview",
                "native-ios",
                "native-macos",
                "security",
                "github",
                "package",
                "delivery",
            },
        )
        tasks = [
            {
                "id": "SF-1",
                "description": "Build the command",
                "status": "ready",
                "mode": "delegate",
                "files": "src/app.py",
                "depends": "-",
                "acs": "AC-1",
                "proof": "unit",
                "verify": "python3 -m unittest",
                "evidence": "-",
            },
            {
                "id": "SF-2",
                "description": "Deliver the source handoff",
                "status": "queued",
                "mode": "docs",
                "files": "HANDOFF.md",
                "depends": "SF-1",
                "acs": "AC-2",
                "proof": "delivery",
                "verify": "python3 tests/check_handoff.py",
                "evidence": "-",
            },
        ]

        self.assertEqual(
            contracts.validate_plan_v2_contract(
                self.BLUEPRINT,
                self.plan(tasks),
            ),
            [],
        )

    def test_rejects_unknown_uncovered_and_missing_task_contracts(self) -> None:
        messages = self.messages(
            self.BLUEPRINT,
            [
                {
                    "id": "SF-1",
                    "description": "Claim a ghost criterion",
                    "status": "queued",
                    "mode": "delegate",
                    "files": "src/app.py",
                    "depends": "-",
                    "acs": "AC-99",
                    "proof": "unit",
                    "verify": "python3 -m unittest",
                    "evidence": "-",
                }
            ],
        )

        self.assertTrue(any("unknown Blueprint criterion `AC-99`" in msg for msg in messages))
        self.assertTrue(any("criterion `AC-1` is not covered" in msg for msg in messages))
        self.assertTrue(any("criterion `AC-2` is not covered" in msg for msg in messages))
        self.assertTrue(any("no substantive delivery task" in msg for msg in messages))

    def test_rejects_unknown_or_missing_proof_kinds(self) -> None:
        messages = self.messages(
            self.BLUEPRINT,
            [
                {
                    "id": "SF-1",
                    "description": "Build and hand off",
                    "status": "queued",
                    "mode": "delegate",
                    "files": "src/app.py",
                    "depends": "-",
                    "acs": "AC-1, AC-2",
                    "proof": "smoke, delivery",
                    "verify": "python3 -m unittest",
                    "evidence": "-",
                },
                {
                    "id": "SF-2",
                    "description": "No proof",
                    "status": "queued",
                    "mode": "delegate",
                    "files": "src/more.py",
                    "depends": "SF-1",
                    "acs": "AC-1",
                    "proof": "",
                    "verify": "python3 -m unittest",
                    "evidence": "-",
                },
            ],
        )

        self.assertTrue(any("unknown Proof kind `smoke`" in msg for msg in messages))
        self.assertTrue(any("must name at least one Proof kind" in msg for msg in messages))

    def test_maintenance_exemption_is_narrow_and_never_covers_an_ac(self) -> None:
        valid_maintenance = {
            "id": "SF-M",
            "description": "Record the decision log",
            "status": "queued",
            "mode": "docs",
            "files": "docs/decision.md",
            "depends": "-",
            "acs": contracts.PLAN_MAINTENANCE_EXEMPTION,
            "proof": "",
            "verify": "noop",
            "evidence": "-",
        }
        substantive = {
            "id": "SF-1",
            "description": "Build and deliver",
            "status": "queued",
            "mode": "delegate",
            "files": "src/app.py",
            "depends": "SF-M",
            "acs": "AC-1, AC-2",
            "proof": "unit, delivery",
            "verify": "python3 -m unittest",
            "evidence": "-",
        }
        self.assertEqual(
            self.messages(self.BLUEPRINT, [valid_maintenance, substantive]),
            [],
        )

        fake = dict(valid_maintenance)
        fake.update(
            {
                "mode": "delegate",
                "files": "scripts/rewrite.py",
                "acs": "maintenance, AC-2",
                "proof": "delivery",
            }
        )
        messages = self.messages(
            self.BLUEPRINT,
            [fake, {**substantive, "acs": "AC-1", "proof": "unit"}],
        )
        self.assertTrue(any("only value in ACs" in msg for msg in messages))
        self.assertTrue(any("limited to docs-mode" in msg for msg in messages))
        self.assertTrue(any("cannot own code or" in msg for msg in messages))
        self.assertTrue(any("criterion `AC-2` is not covered" in msg for msg in messages))
        self.assertTrue(any("no substantive delivery task" in msg for msg in messages))

    def test_delivery_target_and_proof_must_agree(self) -> None:
        preview_blueprint = self.BLUEPRINT.replace(
            "Delivery target: source-only",
            "Delivery target: preview",
        )
        task = {
            "id": "SF-1",
            "description": "Build and deliver",
            "status": "queued",
            "mode": "delegate",
            "files": "src/app.py",
            "depends": "-",
            "acs": "AC-1, AC-2",
            "proof": "unit, delivery",
            "verify": "python3 -m unittest",
            "evidence": "-",
        }
        messages = self.messages(preview_blueprint, [task])
        self.assertTrue(any("requires Proof kind `preview`" in msg for msg in messages))

        messages = self.messages(
            self.BLUEPRINT,
            [{**task, "proof": "unit, preview, delivery"}],
        )
        self.assertTrue(any("contradicts delivery target `source-only`" in msg for msg in messages))

    def test_cli_labels_legacy_readability_and_strict_v2_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "Blueprint.md").write_text(self.BLUEPRINT, encoding="utf-8")
            legacy = project / "Plan.md"
            legacy.write_text(PlanV2ContractTests.LEGACY, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = star_forge.cmd_validate_plan(
                    argparse.Namespace(
                        project=str(project),
                        file="Plan.md",
                        strict=True,
                    )
                )
            payload = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["plan_version"], "legacy")
            self.assertEqual(payload["traceability"], "legacy-readable")

            migrated, _ = contracts.migrate_plan_text(legacy.read_text())
            legacy.write_text(migrated, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = star_forge.cmd_validate_plan(
                    argparse.Namespace(
                        project=str(project),
                        file="Plan.md",
                        strict=True,
                    )
                )
            payload = json.loads(stdout.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(payload["plan_version"], "v2")
            self.assertEqual(payload["traceability"], "strict-v2")
            self.assertTrue(
                any(
                    item.get("rule") == "plan-v2-review-required"
                    for item in payload["problems"]
                )
            )


if __name__ == "__main__":
    unittest.main()
