from __future__ import annotations

from dataclasses import dataclass

from photo_dedupe.core.models import OperationRecord


ACTION_ALL = "全部动作"
ACTION_MOVE = "移动"
ACTION_TRASH = "回收站"

STATUS_ALL = "全部状态"
STATUS_CAN_UNDO = "可撤销"
STATUS_UNDONE = "已撤销"
STATUS_NOT_UNDOABLE = "不可撤销"


@dataclass(frozen=True, slots=True)
class OperationFilterSummary:
    total_count: int
    visible_count: int
    move_count: int
    trash_count: int
    can_undo_count: int
    undone_count: int
    not_undoable_count: int


def operation_action_label(operation: OperationRecord) -> str:
    return ACTION_TRASH if operation.action == "trash" else ACTION_MOVE


def operation_status_label(operation: OperationRecord) -> str:
    if operation.is_undone:
        return STATUS_UNDONE
    if operation.action == "trash":
        return STATUS_NOT_UNDOABLE
    return STATUS_CAN_UNDO


def filter_operations(
    operations: list[OperationRecord],
    action_filter: str = ACTION_ALL,
    status_filter: str = STATUS_ALL,
    keyword: str = "",
) -> list[OperationRecord]:
    keyword = keyword.strip().lower()
    visible: list[OperationRecord] = []
    for operation in operations:
        if action_filter != ACTION_ALL and operation_action_label(operation) != action_filter:
            continue
        if status_filter != STATUS_ALL and operation_status_label(operation) != status_filter:
            continue
        if keyword:
            haystack = f"{operation.source_path}\n{operation.target_path}".lower()
            if keyword not in haystack:
                continue
        visible.append(operation)
    return visible


def summarize_operations(
    all_operations: list[OperationRecord],
    visible_operations: list[OperationRecord],
) -> OperationFilterSummary:
    return OperationFilterSummary(
        total_count=len(all_operations),
        visible_count=len(visible_operations),
        move_count=sum(1 for operation in visible_operations if operation.action == "move"),
        trash_count=sum(1 for operation in visible_operations if operation.action == "trash"),
        can_undo_count=sum(1 for operation in visible_operations if operation_status_label(operation) == STATUS_CAN_UNDO),
        undone_count=sum(1 for operation in visible_operations if operation_status_label(operation) == STATUS_UNDONE),
        not_undoable_count=sum(1 for operation in visible_operations if operation_status_label(operation) == STATUS_NOT_UNDOABLE),
    )


def format_operation_filter_summary(summary: OperationFilterSummary) -> str:
    return (
        f"当前显示 {summary.visible_count}/{summary.total_count} 条操作记录"
        f" | 移动 {summary.move_count}"
        f" | 回收站 {summary.trash_count}"
        f" | 可撤销 {summary.can_undo_count}"
        f" | 已撤销 {summary.undone_count}"
        f" | 不可撤销 {summary.not_undoable_count}"
    )
