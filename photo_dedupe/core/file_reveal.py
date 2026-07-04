from __future__ import annotations

import subprocess
import sys
from pathlib import Path


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
