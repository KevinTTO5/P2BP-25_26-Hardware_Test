# DELETION-REVIEW — Repo triage for the installer port (owner: shared)

Status: review artifact and standing record. It depends on the harvest
specified in [`STEP-7` §B–§F](STEP-7-WEBAPP-INTEGRATION.md) and
[`00` §14](00-FRAMEWORK-AND-BOOTSTRAP.md#14-web-app-connection-contract), and
does **not** restate those contracts — it links back.

**Execution state:**

| Section | State |
|---|---|
| [§2](#2-immediate-deletions-no-harvest-required) immediate deletions | **EXECUTED** — approved and removed |
| [§4](#4-explicit-calls-resolved--both-deleted) the two judgment calls | **EXECUTED** — both resolved to delete; camera facts harvested first ([§4.1](#41-camera-facts-harvested-before-deletion)) |
| [§3](#3-deletions-gated-on-the-harvest-the-jetson-tree) the Jetson tree | **EXECUTED** — gate verified against the sources immediately before removal ([§3.2](#32-what-must-be-captured-before-these-files-go)); `CLAUDE.md` rewritten per [§3.3](#33-documentation-that-must-change-in-the-same-commit) |

The triage is complete: **14,692 files removed**, working tree down from ~1 GB
to ~23 MB. What remains is `laptop/` (the DeepStream harness), `installer/`
(these specs), `CLAUDE.md`, and `README.md`.

Everything removed remains recoverable via `git revert` or from the parent
repository; no history rewrite has been performed, so `.git` is still ~892 MB
(see the reclaim note in [§2](#2-immediate-deletions-no-harvest-required)).

> **Counting correction.** `homographies/` was originally scored as "14
> `*.yml`". It held **15** files — a `4p-ground-truth.txt` was missed because
> the survey filter excluded `*.txt`. Same verdict (EPFL dataset sample, zero
> references), but worth recording: that filter is also what initially hid
> `laptop/deepstream/*.txt` from the survey. Any future triage should enumerate
> by `git ls-files` without extension filters.

This repo is a **fork** (`KevinTTO5/P2BP-25_26-Hardware_Test`, single `origin`,
no upstream configured) and the work is moving to the DeepStream workstation.
Every file removed here survives in the parent repo, so the governing question
is not "is deletion safe?" but **"does this file contribute code or a pattern
the workstation plan needs — above all, for talking to the web app?"**

The ordering constraint that remains is **doc coherence, not runtime safety**:
files cited by `installer/plan/*.md` as the ported source must not disappear
before the port is written, or the specs lose the implementation they were
written against. Hence two tiers — [§2](#2-immediate-deletions-no-harvest-required)
(unblocked) and [§3](#3-deletions-gated-on-the-harvest-the-jetson-tree) (gated).

---

## 1. Scope and the deletion test

**REQUIRED** — every file in the repo is placed in exactly one of four
categories by the single test below. A file is retained only if it earns
retention; "might be useful later" is not a category, because the parent repo
already serves that purpose.

| Category | Test | Outcome |
|---|---|---|
| **HARVEST** | Contributes code or a pattern the workstation plan needs | Ported into a named plan section, *then* the source is deleted ([§3](#3-deletions-gated-on-the-harvest-the-jetson-tree)) |
| **DELETE-NOW** | No references anywhere; nothing to harvest | Removed on sign-off ([§2](#2-immediate-deletions-no-harvest-required)) |
| **RETAIN** | Cited by a plan doc, bundled as an installer asset, or unported tooling | Kept ([§5](#5-retained-files-and-why)) |
| **CALL** | Retention is a genuine judgment call, not a mechanical one | Human decides ([§4](#4-explicit-calls-resolved--both-deleted)) |

Reference checks backing the DELETE verdicts were run with `grep -rIn` across
`*.py`, `*.sh`, `*.md`, `*.yml`, and `*.ps1`, excluding the `amc/` tree itself.

---

## 2. Immediate deletions (no harvest required)

These are unblocked: no plan doc cites them, no code path reads them, and there
is no pattern in them worth porting.

| Target | Size / count | Verdict basis |
|---|---|---|
| `amc/alignment_data (Copy)/`, `amc/single_view_results (Copy)/` | **964 MB, 14,606 tracked files** | AMC run output — `kitti_detector/` per-frame label dumps, overlay JPGs, per-camera YAMLs. The `(Copy)` suffixes mark a manual paste, not a curated fixture. [`STEP-4` §2](STEP-4-CALIB-OUTPUT-WIRING.md#2-what-the-amc-output-is) treats the AMC export as an opaque directory produced **at run time** under `$AMC_ROOT/projects/$PROJECT_NAME/exports/`, never read from the repo tree. |
| `homographies/` (whole directory) | 15 files | EPFL multi-camera dataset samples — 14 `*-homography.yml` (`4p-c0`, `campus4-c*`, `passageway1-c*`, `terrace1-c*`) plus `4p-ground-truth.txt` (2,957 lines of frame/track/bbox annotations). The runtime resolved homographies **by MAC** — `camera_handler.py:241`, `aruco_scanner.py:101`, and `homography.py:760` all used `{safe_mac}_homography.yml`. No committed file matched that scheme, so nothing ever read these. |
| `.DS_Store` | 6 KB | macOS Finder artifact. Also absent from `.gitignore` — add it there in the same change. |
| `virtual-cameras/` (whole directory, incl. the 48 MB `mediamtx.exe`) | **48 MB** | Windows binary in a repo whose installer targets Ubuntu 24.04 `x86_64` ([`00` §5.1](00-FRAMEWORK-AND-BOOTSTRAP.md#51-exact-responsibilities-in-order)); also exposed to line-ending mangling (see the hazard note below). Both POSIX launchers already used the `bluenviron/mediamtx:latest` **Docker image** (`start_rtsp_cams.sh:22-24`) and the Windows launcher already fell back to it (`start_rtsp_cams.ps1:35-36`), so the whole directory is replaceable by one `docker run` — see [§4](#4-explicit-calls-resolved--both-deleted) item 2. |
| `config/config.json`, `config/cameras_runtime.json` | small | Committed before `config/` was added to `.gitignore`, so they still override the ignore rule. Device-specific runtime state — `cameras_runtime.json` is written by `camera_scanner.py`. **Harvest first:** these two files were the sole source of the fleet MAC inventory and the native sensor resolution — extracted to `cameras.yml` per [§4.1](#41-camera-facts-harvested-before-deletion) before removal. |

> **Line-ending hazard (fix with the same change):** `.gitattributes` sets
> `* text=auto eol=lf` and does **not** list `*.exe` as binary, so
> `mediamtx.exe` is currently exposed to line-ending normalization. Deleting it
> removes the hazard; if any `.exe` is ever re-added, add `*.exe binary` first.

**Reclaim note:** deleting `amc/` at `HEAD` does not shrink existing clones —
the objects stay in history. Reclaiming the 964 MB requires a history rewrite,
which is a **separate decision** and is not proposed here.

---

## 3. Deletions gated on the harvest (the Jetson tree)

**Gate (REQUIRED):** proceed only once
[`STEP-7`](STEP-7-WEBAPP-INTEGRATION.md) and
[`00` §14](00-FRAMEWORK-AND-BOOTSTRAP.md#14-web-app-connection-contract) are
written **and** the five critical patterns in
[§3.2](#32-what-must-be-captured-before-these-files-go) verify. Until then
these files are the reference implementation the specs are written against.

### 3.1 What is removed

| Path | Contents |
|---|---|
| `scripts/` | 17 modules, ~5,700 lines — every pattern worth keeping is harvested into `STEP-7` / `00` §14 first |
| `scripts/json_models/` | 6 DTO modules; the `cloud_storage` and `heartbeat_payload` field sets are inlined into `STEP-7` §C/§D before removal |
| `scripts/camera_controllers/` | `annke_controller.py` — see [§4](#4-explicit-calls-resolved--both-deleted) |
| `services/` | 11 systemd units + the logrotate drop-in; `tracker.path` / `tracker-toggle.service` are inlined into [`STEP-6` §B.1](STEP-6-REMOTE-SUPERVISION.md#b1-the-agent-systemd-unit) first |
| `models/` | 22 MB OSNet ReID weights + encoder — DeepStream supplies its own ReID engine via `config_tracker_NvMOT.yml` |
| `install.sh` | Jetson installer; its credential-capture (`42-109`) and unit-enable (`147-207`) logic is ported to `00` §14 / [`STEP-6` §A.4](STEP-6-REMOTE-SUPERVISION.md#a4-installer-integration-mirror-installsh) |
| `requirements.txt` | `ultralytics` / `torch` / `playwright` — Jetson tracker dependencies with no workstation role |
| `config/`, `homographies/` | Runtime state and dead sample data ([§2](#2-immediate-deletions-no-harvest-required)) |

Rationale, per subsystem: these implement per-camera YOLO+OSNet tracking on
Jetson hardware. The DeepStream MV3DT pipeline replaces that function entirely,
and no step doc in `installer/plan/` targets a Jetson.

### 3.2 What must be captured before these files go

**REQUIRED** — these five behaviors are the non-obvious ones. If a plan doc
merely *names* the source file rather than describing the behavior, the harvest
has failed and deletion must not proceed.

- [ ] **Retry predicate** — retry on `429` and `5xx` only; fail fast on every
      other `4xx` (`cloud_storage_media.py:197-245`).
- [ ] **mtime edge-trigger** — fire a one-shot operation only when the config
      file is *freshly written* AND the flag is true, so a stale `true` never
      re-fires (`aruco_scanner.py:307-345`).
- [ ] **Signed-URL redaction** — strip everything from `?` onward before any
      URL reaches a log or transcript (`cloud_storage_media.py:46-51`).
- [ ] **Size + mtime dedupe with per-file cooldown** — re-upload only on a
      changed fingerprint; back off after repeated failures
      (`tracking_uploader.py:64-104`).
- [ ] **Exact DTO field names** — the PascalCase wire format the backend
      expects (`json_models/cloud_storage.py`, `json_models/heartbeat_payload.py`).

### 3.3 Documentation that must change in the same commit

[`CLAUDE.md`](../../CLAUDE.md) describes "**two independent subsystems**" and
instructs contributors to treat `scripts/` and `laptop/scripts/` as "separate
worlds". Both statements become false the moment this section executes. The
rewrite is specified in the plan and must land atomically with the deletion, or
the repo ships instructions referring to directories that no longer exist.

---

## 4. Explicit calls (RESOLVED — both deleted)

Two retention questions were genuine judgment rather than mechanical
application of [§1](#1-scope-and-the-deletion-test). **Both are RESOLVED:
delete.** The governing criterion, from the repo owner, was:

> Keep only what pertains to the workstation — for cameras, that means MAC
> addresses or information *about* the cameras that benefits the new plan.

That criterion is narrower than "keep the code" and is what decided both: the
*facts* the camera tooling encoded are worth keeping, the *tooling* is not.
Those facts were harvested into
[`laptop/config/cameras.yml`](../../laptop/config/cameras.yml) before deletion
— see [§4.1](#41-camera-facts-harvested-before-deletion).

1. **`scripts/camera_controllers/annke_controller.py` (1,533 lines).**
   Automated camera web-UI control — activation and OSD-text disable. The plans
   **deliberately** place per-camera network and stream configuration out of
   scope as manual work via each camera's web UI, in both
   [`00` §13](00-FRAMEWORK-AND-BOOTSTRAP.md#13-out-of-scope--defer-to-human)
   and [`STEP-5` §9](STEP-5-PER-PROJECT-EXES.md#9-out-of-scope--flag-for-human).
   This file is the only asset in the repo that could close that gap.
   **RESOLVED: delete.** The scope call was made twice and independently, and
   it is Playwright-driven browser automation — a heavyweight dependency
   (`playwright` + a browser download) for work the plans assign to a human.
   What mattered in it was never the automation but the four facts it encoded
   about the hardware; those are now in `cameras.yml`
   ([§4.1](#41-camera-facts-harvested-before-deletion)), including the manual
   procedure it automated, so the capability is documented rather than lost.

2. **`virtual-cameras/`.** Serves RTSP test streams via `mediamtx`, which made
   it a candidate for exercising Steps 5–7 without cameras attached.
   **RESOLVED: delete the whole directory**, not just `mediamtx.exe`. It sits
   outside `laptop/`, no plan doc references it, and its function is replaceable
   in one command — `mediamtx` runs from the upstream Docker image
   (`bluenviron/mediamtx:latest`), which is exactly what the shell launchers did
   anyway. Keeping a 48 MB vendored binary plus three launcher scripts to avoid
   one `docker run` is not a trade worth making in a workstation-only tree.

### 4.1 Camera facts harvested before deletion

Applying the "keep information *about* the cameras" criterion surfaced three
facts that existed **nowhere else in the repo** and were about to be deleted
with `config/` and the camera controller. All are now recorded in the header of
[`laptop/config/cameras.yml`](../../laptop/config/cameras.yml), the retained
camera inventory:

| Fact | Was only in | Why it benefits the new plan |
|---|---|---|
| **Fleet MAC inventory** — 8 cameras, OUI `d0:3b:f4` | `config/config.json` (`TrackingCameras`) | The MAC is the stable per-camera identity; `cameras.yml` previously listed IPs only |
| **Native sensor resolution 3072x1728** | `config/cameras_runtime.json` | `deepstream_app_config.txt` sets `[streammux] width=1920 height=1080`, so the pipeline **downscales** every source — a deliberate throughput choice that was nowhere written down |
| **Vendor/firmware + manual pre-flight** — ANNKE on Hikvision-OEM firmware; cameras ship un-activated; OSD text must be disabled | `annke_controller.py` | The OSD overlay is burned into the encoded stream, so leaving it on feeds timestamp/name pixels into PeopleNet detection and AMC feature matching |

A fourth finding is recorded there as a caveat rather than a fact: the
`169.254.0.0/16` link-local IPs pinned in `cameras.yml` are **volatile**. A
snapshot of four of these cameras in `cameras_runtime.json` held a completely
disjoint IP set from the eight pinned below it, and the addresses are not
derivable from the MAC. This is why the camera-node stack discovered by MAC
instead of trusting a static list — and it is a live risk for the new plan,
whose `cameras.yml` *is* a static list. Flagged, not solved.

> **No MAC-to-position mapping survived.** `config.json` keyed cameras by MAC,
> `cameras.yml` keys them by IP and position, and the two shared no common
> field. The MAC inventory is preserved; re-derive the mapping with
> `arp-scan -l` on the camera LAN if it is needed.

---

## 5. Retained files and why

| File(s) | Reason for retention |
|---|---|
| `laptop/deepstream/*`, `laptop/config/*`, `laptop/mosquitto/mv3dt.conf` | Bundled as PyInstaller **data assets** by [`00` §4.1](00-FRAMEWORK-AND-BOOTSTRAP.md#41-what-builds-the-binary). Permanent. |
| `laptop/docs/*`, `laptop/README.md` | Cited 22+ times across the plans; `DEEPSTREAM-SETUP.md` is the source of truth for the version pins in [`STEP-1` §2](STEP-1-PREREQUISITES.md#2-the-ds-90-dgpu-prerequisite-pins-equality). |
| `laptop/scripts/00_bootstrap.sh`, `lib/common.sh`, `30_start_amc.sh`, `50_start_pipeline.sh`, `99_stop_all.sh` | Ported by Steps 1, 2, 3, 5, and 6. Deletable on the same gate as [§3](#3-deletions-gated-on-the-harvest-the-jetson-tree), but the gate there is the *installer implementation*, not this documentation work — **out of scope for this triage**. |
| `laptop/scripts/40_export_watcher.sh` | **Never deletable.** [`STEP-4` §4.4](STEP-4-CALIB-OUTPUT-WIRING.md#44-one-shot-vs-watcher) retains the long-running `inotifywait` watch mode as a standalone script; only the one-shot path is ported. |
| `laptop/scripts/10_setup_mosquitto.sh`, `20_verify_cameras.sh` | No longer load-bearing — gaps 1 and 2 in [§6](#6-coverage-gaps-this-triage-exposed) are closed — but retained in git as developer tools. |
| `laptop/scripts/60_record_tracking.sh`, `70_plot_floorplan.py`, `record_cameras_mp4.sh`, `view_cameras.sh` | Sponsor-demo and validation tooling with no plan coverage — and the **producers of the artifacts** [`STEP-7` §E](STEP-7-WEBAPP-INTEGRATION.md#e-artifact-upload-daemon) uploads. |

---

## 6. Coverage gaps this triage exposed

Surfaced by checking which `laptop/scripts/` entries no step doc claims. Each
is a spec gap, not a deletion candidate — recorded here so the retention above
has a stated reason and the gap is not lost.

1. **RESOLVED — Mosquitto install is owned by Step 1.**
   [`STEP-1` §3.2](STEP-1-PREREQUISITES.md#32-mosquitto-broker) now owns
   installing the `mosquitto` broker daemon and its `mv3dt.conf` drop-in,
   closing the gap that used to leave
   [`STEP-6` §E.1](STEP-6-REMOTE-SUPERVISION.md#e1-lifecycle) `preflight`
   requiring a reachable broker nothing installed.
   `10_setup_mosquitto.sh` is no longer load-bearing.

2. **RESOLVED — camera verification is fully ported, split across two
   steps.**
   [`STEP-5` §3.3](STEP-5-PER-PROJECT-EXES.md#33-what-the-exe-does-at-runtime-pipeline-subcommand)
   ports the shallow ping sweep from `20_verify_cameras.sh`; the
   `ffprobe`-over-RTSP check is ported into camera discovery
   ([`00` §15](00-FRAMEWORK-AND-BOOTSTRAP.md#15-camera-discovery)), which
   records the result as a `stream_ok` field on each inventory entry.
   `20_verify_cameras.sh` is no longer load-bearing.

3. **The export watcher is not superseded.** Recorded in
   [§5](#5-retained-files-and-why) and made explicit in
   [`STEP-4` §4.4](STEP-4-CALIB-OUTPUT-WIRING.md#44-one-shot-vs-watcher) so the
   script is not removed on the assumption that Step 4 replaced it.

---

## 7. Execution and rollback

Ordered, one commit per group, so any step can be reverted independently:

1. **Harvest** — write [`STEP-7`](STEP-7-WEBAPP-INTEGRATION.md) and
   [`00` §14](00-FRAMEWORK-AND-BOOTSTRAP.md#14-web-app-connection-contract);
   apply the `STEP-4` / `STEP-5` / `STEP-6` edits.
2. **Verify the gate** — confirm every checkbox in
   [§3.2](#32-what-must-be-captured-before-these-files-go), then re-run the
   reference check immediately before removing anything:

   ```bash
   grep -rIn "homographies/4p-\|single_view_results\|alignment_data\|mediamtx.exe" \
     --include='*.py' --include='*.sh' --include='*.md' --include='*.yml' \
     --include='*.ps1' . | grep -v '^./amc/'
   ```

   Expect no hits outside `amc/` itself and `start_rtsp_cams.ps1`.
3. **Commit A** — [§2](#2-immediate-deletions-no-harvest-required) deletions
   plus the `.gitignore` addition of `.DS_Store`.
4. **Commit B** — [§3](#3-deletions-gated-on-the-harvest-the-jetson-tree)
   deletions plus the [`CLAUDE.md`](../../CLAUDE.md) rewrite, atomically.
5. **Confirm isolation holds** — `laptop/` and `installer/` must not reference
   any removed path:

   ```bash
   grep -rn "\.\./scripts/\|\.\./services/\|\.\./models/\|\.\./config/" laptop/ installer/
   bash -n laptop/scripts/*.sh && echo "laptop scripts parse OK"
   ```

**Rollback:** every deletion is recoverable via `git revert` of the relevant
commit, or from the parent repository. No history rewrite is performed, so no
deletion in this document is destructive to the object store.

---

## References

Authority for this document is the repo itself: the verdicts rest on reference
checks against the working tree and on the scope decisions recorded in the
`installer/plan/` specs. No external documentation is cited — where a DeepStream
fact underlies a verdict, it is cited in the plan doc linked instead.

Repo files referenced:

- [`00-FRAMEWORK-AND-BOOTSTRAP.md`](00-FRAMEWORK-AND-BOOTSTRAP.md) — §4.1 asset
  bundling, §5.1 target platform, §13 out-of-scope scope calls, and §14 the
  web-app credential contract that gates [§3](#3-deletions-gated-on-the-harvest-the-jetson-tree).
- [`STEP-4-CALIB-OUTPUT-WIRING.md`](STEP-4-CALIB-OUTPUT-WIRING.md) — §2 treats
  the AMC export as run-time-produced (basis for deleting `amc/`); §4.4 retains
  the export watcher permanently.
- [`STEP-5-PER-PROJECT-EXES.md`](STEP-5-PER-PROJECT-EXES.md) — §3.3 the partial
  camera-verification port; §9 the per-camera-config scope call.
- [`STEP-6-REMOTE-SUPERVISION.md`](STEP-6-REMOTE-SUPERVISION.md) — §A.4 and
  §B.1 consume `install.sh` and the `services/` unit conventions before those
  files are removed; §D and §E.1 are why Mosquitto setup is load-bearing.
- [`STEP-7-WEBAPP-INTEGRATION.md`](STEP-7-WEBAPP-INTEGRATION.md) — the harvest
  target for every web-app pattern in the Jetson tree; the gate on
  [§3](#3-deletions-gated-on-the-harvest-the-jetson-tree).
- [`CLAUDE.md`](../../CLAUDE.md) — the "two independent subsystems" framing that
  must be rewritten atomically with the Jetson-tree deletion ([§3.3](#33-documentation-that-must-change-in-the-same-commit)).
