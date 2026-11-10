#!/usr/bin/env python3
"""Publish and promote Katello Content Views sequentially.

Target compatibility: Foreman 3.13 / Katello 4.15 / Hammer CLI.
No external Python packages are required; authentication is delegated to Hammer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


class PublishError(RuntimeError):
    """Operational error that must stop the run."""


@dataclass(frozen=True)
class ContentView:
    id: int
    name: str
    composite: bool


@dataclass(frozen=True)
class Component:
    relation_id: int
    content_view_id: int
    follows_latest: bool


@dataclass(frozen=True)
class Environment:
    id: int
    name: str


@dataclass(frozen=True)
class Version:
    id: int
    number: str


def _normalise_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _field(row: dict[str, Any], *names: str) -> Any:
    normalised = {_normalise_key(key): value for key, value in row.items()}
    for name in names:
        key = _normalise_key(name)
        if key in normalised:
            return normalised[key]
    raise PublishError(
        f"Field {names[0]!r} is missing from the Hammer response; "
        f"received fields: {', '.join(map(str, row.keys()))}"
    )


def _as_int(value: Any, field_name: str) -> int:
    match = re.search(r"\d+", str(value))
    if not match:
        raise PublishError(f"Invalid value for {field_name}: {value!r}")
    return int(match.group())


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"true", "yes", "y", "1"}


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and "results" in payload:
        payload = payload["results"]
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise PublishError("Hammer did not return a JSON list in the expected format")
    return payload


class Hammer:
    def __init__(self, executable: str, organization: str, verbose: bool = False):
        self.executable = executable
        self.organization = organization
        self.verbose = verbose

    def _execute(self, arguments: Sequence[str], json_output: bool) -> str:
        command = [self.executable]
        if json_output:
            command += ["--output", "json"]
        command += list(arguments)
        if self.verbose:
            print("+ " + " ".join(command), flush=True)

        environment = os.environ.copy()
        # Hammer JSON column labels are translatable. Force English so the
        # output can be parsed consistently without changing the server locale.
        environment["LC_ALL"] = "C"
        environment["LANG"] = "C"
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        if result.returncode != 0:
            details = "\n".join(
                part.strip() for part in (result.stdout, result.stderr) if part.strip()
            )
            raise PublishError(
                f"Hammer exited with status {result.returncode}: {' '.join(command)}"
                + (f"\n{details}" if details else "")
            )
        return result.stdout

    def json(self, *arguments: str) -> Any:
        output = self._execute(arguments, json_output=True)
        try:
            return json.loads(output)
        except json.JSONDecodeError as error:
            raise PublishError(f"Invalid JSON output from Hammer: {error}\n{output}") from error

    def mutate(self, *arguments: str) -> None:
        # Without --async Hammer waits for the Dynflow task to finish. The next
        # operation is not started until this method returns.
        output = self._execute(arguments, json_output=False).strip()
        if self.verbose and output:
            print(output, flush=True)

    def list_content_views(self) -> list[ContentView]:
        payload = self.json(
            "content-view",
            "list",
            "--organization",
            self.organization,
            "--per-page",
            "all",
        )
        views: list[ContentView] = []
        for row in _rows(payload):
            views.append(
                ContentView(
                    id=_as_int(_field(row, "Content View ID", "Id"), "Content View ID"),
                    name=str(_field(row, "Name")),
                    composite=_as_bool(_field(row, "Composite")),
                )
            )
        return views

    def list_components(self, composite_id: int) -> list[Component]:
        payload = self.json(
            "content-view",
            "component",
            "list",
            "--composite-content-view-id",
            str(composite_id),
            "--organization",
            self.organization,
            "--per-page",
            "all",
        )
        components: list[Component] = []
        for row in _rows(payload):
            version = str(_field(row, "Version")).strip().casefold()
            components.append(
                Component(
                    relation_id=_as_int(_field(row, "Component Id"), "Component Id"),
                    content_view_id=_as_int(
                        _field(row, "Content View Id"), "Content View Id"
                    ),
                    follows_latest=version == "latest",
                )
            )
        return components

    def list_environments(self) -> list[Environment]:
        payload = self.json(
            "lifecycle-environment",
            "list",
            "--organization",
            self.organization,
            "--per-page",
            "all",
        )
        return [
            Environment(
                id=_as_int(_field(row, "ID", "Id"), "Environment ID"),
                name=str(_field(row, "Name")),
            )
            for row in _rows(payload)
        ]

    def list_versions(self, content_view_id: int) -> list[Version]:
        payload = self.json(
            "content-view",
            "version",
            "list",
            "--content-view-id",
            str(content_view_id),
            "--organization",
            self.organization,
            "--per-page",
            "all",
        )
        return [
            Version(
                id=_as_int(_field(row, "Id", "ID"), "Version ID"),
                number=str(_field(row, "Version")),
            )
            for row in _rows(payload)
        ]

    def disable_auto_publish(self, view: ContentView) -> None:
        self.mutate(
            "content-view",
            "update",
            "--id",
            str(view.id),
            "--organization",
            self.organization,
            "--auto-publish",
            "no",
        )

    def publish(self, view: ContentView, description: str) -> None:
        self.mutate(
            "content-view",
            "publish",
            "--id",
            str(view.id),
            "--organization",
            self.organization,
            "--description",
            description,
        )

    def promote(self, version: Version, environment: Environment) -> None:
        self.mutate(
            "content-view",
            "version",
            "promote",
            "--id",
            str(version.id),
            "--organization",
            self.organization,
            "--to-lifecycle-environment-id",
            str(environment.id),
        )

    def pin_component(
        self, composite: ContentView, component: Component, version: Version
    ) -> None:
        self.mutate(
            "content-view",
            "component",
            "update",
            "--id",
            str(component.relation_id),
            "--composite-content-view-id",
            str(composite.id),
            "--component-content-view-version-id",
            str(version.id),
            "--organization",
            self.organization,
        )


def _version_key(version: Version) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version.number)
    return tuple(map(int, parts)) if parts else (0,)


def latest_version(versions: Iterable[Version]) -> Version | None:
    values = list(versions)
    return max(values, key=lambda item: (_version_key(item), item.id), default=None)


class Catalog:
    def __init__(self, views: Iterable[ContentView], components: dict[int, list[Component]]):
        self.by_id = {view.id: view for view in views}
        self.by_name = {view.name: view for view in self.by_id.values()}
        self.components = components
        self.dependents: dict[int, set[int]] = {}
        for composite_id, relations in components.items():
            for relation in relations:
                self.dependents.setdefault(relation.content_view_id, set()).add(composite_id)

    def select(self, requested_names: Sequence[str]) -> set[int]:
        unknown = [name for name in requested_names if name not in self.by_name]
        if unknown:
            raise PublishError("Content View(s) not found: " + ", ".join(unknown))

        roots = [self.by_name[name] for name in requested_names]
        selected = {view.id for view in roots}

        # An explicitly requested non-composite view pulls in its dependent
        # composites. Components included to complete a composite do not pull
        # in unrelated sibling composites.
        queue = [view.id for view in roots if not view.composite]
        while queue:
            component_id = queue.pop(0)
            for composite_id in sorted(self.dependents.get(component_id, set())):
                if composite_id not in selected:
                    selected.add(composite_id)

        # Every selected composite pulls in all of its components.
        queue = [view_id for view_id in selected if self.by_id[view_id].composite]
        visited: set[int] = set()
        while queue:
            composite_id = queue.pop(0)
            if composite_id in visited:
                continue
            visited.add(composite_id)
            for relation in self.components.get(composite_id, []):
                component_id = relation.content_view_id
                if component_id not in self.by_id:
                    raise PublishError(
                        f"Composite ID {composite_id} references unknown Content View ID "
                        f"{component_id}"
                    )
                if component_id not in selected:
                    selected.add(component_id)
                if self.by_id[component_id].composite:
                    queue.append(component_id)
        return selected

    def ordered(self, selected: set[int]) -> list[ContentView]:
        # Kahn topological sort. Names are used only to keep ordering stable.
        dependencies: dict[int, set[int]] = {}
        for view_id in selected:
            dependencies[view_id] = {
                component.content_view_id
                for component in self.components.get(view_id, [])
                if component.content_view_id in selected
            }

        result: list[ContentView] = []
        remaining = set(selected)
        while remaining:
            ready = sorted(
                (view_id for view_id in remaining if not (dependencies[view_id] & remaining)),
                key=lambda view_id: (self.by_id[view_id].composite, self.by_id[view_id].name),
            )
            if not ready:
                names = ", ".join(sorted(self.by_id[item].name for item in remaining))
                raise PublishError(f"Dependency cycle detected between: {names}")
            for view_id in ready:
                result.append(self.by_id[view_id])
                remaining.remove(view_id)
        return result


def parse_requested(values: Sequence[str], file_name: str | None) -> list[str]:
    names: list[str] = []
    for value in values:
        names.extend(part.strip() for part in value.split(",") if part.strip())
    if file_name:
        try:
            lines = Path(file_name).read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise PublishError(f"Could not read {file_name}: {error}") from error
        names.extend(
            line.strip()
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        )
    # Preserve input order while removing duplicates.
    return list(dict.fromkeys(names))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Publish non-composite Content Views before composites and promote them "
            "through the requested environments."
        )
    )
    parser.add_argument(
        "content_views",
        nargs="*",
        metavar="CV",
        help="Content View name(s); comma-separated values are also accepted",
    )
    parser.add_argument(
        "--organization", "-o", required=True, help="exact organization name"
    )
    parser.add_argument(
        "--environment",
        "-e",
        action="append",
        dest="environments",
        required=True,
        help="target environment in promotion order; repeat this option",
    )
    parser.add_argument(
        "--from-file", metavar="FILE", help="read one Content View name per line"
    )
    parser.add_argument("--description", help="description for each new version")
    parser.add_argument(
        "--dry-run", action="store_true", help="show the plan without changing Foreman"
    )
    parser.add_argument(
        "--keep-auto-publish",
        action="store_true",
        help="do not disable auto-publish (not recommended; it can break sequential ordering)",
    )
    parser.add_argument(
        "--hammer", default="hammer", help="Hammer executable (default: hammer)"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def discover(hammer: Hammer) -> Catalog:
    views = hammer.list_content_views()
    if not views:
        raise PublishError("The organization has no Content Views visible to this user")
    components: dict[int, list[Component]] = {}
    for view in views:
        if view.composite:
            components[view.id] = hammer.list_components(view.id)
    return Catalog(views, components)


def resolve_environments(hammer: Hammer, names: Sequence[str]) -> list[Environment]:
    by_name = {environment.name: environment for environment in hammer.list_environments()}
    missing = [name for name in names if name not in by_name]
    if missing:
        raise PublishError("Lifecycle Environment(s) not found: " + ", ".join(missing))
    environments = [by_name[name] for name in names]
    if any(environment.name.casefold() == "library" for environment in environments):
        raise PublishError("Do not specify Library: every publication is already created there")
    return environments


def print_plan(
    ordered: Sequence[ContentView],
    catalog: Catalog,
    environments: Sequence[Environment],
    disable_auto_publish: bool,
) -> None:
    print("\nExecution plan (strictly sequential):")
    composites = [view for view in ordered if view.composite]
    if composites and disable_auto_publish:
        print("  0. Disable auto-publish: " + ", ".join(view.name for view in composites))
    for index, view in enumerate(ordered, start=1):
        kind = "composite" if view.composite else "non-composite"
        dependencies = catalog.components.get(view.id, [])
        suffix = ""
        if dependencies:
            names = [catalog.by_id[item.content_view_id].name for item in dependencies]
            suffix = " [depends on: " + ", ".join(names) + "]"
        print(f"  {index}. Publish {view.name} ({kind}){suffix}")
        for environment in environments:
            print(f"     promote the new version to {environment.name}")


def run(args: argparse.Namespace) -> None:
    requested = parse_requested(args.content_views, args.from_file)
    if not requested:
        raise PublishError("Specify at least one Content View or use --from-file")
    environment_names = args.environments
    if len(set(environment_names)) != len(environment_names):
        raise PublishError("The environment list contains duplicate names")
    if shutil.which(args.hammer) is None:
        raise PublishError(f"Hammer executable not found: {args.hammer}")

    hammer = Hammer(args.hammer, args.organization, args.verbose)
    print(f"Discovering Content Views in organization {args.organization!r}...", flush=True)
    catalog = discover(hammer)
    selected = catalog.select(requested)
    ordered = catalog.ordered(selected)
    environments = resolve_environments(hammer, environment_names)
    print_plan(ordered, catalog, environments, not args.keep_auto_publish)
    if args.dry_run:
        print("\nDry run completed; no changes were made.")
        return

    description = args.description or (
        "Automated publication " + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    composites = [view for view in ordered if view.composite]
    if composites and not args.keep_auto_publish:
        print("\nDisabling auto-publish on selected composites...", flush=True)
        for view in composites:
            print(f"  [{view.name}] auto-publish = no", flush=True)
            hammer.disable_auto_publish(view)

    published_versions: dict[int, Version] = {}
    completed: set[int] = set()
    for view in ordered:
        dependencies = catalog.components.get(view.id, [])
        missing = [
            item.content_view_id
            for item in dependencies
            if item.content_view_id not in completed
        ]
        if missing:
            names = ", ".join(catalog.by_id[item].name for item in missing)
            raise PublishError(
                f"Cannot safely publish {view.name}; incomplete dependencies: {names}"
            )

        if view.composite:
            # Components configured for a fixed version remain fixed, but are
            # explicitly pointed to the version created by this run.
            for component in dependencies:
                if not component.follows_latest:
                    version = published_versions[component.content_view_id]
                    component_name = catalog.by_id[component.content_view_id].name
                    print(
                        f"\n[{view.name}] update fixed component {component_name} "
                        f"to {version.number} (ID {version.id})",
                        flush=True,
                    )
                    hammer.pin_component(view, component, version)

        versions_before = {version.id for version in hammer.list_versions(view.id)}
        print(f"\n[{view.name}] publishing a new version...", flush=True)
        hammer.publish(view, description)
        versions_after = hammer.list_versions(view.id)
        new_versions = [version for version in versions_after if version.id not in versions_before]
        version = latest_version(new_versions)
        if version is None:
            raise PublishError(
                f"Publication of {view.name} finished, but no new version was found"
            )
        print(f"[{view.name}] created version {version.number} (ID {version.id})", flush=True)

        for environment in environments:
            print(f"[{view.name}] promoting to {environment.name}...", flush=True)
            hammer.promote(version, environment)
        published_versions[view.id] = version
        completed.add(view.id)

    print("\nCompleted: all publications and promotions finished successfully.")


def main() -> int:
    parser = build_parser()
    try:
        run(parser.parse_args())
    except PublishError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
