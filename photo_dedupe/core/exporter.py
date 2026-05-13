from __future__ import annotations

import csv
from pathlib import Path

from photo_dedupe.core.models import AiAnalysisRecord, FileRecord, OperationRecord


def export_files_csv(records: list[FileRecord], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["path", "size", "modified_at", "sha256", "image_hash", "video_hash", "width", "height"])
        for record in records:
            writer.writerow(
                [
                    str(record.path),
                    record.size,
                    record.modified_at,
                    record.sha256 or "",
                    record.image_hash if record.image_hash is not None else "",
                    record.video_hash if record.video_hash is not None else "",
                    record.width if record.width is not None else "",
                    record.height if record.height is not None else "",
                ]
            )


def export_operations_csv(records: list[OperationRecord], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["id", "action", "source_path", "target_path", "created_at", "undone_at"])
        for record in records:
            writer.writerow(
                [
                    record.id,
                    record.action,
                    str(record.source_path),
                    str(record.target_path),
                    record.created_at,
                    record.undone_at if record.undone_at is not None else "",
                ]
            )


def export_ai_analysis_csv(records: list[AiAnalysisRecord], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["id", "path", "model", "score", "summary", "created_at"])
        for record in records:
            writer.writerow(
                [
                    record.id,
                    str(record.path),
                    record.model,
                    "" if record.score is None else record.score,
                    record.summary,
                    record.created_at,
                ]
            )
