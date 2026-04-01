from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class IntrinsicsSightingDto:
    CornerCount: int
    ImagePoints: List[List[float]]  # [[x,y], ...]
    CornerIds: List[int]
    FrameSize: List[int]            # [width, height]
    Rmse: float
    CapturedAt: str                 # ISO 8601 UTC

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SubmitIntrinsicsSightingsDto:
    CameraMac: str
    IsPerUnit: bool
    ModelId: Optional[str]
    Sightings: List[IntrinsicsSightingDto] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IntrinsicsSightingsResponseDto:
    SightingsStored: int

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "IntrinsicsSightingsResponseDto":
        return IntrinsicsSightingsResponseDto(
            SightingsStored=int(data.get("SightingsStored") or 0),
        )


@dataclass
class SubmitIntrinsicsResultDto:
    CameraMac: str
    IsPerUnit: bool
    ModelId: Optional[str]
    CameraMatrix: List[List[float]]     # 3x3
    DistortionCoefficients: List[float] # flat
    ReprojectionError: float
    SightingsUsed: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IntrinsicsResultResponseDto:
    Id: str
    CameraMac: Optional[str]
    ModelId: Optional[str]
    IsPerUnit: bool
    CameraMatrix: List[List[float]]
    DistortionCoefficients: List[float]
    ReprojectionError: float
    SightingsUsed: int
    ComputedAtUnix: float

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "IntrinsicsResultResponseDto":
        return IntrinsicsResultResponseDto(
            Id=str(data.get("Id") or ""),
            CameraMac=data.get("CameraMac"),
            ModelId=data.get("ModelId"),
            IsPerUnit=bool(data.get("IsPerUnit")),
            CameraMatrix=data.get("CameraMatrix") or [],
            DistortionCoefficients=data.get("DistortionCoefficients") or [],
            ReprojectionError=float(data.get("ReprojectionError") or 0.0),
            SightingsUsed=int(data.get("SightingsUsed") or 0),
            ComputedAtUnix=float(data.get("ComputedAtUnix") or 0.0),
        )
