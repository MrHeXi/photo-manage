from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from photo_dedupe.core.hashers import average_image_hash


def video_hash_available() -> bool:
    return shutil.which("ffmpeg") is not None


def average_video_hash(path: Path, sample_seconds: tuple[int, ...] = (1, 3, 5)) -> int | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None

    with tempfile.TemporaryDirectory() as directory:
        hashes: list[int] = []
        for second in sample_seconds:
            frame_path = Path(directory) / f"frame-{second}.jpg"
            command = [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"00:00:{second:02d}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                str(frame_path),
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if result.returncode != 0 or not frame_path.exists():
                continue
            hashed = average_image_hash(frame_path)
            if hashed is not None:
                hashes.append(hashed[0])
        if not hashes:
            return None
        return combine_hashes(hashes)


def combine_hashes(hashes: list[int]) -> int:
    value = 0
    for bit in range(64):
        votes = sum(1 for item in hashes if item & (1 << bit))
        if votes >= (len(hashes) / 2):
            value |= 1 << bit
    return value
