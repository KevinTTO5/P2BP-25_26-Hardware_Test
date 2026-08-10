#!/usr/bin/env bash
# installer/bootstrap/bootstrap.sh
#
# Bare-metal bootstrap for the mv3dt-installer binary.
# Implements doc 00-FRAMEWORK-AND-BOOTSTRAP.md §5 (§5.1 responsibilities,
# §5.2 idempotency) — nothing else. It does NOT implement any step1-7
# business logic; that lives in the Python mv3dt_installer package this
# script builds and launches.
#
# Delivered two ways, identical behavior:
#   curl -fsSL https://raw.githubusercontent.com/KevinTTO5/P2BP-25_26-Hardware_Test/main/installer/bootstrap/bootstrap.sh | sudo -E bash
#   sudo -E bash /media/usb/bootstrap.sh
#
# If this script is already sitting inside a real clone of the repo
# (installer/bootstrap/bootstrap.sh next to a .git directory), it skips
# the clone step and builds from that local tree instead (doc 00 §5,
# "USB-file variant").
#
# This is a self-contained sibling of laptop/ (doc 00 §3.1): it must run
# from a bare `curl | bash` context before any local clone exists, so it
# does NOT source laptop/scripts/lib/common.sh or anything under laptop/.
#
# Responsibilities, in order (doc 00 §5.1):
#   1. Preflight: Ubuntu 24.04 + x86_64, root via sudo, real SUDO_USER.
#   2. Install git + build deps (single apt transaction).
#   3. Clone the public repo (no auth) — or reuse the local tree.
#   4. Capture the NGC API key -> <install_dir>/secrets/ngc.env.
#   5. Capture the web-app credential, only if MV3DT_WEBAPP_INTEGRATION=on
#      -> <install_dir>/secrets/webapp.env.
#   6. Build mv3dt-installer with PyInstaller, as the invoking user.
#   7. Launch mv3dt-installer as root.
#
# Idempotent (doc 00 §5.2): re-running is safe. apt installs are no-ops
# when satisfied, the clone is refreshed with `git pull` if it already
# exists, and the binary is rebuilt only if missing or --rebuild is passed.

set -euo pipefail

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------

readonly REPO_URL="https://github.com/KevinTTO5/P2BP-25_26-Hardware_Test.git"
readonly DEFAULT_CLONE_DIRNAME="P2BP-25_26-Hardware_Test"

# -----------------------------------------------------------------------
# Logging (minimal — the Python side owns the full logs.py/§8 contract;
# this is just enough for an auditable bootstrap transcript on stderr).
# -----------------------------------------------------------------------

if [[ -t 2 ]]; then
  C_INFO=$'\033[0;36m'; C_WARN=$'\033[0;33m'; C_ERROR=$'\033[0;31m'; C_RESET=$'\033[0m'
else
  C_INFO=""; C_WARN=""; C_ERROR=""; C_RESET=""
fi

_ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

log_info()  { printf '%s[%s] [INFO ] %s%s\n'  "$C_INFO"  "$(_ts)" "$*" "$C_RESET" >&2; }
log_warn()  { printf '%s[%s] [WARN ] %s%s\n'  "$C_WARN"  "$(_ts)" "$*" "$C_RESET" >&2; }
log_error() { printf '%s[%s] [ERROR] %s%s\n'  "$C_ERROR" "$(_ts)" "$*" "$C_RESET" >&2; }
die() { log_error "$*"; exit 1; }

# -----------------------------------------------------------------------
# Usage
# -----------------------------------------------------------------------

usage() {
  cat <<'EOF'
Usage:
  curl -fsSL https://raw.githubusercontent.com/KevinTTO5/P2BP-25_26-Hardware_Test/main/installer/bootstrap/bootstrap.sh | sudo -E bash
  sudo -E bash /media/usb/bootstrap.sh
  sudo -E bash installer/bootstrap/bootstrap.sh [OPTIONS]

Bare-metal bootstrap for the mv3dt-installer binary (doc 00 §5). Brings a
fresh Ubuntu 24.04 x86_64 workstation from nothing to a launched
mv3dt-installer: installs git + build deps, clones this public repo (or
reuses the local tree if already running from inside a clone), captures
the NGC API key and, if enabled, the web-app credential, builds
mv3dt-installer with PyInstaller, and launches it as root.

Must be run as root via sudo, from a real non-root login, so the invoking
user (SUDO_USER) can be resolved for cloning, secret ownership, and the
PyInstaller build.

Options:
  --rebuild            Force a rebuild of mv3dt-installer even if
                        installer/dist/mv3dt-installer already exists.
  --install-dir PATH   Where secrets/ live at this bootstrap stage.
                        Default: /opt/mv3dt. May also be set via the
                        INSTALL_DIR environment variable.
  --clone-dir PATH     Where to clone/refresh the repo when not already
                        running from inside a clone. Default:
                        <SUDO_USER home>/P2BP-25_26-Hardware_Test. May
                        also be set via the CLONE_DIR environment
                        variable.
  -h, --help            Show this help and exit.

Environment variables consulted:
  MV3DT_WEBAPP_INTEGRATION=on   Prompt for and store a web-app API key +
                                 endpoint (doc 00 §5.1 step 5 / §14).
  INSTALL_DIR / CLONE_DIR       See --install-dir / --clone-dir above.

Idempotent (doc 00 §5.2): safe to re-run. apt installs are no-ops when
already satisfied, the clone is refreshed with 'git pull' if it already
exists, and the binary is rebuilt only if missing or --rebuild is passed.

From here on, mv3dt-installer itself owns the resumable install (state
machine + dispatch loop, doc 00 §3.2 / §6) — this bootstrap script only
needs to run once, or again after a 'git pull' + rebuild.
EOF
}

# -----------------------------------------------------------------------
# Globals populated during the run
# -----------------------------------------------------------------------

REBUILD=0
INSTALL_DIR="${INSTALL_DIR:-/opt/mv3dt}"
CLONE_DIR="${CLONE_DIR:-}"
SUDO_USER_HOME=""
REPO_ROOT=""

# -----------------------------------------------------------------------
# Arg parsing
# -----------------------------------------------------------------------

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --rebuild)
        REBUILD=1
        ;;
      --install-dir)
        [[ $# -ge 2 ]] || die "--install-dir requires a PATH argument."
        INSTALL_DIR="$2"
        shift
        ;;
      --clone-dir)
        [[ $# -ge 2 ]] || die "--clone-dir requires a PATH argument."
        CLONE_DIR="$2"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        log_error "Unknown argument: $1"
        usage
        exit 2
        ;;
    esac
    shift
  done
}

# -----------------------------------------------------------------------
# Interactive prompt helpers.
#
# Under `curl | sudo -E bash`, this script's own stdin IS the piped
# script text, so a plain `read` would consume script bytes instead of
# operator input. We read from /dev/tty explicitly and treat "no
# controlling terminal" as "operator declined" (blank), never as a hang
# or a crash — matching the NGC/web-app "blank is a valid answer"
# contract (doc 00 §5.1 steps 4-5).
# -----------------------------------------------------------------------

have_tty() {
  [[ -r /dev/tty && -w /dev/tty ]]
}

prompt_secret() {
  local prompt_text="$1"
  local value=""
  if have_tty; then
    read -r -s -p "$prompt_text" value < /dev/tty
    printf '\n' >&2
  else
    log_warn "No controlling terminal available; skipping prompt: ${prompt_text}"
  fi
  printf '%s' "$value"
}

prompt_plain() {
  local prompt_text="$1"
  local value=""
  if have_tty; then
    read -r -p "$prompt_text" value < /dev/tty
  else
    log_warn "No controlling terminal available; skipping prompt: ${prompt_text}"
  fi
  printf '%s' "$value"
}

# -----------------------------------------------------------------------
# Step 1 (doc 00 §5.1.1) — preflight: OS/arch + root/SUDO_USER.
# Mirrors the *checks* of laptop/scripts/00_bootstrap.sh's
# run_phase0_preflight (lsb_release / uname -m / require_root), reimplemented
# standalone since this tree must run before any local clone exists and must
# not source laptop/scripts/lib/common.sh (doc 00 §3.1).
# -----------------------------------------------------------------------

preflight_os_arch() {
  if command -v lsb_release >/dev/null 2>&1; then
    local distro release
    distro="$(lsb_release -is 2>/dev/null || echo unknown)"
    release="$(lsb_release -rs 2>/dev/null || echo 0)"
    if [[ "$distro" != "Ubuntu" || "$release" != "24.04" ]]; then
      die "Ubuntu 24.04 required (found: ${distro} ${release})."
    fi
    log_info "OS OK: ${distro} ${release}"
  else
    log_warn "lsb_release not found; cannot verify Ubuntu 24.04 — proceeding."
  fi

  local arch
  arch="$(uname -m)"
  if [[ "$arch" != "x86_64" ]]; then
    die "x86_64 required (found: ${arch})."
  fi
  log_info "Architecture OK: ${arch}"
}

preflight_root_and_user() {
  if [[ "$(id -u)" -ne 0 ]]; then
    die "This script must be run as root. Re-run via: sudo -E bash $0 (or pipe through 'sudo -E bash')."
  fi

  if [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == "root" ]]; then
    die "SUDO_USER is unset or 'root'. Run this via 'sudo -E bash ...' from a real" \
        " non-root login — a bare root shell cannot resolve an invoking user for" \
        " cloning, secret ownership, or the PyInstaller build."
  fi

  if ! id -u "$SUDO_USER" >/dev/null 2>&1; then
    die "SUDO_USER='${SUDO_USER}' is not a valid login on this system."
  fi

  log_info "Running as root via sudo; invoking user: ${SUDO_USER}"
}

resolve_sudo_user_home() {
  SUDO_USER_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
  if [[ -z "$SUDO_USER_HOME" || ! -d "$SUDO_USER_HOME" ]]; then
    die "Could not resolve a home directory for SUDO_USER='${SUDO_USER}' via getent passwd."
  fi
  log_info "Resolved SUDO_USER home: ${SUDO_USER_HOME}"

  if [[ -z "$CLONE_DIR" ]]; then
    CLONE_DIR="${SUDO_USER_HOME}/${DEFAULT_CLONE_DIRNAME}"
  fi
}

# -----------------------------------------------------------------------
# Step 2 (doc 00 §5.1.2) — git + build deps, single apt transaction.
# -----------------------------------------------------------------------

install_build_deps() {
  log_info "Installing git + build dependencies (single apt transaction)."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends \
    git \
    python3 \
    python3-venv \
    python3-pip \
    ca-certificates \
    curl \
    build-essential
  log_info "git + build dependencies installed (already-satisfied packages are a no-op)."
}

# -----------------------------------------------------------------------
# Step 3 (doc 00 §5.1.3) — clone the public repo, no auth; or reuse the
# local tree if this script is already running from inside a real clone.
# -----------------------------------------------------------------------

clone_or_update_repo() {
  local script_path="" script_real="" candidate_root=""
  script_path="${BASH_SOURCE[0]:-}"
  if [[ -n "$script_path" && -f "$script_path" ]]; then
    script_real="$(readlink -f "$script_path" 2>/dev/null || true)"
    if [[ "$script_real" == */installer/bootstrap/bootstrap.sh ]]; then
      candidate_root="${script_real%/installer/bootstrap/bootstrap.sh}"
      if [[ -d "$candidate_root/.git" ]]; then
        REPO_ROOT="$candidate_root"
        log_info "Already running from inside a clone at ${REPO_ROOT}; skipping clone step."
        return 0
      fi
    fi
  fi

  export GIT_TERMINAL_PROMPT=0
  if [[ -d "$CLONE_DIR/.git" ]]; then
    log_info "Existing clone found at ${CLONE_DIR}; refreshing with git pull."
    sudo -u "$SUDO_USER" -H env GIT_TERMINAL_PROMPT=0 git -C "$CLONE_DIR" pull --ff-only \
      || die "git pull failed in ${CLONE_DIR}."
  elif [[ -e "$CLONE_DIR" ]]; then
    die "${CLONE_DIR} exists and is not a git repository; move it aside or pass" \
        " --clone-dir/CLONE_DIR to pick a different path."
  else
    log_info "Cloning ${REPO_URL} into ${CLONE_DIR} (public repo, no auth)."
    mkdir -p "$(dirname "$CLONE_DIR")"
    sudo -u "$SUDO_USER" -H env GIT_TERMINAL_PROMPT=0 \
      git clone --depth 1 "$REPO_URL" "$CLONE_DIR" \
      || die "git clone failed. If this repo is ever made private, bootstrap.sh has" \
             " no credential path (doc 00 §5.1.3) — GIT_TERMINAL_PROMPT=0 fails fast" \
             " instead of hanging on a credential prompt."
  fi
  REPO_ROOT="$CLONE_DIR"
}

# -----------------------------------------------------------------------
# Step 4 (doc 00 §5.1.4) — NGC API key capture + local secure storage.
# Storage contract mirrors §10.1 so ngc.py (not yet built) can read it back.
# -----------------------------------------------------------------------

capture_and_store_ngc_key() {
  log_info "NGC API key capture (input hidden; blank -> manual download fallback)."
  local key
  key="$(prompt_secret 'NGC API key (blank to skip, see docs for manual download): ')"

  local secrets_dir="${INSTALL_DIR}/secrets"
  mkdir -p "$secrets_dir"
  chmod 700 "$secrets_dir"

  local ngc_env="${secrets_dir}/ngc.env"
  if [[ -n "$key" ]]; then
    (
      umask 0177
      printf 'NGC_API_KEY=%s\n' "$key" > "$ngc_env"
    )
    log_info "NGC API key stored at ${ngc_env} (redacted in all logs: NGC_API_KEY=<redacted>)."
  else
    (
      umask 0177
      printf '# NGC_API_KEY intentionally left unset -- operator chose the manual\n# download fallback (doc 00 §10.3). Step 2 will surface guided\n# instructions instead of reading a key from this file.\n' > "$ngc_env"
    )
    log_warn "No NGC API key entered; recorded manual download fallback at ${ngc_env}."
  fi
  chmod 600 "$ngc_env"
  chown "$SUDO_USER" "$secrets_dir" "$ngc_env" 2>/dev/null || true
  key=""
}

# -----------------------------------------------------------------------
# Step 5 (doc 00 §5.1.5) — web-app credential, only when
# MV3DT_WEBAPP_INTEGRATION=on. Raw values only: endpoint normalization
# (§14.2) belongs to webapp.py, not this script.
# -----------------------------------------------------------------------

capture_and_store_webapp_credentials() {
  if [[ "${MV3DT_WEBAPP_INTEGRATION:-off}" != "on" ]]; then
    log_info "MV3DT_WEBAPP_INTEGRATION is not 'on'; skipping web-app credential capture."
    return 0
  fi

  log_info "MV3DT_WEBAPP_INTEGRATION=on; capturing web-app credential."
  local api_key endpoint
  api_key="$(prompt_secret 'Web-app API key (input hidden): ')"
  endpoint="$(prompt_plain 'Web-app endpoint URL: ')"

  local secrets_dir="${INSTALL_DIR}/secrets"
  mkdir -p "$secrets_dir"
  chmod 700 "$secrets_dir"

  local webapp_env="${secrets_dir}/webapp.env"
  (
    umask 0177
    {
      printf 'API_KEY=%s\n' "$api_key"
      printf 'ENDPOINT=%s\n' "$endpoint"
    } > "$webapp_env"
  )
  chmod 600 "$webapp_env"
  chown "$SUDO_USER" "$secrets_dir" "$webapp_env" 2>/dev/null || true

  log_info "Web-app credential stored at ${webapp_env} (API_KEY redacted in all logs;" \
           " ENDPOINT stored as entered, not normalized here — see doc 00 §14.2, owned by webapp.py)."
  api_key=""
}

# -----------------------------------------------------------------------
# Step 6 (doc 00 §5.1.6) — build mv3dt-installer with PyInstaller, as the
# invoking user. Rebuilt only if missing or --rebuild was passed (§5.2).
# -----------------------------------------------------------------------

build_installer_binary() {
  local spec="${REPO_ROOT}/installer/installer.spec"
  local dist_bin="${REPO_ROOT}/installer/dist/mv3dt-installer"

  if [[ -x "$dist_bin" && "$REBUILD" -eq 0 ]]; then
    log_info "mv3dt-installer already built at ${dist_bin}; skipping (pass --rebuild to force)."
    return 0
  fi

  if [[ ! -f "$spec" ]]; then
    die "installer/installer.spec not found under ${REPO_ROOT}. It is built by the" \
        " packaging module (doc 00 §4), which may not have merged into this tree yet."
  fi

  log_info "Installing PyInstaller for ${SUDO_USER} (pip install --user)."
  sudo -u "$SUDO_USER" -H python3 -m pip install --user --quiet pyinstaller \
    || die "pip install --user pyinstaller failed."

  log_info "Building mv3dt-installer with PyInstaller (as ${SUDO_USER})."
  local build_script
  build_script="$(mktemp)"
  cat > "$build_script" <<'BUILD'
#!/usr/bin/env bash
set -euo pipefail
cd "$1"
export PATH="$HOME/.local/bin:$PATH"
pyinstaller installer/installer.spec --distpath installer/dist --workpath /tmp/mv3dt-build
BUILD
  chmod 0755 "$build_script"
  if ! sudo -u "$SUDO_USER" -H "$build_script" "$REPO_ROOT"; then
    rm -f "$build_script"
    die "PyInstaller build failed."
  fi
  rm -f "$build_script"

  [[ -x "$dist_bin" ]] || die "PyInstaller reported success but ${dist_bin} was not produced."
  log_info "Built ${dist_bin}."
}

# -----------------------------------------------------------------------
# Step 7 (doc 00 §5.1.7) — launch mv3dt-installer. From here the binary
# owns the flow (state machine + dispatch loop, doc 00 §3.2).
# -----------------------------------------------------------------------

launch_installer() {
  local dist_bin="${REPO_ROOT}/installer/dist/mv3dt-installer"
  if [[ ! -x "$dist_bin" ]]; then
    die "Cannot launch: ${dist_bin} is missing or not executable."
  fi
  log_info "Launching mv3dt-installer."
  exec sudo -E "$dist_bin"
}

# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

main() {
  parse_args "$@"

  preflight_root_and_user
  preflight_os_arch
  resolve_sudo_user_home

  log_info "install_dir=${INSTALL_DIR} clone_dir=${CLONE_DIR}"

  install_build_deps
  clone_or_update_repo
  capture_and_store_ngc_key
  capture_and_store_webapp_credentials
  build_installer_binary
  launch_installer
}

main "$@"
