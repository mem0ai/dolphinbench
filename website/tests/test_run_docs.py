"""The checked-in website bundle must match its runner source files."""

import json
import unittest

from scripts.build_run_docs import OUTPUT, build


class RunDocsTests(unittest.TestCase):
    def test_bundle_matches_sources(self):
        self.assertEqual(json.loads(OUTPUT.read_text()), build())


if __name__ == "__main__":
    unittest.main()
