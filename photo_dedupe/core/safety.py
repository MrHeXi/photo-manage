from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from photo_dedupe.core.selection import normalize_path


SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

SYSTEM_DIRECTORY_NAMES = {
    "$recycle.bin",
    "boot",
    "program files",
    "program files (x86)",
    "programdata",
    "recovery",
    "system volume information",
    "windows",
}


@dataclass(frozen=True, slots=True)
class ScanRootIssue:
    path: Path
    severity: str
    title: str
    message: str

    @property
    def blocking(self) -> bool:
        return self.severity == SEVERITY_ERROR


def validate_scan_roots(
    roots: Iterable[Path],
    target_roots: Iterable[Path] | None = None,
) -> list[ScanRootIssue]:
    """Return user-facing warnings/errors for risky scan roots."""

    normalized_roots = _unique_paths(roots)
    normalized_targets = _unique_paths(target_roots or [])
    issues: list[ScanRootIssue] = []

    for root in normalized_roots:
        if _is_drive_or_filesystem_root(root):
            issues.append(
                ScanRootIssue(
                    path=root,
                    severity=SEVERITY_ERROR,
                    title="不建议扫描整盘根目录",
                    message="整盘扫描会非常慢，也容易把系统缓存、程序文件和整理目标一起扫进去。请改选照片、资料或工作目录。",
                )
            )

        system_root = _find_system_directory(root)
        if system_root is not None:
            issues.append(
                ScanRootIssue(
                    path=root,
                    severity=SEVERITY_ERROR,
                    title="不能扫描系统目录",
                    message=f"{system_root} 属于 Windows 或程序运行目录，扫描这里容易遇到权限错误并产生无意义结果。",
                )
            )

        if not root.exists():
            issues.append(
                ScanRootIssue(
                    path=root,
                    severity=SEVERITY_ERROR,
                    title="目录不存在或未连接",
                    message="这个目录当前不可访问。请先确认移动硬盘、网络盘或路径已经连接，再开始扫描。",
                )
            )
        elif not root.is_dir():
            issues.append(
                ScanRootIssue(
                    path=root,
                    severity=SEVERITY_ERROR,
                    title="扫描目标不是文件夹",
                    message="扫描目录必须是文件夹。请移除这个路径，改选它所在的文件夹或其他资料目录。",
                )
            )
        else:
            readable, is_empty = _directory_access(root)
            if not readable:
                issues.append(
                    ScanRootIssue(
                        path=root,
                        severity=SEVERITY_ERROR,
                        title="目录无法读取",
                        message="当前用户没有权限读取这个目录，或目录暂时不可用。请检查权限后再扫描。",
                    )
                )
            elif is_empty:
                issues.append(
                    ScanRootIssue(
                        path=root,
                        severity=SEVERITY_WARNING,
                        title="目录为空",
                        message="这个目录当前没有文件或子目录，扫描可能不会产生结果。",
                    )
                )

        for target in normalized_targets:
            if _same_or_under(root, target):
                issues.append(
                    ScanRootIssue(
                        path=root,
                        severity=SEVERITY_ERROR,
                        title="扫描目录位于处理目标目录内",
                        message=f"目标目录 {target} 用于接收移动/整理后的文件，继续扫描它会把已处理文件再次纳入结果。",
                    )
                )
            elif _same_or_under(target, root):
                issues.append(
                    ScanRootIssue(
                        path=root,
                        severity=SEVERITY_ERROR,
                        title="扫描目录包含处理目标目录",
                        message=f"扫描范围包含目标目录 {target}，处理后的文件可能在下次扫描里反复出现。",
                    )
                )

    for child, parent in _nested_root_pairs(normalized_roots):
        issues.append(
            ScanRootIssue(
                path=child,
                severity=SEVERITY_WARNING,
                title="扫描目录被父目录覆盖",
                message=f"{child} 已经包含在 {parent} 里，同时选择会重复遍历，建议只保留父目录或子目录之一。",
            )
        )

    return issues


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        normalized = normalize_path(path)
        key = _path_key(normalized)
        if key in seen:
            continue
        seen.add(key)
        unique.append(normalized)
    return unique


def _path_key(path: Path) -> str:
    text = os.path.normcase(str(normalize_path(path))).rstrip("\\/")
    return text or os.path.normcase(str(path))


def _same_or_under(path: Path, root: Path) -> bool:
    root_key = _path_key(root)
    if _path_key(path) == root_key:
        return True
    return any(_path_key(parent) == root_key for parent in normalize_path(path).parents)


def _is_drive_or_filesystem_root(path: Path) -> bool:
    normalized = normalize_path(path)
    return normalized.parent == normalized


def _directory_access(path: Path) -> tuple[bool, bool]:
    try:
        with os.scandir(path) as entries:
            return True, next(entries, None) is None
    except OSError:
        return False, False


def _known_system_roots() -> list[Path]:
    candidates = [
        os.environ.get("WINDIR"),
        os.environ.get("SystemRoot"),
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramData"),
        "C:/Windows",
        "C:/Program Files",
        "C:/Program Files (x86)",
        "C:/ProgramData",
    ]
    return _unique_paths(Path(item) for item in candidates if item)


def _find_system_directory(path: Path) -> Path | None:
    normalized = normalize_path(path)
    for system_root in _known_system_roots():
        if _same_or_under(normalized, system_root):
            return system_root

    for candidate in (normalized, *normalized.parents):
        if candidate.name.lower() in SYSTEM_DIRECTORY_NAMES and _is_drive_or_filesystem_root(candidate.parent):
            return candidate
    return None


def _nested_root_pairs(roots: list[Path]) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for child in roots:
        for parent in roots:
            if child == parent:
                continue
            if _same_or_under(child, parent):
                pairs.append((child, parent))
                break
    return pairs
