from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from photo_dedupe.core.file_ops import unique_target_path
from photo_dedupe.core.hashers import is_image


def collect_files(source_root: Path) -> list[Path]:
    if not source_root.exists():
        return []
    return [source_root] if source_root.is_file() else [path for path in source_root.rglob("*") if path.is_file()]


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
) -> list[tuple[Path, Path]]:
    moved: list[tuple[Path, Path]] = []
    for source in collect_files(source_root):
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


def organize_by_type(source_root: Path, destination_root: Path) -> list[tuple[Path, Path]]:
    moved: list[tuple[Path, Path]] = []
    for source in collect_files(source_root):
        suffix = source.suffix.lower().lstrip(".") or "no-extension"
        result = move_file(source, destination_root / safe_folder_name(suffix))
        if result:
            moved.append(result)
    return moved


def organize_images_by_camera(source_root: Path, destination_root: Path) -> list[tuple[Path, Path]]:
    moved: list[tuple[Path, Path]] = []
    for source in collect_files(source_root):
        if not is_image(source):
            continue
        camera = image_camera_model(source) or "Unknown Camera"
        result = move_file(source, destination_root / safe_folder_name(camera))
        if result:
            moved.append(result)
    return moved


def organize_files(source_root: Path, destination_root: Path, rule: str) -> list[tuple[Path, Path]]:
    if rule == "date_day":
        return organize_images_by_date(source_root, destination_root, granularity="day")
    if rule == "type":
        return organize_by_type(source_root, destination_root)
    if rule == "camera":
        return organize_images_by_camera(source_root, destination_root)
    return organize_images_by_date(source_root, destination_root, granularity="month")
