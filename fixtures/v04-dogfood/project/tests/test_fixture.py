from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from starforge.dogfood_status import format_status


class DogfoodStatusTests(unittest.TestCase):
    def test_formats_stable_gate_summary(self) -> None:
        digest = "a" * 64
        self.assertEqual(
            format_status("Review", "Source-Only", digest),
            "phase=review; route=source-only; source=aaaaaaaaaaaa",
        )

    def test_rejects_invalid_source_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            format_status("review", "source-only", "stale")


if __name__ == "__main__":
    unittest.main()
