from __future__ import annotations

import argparse
import unittest
from unittest.mock import patch

from cyberark_cli import (
    build_parser,
    platforms_from_args,
    read_secret_from_env_or_prompt,
    read_username_or_prompt,
)
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

    def test_original_target_and_filter_options_are_supported(self):
        args = self.parser.parse_args(
            [
                "list",
                "-H",
                "host01.example.com",
                "-f",
                "hosts.txt",
                "-F",
                "user",
                "-m",
                "^svc_",
            ]
        )
        self.assertEqual(args.hostname, "host01.example.com")
        self.assertEqual(args.file, "hosts.txt")
        self.assertEqual(args.field, ["user"])
        self.assertEqual(args.match, ["^svc_"])

    def test_fields_and_matches_are_repeatable(self):
        args = self.parser.parse_args(
            [
                "list",
                "--hostname",
                "host01.example.com",
                "--field",
                "user",
                "--match",
                "^svc_",
                "--field",
                "safe",
                "--match",
                "^Linux-Prod$",
            ]
        )
        self.assertEqual(args.field, ["user", "safe"])
        self.assertEqual(args.match, ["^svc_", "^Linux-Prod$"])

    def test_password_update_retains_safe_as_default_filter_field(self):
        args = self.parse_update("--match", "^Linux-Prod$")
        self.assertEqual(args.field, ["safe"])

    @patch("builtins.input")
    def test_configured_username_does_not_prompt(self, prompt):
        self.assertEqual(read_username_or_prompt("operator"), "operator")
        prompt.assert_not_called()

    @patch("builtins.input", return_value="  prompted-operator  ")
    def test_missing_username_prompts(self, prompt):
        self.assertEqual(read_username_or_prompt(None), "prompted-operator")
        prompt.assert_called_once_with("PVWA username: ")

    @patch("builtins.input", return_value="")
    def test_empty_prompted_username_is_rejected(self, _prompt):
        with self.assertRaisesRegex(ConfigurationError, "must not be empty"):
            read_username_or_prompt(None)

    @patch.dict(
        "os.environ",
        {"CYBERARK_LOGIN_PASSWORD": "secret-value"},
        clear=True,
    )
    def test_conventional_password_environment_variable_is_detected(self):
        args = build_parser().parse_args(["list", "--hostname", "host01"])
        self.assertEqual(args.login_password_env, "CYBERARK_LOGIN_PASSWORD")

    @patch.dict("os.environ", {"LOGIN_SECRET": "from-environment"}, clear=True)
    @patch("getpass.getpass")
    def test_password_comes_from_environment_without_prompt(self, prompt):
        self.assertEqual(
            read_secret_from_env_or_prompt("LOGIN_SECRET", "PVWA password: "),
            "from-environment",
        )
        prompt.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    @patch("getpass.getpass", return_value="prompted-secret")
    def test_missing_password_environment_variable_falls_back_to_prompt(self, prompt):
        self.assertEqual(
            read_secret_from_env_or_prompt("LOGIN_SECRET", "PVWA password: "),
            "prompted-secret",
        )
        prompt.assert_called_once_with("PVWA password: ")


if __name__ == "__main__":
    unittest.main()
