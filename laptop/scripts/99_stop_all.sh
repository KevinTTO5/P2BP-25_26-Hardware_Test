#!/usr/bin/env bash
# laptop/scripts/99_stop_all.sh
#
# Tear-down counterpart to the laptop/scripts/ run order. Stops everything
# the earlier scripts started, in reverse of 10 -> 30 -> 50:
#
#   1. deepstream-app (from 50_start_pipeline.sh) — pkill by name.
#   2. AMC docker compose stack (from 30_start_amc.sh) — `docker compose down`
#      in $AMC_ROOT/compose (or $AMC_ROOT if the upstream layout moved).
#   3. Mosquitto (from 10_setup_mosquitto.sh) — `systemctl stop mosquitto`.
#
# This script leaves INSTALLED packages, the AMC clone at $AMC_ROOT, and
# laptop/deepstream/calibration/<LOCATION_ID>/ in place; it only stops
# running processes/services.
#
# Idempotent: re-running on a clean laptop is a no-op. Individual steps are
# best-effort — failure in one does not abort the rest.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

STOP_DS=1
STOP_AMC=1
STOP_MQTT=1

usage() {
  cat <<'EOF'
Usage: 99_stop_all.sh [--no-deepstream] [--no-amc] [--no-mosquitto] [-h|--help]

Stops the laptop DS 9.0 MV3DT harness. All three services are stopped by
default; flags let you skip individual steps.

Options:
  --no-deepstream   Do not pkill deepstream-app.
  --no-amc          Do not run 'docker compose down' for AMC.
  --no-mosquitto    Do not stop the mosquitto service.
  -h, --help        Show this help and exit.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-deepstream) STOP_DS=0 ;;
    --no-amc) STOP_AMC=0 ;;
    --no-mosquitto) STOP_MQTT=0 ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "Unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

load_env || true

: "${AMC_ROOT:=$HOME/auto-magic-calib}"

#
# 1. deepstream-app
#
if [[ "$STOP_DS" -eq 1 ]]; then
  if pgrep -x deepstream-app >/dev/null 2>&1; then
    log_info "Stopping deepstream-app (SIGTERM)"
    pkill -TERM -x deepstream-app || true
    for _ in 1 2 3 4 5; do
      pgrep -x deepstream-app >/dev/null 2>&1 || break
      sleep 1
    done
    if pgrep -x deepstream-app >/dev/null 2>&1; then
      log_warn "deepstream-app still running after SIGTERM; sending SIGKILL"
      pkill -KILL -x deepstream-app || true
    fi
    log_info "deepstream-app stopped."
  else
    log_info "deepstream-app not running."
  fi
fi

#
# 2. AMC docker compose down
#
if [[ "$STOP_AMC" -eq 1 ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    log_warn "docker not found; skipping AMC shutdown."
  elif [[ ! -d "$AMC_ROOT" ]]; then
    log_info "AMC clone absent ($AMC_ROOT); nothing to stop."
  else
    DC=(docker compose)
    if ! docker compose version >/dev/null 2>&1; then
      if command -v docker-compose >/dev/null 2>&1; then
        DC=(docker-compose)
      else
        log_warn "Neither 'docker compose' nor 'docker-compose' available; skipping AMC shutdown."
        DC=()
      fi
    fi

    if [[ "${#DC[@]}" -gt 0 ]]; then
      COMPOSE_DIR="$AMC_ROOT/compose"
      if [[ ! -f "$COMPOSE_DIR/compose.yaml" && ! -f "$COMPOSE_DIR/docker-compose.yml" && ! -f "$COMPOSE_DIR/compose.yml" ]]; then
        if [[ -f "$AMC_ROOT/compose.yaml" || -f "$AMC_ROOT/docker-compose.yml" || -f "$AMC_ROOT/compose.yml" ]]; then
          COMPOSE_DIR="$AMC_ROOT"
        fi
      fi

      if [[ -d "$COMPOSE_DIR" ]]; then
        log_info "docker compose down (in $COMPOSE_DIR)"
        ( cd "$COMPOSE_DIR" && "${DC[@]}" down ) || \
          log_warn "'docker compose down' exited non-zero; check 'docker ps' manually."
      else
        log_warn "Cannot locate AMC compose dir under $AMC_ROOT; skipping."
      fi
    fi
  fi
fi

#
# 3. mosquitto
#
if [[ "$STOP_MQTT" -eq 1 ]]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    log_warn "systemctl not found; skipping mosquitto shutdown."
  elif ! systemctl is-active --quiet mosquitto; then
    log_info "mosquitto not active."
  else
    log_info "Stopping mosquitto (systemctl stop mosquitto)"
    if [[ "$(id -u)" -eq 0 ]]; then
      systemctl stop mosquitto || log_warn "'systemctl stop mosquitto' returned non-zero."
    else
      sudo systemctl stop mosquitto || log_warn "'sudo systemctl stop mosquitto' returned non-zero."
    fi
  fi
fi

log_info "All requested components stopped."
