#!/usr/bin/env bash
# laptop/scripts/30_start_amc.sh
#
# Notion page 337b5d58-7212-81e1-b07a-d510d9605bbb §8.3-8.5 — AMC bring-up.
#
# Treats NVIDIA's AMC repo as a RUNTIME DEPENDENCY, not part of this repo:
#   * clones https://github.com/NVIDIA-AI-IOT/auto-magic-calib.git into
#     $HOME/auto-magic-calib/ (never under laptop/) if missing.
#   * `docker login nvcr.io` using NGC_API_KEY (if provided).
#   * creates projects/ and models/ under $AMC_ROOT with the chown 1000:1000
#     ownership tweak from Notion §8.3.
#   * writes $AMC_ROOT/compose/.env mapping HOST_IP, ports, PROJECT_DIR,
#     MODEL_DIR, NVIDIA_VISIBLE_DEVICES=all from laptop/config/laptop.env.
#   * docker compose pull && docker compose up -d.
#   * xdg-open http://localhost:${AUTO_MAGIC_CALIB_UI_PORT} if on a desktop
#     session; otherwise prints the URL.
#
# The 6-step AMC workflow itself is HUMAN-DRIVEN in the browser — this script
# only gets the operator to the landing page. See the DS 9.0 AutoMagicCalib
# doc (via .cursor/skills/deepstream-9-docs/):
#   https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_AutoMagicCalib.html

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

SKIP_PULL=0

usage() {
  cat <<'EOF'
Usage: 30_start_amc.sh [--skip-pull] [-h|--help]

Bring up NVIDIA-AI-IOT/auto-magic-calib via docker compose in
$HOME/auto-magic-calib/ (or $AMC_ROOT if set in laptop/config/laptop.env),
then open the AMC web UI.

Options:
  --skip-pull   Skip 'docker compose pull' (faster re-runs on metered links).
  -h, --help    Show this help and exit.

Environment used (laptop/config/laptop.env):
  HOST_IP                    IPv4 on camera LAN (required).
  LOCATION_ID / PROJECT_NAME Project label used by AMC.
  NGC_API_KEY                Enables non-interactive 'docker login nvcr.io'.
  AMC_ROOT                   AMC clone path (default $HOME/auto-magic-calib).
  AUTO_MAGIC_CALIB_MS_PORT   Microservice port (default 8000).
  AUTO_MAGIC_CALIB_UI_PORT   Web UI port         (default 5000).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-pull) SKIP_PULL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "Unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

require_tool git
require_tool docker

# docker compose plugin vs legacy docker-compose
DC=(docker compose)
if ! docker compose version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
  else
    die "Neither 'docker compose' nor 'docker-compose' is available. Re-run 00_bootstrap.sh."
  fi
fi

load_env

: "${HOST_IP:?HOST_IP is required in laptop/config/laptop.env}"
: "${PROJECT_NAME:=${LOCATION_ID:-test-lab-01}}"
: "${AMC_ROOT:=$HOME/auto-magic-calib}"
: "${AUTO_MAGIC_CALIB_MS_PORT:=8000}"
: "${AUTO_MAGIC_CALIB_UI_PORT:=5000}"

# AMC clone MUST live outside this repo's working tree (plan isolation rule).
REPO_ROOT="$(repo_root)"
case "$AMC_ROOT" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    die "AMC_ROOT ($AMC_ROOT) must not live under this repo ($REPO_ROOT). Fix laptop/config/laptop.env."
    ;;
esac

if [[ ! -d "$AMC_ROOT" ]]; then
  log_info "Cloning NVIDIA-AI-IOT/auto-magic-calib into $AMC_ROOT"
  git clone https://github.com/NVIDIA-AI-IOT/auto-magic-calib.git "$AMC_ROOT"
else
  log_info "AMC repo already present at $AMC_ROOT"
fi

PROJECT_DIR="$AMC_ROOT/projects"
MODEL_DIR="$AMC_ROOT/models"
mkdir -p "$PROJECT_DIR" "$MODEL_DIR"

# Notion §8.3 ownership tweak so the in-container UID (1000) can write.
if command -v chown >/dev/null 2>&1; then
  if [[ "$(id -u)" -eq 0 ]]; then
    chown -R 1000:1000 "$PROJECT_DIR" "$MODEL_DIR" || true
  else
    sudo chown -R 1000:1000 "$PROJECT_DIR" "$MODEL_DIR" || \
      log_warn "chown 1000:1000 on $PROJECT_DIR/$MODEL_DIR failed; AMC may hit permission errors."
  fi
fi

if [[ -n "${NGC_API_KEY:-}" ]]; then
  log_info "docker login nvcr.io (using NGC_API_KEY)"
  echo "$NGC_API_KEY" | docker login nvcr.io --username '$oauthtoken' --password-stdin >/dev/null || \
    log_warn "'docker login nvcr.io' failed; if AMC images are public you can ignore this."
else
  log_warn "NGC_API_KEY not set; assuming 'docker login nvcr.io' was already run."
fi

COMPOSE_DIR="$AMC_ROOT/compose"
if [[ ! -d "$COMPOSE_DIR" ]]; then
  # Upstream may move things; fall back to repo root.
  if [[ -f "$AMC_ROOT/compose.yaml" || -f "$AMC_ROOT/docker-compose.yml" ]]; then
    COMPOSE_DIR="$AMC_ROOT"
  else
    die "Cannot locate compose dir inside $AMC_ROOT. Upstream layout changed; re-check NVIDIA-AI-IOT/auto-magic-calib README."
  fi
fi

ENV_FILE="$COMPOSE_DIR/.env"
log_info "Writing AMC compose env: $ENV_FILE"

# Surface upstream-rename drift: compare our keys to AMC's own .env.example.
UPSTREAM_EX="$COMPOSE_DIR/.env.example"
if [[ -f "$UPSTREAM_EX" ]]; then
  for key in HOST_IP AUTO_MAGIC_CALIB_MS_PORT AUTO_MAGIC_CALIB_UI_PORT PROJECT_DIR MODEL_DIR NVIDIA_VISIBLE_DEVICES; do
    if ! grep -q "^${key}=" "$UPSTREAM_EX"; then
      log_warn "Upstream AMC .env.example no longer defines '$key'. Re-check NVIDIA-AI-IOT/auto-magic-calib README."
    fi
  done
fi

TMP_ENV="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
cat > "$TMP_ENV" <<EOF
# Generated by laptop/scripts/30_start_amc.sh
# Source: laptop/config/laptop.env + Notion §8.3-8.4 defaults.
# Re-verify against the upstream NVIDIA-AI-IOT/auto-magic-calib README if
# AMC version drifts.
HOST_IP=${HOST_IP}
AUTO_MAGIC_CALIB_MS_PORT=${AUTO_MAGIC_CALIB_MS_PORT}
AUTO_MAGIC_CALIB_UI_PORT=${AUTO_MAGIC_CALIB_UI_PORT}
PROJECT_DIR=${PROJECT_DIR}
MODEL_DIR=${MODEL_DIR}
NVIDIA_VISIBLE_DEVICES=all
PROJECT_NAME=${PROJECT_NAME}
EOF
mv -f "$TMP_ENV" "$ENV_FILE"

mkdir -p "$PROJECT_DIR/$PROJECT_NAME"

if [[ "$SKIP_PULL" -ne 1 ]]; then
  log_info "docker compose pull (in $COMPOSE_DIR)"
  ( cd "$COMPOSE_DIR" && "${DC[@]}" pull ) || \
    log_warn "docker compose pull failed; continuing with any locally cached images."
fi

log_info "docker compose up -d (in $COMPOSE_DIR)"
( cd "$COMPOSE_DIR" && "${DC[@]}" up -d )

URL="http://localhost:${AUTO_MAGIC_CALIB_UI_PORT}"
log_info "AMC web UI should be reachable at $URL"

if [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
  if command -v xdg-open >/dev/null 2>&1; then
    ( xdg-open "$URL" >/dev/null 2>&1 & ) || true
  fi
else
  log_info "Headless session detected; open $URL in your browser manually."
fi

cat <<EOF

AMC is now running. Complete the 6-step workflow in the browser
(Notion §8.6, cross-referenced with the DS 9.0 AutoMagicCalib guide):
  1. Project Setup   2. Video Upload   3. Parameters
  4. Manual Align    5. Execute        6. Results / Export
Reference: https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_AutoMagicCalib.html

VGGT note (via .cursor/skills/deepstream-9-docs/): if AMC times out during
"Execute", the common cause is GPU VRAM pressure from the VGGT stage — see
the AutoMagicCalib doc for the workaround.

When AMC finishes export, start the watcher in a second shell:
  laptop/scripts/40_export_watcher.sh
EOF
