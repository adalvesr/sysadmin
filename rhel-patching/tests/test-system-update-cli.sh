#!/usr/bin/env bash
set -o errexit
set -o nounset
set -o pipefail

export PATH="/usr/bin:/bin:${PATH:-}"

readonly PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT

fail_test() {
    printf 'FAIL: %s\n' "$*" >&2
    if [[ -d "${test_root:-}/log" ]]; then
        find "${test_root}/log" -maxdepth 1 -type f -exec sh -c '
            for log_file do
                printf '%s\n' "--- ${log_file} ---" >&2
                cat "${log_file}" >&2
            done
        ' sh {} +
    fi
    exit 1
}

mkdir -p "${test_root}/etc/patching/repos-config.d" \
         "${test_root}/etc/patching/repos-profiles.d" \
         "${test_root}/log" "${test_root}/lock" "${test_root}/bin"

cat >"${test_root}/etc/patching/repos-config" <<'EOF'
REPO_PROFILE="manual"
ENABLED_REPOS=(test-base)
EOF

cat >"${test_root}/bin/rpm" <<'EOF'
#!/usr/bin/env bash
printf 'x86_64\n'
EOF

cat >"${test_root}/bin/flock" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat >"${test_root}/bin/dnf" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_DNF_CALLS}"
exit "${FAKE_DNF_RC}"
EOF

chmod +x "${test_root}/bin/rpm" "${test_root}/bin/flock" \
         "${test_root}/bin/dnf"

export PATH="${test_root}/bin:${PATH}"
export PATCHING_CONFIG_ROOT="${test_root}/etc/patching"
export PATCHING_OS_RELEASE_FILE="${test_root}/missing-os-release"
export PATCHING_LOG_DIR="${test_root}/log"
export PATCHING_LOCK_DIR="${test_root}/lock"
export FAKE_DNF_CALLS="${test_root}/dnf.calls"

set +o errexit
bash "${PROJECT_DIR}/scripts/system-update.sh" --help >/dev/null 2>&1
rc=$?
set -o errexit
[[ $rc -eq 0 ]] || fail_test "--help returned ${rc}, expected 0"

set +o errexit
bash "${PROJECT_DIR}/scripts/system-update.sh" --invalid >/dev/null 2>&1
rc=$?
set -o errexit
[[ $rc -eq 2 ]] || fail_test "invalid option returned ${rc}, expected 2"

for dnf_rc in 0 100 1; do
    : >"${FAKE_DNF_CALLS}"
    export FAKE_DNF_RC="$dnf_rc"

    set +o errexit
    command_output="$(bash "${PROJECT_DIR}/scripts/system-update.sh" --dry-run 2>&1)"
    rc=$?
    set -o errexit

    if [[ $dnf_rc -eq 0 || $dnf_rc -eq 100 ]]; then
        [[ $rc -eq 0 ]] || \
            { printf '%s\n' "${command_output}" >&2; fail_test "DNF ${dnf_rc} preview returned script status ${rc}, expected 0"; }
    else
        [[ $rc -eq 1 ]] || \
            { printf '%s\n' "${command_output}" >&2; fail_test "DNF failure returned script status ${rc}, expected 1"; }
    fi

    calls="$(<"${FAKE_DNF_CALLS}")"
    [[ "$calls" == *"check-update"* ]] || \
        fail_test "dry-run did not invoke dnf check-update"
    [[ "$calls" != *"clean all"* ]] || \
        fail_test "dry-run invoked dnf clean all"
done

printf 'PASS: system-update CLI and dry-run tests\n'
