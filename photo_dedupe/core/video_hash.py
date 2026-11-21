from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from photo_dedupe.core.hashers import average_image_hash


def video_hash_available() -> bool:
    return shutil.which("ffmpeg") is not None


def average_video_hash(
    path: Path, sample_seconds: tuple[int, ...] = (1, 3, 5), *,
    check_cancel: Callable[[], None] | None = None,
) -> int | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None

    with tempfile.TemporaryDirectory() as directory:
        hashes: list[int] = []
        for second in sample_seconds:
            if check_cancel:
                check_cancel()
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
            process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = time.monotonic() + 30
            try:
                while process.poll() is None:
                    if check_cancel:
                        check_cancel()
                    if time.monotonic() >= deadline:
                        break
                    try:
                        process.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
            if check_cancel:
                check_cancel()
            if process.returncode != 0 or not frame_path.exists():
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
