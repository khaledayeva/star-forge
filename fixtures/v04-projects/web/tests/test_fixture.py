import unittest
from pathlib import Path


class WebFixtureTest(unittest.TestCase):
    def test_accessible_dashboard_shell_exists(self):
        page = Path("web/index.html").read_text(encoding="utf-8")
        self.assertIn('lang="en"', page)
        self.assertIn("<main>", page)
        self.assertIn("<button", page)


if __name__ == "__main__":
    unittest.main()
