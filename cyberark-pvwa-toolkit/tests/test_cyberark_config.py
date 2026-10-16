from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cyberark_cli import build_parser
from cyberark_config import connection_defaults, load_config
from cyberark_utils import ConfigurationError


class ConfigTests(unittest.TestCase):
    @staticmethod
    def write_config(directory: str, content: str) -> Path:
        path = Path(directory) / "config.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_loads_named_profiles_and_resolves_relative_ca_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                """
default_profile = "production"

[connections.production]
url = "https://production.example/PasswordVault"
auth_type = "LDAP"
ca_bundle = "certs/ca.pem"

[connections.development]
url = "https://development.example/PasswordVault"
""",
            )
            config = load_config(path)
            name, connection = connection_defaults(config)
            self.assertEqual(name, "production")
            self.assertEqual(
                connection["ca_bundle"],
                str((Path(directory) / "certs" / "ca.pem").resolve()),
            )
            name, connection = connection_defaults(config, "development")
            self.assertEqual(name, "development")
            self.assertEqual(
                connection["url"], "https://development.example/PasswordVault"
            )

    def test_rejects_removed_config_sections(self):
        for section in ("connection", "selection", "commands.list"):
            with self.subTest(section=section), tempfile.TemporaryDirectory() as directory:
                path = self.write_config(directory, f"[{section}]\nurl = 'unused'\n")
                with self.assertRaisesRegex(ConfigurationError, "Unknown top-level"):
                    load_config(path)

    def test_rejects_plaintext_password_options(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                """
[connections.production]
url = "https://production.example/PasswordVault"
password = "must-not-be-stored"
""",
            )
            with self.assertRaisesRegex(ConfigurationError, "Unknown option"):
                load_config(path)

    def test_rejects_conflicting_tls_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                """
[connections.production]
insecure = true
ca_bundle = "ca.pem"
""",
            )
            with self.assertRaisesRegex(ConfigurationError, "mutually exclusive"):
                load_config(path)

    def test_rejects_unknown_profile(self):
        with self.assertRaisesRegex(ConfigurationError, "Unknown connection profile"):
            connection_defaults({"connections": {}}, "missing")

    @patch.dict(
        "os.environ",
        {"CYBERARK_PVWA_URL": "https://environment.example/PasswordVault"},
        clear=True,
    )
    def test_environment_overrides_profile_and_cli_overrides_environment(self):
        config = {
            "default_profile": "production",
            "connections": {
                "production": {"url": "https://config.example/PasswordVault"}
            },
        }
        parser = build_parser(config)
        from_environment = parser.parse_args(["accounts", "-H", "host01", "list"])
        self.assertEqual(from_environment.url, "https://environment.example/PasswordVault")

        from_cli = parser.parse_args(
            [
                "--url",
                "https://cli.example/PasswordVault",
                "accounts",
                "-H",
                "host01",
                "list",
            ]
        )
        self.assertEqual(from_cli.url, "https://cli.example/PasswordVault")

    @patch.dict("os.environ", {}, clear=True)
    def test_cli_tls_option_overrides_profile_tls_mode(self):
        insecure_config = {
            "default_profile": "production",
            "connections": {"production": {"insecure": True}},
        }
        args = build_parser(insecure_config).parse_args(
            ["--ca-bundle", "cli-ca.pem", "accounts", "-H", "host01", "list"]
        )
        self.assertFalse(args.insecure)
        self.assertEqual(args.ca_bundle, "cli-ca.pem")

        ca_config = {
            "default_profile": "production",
            "connections": {"production": {"ca_bundle": "configured-ca.pem"}},
        }
        args = build_parser(ca_config).parse_args(
            ["--insecure", "accounts", "-H", "host01", "list"]
        )
        self.assertTrue(args.insecure)
        self.assertIsNone(args.ca_bundle)


if __name__ == "__main__":
    unittest.main()
