#!/usr/bin/env bash
# laptop/scripts/00_bootstrap.sh
#
# Phased, resumable installer that brings a fresh Ubuntu 24.04 laptop to
# DS 9.0 readiness. All installs use .deb packages only — no .run files,
# tarballs, pip, or source builds.
#
# Phase map:
#   0  Preflight: OS / arch / GPU / deb-only policy banner / manifest table
#   1  Base deps: build-essential, dkms, linux-headers, gnupg, curl, …
#   2  Nouveau cleanup: purge distro nvidia-*, blacklist nouveau (reboot if needed)
#   2b Local .deb verification: confirm driver + cuda-keyring debs are present
#   3  NVIDIA stack: driver 590.48.01 + CUDA 13.1 + cuDNN 9.18 + TRT 10.14 (reboot)
#   4  Runtime deps: GStreamer 1.24, librdkafka, Mosquitto, Docker, NV CTK
#   5  NGC CLI guidance + Enter gate
#   6  DeepStream 9.0 deb download + install
#   7  Post-install: update_rtpmanager.sh + /etc/profile.d/deepstream.sh
#   8  Version audit (hard gate — dies on any mismatch)
#   9  Write laptop/config/laptop.env interactively
#  10  PeopleNet ONNX model download (ngc registry model download-version)
#
# Resume: re-run the script after any reboot; completed phases are skipped
# automatically via /var/lib/mv3dt-laptop-bootstrap.state.
#
# References (via .cursor/skills/deepstream-9-docs/):
#   DS 9.0 Installation:
#     https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Installation.html
#   DS 9.0 Quickstart:
#     https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Quickstart.html

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

# ---------------------------------------------------------------------------
# CLI flags
# ---------------------------------------------------------------------------

# Legacy flags — still honoured for backward compatibility.
SKIP_PREFLIGHT=0
SKIP_INSTALL=0
NONINTERACTIVE=0

# New flags. Default LOCAL_DEB_DIR is set after argv parsing (invoking user's
# ~/Downloads when run under sudo).
LOCAL_DEB_DIR=""
LOCAL_DEB_DIR_FROM_CLI=0
ALLOW_CURL_KEYRING=0
RESET_STATE_FLAG=0
RESUME_FROM=""
NO_PAUSE=0
FORCE_MODELS=0
DO_UNINSTALL=0
PURGE_ENV=0
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: sudo bash laptop/scripts/00_bootstrap.sh [OPTIONS]

Phased .deb-only installer: Ubuntu 24.04 laptop → DS 9.0 + PeopleNet ONNX.

Legacy options (still honoured):
  --skip-preflight       Skip Phase 0 OS/arch/GPU preflight checks.
  --skip-install         Skip install phases 1–10; only write laptop.env.
  --non-interactive      Reuse existing laptop.env values without prompting;
                         implies --no-pause.

New options:
  --local-deb-dir PATH   Directory to search for pre-downloaded .deb files.
                         Default: $HOME/Downloads
  --allow-curl-keyring   Download cuda-keyring_1.1-1_all.deb via curl if it
                         is absent from LOCAL_DEB_DIR (tiny keyring only).
  --reset-state          Wipe the phase state file and start from Phase 0.
  --force-models         Re-run Phase 10 (model download) even if already done.
  --resume-from PHASE    Force-start at the given phase name (debug use only).
  --no-pause             Skip all "Press Enter to write this file" pauses.
  --uninstall            Reverse-order teardown of everything bootstrap installed.
  --purge-env            With --uninstall: also delete laptop/config/laptop.env.
  --yes                  With --uninstall: skip the single confirmation prompt
                         (same as UNINSTALL_ASSUME_YES=1 in the environment).
  -h, --help             Show this help and exit.

Required pre-downloaded .deb files (place in LOCAL_DEB_DIR before running):
  nvidia-driver-local-repo-ubuntu2404-590.48.01_*_amd64.deb
    → https://www.nvidia.com/en-us/drivers/details/259258/
  cuda-keyring_1.1-1_all.deb
    → https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    # Legacy
    --skip-preflight)   SKIP_PREFLIGHT=1 ;;
    --skip-install)     SKIP_INSTALL=1 ;;
    --non-interactive)  NONINTERACTIVE=1; NO_PAUSE=1 ;;
    # New
    --local-deb-dir)    shift; LOCAL_DEB_DIR="$1"; LOCAL_DEB_DIR_FROM_CLI=1 ;;
    --allow-curl-keyring) ALLOW_CURL_KEYRING=1 ;;
    --reset-state)      RESET_STATE_FLAG=1 ;;
    --force-models)     FORCE_MODELS=1 ;;
    --resume-from)      shift; RESUME_FROM="$1" ;;
    --no-pause)         NO_PAUSE=1 ;;
    --uninstall)        DO_UNINSTALL=1 ;;
    --purge-env)        PURGE_ENV=1 ;;
    --yes)              ASSUME_YES=1 ;;
    -h|--help)          usage; exit 0 ;;
    *)  log_error "Unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

if [[ "$LOCAL_DEB_DIR_FROM_CLI" -eq 0 ]]; then
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    LOCAL_DEB_DIR="$(getent passwd "$SUDO_USER" | cut -d: -f6)/Downloads"
  else
    LOCAL_DEB_DIR="${HOME:-/root}/Downloads"
  fi
fi

REPO_ROOT="$(repo_root)"
ENV_FILE="$REPO_ROOT/laptop/config/laptop.env"
ENV_EXAMPLE="$REPO_ROOT/laptop/config/laptop.env.example"
SETUP_DOC="laptop/docs/DEEPSTREAM-SETUP.md"

# ---------------------------------------------------------------------------
# Phase 0 — preflight + deb-only policy banner + config-file manifest table
# ---------------------------------------------------------------------------
run_phase0_preflight() {
  # Policy banner — printed before any install work.
  cat >&2 <<'BANNER'

  +-----------------------------------------------------------------------+
  |  mv3dt laptop bootstrap -- .deb-only installer                        |
  |                                                                       |
  |  This script installs EVERYTHING from .deb packages only.             |
  |  Tarballs, .run installers, pip, and source builds are refused.       |
  |  Required pre-downloaded .deb files are listed below.                 |
  +-----------------------------------------------------------------------+

BANNER

  # Ubuntu 24.04
  if command -v lsb_release >/dev/null 2>&1; then
    local distro release codename
    distro="$(lsb_release -is 2>/dev/null || echo unknown)"
    release="$(lsb_release -rs 2>/dev/null || echo 0)"
    codename="$(lsb_release -cs 2>/dev/null || echo unknown)"
    if [[ "$distro" != "Ubuntu" || "$release" != "24.04" ]]; then
      die "Ubuntu 24.04 required (found: ${distro} ${release} / ${codename}). See ${SETUP_DOC} §3."
    fi
    log_info "OS OK: ${distro} ${release} (${codename})"
  else
    log_warn "lsb_release not found; cannot verify Ubuntu 24.04 — proceeding."
  fi

  # Must be root for the install phases.
  require_root

  # Architecture
  local arch
  arch="$(uname -m)"
  if [[ "$arch" != "x86_64" ]]; then
    die "x86_64 required (found: ${arch}). See ${SETUP_DOC} §2."
  fi
  log_info "Architecture OK: ${arch}"

  # GPU presence via PCI — driver not required yet, so do not use nvidia-smi.
  if command -v lspci >/dev/null 2>&1; then
    if lspci 2>/dev/null | grep -qi nvidia; then
      log_info "NVIDIA GPU detected via lspci."
    else
      log_warn "No NVIDIA GPU found via lspci — proceeding (may be a pass-through or headless config)."
    fi
  else
    log_warn "lspci not available; cannot probe GPU via PCI. Proceeding."
  fi

  # Print required .deb inventory.
  cat >&2 <<EOF

  Pre-downloaded .deb files required in LOCAL_DEB_DIR=${LOCAL_DEB_DIR}:

    1. nvidia-driver-local-repo-ubuntu2404-590.48.01_*_amd64.deb
       (Phase 3 — NVIDIA driver 590.48.01 + CUDA 13.1 + cuDNN 9.18 + TRT 10.14.1.48)
       Download: https://www.nvidia.com/en-us/drivers/details/259258/

    2. cuda-keyring_1.1-1_all.deb
       (Phase 3 — CUDA apt repository signing keyring)
       Download: https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/
       Note: pass --allow-curl-keyring to fetch this automatically if absent.

EOF

  # Config-file manifest table — operator sees the full side-effect list
  # before any install work begins.
  cat >&2 <<'MANIFEST'
  Config files this run MAY create or modify:
  +---------------------------------------------------+---------+------------------------------+
  | File                                              | Phase   | Purpose                      |
  +---------------------------------------------------+---------+------------------------------+
  | /etc/modprobe.d/blacklist-nouveau.conf            | 2       | Blacklist nouveau driver     |
  | /etc/profile.d/cuda.sh                            | 3       | CUDA 13.1 PATH / LD env      |
  | /etc/apt/sources.list.d/docker.list               | 4       | Docker CE apt repo           |
  | /etc/apt/keyrings/docker.gpg                      | 4       | Docker CE apt keyring        |
  | /etc/apt/sources.list.d/nvidia-container-toolkit  | 4       | NV Container Toolkit repo    |
  | /etc/profile.d/deepstream.sh                      | 7       | DS 9.0 PATH / LD env         |
  | /var/lib/mv3dt-laptop-bootstrap.state             | all     | Phase resume marker          |
  | laptop/config/laptop.env                          | 9       | Operator-facing env file     |
  +---------------------------------------------------+---------+------------------------------+

  NOT managed by 00_bootstrap.sh:
    /etc/mosquitto/conf.d/mv3dt.conf  → laptop/scripts/10_setup_mosquitto.sh

MANIFEST

  mark_phase_done preflight
  log_info "Phase 0 complete."
}

# ---------------------------------------------------------------------------
# Phase 1 — base deps (no reboot)
# ---------------------------------------------------------------------------
run_phase1_base() {
  log_info "Phase 1: installing base build dependencies."
  require_root
  export DEBIAN_FRONTEND=noninteractive

  apt-get update -y

  # Single transaction per the plan — apt resolves conflicts atomically.
  # Covers §4.7.1 (build tools) and §4.7.2 (runtime prereqs for later phases).
  apt-get install -y --no-install-recommends \
    build-essential \
    dkms \
    "linux-headers-$(uname -r)" \
    software-properties-common \
    ca-certificates \
    gnupg \
    curl \
    lsb-release \
    xdg-utils \
    inotify-tools \
    jq \
    ffmpeg \
    unzip

  mark_phase_done base-deps
  log_info "Phase 1 complete."
}

# ---------------------------------------------------------------------------
# Phase 2 — nouveau / old NVIDIA cleanup (reboot only if state actually changed)
# ---------------------------------------------------------------------------
run_phase2_nouveau() {
  log_info "Phase 2: checking for nouveau / distro NVIDIA packages."
  require_root
  export DEBIAN_FRONTEND=noninteractive

  local need_reboot=0
  local NOUVEAU_CONF="/etc/modprobe.d/blacklist-nouveau.conf"

  # Purge any distro-packaged nvidia-* / libnvidia-* before the proprietary
  # driver lands. The distro packages conflict with the local-repo .deb.
  if dpkg -l 2>/dev/null | awk '{print $2}' | grep -qE '^(nvidia-|libnvidia-)'; then
    log_info "Phase 2: purging distro nvidia-* / libnvidia-* packages (§4.7.3)."
    # shellcheck disable=SC2046
    apt-get purge -y $(dpkg -l 2>/dev/null | awk '{print $2}' | grep -E '^(nvidia-|libnvidia-)' | tr '\n' ' ') || true
    apt-get autoremove -y || true
    need_reboot=1
    log_info "Phase 2: distro NVIDIA packages purged."
  else
    log_info "Phase 2: no distro nvidia-* packages found."
  fi

  # Blacklist nouveau if it is currently loaded in the kernel.
  if lsmod 2>/dev/null | grep -q nouveau; then
    log_info "Phase 2: nouveau kernel module active — blacklisting and rebuilding initramfs (§4.7.4)."

    local _staged
    _staged="$(mktemp)"
    cat > "$_staged" <<'EOF'
# Managed by laptop/scripts/00_bootstrap.sh (phase: nouveau-blacklist)
# Canonical path: /etc/modprobe.d/blacklist-nouveau.conf ; source-of-truth in repo: N/A
# Purpose: Blacklist the open-source nouveau driver so the NVIDIA proprietary
#          driver (nvidia-driver-590) can load cleanly. Required before Phase 3
#          installs the NVIDIA driver deb (DS 9.0 §4.7.4).
# Remove with: sudo bash laptop/scripts/00_bootstrap.sh --uninstall
blacklist nouveau
options nouveau modeset=0
EOF
    STAGED_CONTENT_FILE="$_staged"
    announce_config_file "$NOUVEAU_CONF" \
      "Blacklist nouveau so NVIDIA proprietary driver can load (DS 9.0 §4.7.4)."
    mv -f "$_staged" "$NOUVEAU_CONF"
    unset STAGED_CONTENT_FILE
    chmod 0644 "$NOUVEAU_CONF"

    log_info "Phase 2: running update-initramfs -u (this may take a minute)."
    update-initramfs -u
    need_reboot=1

  elif [[ ! -f "$NOUVEAU_CONF" ]]; then
    # nouveau is not loaded but the blacklist file doesn't exist — write it
    # proactively so it survives a kernel update that might re-enable nouveau.
    log_info "Phase 2: nouveau not loaded; writing blacklist proactively."

    local _staged
    _staged="$(mktemp)"
    cat > "$_staged" <<'EOF'
# Managed by laptop/scripts/00_bootstrap.sh (phase: nouveau-blacklist)
# Canonical path: /etc/modprobe.d/blacklist-nouveau.conf ; source-of-truth in repo: N/A
# Purpose: Blacklist the open-source nouveau driver so the NVIDIA proprietary
#          driver (nvidia-driver-590) can load cleanly. Required before Phase 3
#          installs the NVIDIA driver deb (DS 9.0 §4.7.4).
# Remove with: sudo bash laptop/scripts/00_bootstrap.sh --uninstall
blacklist nouveau
options nouveau modeset=0
EOF
    STAGED_CONTENT_FILE="$_staged"
    announce_config_file "$NOUVEAU_CONF" \
      "Blacklist nouveau so NVIDIA proprietary driver can load (DS 9.0 §4.7.4)."
    mv -f "$_staged" "$NOUVEAU_CONF"
    unset STAGED_CONTENT_FILE
    chmod 0644 "$NOUVEAU_CONF"
    log_info "Phase 2: nouveau blacklist written (no initramfs rebuild needed since nouveau is not loaded)."

  else
    log_info "Phase 2: ${NOUVEAU_CONF} already present and nouveau not loaded; nothing to do."
  fi

  mark_phase_done nouveau-blacklist

  if [[ "$need_reboot" -eq 1 ]]; then
    cat >&2 <<'REBOOT'

  +-----------------------------------------------------------------------+
  |  REBOOT REQUIRED                                                      |
  |                                                                       |
  |  nouveau was purged / blacklisted. The system must reboot before the  |
  |  NVIDIA proprietary driver can be installed in Phase 3.               |
  |                                                                       |
  |  After the machine comes back up, re-run:                             |
  |    sudo bash laptop/scripts/00_bootstrap.sh                           |
  |                                                                       |
  |  The state file will skip Phases 0-2 automatically.                   |
  +-----------------------------------------------------------------------+

REBOOT
    exit 0
  fi

  log_info "Phase 2 complete (no reboot needed)."
}

# ---------------------------------------------------------------------------
# Resolve pre-downloaded NVIDIA .deb paths (Phase 2b + Phase 3 resume).
# ---------------------------------------------------------------------------
resolve_bootstrap_debs() {
  DRIVER_LOCAL_REPO_DEB=""
  CUDA_KEYRING_DEB=""
  if [[ -z "$LOCAL_DEB_DIR" || ! -d "$LOCAL_DEB_DIR" ]]; then
    return 1
  fi
  DRIVER_LOCAL_REPO_DEB="$(find_local_deb "nvidia-driver-local-repo-ubuntu2404-590.48.01*_amd64.deb" "$LOCAL_DEB_DIR" || true)"
  CUDA_KEYRING_DEB="$(find_local_deb "cuda-keyring_1.1-1_all.deb" "$LOCAL_DEB_DIR" || true)"
  [[ -n "$DRIVER_LOCAL_REPO_DEB" && -n "$CUDA_KEYRING_DEB" ]]
}

# ---------------------------------------------------------------------------
# Phase 2b — verify local .deb files
# ---------------------------------------------------------------------------
run_phase2b_verify_debs() {
  if phase_done local-deb-verified; then
    log_info "Phase 2b (local .deb verification) already complete; skipping."
    resolve_bootstrap_debs || true
    export DRIVER_LOCAL_REPO_DEB="${DRIVER_LOCAL_REPO_DEB:-}" CUDA_KEYRING_DEB="${CUDA_KEYRING_DEB:-}"
    return 0
  fi

  log_info "Phase 2b: verifying required .deb files under ${LOCAL_DEB_DIR}."
  require_root
  if [[ ! -d "$LOCAL_DEB_DIR" ]]; then
    die "LOCAL_DEB_DIR is not a directory: ${LOCAL_DEB_DIR}"
  fi

  DRIVER_LOCAL_REPO_DEB="$(find_local_deb "nvidia-driver-local-repo-ubuntu2404-590.48.01*_amd64.deb" "$LOCAL_DEB_DIR" || true)"
  if [[ -z "$DRIVER_LOCAL_REPO_DEB" ]]; then
    die "Missing: nvidia-driver-local-repo-ubuntu2404-590.48.01_*_amd64.deb under ${LOCAL_DEB_DIR} (see ${SETUP_DOC} §4.4, NVIDIA driver 590.48.01 local-repo .deb)."
  fi

  CUDA_KEYRING_DEB="$(find_local_deb "cuda-keyring_1.1-1_all.deb" "$LOCAL_DEB_DIR" || true)"
  if [[ -z "$CUDA_KEYRING_DEB" ]]; then
    if [[ "$ALLOW_CURL_KEYRING" -eq 1 ]]; then
      log_info "Phase 2b: fetching cuda-keyring_1.1-1_all.deb via curl (--allow-curl-keyring)."
      local _k
      _k="$(mktemp /tmp/cuda-keyring_XXXXXX.deb)"
      curl -fsSL -o "$_k" \
        "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb" \
        || die "Could not download cuda-keyring_1.1-1_all.deb."
      install -m0644 -o root -g root "$_k" "$LOCAL_DEB_DIR/cuda-keyring_1.1-1_all.deb"
      rm -f "$_k"
      if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
        chown "$SUDO_USER:" "$LOCAL_DEB_DIR/cuda-keyring_1.1-1_all.deb" 2>/dev/null || true
      fi
      CUDA_KEYRING_DEB="$LOCAL_DEB_DIR/cuda-keyring_1.1-1_all.deb"
    else
      die "Missing: cuda-keyring_1.1-1_all.deb under ${LOCAL_DEB_DIR}. Download from https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/ or pass --allow-curl-keyring."
    fi
  fi

  export DRIVER_LOCAL_REPO_DEB CUDA_KEYRING_DEB
  log_info "Phase 2b: driver local-repo: ${DRIVER_LOCAL_REPO_DEB}"
  log_info "Phase 2b: cuda keyring:      ${CUDA_KEYRING_DEB}"
  mark_phase_done local-deb-verified
  log_info "Phase 2b complete."
}

# ---------------------------------------------------------------------------
# Phase 3 — NVIDIA driver + CUDA 13.1 + cuDNN 9.18 + TensorRT 10.14 (reboot)
# ---------------------------------------------------------------------------
run_phase3_nvidia_stack() {
  if phase_done nvidia-stack; then
    log_info "Phase 3 (NVIDIA stack) already complete; skipping."
    return 0
  fi

  log_info "Phase 3: installing NVIDIA driver 590 + CUDA 13.1 + cuDNN + TensorRT (single apt transaction)."
  require_root
  require_tool apt-get
  export DEBIAN_FRONTEND=noninteractive

  if ! resolve_bootstrap_debs; then
    die "Could not find local .deb files. Re-run Phase 2b (or place nvidia-driver-local-repo-*.deb and cuda-keyring_1.1-1_all.deb in ${LOCAL_DEB_DIR})."
  fi
  # shellcheck disable=SC2154
  dpkg -i "$CUDA_KEYRING_DEB" "$DRIVER_LOCAL_REPO_DEB" || die "dpkg -i of NVIDIA local debs failed."

  local _f
  shopt -s nullglob
  for _f in /var/nvidia-driver-local-repo-*/*keyring.gpg; do
    install -m 644 "$_f" "/usr/share/keyrings/$(basename "$_f")"
  done
  shopt -u nullglob

  apt-get update -y

  local TRT_VER="10.14.1.48-1+cuda13.0"
  # shellcheck disable=SC2209
  apt-get install -y --no-install-recommends \
    cuda-drivers-590 \
    cuda-toolkit-13-1 \
    libcudnn9-cuda-13 libcudnn9-dev-cuda-13 \
    "libnvinfer-dev=$TRT_VER" "libnvinfer-dispatch-dev=$TRT_VER" \
    "libnvinfer-dispatch10=$TRT_VER" "libnvinfer-headers-dev=$TRT_VER" \
    "libnvinfer-headers-plugin-dev=$TRT_VER" "libnvinfer-lean-dev=$TRT_VER" \
    "libnvinfer-lean10=$TRT_VER" "libnvinfer-plugin-dev=$TRT_VER" \
    "libnvinfer-plugin10=$TRT_VER" "libnvinfer-vc-plugin-dev=$TRT_VER" \
    "libnvinfer-vc-plugin10=$TRT_VER" "libnvinfer10=$TRT_VER" \
    "libnvonnxparsers-dev=$TRT_VER" "libnvonnxparsers10=$TRT_VER" \
    "tensorrt-dev=$TRT_VER" \
    || die "apt install of NVIDIA / CUDA / TensorRT stack failed. See ${SETUP_DOC} §4."

  local CUDA_PROFILE="/etc/profile.d/cuda.sh"
  local _cstage
  _cstage="$(mktemp)"
  cat > "$_cstage" <<'EOF'
# Managed by laptop/scripts/00_bootstrap.sh (phase: nvidia-cuda-profile)
# Canonical path: /etc/profile.d/cuda.sh ; source-of-truth in repo: N/A
# Purpose: Put CUDA 13.1 on PATH and LD_LIBRARY_PATH for all login shells
#          (DS 9.0 Installation page — dGPU Ubuntu §4.3).
# Remove with: sudo bash laptop/scripts/00_bootstrap.sh --uninstall
export PATH=/usr/local/cuda-13.1/bin${PATH:+:$PATH}
export LD_LIBRARY_PATH=/usr/local/cuda-13.1/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
EOF
  STAGED_CONTENT_FILE="$_cstage"
  announce_config_file "$CUDA_PROFILE" "CUDA 13.1 PATH and LD_LIBRARY_PATH for all login shells (DS 9.0 §4.3)."
  mv -f "$_cstage" "$CUDA_PROFILE"
  unset STAGED_CONTENT_FILE
  chmod 0644 "$CUDA_PROFILE"

  mark_phase_done nvidia-stack
  cat >&2 <<'R3'

  +-----------------------------------------------------------------------+
  |  REBOOT REQUIRED                                                      |
  |                                                                       |
  |  The NVIDIA driver kernel module and CUDA 13.1 stack are installed.   |
  |  Reboot so nvidia.ko loads before Phases 4–8.                        |
  |                                                                       |
  |  After the machine comes back up, re-run:                             |
  |    sudo bash laptop/scripts/00_bootstrap.sh                           |
  |                                                                       |
  |  The state file will skip Phases 0–3 automatically.                    |
  +-----------------------------------------------------------------------+

R3
  exit 0
}

# ---------------------------------------------------------------------------
# Phase 4 — GStreamer 1.24, DS apt prereqs, Mosquitto, Docker, NCT, Kafka
# ---------------------------------------------------------------------------
run_phase4_runtime() {
  if phase_done runtime-deps; then
    log_info "Phase 4 (runtime dependencies) already complete; skipping."
    return 0
  fi

  log_info "Phase 4: GStreamer, DeepStream apt prerequisites, Mosquitto, Docker, NVIDIA Container Toolkit, librdkafka."
  require_root
  require_tool apt-get
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y

  apt-get install -y --no-install-recommends \
    libssl3 libssl-dev libcurl4-openssl-dev libgles2-mesa-dev \
    libgstreamer1.0-0 gstreamer1.0-tools \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav gstreamer1.0-plugins-rtp \
    libgstreamer-plugins-base1.0-dev \
    libgstrtspserver-1.0-0 gstreamer1.0-rtsp \
    libjansson4 libyaml-cpp-dev libjsoncpp-dev protobuf-compiler \
    libmosquitto1 \
    gcc make git python3 \
    mosquitto mosquitto-clients \
    librdkafka1 librdkafka-dev \
    lsb-release xdg-utils inotify-tools jq ffmpeg unzip ca-certificates curl gnupg \
    || die "apt install of Phase 4 packages failed."

  if ! command -v docker >/dev/null 2>&1; then
    log_info "Phase 4: installing Docker Engine (§8.2)"
    install -m 0755 -d /etc/apt/keyrings
    if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
        gpg --dearmor -o /etc/apt/keyrings/docker.gpg
      chmod a+r /etc/apt/keyrings/docker.gpg
    fi

    local _staged
    _staged="$(mktemp)"
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu %s stable\n' \
      "$(dpkg --print-architecture)" "$(lsb_release -cs)" > "$_staged"
    STAGED_CONTENT_FILE="$_staged"
    announce_config_file "/etc/apt/sources.list.d/docker.list" \
      "Docker CE apt repo (required for AMC container images, §8.2)."
    mv -f "$_staged" /etc/apt/sources.list.d/docker.list
    unset STAGED_CONTENT_FILE

    apt-get update -y
    apt-get install -y --no-install-recommends \
      docker-ce docker-ce-cli containerd.io \
      docker-buildx-plugin docker-compose-plugin \
      || die "apt install docker-ce failed."
  else
    log_info "docker already installed; skipping Docker apt setup."
  fi

  if ! dpkg -s nvidia-container-toolkit >/dev/null 2>&1; then
    log_info "Phase 4: installing NVIDIA Container Toolkit (§8.2)"
    if [[ ! -f /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg ]]; then
      curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
        gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    fi

    local _st2
    _st2="$(mktemp)"
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
      sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      > "$_st2"
    STAGED_CONTENT_FILE="$_st2"
    announce_config_file "/etc/apt/sources.list.d/nvidia-container-toolkit.list" \
      "NVIDIA Container Toolkit apt repo (required for Docker GPU access, §8.2)."
    mv -f "$_st2" /etc/apt/sources.list.d/nvidia-container-toolkit.list
    unset STAGED_CONTENT_FILE

    apt-get update -y
    apt-get install -y --no-install-recommends nvidia-container-toolkit \
      || die "apt install nvidia-container-toolkit failed."
    if command -v nvidia-ctk >/dev/null 2>&1; then
      nvidia-ctk runtime configure --runtime=docker || \
        log_warn "nvidia-ctk runtime configure failed; configure Docker NVIDIA runtime manually."
      systemctl restart docker || log_warn "systemctl restart docker failed."
    fi
  else
    log_info "nvidia-container-toolkit already installed; skipping."
  fi

  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    if ! id -nG "$SUDO_USER" | tr ' ' '\n' | grep -qx docker; then
      log_info "Phase 4: adding ${SUDO_USER} to the 'docker' group (re-login required)."
      usermod -aG docker "$SUDO_USER" || log_warn "usermod -aG docker failed."
    fi
  fi

  mark_phase_done runtime-deps
  log_info "Phase 4 complete."
}

# ---------------------------------------------------------------------------
# Phase 5 — NGC CLI guidance + Enter gate
# ---------------------------------------------------------------------------
run_phase5_ngc_gate() {
  if phase_done ngc-pause; then
    log_info "Phase 5 (NGC CLI gate) already complete; skipping."
    return 0
  fi

  cat <<'P5' >&2

  +-----------------------------------------------------------------------+
  |  NGC CLI required for Phases 6 and 10                                 |
  |                                                                       |
  |  Install the CLI in a separate shell as your regular user, then      |
  |  run:   ngc config set                                                |
  |  (API key, ascii, your NGC org — see https://ngc.nvidia.com/setup)    |
  |                                                                       |
  |  Example install:                                                     |
  |    cd ~ && mkdir -p ngc-cli && cd ngc-cli                            |
  |    curl -LO https://ngc.nvidia.com/downloads/ngccli_linux.zip         |
  |    unzip -o ngccli_linux.zip && chmod u+x ngc-cli/ngc                 |
  |    echo 'export PATH="$HOME/ngc-cli/ngc-cli:$PATH"' >> ~/.bashrc     |
  |    source ~/.bashrc                                                   |
  |                                                                       |
  |  Verify:  ngc config current                                          |
  |                                                                       |
  +-----------------------------------------------------------------------+

P5
  if [[ "${NO_PAUSE:-0}" -eq 0 && -t 0 ]]; then
    read -r -p "Press Enter here once 'ngc' works and is authenticated..." _
  else
    log_info "Non-interactive / --no-pause: not waiting for Enter (Phase 5)."
  fi
  mark_phase_done ngc-pause
  log_info "Phase 5 complete."
}

# ---------------------------------------------------------------------------
# Phase 6 — NGC: download deepstream-9.0 .deb and apt install
# ---------------------------------------------------------------------------
run_phase6_ds_deb() {
  if phase_done ds-deb; then
    if dpkg -s deepstream-9.0 >/dev/null 2>&1; then
      log_info "Phase 6 (DeepStream .deb) already complete; skipping."
      return 0
    fi
    log_warn "State says ds-deb=done but deepstream-9.0 is not installed; re-running Phase 6."
  fi

  log_info "Phase 6: NGC download of deepstream-9.0 .deb and apt install."
  require_root
  if [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == "root" ]]; then
    die "Phase 6 must be run with sudo from a non-root login (need SUDO_USER for ngc and ~/Downloads)."
  fi

  local DS_PATTERN='deepstream-9.0_9.0.0-1_amd64.deb'
  local ds_path
  ds_path="$(find "$LOCAL_DEB_DIR" -maxdepth 4 -name "$DS_PATTERN" 2>/dev/null | head -n1 || true)"
  if [[ -z "$ds_path" || ! -f "$ds_path" ]]; then
    log_info "Phase 6: downloading ${DS_PATTERN} via NGC into ${LOCAL_DEB_DIR} (as ${SUDO_USER})."
    sudo -u "$SUDO_USER" -H bash -lc "set -euo pipefail; cd \"$(printf '%q' "$LOCAL_DEB_DIR")\"; command -v ngc >/dev/null; ngc registry resource download-version \"nvidia/deepstream/deepstream:9.0\"" \
      || die "NGC download of DeepStream 9.0 failed. Install/configure ngc (Phase 5) and re-run."
    ds_path="$(find "$LOCAL_DEB_DIR" -maxdepth 4 -name "$DS_PATTERN" 2>/dev/null | head -n1 || true)"
  fi
  [[ -n "$ds_path" && -f "$ds_path" ]] || die "Could not find ${DS_PATTERN} under ${LOCAL_DEB_DIR} after NGC download."

  log_info "Phase 6: installing ${ds_path} via apt-get install (resolves dependencies)."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends "$ds_path" \
    || die "apt install of DeepStream 9.0 .deb failed. See ${SETUP_DOC} §5."

  mark_phase_done ds-deb
  log_info "Phase 6 complete."
}

# ---------------------------------------------------------------------------
# Phase 7 — post-install: update_rtpmanager.sh + deepstream profile
# ---------------------------------------------------------------------------
write_deepstream_profile() {
  local DS_PROFILE="/etc/profile.d/deepstream.sh"
  local DS_DIR="/opt/nvidia/deepstream/deepstream-9.0"
  local _staged
  _staged="$(mktemp)"
  cat > "$_staged" <<EOF
# Managed by laptop/scripts/00_bootstrap.sh (phase: deepstream-profile)
# Canonical path: /etc/profile.d/deepstream.sh ; source-of-truth in repo: N/A
# Purpose: Put DeepStream 9.0 on PATH / LD_LIBRARY_PATH and set DEEPSTREAM_DIR
#          for all login shells (DS 9.0 §5.5).
# Remove with: sudo bash laptop/scripts/00_bootstrap.sh --uninstall
export DEEPSTREAM_DIR=${DS_DIR}
case ":\$PATH:" in
  *":${DS_DIR}/bin:"*) : ;;
  *) export PATH="${DS_DIR}/bin:\$PATH" ;;
esac
case ":\${LD_LIBRARY_PATH:-}:" in
  *":${DS_DIR}/lib:"*) : ;;
  *) export LD_LIBRARY_PATH="${DS_DIR}/lib:\${LD_LIBRARY_PATH:-}" ;;
esac
EOF
  STAGED_CONTENT_FILE="$_staged"
  announce_config_file "$DS_PROFILE" \
    "Put DeepStream 9.0 on PATH / LD_LIBRARY_PATH; sets DEEPSTREAM_DIR (DS 9.0 §5.5)."
  mv -f "$_staged" "$DS_PROFILE"
  unset STAGED_CONTENT_FILE
  chmod 0644 "$DS_PROFILE"
}

run_phase7_post_install() {
  if phase_done ds-post; then
    log_info "Phase 7 (DS post-install) already complete; skipping."
    return 0
  fi

  log_info "Phase 7: update_rtpmanager.sh and /etc/profile.d/deepstream.sh"
  require_root

  local _rtp_ran=0
  for f in /opt/nvidia/deepstream/deepstream/update_rtpmanager.sh \
           /opt/nvidia/deepstream/deepstream-9.0/update_rtpmanager.sh; do
    if [[ -f "$f" && -x "$f" ]]; then
      log_info "Running $f"
      "$f" || log_warn "update_rtpmanager.sh returned non-zero; check DS RTSP buffer notes in ${SETUP_DOC} §5.1."
      _rtp_ran=1
      break
    fi
  done
  if [[ "$_rtp_ran" -ne 1 ]]; then
    log_warn "update_rtpmanager.sh not found — ensure deepstream-9.0 is installed (Phase 6)."
  fi

  if [[ -f /etc/profile.d/deepstream.sh ]]; then
    log_info "Phase 7: /etc/profile.d/deepstream.sh already exists; writing canonical block."
  fi
  write_deepstream_profile

  mark_phase_done ds-post
  log_info "Phase 7 complete."
}

# ---------------------------------------------------------------------------
# Phase 8 — version audit (hard gate)
# ---------------------------------------------------------------------------
run_phase8_version_audit() {
  if phase_done version-audit; then
    log_info "Phase 8 (version audit) already complete; skipping."
    return 0
  fi

  log_info "Phase 8: hard-gate version audit (all pins must match)."
  require_root
  if [[ -f /etc/profile.d/cuda.sh ]]; then
    # shellcheck disable=SC1091
    . /etc/profile.d/cuda.sh
  fi

  require_tool nvidia-smi
  if ! command -v nvcc >/dev/null 2>&1; then
    die "nvcc not on PATH — re-login after Phase 3, or ensure /usr/local/cuda-13.1/bin exists."
  fi
  require_tool gst-inspect-1.0

  # shellcheck disable=SC2155
  local drv cuda_rel gst_ver ds_ver trt_line cudnn_line
  drv="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' ')"
  require_version_eq "NVIDIA driver" "$drv" "590.48.01"

  cuda_rel="$(nvcc --version 2>/dev/null | sed -n 's/.*release \([0-9.]*\).*/\1/p' | head -n1)"
  require_version_eq "CUDA (nvcc release)" "$cuda_rel" "13.1"

  cudnn_line="$(dpkg -l 2>/dev/null | awk '/^ii/ && $2 ~ /^libcudnn9/ {print $3; exit}')"
  if [[ -z "$cudnn_line" ]] || ! grep -qE '9\.18\.0' <<<"$cudnn_line"; then
    die "cuDNN 9.18.0 required — expected libcudnn9 line to contain 9.18.0, got: ${cudnn_line:-<none>}"
  fi
  log_info "Version OK: libcudnn9 (contains 9.18.0)"

  trt_line="$(dpkg -s libnvinfer10 2>/dev/null | sed -n 's/^Version: //p' | tr -d ' ' || true)"
  require_version_eq "TensorRT (libnvinfer10)" "$trt_line" "10.14.1.48-1+cuda13.0"

  gst_ver="$(gst-inspect-1.0 --version 2>&1 | head -n1 | sed -n 's/^gst-inspect-1.0 version \([0-9.]*\).*/\1/p')"
  require_version_eq "GStreamer" "$gst_ver" "1.24.2"

  ds_ver="$(dpkg -s deepstream-9.0 2>/dev/null | sed -n 's/^Version: //p' | tr -d ' ' || true)"
  require_version_eq "DeepStream" "$ds_ver" "9.0.0-1"

  for p in \
    docker-ce nvidia-container-toolkit \
    mosquitto mosquitto-clients libmosquitto1 \
    librdkafka1 librdkafka-dev; do
    dpkg -s "$p" >/dev/null 2>&1 || die "Required package not installed: $p (Phase 4)."
  done
  log_info "Version OK: docker-ce, nvidia-container-toolkit, Mosquitto stack, librdkafka present."

  cat <<'SUMM' >&2
  +----------------------------------------------------------------------+
  |  Version audit PASSED (paste-ready)                                   |
  +----------------------------------------------------------------------+
  |  Driver 590.48.01 | CUDA 13.1 | cuDNN 9.18 | TRT 10.14.1.48-1+cuda13.0
  |  GStreamer 1.24.2 | deepstream-9.0 9.0.0-1
  +----------------------------------------------------------------------+
SUMM

  mark_phase_done version-audit
  log_info "Phase 8 complete."
}

# ---------------------------------------------------------------------------
# Phase 9 — write laptop/config/laptop.env interactively
# ---------------------------------------------------------------------------
run_phase9_env() {
  if phase_done env-write && [[ -f "$ENV_FILE" ]]; then
    log_info "Phase 9 (laptop env) already complete; skipping."
    return 0
  fi

  mkdir -p "$(dirname "$ENV_FILE")"

  # Seed defaults from existing env if present, else from the example file.
  _get() {
    local key="$1" src="$2"
    [[ -f "$src" ]] || { echo ""; return; }
    local line
    line="$(grep -m1 "^${key}=" "$src" || true)"
    echo "${line#${key}=}"
  }

  local EXISTING_SRC="$ENV_FILE"
  [[ -f "$EXISTING_SRC" ]] || EXISTING_SRC="$ENV_EXAMPLE"

  local CUR_HOST_IP CUR_LOCATION_ID CUR_CAM_USER CUR_CAM_PASSWORD
  local CUR_NGC_API_KEY CUR_PROJECT_NAME CUR_PEOPLENET_NGC_TAG
  local CUR_AMC_ROOT CUR_MS_PORT CUR_UI_PORT
  local CUR_MQTT_HOST CUR_MQTT_PORT CUR_MQTT_TOPIC_BASE

  CUR_HOST_IP="$(_get HOST_IP "$EXISTING_SRC")"
  CUR_LOCATION_ID="$(_get LOCATION_ID "$EXISTING_SRC")"
  CUR_CAM_USER="$(_get CAM_USER "$EXISTING_SRC")"
  CUR_CAM_PASSWORD="$(_get CAM_PASSWORD "$EXISTING_SRC")"
  CUR_NGC_API_KEY="$(_get NGC_API_KEY "$EXISTING_SRC")"
  CUR_PROJECT_NAME="$(_get PROJECT_NAME "$EXISTING_SRC")"
  CUR_PEOPLENET_NGC_TAG="$(_get PEOPLENET_NGC_TAG "$EXISTING_SRC")"
  CUR_AMC_ROOT="$(_get AMC_ROOT "$EXISTING_SRC")"
  CUR_MS_PORT="$(_get AUTO_MAGIC_CALIB_MS_PORT "$EXISTING_SRC")"
  CUR_UI_PORT="$(_get AUTO_MAGIC_CALIB_UI_PORT "$EXISTING_SRC")"
  CUR_MQTT_HOST="$(_get MQTT_HOST "$EXISTING_SRC")"
  CUR_MQTT_PORT="$(_get MQTT_PORT "$EXISTING_SRC")"
  CUR_MQTT_TOPIC_BASE="$(_get MQTT_TOPIC_BASE "$EXISTING_SRC")"

  _prompt() {
    local label="$1" default="$2" secret="${3:-0}" var
    if [[ "$NONINTERACTIVE" -eq 1 ]]; then
      printf '%s\n' "$default"
      return
    fi
    if [[ "$secret" -eq 1 ]]; then
      read -r -s -p "$label [$(test -n "$default" && echo "keep existing" || echo "blank")]: " var
      echo >&2
    else
      read -r -p "$label [$default]: " var
    fi
    [[ -z "$var" ]] && var="$default"
    printf '%s\n' "$var"
  }

  if [[ -f "$ENV_FILE" ]]; then
    log_info "Reusing existing values from ${ENV_FILE} (press Enter to keep each)."
  else
    log_info "Creating ${ENV_FILE} from laptop.env.example defaults."
  fi

  local HOST_IP LOCATION_ID PROJECT_NAME CAM_USER CAM_PASSWORD NGC_API_KEY
  HOST_IP="$(_prompt "HOST_IP (laptop IPv4 on camera LAN)" "${CUR_HOST_IP:-192.168.10.10}")"
  LOCATION_ID="$(_prompt "LOCATION_ID (short site label)" "${CUR_LOCATION_ID:-test-lab-01}")"
  PROJECT_NAME="$(_prompt "PROJECT_NAME (AMC project name)" "${CUR_PROJECT_NAME:-$LOCATION_ID}")"
  CAM_USER="$(_prompt "CAM_USER" "${CUR_CAM_USER:-admin}")"
  CAM_PASSWORD="$(_prompt "CAM_PASSWORD" "${CUR_CAM_PASSWORD:-}" 1)"
  NGC_API_KEY="$(_prompt "NGC_API_KEY (blank if docker login nvcr.io already done)" "${CUR_NGC_API_KEY:-}" 1)"

  # DETECTOR is pinned to peoplenet — not operator-selectable.
  local DETECTOR="peoplenet"
  local PEOPLENET_NGC_TAG="${CUR_PEOPLENET_NGC_TAG:-nvidia/tao/peoplenet:deployable_quantized_onnx_v2.6.3}"
  local AMC_ROOT="${CUR_AMC_ROOT:-\$HOME/auto-magic-calib}"
  local MS_PORT="${CUR_MS_PORT:-8000}"
  local UI_PORT="${CUR_UI_PORT:-5000}"
  local MQTT_HOST="${CUR_MQTT_HOST:-127.0.0.1}"
  local MQTT_PORT="${CUR_MQTT_PORT:-1883}"
  local MQTT_TOPIC_BASE="${CUR_MQTT_TOPIC_BASE:-mv3dt}"

  if [[ -z "$HOST_IP" || -z "$LOCATION_ID" || -z "$CAM_USER" || -z "$CAM_PASSWORD" ]]; then
    die "HOST_IP, LOCATION_ID, CAM_USER, and CAM_PASSWORD must not be empty."
  fi

  local _staged
  _staged="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
  cat > "$_staged" <<EOF
# Managed by laptop/scripts/00_bootstrap.sh (phase: env-write)
# Canonical path: $(realpath "$ENV_FILE" 2>/dev/null || echo "$ENV_FILE") ; source-of-truth in repo: laptop/config/laptop.env
# Purpose: Operator-facing env consumed by every downstream laptop/scripts/*.
#          Edit by re-running 00_bootstrap.sh; direct edits are preserved on
#          next run (values are read back as defaults).
# Remove with: sudo bash laptop/scripts/00_bootstrap.sh --uninstall --purge-env
HOST_IP=${HOST_IP}
LOCATION_ID=${LOCATION_ID}
CAM_USER=${CAM_USER}
CAM_PASSWORD=${CAM_PASSWORD}
NGC_API_KEY=${NGC_API_KEY}
PROJECT_NAME=${PROJECT_NAME}

# Pinned detector — PeopleNet ONNX (DS 9.0 MV3DT reference).
# Downloaded by Phase 10 of 00_bootstrap.sh via ngc registry model download-version.
DETECTOR=${DETECTOR}
PEOPLENET_NGC_TAG=${PEOPLENET_NGC_TAG}

# AMC runtime (cloned outside this repo by 30_start_amc.sh).
AMC_ROOT=${AMC_ROOT}
AUTO_MAGIC_CALIB_MS_PORT=${MS_PORT}
AUTO_MAGIC_CALIB_UI_PORT=${UI_PORT}

# Mosquitto.
MQTT_HOST=${MQTT_HOST}
MQTT_PORT=${MQTT_PORT}
MQTT_TOPIC_BASE=${MQTT_TOPIC_BASE}
EOF
  STAGED_CONTENT_FILE="$_staged"
  announce_config_file "$ENV_FILE" \
    "Operator-facing env file consumed by all laptop/scripts/*. Contains credentials."
  chown root:root "$_staged" 2>/dev/null || true
  chmod 0640 "$_staged"
  mv -f "$_staged" "$ENV_FILE"
  unset STAGED_CONTENT_FILE

  log_info "Wrote ${ENV_FILE}"
  mark_phase_done env-write
}

# ---------------------------------------------------------------------------
# Phase 10 — PeopleNet ONNX (ngc registry model download + labels.txt)
# ---------------------------------------------------------------------------
run_phase10_models() {
  local PN_DIR="$REPO_ROOT/laptop/deepstream/models/peoplenet"
  local LABELS_FILE="$PN_DIR/labels.txt"

  if [[ "$FORCE_MODELS" -ne 1 ]] && phase_done models; then
    if [[ -d "$PN_DIR" ]] && compgen -G "$PN_DIR/*" >/dev/null; then
      log_info "Phase 10 (PeopleNet models) already complete; skipping."
      return 0
    fi
  fi

  if [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == "root" ]]; then
    die "Phase 10 requires sudo from a non-root login (SUDO_USER) so ngc runs with your NGC config."
  fi

  log_info "Phase 10: PeopleNet ONNX download (ngc registry model download-version)."
  if [[ ! -f "$ENV_FILE" ]]; then
    die "laptop/config/laptop.env missing — complete Phase 9 first (or run without --skip-install)."
  fi

  if ! sudo -u "$SUDO_USER" -H bash -lc 'command -v ngc >/dev/null 2>&1'; then
    die "NGC CLI ('ngc') not found for user ${SUDO_USER}. Install it (Phase 5), put it on PATH, and re-run."
  fi

  mkdir -p "$PN_DIR"
  local _env_abs
  _env_abs="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"

  if ! sudo -u "$SUDO_USER" -H \
    env BOOTSTRAP_ENV_FILE="$_env_abs" BOOTSTRAP_PN_DIR="$PN_DIR" \
    bash -s <<'INNER'
set -euo pipefail
set -a; . "$BOOTSTRAP_ENV_FILE"; set +a
: "${PEOPLENET_NGC_TAG:=nvidia/tao/peoplenet:deployable_quantized_onnx_v2.6.3}"
if [[ -n "${NGC_API_KEY:-}" ]]; then
  mkdir -p "$HOME/.ngc"
  chmod 700 "$HOME/.ngc"
  cat > "$HOME/.ngc/config" <<NGCINI
[CURRENT]
apikey = ${NGC_API_KEY}
format_type = ascii
NGCINI
  chmod 600 "$HOME/.ngc/config"
fi
TMP_DL="$(mktemp -d)"
trap 'rm -rf "$TMP_DL"' EXIT
ngc registry model download-version "$PEOPLENET_NGC_TAG" --dest "$TMP_DL"
mapfile -t _roots < <(find "$TMP_DL" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
if [[ ${#_roots[@]} -eq 0 ]]; then
  echo "NGC: expected a versioned subdirectory under the download temp dir" >&2
  exit 1
fi
for d in "${_roots[@]}"; do
  cp -a "$d"/. "$BOOTSTRAP_PN_DIR"/
done
INNER
  then
    die "NGC PeopleNet download failed. Check PEOPLENET_NGC_TAG and NGC_API_KEY in ${ENV_FILE}."
  fi

  if [[ ! -f "$LABELS_FILE" ]]; then
    cat > "$LABELS_FILE" <<'LABELS'
person
bag
face
LABELS
    log_info "Wrote $LABELS_FILE"
  fi

  mark_phase_done models
  log_info "Phase 10 complete: PeopleNet under ${PN_DIR}"
}

# ---------------------------------------------------------------------------
# Uninstall (reverse of install; probes first — works on partial installs)
# ---------------------------------------------------------------------------

_dpkg_installed() {
  dpkg -s "$1" >/dev/null 2>&1
}

_systemd_active_or_enabled() {
  local u="$1"
  systemctl is-active --quiet "$u" 2>/dev/null && return 0
  systemctl is-enabled --quiet "$u" 2>/dev/null
}

# uninstall_confirm_or_skip: returns 0 to proceed, 1 to abort.
_uninstall_confirm_or_skip() {
  if [[ "$ASSUME_YES" -eq 1 || "${UNINSTALL_ASSUME_YES:-0}" == "1" ]]; then
    return 0
  fi
  local reply
  read -r -p "Proceed with uninstall? [y/N] " reply
  [[ "$reply" == [yY] || "$reply" == [yY][eE][sS] ]]
}

run_uninstall() {
  require_root
  require_tool apt-get
  export DEBIAN_FRONTEND=noninteractive

  local PN_DIR="${REPO_ROOT}/laptop/deepstream/models/peoplenet"
  local did_remove_nvidia=0
  # Bootstrap-touched docker/NCT sources (if present, we assume this script added Docker/NCT)
  local have_docker_repo=0
  [[ -f /etc/apt/sources.list.d/docker.list ]] && have_docker_repo=1
  local have_nct_repo=0
  [[ -f /etc/apt/sources.list.d/nvidia-container-toolkit.list ]] && have_nct_repo=1
  local bootstrap_docker_nct=0
  [[ "$have_docker_repo" -eq 1 || "$have_nct_repo" -eq 1 ]] && bootstrap_docker_nct=1

  local -a plan=()
  # 1 — PeopleNet (only if we recorded models=done in state)
  if [[ -f "$BOOTSTRAP_STATE_FILE" ]] && grep -qx 'models=done' "$BOOTSTRAP_STATE_FILE" && [[ -d "$PN_DIR" ]]; then
    plan+=("Remove PeopleNet tree (Phase 10): $PN_DIR [state has models=done]")
  elif [[ -d "$PN_DIR" ]] && compgen -G "$PN_DIR/*" >/dev/null 2>&1; then
    plan+=("(skip) $PN_DIR exists but state has no models=done — not removing (may be from another tool)")
  fi
  # 2 — DeepStream
  if _dpkg_installed deepstream-9.0 || [[ -d /opt/nvidia/deepstream ]]; then
    plan+=("Purge deepstream-9.0 packages, rm /etc/profile.d/deepstream.sh, rm -rf /opt/nvidia/deepstream")
  fi
  # 3 — Docker + NCT
  if [[ "$bootstrap_docker_nct" -eq 1 ]]; then
    plan+=("Purge nvidia-container-toolkit + docker CE stack (repo files from this script present); remove lists/keyrings; gpasswd -d user from docker")
  elif _dpkg_installed docker-ce; then
    plan+=("(skip) docker-ce installed but no docker.list / nvidia-container-toolkit.list from this script — leaving Docker in place")
  fi
  # 4 — Mosquitto
  if _dpkg_installed mosquitto || _dpkg_installed mosquitto-clients; then
    plan+=("systemctl stop/disable mosquitto; apt purge mosquitto mosquitto-clients (leave /etc/mosquitto/conf.d/mv3dt.conf if present — from 10_setup_mosquitto.sh)")
  fi
  # 5 — Runtime / GStreamer / prereqs
  local -a run_pkgs=(
    libssl-dev libcurl4-openssl-dev libgles2-mesa-dev
    libgstreamer1.0-0 gstreamer1.0-tools
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
    gstreamer1.0-libav gstreamer1.0-plugins-rtp
    libgstreamer-plugins-base1.0-dev
    libgstrtspserver-1.0-0 gstreamer1.0-rtsp
    libjansson4 libyaml-cpp-dev libjsoncpp-dev protobuf-compiler
    libmosquitto1
    gcc make git python3
    librdkafka1 librdkafka-dev
    lsb-release xdg-utils inotify-tools jq ffmpeg unzip
  )
  local -a run_installed=()
  local p
  for p in "${run_pkgs[@]}"; do
    if _dpkg_installed "$p"; then
      run_installed+=("$p")
    fi
  done
  if [[ ${#run_installed[@]} -gt 0 ]]; then
    plan+=("Purge Phase 4 runtime set (${#run_installed[@]} packages currently installed) — libssl3 intentionally kept")
  fi
  # 6 — NVIDIA driver / CUDA / TRT
  if _dpkg_installed cuda-drivers-590 || _dpkg_installed cuda-toolkit-13-1 || _dpkg_installed libnvinfer10; then
    plan+=("Purge Phase 3 NVIDIA stack; rm /etc/profile.d/cuda.sh; apt autoremove --purge -y")
  fi
  # 7 — Local CUDA/driver repo packages
  if dpkg -l 2>/dev/null | grep -qE '^(ii|rc) +nvidia-driver-local-repo-|^ii +cuda-keyring|^ii +cuda-repo-'; then
    plan+=("Purge nvidia driver local-repo / cuda-keyring / cuda-repo-* packages; remove /var/cuda-repo-* and /var/nvidia-driver-local-repo-* stragglers; cuda .list and keyring cleanup")
  fi
  # 8 — Nouveau blacklist
  if [[ -f /etc/modprobe.d/blacklist-nouveau.conf ]]; then
    if grep -qE 'blacklist\s+nouveau' /etc/modprobe.d/blacklist-nouveau.conf 2>/dev/null; then
      plan+=("Remove /etc/modprobe.d/blacklist-nouveau.conf; update-initramfs -u (restore open-source driver path for next boot)")
    fi
  elif lsmod 2>/dev/null | grep -q '^nvidia'; then
    plan+=("(note) nouveau blacklist not on disk but nvidia module loaded — see reboot note after driver purge")
  fi
  # 9 — State file
  if [[ -f "$BOOTSTRAP_STATE_FILE" ]]; then
    plan+=("Remove bootstrap state: $BOOTSTRAP_STATE_FILE")
  fi
  # 10 — Optional env
  if [[ "$PURGE_ENV" -eq 1 && -f "$ENV_FILE" ]]; then
    plan+=("PURGE: remove $ENV_FILE (--purge-env)")
  elif [[ "$PURGE_ENV" -eq 1 ]]; then
    plan+=("PURGE: laptop.env not present; nothing to delete")
  fi

  cat <<EOF >&2

  +-----------------------------------------------------------------------+
  |  UNINSTALL  (mv3dt laptop bootstrap rollback)                        |
  +-----------------------------------------------------------------------+

EOF
  if [[ ${#plan[@]} -eq 0 ]]; then
    log_info "Nothing detected to remove (or only skipped items). Plan is empty."
  else
    log_info "Planned steps:"
    local i
    for i in "${!plan[@]}"; do
      printf '  %d) %s\n' "$((i + 1))" "${plan[i]}" >&2
    done
  fi

  if [[ ${#plan[@]} -eq 0 ]]; then
    _uninstall_print_footer "$did_remove_nvidia" 0
    return 0
  fi

  if ! _uninstall_confirm_or_skip; then
    log_info "Uninstall cancelled."
    return 0
  fi

  log_info "Executing uninstall..."

  # 1 — models
  if [[ -f "$BOOTSTRAP_STATE_FILE" ]] && grep -qx 'models=done' "$BOOTSTRAP_STATE_FILE" && [[ -d "$PN_DIR" ]]; then
    log_info "Removing $PN_DIR"
    rm -rf "$PN_DIR"
  fi

  # 2 — DeepStream
  local -a ds_pkgs=()
  for p in deepstream-9.0 deepstream-9.0-reference-graphs deepstream-9.0-samples; do
    _dpkg_installed "$p" && ds_pkgs+=("$p")
  done
  if [[ ${#ds_pkgs[@]} -gt 0 ]]; then
    apt-get purge -y "${ds_pkgs[@]}" || log_warn "Some DeepStream packages failed to purge; continuing."
  fi
  rm -f /etc/profile.d/deepstream.sh
  if [[ -d /opt/nvidia/deepstream ]]; then
    log_info "Removing /opt/nvidia/deepstream"
    rm -rf /opt/nvidia/deepstream
  fi

  # 3 — Docker + NCT
  if [[ "$bootstrap_docker_nct" -eq 1 ]]; then
    systemctl stop docker 2>/dev/null || true
    local -a dpkg_remove=(
      nvidia-container-toolkit
      docker-ce docker-ce-cli containerd.io
      docker-buildx-plugin docker-compose-plugin
    )
    local d
    for d in "${dpkg_remove[@]}"; do
      if _dpkg_installed "$d"; then
        apt-get purge -y "$d" || log_warn "apt purge $d failed; continuing."
      fi
    done
    rm -f /etc/apt/sources.list.d/docker.list
    rm -f /etc/apt/sources.list.d/nvidia-container-toolkit.list
    rm -f /etc/apt/keyrings/docker.gpg
    rm -f /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]] && id -nG "$SUDO_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
      gpasswd -d "$SUDO_USER" docker 2>/dev/null || log_warn "gpasswd -d $SUDO_USER docker failed (ignore if not in group)."
    fi
  fi

  # 4 — Mosquitto
  if _systemd_active_or_enabled mosquitto; then
    systemctl disable --now mosquitto 2>/dev/null || true
  fi
  if _dpkg_installed mosquitto || _dpkg_installed mosquitto-clients; then
    apt-get purge -y mosquitto mosquitto-clients 2>/dev/null || true
  fi

  # 5 — Runtime (only packages still installed; libssl3 not listed)
  if [[ ${#run_installed[@]} -gt 0 ]]; then
    apt-get purge -y "${run_installed[@]}" 2>/dev/null || log_warn "Some runtime packages failed to purge; continuing."
  fi

  # 6 — NVIDIA stack (exact Phase 3 names)
  local -a nv_pkgs=(
    cuda-drivers-590
    cuda-toolkit-13-1
    libcudnn9-cuda-13
    libcudnn9-dev-cuda-13
    libnvinfer-dev
    libnvinfer-dispatch-dev
    libnvinfer-dispatch10
    libnvinfer-headers-dev
    libnvinfer-headers-plugin-dev
    libnvinfer-lean-dev
    libnvinfer-lean10
    libnvinfer-plugin-dev
    libnvinfer-plugin10
    libnvinfer-vc-plugin-dev
    libnvinfer-vc-plugin10
    libnvinfer10
    libnvonnxparsers-dev
    libnvonnxparsers10
    tensorrt-dev
  )
  local -a nv_installed=()
  for p in "${nv_pkgs[@]}"; do
    _dpkg_installed "$p" && nv_installed+=("$p")
  done
  if [[ ${#nv_installed[@]} -gt 0 ]]; then
    did_remove_nvidia=1
    apt-get purge -y "${nv_installed[@]}" 2>/dev/null || log_warn "Some NVIDIA stack packages failed to purge; continuing."
  fi
  rm -f /etc/profile.d/cuda.sh
  apt-get autoremove --purge -y 2>/dev/null || true

  # 7 — Local driver/CUDA repo packages
  local pkg
  while read -r pkg; do
    [[ -n "$pkg" ]] && apt-get purge -y "$pkg" 2>/dev/null || true
  done < <(dpkg -l 2>/dev/null | awk '/^ii/ && $2 ~ /^(nvidia-driver-local-repo-|cuda-repo-)/ {print $2}' | sort -u)
  if _dpkg_installed cuda-keyring; then
    apt-get purge -y cuda-keyring 2>/dev/null || true
  fi
  shopt -s nullglob
  local _c
  for _c in /etc/apt/sources.list.d/cuda-*.list; do
    rm -f "$_c"
  done
  for _c in /var/cuda-repo-*; do
    [[ -d "$_c" ]] && rm -rf "$_c"
  done
  for _c in /var/nvidia-driver-local-repo-*; do
    [[ -d "$_c" ]] && rm -rf "$_c"
  done
  for _c in /usr/share/keyrings/cuda-*.gpg; do
    rm -f "$_c"
  done
  shopt -u nullglob
  apt-get update -y 2>/dev/null || true

  # 8 — Nouveau blacklist
  if [[ -f /etc/modprobe.d/blacklist-nouveau.conf ]] && grep -qE 'blacklist\s+nouveau' /etc/modprobe.d/blacklist-nouveau.conf 2>/dev/null; then
    rm -f /etc/modprobe.d/blacklist-nouveau.conf
    if command -v update-initramfs >/dev/null 2>&1; then
      update-initramfs -u 2>/dev/null || log_warn "update-initramfs -u failed (may still be ok)."
    fi
  fi

  # 9 — State
  rm -f "$BOOTSTRAP_STATE_FILE"
  log_info "Removed $BOOTSTRAP_STATE_FILE (if it existed)."

  # 10 — Env
  if [[ "$PURGE_ENV" -eq 1 && -f "$ENV_FILE" ]]; then
    rm -f "$ENV_FILE"
    log_info "Removed $ENV_FILE (--purge-env)."
  fi

  _uninstall_print_footer "$did_remove_nvidia" 1
}

_uninstall_print_footer() {
  local removed_nvidia="$1"
  local ran="${2:-0}"
  cat <<EOF >&2

  +-----------------------------------------------------------------------+
  |  Uninstall summary                                                    |
  +-----------------------------------------------------------------------+

  Not removed (on purpose):
    • Phase 1 base tools: build-essential, dkms, linux-headers-*, software-properties-common,
      ca-certificates, gnupg, curl, unzip, etc. — safe to keep; remove manually with apt if desired.
    • NGC CLI under ~/ngc-cli (or your install path) — this script never installed it. To remove:
        rm -rf "\$HOME/ngc-cli"
    • Pre-downloaded .deb files in LOCAL_DEB_DIR — not touched.

EOF
  if [[ "$removed_nvidia" -eq 1 ]]; then
    cat <<EOF >&2
  Reboot recommended: the NVIDIA driver packages were purged, but a loaded
  nvidia.ko (if any) stays until the next boot. Reboot to complete cleanup.

EOF
  elif [[ "$ran" -eq 1 ]]; then
    cat <<EOF >&2
  Reboot: optional unless you removed kernel-level NVIDIA or nouveau state.

EOF
  fi
}

# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------
main() {
  # --uninstall runs first: rollback only, no install phases.
  if [[ "$DO_UNINSTALL" -eq 1 ]]; then
    if [[ "$RESET_STATE_FLAG" -eq 1 || "$FORCE_MODELS" -eq 1 || -n "$RESUME_FROM" ]]; then
      log_warn "Ignoring --reset-state / --force-models / --resume-from while --uninstall is set."
    fi
    run_uninstall
    return 0
  fi

  # --- State management flags (applied before any phase runs) ---
  if [[ "$RESET_STATE_FLAG" -eq 1 ]]; then
    reset_state
  fi

  if [[ "$FORCE_MODELS" -eq 1 && -f "$BOOTSTRAP_STATE_FILE" ]]; then
    sed -i '/^models=done$/d' "$BOOTSTRAP_STATE_FILE"
    log_info "--force-models: cleared models phase from state file."
  fi

  if [[ -n "$RESUME_FROM" ]]; then
    log_warn "--resume-from ${RESUME_FROM} set; phases before '${RESUME_FROM}' will still be skipped if already marked done."
  fi

  # --- Phase 0: preflight + policy banner ---
  if [[ "$SKIP_PREFLIGHT" -ne 1 ]]; then
    if phase_done preflight; then
      log_info "Phase 0 (preflight) already complete; skipping."
    else
      run_phase0_preflight
    fi
  else
    log_warn "Phase 0 (preflight) skipped via --skip-preflight."
  fi

  # --- Phases 1-10: install ---
  if [[ "$SKIP_INSTALL" -ne 1 ]]; then

    # Phase 1: base deps
    if phase_done base-deps; then
      log_info "Phase 1 (base deps) already complete; skipping."
    else
      run_phase1_base
    fi

    # Phase 2: nouveau cleanup
    if phase_done nouveau-blacklist; then
      log_info "Phase 2 (nouveau blacklist) already complete; skipping."
    else
      run_phase2_nouveau
    fi

    if phase_done local-deb-verified; then
      log_info "Phase 2b (local .deb verification) already complete; skipping."
    else
      run_phase2b_verify_debs
    fi

    if phase_done nvidia-stack; then
      log_info "Phase 3 (NVIDIA driver + CUDA + cuDNN + TRT) already complete; skipping."
    else
      run_phase3_nvidia_stack
    fi

    if phase_done runtime-deps; then
      log_info "Phase 4 (runtime / GStreamer / Docker / NCT) already complete; skipping."
    else
      run_phase4_runtime
    fi

    if phase_done ngc-pause; then
      log_info "Phase 5 (NGC CLI gate) already complete; skipping."
    else
      run_phase5_ngc_gate
    fi

    if phase_done ds-deb && dpkg -s deepstream-9.0 >/dev/null 2>&1; then
      log_info "Phase 6 (DeepStream .deb) already complete; skipping."
    else
      run_phase6_ds_deb
    fi

    if phase_done ds-post; then
      log_info "Phase 7 (DS post-install) already complete; skipping."
    else
      run_phase7_post_install
    fi

    if phase_done version-audit; then
      log_info "Phase 8 (version audit) already complete; skipping."
    else
      run_phase8_version_audit
    fi

  else
    log_warn "Install phases (1-8, 10) skipped via --skip-install."
  fi

  run_phase9_env

  if [[ "$SKIP_INSTALL" -ne 1 ]]; then
    run_phase10_models
  fi

  cat <<'EOF'

Bootstrap complete. Next steps:

  sudo bash laptop/scripts/10_setup_mosquitto.sh
       bash laptop/scripts/20_verify_cameras.sh
       bash laptop/scripts/30_start_amc.sh
  # complete the 6-step AMC workflow in your browser
       bash laptop/scripts/40_export_watcher.sh --oneshot
       bash laptop/scripts/50_start_pipeline.sh

Detector: PeopleNet ONNX (PEOPLENET_NGC_TAG in laptop/config/laptop.env).
Phases 9-10: laptop.env and PeopleNet models under laptop/deepstream/models/peoplenet/.
EOF
}

main
