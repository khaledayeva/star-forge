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
            self.assertIn("AMEND-1", (project / "Plan.md").read_text())

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


if __name__ == "__main__":
    unittest.main()
