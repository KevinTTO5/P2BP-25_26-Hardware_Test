"""Bundled-asset locator + subprocess runner.

Implements doc `installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md` §4.2:
PyInstaller `--onefile` unpacks bundled data files into a temp dir exposed
as `sys._MEIPASS` at runtime ("frozen" mode). When running from a plain
source checkout ("dev mode") there is no `sys._MEIPASS`, so assets are
resolved relative to this file's own directory instead.

Framework-only module: no step1-7 business logic lives here, and this
module deliberately does NOT import `privilege.py` (§9) — that module does
not exist yet (it lands in a later wave). `run_bundled_script` instead
accepts an already-prepared `env` mapping as a plain parameter; the caller
(eventually `app.py`, once `privilege.py` exists) is responsible for
building that environment and passing it in.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile


def asset_path(*parts: str) -> pathlib.Path:
    """Resolve a path under the bundled ``assets/`` directory.

    Frozen mode (running as a PyInstaller ``--onefile`` binary):
    ``sys._MEIPASS`` points at the unpacked temp dir for this run, and
    assets live under ``<_MEIPASS>/assets/...`` per the ``datas`` mapping
    in ``installer.spec`` (§4.1).

    Dev mode (running from a source checkout, no ``sys._MEIPASS``):
    assets live under ``assets/`` next to this module.

    Note: the doc's own code block spells the dev-mode line as
    ``pathlib.Path(__file__).parent / "assets" / *parts``, which is not
    valid Python (``/`` cannot be followed by an unpacked ``*parts``).
    This implements the clearly-intended equivalent using ``.joinpath()``.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:  # frozen binary
        return pathlib.Path(base, "assets", *parts)
    return pathlib.Path(__file__).parent.joinpath("assets", *parts)  # dev mode


def run_bundled_script(
    *asset_parts: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Copy a bundled bash fragment out to a temp dir and execute it.

    Per §4.2: never execute directly out of ``sys._MEIPASS`` if the
    fragment writes next to itself — copy it out to a fresh, run-scoped
    temp dir first, ``chmod +x`` it there, then run it.

    ``env`` is an already-prepared environment mapping (e.g. produced by
    the not-yet-built ``privilege.py``, §9). This function accepts it as
    a plain parameter and does not construct or import any privilege
    handling itself — that stays decoupled so this module has no
    dependency on `privilege.py`.

    Known non-goal: cleanup of the temp directory created for the copy is
    NOT performed here. The spec does not require it, and leaving the
    directory in place aids post-run debugging of the executed fragment.
    """
    source = asset_path(*asset_parts)
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="mv3dt-shellout-"))
    copied_path = temp_dir / source.name
    copied_path.write_bytes(source.read_bytes())
    copied_path.chmod(0o755)

    return subprocess.run(
        [str(copied_path), *(args or [])],
        env=env,
        capture_output=True,
        text=True,
    )
