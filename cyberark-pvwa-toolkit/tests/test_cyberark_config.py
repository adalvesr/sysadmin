from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cyberark_cli import build_parser, platforms_from_args
from cyberark_config import load_config
from cyberark_utils import ConfigurationError


class ConfigTests(unittest.TestCase):
    def write_config(self, directory: str, content: str) -> Path:
        path = Path(directory) / "cyberark.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_loads_config_and_resolves_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                """
[connection]
url = "https://vault.example/PasswordVault"
ca_bundle = "certs/ca.pem"

[selection]
file = "hosts.txt"
platforms = ["UnixSSH"]
field = ["user", "safe"]
match = ["^svc_", "^Linux-Prod$"]
""",
            )
            config = load_config(path)
            self.assertEqual(config["path"], str(path.resolve()))
            self.assertEqual(
                config["connection"]["ca_bundle"],
                str((Path(directory) / "certs" / "ca.pem").resolve()),
            )
            self.assertEqual(
                config["selection"]["file"],
                str((Path(directory) / "hosts.txt").resolve()),
            )
            self.assertEqual(config["selection"]["field"], ["user", "safe"])
            self.assertEqual(
                config["selection"]["match"], ["^svc_", "^Linux-Prod$"]
            )

    def test_rejects_unknown_and_secret_options(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                """
[connection]
password = "must-not-be-stored"
""",
            )
            with self.assertRaisesRegex(ConfigurationError, "Unknown option"):
                load_config(path)

    def test_rejects_execute_in_command_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                """
[commands.rotate-reconcile]
execute = true
""",
            )
            with self.assertRaisesRegex(ConfigurationError, "execute"):
                load_config(path)

    def test_rejects_conflicting_tls_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                """
[connection]
insecure = true
ca_bundle = "ca.pem"
""",
            )
            with self.assertRaisesRegex(ConfigurationError, "mutually exclusive"):
                load_config(path)

    @patch.dict("os.environ", {}, clear=True)
    def test_command_defaults_satisfy_required_arguments(self):
        config = {
            "selection": {"platforms": ["ConfiguredPlatform"]},
            "commands": {"update-password": {"account_user": "configured_svc"}},
        }
        args = build_parser(config).parse_args(["update-password", "--hostname", "host01"])
        self.assertEqual(args.account_user, "configured_svc")
        self.assertEqual(platforms_from_args(args, mutation=True), ["ConfiguredPlatform"])

    @patch.dict(
        "os.environ",
        {
            "CYBERARK_PVWA_URL": "https://environment.example/PasswordVault",
            "CYBERARK_PLATFORMS": "EnvironmentPlatform",
        },
        clear=True,
    )
    def test_environment_overrides_file_and_cli_overrides_environment(self):
        config = {
            "connection": {"url": "https://config.example/PasswordVault"},
            "selection": {"platforms": ["ConfiguredPlatform"]},
        }
        parser = build_parser(config)
        from_environment = parser.parse_args(["list", "--hostname", "host01"])
        self.assertEqual(from_environment.url, "https://environment.example/PasswordVault")
        self.assertEqual(from_environment.platform, ["EnvironmentPlatform"])

        from_cli = parser.parse_args(
            [
                "--url",
                "https://cli.example/PasswordVault",
                "list",
                "--hostname",
                "host01",
                "--platform",
                "CliPlatform",
            ]
        )
        self.assertEqual(from_cli.url, "https://cli.example/PasswordVault")
        self.assertEqual(from_cli.platform, ["CliPlatform"])

    @patch.dict("os.environ", {}, clear=True)
    def test_cli_tls_option_overrides_configured_tls_mode(self):
        insecure_parser = build_parser({"connection": {"insecure": True}})
        args = insecure_parser.parse_args(
            ["--ca-bundle", "cli-ca.pem", "list", "--hostname", "host01"]
        )
        self.assertFalse(args.insecure)
        self.assertEqual(args.ca_bundle, "cli-ca.pem")

        ca_parser = build_parser({"connection": {"ca_bundle": "configured-ca.pem"}})
        args = ca_parser.parse_args(["--insecure", "list", "--hostname", "host01"])
        self.assertTrue(args.insecure)
        self.assertIsNone(args.ca_bundle)


if __name__ == "__main__":
    unittest.main()
