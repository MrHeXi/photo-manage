from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class FileRecord:
    path: Path
    size: int
    modified_at: float
    sha256: str | None = None
    image_hash: int | None = None
    video_hash: int | None = None
    width: int | None = None
    height: int | None = None
    keep_selected: bool = False

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()


@dataclass(slots=True)
class DuplicateGroup:
    kind: str
    title: str
    records: list[FileRecord]
    score: float = 1.0
    storage_id: int | None = None

    @property
    def reclaimable_bytes(self) -> int:
        if len(self.records) <= 1:
            return 0
        if any(item.keep_selected for item in self.records):
            return sum(item.size for item in self.records if not item.keep_selected)
        best = max(self.records, key=lambda item: (item.size, item.modified_at))
        return sum(item.size for item in self.records if item.path != best.path)


@dataclass(slots=True)
class OperationRecord:
    id: int
    action: str
    source_path: Path
    target_path: Path
    created_at: float
    undone_at: float | None = None

    @property
    def is_undone(self) -> bool:
        return self.undone_at is not None


@dataclass(slots=True)
class AiAnalysisRecord:
    id: int
    path: Path
    model: str
    summary: str
    score: float | None
    raw_response: str
    created_at: float


@dataclass(slots=True)
class ScanSessionRecord:
    id: int
    created_at: float
    root_paths: list[Path]


@dataclass(slots=True)
class ScanSessionSummaryRecord:
    id: int
    created_at: float
    root_paths: list[Path]
    group_count: int
    file_count: int
    duplicate_bytes: int
    reclaimable_bytes: int
