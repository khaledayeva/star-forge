#!/usr/bin/env python3
"""Coverage for v0.4 source classification and architecture quality."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import star_forge
from starforge import quality


def write_lines(path: Path, count: int, line: str = "value = 1") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((line + "\n") * count, encoding="utf-8")
    return path


class SourceClassificationTests(unittest.TestCase):
    def test_common_web_mobile_native_cli_service_and_monorepo_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            paths = [
                project / "index.html",
                project / "src" / "web.tsx",
                project / "app" / "screen.tsx",
                project / "android" / "app" / "src" / "main" / "MainActivity.kt",
                project / "ios" / "Widget" / "Sources" / "Widget.swift",
                project / "cmd" / "server" / "main.go",
                project / "internal" / "store" / "store.go",
                project / "services" / "billing" / "src" / "api.py",
                project / "packages" / "shared" / "lib" / "index.ts",
                project / "apps" / "console" / "src" / "main.ts",
                project / "scripts" / "release.py",
            ]
            for path in paths:
                write_lines(path, 1)

            classifications = {
                item.path: item for item in quality.classify_project(project)
            }

            self.assertEqual(
                {classifications[path.relative_to(project).as_posix()].category for path in paths},
                {"production"},
            )
            self.assertEqual(
                classifications["apps/console/src/main.ts"].source_root,
                "apps/console/src",
            )
            self.assertEqual(
                classifications["ios/Widget/Sources/Widget.swift"].source_root,
                "ios/Widget/Sources",
            )

    def test_production_test_docs_and_config_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            samples = {
                "worker.py": "production",
                "tests/test_worker.py": "test",
                "src/worker.spec.ts": "test",
                "docs/example.py": "docs",
                "README.md": "docs",
                "config/settings.py": "config",
                "pyproject.toml": "config",
            }
            for relative in samples:
                write_lines(project / relative, 1)
            actual = {
                item.path: item.category for item in quality.classify_project(project)
            }
            self.assertEqual(actual, samples)

    def test_repository_declared_source_roots_are_recognized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "StarForge.profile.json").write_text(
                json.dumps(
                    {
                        "quality": {
                            "source_roots": ["product/runtime", "tools/commands"]
                        }
                    }
                ),
                encoding="utf-8",
            )
            runtime = write_lines(project / "product" / "runtime" / "engine.py", 1)
            command = write_lines(project / "tools" / "commands" / "ship.py", 1)

            roots = quality.discover_source_roots(project)
            self.assertIn("product/runtime", roots)
            self.assertIn("tools/commands", roots)
            self.assertEqual(
                quality.classify_source_path(runtime, project).source_root,
                "product/runtime",
            )
            self.assertEqual(
                quality.classify_source_path(command, project).source_root,
                "tools/commands",
            )

    def test_generated_vendor_build_and_cache_exclusions_are_precise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            excluded = [
                project / "node_modules" / "pkg" / "index.js",
                project / "vendor" / "library.go",
                project / "dist" / "bundle.js",
                project / "apps" / "web" / "build" / "bundle.js",
                project / "src" / "generated" / "client.ts",
                project / "src" / "client.generated.ts",
                project / "__pycache__" / "module.py",
            ]
            included = [
                project / "src" / "build" / "pipeline.py",
                project / "src" / "vendor_tools" / "sync.py",
                project / "builder" / "dist" / "model.py",
            ]
            for path in [*excluded, *included]:
                write_lines(path, 1)

            scanned = {
                path.relative_to(project).as_posix()
                for path in quality.iter_project_files(project)
            }
            for path in excluded:
                self.assertNotIn(path.relative_to(project).as_posix(), scanned)
                self.assertTrue(quality.is_generated_or_vendored(path, project))
            for path in included:
                self.assertIn(path.relative_to(project).as_posix(), scanned)
                self.assertFalse(quality.is_generated_or_vendored(path, project))

    def test_generated_header_excludes_code_without_broad_name_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            generated = project / "src" / "api_client.py"
            generated.parent.mkdir(parents=True)
            generated.write_text(
                "# Code generated by schema compiler. DO NOT EDIT.\nvalue = 1\n",
                encoding="utf-8",
            )
            handwritten = write_lines(project / "src" / "generator_helpers.py", 1)
            self.assertEqual(
                quality.exclusion_reason(generated, project),
                "generated-file header",
            )
            self.assertIsNone(quality.exclusion_reason(handwritten, project))

    def test_absolute_relative_and_component_symlinks_are_rejected_without_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            source = project / "src"
            source.mkdir(parents=True)
            external = root / "external.py"
            external.write_text(
                "# Code generated by secret compiler. DO NOT EDIT.\n",
                encoding="utf-8",
            )
            absolute = source / "absolute.py"
            absolute.symlink_to(external.resolve())
            relative = source / "relative.py"
            relative.symlink_to(Path("../../external.py"))
            outside = root / "outside"
            outside.mkdir()
            (outside / "component.py").write_text("secret = 1\n", encoding="utf-8")
            component = source / "component"
            component.symlink_to(outside, target_is_directory=True)

            with mock.patch(
                "starforge.safe_io.os.read", side_effect=AssertionError("target read")
            ):
                for candidate in (absolute, relative, component / "component.py"):
                    classification = quality.classify_source_path(candidate, project)
                    self.assertEqual(classification.category, "excluded")
                    self.assertEqual(classification.reason, "unsafe path")
                self.assertEqual(list(quality.iter_project_files(project)), [])


class ArchitectureDebtTests(unittest.TestCase):
    def test_module_and_cli_budgets_are_reported_with_exact_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_lines(
                project / "scripts" / "star_forge.py",
                quality.MAX_CLI_MODULE_LINES + 1,
            )
            findings = quality.architecture_debt_findings(
                quality.iter_project_files(project), project
            )
            by_rule = {item["rule"]: item for item in findings}
            self.assertIn("architecture-debt-large-file", by_rule)
            self.assertIn("architecture-debt-cli-concentration", by_rule)
            self.assertIn(
                str(quality.MAX_CLI_MODULE_LINES),
                by_rule["architecture-debt-cli-concentration"]["evidence"],
            )

    def test_python_budget_violation_counts_production_not_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_lines(project / "src" / "runtime.py", 6)
            write_lines(project / "tests" / "test_runtime.py", 100)
            original = quality.MAX_PRODUCTION_PYTHON_LINES
            quality.MAX_PRODUCTION_PYTHON_LINES = 5
            try:
                findings = quality.architecture_debt_findings(
                    quality.iter_project_files(project), project
                )
            finally:
                quality.MAX_PRODUCTION_PYTHON_LINES = original
            budget = next(
                item
                for item in findings
                if item["rule"] == "architecture-debt-python-budget"
            )
            self.assertIn("6 production Python lines", budget["evidence"])

    def test_python_budget_counts_only_authoritative_discovered_roots(self) -> None:
        original = quality.MAX_PRODUCTION_PYTHON_LINES
        quality.MAX_PRODUCTION_PYTHON_LINES = 5
        try:
            with tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                write_lines(project / "scripts" / "runtime.py", 5)
                fixture = write_lines(
                    project / "fixtures" / "sample-product" / "app.py",
                    quality.MAX_RUNTIME_MODULE_LINES + 1,
                )
                self.assertEqual(quality.discover_source_roots(project), ("scripts",))
                fixture_classification = quality.classify_source_path(fixture, project)
                self.assertEqual(fixture_classification.category, "production")
                self.assertEqual(fixture_classification.source_root, ".")
                findings = quality.architecture_debt_findings(
                    quality.iter_project_files(project), project
                )
                self.assertFalse(
                    any(item["rule"] == "architecture-debt-python-budget" for item in findings)
                )
                self.assertTrue(
                    any(
                        item["rule"] == "architecture-debt-large-file"
                        and item["file"] == "fixtures/sample-product/app.py"
                        for item in findings
                    )
                )

            with tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                write_lines(project / "go.mod", 1, "module example.test/root")
                write_lines(project / "runtime.py", 6)
                self.assertEqual(quality.discover_source_roots(project), (".",))
                findings = quality.architecture_debt_findings(
                    quality.iter_project_files(project), project
                )
                budget = next(
                    item
                    for item in findings
                    if item["rule"] == "architecture-debt-python-budget"
                )
                self.assertIn("6 production Python lines", budget["evidence"])
        finally:
            quality.MAX_PRODUCTION_PYTHON_LINES = original

    def test_packed_lines_and_statements_cannot_fake_budget_headroom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "src" / "packed.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                "payload = " + repr("x" * quality.MAX_SOURCE_LINE_LENGTH) + "\n"
                "left = 1; right = 2\n"
                "literal = '; is data, not a statement separator'\n"
                "if True: value = 1\n"
                "for item in (): value = item\n"
                "class Packed: pass\n",
                encoding="utf-8",
            )

            findings = quality.architecture_debt_findings(
                quality.iter_project_files(project), project
            )
            packed = [
                item
                for item in findings
                if item["rule"].startswith("architecture-debt-packed-")
            ]
            self.assertEqual(
                [(item["rule"], item["line"]) for item in packed],
                [
                    ("architecture-debt-packed-line", 1),
                    ("architecture-debt-packed-statements", 2),
                    ("architecture-debt-packed-statements", 4),
                    ("architecture-debt-packed-statements", 5),
                    ("architecture-debt-packed-statements", 6),
                ],
            )
            self.assertTrue(all(item["severity"] == "medium" for item in packed))

    def test_cycles_coupling_and_duplicate_control_plane_responsibilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "src").mkdir()
            (project / "src" / "a.py").write_text(
                "import src.b\n\ndef load_state():\n    return 1\n",
                encoding="utf-8",
            )
            (project / "src" / "b.py").write_text(
                "import src.a\n\ndef load_state():\n    return 2\n",
                encoding="utf-8",
            )
            (project / "src" / "a.ts").write_text(
                'import { b } from "./b";\nexport const a = b;\n',
                encoding="utf-8",
            )
            (project / "src" / "b.ts").write_text(
                'import { a } from "./a";\nexport const b = a;\n',
                encoding="utf-8",
            )
            findings = quality.architecture_debt_findings(
                quality.iter_project_files(project), project
            )
            rules = {item["rule"] for item in findings}
            self.assertIn("architecture-debt-import-cycle", rules)
            self.assertIn("architecture-debt-duplicated-responsibility", rules)
            self.assertEqual(
                sum(
                    item["rule"] == "architecture-debt-import-cycle"
                    for item in findings
                ),
                2,
            )

    def test_ts_ignore_only_applies_to_typescript_and_javascript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_lines(project / "src" / "matcher.py", 1, 'TOKEN = "@ts-ignore"')
            write_lines(project / "src" / "unsafe.ts", 1, "// @ts-ignore")
            findings = quality.architecture_debt_findings(
                quality.iter_project_files(project), project
            )
            ts_files = [
                item["file"]
                for item in findings
                if item["rule"] == "architecture-debt-ts-ignore"
            ]
            self.assertEqual(ts_files, ["src/unsafe.ts"])

    def test_report_is_deterministic_and_explains_excluded_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_lines(project / "src" / "app.py", 2)
            write_lines(project / "dist" / "app.py", 2)
            first = quality.quality_report(project, include_files=True)
            second = quality.quality_report(project, include_files=True)
            self.assertEqual(first, second)
            self.assertEqual(first["schema"], quality.QUALITY_REPORT_SCHEMA)
            self.assertEqual(
                first["excluded_artifacts"],
                [{"path": "dist", "reason": "distribution output"}],
            )
            self.assertEqual(first["classification_counts"]["production"], 1)

    def test_star_forge_dogfood_meets_cli_runtime_and_total_budgets(self) -> None:
        report = quality.quality_report(ROOT)
        blocking_budget_rules = {
            "architecture-debt-cli-concentration",
            "architecture-debt-large-file",
            "architecture-debt-packed-line",
            "architecture-debt-packed-statements",
            "architecture-debt-python-budget",
        }
        violations = [
            item
            for item in report["findings"]
            if item["severity"] in {"critical", "high", "medium"}
            and item["rule"] in blocking_budget_rules
        ]
        self.assertEqual(violations, [])
        self.assertLess(
            len((ROOT / "scripts" / "star_forge.py").read_text().splitlines()),
            quality.MAX_CLI_MODULE_LINES,
        )


class QualityCliTests(unittest.TestCase):
    def test_cli_is_thin_json_adapter_with_strict_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_lines(
                project / "scripts" / "star_forge.py",
                quality.MAX_CLI_MODULE_LINES + 1,
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = star_forge.main(
                    ["quality", "--project", str(project), "--strict"]
                )
            payload = json.loads(stdout.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(payload["verdict"], "REQUEST_CHANGES")
            self.assertGreater(payload["blocking_findings"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
