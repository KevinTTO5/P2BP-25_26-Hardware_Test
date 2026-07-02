# Step 4 — Calibration Output Placement + Config Wiring (owner: DevD)

Status: step spec. This document depends on the shared framework contracts in
[`00-FRAMEWORK-AND-BOOTSTRAP.md`](00-FRAMEWORK-AND-BOOTSTRAP.md) and does not
restate them — it links back. Read that doc first for the step-module
interface (§12), install-location config (§11), USER-ACTION display (§9),
logging/reporting (§8), and the path-prompt helper.

Module: `installer/mv3dt_installer/steps/step4_calib_output_wiring.py`
(registered in `STEP_REGISTRY` with `order = 4`; `id =
"step4_calib_output_wiring"`).

Step 4 runs **after** Step 3 (DevC) has brought up the AutoMagicCalib (AMC)
UI and a human has completed the 6-step AMC workflow through
**Results / Export** (see [`DEEPSTREAM-SETUP.md` §8.6](../../laptop/docs/DEEPSTREAM-SETUP.md)).
Its job is the product-owner requirement: once the AMC calibration file the
pipeline needs exists, **place it where it needs to go automatically**, prompt
the operator (on success) for where that file/dir should live, and **wire that
location into every DeepStream config** so the pipeline can run.

---

## 1. Scope

In scope (this step only):

1. **Ingest** the AMC calibration/MV3DT export from the user's AMC project
   into the install tree — porting the logic in
   [`laptop/scripts/40_export_watcher.sh`](../../laptop/scripts/40_export_watcher.sh)
   into a one-shot Python `run()` (watcher mode noted as an option, §4.4).
2. **Prompt** (on success) for the calibration location using the
   framework path-prompt helper with a sensible default, and **persist** the
   chosen location.
3. **Wire** the chosen location into the DeepStream configs under
   `<install_dir>/deepstream/`:
   - patch [`config_tracker_NvMOT.yml`](../../laptop/deepstream/config_tracker_NvMOT.yml)
     (`SV3DT.calibrationDirectory`, `MV3DT.nodeID` / `LOCATION_ID`);
   - render `deepstream_app_config.rendered.txt` from the committed template
     [`deepstream_app_config.txt`](../../laptop/deepstream/deepstream_app_config.txt).
4. **Verify** the calibration dir is populated and the rendered config points
   at it; report via the framework strings (§8.3).

Out of scope (deferred / other steps):

- Detector selection or model download — PeopleNet only, owned by Step 2. Step
  4 does **not** touch [`config_infer_primary.txt`](../../laptop/deepstream/config_infer_primary.txt)
  except to confirm it is referenced (§6.3).
- Launching the pipeline / building the project-named exe — that is **Step 5**
  (§7 handoff).
- Running AMC or defining the AMC project — that is the human (Step 3 brings up
  the UI; the operator drives the 6-step workflow).
- Mosquitto install/config — owned by Step 1/2 bootstrap (`mv3dt.conf`); Step 4
  only references `127.0.0.1:1883` to match the local broker.

The tracker YAML schema is **settled** (see §6.1): Step 4 uses the repo's
`SV3DT.calibrationDirectory` field and does not re-schematize to NVIDIA's
`ObjectModelProjection.cameraModelFilepath` list form. Rationale from the
product owner: the NVIDIA docs' `cameraModelFilepath` form is written for their
sample projects, whereas this repo's `calibrationDirectory` config schema is the
authoritative one for our local deployment.

---

## 2. What the AMC output is

AMC's **Results / Export** step (workflow step 6, [`DEEPSTREAM-SETUP.md`
§8.6](../../laptop/docs/DEEPSTREAM-SETUP.md)) produces the calibration artefacts
that SV3DT/MV3DT consume. Per the DS 9.0 AutoMagicCalib docs (see
[References](#references)):

- The Results screen exposes overlay images, evaluation metrics (e.g. L2
  distance / RMSE when ground truth is available), and per-camera parameters
  (projection matrix, intrinsics/extrinsics).
- Two export shapes are offered:
  - A **JSON** export with full calibration detail incl. ROI/tripwire world
    coordinates and camera parameters.
  - A **MV3DT-compatible ZIP archive** — selected on the Results step as
    **"MV3DT ZIP AMC"** or **"MV3DT ZIP VGGT"** (AMC vs VGGT solver). This is
    the artefact the tracker consumes.
- Exports land under:

  ```
  $HOME/auto-magic-calib/projects/$PROJECT_NAME/exports/
  ```

  (`$AMC_ROOT` defaults to `$HOME/auto-magic-calib`; see
  [`laptop.env.example`](../../laptop/config/laptop.env.example) `AMC_ROOT` /
  `PROJECT_NAME`).

For the MV3DT/SV3DT tracker, the consumed unit is a **directory of per-camera
calibration files** — one file per source stream (NVIDIA's reference uses
`camInfo-01.yml`, `camInfo_02.yml`, … each carrying a 3×4 world→pixel
projection matrix `projectionMatrix_3x4_w2p` and per-class `modelInfo`
height/radius). The MV3DT ZIP unpacks into this `camInfo`-style directory.
Step 4 treats the export as an **opaque directory of calibration files**: it
lands the contents under a single calibration directory and points the tracker
config at that directory. It does not parse or validate individual matrices
(that is AMC's job and is surfaced as RMSE to the human, §8).

> Schema note (RESOLVED — use the repo config schema): the committed
> [`config_tracker_NvMOT.yml`](../../laptop/deepstream/config_tracker_NvMOT.yml)
> uses `SV3DT.calibrationDirectory` + `projectionType: homography`, whereas the
> DS 9.0 Gst-nvtracker reference documents SV3DT via an
> `ObjectModelProjection.cameraModelFilepath` list of `camInfo` files. Per the
> product owner, the NVIDIA `cameraModelFilepath` form targets NVIDIA's **sample
> projects**, while the repo's `calibrationDirectory` schema is authoritative for
> **this local deployment** — so Step 4 writes `SV3DT.calibrationDirectory` and
> does **not** switch to the `cameraModelFilepath` list form. The DS
> Gst-nvtracker page is retained in [References](#references) only as background,
> not as the schema Step 4 targets.

---

## 3. Preflight (`preflight(ctx)`)

Cheap gating checks (framework §12.1). Returns `COMPLETE` (== "ok to run"),
`USER_ACTION_REQUIRED`, or `FAILED`.

1. **Step 3 complete.** If `state.status("step3_amc_launcher") != COMPLETE`,
   return `FAILED` with a message pointing at Step 3 (the framework halts
   before Step 4 anyway; this is a defensive check).
2. **Resolve project inputs** (§7 sourcing): `LOCATION_ID`, `PROJECT_NAME`,
   `AMC_ROOT`, `CAM_USER`, `CAM_PASSWORD` from `ctx.conf` / `laptop.env`
   (mirrors the `: "${VAR:?}"` guards at the top of
   [`40_export_watcher.sh`](../../laptop/scripts/40_export_watcher.sh)). Missing
   a required value → `USER_ACTION_REQUIRED` telling the operator to fill it in
   `<install_dir>/installer.conf` / `laptop.env`.
3. **AMC present.** If `$AMC_ROOT` does not exist → `FAILED` ("run Step 3 /
   `30_start_amc.sh` first"), matching the `die` in `40_export_watcher.sh`.
4. **Export exists (the key case).** Resolve
   `EXPORT_DIR = $AMC_ROOT/projects/$PROJECT_NAME/exports`. If it is missing or
   empty, the human has not finished AMC's Results/Export yet — return
   **`USER_ACTION_REQUIRED`** (§9 display) with actions:
   - "Open the AMC UI (Step 3 left it running at `http://localhost:5000`)."
   - "Complete the 6-step workflow through **Execute** and **Results /
     Export**; on Results choose **MV3DT ZIP AMC** (or **MV3DT ZIP VGGT**)."
   - "Confirm files appear under `$AMC_ROOT/projects/$PROJECT_NAME/exports/`."
   - closing framework line: "Then run the installer again to continue."
5. **Template present.** Confirm the committed
   `deepstream_app_config.txt` and `config_tracker_NvMOT.yml` templates are
   resolvable via `ctx.asset_path("deepstream", ...)` (framework §4.2) and that
   `cameras.yml` is available; else `FAILED`.

`preflight` performs no writes.

---

## 4. Ingest (port of `40_export_watcher.sh`)

`run()` first ingests the export, reusing the exact strategy in
[`40_export_watcher.sh`](../../laptop/scripts/40_export_watcher.sh)
(`ingest_exports()`), ported to Python.

### 4.1 Destination

- Ingest destination = the chosen calibration location from the §5 prompt.
  In `--non-interactive` mode the default is used without prompting.
- Default: `<install_dir>/deepstream/calibration/<LOCATION_ID>/`
  (framework install layout §11; the repo precedent is
  `laptop/deepstream/calibration/$LOCATION_ID/`). Create with `mkdir -p`.
- Because the prompt is "on success", the sequence is: ingest into the default,
  then prompt (§5); if the operator picks an alternate location, the ingested
  contents are **moved** there and the configs are wired to the final choice.
  (Equivalently, prompt first then ingest into the chosen dir — DevD picks one;
  the persisted final location is what matters. Document the chosen order in
  the module docstring.)

### 4.2 Exporter-first, copy-fallback (unchanged logic)

Mirror `ingest_exports()` precisely:

1. **Upstream exporter first.** If `$AMC_ROOT/scripts/export_mv3dt.py` exists,
   run it as the invoking user (framework §9.2 `run_as_user`):

   ```bash
   ( cd "$AMC_ROOT" && python3 scripts/export_mv3dt.py \
       --project "$PROJECT_NAME" \
       --output  "<chosen-calibration-dir>" )
   ```

   The upstream AMC repo is the run-time ground truth (DS 9.0 AutoMagicCalib
   doc). On success, mark exporter used.
2. **Raw-copy fallback.** If the exporter is missing (renamed in a newer AMC
   release) or exits non-zero, fall back to `cp -a "$EXPORT_DIR"/. <dest>/`
   (Python: `shutil.copytree(..., dirs_exist_ok=True)` / `copy2`). If a ZIP was
   produced ("MV3DT ZIP AMC/VGGT"), unpack it into `<dest>` so the tracker sees
   a flat `camInfo`-style directory.
3. If `EXPORT_DIR` is empty at this point, return `USER_ACTION_REQUIRED`
   (§3 step 4) — nothing to ingest yet.
4. Append an ingest breadcrumb (`.ingest.log` with a UTC stamp), matching the
   script.

Report each artefact touched with the framework reporters (§8.3), e.g.
`installed calibration-export version <PROJECT_NAME>@<stamp>` /
`already installed calibration-export version <...>` when re-running
idempotently.

### 4.3 Idempotency

Re-running Step 4 is safe: the copy/unpack is a no-op overwrite into the same
`<dest>`, the render is regenerated, and the state machine skips Step 4 once it
is `COMPLETE` (framework §6). Matches the "idempotent" contract in the
`40_export_watcher.sh` header.

### 4.4 One-shot vs watcher

- **Installer context = one-shot.** Step 4 does the copy+render pass **once**
  and returns (equivalent to `40_export_watcher.sh --oneshot`). The installer
  must not block on a long-running `inotifywait` loop inside `run()`.
- **Watcher option (noted, not default).** The long-running watch mode
  (`inotifywait`, 5 s polling fallback) remains available as the standalone
  `40_export_watcher.sh` for operators who want to iterate on AMC exports after
  install. Step 4 may print a hint pointing at it. It is not invoked by the
  installer flow.

---

## 5. The prompt (on success)

After a successful ingest, prompt for where the calibration output should live,
using the **framework path-prompt helper** (the "GUI-like path selection with a
default", framework §2 / §11.1 — the same affordance used for `--install-dir`).

- **Prompt text:** "Choose where the calibration output for this project should
  live:".
- **Default:** `<install_dir>/deepstream/calibration/<LOCATION_ID>/`.
- **Alternate allowed:** any absolute path the operator types; Step 4 creates
  it (`mkdir -p`, chowned to the invoking user, framework §9.2) and moves the
  ingested contents there.
- **`--non-interactive` / `--no-pause`:** skip the prompt, use the default
  silently (framework §3.3).

### 5.1 Persistence

The chosen location is persisted so Step 5 and re-runs can find it:

- Written into `<install_dir>/installer.conf` as `CALIBRATION_DIR=<abspath>`
  (KEY=VALUE, the `set -a; . installer.conf` shape from framework §11.2). Steps
  read it via `config.load()`; they never hardcode it.
- Steps never write `state.json` directly (framework §12.2); if the location
  needs to live in state, DevD returns it in the `StepResult.message` /
  relies on the framework's config persistence. `installer.conf` is the shared
  source of truth for bash-facing consumers.

---

## 6. Wire the location into the configs (the crux)

All rendered outputs are written under `<install_dir>/deepstream/` (framework
§11.2 layout). The committed templates are the bundled assets
(`ctx.asset_path("deepstream", ...)`); the originals are never mutated in place
— they are read as templates and rendered to new files, exactly as
`40_export_watcher.sh` leaves `deepstream_app_config.txt` untouched and writes
`.rendered.txt`.

### 6.1 `config_tracker_NvMOT.yml`

Patch a copy written to `<install_dir>/deepstream/config_tracker_NvMOT.yml`.
Templated fields:

| Field | Set to | Notes |
|-------|--------|-------|
| `SV3DT.calibrationDirectory` | the chosen calibration dir | Repo template default is `calibration/${LOCATION_ID}` (relative to the `deepstream/` working dir). If the operator kept the default location, keep it **relative** (`calibration/<LOCATION_ID>`) so `ll-config-file` resolution stays working-dir-relative; if an alternate absolute path was chosen, write the absolute path. |
| `MV3DT.nodeID` | `<LOCATION_ID>` | Template has `nodeID: ${LOCATION_ID}`. This is the MV3DT node identity on the broker. |

Left **as-is** (verified against DS 9.0 MV3DT / Gst-nvtracker, do not touch):

- `SV3DT.projectionType: homography` — locked; the 8-camera ceiling-mount rig
  is a homography (ground-plane) projection.
- `MV3DT.mqttBrokerIP: 127.0.0.1` / `mqttBrokerPort: 1883` — must match the
  local Mosquitto listener from `mv3dt.conf`
  ([`DEEPSTREAM-SETUP.md` §6](../../laptop/docs/DEEPSTREAM-SETUP.md)); leave as
  the local broker.
- `SV3DT.enable: 1`, `MV3DT.enable: 1`, `fusionUpdateRate`,
  `globalIDNegotiationTimeout`, ReID `modelEngineFile`, and all
  `BaseConfig`/`TargetManagement`/`DataAssociator`/`StateEstimator`/
  `VisualTracker` blocks — tuning, not location wiring.

> `LOCATION_ID` here is the single knob that ties the calibration directory
> name, the MV3DT `nodeID`, and the MQTT topic (`mv3dt/<LOCATION_ID>/sv3d`)
> together — keep all three consistent.

### 6.2 `deepstream_app_config.rendered.txt`

Render from the committed template
[`deepstream_app_config.txt`](../../laptop/deepstream/deepstream_app_config.txt)
to `<install_dir>/deepstream/deepstream_app_config.rendered.txt`, reusing the
`render_pipeline()` Python in
[`40_export_watcher.sh`](../../laptop/scripts/40_export_watcher.sh) (lines
128–222). Substitutions:

| Placeholder / field | Rendered value | Source |
|---------------------|----------------|--------|
| `${CAM_USER}` | camera RTSP user | `laptop.env` `CAM_USER` |
| `${CAM_PASSWORD}` | camera RTSP password | `laptop.env` `CAM_PASSWORD` |
| `${LOCATION_ID}` | site id (used in `[sink1] topic=mv3dt/${LOCATION_ID}/sv3d`) | `laptop.env` `LOCATION_ID` |
| each `[sourceN] uri=` | `rtsp://<user>:<pass>@<ip>:554<rtsp_path>` | rewritten per enabled entry in [`cameras.yml`](../../laptop/config/cameras.yml), preserving YAML order (`rewrite_source_uris()`) |

Left **as-is** in the rendered file (these already point the pipeline at the
tracker config, which is what carries the calibration dir — so the calibration
wiring flows through §6.1, not through direct edits here):

- `[tracker] ll-config-file=config_tracker_NvMOT.yml` — relative reference to
  the patched YAML in the same `deepstream/` dir; the `[tracker] ll-lib-file`
  path to `libnvds_nvmultiobjecttracker.so`.
- `[primary-gie] config-file=config_infer_primary.txt` (PeopleNet, §6.3).
- `[sink1] msg-conv-config=msgconv_config.txt`, `msg-broker-conn-str=127.0.0.1;1883`,
  `msg-broker-proto-lib=.../libnvds_mqtt_proto.so` — match the local broker.
- `[streammux] batch-size=8`, `[primary-gie] batch-size=8` — 8-camera rig.

A generated header (as in the script) records the source template, the
calibration dir, and `LOCATION_ID`, plus "Edit the committed template, not this
file; it is regenerated."

### 6.3 `config_infer_primary.txt` (PeopleNet) — reference only

Step 4 does **not** template or edit
[`config_infer_primary.txt`](../../laptop/deepstream/config_infer_primary.txt).
It is PeopleNet-only (locked detector policy) and its model artefacts are
placed by Step 2. Step 4 only relies on the fact that the rendered
`deepstream_app_config.rendered.txt` still references it via
`[primary-gie] config-file=config_infer_primary.txt`, and copies it verbatim
into `<install_dir>/deepstream/` if not already present so the rendered app
config resolves it relatively. No fields change.

### 6.4 `msgconv_config.txt` — reference only

[`msgconv_config.txt`](../../laptop/deepstream/msgconv_config.txt) seeds the
`[sensor0]`/`[place0]`/`[analytics0]` payload for the MQTT sink. Step 4 does
**not** template it (its `id`/`location` fields are static harness metadata,
not calibration-derived). It is copied verbatim into `<install_dir>/deepstream/`
so `[sink1] msg-conv-config=msgconv_config.txt` resolves relatively. If a
future iteration wants per-site sensor metadata, that is flagged extra scope.

### 6.5 Output tree

After `run()`:

```
<install_dir>/deepstream/
├── config_tracker_NvMOT.yml            # patched: calibrationDirectory + nodeID
├── deepstream_app_config.rendered.txt  # rendered: creds + LOCATION_ID + RTSP URIs
├── config_infer_primary.txt            # copied verbatim (PeopleNet, Step 2 owns model)
├── msgconv_config.txt                  # copied verbatim
└── calibration/
    └── <LOCATION_ID>/                  # ingested AMC export (or the alternate chosen dir)
```

All files chowned to the invoking user (framework §9.2).

---

## 7. LOCATION_ID / PROJECT_NAME sourcing + Step 5 handoff

- **`PROJECT_NAME`** is the AMC project the human named in the AMC GUI (AMC
  workflow step 1, [`DEEPSTREAM-SETUP.md` §8.6](../../laptop/docs/DEEPSTREAM-SETUP.md)),
  mirrored into [`laptop.env`](../../laptop/config/laptop.env.example)
  `PROJECT_NAME` (defaults to `LOCATION_ID`). It selects the AMC export dir
  (`$AMC_ROOT/projects/$PROJECT_NAME/exports/`).
- **`LOCATION_ID`** is the site/deployment id from `laptop.env` `LOCATION_ID`.
  It names the calibration subdir, the MV3DT `nodeID`, and the MQTT topic. The
  `40_export_watcher.sh` default is `PROJECT_NAME := LOCATION_ID` when unset.
- **Handoff to Step 5:** Step 4 persists `CALIBRATION_DIR`, and the final
  rendered `deepstream_app_config.rendered.txt` + patched
  `config_tracker_NvMOT.yml` paths, into `<install_dir>/installer.conf` /
  state. **Step 5** ("Per-project exes", DevD) builds the project-named
  DeepStream launcher that `cd`s into `<install_dir>/deepstream/` and runs
  `deepstream-app -c deepstream_app_config.rendered.txt` — the exe is named
  after `PROJECT_NAME` and consumes exactly the artefacts Step 4 produced. Step
  4 must therefore leave the rendered config runnable and self-consistent
  (relative references intact).

---

## 8. Verify (`verify(ctx)`) + failure surfaces

`verify()` is idempotent (framework §12.1) and returns `COMPLETE` only when all
checks pass; it uses `verify_pinned` where a pinned value applies and the §8.3
reporters for what it confirms.

Checks:

1. **Calibration dir populated.** The chosen calibration dir exists and is
   non-empty (contains at least one calibration file from the AMC export). Empty
   → not complete.
2. **Tracker config wired.** `<install_dir>/deepstream/config_tracker_NvMOT.yml`
   parses as YAML and `SV3DT.calibrationDirectory` resolves to the chosen dir
   and `MV3DT.nodeID == LOCATION_ID`. `projectionType == homography` and
   `mqttBrokerIP/Port == 127.0.0.1/1883` are asserted unchanged (use
   `verify_pinned("MV3DT.projectionType", actual, "homography")`).
3. **Rendered app config references calibration.** The rendered file exists, no
   `${...}` placeholders remain (creds + `LOCATION_ID` all substituted), every
   enabled camera has a concrete `rtsp://` `uri=`, and
   `[tracker] ll-config-file=config_tracker_NvMOT.yml` still points at the
   patched YAML (so the calibration dir is reachable through it).
4. **Referenced siblings present.** `config_infer_primary.txt` and
   `msgconv_config.txt` exist alongside the rendered app config so relative
   references resolve.

`report()` prints a summary block: chosen calibration dir, number of calibration
files ingested, exporter-vs-copy path used, and the rendered/patched file paths
(no side effects).

### 8.1 User-action / failure surfaces (framework §9.3)

- **Export missing / empty** → `USER_ACTION_REQUIRED` (finish AMC Results /
  Export first; §3 step 4). This is the primary "AMC not done yet" surface the
  framework contract calls out.
- **Poor calibration (bad RMSE)** → Step 4 cannot judge calibration quality
  itself, but if the ingest produced an obviously incomplete export (e.g. fewer
  camInfo files than enabled cameras), surface `USER_ACTION_REQUIRED`: "Re-run
  AMC **Execute** and re-export — the calibration looks incomplete / RMSE was
  rejected on the Results screen," then re-verify on next launch.
- **Exporter present but failing** → logged warning, fall back to raw copy
  (§4.2); only `FAILED` if both exporter and copy yield nothing.
- **Config parse/render error** (bad `cameras.yml`, missing template) →
  `FAILED` with the offending path.
- No reboots are ever required by Step 4.

---

## References

DeepStream 9.0 official documentation (facts cross-checked via Context7 library
`/websites/nvidia_metropolis_deepstream_dev-guide`). DS 9.0 only.

- DS 9.0 AutoMagicCalib (Results/Export → JSON + **MV3DT ZIP AMC / MV3DT ZIP
  VGGT**; ROI/tripwire world coords + camera params):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_AutoMagicCalib.html>
- DS 9.0 Multi-View 3D Tracking (MV3DT) (custom-dataset `videos`/`camInfo`
  layout, `deepstream_auto_configurator.py`, MV3DT integration):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_MV3DT.html>
- DS 9.0 Gst-nvtracker (SV3DT `ObjectModelProjection.cameraModelFilepath`,
  `projectionMatrix_3x4_w2p`, `modelInfo` — the §2/§6.1 schema note):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvtracker.html>

Repo files referenced:

- [`laptop/scripts/40_export_watcher.sh`](../../laptop/scripts/40_export_watcher.sh)
  — ingest + render logic this step ports to a one-shot Python `run()`.
- [`laptop/deepstream/config_tracker_NvMOT.yml`](../../laptop/deepstream/config_tracker_NvMOT.yml)
  — tracker YAML patched with `calibrationDirectory` / `nodeID`.
- [`laptop/deepstream/deepstream_app_config.txt`](../../laptop/deepstream/deepstream_app_config.txt)
  — committed template rendered to `deepstream_app_config.rendered.txt`.
- [`laptop/deepstream/config_infer_primary.txt`](../../laptop/deepstream/config_infer_primary.txt)
  — PeopleNet (reference only; not edited).
- [`laptop/deepstream/msgconv_config.txt`](../../laptop/deepstream/msgconv_config.txt)
  — msgconv payload seed (reference only; not edited).
- [`laptop/config/laptop.env.example`](../../laptop/config/laptop.env.example)
  — `LOCATION_ID` / `PROJECT_NAME` / `AMC_ROOT` / `CAM_USER` / `CAM_PASSWORD`
  sourcing.
- [`laptop/config/cameras.yml`](../../laptop/config/cameras.yml) — per-camera
  RTSP URIs for `[sourceN]` rewrite.
- [`laptop/docs/DEEPSTREAM-SETUP.md`](../../laptop/docs/DEEPSTREAM-SETUP.md)
  §8.6 (AMC workflow), §8.7 (export ingest), §9 (MV3DT layout / tracker YAML).
- [`00-FRAMEWORK-AND-BOOTSTRAP.md`](00-FRAMEWORK-AND-BOOTSTRAP.md) — step
  interface (§12), install-location config + path prompt (§11), USER-ACTION
  display (§9), logging/reporting (§8).
