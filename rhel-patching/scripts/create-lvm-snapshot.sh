#!/bin/bash
set -o errexit
set -o nounset
set -o pipefail

CONFIG="${PATCHING_LVM_CONFIG:-/etc/patching/lvm-config}"
LOGFILE="${PATCHING_LVM_LOGFILE:-/var/log/patching-lvm-snapshots.log}"
LOCK_DIR="${PATCHING_LOCK_DIR:-/run/lock/patching}"
LOCK_FILE="${LOCK_DIR}/patching.lock"

DRYRUN=0
TRANSACTION_ACTIVE=0
CURRENT_WAVE_TAG=""

declare -a CREATED_SNAPSHOTS=()
declare -a REMOVED_RESERVATION_VGS=()
declare -A REMOVED_RESERVATION_SIZE_BY_VG=()

usage() {
    cat <<'EOF'
Usage: create-lvm-snapshot.sh [--dry-run]

Options:
  --dry-run  Print the validated snapshot plan without changing LVM or caches
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

run() {
    local rendered
    rendered="$(render_command "$@")"
    if (( DRYRUN )); then
        log "[DRY-RUN] $rendered"
    else
        log "RUN: $rendered"
        "$@"
    fi
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

validate_snapshot_health() {
    local vg="$1" lv="$2" snap="$3" attr state data_percent lv_time
    local created_epoch now_epoch age_seconds max_age_seconds
    attr="$(lvs_field lv_attr "${vg}/${snap}")" || fail "Cannot determine state of snapshot ${vg}/${snap}"
    state="${attr:4:1}"
    [[ "${attr:0:1}" == "s" && "$state" == "a" ]] || \
        fail "Snapshot ${vg}/${snap} is not an active, valid snapshot (lv_attr=$attr)"
    data_percent="$(lvs_field data_percent "${vg}/${snap}")" || fail "Cannot determine data usage of snapshot ${vg}/${snap}"
    data_percent="${data_percent#<}"
    data_percent="${data_percent#>}"
    [[ "$data_percent" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "Invalid data_percent for snapshot ${vg}/${snap}: $data_percent"
    awk -v used="$data_percent" -v maximum="$LVM_MAX_SNAPSHOT_DATA_PERCENT" \
        'BEGIN { exit !(used < maximum) }' || \
        fail "Snapshot ${vg}/${snap} is ${data_percent}% full; limit is below ${LVM_MAX_SNAPSHOT_DATA_PERCENT}%"
    if (( LVM_MAX_SNAPSHOT_AGE_HOURS > 0 )); then
        lv_time="$(lvs_field lv_time "${vg}/${snap}")" || fail "Cannot determine creation time of snapshot ${vg}/${snap}"
        created_epoch="$(date -d "$lv_time" +%s 2>/dev/null)" || fail "Cannot parse creation time for snapshot ${vg}/${snap}: $lv_time"
        now_epoch="$(date +%s)"
        age_seconds=$(( now_epoch - created_epoch ))
        (( age_seconds < 0 )) && age_seconds=0
        max_age_seconds=$(( LVM_MAX_SNAPSHOT_AGE_HOURS * 3600 ))
        (( age_seconds <= max_age_seconds )) || fail "Snapshot ${vg}/${snap} is older than ${LVM_MAX_SNAPSHOT_AGE_HOURS} hours"
    fi
    log "Validated reusable snapshot ${vg}/${snap}: origin=${vg}/${lv} data=${data_percent}%"
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

rollback() {
    local rc=$? i snapshot vg size_bytes
    trap - EXIT
    if (( TRANSACTION_ACTIVE == 1 && rc != 0 )); then
        warn "Snapshot preparation failed; rolling back changes from this invocation"
        set +o errexit
        for ((i=${#CREATED_SNAPSHOTS[@]} - 1; i >= 0; i--)); do
            snapshot="${CREATED_SNAPSHOTS[$i]}"
            if lvs "$snapshot" >/dev/null 2>&1; then
                log "ROLLBACK: removing newly created snapshot $snapshot"
                lvremove -y "$snapshot" || warn "Rollback could not remove snapshot $snapshot"
            fi
        done
        for vg in "${REMOVED_RESERVATION_VGS[@]}"; do
            size_bytes="${REMOVED_RESERVATION_SIZE_BY_VG[$vg]}"
            if lv_exists "$vg" "$LVM_RESERVATION_LV_NAME"; then
                warn "Rollback found an existing reservation ${vg}/${LVM_RESERVATION_LV_NAME}; leaving it unchanged"
                continue
            fi
            log "ROLLBACK: recreating reservation ${vg}/${LVM_RESERVATION_LV_NAME} ($(format_mib "$size_bytes"))"
            lvcreate --zero y --wipesignatures y --yes --addtag "$LVM_RESERVATION_LV_TAG" \
                -n "$LVM_RESERVATION_LV_NAME" -L "${size_bytes}B" "$vg" || \
                warn "Rollback could not recreate reservation ${vg}/${LVM_RESERVATION_LV_NAME}"
        done
    fi
    exit "$rc"
}

parse_args "$@"
log_setup
take_lock
trap rollback EXIT

[[ -r "$CONFIG" ]] || fail "Missing config file $CONFIG"
source "$CONFIG"

: "${LVM_SNAPSHOT_NAME_PREFIX:=patching-}"
: "${LVM_SNAPSHOT_NAME_SUFFIX:=}"
: "${LVM_RESERVATION_LV_NAME:=patch-reserved}"
: "${LVM_RESERVATION_LV_TAG:=patching-reservation}"
: "${LVM_SNAPSHOT_LV_TAG:=patching-snapshot}"
: "${LVM_MAX_SNAPSHOT_AGE_HOURS:=48}"
: "${LVM_MAX_SNAPSHOT_DATA_PERCENT:=90}"
: "${LVM_ADOPT_UNTAGGED_RESERVATION:=0}"
: "${LVM_ADOPT_UNTAGGED_SNAPSHOTS:=0}"

declare -p LV_SNAP_PATHS >/dev/null 2>&1 || fail "LV_SNAP_PATHS is not defined"
[[ ${#LV_SNAP_PATHS[@]} -gt 0 ]] || fail "LV_SNAP_PATHS is empty"
valid_lvm_name "$LVM_RESERVATION_LV_NAME" || fail "Invalid reservation LV name: $LVM_RESERVATION_LV_NAME"
valid_lvm_tag "$LVM_RESERVATION_LV_TAG" || fail "Invalid reservation LV tag: $LVM_RESERVATION_LV_TAG"
valid_lvm_tag "$LVM_SNAPSHOT_LV_TAG" || fail "Invalid snapshot LV tag: $LVM_SNAPSHOT_LV_TAG"
[[ "$LVM_MAX_SNAPSHOT_AGE_HOURS" =~ ^[0-9]+$ ]] || fail "LVM_MAX_SNAPSHOT_AGE_HOURS must be a non-negative integer"
[[ "$LVM_MAX_SNAPSHOT_DATA_PERCENT" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "LVM_MAX_SNAPSHOT_DATA_PERCENT must be numeric"
awk -v maximum="$LVM_MAX_SNAPSHOT_DATA_PERCENT" \
    'BEGIN { exit !(maximum > 0 && maximum <= 100) }' || \
    fail "LVM_MAX_SNAPSHOT_DATA_PERCENT must be greater than 0 and no more than 100"
[[ "$LVM_ADOPT_UNTAGGED_RESERVATION" =~ ^[01]$ ]] || fail "LVM_ADOPT_UNTAGGED_RESERVATION must be 0 or 1"
[[ "$LVM_ADOPT_UNTAGGED_SNAPSHOTS" =~ ^[01]$ ]] || fail "LVM_ADOPT_UNTAGGED_SNAPSHOTS must be 0 or 1"

log "=== Starting LVM snapshot preparation (dry-run=$DRYRUN) ==="

declare -A LV_BY_KEY=()
declare -A MISSING_LV_BY_KEY=()
declare -A MISSING_TOTAL_BY_VG=()
declare -A RESERVATION_SIZE_BY_VG=()
declare -A RESERVATION_NEEDS_TAG_BY_VG=()
declare -A SNAPSHOT_NEEDS_TAG_BY_KEY=()
declare -A WAVE_TAGS=()

for path in "${LV_SNAP_PATHS[@]}"; do
    lv_info="$(get_lv_info_from_path "$path")" || exit 1
    read -r vg lv size_bytes <<< "$lv_info"
    key="${vg}/${lv}"
    snap="$(snapshot_name_for_lv "$lv")"
    valid_lvm_name "$snap" || fail "Invalid generated snapshot name: $snap"
    if [[ -z "${LV_BY_KEY[$key]:-}" ]]; then
        LV_BY_KEY["$key"]="$vg $lv $size_bytes"
        log "Detected LV for path $path -> $vg/$lv ($(format_mib "$size_bytes"))"
    else
        log "Path $path maps to same LV $vg/$lv - deduped"
    fi
done

for key in "${!LV_BY_KEY[@]}"; do
    read -r vg lv size_bytes <<< "${LV_BY_KEY[$key]}"
    snap="$(snapshot_name_for_lv "$lv")"
    if snapshot_exists "$vg" "$lv" "$snap"; then
        validate_snapshot_health "$vg" "$lv" "$snap"
        tags="$(lvs_field lv_tags "${vg}/${snap}")" || fail "Cannot inspect tags on ${vg}/${snap}"
        owned=0
        tag_list_contains "$tags" "$LVM_SNAPSHOT_LV_TAG" && owned=1
        if (( owned == 0 && LVM_ADOPT_UNTAGGED_SNAPSHOTS == 0 )); then
            fail "Snapshot ${vg}/${snap} lacks ownership tag ${LVM_SNAPSHOT_LV_TAG}; refusing to reuse it"
        fi
        (( owned == 1 )) || warn "Adopting untagged snapshot ${vg}/${snap} by explicit configuration"
        found_wave=""
        IFS=',' read -r -a parsed_tags <<< "$tags"
        for tag in "${parsed_tags[@]}"; do
            tag="$(trim "$tag")"
            if [[ "$tag" == "${LVM_SNAPSHOT_LV_TAG}.wave."* ]]; then
                [[ -z "$found_wave" ]] || fail "Snapshot ${vg}/${snap} has multiple patch-wave tags"
                found_wave="$tag"
                WAVE_TAGS["$tag"]=1
            fi
        done
        [[ -n "$found_wave" && $owned -eq 1 ]] || SNAPSHOT_NEEDS_TAG_BY_KEY["$key"]=1
        warn "Snapshot already exists and will be reused: ${vg}/${snap}"
        continue
    fi
    lv_exists "$vg" "$snap" && fail "LV name collision: ${vg}/${snap} exists but is not a snapshot of ${vg}/${lv}"
    MISSING_LV_BY_KEY["$key"]="${LV_BY_KEY[$key]}"
    current="${MISSING_TOTAL_BY_VG[$vg]:-0}"
    MISSING_TOTAL_BY_VG["$vg"]=$(( current + size_bytes ))
done

(( ${#WAVE_TAGS[@]} <= 1 )) || fail "Existing snapshots belong to different patch waves"
if (( ${#WAVE_TAGS[@]} == 1 )); then
    for CURRENT_WAVE_TAG in "${!WAVE_TAGS[@]}"; do :; done
else
    CURRENT_WAVE_TAG="${LVM_SNAPSHOT_LV_TAG}.wave.$(date -u +%Y%m%dT%H%M%SZ).$$"
fi
valid_lvm_tag "$CURRENT_WAVE_TAG" || fail "Invalid generated patch-wave tag: $CURRENT_WAVE_TAG"
log "Patch-wave tag: $CURRENT_WAVE_TAG"

for vg in "${!MISSING_TOTAL_BY_VG[@]}"; do
    required_bytes="${MISSING_TOTAL_BY_VG[$vg]}"
    free_bytes="$(get_vg_free_bytes "$vg")" || fail "Cannot determine free space for VG $vg"
    reservation_bytes=0
    if lv_exists "$vg" "$LVM_RESERVATION_LV_NAME"; then
        validate_reservation_candidate "$vg" "$LVM_RESERVATION_LV_NAME"
        reservation_bytes="$(get_lv_size_bytes "${vg}/${LVM_RESERVATION_LV_NAME}")" || \
            fail "Cannot determine size of reservation ${vg}/${LVM_RESERVATION_LV_NAME}"
    else
        warn "Reservation LV is missing in VG $vg; using available free space"
    fi
    available_bytes=$(( free_bytes + reservation_bytes ))
    RESERVATION_SIZE_BY_VG["$vg"]="$reservation_bytes"
    log "VG $vg needs $(format_mib "$required_bytes") for missing snapshots; $(format_mib "$free_bytes") free + $(format_mib "$reservation_bytes") reserved = $(format_mib "$available_bytes") available"
    (( available_bytes >= required_bytes )) || \
        fail "VG $vg has insufficient capacity: $(format_mib "$available_bytes") available, $(format_mib "$required_bytes") required"
done

if (( DRYRUN )); then
    log "[DRY-RUN] Skipping DNF cache cleanup"
elif (( ${#MISSING_LV_BY_KEY[@]} > 0 )); then
    log "Cleaning DNF caches after successful snapshot preflight"
    dnf -q clean all || warn "dnf clean all returned non-zero"
    rm -rf /var/cache/yum/* /var/cache/dnf/* || warn "Could not completely remove DNF/YUM cache contents"
    TRANSACTION_ACTIVE=1
fi

for key in "${!SNAPSHOT_NEEDS_TAG_BY_KEY[@]}"; do
    read -r vg lv size_bytes <<< "${LV_BY_KEY[$key]}"
    snap="$(snapshot_name_for_lv "$lv")"
    run lvchange --addtag "$LVM_SNAPSHOT_LV_TAG" --addtag "$CURRENT_WAVE_TAG" "${vg}/${snap}"
done

if [[ ${#MISSING_LV_BY_KEY[@]} -eq 0 ]]; then
    log "All required snapshots already exist and passed validation - exiting without reservation changes"
    TRANSACTION_ACTIVE=0
    exit 0
fi

for vg in "${!MISSING_TOTAL_BY_VG[@]}"; do
    reservation_bytes="${RESERVATION_SIZE_BY_VG[$vg]}"
    if (( reservation_bytes > 0 )); then
        if [[ "${RESERVATION_NEEDS_TAG_BY_VG[$vg]:-0}" == 1 ]]; then
            run lvchange --addtag "$LVM_RESERVATION_LV_TAG" "${vg}/${LVM_RESERVATION_LV_NAME}"
        fi
        log "Releasing reservation ${vg}/${LVM_RESERVATION_LV_NAME} ($(format_mib "$reservation_bytes"))"
        run lvchange -an "${vg}/${LVM_RESERVATION_LV_NAME}"
        run lvremove -y "${vg}/${LVM_RESERVATION_LV_NAME}"
        if (( DRYRUN == 0 )); then
            REMOVED_RESERVATION_VGS+=("$vg")
            REMOVED_RESERVATION_SIZE_BY_VG["$vg"]="$reservation_bytes"
        fi
    fi
done

for key in "${!MISSING_LV_BY_KEY[@]}"; do
    read -r vg lv size_bytes <<< "${MISSING_LV_BY_KEY[$key]}"
    snap="$(snapshot_name_for_lv "$lv")"
    log "Creating snapshot ${vg}/${snap} ($(format_mib "$size_bytes")) of ${vg}/${lv}"
    run lvcreate --addtag "$LVM_SNAPSHOT_LV_TAG" --addtag "$CURRENT_WAVE_TAG" \
        -n "$snap" -L "${size_bytes}B" -s "${vg}/${lv}"
    (( DRYRUN == 1 )) || CREATED_SNAPSHOTS+=("${vg}/${snap}")
done

TRANSACTION_ACTIVE=0
log "=== Snapshot creation completed successfully ==="
