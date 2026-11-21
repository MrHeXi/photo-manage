from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from photo_dedupe.core.file_ops import unique_target_path
from photo_dedupe.core.hashers import is_image
from photo_dedupe.core.hashers import is_video
from photo_dedupe.core.selection import is_under_any, normalize_path
from photo_dedupe.core.storage import AppDatabase


PHOTO_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
    ".raw",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".dng",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".wmv",
    ".m4v",
    ".mts",
    ".3gp",
}

DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".zip",
    ".7z",
    ".rar",
}


@dataclass(slots=True)
class OrganizeTargetConfig:
    photo_root: Path
    video_root: Path
    document_root: Path
    other_root: Path


@dataclass(slots=True)
class OrganizePreviewItem:
    source: Path
    category: str
    reason: str
    target: Path
    size: int | None = None
    modified_at: float | None = None


@dataclass(slots=True)
class OrganizePreviewSummary:
    file_count: int
    total_bytes: int
    category_counts: dict[str, int]
    source_directories: list[Path]
    target_directories: list[Path]
    missing_count: int
    already_in_target_count: int


@dataclass(slots=True)
class OrganizePreviewItemStatus:
    size: int
    state: str
    label: str
    will_move: bool


def category_label(category: str) -> str:
    labels = {
        "photo": "照片",
        "video": "视频",
        "document": "文档",
        "other": "其他",
        "mixed": "混合",
    }
    return labels.get(category, category)


class PreviewCancelled(Exception):
    """The preview was cancelled and must not replace the previous preview."""


def _iter_files(
    source_root: Path,
    exclude_roots: set[Path] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[Path]:
    if not source_root.exists():
        return
    excluded = {normalize_path(root) for root in (exclude_roots or set())}

    def excluded_path(path: Path) -> bool:
        normalized = normalize_path(path)
        return any(normalized == root or root in normalized.parents for root in excluded)

    def check_cancel() -> None:
        if should_cancel and should_cancel():
            raise PreviewCancelled("整理预览已取消")

    check_cancel()
    if excluded_path(source_root) or source_root.is_symlink():
        return
    if source_root.is_file():
        yield source_root
        return
    pending = [source_root]
    while pending:
        check_cancel()
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    check_cancel()
                    path = Path(entry.path)
                    if entry.is_symlink() or excluded_path(path):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        yield path
        except OSError:
            continue


def collect_files(source_root: Path, exclude_roots: set[Path] | None = None) -> list[Path]:
    return list(_iter_files(source_root, exclude_roots))


def image_taken_at(path: Path) -> datetime | None:
    if not is_image(path):
        return None
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(path) as image:
            exif = image.getexif()
    except Exception:
        return None

    for tag in (36867, 36868, 306):
        value = exif.get(tag)
        if not value:
            continue
        try:
            return datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            continue
    return None


def image_camera_model(path: Path) -> str | None:
    if not is_image(path):
        return None
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(path) as image:
            exif = image.getexif()
    except Exception:
        return None

    make = str(exif.get(271, "")).strip()
    model = str(exif.get(272, "")).strip()
    label = " ".join(part for part in (make, model) if part)
    return label or None


def safe_folder_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in " ._-" else "_" for char in value).strip()
    return cleaned or "Unknown"


def classify_file(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if is_image(path) or suffix in PHOTO_EXTENSIONS:
        return "photo", "图片格式"
    if is_video(path) or suffix in VIDEO_EXTENSIONS:
        return "video", "视频格式"
    if suffix in DOCUMENT_EXTENSIONS:
        return "document", "文档/压缩包格式"
    return "other", "未命中照片/视频/文档规则"


def classify_group_category(paths: list[Path]) -> str:
    categories = {classify_file(path)[0] for path in paths}
    if len(categories) == 1:
        return next(iter(categories))
    return "mixed"


def target_root_for_category(targets: OrganizeTargetConfig, category: str) -> Path:
    if category == "photo":
        return targets.photo_root
    if category == "video":
        return targets.video_root
    if category == "document":
        return targets.document_root
    return targets.other_root


def target_directory_for_rule(source: Path, target_root: Path, category: str, rule: str) -> tuple[Path, str]:
    if rule == "type":
        suffix = source.suffix.lower().lstrip(".") or "no-extension"
        return target_root / safe_folder_name(suffix), "按扩展名归档"

    if category != "photo":
        return target_root, "按文件分类归档"

    if rule in {"date_month", "date_day"}:
        taken_at = image_taken_at(source)
        if taken_at is None:
            try:
                taken_at = datetime.fromtimestamp(source.stat().st_mtime)
            except OSError:
                return target_root, "无法读取日期，保留在分类根目录"
        target_dir = target_root / f"{taken_at:%Y}" / f"{taken_at:%Y-%m}"
        if rule == "date_day":
            target_dir /= f"{taken_at:%Y-%m-%d}"
        return target_dir, "按拍摄日期归档" if rule == "date_day" else "按拍摄月份归档"

    if rule == "camera":
        camera = image_camera_model(source) or "Unknown Camera"
        return target_root / safe_folder_name(camera), "按相机型号归档"

    return target_root, "按文件分类归档"


def build_target_path(
    source: Path,
    targets: OrganizeTargetConfig,
    rule: str = "category",
) -> OrganizePreviewItem:
    category, reason = classify_file(source)
    target_root = target_root_for_category(targets, category)
    try:
        stat = source.stat()
        size, modified_at = stat.st_size, stat.st_mtime
    except OSError:
        size, modified_at = None, None
    if rule == "category":
        return OrganizePreviewItem(
            source=source,
            category=category,
            reason=reason,
            target=target_root / source.name,
            size=size,
            modified_at=modified_at,
        )
    target_dir, rule_reason = target_directory_for_rule(source, target_root, category, rule)
    return OrganizePreviewItem(
        source=source,
        category=category,
        reason=f"{reason}；{rule_reason}",
        target=target_dir / source.name,
        size=size,
        modified_at=modified_at,
    )


def build_organize_preview(
    roots: list[Path],
    targets: OrganizeTargetConfig,
    include_paths: set[Path] | None = None,
    exclude_roots: set[Path] | None = None,
    rule: str = "category",
    *,
    progress: Callable[[str, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[OrganizePreviewItem]:
    preview: list[OrganizePreviewItem] = []
    seen: set[Path] = set()
    included = {normalize_path(path) for path in include_paths} if include_paths is not None else None
    normalized_roots = sorted({normalize_path(root) for root in roots}, key=lambda path: (len(path.parts), str(path)))
    traversal_roots: list[Path] = []
    for root in normalized_roots:
        if not any(parent == root or parent in root.parents for parent in traversal_roots):
            traversal_roots.append(root)
    for root in traversal_roots:
        for source in _iter_files(root, exclude_roots=exclude_roots, should_cancel=should_cancel):
            normalized = normalize_path(source)
            if included is not None and normalized not in included:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            if progress:
                progress(f"正在生成整理预览：{source.name}", len(preview), 0)
            preview.append(build_target_path(source, targets, rule=rule))
    if should_cancel and should_cancel():
        raise PreviewCancelled("整理预览已取消")
    preview.sort(key=lambda item: (item.category, str(item.source).lower()))
    return preview


def summarize_organize_preview(preview: list[OrganizePreviewItem]) -> OrganizePreviewSummary:
    category_counts = {"photo": 0, "video": 0, "document": 0, "other": 0, "mixed": 0}
    source_directories: set[Path] = set()
    target_directories: set[Path] = set()
    total_bytes = 0
    missing_count = 0
    already_in_target_count = 0

    for item in preview:
        category_counts[item.category] = category_counts.get(item.category, 0) + 1
        source_directories.add(item.source.parent)
        target_directories.add(item.target.parent)

        status = describe_organize_preview_item(item)
        total_bytes += status.size
        if status.state in {"missing", "unreadable"}:
            missing_count += 1
        if status.state == "already_in_target":
            already_in_target_count += 1

    return OrganizePreviewSummary(
        file_count=len(preview),
        total_bytes=total_bytes,
        category_counts=category_counts,
        source_directories=sorted(source_directories, key=lambda item: str(item).lower()),
        target_directories=sorted(target_directories, key=lambda item: str(item).lower()),
        missing_count=missing_count,
        already_in_target_count=already_in_target_count,
    )


def describe_organize_preview_item(item: OrganizePreviewItem) -> OrganizePreviewItemStatus:
    try:
        if not item.source.is_file() or item.source.is_symlink():
            return OrganizePreviewItemStatus(0, "missing", "源文件不存在", False)
        stat = item.source.stat()
        size = stat.st_size
        if (
            item.size is not None and item.size != size
            or item.modified_at is not None and item.modified_at != stat.st_mtime
        ):
            return OrganizePreviewItemStatus(size, "changed", "文件已变化，请重新预览", False)
        if item.source.resolve() == item.target.resolve():
            return OrganizePreviewItemStatus(size, "already_in_target", "已在目标位置", False)
        if item.target.exists():
            return OrganizePreviewItemStatus(size, "target_conflict", "可移动，同名将改名", True)
        return OrganizePreviewItemStatus(size, "ready", "可移动", True)
    except OSError:
        return OrganizePreviewItemStatus(0, "unreadable", "无法读取，执行时跳过", False)


def execute_organize_preview(
    preview: list[OrganizePreviewItem],
    database: AppDatabase | None = None,
) -> list[tuple[Path, Path]]:
    moved: list[tuple[Path, Path]] = []
    for item in preview:
        if not item.source.exists():
            continue
        item.target.parent.mkdir(parents=True, exist_ok=True)
        if item.source.resolve() == item.target.resolve():
            continue
        target = unique_target_path(item.target.parent, item.target.name)
        shutil.move(str(item.source), str(target))
        if database is not None:
            database.log_move(item.source, target)
        moved.append((item.source, target))
    return moved


def move_file(source: Path, target_dir: Path) -> tuple[Path, Path] | None:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = unique_target_path(target_dir, source.name)
    if source.resolve() == target.resolve():
        return None
    shutil.move(str(source), str(target))
    return source, target


def organize_images_by_date(
    source_root: Path,
    destination_root: Path,
    granularity: str = "month",
    exclude_roots: set[Path] | None = None,
) -> list[tuple[Path, Path]]:
    moved: list[tuple[Path, Path]] = []
    excluded = set(exclude_roots or set())
    excluded.add(destination_root)
    for source in collect_files(source_root, exclude_roots=excluded):
        if not is_image(source):
            continue
        taken_at = image_taken_at(source)
        if taken_at is None:
            taken_at = datetime.fromtimestamp(source.stat().st_mtime)
        if granularity == "day":
            target_dir = destination_root / f"{taken_at:%Y}" / f"{taken_at:%Y-%m}" / f"{taken_at:%Y-%m-%d}"
        else:
            target_dir = destination_root / f"{taken_at:%Y}" / f"{taken_at:%Y-%m}"
        result = move_file(source, target_dir)
        if result:
            moved.append(result)
    return moved


def organize_by_type(
    source_root: Path,
    destination_root: Path,
    exclude_roots: set[Path] | None = None,
) -> list[tuple[Path, Path]]:
    moved: list[tuple[Path, Path]] = []
    excluded = set(exclude_roots or set())
    excluded.add(destination_root)
    for source in collect_files(source_root, exclude_roots=excluded):
        suffix = source.suffix.lower().lstrip(".") or "no-extension"
        result = move_file(source, destination_root / safe_folder_name(suffix))
        if result:
            moved.append(result)
    return moved


def organize_images_by_camera(
    source_root: Path,
    destination_root: Path,
    exclude_roots: set[Path] | None = None,
) -> list[tuple[Path, Path]]:
    moved: list[tuple[Path, Path]] = []
    excluded = set(exclude_roots or set())
    excluded.add(destination_root)
    for source in collect_files(source_root, exclude_roots=excluded):
        if not is_image(source):
            continue
        camera = image_camera_model(source) or "Unknown Camera"
        result = move_file(source, destination_root / safe_folder_name(camera))
        if result:
            moved.append(result)
    return moved


def organize_files(
    source_root: Path,
    destination_root: Path,
    rule: str,
    exclude_roots: set[Path] | None = None,
) -> list[tuple[Path, Path]]:
    if rule == "date_day":
        return organize_images_by_date(source_root, destination_root, granularity="day", exclude_roots=exclude_roots)
    if rule == "type":
        return organize_by_type(source_root, destination_root, exclude_roots=exclude_roots)
    if rule == "camera":
        return organize_images_by_camera(source_root, destination_root, exclude_roots=exclude_roots)
    return organize_images_by_date(source_root, destination_root, granularity="month", exclude_roots=exclude_roots)
