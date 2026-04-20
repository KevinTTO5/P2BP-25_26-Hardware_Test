#!/usr/bin/env bash
# laptop/scripts/00_bootstrap.sh
#
# Entry point for the DS 9.0 laptop scripted testing harness. Runs two phases:
#
#   (a) PREFLIGHT — hard verify that Notion page
#       337b5d58-7212-81e1-b07a-d510d9605bbb §1-4 prerequisites were already
#       completed manually. Does NOT attempt to install nvidia-driver,
#       cuda-toolkit, libcudnn9, or tensorrt — those are out of scope.
#       Fails fast with a pointer to laptop/docs/DEEPSTREAM-SETUP.md §1-4 if
#       anything is missing.
#
#   (b) INSTALL — idempotent install of exactly Notion §5 + §8.2 scope:
#         - DeepStream 9.0 apt repo + deepstream-9.0 Debian package.
#         - GStreamer 1.24 plugin set per Notion §5.3.
#         - Mosquitto + mosquitto-clients.
#         - Docker Engine + NVIDIA Container Toolkit (for AMC, §8.2).
#         - xdg-utils, inotify-tools, jq, ffmpeg (script dependencies).
#       Exports DEEPSTREAM_DIR=/opt/nvidia/deepstream/deepstream-9.0 via
#       /etc/profile.d/deepstream.sh per Notion §5.2.
#       Writes laptop/config/laptop.env interactively (same reuse pattern as
#       install.sh lines 48-109) with prompts for HOST_IP, LOCATION_ID,
#       CAM_USER, CAM_PASSWORD, NGC_API_KEY, PROJECT_NAME. DETECTOR is pinned
#       to `peoplenet` and is not operator-selectable.
#
# Isolation: writes only under /etc/, laptop/config/laptop.env, and via apt.
# Never touches the Jetson-side tree (scripts/, services/, config/, ...).
#
# References (via .cursor/skills/deepstream-9-docs/):
#   - DS 9.0 Installation:
#     https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Installation.html
#   - DS 9.0 Quickstart:
#     https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Quickstart.html
#   - DS 9.0 Docker Containers (for NVIDIA Container Toolkit pointer):
#     https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_docker_containers.html

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

SKIP_PREFLIGHT=0
SKIP_INSTALL=0
NONINTERACTIVE=0

usage() {
  cat <<'EOF'
Usage: 00_bootstrap.sh [--skip-preflight] [--skip-install]
                       [--non-interactive] [-h|--help]

Preflights Notion §1-4 prerequisites then installs Notion §5 (DeepStream 9.0 +
GStreamer 1.24), §6 (Mosquitto), and §8.2 (Docker + NVIDIA Container Toolkit).
Interactively writes laptop/config/laptop.env.

Options:
  --skip-preflight   Skip the §1-4 prerequisite checks (not recommended).
  --skip-install     Only run the preflight and env-write steps; no apt.
  --non-interactive  Reuse existing laptop/config/laptop.env values (or empty
                     fallbacks from laptop.env.example) without prompting.
  -h, --help         Show this help and exit.

Must be run with sudo (the install phase writes to /etc and runs apt).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-preflight) SKIP_PREFLIGHT=1 ;;
    --skip-install)   SKIP_INSTALL=1 ;;
    --non-interactive) NONINTERACTIVE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "Unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

REPO_ROOT="$(repo_root)"
ENV_FILE="$REPO_ROOT/laptop/config/laptop.env"
ENV_EXAMPLE="$REPO_ROOT/laptop/config/laptop.env.example"
SETUP_DOC="laptop/docs/DEEPSTREAM-SETUP.md"

# ---------------------------------------------------------------------------
# (a) Preflight — verify Notion §1-4 were completed manually.
# ---------------------------------------------------------------------------
run_preflight() {
  log_info "Preflight: verifying Notion §1-4 prerequisites (manual, out of scope here)."

  local -a missing=()

  # §3 Ubuntu 24.04
  if command -v lsb_release >/dev/null 2>&1; then
    local distro codename release
    distro="$(lsb_release -is 2>/dev/null || echo "unknown")"
    release="$(lsb_release -rs 2>/dev/null || echo "0")"
    codename="$(lsb_release -cs 2>/dev/null || echo "unknown")"
    if [[ "$distro" != "Ubuntu" || "$release" != "24.04" ]]; then
      missing+=("Ubuntu 24.04 (found: $distro $release / $codename)  [§3]")
    fi
  else
    missing+=("lsb_release not available; cannot verify Ubuntu 24.04  [§3]")
  fi

  # §4 NVIDIA driver >= 550
  if command -v nvidia-smi >/dev/null 2>&1; then
    local drv_ver drv_major
    drv_ver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' ')"
    drv_major="${drv_ver%%.*}"
    if [[ -z "$drv_major" || "$drv_major" -lt 550 ]]; then
      missing+=("NVIDIA driver >= 550 (found: ${drv_ver:-none})  [§4]")
    fi
  else
    missing+=("nvidia-smi not found; NVIDIA driver not installed  [§4]")
  fi

  # §4 CUDA Toolkit >= 12.4
  if command -v nvcc >/dev/null 2>&1; then
    local cuda_ver cuda_major cuda_minor
    cuda_ver="$(nvcc --version 2>/dev/null | awk -F'release ' '/release/ {print $2}' | awk '{print $1}' | tr -d ',')"
    cuda_major="${cuda_ver%%.*}"
    cuda_minor="${cuda_ver#*.}"
    cuda_minor="${cuda_minor%%.*}"
    if [[ -z "$cuda_major" ]]; then
      missing+=("CUDA Toolkit >= 12.4 (could not parse nvcc output)  [§4]")
    elif (( cuda_major < 12 )) || (( cuda_major == 12 && cuda_minor < 4 )); then
      missing+=("CUDA Toolkit >= 12.4 (found: $cuda_ver)  [§4]")
    fi
  else
    missing+=("nvcc not found; CUDA Toolkit 12.4+ not installed  [§4]")
  fi

  # §4 cuDNN 9.x (debian package libcudnn9-*)
  if command -v dpkg >/dev/null 2>&1; then
    if ! dpkg -l 2>/dev/null | awk '{print $2}' | grep -qE '^libcudnn9(-|$)'; then
      missing+=("cuDNN >= 9.0 (no libcudnn9 package found via dpkg -l)  [§4]")
    fi
  else
    missing+=("dpkg not available; cannot verify cuDNN 9  [§4]")
  fi

  # §4 TensorRT 10.x
  if command -v dpkg >/dev/null 2>&1; then
    if ! dpkg -l 2>/dev/null | awk '{print $2}' | grep -qE '^(libnvinfer10|tensorrt)'; then
      missing+=("TensorRT >= 10.0 (no libnvinfer10/tensorrt package found via dpkg -l)  [§4]")
    fi
  fi

  # §1-2 CUDA-capable GPU (Ampere or newer — compute capability >= 8.0)
  if command -v nvidia-smi >/dev/null 2>&1; then
    local cc
    cc="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' ')"
    if [[ -n "$cc" ]]; then
      local cc_major cc_minor
      cc_major="${cc%%.*}"
      cc_minor="${cc#*.}"
      if (( cc_major < 8 )); then
        missing+=("Ampere-or-newer NVIDIA GPU (compute capability >= 8.0; found $cc)  [§1-2]")
      fi
      log_info "GPU compute capability: $cc"
    fi
  fi

  if (( ${#missing[@]} > 0 )); then
    log_error "Preflight failed — the following Notion §1-4 prerequisites are not satisfied:"
    local item
    for item in "${missing[@]}"; do
      printf '         - %s\n' "$item" >&2
    done
    cat >&2 <<EOF

These sections are MANUAL and out of scope for every script in laptop/scripts/.
See: $SETUP_DOC  §1-4 for the step-by-step procedure.

Once §1-4 are complete, re-run this script.
EOF
    exit 1
  fi

  log_info "Preflight OK: Ubuntu 24.04 + NVIDIA driver + CUDA 12.4+ + cuDNN 9 + TensorRT 10 detected."
}

if [[ "$SKIP_PREFLIGHT" -ne 1 ]]; then
  run_preflight
else
  log_warn "Preflight skipped via --skip-preflight. Notion §1-4 assumed satisfied."
fi

# ---------------------------------------------------------------------------
# (b) Install — Notion §5 + §8.2 only. Idempotent.
# ---------------------------------------------------------------------------

if [[ "$SKIP_INSTALL" -ne 1 ]]; then
  require_root
  require_tool apt-get

  export DEBIAN_FRONTEND=noninteractive

  log_info "apt-get update"
  apt-get update -y

  log_info "Installing base tooling (ffmpeg, jq, inotify-tools, xdg-utils, curl, gnupg)"
  apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg lsb-release \
    xdg-utils inotify-tools jq ffmpeg \
    mosquitto mosquitto-clients

  # --- DS 9.0 apt repo + package (Notion §5.1, §5.2) ----------------------
  DS_PKG="deepstream-9.0"
  if ! dpkg -s "$DS_PKG" >/dev/null 2>&1; then
    log_info "Configuring NVIDIA DeepStream apt repo and installing $DS_PKG (Notion §5)"
    # NVIDIA publishes the DS apt repo via the CUDA keyring on Ubuntu 24.04.
    # Prefer the keyring already shipped by §4 (cuda-keyring). If not present,
    # warn — §4 should have installed it.
    if [[ ! -f /usr/share/keyrings/cuda-archive-keyring.gpg ]]; then
      log_warn "cuda-archive-keyring.gpg not found. Installing cuda-keyring (Notion §4 prerequisite)."
      _tmp_keyring="$(mktemp --suffix=.deb)"
      curl -fsSL -o "$_tmp_keyring" \
        "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb"
      dpkg -i "$_tmp_keyring"
      rm -f "$_tmp_keyring"
      apt-get update -y
    fi
    apt-get install -y --no-install-recommends \
      "$DS_PKG" \
      deepstream-9.0-reference-graphs \
      deepstream-9.0-samples || {
        die "Failed to install $DS_PKG. Re-check the DS 9.0 Installation doc: https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Installation.html (via .cursor/skills/deepstream-9-docs/)"
      }
  else
    log_info "$DS_PKG already installed; skipping DS apt install."
  fi

  # --- GStreamer 1.24 plugin set (Notion §5.3) -----------------------------
  log_info "Installing GStreamer 1.24 plugin set (Notion §5.3)"
  apt-get install -y --no-install-recommends \
    libgstreamer1.0-0 gstreamer1.0-tools \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav gstreamer1.0-plugins-rtp \
    libgstreamer-plugins-base1.0-dev \
    libgstrtspserver-1.0-0 gstreamer1.0-rtsp

  # --- /etc/profile.d/deepstream.sh (Notion §5.2) --------------------------
  DS_PROFILE="/etc/profile.d/deepstream.sh"
  DS_DIR="/opt/nvidia/deepstream/deepstream-9.0"
  if [[ ! -f "$DS_PROFILE" ]] || ! grep -q "$DS_DIR" "$DS_PROFILE" 2>/dev/null; then
    log_info "Writing $DS_PROFILE (Notion §5.2)"
    cat > "$DS_PROFILE" <<EOF
# Managed by laptop/scripts/00_bootstrap.sh (Notion §5.2)
export DEEPSTREAM_DIR=$DS_DIR
case ":\$PATH:" in
  *":$DS_DIR/bin:"*) : ;;
  *) export PATH="$DS_DIR/bin:\$PATH" ;;
esac
case ":\${LD_LIBRARY_PATH:-}:" in
  *":$DS_DIR/lib:"*) : ;;
  *) export LD_LIBRARY_PATH="$DS_DIR/lib:\${LD_LIBRARY_PATH:-}" ;;
esac
EOF
    chmod 0644 "$DS_PROFILE"
  else
    log_info "$DS_PROFILE already in place; leaving untouched."
  fi

  # --- Docker Engine + NVIDIA Container Toolkit (Notion §8.2) --------------
  if ! command -v docker >/dev/null 2>&1; then
    log_info "Installing Docker Engine (Notion §8.2)"
    install -m 0755 -d /etc/apt/keyrings
    if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
        gpg --dearmor -o /etc/apt/keyrings/docker.gpg
      chmod a+r /etc/apt/keyrings/docker.gpg
    fi
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y --no-install-recommends \
      docker-ce docker-ce-cli containerd.io \
      docker-buildx-plugin docker-compose-plugin
  else
    log_info "docker already installed; skipping."
  fi

  if ! dpkg -s nvidia-container-toolkit >/dev/null 2>&1; then
    log_info "Installing NVIDIA Container Toolkit (Notion §8.2)"
    if [[ ! -f /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg ]]; then
      curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
        gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    fi
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
      sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update -y
    apt-get install -y --no-install-recommends nvidia-container-toolkit
    if command -v nvidia-ctk >/dev/null 2>&1; then
      nvidia-ctk runtime configure --runtime=docker || \
        log_warn "nvidia-ctk runtime configure failed; configure Docker NVIDIA runtime manually."
      systemctl restart docker || log_warn "systemctl restart docker failed."
    fi
  else
    log_info "nvidia-container-toolkit already installed; skipping."
  fi

  # Add invoking user to docker group so subsequent scripts don't need sudo
  # for docker compose (best-effort; operator may need a re-login).
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    if ! id -nG "$SUDO_USER" | tr ' ' '\n' | grep -qx docker; then
      log_info "Adding $SUDO_USER to the 'docker' group (re-login required)."
      usermod -aG docker "$SUDO_USER" || log_warn "usermod -aG docker failed."
    fi
  fi
else
  log_warn "Install phase skipped via --skip-install."
fi

# ---------------------------------------------------------------------------
# (c) Write laptop/config/laptop.env interactively
# ---------------------------------------------------------------------------

mkdir -p "$(dirname "$ENV_FILE")"

# Seed defaults from an existing env file if present, else from the example.
_get() {
  local key="$1"; local src="$2"
  [[ -f "$src" ]] || { echo ""; return; }
  local line
  line="$(grep -m1 "^${key}=" "$src" || true)"
  echo "${line#${key}=}"
}

EXISTING_SRC="$ENV_FILE"
[[ -f "$EXISTING_SRC" ]] || EXISTING_SRC="$ENV_EXAMPLE"

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

prompt() {
  local label="$1"; local default="$2"; local secret="${3:-0}"; local var
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
  log_info "Reusing existing values from $ENV_FILE (press Enter to keep each)."
else
  log_info "Creating $ENV_FILE from laptop.env.example defaults."
fi

HOST_IP="$(prompt "HOST_IP (laptop IPv4 on camera LAN)" "${CUR_HOST_IP:-192.168.10.10}")"
LOCATION_ID="$(prompt "LOCATION_ID (short site label)" "${CUR_LOCATION_ID:-test-lab-01}")"
PROJECT_NAME="$(prompt "PROJECT_NAME (AMC project name)" "${CUR_PROJECT_NAME:-$LOCATION_ID}")"
CAM_USER="$(prompt "CAM_USER" "${CUR_CAM_USER:-admin}")"
CAM_PASSWORD="$(prompt "CAM_PASSWORD" "${CUR_CAM_PASSWORD:-}" 1)"
NGC_API_KEY="$(prompt "NGC_API_KEY (blank if docker login nvcr.io already done)" "${CUR_NGC_API_KEY:-}" 1)"

# DETECTOR is pinned per the plan: PeopleNet only. Do NOT prompt for it.
DETECTOR="peoplenet"
PEOPLENET_NGC_TAG="${CUR_PEOPLENET_NGC_TAG:-nvidia/tao/peoplenet:deployable_quantized_v2.6.3}"
AMC_ROOT="${CUR_AMC_ROOT:-\$HOME/auto-magic-calib}"
MS_PORT="${CUR_MS_PORT:-8000}"
UI_PORT="${CUR_UI_PORT:-5000}"
MQTT_HOST="${CUR_MQTT_HOST:-127.0.0.1}"
MQTT_PORT="${CUR_MQTT_PORT:-1883}"
MQTT_TOPIC_BASE="${CUR_MQTT_TOPIC_BASE:-mv3dt}"

if [[ -z "$HOST_IP" || -z "$LOCATION_ID" || -z "$CAM_USER" || -z "$CAM_PASSWORD" ]]; then
  die "HOST_IP, LOCATION_ID, CAM_USER, and CAM_PASSWORD must not be empty."
fi

TMP_ENV="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
cat > "$TMP_ENV" <<EOF
# Managed by laptop/scripts/00_bootstrap.sh. Edit via re-running that script;
# direct edits are fine but will be preserved on next run.
HOST_IP=$HOST_IP
LOCATION_ID=$LOCATION_ID
CAM_USER=$CAM_USER
CAM_PASSWORD=$CAM_PASSWORD
NGC_API_KEY=$NGC_API_KEY
PROJECT_NAME=$PROJECT_NAME

# Pinned detector — PeopleNet only (DS 9.0 MV3DT reference).
DETECTOR=$DETECTOR
PEOPLENET_NGC_TAG=$PEOPLENET_NGC_TAG

# AMC runtime (cloned outside this repo by 30_start_amc.sh).
AMC_ROOT=$AMC_ROOT
AUTO_MAGIC_CALIB_MS_PORT=$MS_PORT
AUTO_MAGIC_CALIB_UI_PORT=$UI_PORT

# Mosquitto.
MQTT_HOST=$MQTT_HOST
MQTT_PORT=$MQTT_PORT
MQTT_TOPIC_BASE=$MQTT_TOPIC_BASE
EOF
chown root:root "$TMP_ENV" 2>/dev/null || true
chmod 0640 "$TMP_ENV"
mv -f "$TMP_ENV" "$ENV_FILE"

log_info "Wrote $ENV_FILE"

cat <<EOF

Bootstrap complete. Next steps:

  sudo bash laptop/scripts/10_setup_mosquitto.sh
       bash laptop/scripts/20_verify_cameras.sh
       bash laptop/scripts/25_prepare_models.sh
       bash laptop/scripts/30_start_amc.sh
  # complete the 6-step AMC workflow in your browser
       bash laptop/scripts/40_export_watcher.sh --oneshot
       bash laptop/scripts/50_start_pipeline.sh

Detector policy: PeopleNet only (NVIDIA's DS 9.0 MV3DT reference). yolo11n is
the single approved alternative detector for future work and is NOT installed
by any script in this harness.
EOF
