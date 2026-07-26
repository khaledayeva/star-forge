import json
import unittest
from pathlib import Path


class ExpoFixtureTest(unittest.TestCase):
    def test_expo_manifest_is_complete(self):
        manifest = json.loads(Path("app.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["expo"]["slug"], "forge-expo-fixture")
        self.assertEqual(manifest["expo"]["version"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
