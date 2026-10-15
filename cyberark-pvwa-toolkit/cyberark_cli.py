#!/usr/bin/env python3
"""Command-line workflows for CyberArk PVWA account administration."""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import string
import sys
from collections.abc import Callable
from typing import Any

from cyberark_config import command_defaults, load_config
from cyberark_utils import (
    FIELD_MAP,
    ConfigurationError,
    CyberArkClient,
    CyberArkError,
    collect_targets,
    extract_file_categories,
    filter_accounts,
    regex_filter_accounts,
    split_values,
)


class OverrideAppendAction(argparse.Action):
    """Append repeatable CLI values while replacing configured defaults."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str,
        option_string: str | None = None,
    ) -> None:
        marker = f"_{self.dest}_set_on_cli"
        current = list(getattr(namespace, self.dest, []) or [])
        if not getattr(namespace, marker, False):
            current = []
            setattr(namespace, marker, True)
        current.append(values)
        setattr(namespace, self.dest, current)


class InsecureAction(argparse.Action):
    """Enable insecure TLS and clear a configured CA bundle."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, nargs=0, **kwargs)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        namespace.insecure = True
        namespace.ca_bundle = None


class CaBundleAction(argparse.Action):
    """Set a CA bundle and override configured insecure TLS mode."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str,
        option_string: str | None = None,
    ) -> None:
        namespace.ca_bundle = values
        namespace.insecure = False


def environment_or(name: str, configured: Any, builtin: Any = None) -> Any:
    """Apply environment-over-file precedence while preserving falsey values."""

    value = os.getenv(name)
    return value if value is not None else configured if configured is not None else builtin


def info(message: str) -> None:
    print(f"[INFO] {message}")


def success(message: str) -> None:
    print(f"[OK]   {message}")


def warning(message: str) -> None:
    print(f"[WARN] {message}", file=sys.stderr)


def add_target_arguments(parser: argparse.ArgumentParser, defaults: dict[str, Any]) -> None:
    targets = parser.add_argument_group("target selection")
    targets.add_argument(
        "-H", "--hostname", default=defaults.get("hostname"), help="one account address/hostname"
    )
    targets.add_argument(
        "-f", "--file", default=defaults.get("file"), help="file containing addresses/hostnames"
    )
    configured_platforms = defaults.get("platforms")
    env_platforms = os.getenv("CYBERARK_PLATFORMS")
    platform_default = [env_platforms] if env_platforms is not None else configured_platforms
    targets.add_argument(
        "--platform",
        action=OverrideAppendAction,
        default=platform_default,
        help="allowed platform ID; repeat or comma-separate (or set CYBERARK_PLATFORMS)",
    )
    targets.add_argument(
        "--all-platforms",
        action="store_true",
        help="deliberately disable platform filtering",
    )
    targets.add_argument(
        "-m",
        "--match",
        default=defaults.get("match"),
        help="regular expression applied after selection",
    )
    targets.add_argument(
        "--field",
        choices=tuple(FIELD_MAP),
        default=defaults.get("field", "user"),
        help="account field used by --match (default: user)",
    )
    targets.add_argument(
        "--include-domain-users",
        action=argparse.BooleanOptionalAction,
        default=defaults.get("include_domain_users", False),
        help="include usernames containing '@' (local accounts only by default)",
    )


def add_mutation_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--execute",
        action="store_true",
        help="apply changes; without this flag the command is a preview",
    )


def build_parser(
    config: dict[str, Any] | None = None,
    config_path: str | None = None,
) -> argparse.ArgumentParser:
    config = config or {}
    connection = config.get("connection", {})
    login_password_env = (
        "CYBERARK_LOGIN_PASSWORD"
        if os.getenv("CYBERARK_LOGIN_PASSWORD") is not None
        else connection.get("login_password_env")
    )
    parser = argparse.ArgumentParser(
        description="CyberArk PVWA account administration toolkit.",
    )
    parser.add_argument(
        "--config",
        default=config_path,
        help="TOML config file (or CYBERARK_CONFIG)",
    )
    parser.add_argument(
        "--url",
        default=environment_or("CYBERARK_PVWA_URL", connection.get("url")),
        help="PVWA base URL (or CYBERARK_PVWA_URL)",
    )
    parser.add_argument(
        "-u",
        "--username",
        default=environment_or("CYBERARK_USERNAME", connection.get("username")),
        help="PVWA username (or CYBERARK_USERNAME; prompt if omitted)",
    )
    parser.add_argument(
        "--auth-type",
        default=environment_or("CYBERARK_AUTH_TYPE", connection.get("auth_type"), "LDAP"),
        help="PVWA authentication method (default: LDAP)",
    )
    parser.add_argument(
        "--login-password-env",
        metavar="NAME",
        default=login_password_env,
        help="environment variable holding the logon password",
    )
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument(
        "-k",
        "--insecure",
        action=InsecureAction,
        default=connection.get("insecure", False),
        help="disable TLS verification",
    )
    tls.add_argument(
        "--ca-bundle",
        action=CaBundleAction,
        default=connection.get("ca_bundle"),
        help="CA bundle for TLS verification",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=connection.get("timeout", 45),
        help="HTTP timeout in seconds",
    )

    commands = parser.add_subparsers(dest="command", required=True)

    list_defaults = command_defaults(config, "list")
    list_parser = commands.add_parser("list", help="list selected accounts")
    add_target_arguments(list_parser, list_defaults)
    list_parser.add_argument(
        "--activity-category",
        default=list_defaults.get("activity_category", "DiscoveryPlatformType"),
        help="file category to read from account activities; empty disables lookup",
    )
    list_parser.add_argument(
        "--activities-limit", type=int, default=list_defaults.get("activities_limit", 200)
    )
    list_parser.set_defaults(handler=command_list)

    password_defaults = command_defaults(config, "update-password")
    password_parser = commands.add_parser(
        "update-password", help="set one account user's Vault-only password"
    )
    add_target_arguments(password_parser, password_defaults)
    add_mutation_argument(password_parser)
    password_parser.add_argument(
        "--account-user",
        default=password_defaults.get("account_user"),
        required="account_user" not in password_defaults,
        help="exact username to update on each address",
    )
    password_parser.add_argument(
        "--new-password-env",
        metavar="NAME",
        default=password_defaults.get("new_password_env"),
        help="read the new Vault password from this environment variable",
    )
    password_parser.set_defaults(handler=command_update_password)

    policy_defaults = command_defaults(config, "set-ssh-key-policy")
    policy_parser = commands.add_parser(
        "set-ssh-key-policy",
        help="change platform and disable automatic management based on an activity category",
    )
    add_target_arguments(policy_parser, policy_defaults)
    add_mutation_argument(policy_parser)
    policy_parser.add_argument(
        "--target-platform",
        default=policy_defaults.get("target_platform"),
        required="target_platform" not in policy_defaults,
    )
    policy_parser.add_argument(
        "--activity-category",
        default=policy_defaults.get("activity_category", "DiscoveryPlatformType"),
    )
    policy_parser.add_argument(
        "--activity-value", default=policy_defaults.get("activity_value", "Unix SSH Key")
    )
    policy_parser.add_argument(
        "--reason", default=policy_defaults.get("reason", "SSH key management is disabled")
    )
    policy_parser.add_argument(
        "--activities-limit", type=int, default=policy_defaults.get("activities_limit", 200)
    )
    policy_parser.set_defaults(handler=command_set_ssh_key_policy)

    link_defaults = command_defaults(config, "link-reconcile")
    link_parser = commands.add_parser(
        "link-reconcile", help="link a per-address reconciliation account"
    )
    add_target_arguments(link_parser, link_defaults)
    add_mutation_argument(link_parser)
    link_parser.add_argument(
        "--reconcile-user",
        default=link_defaults.get("reconcile_user"),
        required="reconcile_user" not in link_defaults,
        help="exact username of the reconciliation account",
    )
    link_parser.add_argument(
        "--index", type=int, default=link_defaults.get("index", 3), help="extra password index"
    )
    link_parser.add_argument(
        "--default-folder", default=link_defaults.get("default_folder", "Root")
    )
    link_parser.set_defaults(handler=command_link_reconcile)

    rotate_defaults = command_defaults(config, "rotate-reconcile")
    rotate_parser = commands.add_parser(
        "rotate-reconcile",
        help="set a random Vault value and ask CPM to reconcile each selected account",
    )
    add_target_arguments(rotate_parser, rotate_defaults)
    add_mutation_argument(rotate_parser)
    rotate_parser.add_argument(
        "--exclude-user",
        action=OverrideAppendAction,
        default=rotate_defaults.get("exclude_users", []),
        help="exact username to skip; repeatable",
    )
    rotate_parser.add_argument(
        "--length",
        type=int,
        default=rotate_defaults.get("length", 30),
        help="random password length",
    )
    rotate_parser.set_defaults(handler=command_rotate_reconcile)

    return parser


def platforms_from_args(args: argparse.Namespace, *, mutation: bool) -> list[str]:
    if args.all_platforms:
        return []
    values = list(args.platform or [])
    if not values and os.getenv("CYBERARK_PLATFORMS"):
        values = [os.environ["CYBERARK_PLATFORMS"]]
    platforms = split_values(values)
    if mutation and not platforms:
        raise ConfigurationError(
            "Mutation commands require --platform (or CYBERARK_PLATFORMS). "
            "Use --all-platforms only when that broad scope is intentional."
        )
    return platforms


def selected_accounts(
    client: CyberArkClient,
    host: str,
    args: argparse.Namespace,
    platforms: list[str],
    *,
    usernames: list[str] | None = None,
    excluded_usernames: list[str] | None = None,
) -> list[dict[str, Any]]:
    accounts = filter_accounts(
        client.search_accounts(host),
        address=host,
        platform_ids=platforms,
        usernames=usernames,
        excluded_usernames=excluded_usernames,
        local_only=not args.include_domain_users,
    )
    return regex_filter_accounts(accounts, args.match, args.field)


def print_account(account: dict[str, Any], *, prefix: str = "  ") -> None:
    print(
        f"{prefix}{account.get('id')} | {account.get('userName')} | "
        f"{account.get('address')} | {account.get('safeName')} | {account.get('platformId')}"
    )


def command_list(client: CyberArkClient, args: argparse.Namespace) -> int:
    platforms = platforms_from_args(args, mutation=False)
    total = 0
    failures: list[str] = []
    for host in collect_targets(args.hostname, args.file):
        accounts = selected_accounts(client, host, args, platforms)
        info(f"{host}: {len(accounts)} account(s)")
        total += len(accounts)
        for account in accounts:
            print_account(account)
            if args.activity_category:
                try:
                    categories = extract_file_categories(
                        client.get_activities(account["id"], limit=args.activities_limit)
                    )
                    print(
                        f"    {args.activity_category}: "
                        f"{categories.get(args.activity_category, 'N/A')}"
                    )
                except CyberArkError as exc:
                    failures.append(f"{account.get('id')}: {exc}")
                    warning(failures[-1])
    success(f"Found {total} account(s).")
    return 2 if failures else 0


def read_secret_from_env_or_prompt(variable: str | None, prompt: str) -> str:
    if variable:
        value = os.getenv(variable)
        if value:
            return value
    try:
        value = getpass.getpass(prompt)
    except EOFError as exc:
        raise ConfigurationError(
            "PVWA password is required; interactive input is unavailable"
        ) from exc
    if not value:
        raise ConfigurationError("Password must not be empty")
    return value


def read_username_or_prompt(value: str | None) -> str:
    """Return a configured username or request one interactively."""

    username = (value or "").strip()
    if username:
        return username
    try:
        username = input("PVWA username: ").strip()
    except EOFError as exc:
        raise ConfigurationError(
            "PVWA username is required; interactive input is unavailable"
        ) from exc
    if not username:
        raise ConfigurationError("PVWA username must not be empty")
    return username


def command_update_password(client: CyberArkClient, args: argparse.Namespace) -> int:
    platforms = platforms_from_args(args, mutation=True)
    targets: list[dict[str, Any]] = []
    for host in collect_targets(args.hostname, args.file):
        targets.extend(
            selected_accounts(client, host, args, platforms, usernames=[args.account_user])
        )
    for account in targets:
        print_account(account)
    if not args.execute:
        success(f"Preview: {len(targets)} account(s) would receive a Vault-only password update.")
        return 0
    new_password = read_secret_from_env_or_prompt(
        args.new_password_env, "New Vault password: "
    )
    return perform_per_account(
        targets,
        lambda account: client.update_password(account["id"], new_password),
        "Vault password updated",
    )


def command_set_ssh_key_policy(client: CyberArkClient, args: argparse.Namespace) -> int:
    platforms = platforms_from_args(args, mutation=True)
    planned: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    failures: list[str] = []
    for host in collect_targets(args.hostname, args.file):
        for account in selected_accounts(client, host, args, platforms):
            try:
                categories = extract_file_categories(
                    client.get_activities(account["id"], limit=args.activities_limit)
                )
                if categories.get(args.activity_category) != args.activity_value:
                    continue
                detail = client.get_account(account["id"])
                management = detail.get("secretManagement") or {}
                operations: list[dict[str, Any]] = []
                if detail.get("platformId") != args.target_platform:
                    operations.append(
                        {
                            "op": "replace",
                            "path": "/platformId",
                            "value": args.target_platform,
                        }
                    )
                if management.get("automaticManagementEnabled") is not False:
                    operations.append(
                        {
                            "op": "replace",
                            "path": "/secretManagement/automaticManagementEnabled",
                            "value": False,
                        }
                    )
                if management.get("manualManagementReason") != args.reason:
                    operations.append(
                        {
                            "op": "replace",
                            "path": "/secretManagement/manualManagementReason",
                            "value": args.reason,
                        }
                    )
                if operations:
                    planned.append((account, operations))
            except CyberArkError as exc:
                failures.append(f"{account.get('id')}: {exc}")
                warning(failures[-1])
    for account, operations in planned:
        print_account(account)
        for operation in operations:
            print(f"    {operation['path']} = {operation['value']!r}")
    if not args.execute:
        success(f"Preview: {len(planned)} account(s) would be patched.")
        return 2 if failures else 0
    operations_by_id = {account["id"]: operations for account, operations in planned}
    result = perform_per_account(
        [item[0] for item in planned],
        lambda account: client.patch_account(account["id"], operations_by_id[account["id"]]),
        "account policy updated",
    )
    return 2 if failures or result else 0


def find_reconcile_account(
    client: CyberArkClient,
    host: str,
    username: str,
    platforms: list[str],
    default_folder: str,
) -> dict[str, str]:
    matches = filter_accounts(
        client.search_accounts(host),
        address=host,
        platform_ids=platforms,
        usernames=[username],
        local_only=False,
    )
    if len(matches) != 1:
        raise ConfigurationError(
            f"{host}: expected exactly one reconciliation account for {username!r}; "
            f"found {len(matches)}"
        )
    account = matches[0]
    detail = client.get_account(account["id"], include_internal=True)
    safe = detail.get("safeName") or account.get("safeName")
    name = detail.get("name") or account.get("name")
    if not safe or not name:
        raise CyberArkError(f"{host}: reconciliation account lacks safeName or name")
    return {
        "id": str(account["id"]),
        "safe": str(safe),
        "name": str(name),
        "folder": str(detail.get("folder") or detail.get("location") or default_folder),
    }


def command_link_reconcile(client: CyberArkClient, args: argparse.Namespace) -> int:
    platforms = platforms_from_args(args, mutation=True)
    planned: list[tuple[dict[str, Any], dict[str, str]]] = []
    failures: list[str] = []
    for host in collect_targets(args.hostname, args.file):
        try:
            reconcile = find_reconcile_account(
                client, host, args.reconcile_user, platforms, args.default_folder
            )
            accounts = selected_accounts(
                client,
                host,
                args,
                platforms,
                excluded_usernames=[args.reconcile_user],
            )
            planned.extend((account, reconcile) for account in accounts)
        except CyberArkError as exc:
            failures.append(str(exc))
            warning(str(exc))
    for account, reconcile in planned:
        print_account(account)
        print(f"    reconcile -> {reconcile['name']} ({reconcile['safe']}/{reconcile['folder']})")
    if not args.execute:
        success(f"Preview: {len(planned)} account link(s) would be created.")
        return 2 if failures else 0
    reconcile_by_id = {account["id"]: reconcile for account, reconcile in planned}
    result = perform_per_account(
        [item[0] for item in planned],
        lambda account: _link_account(
            client, account, reconcile_by_id[account["id"]], args.index
        ),
        "reconciliation account linked",
    )
    return 2 if failures or result else 0


def _link_account(
    client: CyberArkClient,
    account: dict[str, Any],
    reconcile: dict[str, str],
    index: int,
) -> None:
    client.link_account(
        account["id"],
        safe=reconcile["safe"],
        name=reconcile["name"],
        folder=reconcile["folder"],
        extra_password_index=index,
    )


def command_rotate_reconcile(client: CyberArkClient, args: argparse.Namespace) -> int:
    if args.length < 12:
        raise ConfigurationError("--length must be at least 12")
    platforms = platforms_from_args(args, mutation=True)
    excluded = split_values(args.exclude_user)
    accounts: list[dict[str, Any]] = []
    for host in collect_targets(args.hostname, args.file):
        accounts.extend(
            selected_accounts(
                client, host, args, platforms, excluded_usernames=excluded
            )
        )
    for account in accounts:
        print_account(account)
    if not args.execute:
        success(f"Preview: {len(accounts)} account(s) would be rotated and reconciled.")
        return 0

    alphabet = string.ascii_letters + string.digits

    def rotate(account: dict[str, Any]) -> None:
        password = "".join(secrets.choice(alphabet) for _ in range(args.length))
        client.update_password(account["id"], password)
        client.reconcile(account["id"])

    return perform_per_account(accounts, rotate, "Vault password updated; reconcile triggered")


def perform_per_account(
    accounts: list[dict[str, Any]],
    operation: Callable[[dict[str, Any]], None],
    success_message: str,
) -> int:
    completed = 0
    failures: list[str] = []
    for account in accounts:
        try:
            operation(account)
            completed += 1
            success(f"{account['id']}: {success_message}")
        except CyberArkError as exc:
            failures.append(f"{account.get('id')}: {exc}")
            warning(failures[-1])
    success(f"Completed {completed}/{len(accounts)} operation(s).")
    return 2 if failures else 0


def main(argv: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", default=os.getenv("CYBERARK_CONFIG"))
    config_args, _ = config_parser.parse_known_args(raw_arguments)
    try:
        config = load_config(config_args.config)
    except (CyberArkError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    parser = build_parser(config, config_args.config)
    args = parser.parse_args(raw_arguments)
    try:
        if not args.url:
            raise ConfigurationError("Set --url, CYBERARK_PVWA_URL, or [connection].url")
        if args.insecure and args.ca_bundle:
            raise ConfigurationError("insecure and ca_bundle are mutually exclusive")
        verify: bool | str = args.ca_bundle or not args.insecure
        username = read_username_or_prompt(args.username)
        password = read_secret_from_env_or_prompt(
            args.login_password_env, "PVWA password: "
        )
        with CyberArkClient(args.url, verify=verify, timeout=args.timeout) as client:
            info(f"Authenticating with {args.auth_type}...")
            client.login(username, password, args.auth_type)
            success("Authenticated.")
            return int(args.handler(client, args))
    except (CyberArkError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
