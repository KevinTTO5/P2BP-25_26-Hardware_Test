#!/usr/bin/env bash
# laptop/scripts/40_export_watcher.sh
#
# Notion page 337b5d58-7212-81e1-b07a-d510d9605bbb §8.7 — AMC → MV3DT
# calibration export ingestion.
#
# Watches $AMC_ROOT/projects/$PROJECT_NAME/exports/ for new files. When new
# files appear, this script:
#
#   (a) Probes AMC for the actual exporter name. First tries
#       $AMC_ROOT/scripts/export_mv3dt.py as documented in Notion §8.7; if
#       that script is missing in a newer AMC release, falls back to copying
#       the files from exports/ directly (the upstream AMC repo is the
#       ground truth at run time — see
#       .cursor/skills/deepstream-9-docs/ AutoMagicCalib doc).
#
#   (b) Lands artifacts INSIDE this repo under
#       laptop/deepstream/calibration/$LOCATION_ID/ (the nested
#       laptop/.gitignore excludes calibration/*/ so nothing leaks in, but
#       the parent directory remains for committing if desired).
#
#   (c) Renders laptop/deepstream/deepstream_app_config.rendered.txt from
#       the committed deepstream_app_config.txt template by expanding the
#       ${CAM_USER}/${CAM_PASSWORD}/${LOCATION_ID} placeholders and the
#       per-camera RTSP URIs from laptop/config/cameras.yml. The original
#       template is left untouched so it can be re-rendered.
#
# Idempotent: re-running performs the copy/render once on startup, then
# watches for further changes. Ctrl-C to stop.
#
# Writes only under:
#   - laptop/deepstream/calibration/$LOCATION_ID/   (inside repo)
#   - laptop/deepstream/deepstream_app_config.rendered.txt (inside repo)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

ONESHOT=0

usage() {
  cat <<'EOF'
Usage: 40_export_watcher.sh [--oneshot] [-h|--help]

Ingests AMC exports into laptop/deepstream/calibration/$LOCATION_ID/ and
renders laptop/deepstream/deepstream_app_config.rendered.txt.

Options:
  --oneshot   Run the copy + render pass once and exit (do not watch).
  -h, --help  Show this help and exit.

Uses laptop/config/laptop.env. Requires 'inotifywait' for watch mode (falls
back to polling every 5s if not installed).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --oneshot) ONESHOT=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "Unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

require_tool python3
load_env

: "${LOCATION_ID:?LOCATION_ID is required in laptop/config/laptop.env}"
: "${PROJECT_NAME:=${LOCATION_ID}}"
: "${AMC_ROOT:=$HOME/auto-magic-calib}"
: "${CAM_USER:?CAM_USER is required in laptop/config/laptop.env}"
: "${CAM_PASSWORD:?CAM_PASSWORD is required in laptop/config/laptop.env}"

REPO_ROOT="$(repo_root)"
EXPORT_DIR="$AMC_ROOT/projects/$PROJECT_NAME/exports"
CAL_DEST="$REPO_ROOT/laptop/deepstream/calibration/$LOCATION_ID"
TEMPLATE="$REPO_ROOT/laptop/deepstream/deepstream_app_config.txt"
RENDERED="$REPO_ROOT/laptop/deepstream/deepstream_app_config.rendered.txt"
CAMERAS_YML="$REPO_ROOT/laptop/config/cameras.yml"

if [[ ! -d "$AMC_ROOT" ]]; then
  die "AMC not present at $AMC_ROOT. Run laptop/scripts/30_start_amc.sh first."
fi
if [[ ! -f "$TEMPLATE" ]]; then
  die "Missing template: $TEMPLATE"
fi
if [[ ! -f "$CAMERAS_YML" ]]; then
  die "Missing $CAMERAS_YML"
fi

mkdir -p "$CAL_DEST" "$EXPORT_DIR"

ingest_exports() {
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"

  local used_exporter=0
  if [[ -f "$AMC_ROOT/scripts/export_mv3dt.py" ]]; then
    log_info "Running upstream exporter: $AMC_ROOT/scripts/export_mv3dt.py"
    if ( cd "$AMC_ROOT" && python3 scripts/export_mv3dt.py \
          --project "$PROJECT_NAME" \
          --output "$CAL_DEST" ); then
      used_exporter=1
    else
      log_warn "Upstream exporter failed; falling back to raw copy from $EXPORT_DIR"
    fi
  else
    log_info "No upstream exporter script found (upstream layout may have changed)."
  fi

  if [[ "$used_exporter" -eq 0 ]]; then
    if compgen -G "$EXPORT_DIR/*" >/dev/null; then
      log_info "Copying $EXPORT_DIR/ -> $CAL_DEST/"
      cp -a "$EXPORT_DIR"/. "$CAL_DEST"/
    else
      log_warn "Nothing in $EXPORT_DIR yet; skipping copy (will re-try on next event)."
      return 1
    fi
  fi

  echo "$stamp  ingested from $EXPORT_DIR" >> "$CAL_DEST/.ingest.log"
  return 0
}

render_pipeline() {
  log_info "Rendering $RENDERED"
  CAM_USER_VAL="$CAM_USER" \
  CAM_PASSWORD_VAL="$CAM_PASSWORD" \
  LOCATION_ID_VAL="$LOCATION_ID" \
  CAL_DEST_VAL="$CAL_DEST" \
  CAMERAS_YML_VAL="$CAMERAS_YML" \
  TEMPLATE_VAL="$TEMPLATE" \
  RENDERED_VAL="$RENDERED" \
  python3 <<'PY'
import os, re, sys

cam_user = os.environ["CAM_USER_VAL"]
cam_pass = os.environ["CAM_PASSWORD_VAL"]
location = os.environ["LOCATION_ID_VAL"]
cal_dest = os.environ["CAL_DEST_VAL"]
cameras_yml = os.environ["CAMERAS_YML_VAL"]
template_path = os.environ["TEMPLATE_VAL"]
rendered_path = os.environ["RENDERED_VAL"]

def parse_cameras(path):
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
    return [r for r in rows if str(r.get("enabled", "true")).lower() not in ("false", "0", "no")]

cams = parse_cameras(cameras_yml)

with open(template_path, "r", encoding="utf-8") as f:
    tpl = f.read()

def substitute_placeholders(text):
    text = text.replace("${CAM_USER}", cam_user)
    text = text.replace("${CAM_PASSWORD}", cam_pass)
    text = text.replace("${LOCATION_ID}", location)
    return text

tpl = substitute_placeholders(tpl)

# Overwrite [sourceN] uri= with the per-camera RTSP URL from cameras.yml,
# preserving the order from the YAML (one [sourceN] block per enabled entry).
def rewrite_source_uris(text, cams):
    def replace_block(match):
        idx = int(match.group(1))
        if idx >= len(cams):
            return match.group(0)
        cam = cams[idx]
        ip = cam.get("ip", "")
        rtsp_path = cam.get("rtsp_path", "/stream1")
        new_uri = f"rtsp://{cam_user}:{cam_pass}@{ip}:554{rtsp_path}"
        block = match.group(0)
        block = re.sub(r"(?m)^uri=.*$", f"uri={new_uri}", block, count=1)
        return block
    pattern = re.compile(r"\[source(\d+)\][^\[]*", re.DOTALL)
    return pattern.sub(replace_block, text)

tpl = rewrite_source_uris(tpl, cams)

header = (
    "# Rendered by laptop/scripts/40_export_watcher.sh\n"
    f"# Source template: {template_path}\n"
    f"# Calibration dir: {cal_dest}\n"
    f"# LOCATION_ID    : {location}\n"
    "# Edit the committed template, not this file; it is regenerated.\n"
    "\n"
)
with open(rendered_path, "w", encoding="utf-8") as f:
    f.write(header + tpl)
print(f"[rendered] {rendered_path}")
PY
}

do_pass() {
  if ingest_exports; then
    render_pipeline
    cat <<EOF

[ready] Pipeline config rendered:
  $RENDERED
Calibration:
  $CAL_DEST/

Next:
  laptop/scripts/50_start_pipeline.sh
EOF
  fi
}

log_info "Initial ingestion + render pass"
do_pass || true

if [[ "$ONESHOT" -eq 1 ]]; then
  exit 0
fi

if command -v inotifywait >/dev/null 2>&1; then
  log_info "Watching $EXPORT_DIR with inotifywait (Ctrl-C to stop)"
  while inotifywait -qq -e close_write,moved_to,create -r "$EXPORT_DIR"; do
    sleep 1
    do_pass || true
  done
else
  log_warn "inotifywait not found; falling back to 5s polling."
  last_hash=""
  while true; do
    cur_hash="$(ls -la "$EXPORT_DIR" 2>/dev/null | sha256sum | cut -d' ' -f1)"
    if [[ "$cur_hash" != "$last_hash" ]]; then
      last_hash="$cur_hash"
      do_pass || true
    fi
    sleep 5
  done
fi
