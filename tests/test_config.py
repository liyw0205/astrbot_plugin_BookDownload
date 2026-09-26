import json
import unittest
from pathlib import Path


class ConfigDefaultsTests(unittest.TestCase):
    def test_tag_filter_and_daily_tag_regex_defaults_are_empty(self):
        schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))

        self.assertTrue(schema["tag_filter_enabled"]["default"])
        self.assertEqual(schema["tag_filter_regex"]["default"], "")
        self.assertEqual(schema["daily_push_tag_regex"]["default"], "")
        self.assertNotIn("daily_push_target", schema)


if __name__ == "__main__":
    unittest.main()
