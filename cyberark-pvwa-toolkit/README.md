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

`constraints-rhel9.txt` pins the Python 3.9-compatible dependency set so
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
5. Interactive credential prompts

Copy [`pvwa-toolkit.example.toml`](pvwa-toolkit.example.toml) to a private location and
adjust it for the environment. Relative host-list and CA-bundle paths are
resolved relative to the config file, not the current working directory.

```bash
cp pvwa-toolkit.example.toml pvwa-toolkit.toml
pvwa-toolkit --config ./pvwa-toolkit.toml accounts -H host01 list
```

Alternatively, select the file once for the current shell:

```bash
export CYBERARK_CONFIG=/etc/cyberark-pvwa-toolkit/config.toml
pvwa-toolkit accounts -f hosts.txt list
```

Connection values can also be stored directly in environment variables:

```bash
export CYBERARK_PVWA_URL='https://vault.example/PasswordVault'
export CYBERARK_USERNAME='operator@example.com'
export CYBERARK_AUTH_TYPE='LDAP'
# CYBERARK_LOGIN_PASSWORD may be populated by a secret manager for unattended use.
```

Named connection profiles are selected with `--profile` or `default_profile`.
Connection options are accepted anywhere on the command line. TLS certificates
are verified by default; use `--ca-bundle` for a private CA. `--insecure`
should only be used for controlled troubleshooting.

Configuration files reject unknown options, plaintext password fields, and the
`execute` option. Mutation therefore remains a deliberate command-line action.
When credentials are not configured, the CLI prompts for the PVWA username and
prompts for the password without echo. `CYBERARK_LOGIN_PASSWORD` is detected
automatically for unattended use. A different secret variable can be selected
with `--login-password-env` or `login_password_env` in a named connection
profile. The `set-password --password-env NAME` option provides the same
indirection for a Vault password. Passwords are intentionally not accepted
directly as arguments or configuration values because process command lines
and files are often logged or broadly readable.

## Account commands

The resource-oriented command form places selectors before the action:

```text
pvwa-toolkit [connection options] accounts [selectors] ACTION [action options]
```

`-H/--hostname/--address` and `-f/--file/--address-file` form one address
category. Explicit values and every file are unioned and de-duplicated. Values
inside another category are also ORed; different categories are ANDed:

```bash
pvwa-toolkit accounts \
  -H host01 -f additional-hosts.txt \
  --account root --account oracle \
  --platform UnixSSH --platform UnixSSHKeys \
  list
```

Use `--where FIELD REGEX` for ANDed regex rules and `--where-any FIELD REGEX`
for a single OR group:

```bash
pvwa-toolkit accounts -f hosts.txt \
  --where user '^svc_' --where safe '^Linux-Prod$' list
```

All `--where-any` occurrences belong to one OR group, which is ANDed with exact
selectors, every `--where` rule, and activity selectors:

```bash
pvwa-toolkit accounts -f hosts.txt \
  --where safe '^Linux-Prod$' \
  --where-any user '^svc_' --where-any address '^db' list
```

Activity file categories are selectors. `--dpt` is an alias for the commonly
used `DiscoveryPlatformType` category:

```bash
pvwa-toolkit accounts -f hosts.txt --dpt 'Unix SSH Key' list
pvwa-toolkit accounts -f hosts.txt \
  --activity AccountType 'Operating System' \
  --activity-match DiscoveryPlatformType '^Unix .*Key$' list
```

Available actions are `list`, `set-password`, `copy-password`, `link-account`,
`change-platform`, `move-safe`, `set-management`, and `reconcile`. Mutations
are previews unless `--execute` is present and require an address or account-ID
scope unless `--all-accounts` is explicit.

`move-safe` is reserved in the grammar but execution is intentionally blocked
until a supported safe-move adapter is selected for the target PVWA version; the
toolkit does not emulate it with a destructive retrieve/delete/recreate sequence.

Password behavior is always explicit:

```bash
pvwa-toolkit accounts -f hosts.txt --account reconcile_svc \
  set-password --mode prompt-shared --execute

pvwa-toolkit accounts -f hosts.txt --account root \
  set-password --mode random-distinct --length 30 --execute
```

The modes are `prompt-shared`, `prompt-distinct`, `random-shared`,
`random-distinct`, and `environment-shared`. Prompted passwords are confirmed;
generated passwords and prompts occur only during execution and are never
written to normal output.

Copying a password uses a separate, exactly-one-account source selector:

```bash
pvwa-toolkit accounts -f hosts.txt --account local_admin \
  copy-password --source-id SOURCE_ACCOUNT_ID --execute
```

To link one reconciliation account per destination address, combine its account
name with `--source-same-address`. The toolkit resolves every source before it
changes any account and requires exactly one match on each address:

```bash
pvwa-toolkit accounts -f hosts.txt \
  link-account \
  --role reconcile \
  --source-account svc_reconcile \
  --source-same-address
```

The command is a preview until `--execute` is added. Reconciliation links use
extra-password index `3` by default; override it with `--index` when needed.

## Playbooks

YAML playbooks provide ordered account actions. See
[`account-playbook.example.yml`](account-playbook.example.yml). Runtime CLI
selectors supply or narrow playbook selectors, so inventories do not need to be
embedded in the playbook:

Playbook keys use the same hyphenated style as CLI commands and options. Python
identifiers are normalized internally and are not part of the playbook format:

```yaml
version: 1
required-selectors: [addresses]
tasks:
  - name: Link reconciliation accounts
    select:
      where:
        - field: user
          regex: "^(root|oracle)$"
    actions:
      - link-account:
          role: reconcile
          source:
            account: svc_reconcile
            same-address: true
```

```bash
pvwa-toolkit --config ./pvwa-toolkit.toml playbook \
  -f production-hosts.txt \
  run account-playbook.example.yml

pvwa-toolkit playbook -f production-hosts.txt \
  run account-playbook.example.yml --execute
```

A selector category present only on the CLI is supplied to every task. A
category present only in the playbook is retained. When both provide the same
category, their values are intersected. Playbooks can declare required
categories such as `addresses`; validation occurs before any account mutation.
Each task resolves and freezes its account IDs before its ordered actions run.

The process exits with `0` on success, `1` for configuration or session errors,
and `2` when one or more per-account operations fail.

## Testing

The unit tests use fake HTTP sessions and never connect to a Vault:

```bash
python3 -m unittest discover -s tests -v
```

## Maintainer

Adérito Alves (`adalvesr`)

## License

Licensed under the MIT License. See [LICENSE](../LICENSE).

