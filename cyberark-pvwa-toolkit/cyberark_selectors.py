"""Reusable account selectors for CLI commands and playbooks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from cyberark_utils import (
    FIELD_MAP,
    ConfigurationError,
    CyberArkClient,
    extract_file_categories,
    read_lines_dedup,
)


@dataclass(frozen=True)
class MatchRule:
    field: str
    pattern: str


@dataclass
class SelectorSpec:
    """Normalized account-selection criteria.

    Values within a category are ORed. Different categories and every ``all``
    match rule are ANDed. Each ``any`` group contributes one additional ANDed
    condition, with the rules inside that group ORed.
    """

    addresses: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    safes: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    matches_all: list[MatchRule] = field(default_factory=list)
    match_any_groups: list[list[MatchRule]] = field(default_factory=list)
    activity_equals: dict[str, list[str]] = field(default_factory=dict)
    activity_matches: dict[str, list[str]] = field(default_factory=dict)
    include_domain_users: bool | None = None
    activities_limit: int | None = None
    all_accounts: bool = False
    matches_nothing: bool = False
    empty_categories: set[str] = field(default_factory=set)

    def merged_with(self, runtime: "SelectorSpec") -> "SelectorSpec":
        """Intersect a playbook selector with additional runtime constraints."""

        include_domain_users: bool | None
        if self.include_domain_users is None:
            include_domain_users = runtime.include_domain_users
        elif runtime.include_domain_users is None:
            include_domain_users = self.include_domain_users
        else:
            include_domain_users = self.include_domain_users and runtime.include_domain_users

        addresses = _intersect_category(self.addresses, runtime.addresses)
        accounts = _intersect_category(self.accounts, runtime.accounts)
        platforms = _intersect_category(self.platforms, runtime.platforms)
        safes = _intersect_category(self.safes, runtime.safes)
        ids = _intersect_category(self.ids, runtime.ids)
        activity_equals = _merge_mapping_constraints(
            self.activity_equals, runtime.activity_equals
        )
        empty_categories = {
            name
            for name, left, right, merged in (
                ("addresses", self.addresses, runtime.addresses, addresses),
                ("accounts", self.accounts, runtime.accounts, accounts),
                ("platforms", self.platforms, runtime.platforms, platforms),
                ("safes", self.safes, runtime.safes, safes),
                ("ids", self.ids, runtime.ids, ids),
            )
            if left and right and not merged
        }
        if any(
            category in self.activity_equals
            and category in runtime.activity_equals
            and not values
            for category, values in activity_equals.items()
        ):
            empty_categories.add("activities")

        return SelectorSpec(
            addresses=addresses,
            accounts=accounts,
            platforms=platforms,
            safes=safes,
            ids=ids,
            matches_all=[*self.matches_all, *runtime.matches_all],
            match_any_groups=[*self.match_any_groups, *runtime.match_any_groups],
            activity_equals=activity_equals,
            activity_matches=_merge_mapping_patterns(
                self.activity_matches, runtime.activity_matches
            ),
            include_domain_users=include_domain_users,
            activities_limit=runtime.activities_limit or self.activities_limit,
            all_accounts=self.all_accounts or runtime.all_accounts,
            matches_nothing=self.matches_nothing
            or runtime.matches_nothing
            or bool(empty_categories),
            empty_categories={
                *self.empty_categories,
                *runtime.empty_categories,
                *empty_categories,
            },
        )

    def has_category(self, name: str) -> bool:
        aliases = {
            "addresses": self.addresses,
            "accounts": self.accounts,
            "platforms": self.platforms,
            "safes": self.safes,
            "ids": self.ids,
            "activities": [*self.activity_equals, *self.activity_matches],
        }
        if name not in aliases:
            raise ConfigurationError(f"Unknown required selector category: {name}")
        return bool(aliases[name]) or name in self.empty_categories

    def has_mutation_scope(self) -> bool:
        return bool(
            self.addresses
            or self.ids
            or self.all_accounts
            or "addresses" in self.empty_categories
            or "ids" in self.empty_categories
        )


def _deduplicate(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value).strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def _intersect_category(playbook: list[str], runtime: list[str]) -> list[str]:
    if not playbook:
        return _deduplicate(runtime)
    if not runtime:
        return _deduplicate(playbook)
    allowed = {value.casefold() for value in playbook}
    return _deduplicate(value for value in runtime if value.casefold() in allowed)


def _merge_mapping_constraints(
    playbook: Mapping[str, list[str]], runtime: Mapping[str, list[str]]
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for category in {*playbook, *runtime}:
        result[category] = _intersect_category(
            list(playbook.get(category, [])), list(runtime.get(category, []))
        )
    return result


def _merge_mapping_patterns(
    playbook: Mapping[str, list[str]], runtime: Mapping[str, list[str]]
) -> dict[str, list[str]]:
    return {
        category: [*playbook.get(category, []), *runtime.get(category, [])]
        for category in {*playbook, *runtime}
    }


def addresses_from_sources(
    hostnames: Iterable[str] | None,
    filenames: Iterable[str] | None,
    *,
    base_directory: Path | None = None,
) -> list[str]:
    """Union explicit addresses and address files with case-insensitive deduplication."""

    values = list(hostnames or [])
    for filename in filenames or []:
        path = Path(filename).expanduser()
        if base_directory is not None and not path.is_absolute():
            path = base_directory / path
        values.extend(read_lines_dedup(path))
    return _deduplicate(values)


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConfigurationError(f"Invalid regular expression {pattern!r}: {exc}") from exc


def _rule_matches(account: Mapping[str, Any], rule: MatchRule) -> bool:
    if rule.field not in FIELD_MAP:
        raise ConfigurationError(f"Unsupported filter field: {rule.field}")
    return _compile(rule.pattern).search(str(account.get(FIELD_MAP[rule.field]) or "")) is not None


class AccountSelector:
    """Resolve a selector into a stable, de-duplicated account list."""

    def __init__(self, client: CyberArkClient) -> None:
        self.client = client
        self._activities: dict[str, dict[str, str]] = {}

    def resolve(self, spec: SelectorSpec) -> list[dict[str, Any]]:
        if spec.matches_nothing:
            return []
        candidates = self._search(spec)
        selected = [account for account in candidates if self._matches(account, spec)]
        return selected

    def _search(self, spec: SelectorSpec) -> list[dict[str, Any]]:
        if spec.ids:
            candidates = [self.client.get_account(account_id) for account_id in spec.ids]
        else:
            terms = spec.addresses or spec.accounts or [""]
            candidates = []
            for term in terms:
                candidates.extend(self.client.search_accounts(term))
        by_id: dict[str, dict[str, Any]] = {}
        for account in candidates:
            account_id = str(account.get("id") or "")
            if account_id:
                by_id.setdefault(account_id, dict(account))
        return list(by_id.values())

    def _matches(self, account: Mapping[str, Any], spec: SelectorSpec) -> bool:
        exact = {
            "address": spec.addresses,
            "userName": spec.accounts,
            "platformId": spec.platforms,
            "safeName": spec.safes,
            "id": spec.ids,
        }
        for field_name, allowed_values in exact.items():
            if allowed_values and str(account.get(field_name) or "").casefold() not in {
                value.casefold() for value in allowed_values
            }:
                return False
        if spec.include_domain_users is not True and "@" in str(account.get("userName") or ""):
            return False
        if not all(_rule_matches(account, rule) for rule in spec.matches_all):
            return False
        if any(
            not any(_rule_matches(account, rule) for rule in group)
            for group in spec.match_any_groups
        ):
            return False
        return self._activities_match(account, spec)

    def _activities_match(self, account: Mapping[str, Any], spec: SelectorSpec) -> bool:
        if not spec.activity_equals and not spec.activity_matches:
            return True
        account_id = str(account.get("id") or "")
        if account_id not in self._activities:
            activities = self.client.get_activities(
                account_id, limit=spec.activities_limit or 200
            )
            self._activities[account_id] = extract_file_categories(activities)
        categories = self._activities[account_id]
        for category, allowed in spec.activity_equals.items():
            actual = categories.get(category)
            if actual is None or actual.casefold() not in {
                value.casefold() for value in allowed
            }:
                return False
        for category, patterns in spec.activity_matches.items():
            actual = categories.get(category)
            if actual is None or not all(_compile(pattern).search(actual) for pattern in patterns):
                return False
        return True
