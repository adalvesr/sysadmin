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
    *"-o lv_name,origin vg/snap-lv1-safe"*) printf ' snap-lv1-safe lv1\n' ;;
    *"-o lv_name,origin vg/snap-lv2-safe"*) exit 0 ;;
    *"-o lv_tags vg/snap-lv1-safe"*) printf ' patching-snapshot,patching-snapshot.wave.test\n' ;;
    *"-o lv_size"*"vg/snap-lv1-safe"*) printf ' 104857600.00\n' ;;
    *"-o origin vg/growth-buffer"*) printf '\n' ;;
    *"-o lv_tags vg/growth-buffer"*) printf ' patching-reservation\n' ;;
    *"-o lv_path vg/growth-buffer"*) printf ' /dev/vg/growth-buffer\n' ;;
    *"-o lv_size"*"vg/growth-buffer"*) printf ' 52428800.00\n' ;;
    *"vg/growth-buffer"*) printf ' growth-buffer\n' ;;
    *"vg/snap-lv2-safe"*) exit 1 ;;
    *) exit 1 ;;
esac
EOF
cat >"${test_root}/bin/vgs" <<'EOF'
#!/usr/bin/env bash
printf ' 20971520.00\n'
EOF
cat >"${test_root}/bin/flock" <<'EOF'
#!/usr/bin/env bash
exit 0
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
bash "${PROJECT_DIR}/scripts/remove-lvm-snapshot.sh" --dryrun >/dev/null 2>&1
rc=$?
set -o errexit
[[ $rc -eq 2 ]] || fail_test "unknown remove option returned $rc instead of 2"

output="$(bash "${PROJECT_DIR}/scripts/remove-lvm-snapshot.sh" --dry-run)" || fail_test "snapshot removal dry-run failed"
[[ "$output" == *"WARNING: Snapshot missing: vg/snap-lv2-safe"* ]] || fail_test "missing snapshot warning was not emitted"
[[ "$output" == *"target is 200.00MiB, but only 170.00MiB will be available"* ]] || fail_test "best-effort reservation warning was not emitted"
[[ "$output" == *"20.00MiB free + 100.00MiB snapshots + 50.00MiB existing; target=200.00MiB planned=170.00MiB"* ]] || fail_test "projected capacity plan is incorrect"
[[ "$output" == *"lvremove -y vg/snap-lv1-safe"* ]] || fail_test "existing snapshot removal was not planned"
[[ "$output" != *"lvremove -y vg/growth-buffer"* ]] || fail_test "existing reservation was destructively removed"
[[ "$output" == *"lvextend -L 178257920B vg/growth-buffer"* ]] || fail_test "best-effort reservation growth was not planned"

printf 'PASS: snapshot removal planning and CLI safety test\n'
