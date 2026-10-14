#!/usr/bin/env bash
set -o errexit
set -o nounset
set -o pipefail

export PATH="/usr/bin:/bin:${PATH:-}"
readonly PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT

fail_test() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

stage_root="${test_root}/stage"
dry_root="${test_root}/dry-stage"

dry_output="$(bash "${PROJECT_DIR}/install.sh" --root "$dry_root" --dry-run)" ||
    fail_test "installer dry-run failed"
[[ "$dry_output" == *"[DRY-RUN]"* ]] || fail_test "installer dry-run did not print commands"
[[ ! -e "$dry_root" ]] || fail_test "installer dry-run created the staging root"

bash "${PROJECT_DIR}/install.sh" --root "$stage_root" >/dev/null ||
    fail_test "staged installation failed"

for path in \
    usr/local/sbin/create-lvm-snapshot.sh \
    usr/local/sbin/remove-lvm-snapshot.sh \
    usr/local/sbin/system-update.sh \
    etc/patching/lvm-config \
    etc/patching/repos-config \
    etc/patching/repos-profiles.d/rhel-9.conf \
    etc/patching/repos-config.d/10-epel.conf.disabled \
    etc/dracut.conf.d/dmsnapshot.conf; do
    [[ -f "${stage_root}/${path}" ]] || fail_test "installer did not create ${path}"
done
[[ -x "${stage_root}/usr/local/sbin/system-update.sh" ]] ||
    fail_test "installed program is not executable"
[[ ! -e "${stage_root}/etc/patching/repos-config.d/10-epel.conf" ]] ||
    fail_test "installer enabled EPEL"

printf '# local configuration\n' >"${stage_root}/etc/patching/repos-config"
preserve_output="$(bash "${PROJECT_DIR}/install.sh" --root "$stage_root" 2>&1)" ||
    fail_test "installer configuration-preservation run failed"
[[ "$(<"${stage_root}/etc/patching/repos-config")" == '# local configuration' ]] ||
    fail_test "installer overwrote local configuration"
[[ -f "${stage_root}/etc/patching/repos-config.dist" ]] ||
    fail_test "installer did not stage a .dist configuration candidate"
cmp -s "${PROJECT_DIR}/config/patching/repos-config" \
    "${stage_root}/etc/patching/repos-config.dist" ||
    fail_test "staged .dist configuration differs from packaged configuration"
[[ "$preserve_output" == *"Preserving modified configuration"* ]] ||
    fail_test "configuration preservation warning was not emitted"

bash "${PROJECT_DIR}/install.sh" --root "$stage_root" --force-config >/dev/null ||
    fail_test "forced configuration installation failed"
cmp -s "${PROJECT_DIR}/config/patching/repos-config" \
    "${stage_root}/etc/patching/repos-config" ||
    fail_test "--force-config did not install packaged configuration"

set +o errexit
bash "${PROJECT_DIR}/install.sh" --root relative/path >/dev/null 2>&1
rc=$?
set -o errexit
[[ $rc -eq 1 ]] || fail_test "relative installation root returned $rc instead of 1"

printf 'PASS: staged installer and configuration preservation test\n'
