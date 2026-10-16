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
required_selectors: [addresses]
tasks:
  - name: Configure SSH keys
    target: accounts
    select:
      addresses:
        files: [hosts.txt]
      discovery_platform_types: [Unix SSH Key]
    actions:
      - change_platform:
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
                [action.name for action in result.tasks[0].actions],
                ["change_platform", "reconcile"],
            )

    def test_rejects_unknown_playbook_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.yml"
            path.write_text("version: 1\nunknown: true\ntasks: []\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "unknown"):
                load_playbook(path)


if __name__ == "__main__":
    unittest.main()
