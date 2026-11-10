# Sequential Content View publication

This script targets Foreman 3.13 / Katello 4.15 and uses the `hammer`
configuration already present on the Foreman server. It requires no external
Python packages.

## Behavior

- Discovers Content Views and component relationships directly from Katello.
  No Content View or composite names are embedded in the script.
- When a non-composite Content View is requested, includes the composites that
  depend on it and all components required by those composites.
- When a composite is requested, includes all of its dependencies without
  pulling in sibling composites that merely share a component.
- Publishes all non-composite Content Views before any composite.
- Publishes and promotes one Content View at a time. The script never uses
  `--async`; the next operation starts only after Hammer confirms that the
  previous task has finished.
- Requires lifecycle environments as command-line arguments. Their names and
  promotion order are never embedded in the code.
- Disables `auto-publish` on selected composites before starting. This is an
  intentional, persistent change because an automatic publication could start
  before all component publications finish and violate the requested order.
- A component configured with "Always update to the latest" automatically uses
  the newly published version. A component pinned to a fixed version is updated
  to the version created during the run before the composite is published, and
  remains in fixed-version mode.
- Stops at the first error. Katello does not roll back operations that already
  completed successfully.

## Requirements

- `hammer` and `hammer-cli-katello` versions compatible with Foreman/Katello.
- Working Hammer authentication, such as an existing
  `~/.hammer/cli.modules.d/foreman.yml` configuration.
- Permissions to view, publish, promote, update composite Content Views, and
  update their components.
- A lifecycle path that permits promotion through the environments supplied on
  the command line. The script does not use `--force` to bypass lifecycle or
  Katello safety checks.

## Usage

Make the script executable:

```bash
chmod +x publish_content_views.py
```

Always inspect the plan first:

```bash
./publish_content_views.py \
  --organization "My Organization" \
  --environment NonProd \
  --environment Prod \
  --dry-run \
  AgentsRH9
```

Remove `--dry-run` to execute the plan. Separate arguments and comma-separated
values are both accepted:

```bash
./publish_content_views.py \
  --organization "My Organization" \
  --environment NonProd \
  --environment Prod \
  AgentsRH9,BaseRHEL9 EPEL_RHEL9
```

Requesting a composite automatically discovers its components:

```bash
./publish_content_views.py \
  --organization "My Organization" \
  --environment NonProd \
  --environment Prod \
  RHEL9
```

Read Content View names from a file. Blank lines and lines beginning with `#`
are ignored:

```bash
./publish_content_views.py \
  --organization "My Organization" \
  --environment NonProd \
  --environment Prod \
  --from-file content-views.txt
```

Repeat `--environment` in the exact promotion order required by your lifecycle
path. Any names can be used:

```bash
./publish_content_views.py \
  --organization "My Organization" \
  --environment Test \
  --environment Acceptance \
  --environment Production \
  RHEL9
```

Apply one description to every version created during the run:

```bash
./publish_content_views.py \
  --organization "My Organization" \
  --environment NonProd \
  --environment Prod \
  --description "Monthly patch cycle 2026-11" \
  RHEL9 Rocky9
```

Use `--verbose` to display Hammer commands and their output.
`--keep-auto-publish` is intended only for installations that deliberately
manage that concurrency elsewhere; it is not recommended for this workflow.

## Operational safety

Always begin with `--dry-run`. The plan shows the final ordering and all
discovered dependencies. Publication and promotion are not transactional. If a
later step fails, fix the underlying problem before running the script again to
avoid creating unnecessary extra versions.
