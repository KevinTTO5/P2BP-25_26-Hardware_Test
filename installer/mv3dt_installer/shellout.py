"""Bundled-asset locator + subprocess runner.

Implements doc `installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md` §4.2:
PyInstaller `--onefile` unpacks bundled data files into a temp dir exposed
as `sys._MEIPASS` at runtime ("frozen" mode). When running from a plain
source checkout ("dev mode") there is no `sys._MEIPASS`, so assets are
resolved relative to this file's own directory instead.

Framework-only module: no step1-7 business logic lives here, and this
module deliberately does NOT import `privilege.py` (§9). `run_bundled_script`
instead accepts an already-prepared `env` mapping as a plain parameter; the
caller is responsible for building that environment and passing it in.

Public API:
    asset_path(*parts)                 -- locate a bundled asset.
    stage_assets(*parts, prefix=...)   -- copy a bundled asset *directory*
        out to a fresh staging dir, preserving its layout.
    run_bundled_script(*asset_parts, args=, env=, tree=, inherit_env=,
                       cleanup=)       -- stage and execute a fragment.

Three properties this module owes its callers:

1. **Tree staging.** A bundled bash fragment that does
   `source "$SCRIPT_DIR/lib/common.sh"` only works if the whole directory
   lands together, so `stage_assets` copies a directory rather than a single
   file and `run_bundled_script(..., tree=(...))` runs the fragment from
   inside that staged tree.
2. **Environment merging.** `env` is an *overlay* on `os.environ` by default
   (`inherit_env=True`). Replacing the environment outright strips `PATH`,
   which breaks every `command -v` in the staged bash; pass
   `inherit_env=False` for the hermetic replace semantics.
3. **Transcript capture (§8.2).** The command line, any explicit environment
   overrides, and both output streams of every shelled-out fragment go to
   `logs.log` -- stdout at info level, stderr at warn level -- with
   `_REDACT_KEYS` applied so no secret reaches the transcript.
"""

from __future__ import annotations

import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from .logs import log

# Default prefix for the run-scoped staging directory created by
# stage_assets() / run_bundled_script().
STAGE_PREFIX = "mv3dt-shellout-"

# Environment variable pointing the staged bash at the root of its own
# staged tree, so a fragment never has to guess where it was copied to.
# The installer-side `lib/common.sh` reads this as `${MV3DT_ASSET_ROOT:?}`
# in place of the repo-relative `repo_root()` the laptop/ scripts use.
ASSET_ROOT_ENV = "MV3DT_ASSET_ROOT"

# Environment keys whose values must never reach the transcript (§8.2).
# Longest-first alternation below so `NGC_API_KEY` is matched as a whole and
# not as a bare `API_KEY` suffix.
_REDACT_KEYS = ("NGC_API_KEY", "API_KEY", "CAM_PASSWORD", "MQTT_PASSWORD")
_REDACTED = "<redacted>"
_REDACT_RE = re.compile(
    r"(?<![A-Za-z0-9_])("
    + "|".join(sorted(_REDACT_KEYS, key=len, reverse=True))
    + r")=\S*"
)

# Modes applied to a staged tree: executable for shell fragments, plain
# read-only-ish for everything else, traversable for directories.
_SCRIPT_MODE = 0o755
_DATA_MODE = 0o644
_DIR_MODE = 0o755


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


def _redact(text: str) -> str:
    """Blank the value of any ``KEY=value`` occurrence for a redacted key."""
    return _REDACT_RE.sub(lambda m: f"{m.group(1)}={_REDACTED}", text)


def _env_dump(env: dict[str, str]) -> str:
    """Render an environment overlay as a single redacted, sorted line."""
    rendered = " ".join(
        f"{key}={_REDACTED}" if key in _REDACT_KEYS else f"{key}={shlex.quote(value)}"
        for key, value in sorted(env.items())
    )
    # Belt and braces: a key not in _REDACT_KEYS may still carry a secret in
    # `KEY=value` form inside its own value (e.g. a composed command line).
    return _redact(rendered)


def _apply_tree_modes(root: pathlib.Path) -> None:
    """``chmod`` a freshly staged tree: 0755 for ``*.sh``, 0644 otherwise.

    Bundled asset modes are not reliably preserved through PyInstaller's
    ``datas`` packing, so the staged copy is given its modes explicitly
    rather than inherited from whatever landed in ``sys._MEIPASS``.
    """
    root.chmod(_DIR_MODE)
    for entry in sorted(root.rglob("*")):
        if entry.is_dir():
            entry.chmod(_DIR_MODE)
        elif entry.suffix == ".sh":
            entry.chmod(_SCRIPT_MODE)
        else:
            entry.chmod(_DATA_MODE)


def stage_assets(*parts: str, prefix: str = STAGE_PREFIX) -> pathlib.Path:
    """Copy a bundled asset *directory* out to a fresh staging directory.

    Per §4.2 a fragment is never executed straight out of ``sys._MEIPASS``.
    For a fragment that ``source``s a sibling (``lib/common.sh``) or reads a
    neighbouring data file, copying the single script is not enough -- the
    whole directory has to land together, keeping its internal layout.

    ``parts`` names the directory relative to the assets root, exactly as
    ``asset_path`` takes it. The tree is re-created *at the same relative
    path* under the returned staging directory, so relative navigation
    between asset subtrees (``../mosquitto/mv3dt.conf``) resolves the same
    way it does inside the bundle. With no ``parts`` the entire ``assets/``
    tree is staged.

    Returns the staging directory itself -- the thing to hand to
    ``shutil.rmtree`` when done. The requested tree is at
    ``<returned>/<parts...>``.
    """
    source = asset_path(*parts)
    if not source.is_dir():
        raise NotADirectoryError(f"bundled asset tree not found: {source}")

    stage_root = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    destination = stage_root.joinpath(*parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # With no `parts` the destination *is* the freshly-made staging dir, so
    # copytree has to merge into it rather than refuse an existing target.
    shutil.copytree(source, destination, dirs_exist_ok=not parts)
    _apply_tree_modes(destination)
    return stage_root


def _stage_single_file(*asset_parts: str, prefix: str) -> tuple[pathlib.Path, pathlib.Path]:
    """Copy one bundled fragment out to a fresh staging dir and chmod it.

    Backward-compatible path for ``run_bundled_script`` calls that pass no
    ``tree``: the fragment stands alone and needs nothing beside it.
    """
    source = asset_path(*asset_parts)
    payload = source.read_bytes()  # read first, so a missing asset leaks no tempdir
    stage_root = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    script = stage_root / source.name
    script.write_bytes(payload)
    script.chmod(_SCRIPT_MODE)
    return stage_root, script


def _log_stream(emit, name: str, text: str) -> None:
    """Append a captured stream to the transcript, one redacted line each."""
    for line in text.splitlines():
        emit(f"[{name}] {_redact(line)}")


def run_bundled_script(
    *asset_parts: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    tree: tuple[str, ...] | None = None,
    inherit_env: bool = True,
    cleanup: bool = True,
    prefix: str = STAGE_PREFIX,
) -> subprocess.CompletedProcess:
    """Stage a bundled bash fragment out to a temp dir and execute it.

    ``asset_parts`` names the fragment relative to the assets root.

    ``tree`` names an ancestor directory of the fragment, also relative to
    the assets root; when given, that whole directory is staged with
    ``stage_assets`` and the fragment runs from inside the staged copy with
    ``cwd`` set to it and ``MV3DT_ASSET_ROOT`` exported to its absolute
    path. ``asset_parts`` must start with ``tree``. Use ``tree=()`` to stage
    the entire ``assets/`` tree; ``tree=None`` (the default) keeps the
    original single-file copy behaviour.

    ``env`` is an already-prepared environment mapping (e.g. produced by
    ``privilege.py``, §9). By default (``inherit_env=True``) it is merged
    *over* ``os.environ`` rather than replacing it -- replacing outright
    strips ``PATH`` and breaks every ``command -v`` in the staged bash.
    ``inherit_env=False`` gives the hermetic replace semantics, in which
    case ``env`` (plus ``MV3DT_ASSET_ROOT`` when a tree is staged) is the
    complete child environment.

    ``cleanup=True`` (the default) removes the staging directory in a
    ``finally``. Pass ``cleanup=False`` to leave it in place for post-run
    debugging of the executed fragment.

    The command line, the explicit environment overrides, and both captured
    streams are written to the transcript per §8.2, with ``_REDACT_KEYS``
    redacted throughout. The ``CompletedProcess`` -- including the
    unredacted ``stdout``/``stderr`` the caller may need to parse -- is
    returned unchanged.
    """
    if tree is not None:
        tree_parts = tuple(tree)
        if asset_parts[: len(tree_parts)] != tree_parts:
            raise ValueError(
                f"asset_parts {asset_parts!r} is not inside tree {tree_parts!r}"
            )
        relative_parts = asset_parts[len(tree_parts) :]
        if not relative_parts:
            raise ValueError(
                "asset_parts must name a fragment inside tree, not the tree itself"
            )
        stage_root = stage_assets(*tree_parts, prefix=prefix)
        tree_root = stage_root.joinpath(*tree_parts)
        script = tree_root.joinpath(*relative_parts)
        cwd: str | None = str(tree_root)
    else:
        stage_root, script = _stage_single_file(*asset_parts, prefix=prefix)
        tree_root = None
        cwd = None

    child_env: dict[str, str] = dict(os.environ) if inherit_env else {}
    if tree_root is not None:
        child_env[ASSET_ROOT_ENV] = str(tree_root)
    if env:
        child_env.update(env)

    command = [str(script), *(args or [])]

    try:
        log.info(f"shellout: {_redact(shlex.join(command))}")
        if env:
            log.info(f"shellout env: {_env_dump(env)}")
        result = subprocess.run(
            command,
            env=child_env,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        _log_stream(log.info, script.name, result.stdout)
        _log_stream(log.warn, script.name, result.stderr)
        log.info(f"shellout: {script.name} exited {result.returncode}")
        return result
    finally:
        if cleanup:
            shutil.rmtree(stage_root, ignore_errors=True)
