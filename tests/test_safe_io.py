from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
STAR_FORGE = SCRIPTS / "star_forge.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from live_collectors import common
from starforge import safe_io


def run_init(project: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(STAR_FORGE),
            "init",
            "--project",
            str(project),
            *extra,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


class InitializationBoundaryTests(unittest.TestCase):
    def test_init_rejects_symlinked_root_files_without_external_writes(self) -> None:
        cases = (
            ("Blueprint.md", ("--no-agents", "--force")),
            (".gitignore", ("--no-agents",)),
        )
        for relative, arguments in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                project = root / "project"
                project.mkdir()
                outside = root / "outside.txt"
                outside.write_text("sentinel\n", encoding="utf-8")
                (project / relative).symlink_to(outside)
                result = run_init(project, *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel\n")

    def test_init_rejects_symlinked_agent_directories_without_external_writes(self) -> None:
        for relative in (".codex", ".codex/agents"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                project = root / "project"
                outside = root / "outside"
                project.mkdir()
                outside.mkdir()
                link = project / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside, target_is_directory=True)
                result = run_init(project, "--adopt-root")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(outside.iterdir()), [])


class CollectorBoundaryTests(unittest.TestCase):
    def test_live_output_rejects_symlinked_directory_and_file_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = root / "project"
            outside = root / "outside"
            project.mkdir()
            outside.mkdir()
            (project / ".starforge").mkdir()
            live = project / ".starforge" / "live"
            live.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                common.live_collector_dir(project, "SF-1", "security")
            self.assertEqual(list(outside.iterdir()), [])

            live.unlink()
            collector = common.live_collector_dir(project, "SF-1", "security")
            sentinel = root / "sentinel.txt"
            sentinel.write_text("sentinel\n", encoding="utf-8")
            (collector / "manifest.json").symlink_to(sentinel)
            with self.assertRaises(OSError):
                common.write_text(collector / "manifest.json", "replacement\n")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "sentinel\n")


class DescriptorStabilityTests(unittest.TestCase):
    def test_snapshot_hash_and_size_use_the_opened_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            artifact = project / "artifact.bin"
            original = b"original artifact bytes"
            replacement = b"replacement"
            artifact.write_bytes(original)
            original_read = safe_io.os.read
            replaced = False

            def replace_after_open(descriptor: int, size: int) -> bytes:
                nonlocal replaced
                if not replaced:
                    replaced = True
                    moved = project / "opened.bin"
                    artifact.rename(moved)
                    artifact.write_bytes(replacement)
                return original_read(descriptor, size)

            with mock.patch.object(safe_io.os, "read", side_effect=replace_after_open):
                prefix, digest, byte_count = safe_io.snapshot(
                    project,
                    artifact,
                    prefix_limit=8,
                )
            self.assertEqual(prefix, original[:8])
            self.assertEqual(digest, hashlib.sha256(original).hexdigest())
            self.assertEqual(byte_count, len(original))
            self.assertEqual(artifact.read_bytes(), replacement)

    def test_atomic_write_replaces_a_raced_symlink_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            target = project / "record.json"
            outside = project.parent / "outside.json"
            target.write_text("old\n", encoding="utf-8")
            outside.write_text("sentinel\n", encoding="utf-8")
            original_replace = safe_io.os.replace
            raced = False

            def race_before_replace(
                source: str,
                destination: str,
                *,
                src_dir_fd: int | None = None,
                dst_dir_fd: int | None = None,
            ) -> None:
                nonlocal raced
                if not raced:
                    raced = True
                    target.unlink()
                    target.symlink_to(outside)
                original_replace(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with mock.patch.object(safe_io.os, "replace", side_effect=race_before_replace):
                safe_io.atomic_write_text(project, target, "new\n")
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
