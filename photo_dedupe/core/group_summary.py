from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from photo_dedupe.core.models import DuplicateGroup, FileRecord
from photo_dedupe.core.organizer import category_label, classify_group_category
from photo_dedupe.core.selection import choose_keep_records, is_under_any


@dataclass(frozen=True, slots=True)
class DuplicateGroupSummary:
    kind_label: str
    category_label: str
    file_count: int
    total_bytes: int
    reclaimable_bytes: int
    directory_count: int
    directories: list[Path]
    disk_count: int
    disks: list[str]
    protected_count: int
    recommended_names: list[str]
    recommended_reason: str


def summarize_duplicate_group(
    group: DuplicateGroup,
    protected_roots: set[Path] | None = None,
    keep_rule: str = "保留最高质量",
) -> DuplicateGroupSummary:
    protected_roots = protected_roots or set()
    records = group.records
    keep_result = choose_keep_records(records, keep_rule, protected_roots)
    directories = sorted({record.path.parent for record in records}, key=lambda item: str(item).lower())
    disks = sorted({_disk_label(record.path) for record in records})

    return DuplicateGroupSummary(
        kind_label=_kind_label(group.kind),
        category_label=category_label(classify_group_category([record.path for record in records])),
        file_count=len(records),
        total_bytes=sum(record.size for record in records),
        reclaimable_bytes=group.reclaimable_bytes,
        directory_count=len(directories),
        directories=directories,
        disk_count=len(disks),
        disks=disks,
        protected_count=sum(1 for record in records if is_under_any(record.path, protected_roots)),
        recommended_names=[record.name for record in keep_result.keep_records],
        recommended_reason=keep_result.reason,
    )


def _kind_label(kind: str) -> str:
    labels = {
        "exact": "完全重复",
        "similar": "相似图片",
        "video": "相似视频",
    }
    return labels.get(kind, kind)


def _disk_label(path: Path) -> str:
    if path.drive:
        return path.drive.upper()
    if path.anchor:
        return path.anchor
    return path.parts[0] if path.parts else "-"
