"""NGC API key capture and local secure storage for mv3dt-installer (doc 00
§10).

The key is captured once in the bootstrap stage (doc 00 §5.1 step 4) and
consumed later by Step 2 (PeopleNet model fetch, Docker install method) and
any other NGC-gated step, via `load_key()` / `configure_ngc_cli()`. This
module is a self-contained sibling of laptop/ (doc 00 §3.1) and of steps/
(doc 00 §9.3) -- it does not import from either, and adds no step1-7
business logic (that is out of scope here; see doc 00 §10.3 for what a
future step author does with a `None` result from `load_key()`).

Storage contract (doc 00 §10.1):
    Canonical secret file: `<install_dir>/secrets/ngc.env`, owned by the
    invoking user (privilege.resolve()), `chmod 600`, directory `chmod 700`.
    Single line: `NGC_API_KEY=<key>`. The raw key is NEVER written to the
    transcript log -- any log line that would contain it prints the literal
    `NGC_API_KEY=<redacted>` instead.

Public API (doc 00 §10.2):
    KeyState -- small dataclass: `key: str | None`, `manual_fallback: bool`.
    capture_key(non_interactive) -> KeyState -- secret prompt (no echo,
        `getpass.getpass`); blank input (or `--non-interactive`, which never
        prompts at all) is recorded as `manual_fallback = True`.
    store_key(key, install_dir) -> Path -- atomic write of `secrets/ngc.env`.
    load_key(install_dir=DEFAULT_INSTALL_DIR) -> str | None -- read back for
        a step; `None` means the operator chose the manual fallback (doc 00
        §10.3) -- no key was ever stored.
    configure_ngc_cli(install_dir=DEFAULT_INSTALL_DIR) -> Path | None --
        writes `~/.ngc/config` for the invoking user, mirroring Phase 10 of
        00_bootstrap.sh's `NGCINI` heredoc.

`load_key`/`configure_ngc_cli` take an optional `install_dir` (defaulting to
the doc 00 §11.1 default install location, `/opt/mv3dt`) because `config.py`
(the module that will resolve the operator's actual chosen install_dir from
state.json) is a separate, not-yet-implemented PR (doc 00 §11) -- this
keeps the doc-quoted zero-arg call sites (`load_key()`, `configure_ngc_cli()`)
working today while still letting tests (and, later, config.py) point the
lookup elsewhere without touching a real `/opt/mv3dt`.

Design note on `configure_ngc_cli` (doc 00 §10.2 leaves the mechanism to the
implementer -- "via `privilege.run_as_user(...)` or by resolving the
invoking user's home via `privilege.resolve()` ... your call, document
which"): this module resolves the invoking user's home directly via
`privilege.resolve()` and writes there with `os.chown()` back to that user,
rather than shelling out through `run_as_user`. That keeps the write on the
same atomic-write + explicit-chown code path already used for
`secrets/ngc.env`, and is straightforward to unit test by monkeypatching
`privilege.resolve()` -- no subprocess/sudo involved.
"""

from __future__ import annotations

import getpass
import os
import pathlib
import tempfile
from dataclasses import dataclass
from typing import Optional, Union

from . import privilege
from .logs import log

__all__ = [
    "KeyState",
    "DEFAULT_INSTALL_DIR",
    "capture_key",
    "store_key",
    "load_key",
    "configure_ngc_cli",
]

# doc 00 §11.1 default install directory. Only used as the default lookup
# location here -- config.py (doc 00 §11) is the eventual source of truth
# for the operator's actual chosen install_dir.
DEFAULT_INSTALL_DIR = pathlib.Path("/opt/mv3dt")

_SECRET_RELATIVE_PATH = pathlib.Path("secrets") / "ngc.env"
_KEY_PREFIX = "NGC_API_KEY="

# The ONLY string this module ever logs in place of a real key. Never
# interpolate the real key into any string passed to log.*().
_REDACTED_LOG_LINE = "NGC_API_KEY=<redacted>"

StrPath = Union[str, "os.PathLike[str]"]


@dataclass
class KeyState:
    """Result of `capture_key()` (doc 00 §10.2).

    `key` is the raw captured value, or `None` if the operator left the
    prompt blank (or the capture ran `--non-interactive`, which never
    prompts). `manual_fallback` is `True` in exactly that case -- doc 00
    §10.3 governs what a step does when it later observes this via
    `load_key()` returning `None`.
    """

    key: Optional[str]
    manual_fallback: bool


# ---------------------------------------------------------------------------
# 10.2 -- capture
# ---------------------------------------------------------------------------


def capture_key(non_interactive: bool) -> KeyState:
    """Prompt for the NGC API key (doc 00 §10.2).

    Echo is suppressed via `getpass.getpass` -- the key is never displayed
    and never logged. Under `--non-interactive` (doc 00 §4: "never prompt;
    use defaults/config; fail if a required value is missing"), there is no
    stored/default key to fall back to yet at first-capture time, so this
    skips the prompt entirely and reports the manual fallback, exactly as if
    the operator had left the interactive prompt blank.
    """
    if non_interactive:
        log.info(
            "Non-interactive: skipping NGC API key prompt "
            f"({_REDACTED_LOG_LINE} not captured; manual fallback)."
        )
        return KeyState(key=None, manual_fallback=True)

    raw = getpass.getpass(
        "NGC API key (leave blank to configure manually later): "
    ).strip()

    if not raw:
        log.info(f"No key entered ({_REDACTED_LOG_LINE}); manual fallback.")
        return KeyState(key=None, manual_fallback=True)

    log.info(f"Captured {_REDACTED_LOG_LINE}.")
    return KeyState(key=raw, manual_fallback=False)


# ---------------------------------------------------------------------------
# Shared atomic-write helper (doc 00 §6.3 discipline, re-applied here: temp
# file in the same directory, flush + fsync, then os.replace -- see doc 00
# §14.3 for the sibling webapp.py credential using the identical pattern).
# The temp file is created at mode 0o600 from the very first os.open() call
# (tempfile.mkstemp's default), so the secret is never briefly
# world-readable between creation and the final chmod/chown.
# ---------------------------------------------------------------------------


def _atomic_write_secret(
    directory: pathlib.Path, target: pathlib.Path, contents: str
) -> None:
    directory.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(directory))
    tmp_path = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contents)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# 10.1 / 10.2 -- storage
# ---------------------------------------------------------------------------


def store_key(key: str, install_dir: StrPath) -> pathlib.Path:
    """Write `<install_dir>/secrets/ngc.env` (doc 00 §10.1/§10.2).

    Single line `NGC_API_KEY=<key>`. The secrets directory is `chmod 700`,
    the file `chmod 600`, both chowned to the invoking user
    (`privilege.resolve()`). Uses the atomic temp-file + `os.replace`
    pattern (see `_atomic_write_secret`) so a crash mid-write never leaves a
    truncated or world-readable secret file behind.
    """
    secrets_dir = pathlib.Path(install_dir) / _SECRET_RELATIVE_PATH.parent
    target = secrets_dir / _SECRET_RELATIVE_PATH.name

    user = privilege.resolve()

    _atomic_write_secret(secrets_dir, target, f"{_KEY_PREFIX}{key}\n")

    os.chown(secrets_dir, user.uid, user.gid)
    os.chmod(secrets_dir, 0o700)
    os.chown(target, user.uid, user.gid)
    os.chmod(target, 0o600)

    log.info(f"Stored {_REDACTED_LOG_LINE} at {target}.")
    return target


def load_key(install_dir: StrPath = DEFAULT_INSTALL_DIR) -> Optional[str]:
    """Read back the stored key (doc 00 §10.2/§10.3).

    Returns `None` when `secrets/ngc.env` doesn't exist, or exists but has
    no usable `NGC_API_KEY=` value -- both cases mean "the operator chose
    the manual fallback; no key was ever stored" from a calling step's point
    of view.
    """
    path = pathlib.Path(install_dir) / _SECRET_RELATIVE_PATH
    if not path.is_file():
        return None

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(_KEY_PREFIX):
            value = line[len(_KEY_PREFIX):]
            return value or None

    return None


# ---------------------------------------------------------------------------
# 10.2 -- ~/.ngc/config for steps that shell out to the `ngc` CLI
# ---------------------------------------------------------------------------


def configure_ngc_cli(
    install_dir: StrPath = DEFAULT_INSTALL_DIR,
) -> Optional[pathlib.Path]:
    """Write `~/.ngc/config` for the invoking user (doc 00 §10.2).

    Mirrors Phase 10 of laptop/scripts/00_bootstrap.sh's `NGCINI` heredoc
    exactly:

        [CURRENT]
        apikey = <key>
        format_type = ascii

    Resolves the invoking user's home via `privilege.resolve()` (never
    `$HOME` -- see privilege.py's §9.2 docstring for why) and writes there
    directly, chowning the result back to that user -- see the module
    docstring's "Design note" for why this doesn't go through
    `privilege.run_as_user` instead.

    Reads the key via `load_key(install_dir)`. If no key was ever stored
    (manual fallback, doc 00 §10.3), this is a no-op that returns `None` --
    it is each step's job to detect that from `load_key()` directly and
    surface a USER_ACTION_REQUIRED block, not this function's.
    """
    key = load_key(install_dir)
    if key is None:
        log.warn(
            f"{_REDACTED_LOG_LINE} not configured (manual fallback); "
            "skipping ~/.ngc/config."
        )
        return None

    user = privilege.resolve()
    ngc_dir = user.home / ".ngc"
    config_path = ngc_dir / "config"

    contents = f"[CURRENT]\napikey = {key}\nformat_type = ascii\n"
    _atomic_write_secret(ngc_dir, config_path, contents)

    os.chown(ngc_dir, user.uid, user.gid)
    os.chmod(ngc_dir, 0o700)
    os.chown(config_path, user.uid, user.gid)
    os.chmod(config_path, 0o600)

    log.info(f"Wrote ~/.ngc/config for {user.name} ({_REDACTED_LOG_LINE}).")
    return config_path
