import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path("src").resolve()))
from session_api import authorize


class FastMVPFixtureTest(unittest.TestCase):
    def test_owner_check_fails_closed(self):
        self.assertTrue(authorize("user-1", "user-1"))
        self.assertFalse(authorize("", "user-1"))
        self.assertFalse(authorize("user-2", "user-1"))


if __name__ == "__main__":
    unittest.main()
