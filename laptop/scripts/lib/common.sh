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
