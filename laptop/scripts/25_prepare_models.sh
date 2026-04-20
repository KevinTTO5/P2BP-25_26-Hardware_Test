#!/usr/bin/env bash
# laptop/scripts/25_prepare_models.sh
#
# Notion page 337b5d58-7212-81e1-b07a-d510d9605bbb §9.2-9.3 — create the
# DS 9.0 MV3DT models layout and download PeopleNet via the NGC CLI.
#
# PeopleNet is the ONLY detector installed by any script in this harness,
# matching NVIDIA's DS 9.0 MV3DT reference documentation (DS_MV3DT.html, via
# the .cursor/skills/deepstream-9-docs/ skill). `yolo11n` (ultralytics
# yolo11n.pt) is the single approved alternative detector for future work
# and is mentioned (by name only) at the end of this script — it is NOT
# downloaded, exported, or wired into any config here. See the
# marcoslucianops/DeepStream-Yolo entry in
# .cursor/skills/deepstream-9-docs/reference.md for the future-work path.
#
# Idempotent: re-running is a no-op once the PeopleNet artifacts are present.
#
# Writes only under laptop/deepstream/models/peoplenet/ inside this repo.
# Never reaches outside the laptop/ subtree.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

FORCE=0

usage() {
  cat <<'EOF'
Usage: 25_prepare_models.sh [--force] [-h|--help]

Downloads PeopleNet (the only detector this harness wires into the pipeline)
into laptop/deepstream/models/peoplenet/ via the NGC CLI.

Options:
  --force     Redownload even if the target directory already has artifacts.
  -h, --help  Show this help and exit.

Environment:
  PEOPLENET_NGC_TAG  NGC tag to pull (default from laptop.env; fallback to
                     nvidia/tao/peoplenet:deployable_quantized_v2.6.3 per
                     Notion §9.3 Option A).
  NGC_API_KEY        Optional NGC API key. If set, used to configure the NGC
                     CLI non-interactively. If unset, relies on any existing
                     ~/.ngc/config / `docker login nvcr.io` credentials.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "Unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

require_tool python3
load_env

: "${PEOPLENET_NGC_TAG:=nvidia/tao/peoplenet:deployable_quantized_v2.6.3}"

MODELS_ROOT="$(repo_root)/laptop/deepstream/models"
PN_DIR="$MODELS_ROOT/peoplenet"
LABELS_FILE="$PN_DIR/labels.txt"

mkdir -p "$MODELS_ROOT"

if [[ -d "$PN_DIR" && "$FORCE" -ne 1 ]]; then
  if compgen -G "$PN_DIR/*" > /dev/null; then
    log_info "PeopleNet artifacts already present in $PN_DIR (use --force to redownload)."
    SKIP_DOWNLOAD=1
  fi
fi

if [[ "${SKIP_DOWNLOAD:-0}" -ne 1 ]]; then
  if ! command -v ngc >/dev/null 2>&1; then
    cat <<'EOF' >&2

[error] NGC CLI ('ngc') not found on PATH.

Install the NGC CLI from https://ngc.nvidia.com/setup/installers/cli and
re-run this script. On Ubuntu 24.04 the typical install is:

    wget -O /tmp/ngccli_linux.zip \
      https://api.ngc.nvidia.com/v2/resources/nvidia/ngc-apps/ngc_cli/versions/3.41.2/files/ngccli_linux.zip
    unzip /tmp/ngccli_linux.zip -d "$HOME/ngc-cli"
    sudo ln -sf "$HOME/ngc-cli/ngc-cli/ngc" /usr/local/bin/ngc

Or use the official Docker image and mount a cache dir for the download.
EOF
    exit 2
  fi

  if [[ -n "${NGC_API_KEY:-}" ]]; then
    log_info "Configuring NGC CLI from NGC_API_KEY (non-interactive)."
    NGC_CONFIG_DIR="$HOME/.ngc"
    mkdir -p "$NGC_CONFIG_DIR"
    chmod 700 "$NGC_CONFIG_DIR"
    cat > "$NGC_CONFIG_DIR/config" <<NGCCFG
[CURRENT]
apikey = ${NGC_API_KEY}
format_type = ascii
org = nvidia
NGCCFG
    chmod 600 "$NGC_CONFIG_DIR/config"
  else
    log_warn "NGC_API_KEY not set. Assuming 'ngc' is already configured (or you ran 'docker login nvcr.io')."
  fi

  log_info "Downloading PeopleNet model: $PEOPLENET_NGC_TAG"
  mkdir -p "$PN_DIR"
  TMP_DL="$(mktemp -d)"
  trap 'rm -rf "$TMP_DL"' EXIT

  if ! ngc registry model download-version "$PEOPLENET_NGC_TAG" --dest "$TMP_DL"; then
    die "NGC download failed for $PEOPLENET_NGC_TAG. Re-check NGC_API_KEY and the tag (re-verify via .cursor/skills/deepstream-9-docs/ against /websites/nvidia_metropolis_deepstream_dev-guide)."
  fi

  mapfile -t _dl_roots < <(find "$TMP_DL" -mindepth 1 -maxdepth 1 -type d)
  if [[ "${#_dl_roots[@]}" -eq 0 ]]; then
    die "NGC CLI returned no artifacts into $TMP_DL"
  fi

  for d in "${_dl_roots[@]}"; do
    cp -a "$d"/. "$PN_DIR"/
  done

  log_info "PeopleNet artifacts installed under $PN_DIR"
fi

if [[ ! -f "$LABELS_FILE" ]]; then
  log_info "Writing $LABELS_FILE (Notion §9.3 Option A class list)"
  cat > "$LABELS_FILE" <<'LABELS'
person
bag
face
LABELS
fi

cat <<EOF

Model layout:
  $PN_DIR/
    labels.txt
    ... (NGC-downloaded artifacts)

DS 9.0 note (breaking change, see
.cursor/skills/deepstream-9-docs/reference.md "DS 9.0 Breaking Changes"):
INT8 calibration was removed for TAO models in DS 9.0. The in-repo
config_infer_primary.txt therefore defaults to FP16 unless an explicit INT8
calibration cache ships with the pinned PeopleNet version above.

Approved alternative detector for future work:
  yolo11n (ultralytics yolo11n.pt) — NOT installed by this script.
  See https://github.com/marcoslucianops/DeepStream-Yolo (catalogued in
  .cursor/skills/deepstream-9-docs/reference.md) for the future-work path.

Next step:
  laptop/scripts/30_start_amc.sh
EOF
