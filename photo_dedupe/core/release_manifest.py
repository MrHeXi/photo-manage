from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from photo_dedupe.core.build_info import BUILD_INFO_FILENAME


MANIFEST_FILENAME = "RELEASE-MANIFEST.json"


def build_release_manifest(
    release_dir: Path,
    version: str,
    *,
    commit: str | None = None,
    build_time_utc: str | None = None,
) -> dict[str, Any]:
    root = release_dir.resolve()
    files = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: str(item.relative_to(root)).lower()):
        if path == root / MANIFEST_FILENAME:
            continue
        files.append(
            {
                "path": str(path.relative_to(root)).replace("\\", "/"),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )

    build_info: dict[str, Any] = {}
    info_path = root / BUILD_INFO_FILENAME
    if info_path.is_file():
        try:
            value = json.loads(info_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                build_info = {
                    key: str(value[key])
                    for key in ("version", "commit", "built_at_utc", "runtime", "python_version")
                    if value.get(key) is not None
                }
                if isinstance(value.get("dependencies"), dict):
                    build_info["dependencies"] = value["dependencies"]
        except (OSError, UnicodeError, json.JSONDecodeError):
            build_info = {}
    if commit is not None:
        build_info["commit"] = commit
    if build_time_utc is not None:
        build_info["built_at_utc"] = build_time_utc
    build_info.setdefault("version", version)

    return {
        "product": "Photo Manage GPT",
        "version": version,
        "build": build_info,
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


def verify_release_manifest(release_dir: Path) -> list[str]:
    """Verify file content and complete coverage; never follow a manifest path outside its root."""
    root = release_dir.resolve()
    try:
        manifest = json.loads((root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        records = manifest["files"]
        if not isinstance(records, list) or not records:
            return ["Manifest 文件列表为空或格式错误"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"Manifest 无法读取：{exc}"]
    problems: list[str] = []
    listed: set[str] = set()
    for record in records:
        try:
            relative = record["path"]
            if not isinstance(relative, str) or not relative or "\\" in relative:
                raise ValueError("文件路径格式错误")
            path = root / relative
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or Path(relative).is_absolute():
                raise ValueError("文件路径超出发布目录")
            key = relative.casefold()
            if key in listed:
                raise ValueError("重复文件路径")
            listed.add(key)
            if not path.is_file():
                problems.append(f"缺少文件：{relative}")
            elif path.stat().st_size != record["size"] or sha256_file(path) != record["sha256"]:
                problems.append(f"文件校验失败：{relative}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            problems.append(f"Manifest 条目无效：{exc}")
    actual = {
        path.relative_to(root).as_posix().casefold()
        for path in root.rglob("*") if path.is_file() and path != root / MANIFEST_FILENAME
    }
    unlisted = sorted(actual - listed)
    if unlisted:
        problems.append("Manifest 未记录文件：" + "、".join(unlisted[:5]))
    return problems
