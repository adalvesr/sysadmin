# rhel-patching

`rhel-patching` is a shell-based toolkit for running controlled patch waves on
RHEL-family systems. It combines explicit DNF repository selection with classic
LVM snapshots and a best-effort volume-group reservation, giving administrators
a consistent way to prepare, patch, validate, and clean up a host.

The toolkit supports RHEL, Rocky Linux, AlmaLinux, and Oracle Linux releases 8,
9, and 10. RHEL PAYG systems with enabled RHUI repositories are detected
automatically, while manually selected and site-provided repository profiles
remain fully supported.

> [!IMPORTANT]
> LVM snapshots are not backups, and this project does not automate snapshot
> merging or application-consistent rollback. Test the complete patch and
> recovery procedure on representative systems before using it in production.

## Features

- Automatic distribution, major-release, architecture, and RHEL RHUI detection.
- Shipped BaseOS and AppStream profiles for supported RHEL-family platforms.
- Extensible site profiles without a hardcoded profile allowlist.
- Optional, disabled-by-default examples for EPEL and Oracle UEK.
- Dry-run support for repository updates and both LVM lifecycle operations.
- Classic LVM snapshot preparation with health, age, utilization, ownership,
  and patch-wave validation.
- Configurable snapshot, reservation, and LVM ownership-tag names.
- Best-effort reservation recovery after snapshot cleanup, including when an
  individual snapshot removal fails.
- Shared locking across snapshot creation, package updates, and cleanup.
- Safe installer that preserves locally modified configuration.
- Mock-based regression tests for platform detection and failure handling.

## Supported repository profiles

| Platform | Standard profiles | PAYG profile |
| --- | --- | --- |
| Red Hat Enterprise Linux | `rhel-8`, `rhel-9`, `rhel-10` | `rhel-payg-8`, `rhel-payg-9`, `rhel-payg-10` |
| Rocky Linux | `rocky-8`, `rocky-9`, `rocky-10` | Use a site profile if required |
| AlmaLinux | `alma-8`, `alma-9`, `alma-10` | Use a site profile if required |
| Oracle Linux | `oracle-8`, `oracle-9`, `oracle-10` | Use a site profile if required |

The standard profiles intentionally enable only core operating-system content.
EUS, E4S, SAP, ULN, Satellite aliases, internal mirrors, and provider-specific
repositories must be selected explicitly.

## Requirements

- A supported RHEL-family release using DNF.
- Bash 4.3 or newer.
- Root access for installation and operational commands.
- LVM2 tools, `findmnt`, `flock`, `wipefs`, RPM, and standard GNU utilities.
- Classic thick LVM logical volumes for configured snapshot paths.
- Sufficient VG capacity for the requested snapshots.
- The `dm-snapshot` driver in the initramfs when boot-time recovery depends on
  snapshot support.

Thin LVs are detected and rejected during preflight. Thin-pool snapshot support
is not currently implemented.

## Installation

Review the plan before installing:

```bash
sudo bash ./install.sh --dry-run
sudo bash ./install.sh
```

The installer places:

- operational scripts in `/usr/local/sbin`;
- patching configuration in `/etc/patching`;
- `dmsnapshot.conf` in `/etc/dracut.conf.d`.

Existing configuration is never silently replaced. When an installed file
differs from the packaged version, the installer preserves it and writes the
new version with a `.dist` suffix. Review and merge those files, or deliberately
replace installed configuration with:

```bash
sudo bash ./install.sh --force-config
```

The initramfs is rebuilt only when explicitly requested:

```bash
sudo bash ./install.sh --rebuild-initramfs
```

For packaging or inspection, stage the complete filesystem layout without root
privileges:

```bash
bash ./install.sh --root /tmp/rhel-patching-stage
```

`--rebuild-initramfs` cannot be combined with a staging root.

## Quick start

Review `/etc/patching/repos-config` and `/etc/patching/lvm-config` before the
first wave. Then use the following sequence.

### 1. Preview and create snapshots

```bash
sudo create-lvm-snapshot.sh --dry-run
sudo create-lvm-snapshot.sh
```

Verify snapshot origins, state, utilization, and tags:

```bash
sudo lvs -a -o vg_name,lv_name,origin,lv_attr,data_percent,lv_time,lv_tags
```

### 2. Preview and apply package updates

```bash
sudo system-update.sh --dry-run
sudo system-update.sh
```

The preview uses `dnf check-update`. DNF status `100`, meaning updates are
available, is treated as a successful preview. Preview mode may refresh metadata
but does not run a package transaction or explicitly delete caches.

### 3. Reboot and validate

Reboot when required, then complete the host's functional validation. Do not
remove the snapshots if rollback may still be needed.

### 4. Preview and remove snapshots

After accepting the patched system:

```bash
sudo remove-lvm-snapshot.sh --dry-run
sudo remove-lvm-snapshot.sh
```

Cleanup removes owned snapshots, recomputes actual VG free space, and creates or
grows the largest reservation that fits. A shortfall is reported as a warning.
If one snapshot removal fails, the script still finalizes reservations and then
returns failure so automation can detect incomplete cleanup.

## Repository configuration

`system-update.sh` loads configuration in this order:

1. `/etc/patching/repos-config`
2. the selected file in `/etc/patching/repos-profiles.d`
3. sorted `*.conf` files in `/etc/patching/repos-config.d`

The default setting is:

```bash
REPO_PROFILE="auto"
```

Auto mode reads `/etc/os-release`. On RHEL, it selects a PAYG profile only when
enabled RHUI repositories are found. Disabled RHUI definitions do not cause a
BYOS system to be classified as PAYG.

### Force a shipped profile

```bash
REPO_PROFILE="rhel-payg-9"
```

### Add a site profile

Create a safely named profile such as:

```text
/etc/patching/repos-profiles.d/corp-rhel-9.conf
```

```bash
ENABLED_REPOS+=(corp-baseos corp-appstream)
```

Then select it by basename:

```bash
REPO_PROFILE="corp-rhel-9"
```

Profile names may contain letters, digits, underscores, periods, and hyphens,
and must start with a letter or digit. Directory separators are rejected.

### Manual repository mode

Use manual mode for internal mirrors, Satellite aliases, ULN channels, or other
custom layouts:

```bash
REPO_PROFILE="manual"
ENABLED_REPOS=(
  corp-baseos
  corp-appstream
)
```

Manual mode fails unless at least one repository is configured.

### Optional repositories

EPEL is shipped as
`/etc/patching/repos-config.d/10-epel.conf.disabled`. Rename it to end in
`.conf` only when the EPEL repository definition is installed and its use is
intentional.

The Oracle profiles enable only BaseOS and AppStream. Disabled examples are
provided for:

- Oracle Linux 8: `ol8_UEKR7`
- Oracle Linux 9: `ol9_UEKR8`
- Oracle Linux 10: `ol10_UEKR8`

Enable only the UEK stream appropriate for the host.

## LVM configuration

`/etc/patching/lvm-config` defines the snapshot paths, names, ownership tags,
and reuse thresholds. Important defaults include:

```bash
LV_SNAP_PATHS=(
  "/"
  "/usr"
  "/var"
)

LVM_SNAPSHOT_NAME_PREFIX="patching-"
LVM_SNAPSHOT_NAME_SUFFIX=""
LVM_RESERVATION_LV_NAME="patch-reserved"

LVM_RESERVATION_LV_TAG="patching-reservation"
LVM_SNAPSHOT_LV_TAG="patching-snapshot"

LVM_MAX_SNAPSHOT_AGE_HOURS=48
LVM_MAX_SNAPSHOT_DATA_PERCENT=90
```

Paths resolving to the same LV are deduplicated. Configured paths must exist,
resolve to classic LVM LVs, and must not be thin volumes.

### Snapshot validation

An existing snapshot is reused only when it:

- has the configured ownership tag;
- belongs to the same patch wave as other existing snapshots;
- has the expected origin;
- is active and valid;
- is below the configured data-usage threshold;
- is not older than the configured maximum.

Set `LVM_MAX_SNAPSHOT_AGE_HOURS=0` to disable only the age check. State,
ownership, origin, and utilization checks remain mandatory.

### Reservation behavior

The reservation LV is capacity held aside to reduce the chance that organic
filesystem growth prevents a later patch wave. It is not treated as a strict
quota.

Snapshot preparation calculates capacity only for missing snapshots. Available
capacity is current VG free space plus the exact size of an owned reservation,
if present. A missing or smaller reservation is allowed when the VG still has
enough capacity, with a warning where appropriate.

A reservation is modified only when it:

- carries the configured ownership tag;
- is not a configured origin LV or a snapshot;
- is not mounted;
- has no filesystem signature.

LVM capacity is calculated in bytes, with conservative handling of display
markers such as `<` and `>`.

### Migrating legacy untagged LVs

Snapshots and reservations created by an older version are refused by default.
After independently verifying their identity, temporarily enable the applicable
migration switches:

```bash
LVM_ADOPT_UNTAGGED_RESERVATION=1
LVM_ADOPT_UNTAGGED_SNAPSHOTS=1
```

Leave these enabled only for the migration patch wave. Preparation tags legacy
snapshots that it reuses; cleanup removes them and creates or tags the
reservation. Confirm retained LVs with:

```bash
sudo lvs -o vg_name,lv_name,origin,lv_tags
```

Then return both switches to `0`. An untagged reservation is adopted only when
it is unmounted and `wipefs -n` finds no filesystem signature.

## Filesystem and rollback scope

The default snapshot paths protect operating-system state rather than general
application data:

- `/var` primarily protects package-manager state, including YUM/DNF history.
  The RPM database traditionally resides under `/var/lib/rpm`; newer systems
  can use `/usr/lib/sysimage/rpm`, making `/usr` relevant as well.
- `/usr` primarily protects operating-system binaries, libraries, and other
  vendor-supplied files.
- `/` primarily protects `/etc` and root-resident operating-system state. On
  usr-merged systems, paths such as `/lib` can point into `/usr`.

An LVM snapshot contains only the filesystem mounted at its configured path. A
nested filesystem is not part of its parent's snapshot. For example, if
`/var/log` or `/var/lib/mysql` is separately mounted, rolling back `/var` does
not roll back that separate filesystem.

The reverse is equally important: if `/var/log`, `/var/lib/mysql`, or another
changing data directory is not separately mounted, it is part of `/var` and is
rolled back with `/var`. That can revert logs, databases, queues, uploads, or
other application data.

This design assumes that non-OS application data has a dedicated filesystem or
that its rollback with the containing OS filesystem is explicitly accepted.
Before relying on rollback, verify mount boundaries for databases, logs,
message queues, containers, application uploads, and other stateful workloads.

## Safety model

- All operational scripts share `/run/lock/patching/patching.lock` by default.
- Unknown command-line options are rejected; a misspelled `--dry-run` cannot
  fall through to real execution.
- Snapshot preparation completes a read-only preflight before cleaning caches
  or modifying LVM state.
- Creation failures remove snapshots created by that invocation and attempt to
  recreate reservations removed by it at their original size.
- Cleanup revalidates snapshot identity and ownership immediately before every
  destructive removal.
- Installed configuration is preserved unless replacement is explicitly
  requested.

## Logging and test overrides

`system-update.sh` writes timestamped logs under `/var/log/patching`. The LVM
scripts write `/var/log/patching-lvm-snapshots.log` by default.

The following environment variables support staged tests and packaging:

- `PATCHING_CONFIG_ROOT`
- `PATCHING_OS_RELEASE_FILE`
- `PATCHING_LOG_DIR`
- `PATCHING_LOCK_DIR`
- `PATCHING_LVM_CONFIG`
- `PATCHING_LVM_LOGFILE`

Production defaults remain under `/etc`, `/var/log`, and `/run/lock`.

## Testing

Run the regression suite with:

```bash
for test in tests/test-*.sh; do
  bash "$test" || exit
done
```

The tests use mocked DNF and LVM commands. They cover repository detection,
dry-run behavior, strict argument handling, LVM ownership and health checks,
capacity calculations, partial cleanup failure, and staged installation.

Before production rollout, run the dry-runs and a complete create, patch,
reboot, validation, cleanup, and rollback exercise on representative real
systems.

## Project layout

```text
rhel-patching/
├── install.sh
├── scripts/                       operational commands
├── config/
│   ├── patching/                  installed under /etc/patching
│   │   ├── lvm-config
│   │   ├── repos-config
│   │   ├── repos-config.d/
│   │   └── repos-profiles.d/
│   └── dracut.conf.d/             installed under /etc/dracut.conf.d
└── tests/
```

## Known limitations

- Only classic thick LVM snapshots are supported.
- The toolkit does not quiesce databases or applications before snapshotting.
- Snapshot merge and rollback orchestration are not included.
- LVM snapshots are local recovery aids, not durable or off-host backups.
- Repository profiles and LVM behavior must be validated against the actual
  platform, storage layout, and subscription model before broad rollout.

## Author

Created and maintained by [adalvesr](https://github.com/adalvesr/).

Issues and pull requests are welcome after the project is published.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
