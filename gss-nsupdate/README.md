# gss-nsupdate

`gss-nsupdate` publishes IPv4 A and PTR records to an authoritative DNS service
that accepts Kerberos/GSS-TSIG updates. It obtains credentials from a keytab,
discovers DNS servers through LDAP SRV records, and automatically fails over to
another server when an update cannot be completed.

Each run loads shared settings from `/etc/gss-nsupdate/defaults.conf` and
processes every other `/etc/gss-nsupdate/*.conf` file as an independent DNS
record definition.
A definition can publish the primary address, one secondary address, all
addresses, or all additional addresses from a chosen network interface. It can
also publish an explicit list of IPv4 addresses without requiring them to be
assigned locally.

For multi-address definitions, all selected addresses are written to one A
record set. PTR records are updated separately for each address. A failure in
one definition does not prevent the remaining configuration files from being
processed, and the script exits unsuccessfully if any definition fails.

## Install

```sh
sudo bash install.sh
```

The installer places the executable at `/usr/local/sbin/gss-nsupdate`, creates
`/etc/gss-nsupdate`, installs systemd service and timer units, and installs the
documentation under `/usr/local/share/doc/gss-nsupdate`. It initializes
`defaults.conf` and `primary.conf` when they do not exist. Re-running it updates
the executable, units, and examples while preserving active configuration
files. The timer is not enabled automatically.

Set `DESTDIR` to stage the same filesystem layout for packaging or testing:

```sh
DESTDIR=/tmp/gss-nsupdate-root bash install.sh
```

The runtime dependencies are Bash 4+, `ip`, `host`, `kinit`, `kdestroy`, and
`nsupdate`. Run the script as an account that can read the configured keytab and
write the configured log file.

## systemd

Review the files in `/etc/gss-nsupdate`, then enable scheduled updates:

```sh
sudo systemctl enable --now gss-nsupdate.timer
```

The timer runs the service two minutes after boot and once daily, with up to
thirty seconds of randomized delay. It is persistent, so a daily run missed
while the machine was powered off is started when the machine becomes available
again. The service is a one-shot unit: starting or restarting it executes the
updater immediately.

```sh
sudo systemctl start gss-nsupdate.service
sudo systemctl restart gss-nsupdate.service
sudo systemctl status gss-nsupdate.service
systemctl list-timers gss-nsupdate.timer
```

To use a different schedule, create a timer override with
`sudo systemctl edit gss-nsupdate.timer`. Reset the supplied values before adding
new ones, for example:

```ini
[Timer]
OnBootSec=
OnCalendar=
OnBootSec=5min
OnCalendar=Mon..Fri 03:30
```

Apply unit changes with `sudo systemctl daemon-reload` followed by
`sudo systemctl restart gss-nsupdate.timer`.

## Configuration

Configuration files use Bash assignment syntax. Keep them root-owned and not
writable by untrusted users because the script sources them. `defaults.conf` is
optional; built-in defaults match `defaults.conf.example`. Each other `.conf`
file is isolated, so an override in one record does not affect another.

Address selection supports:

- `ADDRESS_MODE=primary`: preferred source address for `ROUTE_TARGET`; with a
  configured `NIC`, the route lookup is constrained to that interface.
- `ADDRESS_MODE=secondary`: one address, selected by `ADDRESS_INDEX`, among the
  interface's non-primary addresses.
- `ADDRESS_MODE=all`: every IPv4 address configured on the interface.
- `ADDRESS_MODE=additional`: every IPv4 address except the primary address.
- `ADDRESS_MODE=explicit`: one or more addresses from the Bash array
  `ADDRESSES`. Explicit addresses do not need to belong to a local interface.

`NIC` may be set globally or per record. If it is empty, the kernel route to
`ROUTE_TARGET` supplies both the main interface and primary address. In every
mode except `explicit`, selected addresses are guaranteed to come from that NIC.
When several addresses are selected, the script replaces the A-record set in
one update and then updates the PTR record for each address separately.

Kerberos settings are `KEYTAB_FILE`, `KEYTAB_PRINCIPAL`, `KERBEROS_REALM`,
`CREDENTIAL_CACHE`, and the Bash array `KINIT_EXTRA_ARGS`. An empty principal is
derived as `SHORT_HOSTNAME$@REALM`; an empty realm is derived from the local
FQDN. An empty credential cache uses a private temporary cache that is destroyed
after each record.

Set `DNS_SERVERS=(dc1.example.invalid dc2.example.invalid)` to bypass SRV discovery.
Both A and PTR operations independently fail over through the server list.
`UPDATE_A=false` or `UPDATE_PTR=false` disables either operation.

For testing with another directory, set `GSS_NSUPDATE_CONFIG_DIR`; production
uses `/etc/gss-nsupdate` by default.

## Testing

Run `bash test.sh` to exercise all address modes, Kerberos arguments, A/PTR
payloads, and DNS-server failover with mocked commands. The test does not make
network changes.

## License

This project is licensed under the [MIT License](../LICENSE).
