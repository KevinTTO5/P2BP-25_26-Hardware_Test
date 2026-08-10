# 00 — Installer Framework and Bootstrap (owner: DevA)

Status: shared foundation. The five step docs
(`STEP-1-PREREQUISITES.md` … `STEP-5-PER-PROJECT-EXES.md`) depend on the
contracts defined here. Do not restate these contracts in step docs — link
back to this document.

This document specifies the **single self-contained installer binary** that
unifies the loose numbered scripts under
[`laptop/scripts/`](../../laptop/scripts/) into one resumable, CD-style
installer for a brand-new Ubuntu 24.04 workstation
(target GPU: NVIDIA RTX PRO 4500 Blackwell) targeting DeepStream 9.0 +
AutoMagicCalib.

It supersedes and wraps-and-ports the phase logic in
[`laptop/scripts/00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh) and
the numbered runtime scripts. The DeepStream/OS reference facts come from the
DS 9.0 docs (see [References](#references)); all version pins were
cross-checked against Context7 library
`/websites/nvidia_metropolis_deepstream_dev-guide`.

---

## 1. Goals and non-goals

Goals:

- One artifact the operator runs: a single binary that behaves like a GUI
  installer for path selection but is TUI/CLI-first, so it runs reliably
  before the NVIDIA driver / desktop session is fully up.
- Fully resumable: survive the multiple reboots that the driver/CUDA install
  requires, and resume at the exact step on the next launch.
- Verbose, auditable: every dependency touch is reported with name + exact
  version, in a fixed human-readable format, and written to a transcript log.
- Five independently-buildable steps behind a stable module contract.

Non-goals (see [§13](#13-out-of-scope--defer-to-human)): hardware/BIOS setup,
per-camera network configuration, production hardening, systemd
supervision, and anything the DS 9.0 docs mark as manual.

---

## 2. Executable form (LOCKED)

- A **Python application** packaged into a **single self-contained binary**
  via **PyInstaller** (`--onefile`). Binary name: `mv3dt-installer`.
- **TUI/CLI-first.** Prompts are plain terminal prompts with sane defaults;
  the only "GUI-like" affordance is guided path selection with a default.
  No X/Wayland dependency — it must run from a TTY before the desktop is up.
- It **supersedes / wraps-and-ports** the logic in
  [`laptop/scripts/`](../../laptop/scripts/). Bash that still has to run
  verbatim (e.g. NVIDIA `update_rtpmanager.sh` invocation, apt transactions)
  is bundled as data assets and shelled out to; new orchestration lives in
  Python.

---

## 3. Single-binary architecture

### 3.1 Module layout

All installer code lives under `installer/` (a sibling of `laptop/`, so the
existing harness is untouched during the port):

```
installer/
├── bootstrap/
│   └── bootstrap.sh                # bare-metal bootstrap (curl|bash + USB)
├── plan/                           # these spec docs (this file is 00-*)
├── installer.spec                  # PyInstaller build recipe
├── pyproject.toml                  # deps + console-script entrypoint
└── mv3dt_installer/                # the Python package
    ├── __main__.py                 # `python -m mv3dt_installer` entrypoint
    ├── app.py                      # top-level TUI orchestrator / dispatch loop
    ├── state.py                    # StateMachine + state.json (§6)
    ├── logs.py                     # shared logger + transcript (§8)
    ├── report.py                   # verify_pinned / report_* helpers (§8.3)
    ├── reboot.py                   # boot-id reboot detection (§7)
    ├── privilege.py                # root + $SUDO_USER resolution (§9)
    ├── ngc.py                      # NGC key capture + secure store (§10)
    ├── config.py                   # install-location config (§11)
    ├── shellout.py                 # bundled-asset locator + subprocess runner
    └── steps/
        ├── __init__.py             # Step protocol, StepResult, STEP_REGISTRY
        ├── step1_prerequisites.py       # DevA
        ├── step2_deepstream_sdk.py       # DevB
        ├── step3_amc_launcher.py         # DevC
        ├── step4_calib_output_wiring.py  # DevD
        └── step5_per_project_exes.py     # DevD
    └── assets/                     # bundled bash/config ported from laptop/
        ├── scripts/                # ported bash fragments (idempotent)
        ├── deepstream/             # config templates (tracker/infer/app)
        └── mosquitto/              # mv3dt.conf drop-in
```

### 3.2 Entrypoint and dispatch

- `__main__.py` → `app.main()`. `app.main()`:
  1. Parses CLI flags (see §3.3).
  2. Resolves privilege + invoking user (`privilege.resolve()`, §9).
  3. Loads/creates the install-location config (`config.load()`, §11).
  4. Opens the transcript logger (`logs.open_transcript()`, §8).
  5. Loads the state file (`state.load()`, §6) and runs
     `reboot.reconcile()` to clear any satisfied reboot-pending marker (§7).
  6. Enters the **dispatch loop** over `STEP_REGISTRY` (steps 1→7 in order).
     Steps 6 and 7 are opt-in and auto-skip when their gate is `off` (§3.4).

- **Dispatch loop** (the core of the state machine, §6):
  For each step in order, if `state.status(step) == COMPLETE`, skip and emit
  the "already complete" log line. Otherwise call the step lifecycle
  (`preflight → run → verify → report`, §12) and record the returned
  `StepResult`. The loop **halts** the moment a step returns anything other
  than `COMPLETE`:
  - `REBOOT_REQUIRED` → write reboot marker (§7), print the USER-ACTION
    block telling the operator to reboot then **run the installer again to
    continue**, and exit 0.
  - `USER_ACTION_REQUIRED` → print the required manual actions (§9 display
    contract) and exit 0 with the "run the installer again to continue"
    line.
  - `FAILED` → print the failure + remediation and exit non-zero.
  On the next launch the loop resumes at the first non-`COMPLETE` step.

### 3.3 CLI flags (framework-level)

Steps may add their own, but the framework owns these:

- `--install-dir PATH` — override the default install location (§11).
- `--resume` — default behavior; explicit for clarity.
- `--status` — print the state table and exit.
- `--reset-state` — wipe `state.json` (mirrors `00_bootstrap.sh --reset-state`).
- `--reset-step N` — clear one step's completion so it re-runs.
- `--non-interactive` — never prompt; use defaults/config; fail if a required
  value is missing (mirrors `00_bootstrap.sh --non-interactive`).
- `--no-pause` — skip "press Enter" confirmations.
- `--log-dir PATH` — override the transcript directory (§8).
- `-h/--help`, `--version`.

### 3.4 Opt-in step gates

Steps 1–5 always run. Steps 6 and 7 are **opt-in**: a workstation used only for
local calibration and ad-hoc pipeline runs needs neither 24/7 supervision nor a
web-app connection. Each is gated by an `installer.conf` key (§11.2) with a
matching CLI flag:

| Gate key | Values (default first) | Owning step |
|---|---|---|
| `MV3DT_REMOTE_SUPERVISION` | `off` \| `local` \| `remote` | [`STEP-6` §E.2](STEP-6-REMOTE-SUPERVISION.md#e2-gating-opt-in) |
| `MV3DT_WEBAPP_INTEGRATION` | `off` \| `on` | [`STEP-7` §H.2](STEP-7-WEBAPP-INTEGRATION.md#h2-gating-opt-in) |

When a gate is `off` the dispatch loop treats that step as auto-`COMPLETE`
(skipped) with a one-line log, using the same skip discipline as a genuinely
completed step (§3.2). Under `--non-interactive` an unset gate stays `off`, so
an unattended run never enables long-running services or outbound network
connections the operator did not ask for.

---

## 4. PyInstaller packaging

### 4.1 What builds the binary

The binary is built **during the bootstrap stage** (§5), on the target
machine, from the cloned public repo. Bootstrap installs Python + build deps
and runs:

```bash
python3 -m pip install --user pyinstaller
pyinstaller installer/installer.spec --distpath installer/dist --workpath /tmp/mv3dt-build
```

`installer.spec` (data-only summary; DevA owns the exact spec):

- `Analysis(['installer/mv3dt_installer/__main__.py'], ...)`.
- `datas` bundles the runtime assets so they ship inside the binary:
  - `installer/mv3dt_installer/assets/**` → `assets/`
  - the DeepStream config templates from
    [`laptop/deepstream/`](../../laptop/deepstream/) (tracker/infer/app/msgconv)
  - [`laptop/mosquitto/mv3dt.conf`](../../laptop/mosquitto/mv3dt.conf)
- `--onefile` → one executable at `installer/dist/mv3dt-installer`.
- No hidden GUI toolkits; only stdlib + a small TUI dep (e.g. `rich` or
  `questionary`) and `pyyaml`. Keep the dep list minimal so the binary is
  small and boots fast on a bare TTY.

### 4.2 Locating bundled assets at runtime

PyInstaller `--onefile` unpacks datas into a temp dir exposed as
`sys._MEIPASS`. `shellout.py` MUST resolve asset paths through a single
helper so both "frozen" and "run-from-source" modes work:

```python
def asset_path(*parts: str) -> pathlib.Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:                       # frozen binary
        return pathlib.Path(base, "assets", *parts)
    return pathlib.Path(__file__).parent / "assets" / *parts   # dev mode
```

- Bundled bash fragments are extracted to a run-scoped temp dir, `chmod +x`,
  and executed via `subprocess` with the environment prepared by
  `privilege.py` (§9). Never execute directly from `sys._MEIPASS` if the
  fragment writes next to itself — copy out first.
- The binary is the unit of delivery; the operator does **not** need the repo
  checkout at runtime. (The bootstrap still leaves the clone in place for
  logs/debugging.)

---

## 5. Bare-metal bootstrap script

Path: [`installer/bootstrap/bootstrap.sh`](../bootstrap/bootstrap.sh).
Delivered two ways with identical behavior:

- **`curl | bash` one-liner** (network install):

  ```bash
  curl -fsSL https://raw.githubusercontent.com/KevinTTO5/P2BP-25_26-Hardware_Test/main/installer/bootstrap/bootstrap.sh | sudo -E bash
  ```

- **USB-file variant** (offline/lab install): copy `bootstrap.sh` from the
  USB stick and run it directly:

  ```bash
  sudo -E bash /media/usb/bootstrap.sh
  ```

  It behaves identically; if it detects it is already running from inside a
  clone (`installer/bootstrap/bootstrap.sh` present alongside a `.git`), it
  skips the clone step and builds from the local tree.

### 5.1 Exact responsibilities (in order)

1. **Preflight**: confirm Ubuntu 24.04 + `x86_64` (mirror the checks in
   [`00_bootstrap.sh` Phase 0](../../laptop/scripts/00_bootstrap.sh)); confirm
   run as root via `sudo` and that `$SUDO_USER` is a real non-root login
   (§9).
2. **Install `git` + build deps** (single apt transaction):
   `git`, `python3`, `python3-venv`, `python3-pip`, `ca-certificates`,
   `curl`, `build-essential`. (These are the minimum to clone and to build
   the PyInstaller binary; the full stack is installed later by Step 1.)
3. **Clone the PUBLIC repo — no GitHub auth.** The repo lives in a public
   GitHub org, so cloning uses an anonymous HTTPS URL with **no token, no SSH
   key, no credential prompt**:

   ```bash
   git clone --depth 1 https://github.com/KevinTTO5/P2BP-25_26-Hardware_Test.git "$CLONE_DIR"
   ```

   `GIT_TERMINAL_PROMPT=0` is exported so a mis-set private repo fails fast
   instead of hanging on a credential prompt. Default `CLONE_DIR` is the
   invoking user's home: `"$SUDO_USER_HOME/P2BP-25_26-Hardware_Test"`.
4. **Capture the NGC API key (bootstrap stage).** Prompt the operator (secret
   input, no echo). Store it ONLY locally per §10 (gitignored env file,
   `chmod 600`). If the operator leaves it blank, record "manual download
   fallback" so Step 2 knows to use the guided manual placement path. The key
   is NEVER written to the transcript log, NEVER committed, NEVER passed on a
   command line that lands in shell history.
5. **Capture the web-app credential (optional, bootstrap stage).** Only when
   `MV3DT_WEBAPP_INTEGRATION` is on (§3.4). Prompt for `API_KEY` (secret input,
   no echo) and `ENDPOINT` (plain), normalize the endpoint, and store both per
   §14. Blank input leaves the gate effectively inert and Step 7 surfaces a
   USER-ACTION block on its first run. Same secrecy rules as the NGC key: never
   logged, never committed, never on a command line.
6. **Build the single binary** with PyInstaller (§4.1), building as the
   invoking user where possible (only the final launch needs root).
7. **Launch the installer**:

   ```bash
   sudo -E installer/dist/mv3dt-installer
   ```

   From here the binary owns the flow (state machine + dispatch, §3.2). On
   subsequent runs the operator re-launches the binary directly; bootstrap is
   only needed once (or after `git pull` + rebuild).

### 5.2 Idempotency

Re-running `bootstrap.sh` is safe: apt installs are no-ops when satisfied
(reported per §8.3), the clone is refreshed with `git pull` if the directory
already exists, and the binary is rebuilt only if missing or `--rebuild` is
passed.

---

## 6. Resumable state machine

### 6.1 State file

- Canonical path: **`/var/lib/mv3dt-installer/state.json`** (root-owned,
  `chmod 0644`; the directory is created `chmod 0755`). This is the direct
  successor to the existing line-based marker
  `/var/lib/mv3dt-laptop-bootstrap.state` used by
  [`00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh) via
  [`lib/common.sh`](../../laptop/scripts/lib/common.sh)
  (`phase_done` / `mark_phase_done`); it upgrades that flat pattern to JSON so
  it can also hold reboot markers, timestamps, and the chosen install path.
- If `--install-dir` moves the install root, the state file stays at the
  canonical `/var/lib/mv3dt-installer/state.json` path (a stable, absolute,
  root-owned location survives home-dir/desktop churn during driver install).

### 6.2 Schema

```json
{
  "schema_version": 1,
  "install_dir": "/opt/mv3dt",
  "created_utc": "2026-07-01T22:00:00Z",
  "updated_utc": "2026-07-01T22:41:12Z",
  "steps": {
    "step1_prerequisites":    { "status": "COMPLETE", "finished_utc": "..." },
    "step2_deepstream_sdk":   { "status": "PENDING" },
    "step3_amc_launcher":     { "status": "PENDING" },
    "step4_calib_output_wiring": { "status": "PENDING" },
    "step5_per_project_exes": { "status": "PENDING" },
    "step6_remote_supervision":  { "status": "PENDING" },
    "step7_webapp_integration":  { "status": "PENDING" }
  },
  "reboot_pending": {
    "requested_by": "step1_prerequisites",
    "boot_id_at_request": "3f6b...c1",
    "requested_utc": "..."
  }
}
```

- `steps[*].status` ∈ the `StepStatus` enum (§12.2).
- `reboot_pending` is `null` unless a step requested a reboot (§7). While it
  is non-null, the dispatch loop refuses to advance past the requesting step.

### 6.3 API (`state.py`)

- `load() -> State` / `save(State)` — atomic write, per the shared helper
  below.
- `status(step_id) -> StepStatus` / `set_status(step_id, status)`.
- `mark_complete(step_id)` — sets `COMPLETE` + `finished_utc`.
- `set_reboot_pending(step_id, boot_id)` / `clear_reboot_pending()`.
- `all_complete() -> bool` — used to print the final success banner.

**REQUIRED — the shared atomic-write helper.** `state.json`, the Step 5
registry ([`STEP-5` §4.1](STEP-5-PER-PROJECT-EXES.md#41-path)), and every
run-state file written by [`STEP-7` §F.2](STEP-7-WEBAPP-INTEGRATION.md#f2-state-file-fan-in)
use one implementation. The `flush` + `fsync` pair before `os.replace` is the
part that matters: without it a power loss mid-install can leave a
zero-length `state.json` and lose every completed step.

```python
def write_json_atomic(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)          # atomic within a filesystem
```

Readers are correspondingly forgiving: a missing file yields the empty default
and a `json.JSONDecodeError` is treated as "absent", never as a fatal error —
so a partially-written file can never wedge the installer.

Re-running the binary after a reboot resumes at the first non-`COMPLETE`
step; completed steps log the standard "already complete" line and are
skipped, exactly like the phase-skip behavior in `00_bootstrap.sh`.

---

## 7. Reboot detection / continuation contract

This is the contract **Step 1 consumes** (driver/CUDA install requires
reboots) and any later step may use.

### 7.1 Signaling "reboot required"

A step signals a reboot by returning `StepResult(status=REBOOT_REQUIRED,
message=..., user_actions=[...])`. The framework then:

1. Reads the current boot id and stores it:
   `state.set_reboot_pending(step_id, boot_id=current_boot_id())`.
2. Prints the USER-ACTION reboot block (§9.4) including the exact line
   **"run the installer again to continue"**.
3. Exits 0 (a pending reboot is a normal, expected pause — not a failure).

### 7.2 Detecting the reboot actually happened

`reboot.current_boot_id()` reads, in priority order:

1. **`/proc/sys/kernel/random/boot_id`** — a fresh random UUID generated by
   the kernel on every boot. This is the primary signal.
2. Fallback: **`btime`** from `/proc/stat` (boot wall-clock epoch), used only
   if `boot_id` is unreadable.

`reboot.reconcile(state)` runs early in `app.main()` (step 5 of §3.2):

- If `state.reboot_pending` is `null` → nothing to do.
- Else compare `state.reboot_pending.boot_id_at_request` to
  `current_boot_id()`:
  - **Different** → the machine really rebooted. Mark the requesting step
    `COMPLETE` (its work finished; the reboot was its tail), call
    `state.clear_reboot_pending()`, log "reboot confirmed", and let dispatch
    continue to the next step.
  - **Same** → the operator re-ran the binary **without** rebooting. Do NOT
    advance. Re-print the reboot USER-ACTION block and exit 0. This blocks the
    next step until the reboot is confirmed.

### 7.3 Guarantee for step authors

A step that returns `REBOOT_REQUIRED` is guaranteed that, on the next launch
that actually follows a real reboot, it is treated as `COMPLETE` and the next
step runs. If a step needs to do post-reboot verification itself (rather than
being auto-completed), it instead returns `USER_ACTION_REQUIRED` for the
reboot and re-runs its own `verify()` on next launch — Step 1 will document
which it uses.

---

## 8. Verbose logging + reporting contract

### 8.1 Shared logger (`logs.py`)

- Colour-aware, level-prefixed lines to **stderr** so stdout stays reserved
  for machine-parsable output — same discipline as
  [`lib/common.sh`](../../laptop/scripts/lib/common.sh) (`log_info`,
  `log_warn`, `log_error`, `die`). Python equivalents: `log.info/warn/error`
  and `die(msg)` (logs error + non-zero exit).
- Every emitted line is **also** appended to the transcript (§8.2).

### 8.2 Transcript log file

- Directory: **`/var/lib/mv3dt-installer/logs/`** (override with `--log-dir`).
- Per-run file: `install-YYYYMMDD-HHMMSS.log` plus a `latest.log` symlink.
- The transcript captures every logger line, every prompt + chosen value
  (secrets redacted — see §10), and the stdout/stderr of every shelled-out
  bash fragment. It is the auditable record of the whole CD-style install.

### 8.3 Reporting format for dependencies (REQUIRED, exact strings)

Every step reports dependency state through two shared helpers so the wording
is identical everywhere. These are the required human-facing strings:

- **Newly installed** dependency — reported with name + version:

  ```
  installed <dependency> version <version>
  ```

- **Pre-existing** dependency — reported as:

  ```
  already installed <dependency> version <version>
  ```

Concrete examples:

```
installed cuda-toolkit-13-1 version 13.1
already installed gstreamer1.0-tools version 1.24.2
already installed deepstream-9.0 version 9.0.0-1
```

Helper signatures (`report.py`):

- `report_installed(dependency: str, version: str) -> None`
  → prints/logs `installed <dependency> version <version>`.
- `report_already_installed(dependency: str, version: str) -> None`
  → prints/logs `already installed <dependency> version <version>`.

### 8.4 "Verify at exact pinned version" helper (REQUIRED)

A single reusable helper does equality-pinned verification, porting
`require_version_eq` from
[`lib/common.sh`](../../laptop/scripts/lib/common.sh):

- `verify_pinned(label: str, actual: str, expected: str) -> bool`
  - On match: logs `Version OK: <label> == <actual>` and returns `True`.
  - On mismatch: logs
    `Version check failed: <label> — expected '<expected>', got '<actual>'`
    and returns `False` (the caller decides whether to `die` or surface a
    `USER_ACTION_REQUIRED`).

Every step MUST use `verify_pinned` in its `verify()` and one of the two §8.3
reporters after every dependency it touches, so the transcript is uniform and
greppable. Steps use **equality pins** (DS 9.0 refuses older/newer minors of
the driver and `libnvinfer*`; see [References](#references)).

---

## 9. USER-ACTION display + privilege/sudo contract

### 9.1 Privilege handling

- The installer must run as **root** (installs packages, writes
  `/etc/profile.d/*`, `/var/lib/*`). `privilege.require_root()` mirrors
  `require_root` in [`lib/common.sh`](../../laptop/scripts/lib/common.sh):
  exit with a clear message if `os.geteuid() != 0`.
- The operator launches it via `sudo -E mv3dt-installer` (the `-E` preserves
  a pre-set `NGC_API_KEY` in the environment if the operator prefers env over
  the prompt; see §10).

### 9.2 Resolving the invoking user / home

Ported directly from the `$SUDO_USER` logic in
[`00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh) (LOCAL_DEB_DIR
resolution) and the per-user NGC handling:

`privilege.resolve() -> InvokingUser` returns:

- `name`: `$SUDO_USER` if set and not `"root"`, else the current user.
- `home`: resolved from the passwd DB, equivalent to
  `getent passwd "$SUDO_USER" | cut -d: -f6` (NOT `$HOME`, which is
  root's under sudo).
- `uid`/`gid`: the invoking user's, for `chown`-ing artifacts back.

Rules for step authors:

- Anything that must run "as the user" (the `ngc` CLI, `docker` without sudo,
  files under the user's home, the AMC clone under `$HOME/auto-magic-calib`)
  MUST be run via `privilege.run_as_user(...)` (equivalent to
  `sudo -u "$SUDO_USER" -H ...`), exactly as Phases 6/10 of
  `00_bootstrap.sh` do for `ngc`.
- Files created for the operator (env files, models, per-project exes) MUST be
  `chown`ed to the invoking user/group.

### 9.3 USER-ACTION display contract

When a step needs the operator to do something by hand (PATH edits, a command
to run, placing an NGC download, a reboot), it returns
`USER_ACTION_REQUIRED` with a `user_actions: list[UserAction]`. The framework
renders a single, unmissable block to stderr + transcript:

```
+---------------------------------------------------------------+
|  ACTION REQUIRED                                              |
+---------------------------------------------------------------+
  Step: <step title>
  Why:  <one-line reason>

  Do the following, in order:
    1. <action text>            (command, if any, shown verbatim)
    2. <action text>

  Then run the installer again to continue.
+---------------------------------------------------------------+
```

- The final line is ALWAYS exactly: **"Then run the installer again to
  continue."** (the phrase "run the installer again to continue" is the
  contract).
- `UserAction` fields: `text: str`, optional `command: str` (rendered
  verbatim, copy-pasteable), optional `path: str` (for "edit this file"
  actions like appending CUDA/DS exports to a profile).

### 9.4 Reboot block

The `REBOOT_REQUIRED` path (§7.1) renders the same frame with the reason
"a reboot is required before the next step can run" and the same closing
"run the installer again to continue" line, matching the reboot banners
already in [`00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh)
(Phases 2 and 3).

---

## 10. NGC API key capture + local secure storage

The key is captured in the **bootstrap stage** (§5.1 step 4) and consumed by
**Step 2** (DS SDK + PeopleNet fetch via the `ngc` CLI) and later steps that
pull gated NGC content.

### 10.1 Storage contract

- Canonical secret file: **`<install_dir>/secrets/ngc.env`** owned by the
  invoking user, permissions **`chmod 600`**, directory `chmod 700`. Single
  line: `NGC_API_KEY=<key>`.
- If `<install_dir>` is inside a git working tree, the secret dir MUST be
  gitignored. Add `installer/**/secrets/` and `secrets/ngc.env` to the
  relevant `.gitignore` (the existing
  [`laptop/.gitignore`](../../laptop/.gitignore) already demonstrates the
  pattern by ignoring `config/laptop.env`). The key is **NEVER** committed.
- The key is **NEVER** written to the transcript log. Any prompt echo is
  suppressed (secret input), and any log line that would contain it prints
  `NGC_API_KEY=<redacted>`.

### 10.2 Capture + handoff API (`ngc.py`)

- `capture_key(non_interactive: bool) -> KeyState` — secret prompt; blank
  input is allowed and recorded as `manual_fallback = True`.
- `store_key(key: str, install_dir) -> Path` — writes `secrets/ngc.env`
  (`chmod 600`), chowned to the invoking user.
- `load_key() -> str | None` — read back for a step; returns `None` when the
  operator chose the manual fallback.
- `configure_ngc_cli()` — for steps that call `ngc`, writes
  `~/.ngc/config` (`chmod 600`) as the invoking user, mirroring Phase 10 of
  [`00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh).

### 10.3 Manual fallback

If `load_key()` is `None`, gated steps MUST surface a `USER_ACTION_REQUIRED`
block (§9.3) with the guided manual download-and-placement instructions
(sign in at NGC, download the `.deb` / model, drop it in the expected dir),
then re-verify on next launch. Step 2 owns the exact DS 9.0 download URLs and
placement paths.

---

## 11. Install-location config

### 11.1 Default and selection

- Default install directory: **`/opt/mv3dt`**.
- On first run (or when `--install-dir` is not given and no config exists),
  the TUI prompts for the path with `/opt/mv3dt` prefilled (GUI-installer-like
  path selection). `--non-interactive` uses the default silently.

### 11.2 Persistence + sharing with later steps

- The chosen path is written into `state.json` as `install_dir` (§6.2) — the
  single source of truth — and mirrored into a small
  `<install_dir>/installer.conf` (KEY=VALUE) for human inspection and for
  bundled bash fragments (`set -a; . installer.conf`), same shape as
  [`laptop/config/laptop.env`](../../laptop/config/laptop.env.example).
- `config.load()` resolves precedence: `--install-dir` > `state.json` >
  `installer.conf` default > `/opt/mv3dt`.
- Layout under the install dir (created by the framework, consumed by steps):

  ```
  <install_dir>/
  ├── installer.conf        # chosen path + shared vars
  ├── secrets/
  │   ├── ngc.env           # NGC key, chmod 600 (§10)
  │   └── webapp.env        # API_KEY + ENDPOINT, chmod 600 (§14)
  ├── bin/                  # per-project exes dropped here (Steps 3 & 5)
  ├── deepstream/           # rendered DS configs + calibration (Step 4)
  ├── projects/             # per-project registry entries (Step 5)
  ├── agent/                # MQTT control agent env/config (Step 6)
  ├── webapp/               # upload queue + run-state files (Step 7)
  └── run/                  # worker state files, fan-in source (Step 7 §F.2)
  ```

- **Steps 3 and 5** drop per-project executables into `<install_dir>/bin/`;
  **Step 4** writes rendered DeepStream config + calibration under
  `<install_dir>/deepstream/`. They MUST read the path via `config.load()`,
  never hardcode it.

---

## 12. Step-module interface (the contract for Steps 1–5)

Steps are independently buildable against this interface. Each
`stepN_*.py` module exposes one class implementing the `Step` protocol and
registers it in `STEP_REGISTRY` (ordered 1→5).

### 12.1 Protocol

```python
class Step(Protocol):
    id: str          # e.g. "step2_deepstream_sdk"  (matches state.json key)
    title: str       # human title for logs/USER-ACTION blocks
    order: int       # 1..5

    def preflight(self, ctx: Context) -> StepResult: ...
    def run(self, ctx: Context) -> StepResult: ...
    def verify(self, ctx: Context) -> StepResult: ...
    def report(self, ctx: Context) -> None: ...
```

- **`preflight(ctx)`** — cheap checks that prerequisites from prior steps /
  the environment are satisfied (tools present, previous step COMPLETE,
  required inputs available). May return `USER_ACTION_REQUIRED` (e.g. missing
  NGC key with no fallback) or `FAILED`; returns `COMPLETE` to mean "ok to
  run".
- **`run(ctx)`** — does the work (apt transactions, NGC downloads, docker
  compose, rendering configs, dropping exes). Uses `report_installed` /
  `report_already_installed` (§8.3) for every dependency touched. May return
  `REBOOT_REQUIRED` / `USER_ACTION_REQUIRED` / `FAILED` / `COMPLETE`.
- **`verify(ctx)`** — idempotent post-checks using `verify_pinned` (§8.4)
  against the DS 9.0 pins. Returns `COMPLETE` only when every pin matches.
- **`report(ctx)`** — prints the step's human-facing summary block (what was
  installed/skipped, versions, where files landed). No side effects.

### 12.2 `StepResult` and status recorded by the state machine

```python
class StepStatus(Enum):
    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    REBOOT_REQUIRED = "REBOOT_REQUIRED"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
    FAILED = "FAILED"

@dataclass
class StepResult:
    status: StepStatus
    message: str = ""
    user_actions: list[UserAction] = field(default_factory=list)
```

- The dispatch loop (§3.2) runs `preflight → run → verify`; the **effective**
  result is the first non-`COMPLETE` result, else `COMPLETE`. On `COMPLETE`
  it calls `report()` and `state.mark_complete(step.id)`. Any other status is
  recorded verbatim and halts the loop with the appropriate USER-ACTION /
  reboot / failure handling.
- Steps never write `state.json` directly — they only return a `StepResult`;
  the framework owns all persistence.

### 12.3 `Context` object passed to every step

`Context` bundles the shared services so steps stay decoupled from globals:

- `install_dir: Path`, `conf: dict` (from `config.load()`, §11).
- `user: InvokingUser` (§9.2) and `run_as_user(...)`, `run_root(...)`.
- `log` (§8.1), `report_installed`, `report_already_installed`,
  `verify_pinned` (§8.3–8.4).
- `ngc` handle (`load_key`, `configure_ngc_cli`, `manual_fallback`, §10).
- `webapp` handle (`load_credentials`, `enabled`, §14) — the web-app API key
  and normalized endpoint, for steps that talk to the backend.
- `asset_path(...)` (§4.2) to locate bundled bash/config.
- `reboot.request()` helper (which just returns `REBOOT_REQUIRED`; the
  framework does the boot-id bookkeeping, §7).

### 12.4 What each step owns (cross-doc map)

Each step doc defines its own internals but consumes only the contracts above:

- **Step 1 — Prerequisites (DevA):** driver `590.48.01` / CUDA `13.1` /
  cuDNN `9.18.0` / TensorRT `10.14.1.48-1+cuda13.0` / GStreamer `1.24.2` +
  apt prereqs; Blackwell driver-support check for the RTX PRO 4500;
  verification via `verify_pinned`; uses the **reboot gate** (§7).
- **Step 2 — DeepStream SDK (DevB):** DS 9.0 install via deb / tar / docker
  (auto-detect or prompt), NGC download using the key from §10 (manual
  fallback if none), install-path, post-install (`update_rtpmanager.sh`,
  `ldconfig`, `/etc/profile.d/deepstream.sh`), smoke test.
- **Step 3 — AMC launcher (DevC):** docker compose AMC bring-up, open the
  localhost UI, keep the service up until the browser closes; standalone AMC
  exe dropped in `<install_dir>/bin/` (§11).
- **Step 4 — Calib output wiring (DevD):** place the AMC output file (prompt +
  default), render `deepstream_app_config`, patch
  `config_tracker_NvMOT.yml` `calibrationDirectory` / `LOCATION_ID`; writes
  under `<install_dir>/deepstream/`.
- **Step 5 — Per-project exes (DevD):** start-or-close, project-named
  DeepStream exe + reusable AMC exe, project registry / re-run; writes to
  `<install_dir>/bin/` and `<install_dir>/projects/`.
- **Step 6 — Remote supervision (DevD, opt-in §3.4):** per-project pipelines
  become boot-enabled `mv3dt-pipeline@<slug>.service` instances plus an MQTT
  control agent — the **control plane** (run / stop / restart). See
  [`STEP-6`](STEP-6-REMOTE-SUPERVISION.md).
- **Step 7 — Web-app integration (DevD, opt-in §3.4):** the HTTP **data
  plane** — registration/status, signed-URL artifact upload, and
  web-app-initiated one-shot operations, against the credential contract in
  §14. See [`STEP-7`](STEP-7-WEBAPP-INTEGRATION.md).

---

## 13. Out of scope / defer to human

- Everything the DS 9.0 Installation page treats as manual and everything in
  Notion §1–3 / [`DEEPSTREAM-SETUP.md`](../../laptop/docs/DEEPSTREAM-SETUP.md)
  §1–3: hardware selection, BIOS (Secure Boot, virtualization, discrete-GPU
  primary), and the Ubuntu 24.04 dual-boot install itself. The installer
  assumes it is already booted into Ubuntu 24.04.
- Per-camera IP / stream-profile configuration via each camera's web UI
  (Notion §7.1–7.4). The pipeline only consumes the resulting RTSP URLs.
- Secure Boot MOK enrollment and any BIOS interaction — surfaced as
  `USER_ACTION_REQUIRED` by Step 1, performed by the operator.
- `ufw`, Tailscale/NoMachine, and dashboards — see the "Out of scope" /
  "Future work" sections of
  [`SCRIPTED-WORKFLOW.md`](../../laptop/docs/SCRIPTED-WORKFLOW.md).
  (systemd supervision and Mosquitto ACL/password hardening are **no longer**
  out of scope — [`STEP-6`](STEP-6-REMOTE-SUPERVISION.md) owns both.)
- **The web app itself** — its UI, its API implementation, cloud auth
  infrastructure, and API-key issuance/rotation. The framework captures and
  stores the credential (§14) but never provisions it; the desktop-side half
  of the integration is [`STEP-7`](STEP-7-WEBAPP-INTEGRATION.md), and its own
  exclusions are listed in
  [`STEP-7` §I](STEP-7-WEBAPP-INTEGRATION.md#i-out-of-scope--flag-for-human).
- Alternate detectors (`yolo11n`): explicitly deferred; PeopleNet is the only
  detector installed, matching NVIDIA's DS 9.0 MV3DT reference.
- The NGC account itself and any credential the operator must obtain from
  NVIDIA — the installer captures and stores the key locally (§10) but never
  provisions it.

---

## 14. Web-app connection contract

The desktop talks to a cloud web app over **HTTP with a bearer API key**. This
section owns the credential and endpoint contract — the *transport* and the
*routes* belong to [`STEP-7`](STEP-7-WEBAPP-INTEGRATION.md), and
[`STEP-6`](STEP-6-REMOTE-SUPERVISION.md) owns the separate MQTT control plane.
It is deliberately a sibling of §10 (NGC key): same storage discipline, same
secrecy rules, different consumer.

> **Provenance:** this contract is ported from the P2BP Jetson camera-node
> agent, which has been running against the same backend. The capture flow
> mirrors that tree's `install.sh` (credential prompt → normalize → atomic
> write → `chmod 600`) and the endpoint rule mirrors its `_normalize_endpoint`.
> Those source files are being removed from this fork — see
> [`DELETION-REVIEW` §3](DELETION-REVIEW.md#3-deletions-gated-on-the-harvest-the-jetson-tree)
> — so the behavior is specified here in full rather than by reference.

### 14.1 Storage contract

- Canonical secret file: **`<install_dir>/secrets/webapp.env`**, owned by the
  invoking user (§9.2), permissions **`chmod 600`**, directory `chmod 700` —
  identical to §10.1. Exactly two lines:

  ```
  API_KEY=<key>
  ENDPOINT=<normalized-base-url>
  ```

- The file is `KEY=VALUE` with no quoting or expansion, so it is directly
  consumable both by `EnvironmentFile=` in a systemd unit and by
  `set -a; . webapp.env` in a bundled bash fragment (§11.2).
- If `<install_dir>` is inside a git working tree, the secrets directory MUST
  be gitignored, exactly as §10.1 requires for `ngc.env`. The key is **NEVER**
  committed.
- Only the API key is secret. `ENDPOINT` is not, but it lives in the same file
  so there is one place to look and one file to `chmod`.

### 14.2 Endpoint normalization (REQUIRED)

Operators paste base URLs inconsistently — with a trailing slash, or with the
`/api` suffix already attached, because that is what the browser address bar
shows. Every consumer joins routes as `<endpoint> + /api/...`, so a
non-normalized value silently produces `https://host/api/api/...`.

Normalization is applied **once at capture** and **again on every load** (cheap,
and it protects against a hand-edited file):

1. Strip surrounding whitespace.
2. Strip **all** trailing `/` characters.
3. If the result ends in `/api` (case-insensitive), remove that suffix, then
   strip trailing `/` again.
4. An empty result is an error, not an empty endpoint.

| Operator input | Normalized `ENDPOINT` |
|---|---|
| `https://host` | `https://host` |
| `https://host/` | `https://host` |
| `https://host/api` | `https://host` |
| `https://host/api/` | `https://host` |
| `https://host/API//` | `https://host` |

Route joining is correspondingly strict: `join(endpoint, path)` right-strips
`/` from the base, left-pads a single `/` onto the path, and concatenates —
never `urljoin`, whose relative-reference semantics would discard a base path
component.

### 14.3 Capture + handoff API (`webapp.py`)

Mirrors the §10.2 shape so the two credential handles behave identically:

- `capture_credentials(non_interactive: bool) -> Credentials` — the prompt
  flow. **When a value already exists, ask before replacing it** rather than
  forcing re-entry: print that a credential was found and offer to keep it,
  defaulting to keep. The key prompt suppresses echo; the endpoint prompt does
  not. Under `--non-interactive`, existing values are kept silently and missing
  ones are left unset.
- `store_credentials(creds, install_dir) -> Path` — writes `secrets/webapp.env`
  via the §6.3 atomic-write discipline, then `chmod 600` and `chown` to the
  invoking user. Write-then-permission ordering matters: create the file with a
  restrictive mode from the start (`os.open` with `0o600`) so the key is never
  briefly world-readable.
- `load_credentials() -> Credentials | None` — reads back, re-normalizes the
  endpoint (§14.2), and returns `None` when either value is missing. Steps
  treat `None` as "not configured" and surface a USER-ACTION block (§9.3), not
  a crash.
- `enabled() -> bool` — the §3.4 gate combined with a successful
  `load_credentials()`. This is what a step's `preflight` checks.

### 14.4 Redaction (REQUIRED)

The transcript log (§8.2) captures every prompt and every shelled-out command's
output, so redaction is a contract, not a nicety. Two distinct rules:

- **The API key** — any log line that would contain it prints
  `API_KEY=<redacted>`. The capture prompt echoes nothing.
- **Signed URLs** — the upload flow
  ([`STEP-7` §B](STEP-7-WEBAPP-INTEGRATION.md#b-signed-url-upload)) receives
  pre-signed URLs whose **query string is itself the credential**. Before any
  such URL reaches a log, transcript, or error message, everything from the
  first `?` onward is replaced:

  ```python
  def redact_url(url: str) -> str:
      if not url:
          return ""
      q = url.find("?")
      return url if q < 0 else url[:q] + "?<redacted>"
  ```

  This applies to failure paths too — an exception message containing a raw
  signed URL is the most likely way one leaks, so the redaction happens in the
  logging helper, not at each call site.

---

## References

DeepStream 9.0 official documentation (facts cross-checked via Context7
library `/websites/nvidia_metropolis_deepstream_dev-guide`). Reference DS 9.0
only — NVIDIA's current release.

- DS 9.0 Installation (dGPU Ubuntu prerequisites + three install methods:
  Debian package, tar package, Docker; `update_rtpmanager.sh`;
  `sudo apt-get install ./deepstream-9.0_9.0.0-1_amd64.deb`):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Installation.html>
- DS 9.0 Quickstart (sample-app smoke test; Triton Docker image
  `nvcr.io/nvidia/deepstream:9.0-triton-multiarch`):
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Quickstart.html>
- DS 9.0 `deepstream-app` reference:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_ref_app_deepstream.html>
- DS 9.0 Release Notes:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_Release_notes.html>
- DS 9.0 MV3DT:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_MV3DT.html>
- DS 9.0 AutoMagicCalib:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_AutoMagicCalib.html>
- DS 9.0 Docker containers:
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_docker_containers.html>

Verified DS 9.0 prerequisite pins (DS 9.0 Installation page, dGPU Ubuntu →
Prerequisites; confirmed via Context7): Ubuntu 24.04, GStreamer 1.24.2, NVIDIA
driver 590.48.01, CUDA 13.1, TensorRT 10.14.1.48 (cuDNN 9.18.0 per the DS 9.0
dGPU compatibility table).

Repo files referenced:

- [`laptop/scripts/00_bootstrap.sh`](../../laptop/scripts/00_bootstrap.sh) —
  phased resumable installer this binary ports.
- [`laptop/scripts/lib/common.sh`](../../laptop/scripts/lib/common.sh) —
  logging, `require_root`/`require_tool`, `require_version_eq`, and the
  `phase_done`/`mark_phase_done` state pattern.
- [`laptop/docs/DEEPSTREAM-SETUP.md`](../../laptop/docs/DEEPSTREAM-SETUP.md) /
  [`laptop/docs/SCRIPTED-WORKFLOW.md`](../../laptop/docs/SCRIPTED-WORKFLOW.md)
  — source workflow the installer unifies.
- [`laptop/.gitignore`](../../laptop/.gitignore) — precedent for gitignoring
  local secrets/env (`config/laptop.env`).
- [`STEP-6-REMOTE-SUPERVISION.md`](STEP-6-REMOTE-SUPERVISION.md) — the opt-in
  control plane (§3.4 gate, §12.4 map).
- [`STEP-7-WEBAPP-INTEGRATION.md`](STEP-7-WEBAPP-INTEGRATION.md) — the opt-in
  data plane; sole consumer of the §14 credential contract.
- [`DELETION-REVIEW.md`](DELETION-REVIEW.md) — records which Jetson-tree files
  the §14 contract was harvested from, and why they are being removed.

> **Attribution — sources no longer in this fork.** The §14 credential flow,
> the §6.3 atomic-write helper, and the §14.4 redaction rule were ported from
> the P2BP Jetson camera-node agent (`scripts/cloud_storage_media.py`,
> `scripts/heartbeat.py`, `scripts/config_io.py`, and `install.sh` at the repo
> root). Those files are removed from this fork per
> [`DELETION-REVIEW` §3](DELETION-REVIEW.md#3-deletions-gated-on-the-harvest-the-jetson-tree)
> and remain available in the parent repository. They are named here as
> provenance only — deliberately not linked, so the links cannot rot.
