from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from photo_dedupe.core.selection import ProcessingPlan


RISK_LOW = "低"
RISK_MEDIUM = "中"
RISK_HIGH = "高"


@dataclass(frozen=True, slots=True)
class ProcessingRiskSummary:
    risk_level: str
    action_label: str
    group_count: int
    keep_count: int
    process_count: int
    reclaimable_bytes: int
    protected_skip_count: int
    affected_directories: tuple[Path, ...]
    destination: Path | None
    destination_free_bytes: int | None
    warnings: tuple[str, ...]


def build_processing_risk_summary(
    plan: ProcessingPlan,
    action_label: str,
    destination: Path | None = None,
    destination_free_bytes: int | None = None,
) -> ProcessingRiskSummary:
    process_count = len(plan.records_to_process)
    affected_count = len(plan.affected_directories)
    is_trash = _is_trash_action(action_label)
    warnings: list[str] = []

    if is_trash:
        warnings.append("将移动到 Windows 回收站；本软件不能直接撤销回收站操作，需要在系统回收站中还原。")
    if plan.protected_skip_count:
        warnings.append(f"已自动跳过 {plan.protected_skip_count} 个保护目录内文件。")
    if affected_count >= 10:
        warnings.append(f"本次会影响 {affected_count} 个来源目录，请重点核对目录列表。")
    if process_count >= 500:
        warnings.append(f"本次处理 {process_count} 个文件，数量较大，建议先导出报告或分批处理。")
    if not is_trash and destination is not None and destination_free_bytes is not None:
        if plan.reclaimable_bytes > destination_free_bytes:
            warnings.append(
                "目标磁盘可用空间不足："
                f"需要 {_format_size(plan.reclaimable_bytes)}，"
                f"可用 {_format_size(destination_free_bytes)}。请改用其他目标目录或先释放空间。"
            )
        elif plan.reclaimable_bytes > int(destination_free_bytes * 0.8):
            warnings.append(
                "本次移动接近目标磁盘剩余空间："
                f"需要 {_format_size(plan.reclaimable_bytes)}，"
                f"可用 {_format_size(destination_free_bytes)}。建议确认目标盘空间。"
            )

    risk_level = RISK_LOW
    if (
        is_trash
        or process_count >= 500
        or affected_count >= 20
        or (
            not is_trash
            and destination is not None
            and destination_free_bytes is not None
            and plan.reclaimable_bytes > destination_free_bytes
        )
    ):
        risk_level = RISK_HIGH
    elif (
        process_count >= 50
        or affected_count >= 5
        or plan.protected_skip_count > 0
        or (
            not is_trash
            and destination is not None
            and destination_free_bytes is not None
            and plan.reclaimable_bytes > int(destination_free_bytes * 0.8)
        )
    ):
        risk_level = RISK_MEDIUM

    return ProcessingRiskSummary(
        risk_level=risk_level,
        action_label=action_label,
        group_count=len(plan.groups),
        keep_count=plan.keep_count,
        process_count=process_count,
        reclaimable_bytes=plan.reclaimable_bytes,
        protected_skip_count=plan.protected_skip_count,
        affected_directories=tuple(plan.affected_directories),
        destination=None if is_trash else destination,
        destination_free_bytes=None if is_trash else destination_free_bytes,
        warnings=tuple(warnings),
    )


def format_processing_confirmation(summary: ProcessingRiskSummary, directory_limit: int = 8) -> str:
    lines = [
        f"风险等级：{summary.risk_level}",
        f"当前筛选结果：{summary.group_count} 组。",
        f"保留文件：{summary.keep_count} 个。",
        f"将按“{summary.action_label}”处理：{summary.process_count} 个文件。",
        f"预估可释放/转移空间：{_format_size(summary.reclaimable_bytes)}。",
        f"保护目录跳过：{summary.protected_skip_count} 个文件。",
        "",
        "风险提示：",
    ]
    if summary.warnings:
        lines.extend(f"- {warning}" for warning in summary.warnings)
    else:
        lines.append("- 未发现额外高风险项；仍建议核对目录和保留文件。")

    lines.extend(["", "涉及来源目录：", _limited_path_block(summary.affected_directories, directory_limit)])
    if summary.destination is not None:
        lines.extend(["", "目标目录：", str(summary.destination)])
        if summary.destination_free_bytes is not None:
            remaining = summary.destination_free_bytes - summary.reclaimable_bytes
            remaining_text = _format_size(max(0, remaining)) if remaining >= 0 else f"不足 {_format_size(abs(remaining))}"
            lines.append(f"目标磁盘可用空间：{_format_size(summary.destination_free_bytes)}。")
            lines.append(f"预计移动后剩余：{remaining_text}。")
    return "\n".join(lines)


def available_destination_free_bytes(destination: Path) -> int | None:
    target = Path(destination)
    while not target.exists():
        parent = target.parent
        if parent == target:
            return None
        target = parent
    try:
        return shutil.disk_usage(target).free
    except OSError:
        return None


def _is_trash_action(action_label: str) -> bool:
    return "回收站" in action_label or "trash" in action_label.lower()


def _limited_path_block(paths: tuple[Path, ...], limit: int) -> str:
    if not paths:
        return "-"
    visible = [str(path) for path in paths[:limit]]
    if len(paths) > limit:
        visible.append(f"... 另有 {len(paths) - limit} 个目录")
    return "\n".join(visible)


def _format_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"
