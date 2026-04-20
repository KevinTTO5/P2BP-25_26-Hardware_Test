#!/usr/bin/env bash
# laptop/scripts/50_start_pipeline.sh
#
# Notion page 337b5d58-7212-81e1-b07a-d510d9605bbb §10 — startup sequence and
# monitoring for the DS 9.0 MV3DT laptop pipeline.
#
# Responsibilities (Notion §10.1):
#   1. Ensure the Mosquitto broker is up (idempotent: `systemctl start`).
#   2. Ping-sweep C1..C8 from laptop/config/cameras.yml as a quick
#      network-sanity check; warn on misses but do not block (operators can
#      explicitly gate on 20_verify_cameras.sh).
#   3. Source /etc/profile.d/deepstream.sh so `deepstream-app` and
#      DEEPSTREAM_DIR are on PATH / in env.
#   4. cd into laptop/deepstream/ and exec `deepstream-app -c <config>`.
#      By default the rendered config written by 40_export_watcher.sh
#      (deepstream_app_config.rendered.txt) is used; falls back to the
#      committed template if the rendered copy is missing and --force is
#      passed, since the template still contains ${CAM_USER}/${CAM_PASSWORD}
#      placeholders and is not directly runnable without render.
#
# On first line of pipeline output the script prints the Notion §10.2
# validation helpers (mosquitto_sub + nvidia-smi watch) so the operator can
# confirm tracks are flowing from a second tty.
#
# References (via .cursor/skills/deepstream-9-docs/):
#   - deepstream-app CLI:
#     https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_ref_app_deepstream.html
#   - Gst-nvmsgbroker (MQTT publish sink):
#     https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvmsgbroker.html
#   - MV3DT 9.0:
#     https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_MV3DT.html

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

FORCE_TEMPLATE=0
DRY_RUN=0
CONFIG_OVERRIDE=""
SKIP_PING=0
PING_TIMEOUT="${CAMERA_PING_TIMEOUT:-2}"

usage() {
  cat <<'EOF'
Usage: 50_start_pipeline.sh [--config <path>] [--force-template]
                            [--skip-ping] [--dry-run] [-h|--help]

Runs the DS 9.0 MV3DT pipeline for the laptop testing harness (Notion §10.1).
Prints the §10.2 validation commands before handing off to deepstream-app.

Options:
  --config <path>   Use an explicit deepstream-app config (absolute or
                    relative to laptop/deepstream/). Overrides auto-detection.
  --force-template  Run the committed laptop/deepstream/deepstream_app_config.txt
                    even if the rendered copy is missing. The template still
                    contains ${CAM_USER}/${CAM_PASSWORD} placeholders, so this
                    is only useful for syntax/structure smoke tests.
  --skip-ping       Skip the C1..C8 ping-sweep sanity check.
  --dry-run         Print the final command and exit without launching
                    deepstream-app.
  -h, --help        Show this help and exit.

Uses laptop/config/laptop.env (LOCATION_ID, CAM_USER, CAM_PASSWORD required).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || { log_error "--config requires a path"; usage; exit 2; }
      CONFIG_OVERRIDE="$2"; shift 2; continue
      ;;
    --force-template) FORCE_TEMPLATE=1 ;;
    --skip-ping) SKIP_PING=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "Unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

load_env

: "${LOCATION_ID:?LOCATION_ID is required in laptop/config/laptop.env}"
: "${CAM_USER:?CAM_USER is required in laptop/config/laptop.env}"
: "${CAM_PASSWORD:?CAM_PASSWORD is required in laptop/config/laptop.env}"
: "${MQTT_TOPIC_BASE:=mv3dt}"

REPO_ROOT="$(repo_root)"
DS_DIR="$REPO_ROOT/laptop/deepstream"
TEMPLATE="$DS_DIR/deepstream_app_config.txt"
RENDERED="$DS_DIR/deepstream_app_config.rendered.txt"

if [[ ! -d "$DS_DIR" ]]; then
  die "Missing $DS_DIR. This script expects to run from a fresh clone of the repo."
fi

#
# 1. Mosquitto: idempotent start (Notion §10.1).
#
ensure_mosquitto() {
  if ! command -v systemctl >/dev/null 2>&1; then
    log_warn "systemctl not found; cannot verify mosquitto. Proceeding anyway."
    return 0
  fi
  if systemctl is-active --quiet mosquitto; then
    log_info "mosquitto is already active."
    return 0
  fi
  log_info "Starting mosquitto (systemctl start mosquitto)"
  if [[ "$(id -u)" -eq 0 ]]; then
    systemctl start mosquitto
  else
    sudo systemctl start mosquitto
  fi
  sleep 1
  if ! systemctl is-active --quiet mosquitto; then
    die "mosquitto failed to start. Run 'journalctl -u mosquitto' and re-run 10_setup_mosquitto.sh if needed."
  fi
}
ensure_mosquitto

#
# 2. Ping-sweep C1..C8 (Notion §10.1 sanity check).
#
if [[ "$SKIP_PING" -ne 1 ]]; then
  CAMERAS_YML="$REPO_ROOT/laptop/config/cameras.yml"
  if [[ -f "$CAMERAS_YML" ]] && command -v python3 >/dev/null 2>&1 && command -v ping >/dev/null 2>&1; then
    log_info "Ping-sweep C1..C8 (Notion §10.1)"
    miss=0
    while IFS=$'\t' read -r cid ip enabled; do
      [[ -z "$cid" ]] && continue
      if [[ "$enabled" != "1" ]]; then
        printf '  %-4s %-16s SKIP (disabled in cameras.yml)\n' "$cid" "$ip"
        continue
      fi
      if ping -c 1 -W "$PING_TIMEOUT" "$ip" >/dev/null 2>&1; then
        printf '  %-4s %-16s OK\n' "$cid" "$ip"
      else
        printf '  %-4s %-16s MISS\n' "$cid" "$ip"
        miss=$((miss + 1))
      fi
    done < <(
      python3 - "$CAMERAS_YML" <<'PY'
import re, sys
path = sys.argv[1]
rows = []
cur = None
in_cameras = False
with open(path, "r", encoding="utf-8") as f:
    for raw in f:
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if re.match(r"^cameras:\s*$", line):
            in_cameras = True
            continue
        if not in_cameras:
            continue
        m_item = re.match(r"^\s*-\s+(.*)$", line)
        m_kv = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if m_item:
            if cur:
                rows.append(cur)
            cur = {}
            rest = m_item.group(1)
            km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", rest)
            if km:
                cur[km.group(1)] = km.group(2).strip().strip('"').strip("'")
        elif m_kv and cur is not None:
            cur[m_kv.group(1)] = m_kv.group(2).strip().strip('"').strip("'")
if cur:
    rows.append(cur)
for r in rows:
    enabled = str(r.get("enabled", "true")).lower() not in ("false", "0", "no")
    print("\t".join([r.get("id", ""), r.get("ip", ""), "1" if enabled else "0"]))
PY
    )
    if [[ "$miss" -gt 0 ]]; then
      log_warn "$miss enabled camera(s) did not respond to ping. Continuing; run 20_verify_cameras.sh for details."
    fi
  else
    log_warn "Skipping ping-sweep (missing cameras.yml, python3, or ping)."
  fi
fi

#
# 3. Source deepstream env (Notion §5.2).
#
DS_PROFILE="/etc/profile.d/deepstream.sh"
if [[ -f "$DS_PROFILE" ]]; then
  log_info "Sourcing $DS_PROFILE"
  # shellcheck disable=SC1090
  set +u; . "$DS_PROFILE"; set -u
else
  log_warn "$DS_PROFILE not found. Re-run 00_bootstrap.sh if deepstream-app is not on PATH."
fi

require_tool deepstream-app

#
# 4. Resolve config file.
#
if [[ -n "$CONFIG_OVERRIDE" ]]; then
  CONFIG="$CONFIG_OVERRIDE"
  case "$CONFIG" in
    /*) : ;;                       # absolute path as-is
    *)  CONFIG="$DS_DIR/$CONFIG" ;; # resolve relative to laptop/deepstream/
  esac
elif [[ -f "$RENDERED" ]]; then
  CONFIG="$RENDERED"
  log_info "Using rendered config: $CONFIG"
elif [[ "$FORCE_TEMPLATE" -eq 1 && -f "$TEMPLATE" ]]; then
  CONFIG="$TEMPLATE"
  log_warn "No rendered config found; --force-template in effect. Template contains placeholder credentials."
else
  cat >&2 <<EOF
error: no rendered pipeline config found.

  expected: $RENDERED

Run the AMC export watcher first (§8.7 -> render):
  laptop/scripts/40_export_watcher.sh --oneshot

Or, to smoke-test the committed template despite unresolved
\${CAM_USER}/\${CAM_PASSWORD}/\${LOCATION_ID} placeholders:
  laptop/scripts/50_start_pipeline.sh --force-template
EOF
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  die "Config file not found: $CONFIG"
fi

cat <<EOF

[validation helpers — Notion §10.2]
Run these in a second tty while the pipeline is active:

  # MV3DT/SV3DT tracks (topic base from laptop/config/laptop.env):
  mosquitto_sub -h 127.0.0.1 -t '${MQTT_TOPIC_BASE}/#' -v

  # GPU utilization / memory / temperature:
  watch -n 1 'nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu --format=csv'

  # Broker health (laptop/mosquitto/mv3dt.conf listens on 1883 + 9001):
  systemctl status mosquitto --no-pager

EOF

log_info "Launching: deepstream-app -c $(basename "$CONFIG") (cwd: $DS_DIR)"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "cd $DS_DIR && deepstream-app -c $CONFIG"
  exit 0
fi

cd "$DS_DIR"
exec deepstream-app -c "$CONFIG"
