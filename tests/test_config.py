import json
import unittest
from pathlib import Path


class ConfigDefaultsTests(unittest.TestCase):
    def test_tag_filter_defaults_are_enabled_and_exclude_legacy_daily_tag(self):
        schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))

        self.assertTrue(schema["tag_filter_enabled"]["default"])
        self.assertEqual(
            schema["tag_filter_regex"]["default"],
            "yaoi|tomgirl|futanari|guro|scat|vore|bestiality",
        )
        self.assertNotIn("masturbation", schema["daily_push_tag_regex"]["default"].lower())


if __name__ == "__main__":
    unittest.main()
