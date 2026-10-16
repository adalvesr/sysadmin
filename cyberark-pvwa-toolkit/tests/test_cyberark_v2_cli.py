from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cyberark_cli import build_parser, normalize_global_arguments
from cyberark_v2_cli import selector_from_args


class ResourceCliTests(unittest.TestCase):
    def test_selectors_precede_action(self):
        args = build_parser().parse_args(
            [
                "accounts",
                "-H",
                "host01",
                "-f",
                "hosts.txt",
                "--account",
                "root",
                "--dpt",
                "Unix SSH Key",
                "change-platform",
                "--to",
                "UnixSSHKeys",
            ]
        )
        self.assertEqual(args.account_action, "change-platform")
        self.assertEqual(args.target_platform, "UnixSSHKeys")

    def test_connection_options_are_moved_before_resource_parser(self):
        normalized = normalize_global_arguments(
            ["accounts", "-f", "hosts.txt", "list", "-u", "operator"]
        )
        self.assertEqual(normalized[:2], ["-u", "operator"])
        args = build_parser().parse_args(normalized)
        self.assertEqual(args.username, "operator")

    def test_cli_hostnames_and_files_become_one_address_category(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hosts.txt"
            path.write_text("HOST01\nhost02\n", encoding="utf-8")
            args = build_parser().parse_args(
                ["accounts", "-H", "host01", "-f", str(path), "list"]
            )
            self.assertEqual(selector_from_args(args).addresses, ["host01", "host02"])

    def test_password_mode_is_explicit(self):
        args = build_parser().parse_args(
            [
                "accounts",
                "-H",
                "host01",
                "set-password",
                "--mode",
                "random-distinct",
            ]
        )
        self.assertEqual(args.mode, "random-distinct")
        self.assertFalse(args.execute)


if __name__ == "__main__":
    unittest.main()
