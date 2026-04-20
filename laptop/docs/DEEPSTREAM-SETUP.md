# Laptop DeepStream 9.0 Setup

This document is the **DS 9.0 laptop setup reference** for the scripted
testing harness. It is a standalone mirror of Notion page
`337b5d58-7212-81e1-b07a-d510d9605bbb`: §1–4 are manual prerequisites that
operators complete **outside this repo** before running any script; §5–10
are the parts that [`laptop/scripts/`](../scripts/) automates.

Every external DS 9.0 URL in this doc is drawn from
[`.cursor/skills/deepstream-9-docs/reference.md`](../../.cursor/skills/deepstream-9-docs/reference.md)
so the link set stays in sync with NVIDIA's authoritative index. Every plugin
field and config fact was cross-checked against Context7 (library
`/websites/nvidia_metropolis_deepstream_dev-guide`) via the
[`deepstream-9-docs`](../../.cursor/skills/deepstream-9-docs/SKILL.md) skill
before being written.

This doc **does not read from or write to** [`my-docs/`](../../my-docs/).
The legacy DS 8.0 doc at
[`my-docs/02-LAPTOP-DEEPSTREAM-SETUP.md`](../../my-docs/02-LAPTOP-DEEPSTREAM-SETUP.md)
is left untouched; retiring it is a separate housekeeping PR.

## Overview

Target platform:

- Ubuntu 24.04 dual-boot (§3)
- Ampere-or-newer NVIDIA dGPU (§1–2)
- NVIDIA driver ≥ 550 (§4)
- CUDA Toolkit 12.4+ (§4)
- cuDNN 9.x (§4)
- TensorRT 10.x (§4)
- DeepStream 9.0 + GStreamer 1.24 (§5)
- Mosquitto 2.x (§6)
- 8× IP cameras on `192.168.10.101..108` (§7.2)
- Docker Engine + NVIDIA Container Toolkit (§8.2) — for AMC
- `NVIDIA-AI-IOT/auto-magic-calib` (§8.3) — cloned into `$HOME/auto-magic-calib/`
- PeopleNet v2.6.3 deployable (§9.3, only detector this harness installs)

## §1–4  Manual prerequisites (out of scope for every script)

The laptop must be booted into Ubuntu 24.04 with the full NVIDIA stack in
place **before** cloning this repo. None of the scripts under
[`laptop/scripts/`](../scripts/) install any of these; `00_bootstrap.sh` only
preflights them.

### §1 Overview / §2 Hardware & BIOS

- Ampere-or-newer NVIDIA discrete GPU, CUDA compute capability ≥ 8.0
  (`nvidia-smi --query-gpu=compute_cap --format=csv,noheader`).
- BIOS: enable virtualization extensions (VT-x / AMD-V) and disable Secure
  Boot if your distro-signed NVIDIA driver path requires it.
- ≥ 32 GB RAM recommended when running 8 cameras through DeepStream + AMC.

### §3 Dual-boot Ubuntu 24.04

Standard Ubuntu 24.04 LTS install from an installer USB (Rufus or
balenaEtcher). Shrink the existing Windows partition, let the Ubuntu
installer use the freed space. After first boot, `sudo apt update && sudo
apt full-upgrade`.

### §4 NVIDIA driver + CUDA + cuDNN + TensorRT

Use NVIDIA's official `cuda-keyring` repo for all four components. Minimum
versions:

| Component | Minimum | Verify with |
|-----------|---------|-------------|
| NVIDIA driver | 550 | `nvidia-smi --query-gpu=driver_version --format=csv,noheader` |
| CUDA Toolkit | 12.4 | `nvcc --version` |
| cuDNN | 9.0 | `dpkg -l \| grep libcudnn9` |
| TensorRT | 10.0 | `dpkg -l \| grep -E 'libnvinfer10\|tensorrt'` |

Reference installation guidance:

- DS 9.0 Installation (lists the exact supported driver/CUDA/cuDNN/TRT
  matrix): <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Installation.html>
- Platform & OS compatibility:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Installation.html#platform-and-os-compatibility>

Reboot after installing the driver so the kernel module is live. If
`nvidia-smi` returns a valid output and the four `dpkg -l` queries above all
succeed, §1–4 are complete.

**At this point you may clone this repo and run
`sudo bash laptop/scripts/00_bootstrap.sh`.** Everything from §5 onward is
automated.

## §5  DeepStream 9.0 + GStreamer 1.24  _(automated by `00_bootstrap.sh`)_

`00_bootstrap.sh` installs the following via apt:

- `deepstream-9.0`, `deepstream-9.0-reference-graphs`,
  `deepstream-9.0-samples`.
- The GStreamer 1.24 plugin set (`gstreamer1.0-{tools,plugins-*,libav,rtsp}`,
  `libgstrtspserver-1.0-0`).
- `/etc/profile.d/deepstream.sh` exporting `DEEPSTREAM_DIR`,
  `/opt/nvidia/deepstream/deepstream-9.0/{bin,lib}` onto `PATH` /
  `LD_LIBRARY_PATH`.

Reference docs (via the skill catalog):

- DS 9.0 Quickstart:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Quickstart.html>
- DS 9.0 deepstream-app:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_ref_app_deepstream.html>
- DS 9.0 Release Notes:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Release_notes.html>
- DS 9.0 Application Migration 8.0 → 9.0:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Application_migration.html>

### DS 9.0 breaking changes that affect this harness

Per the [skill's quick-reference table](../../.cursor/skills/deepstream-9-docs/reference.md)
("DS 9.0 Breaking Changes"):

| Change | How this harness deals with it |
|--------|--------------------------------|
| PyDS deprecated | No Python pipeline code. All pipeline assembly is via `deepstream-app` configs. |
| Graph Composer removed | N/A — we author configs by hand. |
| TF/UFF/Caffe model formats removed | PeopleNet ETLT still works (TAO-encrypted format is not UFF). |
| INT8 calibration removed (TAO) | [`config_infer_primary.txt`](../deepstream/config_infer_primary.txt) honours the PeopleNet INT8 cache if present; flip `network-mode=2` (FP16) if you hit issues. |
| YOLOv3/v4, SSD, FasterRCNN removed | Not used — PeopleNet only. |
| DLA not supported on Jetson Thor | N/A (this is the laptop harness). |

## §6  Mosquitto  _(automated by `00_bootstrap.sh` + `10_setup_mosquitto.sh`)_

`00_bootstrap.sh` installs `mosquitto` and `mosquitto-clients`.
`10_setup_mosquitto.sh` installs
[`laptop/mosquitto/mv3dt.conf`](../mosquitto/mv3dt.conf) into
`/etc/mosquitto/conf.d/mv3dt.conf` and restarts the service.

### `mv3dt.conf` (simple-testing posture)

```conf
listener 1883 0.0.0.0
listener 9001
protocol websockets
allow_anonymous true
max_inflight_messages 100
max_queued_messages 1000
max_packet_size 268435456
persistence false
log_dest file /var/log/mosquitto/mosquitto.log
log_type error
log_type warning
log_type notice
```

### Production hardening (reference — not applied by the scripts)

For a hardened production deployment, replace the simple-testing posture
with a bound listener, a password file, and an ACL file:

```conf
listener 1883 127.0.0.1
listener 9001 127.0.0.1
protocol websockets

password_file /etc/mosquitto/passwd
acl_file /etc/mosquitto/aclfile
allow_anonymous false
```

Then:

```bash
sudo mosquitto_passwd -c /etc/mosquitto/passwd mv3dt
sudo systemctl restart mosquitto
```

A minimal ACL:

```conf
user mv3dt
topic readwrite mv3dt/#
```

This deliberately lives in this doc (not [`my-docs/`](../../my-docs/)) so the
`laptop/` tree is self-contained.

### Firewall (Notion §6.3)

If the operator is running `ufw`, run `10_setup_mosquitto.sh --with-firewall`
to `ufw allow` 1883/tcp and 9001/tcp. Skipped by default because the
simple-testing posture does not assume `ufw` is active.

### DS 9.0 IoT / broker docs

- Gst-nvmsgbroker (MQTT proto lib):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvmsgbroker.html>
- Gst-nvmsgconv:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvmsgconv.html>
- IoT / Edge-to-Cloud Messaging:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_IoT.html>

## §7  Cameras  _(§7.5 automated by `20_verify_cameras.sh`)_

### §7.1–7.4  Per-camera manual setup (out of scope)

Static IP assignment and stream-profile selection are performed through each
camera's own web UI. Assign:

| Camera | IP | Stream path (default) |
|--------|-----|-----------------------|
| C1 | 192.168.10.101 | `/stream1` |
| C2 | 192.168.10.102 | `/stream1` |
| C3 | 192.168.10.103 | `/stream1` |
| C4 | 192.168.10.104 | `/stream1` |
| C5 | 192.168.10.105 | `/stream1` |
| C6 | 192.168.10.106 | `/stream1` |
| C7 | 192.168.10.107 | `/stream1` |
| C8 | 192.168.10.108 | `/stream1` |

All eight rows (including the `position` label and the `enabled` flag) are
encoded in [`laptop/config/cameras.yml`](../config/cameras.yml). Override
`rtsp_path` per camera for mixed fleets.

### §7.5  Verification (automated)

```bash
bash laptop/scripts/20_verify_cameras.sh
# or, for partial rigs:
bash laptop/scripts/20_verify_cameras.sh --allow-partial
```

## §8  AutoMagicCalib  _(§8.2 automated by `00_bootstrap.sh`; §8.3–8.7 by `30_`/`40_`)_

### §8.2  Docker + NVIDIA Container Toolkit

`00_bootstrap.sh` installs:

- Docker Engine + `docker-compose-plugin` from Docker's Ubuntu apt repo.
- `nvidia-container-toolkit`, and runs `nvidia-ctk runtime configure --runtime=docker`.
- Adds the invoking (`$SUDO_USER`) user to the `docker` group — **log out /
  back in** once before running `30_start_amc.sh`, or use `sudo docker` for
  that first run.

Reference:
<https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_docker_containers.html>

### §8.3–8.5  AMC bring-up (`30_start_amc.sh`)

`30_start_amc.sh` is a runtime orchestrator; **AMC itself is not vendored
into this repo**. The script:

1. Clones `https://github.com/NVIDIA-AI-IOT/auto-magic-calib.git` into
   `$HOME/auto-magic-calib/` if missing (refuses to place the clone under
   the repo working tree).
2. Creates `projects/` and `models/` subdirs and `chown 1000:1000`s them
   (Notion §8.3 — the in-container UID is 1000).
3. `docker login nvcr.io` using `NGC_API_KEY` if set.
4. Writes `$HOME/auto-magic-calib/compose/.env` from
   [`laptop/config/laptop.env`](../config/laptop.env.example) with:
   - `HOST_IP`
   - `AUTO_MAGIC_CALIB_MS_PORT=8000`
   - `AUTO_MAGIC_CALIB_UI_PORT=5000`
   - `PROJECT_DIR=$AMC_ROOT/projects`
   - `MODEL_DIR=$AMC_ROOT/models`
   - `NVIDIA_VISIBLE_DEVICES=all`
   - `PROJECT_NAME=$PROJECT_NAME`

   Upstream AMC's own `.env.example` is diffed at run time and a warning is
   printed for any key that no longer exists upstream.
5. `docker compose pull && docker compose up -d`.
6. Opens `http://localhost:5000` via `xdg-open` (or prints the URL on
   headless sessions).

Reference:
<https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_AutoMagicCalib.html>

### §8.6  AMC 6-step workflow (**human-driven in the browser**)

1. **Project Setup** — name: `$PROJECT_NAME`; pick #cameras = 8.
2. **Video Upload** — one MP4 per camera, ≥ 15 s, stationary scene.
3. **Parameters** — intrinsic guess: let AMC auto-detect; extrinsic guess:
   pick the nearest preset layout.
4. **Manual Align** — click-to-match ≥ 6 correspondences per pair so AMC
   has enough to bootstrap.
5. **Execute** — run calibration. This is where VGGT runs.

   > **VGGT note (via skill):** if Execute times out, the common cause is
   > GPU VRAM pressure from the VGGT stage. Reduce the in-frame resolution
   > of the uploaded clips or restart AMC before retry.

6. **Results / Export** — review RMSE, export MV3DT artefacts into
   `$HOME/auto-magic-calib/projects/$PROJECT_NAME/exports/`.

### §8.7  Export ingest (`40_export_watcher.sh`)

`40_export_watcher.sh`:

1. Watches `$HOME/auto-magic-calib/projects/$PROJECT_NAME/exports/` with
   `inotifywait` (polls every 5 s if not installed).
2. On a change, first tries `$AMC_ROOT/scripts/export_mv3dt.py
   --project $PROJECT_NAME --output <REPO_ROOT>/laptop/deepstream/calibration/$LOCATION_ID/`.
   The AMC upstream is the ground truth — if that script was renamed in a
   newer release, the watcher falls back to a raw `cp -a` from `exports/`.
3. Renders `laptop/deepstream/deepstream_app_config.rendered.txt` from the
   committed template, substituting `${CAM_USER}` / `${CAM_PASSWORD}` /
   `${LOCATION_ID}` and rewriting each `[sourceN]` `uri=...` with the per-
   camera URL from [`cameras.yml`](../config/cameras.yml).

## §9  MV3DT directory layout + models  _(automated by `25_prepare_models.sh`)_

### §9.2  Directory layout

```
laptop/deepstream/
  deepstream_app_config.txt              # committed template (8 sources, NvMOT, MQTT sink)
  deepstream_app_config.rendered.txt     # generated by 40_export_watcher.sh (gitignored)
  config_infer_primary.txt               # PeopleNet only
  config_tracker_NvMOT.yml               # NvDCF + ReID + SV3DT + MV3DT
  msgconv_config.txt
  calibration/
    .gitkeep
    <LOCATION_ID>/                       # written by 40_export_watcher.sh
  models/
    .gitkeep
    peoplenet/                           # written by 25_prepare_models.sh
```

### §9.3  Detector model

**PeopleNet only** — matches NVIDIA's DS 9.0 MV3DT reference documentation
and is intentionally enforced by this plan. `25_prepare_models.sh` runs:

```bash
ngc registry model download-version "$PEOPLENET_NGC_TAG"
# default: nvidia/tao/peoplenet:deployable_quantized_v2.6.3
```

into `laptop/deepstream/models/peoplenet/` and writes `labels.txt`
(`person`, `bag`, `face`). Override the NGC tag via `PEOPLENET_NGC_TAG` in
[`laptop.env`](../config/laptop.env.example) without editing the script.

#### yolo11n — future work, NOT installed

`yolo11n` (ultralytics `yolo11n.pt`) is the **single approved alternative**
detector reserved for future work. It is explicitly **not** installed,
exported, or configured by any script in this plan. See the
`marcoslucianops/DeepStream-Yolo` entry in
[`.cursor/skills/deepstream-9-docs/reference.md`](../../.cursor/skills/deepstream-9-docs/reference.md)
(Third-Party / Community) when wiring it in a future iteration.

### DS 9.0 plugin reference (verified via skill)

- Gst-nvvideo4linux2 (decoder):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvvideo4linux2.html>
- Gst-nvstreammux:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvstreammux.html>
- Gst-nvinfer (TensorRT inference):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvinfer.html>
- Gst-nvtracker:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvtracker.html>
- MV3DT 9.0:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_MV3DT.html>

### NvMultiObjectTracker YAML (reference view)

Full template:
[`laptop/deepstream/config_tracker_NvMOT.yml`](../deepstream/config_tracker_NvMOT.yml).
Key blocks:

```yaml
SV3DT:
  enable: 1
  calibrationDirectory: calibration/${LOCATION_ID}
  projectionType: homography

MV3DT:
  enable: 1
  mqttBrokerIP: 127.0.0.1
  mqttBrokerPort: 1883
  nodeID: ${LOCATION_ID}
  fusionUpdateRate: 30
  globalIDNegotiationTimeout: 100
```

`calibrationDirectory` is populated by `40_export_watcher.sh` from AMC's
export. `mqttBrokerIP`/`Port` match the local Mosquitto listener installed
by `10_setup_mosquitto.sh`.

## §10  Startup + monitoring  _(automated by `50_start_pipeline.sh`)_

### §10.1  Startup sequence

```bash
bash laptop/scripts/50_start_pipeline.sh
```

Internally:

1. `sudo systemctl start mosquitto` (idempotent).
2. Ping-sweep C1..C8 from [`cameras.yml`](../config/cameras.yml).
3. Source `/etc/profile.d/deepstream.sh`.
4. `cd laptop/deepstream/ && exec deepstream-app -c deepstream_app_config.rendered.txt`.

Use `--force-template` for a dry-run against the unrendered committed
template (placeholder credentials), `--skip-ping` to skip the sweep, and
`--dry-run` to print the final command without launching.

### §10.2  Validation / monitoring

In a second tty:

```bash
mosquitto_sub -h 127.0.0.1 -t 'mv3dt/#' -v
watch -n 1 'nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu --format=csv'
systemctl status mosquitto --no-pager
```

`50_start_pipeline.sh` prints these same commands as a block before
executing `deepstream-app` so the operator has them at hand.

## References

This doc's external links come from the catalog in
[`.cursor/skills/deepstream-9-docs/reference.md`](../../.cursor/skills/deepstream-9-docs/reference.md).
For edits to this doc, the canonical DS 9.0 lookup path is (in priority
order):

1. **Context7** — library `/websites/nvidia_metropolis_deepstream_dev-guide`
   (20,418 snippets, high reputation) for any conceptual / API question.
2. **WebFetch** the specific URL from the skill's catalog.
3. **GitHub** samples in the skill's repo catalog
   (e.g. `NVIDIA-AI-IOT/deepstream_reference_apps/deepstream-tracker-3d-multi-view`
   for the MV3DT reference pipeline).

See [`../../.cursor/skills/deepstream-9-docs/SKILL.md`](../../.cursor/skills/deepstream-9-docs/SKILL.md)
for the full routing rules.
