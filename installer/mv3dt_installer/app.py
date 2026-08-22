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

Ordering note (doc 00 §3.2 vs. the actual `config.load()` signature)
----------------------------------------------------------------------
Doc 00 §3.2 lists `app.main()`'s steps as: (1) parse flags, (2) resolve
privilege, (3) load config, (4) open transcript, (5) load state + reconcile
reboot, (6) dispatch. But `config.load()` as actually built (`config.py`,
a sibling wave-3 PR) takes a `state: StateMachine` parameter -- it needs to
check `state.json`'s `install_dir` field for precedence (doc 00 §11.2:
`--install-dir` > `state.json` > `installer.conf` default > `/opt/mv3dt`).
So a `StateMachine` must exist BEFORE step 3, apparently contradicting the
doc's step-5 placement of "loads the state file".

This is resolved cleanly, not accidentally: doc 00 §6.1 says the state file
"stays at the canonical `/var/lib/mv3dt-installer/state.json` path" even
when `--install-dir` moves the install root -- `state.json`'s path is fixed
and entirely independent of `install_dir`. There is therefore no real
ordering conflict, just a narrative simplification in the doc's five-line
summary. This module implements the *intent* precisely:

    1. parse flags
    2. resolve privilege (`privilege.require_root()` + `privilege.resolve()`)
    3. construct/load the `StateMachine` at its fixed canonical path
       (honoring `--reset-state` / `--reset-step` here, before anything
       else touches it)
    4. call `config.load(..., state=<that StateMachine>)`
    5. open the transcript (`logs.open_transcript()`)
    6. call `reboot.reconcile(state)` using that same already-loaded
       `StateMachine`
    7. enter the dispatch loop

`--status` is a deliberate exception to this order -- see `main()`'s
docstring.
"""

from __future__ import annotations

import argparse
import contextlib
import pathlib
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Optional

from mv3dt_installer import __version__, build_stamp
from mv3dt_installer import config as config_mod
from mv3dt_installer import ngc as ngc_mod
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
    `configure_ngc_cli`, expose `manual_fallback` status to the resolved
    `install_dir`)")."""

    install_dir: pathlib.Path

    def load_key(self) -> Optional[str]:
        return ngc_mod.load_key(self.install_dir)

    def configure_ngc_cli(self) -> Optional[pathlib.Path]:
        return ngc_mod.configure_ngc_cli(self.install_dir)

    @property
    def manual_fallback(self) -> bool:
        """Whether the operator chose the manual NGC key fallback (doc 00
        §10.3). `ngc.py` doesn't persist this flag itself -- `KeyState` is
        only the transient result of `capture_key()` at bootstrap time --
        so it's derived here from `load_key()` returning `None`, which is
        exactly the signal doc 00 §10.3 tells step authors to check."""
        return self.load_key() is None


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


def build_context(cfg: config_mod.Config, user: InvokingUser) -> Context:
    """Assemble the `Context` a dispatched step's lifecycle methods receive
    (doc 00 §12.3), bound to the resolved `Config` and invoking user."""
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
    )


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
    """
    args = parse_args(argv)

    sm_path = state_path if state_path is not None else CANONICAL_STATE_PATH
    sm = StateMachine(path=sm_path)

    if args.status:
        return _print_status(sm)

    privilege.require_root()
    user = privilege.resolve()

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

    reconcile_result = reboot_mod.reconcile(sm)
    if reconcile_result is reboot_mod.ReconcileResult.STILL_PENDING:
        pending = sm.load().reboot_pending
        title = _step_title(pending.requested_by) if pending is not None else "installer"
        privilege.show_reboot_required(title)
        return 0

    ctx = build_context(cfg, user)
    return _dispatch(sm, ctx, cfg)
