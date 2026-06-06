from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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


def category_label(category: str) -> str:
    labels = {
        "photo": "照片",
        "video": "视频",
        "document": "文档",
        "other": "其他",
        "mixed": "混合",
    }
    return labels.get(category, category)


def collect_files(source_root: Path, exclude_roots: set[Path] | None = None) -> list[Path]:
    if not source_root.exists():
        return []
    excluded = {normalize_path(root) for root in (exclude_roots or set())}

    def excluded_path(path: Path) -> bool:
        normalized = normalize_path(path)
        return any(normalized == root or root in normalized.parents for root in excluded)

    if source_root.is_file():
        return [] if excluded_path(source_root) else [source_root]
    return [
        path
        for path in source_root.rglob("*")
        if path.is_file() and not excluded_path(path)
    ]


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


def build_target_path(source: Path, targets: OrganizeTargetConfig) -> OrganizePreviewItem:
    category, reason = classify_file(source)
    target_root = target_root_for_category(targets, category)
    return OrganizePreviewItem(
        source=source,
        category=category,
        reason=reason,
        target=target_root / source.name,
    )


def build_organize_preview(
    roots: list[Path],
    targets: OrganizeTargetConfig,
    include_paths: set[Path] | None = None,
    exclude_roots: set[Path] | None = None,
) -> list[OrganizePreviewItem]:
    preview: list[OrganizePreviewItem] = []
    seen: set[Path] = set()
    for root in roots:
        for source in collect_files(root, exclude_roots=exclude_roots):
            if include_paths is not None and source not in include_paths:
                continue
            if source in seen:
                continue
            seen.add(source)
            preview.append(build_target_path(source, targets))
    preview.sort(key=lambda item: (item.category, str(item.source).lower()))
    return preview


def execute_organize_preview(
    preview: list[OrganizePreviewItem],
    database: AppDatabase | None = None,
) -> list[tuple[Path, Path]]:
    moved: list[tuple[Path, Path]] = []
    for item in preview:
        if not item.source.exists():
            continue
        item.target.parent.mkdir(parents=True, exist_ok=True)
        target = unique_target_path(item.target.parent, item.target.name)
        if item.source.resolve() == target.resolve():
            continue
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
