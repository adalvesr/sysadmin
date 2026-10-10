#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=gss-nsupdate
source "$SCRIPT_DIR/gss-nsupdate"

TEST_ROOT=$(mktemp -d)
cleanup() {
    rm -f -- \
        "$TEST_ROOT/config/defaults.conf" \
        "$TEST_ROOT/config/10-primary.conf" \
        "$TEST_ROOT/config/20-secondary.conf" \
        "$TEST_ROOT/config/30-all.conf" \
        "$TEST_ROOT/config/40-additional.conf" \
        "$TEST_ROOT/config/50-explicit.conf" \
        "$TEST_ROOT/keytab" \
        "$TEST_ROOT/log" \
        "$TEST_ROOT/kinit.calls" \
        "$TEST_ROOT/nsupdate.calls"
    rmdir -- "$TEST_ROOT/config" "$TEST_ROOT" 2>/dev/null || true
}
trap cleanup EXIT
mkdir -p "$TEST_ROOT/config"
touch "$TEST_ROOT/keytab" "$TEST_ROOT/log"

cat > "$TEST_ROOT/config/defaults.conf" <<EOF
LOG_FILE='$TEST_ROOT/log'
KEYTAB_FILE='$TEST_ROOT/keytab'
DNS_SERVERS=(dc1.example.invalid dc2.example.invalid)
KINIT_EXTRA_ARGS=(-V)
RETRY_DELAY=0
EOF

cat > "$TEST_ROOT/config/10-primary.conf" <<'EOF'
RECORD_FQDN=host.example.invalid
ADDRESS_MODE=primary
NIC=ens192
EOF

cat > "$TEST_ROOT/config/20-secondary.conf" <<'EOF'
RECORD_FQDN=service.example.invalid
ADDRESS_MODE=secondary
NIC=ens192
ADDRESS_INDEX=0
EOF

cat > "$TEST_ROOT/config/30-all.conf" <<'EOF'
RECORD_FQDN=all.example.invalid
ADDRESS_MODE=all
NIC=ens192
EOF

cat > "$TEST_ROOT/config/40-additional.conf" <<'EOF'
RECORD_FQDN=additional.example.invalid
ADDRESS_MODE=additional
NIC=ens192
EOF

cat > "$TEST_ROOT/config/50-explicit.conf" <<'EOF'
RECORD_FQDN=explicit.example.invalid
ADDRESS_MODE=explicit
NIC=does-not-exist
ADDRESSES=(198.51.100.40 198.51.100.50)
EOF

date() {
    printf '2026-01-01 00:00:00+0000\n'
}

hostname() {
    [[ ${1:-} == -f ]] && printf 'host.example.invalid\n'
}

ip() {
    case "$*" in
        '-4 route get 192.0.2.1 oif ens192')
            printf '192.0.2.1 via 192.0.2.254 dev ens192 src 192.0.2.10\n'
            ;;
        'link show dev ens192')
            printf '2: ens192: <UP>\n'
            ;;
        '-o -4 addr show dev ens192 scope global')
            printf '2: ens192 inet 192.0.2.10/24 scope global ens192\n'
            printf '2: ens192 inet 192.0.2.20/24 scope global secondary ens192\n'
            printf '2: ens192 inet 192.0.2.30/24 scope global secondary ens192\n'
            ;;
        *) return 1 ;;
    esac
}

kinit() {
    printf '%s\n' "$*" >> "$TEST_ROOT/kinit.calls"
}

kdestroy() {
    return 0
}

nsupdate() {
    local payload
    payload=$(cat)
    printf '%s\n---\n' "$payload" >> "$TEST_ROOT/nsupdate.calls"
    [[ $payload != *'server dc1.example.invalid'* ]]
}

sleep() {
    return 0
}

CONFIG_DIR="$TEST_ROOT/config"
main

grep -q -- '-V -k -t .*keytab HOST\$@EXAMPLE.INVALID' "$TEST_ROOT/kinit.calls"
grep -q -- 'update add host.example.invalid. 3600 A 192.0.2.10' "$TEST_ROOT/nsupdate.calls"
grep -q -- 'update add 10.2.0.192.in-addr.arpa. 3600 PTR host.example.invalid.' "$TEST_ROOT/nsupdate.calls"
grep -q -- 'update add service.example.invalid. 3600 A 192.0.2.20' "$TEST_ROOT/nsupdate.calls"
grep -q -- 'update add 20.2.0.192.in-addr.arpa. 3600 PTR service.example.invalid.' "$TEST_ROOT/nsupdate.calls"
grep -q -- 'update add all.example.invalid. 3600 A 192.0.2.10' "$TEST_ROOT/nsupdate.calls"
grep -q -- 'update add all.example.invalid. 3600 A 192.0.2.20' "$TEST_ROOT/nsupdate.calls"
grep -q -- 'update add all.example.invalid. 3600 A 192.0.2.30' "$TEST_ROOT/nsupdate.calls"
grep -q -- 'update add additional.example.invalid. 3600 A 192.0.2.20' "$TEST_ROOT/nsupdate.calls"
grep -q -- 'update add additional.example.invalid. 3600 A 192.0.2.30' "$TEST_ROOT/nsupdate.calls"
if grep -q -- 'update add additional.example.invalid. 3600 A 192.0.2.10' "$TEST_ROOT/nsupdate.calls"; then
    printf 'Primary address unexpectedly included in additional mode.\n' >&2
    exit 1
fi
grep -q -- 'update add explicit.example.invalid. 3600 A 198.51.100.40' "$TEST_ROOT/nsupdate.calls"
grep -q -- 'update add explicit.example.invalid. 3600 A 198.51.100.50' "$TEST_ROOT/nsupdate.calls"
grep -q -- 'server dc2.example.invalid' "$TEST_ROOT/nsupdate.calls"

printf 'All gss-nsupdate tests passed.\n'
