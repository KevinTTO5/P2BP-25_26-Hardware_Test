from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List


@dataclass
class DiskPartitionState:
    Path: str
    TotalMb: int
    UsedMb: int
    FreeMb: int
    UsePct: int
    Status: str       # "ok" | "warning" | "critical"
    DeletedFiles: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "DiskPartitionState":
        return DiskPartitionState(
            Path=str(data.get("Path") or ""),
            TotalMb=int(data.get("TotalMb") or 0),
            UsedMb=int(data.get("UsedMb") or 0),
            FreeMb=int(data.get("FreeMb") or 0),
            UsePct=int(data.get("UsePct") or 0),
            Status=str(data.get("Status") or "ok"),
            DeletedFiles=int(data.get("DeletedFiles") or 0),
        )
