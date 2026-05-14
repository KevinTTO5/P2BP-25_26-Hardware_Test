#!/usr/bin/env bash
# laptop/scripts/record_cameras_mp4.sh
#
# Records a short MP4 clip from each enabled camera in laptop/config/cameras.yml
# (parallel ffmpeg processes). Reads CAM_USER / CAM_PASSWORD from
# laptop/config/laptop.env.
#
# Exit codes:
#   0 - every enabled camera produced a file.
#   1 - one or more recordings failed.
#   2 - invalid arguments / missing config.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

DEFAULT_SECONDS=180

usage() {
  cat <<EOF
Usage: record_cameras_mp4.sh [--seconds N] [--out-dir DIR] [-h|--help]

Record MP4 clips from all enabled cameras in laptop/config/cameras.yml (RTSP/TCP).

Options:
  --seconds N   Duration per camera in seconds (default: ${DEFAULT_SECONDS}).
  --out-dir DIR Output directory (default: laptop/recordings/<timestamp>/).
  -h, --help    Show this help and exit.

Environment:
  CAM_USER, CAM_PASSWORD — required; from laptop/config/laptop.env.
EOF
}

SECONDS_ARG=""
OUT_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --seconds)
      [[ -n "${2:-}" ]] || { log_error "--seconds needs a value"; usage; exit 2; }
      SECONDS_ARG="$2"
      shift 2
      ;;
    --out-dir)
      [[ -n "${2:-}" ]] || { log_error "--out-dir needs a value"; usage; exit 2; }
      OUT_DIR="$2"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "Unknown argument: $1"; usage; exit 2 ;;
  esac
done

DURATION="${SECONDS_ARG:-${RECORD_SECONDS:-$DEFAULT_SECONDS}}"
if ! [[ "$DURATION" =~ ^[0-9]+$ ]] || [[ "$DURATION" -lt 1 ]]; then
  die "Duration must be a positive integer (got: $DURATION)"
fi

require_tool ffmpeg
require_tool python3

load_env

CAMERAS_YML="$(repo_root)/laptop/config/cameras.yml"
if [[ ! -f "$CAMERAS_YML" ]]; then
  die "Missing $CAMERAS_YML"
fi
if [[ -z "${CAM_USER:-}" ]]; then
  die "CAM_USER is empty. Set it in laptop/config/laptop.env."
fi
if [[ -z "${CAM_PASSWORD:-}" ]]; then
  die "CAM_PASSWORD is empty. Set it in laptop/config/laptop.env."
fi

if [[ -z "$OUT_DIR" ]]; then
  ts="$(date +%Y%m%d_%H%M%S)"
  OUT_DIR="$(repo_root)/laptop/recordings/${ts}"
fi
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

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

log_info "Writing ${DURATION}s MP4 clips under: $OUT_DIR"

declare -a pids=()

while IFS=$'\t' read -r cid ip position rtsp_path enabled; do
  [[ -z "$cid" ]] && continue
  if [[ "$enabled" != "1" ]]; then
    log_info "skip  ${cid} (${ip}) — disabled in cameras.yml"
    continue
  fi

  rtsp_url="rtsp://${CAM_USER}:${CAM_PASSWORD}@${ip}:554${rtsp_path}"
  out="${OUT_DIR}/${cid}.mp4"
  log_info "start ${cid} -> $(basename "$out")"

  (
    if ffmpeg -hide_banner -loglevel error -nostats -y \
      -rtsp_transport tcp \
      -i "$rtsp_url" \
      -t "$DURATION" \
      -map 0:v:0 -c:v copy \
      -an \
      -movflags +faststart \
      "$out" </dev/null
    then
      log_info "done  ${cid}: $out"
    else
      log_error "FAIL ${cid}: ffmpeg exited non-zero ($out)"
      rm -f "$out"
      exit 1
    fi
  ) &

  pid=$!
  pids+=("$pid")
done < <(parse_cameras)

if [[ ${#pids[@]:-0} -eq 0 ]]; then
  die "No enabled cameras found in $CAMERAS_YML"
fi

fail=0
for pid in "${pids[@]}"; do
  if ! wait "$pid"; then
    fail=1
  fi
done

if [[ "$fail" -ne 0 ]]; then
  log_error "One or more recordings failed."
  exit 1
fi

log_info "All recordings finished successfully."
exit 0
