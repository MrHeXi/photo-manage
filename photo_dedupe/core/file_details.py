from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from photo_dedupe.core.hashers import is_image
from photo_dedupe.core.models import FileRecord
from photo_dedupe.core.organizer import category_label, classify_file, image_camera_model, image_taken_at


@dataclass(frozen=True, slots=True)
class FilePreviewDetails:
    exists: bool
    category: str
    category_label: str
    size: int
    modified_at: float
    width: int | None
    height: int | None
    taken_at: datetime | None
    camera: str | None

    @property
    def megapixels(self) -> float | None:
        if not self.width or not self.height:
            return None
        return self.width * self.height / 1_000_000


def inspect_file_preview(record: FileRecord) -> FilePreviewDetails:
    category, _reason = classify_file(record.path)
    exists = record.path.exists()
    size = record.size
    modified_at = record.modified_at
    width = record.width
    height = record.height
    taken_at: datetime | None = None
    camera: str | None = None

    if exists:
        try:
            stat = record.path.stat()
            size = stat.st_size
            modified_at = stat.st_mtime
        except OSError:
            exists = False

    if exists and is_image(record.path):
        image_size = _read_image_size(record.path)
        if image_size is not None:
            width, height = image_size
        taken_at = image_taken_at(record.path)
        camera = image_camera_model(record.path)

    return FilePreviewDetails(
        exists=exists,
        category=category,
        category_label=category_label(category),
        size=size,
        modified_at=modified_at,
        width=width,
        height=height,
        taken_at=taken_at,
        camera=camera,
    )


def _read_image_size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None

    try:
        with Image.open(path) as image:
            return ImageOps.exif_transpose(image).size
    except Exception:
        return None
