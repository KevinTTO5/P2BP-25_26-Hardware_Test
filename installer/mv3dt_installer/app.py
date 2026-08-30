"""Top-level orchestrator / dispatch loop for mv3dt-installer.

Implements doc `installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md` §3 (module
layout §3.1 lists this file as "app.py -- top-level TUI orchestrator /
dispatch loop"; entrypoint + dispatch §3.2; CLI flags §3.3; opt-in step
gates §3.4) and §12.3 (the `Context` object passed to every step's
`preflight/run/verify/report` lifecycle method).

This module is integrated last on purpose: it is the concrete
implementation of the `Context` forward reference `steps/__init__.py`'s
`Step` protocol declares but deliberately never imports (that module uses
`from __future__ import annotations` specifically so it wouldn't need this
not-yet-built class -- see its own docstring). It also depends on every
other framework module (`state`, `config`, `reboot`, `privilege`, `ngc`,
`webapp`, `report`, `shellout`, `logs`) -- all wave 1-3 work, already merged
in before this file was written.

Ordering note (doc 00 §3.2's 11-step list vs. `state.json`'s fixed path)
----------------------------------------------------------------------
Doc 00 §3.2's numbered list reads naturally as top-to-bottom prose, but one
detail only becomes clear from `config.load()`'s actual signature: it takes
a `state: StateMachine` parameter, because it needs to check `state.json`'s
`install_dir` field for precedence (doc 00 §11.2: `--install-dir` >
`state.json` > `installer.conf` default > `/opt/mv3dt`). So a
`StateMachine` must exist before config is loaded, even though the doc's
prose lists "load the state file" and "load config" as separate, ordered
steps.

This is resolved cleanly, not accidentally: doc 00 §6.1 says the state file
"stays at the canonical `/var/lib/mv3dt-installer/state.json` path" even
when `--install-dir` moves the install root -- `state.json`'s path is fixed
and entirely independent of `install_dir`. There is therefore no real
ordering conflict, just an ambiguity in exactly which of two adjacent list
items happens first. This module implements the doc's full ordering
precisely:

    1. parse flags
    2. construct/load the `StateMachine` at its fixed canonical path
       (honoring `--reset-state` / `--reset-step` here, before anything
       else touches it)
    3. `--status` early exit (pre-root; reads state.json only)
    4. `privilege.require_root()`
    5. `onboarding.run_platform_preflight()` (replaces the bare
       `privilege.resolve()` this module used before `preflight.py`
       existed)
    6. apply the reset flags
    7. `config.load(..., state=<that StateMachine>, gate_overrides=...)`
    8. open the transcript (`logs.open_transcript()`)
    9. `onboarding.onboard(...)` -- first-run credential capture
    10. `reboot.reconcile(state)` using that same already-loaded
        `StateMachine`
    11. enter the dispatch loop

`--status` is a deliberate exception to this order -- see `main()`'s
docstring.
"""

from __future__ import annotations

import argparse
import contextlib
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

from mv3dt_installer import __version__, build_stamp
from mv3dt_installer import cameras as cameras_mod
from mv3dt_installer import config as config_mod
from mv3dt_installer import ngc as ngc_mod
from mv3dt_installer import onboarding
from mv3dt_installer import privilege
from mv3dt_installer import reboot as reboot_mod
from mv3dt_installer import report
from mv3dt_installer import shellout
from mv3dt_installer import webapp as webapp_mod
from mv3dt_installer.logs import log, open_transcript
from mv3dt_installer.privilege import InvokingUser
from mv3dt_installer.state import (
    CANONICAL_STATE_PATH,
    STEP_IDS,
    StateMachine,
    default_state,
)
from mv3dt_installer.steps import STEP_REGISTRY, Step, StepResult, StepStatus

__all__ = [
    "build_parser",
    "parse_args",
    "Context",
    "NgcHandle",
    "WebappHandle",
    "RebootHandle",
    "build_context",
    "SUBCOMMAND_REGISTRY",
    "register_subcommand",
    "main",
]


# ---------------------------------------------------------------------------
# doc 00 §3.3 -- CLI flags (framework-level)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the framework-level `argparse.ArgumentParser` (doc 00 §3.3).

    Every flag here is owned by the framework; individual steps may add
    their own in a later, out-of-scope PR (doc 00 §3.3's opening line) --
    none exist yet since `STEP_REGISTRY` is empty.
    """
    parser = argparse.ArgumentParser(
        prog="mv3dt-installer",
        description=(
            "Single self-contained installer for the DeepStream 9.1 / "
            "AutoMagicCalib MV3DT workstation stack."
        ),
    )
    parser.add_argument(
        "--install-dir",
        metavar="PATH",
        default=None,
        help="Override the default install location (doc 00 §11).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Default behavior; explicit for clarity (effectively a no-op).",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the state table and exit.",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Wipe state.json and start fresh "
        "(mirrors 00_bootstrap.sh --reset-state).",
    )
    parser.add_argument(
        "--reset-step",
        metavar="N",
        type=int,
        default=None,
        help="Clear one step's completion (by its .order) so it re-runs.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt; use defaults/config; fail if a required value "
        "is missing (mirrors 00_bootstrap.sh --non-interactive).",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="Skip 'press Enter' confirmations.",
    )
    parser.add_argument(
        "--log-dir",
        metavar="PATH",
        default=None,
        help="Override the transcript directory (doc 00 §8.2).",
    )
    # doc 00 §3.4's gate table promises "a matching CLI flag" for each
    # opt-in gate. Both default to None rather than to "off" on purpose:
    # config.load() must be able to tell "the operator did not pass this"
    # (fall through to env var, then prompt, then "off") from "the operator
    # explicitly passed off" (which overwrites an already-persisted value).
    # Collapsing the two would make every run silently reassert "off" over
    # whatever the operator had previously chosen.
    parser.add_argument(
        "--remote-supervision",
        choices=config_mod.GATE_CHOICES[config_mod.GATE_REMOTE_SUPERVISION],
        default=None,
        help="Set the Step 6 remote-supervision gate "
        "(MV3DT_REMOTE_SUPERVISION in installer.conf, doc 00 §3.4). "
        "Overrides an already-persisted value.",
    )
    parser.add_argument(
        "--webapp-integration",
        choices=config_mod.GATE_CHOICES[config_mod.GATE_WEBAPP_INTEGRATION],
        default=None,
        help="Set the Step 7 web-app-integration gate "
        "(MV3DT_WEBAPP_INTEGRATION in installer.conf, doc 00 §3.4). "
        "Overrides an already-persisted value.",
    )
    parser.add_argument(
        "--scan-cameras",
        action="store_true",
        help="Discover the camera fleet by MAC OUI, probe RTSP, run the "
        "one-time position binding, write cameras.yml/cameras.scan.json, "
        "print the table, and exit (doc 00 §15). Needs sudo for raw "
        "sockets, so unlike --status this runs after the root check.",
    )
    parser.add_argument(
        "--camera-scan-cidr",
        metavar="CIDR",
        default=None,
        help="Override the discovery sweep range "
        f"(default {cameras_mod.DEFAULT_SCAN_CIDR}); persisted as "
        "CAMERA_SCAN_CIDR (doc 00 §11.2).",
    )
    parser.add_argument(
        "--camera-scan-iface",
        metavar="IFACE",
        default=None,
        help="Restrict discovery to one interface; persisted as "
        "CAMERA_SCAN_IFACE (doc 00 §11.2).",
    )
    parser.add_argument(
        "--version",
        action="version",
        # A release binary also names the tag, commit, and build time CI
        # stamped into it (doc 00 section 4.1); a source checkout renders
        # the bare version. The program name is spelled literally rather
        # than via argparse's %(prog)s so the banner reads the same however
        # the installer was invoked, which matters when an operator pastes
        # it into a bug report.
        version=f"mv3dt-installer {__version__}{build_stamp()}",
    )
    return parser


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse `argv` (or `sys.argv[1:]` when `None`) against `build_parser()`."""
    return build_parser().parse_args(argv)


# ---------------------------------------------------------------------------
# doc 00 §12.3 -- Context object
# ---------------------------------------------------------------------------


@dataclass
class NgcHandle:
    """Binds `ngc.py`'s install-dir-parameterized API to the resolved
    `install_dir` (doc 00 §12.3: "ngc handle (`load_key`,
    `configure_ngc_cli`, §10)"). The NGC key is required, so a present key
    is guaranteed by the time any step runs -- `load_key()` returning
    `None` here would mean onboarding did not complete, not a fallback
    state a step needs to branch on."""

    install_dir: pathlib.Path

    def load_key(self) -> Optional[str]:
        return ngc_mod.load_key(self.install_dir)

    def configure_ngc_cli(self) -> Optional[pathlib.Path]:
        return ngc_mod.configure_ngc_cli(self.install_dir)


@dataclass
class WebappHandle:
    """Binds `webapp.py`'s API to the resolved `install_dir` and the
    already-resolved `MV3DT_WEBAPP_INTEGRATION` gate value (doc 00 §12.3:
    "webapp handle (`load_credentials`, `enabled`) -- the web-app API key
    and normalized endpoint, for steps that talk to the backend")."""

    install_dir: pathlib.Path
    gate_value: str

    def load_credentials(self) -> Optional[webapp_mod.Credentials]:
        return webapp_mod.load_credentials(self.install_dir)

    def enabled(self) -> bool:
        """Doc 00 §14.3's `enabled()` rule (gate == "on" AND
        `load_credentials()` succeeds), bound to *this* `install_dir`.

        `webapp.enabled(gate_value)` itself only accepts the gate value as
        a parameter and always reads credentials from its own module-level
        `DEFAULT_INSTALL_DIR` (see that module's docstring) -- not
        necessarily the operator's actually-chosen install dir. This
        re-implements the same one-line rule against `self.install_dir` so
        the handle is correctly bound the way doc 00 §12.3 asks for.
        """
        return self.gate_value == "on" and self.load_credentials() is not None


@dataclass
class RebootHandle:
    """`reboot.request()` helper (doc 00 §12.3): "just returns
    `StepStatus.REBOOT_REQUIRED` -- the framework does the boot-id
    bookkeeping itself, not the step." The actual bookkeeping
    (`state.set_reboot_pending(...)`) happens in the dispatch loop
    (`_dispatch`, below) once it observes this status returned from a
    step's lifecycle method."""

    def request(self) -> StepStatus:
        return StepStatus.REBOOT_REQUIRED


@dataclass
class Context:
    """Concrete implementation of the `Context` forward reference used
    throughout `steps/__init__.py`'s `Step` protocol (doc 00 §12.3)."""

    install_dir: pathlib.Path
    conf: dict
    user: InvokingUser
    log: Any
    report_installed: Callable[[str, str], None]
    report_already_installed: Callable[[str, str], None]
    verify_pinned: Callable[[str, str, str], bool]
    ngc: NgcHandle
    webapp: WebappHandle
    asset_path: Callable[..., pathlib.Path]
    reboot: RebootHandle
    non_interactive: bool

    def run_as_user(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
        """Doc 00 §9.2: anything that must run "as the user" (the `ngc`
        CLI, `docker` without sudo, files under the user's home) MUST go
        through this rather than running unwrapped as root."""
        return privilege.run_as_user(*args, **kwargs)

    def run_root(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
        """Doc 00 §12.3's "run_root(...) convenience". `privilege.py`
        defines no such helper because by the time any step executes, the
        whole process already runs as root (`privilege.require_root()`
        gates `main()`, below) -- "running as root" at that point means
        nothing more than a plain `subprocess.run` with no user-switching,
        so that's exactly what this does."""
        return subprocess.run(args, **kwargs)


def build_context(
    cfg: config_mod.Config, user: InvokingUser, non_interactive: bool
) -> Context:
    """Assemble the `Context` a dispatched step's lifecycle methods receive
    (doc 00 §12.3), bound to the resolved `Config` and invoking user.

    `non_interactive` mirrors the `--non-interactive` flag (doc 00 §3.3)
    straight onto the `Context`, so a step's `preflight/run/verify` methods
    can branch on it directly instead of guessing from `sys.argv` or
    `sys.stdin.isatty()` -- both workarounds Step 1 and Step 2 used before
    this field existed.
    """
    return Context(
        install_dir=cfg.install_dir,
        conf=cfg.values,
        user=user,
        log=log,
        report_installed=report.report_installed,
        report_already_installed=report.report_already_installed,
        verify_pinned=report.verify_pinned,
        ngc=NgcHandle(install_dir=cfg.install_dir),
        webapp=WebappHandle(
            install_dir=cfg.install_dir, gate_value=cfg.webapp_integration
        ),
        asset_path=shellout.asset_path,
        reboot=RebootHandle(),
        non_interactive=non_interactive,
    )


# ---------------------------------------------------------------------------
# STEP-3 §6.2 -- generic subcommand dispatch extension
# ---------------------------------------------------------------------------
#
# STEP-3-AMC-LAUNCHER.md §6.2 flags a "small framework CLI dispatch
# extension owned by DevA": `mv3dt-installer amc [...]` must bypass the
# `--status`/dispatch-loop flow entirely and call Step 3's launcher
# directly, so it also runs standalone, long after install (not only as
# part of a resumable install run). STEP-5's per-project exes reuse it
# (doc 00 §12.4: "Step 5 ... reuses the `amc` exe"), and STEP-4/6's own
# docs describe a `mv3dt-installer ingest ...` / `... pipeline ...` /
# `... agent ...` invocation on the same shape (e.g. the `ingest` line
# baked into `systemd.render_ingest_units`'s rendered `ExecStart=`). This
# registry is built generically enough that each of those steps only needs
# to call `register_subcommand(name, handler)` at import time -- mirroring
# `steps.register()` -- without any further change to this module.

#: A subcommand handler receives the remaining argv (everything after the
#: subcommand name itself) and a real, bootstrapped `Context` (built by
#: `_bootstrap_subcommand_context`, below); it returns the process exit
#: code, exactly like `main()` itself.
SubcommandHandler = Callable[[list, Context], int]


@dataclass(frozen=True)
class _SubcommandRegistration:
    """One `SUBCOMMAND_REGISTRY` entry: a handler plus its root requirement
    (unit U6's fix -- see `register_subcommand`'s docstring)."""

    handler: SubcommandHandler
    requires_root: Any = True


#: Populated at import time by each owning step module (`step3_amc_launcher
#: .register_subcommand("amc", ...)`), the same "import side effect"
#: pattern `steps.STEP_REGISTRY` uses. Empty until that module is actually
#: imported -- see `register_subcommand`'s docstring for the pre-existing
#: gap this shares with `STEP_REGISTRY`.
SUBCOMMAND_REGISTRY: dict[str, _SubcommandRegistration] = {}


def register_subcommand(
    name: str,
    handler: SubcommandHandler,
    *,
    requires_root: Any = True,
) -> None:
    """Register a top-level subcommand (STEP-3 §6.2).

    Call this once at module scope in the owning step module, mirroring
    `steps.register()`'s calling convention:

        from mv3dt_installer import app

        def _handle_amc(argv: list[str], ctx: "app.Context") -> int:
            ...

        app.register_subcommand("amc", _handle_amc)

    Note the asymmetry with `steps.register()`: that registry is populated
    by importing `steps/__init__.py`'s own package, which is not the case
    here -- nothing today imports `step3_amc_launcher` (or any other step
    module) as a side effect of importing `app`, so `SUBCOMMAND_REGISTRY`
    stays empty in a process that never explicitly imports a step module.
    `__main__.py` gains that import once Step 3 (and later steps) actually
    exist as importable modules; wiring that up is a pre-existing gap
    shared with `STEP_REGISTRY` (see that registry's own docstring) and is
    out of scope for this extension point itself.

    `requires_root` (unit U6's fix for a cross-cutting defect PR #50's
    review surfaced): whether `_bootstrap_subcommand_context()` must call
    `privilege.require_root()` before this subcommand's handler runs (doc
    00 §9.1). Defaults to `True`, matching every registration that existed
    before this parameter did (`amc`, `ingest`, `reporter`, `uploader`, and
    -- pre-STEP-6 -- `pipeline`), so this is a strictly additive change:
    nothing about an existing registration's behavior changes unless it
    opts out.

    Two shapes are accepted, for the two ways a subcommand can be
    non-root:

    - A plain `bool`, for a subcommand whose *entire* process runs as the
      non-root invoking user (STEP-6's `agent`, per its
      `mv3dt-agent.service.in`'s `User=@USER@` -- the whole point of its
      scoped polkit rule is that the agent is never root).
    - A `Callable[[list[str]], bool]` -- handed the subcommand's own argv,
      the exact list its handler also receives -- for a subcommand where
      only SOME modes run non-root. STEP-5's `pipeline` is the only
      example today: its `--service-exec` mode is what
      `mv3dt-pipeline@.service.in`'s own non-root `ExecStart=` invokes, but
      its default/`--stop`/`--stop-all`/`--foreground`/`--dry-run` modes
      still call `ctx.run_root`/expect to run under `sudo`, unchanged.
    """
    SUBCOMMAND_REGISTRY[name] = _SubcommandRegistration(
        handler=handler, requires_root=requires_root
    )


def _read_only_subcommand_config(
    known: argparse.Namespace, sm: StateMachine
) -> config_mod.Config:
    """Read-only counterpart to `config.load()`, for a `requires_root=False`
    subcommand (unit U6's fix).

    `config.load()` (module docstring: "Side effects: creates `install_dir`
    if missing, reads-or-writes `installer.conf` under it, and ... writes it
    back into `state.json`") assumes root the same way `require_root()`
    does: `install_dir` (default `/opt/mv3dt`) and the canonical
    `state.json` (`state.CANONICAL_STATE_PATH`, root-owned, its `save()`
    unconditionally `chmod`s both the file and its parent dir) were created
    by a *root* install run. A non-root subcommand -- STEP-6's `agent`,
    STEP-5's `pipeline --service-exec` -- only ever runs long after that
    install completed, so it has no business creating or mutating either:
    it just needs to read back the `install_dir`/`installer.conf` that
    install already resolved and persisted.

    This reimplements `config.load()`'s precedence chain (`--install-dir` >
    `state.json` > `installer.conf`'s own record > the hardcoded default,
    doc 00 §11.2) using `config`'s own resolution/parsing helpers, but never
    prompts, never creates `install_dir`, never writes `installer.conf`, and
    never touches `state.json`.
    """
    resolved, _source = config_mod._resolve_install_dir(
        known.install_dir, sm, config_mod.DEFAULT_INSTALL_DIR
    )
    values = config_mod._read_conf(resolved / config_mod.CONF_FILENAME)
    return config_mod.Config(
        install_dir=resolved,
        remote_supervision=values.get(
            config_mod.GATE_REMOTE_SUPERVISION,
            config_mod.GATE_DEFAULTS[config_mod.GATE_REMOTE_SUPERVISION],
        ),
        webapp_integration=values.get(
            config_mod.GATE_WEBAPP_INTEGRATION,
            config_mod.GATE_DEFAULTS[config_mod.GATE_WEBAPP_INTEGRATION],
        ),
        values=values,
    )


def _bootstrap_subcommand_context(
    argv: list,
    *,
    state_path: Optional[pathlib.Path] = None,
    requires_root: bool = True,
) -> Context:
    """Bootstrap the subset of `main()`'s startup sequence a standalone
    subcommand needs, without re-entering `_dispatch()` (STEP-3 §6.2).

    **Reused** from `main()`'s own ordering (module docstring, steps
    1/2/4/7/... above), when `requires_root` is `True` (the default -- every
    subcommand registered without a `requires_root=` argument, unit U6):

    - `privilege.require_root()` -- a subcommand still touches docker,
      root-owned package state, and files under the install root (STEP-3
      §3-§4), so it needs the same privilege the install flow does.
    - `config.load(...)` -- the only way to get a real `install_dir` /
      `Config` to build a `Context` from.
    - `logs.open_transcript(...)` -- doc 00 §8.2's auditable-record
      contract applies just as much to a subcommand's docker/clone/compose
      calls as to anything a step does during install.

    **When `requires_root` is `False`** (unit U6's fix: STEP-6's `agent`,
    and STEP-5's `pipeline` in its `--service-exec` mode -- both run as the
    non-root invoking user by design, per their `.service.in` units'
    `User=@USER@`):

    - `privilege.require_root()` is skipped entirely -- calling it would
      exit the process immediately, before the subcommand's own code ever
      runs, which is precisely the defect this fix addresses.
    - `config.load(...)` is replaced by `_read_only_subcommand_config(...)`,
      above -- `config.load()`'s own side effects (creating `install_dir`,
      writing `installer.conf`, writing/`chmod`ing `state.json`) all assume
      root ownership of paths a root install run already created, so a
      non-root process cannot safely perform them (and does not need to --
      it only ever reads back what install already resolved).
    - `logs.open_transcript(...)` is skipped -- `DEFAULT_LOG_DIR`
      (`/var/lib/mv3dt-installer/logs/`) is root-owned, so a non-root
      process cannot create the per-run transcript file there either. This
      loses nothing operationally: `logs.py`'s `log.info/warn/error()`
      always print to stderr regardless of whether a transcript is open
      (`logs.py` `_emit()`), and both non-root units set
      `StandardOutput=journal`/`StandardError=journal`, so systemd already
      captures every line in the journal.

    `onboarding.run_platform_preflight()` is always reused, root or not --
    it is cheap, does not itself require root (only a bare-root-shell
    invoking user is rejected, doc 00 §9.2), and is the source of the
    `InvokingUser` a subcommand's `ctx.run_as_user(...)` calls need either
    way.

    **Deliberately NOT reused** (both are install-flow-specific, per this
    unit's own scope note -- see this function's call site in `main()`):

    - `onboarding.onboard(...)` -- first-run credential capture. A
      subcommand only ever runs after Step 2's onboarding already
      completed; re-entering it would risk a stray prompt in what is
      supposed to be a quick, scriptable re-launch.
    - `reboot.reconcile(...)` -- there is no install-time reboot marker for
      a standalone subcommand to confirm; `state.json`'s `reboot_pending`
      field is a step-dispatch-loop concern this bypass has no business
      touching.

    A small, permissive parser (`parse_known_args`, so it never errors on a
    subcommand's own flags) plucks exactly the framework-owned flags this
    bootstrap itself needs -- `--install-dir`, `--non-interactive`,
    `--log-dir` -- out of `argv` without consuming or otherwise altering
    it: the unmodified `argv` is still exactly what the subcommand handler
    receives. This is what lets a systemd `ExecStart=` line for a later
    step's unit (e.g. an `ingest` invocation baked with `--non-interactive
    --install-dir <install_dir>`, doc 00's `render_ingest_units` example)
    resolve the right `install_dir` for its `Context` while the handler
    itself still sees -- and can independently parse -- those same flags.
    """
    peek = argparse.ArgumentParser(add_help=False)
    peek.add_argument("--install-dir", default=None)
    peek.add_argument("--non-interactive", action="store_true")
    peek.add_argument("--log-dir", default=None)
    known, _unused = peek.parse_known_args(argv)

    if requires_root:
        privilege.require_root()
    user = onboarding.run_platform_preflight()

    sm_path = state_path if state_path is not None else CANONICAL_STATE_PATH
    sm = StateMachine(path=sm_path)

    if requires_root:
        cfg = config_mod.load(
            known.install_dir, sm, non_interactive=known.non_interactive
        )
        log_dir = pathlib.Path(known.log_dir) if known.log_dir else None
        open_transcript(log_dir)
    else:
        cfg = _read_only_subcommand_config(known, sm)

    return build_context(cfg, user, known.non_interactive)


# ---------------------------------------------------------------------------
# doc 00 §3.4 -- opt-in step gates
# ---------------------------------------------------------------------------

# Maps a step's `.order` to the `Config` attribute holding its already-
# resolved installer.conf gate value ("off"/"local"/"remote" for step 6,
# "off"/"on" for step 7 -- doc 00 §3.4's table). Steps 1-5 have no entry
# here and therefore always run.
_GATE_ORDER_TO_CONF_ATTR: dict[int, str] = {
    6: "remote_supervision",
    7: "webapp_integration",
}

# Maps each installer.conf gate key to the argparse destination its
# matching flag parses into. The dests happen to equal the `Config`
# attribute names above, but they are listed separately because they are
# two different contracts: one is argparse's, the other is `config.py`'s.
_GATE_KEY_TO_ARG_DEST: dict[str, str] = {
    config_mod.GATE_REMOTE_SUPERVISION: "remote_supervision",
    config_mod.GATE_WEBAPP_INTEGRATION: "webapp_integration",
}


def _gate_overrides_from_args(args: argparse.Namespace) -> dict[str, str]:
    """Collect the §3.4 gate flags the operator actually passed, keyed by
    their `installer.conf` key, for `config.load(gate_overrides=...)`.

    Flags that were not passed parse to `None` and are omitted entirely
    rather than mapped to `"off"` -- `config.load()` distinguishes "absent"
    (fall through to env var / prompt / default) from "explicitly off"
    (overwrite whatever is persisted), and that distinction only survives
    if it is preserved here.
    """
    overrides: dict[str, str] = {}
    for key, dest in _GATE_KEY_TO_ARG_DEST.items():
        value = getattr(args, dest, None)
        if value is not None:
            overrides[key] = value
    return overrides


def _gate_value_for_step(step: Step, cfg: config_mod.Config) -> Optional[str]:
    """The step's already-resolved gate value, or `None` if it isn't gated
    (steps 1-5 always run -- doc 00 §3.4)."""
    attr = _GATE_ORDER_TO_CONF_ATTR.get(step.order)
    return getattr(cfg, attr) if attr is not None else None


def _gate_is_off(gate_value: Optional[str]) -> bool:
    """Whether a *gated* step (order 6 or 7) should be auto-skipped.

    "off" is the uniform skip-indicator across both gates (doc 00 §3.4's
    table), regardless of how many non-off states a given gate has: Step
    7's `MV3DT_WEBAPP_INTEGRATION` is binary (`off`/`on`), but Step 6's
    `MV3DT_REMOTE_SUPERVISION` is three-valued (`off`/`local`/`remote`).
    Checking `gate_value != "on"` (an earlier, incorrect version of this
    function) would wrongly auto-skip Step 6 whenever it was set to
    `local` or `remote`, since neither equals the string `"on"`. The
    correct rule is symmetric: skip only when the value actually *is*
    `"off"` (or falsy/unset, treated the same way defensively) --
    `"on"`, `"local"`, and `"remote"` all mean "run"."""
    return not gate_value or gate_value == "off"


# ---------------------------------------------------------------------------
# doc 00 §12.2 -- step lifecycle
# ---------------------------------------------------------------------------


def _run_step_lifecycle(step: Step, ctx: Context) -> StepResult:
    """Run `preflight -> run -> verify` (doc 00 §12.2). The *effective*
    result is the first non-`COMPLETE` result across the three, else
    `COMPLETE`. `report()` is intentionally NOT called here -- the dispatch
    loop only calls it once it has confirmed the effective result is
    `COMPLETE` (doc 00 §12.2: "On `COMPLETE` it calls `report()` and
    `state.mark_complete(step.id)`")."""
    for phase in (step.preflight, step.run, step.verify):
        result = phase(ctx)
        if result.status is not StepStatus.COMPLETE:
            return result
    return StepResult(status=StepStatus.COMPLETE)


def _step_title(step_id: str) -> str:
    """Best-effort human title for `step_id`. Looks it up in
    `STEP_REGISTRY` (empty for now -- no step1-7 modules exist yet, doc 00
    scope); falls back to the raw id so a reboot/USER-ACTION block always
    has *something* readable to show even before those modules land."""
    for step in STEP_REGISTRY:
        if step.id == step_id:
            return step.title
    return step_id


# ---------------------------------------------------------------------------
# doc 00 §3.2 -- dispatch loop
# ---------------------------------------------------------------------------


def _dispatch(sm: StateMachine, ctx: Context, cfg: config_mod.Config) -> int:
    """The core state-machine dispatch loop (doc 00 §3.2).

    Iterates `STEP_REGISTRY` (kept sorted by `.order` by `steps.register()`)
    in order. For each step:

    - If its gate (steps 6/7 only, doc 00 §3.4) is off, auto-`COMPLETE` it
      with a one-line log -- "the same skip discipline as a genuinely
      completed step".
    - Else if already `COMPLETE`, skip with the "already complete" log line.
    - Else run its lifecycle (`preflight -> run -> verify`) and record the
      effective `StepResult`. On `COMPLETE`, call `report()` and
      `state.mark_complete()`, then continue to the next step. On anything
      else, halt: `REBOOT_REQUIRED` writes the reboot marker and prints the
      reboot USER-ACTION block (exit 0); `USER_ACTION_REQUIRED` prints the
      manual-action block (exit 0); `FAILED` prints the failure (exit
      non-zero).

    `STEP_REGISTRY` is currently empty (no step1-7 modules exist -- that is
    separate, out-of-scope work), so in practice this loop iterates zero
    times today and falls straight through to the "all complete" banner --
    but the loop itself is implemented generically and correctly regardless.
    """
    for step in STEP_REGISTRY:
        gate_value = _gate_value_for_step(step, cfg)
        if gate_value is not None and _gate_is_off(gate_value):
            if sm.status(step.id) is not StepStatus.COMPLETE:
                sm.mark_complete(step.id)
            log.info(f"{step.id}: skipped (gate off)")
            continue

        if sm.status(step.id) is StepStatus.COMPLETE:
            log.info(f"{step.id}: already complete")
            continue

        result = _run_step_lifecycle(step, ctx)

        if result.status is StepStatus.COMPLETE:
            step.report(ctx)
            sm.mark_complete(step.id)
            continue

        if result.status is StepStatus.REBOOT_REQUIRED:
            sm.set_reboot_pending(step.id, boot_id=reboot_mod.current_boot_id())
            privilege.show_reboot_required(step.title)
            return 0

        if result.status is StepStatus.USER_ACTION_REQUIRED:
            why = result.message or "manual action required"
            privilege.show_user_action_block(step.title, why, result.user_actions)
            return 0

        if result.status is StepStatus.FAILED:
            log.error(f"{step.id} failed: {result.message}")
            return 1

    if sm.all_complete():
        log.info("all steps complete")
    return 0


# ---------------------------------------------------------------------------
# doc 00 §3.3 -- --status / --reset-state / --reset-step
# ---------------------------------------------------------------------------


def _print_status(sm: StateMachine) -> int:
    """`--status`: print the state table and exit (doc 00 §3.3). Reads
    directly from `state.json` via the `StateMachine` -- a missing/corrupt
    file yields the empty default (`state.py`'s forgiving-reader contract),
    so this never crashes on a fresh install."""
    state = sm.load()
    print(f"install_dir: {state.install_dir}")
    for step_id in STEP_IDS:
        entry = state.steps.get(step_id)
        status_value = (
            entry.status.value if entry is not None else StepStatus.PENDING.value
        )
        print(f"{step_id}: {status_value}")
    print(f"all complete: {sm.all_complete()}")
    return 0


def _reset_state(sm: StateMachine) -> None:
    """`--reset-state`: wipe `state.json` and start fresh (doc 00 §3.3,
    mirrors `00_bootstrap.sh --reset-state`'s `reset_state()`, which
    `rm -f`s the state file). Applied "before any phase runs" -- same
    discipline as the bash precedent -- then execution falls through to the
    normal dispatch flow with a freshly (re)initialized state.

    Preserves the previously-recorded `install_dir` across the reset.
    `install_dir` lives inside `state.json` (doc 00 §6.2), so naively
    reinitializing to `default_state()` would silently reset it to the
    hardcoded `/opt/mv3dt` -- and because `state.json` then *exists* again
    with that value, `config.py`'s precedence chain (`--install-dir override
    > state.json > ...`, §11.2) would pick it up ahead of the operator's
    real, previously-chosen install location on the very next run that
    doesn't re-pass `--install-dir`. `--reset-state` is meant to clear step
    completion, not relocate a live install.
    """
    previous_install_dir = sm.load().install_dir
    with contextlib.suppress(FileNotFoundError):
        sm.path.unlink()
    fresh = default_state()
    fresh.install_dir = previous_install_dir
    sm.save(fresh)
    log.info(
        f"state reset: {sm.path} wiped and reinitialized "
        f"(install_dir preserved: {previous_install_dir})"
    )


def _reset_step(sm: StateMachine, order: int) -> int:
    """`--reset-step N`: clear one step's completion by `.order` so it
    re-runs (doc 00 §3.3). Implemented as a standalone command (like
    `--status`) rather than falling through to dispatch: a `--reset-step`
    invocation is about inspecting/adjusting state, not immediately
    re-running the step in the same process, so it prints its result and
    exits cleanly either way.

    Since `STEP_REGISTRY` is currently empty (no step1-7 modules exist
    yet), `N` will never match anything today -- that is expected, not an
    error: prints a clear message and returns 0 without crashing.
    """
    for step in STEP_REGISTRY:
        if step.order == order:
            sm.set_status(step.id, StepStatus.PENDING)
            log.info(f"{step.id}: cleared (order {order}); it will re-run")
            return 0
    log.warn(f"no step registered with order {order}")
    return 0


def _seed_camera_inventory() -> tuple[str, list[str]]:
    """Read the bundled `assets/cameras/cameras.yml` (doc 00 §4.1/§15.5)
    for its header block and its cameras' IPs, used only on a genuinely
    first scan (no `<install_dir>/cameras.yml` yet)."""
    seed_path = shellout.asset_path("cameras", "cameras.yml")
    try:
        text = seed_path.read_text(encoding="utf-8")
    except OSError:
        return "", []
    header = text.split("\n\ncameras:", 1)[0]
    prime_ips = [cam.ip for cam in cameras_mod.parse_inventory(text) if cam.ip]
    return header, prime_ips


def _run_scan_cameras(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    """`--scan-cameras` (doc 00 §3.3/§15.5): a standalone mode shaped like
    `--status`, but run after `require_root()` since ARP scanning needs
    raw sockets. Discovers, probes, binds, writes both artifacts, prints
    the resulting table, and exits -- it never enters the dispatch loop.
    """
    if args.camera_scan_cidr:
        config_mod.persist_value(
            cfg.install_dir, config_mod.CAMERA_SCAN_CIDR_KEY, args.camera_scan_cidr
        )
    if args.camera_scan_iface:
        config_mod.persist_value(
            cfg.install_dir, config_mod.CAMERA_SCAN_IFACE_KEY, args.camera_scan_iface
        )

    cidr = (
        args.camera_scan_cidr
        or cfg.values.get(config_mod.CAMERA_SCAN_CIDR_KEY)
        or cameras_mod.DEFAULT_SCAN_CIDR
    )
    iface = args.camera_scan_iface or cfg.values.get(config_mod.CAMERA_SCAN_IFACE_KEY)
    interfaces = [iface] if iface else None

    seed_header, prime_ips = _seed_camera_inventory()

    result = cameras_mod.refresh(
        cfg.install_dir,
        seed_header=seed_header,
        cam_user=cfg.values.get("CAM_USER", ""),
        cam_password=cfg.values.get("CAM_PASSWORD", ""),
        cidr=cidr,
        interfaces=interfaces,
        prime_ips=prime_ips,
        non_interactive=args.non_interactive,
    )

    # doc 00 §15.5: the pointer bundled bash and later steps read.
    config_mod.persist_value(
        cfg.install_dir,
        config_mod.CAMERAS_FILE_KEY,
        str(cfg.install_dir / "cameras.yml"),
    )

    for cam in result.cameras:
        stream = "unknown" if cam.stream_ok is None else ("ok" if cam.stream_ok else "FAILED")
        log.info(
            f"{cam.id}: {cam.mac or '(no mac)'} {cam.ip} {cam.position or '(unlabeled)'} "
            f"stream={stream} enabled={cam.enabled}"
        )
    if result.unmatched:
        log.info(f"unmatched hosts (non-camera OUI): {', '.join(result.unmatched)}")

    return 0


# ---------------------------------------------------------------------------
# doc 00 §3.2 -- entrypoint
# ---------------------------------------------------------------------------


def main(
    argv: Optional[list[str]] = None,
    *,
    state_path: Optional[pathlib.Path] = None,
) -> int:
    """`app.main()` -- see this module's docstring for the full ordering
    resolution. Returns the process exit code (`__main__.py` passes this
    straight to `sys.exit`).

    `state_path` is a test-only override for the fixed canonical state path
    (`state.CANONICAL_STATE_PATH`, doc 00 §6.1 -- `/var/lib/mv3dt-installer/
    state.json`, independent of `--install-dir`). There is deliberately no
    CLI flag for it: production callers (`__main__.py`) never pass it, so it
    defaults to the canonical location; tests inject a `tmp_path`-derived
    file instead, so no test ever touches the real `/var/lib` path.

    `--status` deliberately runs BEFORE `privilege.require_root()`: doc 00
    §9.1 requires root for the real install flow (apt transactions,
    `/etc/profile.d`, `/var/lib` writes), but `state.json` itself is
    world-readable (`chmod 0644`, doc 00 §6.1) and `--status` performs no
    writes, so an operator (or an unattended health check) can inspect
    installer progress without `sudo`. Every other flag runs after the root
    check, per doc 00 §9.1.

    A registered subcommand (STEP-3 §6.2 -- `amc`, and later `ingest`/
    `pipeline`/`agent`) is an even earlier exception than `--status`: it is
    peeked for and dispatched before `parse_args()` even runs, since the
    framework's own `argparse.ArgumentParser` (`build_parser()`) knows
    nothing about subcommand names or their flags and would reject them.
    """
    effective_argv = argv if argv is not None else sys.argv[1:]
    if effective_argv and effective_argv[0] in SUBCOMMAND_REGISTRY:
        name, rest = effective_argv[0], effective_argv[1:]
        registration = SUBCOMMAND_REGISTRY[name]
        needs_root = (
            registration.requires_root(rest)
            if callable(registration.requires_root)
            else registration.requires_root
        )
        ctx = _bootstrap_subcommand_context(
            rest, state_path=state_path, requires_root=needs_root
        )
        return registration.handler(rest, ctx)

    args = parse_args(argv)

    sm_path = state_path if state_path is not None else CANONICAL_STATE_PATH
    sm = StateMachine(path=sm_path)

    if args.status:
        return _print_status(sm)

    privilege.require_root()
    user = onboarding.run_platform_preflight()

    # State management flags, applied before any phase runs (doc 00 §3.3;
    # mirrors 00_bootstrap.sh's RESET_STATE_FLAG / FORCE_MODELS handling,
    # which runs at the top of its main() dispatcher before any phase).
    if args.reset_state:
        _reset_state(sm)

    if args.reset_step is not None:
        return _reset_step(sm, args.reset_step)

    # Ordering resolution (see module docstring): StateMachine already
    # exists at this point, so config.load() gets a real one to check
    # state.json's install_dir precedence tier against (doc 00 §11.2).
    cfg = config_mod.load(
        args.install_dir,
        sm,
        non_interactive=args.non_interactive,
        gate_overrides=_gate_overrides_from_args(args),
    )

    log_dir = pathlib.Path(args.log_dir) if args.log_dir else None
    open_transcript(log_dir)

    if args.scan_cameras:
        return _run_scan_cameras(cfg, args)

    # doc 00 §3.2 step 9: after the transcript opens, so every prompt and
    # its redacted outcome is part of the auditable record. No-op on every
    # launch after the first (doc 00 §5.2).
    onboarding.onboard(
        cfg.install_dir, cfg.webapp_integration, non_interactive=args.non_interactive
    )

    reconcile_result = reboot_mod.reconcile(sm)
    if reconcile_result is reboot_mod.ReconcileResult.STILL_PENDING:
        pending = sm.load().reboot_pending
        title = _step_title(pending.requested_by) if pending is not None else "installer"
        privilege.show_reboot_required(title)
        return 0

    ctx = build_context(cfg, user, args.non_interactive)
    return _dispatch(sm, ctx, cfg)
