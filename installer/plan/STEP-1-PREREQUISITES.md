# STEP 1 — Prerequisites (owner: DevA)

Status: step spec. Depends on the shared contracts in
[`00-FRAMEWORK-AND-BOOTSTRAP.md`](00-FRAMEWORK-AND-BOOTSTRAP.md) — this doc
does **not** restate the framework (state machine, reboot detection, logging,
`Context`, `StepResult`). It links back to it and specifies only what Step 1
owns.

Step 1 installs and verifies **every DeepStream 9.1 dGPU prerequisite** on a
brand-new Ubuntu 24.04 workstation **before** the DeepStream SDK is pulled
(that is Step 2's job). Every prerequisite fact below is drawn directly from
the DS 9.1 Installation page (see [References](#references)).

Target machine (LOCKED): Ubuntu 24.04, GPU = NVIDIA RTX PRO 4500 Blackwell.

---

## 1. Module identity

Per the step-module interface in
[`00` §12](00-FRAMEWORK-AND-BOOTSTRAP.md#12-step-module-interface-the-contract-for-steps-15):

- Module: `installer/mv3dt_installer/steps/step1_prerequisites.py`
- `id = "step1_prerequisites"` (matches the `state.json` key in
  [`00` §6.2](00-FRAMEWORK-AND-BOOTSTRAP.md#62-schema)).
- `title = "Prerequisites (driver / CUDA / cuDNN / TensorRT / GStreamer)"`
- `order = 1` — the first step the dispatch loop runs.
- Consumes: `Context` (`install_dir`, `run_root`, `run_as_user`, `log`,
  `report_installed`, `report_already_installed`, `verify_pinned`,
  `asset_path`, `reboot.request`), the reboot gate
  ([`00` §7](00-FRAMEWORK-AND-BOOTSTRAP.md#7-reboot-detection--continuation-contract)),
  and the exact reporting strings
  ([`00` §8.3–8.4](00-FRAMEWORK-AND-BOOTSTRAP.md#83-reporting-format-for-dependencies-required-exact-strings)).
- Produces no NGC/download artifacts (Step 1 needs no NGC key). It writes only
  system state (packages, `/etc/modprobe.d/*`, `/etc/profile.d/*`) — never
  `state.json` directly.

This step is the port of Phases 0–4 and Phase 8 of
[`laptop/scripts/00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh)
(preflight, base deps, nouveau cleanup, NVIDIA stack, runtime/GStreamer
prereqs, version audit), rebuilt against the framework contract and the DS 9.1
GA equality pins.

---

## 2. The DS 9.1 dGPU prerequisite pins (EQUALITY)

These are the exact versions the DS 9.1 Installation page (`dGPU Setup for
Ubuntu → Prerequisites`) pins. They are **equality pins**, not minimums — DS
9.1's runtime loader refuses older or newer minors of the driver and
`libnvinfer*`. Per the DS 9.1 Installation page: *"Before installing the
DeepStream SDK, ensure you have Ubuntu 24.04, GStreamer 1.24.2, NVIDIA driver
595.58.03, CUDA 13.2, and TensorRT 10.16.0.72 installed."* Also transcribed in
[`DEEPSTREAM-SETUP.md` §4](../../laptop/docs/DEEPSTREAM-SETUP.md).

| Component | Pinned version | Install source | Verify command | `verify_pinned` label / expected |
|-----------|----------------|----------------|----------------|----------------------------------|
| NVIDIA driver | `595.58.03` | `.run` installer (`NVIDIA-Linux-x86_64-595.58.03.run`) | `nvidia-smi --query-gpu=driver_version --format=csv,noheader` | `verify_pinned("NVIDIA driver", <actual>, "595.58.03")` |
| CUDA Toolkit | `13.2` (`cuda-toolkit-13-2`) | NVIDIA `ubuntu2404/x86_64` apt repo | `nvcc --version` → `release X.Y` | `verify_pinned("CUDA (nvcc release)", <actual>, "13.2")` |
| cuDNN | `9.20.0.48` | apt (`libcudnn9*`) | `dpkg -l \| grep libcudnn9` | `verify_pinned("cuDNN (libcudnn9)", <actual>, "9.20.0.48")` |
| TensorRT | `10.16.0.72-1+cuda13.2` | apt (all `libnvinfer*` pinned) | `dpkg -l \| grep libnvinfer10` | `verify_pinned("TensorRT (libnvinfer10)", <actual>, "10.16.0.72-1+cuda13.2")` |
| GStreamer | `1.24.2` | apt (`gstreamer1.0-*`) | `gst-inspect-1.0 --version` | `verify_pinned("GStreamer", <actual>, "1.24.2")` |
| OS | Ubuntu 24.04 / `x86_64` | (precondition) | `lsb_release -rs`, `uname -m` | preflight check, not `verify_pinned` |

### 2.1 TensorRT package set (all pinned to one version)

Every `libnvinfer*` package MUST be pinned to the same string
`10.16.0.72-1+cuda13.2` (from the DS 9.1 Installation page):

```
version="10.16.0.72-1+cuda13.2"
libnvinfer-dev                       libnvinfer-dispatch-dev
libnvinfer-dispatch10                libnvinfer-headers-dev
libnvinfer-headers-plugin-dev        libnvinfer-safe-headers-dev
libnvinfer-lean-dev                  libnvinfer-lean10
libnvinfer-plugin-dev                libnvinfer-plugin10
libnvinfer-vc-plugin-dev             libnvinfer-vc-plugin10
libnvinfer10                         libnvonnxparsers-dev
libnvonnxparsers10                   tensorrt-dev
libnvinfer-headers-python-plugin-dev libnvinfer-win-builder-resource10
```

### 2.2 Reporting each pin

For every component above, `run()` emits exactly one of the two required
strings from
[`00` §8.3](00-FRAMEWORK-AND-BOOTSTRAP.md#83-reporting-format-for-dependencies-required-exact-strings):

- Newly installed → `report_installed(dep, version)` →
  `installed <dependency> version <version>`
- Already at the pinned version → `report_already_installed(dep, version)` →
  `already installed <dependency> version <version>`

The "already installed" path is taken when a probe (see §7.2) finds the
component present **at the exact pinned version**. Examples:

```
installed cuda-toolkit-13-2 version 13.2
already installed gstreamer1.0-tools version 1.24.2
installed libnvinfer10 version 10.16.0.72-1+cuda13.2
already installed nvidia-driver version 595.58.03
```

`verify()` then re-checks every pin with `verify_pinned` (§7.3) and only
returns `COMPLETE` when all match.

---

## 3. DS 9.1 §4.1 apt prerequisite package list

Transcribed from
[`DEEPSTREAM-SETUP.md` §4.1](../../laptop/docs/DEEPSTREAM-SETUP.md) and
cross-checked against the DS 9.1 Installation page "Install prerequisite
packages" block. `run()` installs these in a single apt
transaction, reporting each with the §8.3 strings.

```bash
apt install \
  libssl3 libssl-dev libcurl4-openssl-dev libgles2-mesa-dev \
  libgstreamer1.0-0 gstreamer1.0-tools gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  libgstreamer-plugins-base1.0-dev libgstrtspserver-1.0-0 \
  libjansson4 libyaml-cpp-dev libjsoncpp-dev protobuf-compiler \
  libmosquitto1 gcc make git python3 \
  mosquitto mosquitto-clients arp-scan ffmpeg
```

### 3.1 Authoritative vs. repo-added packages

The DS 9.1 Installation page's own "Install prerequisite packages" list is:

```
libssl3 libssl-dev libcurl4-openssl-dev libgstreamer1.0-0 gstreamer1.0-tools
gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
gstreamer1.0-libav libgstreamer-plugins-base1.0-dev libgstrtspserver-1.0-0
libjansson4 libyaml-cpp-dev libmosquitto1
```

The following are **repo additions** carried by
[`DEEPSTREAM-SETUP.md` §4.1](../../laptop/docs/DEEPSTREAM-SETUP.md) and
[`00_bootstrap.sh` Phase 4](../../laptop/scripts/00_bootstrap.sh), not on the
DS 9.1 §4.1 authoritative list, but harmless/required downstream:
`libgles2-mesa-dev`, `libjsoncpp-dev`, `protobuf-compiler`, `gcc`, `make`,
`git`, `python3`. Keep them (they satisfy build tooling and the DS msgconv /
protobuf paths), and report each with the §8.3 strings like any other
dependency. See the open decision in [§9](#9-open-decisions-for-the-human).

Four further repo additions are carried for the installer's own subsystems
rather than for DeepStream itself. They are on the §3 list, are not on the DS
9.1 §4.1 authoritative list, and each has exactly one consumer:

| Package | Why Step 1 installs it | Consumer |
|---------|------------------------|----------|
| `mosquitto` | the MQTT **broker daemon** the MV3DT sink publishes into; without it there is no listener on `127.0.0.1:1883` | [§3.2](#32-mosquitto-broker), [`STEP-6` §E.1](STEP-6-REMOTE-SUPERVISION.md#e1-lifecycle) |
| `mosquitto-clients` | `mosquitto_sub` / `mosquitto_pub`, used by the broker reachability probe and by every documented validation helper | [`STEP-5` §6](STEP-5-PER-PROJECT-EXES.md#6-validation--monitoring) |
| `arp-scan` | the MAC-OUI sweep that finds the cameras on the link-local segment; needs raw sockets, which the installer already has | [`00` §15](00-FRAMEWORK-AND-BOOTSTRAP.md#15-camera-discovery) |
| `ffmpeg` | supplies `ffprobe` for the RTSP stream probe and `ffmpeg` for the single-frame still capture used in guided position binding | [`00` §15](00-FRAMEWORK-AND-BOOTSTRAP.md#15-camera-discovery), [`STEP-5` §3.3](STEP-5-PER-PROJECT-EXES.md#33-what-the-exe-does-at-runtime-pipeline-subcommand) |

> `libmosquitto1` MUST NOT be omitted — it is the DS MQTT protocol client lib
> required by `Gst-nvmsgbroker`
> ([`DEEPSTREAM-SETUP.md` §4.1](../../laptop/docs/DEEPSTREAM-SETUP.md)). It is
> **not** the broker: it is a shared library that `libnvds_mqtt_proto.so`
> links against, and installing it leaves nothing listening on port `1883`.
> The broker daemon is the separate `mosquitto` package — see
> [§3.2](#32-mosquitto-broker).

`gstreamer1.0-plugins-rtp` / `gstreamer1.0-rtsp` from
[`00_bootstrap.sh` Phase 4](../../laptop/scripts/00_bootstrap.sh) are optional
extras and may be included in the same transaction; they are not on the DS 9.1
§4.1 list.

### 3.2 Mosquitto broker

**LOCKED — Step 1 owns the Mosquitto broker.** Installing the broker daemon
and its `/etc/mosquitto/conf.d/mv3dt.conf` drop-in is Step 1 work, executed as
part of this step's `run()`. This closes gap 1 in
[`DELETION-REVIEW` §6](DELETION-REVIEW.md#6-coverage-gaps-this-triage-exposed),
which recorded that no step claimed the broker while
[`STEP-6` §E.1](STEP-6-REMOTE-SUPERVISION.md#e1-lifecycle) `preflight`
**requires** a reachable broker and
[`STEP-6` §D](STEP-6-REMOTE-SUPERVISION.md#d-security-remote-control-must-be-authenticated)
**rewrites** its configuration.

The `libmosquitto1` line in §3 is a different thing and does not satisfy this
requirement. Keep the two straight:

| Package | Role | Provides |
|---------|------|----------|
| `libmosquitto1` | DeepStream's MQTT **client library** (DS 9.1 §4.1 prerequisite) | the shared object `libnvds_mqtt_proto.so` links against |
| `mosquitto` | the MQTT **broker daemon** | `mosquitto.service`, the `127.0.0.1:1883` listener, `/etc/mosquitto/conf.d/` |

#### Execution: the bundled script, never a typed command

Step 1 does not re-implement the broker setup in Python. It runs the bundled
script, staged as a **tree** so `source "$SCRIPT_DIR/lib/common.sh"` resolves
([`00` §4.2](00-FRAMEWORK-AND-BOOTSTRAP.md#42-locating-bundled-assets-at-runtime)):

```python
shellout.run_bundled_script("scripts", "10_setup_mosquitto.sh", tree=("scripts",))
```

The script ships inside the release binary
([`00` §5](00-FRAMEWORK-AND-BOOTSTRAP.md#5-distribution-the-github-release-binary)),
so the operator never types a script name and no repo checkout is involved.
Its stdout and stderr land in the transcript through the shared logger
([`00` §8.2](00-FRAMEWORK-AND-BOOTSTRAP.md#82-transcript-log-file)).

What the script does, in order:

1. **Packages**: `apt-get install -y --no-install-recommends mosquitto
   mosquitto-clients libmosquitto1` when the broker is missing; a no-op when
   it is already present.
2. **Drop-in**: install the bundled `mv3dt.conf` to
   `/etc/mosquitto/conf.d/mv3dt.conf` by atomic replace (write a temp file in
   the destination directory, then `mv`), so a half-written config is never
   visible to a restarting broker.
3. **Service**: `systemctl enable mosquitto` then `systemctl restart
   mosquitto`, and confirm with `systemctl is-active --quiet mosquitto`.
4. **Firewall (optional, off by default)**: `--with-firewall` opens `1883/tcp`
   and `9001/tcp` via `ufw`. Step 1 does not pass it; the default posture
   assumes a workstation on a private segment.

#### Idempotency and reporting

Re-running is safe: apt is skipped when the packages are present, the drop-in
is compared before replacement, and `restart` is unconditional but cheap. Step
1 reports the outcome with the §8.3 strings like any other dependency —
`installed mosquitto version <dpkg version>` on first install,
`already installed mosquitto version <dpkg version>` thereafter — and reports
the drop-in itself as `installed mv3dt.conf version <sha256[:12]>` /
`already installed mv3dt.conf version <sha256[:12]>` so config drift is
visible in the transcript.

`verify()` (§7.3) treats the broker as a pass/fail check, not a pinned
version: `systemctl is-active --quiet mosquitto` succeeds and
`/etc/mosquitto/conf.d/mv3dt.conf` exists with the expected contents. Ubuntu
24.04's `mosquitto` version is not pinned — nothing in the DS 9.1 docs or in
[`mv3dt.conf`](../../laptop/mosquitto/mv3dt.conf) depends on a specific
broker minor, so an equality pin here would create false failures with no
corresponding benefit.

Failure surface: apt failure or the broker refusing to start → `FAILED` with
the `systemctl status mosquitto --no-pager` output in the transcript. Step 1
never returns `USER_ACTION_REQUIRED` for the broker; there is no manual
fallback to point the operator at.

---

## 4. First-install caveats (install-time preconditions)

The DS 9.1 Installation page assumes a workstation with the standard dev
toolchain, no previous NVIDIA stack, Secure Boot disabled, and `nouveau`
already out of the way. On a brand-new Ubuntu 24.04 box those assumptions do
not hold. Step 1 MUST satisfy each of the following **before** running the
`.run` driver installer (§5 step 4). These mirror the caveat table in
[`DEEPSTREAM-SETUP.md` §4 first-install caveats](../../laptop/docs/DEEPSTREAM-SETUP.md)
and Phases 1–2 of
[`00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh).

| # | Precondition | Action Step 1 takes | Outcome |
|---|--------------|---------------------|---------|
| 1 | Kernel headers for `.run` to build `nvidia.ko` | `apt install build-essential dkms linux-headers-$(uname -r)` | reported via §8.3 |
| 2 | `add-apt-repository` / `apt-key` present on minimal 24.04 | `apt install software-properties-common ca-certificates gnupg curl` | reported via §8.3 |
| 3 | Distro `nvidia-*` / `libnvidia-*` conflict with `.run` | `apt purge 'nvidia-*' 'libnvidia-*'` + `apt autoremove` | may set `REBOOT_REQUIRED` (§5) |
| 4 | `nouveau` will abort the `.run` installer | write `/etc/modprobe.d/blacklist-nouveau.conf` (`blacklist nouveau` + `options nouveau modeset=0`), `update-initramfs -u` | `REBOOT_REQUIRED` if nouveau was loaded (§5) |
| 5 | Secure Boot → unsigned `nvidia.ko` | probe `mokutil --sb-state`; if enabled, surface `USER_ACTION_REQUIRED` (disable Secure Boot in BIOS **or** complete MOK enrollment on next boot) | operator action; MOK/BIOS is out of scope ([`00` §13](00-FRAMEWORK-AND-BOOTSTRAP.md#13-out-of-scope--defer-to-human)) |
| 6 | GDM/Xorg holding the GPU during `.run` | `service gdm stop` (fallback `lightdm`), `pkill -9 Xorg` before running `.run`; if it still fails under a desktop, surface a `USER_ACTION_REQUIRED` telling the operator to switch to a TTY (`Ctrl+Alt+F3`) and re-run | precondition for §5 step 4 |
| 7 | CUDA 13.2 not on `PATH` / `LD_LIBRARY_PATH` | write `/etc/profile.d/cuda.sh` exporting `/usr/local/cuda-13.2/bin` and `/usr/local/cuda-13.2/lib64` | enables `nvcc` in `verify()` |

Notes:

- **Nouveau blacklist + `update-initramfs -u`** and the **distro-`nvidia-*`
  purge** are the two reboot triggers (§5). They match Phase 2 of
  [`00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh).
- **Secure Boot / MOK enrollment and any BIOS interaction are out of scope**
  ([`00` §13](00-FRAMEWORK-AND-BOOTSTRAP.md#13-out-of-scope--defer-to-human));
  Step 1 detects and surfaces them as `USER_ACTION_REQUIRED`, it does not
  perform them.
- Config files written here (`blacklist-nouveau.conf`, `cuda.sh`) reuse the
  same canonical paths and "Managed by …" header discipline the existing
  bootstrap uses.

---

## 5. Order of operations

Step 1 mirrors the DS 9.1 Installation page ordering (apt prereqs → CUDA repo
→ driver → reboot → TensorRT/cuDNN → verify). Because the driver `.run`
requires a reboot mid-step, the work is split across **two launches** using
the framework's reboot gate
([`00` §7](00-FRAMEWORK-AND-BOOTSTRAP.md#7-reboot-detection--continuation-contract)).

**Launch A — up to and including the driver install:**

1. **Preflight** (§7.1): Ubuntu 24.04 + `x86_64`; root; NVIDIA GPU present via
   `lspci` (driver not required yet).
2. **Base deps + caveats 1–2** (§4): `build-essential`, `dkms`,
   `linux-headers-$(uname -r)`, `software-properties-common`,
   `ca-certificates`, `gnupg`, `curl`.
3. **DS 9.1 §4.1 apt prerequisites** (§3), single transaction.
4. **CUDA repo + keyring** and `apt-get install cuda-toolkit-13-2`
   (DS 9.1 §4.2). Write `/etc/profile.d/cuda.sh` (caveat 7).
5. **Nouveau + old-NVIDIA cleanup** (caveats 3–4). If nouveau was loaded or a
   distro `nvidia-*` package was purged → **return `REBOOT_REQUIRED`** now
   (before touching the `.run`), because the `.run` cannot build against a
   live nouveau.
6. **Secure Boot check** (caveat 5). If enabled → `USER_ACTION_REQUIRED`.
7. **Stop GDM/Xorg** (caveat 6), then **run the driver `.run`**
   (`NVIDIA-Linux-x86_64-595.58.03.run --no-cc-version-check`).
8. **Return `REBOOT_REQUIRED`** — the driver kernel module needs a reboot to
   load (§6).

**Launch B — after the confirmed reboot (§6):**

9. **TensorRT** (all `libnvinfer*` pinned to `10.16.0.72-1+cuda13.2`) and
   **cuDNN** (`libcudnn9*` at `9.20.0.48`) via apt (DS 9.1 §4.4).
10. **GStreamer** pin is satisfied by the §3 prereq set; confirm `1.24.2`.
11. **Mosquitto broker** ([§3.2](#32-mosquitto-broker)): run the bundled
    `10_setup_mosquitto.sh` to install the daemon, drop in `mv3dt.conf`, and
    enable + restart the service. Placed here because nothing in the NVIDIA
    stack depends on it and the broker survives the driver reboot untouched;
    it must nevertheless complete inside Step 1, since
    [`STEP-6` §E.1](STEP-6-REMOTE-SUPERVISION.md#e1-lifecycle) `preflight`
    fails without a reachable broker.
12. **`verify()`** (§7.3): every pin via `verify_pinned`; confirm the driver
    now loads (`nvidia-smi` succeeds and reports `595.58.03`) and the broker
    is active.
13. On all-match → `COMPLETE`; the dispatch loop advances to Step 2.

> The single reboot in this spec is the **driver `.run`** reboot (step 8). The
> nouveau/purge reboot (step 5) only fires on machines that shipped with
> nouveau loaded or a distro driver preinstalled; on a clean image it is a
> no-op and Launch A proceeds straight to the driver `.run`. Both use the same
> reboot gate, so a machine may legitimately require **two** reboots before
> Step 1 completes; the framework resumes at the first incomplete step each
> time.

### 5.1 Driver install method: `.run` runfile (LOCKED, verified against NVIDIA docs)

This spec installs the driver with the **`.run` runfile installer**, matching
the DS 9.1 Installation page verbatim. Verified against the authoritative
source (DS 9.1 Installation page, "Install the DeepStream SDK → Install
Dependencies → Install NVIDIA driver 595.58.03"), which prescribes exactly:

```bash
$ chmod 755 NVIDIA-Linux-x86_64-595.58.03.run
$ sudo ./NVIDIA-Linux-x86_64-595.58.03.run --no-cc-version-check
```

The DS 9.1 Installation page documents **no** `.deb`/apt path for the *driver*
— only CUDA Toolkit `13.2`, TensorRT `10.16.0.72`, and cuDNN `9.20.0.48` are
installed from apt/deb packages (§3, §5). The existing
[`00_bootstrap.sh` Phase 3](../../laptop/scripts/00_bootstrap.sh) diverges by
using the NVIDIA **local-repo `.deb`** (an older `cuda-drivers-590` pin); Step
1 supersedes that and uses the `.run` because "use what the NVIDIA docs say"
is the ruling constraint. Only the `.run` path is documented.

The `.run` file is a bundled asset located via `ctx.asset_path(...)`
([`00` §4.2](00-FRAMEWORK-AND-BOOTSTRAP.md#42-locating-bundled-assets-at-runtime))
or, if too large to bundle, surfaced as a `USER_ACTION_REQUIRED` download from
NVIDIA's driver download search (<https://www.nvidia.com/en-us/drivers/>) for
driver `595.58.03` — resolve the exact `driver/details/<id>` URL for that
build at implementation time rather than hardcoding one here, since it is
driver-build-specific and not part of the DS 9.1 documentation set.
Bundled-vs-download is the only remaining DevA implementation choice here
(see [§9](#9-open-decisions-for-the-human)); the method itself is settled.

---

## 6. Reboot gating (Step 1's use of the §7 contract)

Step 1 consumes the reboot contract in
[`00` §7](00-FRAMEWORK-AND-BOOTSTRAP.md#7-reboot-detection--continuation-contract).
It uses the **auto-complete-on-reboot** variant, not self-verification:

### 6.1 When Step 1 returns `REBOOT_REQUIRED`

- After the **driver `.run`** completes (§5 step 8) — always.
- After a **nouveau blacklist that was applied while nouveau was loaded**, or
  after **purging a preinstalled distro `nvidia-*`** (§5 step 5) — only when
  that state actually changed.

`run()` returns `StepResult(status=REBOOT_REQUIRED, message=..., user_actions=[...])`
via `ctx.reboot.request()`. The framework then stores
`/proc/sys/kernel/random/boot_id`
([`00` §7.1](00-FRAMEWORK-AND-BOOTSTRAP.md#71-signaling-reboot-required)),
prints the reboot block, and exits 0.

### 6.2 User-action text displayed on `REBOOT_REQUIRED`

Rendered inside the framework's reboot frame
([`00` §9.4](00-FRAMEWORK-AND-BOOTSTRAP.md#94-reboot-block)). The `UserAction`
list Step 1 supplies:

1. text: "The NVIDIA driver 595.58.03 kernel module is installed but not yet
   loaded. Reboot so `nvidia.ko` loads before TensorRT/cuDNN install."
2. text: "CUDA 13.2 has been added to PATH/LD_LIBRARY_PATH for new login
   shells." — path: `/etc/profile.d/cuda.sh` (written by Step 1; sourced
   automatically on next login).
3. command: `sudo reboot`

The frame's closing line is always the framework's contract phrase
**"Then run the installer again to continue."**
([`00` §9.3](00-FRAMEWORK-AND-BOOTSTRAP.md#93-user-action-display-contract)).

### 6.3 Confirming the reboot happened before Step 2

On the next launch, `reboot.reconcile()`
([`00` §7.2](00-FRAMEWORK-AND-BOOTSTRAP.md#72-detecting-the-reboot-actually-happened))
compares the stored boot-id to the current one:

- **Different boot-id** → real reboot confirmed. The framework auto-marks
  Step 1's requesting stage complete and dispatch continues.
- **Same boot-id** → operator re-ran without rebooting → the reboot block is
  re-printed and the loop refuses to advance.

Because Step 1 uses auto-complete, its **post-reboot** work (TensorRT/cuDNN,
final verify) lives in the same step module and runs on the launch after the
confirmed reboot: the dispatch loop re-enters `step1` (still not `COMPLETE`
in `state.json` — the driver reboot was one of two internal stages), and
`preflight()` detects that the driver stage is done (driver `.run` applied +
reboot confirmed) and proceeds to the TensorRT/cuDNN stage. Step 1 tracks its
own internal A/B stage via an idempotent probe (driver present + `nvidia-smi`
loads), **not** by writing `state.json` (which only the framework owns,
[`00` §12.2](00-FRAMEWORK-AND-BOOTSTRAP.md#122-stepresult-and-status-recorded-by-the-state-machine)).

> Guard: if after the confirmed reboot `nvidia-smi` still fails (driver did
> not load — e.g. Secure Boot rejected the unsigned module), Step 1 returns
> `USER_ACTION_REQUIRED` with the MOK/Secure-Boot remediation rather than
> `COMPLETE`, blocking Step 2.

---

## 7. Lifecycle behavior (`preflight`/`run`/`verify`/`report`)

Against the protocol in
[`00` §12.1](00-FRAMEWORK-AND-BOOTSTRAP.md#121-protocol).

### 7.1 `preflight(ctx)`

- Assert Ubuntu 24.04 + `x86_64` (`lsb_release`, `uname -m`); NVIDIA GPU via
  `lspci`. On mismatch → `FAILED` with a pointer to
  [`DEEPSTREAM-SETUP.md` §2–3](../../laptop/docs/DEEPSTREAM-SETUP.md).
- Determine internal stage: **Stage A** (driver not yet installed/loaded) vs
  **Stage B** (driver `595.58.03` present and `nvidia-smi` loads). The probe
  is idempotent so re-runs are safe.
- Returns `COMPLETE` ("ok to run") unless the OS/arch gate fails.

### 7.2 `run(ctx)`

- Executes the §5 order for the current stage. Bundled bash fragments (apt
  transactions, nouveau blacklist, the `.run` invocation) are shelled out via
  `ctx.run_root(...)` after being located with `ctx.asset_path(...)`
  ([`00` §4.2](00-FRAMEWORK-AND-BOOTSTRAP.md#42-locating-bundled-assets-at-runtime)).
- The Mosquitto broker is installed through the bundled script
  ([§3.2](#32-mosquitto-broker)) rather than open-coded apt/systemctl calls.
- Every dependency touched is reported with `report_installed` /
  `report_already_installed` (§2.2, §3, §3.2). The "already installed" path is
  taken when a pre-check (`dpkg -s` / `nvidia-smi` / `nvcc` /
  `gst-inspect-1.0`) shows the component already at the pinned version.
- May return `REBOOT_REQUIRED` (§6), `USER_ACTION_REQUIRED` (Secure Boot,
  GDM-stop failure, driver failed to load), `FAILED` (apt/`.run` error), or
  `COMPLETE`.

### 7.3 `verify(ctx)` — the pinned checklist

Idempotent; returns `COMPLETE` only when **every** check passes. Each maps a
verification command to `verify_pinned`
([`00` §8.4](00-FRAMEWORK-AND-BOOTSTRAP.md#84-verify-at-exact-pinned-version-helper-required)):

```
nvidia-smi --query-gpu=driver_version --format=csv,noheader
        -> verify_pinned("NVIDIA driver", <out>, "595.58.03")
nvcc --version   (parse "release X.Y")
        -> verify_pinned("CUDA (nvcc release)", <out>, "13.2")
dpkg -l | grep libcudnn9   (extract version field)
        -> verify_pinned("cuDNN (libcudnn9)", <out>, "9.20.0.48")
dpkg -s libnvinfer10   (Version:)
        -> verify_pinned("TensorRT (libnvinfer10)", <out>, "10.16.0.72-1+cuda13.2")
gst-inspect-1.0 --version   (parse "version X.Y.Z")
        -> verify_pinned("GStreamer", <out>, "1.24.2")
```

Any `verify_pinned` returning `False` → `verify()` returns
`USER_ACTION_REQUIRED` (or `FAILED`) with the mismatch, never `COMPLETE`. This
is the hard gate that Phase 8 of
[`00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh) performs, ported to
the framework helper.

### 7.4 `report(ctx)`

Prints the human summary block (no side effects): each pin and whether it was
newly installed vs already present, plus where the two config files landed
(`/etc/profile.d/cuda.sh`, `/etc/modprobe.d/blacklist-nouveau.conf`).

---

## 8. Blackwell (RTX PRO 4500) compatibility check

**Result: the pinned driver `595.58.03` and CUDA `13.2` support the RTX PRO
4500 Blackwell. No pin change is required.**

- DS 9.1 **Platform and OS Compatibility → dGPU**: the dGPU compatibility
  table lists supported architectures as *"Turing, Ampere, Hopper, ADA,
  Blackwell"* alongside Ubuntu 24.04, GCC 11.4.0, CUDA 13.2, cuDNN 9.20.0.48,
  TRT 10.16.0.72, and Display Driver R595.58.03.
- DS 9.1 **Installation**: the dGPU setup introduction lists *"NVIDIA
  GeForce® RTX pro 4500 and GeForce®/NVIDIA RTX/QUADRO"* among the supported
  dGPU products.

So Step 1 keeps the exact pins in §2. The RTX PRO 4500 is named as a supported
device at these exact versions; there is no evidence a newer driver/CUDA minor
is required for Blackwell.

`verify()` MAY additionally record the GPU's compute capability for the
transcript (informational, not a gate):

```
nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader
```

> Escalation rule (per LOCKED scope): if a future device or a driver
> regression means `595.58.03` / CUDA `13.2` do **not** enumerate the RTX PRO
> 4500 (e.g. `nvidia-smi` shows the GPU as unsupported, or CUDA cannot see
> it), Step 1 must **not** silently bump the pins — it returns
> `USER_ACTION_REQUIRED` and the version bump is escalated as an open decision
> ([§9](#9-open-decisions-for-the-human)).

---

## 9. Open decisions for the human

DevA does not invent new scope; the following need a human decision (per the
LOCKED constraints):

1. **Blackwell driver support — RESOLVED as a confirmation, not a change.** DS
   9.1 docs explicitly list the RTX PRO 4500 / Blackwell as supported at driver
   `595.58.03` + CUDA `13.2` + cuDNN `9.20.0.48` + TRT `10.16.0.72` (§8). No pin
   change. Flagged here only so the human can confirm the workstation's actual
   `nvidia-smi` output matches on first real hardware run; if it does not, the
   pin bump is a human decision, not an automatic one.
2. **Driver install method — RESOLVED: `.run` runfile.** Verified against the
   DS 9.1 Installation page: NVIDIA documents only the `.run`
   runfile for the driver (`NVIDIA-Linux-x86_64-595.58.03.run --no-cc-version-check`);
   there is no documented `.deb`/apt driver path. Step 1 uses the `.run`
   accordingly and supersedes the local-repo `.deb` driver install in the
   existing [`00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh). This
   accepts the GDM-stop / TTY / MOK friction (caveats 5–6) as the documented
   cost. The **only** residual DevA choice is whether the `.run` ships
   **bundled** in the binary or is fetched via a `USER_ACTION_REQUIRED` download
   (§5.1).
3. **apt prereq superset.** Step 1 installs the DS 9.1 §4.1 list **plus** the
   repo additions in §3.1 (`libgles2-mesa-dev`, `libjsoncpp-dev`,
   `protobuf-compiler`, `gcc`, `make`, `git`, `python3`). Confirm the superset
   is acceptable (recommended — they are needed downstream) or trim to the DS
   9.1 §4.1 authoritative list only. The four installer-subsystem additions
   (`mosquitto`, `mosquitto-clients`, `arp-scan`, `ffmpeg`) are **not** part of
   this open decision — they are RESOLVED and required by
   [§3.2](#32-mosquitto-broker) and
   [`00` §15](00-FRAMEWORK-AND-BOOTSTRAP.md#15-camera-discovery).
4. **Documented drift from the existing script** — see [§10](#10-documented-drift-from-00_bootstrapsh);
   confirm the equality-pin enforcement replaces the old `>= 550` / `>= 12.4`
   preflight.

---

## 10. Documented drift from `00_bootstrap.sh`

The existing
[`00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh) preflight and
[`laptop/deepstream/*`](../../laptop/deepstream/) still target the older DS
9.0 pins and NGC-based acquisition — that script/config tree has not yet been
updated to match this spec (noted in
[`DEEPSTREAM-SETUP.md` §5.2 "Known drift"](../../laptop/docs/DEEPSTREAM-SETUP.md)).

**Step 1 targets the DS 9.1 GA equality pins in §2**: driver
**`== 595.58.03`**, CUDA **`== 13.2`**, cuDNN `== 9.20.0.48`, TRT
`== 10.16.0.72-1+cuda13.2`, GStreamer `== 1.24.2`, all enforced through
`verify_pinned` (§7.3). Until `00_bootstrap.sh` is updated to match, running it
against this spec's expectations will under-provision the driver/CUDA/TRT/cuDNN
stack and `verify()` will fail.

Note: Phase 8 of the current script hard-gates equality values via
`require_version_eq`, but those values are still the DS 9.0 pins — the drift
now spans the full stack (driver, CUDA, cuDNN, TensorRT, DS SDK acquisition),
not just the preflight thresholds. Step 1 makes the DS 9.1 equality pin the
single, consistent gate from preflight through verify; bringing
`00_bootstrap.sh` up to match is a follow-up implementation task, not
something this spec change performs by itself.

---

## 11. What the operator must do after this step

- Nothing further **if** `verify()` returned `COMPLETE` — the dispatch loop
  advances straight to Step 2.
- If Step 1 returned `REBOOT_REQUIRED`: `sudo reboot`, then **run the
  installer again to continue** (framework prints this).
- If Step 1 returned `USER_ACTION_REQUIRED` for **Secure Boot / MOK**: disable
  Secure Boot in BIOS, **or** complete MOK Manager enrollment on the next
  boot, then re-run. (BIOS/MOK are out of scope for the installer,
  [`00` §13](00-FRAMEWORK-AND-BOOTSTRAP.md#13-out-of-scope--defer-to-human).)
- If Step 1 returned `USER_ACTION_REQUIRED` for **GDM/desktop**: switch to a
  TTY (`Ctrl+Alt+F3`), log in, re-run the installer from there.
- New login shells pick up CUDA 13.2 automatically from
  `/etc/profile.d/cuda.sh`; a shell open from before the reboot must
  `source /etc/profile.d/cuda.sh` or re-login for `nvcc` to be on `PATH`.

---

## 12. `verify()` checklist (developer quick-reference)

Step 1 is `COMPLETE` iff **all** of these pass:

- [ ] `lsb_release -rs` = `24.04` and `uname -m` = `x86_64`.
- [ ] `nvidia-smi` runs (driver loaded) and driver_version == `595.58.03`.
- [ ] `nvcc --version` release == `13.2` (CUDA on PATH via
      `/etc/profile.d/cuda.sh`).
- [ ] `dpkg -l | grep libcudnn9` version contains `9.20.0.48`.
- [ ] `dpkg -s libnvinfer10` Version == `10.16.0.72-1+cuda13.2` (and the full
      `libnvinfer*` set from §2.1 all at that version).
- [ ] `gst-inspect-1.0 --version` == `1.24.2`.
- [ ] DS 9.1 §4.1 apt prereqs (§3) all installed, including `mosquitto`,
      `mosquitto-clients`, `arp-scan`, and `ffmpeg` (§3.1).
- [ ] `systemctl is-active --quiet mosquitto` succeeds and
      `/etc/mosquitto/conf.d/mv3dt.conf` matches the bundled config (§3.2).
- [ ] No `reboot_pending` marker for `step1_prerequisites`
      ([`00` §7](00-FRAMEWORK-AND-BOOTSTRAP.md#7-reboot-detection--continuation-contract)).
- [ ] (informational) `nvidia-smi --query-gpu=name,compute_cap` recorded for
      the RTX PRO 4500 (§8).

Each checked item is reported with the §8.3 strings and validated with
`verify_pinned` (§8.4).

---

## References

DeepStream 9.1 official documentation only. Reference DS 9.1 only.

- DS 9.1 Installation (authority for the dGPU prerequisites §4.1–4.4, the
  driver/CUDA/cuDNN/TRT/GStreamer pins, and install ordering):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Installation.html>
- DS 9.1 Overview (platform support incl. RTX PRO 4500 / Blackwell):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Overview.html>
- DS 9.1 Release Notes:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Release_notes.html>
- DS 9.1 Quickstart (post-install sample-app smoke test — Step 2's concern):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Quickstart.html>

Facts confirmed directly against the pages above:

- "Before installing the DeepStream SDK, ensure you have Ubuntu 24.04,
  GStreamer 1.24.2, NVIDIA driver 595.58.03, CUDA 13.2, and TensorRT
  10.16.0.72 installed."
- CUDA Toolkit 13.2 via `cuda-toolkit-13-2` from the `ubuntu2404/x86_64` repo.
- TensorRT `version="10.16.0.72-1+cuda13.2"` across all `libnvinfer*` packages.
- dGPU compatibility table: "Turing, Ampere, Hopper, ADA, Blackwell" with
  Ubuntu 24.04, GCC 11.4.0, CUDA 13.2, cuDNN 9.20.0.48, TRT 10.16.0.72,
  Display Driver R595.58.03.

Repo files referenced:

- [`installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md`](00-FRAMEWORK-AND-BOOTSTRAP.md)
  — shared framework contracts consumed by this step.
- [`laptop/docs/DEEPSTREAM-SETUP.md`](../../laptop/docs/DEEPSTREAM-SETUP.md) —
  §4 pins, §4.1 apt list, first-install caveats, §5.2 known drift.
- [`laptop/scripts/00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh) —
  Phases 0–4 + Phase 8 this step ports; source of the `>= 550` / `>= 12.4`
  preflight drift.
- [`laptop/scripts/lib/common.sh`](../../laptop/scripts/lib/common.sh) —
  `require_version_eq` (ported to `verify_pinned`), logging, `require_root`.
- [`laptop/scripts/10_setup_mosquitto.sh`](../../laptop/scripts/10_setup_mosquitto.sh)
  — the original of the bundled broker-setup script this step now owns and
  runs (§3.2).
- [`laptop/mosquitto/mv3dt.conf`](../../laptop/mosquitto/mv3dt.conf) — the
  drop-in installed to `/etc/mosquitto/conf.d/mv3dt.conf` (§3.2).
