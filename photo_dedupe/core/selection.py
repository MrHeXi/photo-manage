from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from photo_dedupe.core.models import DuplicateGroup, FileRecord


COPY_HINTS = ("copy", "duplicate", "wechat", "weixin", "副本", "复制", "重复", "拷贝", "缓存")


@dataclass(slots=True)
class KeepRuleResult:
    keep_records: list[FileRecord]
    reason: str


@dataclass(slots=True)
class ProcessingPlan:
    groups: list[DuplicateGroup]
    records_to_process: list[FileRecord]
    keep_count: int
    protected_skip_count: int
    reclaimable_bytes: int
    affected_directories: list[Path]
    invalid_groups: list[DuplicateGroup]


def normalize_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except OSError:
        return path.expanduser().absolute()


def is_under_any(path: Path, roots: set[Path]) -> bool:
    if not roots:
        return False
    normalized = normalize_path(path)
    for root in roots:
        normalized_root = normalize_path(root)
        if normalized == normalized_root or normalized_root in normalized.parents:
            return True
    return False


def choose_keep_records(
    records: list[FileRecord],
    rule_label: str,
    protected_roots: set[Path] | None = None,
) -> KeepRuleResult:
    if not records:
        return KeepRuleResult([], "没有可保留的文件")

    protected_roots = protected_roots or set()
    protected_records = [record for record in records if is_under_any(record.path, protected_roots)]
    if protected_records:
        return KeepRuleResult(protected_records, "保护目录内文件")

    if rule_label == "保留最新修改":
        return KeepRuleResult([max(records, key=lambda item: item.modified_at)], "修改时间最新")
    if rule_label == "保留最大文件":
        return KeepRuleResult([max(records, key=lambda item: item.size)], "文件最大")
    if rule_label == "保留最高分辨率":
        return KeepRuleResult(
            [max(records, key=lambda item: ((item.width or 0) * (item.height or 0), item.size, item.modified_at))],
            "图片分辨率最高",
        )
    if rule_label == "保留路径最短":
        return KeepRuleResult([min(records, key=lambda item: (len(item.path.parts), len(str(item.path)), -item.size))], "路径最短")
    if rule_label == "保留路径最深":
        return KeepRuleResult([max(records, key=lambda item: (len(item.path.parts), item.size, item.modified_at))], "路径最深")
    if rule_label == "保留非副本命名":
        clean_records = [
            record
            for record in records
            if not any(hint in record.name.lower() for hint in COPY_HINTS)
        ]
        candidates = clean_records or records
        reason = "文件名不像副本" if clean_records else "未找到明显非副本文件，回退到最高质量"
        return KeepRuleResult(
            [max(candidates, key=lambda item: ((item.width or 0) * (item.height or 0), item.size, item.modified_at))],
            reason,
        )

    return KeepRuleResult(
        [max(records, key=lambda item: ((item.width or 0) * (item.height or 0), item.size, item.modified_at))],
        "综合质量最高",
    )


def build_processing_plan(groups: list[DuplicateGroup], protected_roots: set[Path]) -> ProcessingPlan:
    records_to_process: list[FileRecord] = []
    keep_count = 0
    protected_skip_count = 0
    invalid_groups: list[DuplicateGroup] = []

    for group in groups:
        keep_records = [record for record in group.records if record.keep_selected]
        if not keep_records:
            invalid_groups.append(group)
            continue
        keep_count += len(keep_records)
        for record in group.records:
            if record.keep_selected:
                continue
            if is_under_any(record.path, protected_roots):
                protected_skip_count += 1
                continue
            records_to_process.append(record)

    affected_directories = sorted({record.path.parent for record in records_to_process}, key=lambda item: str(item).lower())
    reclaimable_bytes = sum(record.size for record in records_to_process)
    return ProcessingPlan(
        groups=groups,
        records_to_process=records_to_process,
        keep_count=keep_count,
        protected_skip_count=protected_skip_count,
        reclaimable_bytes=reclaimable_bytes,
        affected_directories=affected_directories,
        invalid_groups=invalid_groups,
    )
