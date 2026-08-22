# laptop/ — DeepStream 9.1 scripted testing harness

> **Developer harness — this is not the operator path.** Everything under
> `laptop/` runs from a clone of this repo and exists so the DS 9.1 stack can
> be exercised by hand. The **operator** path is a prebuilt `mv3dt-installer`
> binary downloaded from the repo's GitHub Releases page; nothing clones this
> repo onto a workstation. The `Disposition` column in
> [Script order](#script-order) records whether each script is bundled into
> that binary, dropped from it, or developer-only; the full record is
> [`installer/plan/DELETION-REVIEW.md` §8](../installer/plan/DELETION-REVIEW.md#8-script-disposition-under-the-binary-distribution).

This subtree contains the **laptop-side** scripted workflow for the MV3DT
pipeline. It is isolated from the Jetson tree at the repo root: every file
under `laptop/` is self-contained and does not read from or write to
`scripts/`, `services/`, or `models/`.

Full harness doc: [`laptop/docs/SCRIPTED-WORKFLOW.md`](docs/SCRIPTED-WORKFLOW.md)
DS 9.1 setup reference: [`laptop/docs/DEEPSTREAM-SETUP.md`](docs/DEEPSTREAM-SETUP.md)

> **Known drift:** targets below are DS 9.1; `scripts/00_bootstrap.sh` and
> `scripts/30_start_amc.sh` have not yet been updated to match (still
> DS 9.0 / the standalone AMC repo) — see
> [`docs/DEEPSTREAM-SETUP.md`](docs/DEEPSTREAM-SETUP.md) §5.2.

## Prerequisites (manual, outside this repo)

Complete Notion page `337b5d58-7212-81e1-b07a-d510d9605bbb` **Sections 1–4**
as needed. You can do the full NVIDIA + DS stack with `00_bootstrap.sh` and
the pre-downloaded `.deb` files listed in
[`laptop/docs/DEEPSTREAM-SETUP.md`](docs/DEEPSTREAM-SETUP.md) §4, or install
the §1–4 pins manually and use bootstrap for the rest. Summary:

- §1–2: Hardware (Ampere-or-newer NVIDIA GPU) and BIOS.
- §3: Dual-boot Ubuntu 24.04.
- §4: Driver `595.58.03`, CUDA `13.2`, cuDNN `9.20.0.48`, TRT `10.16.0.72-1+cuda13.2` (exact pins in that doc).

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

| # | Script | Notion § | Purpose | Disposition |
|---|--------|----------|---------|-------------|
| 00 | [`scripts/00_bootstrap.sh`](scripts/00_bootstrap.sh) | §4–§5 + §6 + §8.2 + §9.3 | Phased full stack + `laptop.env` + PeopleNet | developer-only — superseded by STEP-1 / STEP-2 |
| 10 | [`scripts/10_setup_mosquitto.sh`](scripts/10_setup_mosquitto.sh) | §6 | Install `mv3dt.conf` into `/etc/mosquitto/conf.d/`, enable service | **bundled (PLANNED)** — `assets/scripts/` copy is meant to be authoritative once U12 (`feat/installer-bundled-scripts`) merges |
| 20 | [`scripts/20_verify_cameras.sh`](scripts/20_verify_cameras.sh) | §7.5 | Ping + `ffprobe` C1..C8, print pass/fail table | **dropped** — `ffprobe` check ported to `cameras.py` |
| 30 | [`scripts/30_start_amc.sh`](scripts/30_start_amc.sh) | §8.3–8.5 | Clone AMC into `$HOME/auto-magic-calib/`, `docker compose up -d`, open UI | developer-only — superseded by STEP-3 |
| — | _human_ | §8.6 | AMC 6-step workflow in the browser | unchanged — still manual under the binary |
| 40 | [`scripts/40_export_watcher.sh`](scripts/40_export_watcher.sh) | §8.7 | Ingest AMC exports, render pipeline config | developer-only — superseded by the STEP-4 auto-ingest |
| 50 | [`scripts/50_start_pipeline.sh`](scripts/50_start_pipeline.sh) | §10.1–10.2 | Start mosquitto, source DS env, launch `deepstream-app` | developer-only — superseded by the STEP-5 per-project exe |
| 60 | [`scripts/60_record_tracking.sh`](scripts/60_record_tracking.sh) | §10.2 extension | Record `mv3dt/#` to `tracks.jsonl` / `tracks.csv` / `summary.json` | **bundled (PLANNED)** — `assets/scripts/` copy is meant to be authoritative once U12 (`feat/installer-bundled-scripts`) merges |
| 70 | [`scripts/70_plot_floorplan.py`](scripts/70_plot_floorplan.py) | sponsor artifact | Plot recorded trajectories to a PNG | **dropped** — the web app visualizes |
| 99 | [`scripts/99_stop_all.sh`](scripts/99_stop_all.sh) | — | Stop deepstream-app, AMC compose, mosquitto | developer-only — superseded by the STEP-5 per-project exe |

The unnumbered `record_cameras_mp4.sh` and `view_cameras.sh` capture helpers
are likewise **dropped** from the binary and retained as developer tools. Once
U12 lands, edit the copy under `installer/mv3dt_installer/assets/scripts/`
when changing what the binary does — see
[`DELETION-REVIEW.md` §8.1](../installer/plan/DELETION-REVIEW.md#81-which-copy-do-i-edit).

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
│   ├── config_infer_primary.txt     # PeopleNet only (NVIDIA DS 9.1 MV3DT reference)
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
matching NVIDIA's DS 9.1 MV3DT reference documentation. `yolo11n`
(ultralytics `yolo11n.pt`) is named as the _only_ approved alternative
detector for future work, but no script here installs, exports, or
configures it. See [`deepstream/config_infer_primary.txt`](deepstream/config_infer_primary.txt)
and the [`deepstream-9-docs` skill](../.cursor/skills/deepstream-9-docs/SKILL.md)
entry for `marcoslucianops/DeepStream-Yolo` when wiring it later.

## Documentation source of truth

All DS 9.1 facts in this subtree (plugin fields, MV3DT semantics, AMC
workflow, distribution tags, breaking changes) are resolved via
[`.cursor/skills/deepstream-9-docs/SKILL.md`](../.cursor/skills/deepstream-9-docs/SKILL.md)
(WebFetch → GitHub, in that order). See [`docs/SCRIPTED-WORKFLOW.md`](docs/SCRIPTED-WORKFLOW.md)
for the end-to-end flow diagram and future-work items.
