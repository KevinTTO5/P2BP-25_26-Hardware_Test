# Step 3 — AutoMagicCalib launcher (owner: DevC)

Status: step spec. This document builds against the shared contracts in
[`00-FRAMEWORK-AND-BOOTSTRAP.md`](00-FRAMEWORK-AND-BOOTSTRAP.md) and does **not**
restate them — it links back. Read doc 00 first (§8 logging/reporting, §9
privilege + USER-ACTION, §10 NGC key, §11 install-location, §12 step-module
interface).

Step 3 is the third of five steps in the single `mv3dt-installer` binary. It
runs **after Step 2 (DeepStream SDK) is `COMPLETE`**. Its deliverable is a
dead-simple AutoMagicCalib (AMC) launcher: bring up NVIDIA's AMC stack via
`docker compose`, open the localhost UI, and keep the AMC service running until
the operator closes the AMC browser window themselves — plus a standalone
`amc` executable dropped in `<install_dir>/bin/` so the operator can do the
same thing any time later (and which Step 5 reuses).

This step ports the runtime logic already proven in
[`laptop/scripts/30_start_amc.sh`](../../laptop/scripts/30_start_amc.sh) into
the installer's Python step module. AMC is **not** vendored: it is cloned from
`https://github.com/NVIDIA-AI-IOT/auto-magic-calib.git` into
`$HOME/auto-magic-calib/` (never under this repo tree) and run via
`docker compose`, per the DS 9.0 AutoMagicCalib page (see [References](#references)).

---

## 1. Scope

In scope:

- Assert / (optionally self-heal) the Docker Engine + `docker compose` plugin +
  NVIDIA Container Toolkit prerequisite (§3).
- AMC bring-up ported from `30_start_amc.sh`: clone, `projects/`+`models/` +
  `chown 1000:1000`, optional `docker login nvcr.io`, write `compose/.env`,
  diff against upstream `.env.example`, `docker compose pull && up -d` (§4).
- Open `http://localhost:5000` and **keep AMC up until the operator closes the
  AMC browser window** (the crux — §5).
- Drop a standalone `amc` executable in `<install_dir>/bin/` that repeats
  bring-up + open + hold-until-close on demand (§6).
- Offer an immediate launch at the end of Step 3, or leave the exe for later
  (§2).
- `preflight()/run()/verify()/report()` + USER-ACTION blocks (§7).

Out of scope (see [§9](#9-out-of-scope--open-decisions)):

- The AMC 6-step calibration workflow itself (Project Setup → Video Upload →
  Parameters → Manual Align → Execute/VGGT → Results/Export) — **human-driven
  in the browser** (§8). Step 3 only brings up the UI and holds it open.
- Ingesting the AMC export into DeepStream configs — that is **Step 4**
  ([`STEP-4-*`](00-FRAMEWORK-AND-BOOTSTRAP.md); calib output wiring, DevD).
- Per-project / re-run project management — that is **Step 5**, which reuses the
  `amc` exe from this step.

Module: `installer/mv3dt_installer/steps/step3_amc_launcher.py`
(id `step3_amc_launcher`, `order = 3`, per doc 00 §12).

---

## 2. Where the "launch now vs. later" affordance lives

The product-owner requirement — "the user can either close the installer or
launch AMC immediately; if they don't, an exe in their install folder lets them
run it later" — is satisfied by splitting deliverable from action:

- **`run()` always drops the exe + writes config and marks the step
  `COMPLETE`.** This is the durable deliverable and it never blocks on a
  browser. It does **not** force AMC up (a ~4 GB image pull — see §9 open
  decision on pre-pull).
- **After the exe is in place, `run()` offers an immediate launch** (interactive
  only): a plain TUI prompt `Launch AutoMagicCalib now? [y/N]`. Default **No**.
  `--non-interactive` / `--no-pause` skip the prompt.
  - **Yes** → call the shared `launch_amc(ctx, ...)` routine (§4–§5), which
    blocks until the operator closes the AMC browser window, tears AMC down,
    then returns. Step 3 still resolves to `COMPLETE`.
  - **No** → return `COMPLETE` immediately; `report()` tells the operator the
    exe path so they can run it whenever they want.

Because bring-up + calibration is a prerequisite for Step 4's export ingest,
offering the launch right at Step 3 is natural, but it is never mandatory: the
exe is the contract, the launch is a convenience.

`launch_amc(...)`, the `amc` subcommand (§6), and Step 5's re-run entry point
all call **the same** routine, so behavior is identical everywhere.

---

## 3. Docker + NVIDIA Container Toolkit prerequisite

Per [`DEEPSTREAM-SETUP.md` §8.2](../../laptop/docs/DEEPSTREAM-SETUP.md),
`00_bootstrap.sh` already installs Docker Engine + `docker-compose-plugin`,
`nvidia-container-toolkit`, runs `nvidia-ctk runtime configure --runtime=docker`,
and adds the invoking user to the `docker` group.

**Decision (recommended default): Step 3 ASSERTS these, it does not own their
install.** Package installs belong to Step 1 / the framework so the step
boundaries stay clean and installs are not duplicated. Step 3's `preflight()`
verifies presence; if missing it returns `USER_ACTION_REQUIRED` (§7) with the
exact remediation commands rather than silently apt-installing inside Step 3.

The exact assertions in `preflight()`:

1. **Step 2 complete** — `ctx` exposes prior-step status via the framework;
   `preflight()` refuses to run if `step2_deepstream_sdk` is not `COMPLETE`
   (returns `FAILED` with "run Step 2 first"). Doc 00 §12.1.
2. **Docker Engine** — `docker --version` and `docker info` succeed.
3. **`docker compose` plugin** — `docker compose version` succeeds (fall back to
   legacy `docker-compose` exactly as `30_start_amc.sh` does, lines 97–105).
4. **NVIDIA Container Toolkit runtime** — the `nvidia` runtime is registered
   with the Docker daemon (`docker info` lists it, or
   `/etc/docker/daemon.json` has the `nvidia` runtime from
   `nvidia-ctk runtime configure`). This is what lets AMC's containers see the
   RTX PRO 4500 (`NVIDIA_VISIBLE_DEVICES=all`).

If any of 2–4 fail, the USER-ACTION block lists (verbatim, copy-pasteable),
mirroring `DEEPSTREAM-SETUP.md` §8.2:

```bash
# Docker Engine + compose plugin (Docker's Ubuntu apt repo)
sudo apt-get install -y docker.io docker-compose-plugin
# NVIDIA Container Toolkit + wire it into the Docker daemon
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
# let the invoking user run docker without sudo
sudo usermod -aG docker "$USER"
```

> **Optional self-heal (flagged decision, §9):** Step 3 MAY run the four
> commands above itself when `--amc-install-prereqs` is passed, reporting each
> via `report_installed()` / `report_already_installed()` (doc 00 §8.3). Default
> is **assert-only** to avoid overlapping Step 1. Confirm the boundary with DevA.

### 3.1 The docker-group first-run caveat (important)

Adding a user to the `docker` group **does not take effect until that user
starts a new login session**. Consequences:

- **During the install run**, the binary is launched via `sudo -E`
  (doc 00 §9.1), so `docker` calls run as root and work regardless of group
  membership. Bring-up from inside the installer is fine.
- **Later, when the operator runs `<install_dir>/bin/amc` as themselves**,
  their shell may not yet have picked up the `docker` group. The launcher must
  handle this:
  - Probe with `docker info`. On permission-denied, and if the process is not
    root, either (a) re-exec itself under `sudo` (prompting once), or
    (b) emit the caveat: **"log out and back in once, or run `sudo amc` for
    this first run."**
  - Prefer (b) as the non-surprising default; make `sudo` re-exec opt-in via
    `amc --sudo`.

This is the same caveat documented in `DEEPSTREAM-SETUP.md` §8.2 ("log out /
back in once before running `30_start_amc.sh`, or use `sudo docker`").

---

## 4. AMC bring-up (ported from `30_start_amc.sh`)

`launch_amc(ctx, project, ...)` reproduces the orchestration in
[`laptop/scripts/30_start_amc.sh`](../../laptop/scripts/30_start_amc.sh),
run **as the invoking user** via `ctx.run_as_user(...)` (doc 00 §9.2) so the
clone and files land under the operator's home, not root's.

Steps, in order:

1. **Resolve config** (§4.1). Compute `AMC_ROOT`, `HOST_IP`, ports,
   `PROJECT_NAME`.
2. **Repo-isolation guard.** Refuse to place the clone under this repo's working
   tree — port the `REPO_ROOT` guard (`30_start_amc.sh` lines 116–121):
   if `AMC_ROOT == REPO_ROOT` or is a child of it, `die`/`FAILED`.
3. **Clone if missing** into `AMC_ROOT` (default `$HOME/auto-magic-calib`):
   `git clone https://github.com/NVIDIA-AI-IOT/auto-magic-calib.git "$AMC_ROOT"`
   (lines 123–128). If present, log "AMC repo already present".
4. **Create `projects/` + `models/`** and `chown 1000:1000` them so the
   in-container UID 1000 can write (Notion §8.3; lines 130–142). As root this is
   a direct `chown`; as the user it is `sudo chown` with a warning fallback.
5. **Optional `docker login nvcr.io`** using the NGC key from the `ctx.ngc`
   handle (doc 00 §10): `echo "$key" | docker login nvcr.io --username
   '$oauthtoken' --password-stdin` (lines 144–150). AMC images may be public;
   on failure, warn and continue. If `ctx.ngc.load_key()` is `None`
   (manual-fallback), skip and log "assuming AMC images are public or
   `docker login` already done".
6. **Locate the compose dir** — `AMC_ROOT/compose` if present, else fall back to
   `AMC_ROOT` if `compose.yaml`/`docker-compose.yml` sit at the root; else
   `FAILED` "upstream AMC layout changed" (lines 152–160).
7. **Write `compose/.env`** (§4.2), atomically (`tmp` + `mv`), after diffing
   against upstream `.env.example` (§4.3).
8. **Pull + up:** `docker compose pull` (skippable via `--skip-pull`) then
   `docker compose up -d`, each `cd`'d into the compose dir (lines 214–221).
9. **Readiness poll** — port `_wait_amc_ui()` (lines 34–58): `curl -sf`
   `http://localhost:<UI_PORT>` for up to ~30 s; non-fatal, prints a
   `docker compose logs` hint on timeout.
10. **Open the UI + hold until browser close** (§5).

Every dependency/tool touched is reported through `report_installed()` /
`report_already_installed()` (doc 00 §8.3), e.g. the AMC images pulled and the
clone.

### 4.1 Config resolution

Step 3 has no `laptop.env`; values come from `config.load()` (doc 00 §11) and a
small set of Step-3 keys mirrored into `<install_dir>/installer.conf`, with
step-level CLI overrides:

| Key | Default | Notes |
|-----|---------|-------|
| `AMC_ROOT` | `$HOME/auto-magic-calib` | invoking user's home; **must be outside the repo tree** |
| `HOST_IP` | auto-detected primary IPv4 (`ip route get 1.1.1.1`), fallback `127.0.0.1` | AMC UI uses it to reach the microservice |
| `AUTO_MAGIC_CALIB_UI_PORT` | `5000` | localhost UI |
| `AUTO_MAGIC_CALIB_MS_PORT` | `8000` | microservice API |
| `AUTO_MAGIC_CALIB_MS_API_URL` | unset | optional full API base (e.g. `http://127.0.0.1:8000/v1`) to avoid UI hangs when `HOST_IP` is a LAN address unreachable from the local browser (port of `30_start_amc.sh` lines 74–76, 189–192) |
| `PROJECT_NAME` | `default` | Step 3 is generic; **Step 5 passes `--project <name>`** |
| `NVIDIA_VISIBLE_DEVICES` | `all` | GPU passthrough |

Because the browser runs on the same host as AMC, `HOST_IP=127.0.0.1` is a safe
default for the localhost-only flow; expose `--host-ip` for LAN cases.

### 4.2 `compose/.env` contents

Written exactly as `30_start_amc.sh` does (lines 176–193):

```bash
HOST_IP=${HOST_IP}
AUTO_MAGIC_CALIB_MS_PORT=${AUTO_MAGIC_CALIB_MS_PORT}   # 8000
AUTO_MAGIC_CALIB_UI_PORT=${AUTO_MAGIC_CALIB_UI_PORT}   # 5000
PROJECT_DIR=${AMC_ROOT}/projects
MODEL_DIR=${AMC_ROOT}/models
NVIDIA_VISIBLE_DEVICES=all
PROJECT_NAME=${PROJECT_NAME}
# optional, only when set:
# AUTO_MAGIC_CALIB_MS_API_URL=${AUTO_MAGIC_CALIB_MS_API_URL}
```

Also `mkdir -p "$PROJECT_DIR/$PROJECT_NAME"` (line 195). The file is chowned to
the invoking user.

### 4.3 Upstream drift guard

Before writing, diff our key set against `compose/.env.example` (lines 165–173):
for each of `HOST_IP`, `AUTO_MAGIC_CALIB_MS_PORT`, `AUTO_MAGIC_CALIB_UI_PORT`,
`PROJECT_DIR`, `MODEL_DIR`, `NVIDIA_VISIBLE_DEVICES`, warn if the key is no
longer present upstream ("re-check the NVIDIA-AI-IOT/auto-magic-calib README").
This surfaces upstream renames without hard-failing.

---

## 5. Keep-alive-until-browser-closed (the crux)

Requirement: running the launcher opens AMC on localhost and **does not stop the
AMC service until the operator has closed the browser page themselves.**

A plain `xdg-open URL` returns immediately and cannot report when a tab is
closed, so it cannot drive teardown. The design therefore launches a
**dedicated, monitorable browser process** whose lifetime maps 1:1 to a window,
blocks on that process, and tears AMC down only when it exits.

### 5.1 Mechanism (primary path)

1. **Open the UI in a dedicated app-mode window** with an ephemeral profile so a
   brand-new OS process is guaranteed. Try, first-supported-wins, as the
   invoking user with `DISPLAY`/`WAYLAND_DISPLAY` propagated:

   ```bash
   # Chromium family — app mode, isolated profile:
   <chrome> --app=http://localhost:5000 \
            --user-data-dir="$(mktemp -d)" --no-first-run --no-default-browser-check
   #   <chrome> ∈ google-chrome | chromium | chromium-browser | microsoft-edge
   # Firefox fallback — dedicated instance + throwaway profile:
   firefox --new-instance --profile "$(mktemp -d)" http://localhost:5000
   ```

   Launch via `subprocess.Popen` and **capture the PID**. The ephemeral
   `--user-data-dir` / `--profile` is what forces a **new process**: without it,
   an already-running Chrome would hand the URL to the existing process and the
   Popen child would exit instantly, breaking the hold.

   **What each flag does (plain-English glossary).** These are browser
   command-line flags, not installer flags:

   - `--app=http://localhost:5000` (Chromium/Chrome/Edge): opens the URL in
     **application mode** — a standalone window with no address bar, tab strip,
     or bookmarks. It looks like a native desktop app for the AMC UI. This is
     still a *window* (satisfies "new window, not tab"); it just has no browser
     chrome. If the operator prefers a normal window *with* the address bar/tabs,
     swap `--app=<url>` for `--new-window <url>` — everything else is identical
     and it is still window-level.
   - `--user-data-dir="$(mktemp -d)"` (Chromium/Chrome/Edge): points the browser
     at a **fresh, throwaway profile directory**. This is the load-bearing flag:
     it guarantees the browser starts a **brand-new OS process** (which we can
     `wait()` on) instead of forwarding the URL to an already-running browser and
     exiting. The temp profile has no saved logins/extensions — harmless for a
     localhost tool, and it is deleted with the window.
   - `--no-first-run --no-default-browser-check` (Chromium/Chrome/Edge): suppress
     the first-run wizard and the "make me your default browser" prompt so the
     window lands directly on AMC.
   - `--new-instance --profile "$(mktemp -d)"` (Firefox): the Firefox equivalent
     of the two flags above — `--new-instance` (with `-no-remote` behavior) forces
     a separate Firefox process, and `--profile <dir>` gives it a throwaway
     profile so it does not attach to an already-open Firefox.

   `--keep-up`, by contrast, is **our installer's** flag (§6), not a browser
   flag: it tells the launcher to leave AMC running after the window closes.

2. **Hold**: `proc.wait()` — block until that browser process exits, i.e. the
   operator closes the AMC window. Print:
   *"AutoMagicCalib is running at http://localhost:5000 — close the AMC browser
   window when you're done; the service stays up until you do."*

3. **Teardown (once, fail-safe)**: on the wait returning — or on `SIGINT` /
   `SIGTERM`, or via `atexit` — run `docker compose down` in the compose dir
   (the DS 9.0 documented stop command). Guard with a run-once flag so signal +
   normal-exit paths don't double-invoke. Skip teardown when `--keep-up` is set.

Install `SIGINT`/`SIGTERM` handlers and an `atexit` hook **before** `up -d` so
AMC is always torn down even if the launcher is interrupted (unless `--keep-up`).

### 5.2 Fallbacks

- **No monitorable browser (only `xdg-open`/`x-www-browser` available), but
  interactive TTY:** open with `xdg-open`, then block on operator input:
  *"Press Enter (or Ctrl-C) when you have closed AMC to shut it down."* The
  keypress / signal triggers the same teardown. (Window close cannot be detected
  here, so the operator explicitly signals completion.)
- **Headless / remote (no `DISPLAY` and no `WAYLAND_DISPLAY`):** print the URL
  (port `30_start_amc.sh` lines 227–233) and:
  - interactive → same "press Enter / Ctrl-C to tear down" hold;
  - `--non-interactive` → **leave AMC up** and print the stop command
    (`amc --down`, §6) — there is no browser to monitor, so tearing down blindly
    would be wrong.

### 5.3 Trade-offs and limits (be explicit)

- **Window granularity, not tab granularity.** The launcher detects the
  dedicated *window/process* closing. Closing the AMC window tears AMC down
  (correct). But **reliably detecting a specific TAB closing inside a shared
  browser is not possible** with `xdg-open`/OS signals — that is why the design
  uses a dedicated window/process. If the operator instead opens the URL in a
  tab of their everyday browser, the launcher cannot see that tab close.
- **Ephemeral profile cost:** the app-mode window has no saved logins /
  extensions. Harmless for a localhost tool; called out so no one is surprised.
- **Crash = teardown.** If the browser process dies unexpectedly, AMC is torn
  down (fail-safe). Use `--keep-up` if the operator wants AMC to survive the
  window.
- **True tab-level detection would require** a different mechanism — e.g. a
  small local wrapper page or reverse proxy in front of the AMC UI that sends a
  `navigator.sendBeacon` on `beforeunload` or holds a WebSocket heartbeat, so
  the tab's close is reported to the launcher. This cannot be bolted onto
  NVIDIA's UI without proxying it and is more complex/fragile. **Flagged as an
  open decision (§9)** — the shipped design is dedicated-window.

---

## 6. The standalone `amc` executable in `<install_dir>/bin/`

Deliverable dropped by `run()` (doc 00 §11 puts per-project/launcher exes in
`<install_dir>/bin/`). Name: **`amc`**.

### 6.1 Relationship to the installer binary

**Recommended: `amc` is a thin generated wrapper that re-invokes the single
PyInstaller binary with an `amc` subcommand.** This avoids duplicating the large
`--onefile` binary and keeps one code path.

1. `run()` ensures a stable copy of the installer binary exists at
   `<install_dir>/bin/mv3dt-installer` — copied from the running executable
   (`sys.executable` when frozen; doc 00 §4.2) so the launcher survives the repo
   clone being moved/deleted.
2. `run()` writes `<install_dir>/bin/amc`, `chmod +x`, chowned to the invoking
   user:

   ```bash
   #!/usr/bin/env bash
   # Generated by mv3dt-installer Step 3. Brings up AutoMagicCalib, opens the
   # localhost UI, and holds it open until you close the AMC browser window.
   exec "/opt/mv3dt/bin/mv3dt-installer" amc "$@"
   ```

   (`/opt/mv3dt` is substituted with the resolved `install_dir`.)

`amc` accepts pass-through flags: `--project <name>`, `--skip-pull`,
`--keep-up`, `--down` (tear down without bring-up), `--host-ip`, `--sudo`,
`--no-open` (bring up without opening/holding — for scripting).

Alternative considered: copy/symlink the whole binary to `bin/amc`. Rejected as
the default (binary bloat / two artifacts to keep in sync), but acceptable if
DevA prefers not to add a subcommand.

### 6.2 Framework coordination (flagged)

The `amc` subcommand must be routed by the framework's CLI dispatch
(doc 00 §3.2–3.3 currently lists flags, not subcommands) to Step 3's
`launch_amc(...)`. This is a **small framework extension owned by DevA** — flag
it (§9): `mv3dt-installer amc [...]` should bypass the step dispatch loop and
call the launcher directly (so it can be run standalone after install, not just
as part of a resumable install).

### 6.3 Reuse by Step 5

Step 5 ("per-project exes") reuses this exe as its "re-run calibration for an
existing project / start a new project" entry point by invoking
`amc --project <name>` (doc 00 §12.4). Step 3 owns the launcher; Step 5 owns the
project registry that chooses the `<name>`.

---

## 7. `preflight` / `run` / `verify` / `report` + user actions

Per the step-module contract (doc 00 §12). Steps return `StepResult` and never
write `state.json` directly.

### 7.1 `preflight(ctx)`

- Assert `step2_deepstream_sdk == COMPLETE` → else `FAILED`.
- Assert Docker Engine + `docker compose` + NVIDIA Container Toolkit runtime
  (§3). Missing → `USER_ACTION_REQUIRED` with the install block (§3), unless
  `--amc-install-prereqs` self-heal is enabled.
- Assert `git` present (needed to clone AMC) → else `USER_ACTION_REQUIRED`.
- Returns `COMPLETE` (= "ok to run") when all hold.

### 7.2 `run(ctx)`

- Compute config (§4.1); guard AMC clone path against the repo tree.
- Ensure `<install_dir>/bin/mv3dt-installer` copy + write `<install_dir>/bin/amc`
  wrapper (§6). Report each artifact.
- Write/refresh `compose/.env` groundwork lazily: the clone + `.env` are created
  at first actual launch (they need `AMC_ROOT` under the user's home), but the
  **exe + installer.conf keys are written now** so "run later" always works.
- Offer immediate launch (§2). If accepted, call `launch_amc(...)` (§4–§5),
  which blocks until browser close then tears AMC down.
- Return `COMPLETE` (deliverable = exe + config). `docker compose` / clone
  failures during an *opted-in* immediate launch are surfaced as `FAILED` with a
  `docker compose logs` hint; a failure to *drop the exe* is always `FAILED`.

### 7.3 `verify(ctx)`

Idempotent post-checks (doc 00 §12.1, §8.4 `verify_pinned` where a version
applies):

- `<install_dir>/bin/amc` exists, is executable, and is owned by the invoking
  user; `<install_dir>/bin/mv3dt-installer` exists.
- `docker compose version` resolves and the `nvidia` runtime is registered.
- The Step-3 keys are present in `installer.conf`.
- (No pinned upstream AMC version — AMC tracks `NVIDIA-AI-IOT/auto-magic-calib`
  `main`; `verify()` records the resolved commit for the transcript rather than
  equality-pinning it.)

Returns `COMPLETE` only when the exe deliverable is in place and Docker is
usable.

### 7.4 `report(ctx)`

Human summary (no side effects): the `amc` exe path, the UI/API URLs
(`http://localhost:5000` / `:8000`), `AMC_ROOT`, and the manage commands:

```
AutoMagicCalib launcher installed.
  Run it any time:   <install_dir>/bin/amc
  Web UI:            http://localhost:5000
  Microservice API:  http://localhost:8000
  AMC clone:         $HOME/auto-magic-calib
  Stop AMC:          <install_dir>/bin/amc --down   (or close the AMC window)

The AMC service stays up until you close the AMC browser window (or run --down).
Next: complete the 6-step calibration in the browser (see the DS 9.0
AutoMagicCalib guide), then Step 4 ingests the export.
```

### 7.5 USER-ACTION cases (doc 00 §9.3)

- Docker / toolkit missing → §3 install block; closing line
  "Then run the installer again to continue."
- Docker permission denied for the non-root `amc` run → the group caveat (§3.1):
  "log out and back in once, or run `sudo amc` for this first run."
- Headless launch → print the URL to open manually (§5.2).

---

## 8. The AMC 6-step workflow (reference only — NOT automated)

Step 3 gets the operator to the AMC landing page and holds it open; the
calibration itself is **human-driven in the browser** and stays out of scope
(same boundary as `30_start_amc.sh`). For reference, the workflow is (Notion
§8.6, cross-referenced with the DS 9.0 AutoMagicCalib page):

1. **Project Setup** — name + camera count.
2. **Video Upload** — one clip per camera.
3. **Parameters** — intrinsic/extrinsic guesses.
4. **Manual Align** — correspondences per pair.
5. **Execute** — runs calibration (VGGT stage; watch GPU VRAM).
6. **Results / Export** — review RMSE, export MV3DT artefacts into
   `$HOME/auto-magic-calib/projects/<PROJECT_NAME>/exports/`.

Ingesting that export → DeepStream configs is **Step 4**, not Step 3.

---

## 9. Out of scope / open decisions

Out of scope (deferred to other steps or the human):

- The 6-step calibration workflow automation (§8) — human-driven.
- Export ingest / config rendering — **Step 4**.
- Project registry / multi-project management — **Step 5** (reuses `amc`).
- Installing Docker/toolkit packages — **Step 1 / framework** by default (§3).
- systemd supervision of AMC / auto-restart — production hardening, deferred per
  doc 00 §13.

Open decisions to confirm with the product owner / DevA (do not silently expand
scope):

1. **Window-level close detection — RESOLVED: dedicated window, not tab.** The
   product owner confirmed a new *browser window* (not a tab) is the desired
   behavior, so the shipped dedicated window/process design (§5.1) is final. True
   *tab*-level close detection inside a shared browser is intentionally NOT
   pursued (it is impossible without proxying the AMC UI via a beacon/WebSocket
   heartbeat). App-mode (`--app=`) is the default window style; `--new-window` is
   the drop-in variant if a normal window with browser chrome is preferred.
2. **Assert vs. self-heal for the Docker prerequisite (§3).** Default is
   assert-only (Step 1 owns installs); `--amc-install-prereqs` self-heal is
   available but off by default. Confirm the Step 1/Step 3 boundary with DevA.
3. **`amc` subcommand routing (§6.2)** — needs a small framework CLI dispatch
   extension owned by DevA.
4. **Pre-pull AMC images at install time vs. lazy at first launch (§2, §4).**
   Default is lazy (avoid a ~4 GB pull during install); a `--amc-prepull`
   opt-in could warm the cache. Confirm expected install-time bandwidth.

---

## References

DeepStream 9.0 official documentation — DS 9.0 only (NVIDIA's current release).
AMC facts cross-checked via Context7 library
`/websites/nvidia_metropolis_deepstream_dev-guide` (confirmed: `docker compose
up -d` from `compose/`, `docker compose down` to stop, `docker login nvcr.io`
with `$oauthtoken` + NGC API key, `HOST_IP` in `compose/.env`, UI on
`AUTO_MAGIC_CALIB_UI_PORT` default 5000 and microservice on
`AUTO_MAGIC_CALIB_MS_PORT` default 8000).

- DS 9.0 AutoMagicCalib (NGC setup, clone, `compose/.env` `HOST_IP`,
  `docker compose up -d`, UI 5000 / microservice 8000, `docker compose down`):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_AutoMagicCalib.html>
- DS 9.0 Docker Containers (NVIDIA Container Toolkit / `--gpus`, `nvcr.io`
  registry, running DeepStream containers):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_docker_containers.html>

Repo files referenced:

- [`laptop/scripts/30_start_amc.sh`](../../laptop/scripts/30_start_amc.sh) — the
  existing AMC orchestrator this step ports (clone guard, `chown 1000:1000`,
  `.env` write, drift diff, pull/up, readiness poll, `xdg-open`).
- [`laptop/docs/DEEPSTREAM-SETUP.md`](../../laptop/docs/DEEPSTREAM-SETUP.md) —
  §8.2 Docker + NVIDIA Container Toolkit, §8.3–8.6 AMC bring-up + 6-step
  workflow.
- [`installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md`](00-FRAMEWORK-AND-BOOTSTRAP.md)
  — shared framework: step interface (§12), logging/reporting (§8),
  privilege/USER-ACTION (§9), NGC key (§10), install-location + `bin/` (§11).
