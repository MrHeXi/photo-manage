"""Cancellable file batches with durable intent records and per-file outcomes."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from photo_dedupe.core.file_ops import unique_target_path
from photo_dedupe.core.models import FileRecord
from photo_dedupe.core.organizer import OrganizePreviewItem
from photo_dedupe.core.selection import ProcessingPlan, is_under_any, normalize_path
from photo_dedupe.core.storage import AppDatabase

ProgressCallback = Callable[[str, int, int], None]
CancelCallback = Callable[[], bool]


class OperationCancelled(Exception):
    """Cancellation before the current file has been removed."""


class OperationSkipped(Exception):
    """The source must stay in place because its validation became stale."""


@dataclass(frozen=True, slots=True)
class BatchItemResult:
    source_path: Path
    target_path: Path | None
    status: str
    size: int
    message: str


@dataclass(frozen=True, slots=True)
class BatchResult:
    items: tuple[BatchItemResult, ...]
    journal_path: Path

    @property
    def completed_count(self) -> int:
        return sum(item.status == "completed" for item in self.items)

    @property
    def failed_count(self) -> int:
        return sum(item.status == "failed" for item in self.items)

    @property
    def skipped_count(self) -> int:
        return sum(item.status == "skipped" for item in self.items)

    @property
    def cancelled_count(self) -> int:
        return sum(item.status == "cancelled" for item in self.items)

    @property
    def completed_sources(self) -> set[Path]:
        return {item.source_path for item in self.items if item.status == "completed"}

    @property
    def moved_pairs(self) -> list[tuple[Path, Path]]:
        return [
            (item.source_path, item.target_path)
            for item in self.items
            if item.status == "completed" and item.target_path is not None
        ]

    @property
    def retryable_sources(self) -> set[Path]:
        return {item.source_path for item in self.items if item.status in {"failed", "cancelled"}}


def _check_cancel(should_cancel: CancelCallback | None) -> None:
    if should_cancel and should_cancel():
        raise OperationCancelled("用户已取消，文件未移动")


def _identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _snapshot_error(record: FileRecord) -> str | None:
    try:
        if record.path.is_symlink() or not record.path.is_file():
            return "文件不存在或不是普通文件，请重新扫描"
        stat = record.path.stat()
        if stat.st_size != record.size or stat.st_mtime != record.modified_at:
            return "文件自扫描或预览后发生变化，请重新扫描"
    except OSError as exc:
        return f"无法读取文件：{exc}"
    return None


def _digest(path: Path, should_cancel: CancelCallback | None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _check_cancel(should_cancel)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _journal_location(database: AppDatabase | None, requested: Path | None) -> Path:
    if requested is not None:
        return requested
    base = database.path.parent if database is not None else Path(tempfile.gettempdir()) / "PhotoManageGPT"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return base / "operation-journal" / f"{stamp}-{uuid.uuid4().hex}.jsonl"


def _append_journal(path: Path, **entry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"time": time.time(), **entry}, ensure_ascii=False, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _move_file_safely(
    source: Path,
    target: Path,
    should_cancel: CancelCallback | None,
    before_remove: Callable[[], None] | None = None,
) -> None:
    """Reserve destination exclusively; remove source only after a complete copy.

    Same-volume hard links avoid copying large videos and cannot overwrite another
    file. Other volumes use exclusive creation and verified content before unlink.
    """
    original = _identity(source)
    _check_cancel(should_cancel)
    linked = False
    try:
        os.link(source, target)
        linked = True
    except FileExistsError:
        raise
    except OSError:
        pass
    if linked:
        try:
            _check_cancel(should_cancel)
            if before_remove:
                before_remove()
            if _identity(source) != original or not os.path.samefile(source, target):
                raise OSError("源文件在移动期间变化，已保留源文件")
            source.unlink()
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return

    created = False
    try:
        digest = hashlib.sha256()
        with source.open("rb") as input_stream, target.open("xb") as output_stream:
            created = True
            while True:
                _check_cancel(should_cancel)
                chunk = input_stream.read(1024 * 1024)
                if not chunk:
                    break
                output_stream.write(chunk)
                digest.update(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if _digest(target, should_cancel) != digest.hexdigest() or _identity(source) != original:
            raise OSError("复制校验失败或源文件在移动期间变化，已保留源文件")
        shutil.copystat(source, target)
        _check_cancel(should_cancel)
        if before_remove:
            before_remove()
        if _identity(source) != original:
            raise OSError("源文件在移动期间变化，已保留源文件")
        source.unlink()
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class _Task:
    record: FileRecord
    target: Path | None


def _run_batch(
    tasks: list[_Task],
    database: AppDatabase | None,
    action: str,
    validator: Callable[[FileRecord], str | None],
    *,
    progress: ProgressCallback | None,
    should_cancel: CancelCallback | None,
    previous_result: BatchResult | None,
    journal_path: Path | None,
) -> BatchResult:
    journal = _journal_location(database, journal_path or (previous_result.journal_path if previous_result else None))
    previous = {normalize_path(item.source_path): item for item in previous_result.items} if previous_result else {}
    results: list[BatchItemResult] = []
    seen: set[Path] = set()
    for index, task in enumerate(tasks, start=1):
        record = task.record
        key = normalize_path(record.path)
        if key in seen:
            continue
        seen.add(key)
        prior = previous.get(key)
        if prior is not None and prior.status not in {"failed", "cancelled"}:
            results.append(prior)
            continue
        target: Path | None = task.target
        status, message = "failed", ""
        completed = False
        try:
            _check_cancel(should_cancel)
            if progress:
                progress(f"正在处理：{record.name}", index - 1, len(tasks))
            error = _snapshot_error(record) or validator(record)
            if error:
                status, message = "skipped", error
            elif target is not None and normalize_path(target) == key:
                status, message = "skipped", "文件已经在目标位置"
            else:
                if target is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target = unique_target_path(target.parent, target.name)
                # A failed intent write must prevent the filesystem mutation.
                _append_journal(
                    journal, state="intent", action=action, source=record.path,
                    target=target, size=record.size, modified_at=record.modified_at,
                )
                _check_cancel(should_cancel)
                # Recheck the survivor immediately before removing a source.
                error = _snapshot_error(record) or validator(record)
                if error:
                    status, message = "skipped", error
                else:
                    def revalidate_before_remove() -> None:
                        error = _snapshot_error(record) or validator(record)
                        if error:
                            raise OperationSkipped(error)
                        _check_cancel(should_cancel)

                    if target is None:
                        from send2trash import send2trash
                        revalidate_before_remove()
                        send2trash(str(record.path))
                    else:
                        _move_file_safely(record.path, target, should_cancel, revalidate_before_remove)
                    completed = True
                    status, message = "completed", "已移动到回收站" if target is None else "已移动到目标目录"
                    if database is not None:
                        try:
                            if target is None:
                                database.log_trash(record.path)
                            else:
                                database.log_move(record.path, target)
                        except Exception as exc:
                            message += f"；操作记录写入失败，请按恢复日志核对：{exc}"
        except OperationCancelled as exc:
            status, message = "cancelled", str(exc)
        except OperationSkipped as exc:
            status, message = "skipped", str(exc)
        except Exception as exc:
            status, message = "failed", str(exc)
        if completed:
            status = "completed"
        item = BatchItemResult(record.path, target, status, record.size, message)
        try:
            _append_journal(
                journal, state=status, action=action, source=record.path,
                target=target, size=record.size, message=message,
            )
        except Exception as exc:
            item = BatchItemResult(record.path, target, status, record.size, f"{message}；恢复日志写入失败：{exc}")
        results.append(item)
        if progress:
            progress(f"已处理 {index}/{len(tasks)}", index, len(tasks))
    return BatchResult(tuple(results), journal)


def process_duplicates(
    plan: ProcessingPlan,
    destination_root: Path,
    action: str,
    database: AppDatabase | None = None,
    *,
    progress: ProgressCallback | None = None,
    should_cancel: CancelCallback | None = None,
    previous_result: BatchResult | None = None,
    journal_path: Path | None = None,
) -> BatchResult:
    """Validate surviving copies, then process each planned candidate independently."""
    trash = action == "trash" or "回收站" in action
    kept = {normalize_path(record.path) for group in plan.groups for record in group.records if record.keep_selected}
    candidates = {normalize_path(record.path) for record in plan.records_to_process}
    groups_by_path: dict[Path, list] = {}
    for group in plan.groups:
        for record in group.records:
            groups_by_path.setdefault(normalize_path(record.path), []).append(group)
    verified: dict[tuple[Path, tuple[int, int, int, int]], str] = {}

    def verified_digest(record: FileRecord) -> str:
        identity = _identity(record.path)
        cache_key = normalize_path(record.path), identity
        if cache_key not in verified:
            digest = _digest(record.path, should_cancel)
            if _identity(record.path) != identity:
                raise OSError("文件在校验期间变化，请重新扫描")
            verified[cache_key] = digest
        return verified[cache_key]

    def validate(record: FileRecord) -> str | None:
        key = normalize_path(record.path)
        if key in kept or is_under_any(record.path, set(plan.protected_roots)):
            return "此文件为保留项或位于保护目录，已跳过"
        groups = groups_by_path.get(key, [])
        if not groups:
            return "文件不属于当前扫描分组，请重新扫描"
        for group in groups:
            survivors = [
                other for other in group.records
                if other.keep_selected and normalize_path(other.path) != key
                and normalize_path(other.path) not in candidates
                and _snapshot_error(other) is None
            ]
            if not survivors:
                return "本组保留副本不存在或已变化，已阻止处理，请重新扫描或选择有效保留项"
            if group.kind == "exact":
                source_digest = verified_digest(record)
                if record.sha256 and source_digest != record.sha256:
                    return "源文件内容与扫描指纹不符，请重新扫描"
                matching_survivor = False
                for other in survivors:
                    try:
                        digest = verified_digest(other)
                    except OSError:
                        continue
                    if digest == source_digest and (not other.sha256 or digest == other.sha256):
                        matching_survivor = True
                        break
                if not matching_survivor:
                    return "保留副本内容与待处理文件不一致，已阻止处理，请重新扫描"
        return None

    tasks = [
        _Task(record, None if trash else destination_root / "当前筛选结果" / record.name)
        for record in plan.records_to_process
    ]
    return _run_batch(
        tasks, database, "trash" if trash else "move", validate,
        progress=progress, should_cancel=should_cancel,
        previous_result=previous_result, journal_path=journal_path,
    )


def process_organize(
    preview: list[OrganizePreviewItem],
    database: AppDatabase | None = None,
    *,
    progress: ProgressCallback | None = None,
    should_cancel: CancelCallback | None = None,
    previous_result: BatchResult | None = None,
    journal_path: Path | None = None,
) -> BatchResult:
    tasks = []
    previous = {normalize_path(item.source_path): item for item in previous_result.items} if previous_result else {}
    for item in preview:
        size, modified_at = item.size, item.modified_at
        if size is None or modified_at is None:
            try:
                stat = item.source.stat()
                size, modified_at = stat.st_size, stat.st_mtime
            except OSError:
                prior = previous.get(normalize_path(item.source))
                size, modified_at = prior.size if prior else 0, 0.0
        tasks.append(_Task(FileRecord(item.source, size, modified_at), item.target))
    return _run_batch(
        tasks, database, "move", lambda _record: None,
        progress=progress, should_cancel=should_cancel,
        previous_result=previous_result, journal_path=journal_path,
    )
