from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

from cyberark_cli import build_parser, read_secret_from_env_or_prompt, read_username_or_prompt
from cyberark_resources import selector_from_args
from cyberark_utils import ConfigurationError


class CliSafetyTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_mutation_is_preview_by_default(self):
        args = self.parser.parse_args(
            ["accounts", "-H", "host01", "reconcile"]
        )
        self.assertFalse(args.execute)

    def test_where_selectors_are_repeatable(self):
        args = self.parser.parse_args(
            [
                "accounts",
                "-H",
                "host01",
                "--where",
                "user",
                "^svc_",
                "--where",
                "safe",
                "^Linux-Prod$",
                "list",
            ]
        )
        self.assertEqual(args.selector_hostnames, ["host01"])
        selector = selector_from_args(args)
        self.assertEqual(
            [(rule.field, rule.pattern) for rule in selector.matches_all],
            [("user", "^svc_"), ("safe", "^Linux-Prod$")],
        )

    def test_field_and_match_options_are_not_accepted(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(
                    ["accounts", "-H", "host01", "-F", "user", "list"]
                )

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
        args = build_parser().parse_args(["accounts", "-H", "host01", "list"])
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
