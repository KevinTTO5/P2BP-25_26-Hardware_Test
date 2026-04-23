# Laptop Scripts & Config Reference (DS 9.0 + AMC + MV3DT)

Concise map of what every script under [`laptop/scripts/`](../scripts/) does,
which DS 9.0 doc section it implements, and exactly which config files you
must customize for your own cameras / site / MV3DT output. Read this before
running anything.

Companion docs:
[`SCRIPTED-WORKFLOW.md`](SCRIPTED-WORKFLOW.md) (operator run order) and
[`DEEPSTREAM-SETUP.md`](DEEPSTREAM-SETUP.md) (manual §1–4 prereqs).

---

## 1. What runs where (high-level)

```
Manual (§1–4)  →  00_bootstrap  →  10_mosquitto  →  20_verify_cameras
                 (incl. PeopleNet)                 →  30_start_amc  →  [human: AMC 6-step UI]
                                 →  40_export_watcher  →  50_start_pipeline
                                                       →  99_stop_all
```

Or supply local `.deb` files (see `DEEPSTREAM-SETUP.md` §4) and let
`00_bootstrap.sh` install the full NVIDIA + DS 9.0 stack in phases. All numbered
scripts are idempotent. The **AMC browser** workflow (§8.6) is always manual in
the UI.

---

## 2. Script-by-script table (cross-referenced to DS 9.0 docs)

| # | Script | DS 9.0 doc section | What it installs / does | sudo? | Idempotent |
|---|--------|--------------------|-------------------------|-------|-----------|
| — | **Manual prereq** | [DS_Installation → dGPU Setup for Ubuntu → Prerequisites](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Installation.html) | Ubuntu 24.04, NVIDIA driver **590.48.01**, CUDA **13.1**, TensorRT **10.14.1.48-1+cuda13.0**, cuDNN **9.18.0**, GStreamer **1.24.2**. | yes | n/a |
| 00 | [`00_bootstrap.sh`](../scripts/00_bootstrap.sh) | [DS_Installation](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Installation.html); [DS_docker_containers](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_docker_containers.html) | **Phased install**: pre-downloaded NVIDIA local-repo + cuda-keyring debs; driver `590.48.01` + CUDA 13.1 + cuDNN 9.18 + TRT 10.14.1.48-1; `/etc/profile.d/cuda.sh`; GStreamer 1.24 + DS apt prerequisites; Mosquitto; Docker + `nvidia-container-toolkit`; NGC `ngc registry resource download-version` of `deepstream-9.0_9.0.0-1_amd64.deb` + `apt install`; `update_rtpmanager.sh` + `/etc/profile.d/deepstream.sh`; version audit; `laptop/config/laptop.env`; PeopleNet ONNX → `laptop/deepstream/models/peoplenet/`. | yes | yes |
| 10 | [`10_setup_mosquitto.sh`](../scripts/10_setup_mosquitto.sh) | [DS_IoT](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_IoT.html); [Gst-nvmsgbroker](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvmsgbroker.html) | Installs [`laptop/mosquitto/mv3dt.conf`](../mosquitto/mv3dt.conf) → `/etc/mosquitto/conf.d/mv3dt.conf`; `systemctl enable --now mosquitto`. `--with-firewall` opens `ufw` 1883/9001. | yes | yes |
| 20 | [`20_verify_cameras.sh`](../scripts/20_verify_cameras.sh) | [DS_Quickstart](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Quickstart.html) (RTSP input sanity) | For each enabled row in [`cameras.yml`](../config/cameras.yml): `ping` then `ffprobe -rtsp_transport tcp` against `rtsp://$CAM_USER:$CAM_PASSWORD@$ip:554$rtsp_path`. Emits a PASS/FAIL/SKIP table. | no | yes |
| 30 | [`30_start_amc.sh`](../scripts/30_start_amc.sh) | [DS_AutoMagicCalib](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_AutoMagicCalib.html); [NVIDIA-AI-IOT/auto-magic-calib](https://github.com/NVIDIA-AI-IOT/auto-magic-calib) README | `git clone https://github.com/NVIDIA-AI-IOT/auto-magic-calib.git $AMC_ROOT` (default `$HOME/auto-magic-calib`, **must live outside this repo**). `chown 1000:1000` on `projects/` and `models/`. `docker login nvcr.io` (if `NGC_API_KEY` set). Writes `$AMC_ROOT/compose/.env` from your `laptop.env`. `docker compose pull && up -d`. Opens `http://localhost:$AUTO_MAGIC_CALIB_UI_PORT` (default 5000). | no (docker group) | yes |
| — | _human_ | [DS_AutoMagicCalib §6-step workflow](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_AutoMagicCalib.html) | In the AMC web UI: **(1) Project Setup → (2) Video Upload → (3) Parameters → (4) Manual Align → (5) Execute → (6) Results / Export**. | — | n/a |
| 40 | [`40_export_watcher.sh`](../scripts/40_export_watcher.sh) | [DS_AutoMagicCalib §Export](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_AutoMagicCalib.html); [DS_MV3DT §Calibration input](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_MV3DT.html) | Watches `$AMC_ROOT/projects/$PROJECT_NAME/exports/` (inotify; 5 s polling fallback). Prefers `$AMC_ROOT/scripts/export_mv3dt.py`; falls back to raw copy. Lands artifacts in `laptop/deepstream/calibration/$LOCATION_ID/`. **Renders** `laptop/deepstream/deepstream_app_config.rendered.txt` by substituting `${CAM_USER}`, `${CAM_PASSWORD}`, `${LOCATION_ID}` and rewriting each `[sourceN]` `uri=` from `cameras.yml`. `--oneshot` for a single pass. | no | yes |
| 50 | [`50_start_pipeline.sh`](../scripts/50_start_pipeline.sh) | [DS_ref_app_deepstream](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_ref_app_deepstream.html); [DS_MV3DT §Pipeline](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_MV3DT.html); [Gst-nvmsgbroker](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvmsgbroker.html) | Ensures `mosquitto` is active, ping-sweeps C1..C8, sources `/etc/profile.d/deepstream.sh`, then `exec deepstream-app -c laptop/deepstream/deepstream_app_config.rendered.txt`. Prints §10.2 validation helpers (`mosquitto_sub`, `nvidia-smi`). `--force-template`, `--config`, `--skip-ping`, `--dry-run` flags. | sudo only if mosquitto not yet active | yes |
| 99 | [`99_stop_all.sh`](../scripts/99_stop_all.sh) | — | `pkill deepstream-app` → `docker compose down` in `$AMC_ROOT/compose` → `systemctl stop mosquitto`. Per-component skip flags. Does **not** remove packages, calibration, or the AMC clone. | partial | yes |

### 2.1 Shared lib

[`lib/common.sh`](../scripts/lib/common.sh) — must be sourced, never
executed. Provides `log_info/warn/error`, `die`, `require_root`,
`require_tool`, `repo_root`, `load_env` (sources
`laptop/config/laptop.env` with `set -a`).

---

## 3. Gotchas (the "had to install it along the way" list)

These are the most common failure modes and what to do:

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `00_bootstrap` preflight fails with driver/CUDA/TRT version errors | §1–4 was not completed at the exact pinned versions | Follow [`DEEPSTREAM-SETUP.md`](DEEPSTREAM-SETUP.md) §4 **exactly** — driver `590.48.01`, CUDA `13.1`, TRT `10.14.1.48-1+cuda13.0`, cuDNN `9.18.0`. DS 9.0 refuses to load older/newer minors. |
| `deepstream-app` complains about missing `libmosquitto1`, `libjansson4`, `libyaml-cpp`, `libjsoncpp`, `protobuf-compiler`, or `libgles2-mesa-dev` | These are [DS 9.0 §Install Dependencies](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Installation.html) prerequisites. The `deepstream-9.0` deb pulls most as transitive deps but occasionally a clean install misses one. | `sudo apt install libssl3 libssl-dev libcurl4-openssl-dev libgles2-mesa-dev libjansson4 libyaml-cpp-dev libjsoncpp-dev protobuf-compiler libmosquitto1 gcc make git python3` |
| `ngc: command not found` in `00_bootstrap` (Phases 5–6 / 10) | NGC CLI is operator-installed (no official .deb) | Install per Phase 5 banner in `00_bootstrap.sh`, then re-run. |
| PeopleNet / NGC auth error in `00_bootstrap` Phase 10 | `NGC_API_KEY` missing and `ngc` not yet configured | Set `NGC_API_KEY` in `laptop/config/laptop.env`, or run `ngc config set` / `docker login nvcr.io` as your user. |
| `30_start_amc` fails: "Cannot locate compose dir" | Upstream AMC repo layout changed | Check the [AMC README](https://github.com/NVIDIA-AI-IOT/auto-magic-calib); compose dir may have moved to repo root. |
| AMC "Execute" step times out | GPU VRAM pressure from the VGGT stage | See the workaround in [DS_AutoMagicCalib](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_AutoMagicCalib.html); reduce resolution or shorten clips. |
| `50_start_pipeline` errors "no rendered pipeline config found" | `40_export_watcher` has not rendered `deepstream_app_config.rendered.txt` yet | Run `laptop/scripts/40_export_watcher.sh --oneshot` first. For a structural smoke test only, use `50_start_pipeline.sh --force-template`. |
| INT8 calibration cache missing | DS 9.0 **removed INT8 calibration for TAO models** | Use FP16 (default in `config_infer_primary.txt`). This is a DS 9.0 breaking change. |

---

## 4. Config files — what you must customize

Only **four** files carry per-site state. Everything else is either pinned by
NVIDIA or generated.

### 4.1 [`laptop/config/laptop.env`](../config/laptop.env.example) — operator env

Created by `00_bootstrap.sh` (interactive). Re-run `00_bootstrap.sh` to
change values; direct edits are also fine and preserved on next run.

| Key | Required? | What it controls | Change when… |
|-----|----------|------------------|--------------|
| `HOST_IP` | yes | Laptop IPv4 on camera LAN (written into AMC `compose/.env` so the AMC UI is reachable). | Your camera subnet differs. |
| `LOCATION_ID` | yes | Short site label; used as AMC `PROJECT_NAME` default and as the subdir under `laptop/deepstream/calibration/<LOCATION_ID>/`. Also appears in MQTT topics (`mv3dt/<LOCATION_ID>/sv3d`, `/fused`). | Every new site / dataset. |
| `PROJECT_NAME` | no | AMC project name (defaults to `LOCATION_ID`). | Running multiple AMC projects on the same laptop. |
| `CAM_USER`, `CAM_PASSWORD` | yes | RTSP credentials used by `20_verify_cameras.sh` and injected into `[sourceN] uri=` by `40_export_watcher.sh`. | Cameras use non-default creds. |
| `NGC_API_KEY` | recommended | Used for non-interactive `docker login nvcr.io` and NGC config for Phase 10. | Fresh laptop, or key rotation. |
| `LOCAL_DEB_DIR` | no | Pre-downloaded NVIDIA `.debs` and NGC DeepStream output (default `~/Downloads` of the user invoking `sudo`). | Using a non-default download directory. |
| `DETECTOR` | pinned | Fixed at `peoplenet`. **Do not change** (only approved alternative is `yolo11n`, not wired in). | Never (future work only). |
| `PEOPLENET_NGC_TAG` | no | NGC tag for PeopleNet ONNX (Phase 10 of `00_bootstrap.sh`). | Pinning a specific PeopleNet build. |
| `AMC_ROOT` | no | Where AMC gets cloned (default `$HOME/auto-magic-calib`). **Must NOT be inside this repo.** | You want the AMC clone elsewhere. |
| `AUTO_MAGIC_CALIB_MS_PORT`, `AUTO_MAGIC_CALIB_UI_PORT` | no | AMC microservice / UI ports (defaults 8000 / 5000). | Port conflicts. |
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_TOPIC_BASE` | no | Consumed by the rendered pipeline config's `[sink0]` block. Defaults `127.0.0.1`, `1883`, `mv3dt`. | Remote broker, different topic tree. |

### 4.2 [`laptop/config/cameras.yml`](../config/cameras.yml) — per-camera table

One YAML block per physical camera. `20_verify_cameras.sh` reads this,
`40_export_watcher.sh` writes one `[sourceN]` block per **enabled** entry in
the rendered pipeline config, and `50_start_pipeline.sh` ping-sweeps it.

```yaml
cameras:
  - id: c1                      # short label; must match [sourceN] ordering
    ip: 192.168.10.101          # IPv4 on the camera LAN (from your DHCP / static plan)
    position: "entry-north"     # human label, surfaced in the verify table
    rtsp_path: /stream1         # camera-model-specific (Hikvision: /Streaming/Channels/101, Amcrest: /cam/realmonitor?channel=1&subtype=0)
    enabled: true               # set false to skip without deleting
```

**What to change for your dataset:**

1. **Number of entries** — add or remove rows to match your camera count.
   NVIDIA's DS 9.0 MV3DT reference uses up to 8 cameras, matching the
   seeded C1..C8.
2. **IPs** — match whatever your DHCP / static plan assigns on the
   camera LAN. The reference uses `192.168.10.101..108` with laptop on
   `192.168.10.10`.
3. **`rtsp_path`** — varies by camera model. Verify via the camera's web
   UI or a one-off `ffprobe rtsp://user:pass@ip:554/<path>`.
4. **`position`** — human-readable; used only for reports and doc comments
   inside the rendered config. Pick names that match your site plan.

NVIDIA's MV3DT reference expects **overlapping fields of view** across
cameras so the fusion stage has re-identification cues. Your site layout
must match that assumption; otherwise fused tracks will drop.

### 4.3 [`laptop/mosquitto/mv3dt.conf`](../mosquitto/mv3dt.conf) — broker config

Installed to `/etc/mosquitto/conf.d/mv3dt.conf` by `10_setup_mosquitto.sh`.
"Simple testing" posture by default:

- `listener 1883 0.0.0.0` — MQTT over TCP (where the DS 9.0
  `nvmsgbroker` sink publishes via `libnvds_mqtt_proto.so`).
- `listener 9001` + `protocol websockets` — for browser-based inspectors.
- `allow_anonymous true` — **no auth**.
- `persistence false` — no on-disk queue.

**What to change, and when:**

| Change | Reason |
|--------|--------|
| Set `listener 1883 127.0.0.1` | Lock broker to localhost (production / untrusted network). |
| Add `password_file /etc/mosquitto/passwd` + `allow_anonymous false` | Require auth. Create the file with `mosquitto_passwd -c /etc/mosquitto/passwd mv3dt`. |
| Add `acl_file /etc/mosquitto/acl` | Restrict topic writes to specific users. |
| `persistence true` + `persistence_location /var/lib/mosquitto/` | Queue messages while consumer is offline. |
| Comment out `listener 9001` | Drop websockets if you don't need browser clients. |

After any edit: `sudo systemctl restart mosquitto`. The DS 9.0 side must
also receive matching creds via `gst-nvmsgbroker` config (see
[DS_plugin_gst-nvmsgbroker](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvmsgbroker.html)).

### 4.4 [`laptop/deepstream/deepstream_app_config.txt`](../deepstream/) — pipeline template

> The committed template file is in [`laptop/deepstream/`](../deepstream/).
> It contains `${CAM_USER}`, `${CAM_PASSWORD}`, `${LOCATION_ID}` placeholders
> and **is never run directly** — `40_export_watcher.sh` renders it into
> `deepstream_app_config.rendered.txt` every time new AMC artifacts land.

Structure matches the NVIDIA DS 9.0 MV3DT reference
([`DS_MV3DT.html`](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_MV3DT.html)):
one `[sourceN]` per camera, a `[primary-gie]` pointing at
`config_infer_primary.txt` (PeopleNet), a `[tracker]` block, a `[sink0]`
`nvmsgbroker` publisher targeting Mosquitto, and an optional
`[sink1]` display.

**Customize only these spots** for a new dataset:

| Edit | Where | Why |
|------|-------|-----|
| Number of `[sourceN]` blocks | top of file, one per camera | Must equal the number of **enabled** entries in `cameras.yml`. URLs are overwritten by `40_export_watcher.sh`. |
| Calibration paths | any `[mv3dt]` / tracker config block referencing `calibration/` | Re-point to `laptop/deepstream/calibration/<LOCATION_ID>/` — already automatic if you leave `${LOCATION_ID}` in. |
| `[sink0] topic=` | MQTT publish sink | Must match `MQTT_TOPIC_BASE` in `laptop.env` (default `mv3dt`). |
| `[primary-gie] model-engine-file` | if you pin a pre-built TRT engine | Default is PeopleNet FP16 rebuilt on first run (DS 9.0 removed TAO INT8 calibration). |

Do **not** edit the rendered file — it's regenerated.

---

## 5. Matching NVIDIA's MV3DT testing format

NVIDIA's DS 9.0 MV3DT reference test expects:

1. **PeopleNet detector** (Phase 10 of `00_bootstrap.sh`; see
   [DS_MV3DT §Models](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_MV3DT.html)).
2. **Per-camera calibration** produced by AMC's 6-step workflow
   ([DS_AutoMagicCalib](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_AutoMagicCalib.html)).
   Each camera needs overlapping FOV with at least one neighbor.
3. **Pipeline config shape** — `[source*]` → `[streammux]` → `[primary-gie]`
   → `[tracker]` → `[mv3dt]` → `[sink0:nvmsgbroker]` (committed template
   already follows this).
4. **MQTT output** on `mv3dt/<LOCATION_ID>/sv3d` (per-camera 3D tracks)
   and `mv3dt/<LOCATION_ID>/fused` (cross-camera fused tracks). Validate
   with `mosquitto_sub -h 127.0.0.1 -t 'mv3dt/#' -v` while the pipeline
   runs.

You are **only changing the camera list and site name** — everything else
(detector, tracker, MV3DT params, message schema) stays at NVIDIA's
reference values so your output format is byte-compatible with their
test harness.

---

## 6. Files written on disk — quick map

Inside this repo (committable or gitignored):

| Path | Owner script | Gitignored |
|------|-------------|-----------|
| `laptop/config/laptop.env` | `00_bootstrap.sh` | yes |
| `laptop/deepstream/models/peoplenet/` | `00_bootstrap.sh` (Phase 10) | yes |
| `laptop/deepstream/calibration/<LOCATION_ID>/` | `40_export_watcher.sh` | yes (parent dir kept) |
| `laptop/deepstream/deepstream_app_config.rendered.txt` | `40_export_watcher.sh` | yes |

Outside this repo:

| Path | Owner script |
|------|-------------|
| `/etc/mosquitto/conf.d/mv3dt.conf` | `10_setup_mosquitto.sh` |
| `/etc/profile.d/deepstream.sh` | `00_bootstrap.sh` |
| `$HOME/auto-magic-calib/` | `30_start_amc.sh` |
| `$HOME/auto-magic-calib/compose/.env` | `30_start_amc.sh` |
