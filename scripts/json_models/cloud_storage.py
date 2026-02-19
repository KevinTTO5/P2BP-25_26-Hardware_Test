from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class RequestUploadUrlDto:
    PathFromRoot: str
    FileName: str
    Extension: str
    SizeBytes: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UploadUrlResponseDto:
    PathFromRoot: str
    SignedUrl: str
    ExpiresAt: Optional[str] = None

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "UploadUrlResponseDto":
        return UploadUrlResponseDto(
            PathFromRoot=str(data.get("PathFromRoot") or ""),
            SignedUrl=str(data.get("SignedUrl") or ""),
            ExpiresAt=(str(data.get("ExpiresAt")) if data.get("ExpiresAt") is not None else None),
        )


@dataclass(frozen=True)
class RequestDownloadUrlDto:
    PathFromRoot: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DownloadUrlResponseDto:
    PathFromRoot: str
    SignedUrl: str
    ExpiresAt: Optional[str] = None

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "DownloadUrlResponseDto":
        return DownloadUrlResponseDto(
            PathFromRoot=str(data.get("PathFromRoot") or ""),
            SignedUrl=str(data.get("SignedUrl") or ""),
            ExpiresAt=(str(data.get("ExpiresAt")) if data.get("ExpiresAt") is not None else None),
        )


@dataclass(frozen=True)
class ConfirmUploadedMediaDto:
    PathFromRoot: str
    FileName: str
    Extension: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MediaRecordResponseDto:
    Id: str
    Name: str
    PathFromRoot: str
    Extension: str

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "MediaRecordResponseDto":
        return MediaRecordResponseDto(
            Id=str(data.get("Id") or ""),
            Name=str(data.get("Name") or ""),
            PathFromRoot=str(data.get("PathFromRoot") or ""),
            Extension=str(data.get("Extension") or ""),
        )
