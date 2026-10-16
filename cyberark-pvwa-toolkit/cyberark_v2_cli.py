"""Resource-oriented CLI for account actions and playbook execution."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from cyberark_engine import ExecutionEngine
from cyberark_playbook import ActionSpec, TaskSpec, load_playbook
from cyberark_selectors import MatchRule, SelectorSpec, addresses_from_sources
from cyberark_utils import FIELD_MAP, ConfigurationError, CyberArkClient


def _append_argument(
    parser: argparse._ActionsContainer,
    *names: str,
    dest: str,
    help: str,
    **kwargs: Any,
) -> None:
    parser.add_argument(*names, dest=dest, action="append", help=help, **kwargs)


def add_selector_arguments(parser: argparse.ArgumentParser) -> None:
    selectors = parser.add_argument_group("account selectors")
    _append_argument(
        selectors,
        "-H",
        "--hostname",
        "--address",
        dest="selector_hostnames",
        help="account address; repeatable",
    )
    _append_argument(
        selectors,
        "-f",
        "--file",
        "--address-file",
        dest="selector_files",
        help="file containing account addresses; repeatable",
    )
    _append_argument(
        selectors,
        "--account",
        dest="selector_accounts",
        help="exact account username; repeatable",
    )
    _append_argument(
        selectors,
        "--platform",
        dest="selector_platforms",
        help="exact platform ID; repeatable",
    )
    _append_argument(
        selectors,
        "--safe",
        dest="selector_safes",
        help="exact safe name; repeatable",
    )
    _append_argument(
        selectors,
        "--id",
        dest="selector_ids",
        help="exact account ID; repeatable",
    )
    selectors.add_argument(
        "--where",
        nargs=2,
        action="append",
        metavar=("FIELD", "REGEX"),
        help="ANDed field/regex condition; repeatable",
    )
    selectors.add_argument(
        "--where-any",
        nargs=2,
        action="append",
        metavar=("FIELD", "REGEX"),
        help="ORed field/regex condition; repeatable",
    )
    _append_argument(
        selectors,
        "-F",
        "--field",
        dest="legacy_fields",
        choices=tuple(FIELD_MAP),
        help="compatibility field paired with -m/--match",
    )
    _append_argument(
        selectors,
        "-m",
        "--match",
        dest="legacy_matches",
        help="compatibility regex paired with -F/--field",
    )
    selectors.add_argument(
        "--activity",
        nargs=2,
        action="append",
        metavar=("CATEGORY", "VALUE"),
        help="exact activity file-category value; repeatable",
    )
    selectors.add_argument(
        "--activity-match",
        nargs=2,
        action="append",
        metavar=("CATEGORY", "REGEX"),
        help="activity file-category regex; repeatable",
    )
    _append_argument(
        selectors,
        "--dpt",
        "--discovery-platform-type",
        dest="selector_dpt",
        help="alias for --activity DiscoveryPlatformType VALUE",
    )
    _append_argument(
        selectors,
        "--dpt-match",
        dest="selector_dpt_matches",
        help="regex alias for DiscoveryPlatformType",
    )
    selectors.add_argument(
        "--include-domain-users",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    selectors.add_argument("--activities-limit", type=int)
    selectors.add_argument(
        "--all-accounts",
        action="store_true",
        help="explicitly allow an unbounded account scope",
    )


def _add_execute(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--execute",
        action="store_true",
        help="apply mutations; without this flag the action is a preview",
    )


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_argument_group("source account")
    source.add_argument("--source-id")
    source.add_argument("--source-account")
    source.add_argument("--source-address")
    source.add_argument("--source-platform")
    source.add_argument("--source-safe")


def add_resource_parsers(commands: argparse._SubParsersAction[Any]) -> None:
    accounts = commands.add_parser(
        "accounts", help="select accounts, then perform one account action"
    )
    add_selector_arguments(accounts)
    actions = accounts.add_subparsers(dest="account_action", required=True)

    list_parser = actions.add_parser("list", help="list selected accounts")
    _append_argument(
        list_parser,
        "--output-field",
        dest="output_fields",
        choices=tuple(FIELD_MAP),
        help="field to print; repeatable",
    )

    password = actions.add_parser("set-password", help="set Vault-only passwords")
    password.add_argument(
        "--mode",
        required=True,
        choices=(
            "prompt-shared",
            "prompt-distinct",
            "random-shared",
            "random-distinct",
            "environment-shared",
        ),
    )
    password.add_argument("--length", type=int, default=30)
    password.add_argument("--password-env")
    _add_execute(password)

    copy = actions.add_parser(
        "copy-password", help="copy a password from exactly one source account"
    )
    _add_source_arguments(copy)
    copy.add_argument("--reason", default="", help="auditable retrieval reason")
    _add_execute(copy)

    link = actions.add_parser("link-account", help="link a logon or reconcile account")
    link.add_argument("--role", choices=("logon", "reconcile"), required=True)
    link.add_argument("--index", type=int)
    link.add_argument("--folder", default="Root")
    _add_source_arguments(link)
    _add_execute(link)

    platform = actions.add_parser("change-platform", help="change the account platform")
    platform.add_argument("--to", dest="target_platform", required=True)
    _add_execute(platform)

    safe = actions.add_parser(
        "move-safe",
        help="reserved for a version-specific safe-move adapter; not executable yet",
    )
    safe.add_argument("--to", dest="target_safe", required=True)
    _add_execute(safe)

    management = actions.add_parser(
        "set-management", help="enable or disable automatic account management"
    )
    management.add_argument("--enabled", choices=("true", "false"), required=True)
    management.add_argument("--reason", default="")
    _add_execute(management)

    reconcile = actions.add_parser("reconcile", help="trigger account reconciliation")
    _add_execute(reconcile)
    accounts.set_defaults(handler=command_accounts)

    playbook = commands.add_parser(
        "playbook", help="apply runtime selectors to an ordered YAML playbook"
    )
    add_selector_arguments(playbook)
    playbook_actions = playbook.add_subparsers(dest="playbook_action", required=True)
    run = playbook_actions.add_parser("run", help="validate, preview, or execute a playbook")
    run.add_argument("playbook_file")
    _add_execute(run)
    playbook.set_defaults(handler=command_playbook)


def _legacy_rules(args: argparse.Namespace) -> tuple[list[MatchRule], list[list[MatchRule]]]:
    fields = list(args.legacy_fields or [])
    patterns = list(args.legacy_matches or [])
    if not patterns:
        return [], []
    if not fields:
        fields = ["user"]
    if len(fields) == len(patterns):
        return [MatchRule(field, pattern) for field, pattern in zip(fields, patterns)], []
    if len(patterns) == 1:
        return [], [[MatchRule(field, patterns[0]) for field in fields]]
    if len(fields) == 1:
        return [MatchRule(fields[0], pattern) for pattern in patterns], []
    raise ConfigurationError(
        "Repeat --field and --match equally, or provide one field or one match"
    )


def selector_from_args(args: argparse.Namespace) -> SelectorSpec:
    matches_all, match_any_groups = _legacy_rules(args)
    for field_name, pattern in args.where or []:
        if field_name not in FIELD_MAP:
            raise ConfigurationError(f"Unsupported filter field: {field_name}")
        matches_all.append(MatchRule(field_name, pattern))
    any_rules: list[MatchRule] = []
    for field_name, pattern in args.where_any or []:
        if field_name not in FIELD_MAP:
            raise ConfigurationError(f"Unsupported filter field: {field_name}")
        any_rules.append(MatchRule(field_name, pattern))
    if any_rules:
        match_any_groups.append(any_rules)

    activity_equals: dict[str, list[str]] = {}
    for category, value in args.activity or []:
        activity_equals.setdefault(category, []).append(value)
    if args.selector_dpt:
        activity_equals.setdefault("DiscoveryPlatformType", []).extend(args.selector_dpt)
    activity_matches: dict[str, list[str]] = {}
    for category, pattern in args.activity_match or []:
        activity_matches.setdefault(category, []).append(pattern)
    if args.selector_dpt_matches:
        activity_matches.setdefault("DiscoveryPlatformType", []).extend(
            args.selector_dpt_matches
        )

    return SelectorSpec(
        addresses=addresses_from_sources(args.selector_hostnames, args.selector_files),
        accounts=list(args.selector_accounts or []),
        platforms=list(args.selector_platforms or []),
        safes=list(args.selector_safes or []),
        ids=list(args.selector_ids or []),
        matches_all=matches_all,
        match_any_groups=match_any_groups,
        activity_equals=activity_equals,
        activity_matches=activity_matches,
        include_domain_users=args.include_domain_users,
        activities_limit=args.activities_limit,
        all_accounts=args.all_accounts,
    )


def _source_options(args: argparse.Namespace) -> dict[str, str]:
    values = {
        "id": args.source_id,
        "account": args.source_account,
        "address": args.source_address,
        "platform": args.source_platform,
        "safe": args.source_safe,
    }
    return {key: value for key, value in values.items() if value is not None}


def _action_from_args(args: argparse.Namespace) -> ActionSpec:
    name = args.account_action.replace("-", "_")
    if name == "list":
        options = {"fields": args.output_fields} if args.output_fields else {}
    elif name == "set_password":
        options = {"mode": args.mode, "length": args.length}
        if args.password_env:
            options["password_env"] = args.password_env
    elif name == "copy_password":
        options = {"source": _source_options(args), "reason": args.reason}
    elif name == "link_account":
        options = {
            "role": args.role,
            "source": _source_options(args),
            "folder": args.folder,
        }
        if args.index is not None:
            options["index"] = args.index
    elif name == "change_platform":
        options = {"platform": args.target_platform}
    elif name == "move_safe":
        options = {"safe": args.target_safe}
    elif name == "set_management":
        options = {"enabled": args.enabled == "true", "reason": args.reason}
    else:
        options = {}
    return ActionSpec(name=name, options=options)


def command_accounts(client: CyberArkClient, args: argparse.Namespace) -> int:
    task = TaskSpec(
        name=f"accounts {args.account_action}",
        selector=selector_from_args(args),
        actions=[_action_from_args(args)],
    )
    return ExecutionEngine(client).run_tasks(
        [task], execute=bool(getattr(args, "execute", False))
    )


def command_playbook(client: CyberArkClient, args: argparse.Namespace) -> int:
    playbook = getattr(args, "loaded_playbook", None) or load_playbook(args.playbook_file)
    return ExecutionEngine(client).run_tasks(
        playbook.tasks,
        runtime_selector=selector_from_args(args),
        required_selectors=playbook.required_selectors,
        execute=args.execute,
    )


def preload_playbook(args: argparse.Namespace) -> Any:
    if getattr(args, "command", None) != "playbook":
        return None
    if getattr(args, "playbook_action", None) != "run":
        return None
    return load_playbook(Path(args.playbook_file))
