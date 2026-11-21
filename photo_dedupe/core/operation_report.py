from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, TYPE_CHECKING

from photo_dedupe.core.organizer import OrganizePreviewItem, OrganizePreviewSummary, summarize_organize_preview
from photo_dedupe.core.selection import ProcessingPlan, normalize_path

if TYPE_CHECKING:
    from photo_dedupe.core.batch_operations import BatchResult


@dataclass(frozen=True, slots=True)
class OperationResultItem:
    source_path: Path
    target_path: Path | None
    status: str
    size: int
    message: str


@dataclass(frozen=True, slots=True)
class OperationResultReport:
    title: str
    action_label: str
    planned_count: int
    completed_count: int
    skipped_count: int
    planned_bytes: int
    completed_bytes: int
    protected_skip_count: int
    affected_directories: tuple[Path, ...]
    target_directories: tuple[Path, ...]
    restore_hint: str
    notes: tuple[str, ...]
    items: tuple[OperationResultItem, ...]

    @property
    def failed_count(self) -> int:
        return sum(item.status == "failed" for item in self.items)

    @property
    def cancelled_count(self) -> int:
        return sum(item.status == "cancelled" for item in self.items)


def build_batch_result_report(
    result: BatchResult,
    action_label: str,
    *,
    title: str = "重复文件处理报告",
    protected_skip_count: int = 0,
) -> OperationResultReport:
    """Build a report from actual outcomes, without relabelling errors as skips."""
    items = tuple(
        OperationResultItem(item.source_path, item.target_path, item.status, item.size, item.message)
        for item in result.items
    )
    notes = [
        f"恢复日志：{result.journal_path}",
        "已完成文件不会在重试中重复处理；失败和取消的文件可以重试。",
        "操作记录写入异常会保留在逐文件明细中，请按恢复日志核对实际路径。",
    ]
    if protected_skip_count:
        notes.append(f"{protected_skip_count} 个保护目录内文件没有被移动或回收。")
    return OperationResultReport(
        title=title,
        action_label=action_label,
        planned_count=len(items),
        completed_count=result.completed_count,
        skipped_count=result.skipped_count,
        planned_bytes=sum(item.size for item in items),
        completed_bytes=sum(item.size for item in items if item.status == "completed"),
        protected_skip_count=protected_skip_count,
        affected_directories=_sorted_unique_paths(item.source_path.parent for item in items),
        target_directories=_sorted_unique_paths(
            item.target_path.parent for item in items
            if item.status == "completed" and item.target_path is not None
        ),
        restore_hint=_restore_hint(_is_trash_action(action_label)),
        notes=tuple(notes),
        items=items,
    )


def build_processing_result_report(
    plan: ProcessingPlan,
    action_label: str,
    moved_sources: set[Path],
    moved_targets: list[Path] | None = None,
    moved_pairs: list[tuple[Path, Path]] | None = None,
) -> OperationResultReport:
    normalized_sources = {normalize_path(path) for path in moved_sources}
    target_by_source = {normalize_path(source): target for source, target in (moved_pairs or [])}
    if moved_pairs is not None:
        moved_targets = [target for _source, target in moved_pairs]

    completed_bytes = sum(
        record.size
        for record in plan.records_to_process
        if normalize_path(record.path) in normalized_sources
    )
    completed_count = len(normalized_sources)
    skipped_count = max(0, len(plan.records_to_process) - completed_count)
    is_trash = _is_trash_action(action_label)

    notes: list[str] = []
    if skipped_count:
        notes.append(f"{skipped_count} 个候选文件在执行时不存在或无法处理，已自动跳过。")
    if plan.protected_skip_count:
        notes.append(f"{plan.protected_skip_count} 个保护目录内文件没有被移动或回收。")
    notes.append("操作记录已更新，可在“操作记录”页查看或导出 CSV。")
    notes.append("本报告包含处理明细，可作为之后核对和恢复文件的索引。")

    items: list[OperationResultItem] = []
    for record in plan.records_to_process:
        normalized_path = normalize_path(record.path)
        if normalized_path in normalized_sources:
            items.append(
                OperationResultItem(
                    source_path=record.path,
                    target_path=None if is_trash else target_by_source.get(normalized_path),
                    status="completed",
                    size=record.size,
                    message="已移动到 Windows 回收站。" if is_trash else "已移动到目标目录。",
                )
            )
        else:
            items.append(
                OperationResultItem(
                    source_path=record.path,
                    target_path=target_by_source.get(normalized_path),
                    status="skipped",
                    size=record.size,
                    message="执行时未完成处理，可能是文件不存在、权限不足或已被其他程序移动。",
                )
            )

    return OperationResultReport(
        title="重复文件处理报告",
        action_label=action_label,
        planned_count=len(plan.records_to_process),
        completed_count=completed_count,
        skipped_count=skipped_count,
        planned_bytes=plan.reclaimable_bytes,
        completed_bytes=completed_bytes,
        protected_skip_count=plan.protected_skip_count,
        affected_directories=tuple(plan.affected_directories),
        target_directories=_sorted_unique_paths(path.parent for path in (moved_targets or [])),
        restore_hint=_restore_hint(is_trash),
        notes=tuple(notes),
        items=tuple(items),
    )


def build_organize_result_report(
    preview: list[OrganizePreviewItem],
    moved: list[tuple[Path, Path]],
    summary: OrganizePreviewSummary | None = None,
) -> OperationResultReport:
    summary = summary or summarize_organize_preview(preview)
    moved_sources = {normalize_path(source) for source, _target in moved}
    moved_targets = [target for _source, target in moved]
    preview_by_source = {normalize_path(item.source): item for item in preview}
    completed_bytes = 0
    for source, target in moved:
        item = preview_by_source.get(normalize_path(source))
        if item is not None and item.source.exists():
            completed_bytes += item.source.stat().st_size
        elif target.exists():
            completed_bytes += target.stat().st_size

    skipped_count = max(0, summary.file_count - len(moved_sources))
    notes: list[str] = []
    if summary.missing_count:
        notes.append(f"{summary.missing_count} 个源文件在执行前已不可访问，已跳过。")
    if summary.already_in_target_count:
        notes.append(f"{summary.already_in_target_count} 个文件已在目标位置，未重复移动。")
    if skipped_count and not notes:
        notes.append(f"{skipped_count} 个文件未移动，可能是执行时已不存在或无需处理。")
    notes.append("整理移动已写入操作记录，可在“操作记录”页撤销单个移动。")
    notes.append("本报告包含整理前后路径，可作为之后核对和恢复文件的索引。")

    target_by_source = {normalize_path(source): target for source, target in moved}
    items = tuple(
        OperationResultItem(
            source_path=item.source,
            target_path=target_by_source.get(normalize_path(item.source), item.target),
            status="completed" if normalize_path(item.source) in moved_sources else "skipped",
            size=item.source.stat().st_size if item.source.exists() else 0,
            message=(
                "已按整理预览移动到目标目录。"
                if normalize_path(item.source) in moved_sources
                else "未移动，可能是源文件不存在、已经在目标位置或执行时被跳过。"
            ),
        )
        for item in preview
    )

    return OperationResultReport(
        title="整理执行报告",
        action_label="按整理预览移动",
        planned_count=summary.file_count,
        completed_count=len(moved_sources),
        skipped_count=skipped_count,
        planned_bytes=summary.total_bytes,
        completed_bytes=completed_bytes,
        protected_skip_count=0,
        affected_directories=tuple(summary.source_directories),
        target_directories=_sorted_unique_paths(path.parent for path in moved_targets),
        restore_hint=_restore_hint(False),
        notes=tuple(notes),
        items=items,
    )


def format_operation_result_report(
    report: OperationResultReport,
    path_limit: int = 8,
    include_items: bool = False,
    item_limit: int | None = 50,
    generated_at: float | None = None,
) -> str:
    lines = [
        report.title,
        "",
    ]
    if generated_at is not None:
        lines.append(f"生成时间：{datetime.fromtimestamp(generated_at).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.extend(
        [
            f"动作：{report.action_label}",
            f"计划处理：{report.planned_count} 个文件，{_format_size(report.planned_bytes)}",
            f"实际完成：{report.completed_count} 个文件，{_format_size(report.completed_bytes)}",
            f"执行时跳过：{report.skipped_count} 个文件",
            f"执行失败：{report.failed_count} 个文件",
            f"取消未执行：{report.cancelled_count} 个文件",
            f"保护目录跳过：{report.protected_skip_count} 个文件",
            "",
            "恢复方式：",
            report.restore_hint,
            "",
            "涉及来源目录：",
            _limited_path_block(report.affected_directories, path_limit),
        ]
    )
    if report.target_directories:
        lines.extend(["", "实际目标目录：", _limited_path_block(report.target_directories, path_limit)])
    lines.extend(["", "说明："])
    lines.extend(f"- {note}" for note in report.notes)
    if include_items:
        lines.extend(["", "文件明细："])
        selected_items = report.items if item_limit is None else report.items[:item_limit]
        if not selected_items:
            lines.append("-")
        for index, item in enumerate(selected_items, start=1):
            target = str(item.target_path) if item.target_path is not None else "-"
            lines.extend(
                [
                    f"{index}. [{_item_status_label(item.status)}] {item.source_path}",
                    f"   目标：{target}",
                    f"   大小：{_format_size(item.size)}",
                    f"   说明：{item.message}",
                ]
            )
        if item_limit is not None and len(report.items) > item_limit:
            lines.append(f"... 另有 {len(report.items) - item_limit} 个文件未显示")
    return "\n".join(lines)


def export_operation_result_report(
    report: OperationResultReport,
    target: Path,
    generated_at: float | None = None,
) -> None:
    generated_at = time.time() if generated_at is None else generated_at
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        format_operation_result_report(
            report,
            path_limit=100000,
            include_items=True,
            item_limit=None,
            generated_at=generated_at,
        ),
        encoding="utf-8",
    )


def save_operation_result_report(
    report: OperationResultReport,
    report_dir: Path,
    generated_at: float | None = None,
) -> Path:
    generated_at = time.time() if generated_at is None else generated_at
    report_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_report_path(report_dir / _report_file_name(report, generated_at))
    export_operation_result_report(report, target, generated_at=generated_at)
    return target


def _is_trash_action(action_label: str) -> bool:
    return "回收站" in action_label or "trash" in action_label.lower()


def _restore_hint(is_trash: bool) -> str:
    if is_trash:
        return (
            "请在 Windows 回收站中按原路径或文件名还原；本软件会记录回收操作，"
            "但不能直接替系统回收站撤销。如果回收站被清空，需要依赖系统备份或专业恢复工具。"
        )
    return (
        "可在“操作记录”页选中移动记录后撤销，也可以批量撤销当前筛选下的普通移动。"
        "如果目标文件已被再次移动或改名，撤销可能失败，请按本报告中的来源和目标路径手动核对。"
    )


def _report_file_name(report: OperationResultReport, generated_at: float) -> str:
    stamp = datetime.fromtimestamp(generated_at).strftime("%Y%m%d-%H%M%S")
    kind = "organize" if "整理" in report.title else "dedupe"
    return f"{stamp}-{kind}-operation-report.txt"


def _unique_report_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 1000):
        candidate = target.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"无法生成唯一报告文件名：{target}")


def _item_status_label(status: str) -> str:
    return {
        "completed": "已完成",
        "skipped": "已跳过",
        "failed": "失败",
        "cancelled": "已取消",
    }.get(status, status)


def _sorted_unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    unique = {normalize_path(path) for path in paths}
    return tuple(sorted(unique, key=lambda item: str(item).lower()))


def _limited_path_block(paths: tuple[Path, ...], limit: int) -> str:
    if not paths:
        return "-"
    lines = [str(path) for path in paths[:limit]]
    if len(paths) > limit:
        lines.append(f"... 另有 {len(paths) - limit} 个目录")
    return "\n".join(lines)


def _format_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"
