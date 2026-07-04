from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from photo_dedupe.core.file_details import FilePreviewDetails, inspect_file_preview
from photo_dedupe.core.hashers import is_image
from photo_dedupe.core.models import FileRecord
from photo_dedupe.core.selection import describe_keep_reasons, is_under_any


@dataclass(frozen=True, slots=True)
class ImageCompareItem:
    record: FileRecord
    details: FilePreviewDetails
    recommended: bool
    protected: bool
    reason: str


def build_image_compare_items(
    records: list[FileRecord],
    protected_roots: set[Path] | None = None,
    keep_rule: str = "保留最高质量",
) -> list[ImageCompareItem]:
    protected_roots = protected_roots or set()
    image_records = [record for record in records if is_image(record.path)]
    reasons = describe_keep_reasons(image_records, keep_rule, protected_roots) if image_records else {}
    items = [
        ImageCompareItem(
            record=record,
            details=inspect_file_preview(record),
            recommended=reasons.get(record.path, "").startswith("建议保留"),
            protected=is_under_any(record.path, protected_roots),
            reason=reasons.get(record.path, ""),
        )
        for record in image_records
    ]
    return sorted(items, key=_compare_sort_key)


def _compare_sort_key(item: ImageCompareItem) -> tuple[int, int, int, float, str]:
    pixels = (item.details.width or 0) * (item.details.height or 0)
    return (
        0 if item.recommended else 1,
        0 if item.protected else 1,
        -pixels,
        -item.details.size,
        str(item.record.path).lower(),
    )
