from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class SubmitLocalHomographyDto:
    CameraMac: str
    Matrix: List[List[float]]       # 3x3 as nested list
    FrameSize: List[int]            # [width, height]
    Inliers: int
    RmseBoard: float
    CornersUsed: int
    MarkersDetected: int
    ArucoDict: str
    SquaresX: int
    SquaresY: int
    SquareLength: float             # mm
    MarkerLength: float             # mm
    TimestampUnix: float
    SnapshotPath: Optional[str] = None          # cloud storage path, None if upload failed
    CameraMatrix: Optional[List[List[float]]] = None        # 3x3, None if unavailable
    DistortionCoefficients: Optional[List[float]] = None    # flat list, None if unavailable

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LocalHomographyResponseDto:
    HomographyId: str
    CameraMac: str

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "LocalHomographyResponseDto":
        return LocalHomographyResponseDto(
            HomographyId=str(data.get("HomographyId") or ""),
            CameraMac=str(data.get("CameraMac") or ""),
        )
