from __future__ import annotations

import argparse
import unittest
from unittest.mock import patch

from cyberark_cli import build_parser, platforms_from_args
from cyberark_utils import ConfigurationError


class CliSafetyTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def parse_update(self, *extra: str) -> argparse.Namespace:
        return self.parser.parse_args(
            [
                "update-password",
                "--hostname",
                "host01.example.com",
                "--account-user",
                "reconcile_svc",
                *extra,
            ]
        )

    def test_mutation_is_preview_by_default(self):
        self.assertFalse(self.parse_update("--platform", "UnixSSH").execute)

    @patch.dict("os.environ", {}, clear=True)
    def test_mutation_requires_explicit_platform_scope(self):
        with self.assertRaises(ConfigurationError):
            platforms_from_args(self.parse_update(), mutation=True)

    @patch.dict("os.environ", {"CYBERARK_PLATFORMS": "UnixSSH, UnixKeys"}, clear=True)
    def test_platforms_can_come_from_environment(self):
        self.assertEqual(
            platforms_from_args(self.parse_update(), mutation=True),
            ["UnixSSH", "UnixKeys"],
        )

    @patch.dict("os.environ", {}, clear=True)
    def test_all_platforms_is_an_explicit_escape_hatch(self):
        self.assertEqual(
            platforms_from_args(self.parse_update("--all-platforms"), mutation=True),
            [],
        )


if __name__ == "__main__":
    unittest.main()
