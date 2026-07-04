from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MANIFEST_FILENAME = "RELEASE-MANIFEST.json"


def build_release_manifest(release_dir: Path, version: str) -> dict[str, Any]:
    root = release_dir.resolve()
    files = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: str(item.relative_to(root)).lower()):
        if path.name == MANIFEST_FILENAME:
            continue
        files.append(
            {
                "path": str(path.relative_to(root)).replace("\\", "/"),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )

    return {
        "product": "Photo Manage GPT",
        "version": version,
        "platform": "win64",
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "entrypoints": {
            "application": "PhotoManageGPT.exe",
            "safe_mode": "PhotoManageGPT-safe-mode.cmd",
            "readme": "README.md",
        },
        "safety": [
            "默认不做永久删除。",
            "保护目录内文件不会被批量移动或回收。",
            "移动到回收站后需要在 Windows 回收站中还原。",
            "安全模式可跳过历史缓存加载。",
        ],
        "files": files,
    }


def write_release_manifest(release_dir: Path, version: str) -> Path:
    manifest = build_release_manifest(release_dir, version)
    target = release_dir / MANIFEST_FILENAME
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
