#!/usr/bin/env bash
# shellcheck disable=SC1090

set -o errexit
set -o nounset

readonly CONFIG_ROOT="${PATCHING_CONFIG_ROOT:-/etc/patching}"
readonly CONFIG_BASE="${CONFIG_ROOT}/repos-config"
readonly CONFIG_DIR="${CONFIG_ROOT}/repos-config.d"
readonly PROFILE_DIR="${CONFIG_ROOT}/repos-profiles.d"
readonly OS_RELEASE_FILE="${PATCHING_OS_RELEASE_FILE:-/etc/os-release}"
readonly LOG_DIR="${PATCHING_LOG_DIR:-/var/log/patching}"
readonly LOCK_DIR="${PATCHING_LOCK_DIR:-/run/lock/patching}"
readonly LOCK_FILE="${LOCK_DIR}/patching.lock"

export PATH="/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

DRY_RUN="0"
DEBUG="0"

# --- Globals composed at runtime ---
LOG_FILE=""
ERR_FILE=""

# --- Arrays that configs can extend ---
# Enabled repositories
ENABLED_REPOS=()
# Repositories explicitly disabled (wins last)
DISABLED_REPOS=()
# Packages to exclude from updates
BLACKLISTED_PACKAGES=()
# Extra arbitrary DNF options (array-safe)
EXTRA_DNF_OPTS=()

# Repository profile selected by config or automatic platform detection.
REPO_PROFILE="auto"
PATCHING_ARCH=""
DETECTED_OS_ID=""
DETECTED_OS_MAJOR=""

usage() {
  cat <<'EOF'
Usage: system-update.sh [--dry-run] [--debug]

Options:
  --dry-run   List available updates without applying them (dnf check-update)
  --debug     Increase verbosity for troubleshooting
EOF
}

log_setup() {
  mkdir -p -m 0750 "${LOG_DIR}"
  local ts pid
  ts="$(date -u +%F.%H-%M)"
  pid="$$"
  LOG_FILE="${LOG_DIR}/patching.${ts}.${pid}.log"
  ERR_FILE="${LOG_DIR}/patching.${ts}.${pid}.err.log"
  if ! ln -sfn "${LOG_FILE}" "${LOG_DIR}/latest.log"; then
    warn "Could not update ${LOG_DIR}/latest.log; continuing with ${LOG_FILE}."
  fi
  if ! ln -sfn "${ERR_FILE}" "${LOG_DIR}/latest.err.log"; then
    warn "Could not update ${LOG_DIR}/latest.err.log; continuing with ${ERR_FILE}."
  fi
}

log() {
  local line
  line="$(date -u +'%F %T UTC') [INFO] $*"
  if [[ -n "${LOG_FILE}" ]]; then
    printf '%s\n' "${line}" | tee -a "${LOG_FILE}"
  else
    printf '%s\n' "${line}"
  fi
}

warn() {
  local line
  line="$(date -u +'%F %T UTC') [WARN] $*"
  if [[ -n "${LOG_FILE}" && -n "${ERR_FILE}" ]]; then
    printf '%s\n' "${line}" | tee -a "${LOG_FILE}" | tee -a "${ERR_FILE}" >&2
  else
    printf '%s\n' "${line}" >&2
  fi
}

error() {
  local line
  line="$(date -u +'%F %T UTC') [ERROR] $*"
  if [[ -n "${LOG_FILE}" && -n "${ERR_FILE}" ]]; then
    printf '%s\n' "${line}" | tee -a "${LOG_FILE}" | tee -a "${ERR_FILE}" >&2
  else
    printf '%s\n' "${line}" >&2
  fi
}

take_lock() {
  mkdir -p "${LOCK_DIR}"
  exec 9>"${LOCK_FILE}"
  if ! flock -n 9; then
    error "Another patching process is running (lock: ${LOCK_FILE}). Exiting."
    exit 1
  fi
}

cleanup() {
  local rc=$?
  trap - EXIT
  if [[ ${rc} -eq 0 ]]; then
    log "Patching completed successfully."
  else
    error "Patching failed with exit code ${rc}."
  fi
  exit "${rc}"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) DRY_RUN="1"; shift ;;
      --debug)   DEBUG="1"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) error "Unknown option: $1"; usage; exit 2 ;;
    esac
  done
}

array_contains() {
  local needle="$1"; shift
  local e
  for e in "$@"; do [[ "$e" == "$needle" ]] && return 0; done
  return 1
}

dedupe_array() {
  local -n values="$1"
  local -a deduped=()
  local value

  for value in "${values[@]}"; do
    [[ -n "${value}" ]] || continue
    if ! array_contains "${value}" "${deduped[@]}"; then
      deduped+=("${value}")
    fi
  done

  values=("${deduped[@]}")
}

has_rhui_repositories() {
  local major="$1"
  local repo_id ignored

  while read -r repo_id ignored; do
    if [[ ${repo_id} == rhel-"${major}"*-baseos*-rhui-rpms* ]] ||
       [[ ${repo_id} == rhel-"${major}"*-appstream*-rhui-rpms* ]]; then
      return 0
    fi
  done < <(dnf -q repolist --enabled 2>/dev/null || true)

  return 1
}

detect_repo_profile() {
  [[ -r "${OS_RELEASE_FILE}" ]] || {
    error "Cannot auto-detect repository profile: ${OS_RELEASE_FILE} is missing."
    return 1
  }

  local os_id version_id major
  os_id="$(. "${OS_RELEASE_FILE}"; printf '%s' "${ID:-}")"
  version_id="$(. "${OS_RELEASE_FILE}"; printf '%s' "${VERSION_ID:-}")"
  major="${version_id%%.*}"

  [[ -n "${os_id}" && -n "${major}" ]] || {
    error "Cannot auto-detect repository profile: ID or VERSION_ID is missing from ${OS_RELEASE_FILE}."
    return 1
  }

  DETECTED_OS_ID="${os_id}"
  DETECTED_OS_MAJOR="${major}"

  case "${os_id}:${major}" in
    rhel:8|rhel:9|rhel:10)
      if has_rhui_repositories "${major}"; then
        REPO_PROFILE="rhel-payg-${major}"
      else
        REPO_PROFILE="rhel-${major}"
      fi
      ;;
    rocky:8|rocky:9|rocky:10)
      REPO_PROFILE="rocky-${major}"
      ;;
    almalinux:8|almalinux:9|almalinux:10)
      REPO_PROFILE="alma-${major}"
      ;;
    ol:8|ol:9|ol:10)
      REPO_PROFILE="oracle-${major}"
      ;;
    *)
      error "Unsupported platform ${os_id}-${major}; set REPO_PROFILE=manual and define ENABLED_REPOS."
      return 1
      ;;
  esac
}

load_repo_profile() {
  if [[ "${REPO_PROFILE}" == "auto" ]]; then
    detect_repo_profile
  fi

  if [[ "${REPO_PROFILE}" == "manual" ]]; then
    log "Repository profile: manual"
    return 0
  fi

  # Profiles are extensible: any safely named .conf file in PROFILE_DIR can be
  # selected. Reject separators and shell metacharacters so the configured name
  # cannot escape PROFILE_DIR or alter the constructed path.
  if [[ ! "${REPO_PROFILE}" =~ ^[[:alnum:]][[:alnum:]_.-]*$ ]]; then
    error "Invalid repository profile name: ${REPO_PROFILE}"
    return 1
  fi

  local profile_file="${PROFILE_DIR}/${REPO_PROFILE}.conf"
  [[ -r "${profile_file}" ]] || {
    error "Repository profile is not readable: ${profile_file}"
    return 1
  }

  # shellcheck disable=SC1090
  source "${profile_file}"
  log "Repository profile: ${REPO_PROFILE}"
}

load_configs() {
  # Base config first
  if [[ -r "${CONFIG_BASE}" ]]; then
    # shellcheck source=/etc/patching/repos-config
    source "${CONFIG_BASE}"
    [[ ${DEBUG} -eq 1 ]] && log "Loaded base config: ${CONFIG_BASE}"
  fi

  PATCHING_ARCH="$(rpm --eval '%{_arch}')"
  [[ -n "${PATCHING_ARCH}" && "${PATCHING_ARCH}" != '%{_arch}' ]] || {
    error "Cannot determine RPM architecture."
    return 1
  }

  load_repo_profile

  # Site and role drop-ins are loaded after the selected platform profile.
  if [[ -d "${CONFIG_DIR}" ]]; then
    while IFS= read -r -d '' f; do
      # shellcheck disable=SC1090
      source "${f}"
      [[ ${DEBUG} -eq 1 ]] && log "Loaded drop-in: ${f}"
    done < <(find "${CONFIG_DIR}" -maxdepth 1 -type f -name "*.conf" -print0 | sort -z)
  fi

  # Manual mode must provide at least one repository; automatic mode should
  # always get repositories from its selected profile.
  if [[ ${#ENABLED_REPOS[@]} -eq 0 ]]; then
    error "No repositories are enabled by profile ${REPO_PROFILE}."
    return 1
  fi

  : "${ACCEPT_EULA:=Y}"
  export ACCEPT_EULA

  # De-duplicate without word splitting or pathname expansion. This preserves
  # repository wildcards, package globs, and options containing whitespace.
  dedupe_array ENABLED_REPOS
  dedupe_array DISABLED_REPOS
  dedupe_array BLACKLISTED_PACKAGES
  dedupe_array EXTRA_DNF_OPTS

  if [[ ${DEBUG} -eq 1 ]]; then
    log "ENABLED_REPOS=(${ENABLED_REPOS[*]})"
    log "DISABLED_REPOS=(${DISABLED_REPOS[*]})"
    log "BLACKLISTED_PACKAGES=(${BLACKLISTED_PACKAGES[*]})"
    log "EXTRA_DNF_OPTS=(${EXTRA_DNF_OPTS[*]})"
  fi
}

build_dnf_args() {
  # Always start by disabling all repos (safe default)
  local args=(--disablerepo='*')

  # Then explicitly enable requested repos
  local r
  for r in "${ENABLED_REPOS[@]}"; do
    # If a repo appears in DISABLED_REPOS, we still add enable here;
    # DISABLED_REPOS will be appended after and thus win.
    args+=(--enablerepo="$r")
  done

  # Explicit disables win last
  for r in "${DISABLED_REPOS[@]}"; do
    args+=(--disablerepo="$r")
  done

  # Exclusions
  local p
  for p in "${BLACKLISTED_PACKAGES[@]}"; do
    args+=(--exclude "$p")
  done

  # Extra options (array-safe)
  args+=("${EXTRA_DNF_OPTS[@]}")

  # Verbosity and assume flags
  if [[ ${DRY_RUN} -eq 1 ]]; then
    args=(-v -d 3 "${args[@]}")
  else
    args=(-v -d 3 -y "${args[@]}")
  fi

  # Export as a global array for later use
  DNF_ARGS=("${args[@]}")
  if [[ ${DEBUG} -eq 1 ]]; then
    log "DNF_ARGS=(${DNF_ARGS[*]})"
  fi

}

dnf_clean_all() {
  log "Cleaning DNF metadata and caches…"
  if ! dnf -q clean all >>"${LOG_FILE}" 2>>"${ERR_FILE}"; then
    warn "dnf clean all returned non-zero."
  fi
  rm -rf /var/cache/yum/* /var/cache/dnf/* || true
}

run_update() {
  local command rc

  if [[ ${DRY_RUN} -eq 1 ]]; then
    command="check-update"
    log "Previewing available updates: dnf ${DNF_ARGS[*]} ${command}"
  else
    command="update"
    log "Running: dnf ${DNF_ARGS[*]} ${command}"
  fi

  if command -v stdbuf >/dev/null 2>&1; then
    if stdbuf -oL -eL dnf "${DNF_ARGS[@]}" "${command}" \
        >>"${LOG_FILE}" 2>>"${ERR_FILE}"; then
      rc=0
    else
      rc=$?
    fi
  elif dnf "${DNF_ARGS[@]}" "${command}" \
      >>"${LOG_FILE}" 2>>"${ERR_FILE}"; then
    rc=0
  else
    rc=$?
  fi

  if [[ ${DRY_RUN} -eq 1 && ${rc} -eq 100 ]]; then
    log "Updates are available; DNF check-update returned 100 as expected."
    return 0
  fi

  return "${rc}"
}

main() {
  parse_args "$@"
  log_setup
  take_lock
  trap cleanup EXIT
  load_configs
  build_dnf_args
  if [[ ${DRY_RUN} -eq 1 ]]; then
    log "Dry-run: skipping DNF cache cleanup. Metadata may still be refreshed by DNF."
  else
    dnf_clean_all
  fi
  run_update
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

