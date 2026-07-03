from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from photo_dedupe.core.models import DuplicateGroup, FileRecord


COPY_HINTS = ("copy", "duplicate", "wechat", "weixin", "副本", "复制", "重复", "拷贝", "缓存")
LIBRARY_HINTS = ("photos", "photo", "picture", "pictures", "library", "album", "camera", "dcim", "照片", "图片", "相册", "资料库", "原图")
CACHE_HINTS = ("cache", "temp", "tmp", "download", "downloads", "wechat", "weixin", "qq", "缓存", "临时", "下载")


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


def _quality_key(record: FileRecord) -> tuple[int, int, float]:
    return ((record.width or 0) * (record.height or 0), record.size, record.modified_at)


def _path_text(record: FileRecord) -> str:
    return str(record.path).lower()


def _parent_text(record: FileRecord) -> str:
    return str(record.path.parent).lower()


def _drive_key(record: FileRecord) -> str:
    return (record.path.drive or record.path.anchor or (record.path.parts[0] if record.path.parts else "")).lower()


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
            [max(records, key=_quality_key)],
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
            [max(candidates, key=_quality_key)],
            reason,
        )
    if rule_label == "保留资料库目录":
        library_records = [
            record
            for record in records
            if any(hint in _parent_text(record) for hint in LIBRARY_HINTS)
        ]
        candidates = library_records or records
        reason = "路径像资料库/相册目录" if library_records else "未找到资料库目录，回退到最高质量"
        return KeepRuleResult([max(candidates, key=_quality_key)], reason)
    if rule_label == "保留非缓存目录":
        clean_records = [
            record
            for record in records
            if not any(hint in _parent_text(record) for hint in CACHE_HINTS)
        ]
        candidates = clean_records or records
        reason = "路径不像缓存/下载目录" if clean_records else "未找到非缓存目录，回退到最高质量"
        return KeepRuleResult([max(candidates, key=_quality_key)], reason)
    if rule_label == "保留同磁盘最多项":
        drive_counts: dict[str, int] = {}
        for record in records:
            drive_counts[_drive_key(record)] = drive_counts.get(_drive_key(record), 0) + 1
        preferred_drive = max(drive_counts, key=lambda drive: (drive_counts[drive], drive))
        drive_records = [record for record in records if _drive_key(record) == preferred_drive]
        return KeepRuleResult([max(drive_records, key=_quality_key)], "重复项最多的磁盘")

    return KeepRuleResult(
        [max(records, key=_quality_key)],
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
