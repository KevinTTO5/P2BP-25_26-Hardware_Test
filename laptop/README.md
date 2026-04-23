# laptop/ — DeepStream 9.0 scripted testing harness

This subtree contains the **laptop-side** scripted workflow for the MV3DT
pipeline. It is isolated from the Jetson tree at the repo root: every file
under `laptop/` is self-contained and does not read from or write to
`scripts/`, `services/`, `config/`, `models/`, `my-docs/`, `homographies/`,
or `virtual-cameras/`.

Full operator doc: [`laptop/docs/SCRIPTED-WORKFLOW.md`](docs/SCRIPTED-WORKFLOW.md)
DS 9.0 setup reference: [`laptop/docs/DEEPSTREAM-SETUP.md`](docs/DEEPSTREAM-SETUP.md)

## Prerequisites (manual, outside this repo)

Complete Notion page `337b5d58-7212-81e1-b07a-d510d9605bbb` **Sections 1–4**
as needed. You can do the full NVIDIA + DS stack with `00_bootstrap.sh` and
the pre-downloaded `.deb` files listed in
[`laptop/docs/DEEPSTREAM-SETUP.md`](docs/DEEPSTREAM-SETUP.md) §4, or install
the §1–4 pins manually and use bootstrap for the rest. Summary:

- §1–2: Hardware (Ampere-or-newer NVIDIA GPU) and BIOS.
- §3: Dual-boot Ubuntu 24.04.
- §4: Driver `590.48.01`, CUDA `13.1`, cuDNN `9.18.0`, TRT `10.14.1.48-1+cuda13.0` (exact pins in that doc).

## Minimal post-clone sequence

```bash
cd P2BP-25_26-Hardware_Test
cp laptop/config/laptop.env.example laptop/config/laptop.env
sudo bash laptop/scripts/00_bootstrap.sh
```

`00_bootstrap.sh` performs a phased install: pre-downloaded NVIDIA `.debs` (see
`docs/DEEPSTREAM-SETUP.md` §4), driver/CUDA/cuDNN/TensorRT, GStreamer, Mosquitto,
Docker, NVIDIA Container Toolkit, DeepStream 9.0 from NGC, version audit,
`laptop/config/laptop.env`, and PeopleNet ONNX into `laptop/deepstream/models/peoplenet/`.
Re-runs are resumable via `/var/lib/mv3dt-laptop-bootstrap.state`.

## Script order

| # | Script | Notion § | Purpose |
|---|--------|----------|---------|
| 00 | [`scripts/00_bootstrap.sh`](scripts/00_bootstrap.sh) | §4–§5 + §6 + §8.2 + §9.3 | Phased full stack + `laptop.env` + PeopleNet |
| 10 | [`scripts/10_setup_mosquitto.sh`](scripts/10_setup_mosquitto.sh) | §6 | Install `mv3dt.conf` into `/etc/mosquitto/conf.d/`, enable service |
| 20 | [`scripts/20_verify_cameras.sh`](scripts/20_verify_cameras.sh) | §7.5 | Ping + `ffprobe` C1..C8, print pass/fail table |
| 30 | [`scripts/30_start_amc.sh`](scripts/30_start_amc.sh) | §8.3–8.5 | Clone AMC into `$HOME/auto-magic-calib/`, `docker compose up -d`, open UI |
| — | _human_ | §8.6 | AMC 6-step workflow in the browser |
| 40 | [`scripts/40_export_watcher.sh`](scripts/40_export_watcher.sh) | §8.7 | Ingest AMC exports, render pipeline config |
| 50 | [`scripts/50_start_pipeline.sh`](scripts/50_start_pipeline.sh) | §10.1–10.2 | Start mosquitto, source DS env, launch `deepstream-app` |
| 99 | [`scripts/99_stop_all.sh`](scripts/99_stop_all.sh) | — | Stop deepstream-app, AMC compose, mosquitto |

## Layout

```
laptop/
├── README.md                  # this file
├── .gitignore                 # nested; covers laptop.env + calibration/*/
├── docs/                      # SCRIPTED-WORKFLOW.md + DEEPSTREAM-SETUP.md
├── config/                    # laptop.env.example + cameras.yml
├── mosquitto/mv3dt.conf       # broker drop-in installed by 10_setup_mosquitto.sh
├── deepstream/
│   ├── deepstream_app_config.txt    # 8 RTSP sources + MV3DT + MQTT sink (template)
│   ├── config_infer_primary.txt     # PeopleNet only (NVIDIA DS 9.0 MV3DT reference)
│   ├── config_tracker_NvMOT.yml     # NvDCF + ReID + SV3DT + MV3DT
│   ├── msgconv_config.txt
│   ├── calibration/<LOCATION_ID>/   # written by 40_export_watcher.sh (gitignored)
│   └── models/peoplenet/            # written by 00_bootstrap.sh Phase 10 (gitignored)
└── scripts/
    ├── lib/common.sh                # env loader + logging + require-tool helpers
    ├── 00_bootstrap.sh ... 99_stop_all.sh
```

## Validation

While the pipeline is running, from a second tty:

```bash
mosquitto_sub -h 127.0.0.1 -t 'mv3dt/#' -v
watch -n 1 'nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu --format=csv'
```

## Detector policy

PeopleNet is the **only** detector installed and wired into the pipeline,
matching NVIDIA's DS 9.0 MV3DT reference documentation. `yolo11n`
(ultralytics `yolo11n.pt`) is named as the _only_ approved alternative
detector for future work, but no script here installs, exports, or
configures it. See [`deepstream/config_infer_primary.txt`](deepstream/config_infer_primary.txt)
and the [`deepstream-9-docs` skill](../.cursor/skills/deepstream-9-docs/SKILL.md)
entry for `marcoslucianops/DeepStream-Yolo` when wiring it later.

## Documentation source of truth

All DS 9.0 facts in this subtree (plugin fields, MV3DT semantics, AMC
workflow, NGC tags, 9.0 breaking changes) are resolved via
[`.cursor/skills/deepstream-9-docs/SKILL.md`](../.cursor/skills/deepstream-9-docs/SKILL.md)
(Context7 `/websites/nvidia_metropolis_deepstream_dev-guide` → WebFetch →
GitHub, in that order). See [`docs/SCRIPTED-WORKFLOW.md`](docs/SCRIPTED-WORKFLOW.md)
for the end-to-end flow diagram and future-work items.
