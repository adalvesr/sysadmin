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
LVM_SNAPSHOT_NAME_SUFFIX=""
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
    *"-o lv_size"*"vg/lv1"*|*"-o lv_size"*"vg/lv2"*) printf ' 104857600.00\n' ;;
    *"-o lv_layout vg/lv1"*|*"-o lv_layout vg/lv2"*) printf ' linear\n' ;;
    *"-o lv_name,origin vg/snap-lv1"*) printf ' snap-lv1 lv1\n' ;;
    *"-o lv_name,origin vg/snap-lv2"*) printf ' snap-lv2 lv2\n' ;;
    *"-o lv_tags vg/snap-lv1"*|*"-o lv_tags vg/snap-lv2"*) printf ' patching-snapshot,patching-snapshot.wave.test\n' ;;
    *"-o lv_size"*"vg/snap-lv1"*|*"-o lv_size"*"vg/snap-lv2"*) printf ' 104857600.00\n' ;;
    *"vg/growth-buffer"*) exit 1 ;;
    *) exit 1 ;;
esac
EOF
cat >"${test_root}/bin/vgs" <<'EOF'
#!/usr/bin/env bash
if [[ -e "${FAKE_RELEASED_SPACE}" ]]; then
    printf ' 104857600.00\n'
else
    printf ' 0.00\n'
fi
EOF
cat >"${test_root}/bin/lvremove" <<'EOF'
#!/usr/bin/env bash
printf 'lvremove %s\n' "$*" >>"${FAKE_LVM_CALLS}"
if [[ "$*" == *"vg/snap-lv1"* ]]; then
    : >"${FAKE_RELEASED_SPACE}"
    exit 0
fi
exit 1
EOF
cat >"${test_root}/bin/lvcreate" <<'EOF'
#!/usr/bin/env bash
printf 'lvcreate %s\n' "$*" >>"${FAKE_LVM_CALLS}"
exit 0
EOF
cat >"${test_root}/bin/flock" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "${test_root}/bin/"*

export PATH="${test_root}/bin:${PATH}"
export PATCHING_LVM_CONFIG="${test_root}/lvm-config"
export PATCHING_LVM_LOGFILE="${test_root}/snapshot.log"
export PATCHING_LOCK_DIR="${test_root}/lock"
export FAKE_LVM_CALLS="${test_root}/lvm.calls"
export FAKE_RELEASED_SPACE="${test_root}/released"

set +o errexit
output="$(bash "${PROJECT_DIR}/scripts/remove-lvm-snapshot.sh" 2>&1)"
rc=$?
set -o errexit

[[ $rc -eq 1 ]] || fail_test "partial removal returned $rc instead of failure"
calls="$(<"${FAKE_LVM_CALLS}")"
[[ "$calls" == *"lvremove -y vg/snap-lv1"* ]] || fail_test "successful snapshot removal was not attempted"
[[ "$calls" == *"lvremove -y vg/snap-lv2"* ]] || fail_test "failing snapshot removal was not attempted"
[[ "$calls" == *"lvcreate"*"-L 104857600B vg"* ]] || fail_test "reservation was not finalized from actually released space"
[[ "$output" == *"continuing to reservation finalization"* ]] || fail_test "partial failure continuation was not reported"
[[ "$output" == *"completed with errors after reservation finalization"* ]] || fail_test "final failure summary was not reported"

printf 'PASS: partial removal still finalizes reservation test\n'
