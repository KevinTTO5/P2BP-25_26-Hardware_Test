#!/usr/bin/env bash
# installer/mv3dt_installer/assets/scripts/60_record_tracking.sh
#
# Subscribe to MV3DT MQTT topics and persist sponsor/demo-ready local exports:
#   - tracks.jsonl  : raw topic + payload envelope (one JSON object per line)
#   - tracks.csv    : flattened best-effort XY records for quick plotting
#   - summary.json  : run metadata + counts
#
# Output root:
#   ${MV3DT_TRACKING_EXPORTS:-<install_dir>/tracking_exports}/<YYYYMMDD_HHMMSS>_<location_id>/
#
# Ported from laptop/scripts/60_record_tracking.sh (installer/plan/
# DELETION-REVIEW.md §6): the only changes are the installer.conf loader and
# the output root default, which now points under the resolved install_dir
# instead of a repo checkout. The embedded Python tracks.jsonl/tracks.csv/
# summary.json producer is unchanged; it takes every input as argv.
#
# Designed to run in a second terminal while the pipeline step is active.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

RUN_NAME=""
OUTPUT_ROOT_OVERRIDE=""
DURATION_SEC=0
TOPIC_FILTER=""
QUIET=0

usage() {
  cat <<'EOF'
Usage: 60_record_tracking.sh [--duration SEC] [--run-name NAME]
                             [--output-root DIR] [--topic TOPIC]
                             [--quiet] [-h|--help]

Subscribes to MQTT track topics and writes local exports under:
  ${MV3DT_TRACKING_EXPORTS:-<install_dir>/tracking_exports}/<run_id>/

Options:
  --duration SEC    Stop automatically after SEC seconds (default: run until Ctrl-C).
  --run-name NAME   Custom run suffix; default is LOCATION_ID from installer.conf.
  --output-root DIR Override output root directory.
  --topic TOPIC     MQTT topic filter (default: ${MQTT_TOPIC_BASE}/#).
  --quiet           Suppress per-message progress logs.
  -h, --help        Show this help and exit.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration)
      [[ -n "${2:-}" ]] || { log_error "--duration requires a value"; usage; exit 2; }
      DURATION_SEC="$2"; shift 2 ;;
    --run-name)
      [[ -n "${2:-}" ]] || { log_error "--run-name requires a value"; usage; exit 2; }
      RUN_NAME="$2"; shift 2 ;;
    --output-root)
      [[ -n "${2:-}" ]] || { log_error "--output-root requires a value"; usage; exit 2; }
      OUTPUT_ROOT_OVERRIDE="$2"; shift 2 ;;
    --topic)
      [[ -n "${2:-}" ]] || { log_error "--topic requires a value"; usage; exit 2; }
      TOPIC_FILTER="$2"; shift 2 ;;
    --quiet)
      QUIET=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      log_error "Unknown argument: $1"; usage; exit 2 ;;
  esac
done

require_tool python3
require_tool mosquitto_sub
load_installer_conf

: "${LOCATION_ID:=site}"
: "${MQTT_HOST:=127.0.0.1}"
: "${MQTT_PORT:=1883}"
: "${MQTT_TOPIC_BASE:=mv3dt}"

if [[ -z "$TOPIC_FILTER" ]]; then
  TOPIC_FILTER="${MQTT_TOPIC_BASE}/#"
fi

if [[ -z "$RUN_NAME" ]]; then
  RUN_NAME="$LOCATION_ID"
fi

if [[ -n "$OUTPUT_ROOT_OVERRIDE" ]]; then
  OUTPUT_ROOT="$OUTPUT_ROOT_OVERRIDE"
else
  OUTPUT_ROOT="${MV3DT_TRACKING_EXPORTS:-${MV3DT_INSTALL_DIR:?}/tracking_exports}"
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
RUN_ID="${TIMESTAMP}_${RUN_NAME}"
RUN_DIR="${OUTPUT_ROOT}/${RUN_ID}"
mkdir -p "$RUN_DIR"

JSONL_PATH="$RUN_DIR/tracks.jsonl"
CSV_PATH="$RUN_DIR/tracks.csv"
SUMMARY_PATH="$RUN_DIR/summary.json"

log_info "Recording MQTT topic '$TOPIC_FILTER' from ${MQTT_HOST}:${MQTT_PORT}"
log_info "Run directory: $RUN_DIR"
if [[ "$DURATION_SEC" -gt 0 ]]; then
  log_info "Auto-stop after ${DURATION_SEC}s"
fi

PY_ARGS=(
  "$JSONL_PATH"
  "$CSV_PATH"
  "$SUMMARY_PATH"
  "$MQTT_HOST"
  "$MQTT_PORT"
  "$TOPIC_FILTER"
  "$LOCATION_ID"
  "$RUN_ID"
  "$DURATION_SEC"
  "$QUIET"
)

python3 - "${PY_ARGS[@]}" <<'PY'
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

(
    jsonl_path,
    csv_path,
    summary_path,
    mqtt_host,
    mqtt_port,
    topic_filter,
    location_id,
    run_id,
    duration_sec_raw,
    quiet_raw,
) = sys.argv[1:]

duration_sec = int(duration_sec_raw)
quiet = quiet_raw == "1"

stop_requested = False

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def sanitize_number(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None

def deep_get(root, path):
    cur = root
    for part in path:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur

def extract_xy(payload):
    direct_candidates = [
        ("x", "y"),
        ("posX", "posY"),
        ("worldX", "worldY"),
        ("coord_x", "coord_y"),
    ]
    for kx, ky in direct_candidates:
        x = sanitize_number(payload.get(kx)) if isinstance(payload, dict) else None
        y = sanitize_number(payload.get(ky)) if isinstance(payload, dict) else None
        if x is not None and y is not None:
            return [{"x": x, "y": y, "entity_id": payload.get("id") or payload.get("trackingId")}]

    object_paths = [
        ("objects",),
        ("object",),
        ("payload", "objects"),
        ("message", "objects"),
        ("event", "objects"),
        ("tracking", "objects"),
        ("event", "object"),
    ]
    xy_keys = [
        ("x", "y"),
        ("posX", "posY"),
        ("worldX", "worldY"),
        ("coord_x", "coord_y"),
    ]
    id_keys = ["id", "trackingId", "object_id", "objectId", "globalId", "track_id"]

    rows = []
    for path in object_paths:
        obj = deep_get(payload, path) if isinstance(payload, dict) else None
        if obj is None:
            continue
        seq = obj if isinstance(obj, list) else [obj]
        for item in seq:
            if not isinstance(item, dict):
                continue
            found = None
            for kx, ky in xy_keys:
                x = sanitize_number(item.get(kx))
                y = sanitize_number(item.get(ky))
                if x is not None and y is not None:
                    found = (x, y)
                    break
            if found is None:
                continue
            entity_id = None
            for key in id_keys:
                if key in item and item[key] not in (None, ""):
                    entity_id = item[key]
                    break
            rows.append({"x": found[0], "y": found[1], "entity_id": entity_id})
    return rows

def extract_event_timestamp(payload, ingest_ts_ms):
    if not isinstance(payload, dict):
        return ingest_ts_ms
    for key in ("time", "timestamp", "ts", "event_time", "eventTime"):
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            v = int(value)
            if v < 10_000_000_000:
                return v * 1000
            return v
        if isinstance(value, str) and value.isdigit():
            v = int(value)
            if v < 10_000_000_000:
                return v * 1000
            return v
    return ingest_ts_ms

def extract_topic_parts(topic):
    parts = topic.split("/")
    location = parts[1] if len(parts) >= 2 else ""
    stream = parts[2] if len(parts) >= 3 else ""
    return location, stream

def request_stop(*_args):
    global stop_requested
    stop_requested = True

signal.signal(signal.SIGINT, request_stop)
signal.signal(signal.SIGTERM, request_stop)

csv_header = [
    "ingest_iso",
    "ingest_ts_ms",
    "event_ts_ms",
    "topic",
    "location_id",
    "stream",
    "entity_id",
    "x",
    "y",
]

msg_count = 0
json_count = 0
csv_rows = 0
topic_counts = {}
first_ts = None
last_ts = None
start_iso = now_iso()
start_time = time.time()

os.makedirs(os.path.dirname(jsonl_path), exist_ok=True)
with open(jsonl_path, "w", encoding="utf-8") as jf, open(csv_path, "w", newline="", encoding="utf-8") as cf:
    writer = csv.DictWriter(cf, fieldnames=csv_header)
    writer.writeheader()

    cmd = [
        "mosquitto_sub",
        "-h",
        mqtt_host,
        "-p",
        str(mqtt_port),
        "-v",
        "-t",
        topic_filter,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    try:
        while not stop_requested:
            if duration_sec > 0 and (time.time() - start_time) >= duration_sec:
                break

            line = proc.stdout.readline()
            if line == "":
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            line = line.rstrip("\n")
            if not line.strip():
                continue

            msg_count += 1
            now_ms = int(time.time() * 1000)
            now_str = now_iso()

            split_idx = line.find(" ")
            if split_idx <= 0:
                topic = topic_filter
                payload_raw = line
            else:
                topic = line[:split_idx]
                payload_raw = line[split_idx + 1 :]

            location_part, stream_part = extract_topic_parts(topic)
            topic_counts[topic] = topic_counts.get(topic, 0) + 1

            payload_obj = None
            payload_error = ""
            try:
                payload_obj = json.loads(payload_raw)
                json_count += 1
            except Exception as exc:
                payload_error = str(exc)

            envelope = {
                "ingest_iso": now_str,
                "ingest_ts_ms": now_ms,
                "topic": topic,
                "topic_location_id": location_part,
                "topic_stream": stream_part,
                "payload_raw": payload_raw,
            }
            if payload_obj is not None:
                envelope["payload"] = payload_obj
            else:
                envelope["payload_parse_error"] = payload_error

            jf.write(json.dumps(envelope, separators=(",", ":")) + "\n")

            if first_ts is None:
                first_ts = now_ms
            last_ts = now_ms

            if payload_obj is None:
                if not quiet and msg_count % 50 == 0:
                    print(f"[record] messages={msg_count} csv_rows={csv_rows}", file=sys.stderr)
                continue

            event_ts_ms = extract_event_timestamp(payload_obj, now_ms)
            xy_rows = extract_xy(payload_obj)
            for row in xy_rows:
                writer.writerow(
                    {
                        "ingest_iso": now_str,
                        "ingest_ts_ms": now_ms,
                        "event_ts_ms": event_ts_ms,
                        "topic": topic,
                        "location_id": location_part or location_id,
                        "stream": stream_part,
                        "entity_id": row.get("entity_id"),
                        "x": row["x"],
                        "y": row["y"],
                    }
                )
                csv_rows += 1

            if not quiet and msg_count % 50 == 0:
                print(f"[record] messages={msg_count} csv_rows={csv_rows}", file=sys.stderr)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

end_iso = now_iso()
summary = {
    "run_id": run_id,
    "location_id": location_id,
    "mqtt": {
        "host": mqtt_host,
        "port": int(mqtt_port),
        "topic_filter": topic_filter,
    },
    "started_at_utc": start_iso,
    "ended_at_utc": end_iso,
    "duration_seconds": round(time.time() - start_time, 3),
    "message_count": msg_count,
    "json_payload_count": json_count,
    "csv_row_count": csv_rows,
    "first_ingest_ts_ms": first_ts,
    "last_ingest_ts_ms": last_ts,
    "topic_counts": topic_counts,
    "files": {
        "tracks_jsonl": jsonl_path,
        "tracks_csv": csv_path,
        "summary_json": summary_path,
    },
}
with open(summary_path, "w", encoding="utf-8") as sf:
    json.dump(summary, sf, indent=2, sort_keys=True)
    sf.write("\n")

print(f"[done] {summary_path}")
PY

cat <<EOF

[recording complete]
Run dir:
  $RUN_DIR

Files:
  $JSONL_PATH
  $CSV_PATH
  $SUMMARY_PATH
EOF
