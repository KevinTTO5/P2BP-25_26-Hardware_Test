# laptop/scripts/lib/common.sh
#
# Shared helpers for the laptop/ DS 9.0 scripted testing harness. Sourced by
# every script under laptop/scripts/. Never executed on its own.
#
# Scope (plan: laptop-scripts-common):
#   - Locate the repo root from any script under laptop/scripts/.
#   - Load laptop/config/laptop.env safely (export all set vars).
#   - Emit consistent, colour-aware logs to stderr so stdout stays reserved
#     for tool-parsable output (pass/fail tables, rendered configs, etc.).
#   - require_root / require_tool preflight helpers.
#
# Isolation: this file only reads from and writes to paths under laptop/. It
# never reaches into the repo-root scripts/, services/, config/, models/,
# my-docs/, homographies/, or virtual-cameras/ trees.

# Guard: cowardly refuse if the caller ran `bash common.sh` directly.
if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "laptop/scripts/lib/common.sh must be sourced, not executed." >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

if [[ -t 2 ]]; then
  _C_RED=$'\033[31m'
  _C_YEL=$'\033[33m'
  _C_GRN=$'\033[32m'
  _C_DIM=$'\033[2m'
  _C_RST=$'\033[0m'
else
  _C_RED=""; _C_YEL=""; _C_GRN=""; _C_DIM=""; _C_RST=""
fi

_log_script_tag() {
  local src="${BASH_SOURCE[2]:-${BASH_SOURCE[1]:-$0}}"
  printf '%s' "$(basename "$src")"
}

log_info()  { printf '%s[info ]%s %s: %s\n'  "$_C_GRN" "$_C_RST" "$(_log_script_tag)" "$*" >&2; }
log_warn()  { printf '%s[warn ]%s %s: %s\n'  "$_C_YEL" "$_C_RST" "$(_log_script_tag)" "$*" >&2; }
log_error() { printf '%s[error]%s %s: %s\n'  "$_C_RED" "$_C_RST" "$(_log_script_tag)" "$*" >&2; }
log_debug() {
  [[ "${LAPTOP_DEBUG:-0}" -eq 1 ]] || return 0
  printf '%s[debug]%s %s: %s\n' "$_C_DIM" "$_C_RST" "$(_log_script_tag)" "$*" >&2
}

die() {
  log_error "$*"
  exit 1
}

# ---------------------------------------------------------------------------
# Preflight helpers
# ---------------------------------------------------------------------------

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    die "This script must be run as root (use sudo)."
  fi
}

require_tool() {
  local tool="$1"
  if ! command -v "$tool" >/dev/null 2>&1; then
    die "Required tool '$tool' not found on PATH. Install it (or re-run laptop/scripts/00_bootstrap.sh)."
  fi
}

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------

# repo_root: absolute path to the cloned P2BP-25_26-Hardware_Test checkout,
# derived from this file's location (laptop/scripts/lib/common.sh -> 3 up).
repo_root() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  (cd "$here/../../.." && pwd)
}

# ---------------------------------------------------------------------------
# Env loader
# ---------------------------------------------------------------------------
#
# Loads laptop/config/laptop.env if present. The file uses plain KEY=VALUE
# lines (same shape as install.sh's agent.env) so we can source it with
# `set -a` to export every variable. Missing file is not fatal — individual
# scripts check for the vars they need.
load_env() {
  local env_file
  env_file="$(repo_root)/laptop/config/laptop.env"
  if [[ ! -f "$env_file" ]]; then
    log_warn "laptop/config/laptop.env not found; using shell env only."
    log_warn "Run laptop/scripts/00_bootstrap.sh to create it from laptop.env.example."
    return 0
  fi
  log_debug "Loading $env_file"
  # shellcheck disable=SC1090
  set -a; . "$env_file"; set +a
}

# ---------------------------------------------------------------------------
# Bootstrap phase state machine  (used by 00_bootstrap.sh; safe to source
# from other scripts that only need to inspect state without writing it)
# ---------------------------------------------------------------------------

# Path to the bootstrap state file. Can be overridden by exporting
# BOOTSTRAP_STATE_FILE before sourcing this library.
BOOTSTRAP_STATE_FILE="${BOOTSTRAP_STATE_FILE:-/var/lib/mv3dt-laptop-bootstrap.state}"

# phase_done <name>
# Returns 0 (success/true) if <name>=done exists in the state file.
phase_done() {
  local name="$1"
  [[ -f "$BOOTSTRAP_STATE_FILE" ]] && grep -qx "${name}=done" "$BOOTSTRAP_STATE_FILE"
}

# mark_phase_done <name>
# Appends "<name>=done" to the state file, creating it if absent.
# Duplicate lines are not appended.
mark_phase_done() {
  local name="$1"
  touch "$BOOTSTRAP_STATE_FILE"
  chmod 0644 "$BOOTSTRAP_STATE_FILE"
  if grep -qx "${name}=done" "$BOOTSTRAP_STATE_FILE" 2>/dev/null; then
    return 0
  fi
  printf '%s=done\n' "$name" >> "$BOOTSTRAP_STATE_FILE"
  log_debug "Bootstrap phase '${name}' marked done in ${BOOTSTRAP_STATE_FILE}."
}

# reset_state
# Wipes the bootstrap state file so the next run starts from Phase 0.
reset_state() {
  rm -f "$BOOTSTRAP_STATE_FILE"
  log_info "Bootstrap state file removed: ${BOOTSTRAP_STATE_FILE}"
}

# ---------------------------------------------------------------------------
# Local .deb locator
# ---------------------------------------------------------------------------

# find_local_deb <glob_pattern> <search_dir>
# Searches <search_dir> (non-recursively first, then one level deep) for a
# file matching the shell glob pattern. Prints the first match to stdout and
# returns 0. Prints nothing and returns 1 if no match found.
find_local_deb() {
  local pattern="$1" search_dir="$2" found
  found="$(find "$search_dir" -maxdepth 1 -name "$pattern" 2>/dev/null | head -n1)"
  if [[ -z "$found" ]]; then
    found="$(find "$search_dir" -maxdepth 2 -name "$pattern" 2>/dev/null | head -n1)"
  fi
  if [[ -n "$found" ]]; then
    printf '%s\n' "$found"
    return 0
  fi
  return 1
}

# ---------------------------------------------------------------------------
# Version comparison helpers
# ---------------------------------------------------------------------------

# require_version_eq <label> <actual> <expected>
# Dies with a clear message when actual != expected (exact string match).
require_version_eq() {
  local label="$1" actual="$2" expected="$3"
  if [[ "$actual" != "$expected" ]]; then
    die "Version check failed: ${label} — expected '${expected}', got '${actual}'."
  fi
  log_info "Version OK: ${label} == ${actual}"
}

# require_version_ge <label> <actual_major> <actual_minor> <need_major> <need_minor>
# Dies if (actual_major.actual_minor) is strictly less than (need_major.need_minor).
require_version_ge() {
  local label="$1" a_maj="$2" a_min="$3" n_maj="$4" n_min="$5"
  if (( a_maj < n_maj )) || (( a_maj == n_maj && a_min < n_min )); then
    die "Version check failed: ${label} — need >= ${n_maj}.${n_min}, found ${a_maj}.${a_min}."
  fi
  log_info "Version OK: ${label} >= ${n_maj}.${n_min} (found ${a_maj}.${a_min})"
}

# ---------------------------------------------------------------------------
# Config-file announcement helper
# ---------------------------------------------------------------------------
# announce_config_file <target_path> <purpose_blurb>
#
# Call this immediately BEFORE writing any managed config file. It prints:
#   - The absolute target path
#   - A one-line purpose blurb
#   - The literal content about to be written (when STAGED_CONTENT_FILE is
#     set to a readable path containing the staged content)
#   - An interactive "Press Enter" pause, unless NO_PAUSE=1 or stdin is not
#     a TTY (e.g. CI, --non-interactive, pipe).
#
# Typical caller pattern:
#   local _staged
#   _staged="$(mktemp)"
#   cat > "$_staged" <<'EOF'
#   ... file content ...
#   EOF
#   STAGED_CONTENT_FILE="$_staged"
#   announce_config_file "/etc/foo/bar.conf" "Configures foo for bootstrap."
#   mv -f "$_staged" /etc/foo/bar.conf
#   unset STAGED_CONTENT_FILE
announce_config_file() {
  local target_path="$1" purpose="$2"
  printf '\n%s-- CONFIG FILE --%s\n' "$_C_YEL" "$_C_RST" >&2
  printf '  Path   : %s\n'    "$target_path" >&2
  printf '  Purpose: %s\n'    "$purpose" >&2
  printf '  Remove : sudo bash laptop/scripts/00_bootstrap.sh --uninstall\n' >&2
  if [[ -n "${STAGED_CONTENT_FILE:-}" && -f "$STAGED_CONTENT_FILE" ]]; then
    printf '%s  Content:%s\n' "$_C_DIM" "$_C_RST" >&2
    sed 's/^/    /' "$STAGED_CONTENT_FILE" >&2
  fi
  printf '%s--%s\n' "$_C_YEL" "$_C_RST" >&2
  if [[ "${NO_PAUSE:-0}" -eq 0 && -t 0 ]]; then
    read -r -p "  Press Enter to write this file, or Ctrl-C to abort... " _
  fi
}
