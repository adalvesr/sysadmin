from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cyberark_utils import (
    ConfigurationError,
    CyberArkAPIError,
    CyberArkClient,
    collect_targets,
    extract_file_categories,
    filter_accounts,
    normalize_base_url,
    read_lines_dedup,
    regex_filter_accounts,
    split_values,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self.payload = payload
        self.text = text

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class InvalidJsonResponse(FakeResponse):
    def json(self):
        raise ValueError("not JSON")


ACCOUNTS = [
    {
        "id": "1",
        "userName": "root",
        "address": "host01.example.com",
        "platformId": "UnixSSH",
        "safeName": "Linux-Prod",
    },
    {
        "id": "2",
        "userName": "svc@example.com",
        "address": "HOST01.EXAMPLE.COM",
        "platformId": "UnixSSH",
        "safeName": "Linux-Prod",
    },
    {
        "id": "3",
        "userName": "root",
        "address": "host02.example.com",
        "platformId": "Other",
        "safeName": "Linux-Test",
    },
]


class UtilityTests(unittest.TestCase):
    def test_normalize_base_url(self):
        self.assertEqual(
            normalize_base_url(" https://vault.example/PasswordVault/ "),
            "https://vault.example/PasswordVault",
        )
        with self.assertRaises(ConfigurationError):
            normalize_base_url("vault.example")

    def test_read_and_collect_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hosts.txt"
            path.write_text("# comment\nhost01\nHOST01\n\nhost02\n", encoding="utf-8")
            self.assertEqual(read_lines_dedup(path), ["host01", "host02"])
            self.assertEqual(collect_targets("HOST02", str(path)), ["HOST02", "host01"])

    def test_split_values_is_repeatable_and_case_insensitive(self):
        self.assertEqual(
            split_values(["UnixSSH, Other", "unixssh", "Third"]),
            ["UnixSSH", "Other", "Third"],
        )

    def test_filter_accounts_applies_address_platform_and_local_user(self):
        self.assertEqual(
            filter_accounts(
                ACCOUNTS,
                address="HOST01.example.com",
                platform_ids=["unixssh"],
                local_only=True,
            ),
            [ACCOUNTS[0]],
        )

    def test_regex_filter_reports_invalid_expression(self):
        self.assertEqual(regex_filter_accounts(ACCOUNTS, "^Linux-P", "safe"), ACCOUNTS[:2])
        with self.assertRaises(ConfigurationError):
            regex_filter_accounts(ACCOUNTS, "[", "user")

    def test_extract_file_categories_keeps_newest_value(self):
        activities = [
            {
                "Action": "Add File Category",
                "MoreInfo": "DiscoveryPlatformType",
                "Reason": "Value=[Unix SSH Key]",
            },
            {
                "Action": "Add File Category",
                "MoreInfo": "DiscoveryPlatformType",
                "Reason": "Value=[Old Value]",
            },
        ]
        self.assertEqual(
            extract_file_categories(activities),
            {"DiscoveryPlatformType": "Unix SSH Key"},
        )

    def test_login_sets_session_header_and_logout_clears_it(self):
        session = FakeSession([FakeResponse(payload="token"), FakeResponse(status_code=204)])
        client = CyberArkClient("https://vault.example/PasswordVault", session=session)
        self.assertEqual(client.login("user", "secret"), "token")
        self.assertEqual(session.headers["Authorization"], "token")
        client.logout()
        self.assertNotIn("Authorization", session.headers)

    def test_search_accounts_follows_next_link(self):
        session = FakeSession(
            [
                FakeResponse(
                    payload={
                        "value": [{"id": "1"}],
                        "nextLink": "/PasswordVault/api/Accounts?offset=1",
                    }
                ),
                FakeResponse(payload={"value": [{"id": "2"}]}),
            ]
        )
        client = CyberArkClient("https://vault.example/PasswordVault", session=session)
        self.assertEqual(client.search_accounts("host01"), [{"id": "1"}, {"id": "2"}])
        self.assertEqual(session.calls[0][2]["params"], {"search": "host01"})
        self.assertIsNone(session.calls[1][2]["params"])
        self.assertEqual(
            session.calls[1][1],
            "https://vault.example/PasswordVault/api/Accounts?offset=1",
        )

    def test_api_error_does_not_dump_an_unbounded_response(self):
        session = FakeSession([FakeResponse(status_code=500, text="x" * 1000)])
        client = CyberArkClient("https://vault.example/PasswordVault", session=session)
        with self.assertRaises(CyberArkAPIError) as context:
            client.search_accounts("host01")
        self.assertLess(len(str(context.exception)), 600)

    def test_invalid_success_response_is_reported_as_an_api_error(self):
        client = CyberArkClient(
            "https://vault.example/PasswordVault",
            session=FakeSession([InvalidJsonResponse(text="not JSON")]),
        )
        with self.assertRaisesRegex(CyberArkAPIError, "invalid JSON"):
            client.search_accounts("host01")

    def test_account_ids_are_url_encoded(self):
        session = FakeSession([FakeResponse(status_code=204)])
        client = CyberArkClient("https://vault.example/PasswordVault", session=session)
        client.reconcile("account/id")
        self.assertEqual(
            session.calls[0][1],
            "https://vault.example/PasswordVault/api/Accounts/account%2Fid/Reconcile/",
        )


if __name__ == "__main__":
    unittest.main()
