#!/usr/bin/env python3
"""Command-line entry point for CyberArk PVWA account administration."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Any

from cyberark_config import connection_defaults, load_config
from cyberark_utils import ConfigurationError, CyberArkClient, CyberArkError
from cyberark_resources import add_resource_parsers, preload_playbook


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


def build_parser(
    config: dict[str, Any] | None = None,
    config_path: str | None = None,
    profile: str | None = None,
) -> argparse.ArgumentParser:
    """Build the resource-oriented command parser."""

    config = config or {}
    selected_profile, connection = connection_defaults(config, profile)
    login_password_env = (
        "CYBERARK_LOGIN_PASSWORD"
        if os.getenv("CYBERARK_LOGIN_PASSWORD") is not None
        else connection.get("login_password_env")
    )
    parser = argparse.ArgumentParser(
        description="CyberArk PVWA account administration toolkit."
    )
    parser.add_argument(
        "--config",
        default=config_path,
        help="TOML config file (or CYBERARK_CONFIG)",
    )
    parser.add_argument(
        "--profile",
        default=selected_profile,
        help="named connection profile from the TOML config",
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

    resources = parser.add_subparsers(dest="resource", required=True)
    add_resource_parsers(resources)
    return parser


def read_secret_from_env_or_prompt(variable: str | None, prompt: str) -> str:
    """Read a secret from an environment variable, or securely prompt for it."""

    if variable:
        secret = os.getenv(variable)
        if secret:
            return secret
    secret = getpass.getpass(prompt)
    if not secret:
        raise ConfigurationError("Password must not be empty")
    return secret


def read_username_or_prompt(value: str | None) -> str:
    """Return a configured username or prompt when it is missing."""

    username = value.strip() if value else input("PVWA username: ").strip()
    if not username:
        raise ConfigurationError("PVWA username must not be empty")
    return username


_GLOBAL_VALUE_OPTIONS = {
    "--config",
    "--profile",
    "--url",
    "-u",
    "--username",
    "--auth-type",
    "--login-password-env",
    "--ca-bundle",
    "--timeout",
}
_GLOBAL_FLAG_OPTIONS = {"-k", "--insecure"}


def normalize_global_arguments(arguments: list[str]) -> list[str]:
    """Allow connection options before or after resource/action keywords."""

    global_arguments: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in _GLOBAL_VALUE_OPTIONS:
            if index + 1 >= len(arguments):
                raise ConfigurationError(f"Missing value for {argument}")
            global_arguments.extend([argument, arguments[index + 1]])
            index += 2
            continue
        if argument in _GLOBAL_FLAG_OPTIONS or (
            argument.startswith("-u") and not argument.startswith("--") and len(argument) > 2
        ):
            global_arguments.append(argument)
            index += 1
            continue
        if any(
            argument.startswith(f"{option}=")
            for option in _GLOBAL_VALUE_OPTIONS
            if option.startswith("--")
        ):
            global_arguments.append(argument)
            index += 1
            continue
        remaining.append(argument)
        index += 1
    return [*global_arguments, *remaining]


def main(argv: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        normalized_arguments = normalize_global_arguments(raw_arguments)
        config_parser = argparse.ArgumentParser(add_help=False)
        config_parser.add_argument("--config", default=os.getenv("CYBERARK_CONFIG"))
        config_parser.add_argument("--profile")
        config_args, _ = config_parser.parse_known_args(normalized_arguments)
        config = load_config(config_args.config)
        parser = build_parser(config, config_args.config, config_args.profile)
        args = parser.parse_args(normalized_arguments)
        playbook = preload_playbook(args)
        if playbook is not None and playbook.connection and config_args.profile is None:
            parser = build_parser(config, config_args.config, playbook.connection)
            args = parser.parse_args(normalized_arguments)
        if playbook is not None:
            args.loaded_playbook = playbook

        if not args.url:
            raise ConfigurationError(
                "Set --url, CYBERARK_PVWA_URL, or select a connection profile"
            )
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
