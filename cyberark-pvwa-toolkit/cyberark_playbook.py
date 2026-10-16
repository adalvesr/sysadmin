"""Strict YAML playbook loading for the PVWA toolkit."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from cyberark_selectors import MatchRule, SelectorSpec, addresses_from_sources
from cyberark_utils import ConfigurationError, FIELD_MAP


SUPPORTED_ACTIONS = {
    "list",
    "set-password",
    "copy-password",
    "link-account",
    "change-platform",
    "move-safe",
    "set-management",
    "reconcile",
}


@dataclass(frozen=True)
class ActionSpec:
    name: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskSpec:
    name: str
    selector: SelectorSpec
    actions: list[ActionSpec]
    target: str = "accounts"


@dataclass
class PlaybookSpec:
    path: Path
    connection: str | None
    required_selectors: list[str]
    tasks: list[TaskSpec]


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"{context} must be a mapping")
    return dict(value)


def _list(value: Any, context: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    raise ConfigurationError(f"{context} must be a list")


def _strings(value: Any, context: str) -> list[str]:
    values = _list(value, context)
    if not all(isinstance(item, str) and item.strip() for item in values):
        raise ConfigurationError(f"{context} must contain non-empty strings")
    return [item.strip() for item in values]


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ConfigurationError(f"Unknown option(s) in {context}: {', '.join(unknown)}")


def _action_options(value: Any, context: str) -> dict[str, Any]:
    """Validate hyphenated playbook keys and translate them for Python internals."""

    raw = _mapping(value, context)
    normalized: dict[str, Any] = {}
    for key, item in raw.items():
        if not isinstance(key, str) or not key:
            raise ConfigurationError(f"{context} option names must be non-empty strings")
        if "_" in key:
            raise ConfigurationError(
                f"Use hyphenated option names in {context}: {key.replace('_', '-')}"
            )
        internal_key = key.replace("-", "_")
        if isinstance(item, dict):
            normalized[internal_key] = _action_options(item, f"{context}.{key}")
        elif isinstance(item, list):
            normalized[internal_key] = [
                _action_options(value, f"{context}.{key}")
                if isinstance(value, dict)
                else value
                for value in item
            ]
        else:
            normalized[internal_key] = item
    return normalized


def _match_rules(value: Any, context: str) -> list[MatchRule]:
    rules: list[MatchRule] = []
    for index, raw in enumerate(_list(value, context)):
        item = _mapping(raw, f"{context}[{index}]")
        _reject_unknown(item, {"field", "regex"}, f"{context}[{index}]")
        field_name = item.get("field")
        pattern = item.get("regex")
        if field_name not in FIELD_MAP or not isinstance(pattern, str) or not pattern:
            raise ConfigurationError(
                f"{context}[{index}] requires a supported field and non-empty regex"
            )
        rules.append(MatchRule(str(field_name), pattern))
    return rules


def _addresses(
    value: Any, *, context: str, base_directory: Path
) -> list[str]:
    if isinstance(value, list):
        address_values = _strings(value, context)
        address_files: list[str] = []
    else:
        address_mapping = _mapping(value, context)
        _reject_unknown(address_mapping, {"values", "files"}, context)
        address_values = _strings(address_mapping.get("values"), f"{context}.values")
        address_files = _strings(address_mapping.get("files"), f"{context}.files")
    return addresses_from_sources(
        address_values, address_files, base_directory=base_directory
    )


def _activities(
    value: Any, *, context: str
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    equals: dict[str, list[str]] = {}
    matches: dict[str, list[str]] = {}
    for category, constraint in _mapping(value, context).items():
        if not isinstance(category, str) or not category:
            raise ConfigurationError("Activity category names must be non-empty strings")
        if isinstance(constraint, list):
            equals[category] = _strings(constraint, f"{context}.{category}")
            continue
        options = _mapping(constraint, f"{context}.{category}")
        _reject_unknown(options, {"equals", "matches"}, f"{context}.{category}")
        if "equals" in options:
            equals[category] = _strings(
                options["equals"], f"{context}.{category}.equals"
            )
        if "matches" in options:
            matches[category] = _strings(
                options["matches"], f"{context}.{category}.matches"
            )
    return equals, matches


def selector_from_mapping(value: Any, *, base_directory: Path) -> SelectorSpec:
    raw = _mapping(value, "task.select")
    allowed = {
        "addresses",
        "accounts",
        "platforms",
        "safes",
        "ids",
        "where",
        "where-any",
        "activities",
        "discovery-platform-types",
        "include-domain-users",
        "activities-limit",
        "all-accounts",
        "exclude",
    }
    _reject_unknown(raw, allowed, "task.select")

    activity_equals, activity_matches = _activities(
        raw.get("activities"), context="task.select.activities"
    )

    discovery_types = _strings(
        raw.get("discovery-platform-types"),
        "task.select.discovery-platform-types",
    )
    if discovery_types:
        existing = activity_equals.setdefault("DiscoveryPlatformType", [])
        existing.extend(discovery_types)

    all_rules = _match_rules(raw.get("where"), "task.select.where")
    any_rules = _match_rules(raw.get("where-any"), "task.select.where-any")

    exclude = _mapping(raw.get("exclude"), "task.select.exclude")
    _reject_unknown(
        exclude,
        {
            "addresses",
            "accounts",
            "platforms",
            "safes",
            "ids",
            "where",
            "activities",
            "discovery-platform-types",
        },
        "task.select.exclude",
    )
    excluded_activity_equals, excluded_activity_matches = _activities(
        exclude.get("activities"), context="task.select.exclude.activities"
    )
    excluded_discovery_types = _strings(
        exclude.get("discovery-platform-types"),
        "task.select.exclude.discovery-platform-types",
    )
    if excluded_discovery_types:
        excluded_activity_equals.setdefault("DiscoveryPlatformType", []).extend(
            excluded_discovery_types
        )

    include_domain_users = raw.get("include-domain-users")
    if include_domain_users is not None and not isinstance(include_domain_users, bool):
        raise ConfigurationError("task.select.include-domain-users must be a boolean")
    activities_limit = raw.get("activities-limit")
    if activities_limit is not None and (
        not isinstance(activities_limit, int)
        or isinstance(activities_limit, bool)
        or activities_limit <= 0
    ):
        raise ConfigurationError("task.select.activities-limit must be a positive integer")
    all_accounts = raw.get("all-accounts", False)
    if not isinstance(all_accounts, bool):
        raise ConfigurationError("task.select.all-accounts must be a boolean")

    return SelectorSpec(
        addresses=_addresses(
            raw.get("addresses"),
            context="task.select.addresses",
            base_directory=base_directory,
        ),
        accounts=_strings(raw.get("accounts"), "task.select.accounts"),
        platforms=_strings(raw.get("platforms"), "task.select.platforms"),
        safes=_strings(raw.get("safes"), "task.select.safes"),
        ids=_strings(raw.get("ids"), "task.select.ids"),
        matches_all=all_rules,
        match_any_groups=[any_rules] if any_rules else [],
        activity_equals=activity_equals,
        activity_matches=activity_matches,
        excluded_addresses=_addresses(
            exclude.get("addresses"),
            context="task.select.exclude.addresses",
            base_directory=base_directory,
        ),
        excluded_accounts=_strings(
            exclude.get("accounts"), "task.select.exclude.accounts"
        ),
        excluded_platforms=_strings(
            exclude.get("platforms"), "task.select.exclude.platforms"
        ),
        excluded_safes=_strings(
            exclude.get("safes"), "task.select.exclude.safes"
        ),
        excluded_ids=_strings(exclude.get("ids"), "task.select.exclude.ids"),
        excluded_matches=_match_rules(
            exclude.get("where"), "task.select.exclude.where"
        ),
        excluded_activity_equals=excluded_activity_equals,
        excluded_activity_matches=excluded_activity_matches,
        include_domain_users=include_domain_users,
        activities_limit=activities_limit,
        all_accounts=all_accounts,
    )


def _actions(value: Any, context: str) -> list[ActionSpec]:
    actions: list[ActionSpec] = []
    for index, raw in enumerate(_list(value, context)):
        item = _mapping(raw, f"{context}[{index}]")
        if len(item) != 1:
            raise ConfigurationError(f"{context}[{index}] must contain exactly one action")
        name, options = next(iter(item.items()))
        if name not in SUPPORTED_ACTIONS:
            raise ConfigurationError(f"Unsupported action in {context}[{index}]: {name}")
        actions.append(
            ActionSpec(
                name=name.replace("-", "_"),
                options=_action_options(options, f"action {name}"),
            )
        )
    if not actions:
        raise ConfigurationError(f"{context} must contain at least one action")
    return actions


def load_playbook(path: str | Path) -> PlaybookSpec:
    playbook_path = Path(path).expanduser().resolve()
    try:
        with playbook_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Playbook not found: {playbook_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {playbook_path}: {exc}") from exc

    document = _mapping(raw, "playbook")
    _reject_unknown(
        document,
        {"version", "connection", "required-selectors", "tasks"},
        "playbook",
    )
    if document.get("version") != 1:
        raise ConfigurationError("Playbook version must be 1")
    connection = document.get("connection")
    if connection is not None and (not isinstance(connection, str) or not connection.strip()):
        raise ConfigurationError("Playbook connection must be a profile name")
    required = _strings(
        document.get("required-selectors"), "required-selectors"
    )

    tasks: list[TaskSpec] = []
    for index, raw_task in enumerate(_list(document.get("tasks"), "tasks")):
        task = _mapping(raw_task, f"tasks[{index}]")
        _reject_unknown(task, {"name", "target", "select", "actions"}, f"tasks[{index}]")
        name = task.get("name", f"Task {index + 1}")
        if not isinstance(name, str) or not name.strip():
            raise ConfigurationError(f"tasks[{index}].name must be a non-empty string")
        target = task.get("target", "accounts")
        if target != "accounts":
            raise ConfigurationError(
                f"tasks[{index}].target {target!r} is not supported yet"
            )
        tasks.append(
            TaskSpec(
                name=name.strip(),
                target=target,
                selector=selector_from_mapping(
                    task.get("select"), base_directory=playbook_path.parent
                ),
                actions=_actions(task.get("actions"), f"tasks[{index}].actions"),
            )
        )
    if not tasks:
        raise ConfigurationError("Playbook must contain at least one task")
    return PlaybookSpec(
        path=playbook_path,
        connection=connection.strip() if isinstance(connection, str) else None,
        required_selectors=required,
        tasks=tasks,
    )
