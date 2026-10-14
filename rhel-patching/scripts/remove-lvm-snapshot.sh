#!/bin/bash
set -o errexit
set -o nounset
set -o pipefail

CONFIG="${PATCHING_LVM_CONFIG:-/etc/patching/lvm-config}"
LOGFILE="${PATCHING_LVM_LOGFILE:-/var/log/patching-lvm-snapshots.log}"
LOCK_DIR="${PATCHING_LOCK_DIR:-/run/lock/patching}"
LOCK_FILE="${LOCK_DIR}/patching.lock"

DRYRUN=0

usage() {
    cat <<'EOF'
Usage: remove-lvm-snapshot.sh [--dry-run]

Options:
  --dry-run  Print the validated removal and reservation plan without changing LVM
  -h, --help Show this help
EOF
}

parse_args() {
    while (( $# > 0 )); do
        case "$1" in
            --dry-run) DRYRUN=1 ;;
            -h|--help) usage; exit 0 ;;
            *) printf 'ERROR: Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
        esac
        shift
    done
}

log_setup() {
    mkdir -p "$(dirname "$LOGFILE")"
}

log() {
    local msg="$1"
    echo "$(date '+%F %T') $msg" | tee -a "$LOGFILE"
}

warn() {
    log "WARNING: $*"
}

fail() {
    log "ERROR: $*" >&2
    exit 1
}

take_lock() {
    mkdir -p "$LOCK_DIR"
    exec 9>"$LOCK_FILE"
    flock -n 9 || fail "Another patching operation is running (lock: $LOCK_FILE)"
}

render_command() {
    local rendered
    printf -v rendered '%q ' "$@"
    printf '%s' "${rendered% }"
}

trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

normalize_lvm_bytes() {
    local raw mode marker="" value
    raw="$(trim "$1")"
    mode="$2"
    if [[ "$raw" == \<* || "$raw" == \>* ]]; then
        marker="${raw:0:1}"
        raw="${raw:1}"
    fi
    [[ "$raw" =~ ^[0-9]+([.][0-9]+)?$ ]] || return 1
    value="$(awk -v raw="$raw" -v mode="$mode" 'BEGIN {
        value = raw + 0; whole = int(value)
        if (mode == "ceil" && value > whole) whole++
        printf "%.0f", whole
    }')"
    if [[ "$mode" == "floor" && "$marker" == "<" && "$value" -gt 0 ]]; then
        value=$(( value - 1 ))
    elif [[ "$mode" == "ceil" && "$marker" == ">" ]]; then
        value=$(( value + 1 ))
    fi
    printf '%s\n' "$value"
}

lvs_field() {
    local field="$1" target="$2" output
    output="$(lvs --noheadings -o "$field" "$target" 2>/dev/null)" || return 1
    trim "$output"
}

get_lv_size_bytes() {
    local raw
    raw="$(lvs --noheadings -o lv_size --units b --nosuffix "$1" 2>/dev/null)" || return 1
    normalize_lvm_bytes "$raw" ceil
}

get_vg_free_bytes() {
    local raw
    raw="$(vgs --noheadings -o vg_free --units b --nosuffix "$1" 2>/dev/null)" || return 1
    normalize_lvm_bytes "$raw" floor
}

format_mib() {
    awk -v bytes="$1" 'BEGIN { printf "%.2fMiB", bytes / 1048576 }'
}

lv_exists() {
    local vg="$1" lv="$2"
    lvs "${vg}/${lv}" >/dev/null 2>&1
}

snapshot_exists() {
    local vg="$1" lv="$2" snap="$3"
    lvs --noheadings -o lv_name,origin "${vg}/${snap}" 2>/dev/null \
        | awk -v s="$snap" -v o="$lv" '
            $1==s && $2==o {found=1}
            END { exit (found ? 0 : 1) }
        '
}

get_lv_info_from_path() {
    local path="$1" dev output row vg lv size_bytes layout
    [[ -e "$path" ]] || fail "Configured snapshot path does not exist: $path"
    dev="$(findmnt -n -o SOURCE --target "$path")" || fail "Cannot resolve device for path $path"
    output="$(lvs --noheadings --separator '|' -o vg_name,lv_name \
        --select "lv_path='$dev' || lv_dm_path='$dev'" 2>/dev/null)" \
        || fail "Cannot resolve LVM LV for path $path ($dev)"
    row="$(printf '%s\n' "$output" | awk 'NF {print; exit}')"
    IFS='|' read -r vg lv <<< "$row"
    vg="$(trim "${vg:-}")"
    lv="$(trim "${lv:-}")"
    [[ -n "$vg" && -n "$lv" ]] || fail "Path $path does not resolve to an LVM LV"
    size_bytes="$(get_lv_size_bytes "${vg}/${lv}")" || fail "Cannot determine size of ${vg}/${lv}"
    layout="$(lvs_field lv_layout "${vg}/${lv}")" || fail "Cannot determine layout of ${vg}/${lv}"
    [[ "$layout" != *thin* ]] || fail "Thin LV ${vg}/${lv} is not supported by the classic snapshot workflow"
    printf '%s %s %s\n' "$vg" "$lv" "$size_bytes"
}

valid_lvm_name() {
    local name="$1"
    [[ "$name" =~ ^[A-Za-z0-9_.+][A-Za-z0-9_.+-]*$ &&
       "$name" != "." && "$name" != ".." && ${#name} -le 127 ]]
}

valid_lvm_tag() {
    local tag="$1"
    [[ "$tag" =~ ^[A-Za-z0-9_.+][A-Za-z0-9_.+-]*$ && ${#tag} -le 1024 ]]
}

tag_list_contains() {
    local tags="$1" wanted="$2" tag
    local -a parsed_tags=()
    IFS=',' read -r -a parsed_tags <<< "$tags"
    for tag in "${parsed_tags[@]}"; do
        [[ "$(trim "$tag")" == "$wanted" ]] && return 0
    done
    return 1
}

snapshot_name_for_lv() {
    local lv="$1"
    printf '%s%s%s\n' "$LVM_SNAPSHOT_NAME_PREFIX" "$lv" "$LVM_SNAPSHOT_NAME_SUFFIX"
}

validate_reservation_candidate() {
    local vg="$1" lv="$2" tags origin lv_path mounts signatures
    [[ -z "${LV_BY_KEY[${vg}/${lv}]:-}" ]] || fail "Reservation ${vg}/${lv} is also a configured origin LV"
    origin="$(lvs_field origin "${vg}/${lv}")" || fail "Cannot inspect reservation candidate ${vg}/${lv}"
    [[ -z "$origin" ]] || fail "Reservation candidate ${vg}/${lv} is itself a snapshot"
    tags="$(lvs_field lv_tags "${vg}/${lv}")" || fail "Cannot inspect tags on reservation candidate ${vg}/${lv}"
    lv_path="$(lvs_field lv_path "${vg}/${lv}")" || fail "Cannot resolve device path for reservation candidate ${vg}/${lv}"
    mounts="$(findmnt -rn -S "$lv_path" 2>/dev/null || true)"
    [[ -z "$mounts" ]] || fail "Reservation candidate ${vg}/${lv} is mounted and will not be modified"
    signatures="$(wipefs -n "$lv_path" 2>/dev/null)" || fail "Cannot inspect filesystem signatures on reservation candidate ${vg}/${lv}"
    [[ -z "$signatures" ]] || fail "Reservation candidate ${vg}/${lv} has filesystem signatures and will not be modified"
    if tag_list_contains "$tags" "$LVM_RESERVATION_LV_TAG"; then
        return 0
    fi
    (( LVM_ADOPT_UNTAGGED_RESERVATION == 1 )) || \
        fail "LV ${vg}/${lv} lacks ownership tag ${LVM_RESERVATION_LV_TAG}; refusing to modify it"
    RESERVATION_NEEDS_TAG_BY_VG["$vg"]=1
    warn "Adopting untagged reservation candidate ${vg}/${lv} by explicit configuration"
}

parse_args "$@"
log_setup
take_lock

[[ -r "$CONFIG" ]] || fail "Missing config file $CONFIG"
source "$CONFIG"

: "${LVM_SNAPSHOT_NAME_PREFIX:=patching-}"
: "${LVM_SNAPSHOT_NAME_SUFFIX:=}"
: "${LVM_RESERVATION_LV_NAME:=patch-reserved}"
: "${LVM_RESERVATION_LV_TAG:=patching-reservation}"
: "${LVM_SNAPSHOT_LV_TAG:=patching-snapshot}"
: "${LVM_ADOPT_UNTAGGED_RESERVATION:=0}"
: "${LVM_ADOPT_UNTAGGED_SNAPSHOTS:=0}"

declare -p LV_SNAP_PATHS >/dev/null 2>&1 || fail "LV_SNAP_PATHS is not defined"
[[ ${#LV_SNAP_PATHS[@]} -gt 0 ]] || fail "LV_SNAP_PATHS is empty"
valid_lvm_name "$LVM_RESERVATION_LV_NAME" || fail "Invalid reservation LV name: $LVM_RESERVATION_LV_NAME"
valid_lvm_tag "$LVM_RESERVATION_LV_TAG" || fail "Invalid reservation LV tag: $LVM_RESERVATION_LV_TAG"
valid_lvm_tag "$LVM_SNAPSHOT_LV_TAG" || fail "Invalid snapshot LV tag: $LVM_SNAPSHOT_LV_TAG"
[[ "$LVM_ADOPT_UNTAGGED_RESERVATION" =~ ^[01]$ ]] || fail "LVM_ADOPT_UNTAGGED_RESERVATION must be 0 or 1"
[[ "$LVM_ADOPT_UNTAGGED_SNAPSHOTS" =~ ^[01]$ ]] || fail "LVM_ADOPT_UNTAGGED_SNAPSHOTS must be 0 or 1"

log "=== Starting LVM snapshot removal planning (dry-run=$DRYRUN) ==="

declare -A LV_BY_KEY=()
declare -A SNAPSHOT_BY_KEY=()
declare -A SNAPSHOT_ADOPTED_BY_KEY=()
declare -A SNAP_RECLAIM_BY_VG=()
declare -A RESERVATION_TARGET_BY_VG=()
declare -A EXISTING_RESERVATION_BY_VG=()
declare -A PLANNED_RESERVATION_BY_VG=()
declare -A UPDATE_RESERVATION_BY_VG=()
declare -A RESERVATION_NEEDS_TAG_BY_VG=()

for path in "${LV_SNAP_PATHS[@]}"; do
    lv_info="$(get_lv_info_from_path "$path")" || exit 1
    read -r vg lv size_bytes <<< "$lv_info"
    key="${vg}/${lv}"
    snap="$(snapshot_name_for_lv "$lv")"
    valid_lvm_name "$snap" || fail "Invalid generated snapshot name: $snap"
    if [[ -z "${LV_BY_KEY[$key]:-}" ]]; then
        LV_BY_KEY["$key"]="$vg $lv $size_bytes"
        current="${RESERVATION_TARGET_BY_VG[$vg]:-0}"
        RESERVATION_TARGET_BY_VG["$vg"]=$(( current + size_bytes ))
        log "Detected LV for path $path -> $vg/$lv ($(format_mib "$size_bytes"))"
    else
        log "Path $path maps to same LV $vg/$lv - deduped"
    fi
done

for key in "${!LV_BY_KEY[@]}"; do
    read -r vg lv size_bytes <<< "${LV_BY_KEY[$key]}"
    snap="$(snapshot_name_for_lv "$lv")"
    if snapshot_exists "$vg" "$lv" "$snap"; then
        tags="$(lvs_field lv_tags "${vg}/${snap}")" || fail "Cannot inspect tags on ${vg}/${snap}"
        if ! tag_list_contains "$tags" "$LVM_SNAPSHOT_LV_TAG"; then
            (( LVM_ADOPT_UNTAGGED_SNAPSHOTS == 1 )) || \
                fail "Snapshot ${vg}/${snap} lacks ownership tag ${LVM_SNAPSHOT_LV_TAG}; refusing to remove it"
            SNAPSHOT_ADOPTED_BY_KEY["$key"]=1
            warn "Treating untagged snapshot ${vg}/${snap} as owned by explicit configuration"
        fi
        snap_size_bytes="$(get_lv_size_bytes "${vg}/${snap}")" || fail "Cannot determine size of snapshot ${vg}/${snap}"
        SNAPSHOT_BY_KEY["$key"]="$vg $lv $snap $snap_size_bytes"
        current="${SNAP_RECLAIM_BY_VG[$vg]:-0}"
        SNAP_RECLAIM_BY_VG["$vg"]=$(( current + snap_size_bytes ))
        log "Found snapshot ${vg}/${snap} ($(format_mib "$snap_size_bytes"))"
    elif lv_exists "$vg" "$snap"; then
        warn "LV ${vg}/${snap} exists but is not a snapshot of ${vg}/${lv}; leaving it untouched"
    else
        warn "Snapshot missing: ${vg}/${snap}"
    fi
done

for vg in "${!RESERVATION_TARGET_BY_VG[@]}"; do
    target_bytes="${RESERVATION_TARGET_BY_VG[$vg]}"
    reclaimable_bytes="${SNAP_RECLAIM_BY_VG[$vg]:-0}"
    free_bytes="$(get_vg_free_bytes "$vg")" || fail "Cannot determine free space for VG $vg"
    existing_bytes=0
    if lv_exists "$vg" "$LVM_RESERVATION_LV_NAME"; then
        validate_reservation_candidate "$vg" "$LVM_RESERVATION_LV_NAME"
        existing_bytes="$(get_lv_size_bytes "${vg}/${LVM_RESERVATION_LV_NAME}")" || \
            fail "Cannot determine size of reservation ${vg}/${LVM_RESERVATION_LV_NAME}"
    fi
    EXISTING_RESERVATION_BY_VG["$vg"]="$existing_bytes"

    if (( existing_bytes >= target_bytes )); then
        planned_bytes="$existing_bytes"
        update=0
        log "VG $vg reservation $(format_mib "$existing_bytes") already meets target $(format_mib "$target_bytes"); leaving it unchanged"
    else
        projected_bytes=$(( free_bytes + reclaimable_bytes + existing_bytes ))
        if (( projected_bytes < target_bytes )); then
            planned_bytes="$projected_bytes"
            warn "VG $vg reservation target is $(format_mib "$target_bytes"), but only $(format_mib "$projected_bytes") will be available; planning a best-effort reservation"
        else
            planned_bytes="$target_bytes"
        fi
        if (( planned_bytes > existing_bytes )); then
            update=1
        else
            update=0
            warn "VG $vg cannot grow reservation beyond its existing $(format_mib "$existing_bytes")"
        fi
    fi
    PLANNED_RESERVATION_BY_VG["$vg"]="$planned_bytes"
    UPDATE_RESERVATION_BY_VG["$vg"]="$update"
    log "VG $vg plan: $(format_mib "$free_bytes") free + $(format_mib "$reclaimable_bytes") snapshots + $(format_mib "$existing_bytes") existing; target=$(format_mib "$target_bytes") planned=$(format_mib "$planned_bytes")"
done

removal_failed=0
for key in "${!SNAPSHOT_BY_KEY[@]}"; do
    read -r vg lv snap snap_size_bytes <<< "${SNAPSHOT_BY_KEY[$key]}"
    if (( DRYRUN )); then
        log "[DRY-RUN] $(render_command lvremove -y "${vg}/${snap}")"
        continue
    fi
    if ! snapshot_exists "$vg" "$lv" "$snap"; then
        warn "Snapshot ${vg}/${snap} disappeared or changed after planning; skipping it"
        removal_failed=1
        continue
    fi
    tags="$(lvs_field lv_tags "${vg}/${snap}")" || tags=""
    if ! tag_list_contains "$tags" "$LVM_SNAPSHOT_LV_TAG" && \
       [[ "${SNAPSHOT_ADOPTED_BY_KEY[$key]:-0}" != 1 ]]; then
        warn "Snapshot ${vg}/${snap} lost its ownership tag after planning; skipping it"
        removal_failed=1
        continue
    fi
    log "Removing snapshot ${vg}/${snap} ($(format_mib "$snap_size_bytes"))"
    if ! lvremove -y "${vg}/${snap}"; then
        warn "Failed to remove snapshot ${vg}/${snap}; continuing to reservation finalization"
        removal_failed=1
    fi
done

reservation_failed=0
for vg in "${!RESERVATION_TARGET_BY_VG[@]}"; do
    existing_bytes="${EXISTING_RESERVATION_BY_VG[$vg]}"
    planned_bytes="${PLANNED_RESERVATION_BY_VG[$vg]}"
    update="${UPDATE_RESERVATION_BY_VG[$vg]}"
    if [[ "${RESERVATION_NEEDS_TAG_BY_VG[$vg]:-0}" == 1 ]]; then
        if (( DRYRUN )); then
            log "[DRY-RUN] $(render_command lvchange --addtag "$LVM_RESERVATION_LV_TAG" "${vg}/${LVM_RESERVATION_LV_NAME}")"
        elif ! lvchange --addtag "$LVM_RESERVATION_LV_TAG" "${vg}/${LVM_RESERVATION_LV_NAME}"; then
            warn "Could not tag reservation ${vg}/${LVM_RESERVATION_LV_NAME}; it will not be modified"
            reservation_failed=1
            continue
        fi
    fi
    (( update == 1 )) || continue

    final_size_bytes="$planned_bytes"
    if (( DRYRUN == 0 )); then
        actual_free_bytes="$(get_vg_free_bytes "$vg")" || {
            warn "Cannot determine post-removal free space for VG $vg"
            reservation_failed=1
            continue
        }
        maximum_size_bytes=$(( existing_bytes + actual_free_bytes ))
        if (( maximum_size_bytes < final_size_bytes )); then
            warn "VG $vg free space changed during execution; reducing reservation from $(format_mib "$final_size_bytes") to $(format_mib "$maximum_size_bytes")"
            final_size_bytes="$maximum_size_bytes"
        fi
    fi

    if (( existing_bytes > 0 && final_size_bytes > existing_bytes )); then
        log "Growing reservation ${vg}/${LVM_RESERVATION_LV_NAME} from $(format_mib "$existing_bytes") to $(format_mib "$final_size_bytes")"
        if (( DRYRUN )); then
            log "[DRY-RUN] $(render_command lvextend -L "${final_size_bytes}B" "${vg}/${LVM_RESERVATION_LV_NAME}")"
        elif ! lvextend -L "${final_size_bytes}B" "${vg}/${LVM_RESERVATION_LV_NAME}"; then
            warn "Failed to grow reservation ${vg}/${LVM_RESERVATION_LV_NAME}"
            reservation_failed=1
        fi
    elif (( existing_bytes == 0 && final_size_bytes > 0 )); then
        log "Creating reservation ${vg}/${LVM_RESERVATION_LV_NAME} ($(format_mib "$final_size_bytes"))"
        if (( DRYRUN )); then
            log "[DRY-RUN] $(render_command lvcreate --zero y --wipesignatures y --yes --addtag "$LVM_RESERVATION_LV_TAG" -n "$LVM_RESERVATION_LV_NAME" -L "${final_size_bytes}B" "$vg")"
        elif ! lvcreate --zero y --wipesignatures y --yes --addtag "$LVM_RESERVATION_LV_TAG" \
            -n "$LVM_RESERVATION_LV_NAME" -L "${final_size_bytes}B" "$vg"; then
            warn "Failed to create reservation ${vg}/${LVM_RESERVATION_LV_NAME}"
            reservation_failed=1
        fi
    elif (( existing_bytes > 0 )); then
        warn "Reservation ${vg}/${LVM_RESERVATION_LV_NAME} remains at $(format_mib "$existing_bytes") because no additional free space is available"
    else
        warn "Reservation ${vg}/${LVM_RESERVATION_LV_NAME} was not created because no free space is available"
    fi
done

if (( removal_failed || reservation_failed )); then
    fail "Snapshot removal completed with errors after reservation finalization"
fi
log "=== Snapshot removal and reservation planning completed successfully ==="
