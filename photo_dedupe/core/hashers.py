from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".wmv",
    ".m4v",
}


def sha256_file(
    path: Path, chunk_size: int = 1024 * 1024, *,
    check_cancel: Callable[[], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while True:
            if check_cancel:
                check_cancel()
            chunk = file.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_previewable(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def average_image_hash(path: Path) -> tuple[int, int | None, int | None] | None:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None

    try:
        with Image.open(path) as image:
            width, height = image.size
            gray = ImageOps.exif_transpose(image).convert("L").resize((8, 8))
            pixels = list(gray.tobytes())
    except Exception:
        return None

    avg = sum(pixels) / len(pixels)
    value = 0
    for index, pixel in enumerate(pixels):
        if pixel >= avg:
            value |= 1 << index
    return value, width, height


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()
