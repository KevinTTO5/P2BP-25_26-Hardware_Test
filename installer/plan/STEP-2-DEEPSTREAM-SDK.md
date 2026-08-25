# STEP 2 — DeepStream 9.1 SDK (owner: DevB)

Status: step spec. This document builds on the shared framework contract in
[`00-FRAMEWORK-AND-BOOTSTRAP.md`](00-FRAMEWORK-AND-BOOTSTRAP.md) — the
step-module interface, `StepResult`/`StepStatus`, `Context`, state machine,
reboot gate, logging/reporting strings, NGC key handoff, and install-location
config are all defined there and are **not** restated here. Read doc 00 first.

Step 2 module: `installer/mv3dt_installer/steps/step2_deepstream_sdk.py`
(`id = "step2_deepstream_sdk"`, `title = "DeepStream 9.1 SDK"`, `order = 2`).

Scope: install the **DeepStream 9.1 SDK** on Ubuntu 24.04 / x86_64 (RTX PRO
4500 Blackwell), by one of the three official methods (deb / tar / docker) —
deb/tar are public GitHub Release downloads, docker is NGC-gated using the
operator's key captured at onboarding (required, always present — doc 00
§10) — then run post-install wiring and a smoke test. This step assumes
Step 1 already
installed and verified the driver / CUDA / cuDNN / TensorRT / GStreamer stack
and that its reboot is confirmed.

---

## 1. Locked facts and pins (from DS 9.1 docs)

- Target: **Ubuntu 24.04**, **x86_64 (dGPU)**, GPU **RTX PRO 4500 Blackwell**.
  **DeepStream 9.1 only.**
- DS SDK version string: **`9.1.0-1`** (deb/dpkg), SDK dir
  **`/opt/nvidia/deepstream/deepstream-9.1`** with the stable symlink
  **`/opt/nvidia/deepstream/deepstream`** pointing at it.
- DS 9.1 deb/tar artifacts are **public GitHub Release assets** — no NGC
  account or key required to download them. Only the **Docker image** remains
  NGC-gated (`nvcr.io`, needs `docker login`). See the acquisition split in
  [§8](#8-reconciling-the-documented-drift-vs-00_bootstrapsh).
- Artifacts (exact filenames, from the DS 9.1 Installation page):
  - Debian package: **`deepstream-9.1_9.1.0-1_amd64.deb`**
  - Tar archive: **`deepstream_sdk_v9.1.0_x86_64.tbz2`**
  - Docker images: **`nvcr.io/nvidia/deepstream:9.1-triton-multiarch`** and
    **`nvcr.io/nvidia/deepstream:9.1-samples-multiarch`**
- GStreamer prerequisite pin (owned/verified by Step 1): **1.24.2**. Step 2
  does not re-install the driver/CUDA/TRT/GStreamer stack.

Version pins and artifact names cross-checked directly against the DS 9.1
Installation and Docker Containers pages (see [References](#references)).

---

## 2. Preflight (`preflight(ctx)`)

Cheap gate checks; returns `COMPLETE` to mean "ok to run", else
`USER_ACTION_REQUIRED` / `FAILED`. Per the framework, Step 2 runs only after
Step 1 is `COMPLETE` and its reboot confirmed — but preflight re-asserts the
runtime facts because a step must not trust prior state blindly.

1. **Step 1 completion + reboot confirmed.** Assert the framework already
   marked `step1_prerequisites` `COMPLETE` and `reboot_pending is null`
   (doc 00 §6–7). The dispatch loop guarantees this ordering; preflight
   additionally spot-checks the installed pins below so Step 2 fails loudly if
   the environment drifted.
2. **Prereq pins present** (equality, via `ctx.verify_pinned`, doc 00 §8.4) —
   these are Step 1's deliverables, re-checked as *inputs* here:
   - NVIDIA driver `595.58.03`
     (`nvidia-smi --query-gpu=driver_version --format=csv,noheader`)
   - CUDA `13.2` (`nvcc --version` release)
   - cuDNN `9.20.0.48` (`dpkg -l | grep libcudnn9`)
   - TensorRT `10.16.0.72-1+cuda13.2` (`dpkg -s libnvinfer10`)
   - GStreamer `1.24.2` (`gst-inspect-1.0 --version`)
   Any mismatch → `FAILED` with remediation "re-run Step 1"
   (`--reset-step 1`).
3. **OS/arch** — Ubuntu 24.04, `x86_64` (mirror doc 00 / `00_bootstrap.sh`
   Phase 0). Wrong OS/arch → `FAILED`.
4. **Root** — `ctx.run_root` available (`privilege.require_root`, doc 00 §9).
5. **NGC key availability** — call `ctx.ngc.load_key()`. The key is
   required (doc 00 §10), so a present value is the only expected outcome
   here; `None` means onboarding did not run before dispatch reached this
   step, which is an internal ordering bug, not an operator-recoverable
   state — treat it as `FAILED` rather than a fallback path.
6. **Method decidability inputs** — probe the auto-detect inputs
   ([§4](#4-auto-detection-vs-prompt)) so a choice can be made without extra
   privilege escalation later. This is read-only; it never installs anything.
7. **Already installed?** — if `dpkg -s deepstream-9.1` reports `9.1.0-1` (deb)
   **or** `/opt/nvidia/deepstream/deepstream-9.1/version` matches (tar/docker
   host marker), preflight may short-circuit to let `verify()` confirm and the
   framework mark `COMPLETE` (idempotent re-run).

---

## 3. The three official install methods

DS 9.1 x86_64 has exactly three official install paths (DS 9.1 Installation
page). The installer prompts with the operator-facing description of each; the
descriptions below are the prompt copy.

### 3.1 Method A — GitHub Release Debian package (bare-metal host install)

**What it is (operator copy):** "Install DeepStream directly onto this
machine as a system package. Best for running the DeepStream pipeline on the
host (no container). Pulls in its apt prerequisites automatically."

- Artifact: `deepstream-9.1_9.1.0-1_amd64.deb`, downloaded from the
  `NVIDIA/DeepStream` GitHub Release (no NGC account needed).
- Install command (from the Installation page — the leading `./` is required
  so apt treats it as a local file and resolves dependencies):

  ```bash
  sudo apt-get install ./deepstream-9.1_9.1.0-1_amd64.deb
  ```

- Registers as dpkg package `deepstream-9.1` (`9.1.0-1`) → cleanly
  verifiable/removable via apt. This is the default for the MV3DT host
  pipeline this installer targets.

### 3.2 Method B — GitHub Release tar archive (relocatable / non-apt install)

**What it is (operator copy):** "Extract the DeepStream SDK from a tarball and
run its installer script. Use when you want a self-contained SDK tree not
managed by apt, or need to sit alongside another install. Does not register
with dpkg."

- Artifact: `deepstream_sdk_v9.1.0_x86_64.tbz2`, downloaded from the
  `NVIDIA/DeepStream` GitHub Release (no NGC account needed).
- Install commands (from the Installation page):

  ```bash
  sudo tar -xvf deepstream_sdk_v9.1.0_x86_64.tbz2 -C /
  cd /opt/nvidia/deepstream/deepstream-9.1/
  sudo ./install.sh
  sudo ldconfig
  ```

- The archive extracts into the same fixed SDK path
  `/opt/nvidia/deepstream/deepstream-9.1`. `install.sh` + `ldconfig` finalize
  the linker cache. Not visible to `dpkg -s deepstream-9.1`, so `verify()`
  uses the on-disk `version` file + binary checks
  ([§7](#7-verification-verifyctx)).

### 3.3 Method C — NGC Docker image (containerized runtime)

**What it is (operator copy):** "Run DeepStream inside NVIDIA's official
container. The host only needs the driver + Docker + NVIDIA Container Toolkit;
DeepStream and its dependencies live in the image. Best when you prefer
containerized/reproducible runtime or already run Docker."

- Image: `nvcr.io/nvidia/deepstream:9.1-triton-multiarch` (the Triton variant;
  it is the Quickstart-referenced image and carries the sample configs used by
  the smoke test). A `9.1-samples-multiarch` variant also exists. Docker
  images remain NGC-hosted — this is the one method that still needs the
  operator's NGC key.
- Acquire + run (from the Quickstart / Docker Containers page):

  ```bash
  docker pull nvcr.io/nvidia/deepstream:9.1-triton-multiarch
  docker run --gpus all -it --rm --net=host \
    -e CUDA_CACHE_DISABLE=0 \
    nvcr.io/nvidia/deepstream:9.1-triton-multiarch
  ```

- Requires Docker Engine + NVIDIA Container Toolkit on the host (installed by
  the bootstrap/Step 1 runtime phase). The SDK is **not** placed at
  `/opt/nvidia/deepstream` on the host; it lives inside the image. `verify()`
  runs the version/smoke checks *inside the container*
  ([§7](#7-verification-verifyctx)).

---

## 4. Auto-detection vs prompt

`run()` first calls an internal `detect_method(ctx) -> Method | None`. If it
returns a confident method, the installer proceeds (announcing the chosen
method + why). If it returns `None` (ambiguous), the installer **prompts** the
operator, displaying the three §3 descriptions and a sane default. In
`--non-interactive`, an ambiguous result falls back to the configured/default
method (see decision table) rather than prompting.

### 4.1 Decision inputs (read-only probes)

| Input | How it is read | Meaning |
|-------|----------------|---------|
| `containerized` intent | `ctx.conf` key `ds_install_method` (from `installer.conf`) or `--ds-method {deb,tar,docker}` flag | Explicit operator/config override — highest precedence |
| Docker present + usable | `docker` on PATH **and** `docker info` succeeds (as invoking user via `ctx.run_as_user`) | Container runtime available |
| NVIDIA runtime present | `nvidia-container-toolkit` installed (`dpkg -s`) **and** `nvidia-ctk` on PATH **and** Docker `nvidia` runtime configured (`docker info` runtimes) | Container GPU access available |
| Host pipeline intent | default `True` for this installer (MV3DT host pipeline is the product) unless overridden | Bare-metal DS wanted |
| Existing DS install | `dpkg -s deepstream-9.1` == `9.1.0-1` (deb) or `/opt/nvidia/deepstream/deepstream-9.1/version` present (tar) or local image tag present (`docker image inspect`) | Already installed via a specific method |
| Relocatable / non-root / multi-version need | `--ds-method tar` or `installer.conf` `ds_relocatable=true` | Force the tarball path |

### 4.2 Decision logic (precedence, first match wins)

1. **Explicit override** (`--ds-method` / `installer.conf ds_install_method`)
   → use it. No prompt.
2. **Already installed** via a detected method → select that method and let
   `verify()` confirm (idempotent).
3. **Relocatable/multi-version requested** (`ds_relocatable=true` /
   `--ds-method tar`) → **tar**.
4. **Docker + NVIDIA Container Toolkit both present and usable** *and* host
   pipeline **not** explicitly required → **docker**.
5. **Host pipeline intent true** (the default for this product) and no
   container preference → **deb**.
6. **Otherwise ambiguous** (e.g. Docker present but toolkit missing; or no
   clear host-vs-container signal) → **PROMPT** the operator with the three
   §3 descriptions; default highlighted = **deb**. In `--non-interactive`,
   pick **deb** (product default) and log the auto-choice.

The chosen method is logged (`log.info "DS install method: <method> (<reason>)"`)
and persisted by writing `ds_install_method` back through the framework config
so a resumed run is deterministic. (Steps never write `state.json` directly;
the method is mirrored into `installer.conf` via the framework helper, doc 00
§11.)

---

## 5. Acquisition (GitHub Release for deb/tar, NGC for Docker)

Acquisition is method-dependent: deb/tar are anonymous GitHub Release
downloads with no gating at all; Docker remains NGC-gated. Acquisition is
otherwise orthogonal to method — each method needs its own artifact fetched
(or the operator pointed at where to get it).

### 5.1 deb / tar — public GitHub Release download (no key, no login)

- Fetch the release asset directly, run as the invoking user
  (`ctx.run_as_user`):

  ```bash
  # run as the invoking user
  cd <artifact_dir>
  curl -fsSL -o <artifact> \
    "https://github.com/NVIDIA/DeepStream/releases/download/v9.1.0/<artifact>"
  ```

  where `<artifact>` is `deepstream-9.1_9.1.0-1_amd64.deb` (Method A) or
  `deepstream_sdk_v9.1.0_x86_64.tbz2` (Method B). No NGC key, no `docker
  login`, no browser sign-in — this is a plain anonymous HTTPS download.
- **Failure fallback (not NGC-gated — just "no internet" or GitHub
  unreachable):** if the download fails, `run()` returns
  `USER_ACTION_REQUIRED` (doc 00 §9.3) telling the operator to download the
  asset from
  `https://github.com/NVIDIA/DeepStream/releases/tag/v9.1.0` on any machine
  with internet access and place it in `<install_dir>/downloads/deepstream/`,
  then re-run. This replaces the old NGC-sign-in fallback entirely for these
  two methods.

### 5.2 docker — NGC-gated (fully automatic; key is required)

- `ctx.ngc.configure_ngc_cli()` writes the invoking user's `~/.ngc/config`
  (`chmod 600`) from the onboarding-stored key (doc 00 §10, required) only
  when the Docker method is chosen; deb/tar never touch the NGC CLI.
- Authenticate then pull, run as the invoking user:

  ```bash
  echo "$NGC_API_KEY" | docker login nvcr.io -u '$oauthtoken' --password-stdin
  docker pull nvcr.io/nvidia/deepstream:9.1-triton-multiarch
  ```

  The key is never echoed to the transcript (doc 00 §8.2/§10 redaction).
  Since the key is guaranteed present by the time Step 2 runs, there is no
  fallback path here: a `docker login` failure with a valid key indicates a
  real error (revoked/expired key, network issue) and `run()` returns
  `FAILED` with the login command's own error output, not a guided manual
  sign-in.

### 5.3 Known artifact directory

- `artifact_dir` = `<install_dir>/downloads/deepstream/` (created by Step 2,
  chowned to the invoking user). Deb/tar land here whether fetched
  automatically or placed manually, so both paths converge on one location.

On re-run, preflight/`run()` detect the placed artifact (or successful docker
login) and continue without re-prompting.

---

## 6. Install location: what the path prompt governs

There are **two** distinct locations; do not conflate them.

- **Fixed DS SDK path (NVIDIA-owned):**
  `/opt/nvidia/deepstream/deepstream-9.1` (symlink
  `/opt/nvidia/deepstream/deepstream`). This is **not** configurable — the
  deb/tar both install there, and the docker image uses the same path
  internally. The framework install-path prompt does **not** move it.
- **Framework `install_dir` (operator-selectable, default `/opt/mv3dt`,
  doc 00 §11):** governs where the **installer app + per-project artifacts**
  live (`<install_dir>/bin`, `<install_dir>/deepstream`,
  `<install_dir>/secrets`, and Step 2's `<install_dir>/downloads/deepstream`).
  Step 2 reads it via `config.load()` and never hardcodes it.

Operator-facing clarification the prompt must show: "DeepStream itself always
installs to `/opt/nvidia/deepstream/deepstream-9.1` (fixed by NVIDIA). The
install directory you choose is where this installer keeps its own files,
downloads, and the per-project executables." A path prompt is only presented
where the choice is meaningful (the framework `install_dir`); the DS SDK path
is shown as read-only information, GUI-installer style.

---

## 7. Verification (`verify(ctx)`)

Idempotent post-checks; returns `COMPLETE` only when all pass. Uses
`ctx.verify_pinned` (doc 00 §8.4) and the §9 reporters. Verification is
method-aware.

### 7.1 Common checks (deb / tar host installs)

1. **SDK present** — `/opt/nvidia/deepstream/deepstream-9.1` exists and the
   `deepstream` symlink resolves to it.
2. **Version pin** —
   `verify_pinned("DeepStream", <actual>, "9.1.0-1")`:
   - deb: `dpkg -s deepstream-9.1 | Version`.
   - tar: read `/opt/nvidia/deepstream/deepstream-9.1/version` (SDK version
     file) and normalize to `9.1.0`.
3. **Binary + version report** — `deepstream-app --version-all` (falls back to
   `deepstream-app --version`) runs and reports DeepStream 9.1.
4. **Post-install artifacts present** ([§9](#9-post-install-steps-run-tail-host-installs)):
   `/etc/profile.d/deepstream.sh` exists and exports `DEEPSTREAM_DIR`;
   `update_rtpmanager.sh` was executed (record a marker/log line).
5. **Smoke test passed** ([§7.3](#73-smoke-test-ds-90-quickstart)).

### 7.2 Docker checks (Method C)

1. Image present locally: `docker image inspect
   nvcr.io/nvidia/deepstream:9.1-triton-multiarch` succeeds.
2. Version inside the container:
   `docker run --rm --gpus all <image> deepstream-app --version-all` reports
   DeepStream 9.1 → `verify_pinned("DeepStream", <actual>, "9.1.0")`.
3. Smoke test runs inside the container ([§7.3](#73-smoke-test-ds-90-quickstart)).
4. Host wiring for docker: `/etc/profile.d/deepstream.sh` is **not** required
   (SDK is in-container); instead verify Docker + NVIDIA runtime usable
   (`docker info` shows the `nvidia` runtime).

### 7.3 Smoke test (DS 9.1 Quickstart)

Prove the SDK actually runs a pipeline using a stock sample config, per the
Quickstart. Sample configs live under
`/opt/nvidia/deepstream/deepstream-9.1/samples/configs/deepstream-app/`.

- Reference command (Quickstart):

  ```bash
  cd /opt/nvidia/deepstream/deepstream-9.1/samples/configs/deepstream-app
  deepstream-app -c source30_1080p_dec_infer-resnet_tiled_display.txt
  ```

- Headless/TTY constraint (the installer runs before a desktop session):
  Step 2 uses a **fake-sink / EGL-less** smoke variant — render a copy of a
  minimal sample config with the display `[sink0]` set to `type=1`
  (fakesink) / `enable-perf-measurement=1`, run for a bounded number of
  frames, and assert the app reaches PLAYING and emits perf/FPS output
  without error, then exits 0. This avoids requiring X/Wayland while still
  exercising decode + nvinfer (TensorRT) + tracker on the real GPU.
- Docker: the same smoke config is run inside the container with
  `--gpus all` and a fakesink; success criteria identical.
- The smoke config is a bundled asset (doc 00 §4.2, `ctx.asset_path(...)`),
  copied out to a run-scoped temp dir before execution.

A failing smoke test → `FAILED` with the captured `deepstream-app` stderr tail
and remediation pointers (driver/CUDA/TRT mismatch is the usual cause → re-run
Step 1).

### 7.4 `verify()` checklist (summary)

- [ ] Prereq pins still match (driver/CUDA/cuDNN/TRT/GStreamer) — else the
      DS runtime loader would refuse to start.
- [ ] DS SDK present at the fixed path (deb/tar) **or** image present (docker).
- [ ] `verify_pinned("DeepStream", actual, "9.1.0-1"/"9.1.0")` passes.
- [ ] `deepstream-app --version-all` reports 9.1.
- [ ] `update_rtpmanager.sh` executed; `ldconfig` run.
- [ ] `/etc/profile.d/deepstream.sh` present (host installs) exporting
      `DEEPSTREAM_DIR` + DS `bin`/`lib` on `PATH`/`LD_LIBRARY_PATH`.
- [ ] Smoke test reached PLAYING and exited 0.

---

## 8. Reconciling the DOCUMENTED DRIFT vs `00_bootstrap.sh`

[`laptop/scripts/00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh)
still targets DS 9.0 in ways Step 2 must **not** inherit (cross-referenced in
[`laptop/docs/DEEPSTREAM-SETUP.md`](../../laptop/docs/DEEPSTREAM-SETUP.md)
§5.2 "Known drift"):

1. **Acquisition model has changed.** The script downloads the DS 9.0 `.deb`
   via `ngc registry resource download-version` and installs it with
   `apt-get install ./...deb`. Step 2 targets DS 9.1, whose deb/tar are public
   **GitHub Release** assets (§5.1) — no NGC key needed for those two methods;
   only the Docker image stays NGC-gated (§5.2). Bringing the script in line
   with this spec is a follow-up implementation task, not performed by this
   document change.
2. **Prereq pins are Step 1's job.** Step 1's DS 9.1 equality pins (driver
   `595.58.03` / CUDA `13.2` / cuDNN `9.20.0.48` / TensorRT
   `10.16.0.72-1+cuda13.2`) supersede whatever the bootstrap script currently
   checks. Step 2 only *consumes* those pins (§2), it does not install or
   loosen them.

Extra scope beyond installing the DS SDK (e.g. re-syncing the legacy bash) is
**flagged for the human**, not built here (doc 00 §13).

---

## 9. Post-install steps (`run()` tail, host installs)

Applied after a successful deb/tar install (Method A/B). Docker (Method C)
skips the host profile write; the equivalent lives in the image.

1. **RTSP jitter-buffer workaround** (DS 9.1 Installation page note):

   ```bash
   sudo /opt/nvidia/deepstream/deepstream/update_rtpmanager.sh
   ```

   Bundled invocation shelled out via the framework runner. Non-zero exit is
   logged as a warning (matches `00_bootstrap.sh` Phase 7 behavior) but
   surfaced in `report()`.
2. **Linker cache** — `sudo ldconfig` (required after tar `install.sh`; cheap
   no-op after deb).
3. **Environment profile** — write `/etc/profile.d/deepstream.sh` (root,
   `chmod 0644`), porting `00_bootstrap.sh` Phase 7 `write_deepstream_profile`:

   ```sh
   export DEEPSTREAM_DIR=/opt/nvidia/deepstream/deepstream-9.1
   # prepend DS bin/lib to PATH / LD_LIBRARY_PATH (idempotent guards)
   export PATH=/opt/nvidia/deepstream/deepstream-9.1/bin:$PATH
   export LD_LIBRARY_PATH=/opt/nvidia/deepstream/deepstream-9.1/lib:$LD_LIBRARY_PATH
   ```

Each dependency touch is reported per doc 00 §8.3 ([§10](#10-reporting-strings-reportctx--inline)).

---

## 10. Reporting strings (`report(ctx)` + inline)

Use the framework reporters verbatim (doc 00 §8.3) for every dependency
touched, so the transcript is uniform/greppable:

- `report_installed("deepstream-9.1", "9.1.0-1")` after a fresh deb install →
  logs `installed deepstream-9.1 version 9.1.0-1`.
- `report_already_installed("deepstream-9.1", "9.1.0-1")` when re-run finds it
  present → logs `already installed deepstream-9.1 version 9.1.0-1`.
- tar install: `report_installed("deepstream-sdk", "9.1.0")` (no dpkg record;
  version from the SDK `version` file).
- docker: `report_installed("deepstream-image", "9.1-triton-multiarch")` after
  pull; `report_already_installed(...)` if the image tag is already local.

`report()` prints a human summary block: chosen method + reason, artifact
source (NGC-auto vs manual), DS SDK path, post-install actions performed, and
smoke-test result. No side effects.

---

## 11. DS 9.1 breaking changes relevant to install/verify

From [`laptop/docs/DEEPSTREAM-SETUP.md`](../../laptop/docs/DEEPSTREAM-SETUP.md)
"DS 9.1 breaking changes" — only the parts that affect *this step's*
install/verify:

- **`pyds` (Python bindings) deprecated in favor of `pyservicemaker`** — Step
  2 installs the SDK only; it does **not** rely on the Python bindings for
  verification. The smoke test uses `deepstream-app` (C reference app), not a
  Python script.
- **Graph Composer removed** — no Graph Composer install/verify step exists;
  do not check for it.
- **TF/UFF/Caffe removed** — the smoke test uses a stock ResNet/ETLT sample
  config that DS 9.1 still ships; do not select a sample that depends on a
  removed model format.

These are awareness constraints on *which* verification path Step 2 takes;
they do not add scope for detector/model *configuration* work (authoring or
editing inference-graph config), which stays with
[`STEP-4` §6.3](STEP-4-CALIB-OUTPUT-WIRING.md#63-config_infer_primarytxt-peoplenet--reference-only)
and later steps.

> **Open gap, not yet resolved here.** [`00` §10](00-FRAMEWORK-AND-BOOTSTRAP.md#10-ngc-api-key-capture--local-secure-storage)
> and [`STEP-4` §1](STEP-4-CALIB-OUTPUT-WIRING.md#1-scope)/[§6.3](STEP-4-CALIB-OUTPUT-WIRING.md#63-config_infer_primarytxt-peoplenet--reference-only)
> both state that Step 2 places the PeopleNet model artifacts, but §5 above
> (Acquisition) specifies only the DS SDK artifact itself — deb/tar via
> public GitHub Release, or the DS docker image via NGC — with no step that
> fetches or places a PeopleNet model file anywhere. Until that acquisition
> is actually specified (here, or explicitly reassigned elsewhere), treat
> "Step 2 owns the PeopleNet model fetch" as aspirational, not implemented.

---

## 12. `StepResult` matrix (what Step 2 returns)

| Situation | Status |
|-----------|--------|
| Step 1 pins missing/mismatched | `FAILED` (re-run Step 1) |
| deb/tar download failure (no internet / GitHub unreachable) | `USER_ACTION_REQUIRED` (manual download + placement, §5.1) |
| NGC key missing at preflight, or `docker login` fails with a stored key | `FAILED` (§2 step 5, §5.2) — a key is guaranteed present after onboarding, so either is an internal ordering bug or a real auth/network error, not an operator-recoverable state |
| Ambiguous method, interactive | prompt inline; proceeds — no special status |
| Ambiguous method, `--non-interactive` | proceed with **deb** default |
| deb/tar/docker install + post-install + smoke all pass | `COMPLETE` |
| Smoke test or version pin fails | `FAILED` (with captured stderr tail) |

Step 2 does **not** request a reboot (`REBOOT_REQUIRED` is unused here); the DS
SDK install needs no reboot on top of Step 1's driver reboot.

---

## 13. User actions Step 2 may surface

- **Manual DS artifact download** (GitHub Release unreachable from this
  machine) — download the `.deb`/tar from the `NVIDIA/DeepStream` GitHub
  Release on any machine with internet access, place it in
  `<install_dir>/downloads/deepstream/`, re-run (§5.1).
- **Manual `docker login nvcr.io`** (no NGC key, docker method) — authenticate
  to `nvcr.io` by hand, re-run (§5.2).
- **Method choice** (ambiguous auto-detect, interactive) — pick deb/tar/docker
  from the three descriptions (§4).

All rendered through the framework `USER_ACTION_REQUIRED` block ending with
"Then run the installer again to continue." (doc 00 §9.3).

---

## References

DeepStream **9.1** official documentation. DS 9.1 only.

- DS 9.1 Installation — three x86_64 methods (Debian package, tar package,
  Docker), `sudo apt-get install ./deepstream-9.1_9.1.0-1_amd64.deb`, tar
  `install.sh` + `ldconfig`, `update_rtpmanager.sh`, fixed SDK path, deb/tar
  published as GitHub Release assets:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Installation.html>
- DS 9.1 Quickstart — sample-app smoke test
  (`deepstream-app -c source30_1080p_dec_infer-resnet_tiled_display.txt`),
  Triton Docker image `nvcr.io/nvidia/deepstream:9.1-triton-multiarch`:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Quickstart.html>
- DS 9.1 Docker Containers — image tags, `docker login nvcr.io`, `--gpus all`,
  NVIDIA Container Toolkit:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_docker_containers.html>
- DS 9.1 `deepstream-app` reference (`--version` / `--version-all`):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_ref_app_deepstream.html>
- DS 9.1 Release Notes (pins / breaking changes):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Release_notes.html>
- NVIDIA/DeepStream GitHub Releases (deb/tar distribution, v9.1.0):
  <https://github.com/NVIDIA/DeepStream/releases/tag/v9.1.0>

Repo files referenced:

- [`installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md`](00-FRAMEWORK-AND-BOOTSTRAP.md)
  — framework contract (module interface, state machine, NGC handoff,
  config, reporting).
- [`laptop/docs/DEEPSTREAM-SETUP.md`](../../laptop/docs/DEEPSTREAM-SETUP.md)
  — §5 DS 9.1 install (deb/tar/docker), GitHub Release vs. NGC acquisition
  split, breaking changes, documented drift.
- [`laptop/scripts/00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh)
  — Phases 5–7 (NGC gate, DS `.deb` NGC download + install, post-install
  `update_rtpmanager.sh` + `/etc/profile.d/deepstream.sh`) that Step 2 ports.
