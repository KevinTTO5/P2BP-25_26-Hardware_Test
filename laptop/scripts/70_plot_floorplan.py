#!/usr/bin/env python3
"""
laptop/scripts/70_plot_floorplan.py

Render people trajectories from exported tracking data onto a 2D floor-plan
style plot for sponsor-facing artifacts.

Inputs:
  - CSV from 60_record_tracking.sh (preferred), or
  - JSONL from 60_record_tracking.sh.

Outputs:
  - PNG artifact in <run_dir>/artifacts/ by default.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import matplotlib.image as mpimg
import matplotlib.pyplot as plt


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Plot tracked XY trajectories onto a 2D floor-plan style figure."
    )
    p.add_argument("--csv", type=Path, help="Path to tracks.csv from 60_record_tracking.sh.")
    p.add_argument("--jsonl", type=Path, help="Path to tracks.jsonl from 60_record_tracking.sh.")
    p.add_argument(
        "--run-dir",
        type=Path,
        help="Run directory (auto-discovers tracks.csv/tracks.jsonl and writes artifacts/).",
    )
    p.add_argument(
        "--floorplan-image",
        type=Path,
        help="Optional floor plan image path to draw as background.",
    )
    p.add_argument(
        "--output",
        type=Path,
        help="Output PNG path (default: <run_dir>/artifacts/floorplan_paths_<timestamp>.png).",
    )
    p.add_argument(
        "--max-tracks",
        type=int,
        default=40,
        help="Maximum number of trajectories to draw (default: 40).",
    )
    p.add_argument(
        "--min-points",
        type=int,
        default=3,
        help="Minimum points required per trajectory (default: 3).",
    )
    p.add_argument(
        "--title",
        type=str,
        default="People Paths (2D Floor Plan)",
        help="Plot title.",
    )
    return p.parse_args()


def infer_paths(args: argparse.Namespace) -> Tuple[Path, Optional[Path], Path]:
    run_dir = args.run_dir
    csv_path = args.csv
    jsonl_path = args.jsonl

    if run_dir is not None:
        if csv_path is None:
            candidate = run_dir / "tracks.csv"
            if candidate.exists():
                csv_path = candidate
        if jsonl_path is None:
            candidate = run_dir / "tracks.jsonl"
            if candidate.exists():
                jsonl_path = candidate

    if csv_path is None and jsonl_path is None:
        raise SystemExit("Provide at least one input: --csv, --jsonl, or --run-dir.")

    if csv_path is not None and not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")
    if jsonl_path is not None and not jsonl_path.exists():
        raise SystemExit(f"JSONL not found: {jsonl_path}")

    if run_dir is None:
        if csv_path is not None:
            run_dir = csv_path.parent
        else:
            run_dir = jsonl_path.parent

    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    if args.output is not None:
        output = args.output
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = artifacts_dir / f"floorplan_paths_{ts}.png"
    output.parent.mkdir(parents=True, exist_ok=True)

    if csv_path is None:
        csv_path = run_dir / "tracks.csv"

    return csv_path, jsonl_path, output


def to_float(v: object) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            return float(v)
        except ValueError:
            return None
    return None


def points_from_csv(csv_path: Path) -> List[Dict[str, object]]:
    points: List[Dict[str, object]] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            x = to_float(row.get("x"))
            y = to_float(row.get("y"))
            if x is None or y is None:
                continue
            entity = (row.get("entity_id") or "").strip()
            if not entity:
                entity = f"{row.get('stream','')}_anon"
            points.append(
                {
                    "x": x,
                    "y": y,
                    "entity": entity,
                    "stream": row.get("stream", ""),
                    "ts": to_float(row.get("event_ts_ms")) or to_float(row.get("ingest_ts_ms")) or 0.0,
                }
            )
    return points


def extract_xy_from_payload(payload: dict) -> List[Tuple[float, float, Optional[str]]]:
    out: List[Tuple[float, float, Optional[str]]] = []

    direct_pairs = [("x", "y"), ("posX", "posY"), ("worldX", "worldY"), ("coord_x", "coord_y")]
    for kx, ky in direct_pairs:
        x = to_float(payload.get(kx))
        y = to_float(payload.get(ky))
        if x is not None and y is not None:
            ident = payload.get("id") or payload.get("trackingId")
            out.append((x, y, str(ident) if ident is not None else None))
            return out

    containers = []
    for key in ("objects", "object"):
        if key in payload:
            containers.append(payload[key])
    for key in ("payload", "message", "event", "tracking"):
        node = payload.get(key)
        if isinstance(node, dict):
            for child_key in ("objects", "object"):
                if child_key in node:
                    containers.append(node[child_key])

    for container in containers:
        seq = container if isinstance(container, list) else [container]
        for item in seq:
            if not isinstance(item, dict):
                continue
            found = None
            for kx, ky in direct_pairs:
                x = to_float(item.get(kx))
                y = to_float(item.get(ky))
                if x is not None and y is not None:
                    found = (x, y)
                    break
            if found is None:
                continue
            ident = None
            for id_key in ("id", "trackingId", "object_id", "objectId", "globalId", "track_id"):
                if item.get(id_key) not in (None, ""):
                    ident = str(item[id_key])
                    break
            out.append((found[0], found[1], ident))
    return out


def points_from_jsonl(jsonl_path: Path) -> List[Dict[str, object]]:
    points: List[Dict[str, object]] = []
    with jsonl_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            topic = str(obj.get("topic", ""))
            stream = topic.split("/")[2] if len(topic.split("/")) >= 3 else ""
            base_ts = to_float(obj.get("ingest_ts_ms")) or 0.0
            xy_rows = extract_xy_from_payload(payload)
            for x, y, ident in xy_rows:
                entity = ident or f"{stream}_anon"
                points.append(
                    {"x": x, "y": y, "entity": entity, "stream": stream, "ts": base_ts}
                )
    return points


def choose_top_tracks(
    points: Iterable[Dict[str, object]],
    max_tracks: int,
    min_points: int,
) -> Dict[str, List[Dict[str, object]]]:
    grouped: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for p in points:
        grouped[str(p["entity"])].append(p)

    for entity in grouped:
        grouped[entity].sort(key=lambda r: float(r.get("ts", 0.0)))

    eligible = [(entity, rows) for entity, rows in grouped.items() if len(rows) >= min_points]
    eligible.sort(key=lambda er: len(er[1]), reverse=True)
    selected = eligible[:max_tracks]
    return {entity: rows for entity, rows in selected}


def compute_bounds(track_map: Dict[str, List[Dict[str, object]]]) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for rows in track_map.values():
        xs.extend(float(r["x"]) for r in rows)
        ys.extend(float(r["y"]) for r in rows)
    if not xs or not ys:
        raise SystemExit("No valid XY data to plot.")
    return min(xs), max(xs), min(ys), max(ys)


def main() -> None:
    args = parse_args()
    csv_path, jsonl_path, output_path = infer_paths(args)

    points: List[Dict[str, object]] = []
    if csv_path.exists():
        points.extend(points_from_csv(csv_path))
    if not points and jsonl_path is not None and jsonl_path.exists():
        points.extend(points_from_jsonl(jsonl_path))

    track_map = choose_top_tracks(points, max_tracks=args.max_tracks, min_points=args.min_points)
    if not track_map:
        raise SystemExit(
            "No trajectories passed filtering. Try lower --min-points or check CSV/JSONL contents."
        )

    x_min, x_max, y_min, y_max = compute_bounds(track_map)

    fig, ax = plt.subplots(figsize=(12, 8))
    if args.floorplan_image:
        if not args.floorplan_image.exists():
            raise SystemExit(f"Floor plan image not found: {args.floorplan_image}")
        image = mpimg.imread(args.floorplan_image)
        ax.imshow(image, extent=[x_min, x_max, y_min, y_max], origin="upper", alpha=0.35)

    for entity, rows in track_map.items():
        xs = [float(r["x"]) for r in rows]
        ys = [float(r["y"]) for r in rows]
        ax.plot(xs, ys, linewidth=1.8, alpha=0.8)
        ax.scatter(xs[0], ys[0], s=14, marker="o", alpha=0.8)
        ax.scatter(xs[-1], ys[-1], s=18, marker="x", alpha=0.9)

    ax.set_title(args.title)
    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.grid(alpha=0.25)
    ax.set_aspect("equal", adjustable="box")

    margin_x = max((x_max - x_min) * 0.05, 1.0)
    margin_y = max((y_max - y_min) * 0.05, 1.0)
    ax.set_xlim(x_min - margin_x, x_max + margin_x)
    ax.set_ylim(y_min - margin_y, y_max + margin_y)

    subtitle = f"tracks={len(track_map)}  points={sum(len(v) for v in track_map.values())}"
    fig.text(0.99, 0.01, subtitle, ha="right", va="bottom", fontsize=9, alpha=0.7)

    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)

    print(output_path)


if __name__ == "__main__":
    main()
