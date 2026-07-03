from __future__ import annotations

from pathlib import Path

from photo_dedupe.core.diagnostics import app_data_dir


LOCK_FILE_NAME = "PhotoManageGPT.lock"


def default_lock_path(base: Path | None = None) -> Path:
    return app_data_dir(base) / LOCK_FILE_NAME
