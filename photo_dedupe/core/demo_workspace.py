from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


DEMO_WORKSPACE_NAME = "PhotoManageGPT-demo"


@dataclass(frozen=True, slots=True)
class DemoWorkspaceSummary:
    root: Path
    scan_roots: tuple[Path, ...]
    protected_roots: tuple[Path, ...]
    file_count: int
    duplicate_pair_count: int
    total_bytes: int


def create_demo_workspace(parent_dir: Path, name: str = DEMO_WORKSPACE_NAME) -> DemoWorkspaceSummary:
    root = _unique_directory(parent_dir / name)
    library = root / "01-main-library"
    inbox = root / "02-inbox-and-backup"

    library_photos = library / "Photos" / "2026"
    library_docs = library / "Documents"
    inbox_phone = inbox / "Phone"
    inbox_backup = inbox / "Backup"
    inbox_cache = inbox / "ChatCache"

    for directory in (library_photos, library_docs, inbox_phone, inbox_backup, inbox_cache):
        directory.mkdir(parents=True, exist_ok=True)

    family = library_photos / "family-original.jpg"
    _write_image(family, (66, 135, 245))
    shutil.copy2(family, inbox_backup / "family-copy.jpg")
    shutil.copy2(family, inbox_cache / "wechat-family-cache.jpg")

    trip = inbox_phone / "trip-phone.jpg"
    _write_image(trip, (45, 164, 78))
    _write_image(inbox_phone / "trip-phone-brighter.jpg", (50, 170, 86))

    report = library_docs / "project-notes.txt"
    report.write_text(
        "Photo Manage GPT demo notes\nKeep the protected library copy and process the backup copy.\n",
        encoding="utf-8",
    )
    shutil.copy2(report, inbox_backup / "project-notes-copy.txt")

    (root / "README-demo.txt").write_text(
        "\n".join(
            [
                "Photo Manage GPT demo workspace",
                "",
                "Suggested flow:",
                "1. Scan both folders.",
                "2. Keep files in 01-main-library, which should be protected.",
                "3. Move or recycle duplicates from 02-inbox-and-backup.",
                "4. Check the operation report and undo from the operation history if needed.",
            ]
        ),
        encoding="utf-8",
    )

    files = [path for path in root.rglob("*") if path.is_file()]
    return DemoWorkspaceSummary(
        root=root,
        scan_roots=(library, inbox),
        protected_roots=(library,),
        file_count=len(files),
        duplicate_pair_count=3,
        total_bytes=sum(path.stat().st_size for path in files),
    )


def _unique_directory(preferred: Path) -> Path:
    preferred.parent.mkdir(parents=True, exist_ok=True)
    if not preferred.exists():
        return preferred
    for index in range(2, 1000):
        candidate = preferred.with_name(f"{preferred.name}-{index}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"无法创建唯一演示目录：{preferred}")


def _write_image(path: Path, color: tuple[int, int, int]) -> None:
    image = Image.new("RGB", (96, 72), color)
    image.save(path, format="JPEG", quality=92)
