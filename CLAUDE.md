# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this repo is

P2BP Senior Design (Fall '25 – Spring '26) hardware stack for a multi-camera
3D tracking system (MV3DT). It is now a **single subsystem**: the DeepStream
9.0 workstation.

1. **DeepStream 9.0 harness** (`laptop/`) — the working scripted workflow for
   multi-view calibration (AutoMagicCalib) and MV3DT inference. Start at
   [`laptop/README.md`](laptop/README.md) and
   [`laptop/docs/SCRIPTED-WORKFLOW.md`](laptop/docs/SCRIPTED-WORKFLOW.md).
2. **`installer/plan/`** — specs for the unified `mv3dt-installer` binary that
   supersedes the numbered `laptop/scripts/`. Steps 1–5 cover install and
   per-project pipelines; `STEP-6` adds systemd supervision + MQTT remote
   control; `STEP-7` adds the HTTP web-app data plane.

> **The Jetson camera-node stack has been removed** (`scripts/`, `services/`,
> `models/`, `install.sh`, `requirements.txt`, `config/`, `homographies/`,
> `virtual-cameras/`). Everything worth keeping was harvested into the plan
> docs first. **Read
> [`installer/plan/DELETION-REVIEW.md`](installer/plan/DELETION-REVIEW.md)
> before deleting or resurrecting anything** — it records what went, why, and
> where each pattern landed. The originals remain in the parent repository.

## Working in this repo

- **The plan docs are the authoritative spec for the web-app connection.**
  [`STEP-7`](installer/plan/STEP-7-WEBAPP-INTEGRATION.md) (signed-URL upload,
  registration/status, web-app-initiated operations) and
  [`00` §14](installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md) (credentials,
  endpoint normalization, redaction) were ported from the deleted Jetson tree
  and are written to be implementable without it. Don't reinvent that contract.
- `laptop/scripts/` are numbered (`00_` … `99_`) and must run in order; each is
  idempotent. See the script table in
  [`laptop/docs/SCRIPTED-WORKFLOW.md`](laptop/docs/SCRIPTED-WORKFLOW.md).
- Three `laptop/scripts/` entries are **not** superseded by the installer and
  must not be deleted as part of the port —
  `40_export_watcher.sh` (watch mode has no installer equivalent),
  `10_setup_mosquitto.sh` (no step owns Mosquitto install), and
  `20_verify_cameras.sh` (only its ping sweep is ported, not the `ffprobe`
  check). See `DELETION-REVIEW.md` §6.
- PeopleNet is the only detector wired into the DeepStream pipeline; `yolo11n`
  is reserved for future work — don't add YOLO wiring without being asked.
- [`laptop/config/cameras.yml`](laptop/config/cameras.yml) is the camera
  inventory **and** the record of the fleet's hardware facts (MACs, native
  3072x1728 sensor resolution, vendor/firmware, required manual pre-flight).
  Its header is load-bearing documentation, not commentary. Note its caveat:
  the pinned `169.254.*` link-local IPs are volatile.
- `laptop/config/` is explicitly allowlisted in `.gitignore`; `laptop.env`
  (operator secrets) is not committed — only `laptop.env.example` is.
- Line endings are enforced LF via `.gitattributes`; images/weights/binaries
  are marked binary there — don't hand-edit or re-encode those. `*.exe` is
  **not** currently marked binary; add that rule before committing any.

## Documentation

Whenever creating or substantially editing a Markdown doc in this repo, use
the `markdown-docs` skill to match the house style established by
[`installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md`](installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md)
and [`laptop/docs/SCRIPTED-WORKFLOW.md`](laptop/docs/SCRIPTED-WORKFLOW.md).

## Reference docs

- [`installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md`](installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md)
  — shared contracts for the unified installer (state machine, reporting,
  privilege, credentials).
- [`installer/plan/STEP-1…STEP-7`](installer/plan/) — per-step specs; STEP-6
  is the MQTT control plane, STEP-7 the HTTP data plane.
- [`installer/plan/DELETION-REVIEW.md`](installer/plan/DELETION-REVIEW.md) —
  what was removed from this fork and why.
- [`laptop/docs/SCRIPTED-WORKFLOW.md`](laptop/docs/SCRIPTED-WORKFLOW.md) —
  operator guide + script run order + flow diagram.
- [`laptop/docs/DEEPSTREAM-SETUP.md`](laptop/docs/DEEPSTREAM-SETUP.md) — DS 9.0
  package/OS setup and AMC workflow; source of truth for version pins.
- [`laptop/docs/SCRIPTS-AND-CONFIG-REFERENCE.md`](laptop/docs/SCRIPTS-AND-CONFIG-REFERENCE.md)
  — per-script/config field reference.
