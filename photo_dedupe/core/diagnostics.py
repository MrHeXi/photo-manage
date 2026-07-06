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
    app_version: str | None = None,
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

    items = [
        DiagnosticItem("数据库", database_status, database_detail),
        DiagnosticItem("启动日志", log_status, log_detail),
        DiagnosticItem("视频指纹", "正常" if ffmpeg_available else "可选缺失", "已检测到 ffmpeg" if ffmpeg_available else "未检测到 ffmpeg，相似视频检测会跳过"),
        DiagnosticItem("历史缓存", "正常" if loaded_groups or total_groups else "待加载", history_detail),
        DiagnosticItem(
            "后台残留排查",
            "提示",
            f"当前进程 PID {os.getpid()}。如果关闭窗口后任务管理器仍有 PhotoManageGPT.exe，可结束该进程后重启；若怀疑历史缓存导致卡住，请使用安全模式启动。",
        ),
        DiagnosticItem("运行环境", "正常", f"Python {sys.version.split()[0]} / {platform.platform()}"),
    ]
    if app_version:
        items.insert(0, DiagnosticItem("应用版本", "正常", app_version))
    return items


def format_health_report(items: list[DiagnosticItem]) -> str:
    lines = ["Photo Manage GPT 健康诊断报告", ""]
    for item in items:
        lines.append(f"{item.name}: {item.status}")
        lines.append(f"  {item.detail}")
    return "\n".join(lines) + "\n"


def build_support_diagnostic_report(
    *,
    app_version: str,
    database_path: Path,
    log_path: Path,
    ffmpeg_available: bool,
    history_state: str,
    safe_mode: bool = False,
    loaded_groups: int = 0,
    total_groups: int = 0,
    data_dir: Path | None = None,
    executable: str | None = None,
    cwd: Path | None = None,
    pid: int | None = None,
    frozen: bool | None = None,
) -> str:
    resolved_data_dir = data_dir or database_path.parent
    resolved_executable = executable or sys.executable
    resolved_cwd = cwd or Path.cwd()
    resolved_pid = os.getpid() if pid is None else pid
    resolved_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    health_items = build_health_items(
        database_path=database_path,
        log_path=log_path,
        ffmpeg_available=ffmpeg_available,
        history_state=history_state,
        app_version=app_version,
        loaded_groups=loaded_groups,
        total_groups=total_groups,
    )
    history_detail = f"{history_state}，已加载 {loaded_groups}/{total_groups} 组" if total_groups else history_state

    lines = [
        "Photo Manage GPT 诊断信息",
        "",
        "说明：这段信息用于反馈启动、缓存、扫描或打包问题，不包含 AI API Key。",
        "",
        "应用",
        f"- 版本：{app_version}",
        f"- 安全模式：{'是' if safe_mode else '否'}",
        f"- 打包运行：{'是' if resolved_frozen else '否'}",
        f"- 进程 PID：{resolved_pid}",
        "",
        "路径",
        f"- 程序：{resolved_executable}",
        f"- 当前目录：{resolved_cwd}",
        f"- 数据目录：{resolved_data_dir}",
        f"- 数据库：{database_path}",
        f"- 日志：{log_path}",
        "",
        "环境",
        f"- Python：{sys.version.split()[0]}",
        f"- 系统：{platform.platform()}",
        f"- ffmpeg：{'已检测到' if ffmpeg_available else '未检测到'}",
        "",
        "缓存",
        f"- 历史状态：{history_detail}",
        "",
        "健康检查",
    ]
    for item in health_items:
        lines.append(f"- {item.name}：{item.status}；{item.detail}")
    return "\n".join(lines) + "\n"
