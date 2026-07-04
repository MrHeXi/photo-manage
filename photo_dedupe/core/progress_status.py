from __future__ import annotations


def progress_percent(current: int, total: int) -> int:
    if total <= 0:
        return 0
    clamped = max(0, min(current, total))
    return round((clamped / total) * 100)


def format_progress_message(message: str, current: int, total: int) -> str:
    if total <= 0:
        return f"{message}（处理中）"
    clamped = max(0, min(current, total))
    return f"{message}（{clamped}/{total}，{progress_percent(clamped, total)}%）"


def format_restore_status(
    loaded_groups: int,
    total_groups: int,
    phase: str = "正在加载历史扫描任务",
) -> str:
    if total_groups <= 0:
        return f"{phase}：正在读取缓存索引..."
    clamped = max(0, min(loaded_groups, total_groups))
    return f"{phase}：已加载 {clamped}/{total_groups} 组（{progress_percent(clamped, total_groups)}%）"


def format_scan_start(root_count: int) -> str:
    return f"正在扫描：准备读取 {root_count} 个目录..."
