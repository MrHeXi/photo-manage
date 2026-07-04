from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from photo_dedupe.core.organizer import OrganizePreviewItem, OrganizePreviewSummary, summarize_organize_preview
from photo_dedupe.core.selection import ProcessingPlan, normalize_path


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


def build_processing_result_report(
    plan: ProcessingPlan,
    action_label: str,
    moved_sources: set[Path],
    moved_targets: list[Path] | None = None,
) -> OperationResultReport:
    normalized_sources = {normalize_path(path) for path in moved_sources}
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
    )


def format_operation_result_report(report: OperationResultReport, path_limit: int = 8) -> str:
    lines = [
        report.title,
        "",
        f"动作：{report.action_label}",
        f"计划处理：{report.planned_count} 个文件，{_format_size(report.planned_bytes)}",
        f"实际完成：{report.completed_count} 个文件，{_format_size(report.completed_bytes)}",
        f"执行时跳过：{report.skipped_count} 个文件",
        f"保护目录跳过：{report.protected_skip_count} 个文件",
        "",
        "恢复方式：",
        report.restore_hint,
        "",
        "涉及来源目录：",
        _limited_path_block(report.affected_directories, path_limit),
    ]
    if report.target_directories:
        lines.extend(["", "实际目标目录：", _limited_path_block(report.target_directories, path_limit)])
    lines.extend(["", "说明："])
    lines.extend(f"- {note}" for note in report.notes)
    return "\n".join(lines)


def _is_trash_action(action_label: str) -> bool:
    return "回收站" in action_label or "trash" in action_label.lower()


def _restore_hint(is_trash: bool) -> str:
    if is_trash:
        return "请在 Windows 回收站中还原；本软件会记录回收操作，但不能直接替系统回收站撤销。"
    return "可在“操作记录”页选中移动记录后撤销；如果目标文件已被再次移动，需要手动核对。"


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
