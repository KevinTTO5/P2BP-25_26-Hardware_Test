"""Step 3 -- AutoMagicCalib (AMC) launcher.

Implements `installer/plan/STEP-3-AMC-LAUNCHER.md` against the framework
contract in `installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md` (step-module
interface section 12.1, `StepResult`/`StepStatus` section 12.2, `Context`
section 12.3, logging/reporting section 8, privilege/USER-ACTION section 9,
NGC key handoff section 10, install-location section 11) and the framework's
subcommand dispatch extension it flags in its own section 6.2, built in
`app.py` as `SUBCOMMAND_REGISTRY`/`register_subcommand`.

Scope: bring up NVIDIA's AutoMagicCalib (AMC) stack via `docker compose`
(cloned as a sparse checkout of the `NVIDIA/DeepStream` monorepo, per
section 4 step 3's open decision -- the standalone
`NVIDIA-AI-IOT/auto-magic-calib` repo predates the monorepo move this port
targets), open the localhost UI, and hold the service up until the operator
closes the dedicated AMC browser window (section 5) or signals done. `run()`
always drops the durable `<install_dir>/bin/amc` wrapper + writes config
(section 2's "deliverable vs. action" split) and only *offers* an immediate
launch; declining is not a failure.

`launch_amc(...)` is the one routine `run()`'s optional immediate launch,
the registered `amc` subcommand handler, and (in a later, out-of-scope unit)
Step 5's per-project re-run entry point all call, so behavior is identical
everywhere (section 2's closing paragraph).

Two judgment calls this module makes, documented here since the spec leaves
them open:

- **Root for the standalone `amc` subcommand.** Section 3.1 describes a
  docker-group first-run caveat for an operator running `<install_dir>/bin
  /amc` without root, with a suggested `--sudo` re-exec or a caveat message.
  This module's framework-level bootstrap (`app._bootstrap_subcommand_context`,
  a sibling unit's decision, not this module's) already calls
  `privilege.require_root()` for *every* subcommand, matching doc 00
  section 9.1's blanket "the installer must run as root" -- so by the time
  `handle_amc_subcommand` runs, the process is always root and the
  docker-group caveat's premise (a non-root invocation reaching this far)
  never actually occurs. `--sudo` is still accepted as a recognized,
  currently-inert flag (never raises "unrecognized argument") rather than
  silently dropped, in case a future relaxation of the bootstrap's root
  requirement resurrects the scenario section 3.1 describes.
- **"Step 2 complete" without a state-machine handle on `Context`.**
  Doc 00 section 12.3's `Context` carries no accessor for another step's
  recorded `state.json` status (Step 2 itself does not depend on Step 1's
  status that way either -- it re-verifies Step 1's pins directly, see
  `step2_deepstream_sdk._check_prereq_pins`). This module follows the same
  pattern: `step2_deepstream_sdk.CONF_METHOD_KEY` is the durable signal
  `step2_deepstream_sdk.run()` writes into `installer.conf` the moment it
  resolves an install method, so `preflight()` checks that key's presence
  in `ctx.conf` rather than inventing a new `Context` capability.

Every subprocess call goes through `ctx.run_root`/`ctx.run_as_user` (per doc
00 section 9.2: docker, git, and anything touching the invoking user's home
-- the AMC clone under `$HOME/auto-magic-calib` -- run via
`ctx.run_as_user`; the one root-only operation, `chown 1000:1000` on
`projects/`/`models/`, runs via `ctx.run_root`), so no test here shells out
to a real docker/git/curl or opens a browser.
"""

from __future__ import annotations

import argparse
import atexit
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Optional

from mv3dt_installer import app as app_mod
from mv3dt_installer import config as config_mod
from mv3dt_installer.steps import step2_deepstream_sdk as step2_mod
from mv3dt_installer.steps import StepResult, StepStatus, UserAction, register

if TYPE_CHECKING:  # pragma: no cover -- import-time only, never at runtime.
    from mv3dt_installer.app import Context

__all__ = [
    "AmcConfig",
    "resolve_config",
    "repo_root",
    "check_repo_isolation",
    "locate_compose_dir",
    "ENV_KEYS",
    "check_env_drift",
    "render_env",
    "write_env_atomic",
    "render_amc_wrapper",
    "write_amc_wrapper",
    "decide_hold_strategy",
    "launch_amc",
    "teardown_amc",
    "handle_amc_subcommand",
    "Step3AmcLauncher",
]

# ---------------------------------------------------------------------------
# STEP-3 section 1 / references -- AMC lives inside the NVIDIA/DeepStream
# monorepo, sparse-checked-out to its own subdirectory (section 4 step 3).
# ---------------------------------------------------------------------------

AMC_REPO_URL = "https://github.com/NVIDIA/DeepStream.git"
AMC_SPARSE_PATH = "tools/auto-magic-calib"

DEFAULT_UI_PORT = "5000"
DEFAULT_MS_PORT = "8000"
DEFAULT_PROJECT_NAME = "default"
DEFAULT_NVIDIA_VISIBLE_DEVICES = "all"

# installer.conf keys (section 4.1's table), mirrored so a "run later" via
# the `amc` exe resolves the same config the installer itself would.
CONF_AMC_ROOT_KEY = "AMC_ROOT"
CONF_HOST_IP_KEY = "HOST_IP"
CONF_UI_PORT_KEY = "AUTO_MAGIC_CALIB_UI_PORT"
CONF_MS_PORT_KEY = "AUTO_MAGIC_CALIB_MS_PORT"
CONF_MS_API_URL_KEY = "AUTO_MAGIC_CALIB_MS_API_URL"
CONF_PROJECT_NAME_KEY = "PROJECT_NAME"
CONF_NVIDIA_VISIBLE_DEVICES_KEY = "NVIDIA_VISIBLE_DEVICES"

_STEP3_CONF_KEYS: tuple[str, ...] = (
    CONF_AMC_ROOT_KEY,
    CONF_HOST_IP_KEY,
    CONF_UI_PORT_KEY,
    CONF_MS_PORT_KEY,
    CONF_PROJECT_NAME_KEY,
    CONF_NVIDIA_VISIBLE_DEVICES_KEY,
)

# section 4.2/4.3 -- the compose/.env key set, verbatim (not the same
# spelling as the installer.conf keys above for HOST_IP/PROJECT_DIR, since
# .env additionally derives PROJECT_DIR/MODEL_DIR from AMC_ROOT).
ENV_KEYS: tuple[str, ...] = (
    "HOST_IP",
    "AUTO_MAGIC_CALIB_MS_PORT",
    "AUTO_MAGIC_CALIB_UI_PORT",
    "PROJECT_DIR",
    "MODEL_DIR",
    "NVIDIA_VISIBLE_DEVICES",
)

AMC_WRAPPER_NAME = "amc"
INSTALLER_BIN_NAME = "mv3dt-installer"

_UI_WAIT_TIMEOUT_S = 30.0
_UI_WAIT_POLL_S = 1.0

# section 5.1 -- browser candidates, in the order the doc lists them.
_CHROMIUM_FAMILY: tuple[str, ...] = (
    "google-chrome",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
)


# ---------------------------------------------------------------------------
# section 4.1 -- config resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AmcConfig:
    amc_root: pathlib.Path
    host_ip: str
    ui_port: str
    ms_port: str
    ms_api_url: str
    project_name: str
    nvidia_visible_devices: str = DEFAULT_NVIDIA_VISIBLE_DEVICES


_HOST_IP_RE = re.compile(r"\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})\b")


def _detect_host_ip(ctx: "Context") -> Optional[str]:
    """`ip route get 1.1.1.1`'s `src <ip>` field, or `None` on any failure.

    Best-effort only -- `resolve_config` falls back to `127.0.0.1` (a safe
    default for the localhost-only flow, section 4.1) when this can't
    determine anything.
    """
    result = ctx.run_root(
        "ip", "route", "get", "1.1.1.1", check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    match = _HOST_IP_RE.search(result.stdout or "")
    return match.group(1) if match else None


def resolve_config(
    ctx: "Context",
    *,
    project: Optional[str] = None,
    host_ip_override: Optional[str] = None,
) -> AmcConfig:
    """Resolve Step 3's config (section 4.1): `installer.conf` first, then
    auto-detection/defaults, with explicit per-call overrides (`--project`,
    `--host-ip`) winning over everything.
    """
    conf = ctx.conf

    amc_root_value = conf.get(CONF_AMC_ROOT_KEY) or str(
        pathlib.Path(ctx.user.home) / "auto-magic-calib"
    )
    amc_root = pathlib.Path(amc_root_value).expanduser()

    host_ip = (
        host_ip_override
        or conf.get(CONF_HOST_IP_KEY)
        or _detect_host_ip(ctx)
        or "127.0.0.1"
    )

    return AmcConfig(
        amc_root=amc_root,
        host_ip=host_ip,
        ui_port=conf.get(CONF_UI_PORT_KEY) or DEFAULT_UI_PORT,
        ms_port=conf.get(CONF_MS_PORT_KEY) or DEFAULT_MS_PORT,
        ms_api_url=conf.get(CONF_MS_API_URL_KEY) or "",
        project_name=project or conf.get(CONF_PROJECT_NAME_KEY) or DEFAULT_PROJECT_NAME,
        nvidia_visible_devices=(
            conf.get(CONF_NVIDIA_VISIBLE_DEVICES_KEY) or DEFAULT_NVIDIA_VISIBLE_DEVICES
        ),
    )


def persist_config(ctx: "Context", cfg: AmcConfig) -> None:
    """Seed the section 4.1 keys into `installer.conf`, once -- so "run
    later" via the `amc` exe always resolves the same config the installer
    itself just did, without clobbering a value an operator hand-edited or
    a later `--host-ip` override chose."""
    values = {
        CONF_AMC_ROOT_KEY: str(cfg.amc_root),
        CONF_HOST_IP_KEY: cfg.host_ip,
        CONF_UI_PORT_KEY: cfg.ui_port,
        CONF_MS_PORT_KEY: cfg.ms_port,
        CONF_PROJECT_NAME_KEY: cfg.project_name,
        CONF_NVIDIA_VISIBLE_DEVICES_KEY: cfg.nvidia_visible_devices,
    }
    for key, value in values.items():
        if key not in ctx.conf:
            config_mod.persist_value(ctx.install_dir, key, value)
            ctx.conf[key] = value


# ---------------------------------------------------------------------------
# section 4 step 2 -- repo-isolation guard
# ---------------------------------------------------------------------------


def repo_root() -> Optional[pathlib.Path]:
    """Best-effort locate this checkout's git root by walking up from this
    file. Returns `None` when not inside a git working tree -- the frozen
    binary's own case, since nothing clones this repo onto a workstation
    (doc 00 section 5) -- in which case there is no repo tree to guard
    against and the isolation check is trivially satisfied.
    """
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return parent
    return None


def check_repo_isolation(amc_root: pathlib.Path) -> Optional[str]:
    """Port of `30_start_amc.sh`'s `REPO_ROOT` guard (section 4 step 2):
    refuse an `AMC_ROOT` that is this repo's working tree, or a child of
    it. Returns an error message when the guard trips, else `None`.
    """
    root = repo_root()
    if root is None:
        return None
    resolved_root = root.resolve()
    resolved_amc = pathlib.Path(amc_root).expanduser()
    try:
        resolved_amc = resolved_amc.resolve()
    except OSError:
        pass
    if resolved_amc == resolved_root or resolved_root in resolved_amc.parents:
        return (
            f"AMC_ROOT ({amc_root}) must not live under this repo ({root}). "
            "Choose a different path (e.g. $HOME/auto-magic-calib)."
        )
    return None


# ---------------------------------------------------------------------------
# section 4 step 3 -- sparse-checkout clone
# ---------------------------------------------------------------------------


def clone_amc(ctx: "Context", amc_root: pathlib.Path) -> bool:
    """Sparse-checkout clone of `tools/auto-magic-calib` out of the
    `NVIDIA/DeepStream` monorepo (section 4 step 3), as the invoking user.
    Returns `True` when a clone was actually performed, `False` when
    `amc_root` already existed (section 4 step 3: "log 'AMC repo already
    present'").
    """
    if amc_root.is_dir():
        return False

    amc_root.parent.mkdir(parents=True, exist_ok=True)
    ctx.run_as_user(
        "git",
        "clone",
        "--filter=blob:none",
        "--no-checkout",
        AMC_REPO_URL,
        str(amc_root),
        check=False,
        capture_output=True,
        text=True,
    )
    ctx.run_as_user(
        "git",
        "-C",
        str(amc_root),
        "sparse-checkout",
        "set",
        AMC_SPARSE_PATH,
        check=False,
        capture_output=True,
        text=True,
    )
    ctx.run_as_user(
        "git", "-C", str(amc_root), "checkout", check=False, capture_output=True, text=True
    )
    return True


# ---------------------------------------------------------------------------
# section 4 step 4 -- projects/ + models/, chown 1000:1000
# ---------------------------------------------------------------------------


def ensure_projects_and_models(
    ctx: "Context", amc_root: pathlib.Path
) -> tuple[pathlib.Path, pathlib.Path]:
    """`mkdir -p projects/ models/` + `chown -R 1000:1000` (section 4 step
    4, Notion section 8.3's ownership tweak so the in-container UID 1000
    can write). Root-owned operation -- runs via `ctx.run_root`, unlike the
    clone/compose calls, which run as the invoking user (doc 00 section
    9.2's "as root this is a direct chown" case).
    """
    projects_dir = amc_root / "projects"
    models_dir = amc_root / "models"
    projects_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)
    ctx.run_root(
        "chown",
        "-R",
        "1000:1000",
        str(projects_dir),
        str(models_dir),
        check=False,
        capture_output=True,
        text=True,
    )
    return projects_dir, models_dir


# ---------------------------------------------------------------------------
# section 4 step 5 -- optional `docker login nvcr.io`
# ---------------------------------------------------------------------------


def docker_login(ctx: "Context") -> bool:
    """`docker login nvcr.io` using the onboarding-stored NGC key (section
    4 step 5), mirroring `step2_deepstream_sdk._docker_login`'s pattern:
    the key is sourced from `secrets/ngc.env` inside the child shell, never
    interpolated into this module's source or passed as a bare CLI
    argument. Best-effort: a failure only warns (AMC images may be
    public) and never fails the step.
    """
    secrets_path = ctx.install_dir / "secrets" / "ngc.env"
    if not secrets_path.is_file():
        ctx.log.warn(
            "NGC_API_KEY not found; assuming 'docker login nvcr.io' was already run"
        )
        return False

    script = (
        'set -a; . "$NGC_ENV_FILE"; set +a; '
        "echo \"$NGC_API_KEY\" | docker login nvcr.io "
        "-u '$oauthtoken' --password-stdin"
    )
    result = ctx.run_as_user(
        "env",
        f"NGC_ENV_FILE={secrets_path}",
        "bash",
        "-lc",
        script,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        ctx.log.warn(
            "'docker login nvcr.io' failed; if AMC images are public you can "
            "ignore this"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# section 4 step 6 -- locate the compose dir (search-order fallback)
# ---------------------------------------------------------------------------


def locate_compose_dir(amc_root: pathlib.Path) -> Optional[pathlib.Path]:
    """Port of `30_start_amc.sh`'s compose-dir search order (section 4 step
    6): the monorepo layout first, then a standalone-repo checkout's own
    `compose/`, then the repo root itself if a compose file sits there
    directly. `None` means "upstream AMC layout changed".
    """
    monorepo = amc_root / "tools" / "auto-magic-calib" / "compose"
    if monorepo.is_dir():
        return monorepo

    fallback = amc_root / "compose"
    if fallback.is_dir():
        return fallback

    if (amc_root / "compose.yaml").is_file() or (amc_root / "docker-compose.yml").is_file():
        return amc_root

    return None


# ---------------------------------------------------------------------------
# section 4.3 -- upstream drift guard
# ---------------------------------------------------------------------------


def check_env_drift(compose_dir: pathlib.Path) -> list[str]:
    """Compare `ENV_KEYS` against `compose_dir/.env.example`. Returns the
    subset of keys no longer defined upstream (section 4.3); an empty list
    (including when `.env.example` itself is absent -- nothing to diff
    against) means no drift detected.
    """
    example = compose_dir / ".env.example"
    try:
        text = example.read_text(encoding="utf-8")
    except OSError:
        return []
    missing = []
    for key in ENV_KEYS:
        if not re.search(rf"(?m)^{re.escape(key)}=", text):
            missing.append(key)
    return missing


# ---------------------------------------------------------------------------
# section 4.2 -- compose/.env contents
# ---------------------------------------------------------------------------


def render_env(cfg: AmcConfig) -> str:
    """Render `compose/.env` exactly as `30_start_amc.sh` does (section
    4.2), including the optional `AUTO_MAGIC_CALIB_MS_API_URL` line only
    when set.
    """
    lines = [
        f"HOST_IP={cfg.host_ip}",
        f"AUTO_MAGIC_CALIB_MS_PORT={cfg.ms_port}",
        f"AUTO_MAGIC_CALIB_UI_PORT={cfg.ui_port}",
        f"PROJECT_DIR={cfg.amc_root / 'projects'}",
        f"MODEL_DIR={cfg.amc_root / 'models'}",
        f"NVIDIA_VISIBLE_DEVICES={cfg.nvidia_visible_devices}",
        f"PROJECT_NAME={cfg.project_name}",
    ]
    if cfg.ms_api_url:
        lines.append(f"AUTO_MAGIC_CALIB_MS_API_URL={cfg.ms_api_url}")
    return "\n".join(lines) + "\n"


def write_env_atomic(compose_dir: pathlib.Path, content: str) -> bool:
    """Write `compose/.env` atomically (tmp + `os.replace`), content-
    idempotent like `systemd.install_unit`. Returns whether the file
    changed.
    """
    dest = compose_dir / ".env"
    if dest.is_file():
        try:
            existing = dest.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing == content:
            return False

    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, dest)
    return True


# ---------------------------------------------------------------------------
# section 4 step 8 -- pull + up / down
# ---------------------------------------------------------------------------


def compose_pull(ctx: "Context", compose_dir: pathlib.Path) -> None:
    ctx.run_as_user(
        "docker",
        "compose",
        "pull",
        cwd=str(compose_dir),
        check=False,
        capture_output=True,
        text=True,
    )


def compose_up(ctx: "Context", compose_dir: pathlib.Path) -> None:
    ctx.run_as_user(
        "docker",
        "compose",
        "up",
        "-d",
        cwd=str(compose_dir),
        check=False,
        capture_output=True,
        text=True,
    )


def compose_down(ctx: "Context", compose_dir: pathlib.Path) -> None:
    ctx.run_as_user(
        "docker",
        "compose",
        "down",
        cwd=str(compose_dir),
        check=False,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# section 4 step 9 -- readiness poll
# ---------------------------------------------------------------------------


def wait_for_ui(
    ctx: "Context",
    url: str,
    *,
    timeout_s: float = _UI_WAIT_TIMEOUT_S,
    poll_s: float = _UI_WAIT_POLL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Port of `30_start_amc.sh`'s `_wait_amc_ui()` (section 4 step 9):
    `curl -sf` the UI URL for up to `timeout_s`. Non-fatal either way --
    the caller logs a `docker compose logs` hint on timeout.
    """
    started = clock()
    while True:
        result = ctx.run_root(
            "curl",
            "-sf",
            "--max-time",
            "1",
            url,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        if clock() - started >= timeout_s:
            return False
        sleep(poll_s)


# ---------------------------------------------------------------------------
# section 5 -- keep-alive-until-browser-closed
# ---------------------------------------------------------------------------


class HoldStrategy(str, Enum):
    """Which of section 5.1/5.2's mechanisms `execute_hold` should use."""

    DEDICATED_WINDOW = "dedicated_window"
    XDG_OPEN_AND_PROMPT = "xdg_open_and_prompt"
    PRINT_URL_AND_PROMPT = "print_url_and_prompt"
    PRINT_URL_AND_LEAVE_UP = "print_url_and_leave_up"


def decide_hold_strategy(
    *, has_display: bool, browser_available: bool, non_interactive: bool
) -> HoldStrategy:
    """The section 5.1/5.2 decision table, as a pure function of three
    booleans -- the "wait/teardown state machine"'s decision half (the
    execution half, `execute_hold`, is a thin dispatcher over this).

    - Desktop session + a monitorable dedicated-window browser available:
      the primary path (section 5.1).
    - Desktop session, no monitorable browser, interactive: `xdg-open` +
      block on operator input (section 5.2's first bullet).
    - Headless (no `DISPLAY`/`WAYLAND_DISPLAY`), interactive: print the URL
      and block on operator input (section 5.2's second bullet).
    - Any of the above with `non_interactive=True` -- there is no browser
      to monitor and no human to prompt, so the only safe answer is to
      leave AMC running and print the stop command (section 5.2's
      `--non-interactive` bullet). This also covers the one combination
      the doc does not spell out explicitly -- a desktop session present
      but no monitorable browser, under `--non-interactive` -- by the same
      "never blindly tear down, never block on nobody" reasoning.
    """
    if has_display and browser_available:
        return HoldStrategy.DEDICATED_WINDOW
    if non_interactive:
        return HoldStrategy.PRINT_URL_AND_LEAVE_UP
    if has_display:
        return HoldStrategy.XDG_OPEN_AND_PROMPT
    return HoldStrategy.PRINT_URL_AND_PROMPT


def find_browser(*, which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """First available browser from the section 5.1 candidate list, in
    order, or `None` if none is on `PATH`. Firefox is checked last, as its
    own fallback within the "dedicated window" family."""
    for name in _CHROMIUM_FAMILY:
        if which(name):
            return name
    if which("firefox"):
        return "firefox"
    return None


def _browser_argv(browser: str, url: str, profile_dir: str) -> list[str]:
    if browser == "firefox":
        return ["firefox", "--new-instance", "--profile", profile_dir, url]
    return [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def open_dedicated_window(
    url: str,
    *,
    popen: Optional[Callable[..., Any]] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    mkdtemp: Callable[..., str] = tempfile.mkdtemp,
) -> Optional[Any]:
    """Launch a brand-new browser *process* in a throwaway profile (section
    5.1) -- the load-bearing bit is the fresh `--user-data-dir`/`--profile`,
    which forces a new OS process the caller can `wait()` on rather than
    handing the URL to an already-running browser and exiting immediately.
    Returns the `Popen` handle, or `None` if no candidate browser is on
    `PATH`.
    """
    popen = popen or subprocess.Popen
    browser = find_browser(which=which)
    if browser is None:
        return None
    profile_dir = mkdtemp(prefix="mv3dt-amc-browser-")
    argv = _browser_argv(browser, url, profile_dir)
    return popen(argv)


def _xdg_open(url: str, *, popen: Optional[Callable[..., Any]] = None) -> None:
    popen = popen or subprocess.Popen
    try:
        popen(["xdg-open", url])
    except OSError:
        pass


def _install_teardown_guards(teardown: Callable[[], None]) -> Callable[[], None]:
    """Install the section 5.1 step 3 fail-safe: `SIGINT`/`SIGTERM`/normal
    exit all run `teardown` exactly once (`docker compose down`)."""
    state = {"ran": False}

    def _run_once(*_args: Any) -> None:
        if not state["ran"]:
            state["ran"] = True
            teardown()

    atexit.register(_run_once)

    def _signal_handler(signum: int, _frame: Any) -> None:
        _run_once()
        sys.exit(128 + signum)

    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except ValueError:
        # Not the main thread / no signal support here (e.g. under a test
        # runner) -- atexit is still installed, which is the fail-safe that
        # matters most.
        pass

    return _run_once


def execute_hold(
    ctx: "Context",
    strategy: HoldStrategy,
    url: str,
    *,
    teardown: Callable[[], None],
    keep_up: bool = False,
    popen: Optional[Callable[..., Any]] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    prompt: Callable[[str], str] = input,
) -> None:
    """Execute the strategy `decide_hold_strategy` chose. `--keep-up`
    (section 5.1 step 3) short-circuits every strategy: AMC is left
    running and `teardown` is never called.
    """
    if keep_up:
        ctx.log.info(f"--keep-up set: leaving AMC running at {url}")
        return

    if strategy is HoldStrategy.DEDICATED_WINDOW:
        proc = open_dedicated_window(url, popen=popen, which=which)
        if proc is not None:
            _install_teardown_guards(teardown)
            ctx.log.info(
                f"AutoMagicCalib is running at {url} -- close the AMC browser "
                "window when you're done; the service stays up until you do."
            )
            proc.wait()
            teardown()
            return
        # No browser after all (race between decide_hold_strategy's check
        # and here, or a caller passed the strategy in directly) -- fall
        # through to the next-best interactive/headless behavior.
        strategy = HoldStrategy.PRINT_URL_AND_PROMPT

    if strategy is HoldStrategy.XDG_OPEN_AND_PROMPT:
        _xdg_open(url, popen=popen)
        _install_teardown_guards(teardown)
        try:
            prompt("Press Enter (or Ctrl-C) when you have closed AMC to shut it down.")
        except KeyboardInterrupt:
            pass
        teardown()
        return

    if strategy is HoldStrategy.PRINT_URL_AND_PROMPT:
        ctx.log.info(f"Headless session detected; open {url} in your browser.")
        _install_teardown_guards(teardown)
        try:
            prompt("Press Enter (or Ctrl-C) when you have closed AMC to shut it down.")
        except KeyboardInterrupt:
            pass
        teardown()
        return

    # PRINT_URL_AND_LEAVE_UP -- non-interactive with nothing to monitor and
    # nobody to prompt: never tear down blindly.
    ctx.log.info(
        f"AMC left running at {url} (non-interactive, nothing to monitor). "
        "Stop it later with `amc --down`."
    )


# ---------------------------------------------------------------------------
# section 6 -- the standalone `amc` executable
# ---------------------------------------------------------------------------


def ensure_installer_binary(ctx: "Context") -> pathlib.Path:
    """`<install_dir>/bin/mv3dt-installer` (section 6.1 step 1): a stable
    copy of the running executable, so the `amc` wrapper survives the
    operator's downloaded copy being moved or deleted. Copies from
    `sys.executable` only when frozen (`sys.frozen`, matching
    `mv3dt_installer/__init__.py`'s own gate for its build stamp) -- a dev
    checkout has no single binary to copy, so a pre-existing destination is
    left alone and a missing one is logged rather than fabricated.
    """
    bin_dir = ctx.install_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / INSTALLER_BIN_NAME

    if getattr(sys, "frozen", False):
        src = pathlib.Path(sys.executable)
        if src.is_file() and (
            not dest.exists() or dest.stat().st_size != src.stat().st_size
        ):
            shutil.copy2(src, dest)
            dest.chmod(0o755)
    elif not dest.exists():
        ctx.log.warn(
            f"not a frozen binary; cannot stage {dest} from a source checkout "
            "(this is expected in dev/test -- a release build always runs frozen)"
        )

    return dest


def render_amc_wrapper(installer_bin: pathlib.Path) -> str:
    """Render the `amc` wrapper script (section 6.1 step 2, verbatim)."""
    return (
        "#!/usr/bin/env bash\n"
        "# Generated by mv3dt-installer Step 3. Brings up AutoMagicCalib, opens\n"
        "# the localhost UI, and holds it open until you close the AMC browser\n"
        "# window.\n"
        f'exec "{installer_bin}" amc "$@"\n'
    )


def write_amc_wrapper(
    ctx: "Context", installer_bin: pathlib.Path
) -> tuple[pathlib.Path, bool]:
    """Write `<install_dir>/bin/amc`, `chmod +x`, chowned to the invoking
    user (section 6.1 step 2). Returns `(path, changed)`."""
    bin_dir = ctx.install_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / AMC_WRAPPER_NAME
    content = render_amc_wrapper(installer_bin)

    changed = True
    if dest.is_file():
        try:
            changed = dest.read_text(encoding="utf-8") != content
        except OSError:
            changed = True

    if changed:
        dest.write_text(content, encoding="utf-8")

    dest.chmod(0o755)
    try:
        os.chown(dest, ctx.user.uid, ctx.user.gid)
    except OSError:
        pass  # best-effort, e.g. under a non-root test process

    return dest, changed


# ---------------------------------------------------------------------------
# section 3 -- Docker + NVIDIA Container Toolkit prerequisite (assert-only)
# ---------------------------------------------------------------------------


def _docker_usable(ctx: "Context") -> bool:
    version = ctx.run_as_user(
        "docker", "--version", check=False, capture_output=True, text=True
    )
    if version.returncode != 0:
        return False
    info = ctx.run_as_user("docker", "info", check=False, capture_output=True, text=True)
    return info.returncode == 0


def _compose_available(ctx: "Context") -> bool:
    result = ctx.run_as_user(
        "docker", "compose", "version", check=False, capture_output=True, text=True
    )
    if result.returncode == 0:
        return True
    legacy = ctx.run_as_user(
        "docker-compose", "version", check=False, capture_output=True, text=True
    )
    return legacy.returncode == 0


def _nvidia_runtime_registered(ctx: "Context") -> bool:
    info = ctx.run_as_user("docker", "info", check=False, capture_output=True, text=True)
    return "nvidia" in (info.stdout or "").lower()


def _git_present(ctx: "Context") -> bool:
    result = ctx.run_root("which", "git", check=False, capture_output=True, text=True)
    return result.returncode == 0


def _docker_install_actions(ctx: "Context") -> list[UserAction]:
    """The section 3 remediation block, verbatim commands."""
    return [
        UserAction(
            text="Install Docker Engine + the compose plugin.",
            command="sudo apt-get install -y docker.io docker-compose-plugin",
        ),
        UserAction(
            text="Install the NVIDIA Container Toolkit.",
            command="sudo apt-get install -y nvidia-container-toolkit",
        ),
        UserAction(
            text="Wire the toolkit into the Docker daemon.",
            command="sudo nvidia-ctk runtime configure --runtime=docker",
        ),
        UserAction(text="Restart Docker.", command="sudo systemctl restart docker"),
        UserAction(
            text="Let the invoking user run docker without sudo.",
            command=f"sudo usermod -aG docker {ctx.user.name}",
        ),
    ]


# ---------------------------------------------------------------------------
# The shared bring-up routine (section 4-5)
# ---------------------------------------------------------------------------


def launch_amc(
    ctx: "Context",
    *,
    project: Optional[str] = None,
    skip_pull: bool = False,
    keep_up: bool = False,
    host_ip_override: Optional[str] = None,
    no_open: bool = False,
    non_interactive: bool = False,
) -> StepResult:
    """The shared AMC bring-up + hold-until-close routine (sections 4-5).
    Called by `run()`'s optional immediate launch, the registered `amc`
    subcommand, and (a later unit) Step 5's re-run entry point."""
    cfg = resolve_config(ctx, project=project, host_ip_override=host_ip_override)

    guard = check_repo_isolation(cfg.amc_root)
    if guard is not None:
        return StepResult(status=StepStatus.FAILED, message=guard)

    persist_config(ctx, cfg)

    cloned = clone_amc(ctx, cfg.amc_root)
    if cloned:
        ctx.report_installed("auto-magic-calib", f"{AMC_SPARSE_PATH}@main")
    else:
        ctx.report_already_installed("auto-magic-calib", f"{AMC_SPARSE_PATH}@main")

    ensure_projects_and_models(ctx, cfg.amc_root)
    docker_login(ctx)

    compose_dir = locate_compose_dir(cfg.amc_root)
    if compose_dir is None:
        return StepResult(
            status=StepStatus.FAILED,
            message=(
                f"cannot locate compose dir inside {cfg.amc_root}; upstream AMC "
                "layout changed -- re-check the tools/auto-magic-calib README "
                "in NVIDIA/DeepStream"
            ),
        )

    for key in check_env_drift(compose_dir):
        ctx.log.warn(
            f"upstream AMC .env.example no longer defines '{key}'; re-check the "
            "tools/auto-magic-calib README in NVIDIA/DeepStream"
        )

    env_changed = write_env_atomic(compose_dir, render_env(cfg))
    (cfg.amc_root / "projects" / cfg.project_name).mkdir(parents=True, exist_ok=True)
    if env_changed:
        ctx.report_installed("AMC compose/.env", cfg.project_name)
    else:
        ctx.report_already_installed("AMC compose/.env", cfg.project_name)

    if not skip_pull:
        compose_pull(ctx, compose_dir)
    compose_up(ctx, compose_dir)

    ui_url = f"http://localhost:{cfg.ui_port}"
    if not wait_for_ui(ctx, ui_url):
        ctx.log.warn(
            f"AMC UI did not respond within {int(_UI_WAIT_TIMEOUT_S)}s. Check: "
            f"cd {compose_dir} && docker compose logs"
        )

    if no_open:
        return StepResult(status=StepStatus.COMPLETE)

    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    browser_available = find_browser() is not None
    strategy = decide_hold_strategy(
        has_display=has_display,
        browser_available=browser_available,
        non_interactive=non_interactive,
    )

    def _teardown() -> None:
        compose_down(ctx, compose_dir)

    execute_hold(ctx, strategy, ui_url, teardown=_teardown, keep_up=keep_up)

    return StepResult(status=StepStatus.COMPLETE)


def teardown_amc(
    ctx: "Context",
    *,
    project: Optional[str] = None,
    host_ip_override: Optional[str] = None,
) -> StepResult:
    """`amc --down`: tear down without bringing anything up."""
    cfg = resolve_config(ctx, project=project, host_ip_override=host_ip_override)
    compose_dir = locate_compose_dir(cfg.amc_root)
    if compose_dir is None:
        return StepResult(
            status=StepStatus.FAILED,
            message=f"AMC not found at {cfg.amc_root}; nothing to tear down",
        )
    compose_down(ctx, compose_dir)
    return StepResult(status=StepStatus.COMPLETE)


# ---------------------------------------------------------------------------
# section 6.2 -- the `amc` subcommand
# ---------------------------------------------------------------------------


def _build_amc_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mv3dt-installer amc", add_help=True)
    parser.add_argument("--project", default=None)
    parser.add_argument("--skip-pull", action="store_true")
    parser.add_argument("--keep-up", action="store_true")
    parser.add_argument("--down", action="store_true")
    parser.add_argument("--host-ip", default=None)
    # Accepted but currently inert -- see this module's docstring's "Root
    # for the standalone amc subcommand" note: the framework's subcommand
    # bootstrap already requires root unconditionally, so the docker-group
    # re-exec scenario --sudo exists for never actually arises today.
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    # doc 00 §3.3-shaped framework flags a subcommand's own argv may carry
    # (systemd ExecStart= lines, `app._bootstrap_subcommand_context`'s own
    # peek parser) -- accepted here too so `amc`'s parser does not reject
    # them, even though this handler does not need their values itself
    # (the bootstrap already resolved them into `ctx`).
    parser.add_argument("--install-dir", default=None)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--log-dir", default=None)
    return parser


def handle_amc_subcommand(argv: list, ctx: "Context") -> int:
    """`mv3dt-installer amc [...]` (section 6.2's registered handler)."""
    args = _build_amc_arg_parser().parse_args(argv)

    if args.down:
        result = teardown_amc(ctx, project=args.project, host_ip_override=args.host_ip)
    else:
        result = launch_amc(
            ctx,
            project=args.project,
            skip_pull=args.skip_pull,
            keep_up=args.keep_up,
            host_ip_override=args.host_ip,
            no_open=args.no_open,
            non_interactive=ctx.non_interactive,
        )

    if result.status is StepStatus.FAILED:
        ctx.log.error(result.message)
        return 1
    if result.status is StepStatus.USER_ACTION_REQUIRED:
        from mv3dt_installer import privilege

        privilege.show_user_action_block(
            "AutoMagicCalib launcher", result.message, result.user_actions
        )
        return 0
    return 0


# ---------------------------------------------------------------------------
# section 2 -- the "launch now vs. later" prompt
# ---------------------------------------------------------------------------

# Injectable so tests never block on real input().
_INPUT: Callable[[str], str] = input


def _confirm_launch_now(ctx: "Context") -> bool:
    """`Launch AutoMagicCalib now? [y/N]`, default No (section 2).
    `--non-interactive`/`--no-pause` skip the prompt entirely (doc 00
    §3.3's `--no-pause` is a framework-level flag `Context` does not carry,
    so this module honors only `ctx.non_interactive`, consistent with how
    Step 2 makes the same decision)."""
    if ctx.non_interactive:
        return False
    answer = _INPUT("Launch AutoMagicCalib now? [y/N]: ").strip().lower()
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# The Step
# ---------------------------------------------------------------------------


class Step3AmcLauncher:
    """STEP-3-AMC-LAUNCHER.md section 1: module identity."""

    id = "step3_amc_launcher"
    title = "AutoMagicCalib launcher"
    order = 3

    # -- preflight (section 7.1) -------------------------------------------

    def preflight(self, ctx: "Context") -> StepResult:
        if not ctx.conf.get(step2_mod.CONF_METHOD_KEY):
            return StepResult(
                status=StepStatus.FAILED,
                message="DeepStream SDK not installed; run Step 2 first",
            )

        if not _git_present(ctx):
            return StepResult(
                status=StepStatus.USER_ACTION_REQUIRED,
                message="git is required to clone AutoMagicCalib",
                user_actions=[
                    UserAction(text="Install git.", command="sudo apt-get install -y git")
                ],
            )

        if (
            not _docker_usable(ctx)
            or not _compose_available(ctx)
            or not _nvidia_runtime_registered(ctx)
        ):
            return StepResult(
                status=StepStatus.USER_ACTION_REQUIRED,
                message=(
                    "Docker Engine, the compose plugin, or the NVIDIA Container "
                    "Toolkit runtime is not ready"
                ),
                user_actions=_docker_install_actions(ctx),
            )

        return StepResult(status=StepStatus.COMPLETE)

    # -- run (section 7.2) ---------------------------------------------------

    def run(self, ctx: "Context") -> StepResult:
        cfg = resolve_config(ctx)

        guard = check_repo_isolation(cfg.amc_root)
        if guard is not None:
            return StepResult(status=StepStatus.FAILED, message=guard)

        persist_config(ctx, cfg)

        installer_bin = ensure_installer_binary(ctx)
        wrapper_path, wrapper_changed = write_amc_wrapper(ctx, installer_bin)
        if wrapper_changed:
            ctx.report_installed("amc launcher", str(wrapper_path))
        else:
            ctx.report_already_installed("amc launcher", str(wrapper_path))

        if not _confirm_launch_now(ctx):
            return StepResult(status=StepStatus.COMPLETE)

        result = launch_amc(ctx, non_interactive=ctx.non_interactive)
        if result.status is not StepStatus.COMPLETE:
            return result

        return StepResult(status=StepStatus.COMPLETE)

    # -- verify (section 7.3) -------------------------------------------------

    def verify(self, ctx: "Context") -> StepResult:
        wrapper = ctx.install_dir / "bin" / AMC_WRAPPER_NAME
        installer_bin = ctx.install_dir / "bin" / INSTALLER_BIN_NAME

        if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
            return StepResult(
                status=StepStatus.FAILED,
                message=f"{wrapper} missing or not executable",
            )
        if not installer_bin.is_file():
            return StepResult(
                status=StepStatus.FAILED, message=f"{installer_bin} missing"
            )
        if not _compose_available(ctx):
            return StepResult(
                status=StepStatus.FAILED, message="docker compose is not available"
            )
        if not _nvidia_runtime_registered(ctx):
            return StepResult(
                status=StepStatus.FAILED,
                message="the nvidia runtime is not registered with docker",
            )
        for key in _STEP3_CONF_KEYS:
            if key not in ctx.conf:
                return StepResult(
                    status=StepStatus.FAILED,
                    message=f"{key} missing from installer.conf",
                )

        return StepResult(status=StepStatus.COMPLETE)

    # -- report (section 7.4) --------------------------------------------------

    def report(self, ctx: "Context") -> None:
        cfg = resolve_config(ctx)
        wrapper = ctx.install_dir / "bin" / AMC_WRAPPER_NAME
        ctx.log.info(
            "AutoMagicCalib launcher installed.\n"
            f"  Run it any time:   {wrapper}\n"
            f"  Web UI:            http://localhost:{cfg.ui_port}\n"
            f"  Microservice API:  http://localhost:{cfg.ms_port}\n"
            f"  AMC clone:         {cfg.amc_root}\n"
            f"  Stop AMC:          {wrapper} --down   (or close the AMC window)\n"
            "\n"
            "The AMC service stays up until you close the AMC browser window "
            "(or run --down).\n"
            "Next: complete the 6-step calibration in the browser (see the DS "
            "9.1 AutoMagicCalib guide), then Step 4 ingests the export."
        )


register(Step3AmcLauncher())
app_mod.register_subcommand("amc", handle_amc_subcommand)
