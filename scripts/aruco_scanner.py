#!/usr/bin/env python3
"""scripts.aruco_scanner

Scans for ArUco markers visible in each camera and uploads raw pixel sightings
to the server. The server applies local homographies and runs the BFS locking
algorithm server-side — this script only captures and forwards raw observations.

This is phase 2 of the homography locking workflow (phase 1 is the ChArUco scan
in homography.py). Once all Jetsons have submitted sightings the dashboard can
trigger compute-lock, which aggregates all sessions and pushes locked homographies
back via Firestore (read by the server's fusion script, not tracker.py).

Key behaviors:
- Reads ./config/config.json for ArucoLock.BeginScanning.
- Polling is mtime-based: no writes to config.json. A scan fires only when the
  config file is freshly written (new heartbeat) AND BeginScanning is true.
- Grabs MinFrames frames per camera, detects ArUco markers, and averages corner
  positions across detections before uploading.
- Posts raw pixel sightings to /api/Homography/submit-sightings.

To run:
    python3 -m scripts.aruco_scanner --once
    python3 -m scripts.aruco_scanner
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import requests

try:
    import cv2
    from cv2 import aruco as cv2_aruco  # type: ignore
except Exception as e:
    raise SystemExit(
        "OpenCV with aruco is required. Install opencv-contrib-python.\n"
        f"Import error: {e}"
    )

try:
    import scripts.camera_handler as camera_handler  # type: ignore
except Exception as e:
    raise SystemExit(
        "camera_handler module is required (scripts.camera_handler).\n"
        f"Import error: {e}"
    )

from scripts import cloud_storage_media
from scripts.homography import FrameUndistorter, grab_frames, open_capture
from scripts.json_models.aruco_sightings import (
    ArucoMarkerSighting,
    ArucoSightingsResponseDto,
    SubmitArucoSightingsDto,
)


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _resolve_base_dir() -> Path:
    cwd = Path.cwd().resolve()
    if (cwd / "config" / "config.json").exists():
        return cwd
    script_root = Path(__file__).resolve().parent.parent
    if (script_root / "config" / "config.json").exists():
        return script_root
    return cwd


def _aruco_dict_id(name: str) -> int:
    attr = getattr(cv2_aruco, name, None)
    if attr is None and not name.startswith("DICT_"):
        attr = getattr(cv2_aruco, f"DICT_{name}", None)
    if attr is None:
        raise ValueError(f"Unknown ArUco dictionary: {name!r}")
    return int(attr)


def _load_homography_hash(mac: str, homographies_dir: str) -> Optional[str]:
    """Load the raw (unscaled) local homography YAML for a camera and return a short hash.

    We read the YAML directly rather than using camera_handler because camera_handler
    stores a resolution-scaled copy of the matrix. The hash must be based on the
    canonical unscaled values so it stays stable across resolution config changes.
    Returns None if the YAML is missing or the matrix can't be read.
    """
    safe_mac = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in mac)
    path = os.path.join(homographies_dir, f"{safe_mac}_homography.yml")
    if not os.path.exists(path):
        return None
    try:
        fs = cv2.FileStorage(path, cv2.FILE_STORAGE_READ)
        try:
            mat = fs.getNode("homography").mat()
        finally:
            fs.release()
        if mat is None:
            return None
        flat = ",".join(f"{round(float(v), 4):.4f}" for v in mat.flatten())
        return hashlib.sha256(flat.encode()).hexdigest()[:16]
    except Exception:
        return None


def _detect_aruco(image_bgr: np.ndarray, aruco_dict_id: int) -> Dict[int, np.ndarray]:
    """Detect ArUco markers. Returns {marker_id: corners (4, 2)} in pixel coords."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    dictionary = cv2_aruco.getPredefinedDictionary(aruco_dict_id)
    parameters = cv2_aruco.DetectorParameters()
    try:
        detector = cv2_aruco.ArucoDetector(dictionary, parameters)
        corners, ids, _ = detector.detectMarkers(gray)
    except AttributeError:
        corners, ids, _ = cv2_aruco.detectMarkers(gray, dictionary, parameters=parameters)

    result: Dict[int, np.ndarray] = {}
    if ids is not None:
        for i, marker_id in enumerate(ids.flatten()):
            result[int(marker_id)] = corners[i].reshape(4, 2)
    return result


def _submit_sightings(api_key: str, endpoint: str, dto: SubmitArucoSightingsDto) -> ArucoSightingsResponseDto:
    url = cloud_storage_media._join_url(endpoint, "/api/Homography/submit-sightings")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    for attempt in range(1, 4):
        try:
            r = requests.post(url, headers=headers, json=dto.to_dict(), timeout=10)
            r.raise_for_status()
            return ArucoSightingsResponseDto.from_dict(r.json())
        except Exception:
            if attempt < 3:
                time.sleep(float(2 ** attempt))
            else:
                raise


def run_once(base_dir: Path) -> None:
    cfg_path = base_dir / "config" / "config.json"
    if not cfg_path.exists():
        raise SystemExit(f"Missing config file: {cfg_path}")

    log("run_once starting")
    with cfg_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    aruco_cfg = config.get("ArucoLock")
    if not isinstance(aruco_cfg, dict):
        raise SystemExit("Missing required config section: ArucoLock")
    if not bool(aruco_cfg.get("BeginScanning")):
        log("ArucoLock.BeginScanning is false; nothing to do.")
        return

    dict_name: str = str(aruco_cfg.get("ArucoDict", "DICT_4X4_50"))
    min_frames: int = int(aruco_cfg.get("MinFrames", 10))
    max_seconds_per_cam: float = float(aruco_cfg.get("MaxSecondsPerCam", 10.0))

    try:
        aruco_dict_id = _aruco_dict_id(dict_name)
    except ValueError as e:
        raise SystemExit(str(e))

    try:
        api_key, endpoint = cloud_storage_media.load_env()
    except Exception as e:
        raise SystemExit(f"Upload credentials unavailable: {e}")

    states = camera_handler.get_camera_states()
    tc = config.get("TrackingCameras", {})
    cam_macs: List[str] = (
        [mac for mac, enabled in tc.items() if bool(enabled) and mac in states]
        if isinstance(tc, dict) and tc
        else list(states.keys())
    )

    log(f"Scanning {len(cam_macs)} camera(s) for ArUco markers (dict={dict_name}, min_frames={min_frames})")

    homographies_dir = str(base_dir / "homographies")
    homography_hashes: Dict[str, Optional[str]] = {
        mac: _load_homography_hash(mac, homographies_dir) for mac in cam_macs
    }
    for mac, h in homography_hashes.items():
        if h is None:
            log(f"[{mac}] WARNING: no local homography found — sighting will be filtered at compute-lock time")

    session_id: Optional[str] = None
    captured_at = datetime.now(timezone.utc).isoformat()

    for mac in cam_macs:
        cam = camera_handler.get_camera(mac)
        if cam is None:
            log(f"[{mac}] missing camera from camera_handler, skipping")
            continue

        rtsp = getattr(cam, "rtsp", None)
        if not isinstance(rtsp, str) or not rtsp.strip():
            log(f"[{mac}] missing RTSP URL, skipping")
            continue

        K_raw = getattr(cam, "camera_matrix", None)
        dist_raw = getattr(cam, "distortion_coefficients", None)
        declared_res = getattr(cam, "resolution", None)
        declared_size = None
        if isinstance(declared_res, (list, tuple)) and len(declared_res) == 2:
            try:
                declared_size = (int(declared_res[0]), int(declared_res[1]))
            except Exception:
                pass

        K_np = None
        dist_np = None
        if K_raw is not None:
            try:
                K_np = np.array(K_raw, dtype=np.float64)
                if K_np.shape != (3, 3):
                    K_np = None
            except Exception:
                K_np = None
        if dist_raw is not None:
            try:
                dist_np = np.array(dist_raw, dtype=np.float64).reshape(-1)
            except Exception:
                dist_np = None

        undistorter = FrameUndistorter(K_np, dist_np, expected_size=declared_size)

        log(f"[{mac}] opening stream...")
        cap = open_capture(rtsp)
        if not cap.isOpened():
            log(f"[{mac}] failed to open RTSP stream, skipping")
            continue

        try:
            frames = grab_frames(cap, max_frames=min_frames, max_seconds=max_seconds_per_cam)
        finally:
            cap.release()

        if not frames:
            log(f"[{mac}] no frames received, skipping")
            continue

        log(f"[{mac}] grabbed {len(frames)} frame(s)")

        # Accumulate detected pixel corners per marker across all frames, then average.
        corner_accumulator: Dict[int, List[np.ndarray]] = {}
        for frame in frames:
            undist = undistorter.undistort(frame)
            for marker_id, corners in _detect_aruco(undist, aruco_dict_id).items():
                corner_accumulator.setdefault(marker_id, []).append(corners)

        if not corner_accumulator:
            log(f"[{mac}] no ArUco markers detected, skipping")
            continue

        log(f"[{mac}] detected marker IDs: {sorted(corner_accumulator.keys())}")

        sightings: List[ArucoMarkerSighting] = []
        for marker_id, corner_list in corner_accumulator.items():
            avg_px = np.mean(np.stack(corner_list, axis=0), axis=0)  # (4, 2)
            sightings.append(ArucoMarkerSighting(
                MarkerId=marker_id,
                CornersPx=avg_px.tolist(),
            ))

        dto = SubmitArucoSightingsDto(
            CameraMac=mac,
            ArucoDict=dict_name,
            CapturedAt=captured_at,
            Markers=sightings,
            SessionId=session_id,
            LocalHomographyHash=homography_hashes.get(mac),
        )

        try:
            response = _submit_sightings(api_key, endpoint, dto)
            session_id = response.SessionId  # keep all cameras in the same session
            log(
                f"[{mac}] sightings submitted "
                f"(session={session_id}, status={response.Status}, "
                f"checked_in={len(response.CamerasCheckedIn)}/{response.CamerasTotal})"
            )
        except Exception as e:
            log(f"[{mac}] sightings submit failed: {e}")

    log("Scan complete.")


def run_service(base_dir: Path, poll_seconds: float) -> None:
    cfg_path = base_dir / "config" / "config.json"
    log(f"Watching: {cfg_path}")
    last_known_mtime: Optional[float] = None
    last_triggered_mtime: Optional[float] = None

    while True:
        try:
            if cfg_path.exists():
                try:
                    current_mtime = cfg_path.stat().st_mtime
                except OSError:
                    current_mtime = None

                if current_mtime is not None and current_mtime != last_known_mtime:
                    begin = False
                    try:
                        with cfg_path.open("r", encoding="utf-8") as f:
                            config = json.load(f)
                        aruco_cfg = config.get("ArucoLock") if isinstance(config, dict) else None
                        begin = bool(aruco_cfg.get("BeginScanning")) if isinstance(aruco_cfg, dict) else False
                    except Exception:
                        pass

                    last_known_mtime = current_mtime

                    if begin and current_mtime != last_triggered_mtime:
                        last_triggered_mtime = current_mtime
                        run_once(base_dir=base_dir)

        except KeyboardInterrupt:
            log("Exiting.")
            return
        except Exception as e:
            log(f"Error in aruco_scanner service loop: {e}")
            traceback.print_exc(file=sys.stdout)

        time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="ArUco marker scanner for homography locking.")
    parser.add_argument("--once", action="store_true", help="Run one scan if BeginScanning is true, then exit.")
    parser.add_argument("--poll", type=float, default=1.0, help="Polling interval in seconds.")
    args = parser.parse_args()

    base_dir = _resolve_base_dir()
    try:
        log(f"script: {Path(__file__).resolve()}")
        log(f"cwd: {Path.cwd().resolve()}")
        log(f"base_dir: {base_dir}")
    except Exception:
        pass

    if args.once:
        run_once(base_dir=base_dir)
    else:
        run_service(base_dir=base_dir, poll_seconds=args.poll)


if __name__ == "__main__":
    main()
