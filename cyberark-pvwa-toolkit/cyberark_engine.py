"""Execution engine shared by resource commands and YAML playbooks."""

from __future__ import annotations

import getpass
import os
import secrets
import string
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from cyberark_playbook import ActionSpec, TaskSpec
from cyberark_selectors import AccountSelector, SelectorSpec
from cyberark_utils import FIELD_MAP, ConfigurationError, CyberArkClient, CyberArkError


MUTATING_ACTIONS = {
    "set_password",
    "copy_password",
    "link_account",
    "change_platform",
    "move_safe",
    "set_management",
    "reconcile",
}


def _info(message: str) -> None:
    print(f"[INFO] {message}")


def _success(message: str) -> None:
    print(f"[OK]   {message}")


def _warning(message: str) -> None:
    print(f"[WARN] {message}")


class ExecutionEngine:
    def __init__(
        self,
        client: CyberArkClient,
        *,
        info: Callable[[str], None] = _info,
        success: Callable[[str], None] = _success,
        warning: Callable[[str], None] = _warning,
    ) -> None:
        self.client = client
        self.selector = AccountSelector(client)
        self.info = info
        self.success = success
        self.warning = warning

    def run_tasks(
        self,
        tasks: Iterable[TaskSpec],
        *,
        runtime_selector: SelectorSpec | None = None,
        required_selectors: Iterable[str] = (),
        execute: bool = False,
    ) -> int:
        failed = False
        runtime = runtime_selector or SelectorSpec()
        for task in tasks:
            effective = task.selector.merged_with(runtime)
            for category in required_selectors:
                if not effective.has_category(category):
                    raise ConfigurationError(
                        f"Task {task.name!r} requires the {category} selector"
                    )
            if any(action.name in MUTATING_ACTIONS for action in task.actions):
                if not effective.has_mutation_scope():
                    raise ConfigurationError(
                        f"Task {task.name!r} mutates accounts and requires addresses, "
                        "account IDs, or all_accounts: true"
                    )
            self.info(f"Task: {task.name}")
            accounts = self.selector.resolve(effective)
            self.info(f"Frozen target set: {len(accounts)} account(s)")
            if len(task.actions) != 1 or task.actions[0].name != "list":
                for account in accounts:
                    self._print_account(account)
            for action in task.actions:
                if self._run_action(action, accounts, execute=execute):
                    failed = True
        return 2 if failed else 0

    @staticmethod
    def _print_account(account: Mapping[str, Any]) -> None:
        print(
            f"  {account.get('id')} | {account.get('userName')} | "
            f"{account.get('address')} | {account.get('safeName')} | "
            f"{account.get('platformId')}"
        )

    def _run_action(
        self, action: ActionSpec, accounts: list[dict[str, Any]], *, execute: bool
    ) -> bool:
        handlers = {
            "list": self._list,
            "set_password": self._set_password,
            "copy_password": self._copy_password,
            "link_account": self._link_account,
            "change_platform": self._change_platform,
            "move_safe": self._move_safe,
            "set_management": self._set_management,
            "reconcile": self._reconcile,
        }
        return handlers[action.name](accounts, action.options, execute)

    def _list(
        self, accounts: list[dict[str, Any]], options: dict[str, Any], _execute: bool
    ) -> bool:
        self._validate_options(options, {"fields"}, "list")
        fields = options.get("fields", ["id", "user", "address", "safe", "platform"])
        if not isinstance(fields, list) or not all(field in FIELD_MAP for field in fields):
            raise ConfigurationError("list.fields must contain supported field names")
        for account in accounts:
            print(" | ".join(str(account.get(FIELD_MAP[field]) or "") for field in fields))
        self.success(f"Listed {len(accounts)} account(s).")
        return False

    def _set_password(
        self, accounts: list[dict[str, Any]], options: dict[str, Any], execute: bool
    ) -> bool:
        self._validate_options(options, {"mode", "length", "password_env"}, "set_password")
        mode = options.get("mode")
        modes = {
            "prompt-shared",
            "prompt-distinct",
            "random-shared",
            "random-distinct",
            "environment-shared",
        }
        if mode not in modes:
            raise ConfigurationError(
                "set_password.mode must be prompt-shared, prompt-distinct, "
                "random-shared, random-distinct, or environment-shared"
            )
        length = options.get("length", 30)
        if not isinstance(length, int) or isinstance(length, bool) or length < 12:
            raise ConfigurationError("set_password.length must be an integer of at least 12")
        if not execute:
            self.success(
                f"Preview: set password on {len(accounts)} account(s) using {mode}."
            )
            return False

        shared: str | None = None
        if mode == "prompt-shared":
            shared = self._prompt_password("New shared Vault password: ")
        elif mode == "random-shared":
            shared = self._random_password(length)
        elif mode == "environment-shared":
            variable = options.get("password_env")
            if not isinstance(variable, str) or not variable:
                raise ConfigurationError(
                    "environment-shared requires set_password.password_env"
                )
            shared = os.getenv(variable)
            if not shared:
                raise ConfigurationError(f"Environment variable {variable!r} is unset or empty")

        def password_for(account: Mapping[str, Any]) -> str:
            if shared is not None:
                return shared
            if mode == "prompt-distinct":
                label = f"{account.get('userName')}@{account.get('address')}"
                return self._prompt_password(f"New Vault password for {label}: ")
            return self._random_password(length)

        return self._per_account(
            accounts,
            lambda account: self.client.update_password(
                str(account["id"]), password_for(account)
            ),
            "Vault password updated",
        )

    def _copy_password(
        self, accounts: list[dict[str, Any]], options: dict[str, Any], execute: bool
    ) -> bool:
        self._validate_options(options, {"source", "reason"}, "copy_password")
        source = self._resolve_source(options.get("source"), "copy_password")
        self.info(
            f"Password source: {source.get('id')} | {source.get('userName')} | "
            f"{source.get('address')} | {source.get('safeName')}"
        )
        destinations = [item for item in accounts if str(item.get("id")) != str(source["id"])]
        if not execute:
            self.success(f"Preview: copy password to {len(destinations)} account(s).")
            return False
        reason = options.get("reason", "")
        if not isinstance(reason, str):
            raise ConfigurationError("copy_password.reason must be a string")
        password = self.client.retrieve_password(str(source["id"]), reason=reason)
        return self._per_account(
            destinations,
            lambda account: self.client.update_password(str(account["id"]), password),
            "Vault password copied",
        )

    def _link_account(
        self, accounts: list[dict[str, Any]], options: dict[str, Any], execute: bool
    ) -> bool:
        self._validate_options(options, {"role", "source", "index", "folder"}, "link_account")
        role = options.get("role")
        if role not in {"logon", "reconcile"}:
            raise ConfigurationError("link_account.role must be logon or reconcile")
        index = options.get("index", 1 if role == "logon" else 3)
        if not isinstance(index, int) or isinstance(index, bool) or index <= 0:
            raise ConfigurationError("link_account.index must be a positive integer")
        source = self._resolve_source(options.get("source"), "link_account")
        detail = self.client.get_account(str(source["id"]), include_internal=True)
        safe = detail.get("safeName") or source.get("safeName")
        name = detail.get("name") or source.get("name")
        folder = detail.get("folder") or detail.get("location") or options.get("folder", "Root")
        if not safe or not name:
            raise ConfigurationError("Linked account must have a safe and object name")
        destinations = [item for item in accounts if str(item.get("id")) != str(source["id"])]
        if not execute:
            self.success(
                f"Preview: link {role} account {source['id']} to "
                f"{len(destinations)} account(s)."
            )
            return False
        return self._per_account(
            destinations,
            lambda account: self.client.link_account(
                str(account["id"]),
                safe=str(safe),
                name=str(name),
                folder=str(folder),
                extra_password_index=index,
            ),
            f"{role} account linked",
        )

    def _change_platform(
        self, accounts: list[dict[str, Any]], options: dict[str, Any], execute: bool
    ) -> bool:
        self._validate_options(options, {"platform"}, "change_platform")
        platform = options.get("platform")
        if not isinstance(platform, str) or not platform:
            raise ConfigurationError("change_platform.platform is required")
        targets = [item for item in accounts if item.get("platformId") != platform]
        return self._patch_value(
            targets, "/platformId", platform, "platform changed", execute
        )

    def _move_safe(
        self, accounts: list[dict[str, Any]], options: dict[str, Any], execute: bool
    ) -> bool:
        self._validate_options(options, {"safe"}, "move_safe")
        safe = options.get("safe")
        if not isinstance(safe, str) or not safe:
            raise ConfigurationError("move_safe.safe is required")
        raise ConfigurationError(
            "move_safe is reserved but not executable: moving an account between safes "
            "is not a supported Accounts PATCH operation in the validated API"
        )

    def _set_management(
        self, accounts: list[dict[str, Any]], options: dict[str, Any], execute: bool
    ) -> bool:
        self._validate_options(options, {"enabled", "reason"}, "set_management")
        enabled = options.get("enabled")
        if not isinstance(enabled, bool):
            raise ConfigurationError("set_management.enabled must be a boolean")
        reason = options.get("reason", "")
        if not isinstance(reason, str):
            raise ConfigurationError("set_management.reason must be a string")
        planned: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for account in accounts:
            detail = self.client.get_account(str(account["id"]))
            management = detail.get("secretManagement") or {}
            operations: list[dict[str, Any]] = []
            if management.get("automaticManagementEnabled") is not enabled:
                operations.append(
                    {
                        "op": "replace",
                        "path": "/secretManagement/automaticManagementEnabled",
                        "value": enabled,
                    }
                )
            if reason and management.get("manualManagementReason") != reason:
                operations.append(
                    {
                        "op": "replace",
                        "path": "/secretManagement/manualManagementReason",
                        "value": reason,
                    }
                )
            if operations:
                planned.append((account, operations))
        if not execute:
            self.success(f"Preview: update management on {len(planned)} account(s).")
            return False
        operations = {str(account["id"]): ops for account, ops in planned}
        return self._per_account(
            [account for account, _ in planned],
            lambda account: self.client.patch_account(
                str(account["id"]), operations[str(account["id"])]
            ),
            "management settings changed",
        )

    def _reconcile(
        self, accounts: list[dict[str, Any]], options: dict[str, Any], execute: bool
    ) -> bool:
        self._validate_options(options, set(), "reconcile")
        if not execute:
            self.success(f"Preview: reconcile {len(accounts)} account(s).")
            return False
        return self._per_account(
            accounts,
            lambda account: self.client.reconcile(str(account["id"])),
            "reconcile triggered",
        )

    def _patch_value(
        self,
        accounts: list[dict[str, Any]],
        path: str,
        value: Any,
        message: str,
        execute: bool,
    ) -> bool:
        if not execute:
            self.success(f"Preview: patch {len(accounts)} account(s): {path} = {value!r}.")
            return False
        return self._per_account(
            accounts,
            lambda account: self.client.patch_account(
                str(account["id"]), [{"op": "replace", "path": path, "value": value}]
            ),
            message,
        )

    def _resolve_source(self, value: Any, action: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ConfigurationError(f"{action}.source must be a selector mapping")
        allowed = {"id", "account", "address", "platform", "safe"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ConfigurationError(
                f"Unknown source option(s) for {action}: {', '.join(unknown)}"
            )
        if not value:
            raise ConfigurationError(f"{action}.source must specify an account")
        source_spec = SelectorSpec(
            ids=self._one_value(value.get("id")),
            accounts=self._one_value(value.get("account")),
            addresses=self._one_value(value.get("address")),
            platforms=self._one_value(value.get("platform")),
            safes=self._one_value(value.get("safe")),
            include_domain_users=True,
            all_accounts=True,
        )
        matches = self.selector.resolve(source_spec)
        if len(matches) != 1:
            raise ConfigurationError(
                f"{action} source must resolve to exactly one account; found {len(matches)}"
            )
        return matches[0]

    @staticmethod
    def _one_value(value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, str) or not value:
            raise ConfigurationError("Source selector values must be non-empty strings")
        return [value]

    @staticmethod
    def _validate_options(options: Mapping[str, Any], allowed: set[str], action: str) -> None:
        unknown = sorted(set(options) - allowed)
        if unknown:
            raise ConfigurationError(
                f"Unknown option(s) for {action}: {', '.join(unknown)}"
            )

    @staticmethod
    def _prompt_password(prompt: str) -> str:
        try:
            first = getpass.getpass(prompt)
            second = getpass.getpass("Confirm password: ")
        except EOFError as exc:
            raise ConfigurationError("Interactive password input is unavailable") from exc
        if not first:
            raise ConfigurationError("Password must not be empty")
        if first != second:
            raise ConfigurationError("Password confirmation does not match")
        return first

    @staticmethod
    def _random_password(length: int) -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def _per_account(
        self,
        accounts: Iterable[dict[str, Any]],
        operation: Callable[[dict[str, Any]], None],
        message: str,
    ) -> bool:
        failed = False
        for account in accounts:
            try:
                operation(account)
                self.success(f"{account['id']}: {message}")
            except CyberArkError as exc:
                failed = True
                self.warning(f"{account.get('id')}: {exc}")
        return failed
