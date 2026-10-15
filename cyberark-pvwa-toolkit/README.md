# CyberArk PVWA Toolkit

This directory contains a reusable PVWA client and command-line program for
common account-administration workflows.

## Requirements

- Python 3.9 or newer (the default Python runtime in RHEL 9)
- Network access to the PVWA API
- A PVWA user with the permissions required by the selected operation

On RHEL 9, install Python and create a virtual environment:

```bash
sudo dnf install python3 python3-pip
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --constraint constraints-rhel9.txt .
```

`constraints-rhel9.txt` pins the Python 3.9-compatible HTTP dependency set so
pip does not need to backtrack through releases that have dropped Python 3.9.
For a development installation, include the pinned linting extra:

```bash
python3 -m pip install --constraint constraints-rhel9.txt '.[dev]'
```

## Configuration

Configuration can come from a TOML file, environment variables, or command-line
arguments. The precedence from highest to lowest is:

1. Command-line arguments
2. Environment variables
3. TOML configuration
4. Built-in defaults

Copy [`pvwa-toolkit.example.toml`](pvwa-toolkit.example.toml) to a private location and
adjust it for the environment. Relative host-list and CA-bundle paths are
resolved relative to the config file, not the current working directory.

```bash
cp pvwa-toolkit.example.toml pvwa-toolkit.toml
pvwa-toolkit --config ./pvwa-toolkit.toml list
```

Alternatively, select the file once for the current shell:

```bash
export CYBERARK_CONFIG=/etc/cyberark-pvwa-toolkit/config.toml
pvwa-toolkit list
```

Connection values can also be stored directly in environment variables:

```bash
export CYBERARK_PVWA_URL='https://vault.example/PasswordVault'
export CYBERARK_USERNAME='operator@example.com'
export CYBERARK_AUTH_TYPE='LDAP'
export CYBERARK_PLATFORMS='UnixSSH,UnixSSHKeys'
```

`CYBERARK_PLATFORMS` is a comma-separated allowlist. Mutation commands refuse
to run without a platform allowlist unless `--all-platforms` is explicitly
provided. TLS certificates are verified by default; use `--ca-bundle` for a
private CA. `--insecure` should only be used for controlled troubleshooting.

Configuration files reject unknown options, plaintext password fields, and the
`execute` option. Mutation therefore remains a deliberate command-line action.
Passwords are prompted without echo. For non-interactive jobs, point
`--login-password-env` and (where applicable) `--new-password-env` at named
environment variables. Passwords are intentionally not accepted directly as
arguments because process command lines are often logged or visible to other
users.

## Commands

The global connection options come before the subcommand. Run
`pvwa-toolkit --help` or `pvwa-toolkit <command> --help` for the complete
interface.

List local accounts and show their discovery platform type:

```bash
pvwa-toolkit list --hostname linux01.example.com
pvwa-toolkit list --file hosts.txt --match '^app_' --field user
```

Preview a Vault-only password update for one account name on each host, then
apply the reviewed change:

```bash
pvwa-toolkit update-password --file hosts.txt --account-user reconcile_svc
pvwa-toolkit update-password --file hosts.txt --account-user reconcile_svc --execute
```

Preview and apply a platform/management-policy change for discovered SSH keys:

```bash
pvwa-toolkit set-ssh-key-policy --file hosts.txt --target-platform UnixSSHKeys
pvwa-toolkit set-ssh-key-policy --file hosts.txt --target-platform UnixSSHKeys --execute
```

Link a reconciliation account to the other selected accounts on each host:

```bash
pvwa-toolkit link-reconcile --file hosts.txt --reconcile-user reconcile_svc
pvwa-toolkit link-reconcile --file hosts.txt --reconcile-user reconcile_svc --execute
```

Set a unique random Vault value and trigger CPM reconciliation:

```bash
pvwa-toolkit rotate-reconcile --file hosts.txt --exclude-user reconcile_svc
pvwa-toolkit rotate-reconcile --file hosts.txt --exclude-user reconcile_svc --execute
```

All mutation commands are previews unless `--execute` is present. Selection
always performs an exact, case-insensitive address match after the PVWA search,
and excludes usernames containing `@` unless `--include-domain-users` is used.
The process exits with `0` on success, `1` for configuration or session errors,
and `2` when one or more per-account operations fail.

## Testing

The unit tests use fake HTTP sessions and never connect to a Vault:

```bash
python3 -m unittest discover -s tests -v
```

