#!/usr/bin/env bash
# laptop/scripts/view_cameras.sh
#
# Opens a live tiled grid view of all enabled cameras from
# laptop/config/cameras.yml — like a security monitor wall.
#
# Each camera gets its own ffplay window, arranged in a grid. Window titles
# show camera ID and position label (e.g. "c1 — top-right").
#
# Dependencies: ffplay (part of ffmpeg, same as record_cameras_mp4.sh).
#
# Usage:
#   bash laptop/scripts/view_cameras.sh [--width W] [--height H] [--cols N]
#                                        [--latency low|normal] [-h|--help]
#
# Options:
#   --width W       Tile width in pixels (default: 640)
#   --height H      Tile height in pixels (default: 360)
#   --cols N        Number of columns in the grid (default: auto from camera count)
#   --latency low   Minimise buffering for near-realtime view (default: low)
#             normal  Use normal buffering (smoother but ~2s behind)
#   -h, --help      Show this help and exit.
#
# Credentials: CAM_USER / CAM_PASSWORD from laptop/config/laptop.env.
# Close any one ffplay window or press Ctrl-C to kill all streams.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

TILE_W=640
TILE_H=360
COLS=""
LATENCY="low"

usage() {
  cat <<'EOF'
Usage: view_cameras.sh [--width W] [--height H] [--cols N]
                       [--latency low|normal] [-h|--help]

Opens a tiled live view of all enabled cameras (ffplay, one window per camera).

Options:
  --width W       Tile width in pixels (default: 640)
  --height H      Tile height in pixels (default: 360)
  --cols N        Grid columns (default: auto)
  --latency low|normal
                  low    = minimal buffering, near-realtime (default)
                  normal = smoother playback, ~2s latency
  -h, --help      Show this help and exit.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --width)
      [[ -n "${2:-}" ]] || { log_error "--width needs a value"; usage; exit 2; }
      TILE_W="$2"; shift 2 ;;
    --height)
      [[ -n "${2:-}" ]] || { log_error "--height needs a value"; usage; exit 2; }
      TILE_H="$2"; shift 2 ;;
    --cols)
      [[ -n "${2:-}" ]] || { log_error "--cols needs a value"; usage; exit 2; }
      COLS="$2"; shift 2 ;;
    --latency)
      [[ -n "${2:-}" ]] || { log_error "--latency needs a value"; usage; exit 2; }
      LATENCY="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "Unknown argument: $1"; usage; exit 2 ;;
  esac
done

require_tool ffplay
require_tool python3
load_env

: "${CAM_USER:?CAM_USER is required in laptop/config/laptop.env}"
: "${CAM_PASSWORD:?CAM_PASSWORD is required in laptop/config/laptop.env}"

REPO_ROOT="$(repo_root)"
CAMERAS_YML="$REPO_ROOT/laptop/config/cameras.yml"

if [[ ! -f "$CAMERAS_YML" ]]; then
  die "Missing $CAMERAS_YML"
fi

# Parse enabled cameras into tab-separated id/ip/rtsp_path/position lines.
mapfile -t CAM_ROWS < <(
  python3 - "$CAMERAS_YML" <<'PY'
import re, sys
path = sys.argv[1]
rows, cur, in_cameras = [], None, False
with open(path, "r", encoding="utf-8") as f:
    for raw in f:
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if re.match(r"^cameras:\s*$", line):
            in_cameras = True; continue
        if not in_cameras:
            continue
        m_item = re.match(r"^\s*-\s+(.*)$", line)
        m_kv   = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if m_item:
            if cur: rows.append(cur)
            cur = {}
            rest = m_item.group(1)
            km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", rest)
            if km:
                cur[km.group(1)] = km.group(2).strip().strip('"').strip("'")
        elif m_kv and cur is not None:
            cur[m_kv.group(1)] = m_kv.group(2).strip().strip('"').strip("'")
if cur:
    rows.append(cur)
enabled = [r for r in rows
           if str(r.get("enabled", "true")).lower() not in ("false", "0", "no")]
for r in enabled:
    print("\t".join([
        r.get("id",        ""),
        r.get("ip",        ""),
        r.get("rtsp_path", "/Streaming/Channels/101"),
        r.get("position",  ""),
    ]))
PY
)

if [[ ${#CAM_ROWS[@]} -eq 0 ]]; then
  die "No enabled cameras found in $CAMERAS_YML"
fi

COUNT=${#CAM_ROWS[@]}
log_info "Found $COUNT enabled camera(s)"

# Auto-compute grid columns if not set: sqrt rounded up, max 4.
if [[ -z "$COLS" ]]; then
  COLS=$(python3 -c "import math; n=$COUNT; print(min(4, math.ceil(math.sqrt(n))))")
fi

# Build ffplay flags for latency mode.
if [[ "$LATENCY" == "low" ]]; then
  # fflags nobuffer + small probesize/analyzeduration = ~200ms latency.
  LATENCY_FLAGS=(-fflags nobuffer -flags low_delay -probesize 32 -analyzeduration 0)
else
  LATENCY_FLAGS=()
fi

# Trap: kill all spawned ffplay pids on Ctrl-C or script exit.
PIDS=()
cleanup() {
  log_info "Closing all streams..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

col=0
row=0

for cam_row in "${CAM_ROWS[@]}"; do
  IFS=$'\t' read -r cam_id ip rtsp_path position <<< "$cam_row"

  rtsp_url="rtsp://${CAM_USER}:${CAM_PASSWORD}@${ip}:554${rtsp_path}"
  win_title="${cam_id} — ${position}"
  x_pos=$(( col * TILE_W ))
  y_pos=$(( row * TILE_H ))

  log_info "Opening ${cam_id} (${position})  →  ${ip}"

  ffplay \
    "${LATENCY_FLAGS[@]}" \
    -rtsp_transport tcp \
    -i "$rtsp_url" \
    -vf "scale=${TILE_W}:${TILE_H}" \
    -an \
    -volume 0 \
    -window_title "$win_title" \
    -left "$x_pos" \
    -top  "$y_pos" \
    -x "$TILE_W" \
    -y "$TILE_H" \
    -loglevel error \
    2>/dev/null &

  PIDS+=($!)

  col=$(( col + 1 ))
  if [[ "$col" -ge "$COLS" ]]; then
    col=0
    row=$(( row + 1 ))
  fi
done

log_info "All $COUNT streams open. Press Ctrl-C to close all."

# Wait until any ffplay exits, then clean up the rest.
wait -n 2>/dev/null || wait
