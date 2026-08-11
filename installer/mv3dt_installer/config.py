"""Install-location config + opt-in step gate persistence (doc 00 §11, §3.4).

Owns `<install_dir>/installer.conf` — a small, plain `KEY=VALUE` file (same
shape as [`laptop/config/laptop.env.example`](../../laptop/config/laptop.env.example):
no quoting, no shell expansion) that mirrors `state.json`'s `install_dir`
(§6.2) for human inspection and for bundled bash fragments
(`set -a; . installer.conf`). This module is framework-only: it has no
knowledge of what any step or later module (`ngc.py`, `webapp.py`, ...) does
with the directory once resolved — it only resolves *where* `install_dir`
is, creates that directory, and reads/writes `installer.conf`.

Precedence resolved by `load()` (doc 00 §11.2, exact order):

    --install-dir  >  state.json's install_dir  >  installer.conf's own
    recorded install dir (edge case: state.json was reset but a previous
    installer.conf survived at the default location)  >  hardcoded default
    `/opt/mv3dt` (DEFAULT_INSTALL_DIR, doc 00 §11.1).

When resolution falls through to the hardcoded default *and* the caller is
interactive (`non_interactive=False`), `load()` prompts once with that
default prefilled (doc 00 §11.1's "TUI prompts ... with /opt/mv3dt
prefilled"); `--non-interactive` uses the default silently.

`installer.conf` also carries the two §3.4 opt-in step-gate keys
(`MV3DT_REMOTE_SUPERVISION`, `MV3DT_WEBAPP_INTEGRATION`), defaulting to
`"off"` when absent so an unattended run never enables long-running services
or outbound connections the operator did not ask for. `load()` parses both
into the returned `Config` so a later module (`app.py`) can gate Steps 6/7
without re-implementing KEY=VALUE parsing.

Only `install_dir` itself is created by this module. The subdirectories
under it (`secrets/`, `bin/`, `deepstream/`, `projects/`, `agent/`,
`webapp/`, `run/`) are each created by their owning module/step (doc 00
§11.2's layout tree) — never here.

Public API:
    CONF_FILENAME       -- "installer.conf".
    GATE_REMOTE_SUPERVISION / GATE_WEBAPP_INTEGRATION -- the §3.4 key names.
    GATE_KEYS            -- both key names, in table order.
    GATE_DEFAULTS         -- {key: "off"} for both.
    Config                -- dataclass: install_dir, remote_supervision,
        webapp_integration, values (full parsed KEY=VALUE dict).
    load(install_dir_override, state, non_interactive, ...) -> Config
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from typing import Callable

from mv3dt_installer.state import DEFAULT_INSTALL_DIR, StateMachine

__all__ = [
    "CONF_FILENAME",
    "GATE_REMOTE_SUPERVISION",
    "GATE_WEBAPP_INTEGRATION",
    "GATE_KEYS",
    "GATE_DEFAULTS",
    "Config",
    "load",
]

# Mirrored file under the install dir (doc 00 §11.2's layout tree).
CONF_FILENAME = "installer.conf"

# §3.4 opt-in step gates. Key names match the installer.conf KEY=VALUE shape
# exactly (bash-fragment-safe, uppercase, no lowercase/mixed variants).
GATE_REMOTE_SUPERVISION = "MV3DT_REMOTE_SUPERVISION"
GATE_WEBAPP_INTEGRATION = "MV3DT_WEBAPP_INTEGRATION"
GATE_KEYS: tuple[str, ...] = (GATE_REMOTE_SUPERVISION, GATE_WEBAPP_INTEGRATION)
GATE_DEFAULTS: dict[str, str] = {
    GATE_REMOTE_SUPERVISION: "off",
    GATE_WEBAPP_INTEGRATION: "off",
}

# installer.conf also records the resolved install_dir under this key
# (INSTALL_DIR=...), purely to support the "installer.conf's own record of a
# previously-chosen dir" precedence tier below -- the one case where
# state.json was reset (or never written) but a prior install's
# installer.conf survived at the default location.
_INSTALL_DIR_KEY = "INSTALL_DIR"


@dataclass
class Config:
    """Resolved install-location config (doc 00 §11) + parsed §3.4 gates."""

    install_dir: pathlib.Path
    remote_supervision: str = GATE_DEFAULTS[GATE_REMOTE_SUPERVISION]
    webapp_integration: str = GATE_DEFAULTS[GATE_WEBAPP_INTEGRATION]
    # Full parsed installer.conf KEY=VALUE map, including the two gate keys
    # and INSTALL_DIR, plus anything else already present in the file (other
    # modules, e.g. ngc.py/webapp.py, may add their own shared vars here
    # later -- config.py must not clobber keys it doesn't own).
    values: dict[str, str] = field(default_factory=dict)

    @property
    def conf_path(self) -> pathlib.Path:
        return self.install_dir / CONF_FILENAME


def _read_conf(path: pathlib.Path) -> dict[str, str]:
    """Parse a plain `KEY=VALUE` file. No quoting/expansion (doc 00 §11.2:
    same shape as laptop.env.example). Missing file -> empty dict; malformed
    lines (no `=`, blank, `#`-comment) are skipped, never fatal."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _write_conf(path: pathlib.Path, values: dict[str, str]) -> None:
    """Write `values` as a plain KEY=VALUE file, sorted for stable diffs."""
    lines = [
        "# installer.conf -- generated by mv3dt-installer (doc 00 SS11.2).",
        "# Plain KEY=VALUE, no quoting/expansion -- same shape as",
        "# laptop/config/laptop.env.example. Safe to `set -a; . installer.conf`.",
        "",
    ]
    lines.extend(f"{key}={values[key]}" for key in sorted(values))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resolve_install_dir(
    override: str | os.PathLike[str] | None,
    state: StateMachine,
    default_install_dir: str | os.PathLike[str],
) -> tuple[pathlib.Path, str]:
    """Precedence chain (doc 00 §11.2), minus the interactive-prompt step
    (handled by the caller once it sees `source == "default"`)."""
    if override:
        return pathlib.Path(override).expanduser(), "override"

    # state.json is the single source of truth once it has actually been
    # written; a not-yet-existing file must not be confused with an
    # explicit choice of the (coincidentally identical) hardcoded default.
    if state.path.exists():
        loaded = state.load()
        if loaded.install_dir:
            return pathlib.Path(loaded.install_dir).expanduser(), "state"

    # Edge case (doc 00 §11.2, "installer.conf default"): state.json was
    # reset/missing but a previous run's installer.conf survived at the
    # default location. Handled simply: only the default location is ever
    # consulted here -- a prior custom --install-dir cannot be recovered
    # without state.json, by construction.
    default_dir = pathlib.Path(default_install_dir).expanduser()
    default_conf = default_dir / CONF_FILENAME
    if default_conf.exists():
        recorded = _read_conf(default_conf).get(_INSTALL_DIR_KEY)
        if recorded:
            return pathlib.Path(recorded).expanduser(), "conf"

    return default_dir, "default"


def _prompt_for_install_dir(
    default: pathlib.Path, prompt: Callable[[str], str]
) -> pathlib.Path:
    """doc 00 §11.1: TUI prompt, `/opt/mv3dt` (or whatever the resolved
    default is) prefilled; an empty answer accepts it."""
    answer = prompt(f"Install directory [{default}]: ").strip()
    return pathlib.Path(answer).expanduser() if answer else default


def _sync_state(state: StateMachine, resolved: pathlib.Path) -> None:
    """Write `resolved` into state.json's install_dir field (doc 00 §11.2)
    whenever it wasn't already recorded there -- i.e. state.json didn't
    exist yet (first run), or it recorded a different path."""
    file_existed = state.path.exists()
    current = state.load()
    if not file_existed or current.install_dir != str(resolved):
        current.install_dir = str(resolved)
        state.save(current)


def load(
    install_dir_override: str | os.PathLike[str] | None,
    state: StateMachine,
    non_interactive: bool = False,
    *,
    default_install_dir: str | os.PathLike[str] = DEFAULT_INSTALL_DIR,
    prompt: Callable[[str], str] = input,
) -> Config:
    """Resolve the install directory, persist it, and return the parsed
    `installer.conf` config (doc 00 §11, §3.4).

    Args:
        install_dir_override: value of the `--install-dir` CLI flag, if any.
        state: the `StateMachine` for `state.json` (§6.3) -- the single
            source of truth for `install_dir` once chosen.
        non_interactive: mirrors `--non-interactive`; suppresses the §11.1
            prompt and silently accepts the resolved default instead.
        default_install_dir: override for the hardcoded `/opt/mv3dt`
            default (doc 00 §11.1). Production callers should never pass
            this; it exists so tests can exercise the "installer.conf
            default" precedence tier without touching the real
            `/opt/mv3dt`.
        prompt: injectable stand-in for `input()`, for testability.

    Side effects: creates `install_dir` if missing, reads-or-writes
    `installer.conf` under it, and -- when a new path was just resolved --
    writes it back into `state.json` via `state.save()`.
    """
    resolved, source = _resolve_install_dir(
        install_dir_override, state, default_install_dir
    )

    if source == "default" and not non_interactive:
        resolved = _prompt_for_install_dir(resolved, prompt)

    resolved.mkdir(parents=True, exist_ok=True)

    conf_path = resolved / CONF_FILENAME
    values = _read_conf(conf_path)
    for key, default_value in GATE_DEFAULTS.items():
        values.setdefault(key, default_value)
    values[_INSTALL_DIR_KEY] = str(resolved)
    _write_conf(conf_path, values)

    _sync_state(state, resolved)

    return Config(
        install_dir=resolved,
        remote_supervision=values[GATE_REMOTE_SUPERVISION],
        webapp_integration=values[GATE_WEBAPP_INTEGRATION],
        values=values,
    )
