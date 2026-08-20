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
   `logs.log` -- stdout at info level, stderr at warn level.

Exactly what redaction guarantees, stated precisely because the transcript
is an audited artifact:

- **By value.** Every value the child environment carries under a
  `_REDACT_KEYS` name is scrubbed wherever it appears in the command line,
  the environment dump, or either output stream, prefix or no prefix. This
  is what covers `set -x` tracing, `curl -H "Authorization: Bearer
  $NGC_API_KEY"`, and a tool echoing an argument back in an error message.
  Value scrubbing is exact-substring, so a fragment that transforms a secret
  before printing it (base64, URL-encoding, splitting it across lines)
  defeats it.
- **By key.** Any `KEY=` occurrence for a `_REDACT_KEYS` name additionally
  blanks the rest of that line, which catches a secret this process never
  held -- one the fragment read from a file or generated itself.
- **Not covered.** A secret under a key not in `_REDACT_KEYS`, and an empty
  or whitespace-only value (scrubbing those would match everywhere).
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
from typing import Mapping, Sequence

from .logs import log

# Default prefix for the run-scoped staging directory created by
# stage_assets() / run_bundled_script().
STAGE_PREFIX = "mv3dt-shellout-"

# Environment variable exported by run_bundled_script() when it stages a
# tree. Its meaning is invariant: it is always the root of the staged assets
# tree -- the staged stand-in for `assets/` itself -- no matter which subtree
# was staged. A fragment therefore addresses asset subtrees as
# `$MV3DT_ASSET_ROOT/<subtree>/...` in every mode, and locates its own
# directory from `$0` rather than from this variable.
ASSET_ROOT_ENV = "MV3DT_ASSET_ROOT"

# Environment keys whose values must never reach the transcript (§8.2).
# Longest-first alternation below so `NGC_API_KEY` is matched as a whole and
# not as a bare `API_KEY` suffix.
_REDACT_KEYS = ("NGC_API_KEY", "API_KEY", "CAM_PASSWORD", "MQTT_PASSWORD")
_REDACTED = "<redacted>"

# `KEY=` blanks the remainder of the line, not just the next whitespace-free
# run: a password may legitimately contain spaces, and `\S*` would have
# logged `MQTT_PASSWORD=hunter 2 three` as `MQTT_PASSWORD=<redacted> 2 three`.
# Once a secret key is named on a line, no part of that line's tail can be
# assumed safe. Callers wanting per-argument granularity redact each argument
# separately before joining, which _command_dump() below does.
_REDACT_RE = re.compile(
    r"(?<![A-Za-z0-9_])("
    + "|".join(sorted(_REDACT_KEYS, key=len, reverse=True))
    + r")=[^\n]*"
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


# ---------------------------------------------------------------------------
# Redaction (§8.2)
# ---------------------------------------------------------------------------


def _redact(text: str) -> str:
    """Blank a ``KEY=...`` occurrence, and its line's tail, for a secret key."""
    return _REDACT_RE.sub(lambda m: f"{m.group(1)}={_REDACTED}", text)


def _secret_values(env: Mapping[str, str]) -> tuple[str, ...]:
    """Collect the plaintext this process is handing the fragment.

    Longest first, so a value that contains a shorter one is replaced whole
    rather than shredded around it. Empty and whitespace-only values are
    excluded: substituting those would match at every position and destroy
    the transcript without protecting anything.
    """
    values = set()
    for key in _REDACT_KEYS:
        value = env.get(key, "")
        if value.strip():
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


def _scrub(text: str, secrets: Sequence[str]) -> str:
    """Apply value-based scrubbing, then key-based, to one line of text."""
    for secret in secrets:
        text = text.replace(secret, _REDACTED)
    return _redact(text)


def _command_dump(command: Sequence[str], secrets: Sequence[str]) -> str:
    """Render an argv for the transcript, redacting each argument alone.

    Redacting per argument rather than over the joined string keeps a
    ``KEY=`` argument from swallowing every argument that follows it.
    """
    return " ".join(_scrub(shlex.quote(part), secrets) for part in command)


def _env_dump(env: Mapping[str, str], secrets: Sequence[str]) -> str:
    """Render an environment overlay as a single sorted, redacted line.

    Each entry is redacted on its own for the same reason as ``_command_dump``:
    a secret entry must not blank the entries sorted after it.
    """
    entries = []
    for key, value in sorted(env.items()):
        if key in _REDACT_KEYS:
            entries.append(f"{key}={_REDACTED}")
        else:
            entries.append(_scrub(f"{key}={shlex.quote(value)}", secrets))
    return " ".join(entries)


def _log_stream(emit, name: str, text: str, secrets: Sequence[str]) -> None:
    """Append a captured stream to the transcript, one redacted line each."""
    for line in text.splitlines():
        emit(f"[{name}] {_scrub(line, secrets)}")


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


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
    ``asset_path`` takes it, and the tree is re-created at that same relative
    path under the returned staging directory. Returns the staging directory
    itself -- the thing to hand to ``shutil.rmtree`` when done -- with the
    requested tree at ``<returned>/<parts...>``.

    **Only the requested subtree is copied.** ``stage_assets("scripts")``
    produces a staging dir containing ``scripts/`` and nothing else, so a
    fragment reaching outside its own directory (``../mosquitto/mv3dt.conf``)
    gets ENOENT. Call ``stage_assets()`` with no parts to stage the whole
    ``assets/`` tree when a fragment needs a sibling subtree; that is the
    only mode in which cross-subtree paths resolve as they do inside the
    bundle.

    This function sets no environment variable. ``MV3DT_ASSET_ROOT`` is
    exported by ``run_bundled_script``, which is where a child process
    exists to receive it.
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


def _stage_single_file(
    *asset_parts: str, prefix: str
) -> tuple[pathlib.Path, pathlib.Path]:
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


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


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
    ``stage_assets`` and the fragment runs from inside the staged copy, with
    ``cwd`` set to the staged copy of ``tree``. ``asset_parts`` must start
    with ``tree``. Use ``tree=()`` to stage the entire ``assets/`` tree --
    the mode a fragment needs if it reads a sibling subtree, since a partial
    stage copies only the subtree named. ``tree=None`` (the default) keeps
    the original single-file copy behaviour.

    ``MV3DT_ASSET_ROOT`` is exported to the child whenever a tree is staged,
    and always means **the root of the staged assets tree**, whichever
    subtree was staged. So ``$MV3DT_ASSET_ROOT/scripts/lib/common.sh`` is the
    correct spelling under both ``tree=("scripts",)`` and ``tree=()``; what
    differs between them is only what else exists beside ``scripts/``. A
    value for it supplied in ``env`` is ignored, with a warning -- the real
    staging path is the only correct answer, and a stale one sends the
    fragment somewhere that no longer exists.

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
    streams are written to the transcript per §8.2. Secrets are scrubbed by
    value and by key with the guarantees and limits spelled out in this
    module's docstring; note that transformed output (a base64-encoded key,
    say) is beyond any scrubber's reach. The ``CompletedProcess`` returned
    to the caller is unredacted, since callers parse it.
    """
    if tree is not None:
        tree_parts = tuple(tree)
        if asset_parts[: len(tree_parts)] != tree_parts:
            raise ValueError(
                f"asset_parts {asset_parts!r} is not inside tree {tree_parts!r}"
            )
        if not asset_parts[len(tree_parts) :]:
            raise ValueError(
                "asset_parts must name a fragment inside tree, not the tree itself"
            )
        stage_root = stage_assets(*tree_parts, prefix=prefix)
        script = stage_root.joinpath(*asset_parts)
        cwd: str | None = str(stage_root.joinpath(*tree_parts))
        asset_root: str | None = str(stage_root)
    else:
        stage_root, script = _stage_single_file(*asset_parts, prefix=prefix)
        cwd = None
        asset_root = None

    child_env: dict[str, str] = dict(os.environ) if inherit_env else {}
    if env:
        child_env.update(env)
    if asset_root is not None:
        # Assigned after the overlay, deliberately: the staged path is a fact
        # about this run, not a preference the caller gets to state.
        supplied = child_env.get(ASSET_ROOT_ENV)
        if supplied is not None and supplied != asset_root:
            log.warn(
                f"shellout: ignoring supplied {ASSET_ROOT_ENV}={supplied}; "
                f"using the staged tree at {asset_root}"
            )
        child_env[ASSET_ROOT_ENV] = asset_root

    command = [str(script), *(args or [])]
    secrets = _secret_values(child_env)

    try:
        log.info(f"shellout: {_command_dump(command, secrets)}")
        if env:
            log.info(f"shellout env: {_env_dump(env, secrets)}")
        result = subprocess.run(
            command,
            env=child_env,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        _log_stream(log.info, script.name, result.stdout, secrets)
        _log_stream(log.warn, script.name, result.stderr, secrets)
        log.info(f"shellout: {script.name} exited {result.returncode}")
        return result
    finally:
        if cleanup:
            shutil.rmtree(stage_root, ignore_errors=True)
