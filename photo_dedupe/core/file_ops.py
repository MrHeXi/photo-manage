from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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
    journal_path: Path | None = None

    @property
    def completed_count(self) -> int:
        return sum(1 for item in self.items if item.status == "restored")

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.items if item.status == "failed")

    @property
    def skipped_count(self) -> int:
        return sum(1 for item in self.items if item.status == "skipped")

    @property
    def cancelled_count(self) -> int:
        return sum(1 for item in self.items if item.status == "cancelled")


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

    # Import lazily: batch_operations uses unique_target_path from this module.
    from photo_dedupe.core.batch_operations import _append_journal, _journal_location, _move_file_safely

    journal = _journal_location(database, None)
    _append_journal(
        journal, state="intent", action="undo", operation_id=operation.id,
        source=operation.target_path, target=restore_path,
    )
    _move_file_safely(operation.target_path, restore_path, None)
    warning = ""
    if database:
        try:
            database.mark_operation_undone(operation.id)
        except Exception as exc:
            warning = f"文件已恢复，撤销记录写入失败：{exc}"
    try:
        _append_journal(
            journal, state="restored", action="undo", operation_id=operation.id,
            source=operation.target_path, target=restore_path, message=warning,
        )
    except OSError:
        # The durable intent still contains both paths; do not report a completed
        # filesystem move as a failed operation that may be replayed.
        pass
    return restore_path


def undo_move_operations(
    operations: list[OperationRecord],
    database: AppDatabase | None = None,
    *,
    progress: Callable[[str, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    previous_result: UndoMoveBatchResult | None = None,
    journal_path: Path | None = None,
) -> UndoMoveBatchResult:
    from photo_dedupe.core.batch_operations import (
        OperationCancelled, _append_journal, _check_cancel, _journal_location, _move_file_safely,
    )

    journal = _journal_location(database, journal_path or (previous_result.journal_path if previous_result else None))
    previous = {item.operation_id: item for item in previous_result.items} if previous_result else {}
    results: list[UndoMoveItemResult] = []

    def record_result(item: UndoMoveItemResult) -> None:
        message = item.message
        try:
            _append_journal(
                journal, state=item.status, action="undo", operation_id=item.operation_id,
                source=item.target_path, target=item.restored_path, message=message,
            )
        except Exception as exc:
            message += f"；恢复日志写入失败：{exc}"
        results.append(UndoMoveItemResult(
            item.operation_id, item.source_path, item.target_path, item.restored_path, item.status, message,
        ))

    for index, operation in enumerate(operations, start=1):
        prior = previous.get(operation.id)
        if prior is not None and prior.status not in {"failed", "cancelled"}:
            results.append(prior)
            continue
        if should_cancel and should_cancel():
            record_result(
                UndoMoveItemResult(
                    operation.id, operation.source_path, operation.target_path, None,
                    "cancelled", "用户已取消，尚未恢复",
                )
            )
            continue
        if progress:
            progress(f"正在恢复：{operation.source_path.name}", index - 1, len(operations))
        if database:
            try:
                operation = database.get_operation(operation.id) or operation
            except Exception as exc:
                record_result(UndoMoveItemResult(
                    operation.id, operation.source_path, operation.target_path, None, "failed",
                    f"无法确认操作记录状态，文件未恢复：{exc}",
                ))
                continue
        if operation.action != "move":
            record_result(
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
            record_result(
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
        restored_path = None
        status, message = "failed", ""
        try:
            _check_cancel(should_cancel)
            if not operation.target_path.is_file() or operation.target_path.is_symlink():
                raise FileNotFoundError(f"已移动的文件不存在或不是普通文件：{operation.target_path}")
            operation.source_path.parent.mkdir(parents=True, exist_ok=True)
            restore_path = unique_target_path(operation.source_path.parent, operation.source_path.name)
            _append_journal(
                journal, state="intent", action="undo", operation_id=operation.id,
                source=operation.target_path, target=restore_path,
            )
            _move_file_safely(operation.target_path, restore_path, should_cancel)
            restored_path = restore_path
            status, message = "restored", "已恢复"
            if database:
                try:
                    database.mark_operation_undone(operation.id)
                except Exception as exc:
                    message += f"；撤销记录写入失败，请按恢复日志核对：{exc}"
        except OperationCancelled as exc:
            status, message = "cancelled", str(exc)
        except Exception as exc:
            status, message = "failed", str(exc)
        record_result(UndoMoveItemResult(
            operation.id, operation.source_path, operation.target_path, restored_path, status, message,
        ))
        if progress:
            progress(f"已恢复 {index}/{len(operations)}", index, len(operations))
    return UndoMoveBatchResult(results, journal)
