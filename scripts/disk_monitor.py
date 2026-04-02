#!/usr/bin/env python3
"""scripts.disk_monitor

Monitors disk usage on key partitions and auto-remediates when critically low:
  - /tracks  → deletes oldest .jsonl files until free space recovers
  - /var/log → runs journalctl --vacuum-size to trim journal logs
  - /        → alert-only catch-all for the root filesystem

Writes a state file (run/disk_state.json) every cycle that heartbeat.py reads
and includes in the health report payload.

Environment variables (all optional):
  P2BP_DISK_TRACKS_DIR          default: /opt/p2bp/camera/tracks
  P2BP_DISK_WARN_FREE_MB        default: 2048   (2 GB)
  P2BP_DISK_CRITICAL_FREE_MB    default: 512    (512 MB)
  P2BP_DISK_MONITOR_INTERVAL_S  default: 60
  P2BP_DISK_MIN_FILE_AGE_S      default: 300    (skip .jsonl files touched in last 5 min)
  P2BP_DISK_JOURNAL_VACUUM_MB   default: 512    (vacuum target for /var/log journals)
  P2BP_LOG_LEVEL                default: INFO

To run:
    python3 -m scripts.disk_monitor
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from scripts.json_models.disk_state import DiskPartitionState


# --- logger --------------------------------------------------------------

def _build_logger() -> logging.Logger:
    logger = logging.getLogger("p2bp.disk_monitor")
    if logger.handlers:
        return logger
    level = getattr(logging, os.getenv("P2BP_LOG_LEVEL", "INFO").strip().upper(), logging.INFO)
    logger.setLevel(level)
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = _build_logger()


# --- env -----------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


TRACKS_DIR          = os.getenv("P2BP_DISK_TRACKS_DIR", "/opt/p2bp/camera/tracks")
WARN_FREE_MB        = _env_int("P2BP_DISK_WARN_FREE_MB", 2048)
CRITICAL_FREE_MB    = _env_int("P2BP_DISK_CRITICAL_FREE_MB", 512)
MONITOR_INTERVAL_S  = _env_float("P2BP_DISK_MONITOR_INTERVAL_S", 60.0)
MIN_FILE_AGE_S      = _env_float("P2BP_DISK_MIN_FILE_AGE_S", 300.0)
JOURNAL_VACUUM_MB   = _env_int("P2BP_DISK_JOURNAL_VACUUM_MB", 512)

# State file written here; read by heartbeat.py
STATE_FILE = Path("/opt/p2bp/camera/run/disk_state.json")


# --- disk utilities ------------------------------------------------------

def _bytes_to_mb(b: int) -> int:
    return b // (1024 * 1024)


def _measure(path: str) -> Optional[DiskPartitionState]:
    """Return a DiskPartitionState for the given path, or None if stat fails."""
    try:
        usage = shutil.disk_usage(path)
        total_mb = _bytes_to_mb(usage.total)
        used_mb  = _bytes_to_mb(usage.used)
        free_mb  = _bytes_to_mb(usage.free)
        use_pct  = int(used_mb / total_mb * 100) if total_mb > 0 else 0
        if free_mb <= CRITICAL_FREE_MB:
            status = "critical"
        elif free_mb <= WARN_FREE_MB:
            status = "warning"
        else:
            status = "ok"
        return DiskPartitionState(
            Path=path,
            TotalMb=total_mb,
            UsedMb=used_mb,
            FreeMb=free_mb,
            UsePct=use_pct,
            Status=status,
            DeletedFiles=0,
        )
    except Exception as e:
        logger.warning("Could not stat %s: %s", path, e)
        return None


# --- remediation: /tracks ------------------------------------------------

def _cleanup_tracks(tracks_dir: Path) -> int:
    """Delete oldest .jsonl files one at a time until free space > WARN_FREE_MB.

    Skips files modified within MIN_FILE_AGE_S seconds (may be actively written).
    Returns the number of files deleted.
    """
    deleted = 0
    now = time.time()

    while True:
        usage = shutil.disk_usage(str(tracks_dir))
        free_mb = _bytes_to_mb(usage.free)
        if free_mb > WARN_FREE_MB:
            break

        candidates = sorted(
            [
                f for f in tracks_dir.glob("*.jsonl")
                if f.is_file() and (now - f.stat().st_mtime) >= MIN_FILE_AGE_S
            ],
            key=lambda f: f.stat().st_mtime,
        )

        if not candidates:
            logger.warning(
                "Tracks dir still critical (%d MB free) but no deletable .jsonl files found "
                "(all files too recent or none exist).",
                free_mb,
            )
            break

        target = candidates[0]
        size_mb = _bytes_to_mb(target.stat().st_size)
        try:
            target.unlink()
            deleted += 1
            logger.info("Deleted old track file: %s (%d MB freed)", target.name, size_mb)
        except Exception as e:
            logger.warning("Failed to delete %s: %s", target, e)
            break

    return deleted


# --- remediation: /var/log -----------------------------------------------

def _cleanup_journal() -> None:
    """Vacuum systemd journal logs to JOURNAL_VACUUM_MB using journalctl."""
    try:
        result = subprocess.run(
            ["journalctl", f"--vacuum-size={JOURNAL_VACUUM_MB}M"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("Journal vacuum completed: %s", result.stderr.strip() or result.stdout.strip())
        else:
            logger.warning("Journal vacuum exited %d: %s", result.returncode, result.stderr.strip())
    except FileNotFoundError:
        logger.warning("journalctl not found; cannot vacuum journal.")
    except Exception as e:
        logger.warning("Journal vacuum failed: %s", e)


# --- state file ----------------------------------------------------------

def _write_state(states: List[DiskPartitionState]) -> None:
    """Atomically write disk state to the shared state file."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump([s.to_dict() for s in states], f)
        tmp.replace(STATE_FILE)
    except Exception as e:
        logger.warning("Failed to write disk state file: %s", e)


# --- main loop -----------------------------------------------------------

def run_once() -> None:
    tracks_path = Path(TRACKS_DIR)
    states: List[DiskPartitionState] = []

    for path in [TRACKS_DIR, "/var/log", "/"]:
        state = _measure(path)
        if state is None:
            continue

        if state.Status == "critical":
            if path == TRACKS_DIR:
                logger.warning(
                    "CRITICAL: %s only %d MB free — auto-deleting old track files.",
                    path, state.FreeMb,
                )
                deleted = _cleanup_tracks(tracks_path)
                state = _measure(path) or state  # re-measure after cleanup
                state = DiskPartitionState(
                    Path=state.Path,
                    TotalMb=state.TotalMb,
                    UsedMb=state.UsedMb,
                    FreeMb=state.FreeMb,
                    UsePct=state.UsePct,
                    Status=state.Status,
                    DeletedFiles=deleted,
                )
            elif path == "/var/log":
                logger.warning(
                    "CRITICAL: %s only %d MB free — vacuuming journal logs.",
                    path, state.FreeMb,
                )
                _cleanup_journal()
                state = _measure(path) or state  # re-measure after vacuum
        elif state.Status == "warning":
            logger.warning("WARNING: %s only %d MB free (%d%% used).", path, state.FreeMb, state.UsePct)

        states.append(state)

    _write_state(states)

    for s in states:
        if s.Status != "ok":
            logger.info(
                "Disk [%s]: %d/%d MB used (%d%%) — %s%s",
                s.Path, s.UsedMb, s.TotalMb, s.UsePct, s.Status,
                f", deleted {s.DeletedFiles} file(s)" if s.DeletedFiles else "",
            )


def main() -> None:
    logger.info(
        "Disk monitor starting (warn=%dMB, critical=%dMB, interval=%ss, tracks=%s)",
        WARN_FREE_MB, CRITICAL_FREE_MB, MONITOR_INTERVAL_S, TRACKS_DIR,
    )

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            logger.info("Exiting.")
            return
        except Exception as e:
            logger.warning("Unexpected error in monitor cycle: %s", e)

        time.sleep(MONITOR_INTERVAL_S)


if __name__ == "__main__":
    main()
