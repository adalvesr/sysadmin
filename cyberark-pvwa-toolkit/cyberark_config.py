"""TOML configuration loading and validation for the CyberArk CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

from cyberark_utils import ConfigurationError, FIELD_MAP


Validator = Callable[[Any], bool]


def _is_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_is_string(item) for item in value)


def _is_field(value: Any) -> bool:
    return isinstance(value, str) and value in FIELD_MAP


def _is_string_or_string_list(value: Any) -> bool:
    return _is_string(value) or _is_string_list(value)


def _is_field_or_field_list(value: Any) -> bool:
    return _is_field(value) or (
        isinstance(value, list) and bool(value) and all(_is_field(item) for item in value)
    )


CONNECTION_SCHEMA: dict[str, Validator] = {
    "url": _is_string,
    "username": _is_string,
    "auth_type": _is_string,
    "login_password_env": _is_string,
    "insecure": _is_bool,
    "ca_bundle": _is_string,
    "timeout": _is_number,
}

SELECTION_SCHEMA: dict[str, Validator] = {
    "hostname": _is_string,
    "file": _is_string,
    "platforms": _is_string_list,
    "match": _is_string_or_string_list,
    "field": _is_field_or_field_list,
    "include_domain_users": _is_bool,
}

COMMAND_SCHEMAS: dict[str, dict[str, Validator]] = {
    "list": {
        "activity_category": lambda value: isinstance(value, str),
        "activities_limit": _is_positive_int,
    },
    "update-password": {
        "account_user": _is_string,
        "new_password_env": _is_string,
    },
    "set-ssh-key-policy": {
        "target_platform": _is_string,
        "activity_category": _is_string,
        "activity_value": _is_string,
        "reason": _is_string,
        "activities_limit": _is_positive_int,
    },
    "link-reconcile": {
        "reconcile_user": _is_string,
        "index": _is_positive_int,
        "default_folder": _is_string,
    },
    "rotate-reconcile": {
        "exclude_users": _is_string_list,
        "length": _is_positive_int,
    },
}


def _validate_section(
    section_name: str,
    value: Any,
    schema: dict[str, Validator],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"Config section [{section_name}] must be a table")
    unknown = sorted(set(value) - set(schema))
    if unknown:
        raise ConfigurationError(
            f"Unknown option(s) in [{section_name}]: {', '.join(unknown)}"
        )
    for key, item in value.items():
        if not schema[key](item):
            raise ConfigurationError(
                f"Invalid value for [{section_name}].{key}: {item!r}"
            )
    return dict(value)


def _resolve_path(value: str, directory: Path) -> str:
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (directory / path).resolve())


def load_config(path: str | Path | None) -> dict[str, Any]:
    """Load, validate, and normalize a TOML configuration file."""

    if path is None:
        return {}
    config_path = Path(path).expanduser().resolve()
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Config file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"Invalid TOML in {config_path}: {exc}") from exc

    unknown_top = sorted(set(raw) - {"connection", "selection", "commands"})
    if unknown_top:
        raise ConfigurationError(f"Unknown top-level config section(s): {', '.join(unknown_top)}")

    connection = _validate_section(
        "connection", raw.get("connection", {}), CONNECTION_SCHEMA
    )
    if connection.get("insecure") and connection.get("ca_bundle"):
        raise ConfigurationError(
            "[connection].insecure and [connection].ca_bundle are mutually exclusive"
        )
    selection = _validate_section("selection", raw.get("selection", {}), SELECTION_SCHEMA)
    raw_commands = raw.get("commands", {})
    if not isinstance(raw_commands, dict):
        raise ConfigurationError("Config section [commands] must be a table")
    unknown_commands = sorted(set(raw_commands) - set(COMMAND_SCHEMAS))
    if unknown_commands:
        raise ConfigurationError(f"Unknown command config(s): {', '.join(unknown_commands)}")

    commands: dict[str, dict[str, Any]] = {}
    for name, values in raw_commands.items():
        schema = {**SELECTION_SCHEMA, **COMMAND_SCHEMAS[name]}
        commands[name] = _validate_section(f"commands.{name}", values, schema)

    base_directory = config_path.parent
    if "ca_bundle" in connection:
        connection["ca_bundle"] = _resolve_path(connection["ca_bundle"], base_directory)
    if "file" in selection:
        selection["file"] = _resolve_path(selection["file"], base_directory)
    for values in commands.values():
        if "file" in values:
            values["file"] = _resolve_path(values["file"], base_directory)

    return {
        "path": str(config_path),
        "connection": connection,
        "selection": selection,
        "commands": commands,
    }


def command_defaults(config: dict[str, Any], command: str) -> dict[str, Any]:
    """Merge shared selection settings with command-specific settings."""

    return {
        **config.get("selection", {}),
        **config.get("commands", {}).get(command, {}),
    }
