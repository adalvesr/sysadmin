from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cyberark_playbook import load_playbook
from cyberark_utils import ConfigurationError


class PlaybookTests(unittest.TestCase):
    def test_loads_playbook_and_resolves_relative_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hosts.txt").write_text("host01\nhost02\n", encoding="utf-8")
            playbook = root / "accounts.yml"
            playbook.write_text(
                """
version: 1
connection: production
required-selectors: [addresses]
tasks:
  - name: Configure SSH keys
    target: accounts
    select:
      addresses:
        files: [hosts.txt]
      discovery-platform-types: [Unix SSH Key]
      where:
        - field: user
          regex: "^(root|oracle)$"
      where-any:
        - field: safe
          regex: "^Linux-"
        - field: address
          regex: "^host"
    actions:
      - change-platform:
          platform: UnixSSHKeys
      - reconcile: {}
""",
                encoding="utf-8",
            )
            result = load_playbook(playbook)
            self.assertEqual(result.connection, "production")
            self.assertEqual(result.required_selectors, ["addresses"])
            self.assertEqual(result.tasks[0].selector.addresses, ["host01", "host02"])
            self.assertEqual(
                result.tasks[0].selector.activity_equals,
                {"DiscoveryPlatformType": ["Unix SSH Key"]},
            )
            self.assertEqual(
                [
                    (rule.field, rule.pattern)
                    for rule in result.tasks[0].selector.matches_all
                ],
                [("user", "^(root|oracle)$")],
            )
            self.assertEqual(
                [
                    (rule.field, rule.pattern)
                    for rule in result.tasks[0].selector.match_any_groups[0]
                ],
                [("safe", "^Linux-"), ("address", "^host")],
            )
            self.assertEqual(
                [action.name for action in result.tasks[0].actions],
                ["change_platform", "reconcile"],
            )

    def test_rejects_unknown_playbook_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.yml"
            path.write_text("version: 1\nunknown: true\ntasks: []\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "unknown"):
                load_playbook(path)

    def test_loads_same_address_link_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "link.yml"
            path.write_text(
                """
version: 1
tasks:
  - name: Link reconciliation accounts
    select:
      addresses: [host01]
    actions:
      - link-account:
          role: reconcile
          source:
            account: svc_reconcile
            same-address: true
""",
                encoding="utf-8",
            )

            action = load_playbook(path).tasks[0].actions[0]
            self.assertEqual(action.name, "link_account")
            self.assertEqual(
                action.options["source"],
                {"account": "svc_reconcile", "same_address": True},
            )

    def test_rejects_underscore_action_and_selector_names(self):
        documents = [
            """
version: 1
required_selectors: [addresses]
tasks: []
""",
            """
version: 1
tasks:
  - select:
      all_accounts: true
    actions:
      - set_password:
          mode: random-distinct
""",
        ]
        for content in documents:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "underscores.yml"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    load_playbook(path)


if __name__ == "__main__":
    unittest.main()
