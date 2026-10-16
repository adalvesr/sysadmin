from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cyberark_selectors import AccountSelector, MatchRule, SelectorSpec, addresses_from_sources


ACCOUNTS = [
    {
        "id": "1",
        "userName": "root",
        "address": "host01",
        "platformId": "UnixSSH",
        "safeName": "Linux-Prod",
    },
    {
        "id": "2",
        "userName": "oracle",
        "address": "host02",
        "platformId": "UnixSSHKeys",
        "safeName": "Linux-Prod",
    },
]


class FakeClient:
    def __init__(self):
        self.activity_calls = 0

    def search_accounts(self, _term):
        return ACCOUNTS

    def get_account(self, account_id, **_kwargs):
        return next(item for item in ACCOUNTS if item["id"] == account_id)

    def get_activities(self, account_id, *, limit):
        self.activity_calls += 1
        value = "Unix SSH Key" if account_id == "1" else "Unix via SSH"
        return [
            {
                "Action": "Add File Category",
                "MoreInfo": "DiscoveryPlatformType",
                "Reason": f"Value=[{value}]",
            }
        ]


class SelectorTests(unittest.TestCase):
    def test_hostname_and_files_form_one_deduplicated_category(self):
        with tempfile.TemporaryDirectory() as directory:
            hosts = Path(directory) / "hosts.txt"
            hosts.write_text("HOST01\nhost02\n", encoding="utf-8")
            self.assertEqual(
                addresses_from_sources(["host01"], [str(hosts)]),
                ["host01", "host02"],
            )

    def test_runtime_selector_supplies_missing_playbook_category(self):
        playbook = SelectorSpec(platforms=["UnixSSH"])
        runtime = SelectorSpec(addresses=["host01"])
        merged = playbook.merged_with(runtime)
        self.assertEqual(merged.addresses, ["host01"])
        self.assertEqual(merged.platforms, ["UnixSSH"])

    def test_same_category_is_intersected(self):
        playbook = SelectorSpec(addresses=["host01", "host02"])
        runtime = SelectorSpec(addresses=["HOST02", "host03"])
        merged = playbook.merged_with(runtime)
        self.assertEqual(merged.addresses, ["HOST02"])
        self.assertFalse(merged.matches_nothing)

    def test_empty_intersection_remains_a_constrained_empty_scope(self):
        merged = SelectorSpec(addresses=["host01"]).merged_with(
            SelectorSpec(addresses=["host02"])
        )
        self.assertTrue(merged.matches_nothing)
        self.assertTrue(merged.has_category("addresses"))
        self.assertTrue(merged.has_mutation_scope())

    def test_account_and_activity_selectors_are_anded_and_cached(self):
        client = FakeClient()
        selector = AccountSelector(client)
        spec = SelectorSpec(
            addresses=["host01"],
            matches_all=[MatchRule("safe", "^Linux-")],
            activity_equals={"DiscoveryPlatformType": ["Unix SSH Key"]},
        )
        self.assertEqual(selector.resolve(spec), [ACCOUNTS[0]])
        self.assertEqual(selector.resolve(spec), [ACCOUNTS[0]])
        self.assertEqual(client.activity_calls, 1)

    def test_any_exclusion_removes_an_account(self):
        selector = AccountSelector(FakeClient())
        spec = SelectorSpec(
            excluded_accounts=["root"],
            excluded_matches=[MatchRule("platform", "Keys$")],
        )
        self.assertEqual(selector.resolve(spec), [])

    def test_playbook_and_runtime_exclusions_are_unioned(self):
        playbook = SelectorSpec(excluded_accounts=["root"])
        runtime = SelectorSpec(excluded_safes=["Linux-Prod"])
        merged = playbook.merged_with(runtime)
        self.assertEqual(merged.excluded_accounts, ["root"])
        self.assertEqual(merged.excluded_safes, ["Linux-Prod"])

    def test_activity_exclusions_are_or_conditions_and_are_cached(self):
        client = FakeClient()
        selector = AccountSelector(client)
        spec = SelectorSpec(
            excluded_activity_equals={
                "DiscoveryPlatformType": ["Unix SSH Key"]
            },
            excluded_activity_matches={
                "DiscoveryPlatformType": ["via SSH$"]
            },
        )
        self.assertEqual(selector.resolve(spec), [])
        self.assertEqual(selector.resolve(spec), [])
        self.assertEqual(client.activity_calls, 2)


if __name__ == "__main__":
    unittest.main()
