#!/usr/bin/env bash
set -o errexit
set -o nounset
set -o pipefail

export PATH="/usr/bin:/bin:${PATH:-}"

readonly PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail_test() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_equal() {
  local expected="$1" actual="$2" message="$3"
  [[ "${actual}" == "${expected}" ]] ||
    fail_test "${message}: expected <${expected}>, got <${actual}>"
}

run_auto_case() (
  local os_id="$1" major="$2" expected_profile="$3" dnf_repo_id="${4:-}"
  local test_root
  test_root="$(mktemp -d)"
  trap 'rm -rf -- "${test_root}"' EXIT

  mkdir -p "${test_root}/etc/patching/repos-config.d" \
           "${test_root}/etc/patching/repos-profiles.d" \
           "${test_root}/bin"
  cp -R "${PROJECT_DIR}/config/patching/repos-profiles.d/." \
        "${test_root}/etc/patching/repos-profiles.d/"

  printf 'REPO_PROFILE="auto"\n' >"${test_root}/etc/patching/repos-config"
  printf 'ID=%s\nVERSION_ID="%s.0"\n' "${os_id}" "${major}" \
    >"${test_root}/os-release"

  cat >"${test_root}/bin/rpm" <<'EOF'
#!/usr/bin/env bash
printf 'x86_64\n'
EOF
  cat >"${test_root}/bin/dnf" <<EOF
#!/usr/bin/env bash
printf 'repo id repo name status\n'
if [[ "\$*" == *"repolist --enabled"* && -n '${dnf_repo_id}' ]]; then
  printf '%s enabled\n' '${dnf_repo_id}'
fi
EOF
  chmod +x "${test_root}/bin/rpm" "${test_root}/bin/dnf"

  export PATCHING_CONFIG_ROOT="${test_root}/etc/patching"
  export PATCHING_OS_RELEASE_FILE="${test_root}/os-release"
  # shellcheck source=../scripts/system-update.sh
  source "${PROJECT_DIR}/scripts/system-update.sh"
  export PATH="${test_root}/bin:${PATH}"
  log() { :; }
  error() { printf 'ERROR: %s\n' "$*" >&2; }

  load_configs
  assert_equal "${expected_profile}" "${REPO_PROFILE}" \
    "profile for ${os_id}-${major}"

  case "${expected_profile}" in
    rhel-[0-9]*)
      array_contains "rhel-${major}-for-x86_64-baseos-rpms" \
        "${ENABLED_REPOS[@]}" || fail_test "missing RHEL BaseOS repository"
      array_contains "rhel-${major}-for-x86_64-appstream-rpms" \
        "${ENABLED_REPOS[@]}" || fail_test "missing RHEL AppStream repository"
      ;;
    rhel-payg-*)
      array_contains "rhel-${major}*-baseos-rhui-rpms*" \
        "${ENABLED_REPOS[@]}" || fail_test "missing RHEL PAYG BaseOS pattern"
      array_contains "rhel-${major}*-appstream-rhui-rpms*" \
        "${ENABLED_REPOS[@]}" || fail_test "missing RHEL PAYG AppStream pattern"
      ;;
    rocky-*|alma-*)
      assert_equal "3" "${#ENABLED_REPOS[@]}" \
        "core repository count for ${expected_profile}"
      array_contains baseos "${ENABLED_REPOS[@]}" || fail_test "missing baseos"
      array_contains appstream "${ENABLED_REPOS[@]}" || fail_test "missing appstream"
      array_contains extras "${ENABLED_REPOS[@]}" || fail_test "missing extras"
      ;;
    oracle-*)
      array_contains "ol${major}_baseos_latest" \
        "${ENABLED_REPOS[@]}" || fail_test "missing Oracle BaseOS repository"
      array_contains "ol${major}_appstream" \
        "${ENABLED_REPOS[@]}" || fail_test "missing Oracle AppStream repository"
      ;;
  esac
)

run_manual_case() (
  local test_root
  test_root="$(mktemp -d)"
  trap 'rm -rf -- "${test_root}"' EXIT

  mkdir -p "${test_root}/etc/patching/repos-config.d" \
           "${test_root}/etc/patching/repos-profiles.d" \
           "${test_root}/bin"
  cp -R "${PROJECT_DIR}/config/patching/repos-profiles.d/." \
        "${test_root}/etc/patching/repos-profiles.d/"

  cat >"${test_root}/etc/patching/repos-config" <<'EOF'
REPO_PROFILE="manual"
ENABLED_REPOS=(corp-baseos 'corp-*' corp-baseos)
BLACKLISTED_PACKAGES=('kernel*' 'package with spaces' 'kernel*')
EOF
  cat >"${test_root}/bin/rpm" <<'EOF'
#!/usr/bin/env bash
printf 'x86_64\n'
EOF
  chmod +x "${test_root}/bin/rpm"

  export PATCHING_CONFIG_ROOT="${test_root}/etc/patching"
  export PATCHING_OS_RELEASE_FILE="${test_root}/missing-os-release"
  # shellcheck source=../scripts/system-update.sh
  source "${PROJECT_DIR}/scripts/system-update.sh"
  export PATH="${test_root}/bin:${PATH}"
  log() { :; }
  error() { printf 'ERROR: %s\n' "$*" >&2; }

  load_configs
  assert_equal "manual" "${REPO_PROFILE}" "manual profile"
  assert_equal "2" "${#ENABLED_REPOS[@]}" "manual repository deduplication"
  assert_equal 'corp-*' "${ENABLED_REPOS[1]}" "repository glob preservation"
  assert_equal "2" "${#BLACKLISTED_PACKAGES[@]}" "package deduplication"
  assert_equal 'package with spaces' "${BLACKLISTED_PACKAGES[1]}" \
    "package whitespace preservation"

  build_dnf_args
  local arg
  for arg in "${DNF_ARGS[@]}"; do
    [[ -n "${arg}" ]] || fail_test "DNF arguments contain an empty value"
  done
)

run_site_profile_case() (
  local test_root
  test_root="$(mktemp -d)"
  trap 'rm -rf -- "${test_root}"' EXIT

  mkdir -p "${test_root}/etc/patching/repos-config.d" \
           "${test_root}/etc/patching/repos-profiles.d" \
           "${test_root}/bin"

  printf 'REPO_PROFILE="corp-rhel-9"\n' \
    >"${test_root}/etc/patching/repos-config"
  printf 'ENABLED_REPOS+=(corp-baseos corp-appstream)\n' \
    >"${test_root}/etc/patching/repos-profiles.d/corp-rhel-9.conf"
  cat >"${test_root}/bin/rpm" <<'EOF'
#!/usr/bin/env bash
printf 'x86_64\n'
EOF
  chmod +x "${test_root}/bin/rpm"

  export PATCHING_CONFIG_ROOT="${test_root}/etc/patching"
  export PATCHING_OS_RELEASE_FILE="${test_root}/missing-os-release"
  # shellcheck source=../scripts/system-update.sh
  source "${PROJECT_DIR}/scripts/system-update.sh"
  export PATH="${test_root}/bin:${PATH}"
  log() { :; }
  error() { printf 'ERROR: %s\n' "$*" >&2; }

  load_configs
  assert_equal "corp-rhel-9" "${REPO_PROFILE}" "site profile selection"
  assert_equal "2" "${#ENABLED_REPOS[@]}" "site profile repository count"
  array_contains corp-baseos "${ENABLED_REPOS[@]}" ||
    fail_test "missing site BaseOS repository"
  array_contains corp-appstream "${ENABLED_REPOS[@]}" ||
    fail_test "missing site AppStream repository"
)

run_invalid_profile_case() (
  local test_root
  test_root="$(mktemp -d)"
  trap 'rm -rf -- "${test_root}"' EXIT

  mkdir -p "${test_root}/etc/patching/repos-config.d" \
           "${test_root}/etc/patching/repos-profiles.d" \
           "${test_root}/bin"
  printf 'REPO_PROFILE="../outside"\n' \
    >"${test_root}/etc/patching/repos-config"
  cat >"${test_root}/bin/rpm" <<'EOF'
#!/usr/bin/env bash
printf 'x86_64\n'
EOF
  chmod +x "${test_root}/bin/rpm"

  export PATCHING_CONFIG_ROOT="${test_root}/etc/patching"
  export PATCHING_OS_RELEASE_FILE="${test_root}/missing-os-release"
  # shellcheck source=../scripts/system-update.sh
  source "${PROJECT_DIR}/scripts/system-update.sh"
  export PATH="${test_root}/bin:${PATH}"
  log() { :; }
  error() { :; }

  if load_configs; then
    fail_test "unsafe profile name was accepted"
  fi
)

run_auto_case rhel 8 rhel-8
run_auto_case rhel 9 rhel-9
run_auto_case rhel 10 rhel-10
run_auto_case rhel 8 rhel-payg-8 rhel-8-baseos-rhui-rpms
run_auto_case rhel 9 rhel-payg-9 rhel-9-for-x86_64-baseos-rhui-rpms
run_auto_case rhel 10 rhel-payg-10 rhel-10-for-x86_64-appstream-rhui-rpms-8
run_auto_case rocky 8 rocky-8
run_auto_case rocky 9 rocky-9
run_auto_case rocky 10 rocky-10
run_auto_case almalinux 8 alma-8
run_auto_case almalinux 9 alma-9
run_auto_case almalinux 10 alma-10
run_auto_case ol 8 oracle-8
run_auto_case ol 9 oracle-9
run_auto_case ol 10 oracle-10
run_manual_case
run_site_profile_case
run_invalid_profile_case

[[ ! -e "${PROJECT_DIR}/config/patching/repos-config.d/10-epel.conf" ]] ||
  fail_test "EPEL is enabled by default"
[[ -e "${PROJECT_DIR}/config/patching/repos-config.d/10-epel.conf.disabled" ]] ||
  fail_test "disabled EPEL example is missing"

printf 'PASS: repository profile tests\n'
