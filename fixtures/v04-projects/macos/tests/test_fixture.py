import unittest
from pathlib import Path


class MacOSFixtureTest(unittest.TestCase):
    def test_desktop_entry_point_exists(self):
        source = Path("Sources/FixtureMacApp.swift").read_text(encoding="utf-8")
        self.assertIn("import SwiftUI", source)
        self.assertIn("Archive ready", source)


if __name__ == "__main__":
    unittest.main()
