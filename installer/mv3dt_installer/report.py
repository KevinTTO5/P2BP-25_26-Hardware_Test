"""Dependency reporting + pinned-version verification (doc 00 §8.3-8.4).

Two shared helpers give every step identical wording when reporting
dependency state, plus a single reusable helper for equality-pinned version
verification (porting `require_version_eq` from
laptop/scripts/lib/common.sh). All three route through logs.py's `log.info`
so the lines are colour-aware on stderr and also captured in the transcript
(§8.1-8.2) -- this module does no I/O of its own.

Public API:
    report_installed(dependency, version)          -- "installed <dep> version <ver>"
    report_already_installed(dependency, version)   -- "already installed <dep> version <ver>"
    verify_pinned(label, actual, expected) -> bool  -- equality-pinned check

Every step MUST use verify_pinned in its verify() and one of the two
report_* helpers after every dependency it touches, so the transcript stays
uniform and greppable (§8.4).
"""

from __future__ import annotations

from .logs import log


def report_installed(dependency: str, version: str) -> None:
    """Report a newly installed dependency (doc 00 §8.3).

    Logs the exact required string: `installed <dependency> version <version>`.
    """
    log.info(f"installed {dependency} version {version}")


def report_already_installed(dependency: str, version: str) -> None:
    """Report a pre-existing dependency (doc 00 §8.3).

    Logs the exact required string:
    `already installed <dependency> version <version>`.
    """
    log.info(f"already installed {dependency} version {version}")


def verify_pinned(label: str, actual: str, expected: str) -> bool:
    """Equality-pinned version verification (doc 00 §8.4).

    Ports `require_version_eq` from lib/common.sh. On match, logs
    `Version OK: <label> == <actual>` and returns True. On mismatch, logs
    `Version check failed: <label> -- expected '<expected>', got '<actual>'`
    (em dash) and returns False. Never calls die() or exits -- the caller
    decides whether to die() or surface a USER_ACTION_REQUIRED.
    """
    if actual == expected:
        log.info(f"Version OK: {label} == {actual}")
        return True
    log.info(
        f"Version check failed: {label} — expected '{expected}', "
        f"got '{actual}'"
    )
    return False
