#!/usr/bin/env bash
# laptop/scripts/20_verify_cameras.sh
#
# Notion page 337b5d58-7212-81e1-b07a-d510d9605bbb §7.5 — RTSP verification for
# cameras C1..C8. Iterates laptop/config/cameras.yml, pings each camera, then
# runs ffprobe over RTSP/TCP, and prints a pass/fail table matching the loop
# in the Notion section.
#
# Sections §7.1-7.4 (per-camera IP assignment and stream profile via the camera
# web UI) are manual steps documented in laptop/docs/DEEPSTREAM-SETUP.md. This
# script only verifies the resulting RTSP streams.
#
# Exit codes:
#   0 - all enabled cameras passed.
#   1 - one or more enabled cameras failed (unless --allow-partial is set).
#   2 - invalid arguments / preconditions.
#
# This script reads only laptop/config/laptop.env and laptop/config/cameras.yml
# (plan isolation rule). No root required.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

ALLOW_PARTIAL=0
PING_TIMEOUT="${CAMERA_PING_TIMEOUT:-2}"
FFPROBE_TIMEOUT_US="${CAMERA_FFPROBE_TIMEOUT_US:-5000000}"

usage() {
  cat <<'EOF'
Usage: 20_verify_cameras.sh [--allow-partial] [-h|--help]

Ping + ffprobe each enabled camera in laptop/config/cameras.yml and print a
pass/fail table (Notion §7.5).

Options:
  --allow-partial   Exit 0 even if some enabled cameras fail (still prints
                    the table). Handy for partial lab setups.
  -h, --help        Show this help and exit.

Environment overrides:
  CAMERA_PING_TIMEOUT        ping -W value in seconds (default: 2).
  CAMERA_FFPROBE_TIMEOUT_US  ffprobe -timeout value in microseconds
                             (default: 5000000, i.e. 5s).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-partial) ALLOW_PARTIAL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "Unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

require_tool ping
require_tool ffprobe
require_tool python3

load_env

CAMERAS_YML="$(repo_root)/laptop/config/cameras.yml"
if [[ ! -f "$CAMERAS_YML" ]]; then
  die "Missing $CAMERAS_YML"
fi

if [[ -z "${CAM_USER:-}" ]]; then
  die "CAM_USER is empty. Run 00_bootstrap.sh or set it in laptop/config/laptop.env."
fi
if [[ -z "${CAM_PASSWORD:-}" ]]; then
  die "CAM_PASSWORD is empty. Run 00_bootstrap.sh or set it in laptop/config/laptop.env."
fi

parse_cameras() {
  python3 - "$CAMERAS_YML" <<'PY'
import sys, re
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    lines = [l.rstrip("\n") for l in f]

rows = []
cur = None
in_cameras = False
for raw in lines:
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
            k, v = km.group(1), km.group(2).strip()
            cur[k] = v.strip('"').strip("'")
    elif m_kv and cur is not None:
        k, v = m_kv.group(1), m_kv.group(2).strip()
        cur[k] = v.strip('"').strip("'")
if cur:
    rows.append(cur)

for r in rows:
    enabled = str(r.get("enabled", "true")).lower() not in ("false", "0", "no")
    print("\t".join([
        r.get("id", ""),
        r.get("ip", ""),
        r.get("position", ""),
        r.get("rtsp_path", "/stream1"),
        "1" if enabled else "0",
    ]))
PY
}

TOTAL=0
PASS=0
FAIL=0
declare -a ROWS=()

while IFS=$'\t' read -r cid ip position rtsp_path enabled; do
  [[ -z "$cid" ]] && continue
  TOTAL=$((TOTAL + 1))
  if [[ "$enabled" != "1" ]]; then
    ROWS+=("$cid|$ip|$position|SKIP|disabled in cameras.yml")
    continue
  fi

  ping_status="FAIL"
  probe_status="FAIL"
  note=""

  if ping -c 1 -W "$PING_TIMEOUT" "$ip" >/dev/null 2>&1; then
    ping_status="OK"
  else
    note="ping failed"
  fi

  if [[ "$ping_status" == "OK" ]]; then
    rtsp_url="rtsp://${CAM_USER}:${CAM_PASSWORD}@${ip}:554${rtsp_path}"
    if ffprobe -hide_banner -loglevel error \
        -rtsp_transport tcp \
        -timeout "$FFPROBE_TIMEOUT_US" \
        -i "$rtsp_url" \
        -show_entries stream=codec_name,width,height \
        -of default=noprint_wrappers=1 >/dev/null 2>&1; then
      probe_status="OK"
    else
      note="ffprobe failed (auth/path/rtsp)"
    fi
  fi

  overall="FAIL"
  if [[ "$ping_status" == "OK" && "$probe_status" == "OK" ]]; then
    overall="PASS"
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
  fi

  ROWS+=("$cid|$ip|$position|$overall|ping=$ping_status probe=$probe_status${note:+ ; $note}")
done < <(parse_cameras)

echo
printf '%-4s %-16s %-22s %-6s %s\n' "ID" "IP" "POSITION" "STATE" "DETAIL"
printf '%-4s %-16s %-22s %-6s %s\n' "----" "----------------" "----------------------" "------" "------"
for r in "${ROWS[@]}"; do
  IFS='|' read -r cid ip position state detail <<<"$r"
  printf '%-4s %-16s %-22s %-6s %s\n' "$cid" "$ip" "$position" "$state" "$detail"
done
echo
log_info "Enabled cameras: pass=$PASS fail=$FAIL total=$TOTAL"

if [[ "$FAIL" -gt 0 && "$ALLOW_PARTIAL" -ne 1 ]]; then
  die "One or more cameras failed verification. Re-run with --allow-partial to override, or check network/credentials."
fi

exit 0
