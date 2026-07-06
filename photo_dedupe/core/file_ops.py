from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from photo_dedupe.core.models import FileRecord
from photo_dedupe.core.models import OperationRecord
from photo_dedupe.core.storage import AppDatabase


@dataclass(frozen=True)
class UndoMoveItemResult:
    operation_id: int
    source_path: Path
    target_path: Path
    restored_path: Path | None
    status: str
    message: str


@dataclass(frozen=True)
class UndoMoveBatchResult:
    items: list[UndoMoveItemResult]

    @property
    def completed_count(self) -> int:
        return sum(1 for item in self.items if item.status == "restored")

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.items if item.status == "failed")

    @property
    def skipped_count(self) -> int:
        return sum(1 for item in self.items if item.status == "skipped")


def unique_target_path(directory: Path, filename: str) -> Path:
    target = directory / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    counter = 1
    while True:
        candidate = directory / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def move_records(
    records: list[FileRecord],
    destination_root: Path,
    group_title: str,
    database: AppDatabase | None = None,
) -> list[tuple[Path, Path]]:
    safe_group_name = "".join(char if char.isalnum() or char in " ._-" else "_" for char in group_title)
    target_dir = destination_root / safe_group_name
    target_dir.mkdir(parents=True, exist_ok=True)

    moved: list[tuple[Path, Path]] = []
    for record in records:
        if not record.path.exists():
            continue
        target = unique_target_path(target_dir, record.path.name)
        shutil.move(str(record.path), str(target))
        if database:
            database.log_move(record.path, target)
        moved.append((record.path, target))
    return moved


def trash_records(records: list[FileRecord], database: AppDatabase | None = None) -> list[Path]:
    try:
        from send2trash import send2trash
    except ImportError as exc:
        raise RuntimeError("Send2Trash is not installed.") from exc

    trashed: list[Path] = []
    for record in records:
        if not record.path.exists():
            continue
        send2trash(str(record.path))
        if database:
            database.log_trash(record.path)
        trashed.append(record.path)
    return trashed


def undo_move(operation: OperationRecord, database: AppDatabase | None = None) -> Path:
    if operation.action != "move":
        raise ValueError("Only move operations can be undone.")
    if operation.is_undone:
        raise ValueError("This operation has already been undone.")
    if not operation.target_path.exists():
        raise FileNotFoundError(f"Moved file does not exist: {operation.target_path}")

    operation.source_path.parent.mkdir(parents=True, exist_ok=True)
    restore_path = operation.source_path
    if restore_path.exists():
        restore_path = unique_target_path(operation.source_path.parent, operation.source_path.name)

    shutil.move(str(operation.target_path), str(restore_path))
    if database:
        database.mark_operation_undone(operation.id)
    return restore_path


def undo_move_operations(operations: list[OperationRecord], database: AppDatabase | None = None) -> UndoMoveBatchResult:
    results: list[UndoMoveItemResult] = []
    for operation in operations:
        if operation.action != "move":
            results.append(
                UndoMoveItemResult(
                    operation_id=operation.id,
                    source_path=operation.source_path,
                    target_path=operation.target_path,
                    restored_path=None,
                    status="skipped",
                    message="不是普通移动记录",
                )
            )
            continue
        if operation.is_undone:
            results.append(
                UndoMoveItemResult(
                    operation_id=operation.id,
                    source_path=operation.source_path,
                    target_path=operation.target_path,
                    restored_path=None,
                    status="skipped",
                    message="已经撤销",
                )
            )
            continue
        try:
            restored_path = undo_move(operation, database)
        except Exception as exc:
            results.append(
                UndoMoveItemResult(
                    operation_id=operation.id,
                    source_path=operation.source_path,
                    target_path=operation.target_path,
                    restored_path=None,
                    status="failed",
                    message=str(exc),
                )
            )
            continue
        results.append(
            UndoMoveItemResult(
                operation_id=operation.id,
                source_path=operation.source_path,
                target_path=operation.target_path,
                restored_path=restored_path,
                status="restored",
                message="已恢复",
            )
        )
    return UndoMoveBatchResult(results)
