"""TOML connection-profile loading and validation for the CyberArk CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.9 and 3.10
    import tomli as tomllib

from cyberark_utils import ConfigurationError


Validator = Callable[[Any], bool]


def _is_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


CONNECTION_SCHEMA: dict[str, Validator] = {
    "url": _is_string,
    "username": _is_string,
    "auth_type": _is_string,
    "login_password_env": _is_string,
    "insecure": _is_bool,
    "ca_bundle": _is_string,
    "timeout": _is_number,
}


def _validate_connection(name: str, value: Any) -> dict[str, Any]:
    section = f"connections.{name}"
    if not isinstance(value, dict):
        raise ConfigurationError(f"Config section [{section}] must be a table")
    unknown = sorted(set(value) - set(CONNECTION_SCHEMA))
    if unknown:
        raise ConfigurationError(
            f"Unknown option(s) in [{section}]: {', '.join(unknown)}"
        )
    for key, item in value.items():
        if not CONNECTION_SCHEMA[key](item):
            raise ConfigurationError(f"Invalid value for [{section}].{key}: {item!r}")
    if value.get("insecure") and value.get("ca_bundle"):
        raise ConfigurationError(f"[{section}].insecure and ca_bundle are mutually exclusive")
    return dict(value)


def _resolve_path(value: str, directory: Path) -> str:
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (directory / path).resolve())


def load_config(path: str | Path | None) -> dict[str, Any]:
    """Load, validate, and normalize named TOML connection profiles."""

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

    unknown_top = sorted(set(raw) - {"default_profile", "connections"})
    if unknown_top:
        raise ConfigurationError(f"Unknown top-level config section(s): {', '.join(unknown_top)}")

    default_profile = raw.get("default_profile")
    if default_profile is not None and not _is_string(default_profile):
        raise ConfigurationError("default_profile must be a non-empty string")
    raw_connections = raw.get("connections", {})
    if not isinstance(raw_connections, dict):
        raise ConfigurationError("Config section [connections] must be a table")

    connections: dict[str, dict[str, Any]] = {}
    for name, values in raw_connections.items():
        if not _is_string(name):
            raise ConfigurationError("Connection profile names must be non-empty strings")
        connections[name] = _validate_connection(name, values)
    if default_profile is not None and default_profile not in connections:
        raise ConfigurationError(
            f"default_profile references unknown connection profile {default_profile!r}"
        )

    base_directory = config_path.parent
    for profile in connections.values():
        if "ca_bundle" in profile:
            profile["ca_bundle"] = _resolve_path(profile["ca_bundle"], base_directory)

    return {
        "path": str(config_path),
        "default_profile": default_profile,
        "connections": connections,
    }


def connection_defaults(
    config: dict[str, Any], profile: str | None = None
) -> tuple[str | None, dict[str, Any]]:
    """Return the explicitly selected or default named connection profile."""

    selected = profile or config.get("default_profile")
    if selected is None:
        return None, {}
    connections = config.get("connections", {})
    if selected not in connections:
        raise ConfigurationError(f"Unknown connection profile: {selected}")
    return str(selected), dict(connections[selected])
