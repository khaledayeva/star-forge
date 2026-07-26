import unittest
from pathlib import Path


class IOSFixtureTest(unittest.TestCase):
    def test_swiftui_entry_point_exists(self):
        source = Path("Sources/FixtureApp.swift").read_text(encoding="utf-8")
        self.assertIn("import SwiftUI", source)
        self.assertIn("WindowGroup", source)


if __name__ == "__main__":
    unittest.main()
