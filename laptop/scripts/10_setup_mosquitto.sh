#!/usr/bin/env bash
# laptop/scripts/10_setup_mosquitto.sh
#
# Notion page 337b5d58-7212-81e1-b07a-d510d9605bbb §6 — Mosquitto install &
# drop-in configuration for the DS 9.0 laptop scripted testing harness.
#
# Responsibilities (only this, matching the plan):
#   1. Verify mosquitto was already installed by 00_bootstrap.sh.
#   2. Install laptop/mosquitto/mv3dt.conf into /etc/mosquitto/conf.d/mv3dt.conf
#      (atomic replace; idempotent).
#   3. Enable + restart the mosquitto service.
#   4. (Optional, --with-firewall) open ufw 1883/9001 per Notion §6.3 for
#      operators who have ufw enabled. Default posture skips ufw to match the
#      "simple testing" harness.
#
# This script never installs mosquitto itself (00_bootstrap.sh does that), and
# never reaches outside the laptop/ subtree except to write under /etc/ and
# call `systemctl` / `ufw`.
#
# References (via .cursor/skills/deepstream-9-docs/):
#   - DS 9.0 IoT / Edge-to-Cloud Messaging:
#     https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_IoT.html
#   - Gst-nvmsgbroker (MQTT proto lib):
#     https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvmsgbroker.html

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

WITH_FIREWALL=0

usage() {
  cat <<'EOF'
Usage: 10_setup_mosquitto.sh [--with-firewall] [-h|--help]

Installs the laptop/mosquitto/mv3dt.conf drop-in into /etc/mosquitto/conf.d/
and restarts the mosquitto service. Must be run with sudo.

Options:
  --with-firewall   Also open 1883/tcp and 9001/tcp via ufw (Notion §6.3).
                    Skipped by default because the simple-testing posture
                    does not assume ufw is active.
  -h, --help        Show this help and exit.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-firewall) WITH_FIREWALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "Unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

require_root
require_tool systemctl
require_tool install
require_tool mosquitto

SRC_CONF="$(repo_root)/laptop/mosquitto/mv3dt.conf"
DST_DIR="/etc/mosquitto/conf.d"
DST_CONF="$DST_DIR/mv3dt.conf"

if [[ ! -f "$SRC_CONF" ]]; then
  die "Missing source config: $SRC_CONF (expected in this repo)."
fi

log_info "Installing $SRC_CONF -> $DST_CONF"
install -d -m 0755 "$DST_DIR"

TMP_CONF="$(mktemp "${DST_CONF}.tmp.XXXXXX")"
cp "$SRC_CONF" "$TMP_CONF"
chmod 0644 "$TMP_CONF"
chown root:root "$TMP_CONF"
mv -f "$TMP_CONF" "$DST_CONF"

log_info "Enabling mosquitto service"
systemctl enable mosquitto >/dev/null

log_info "Restarting mosquitto service"
systemctl restart mosquitto

sleep 1
if systemctl is-active --quiet mosquitto; then
  log_info "mosquitto is active."
else
  systemctl status --no-pager mosquitto || true
  die "mosquitto failed to start after config install. See 'journalctl -u mosquitto' for details."
fi

if [[ "$WITH_FIREWALL" -eq 1 ]]; then
  if command -v ufw >/dev/null 2>&1; then
    log_info "Opening ufw 1883/tcp and 9001/tcp (Notion §6.3)"
    ufw allow 1883/tcp || true
    ufw allow 9001/tcp || true
  else
    log_warn "--with-firewall requested but 'ufw' is not installed; skipping."
  fi
fi

cat <<EOF

Next step:
  Verify with:
    mosquitto_sub -h 127.0.0.1 -t 'mv3dt/#' -v
  In a second shell you can publish a test message:
    mosquitto_pub -h 127.0.0.1 -t 'mv3dt/test' -m 'hello'

Then continue with:
  laptop/scripts/20_verify_cameras.sh
EOF
