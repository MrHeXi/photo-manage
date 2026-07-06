from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from photo_dedupe.core.models import OperationRecord


def build_reveal_command(path: Path, platform: str | None = None) -> list[str]:
    platform = platform or sys.platform
    target = path.expanduser().resolve(strict=False)

    if platform.startswith("win"):
        if target.exists() and target.is_dir():
            return ["explorer.exe", str(target)]
        return ["explorer.exe", f"/select,{target}"]

    if platform == "darwin":
        if target.exists() and target.is_dir():
            return ["open", str(target)]
        return ["open", "-R", str(target)]

    folder = target if target.exists() and target.is_dir() else target.parent
    return ["xdg-open", str(folder)]


def reveal_in_file_manager(path: Path) -> None:
    command = build_reveal_command(path)
    subprocess.Popen(command)


def operation_reveal_path(operation: OperationRecord, location: str = "current") -> Path:
    if location == "original":
        return operation.source_path.parent
    if location == "current" and operation.action == "move" and str(operation.target_path):
        return operation.target_path if operation.target_path.exists() else operation.target_path.parent
    if operation.source_path.exists():
        return operation.source_path
    return operation.source_path.parent
