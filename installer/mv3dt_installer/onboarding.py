"""First-run credential capture and platform preflight orchestration for
mv3dt-installer (doc 00 §3.2, §5.2).

Everything installer/bootstrap/bootstrap.sh used to do before handing off to
a locally built binary now happens here, inside the exe: platform /
invoking-user preflight (delegating to preflight.py), NGC API key capture
(ngc.py), and -- only when its gate is "on" -- web-app credential capture
(webapp.py). This module owns none of the underlying storage/prompt logic
itself; it only decides WHEN each of those modules' capture functions
should run, per doc 00 §5.2's rule: "have we already asked?" is the
existence of the secret file, not the result of parsing its contents.

Like ngc.py and webapp.py, this module takes already-resolved plain values
(`install_dir`, `webapp_gate`) rather than importing config.py -- app.py,
which has config.py available, is the only caller and is responsible for
resolving those first (doc 00 §3.2 step 7 happens before step 9).

Public API:
    run_platform_preflight() -> privilege.InvokingUser
        -- delegates to preflight.py; replaces the bare privilege.resolve()
        call in app.main() (doc 00 §3.2 step 5).
    ensure_ngc_key(install_dir, *, non_interactive) -> None
        -- captures and stores the NGC key on first run only. Honors a
        pre-set NGC_API_KEY environment variable before ever prompting
        (doc 00 §9.1's `sudo -E` path).
    ensure_webapp_credentials(install_dir, gate_value, *, non_interactive) -> None
        -- captures and stores the web-app credential on first run only,
        and only when gate_value == "on".
    onboard(install_dir, webapp_gate, *, non_interactive) -> None
        -- runs both of the above (doc 00 §3.2 step 9).

Onboarding must run after `logs.open_transcript()` (doc 00 §3.2 step 8, this
module's step 9), so every prompt and its redacted outcome is part of the
auditable transcript -- this module never opens the transcript itself.
"""

from __future__ import annotations

import os
import pathlib
from typing import Union

from . import ngc, preflight, privilege, webapp
from .logs import log

__all__ = [
    "run_platform_preflight",
    "ensure_ngc_key",
    "ensure_webapp_credentials",
    "onboard",
]

StrPath = Union[str, "os.PathLike[str]"]

# The env var doc 00 §9.1 says `sudo -E` is for: a pre-set NGC key, honored
# before ever prompting.
_NGC_ENV_VAR = "NGC_API_KEY"

_NGC_SECRET_RELATIVE_PATH = pathlib.Path("secrets") / "ngc.env"
_WEBAPP_SECRET_RELATIVE_PATH = pathlib.Path("secrets") / "webapp.env"

# The only §3.4 gate value that turns webapp credential capture on.
_WEBAPP_GATE_ON = "on"


def run_platform_preflight() -> privilege.InvokingUser:
    """doc 00 §3.2 step 5: platform + invoking-user gates, replacing the
    bare `privilege.resolve()` call app.main() used before preflight.py
    existed."""
    preflight.require_supported_platform()
    return preflight.require_invoking_user()


def ensure_ngc_key(install_dir: StrPath, *, non_interactive: bool) -> None:
    """doc 00 §10.2's caller: captures and stores the NGC key exactly once.

    "Have we already asked?" is the existence of `secrets/ngc.env`, not
    `ngc.load_key()`'s result -- the two agree today since the key is
    required (a stored file always holds a real key), but checking
    existence directly is the rule that stays correct regardless of how
    `ngc.py`'s storage format evolves.

    A pre-set `NGC_API_KEY` in the environment (doc 00 §9.1's `sudo -E`
    path) is honored before `ngc.capture_key()` is ever called, so a
    `--non-interactive` first run with the variable exported still
    succeeds instead of dying.
    """
    secrets_path = pathlib.Path(install_dir) / _NGC_SECRET_RELATIVE_PATH
    if secrets_path.is_file():
        return

    env_key = os.environ.get(_NGC_ENV_VAR)
    if env_key:
        log.info(f"Using {_NGC_ENV_VAR}=<redacted> from the environment.")
        ngc.store_key(env_key, install_dir)
        return

    key = ngc.capture_key(non_interactive)
    ngc.store_key(key, install_dir)


def ensure_webapp_credentials(
    install_dir: StrPath, gate_value: str, *, non_interactive: bool
) -> None:
    """doc 00 §14.3's caller: captures and stores the web-app credential
    exactly once, and only when the §3.4 gate is `"on"`. `"off"` skips this
    entirely -- there is nothing to ask for.

    Same "file existence, not parsed content" rule as `ensure_ngc_key()`:
    once `secrets/webapp.env` exists, this is a no-op on every later
    launch. `webapp.capture_credentials()`'s own "a value was found, keep
    it?" prompt is for a caller re-entering credentials on purpose (e.g. a
    future reconfigure flag); onboarding never re-triggers it once the file
    exists, which is what keeps onboarding silent after the first run.
    """
    if gate_value != _WEBAPP_GATE_ON:
        return

    secrets_path = pathlib.Path(install_dir) / _WEBAPP_SECRET_RELATIVE_PATH
    if secrets_path.is_file():
        return

    creds = webapp.capture_credentials(non_interactive, install_dir=install_dir)
    if creds.api_key and creds.endpoint:
        webapp.store_credentials(creds, install_dir)
    else:
        log.warn(
            "Web-app credentials incomplete; nothing stored. "
            "Step 7 will surface a USER-ACTION block on first use."
        )


def onboard(install_dir: StrPath, webapp_gate: str, *, non_interactive: bool) -> None:
    """doc 00 §3.2 step 9: run both capture flows against an already
    resolved `install_dir` and web-app gate value. Silent on every launch
    after the first (doc 00 §5.2's closing paragraph)."""
    ensure_ngc_key(install_dir, non_interactive=non_interactive)
    ensure_webapp_credentials(install_dir, webapp_gate, non_interactive=non_interactive)
