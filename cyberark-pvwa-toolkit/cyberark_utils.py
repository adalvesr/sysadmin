"""Reusable CyberArk PVWA REST client and account-selection helpers.

Commands receive the PVWA URL, platform IDs, and account names through
configuration or command-line arguments.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote, urljoin, urlparse

import requests
import urllib3


FIELD_MAP = {
    "id": "id",
    "user": "userName",
    "name": "name",
    "safe": "safeName",
    "platform": "platformId",
    "address": "address",
}

_FILE_CATEGORY_VALUE = re.compile(r"Value=\[(.*?)\]")


class CyberArkError(RuntimeError):
    """Base exception for errors reported by these utilities."""


class CyberArkAPIError(CyberArkError):
    """A PVWA request failed or returned an unexpected response."""


class ConfigurationError(CyberArkError):
    """The local command configuration is invalid."""


def normalize_base_url(value: str) -> str:
    """Validate and normalize a PVWA base URL."""

    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(
            "PVWA URL must be an absolute http(s) URL, for example "
            "https://vault.example/PasswordVault"
        )
    return value


def read_lines_dedup(path: str | Path) -> list[str]:
    """Read non-empty, non-comment lines and de-duplicate case-insensitively."""

    values: list[str] = []
    seen: set[str] = set()
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        for raw in handle:
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                values.append(value)
    return values


def extract_file_categories(activities: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Extract the newest file-category value from newest-first activities."""

    categories: dict[str, str] = {}
    for activity in activities:
        if activity.get("Action") != "Add File Category":
            continue
        key = activity.get("MoreInfo")
        match = _FILE_CATEGORY_VALUE.search(str(activity.get("Reason") or ""))
        if key and match:
            categories.setdefault(str(key), match.group(1).strip())
    return categories


class CyberArkClient:
    """Small, testable client for the PVWA account APIs used by this toolkit."""

    def __init__(
        self,
        base_url: str,
        *,
        verify: bool | str = True,
        timeout: float = 45,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.verify = verify
        self.timeout = timeout
        self.session = session or requests.Session()
        self.token: str | None = None
        if verify is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: Sequence[int] = (200,),
        **kwargs: Any,
    ) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self.verify)
        try:
            response = self.session.request(method, self._url(path), **kwargs)
        except requests.RequestException as exc:
            raise CyberArkAPIError(f"{method} {path} failed: {exc}") from exc
        if response.status_code not in expected:
            detail = response.text.strip().replace("\n", " ")[:500]
            suffix = f": {detail}" if detail else ""
            raise CyberArkAPIError(
                f"{method} {path} returned HTTP {response.status_code}{suffix}"
            )
        return response

    @staticmethod
    def _json(response: requests.Response, context: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise CyberArkAPIError(f"{context} returned invalid JSON") from exc

    def login(self, username: str, password: str, auth_type: str = "LDAP") -> str:
        """Authenticate and retain the returned session token."""

        auth_segment = quote(auth_type.strip(), safe="")
        response = self._request(
            "POST",
            f"api/auth/{auth_segment}/Logon",
            json={"username": username, "password": password},
        )
        try:
            data = response.json()
            token = data if isinstance(data, str) else data.get("CyberArkLogonResult")
        except (ValueError, AttributeError):
            token = response.text.strip().strip('"')
        if not token:
            raise CyberArkAPIError("Logon succeeded but PVWA returned no session token")
        self.token = str(token)
        self.session.headers.update({"Authorization": self.token})
        return self.token

    def logout(self) -> None:
        """End the PVWA session. Logoff is best-effort by design."""

        if not self.token:
            return
        try:
            self._request("POST", "api/auth/Logoff", expected=(200, 204))
        except CyberArkAPIError:
            pass
        finally:
            self.token = None
            self.session.headers.pop("Authorization", None)

    def close(self) -> None:
        self.logout()
        self.session.close()

    def __enter__(self) -> "CyberArkClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def search_accounts(self, search: str, *, max_pages: int = 1000) -> list[dict[str, Any]]:
        """Search all account result pages, following PVWA's ``nextLink``."""

        path = "api/Accounts"
        params: Mapping[str, str] | None = {"search": search}
        accounts: list[dict[str, Any]] = []
        for _ in range(max_pages):
            response = self._request("GET", path, params=params)
            payload = self._json(response, "Account search") or {}
            values = payload.get("value", []) if isinstance(payload, dict) else []
            if not isinstance(values, list):
                raise CyberArkAPIError("Accounts response contains a non-list 'value'")
            accounts.extend(dict(item) for item in values)
            next_link = payload.get("nextLink") if isinstance(payload, dict) else None
            if not next_link:
                return accounts
            path = urljoin(f"{self.base_url}/", str(next_link))
            params = None
        raise CyberArkAPIError(f"Account search exceeded {max_pages} pages")

    def get_account(self, account_id: str, *, include_internal: bool = False) -> dict[str, Any]:
        params = {"includeInternalProperties": "true"} if include_internal else None
        account_path = quote(str(account_id), safe="")
        response = self._request("GET", f"api/Accounts/{account_path}", params=params)
        payload = self._json(response, f"Account {account_id}")
        if not isinstance(payload, dict):
            raise CyberArkAPIError(f"Account {account_id} returned a non-object response")
        return dict(payload)

    def get_activities(
        self, account_id: str, *, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        account_path = quote(str(account_id), safe="")
        response = self._request(
            "GET",
            f"api/Accounts/{account_path}/Activities",
            params={"limit": str(limit), "offset": str(offset)},
        )
        payload = self._json(response, f"Activities for account {account_id}")
        if isinstance(payload, list):
            return [dict(item) for item in payload]
        if isinstance(payload, dict):
            values = payload.get("Activities", payload.get("value", []))
            if isinstance(values, list):
                return [dict(item) for item in values]
        return []

    def update_password(self, account_id: str, new_password: str) -> None:
        account_path = quote(str(account_id), safe="")
        self._request(
            "POST",
            f"api/Accounts/{account_path}/Password/Update/",
            expected=(200, 204),
            json={"NewCredentials": new_password},
        )

    def retrieve_password(self, account_id: str, *, reason: str = "") -> str:
        """Retrieve an account password without writing it to output or logs."""

        account_path = quote(str(account_id), safe="")
        body = {"reason": reason} if reason else {}
        response = self._request(
            "POST",
            f"api/Accounts/{account_path}/Password/Retrieve/",
            json=body,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = response.text.strip().strip('"')
        if isinstance(payload, dict):
            password = payload.get("Content") or payload.get("content")
        else:
            password = payload
        if not isinstance(password, str) or not password:
            raise CyberArkAPIError(
                f"Password retrieval for account {account_id} returned no secret"
            )
        return password

    def reconcile(self, account_id: str) -> None:
        account_path = quote(str(account_id), safe="")
        self._request(
            "POST",
            f"api/Accounts/{account_path}/Reconcile/",
            expected=(200, 202, 204),
        )

    def patch_account(self, account_id: str, operations: Sequence[Mapping[str, Any]]) -> None:
        if operations:
            account_path = quote(str(account_id), safe="")
            self._request(
                "PATCH",
                f"api/Accounts/{account_path}",
                expected=(200, 204),
                json=list(operations),
            )

    def link_account(
        self,
        account_id: str,
        *,
        safe: str,
        name: str,
        folder: str = "Root",
        extra_password_index: int = 3,
    ) -> None:
        account_path = quote(str(account_id), safe="")
        self._request(
            "POST",
            f"api/Accounts/{account_path}/LinkAccount/",
            expected=(200, 201, 204),
            json={
                "safe": safe,
                "extraPasswordIndex": extra_password_index,
                "name": name,
                "folder": folder,
            },
        )
