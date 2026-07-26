import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path("src").resolve()))
from forge_cli import greeting


class CLIFixtureTest(unittest.TestCase):
    def test_greeting_normalizes_input(self):
        self.assertEqual(greeting(" forge "), "hello forge")


if __name__ == "__main__":
    unittest.main()
