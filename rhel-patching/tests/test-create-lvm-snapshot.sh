#!/usr/bin/env bash
set -o errexit
set -o nounset
set -o pipefail

export PATH="/usr/bin:/bin:${PATH:-}"
readonly PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT

fail_test() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

mkdir -p "${test_root}/bin" "${test_root}/lock" "${test_root}/one" "${test_root}/two"
cat >"${test_root}/lvm-config" <<EOF
LV_SNAP_PATHS=("${test_root}/one" "${test_root}/two")
LVM_SNAPSHOT_NAME_PREFIX="snap-"
LVM_SNAPSHOT_NAME_SUFFIX="-safe"
LVM_RESERVATION_LV_NAME="growth-buffer"
LVM_RESERVATION_LV_TAG="patching-reservation"
LVM_SNAPSHOT_LV_TAG="patching-snapshot"
LVM_MAX_SNAPSHOT_AGE_HOURS=0
LVM_MAX_SNAPSHOT_DATA_PERCENT=90
EOF

cat >"${test_root}/bin/findmnt" <<EOF
#!/usr/bin/env bash
case "\${*: -1}" in
    "${test_root}/one") printf '/dev/mapper/vg-lv1\n' ;;
    "${test_root}/two") printf '/dev/mapper/vg-lv2\n' ;;
    *) exit 1 ;;
esac
EOF
cat >"${test_root}/bin/lvs" <<'EOF'
#!/usr/bin/env bash
args="$*"
case "$args" in
    *"-o vg_name,lv_name"*"lv_path='/dev/mapper/vg-lv1'"*) printf ' vg|lv1\n' ;;
    *"-o vg_name,lv_name"*"lv_path='/dev/mapper/vg-lv2'"*) printf ' vg|lv2\n' ;;
    *"-o lv_size"*"vg/lv1"*) printf ' 104857600.00\n' ;;
    *"-o lv_size"*"vg/lv2"*) printf ' 104857600.00\n' ;;
    *"-o lv_layout vg/lv1"*|*"-o lv_layout vg/lv2"*) printf ' linear\n' ;;
    *"-o lv_name,origin vg/snap-lv1-safe"*)
        [[ "${FAKE_NO_EXISTING_SNAPSHOTS:-0}" == 1 ]] || printf ' snap-lv1-safe lv1\n'
        ;;
    *"-o lv_name,origin vg/snap-lv2-safe"*) exit 0 ;;
    *"-o lv_attr vg/snap-lv1-safe"*) printf ' %s\n' "${FAKE_SNAPSHOT_ATTR:-swi-a-s---}" ;;
    *"-o data_percent vg/snap-lv1-safe"*) printf ' %s\n' "${FAKE_SNAPSHOT_DATA_PERCENT:-10.00}" ;;
    *"-o lv_time vg/snap-lv1-safe"*) printf ' %s\n' "${FAKE_SNAPSHOT_TIME:-2000-01-01 00:00:00 +0000}" ;;
    *"-o lv_tags vg/snap-lv1-safe"*) printf ' patching-snapshot,patching-snapshot.wave.test\n' ;;
    *"-o origin vg/growth-buffer"*) printf '\n' ;;
    *"-o lv_tags vg/growth-buffer"*)
        [[ "${FAKE_RESERVATION_UNTAGGED:-0}" == 1 ]] || printf ' patching-reservation\n'
        ;;
    *"-o lv_path vg/growth-buffer"*) printf ' /dev/vg/growth-buffer\n' ;;
    *"-o lv_size"*"vg/growth-buffer"*) printf ' 52428800.00\n' ;;
    *"vg/growth-buffer"*) printf ' growth-buffer\n' ;;
    *"vg/snap-lv2-safe"*) exit 1 ;;
    *) exit 1 ;;
esac
EOF
cat >"${test_root}/bin/vgs" <<'EOF'
#!/usr/bin/env bash
if [[ "${FAKE_LARGE_FREE_SPACE:-0}" == 1 ]]; then
    printf ' 157286400.00\n'
else
    printf ' <73400321.00\n'
fi
EOF
cat >"${test_root}/bin/flock" <<'EOF'
#!/usr/bin/env bash
exit "${FAKE_FLOCK_RC:-0}"
EOF
cat >"${test_root}/bin/wipefs" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "${test_root}/bin/findmnt" "${test_root}/bin/lvs" "${test_root}/bin/vgs" "${test_root}/bin/flock" "${test_root}/bin/wipefs"

export PATH="${test_root}/bin:${PATH}"
export PATCHING_LVM_CONFIG="${test_root}/lvm-config"
export PATCHING_LVM_LOGFILE="${test_root}/snapshot.log"
export PATCHING_LOCK_DIR="${test_root}/lock"

set +o errexit
bash "${PROJECT_DIR}/scripts/create-lvm-snapshot.sh" --dryrun >/dev/null 2>&1
rc=$?
set -o errexit
[[ $rc -eq 2 ]] || fail_test "unknown create option returned $rc instead of 2"

export FAKE_FLOCK_RC=1
set +o errexit
lock_output="$(bash "${PROJECT_DIR}/scripts/create-lvm-snapshot.sh" --dry-run 2>&1)"
rc=$?
set -o errexit
[[ $rc -eq 1 ]] || fail_test "busy shared lock returned $rc instead of 1"
[[ "$lock_output" == *"Another patching operation is running"* ]] || fail_test "busy shared lock was not reported"
unset FAKE_FLOCK_RC

output="$(bash "${PROJECT_DIR}/scripts/create-lvm-snapshot.sh" --dry-run)" || fail_test "snapshot dry-run failed"
[[ "$output" == *"Validated reusable snapshot vg/snap-lv1-safe"* ]] || fail_test "existing snapshot health was not validated"
[[ "$output" == *"WARNING: Snapshot already exists and will be reused: vg/snap-lv1-safe"* ]] || fail_test "existing snapshot warning was not emitted"
[[ "$output" == *"100.00MiB for missing snapshots; 70.00MiB free + 50.00MiB reserved = 120.00MiB available"* ]] || fail_test "byte capacity calculation is incorrect"
[[ "$output" == *"lvremove -y vg/growth-buffer"* ]] || fail_test "configured reservation name was not used"
[[ "$output" == *"-L 104857600B -s vg/lv2"* ]] || fail_test "missing snapshot was not planned in exact bytes"
[[ "$output" != *"-n snap-lv1-safe"* ]] || fail_test "existing snapshot was incorrectly planned for creation"

export FAKE_RESERVATION_UNTAGGED=1
set +o errexit
collision_output="$(bash "${PROJECT_DIR}/scripts/create-lvm-snapshot.sh" --dry-run 2>&1)"
rc=$?
set -o errexit
[[ $rc -eq 1 ]] || fail_test "untagged reservation collision was accepted"
[[ "$collision_output" == *"lacks ownership tag patching-reservation"* ]] || fail_test "untagged reservation refusal was not explained"
[[ "$collision_output" != *"lvremove -y vg/growth-buffer"* ]] || fail_test "untagged reservation was scheduled for removal"
unset FAKE_RESERVATION_UNTAGGED

export FAKE_SNAPSHOT_ATTR='swi-I-s---'
set +o errexit
invalid_output="$(bash "${PROJECT_DIR}/scripts/create-lvm-snapshot.sh" --dry-run 2>&1)"
rc=$?
set -o errexit
[[ $rc -eq 1 ]] || fail_test "invalid snapshot was accepted"
[[ "$invalid_output" == *"not an active, valid snapshot"* ]] || fail_test "invalid snapshot refusal was not explained"
unset FAKE_SNAPSHOT_ATTR

export FAKE_SNAPSHOT_DATA_PERCENT=95
set +o errexit
full_output="$(bash "${PROJECT_DIR}/scripts/create-lvm-snapshot.sh" --dry-run 2>&1)"
rc=$?
set -o errexit
[[ $rc -eq 1 ]] || fail_test "nearly full snapshot was accepted"
[[ "$full_output" == *"95% full"* ]] || fail_test "snapshot utilization refusal was not explained"
unset FAKE_SNAPSHOT_DATA_PERCENT

export FAKE_NO_EXISTING_SNAPSHOTS=1
export FAKE_LARGE_FREE_SPACE=1
new_wave_output="$(bash "${PROJECT_DIR}/scripts/create-lvm-snapshot.sh" --dry-run)" || fail_test "new patch-wave planning failed"
[[ "$new_wave_output" == *"Patch-wave tag: patching-snapshot.wave."* ]] || fail_test "new patch-wave tag was not generated"
[[ "$new_wave_output" == *"-n snap-lv1-safe"* && "$new_wave_output" == *"-n snap-lv2-safe"* ]] || fail_test "all missing snapshots were not planned"
unset FAKE_NO_EXISTING_SNAPSHOTS FAKE_LARGE_FREE_SPACE

printf '\nLVM_MAX_SNAPSHOT_AGE_HOURS=1\n' >>"${test_root}/lvm-config"
set +o errexit
stale_output="$(bash "${PROJECT_DIR}/scripts/create-lvm-snapshot.sh" --dry-run 2>&1)"
rc=$?
set -o errexit
[[ $rc -eq 1 ]] || fail_test "stale snapshot was accepted"
[[ "$stale_output" == *"older than 1 hours"* ]] || fail_test "stale snapshot refusal was not explained"

printf 'PASS: partial LVM snapshot preparation and CLI safety test\n'
