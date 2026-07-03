from __future__ import annotations

import logging
import os
import platform
import sys
import threading
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path


APP_DATA_DIR_NAME = "PhotoManageGPT"
LOG_FILE_NAME = "app.log"
LOGGER_NAME = "photo_dedupe"


@dataclass(frozen=True)
class DiagnosticItem:
    name: str
    status: str
    detail: str


def app_data_dir(base: Path | None = None) -> Path:
    if base is not None:
        root = Path(base)
    else:
        local_app_data = os.environ.get("LOCALAPPDATA")
        root = Path(local_app_data) if local_app_data else Path.home() / ".photo_manage_gpt"
    app_root = root / APP_DATA_DIR_NAME if root.name != APP_DATA_DIR_NAME else root
    app_root.mkdir(parents=True, exist_ok=True)
    return app_root


def default_log_path(base: Path | None = None) -> Path:
    log_dir = app_data_dir(base) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / LOG_FILE_NAME


def setup_logging(log_path: Path | None = None) -> Path:
    resolved_log_path = log_path or default_log_path()
    resolved_log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    for handler in list(root_logger.handlers):
        if getattr(handler, "_photo_manage_handler", False):
            root_logger.removeHandler(handler)
            handler.close()

    file_handler = RotatingFileHandler(
        resolved_log_path,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler._photo_manage_handler = True  # type: ignore[attr-defined]
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    root_logger.addHandler(file_handler)

    logging.getLogger(LOGGER_NAME).info(
        "Logging initialized: python=%s platform=%s executable=%s",
        sys.version.split()[0],
        platform.platform(),
        sys.executable,
    )
    return resolved_log_path


def install_exception_logging() -> None:
    original_excepthook = sys.excepthook

    def log_exception(exc_type, exc_value, exc_traceback):
        logging.getLogger(LOGGER_NAME).critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        original_excepthook(exc_type, exc_value, exc_traceback)

    sys.excepthook = log_exception

    if hasattr(threading, "excepthook"):
        original_threading_hook = threading.excepthook

        def log_thread_exception(args):
            logging.getLogger(LOGGER_NAME).critical(
                "Unhandled thread exception in %s",
                args.thread.name if args.thread else "unknown",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            original_threading_hook(args)

        threading.excepthook = log_thread_exception


def build_health_items(
    *,
    database_path: Path,
    log_path: Path,
    ffmpeg_available: bool,
    history_state: str,
    loaded_groups: int = 0,
    total_groups: int = 0,
) -> list[DiagnosticItem]:
    database_detail = str(database_path)
    if database_path.exists():
        database_status = "正常"
        database_detail = f"{database_detail} ({database_path.stat().st_size} bytes)"
    else:
        database_status = "未创建"

    log_detail = str(log_path)
    if log_path.exists():
        log_status = "正常"
        log_detail = f"{log_detail} ({log_path.stat().st_size} bytes)"
    else:
        log_status = "未创建"

    if total_groups:
        history_detail = f"{history_state}，已加载 {loaded_groups}/{total_groups} 组"
    else:
        history_detail = history_state

    return [
        DiagnosticItem("数据库", database_status, database_detail),
        DiagnosticItem("启动日志", log_status, log_detail),
        DiagnosticItem("视频指纹", "正常" if ffmpeg_available else "可选缺失", "已检测到 ffmpeg" if ffmpeg_available else "未检测到 ffmpeg，相似视频检测会跳过"),
        DiagnosticItem("历史缓存", "正常" if loaded_groups or total_groups else "待加载", history_detail),
        DiagnosticItem("运行环境", "正常", f"Python {sys.version.split()[0]} / {platform.platform()}"),
    ]
