#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
DESTDIR=${DESTDIR:-}
INSTALL_ROOT=${DESTDIR%/}
BIN_DIR="${INSTALL_ROOT}/usr/local/sbin"
CONFIG_DIR="${INSTALL_ROOT}/etc/gss-nsupdate"
DOC_DIR="${INSTALL_ROOT}/usr/local/share/doc/gss-nsupdate"
SYSTEMD_DIR="${INSTALL_ROOT}/etc/systemd/system"

usage() {
    cat <<'EOF'
Usage: bash install.sh

Installs gss-nsupdate and its configuration under /usr/local and /etc.
Existing configuration files are preserved.

Set DESTDIR to stage the installation below another directory:
  DESTDIR=/tmp/package-root bash install.sh
EOF
}

if (($# > 0)); then
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
fi

if [[ -n $INSTALL_ROOT && $INSTALL_ROOT != /* ]]; then
    printf 'ERROR: DESTDIR must be an absolute path.\n' >&2
    exit 1
fi

if [[ -z $INSTALL_ROOT && ${EUID:-$(id -u)} -ne 0 ]]; then
    printf 'ERROR: run this installer as root, or set DESTDIR for a staged installation.\n' >&2
    exit 1
fi

for source_file in \
    "$SCRIPT_DIR/gss-nsupdate" \
    "$SCRIPT_DIR/defaults.conf.example" \
    "$SCRIPT_DIR/primary.conf.example" \
    "$SCRIPT_DIR/secondary.conf.example" \
    "$SCRIPT_DIR/gss-nsupdate.service" \
    "$SCRIPT_DIR/gss-nsupdate.timer" \
    "$PROJECT_ROOT/LICENSE" \
    "$SCRIPT_DIR/README.md"; do
    if [[ ! -f $source_file ]]; then
        printf 'ERROR: required source file is missing: %s\n' "$source_file" >&2
        exit 1
    fi
done

install -d -m 0755 "$BIN_DIR" "$CONFIG_DIR" "$DOC_DIR" "$SYSTEMD_DIR"
install -m 0755 "$SCRIPT_DIR/gss-nsupdate" "$BIN_DIR/gss-nsupdate"
install -m 0644 "$SCRIPT_DIR/gss-nsupdate.service" "$SYSTEMD_DIR/gss-nsupdate.service"
install -m 0644 "$SCRIPT_DIR/gss-nsupdate.timer" "$SYSTEMD_DIR/gss-nsupdate.timer"

install -m 0644 "$SCRIPT_DIR/defaults.conf.example" "$CONFIG_DIR/defaults.conf.example"
install -m 0644 "$SCRIPT_DIR/primary.conf.example" "$CONFIG_DIR/primary.conf.example"
install -m 0644 "$SCRIPT_DIR/secondary.conf.example" "$CONFIG_DIR/secondary.conf.example"
install -m 0644 "$PROJECT_ROOT/LICENSE" "$DOC_DIR/LICENSE"
install -m 0644 "$SCRIPT_DIR/README.md" "$DOC_DIR/README.md"

install_if_missing() {
    local source_file=$1 destination=$2
    if [[ -e $destination ]]; then
        printf 'Preserved existing configuration: %s\n' "$destination"
    else
        install -m 0644 "$source_file" "$destination"
        printf 'Created configuration: %s\n' "$destination"
    fi
}

install_if_missing "$SCRIPT_DIR/defaults.conf.example" "$CONFIG_DIR/defaults.conf"
install_if_missing "$SCRIPT_DIR/primary.conf.example" "$CONFIG_DIR/primary.conf"

printf 'Installed executable: %s\n' "$BIN_DIR/gss-nsupdate"
printf 'Installed systemd units: %s/{gss-nsupdate.service,gss-nsupdate.timer}\n' "$SYSTEMD_DIR"

if [[ -z $INSTALL_ROOT ]]; then
    missing_commands=()
    for command_name in ip host kinit kdestroy nsupdate; do
        command -v "$command_name" >/dev/null 2>&1 || missing_commands+=("$command_name")
    done
    if ((${#missing_commands[@]} > 0)); then
        printf 'WARNING: runtime commands not found: %s\n' "${missing_commands[*]}" >&2
    fi
fi

if [[ -z $INSTALL_ROOT ]] && command -v systemctl >/dev/null 2>&1; then
    if ! systemctl daemon-reload; then
        printf 'WARNING: systemd could not be reloaded; run systemctl daemon-reload manually.\n' >&2
    fi
fi

printf 'Installation complete. Review %s before running gss-nsupdate.\n' "$CONFIG_DIR"
if [[ -z $INSTALL_ROOT ]]; then
    printf 'Enable scheduled updates with: systemctl enable --now gss-nsupdate.timer\n'
fi
