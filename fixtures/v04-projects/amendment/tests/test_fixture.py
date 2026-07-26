import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path("src").resolve()))
from calculator import add


class AmendmentFixtureTest(unittest.TestCase):
    def test_addition(self):
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
