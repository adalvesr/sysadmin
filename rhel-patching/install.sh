#!/usr/bin/env bash
set -o errexit
set -o nounset
set -o pipefail

readonly SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEST_ROOT="/"
DRY_RUN=0
FORCE_CONFIG=0
REBUILD_INITRAMFS=0
CONFIG_CONFLICTS=0
DRACUT_CONFIG_CHANGED=0
LAST_INSTALL_CHANGED=0

usage() {
    cat <<'EOF'
Usage: install.sh [options]

Install the RHEL patching scripts and configuration.

Options:
  --root DIR              Stage installation below DIR instead of /
  --dry-run               Print the installation plan without writing files
  --force-config          Overwrite differing installed configuration files
  --rebuild-initramfs     Run dracut -f after installing its configuration
  -h, --help              Show this help

Existing configuration is preserved by default. A differing packaged version
is written beside it with a .dist suffix for review.
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

log() {
    printf '%s\n' "$*"
}

warn() {
    printf 'WARNING: %s\n' "$*" >&2
}

render_command() {
    local rendered
    printf -v rendered '%q ' "$@"
    printf '%s' "${rendered% }"
}

run() {
    if (( DRY_RUN )); then
        log "[DRY-RUN] $(render_command "$@")"
    else
        "$@"
    fi
}

parse_args() {
    while (( $# > 0 )); do
        case "$1" in
            --root)
                (( $# >= 2 )) || die "--root requires a directory"
                DEST_ROOT="$2"
                shift 2
                ;;
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            --force-config)
                FORCE_CONFIG=1
                shift
                ;;
            --rebuild-initramfs)
                REBUILD_INITRAMFS=1
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "Unknown option: $1"
                ;;
        esac
    done
}

normalize_root() {
    [[ "$DEST_ROOT" == /* ]] || die "--root must be an absolute path"
    while [[ "$DEST_ROOT" != "/" && "$DEST_ROOT" == */ ]]; do
        DEST_ROOT="${DEST_ROOT%/}"
    done
    [[ -n "$DEST_ROOT" ]] || die "Invalid installation root"
    if [[ "$DEST_ROOT" == "/" && ${EUID} -ne 0 ]]; then
        die "Installing to / requires root privileges"
    fi
    if [[ "$DEST_ROOT" != "/" && $REBUILD_INITRAMFS -eq 1 ]]; then
        die "--rebuild-initramfs cannot be used with a staging --root"
    fi
}

target_path() {
    local absolute_path="$1"
    [[ "$absolute_path" == /* ]] || die "Internal target path is not absolute: $absolute_path"
    if [[ "$DEST_ROOT" == "/" ]]; then
        printf '%s\n' "$absolute_path"
    else
        printf '%s%s\n' "$DEST_ROOT" "$absolute_path"
    fi
}

ensure_dir() {
    local mode="$1" path="$2"
    if [[ "$DEST_ROOT" == "/" ]]; then
        run install -d -m "$mode" "$path"
    else
        run mkdir -p "$path"
        run chmod "$mode" "$path"
    fi
}

put_file() {
    local mode="$1" source="$2" target="$3"
    if [[ "$DEST_ROOT" == "/" ]]; then
        run install -m "$mode" "$source" "$target"
    else
        run cp "$source" "$target"
        run chmod "$mode" "$target"
    fi
}

install_program() {
    local source="$1" target="$2"
    log "Installing program: $target"
    put_file 0750 "$source" "$target"
}

install_config() {
    local source="$1" target="$2" mode="$3" candidate
    LAST_INSTALL_CHANGED=0

    if [[ -e "$target" ]] && cmp -s "$source" "$target"; then
        log "Unchanged configuration: $target"
        return 0
    fi

    if [[ -e "$target" && $FORCE_CONFIG -eq 0 ]]; then
        candidate="${target}.dist"
        warn "Preserving modified configuration: $target"
        log "Installing packaged candidate: $candidate"
        put_file "$mode" "$source" "$candidate"
        CONFIG_CONFLICTS=$(( CONFIG_CONFLICTS + 1 ))
        return 0
    fi

    log "Installing configuration: $target"
    put_file "$mode" "$source" "$target"
    LAST_INSTALL_CHANGED=1
}

install_config_tree() {
    local source_dir="$1" target_dir="$2" source target
    for source in "$source_dir"/*; do
        [[ -f "$source" ]] || continue
        target="${target_dir}/$(basename "$source")"
        install_config "$source" "$target" 0644
    done
}

main() {
    local etc_patching profile_dir dropin_dir sbin_dir dracut_dir dracut_target

    parse_args "$@"
    normalize_root

    for command in cmp; do
        command -v "$command" >/dev/null 2>&1 || die "Required command is missing: $command"
    done
    if [[ "$DEST_ROOT" == "/" ]]; then
        command -v install >/dev/null 2>&1 || die "Required command is missing: install"
    else
        for command in mkdir cp chmod; do
            command -v "$command" >/dev/null 2>&1 || die "Required command is missing: $command"
        done
    fi

    etc_patching="$(target_path /etc/patching)"
    profile_dir="${etc_patching}/repos-profiles.d"
    dropin_dir="${etc_patching}/repos-config.d"
    sbin_dir="$(target_path /usr/local/sbin)"
    dracut_dir="$(target_path /etc/dracut.conf.d)"

    ensure_dir 0750 "$etc_patching"
    ensure_dir 0755 "$profile_dir"
    ensure_dir 0755 "$dropin_dir"
    ensure_dir 0755 "$sbin_dir"
    ensure_dir 0755 "$dracut_dir"

    install_program "${SOURCE_DIR}/scripts/create-lvm-snapshot.sh" "${sbin_dir}/create-lvm-snapshot.sh"
    install_program "${SOURCE_DIR}/scripts/remove-lvm-snapshot.sh" "${sbin_dir}/remove-lvm-snapshot.sh"
    install_program "${SOURCE_DIR}/scripts/system-update.sh" "${sbin_dir}/system-update.sh"

    install_config "${SOURCE_DIR}/config/patching/lvm-config" "${etc_patching}/lvm-config" 0640
    install_config "${SOURCE_DIR}/config/patching/repos-config" "${etc_patching}/repos-config" 0640
    install_config_tree "${SOURCE_DIR}/config/patching/repos-profiles.d" "$profile_dir"
    install_config_tree "${SOURCE_DIR}/config/patching/repos-config.d" "$dropin_dir"

    dracut_target="${dracut_dir}/dmsnapshot.conf"
    install_config "${SOURCE_DIR}/config/dracut.conf.d/dmsnapshot.conf" "$dracut_target" 0644
    DRACUT_CONFIG_CHANGED="$LAST_INSTALL_CHANGED"

    if (( REBUILD_INITRAMFS )); then
        command -v dracut >/dev/null 2>&1 || die "dracut is required for --rebuild-initramfs"
        log "Rebuilding initramfs with dracut"
        run dracut -f
    elif (( DRACUT_CONFIG_CHANGED )); then
        warn "Dracut configuration changed; run 'dracut -f' before relying on boot-time snapshot support"
    fi

    if (( CONFIG_CONFLICTS > 0 )); then
        warn "$CONFIG_CONFLICTS installed configuration file(s) were preserved; review the corresponding .dist files"
    fi
    log "Installation completed successfully under $DEST_ROOT"
}

main "$@"
