from __future__ import annotations

import unittest
from unittest.mock import patch

from cyberark_engine import ExecutionEngine
from cyberark_playbook import ActionSpec, TaskSpec
from cyberark_selectors import SelectorSpec
from cyberark_utils import ConfigurationError


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
        "address": "host01",
        "platformId": "UnixSSH",
        "safeName": "Linux-Prod",
    },
]


class FakeClient:
    def __init__(self):
        self.password_updates = []
        self.retrieve_calls = 0
        self.retrieve_reason = None

    def search_accounts(self, _term):
        return ACCOUNTS

    def get_account(self, account_id, **_kwargs):
        return next(item for item in ACCOUNTS if item["id"] == account_id)

    def get_activities(self, _account_id, *, limit):
        return []

    def update_password(self, account_id, password):
        self.password_updates.append((account_id, password))

    def retrieve_password(self, _account_id, *, reason=""):
        self.retrieve_calls += 1
        self.retrieve_reason = reason
        return "source-secret"


class EngineTests(unittest.TestCase):
    def task(self, action, selector=None):
        return TaskSpec(
            name="test",
            selector=selector or SelectorSpec(addresses=["host01"]),
            actions=[action],
        )

    def test_random_password_preview_does_not_generate_or_update(self):
        client = FakeClient()
        engine = ExecutionEngine(client)
        action = ActionSpec(
            "set_password", {"mode": "random-distinct", "length": 30}
        )
        with patch.object(engine, "_random_password") as generate:
            self.assertEqual(engine.run_tasks([self.task(action)]), 0)
        generate.assert_not_called()
        self.assertEqual(client.password_updates, [])

    def test_random_distinct_generates_one_password_per_account(self):
        client = FakeClient()
        engine = ExecutionEngine(client)
        action = ActionSpec(
            "set_password", {"mode": "random-distinct", "length": 30}
        )
        with patch.object(
            engine, "_random_password", side_effect=["first-secret", "second-secret"]
        ):
            self.assertEqual(engine.run_tasks([self.task(action)], execute=True), 0)
        self.assertEqual(
            client.password_updates,
            [("1", "first-secret"), ("2", "second-secret")],
        )

    def test_copy_password_preview_does_not_retrieve_secret(self):
        client = FakeClient()
        engine = ExecutionEngine(client)
        action = ActionSpec("copy_password", {"source": {"id": "1"}})
        self.assertEqual(engine.run_tasks([self.task(action)]), 0)
        self.assertEqual(client.retrieve_calls, 0)

    def test_copy_password_retrieves_once_and_skips_source(self):
        client = FakeClient()
        action = ActionSpec(
            "copy_password",
            {"source": {"id": "1"}, "reason": "approved maintenance"},
        )
        self.assertEqual(
            ExecutionEngine(client).run_tasks([self.task(action)], execute=True), 0
        )
        self.assertEqual(client.retrieve_calls, 1)
        self.assertEqual(client.retrieve_reason, "approved maintenance")
        self.assertEqual(client.password_updates, [("2", "source-secret")])

    def test_move_safe_refuses_unverified_destructive_emulation(self):
        client = FakeClient()
        action = ActionSpec("move_safe", {"safe": "AnotherSafe"})
        with self.assertRaisesRegex(ConfigurationError, "not executable"):
            ExecutionEngine(client).run_tasks([self.task(action)], execute=True)

    def test_mutation_requires_finite_scope(self):
        client = FakeClient()
        action = ActionSpec("reconcile", {})
        with self.assertRaisesRegex(ConfigurationError, "requires addresses"):
            ExecutionEngine(client).run_tasks(
                [self.task(action, SelectorSpec(platforms=["UnixSSH"]))]
            )

    def test_runtime_address_satisfies_playbook_requirement(self):
        client = FakeClient()
        task = self.task(
            ActionSpec("list", {}), SelectorSpec(platforms=["UnixSSH"])
        )
        self.assertEqual(
            ExecutionEngine(client).run_tasks(
                [task],
                runtime_selector=SelectorSpec(addresses=["host01"]),
                required_selectors=["addresses"],
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
