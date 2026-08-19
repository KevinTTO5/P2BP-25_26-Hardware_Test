# mv3dt-installer — operator download-and-run guide

Status: operator guide for the released `mv3dt-installer` binary. This is the
entry point for anyone setting up a workstation; it does **not** restate the
framework contracts — those live in
[`00`](plan/00-FRAMEWORK-AND-BOOTSTRAP.md) and the per-step specs under
[`plan/`](plan/), and are linked from here section by section.

`mv3dt-installer` is a **single self-contained binary** that takes a bare
Ubuntu 24.04 workstation to a working DeepStream 9.1 / AutoMagicCalib MV3DT
setup. It is built once in CI and published as a GitHub Release asset, so the
target machine downloads one file and runs it — it never builds anything and
never needs a checkout of this repository.

This guide covers the operator path: where to get the binary, how to verify
and run it, every flag it accepts, what the first run asks for, and where it
keeps state and logs. Developers building or testing the installer from a
clone want [§8](#8-developer-workflow).

---

## 1. What this binary is

`mv3dt-installer` is the unified installer described by
[`00` §2](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#2-executable-form-locked): one
PyInstaller `--onefile` executable containing the Python framework, the
bundled bash fragments, and the DeepStream/Mosquitto config templates it
needs. It supersedes the numbered `laptop/scripts/` harness for operator use.

It is a **resumable state machine**
([`00` §6](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#6-resumable-state-machine)):
every run picks up where the last one stopped, re-runs nothing that already
completed, and survives the reboots the driver/CUDA install requires. Running
it a second time is always safe.

> **LOCKED — you do not clone this repo on the workstation.** The Release
> binary is the unit of delivery. There is no `git clone`, no `pip install`,
> and no local build on the target machine; the binary carries its own assets
> and unpacks them at runtime
> ([`00` §4.2](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#42-locating-bundled-assets-at-runtime)).
> A checkout on a workstation is a sign something went wrong, not a
> prerequisite.

---

## 2. Requirements

| Requirement | Value | Why |
|---|---|---|
| OS | Ubuntu 24.04 LTS | The published binary is built against this release's glibc ([`00` §4.1](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#41-what-builds-the-binary)) |
| Architecture | `x86_64` | The only architecture the release build targets |
| Privilege | `sudo` from a real login user, not a bare root shell | Package installs and writes to `/etc/profile.d` and `/var/lib` ([`00` §9.1](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#91-privilege-handling)) |
| GPU | NVIDIA, Ampere or newer | DeepStream 9.1 pins ([`laptop/docs/DEEPSTREAM-SETUP.md`](../laptop/docs/DEEPSTREAM-SETUP.md)) |
| Network | Outbound HTTPS | Package repositories, the DS SDK release assets, and the NGC model fetch |

Run the installer under `sudo` from your normal user account. It resolves the
invoking user from `SUDO_USER` and writes every secret, and every file under
that user's home, as that user
([`00` §9.2](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#92-resolving-the-invoking-user--home)).
Launching from a bare root shell where `SUDO_USER` is unset would put those
files in `/root` instead.

---

## 3. Download, verify, run

Releases are published at:

```
https://github.com/KevinTTO5/P2BP-25_26-Hardware_Test/releases
```

Each release carries exactly two assets — the binary `mv3dt-installer` and
its checksum `mv3dt-installer.sha256`. Download both into the same directory,
verify, then run:

```bash
cd ~/Downloads
curl -fLO https://github.com/KevinTTO5/P2BP-25_26-Hardware_Test/releases/latest/download/mv3dt-installer
curl -fLO https://github.com/KevinTTO5/P2BP-25_26-Hardware_Test/releases/latest/download/mv3dt-installer.sha256
sha256sum -c mv3dt-installer.sha256
chmod +x mv3dt-installer
sudo ./mv3dt-installer
```

`sha256sum -c` must print exactly:

```
mv3dt-installer: OK
```

Anything else — `FAILED`, or `No such file or directory` — means the download
is incomplete or the two files came from different releases. Delete both and
download them again; do not run a binary that failed verification.

To pin a specific release instead of `latest`, replace `latest/download` with
`download/<tag>`, for example `download/v0.2.0`. The tag and the binary's own
`--version` output always agree: the release build fails outright if they
drift ([`00` §4.1](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#41-what-builds-the-binary)).

---

## 4. Command-line flags

Framework-level flags
([`00` §3.3](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#33-cli-flags-framework-level)).
The `Status` column separates what the binary accepts today from what is
specified and still landing — check `./mv3dt-installer --help` on the release
you actually downloaded.

| Flag | Argument | What it does | Status |
|---|---|---|---|
| `--install-dir` | `PATH` | Override the default install location (`/opt/mv3dt`); highest-precedence tier of [`00` §11.2](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#112-persistence--sharing-with-later-steps) | shipped |
| `--status` | — | Print the state table and exit; read-only, and the one flag that does not need `sudo` | shipped |
| `--resume` | — | Default behavior, stated explicitly for clarity; effectively a no-op | shipped |
| `--reset-state` | — | Wipe `state.json` and start fresh; the recorded install directory is preserved across the reset | shipped |
| `--reset-step` | `N` | Clear one step's completion by its order number so it re-runs, then exit | shipped |
| `--non-interactive` | — | Never prompt; use defaults and already-persisted config, and fail if a required value is missing | shipped |
| `--no-pause` | — | Skip the "press Enter" confirmations | shipped |
| `--log-dir` | `PATH` | Override the transcript directory ([§6.2](#62-logs)) | shipped |
| `--version` | — | Print the version and exit; release builds also carry the tag, commit, and build timestamp | shipped |
| `--remote-supervision` | `off`, `local`, `remote` | Set the Step 6 gate ([`00` §3.4](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#34-opt-in-step-gates)) without being prompted; overrides an already-persisted value and logs the change | planned |
| `--webapp-integration` | `off`, `on` | Set the Step 7 gate the same way | planned |
| `--scan-cameras` | — | Discover the camera fleet, probe RTSP, run the one-time position binding, write the inventory, print the table, and exit; needs `sudo` for raw sockets | planned |

Not passing a gate flag is different from passing `off`: an unpassed flag
leaves the value to the next tier of precedence — environment variable, then
prompt, then `off` — while `--webapp-integration off` sets it explicitly.

`--status` is deliberately allowed without `sudo`. It runs before the root
check, performs no writes, and reads a world-readable `state.json`, so an
operator or an unattended health check can inspect progress at any time:

```bash
./mv3dt-installer --status
```

---

## 5. First run: what it asks for

The first run captures a handful of answers, writes them down, and never asks
again. Later runs are silent on all of them — a prompt reappearing on a
second launch is a bug, not the design.

1. **Install directory.** Prompted once with `/opt/mv3dt` prefilled
   ([`00` §11.1](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#111-default-and-selection)).
   Blank accepts the default. Everything the installer owns lives under this
   directory, and the choice is recorded so later runs need no flag.
2. **Remote supervision gate** (`off`, `local`, `remote`), prefilled `off`.
   `off` skips Step 6 entirely — systemd supervision and the MQTT control
   plane ([`STEP-6`](plan/STEP-6-REMOTE-SUPERVISION.md)). Take the default
   unless you have been told otherwise.
3. **Web-app integration gate** (`off`, `on`), prefilled `off`. `off` skips
   Step 7, the HTTP data plane
   ([`STEP-7`](plan/STEP-7-WEBAPP-INTEGRATION.md)).
4. **NGC API key.** A secret prompt — nothing is echoed as you type, and the
   key is never written to the transcript
   ([`00` §10.1](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#101-storage-contract)).
   **Blank is a valid answer**: it selects the guided manual fallback, so any
   step needing NGC-gated content prints a USER-ACTION block with the
   sign-in-and-download instructions and re-verifies on the next launch
   ([`00` §10.3](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#103-manual-fallback)).
   Blank is recorded as an answer, so you are not asked again.
5. **Web-app credential** — endpoint plus API key, and **only** if you set
   the web-app gate to `on` in prompt 3. A blank endpoint writes nothing and
   warns; Step 7 then surfaces its own USER-ACTION block on first run
   ([`00` §14.3](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#143-capture--handoff-api-webapppy)).

Under `--non-interactive` nothing is prompted: both gates resolve to `off`,
and a missing required value fails the run rather than blocking on a human.

> **Still manual, by design:** camera activation, disabling the on-screen
> display, and setting each camera's stream profile happen once in the vendor
> tool before the installer runs. See
> [`00` §13](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#13-out-of-scope--defer-to-human)
> and the header of
> [`laptop/config/cameras.yml`](../laptop/config/cameras.yml), which is the
> record of the fleet's hardware facts.

---

## 6. State, logs, and secrets

### 6.1 State

| Path | Contents |
|---|---|
| `/var/lib/mv3dt-installer/state.json` | Per-step completion, the recorded install directory, and any pending-reboot marker |

The path is fixed and independent of `--install-dir`
([`00` §6.1](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#61-state-file)). The file is
world-readable, which is what lets `--status` work without `sudo`. A missing
or corrupt file is read as "nothing has run yet" rather than as an error, so
deleting it is equivalent to `--reset-state`.

### 6.2 Logs

| Path | Contents |
|---|---|
| `/var/lib/mv3dt-installer/logs/` | One timestamped transcript per run |
| `/var/lib/mv3dt-installer/logs/latest.log` | Symlink to the most recent transcript |

`--log-dir PATH` moves both
([`00` §8.2](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#82-transcript-log-file)). The
transcript captures the installer's own output and the output of every bash
fragment it shells out to. Secrets are redacted before anything is written —
a log line that would contain the NGC key prints `NGC_API_KEY=<redacted>`.
When reporting a problem, attach `latest.log`:

```bash
sudo tail -n 200 /var/lib/mv3dt-installer/logs/latest.log
```

### 6.3 Secrets

Credentials live under the install directory, owned by the invoking user,
directory `0700` and files `0600`
([`00` §10.1](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#101-storage-contract),
[`00` §14.1](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#141-storage-contract)):

| Path | Contents |
|---|---|
| `<install_dir>/secrets/ngc.env` | `NGC_API_KEY=...`, or the manual-fallback marker |
| `<install_dir>/secrets/webapp.env` | `API_KEY=...` and `ENDPOINT=...` |
| `<install_dir>/installer.conf` | Non-secret `KEY=VALUE` config: the two gates, the resolved install directory, and the shared values later steps read |

---

## 7. Resuming, re-running, and resetting

- **After a reboot.** When a step needs a reboot the installer says so and
  exits cleanly. Reboot, then run `sudo ./mv3dt-installer` again — it detects
  that the reboot happened and continues
  ([`00` §7](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#7-reboot-detection--continuation-contract)).
- **After a manual action.** A USER-ACTION block lists exactly what to do by
  hand; do it, then run the installer again. Nothing already complete is
  repeated.
- **Force one step to re-run.** `sudo ./mv3dt-installer --reset-step N` with
  that step's order number, then run normally.
- **Start over.** `sudo ./mv3dt-installer --reset-state` clears all step
  completion. The recorded install directory is kept, so a reset never
  silently relocates a live install. Captured credentials and gates are not
  cleared either — remove `<install_dir>/secrets/` and
  `<install_dir>/installer.conf` by hand if that is genuinely what you want.
- **Upgrade the installer.** Download the newer release, verify it as in
  [§3](#3-download-verify-run), and run it; state carries forward.

---

## 8. Developer workflow

Everything in this section assumes a clone of this repository on a
**development** machine. None of it is part of the operator path
([§1](#1-what-this-binary-is)).

### 8.1 Run from source

```bash
cd installer
python3 -m mv3dt_installer --status
```

`--status` is the safe probe: it exits before the root check and writes
nothing. There is deliberately no `mv3dt-installer` console script, so a pip
install can never shadow the real binary on an operator's `PATH`.

### 8.2 Tests

```bash
cd installer
python3 -m pytest tests/ -v
```

The suite injects every external dependency — no test touches `/var/lib`,
`/opt/mv3dt`, apt, systemd, or a socket, and root is always faked.

### 8.3 Local build

From the repository root:

```bash
python3 -m pip install --user pyinstaller
pyinstaller installer/installer.spec --distpath installer/dist --workpath /tmp/mv3dt-build
installer/dist/mv3dt-installer --version
installer/dist/mv3dt-installer --status
```

This produces the same `--onefile` executable the release job publishes, but
it is **not** a substitute for it: a locally built binary links against the
build host's glibc, so only a build on Ubuntu 24.04 is guaranteed to start on
the target
([`00` §4.1](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#41-what-builds-the-binary)).
Use a local build to confirm a newly added asset is actually bundled, and the
release job for anything an operator will run.

---

## 9. Out of scope for this guide

- **What each step installs.** The per-step specs under [`plan/`](plan/) own
  that: [`STEP-1`](plan/STEP-1-PREREQUISITES.md) through
  [`STEP-7`](plan/STEP-7-WEBAPP-INTEGRATION.md).
- **The DeepStream 9.1 version pins** and the manual NVIDIA prerequisites —
  [`laptop/docs/DEEPSTREAM-SETUP.md`](../laptop/docs/DEEPSTREAM-SETUP.md) is
  the source of truth.
- **The scripted `laptop/` harness**, which remains a developer tool and is
  not the operator path; see
  [`laptop/docs/SCRIPTED-WORKFLOW.md`](../laptop/docs/SCRIPTED-WORKFLOW.md).
- **Camera activation, OSD, and stream-profile setup**, which stay manual
  ([`00` §13](plan/00-FRAMEWORK-AND-BOOTSTRAP.md#13-out-of-scope--defer-to-human)).

---

## References

Release artifacts and their verification come from this repository's own
release job. The framework behavior summarized above is specified in
[`00`](plan/00-FRAMEWORK-AND-BOOTSTRAP.md), which is authoritative wherever
this guide condenses it.

- <https://github.com/KevinTTO5/P2BP-25_26-Hardware_Test/releases> — the
  Releases page; **the only supported source** of the `mv3dt-installer`
  binary and its `.sha256` checksum.
- <https://pyinstaller.org/en/stable/usage.html#options> — backs the
  `--onefile` packaging model and the runtime asset unpacking the bundled
  scripts and config templates depend on.

Repo files referenced:

- [`installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md`](plan/00-FRAMEWORK-AND-BOOTSTRAP.md)
  — the framework contracts this guide summarizes: distribution, CLI flags,
  gates, state, logging, privilege, and credentials.
- [`installer/plan/STEP-1-PREREQUISITES.md`](plan/STEP-1-PREREQUISITES.md)
  through
  [`installer/plan/STEP-7-WEBAPP-INTEGRATION.md`](plan/STEP-7-WEBAPP-INTEGRATION.md)
  — what the gated and ungated steps actually do.
- [`installer/installer.spec`](installer.spec) — the PyInstaller recipe used
  by both the release job and the local build in §8.3.
- [`laptop/config/cameras.yml`](../laptop/config/cameras.yml) — the camera
  inventory and the record of the fleet's hardware facts and required manual
  pre-flight.
- [`laptop/docs/DEEPSTREAM-SETUP.md`](../laptop/docs/DEEPSTREAM-SETUP.md) —
  DS 9.1 package pins and the manual NVIDIA prerequisites.
- [`laptop/docs/SCRIPTED-WORKFLOW.md`](../laptop/docs/SCRIPTED-WORKFLOW.md) —
  the developer-side scripted harness the binary supersedes for operators.
