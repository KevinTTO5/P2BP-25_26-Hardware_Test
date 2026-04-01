from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ArucoMarkerSighting:
    MarkerId: int
    CornersPx: List[List[float]]       # [[x,y], [x,y], [x,y], [x,y]] undistorted pixel space


@dataclass
class SubmitArucoSightingsDto:
    CameraMac: str
    ArucoDict: str
    CapturedAt: str                    # ISO 8601 UTC
    Markers: List[ArucoMarkerSighting] = field(default_factory=list)
    SessionId: Optional[str] = None    # None = server creates a fresh session

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArucoSightingsResponseDto:
    SessionId: str
    Status: str                        # "collecting" | "computing" | "done" | "failed"
    CamerasCheckedIn: List[str]
    CamerasTotal: int

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "ArucoSightingsResponseDto":
        return ArucoSightingsResponseDto(
            SessionId=str(data.get("SessionId") or ""),
            Status=str(data.get("Status") or "collecting"),
            CamerasCheckedIn=list(data.get("CamerasCheckedIn") or []),
            CamerasTotal=int(data.get("CamerasTotal") or 0),
        )
