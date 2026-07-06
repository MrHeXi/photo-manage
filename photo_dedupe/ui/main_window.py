from __future__ import annotations

import os
import logging
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QLockFile, QThread, Qt, QTimer, Signal, QUrl
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from photo_dedupe import __version__
from photo_dedupe.core.ai_client import AiConfig, analyze_image, format_ai_config_validation, validate_ai_config
from photo_dedupe.core.diagnostics import (
    build_health_items,
    build_support_diagnostic_report,
    default_log_path,
    install_exception_logging,
    setup_logging,
)
from photo_dedupe.core.directory_index import FileDuplicateLink, build_directory_duplicate_index
from photo_dedupe.core.exporter import export_ai_analysis_csv, export_duplicate_report_html, export_files_csv, export_operations_csv, export_scan_sessions_csv
from photo_dedupe.core.file_details import FilePreviewDetails, inspect_file_preview
from photo_dedupe.core.file_ops import move_records, trash_records, undo_move
from photo_dedupe.core.file_reveal import operation_reveal_path, reveal_in_file_manager
from photo_dedupe.core.group_summary import DuplicateGroupSummary, summarize_duplicate_group
from photo_dedupe.core.hashers import is_image, is_video
from photo_dedupe.core.image_compare import ImageCompareItem, build_image_compare_items
from photo_dedupe.core.models import DuplicateGroup, FileRecord, OperationRecord
from photo_dedupe.core.organizer import (
    OrganizePreviewItem,
    OrganizePreviewSummary,
    OrganizeTargetConfig,
    build_organize_preview,
    category_label,
    classify_group_category,
    describe_organize_preview_item,
    execute_organize_preview,
    organize_files,
    summarize_organize_preview,
)
from photo_dedupe.core.organize_profiles import (
    PROFILE_SETTINGS_KEY,
    OrganizeProfile,
    build_template_profile,
    clean_profile_name,
    decode_profiles,
    encode_profiles,
    get_builtin_template,
    list_builtin_templates,
    profile_summary,
    remove_profile,
    upsert_profile,
)
from photo_dedupe.core.onboarding import OnboardingSnapshot, build_onboarding_steps, onboarding_primary_guidance, onboarding_summary
from photo_dedupe.core.operation_report import (
    build_organize_result_report,
    build_processing_result_report,
    format_operation_result_report,
)
from photo_dedupe.core.operation_filters import (
    ACTION_ALL,
    ACTION_MOVE,
    ACTION_TRASH,
    STATUS_ALL,
    STATUS_CAN_UNDO,
    STATUS_NOT_UNDOABLE,
    STATUS_UNDONE,
    filter_operations,
    format_operation_filter_summary,
    summarize_operations,
)
from photo_dedupe.core.progress_status import (
    format_progress_message,
    format_restore_status,
    format_scan_start,
)
from photo_dedupe.core.processing_risk import build_processing_risk_summary, format_processing_confirmation
from photo_dedupe.core.scanner import DuplicateScanner
from photo_dedupe.core.selection import (
    build_processing_plan,
    choose_keep_records,
    describe_keep_reasons,
    is_under_any,
    normalize_path,
)
from photo_dedupe.core.safety import SEVERITY_ERROR, ScanRootIssue, validate_scan_roots
from photo_dedupe.core.single_instance import default_lock_path
from photo_dedupe.core.storage import AppDatabase
from photo_dedupe.core.video_hash import video_hash_available


APP_NAME = "Photo Manage GPT"
APP_VERSION = __version__
APP_TITLE = f"{APP_NAME} v{APP_VERSION}"
APP_USER_MODEL_ID = "PhotoManageGPT.PhotoManageGPT.1"
EMPTY_SCAN_GUIDANCE = "暂无重复分组。添加扫描目录，必要时先设为保护目录，然后开始扫描。"


def app_asset_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / name


def load_app_icon() -> QIcon:
    for filename in ("app_icon.png", "app_icon.ico"):
        path = app_asset_path(filename)
        if path.exists():
            return QIcon(str(path))
    return QIcon()


def set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def format_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def group_category_color(category: str) -> QColor:
    palette = {
        "photo": QColor("#1f6feb"),
        "video": QColor("#bf8700"),
        "document": QColor("#0969da"),
        "other": QColor("#57606a"),
        "mixed": QColor("#8250df"),
    }
    return palette.get(category, QColor("#57606a"))


class ScanWorker(QThread):
    progress_changed = Signal(str, int, int)
    finished_groups = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        roots: list[Path],
        database_path: Path,
        similarity_threshold: int,
        same_ratio_only: bool = False,
        same_resolution_only: bool = False,
    ) -> None:
        super().__init__()
        self.roots = roots
        self.database_path = database_path
        self.similarity_threshold = similarity_threshold
        self.same_ratio_only = same_ratio_only
        self.same_resolution_only = same_resolution_only
        self._cancel_requested = False

    def request_cancel(self) -> None:
        self._cancel_requested = True

    def should_cancel(self) -> bool:
        return self._cancel_requested

    def run(self) -> None:
        database = AppDatabase(self.database_path)
        try:
            scanner = DuplicateScanner(database)
            groups = scanner.scan(
                self.roots,
                image_similarity_threshold=self.similarity_threshold,
                image_same_ratio_only=self.same_ratio_only,
                image_same_resolution_only=self.same_resolution_only,
                progress=self.progress_changed.emit,
                should_cancel=self.should_cancel,
            )
            self.finished_groups.emit(groups)
        except Exception as exc:
            logging.getLogger(__name__).exception("Scan worker failed")
            self.failed.emit(str(exc))
        finally:
            database.close()


class AiWorker(QThread):
    progress_changed = Signal(str, int, int)
    finished_analysis = Signal(object, object)
    finished_batch = Signal(int)
    failed = Signal(str)

    def __init__(self, paths: list[Path], config: AiConfig) -> None:
        super().__init__()
        self.paths = paths
        self.config = config

    def run(self) -> None:
        try:
            count = 0
            total = len(self.paths)
            for index, path in enumerate(self.paths, start=1):
                self.progress_changed.emit(f"AI 正在分析：{path.name}", index, total)
                result = analyze_image(path, self.config)
                self.finished_analysis.emit(path, result)
                count += 1
            self.finished_batch.emit(count)
        except Exception as exc:
            logging.getLogger(__name__).exception("AI worker failed")
            self.failed.emit(str(exc))


class RestoreSessionWorker(QThread):
    finished_restore = Signal(object, object)
    failed = Signal(str)

    def __init__(
        self,
        database_path: Path,
        session_id: int | None = None,
        offset_groups: int = 0,
        limit_groups: int = 500,
    ) -> None:
        super().__init__()
        self.database_path = database_path
        self.session_id = session_id
        self.offset_groups = offset_groups
        self.limit_groups = limit_groups

    def run(self) -> None:
        database = AppDatabase(self.database_path)
        try:
            session = database.get_latest_scan_session() if self.session_id is None else database.get_scan_session(self.session_id)
            if session is None:
                self.finished_restore.emit(
                    None,
                    {"groups": [], "total_groups": 0, "offset_groups": self.offset_groups, "limit_groups": self.limit_groups},
                )
                return
            total_groups = database.count_scan_session_groups(session.id)
            groups = database.load_scan_session_groups(
                session.id,
                limit_groups=self.limit_groups,
                offset_groups=self.offset_groups,
                limit_items_per_group=200,
            )
            self.finished_restore.emit(
                session,
                {
                    "groups": groups,
                    "total_groups": total_groups,
                    "offset_groups": self.offset_groups,
                    "limit_groups": self.limit_groups,
                },
            )
        except Exception as exc:
            logging.getLogger(__name__).exception("Restore session worker failed")
            self.failed.emit(str(exc))
        finally:
            database.close()


class MainWindow(QMainWindow):
    def __init__(self, safe_mode: bool = False) -> None:
        super().__init__()
        self.safe_mode = safe_mode
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(load_app_icon())
        self.resize(1280, 820)
        self.setMinimumSize(1100, 720)

        self.database = AppDatabase()
        self.groups: list[DuplicateGroup] = []
        self.current_roots: list[Path] = []
        self.worker: ScanWorker | None = None
        self.ai_worker: AiWorker | None = None
        self.restore_worker: RestoreSessionWorker | None = None
        self.operations_loaded = False
        self.history_loaded = False
        self.ai_loaded = False
        self.keep_state_dirty = False
        self.updating_keep_checks = False
        self.manual_keep_group_keys: set[str] = set()
        self.protected_roots: set[Path] = set()
        self.file_duplicate_links: dict[Path, FileDuplicateLink] = {}
        self.directory_duplicate_files: dict[Path, list[Path]] = {}
        self.restored_session_id: int | None = None
        self.restored_total_groups = 0
        self.restored_loaded_groups = 0
        self.restore_batch_size = 500
        self.scan_session_summaries = []
        self.destination = Path(self.database.get_setting("destination", str(self._default_destination())))
        self.organize_targets = OrganizeTargetConfig(
            photo_root=Path(self.database.get_setting("target_photo_root", str(self.destination / "Photos"))),
            video_root=Path(self.database.get_setting("target_video_root", str(self.destination / "Videos"))),
            document_root=Path(self.database.get_setting("target_document_root", str(self.destination / "Documents"))),
            other_root=Path(self.database.get_setting("target_other_root", str(self.destination / "Others"))),
        )
        self.organize_preview_items: list[OrganizePreviewItem] = []
        self.organize_profiles: list[OrganizeProfile] = []

        self.folder_list = QListWidget()
        self.group_list = QListWidget()
        self.group_mode_combo = QComboBox()
        self.group_type_filter_combo = QComboBox()
        self.group_sort_combo = QComboBox()
        self.profile_combo = QComboBox()
        self.template_combo = QComboBox()
        self.group_search_input = QLineEdit()
        self.group_min_size_input = QLineEdit()
        self.group_min_size_unit_combo = QComboBox()
        self.group_max_size_input = QLineEdit()
        self.group_max_size_unit_combo = QComboBox()
        self.load_more_groups_button = QPushButton("继续加载更多")
        self.group_stack = QStackedWidget()
        self.directory_tree = QTreeWidget()
        self.directory_detail_table = QTableWidget(0, 4)
        self.directory_hint_label = QLabel("")
        self.thumbnail_list = QListWidget()
        self.file_table = QTableWidget(0, 9)
        self.operations_table = QTableWidget(0, 6)
        self.operation_action_filter_combo = QComboBox()
        self.operation_status_filter_combo = QComboBox()
        self.operation_search_input = QLineEdit()
        self.session_table = QTableWidget(0, 7)
        self.history_table = QTableWidget(0, 7)
        self.ai_table = QTableWidget(0, 5)
        self.health_table = QTableWidget(0, 3)
        self.organize_preview_table = QTableWidget(0, 4)
        self.onboarding_table = QTableWidget(0, 4)
        self.action_combo = QComboBox()
        self.bulk_keep_rule_combo = QComboBox()
        self.bulk_keep_keyword_input = QLineEdit()
        self.organize_rule_combo = QComboBox()
        self.strategy_combo = QComboBox()
        self.ai_provider_combo = QComboBox()
        self.ai_score_filter_spin = QSpinBox()
        self.threshold_spin = QSpinBox()
        self.session_filter_input = QLineEdit()
        self.session_sort_combo = QComboBox()
        self.session_range_combo = QComboBox()
        self.organize_keep_only_checkbox = QCheckBox("仅整理重复组中的保留文件")
        self.same_ratio_checkbox = QCheckBox("仅同宽高比")
        self.same_resolution_checkbox = QCheckBox("仅同分辨率")
        self.rotate_hint_checkbox = QCheckBox("提示旋转/翻转相似图")
        self.organize_photo_input = QLineEdit()
        self.organize_video_input = QLineEdit()
        self.organize_document_input = QLineEdit()
        self.organize_other_input = QLineEdit()
        self.ai_base_url_input = QLineEdit()
        self.ai_model_input = QLineEdit()
        self.ai_key_input = QLineEdit()
        self.ai_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_show_key_checkbox = QCheckBox("显示 Key")
        self.ai_check_button = QPushButton("检查 AI 配置")
        self.ai_analyze_button = QPushButton("AI 判断当前图片")
        self.ai_batch_button = QPushButton("AI 判断当前分组")
        self.destination_label = QLabel("")
        self.group_status_label = QLabel("")
        self.operation_summary_label = QLabel("")
        self.session_summary_label = QLabel("")
        self.health_summary_label = QLabel("")
        self.health_data_dir_button = QPushButton("打开数据目录")
        self.health_log_dir_button = QPushButton("打开日志目录")
        self.health_diagnostic_text = QPlainTextEdit()
        self.organize_help_label = QLabel("")
        self.group_detail_summary_label = QLabel("")
        self.retention_hint_label = QLabel("勾选的文件会保留在原位置；保护目录内文件不会被批量移动或回收。")
        self.bulk_rule_hint_label = QLabel("")
        self.scan_button = QPushButton("开始扫描")
        self.cancel_button = QPushButton("停止扫描")
        self.preview_label = QLabel("选择图片以预览")
        self.video_widget = QVideoWidget()
        self.preview_stack = QStackedWidget()
        self.detail_label = QLabel("")
        self.organize_summary_label = QLabel("")
        self.onboarding_summary_label = QLabel("")
        self.onboarding_guidance_label = QLabel("")
        self.profile_summary_label = QLabel("")
        self.template_summary_label = QLabel("")
        self.status_label = QLabel("准备就绪")
        self.progress = QProgressBar()
        self.media_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.health_log_path = default_log_path()
        self.history_state = "正在加载最近一次扫描缓存"
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.setVideoOutput(self.video_widget)

        for list_widget in (self.folder_list, self.group_list, self.thumbnail_list):
            list_widget.setAlternatingRowColors(False)
            list_widget.setSpacing(3)

        self._build_ui()
        self._load_settings()
        self._apply_styles()
        self._update_destination_label()
        self.refresh_health_diagnostics()
        self.refresh_onboarding()
        if self.safe_mode:
            self.history_state = "安全模式：已跳过历史缓存"
            self.status_label.setText("安全模式已启用")
            self.group_status_label.setText(f"安全模式已跳过历史结果加载。\n{EMPTY_SCAN_GUIDANCE}")
            self.detail_label.setText("安全模式不会加载旧扫描缓存，适合排查启动卡住或历史数据异常。")
            self.refresh_health_diagnostics()
        else:
            self.status_label.setText("正在加载最近数据")
            QTimer.singleShot(0, self._finish_startup)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.media_player.stop()
        if self.worker and self.worker.isRunning():
            self.worker.request_cancel()
            self.worker.wait(3000)
        if self.ai_worker and self.ai_worker.isRunning():
            self.ai_worker.requestInterruption()
            self.ai_worker.wait(3000)
        if self.restore_worker and self.restore_worker.isRunning():
            self.restore_worker.wait(3000)
        self._save_settings()
        self.database.close()
        super().closeEvent(event)

    def _finish_startup(self) -> None:
        self.group_status_label.setText("正在尝试恢复最近一次扫描结果...")
        self.history_state = "正在加载最近一次扫描缓存"
        health_notes = ["正在加载最近一次扫描缓存"]
        if not video_hash_available():
            health_notes.append("未检测到 ffmpeg，相似视频检测会自动跳过")
        self.status_label.setText("；".join(health_notes))
        self.refresh_health_diagnostics()
        self.start_restore_session()

    def _folder_path_from_item(self, item: QListWidgetItem) -> Path:
        value = item.data(Qt.ItemDataRole.UserRole)
        return Path(str(value if value is not None else item.text().replace(" [保护]", "")))

    def _folder_paths(self) -> list[Path]:
        return [self._folder_path_from_item(self.folder_list.item(index)) for index in range(self.folder_list.count())]

    def _add_folder_item(self, path: Path, protected: bool | None = None) -> None:
        normalized = normalize_path(path)
        is_protected = normalized in {normalize_path(root) for root in self.protected_roots} if protected is None else protected
        if is_protected:
            self.protected_roots.add(normalized)
        item = QListWidgetItem(f"{path} [保护]" if is_protected else str(path))
        item.setData(Qt.ItemDataRole.UserRole, str(path))
        if is_protected:
            item.setForeground(QColor("#1a7f37"))
            item.setToolTip("保护目录：批量处理会跳过此目录内文件。")
        self.folder_list.addItem(item)

    def _refresh_folder_item_labels(self) -> None:
        protected = {normalize_path(path) for path in self.protected_roots}
        for index in range(self.folder_list.count()):
            item = self.folder_list.item(index)
            path = self._folder_path_from_item(item)
            is_protected = normalize_path(path) in protected
            item.setText(f"{path} [保护]" if is_protected else str(path))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setForeground(QColor("#1a7f37") if is_protected else QColor("#24292f"))
            item.setToolTip("保护目录：批量处理会跳过此目录内文件。" if is_protected else str(path))

    def _current_health_items(self):
        return build_health_items(
            database_path=self.database.path,
            log_path=self.health_log_path,
            ffmpeg_available=video_hash_available(),
            history_state=self.history_state,
            app_version=APP_VERSION,
            loaded_groups=self.restored_loaded_groups,
            total_groups=self.restored_total_groups,
        )

    def _current_support_report(self) -> str:
        return build_support_diagnostic_report(
            app_version=APP_VERSION,
            database_path=self.database.path,
            log_path=self.health_log_path,
            ffmpeg_available=video_hash_available(),
            history_state=self.history_state,
            safe_mode=self.safe_mode,
            loaded_groups=self.restored_loaded_groups,
            total_groups=self.restored_total_groups,
        )

    def refresh_health_diagnostics(self) -> None:
        items = self._current_health_items()
        self.health_summary_label.setText(
            f"启动日志：{self.health_log_path}\n遇到启动失败、后台残留或历史加载异常时，可复制下面的诊断信息用于反馈。"
        )
        self.health_diagnostic_text.setPlainText(self._current_support_report())
        self.health_table.setRowCount(len(items))
        for row, item in enumerate(items):
            status_cell = QTableWidgetItem(item.status)
            if item.status in {"正常"}:
                status_cell.setForeground(QColor("#1a7f37"))
            elif item.status in {"可选缺失", "未创建", "待加载", "提示"}:
                status_cell.setForeground(QColor("#bf8700"))
            else:
                status_cell.setForeground(QColor("#cf222e"))
            for column, value in enumerate((item.name, item.status, item.detail)):
                table_item = status_cell if column == 1 else QTableWidgetItem(value)
                self.health_table.setItem(row, column, table_item)
        self.health_table.resizeRowsToContents()

    def open_data_directory(self) -> None:
        target = self.database.path.parent
        try:
            target.mkdir(parents=True, exist_ok=True)
            reveal_in_file_manager(target)
        except Exception as exc:
            QMessageBox.warning(self, "无法打开数据目录", str(exc))
            return
        self.status_label.setText(f"已打开数据目录：{target}")

    def open_log_directory(self) -> None:
        target = self.health_log_path.parent
        try:
            target.mkdir(parents=True, exist_ok=True)
            reveal_in_file_manager(target)
        except Exception as exc:
            QMessageBox.warning(self, "无法打开日志目录", str(exc))
            return
        self.status_label.setText(f"已打开日志目录：{target}")

    def copy_health_diagnostics(self) -> None:
        report = self._current_support_report()
        QApplication.clipboard().setText(report)
        self.health_diagnostic_text.setPlainText(report)
        self.status_label.setText("已复制诊断信息")
        QMessageBox.information(self, "已复制", "诊断信息已复制到剪贴板。")

    def export_health_report(self) -> None:
        target, _ = QFileDialog.getSaveFileName(self, "导出诊断报告", "photo-manage-diagnostics.txt", "Text Files (*.txt)")
        if not target:
            return
        try:
            Path(target).write_text(self._current_support_report(), encoding="utf-8")
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出完成", target)

    def export_current_duplicate_report(self) -> None:
        groups, min_size_bytes, max_size_bytes = self._visible_groups()
        if min_size_bytes < 0 or max_size_bytes < 0:
            QMessageBox.information(self, "筛选条件无效", "可整理空间筛选请输入数字，例如 500 或 1.5。")
            return
        if not groups:
            QMessageBox.information(self, "没有可导出内容", "当前筛选下没有重复分组。")
            return
        target, _ = QFileDialog.getSaveFileName(self, "导出当前扫描报告", "duplicate-report.html", "HTML Files (*.html)")
        if not target:
            return
        try:
            export_duplicate_report_html(groups, Path(target), "Photo Manage GPT 当前重复报告")
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出完成", target)

    def start_restore_session(self, session_id: int | None = None, offset_groups: int = 0) -> None:
        if self.restore_worker and self.restore_worker.isRunning():
            return
        self.load_more_groups_button.setEnabled(False)
        status = format_restore_status(offset_groups, self.restored_total_groups)
        self.status_label.setText(status)
        self.group_status_label.setText(status)
        self.progress.setMaximum(max(self.restored_total_groups, 1))
        self.progress.setValue(min(offset_groups, max(self.restored_total_groups, 1)))
        self.history_state = status
        self.refresh_health_diagnostics()
        self.restore_worker = RestoreSessionWorker(
            self.database.path,
            session_id=session_id if session_id is not None else self.restored_session_id,
            offset_groups=offset_groups,
            limit_groups=self.restore_batch_size,
        )
        self.restore_worker.finished_restore.connect(self.on_restore_finished)
        self.restore_worker.failed.connect(self.on_restore_failed)
        self.restore_worker.start()

    def load_more_restored_groups(self) -> None:
        if self.restored_session_id is None:
            return
        if self.restored_loaded_groups >= self.restored_total_groups:
            self.load_more_groups_button.setEnabled(False)
            return
        status = format_restore_status(self.restored_loaded_groups, self.restored_total_groups, "正在继续加载已保存结果")
        self.status_label.setText(status)
        self.group_status_label.setText(status)
        self.history_state = status
        self.refresh_health_diagnostics()
        self.start_restore_session(session_id=self.restored_session_id, offset_groups=self.restored_loaded_groups)

    def on_restore_failed(self, message: str) -> None:
        self.status_label.setText("启动恢复失败")
        self.group_status_label.setText(f"历史结果加载失败：{message}")
        self.history_state = f"历史结果加载失败：{message}"
        self.refresh_health_diagnostics()

    def on_restore_finished(self, session, payload) -> None:  # type: ignore[override]
        if session is None:
            self.status_label.setText("准备就绪")
            self.group_status_label.setText(f"当前没有可恢复的扫描结果。\n{EMPTY_SCAN_GUIDANCE}")
            self.history_state = "当前没有可恢复的扫描结果"
            self.load_more_groups_button.setEnabled(False)
            self.refresh_health_diagnostics()
            return

        groups = payload["groups"]
        total_groups = int(payload["total_groups"])
        offset_groups = int(payload.get("offset_groups", 0))
        self.current_roots = session.root_paths
        self.restored_session_id = session.id
        self.restored_total_groups = total_groups

        existing = {str(path) for path in self._folder_paths()}
        for root in session.root_paths:
            root_text = str(root)
            if root_text not in existing:
                self._add_folder_item(root)
                existing.add(root_text)

        if groups:
            merged = groups if offset_groups == 0 else [*self.groups, *groups]
            self.on_scan_finished(merged, restored=True)
            self.restored_loaded_groups = min(offset_groups + len(groups), total_groups)
            self.progress.setMaximum(max(total_groups, 1))
            self.progress.setValue(self.restored_loaded_groups)
            if self.restored_loaded_groups < total_groups:
                self.load_more_groups_button.setEnabled(True)
                self.load_more_groups_button.setText(
                    f"继续加载更多 ({self.restored_loaded_groups}/{self.restored_total_groups})"
                )
                status = format_restore_status(self.restored_loaded_groups, self.restored_total_groups, "已恢复部分历史结果")
                self.history_state = status
                self.group_status_label.setText(f"{status}，可继续加载更多。")
                self.status_label.setText(f"{status}，无需重新扫描。")
            else:
                self.load_more_groups_button.setEnabled(False)
                self.load_more_groups_button.setText("已全部加载")
                status = format_restore_status(self.restored_loaded_groups, self.restored_total_groups, "已恢复全部历史结果")
                self.history_state = status
                self.group_status_label.setText(status)
                self.status_label.setText(status)
        else:
            self.status_label.setText("准备就绪")
            self.group_status_label.setText(f"当前没有可恢复的扫描结果。\n{EMPTY_SCAN_GUIDANCE}")
            self.history_state = "当前没有可恢复的扫描结果"
            self.load_more_groups_button.setEnabled(False)
        self.refresh_health_diagnostics()
        self.refresh_onboarding()

    def _default_destination(self) -> Path:
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
        return desktop / "PhotoManageGPT_重复文件"

    def _build_ui(self) -> None:
        add_button = QPushButton("添加文件夹")
        add_button.clicked.connect(self.add_folder)
        remove_button = QPushButton("移除")
        remove_button.clicked.connect(self.remove_folder)
        protect_button = QPushButton("设为保护目录")
        protect_button.clicked.connect(self.toggle_protected_folder)
        dest_button = QPushButton("目标目录")
        dest_button.clicked.connect(self.choose_destination)
        self.scan_button.clicked.connect(self.start_scan)
        self.cancel_button.clicked.connect(self.cancel_scan)
        self.cancel_button.setEnabled(False)
        move_button = QPushButton("处理当前筛选结果")
        move_button.clicked.connect(self.move_unchecked)
        reveal_button = QPushButton("打开所在文件夹")
        reveal_button.clicked.connect(self.reveal_selected_file)
        compare_images_button = QPushButton("图片对比")
        compare_images_button.clicked.connect(self.compare_current_group_images)
        self.action_combo.addItems(["移动到目标目录", "移动到回收站"])
        self.bulk_keep_rule_combo.addItems(
            [
                "保留保护目录内文件",
                "保留最高质量",
                "保留最大文件",
                "保留最新修改",
                "保留最高分辨率",
                "保留路径最短",
                "保留路径最深",
                "保留非副本命名",
                "保留资料库目录",
                "保留非缓存目录",
                "保留同磁盘最多项",
                "保留路径包含关键词",
                "保留文件名包含关键词",
                "保留路径不含关键词",
            ]
        )
        self.bulk_keep_keyword_input.setPlaceholderText("关键词，例如 相册、原图、工作、backup")
        self.organize_rule_combo.addItems(["按拍摄年月", "按拍摄日期", "按文件类型", "按相机型号"])
        organize_button = QPushButton("执行整理规则")
        organize_button.clicked.connect(self.organize_by_rule)
        refresh_ops_button = QPushButton("刷新记录")
        refresh_ops_button.clicked.connect(self.refresh_operations)
        undo_button = QPushButton("撤销选中移动")
        undo_button.clicked.connect(self.undo_selected_operation)
        reveal_current_op_button = QPushButton("打开当前位置")
        reveal_current_op_button.clicked.connect(lambda _checked=False: self.reveal_selected_operation("current"))
        reveal_original_op_button = QPushButton("打开原目录")
        reveal_original_op_button.clicked.connect(lambda _checked=False: self.reveal_selected_operation("original"))
        export_ops_button = QPushButton("导出操作记录")
        export_ops_button.clicked.connect(self.export_operations)
        self.operation_action_filter_combo.addItems([ACTION_ALL, ACTION_MOVE, ACTION_TRASH])
        self.operation_action_filter_combo.currentIndexChanged.connect(self.refresh_operations)
        self.operation_status_filter_combo.addItems([STATUS_ALL, STATUS_CAN_UNDO, STATUS_UNDONE, STATUS_NOT_UNDOABLE])
        self.operation_status_filter_combo.currentIndexChanged.connect(self.refresh_operations)
        self.operation_search_input.setPlaceholderText("按原路径或当前位置筛选")
        self.operation_search_input.textChanged.connect(self.refresh_operations)
        refresh_history_button = QPushButton("刷新历史")
        refresh_history_button.clicked.connect(self.refresh_history)
        clear_history_button = QPushButton("清空扫描历史")
        clear_history_button.clicked.connect(self.clear_history)
        export_sessions_button = QPushButton("导出任务记录")
        export_sessions_button.clicked.connect(self.export_scan_sessions)
        export_history_button = QPushButton("导出文件缓存")
        export_history_button.clicked.connect(self.export_history)
        save_ai_button = QPushButton("保存 AI 配置")
        save_ai_button.clicked.connect(self.save_ai_settings)
        self.ai_show_key_checkbox.stateChanged.connect(
            lambda state: self.ai_key_input.setEchoMode(
                QLineEdit.EchoMode.Normal if state else QLineEdit.EchoMode.Password
            )
        )
        self.ai_check_button.clicked.connect(self.check_ai_settings)
        self.ai_analyze_button.clicked.connect(self.analyze_selected_image)
        self.ai_batch_button.clicked.connect(self.analyze_current_group)
        export_ai_button = QPushButton("导出 AI 结果")
        export_ai_button.clicked.connect(self.export_ai_results)
        filter_ai_button = QPushButton("筛选低分")
        filter_ai_button.clicked.connect(self.refresh_ai_results)
        refresh_health_button = QPushButton("刷新诊断")
        refresh_health_button.clicked.connect(self.refresh_health_diagnostics)
        export_health_button = QPushButton("导出诊断报告")
        export_health_button.clicked.connect(self.export_health_report)
        copy_health_button = QPushButton("复制诊断信息")
        copy_health_button.clicked.connect(self.copy_health_diagnostics)
        self.health_data_dir_button.clicked.connect(self.open_data_directory)
        self.health_log_dir_button.clicked.connect(self.open_log_directory)
        save_profile_button = QPushButton("保存方案")
        save_profile_button.clicked.connect(self.save_current_profile)
        load_profile_button = QPushButton("加载方案")
        load_profile_button.clicked.connect(self.load_selected_profile)
        delete_profile_button = QPushButton("删除方案")
        delete_profile_button.clicked.connect(self.delete_selected_profile)
        apply_template_button = QPushButton("套用模板")
        apply_template_button.clicked.connect(self.apply_selected_template)
        guide_add_folder_button = QPushButton("添加文件夹")
        guide_add_folder_button.clicked.connect(self.guide_add_folder)
        guide_protect_button = QPushButton("设为保护目录")
        guide_protect_button.clicked.connect(self.guide_mark_protected_folder)
        guide_scan_button = QPushButton("开始扫描")
        guide_scan_button.clicked.connect(self.start_scan)
        guide_rule_button = QPushButton("应用保留规则")
        guide_rule_button.clicked.connect(self.guide_apply_keep_rule)
        guide_preview_button = QPushButton("预览整理结果")
        guide_preview_button.clicked.connect(self.guide_preview_organize)
        guide_profile_button = QPushButton("保存方案")
        guide_profile_button.clicked.connect(self.save_current_profile)
        guide_refresh_button = QPushButton("刷新向导")
        guide_refresh_button.clicked.connect(self.refresh_onboarding)

        self.ai_provider_combo.addItems(["自定义", "OpenAI", "DashScope", "DeepSeek", "Moonshot", "Zhipu"])
        self.ai_provider_combo.currentTextChanged.connect(self.apply_ai_provider_preset)
        self.ai_base_url_input.setPlaceholderText("https://api.example.com/v1")
        self.ai_model_input.setPlaceholderText("模型名，例如 gpt-4o-mini")
        self.ai_key_input.setPlaceholderText("API Key")
        self.ai_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_score_filter_spin.setRange(0, 100)
        self.ai_score_filter_spin.setValue(100)
        self.ai_score_filter_spin.setSuffix(" 分及以下")

        self.strategy_combo.addItems(["保留最高质量", "保留最新修改", "保留最大文件"])
        self.strategy_combo.currentIndexChanged.connect(self.reapply_keep_strategy)
        self.strategy_combo.currentIndexChanged.connect(lambda _index: self._save_settings())
        self.threshold_spin.setRange(1, 24)
        self.threshold_spin.setValue(8)
        self.threshold_spin.setSuffix(" 位")
        self.threshold_spin.setToolTip("值越大，相似图片匹配越宽松。建议 6-10。")
        self.threshold_spin.valueChanged.connect(lambda _value: self._save_settings())
        self.profile_combo.currentIndexChanged.connect(self.on_profile_selection_changed)
        self.template_combo.addItem("选择内置模板", None)
        for template in list_builtin_templates():
            self.template_combo.addItem(template.name, template.key)
            self.template_combo.setItemData(self.template_combo.count() - 1, template.description, Qt.ItemDataRole.ToolTipRole)
        self.template_combo.currentIndexChanged.connect(self.on_template_selection_changed)

        folder_buttons = QHBoxLayout()
        folder_buttons.addWidget(add_button)
        folder_buttons.addWidget(remove_button)
        folder_buttons.addWidget(protect_button)
        profile_buttons = QHBoxLayout()
        profile_buttons.addWidget(save_profile_button)
        profile_buttons.addWidget(load_profile_button)
        profile_buttons.addWidget(delete_profile_button)
        template_buttons = QHBoxLayout()
        template_buttons.addWidget(apply_template_button)

        left = QWidget()
        left.setObjectName("Sidebar")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(16, 16, 16, 16)
        left_layout.setSpacing(8)
        left_layout.addWidget(QLabel("扫描目录"))
        left_layout.addWidget(self.folder_list)
        left_layout.addLayout(folder_buttons)
        self.profile_summary_label.setWordWrap(True)
        self.profile_summary_label.setText("保存常用扫描目录、保护目录、保留规则和整理目标。")
        self.template_summary_label.setWordWrap(True)
        self.template_summary_label.setText("选择一个常见场景后套用，不会移动或删除文件。")
        left_layout.addWidget(QLabel("内置模板"))
        left_layout.addWidget(self.template_combo)
        left_layout.addLayout(template_buttons)
        left_layout.addWidget(self.template_summary_label)
        left_layout.addWidget(QLabel("整理方案"))
        left_layout.addWidget(self.profile_combo)
        left_layout.addLayout(profile_buttons)
        left_layout.addWidget(self.profile_summary_label)
        left_layout.addWidget(dest_button)
        left_layout.addWidget(self.destination_label)
        left_layout.addWidget(QLabel("图片相似阈值"))
        left_layout.addWidget(self.threshold_spin)
        left_layout.addWidget(self.same_ratio_checkbox)
        left_layout.addWidget(self.same_resolution_checkbox)
        left_layout.addWidget(self.rotate_hint_checkbox)
        left_layout.addWidget(QLabel("默认保留策略"))
        left_layout.addWidget(self.strategy_combo)
        left_layout.addWidget(self.scan_button)
        left_layout.addWidget(self.cancel_button)
        left_layout.addWidget(QLabel("整理规则"))
        left_layout.addWidget(self.organize_rule_combo)
        left_layout.addWidget(organize_button)
        left_layout.addWidget(self.progress)
        left_layout.addWidget(self.status_label)
        left_layout.addStretch(1)

        self.group_mode_combo.addItems(["重复分组", "磁盘目录"])
        self.group_mode_combo.currentIndexChanged.connect(self.on_group_mode_changed)
        self.group_type_filter_combo.addItems(["全部类型", "照片", "视频", "文档", "其他", "混合"])
        self.group_type_filter_combo.currentIndexChanged.connect(self.refresh_group_list)
        self.group_sort_combo.addItems(["按可整理空间：从大到小", "按文件数：从多到少", "按扫描顺序"])
        self.group_sort_combo.currentIndexChanged.connect(self.refresh_group_list)
        self.group_search_input.setPlaceholderText("搜索分组标题、文件名、路径")
        self.group_search_input.textChanged.connect(self.refresh_group_list)
        self.group_min_size_input.setPlaceholderText("最小可整理空间")
        self.group_min_size_input.textChanged.connect(self.refresh_group_list)
        self.group_min_size_unit_combo.addItems(["MB", "GB"])
        self.group_min_size_unit_combo.currentIndexChanged.connect(self.refresh_group_list)
        self.group_max_size_input.setPlaceholderText("最大可整理空间")
        self.group_max_size_input.textChanged.connect(self.refresh_group_list)
        self.group_max_size_unit_combo.addItems(["MB", "GB"])
        self.group_max_size_unit_combo.currentIndexChanged.connect(self.refresh_group_list)
        clear_group_filters_button = QPushButton("清空分组筛选")
        clear_group_filters_button.clicked.connect(self.clear_group_filters)
        export_report_button = QPushButton("导出当前报告")
        export_report_button.clicked.connect(self.export_current_duplicate_report)
        self.load_more_groups_button.clicked.connect(self.load_more_restored_groups)
        self.load_more_groups_button.setEnabled(False)
        self.group_list.currentItemChanged.connect(self.populate_group)
        self.directory_tree.setHeaderLabels(["磁盘 / 目录 / 文件"])
        self.directory_tree.itemSelectionChanged.connect(self.on_directory_tree_selection_changed)
        self.directory_detail_table.setHorizontalHeaderLabels(["文件", "当前目录", "关联目录", "关联分组"])
        self.directory_detail_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.directory_detail_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.directory_detail_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.directory_detail_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.directory_detail_table.setAlternatingRowColors(True)
        self.directory_detail_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.directory_detail_table.verticalHeader().setDefaultSectionSize(30)
        self.directory_detail_table.itemSelectionChanged.connect(self.on_directory_detail_selection_changed)
        self.directory_hint_label.setWordWrap(True)
        self.directory_hint_label.setText("按磁盘和目录浏览参与重复的文件，右侧仍会显示原分组明细。")
        directory_locate_button = QPushButton("定位首个分组")
        directory_locate_button.clicked.connect(self.locate_first_group_from_directory_selection)
        directory_preview_button = QPushButton("加入整理预览")
        directory_preview_button.clicked.connect(self.preview_selected_directory_paths)

        directory_panel = QWidget()
        directory_layout = QVBoxLayout(directory_panel)
        directory_layout.setContentsMargins(0, 0, 0, 0)
        directory_layout.setSpacing(8)
        directory_layout.addWidget(self.directory_tree, 2)
        directory_actions = QHBoxLayout()
        directory_actions.addWidget(directory_locate_button)
        directory_actions.addWidget(directory_preview_button)
        directory_layout.addLayout(directory_actions)
        directory_layout.addWidget(self.directory_hint_label)
        directory_layout.addWidget(self.directory_detail_table, 1)

        self.group_stack.addWidget(self.group_list)
        self.group_stack.addWidget(directory_panel)
        groups_panel = QWidget()
        groups_panel.setObjectName("GroupsPanel")
        groups_layout = QVBoxLayout(groups_panel)
        groups_layout.setContentsMargins(16, 16, 16, 16)
        groups_layout.setSpacing(8)
        groups_layout.addWidget(QLabel("重复分组"))
        self.group_status_label.setWordWrap(True)
        self.group_status_label.setText("正在准备扫描结果...")
        groups_layout.addWidget(self.group_status_label)
        groups_layout.addWidget(self.group_mode_combo)
        groups_layout.addWidget(self.group_type_filter_combo)
        groups_layout.addWidget(self.group_sort_combo)
        groups_layout.addWidget(self.group_search_input)
        group_size_filter_row = QHBoxLayout()
        group_size_filter_row.addWidget(QLabel("最小可整理空间"))
        group_size_filter_row.addWidget(self.group_min_size_input)
        group_size_filter_row.addWidget(self.group_min_size_unit_combo)
        group_size_filter_row.addWidget(QLabel("最大可整理空间"))
        group_size_filter_row.addWidget(self.group_max_size_input)
        group_size_filter_row.addWidget(self.group_max_size_unit_combo)
        groups_layout.addLayout(group_size_filter_row)
        groups_layout.addWidget(clear_group_filters_button)
        groups_layout.addWidget(export_report_button)
        groups_layout.addWidget(self.load_more_groups_button)
        groups_layout.addWidget(self.group_stack)

        self.file_table.setHorizontalHeaderLabels(["保留", "保护", "文件名", "大小", "修改时间", "尺寸", "相似度", "建议", "路径"])
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        self.file_table.setAlternatingRowColors(True)
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.verticalHeader().setDefaultSectionSize(30)
        self.file_table.itemSelectionChanged.connect(self.preview_selected_file)
        self.file_table.itemChanged.connect(self.on_keep_item_changed)

        self.thumbnail_list.setViewMode(QListView.ViewMode.IconMode)
        self.thumbnail_list.setIconSize(QPixmap(128, 128).size())
        self.thumbnail_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.thumbnail_list.setMovement(QListView.Movement.Static)
        self.thumbnail_list.setMaximumHeight(180)
        self.thumbnail_list.itemClicked.connect(self.select_thumbnail_record)

        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(280)
        self.preview_label.setScaledContents(False)
        self.video_widget.setMinimumHeight(280)
        self.preview_stack.addWidget(self.preview_label)
        self.preview_stack.addWidget(self.video_widget)
        self.detail_label.setWordWrap(True)

        right = QWidget()
        right.setObjectName("ContentPanel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(14, 14, 14, 14)
        right_layout.setSpacing(10)
        self.group_detail_summary_label.setWordWrap(True)
        right_layout.addWidget(self.group_detail_summary_label)
        right_layout.addWidget(self.file_table)
        right_layout.addWidget(self.thumbnail_list)
        right_layout.addWidget(self.retention_hint_label)
        self.bulk_rule_hint_label.setWordWrap(True)
        right_layout.addWidget(QLabel("批量保留规则"))
        right_layout.addWidget(self.bulk_keep_rule_combo)
        right_layout.addWidget(self.bulk_keep_keyword_input)
        bulk_rule_actions = QHBoxLayout()
        preview_rule_button = QPushButton("预览应用规则")
        preview_rule_button.clicked.connect(self.preview_bulk_keep_rule)
        apply_rule_button = QPushButton("应用到当前筛选结果")
        apply_rule_button.clicked.connect(self.apply_bulk_keep_rule)
        bulk_rule_actions.addWidget(preview_rule_button)
        bulk_rule_actions.addWidget(apply_rule_button)
        right_layout.addLayout(bulk_rule_actions)
        right_layout.addWidget(self.bulk_rule_hint_label)
        right_layout.addWidget(self.action_combo)
        right_layout.addWidget(compare_images_button)
        right_layout.addWidget(reveal_button)
        right_layout.addWidget(move_button)
        right_layout.addWidget(self.preview_stack)
        right_layout.addWidget(self.detail_label)

        self.operations_table.setHorizontalHeaderLabels(["状态", "动作", "时间", "原路径", "当前位置", "ID"])
        self.operations_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.operations_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.operations_table.setAlternatingRowColors(True)
        self.operations_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.operations_table.verticalHeader().setDefaultSectionSize(30)
        self.operations_table.setColumnHidden(5, True)

        ops_buttons = QHBoxLayout()
        ops_buttons.addWidget(refresh_ops_button)
        ops_buttons.addWidget(undo_button)
        ops_buttons.addWidget(reveal_current_op_button)
        ops_buttons.addWidget(reveal_original_op_button)
        ops_buttons.addWidget(export_ops_button)

        ops_filter_row = QHBoxLayout()
        ops_filter_row.addWidget(self.operation_action_filter_combo)
        ops_filter_row.addWidget(self.operation_status_filter_combo)
        ops_filter_row.addWidget(self.operation_search_input, 1)

        operations_panel = QWidget()
        operations_panel.setObjectName("ContentPanel")
        operations_layout = QVBoxLayout(operations_panel)
        operations_layout.setContentsMargins(14, 14, 14, 14)
        operations_layout.setSpacing(10)
        self.operation_summary_label.setWordWrap(True)
        operations_layout.addWidget(self.operation_summary_label)
        operations_layout.addLayout(ops_filter_row)
        operations_layout.addWidget(self.operations_table)
        operations_layout.addLayout(ops_buttons)

        self.history_table.setHorizontalHeaderLabels(["文件名", "大小", "修改时间", "尺寸", "SHA-256", "图片指纹", "路径"])
        self.history_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.verticalHeader().setDefaultSectionSize(30)

        self.session_table.setHorizontalHeaderLabels(["时间", "扫描目录", "分组数", "重复文件数", "占用空间", "预估可省", "ID"])
        self.session_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.session_table.setAlternatingRowColors(True)
        self.session_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.session_table.verticalHeader().setDefaultSectionSize(30)
        self.session_table.setColumnHidden(6, True)
        self.session_table.itemSelectionChanged.connect(self.on_session_selection_changed)
        self.session_table.itemDoubleClicked.connect(self.on_session_item_double_clicked)
        self.session_filter_input.setPlaceholderText("搜索扫描时间、目录、分组数、重复文件数")
        self.session_filter_input.textChanged.connect(self.apply_session_filter)
        self.session_sort_combo.addItems(["按时间：最新优先", "按时间：最早优先", "按预估可省：从大到小", "按占用空间：从大到小", "按分组数：从多到少"])
        self.session_sort_combo.currentIndexChanged.connect(self.refresh_history)
        self.session_range_combo.addItems(["全部时间", "最近 7 天", "最近 30 天"])
        self.session_range_combo.currentIndexChanged.connect(self.apply_session_filter)

        session_filter_row = QHBoxLayout()
        session_filter_row.addWidget(QLabel("任务筛选"))
        session_filter_row.addWidget(QLabel("时间范围"))
        session_filter_row.addWidget(self.session_range_combo)
        session_filter_row.addWidget(self.session_filter_input)
        session_filter_row.addWidget(QLabel("排序"))
        session_filter_row.addWidget(self.session_sort_combo)
        history_buttons = QHBoxLayout()
        history_buttons.addWidget(refresh_history_button)
        load_session_button = QPushButton("加载选中扫描任务")
        load_session_button.clicked.connect(self.load_selected_scan_session)
        history_buttons.addWidget(load_session_button)
        delete_session_button = QPushButton("删除选中扫描任务")
        delete_session_button.clicked.connect(self.delete_selected_scan_session)
        history_buttons.addWidget(delete_session_button)
        history_buttons.addWidget(clear_history_button)
        history_buttons.addWidget(export_sessions_button)
        history_buttons.addWidget(export_history_button)

        history_panel = QWidget()
        history_panel.setObjectName("ContentPanel")
        history_layout = QVBoxLayout(history_panel)
        history_layout.setContentsMargins(14, 14, 14, 14)
        history_layout.setSpacing(10)
        self.session_summary_label.setWordWrap(True)
        self.session_summary_label.setText("这里会显示历史扫描任务。")
        history_layout.addWidget(self.session_summary_label)
        history_layout.addLayout(session_filter_row)
        history_layout.addWidget(self.session_table)
        history_layout.addWidget(self.history_table)
        history_layout.addLayout(history_buttons)

        self.ai_table.setHorizontalHeaderLabels(["时间", "模型", "分数", "摘要", "路径"])
        self.ai_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.ai_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.ai_table.setAlternatingRowColors(True)
        self.ai_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.ai_table.verticalHeader().setDefaultSectionSize(30)

        ai_panel = QWidget()
        ai_panel.setObjectName("ContentPanel")
        ai_layout = QVBoxLayout(ai_panel)
        ai_layout.setContentsMargins(14, 14, 14, 14)
        ai_layout.setSpacing(8)
        ai_layout.addWidget(QLabel("厂商预设"))
        ai_layout.addWidget(self.ai_provider_combo)
        ai_layout.addWidget(QLabel("API 地址"))
        ai_layout.addWidget(self.ai_base_url_input)
        ai_layout.addWidget(QLabel("模型名"))
        ai_layout.addWidget(self.ai_model_input)
        ai_layout.addWidget(QLabel("API Key"))
        ai_layout.addWidget(self.ai_key_input)
        ai_layout.addWidget(self.ai_show_key_checkbox)
        ai_buttons = QHBoxLayout()
        ai_buttons.addWidget(save_ai_button)
        ai_buttons.addWidget(self.ai_check_button)
        ai_buttons.addWidget(self.ai_analyze_button)
        ai_buttons.addWidget(self.ai_batch_button)
        ai_layout.addLayout(ai_buttons)
        ai_filter_buttons = QHBoxLayout()
        ai_filter_buttons.addWidget(self.ai_score_filter_spin)
        ai_filter_buttons.addWidget(filter_ai_button)
        ai_filter_buttons.addWidget(export_ai_button)
        ai_layout.addLayout(ai_filter_buttons)
        ai_layout.addWidget(self.ai_table)

        self.health_table.setHorizontalHeaderLabels(["项目", "状态", "详情"])
        self.health_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.health_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.health_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.health_table.setAlternatingRowColors(True)
        self.health_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.health_table.verticalHeader().setDefaultSectionSize(30)
        self.health_diagnostic_text.setReadOnly(True)
        self.health_diagnostic_text.setMinimumHeight(180)
        self.health_diagnostic_text.setPlaceholderText("这里会生成可复制的版本、路径和运行环境诊断信息。")

        health_panel = QWidget()
        health_panel.setObjectName("ContentPanel")
        health_layout = QVBoxLayout(health_panel)
        health_layout.setContentsMargins(14, 14, 14, 14)
        health_layout.setSpacing(10)
        self.health_summary_label.setWordWrap(True)
        health_layout.addWidget(self.health_summary_label)
        health_layout.addWidget(self.health_table)
        health_layout.addWidget(QLabel("可复制诊断信息"))
        health_layout.addWidget(self.health_diagnostic_text)
        health_buttons = QHBoxLayout()
        health_buttons.addWidget(refresh_health_button)
        health_buttons.addWidget(self.health_data_dir_button)
        health_buttons.addWidget(self.health_log_dir_button)
        health_buttons.addWidget(copy_health_button)
        health_buttons.addWidget(export_health_button)
        health_layout.addLayout(health_buttons)

        self.onboarding_table.setHorizontalHeaderLabels(["状态", "步骤", "当前情况", "快捷操作"])
        self.onboarding_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.onboarding_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.onboarding_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.onboarding_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.onboarding_table.setAlternatingRowColors(True)
        self.onboarding_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.onboarding_table.verticalHeader().setDefaultSectionSize(34)

        onboarding_panel = QWidget()
        onboarding_panel.setObjectName("ContentPanel")
        onboarding_layout = QVBoxLayout(onboarding_panel)
        onboarding_layout.setContentsMargins(14, 14, 14, 14)
        onboarding_layout.setSpacing(10)
        self.onboarding_summary_label.setWordWrap(True)
        self.onboarding_guidance_label.setWordWrap(True)
        onboarding_actions = QGridLayout()
        guide_buttons = (
            guide_add_folder_button,
            guide_protect_button,
            guide_scan_button,
            guide_rule_button,
            guide_preview_button,
            guide_profile_button,
            guide_refresh_button,
        )
        for button_index, button in enumerate(guide_buttons):
            onboarding_actions.addWidget(button, button_index // 4, button_index % 4)
        onboarding_layout.addWidget(self.onboarding_summary_label)
        onboarding_layout.addWidget(self.onboarding_guidance_label)
        onboarding_layout.addWidget(self.onboarding_table)
        onboarding_layout.addLayout(onboarding_actions)

        self.organize_photo_input.setText(str(self.organize_targets.photo_root))
        self.organize_video_input.setText(str(self.organize_targets.video_root))
        self.organize_document_input.setText(str(self.organize_targets.document_root))
        self.organize_other_input.setText(str(self.organize_targets.other_root))
        self.organize_summary_label.setWordWrap(True)
        self.organize_summary_label.setText("先生成整理预览，再批量执行移动。")
        self.organize_help_label.setWordWrap(True)
        self.organize_help_label.setText("预览整理结果：只计算建议目标路径，不会移动文件。执行预览中的整理：真正按当前预览列表移动文件。")

        preview_button = QPushButton("预览整理结果")
        preview_button.clicked.connect(self.preview_organize_plan)
        preview_button.setToolTip("只生成整理结果预览，不会移动任何文件。")
        execute_button = QPushButton("执行预览中的整理")
        execute_button.clicked.connect(self.execute_organize_plan)
        execute_button.setToolTip("按当前预览列表真正移动文件。")

        self.organize_preview_table.setHorizontalHeaderLabels(["分类", "状态", "大小", "文件名", "来源路径", "建议目标"])
        self.organize_preview_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.organize_preview_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.organize_preview_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.organize_preview_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.organize_preview_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.organize_preview_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.organize_preview_table.setAlternatingRowColors(True)
        self.organize_preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.organize_preview_table.verticalHeader().setDefaultSectionSize(30)

        organize_panel = QWidget()
        organize_panel.setObjectName("ContentPanel")
        organize_layout = QVBoxLayout(organize_panel)
        organize_layout.setContentsMargins(14, 14, 14, 14)
        organize_layout.setSpacing(10)

        for label_text, line_edit in (
            ("照片目标目录", self.organize_photo_input),
            ("视频目标目录", self.organize_video_input),
            ("文档目标目录", self.organize_document_input),
            ("其他文件目录", self.organize_other_input),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(label_text))
            row.addWidget(line_edit, 1)
            browse_button = QPushButton("选择")
            browse_button.clicked.connect(lambda _checked=False, field=line_edit: self.choose_organize_target(field))
            row.addWidget(browse_button)
            organize_layout.addLayout(row)

        organize_actions = QHBoxLayout()
        organize_actions.addWidget(self.organize_keep_only_checkbox)
        organize_actions.addStretch(1)
        organize_actions.addWidget(preview_button)
        organize_actions.addWidget(execute_button)
        organize_layout.addLayout(organize_actions)
        organize_layout.addWidget(self.organize_help_label)
        organize_layout.addWidget(self.organize_summary_label)
        organize_layout.addWidget(self.organize_preview_table)

        self.main_tabs = QTabWidget()
        tabs = self.main_tabs
        tabs.addTab(onboarding_panel, "新手向导")
        tabs.addTab(organize_panel, "整理中心")
        tabs.addTab(right, "重复处理")
        tabs.addTab(operations_panel, "操作记录")
        tabs.addTab(history_panel, "扫描历史")
        tabs.addTab(ai_panel, "AI 判断")
        tabs.addTab(health_panel, "健康诊断")

        self.main_tabs.currentChanged.connect(self.on_tab_changed)

        splitter = QSplitter()
        splitter.setObjectName("MainSplitter")
        splitter.addWidget(left)
        splitter.addWidget(groups_panel)
        splitter.addWidget(tabs)
        splitter.setSizes([270, 310, 700])

        container = QWidget()
        container.setObjectName("AppRoot")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(splitter)
        self.setCentralWidget(container)

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
                font-size: 13px;
                color: #1f2328;
                background: #f6f7f9;
            }
            #AppRoot {
                background: #f6f7f9;
            }
            #Sidebar, #GroupsPanel, #ContentPanel {
                background: #ffffff;
                border: 1px solid #d8dee4;
                border-radius: 8px;
            }
            #Sidebar {
                background: #fbfbfc;
            }
            #MainSplitter::handle {
                background: transparent;
            }
            QPushButton {
                min-height: 28px;
                padding: 6px 10px;
                border: 1px solid #d0d7de;
                border-radius: 6px;
                background: #f6f8fa;
                color: #24292f;
                font-weight: 500;
            }
            QPushButton:hover {
                background: #eef2f6;
                border-color: #afb8c1;
            }
            QPushButton:pressed {
                background: #e6ebf0;
            }
            QPushButton:disabled {
                color: #8c959f;
                background: #f0f2f4;
                border-color: #d8dee4;
            }
            QComboBox, QSpinBox, QLineEdit {
                min-height: 28px;
                padding: 4px 8px;
                border: 1px solid #d0d7de;
                border-radius: 6px;
                background: #ffffff;
                selection-background-color: #0969da;
            }
            QComboBox:focus, QSpinBox:focus, QLineEdit:focus {
                border-color: #0969da;
            }
            QListWidget, QTableWidget {
                border: 1px solid #d8dee4;
                border-radius: 6px;
                background: #ffffff;
                alternate-background-color: #f6f8fa;
                gridline-color: #eaeef2;
            }
            QListWidget::item:selected, QTableWidget::item:selected {
                background: #ddf4ff;
                color: #0969da;
            }
            QListWidget::item {
                padding: 6px;
                border-radius: 5px;
            }
            QHeaderView::section {
                background: #f6f8fa;
                border: 0;
                border-right: 1px solid #eaeef2;
                border-bottom: 1px solid #d8dee4;
                padding: 6px;
                font-weight: 600;
                color: #57606a;
            }
            QLabel {
                color: #57606a;
                background: transparent;
            }
            QTabWidget::pane {
                border: 1px solid #d8dee4;
                border-radius: 8px;
                background: #ffffff;
                top: -1px;
            }
            QTabBar::tab {
                padding: 8px 14px;
                background: #f6f8fa;
                border: 1px solid #d8dee4;
                border-bottom: none;
                border-top-left-radius: 7px;
                border-top-right-radius: 7px;
                margin-right: 3px;
                color: #57606a;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                color: #0969da;
                font-weight: 600;
            }
            QTabBar::tab:hover {
                color: #24292f;
            }
            QProgressBar {
                height: 16px;
                border: 1px solid #d0d7de;
                border-radius: 8px;
                text-align: center;
                background: #ffffff;
                color: #57606a;
            }
            QProgressBar::chunk {
                background-color: #0969da;
                border-radius: 7px;
            }
            """
        )
        self.retention_hint_label.setWordWrap(True)
        self.retention_hint_label.setStyleSheet(
            "padding: 9px 10px; border: 1px solid #d8dee4; border-radius: 7px; background: #f6f8fa; color: #57606a;"
        )

    def add_folder(self) -> None:
        dialog = QFileDialog(self, "选择一个或多个要扫描的文件夹")
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        for view in dialog.findChildren(QListView):
            view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for view in dialog.findChildren(QTreeView):
            view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        if not dialog.exec():
            return
        folders = dialog.selectedFiles()
        existing = {str(path) for path in self._folder_paths()}
        for folder in folders:
            if folder not in existing:
                self._add_folder_item(Path(folder))
                existing.add(folder)
        self.refresh_onboarding()

    def remove_folder(self) -> None:
        for item in self.folder_list.selectedItems():
            self.protected_roots.discard(normalize_path(self._folder_path_from_item(item)))
            self.folder_list.takeItem(self.folder_list.row(item))
        self._save_settings()
        self.refresh_onboarding()

    def toggle_protected_folder(self) -> None:
        selected_items = self.folder_list.selectedItems()
        if not selected_items:
            QMessageBox.information(self, "没有选中目录", "请先在扫描目录列表中选中一个或多个目录。")
            return
        protected = {normalize_path(path) for path in self.protected_roots}
        for item in selected_items:
            path = normalize_path(self._folder_path_from_item(item))
            if path in protected:
                protected.remove(path)
            else:
                protected.add(path)
        self.protected_roots = protected
        self._refresh_folder_item_labels()
        self._save_settings()
        self.status_label.setText(f"保护目录已更新：{len(self.protected_roots)} 个")
        self.refresh_onboarding()

    def choose_destination(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择重复文件移动目标", str(self.destination))
        if folder:
            self.destination = Path(folder)
            self._update_destination_label()
            self._save_settings()
            self.status_label.setText(f"目标目录：{self.destination}")
            self.refresh_onboarding()

    def _update_destination_label(self) -> None:
        self.destination_label.setWordWrap(True)
        self.destination_label.setText(f"移动到：{self.destination}")

    def choose_organize_target(self, field: QLineEdit) -> None:
        current = field.text().strip() or str(self.destination)
        folder = QFileDialog.getExistingDirectory(self, "选择整理目标目录", current)
        if not folder:
            return
        field.setText(folder)
        self._save_settings()
        self.refresh_onboarding()

    def _load_profiles(self) -> None:
        self.organize_profiles = decode_profiles(self.database.get_setting(PROFILE_SETTINGS_KEY, ""))
        self._refresh_profile_combo()

    def _save_profiles(self, select_name: str | None = None) -> None:
        self.database.set_setting(PROFILE_SETTINGS_KEY, encode_profiles(self.organize_profiles))
        self._refresh_profile_combo(select_name=select_name)

    def _refresh_profile_combo(self, select_name: str | None = None) -> None:
        current_name = select_name or self._selected_profile_name()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("未选择方案", None)
        for profile in self.organize_profiles:
            self.profile_combo.addItem(profile.name, profile.name)
            self.profile_combo.setItemData(self.profile_combo.count() - 1, profile_summary(profile), Qt.ItemDataRole.ToolTipRole)
        if current_name:
            for index in range(self.profile_combo.count()):
                if self.profile_combo.itemData(index) == current_name:
                    self.profile_combo.setCurrentIndex(index)
                    break
        self.profile_combo.blockSignals(False)
        self.on_profile_selection_changed()

    def _selected_profile_name(self) -> str | None:
        value = self.profile_combo.currentData()
        return str(value) if value else None

    def _selected_profile(self) -> OrganizeProfile | None:
        name = self._selected_profile_name()
        if not name:
            return None
        for profile in self.organize_profiles:
            if profile.name == name:
                return profile
        return None

    def on_profile_selection_changed(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.profile_summary_label.setText("保存常用扫描目录、保护目录、保留规则和整理目标。")
            return
        self.profile_summary_label.setText(profile_summary(profile))

    def _selected_template_key(self) -> str | None:
        value = self.template_combo.currentData()
        return str(value) if value else None

    def on_template_selection_changed(self) -> None:
        key = self._selected_template_key()
        if not key:
            self.template_summary_label.setText("选择一个常见场景后套用，不会移动或删除文件。")
            return
        try:
            template = get_builtin_template(key)
        except ValueError:
            self.template_summary_label.setText("这个模板暂时不可用。")
            return
        self.template_summary_label.setText(template.description)

    def apply_selected_template(self) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "扫描中", "请先停止或等待当前扫描完成，再套用整理模板。")
            return
        key = self._selected_template_key()
        if not key:
            QMessageBox.information(self, "没有选中模板", "请先选择一个内置整理模板。")
            return
        base_root = self.destination.parent / "PhotoManageGPT_整理库"
        profile = build_template_profile(
            key,
            base_root,
            scan_roots=tuple(self._folder_paths()),
            protected_roots=tuple(sorted((normalize_path(path) for path in self.protected_roots), key=str)),
        )
        answer = QMessageBox.question(
            self,
            "套用整理模板",
            f"将套用“{profile.name}”，保留当前扫描目录和保护目录，并替换保留规则、整理规则和目标目录。\n\n"
            f"重复待复核目录：{profile.duplicate_destination}\n"
            f"照片目标目录：{profile.photo_target}\n\n"
            "此操作不会移动、回收或删除任何文件。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.profile_combo.setCurrentIndex(0)
        self.apply_profile(profile)
        self.profile_summary_label.setText(f"已套用模板：{profile_summary(profile)}")
        self.status_label.setText(f"已套用整理模板：{profile.name}")
        self.refresh_onboarding()

    def _capture_current_profile(self, name: str) -> OrganizeProfile:
        targets = self.current_organize_targets()
        return OrganizeProfile(
            name=name,
            scan_roots=tuple(self._folder_paths()),
            protected_roots=tuple(sorted((normalize_path(path) for path in self.protected_roots), key=str)),
            duplicate_destination=self.destination,
            keep_strategy=self.strategy_combo.currentText(),
            bulk_keep_rule=self.bulk_keep_rule_combo.currentText(),
            bulk_keep_keyword=self.bulk_keep_keyword_input.text().strip(),
            processing_action=self.action_combo.currentText(),
            similarity_threshold=self.threshold_spin.value(),
            same_ratio_only=self.same_ratio_checkbox.isChecked(),
            same_resolution_only=self.same_resolution_checkbox.isChecked(),
            rotate_hint=self.rotate_hint_checkbox.isChecked(),
            organize_rule=self.organize_rule_combo.currentText(),
            organize_keep_only=self.organize_keep_only_checkbox.isChecked(),
            photo_target=targets.photo_root,
            video_target=targets.video_root,
            document_target=targets.document_root,
            other_target=targets.other_root,
        )

    def save_current_profile(self) -> None:
        current = self._selected_profile()
        default_name = current.name if current else "照片库整理"
        name_text, ok = QInputDialog.getText(self, "保存整理方案", "方案名称", QLineEdit.EchoMode.Normal, default_name)
        if not ok:
            return
        try:
            name = clean_profile_name(name_text)
        except ValueError as exc:
            QMessageBox.information(self, "方案名称无效", str(exc))
            return

        profile = self._capture_current_profile(name)
        self.organize_profiles = upsert_profile(self.organize_profiles, profile)
        self._save_profiles(select_name=name)
        self.status_label.setText(f"已保存整理方案：{name}")
        self.refresh_onboarding()

    def load_selected_profile(self) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "扫描中", "请先停止或等待当前扫描完成，再加载整理方案。")
            return
        profile = self._selected_profile()
        if profile is None:
            QMessageBox.information(self, "没有选中方案", "请先选择一个整理方案。")
            return

        answer = QMessageBox.question(
            self,
            "加载整理方案",
            f"将加载“{profile.name}”，并替换当前扫描目录、保护目录、保留规则和整理目标。\n"
            "此操作不会移动、回收或删除任何文件。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.apply_profile(profile)
        self.status_label.setText(f"已加载整理方案：{profile.name}")
        self.refresh_onboarding()

    def delete_selected_profile(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            QMessageBox.information(self, "没有选中方案", "请先选择一个要删除的整理方案。")
            return
        answer = QMessageBox.question(self, "删除整理方案", f"确认删除整理方案“{profile.name}”？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.organize_profiles = remove_profile(self.organize_profiles, profile.name)
        self._save_profiles()
        self.status_label.setText(f"已删除整理方案：{profile.name}")
        self.refresh_onboarding()

    def apply_profile(self, profile: OrganizeProfile) -> None:
        self.folder_list.clear()
        self.protected_roots = {normalize_path(path) for path in profile.protected_roots}
        for root in profile.scan_roots:
            self._add_folder_item(root, protected=normalize_path(root) in self.protected_roots)

        self.destination = self._profile_path_or_default(profile.duplicate_destination, self._default_destination())
        self._set_combo_text(self.strategy_combo, profile.keep_strategy)
        self._set_combo_text(self.bulk_keep_rule_combo, profile.bulk_keep_rule)
        self._set_combo_text(self.action_combo, profile.processing_action)
        self._set_combo_text(self.organize_rule_combo, profile.organize_rule)
        self.bulk_keep_keyword_input.setText(profile.bulk_keep_keyword)
        self.threshold_spin.setValue(profile.similarity_threshold)
        self.same_ratio_checkbox.setChecked(profile.same_ratio_only)
        self.same_resolution_checkbox.setChecked(profile.same_resolution_only)
        self.rotate_hint_checkbox.setChecked(profile.rotate_hint)
        self.organize_keep_only_checkbox.setChecked(profile.organize_keep_only)
        self.organize_photo_input.setText(str(self._profile_path_or_default(profile.photo_target, self.destination / "Photos")))
        self.organize_video_input.setText(str(self._profile_path_or_default(profile.video_target, self.destination / "Videos")))
        self.organize_document_input.setText(str(self._profile_path_or_default(profile.document_target, self.destination / "Documents")))
        self.organize_other_input.setText(str(self._profile_path_or_default(profile.other_target, self.destination / "Others")))
        self.organize_targets = self.current_organize_targets()
        self.organize_preview_items = []
        self.organize_preview_table.setRowCount(0)
        self._refresh_folder_item_labels()
        self._update_destination_label()
        self._save_settings()
        self.refresh_group_list()
        self.rebuild_directory_browser()
        self.refresh_onboarding()

    def _profile_path_or_default(self, path: Path, default: Path) -> Path:
        return default if str(path).strip() in {"", "."} else path

    def _set_combo_text(self, combo: QComboBox, value: str) -> None:
        if not value:
            return
        index = combo.findText(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _load_settings(self) -> None:
        threshold = self.database.get_setting("similarity_threshold", "")
        if threshold.isdigit():
            self.threshold_spin.setValue(int(threshold))
        strategy = self.database.get_setting("keep_strategy", "")
        if strategy:
            index = self.strategy_combo.findText(strategy)
            if index >= 0:
                self.strategy_combo.setCurrentIndex(index)
        organize_rule = self.database.get_setting("organize_rule", "")
        if organize_rule:
            index = self.organize_rule_combo.findText(organize_rule)
            if index >= 0:
                self.organize_rule_combo.setCurrentIndex(index)
        self.ai_base_url_input.setText(self.database.get_setting("ai_base_url", ""))
        self.ai_model_input.setText(self.database.get_setting("ai_model", ""))
        self.ai_key_input.setText(self.database.get_setting("ai_api_key", ""))
        self.organize_keep_only_checkbox.setChecked(self.database.get_setting("organize_keep_only", "0") == "1")
        self.same_ratio_checkbox.setChecked(self.database.get_setting("similar_same_ratio", "0") == "1")
        self.same_resolution_checkbox.setChecked(self.database.get_setting("similar_same_resolution", "0") == "1")
        self.rotate_hint_checkbox.setChecked(self.database.get_setting("similar_rotate_hint", "1") == "1")
        for checkbox in (self.same_ratio_checkbox, self.same_resolution_checkbox, self.rotate_hint_checkbox):
            checkbox.stateChanged.connect(lambda _state: self._save_settings())
        protected_text = self.database.get_setting("protected_roots", "")
        self.protected_roots = {normalize_path(Path(item)) for item in protected_text.split("|") if item}
        self._refresh_folder_item_labels()
        self.organize_photo_input.setText(str(self.organize_targets.photo_root))
        self.organize_video_input.setText(str(self.organize_targets.video_root))
        self.organize_document_input.setText(str(self.organize_targets.document_root))
        self.organize_other_input.setText(str(self.organize_targets.other_root))
        self._load_profiles()

    def _save_settings(self) -> None:
        self.database.set_setting("destination", str(self.destination))
        self.database.set_setting("similarity_threshold", str(self.threshold_spin.value()))
        self.database.set_setting("keep_strategy", self.strategy_combo.currentText())
        self.database.set_setting("organize_rule", self.organize_rule_combo.currentText())
        self.database.set_setting("organize_keep_only", "1" if self.organize_keep_only_checkbox.isChecked() else "0")
        self.database.set_setting("similar_same_ratio", "1" if self.same_ratio_checkbox.isChecked() else "0")
        self.database.set_setting("similar_same_resolution", "1" if self.same_resolution_checkbox.isChecked() else "0")
        self.database.set_setting("similar_rotate_hint", "1" if self.rotate_hint_checkbox.isChecked() else "0")
        self.database.set_setting("protected_roots", "|".join(str(path) for path in sorted(self.protected_roots, key=str)))
        self.database.set_setting("target_photo_root", self.organize_photo_input.text().strip())
        self.database.set_setting("target_video_root", self.organize_video_input.text().strip())
        self.database.set_setting("target_document_root", self.organize_document_input.text().strip())
        self.database.set_setting("target_other_root", self.organize_other_input.text().strip())

    def restore_latest_scan_session(self) -> None:
        session = self.database.get_latest_scan_session()
        if session is None:
            return
        self.current_roots = session.root_paths
        existing = {str(path) for path in self._folder_paths()}
        for root in session.root_paths:
            root_text = str(root)
            if root_text not in existing:
                self._add_folder_item(root)
                existing.add(root_text)
        groups = self.database.load_scan_session_groups(session.id)
        if groups:
            self.on_scan_finished(groups, restored=True)

    def on_tab_changed(self, index: int) -> None:
        tab_name = self.main_tabs.tabText(index)
        if tab_name == "新手向导":
            self.refresh_onboarding()
        elif tab_name == "操作记录" and not self.operations_loaded:
            self.refresh_operations()
            self.operations_loaded = True
        elif tab_name == "扫描历史" and not self.history_loaded:
            self.refresh_history()
            self.history_loaded = True
        elif tab_name == "AI 判断" and not self.ai_loaded:
            self.refresh_ai_results()
            self.ai_loaded = True

    def _set_main_tab(self, label: str) -> None:
        for index in range(self.main_tabs.count()):
            if self.main_tabs.tabText(index) == label:
                self.main_tabs.setCurrentIndex(index)
                return

    def _onboarding_snapshot(self) -> OnboardingSnapshot:
        keep_selected_count = sum(
            1
            for group in self.groups
            for record in group.records
            if record.keep_selected
        )
        target_count = sum(
            1
            for text in (
                self.organize_photo_input.text(),
                self.organize_video_input.text(),
                self.organize_document_input.text(),
                self.organize_other_input.text(),
            )
            if text.strip()
        )
        return OnboardingSnapshot(
            scan_root_count=len(self._folder_paths()),
            protected_root_count=len(self.protected_roots),
            duplicate_group_count=len(self.groups),
            keep_selected_count=keep_selected_count,
            organize_target_count=target_count,
            organize_preview_count=len(self.organize_preview_items),
            profile_count=len(self.organize_profiles),
        )

    def refresh_onboarding(self) -> None:
        steps = build_onboarding_steps(self._onboarding_snapshot())
        self.onboarding_summary_label.setText(onboarding_summary(steps))
        self.onboarding_guidance_label.setText(onboarding_primary_guidance(steps))
        self.onboarding_table.setRowCount(len(steps))
        for row, step in enumerate(steps):
            values = [step.status, step.title, step.detail, step.action]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 0:
                    item.setForeground(self._onboarding_status_color(step.status))
                self.onboarding_table.setItem(row, column, item)
        self.onboarding_table.resizeRowsToContents()

    def _onboarding_status_color(self, status: str) -> QColor:
        if status == "完成":
            return QColor("#1a7f37")
        if status == "下一步":
            return QColor("#0969da")
        if status == "建议":
            return QColor("#bf8700")
        return QColor("#57606a")

    def guide_add_folder(self) -> None:
        self.add_folder()
        self.refresh_onboarding()

    def guide_mark_protected_folder(self) -> None:
        if self.folder_list.count() == 0:
            QMessageBox.information(self, "没有扫描目录", "请先添加一个扫描目录。")
            return
        if not self.folder_list.selectedItems():
            self.folder_list.setCurrentRow(0)
        self.toggle_protected_folder()
        self.refresh_onboarding()

    def guide_apply_keep_rule(self) -> None:
        self._set_main_tab("重复处理")
        self.apply_bulk_keep_rule()
        self.refresh_onboarding()

    def guide_preview_organize(self) -> None:
        self._set_main_tab("整理中心")
        self.preview_organize_plan()
        self.refresh_onboarding()

    def on_group_mode_changed(self, index: int) -> None:
        self.group_stack.setCurrentIndex(index)

    def refresh_group_list(self) -> None:
        current_group = None
        current_item = self.group_list.currentItem()
        if current_item is not None:
            data = current_item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, DuplicateGroup):
                current_group = data

        filtered_groups, min_size_bytes, max_size_bytes = self._visible_groups()
        selected_type = self.group_type_filter_combo.currentText()
        keyword = self.group_search_input.text().strip().lower()
        min_unit_label = self.group_min_size_unit_combo.currentText()
        max_unit_label = self.group_max_size_unit_combo.currentText()
        self.group_list.clear()

        for group in filtered_groups:
            group_category = classify_group_category([record.path for record in group.records])
            type_label = category_label(group_category)
            kind_label = "完全重复" if group.kind == "exact" else "相似"
            item = QListWidgetItem(
                f"[{type_label}] {kind_label} · {group.title}\n"
                f"{len(group.records)} 个文件 · 可整理 {format_size(group.reclaimable_bytes)}"
            )
            item.setForeground(group_category_color(group_category))
            item.setData(Qt.ItemDataRole.UserRole, group)
            self.group_list.addItem(item)

        if self.group_list.count() == 0:
            self.file_table.setRowCount(0)
            self.thumbnail_list.clear()
            self.preview_label.setText("选择图片以预览")
            self.detail_label.setText("当前没有可预览的文件。")
            self.group_detail_summary_label.setText("选择一个重复分组后，这里会显示占用空间、预估可省、涉及磁盘/目录和推荐保留原因。")
            self.group_status_label.setText(f"当前筛选下没有匹配的重复分组。\n可清空筛选，或添加目录后重新扫描。")
            return

        if min_size_bytes < 0:
            self.group_status_label.setText("最小可整理空间请输入数字，例如 500 或 1.5。")
        elif max_size_bytes < 0:
            self.group_status_label.setText("最大可整理空间请输入数字，例如 500 或 1.5。")
        elif keyword:
            if selected_type == "全部类型":
                self.group_status_label.setText(
                    f"搜索“{self.group_search_input.text().strip()}”后，当前显示 {self.group_list.count()} 组重复/相似文件。"
                )
            else:
                self.group_status_label.setText(
                    f"搜索“{self.group_search_input.text().strip()}”后，当前显示 {selected_type} 类型的 {self.group_list.count()} 组重复/相似文件。"
                )
        elif min_size_bytes > 0 and max_size_bytes > 0:
            self.group_status_label.setText(
                f"当前显示可整理空间位于 {self.group_min_size_input.text().strip()} {min_unit_label} ~ {self.group_max_size_input.text().strip()} {max_unit_label} 的 {self.group_list.count()} 组重复/相似文件。"
            )
        elif min_size_bytes > 0:
            self.group_status_label.setText(
                f"当前显示可整理空间不小于 {self.group_min_size_input.text().strip()} {min_unit_label} 的 {self.group_list.count()} 组重复/相似文件。"
            )
        elif max_size_bytes > 0:
            self.group_status_label.setText(
                f"当前显示可整理空间不大于 {self.group_max_size_input.text().strip()} {max_unit_label} 的 {self.group_list.count()} 组重复/相似文件。"
            )
        elif selected_type == "全部类型":
            self.group_status_label.setText(f"当前显示 {self.group_list.count()} 组重复/相似文件。")
        else:
            self.group_status_label.setText(f"当前显示 {selected_type} 类型的 {self.group_list.count()} 组重复/相似文件。")

        if current_group is not None:
            for row in range(self.group_list.count()):
                item = self.group_list.item(row)
                data = item.data(Qt.ItemDataRole.UserRole)
                if data is current_group:
                    self.group_list.setCurrentRow(row)
                    break
            else:
                self.group_list.setCurrentRow(0)
        else:
            self.group_list.setCurrentRow(0)

    def _visible_groups(self) -> tuple[list[DuplicateGroup], int, int]:
        selected_type = self.group_type_filter_combo.currentText()
        sort_label = self.group_sort_combo.currentText()
        keyword = self.group_search_input.text().strip().lower()
        min_size_bytes = self._size_filter_bytes(self.group_min_size_input, self.group_min_size_unit_combo)
        max_size_bytes = self._size_filter_bytes(self.group_max_size_input, self.group_max_size_unit_combo)

        filtered_groups: list[DuplicateGroup] = []
        for group in self.groups:
            group_category = classify_group_category([record.path for record in group.records])
            type_label = category_label(group_category)
            if selected_type != "全部类型" and type_label != selected_type:
                continue
            if min_size_bytes > 0 and group.reclaimable_bytes < min_size_bytes:
                continue
            if min_size_bytes < 0:
                continue
            if max_size_bytes > 0 and group.reclaimable_bytes > max_size_bytes:
                continue
            if max_size_bytes < 0:
                continue
            if keyword:
                haystack_parts = [group.title.lower()]
                haystack_parts.extend(record.name.lower() for record in group.records)
                haystack_parts.extend(str(record.path).lower() for record in group.records)
                if keyword not in " | ".join(haystack_parts):
                    continue
            filtered_groups.append(group)

        if sort_label == "按可整理空间：从大到小":
            filtered_groups.sort(key=lambda item: item.reclaimable_bytes, reverse=True)
        elif sort_label == "按文件数：从多到少":
            filtered_groups.sort(key=lambda item: len(item.records), reverse=True)
        return filtered_groups, min_size_bytes, max_size_bytes

    def _size_filter_bytes(self, field: QLineEdit, unit_combo: QComboBox) -> int:
        text = field.text().strip()
        if not text:
            return 0
        try:
            value = float(text)
        except ValueError:
            return -1
        multiplier = 1024 * 1024 if unit_combo.currentText() == "MB" else 1024 * 1024 * 1024
        return int(value * multiplier)

    def clear_group_filters(self) -> None:
        self.group_type_filter_combo.setCurrentIndex(0)
        self.group_sort_combo.setCurrentIndex(0)
        self.group_search_input.clear()
        self.group_min_size_input.clear()
        self.group_max_size_input.clear()
        self.refresh_group_list()

    def rebuild_directory_browser(self) -> None:
        self.file_duplicate_links, self.directory_duplicate_files = build_directory_duplicate_index(self.groups)
        self.directory_tree.clear()
        self.directory_detail_table.setRowCount(0)
        if not self.file_duplicate_links:
            self.directory_hint_label.setText("当前没有可浏览的重复目录数据。")
            return

        drive_counts: dict[str, int] = {}
        directory_counts: dict[str, int] = {}
        for path in self.file_duplicate_links:
            drive_label = path.anchor or path.parts[0]
            drive_counts[drive_label] = drive_counts.get(drive_label, 0) + 1
            current_path = Path(path.anchor) if path.anchor else None
            for part in path.parts[1:-1]:
                current_path = (current_path / part) if current_path is not None else Path(part)
                key = str(current_path)
                directory_counts[key] = directory_counts.get(key, 0) + 1

        drive_items: dict[str, QTreeWidgetItem] = {}
        directory_items: dict[str, QTreeWidgetItem] = {}
        for path in sorted(self.file_duplicate_links.keys(), key=lambda item: str(item).lower()):
            drive_label = path.anchor or path.parts[0]
            drive_item = drive_items.get(drive_label)
            if drive_item is None:
                drive_item = QTreeWidgetItem([f"{drive_label} ({drive_counts.get(drive_label, 0)})"])
                drive_item.setData(0, Qt.ItemDataRole.UserRole, {"node_type": "drive", "path": path.anchor})
                self.directory_tree.addTopLevelItem(drive_item)
                drive_items[drive_label] = drive_item

            current_parent = drive_item
            current_path = Path(path.anchor) if path.anchor else None
            for part in path.parts[1:-1]:
                current_path = (current_path / part) if current_path is not None else Path(part)
                key = str(current_path)
                directory_item = directory_items.get(key)
                if directory_item is None:
                    directory_item = QTreeWidgetItem([f"{part} ({directory_counts.get(key, 0)})"])
                    directory_item.setData(0, Qt.ItemDataRole.UserRole, {"node_type": "directory", "path": key})
                    current_parent.addChild(directory_item)
                    directory_items[key] = directory_item
                current_parent = directory_item

            file_item = QTreeWidgetItem([f"{path.name} [{len(self.file_duplicate_links[path].group_indices)}]"])
            file_item.setData(0, Qt.ItemDataRole.UserRole, {"node_type": "file", "path": str(path)})
            current_parent.addChild(file_item)

        self.directory_tree.expandToDepth(1)
        self.directory_hint_label.setText(
            f"已建立目录树：{len(self.directory_duplicate_files)} 个目录，{len(self.file_duplicate_links)} 个重复文件。"
        )

    def on_directory_tree_selection_changed(self) -> None:
        items = self.directory_tree.selectedItems()
        if not items:
            return
        payload = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            return
        node_type = payload.get("node_type")
        if node_type == "file":
            self.populate_directory_file_details(Path(str(payload.get("path", ""))))
            return
        if node_type == "directory":
            self.populate_directory_summary(Path(str(payload.get("path", ""))))
            return
        if node_type == "drive":
            self.populate_drive_summary(str(payload.get("path", "")))

    def populate_directory_file_details(self, path: Path) -> None:
        link = self.file_duplicate_links.get(path)
        if link is None:
            return
        rows: list[tuple[str, str, str, int]] = []
        for group_index in link.group_indices:
            group = self.groups[group_index]
            for record in group.records:
                if record.path == path:
                    continue
                rows.append((record.name, str(path.parent), str(record.path.parent), group_index))
        self.directory_detail_table.setRowCount(len(rows))
        for row, (name, current_dir, peer_dir, group_index) in enumerate(rows):
            values = [name, current_dir, peer_dir, self.groups[group_index].title]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, group_index)
                self.directory_detail_table.setItem(row, column, item)
        self.directory_detail_table.resizeRowsToContents()
        if link.group_indices:
            self.group_list.setCurrentRow(link.group_indices[0])
        peer_dirs = sorted({str(peer.parent) for peer in link.peer_paths})
        self.directory_hint_label.setText(
            f"{path.name} 关联 {len(link.group_indices)} 个重复分组，涉及 {len(peer_dirs)} 个其他目录。"
        )

    def populate_directory_summary(self, directory: Path) -> None:
        files = self.directory_duplicate_files.get(directory, [])
        self.directory_detail_table.setRowCount(len(files))
        for row, path in enumerate(files):
            link = self.file_duplicate_links.get(path)
            peer_dirs = sorted({str(peer.parent) for peer in (link.peer_paths if link else [])})
            group_index = link.group_indices[0] if link and link.group_indices else -1
            values = [
                path.name,
                str(directory),
                ", ".join(peer_dirs[:3]) + (" ..." if len(peer_dirs) > 3 else ""),
                str(len(link.group_indices)) if link else "0",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0 and group_index >= 0:
                    item.setData(Qt.ItemDataRole.UserRole, group_index)
                self.directory_detail_table.setItem(row, column, item)
        self.directory_detail_table.resizeRowsToContents()
        self.directory_hint_label.setText(f"{directory} 下共有 {len(files)} 个参与重复的文件。")

    def populate_drive_summary(self, drive: str) -> None:
        files = [
            path
            for path in sorted(self.file_duplicate_links.keys(), key=lambda item: str(item).lower())
            if str(path).startswith(drive)
        ]
        self.directory_detail_table.setRowCount(len(files))
        for row, path in enumerate(files):
            link = self.file_duplicate_links.get(path)
            peer_dirs = sorted({str(peer.parent) for peer in (link.peer_paths if link else [])})
            group_index = link.group_indices[0] if link and link.group_indices else -1
            values = [
                path.name,
                str(path.parent),
                ", ".join(peer_dirs[:2]) + (" ..." if len(peer_dirs) > 2 else ""),
                str(len(link.group_indices)) if link else "0",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0 and group_index >= 0:
                    item.setData(Qt.ItemDataRole.UserRole, group_index)
                self.directory_detail_table.setItem(row, column, item)
        self.directory_detail_table.resizeRowsToContents()
        self.directory_hint_label.setText(f"{drive} 中共有 {len(files)} 个参与重复的文件。")

    def on_directory_detail_selection_changed(self) -> None:
        row = self.directory_detail_table.currentRow()
        if row < 0:
            return
        item = self.directory_detail_table.item(row, 0)
        if item is None:
            return
        group_index = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(group_index, int) and 0 <= group_index < self.group_list.count():
            self.group_list.setCurrentRow(group_index)

    def selected_directory_browser_paths(self) -> list[Path]:
        items = self.directory_tree.selectedItems()
        if not items:
            return []
        payload = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            return []
        node_type = payload.get("node_type")
        raw_path = str(payload.get("path", ""))
        if not raw_path:
            return []
        if node_type == "file":
            return [Path(raw_path)]
        if node_type == "directory":
            return list(self.directory_duplicate_files.get(Path(raw_path), []))
        if node_type == "drive":
            return [
                path
                for path in sorted(self.file_duplicate_links.keys(), key=lambda item: str(item).lower())
                if str(path).startswith(raw_path)
            ]
        return []

    def locate_first_group_from_directory_selection(self) -> None:
        paths = self.selected_directory_browser_paths()
        for path in paths:
            link = self.file_duplicate_links.get(path)
            if link and link.group_indices:
                self.group_mode_combo.setCurrentIndex(0)
                self.group_list.setCurrentRow(link.group_indices[0])
                return
        QMessageBox.information(self, "没有关联分组", "当前目录节点没有可定位的重复分组。")

    def preview_selected_directory_paths(self) -> None:
        paths = self.selected_directory_browser_paths()
        if not paths:
            QMessageBox.information(self, "没有可整理文件", "请先在磁盘目录视图中选择盘符、目录或文件。")
            return

        self.organize_targets = self.current_organize_targets()
        include_paths = set(paths)
        if self.organize_keep_only_checkbox.isChecked():
            include_paths &= self._selected_keep_paths()
        preview = build_organize_preview(
            paths,
            self.organize_targets,
            include_paths=include_paths,
            exclude_roots={normalize_path(path) for path in self.protected_roots},
        )
        protected_skipped = sum(1 for path in paths if is_under_any(path, self.protected_roots))
        self.organize_preview_items = preview
        self.organize_preview_table.setRowCount(len(preview))

        preview_summary = summarize_organize_preview(preview)
        for row, preview_item in enumerate(preview):
            status = describe_organize_preview_item(preview_item)
            values = [
                category_label(preview_item.category),
                status.label,
                format_size(status.size),
                preview_item.source.name,
                str(preview_item.source),
                str(preview_item.target),
            ]
            for column, value in enumerate(values):
                self.organize_preview_table.setItem(row, column, QTableWidgetItem(value))
        self.organize_preview_table.resizeRowsToContents()

        self.organize_summary_label.setText(
            f"来自目录树选择的预览：{preview_summary.file_count} 个文件"
            f" | {self._category_counts_text(preview_summary)}"
            f" | 总大小 {format_size(preview_summary.total_bytes)}"
            f" | 目标目录 {len(preview_summary.target_directories)} 个"
            f" | 跳过保护目录 {protected_skipped}"
        )
        self._set_main_tab("整理中心")
        self.status_label.setText("已根据目录树选择生成整理预览")
        self.refresh_onboarding()

    def current_organize_targets(self) -> OrganizeTargetConfig:
        return OrganizeTargetConfig(
            photo_root=Path(self.organize_photo_input.text().strip() or str(self.destination / "Photos")),
            video_root=Path(self.organize_video_input.text().strip() or str(self.destination / "Videos")),
            document_root=Path(self.organize_document_input.text().strip() or str(self.destination / "Documents")),
            other_root=Path(self.organize_other_input.text().strip() or str(self.destination / "Others")),
        )

    def _scan_target_roots(self) -> list[Path]:
        targets = self.current_organize_targets()
        return [
            self.destination,
            targets.photo_root,
            targets.video_root,
            targets.document_root,
            targets.other_root,
        ]

    def _format_scan_root_issues(self, issues: list[ScanRootIssue]) -> str:
        visible = issues[:8]
        lines = [
            f"- {issue.title}\n  目录：{issue.path}\n  {issue.message}"
            for issue in visible
        ]
        if len(issues) > len(visible):
            lines.append(f"- 另有 {len(issues) - len(visible)} 个问题未显示，请先缩小扫描范围。")
        return "\n".join(lines)

    def _confirm_scan_roots_are_safe(self, roots: list[Path]) -> bool:
        issues = validate_scan_roots(roots, self._scan_target_roots())
        blocking_issues = [issue for issue in issues if issue.severity == SEVERITY_ERROR]
        warning_issues = [issue for issue in issues if issue.severity != SEVERITY_ERROR]

        if blocking_issues:
            QMessageBox.warning(
                self,
                "扫描目录需要调整",
                (
                    "以下目录不适合直接扫描，为了避免卡死、权限错误或重复整理，已阻止本次扫描：\n\n"
                    f"{self._format_scan_root_issues(blocking_issues)}"
                ),
            )
            return False

        if warning_issues:
            reply = QMessageBox.question(
                self,
                "扫描前提醒",
                (
                    "扫描目录存在一些可能影响效率或结果的问题：\n\n"
                    f"{self._format_scan_root_issues(warning_issues)}\n\n"
                    "是否仍然继续扫描？"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return reply == QMessageBox.StandardButton.Yes

        return True

    def _category_counts_text(self, summary: OrganizePreviewSummary) -> str:
        counts = summary.category_counts
        return (
            f"照片 {counts.get('photo', 0)}"
            f" | 视频 {counts.get('video', 0)}"
            f" | 文档 {counts.get('document', 0)}"
            f" | 其他 {counts.get('other', 0)}"
        )

    def _limited_path_block(self, paths: list[Path], limit: int = 8) -> str:
        if not paths:
            return "-"
        lines = [str(path) for path in paths[:limit]]
        if len(paths) > limit:
            lines.append(f"... 另有 {len(paths) - limit} 个目录")
        return "\n".join(lines)

    def _organize_execution_message(self, summary: OrganizePreviewSummary) -> str:
        skip_notes = []
        if summary.missing_count:
            skip_notes.append(f"{summary.missing_count} 个源文件当前不存在，会自动跳过")
        if summary.already_in_target_count:
            skip_notes.append(f"{summary.already_in_target_count} 个文件已经在目标位置，会自动跳过")
        skip_text = "；".join(skip_notes) if skip_notes else "无"

        return (
            f"将按当前整理预览移动 {summary.file_count} 个文件，合计 {format_size(summary.total_bytes)}。\n"
            f"分类：{self._category_counts_text(summary)}。\n"
            f"预计目标目录：{len(summary.target_directories)} 个。\n"
            f"{self._limited_path_block(summary.target_directories)}\n\n"
            f"涉及来源目录：{len(summary.source_directories)} 个。\n"
            f"{self._limited_path_block(summary.source_directories)}\n\n"
            "如果目标目录已有同名文件，会自动改名避免覆盖。\n"
            f"跳过提醒：{skip_text}。\n\n"
            "是否继续执行整理？"
        )

    def _duplicate_group_summary_text(self, summary: DuplicateGroupSummary) -> str:
        recommended = "、".join(summary.recommended_names[:3]) or "-"
        if len(summary.recommended_names) > 3:
            recommended += f" 等 {len(summary.recommended_names)} 个"
        disks = "、".join(summary.disks[:4]) or "-"
        if len(summary.disks) > 4:
            disks += f" 等 {len(summary.disks)} 个"
        directories = "、".join(str(path) for path in summary.directories[:3]) or "-"
        if len(summary.directories) > 3:
            directories += f" 等 {len(summary.directories)} 个目录"
        protected_text = f" | 保护目录文件 {summary.protected_count} 个" if summary.protected_count else ""
        return (
            f"{summary.kind_label} · {summary.category_label} | "
            f"{summary.file_count} 个文件 | 占用 {format_size(summary.total_bytes)} | "
            f"预估可省 {format_size(summary.reclaimable_bytes)} | "
            f"涉及磁盘 {summary.disk_count} 个：{disks} | "
            f"涉及目录 {summary.directory_count} 个：{directories}{protected_text}\n"
            f"推荐保留：{recommended}（{summary.recommended_reason}）"
        )

    def _group_key(self, group: DuplicateGroup) -> str:
        return f"{group.kind}|{group.title}|{'|'.join(str(record.path) for record in group.records)}"

    def _persist_keep_state(self) -> None:
        if self.current_roots:
            self.database.replace_latest_scan_session(self.current_roots, self.groups)
        self.keep_state_dirty = False

    def _selected_keep_paths(self) -> set[Path]:
        keep_paths: set[Path] = set()
        for group in self.groups:
            if not group.records:
                continue
            group_key = self._group_key(group)
            if group_key in self.manual_keep_group_keys:
                keep_paths.update(record.path for record in group.records if record.keep_selected)
                continue
            keep_paths.add(self._suggest_keep(group.records).path)
        return keep_paths

    def preview_organize_plan(self) -> None:
        roots = self._folder_paths()
        if not roots:
            QMessageBox.information(self, "需要目录", "请先添加至少一个整理目录。")
            return

        self.organize_targets = self.current_organize_targets()
        include_paths = self._selected_keep_paths() if self.organize_keep_only_checkbox.isChecked() else None
        protected_roots = {normalize_path(path) for path in self.protected_roots}
        preview = build_organize_preview(
            roots,
            self.organize_targets,
            include_paths=include_paths,
            exclude_roots=protected_roots,
        )
        protected_skipped = sum(
            1
            for root in roots
            for path in (root.rglob("*") if root.exists() and root.is_dir() else [root])
            if path.is_file() and is_under_any(path, protected_roots)
        )
        self.organize_preview_items = preview
        self.organize_preview_table.setRowCount(len(preview))

        preview_summary = summarize_organize_preview(preview)
        for row, item in enumerate(preview):
            status = describe_organize_preview_item(item)
            values = [
                category_label(item.category),
                status.label,
                format_size(status.size),
                item.source.name,
                str(item.source),
                str(item.target),
            ]
            for column, value in enumerate(values):
                self.organize_preview_table.setItem(row, column, QTableWidgetItem(value))
        self.organize_preview_table.resizeRowsToContents()

        summary_text = (
            f"预览共 {preview_summary.file_count} 个文件"
            f" | {self._category_counts_text(preview_summary)}"
            f" | 总大小 {format_size(preview_summary.total_bytes)}"
            f" | 目标目录 {len(preview_summary.target_directories)} 个"
        )
        if self.organize_keep_only_checkbox.isChecked():
            summary_text += " | 当前仅包含重复组中的保留项"
        if protected_skipped:
            summary_text += f" | 已跳过保护目录 {protected_skipped} 个文件"
        self.organize_summary_label.setText(summary_text)
        self.status_label.setText("整理预览已生成")
        self.refresh_onboarding()
        self._save_settings()

    def execute_organize_plan(self) -> None:
        if not self.organize_preview_items:
            self.organize_summary_label.setText("当前还没有整理预览。请先点“预览整理结果”，确认列表后再执行。")
            QMessageBox.information(self, "没有整理预览", "请先生成整理预览，再执行整理。")
            return

        summary = summarize_organize_preview(self.organize_preview_items)
        reply = QMessageBox.question(
            self,
            "确认整理",
            self._organize_execution_message(summary),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            moved = execute_organize_preview(self.organize_preview_items, self.database)
        except Exception as exc:
            QMessageBox.critical(self, "整理失败", str(exc))
            return

        report = build_organize_result_report(self.organize_preview_items, moved, summary)
        moved_sources = {source for source, _target in moved}
        if moved_sources:
            self._remove_moved_sources_from_groups(moved_sources)

        self.organize_preview_items = []
        self.organize_preview_table.setRowCount(0)
        self.organize_summary_label.setText(f"整理完成，共移动 {len(moved)} 个文件。")
        self.status_label.setText(f"整理完成：{report.completed_count}/{report.planned_count} 个文件")
        self.refresh_onboarding()
        self.refresh_operations()
        if self.history_loaded:
            self.refresh_history()
        QMessageBox.information(self, "整理完成", format_operation_result_report(report))

    def _remove_moved_sources_from_groups(self, moved_sources: set[Path]) -> None:
        updated_groups: list[DuplicateGroup] = []
        for group in self.groups:
            remaining_records = [record for record in group.records if record.path not in moved_sources]
            if len(remaining_records) > 1:
                group.records = remaining_records
                updated_groups.append(group)

        self.groups = updated_groups
        self.manual_keep_group_keys = {
            self._group_key(group)
            for group in self.groups
            if any(record.keep_selected for record in group.records)
        }
        self.keep_state_dirty = False

        self.group_list.clear()
        for group in self.groups:
            item = QListWidgetItem(
                f"{group.title}\n{len(group.records)} 个文件 · 可整理 {format_size(group.reclaimable_bytes)}"
            )
            item.setData(Qt.ItemDataRole.UserRole, group)
            self.group_list.addItem(item)

        self.rebuild_directory_browser()
        self.refresh_group_list()
        self.file_table.setRowCount(0)
        self.thumbnail_list.clear()
        self.group_detail_summary_label.setText("")
        self.directory_detail_table.setRowCount(0)
        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText("选择图片以预览")
        self.detail_label.clear()
        self.media_player.stop()
        self.preview_stack.setCurrentWidget(self.preview_label)
        self.group_status_label.setText(f"当前扫描结果：{len(self.groups)} 组重复/相似文件。")

        if self.current_roots:
            if self.groups:
                self.database.replace_latest_scan_session(self.current_roots, self.groups)
            else:
                if self.restored_session_id is not None:
                    self.database.delete_scan_session(self.restored_session_id)
                self.restored_session_id = None
                self.restored_total_groups = 0
                self.restored_loaded_groups = 0
                self.load_more_groups_button.setEnabled(False)
                self.load_more_groups_button.setText("继续加载更多")

    def save_ai_settings(self) -> None:
        self._save_ai_settings_silent()
        QMessageBox.information(self, "已保存", "AI 配置已保存到本机数据库。")

    def _current_ai_config_validation(self):
        return validate_ai_config(
            self.ai_base_url_input.text(),
            self.ai_model_input.text(),
            self.ai_key_input.text(),
        )

    def check_ai_settings(self) -> None:
        validation = self._current_ai_config_validation()
        if validation.normalized_base_url and validation.normalized_base_url != self.ai_base_url_input.text().strip():
            self.ai_base_url_input.setText(validation.normalized_base_url)
        title = "AI 配置可用" if validation.ok else "AI 配置需要补充"
        QMessageBox.information(self, title, format_ai_config_validation(validation))
        self.status_label.setText("AI 配置检查完成" if validation.ok else "AI 配置需要补充")

    def _save_ai_settings_silent(self) -> None:
        self.database.set_setting("ai_base_url", self.ai_base_url_input.text().strip())
        self.database.set_setting("ai_model", self.ai_model_input.text().strip())
        self.database.set_setting("ai_api_key", self.ai_key_input.text().strip())

    def apply_ai_provider_preset(self, provider: str) -> None:
        presets = {
            "OpenAI": ("https://api.openai.com/v1", "gpt-4o-mini"),
            "DashScope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-vl-plus"),
            "DeepSeek": ("https://api.deepseek.com/v1", "deepseek-chat"),
            "Moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k-vision-preview"),
            "Zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4v-plus"),
        }
        if provider not in presets:
            return
        base_url, model = presets[provider]
        self.ai_base_url_input.setText(base_url)
        self.ai_model_input.setText(model)

    def start_scan(self) -> None:
        roots = self._folder_paths()
        if not roots:
            QMessageBox.information(self, "需要目录", "请先添加至少一个扫描目录。")
            return
        if self.worker and self.worker.isRunning():
            return
        if not self._confirm_scan_roots_are_safe(roots):
            return

        self.group_list.clear()
        self.directory_tree.clear()
        self.directory_detail_table.setRowCount(0)
        self.file_table.setRowCount(0)
        self.group_detail_summary_label.setText("扫描完成后，这里会显示当前重复分组的空间、目录和推荐保留摘要。")
        self.preview_label.setText("扫描中")
        self.detail_label.clear()
        self.progress.setValue(0)
        self.status_label.setText(format_scan_start(len(roots)))
        self.history_state = "正在执行新扫描"
        self.refresh_health_diagnostics()
        self.media_player.stop()
        self.scan_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.current_roots = roots

        self._save_settings()
        self.worker = ScanWorker(
            roots,
            self.database.path,
            self.threshold_spin.value(),
            same_ratio_only=self.same_ratio_checkbox.isChecked(),
            same_resolution_only=self.same_resolution_checkbox.isChecked(),
        )
        self.worker.progress_changed.connect(self.on_progress)
        self.worker.finished_groups.connect(self.on_scan_finished)
        self.worker.failed.connect(self.on_scan_failed)
        self.worker.start()

    def cancel_scan(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.request_cancel()
            self.status_label.setText("正在停止扫描")
            self.cancel_button.setEnabled(False)

    def on_progress(self, message: str, current: int, total: int) -> None:
        formatted = format_progress_message(message, current, total)
        self.status_label.setText(formatted)
        self.history_state = formatted
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(min(max(current, 0), max(total, 1)))

    def on_scan_finished(self, groups: list[DuplicateGroup], restored: bool = False) -> None:
        self.groups = groups
        if not restored:
            self.restored_session_id = None
            self.restored_total_groups = len(groups)
            self.restored_loaded_groups = len(groups)
            self.load_more_groups_button.setEnabled(False)
            self.load_more_groups_button.setText("继续加载更多")
            self.group_status_label.setText(f"当前扫描结果：{len(groups)} 组重复/相似文件。")
        self.manual_keep_group_keys = {
            self._group_key(group)
            for group in groups
            if any(record.keep_selected for record in group.records)
        }
        self.keep_state_dirty = False
        self.refresh_group_list()
        self.rebuild_directory_browser()
        if not restored and self.current_roots:
            self.database.replace_latest_scan_session(self.current_roots, groups)
        if restored:
            self.history_state = "已恢复最近一次扫描"
            self.status_label.setText(f"已恢复最近一次扫描：{len(groups)} 组重复/相似文件")
        else:
            self.history_state = "新扫描已完成"
            self.status_label.setText(f"扫描完成：发现 {len(groups)} 组重复/相似文件")
        self.preview_label.setText("选择图片以预览")
        self.progress.setValue(self.progress.maximum())
        self.scan_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if self.history_loaded and not restored:
            self.refresh_history()
        self.refresh_health_diagnostics()
        self.refresh_onboarding()

    def on_scan_failed(self, message: str) -> None:
        QMessageBox.critical(self, "扫描失败", message)
        self.status_label.setText("扫描失败")
        self.history_state = f"扫描失败：{message}"
        self.scan_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.refresh_health_diagnostics()

    def populate_group(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if self.keep_state_dirty:
            self._persist_keep_state()
        self.file_table.setRowCount(0)
        self.thumbnail_list.clear()
        if not current:
            self.group_detail_summary_label.setText("")
            return
        group = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(group, DuplicateGroup):
            self.group_detail_summary_label.setText("")
            return

        group_key = self._group_key(group)
        manual_selection = group_key in self.manual_keep_group_keys
        keep_result = choose_keep_records(group.records, self.strategy_combo.currentText(), self.protected_roots)
        keep_paths = {record.path for record in keep_result.keep_records}
        keep_reasons = describe_keep_reasons(group.records, self.strategy_combo.currentText(), self.protected_roots)
        summary = summarize_duplicate_group(group, self.protected_roots, self.strategy_combo.currentText())
        self.group_detail_summary_label.setText(self._duplicate_group_summary_text(summary))
        self.file_table.setRowCount(len(group.records))
        self.updating_keep_checks = True
        for row, record in enumerate(group.records):
            keep_item = QTableWidgetItem()
            keep_item.setFlags(keep_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = record.keep_selected if manual_selection else (record.path in keep_paths)
            keep_item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            keep_item.setData(Qt.ItemDataRole.UserRole, record)
            record.keep_selected = checked
            self.file_table.setItem(row, 0, keep_item)
            protected_item = QTableWidgetItem("保护" if is_under_any(record.path, self.protected_roots) else "")
            if protected_item.text():
                protected_item.setForeground(QColor("#1a7f37"))
                protected_item.setToolTip("保护目录内文件不会被批量移动或回收。")
            self.file_table.setItem(row, 1, protected_item)
            self.file_table.setItem(row, 2, QTableWidgetItem(record.name))
            self.file_table.setItem(row, 3, QTableWidgetItem(format_size(record.size)))
            modified = datetime.fromtimestamp(record.modified_at).strftime("%Y-%m-%d %H:%M")
            self.file_table.setItem(row, 4, QTableWidgetItem(modified))
            dimensions = f"{record.width}x{record.height}" if record.width and record.height else "-"
            self.file_table.setItem(row, 5, QTableWidgetItem(dimensions))
            self.file_table.setItem(row, 6, QTableWidgetItem(f"{group.score:.0%}" if group.kind == "similar" else "100%"))
            if manual_selection:
                reason = "手动保留" if checked else "手动未保留：可处理"
                if not checked and is_under_any(record.path, self.protected_roots):
                    reason = "保护目录：批量处理会跳过"
            else:
                reason = keep_reasons.get(record.path, "")
            reason_item = QTableWidgetItem(reason)
            if reason.startswith("建议保留") or reason.startswith("保护目录"):
                reason_item.setForeground(QColor("#1a7f37"))
            self.file_table.setItem(row, 7, reason_item)
            self.file_table.setItem(row, 8, QTableWidgetItem(str(record.path)))
            self._add_thumbnail(record, row)
        self.updating_keep_checks = False
        self.file_table.resizeRowsToContents()
        self.file_table.selectRow(0)

    def _add_thumbnail(self, record: FileRecord, row: int) -> None:
        if not is_image(record.path) or not record.path.exists():
            return
        pixmap = QPixmap(str(record.path))
        if pixmap.isNull():
            return
        icon = QIcon(pixmap.scaled(128, 128, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        item = QListWidgetItem(icon, record.name)
        item.setToolTip(str(record.path))
        item.setData(Qt.ItemDataRole.UserRole, row)
        self.thumbnail_list.addItem(item)

    def select_thumbnail_record(self, item: QListWidgetItem) -> None:
        row = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(row, int):
            self.file_table.selectRow(row)

    def compare_current_group_images(self) -> None:
        current = self.group_list.currentItem()
        if current is None:
            QMessageBox.information(self, "需要分组", "请先选择一个重复分组。")
            return
        group = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(group, DuplicateGroup):
            QMessageBox.information(self, "需要分组", "请先选择一个重复分组。")
            return

        items = build_image_compare_items(group.records, self.protected_roots, self.strategy_combo.currentText())
        if len(items) < 2:
            QMessageBox.information(self, "图片不足", "当前分组至少需要两张图片才能并排对比。")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"图片对比 - {group.title}")
        dialog.resize(1120, 760)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        header = QLabel(
            f"{len(items)} 张图片 | 推荐保留项会排在前面 | 当前策略：{self.strategy_combo.currentText()}"
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(12)
        for index, item in enumerate(items):
            grid.addWidget(self._build_image_compare_card(item), index // 2, index % 2)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.accept)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)
        dialog.exec()

    def _build_image_compare_card(self, item: ImageCompareItem) -> QWidget:
        card = QWidget()
        card.setObjectName("ImageCompareCard")
        card.setStyleSheet(
            "#ImageCompareCard { background: #ffffff; border: 1px solid #d0d7de; border-radius: 8px; }"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        image_label = QLabel()
        image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image_label.setMinimumSize(420, 300)
        image_label.setStyleSheet("background: #f6f8fa; border-radius: 6px;")
        if item.details.exists and is_image(item.record.path):
            pixmap = QPixmap(str(item.record.path))
            if not pixmap.isNull():
                image_label.setPixmap(
                    pixmap.scaled(
                        420,
                        300,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            else:
                image_label.setText("图片无法预览")
        else:
            image_label.setText("图片不存在")
        layout.addWidget(image_label)

        details = QLabel(self._image_compare_item_text(item))
        details.setWordWrap(True)
        layout.addWidget(details)
        return card

    def _image_compare_item_text(self, item: ImageCompareItem) -> str:
        badges = []
        if item.recommended:
            badges.append("建议保留")
        if item.protected:
            badges.append("保护目录")
        badge_text = f"[{' | '.join(badges)}] " if badges else ""
        dimensions = (
            f"{item.details.width} x {item.details.height}"
            if item.details.width and item.details.height
            else "-"
        )
        megapixels = f" · {item.details.megapixels:.1f} MP" if item.details.megapixels is not None else ""
        modified = datetime.fromtimestamp(item.details.modified_at).strftime("%Y-%m-%d %H:%M:%S")
        reason = item.reason or "未命中推荐规则"
        return (
            f"{badge_text}{item.record.name}\n"
            f"尺寸：{dimensions}{megapixels}\n"
            f"大小：{format_size(item.details.size)}\n"
            f"修改时间：{modified}\n"
            f"原因：{reason}\n"
            f"路径：{item.record.path}"
        )

    def on_keep_item_changed(self, item: QTableWidgetItem) -> None:
        if self.updating_keep_checks or item.column() != 0:
            return
        record = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(record, FileRecord):
            return
        current = self.group_list.currentItem()
        if not current:
            return
        group = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(group, DuplicateGroup):
            return
        record.keep_selected = item.checkState() == Qt.CheckState.Checked
        self.manual_keep_group_keys.add(self._group_key(group))
        self.keep_state_dirty = True

    def reapply_keep_strategy(self) -> None:
        current = self.group_list.currentItem()
        if current:
            group = current.data(Qt.ItemDataRole.UserRole)
            if isinstance(group, DuplicateGroup):
                group_key = self._group_key(group)
                self.manual_keep_group_keys.discard(group_key)
                for record in group.records:
                    record.keep_selected = False
                self.keep_state_dirty = True
            self.populate_group(current, None)

    def preview_bulk_keep_rule(self) -> None:
        groups, _min_size, _max_size = self._visible_groups()
        if not groups:
            self.bulk_rule_hint_label.setText("当前筛选下没有可应用规则的重复分组。")
            return
        rule_label = self.bulk_keep_rule_combo.currentText()
        keyword = self.bulk_keep_keyword_input.text().strip()
        protected_hits = 0
        keep_count = 0
        for group in groups:
            result = choose_keep_records(group.records, rule_label, self.protected_roots, keyword=keyword)
            keep_count += len(result.keep_records)
            protected_hits += sum(1 for record in result.keep_records if is_under_any(record.path, self.protected_roots))
        keyword_text = f"，关键词“{keyword}”" if keyword else ""
        self.bulk_rule_hint_label.setText(
            f"预览：规则“{rule_label}”{keyword_text}将作用于当前筛选的 {len(groups)} 组，保留 {keep_count} 个文件，其中保护目录命中 {protected_hits} 个。"
        )

    def apply_bulk_keep_rule(self) -> None:
        groups, _min_size, _max_size = self._visible_groups()
        if not groups:
            QMessageBox.information(self, "没有可应用分组", "当前筛选下没有重复分组。")
            return
        rule_label = self.bulk_keep_rule_combo.currentText()
        keyword = self.bulk_keep_keyword_input.text().strip()
        changed_groups = 0
        keep_count = 0
        for group in groups:
            result = choose_keep_records(group.records, rule_label, self.protected_roots, keyword=keyword)
            keep_paths = {record.path for record in result.keep_records}
            if not keep_paths:
                continue
            for record in group.records:
                record.keep_selected = record.path in keep_paths
            self.manual_keep_group_keys.add(self._group_key(group))
            changed_groups += 1
            keep_count += len(keep_paths)

        self.keep_state_dirty = True
        self._persist_keep_state()
        self.refresh_group_list()
        keyword_text = f"，关键词“{keyword}”" if keyword else ""
        self.bulk_rule_hint_label.setText(
            f"已应用规则“{rule_label}”{keyword_text}：更新 {changed_groups} 组，保留 {keep_count} 个文件。"
        )
        self.status_label.setText("批量保留规则已应用")
        self.refresh_onboarding()

    def _suggest_keep(self, records: list[FileRecord]) -> FileRecord:
        result = choose_keep_records(records, self.strategy_combo.currentText(), self.protected_roots)
        return result.keep_records[0]

    def preview_selected_file(self) -> None:
        items = self.file_table.selectedItems()
        if not items:
            return
        row = items[0].row()
        keep_item = self.file_table.item(row, 0)
        if not keep_item:
            return
        record = keep_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(record, FileRecord):
            return

        details = inspect_file_preview(record)
        self.detail_label.setText(self._file_preview_detail_text(record, details))
        if is_image(record.path) and record.path.exists():
            self.media_player.stop()
            self.preview_stack.setCurrentWidget(self.preview_label)
            pixmap = QPixmap(str(record.path))
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    self.preview_label.width(),
                    300,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.preview_label.setPixmap(scaled)
                return
        if is_video(record.path) and record.path.exists():
            self.preview_label.setPixmap(QPixmap())
            self.preview_stack.setCurrentWidget(self.video_widget)
            self.media_player.setSource(QUrl.fromLocalFile(str(record.path)))
            self.media_player.play()
            return
        self.media_player.stop()
        self.preview_stack.setCurrentWidget(self.preview_label)
        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText("此文件暂无预览")

    def move_unchecked(self) -> None:
        if self.keep_state_dirty:
            self._persist_keep_state()
        groups, _min_size, _max_size = self._visible_groups()
        if not groups:
            QMessageBox.information(self, "没有可处理分组", "当前筛选下没有重复分组。")
            return

        plan = build_processing_plan(groups, {normalize_path(path) for path in self.protected_roots})
        if plan.invalid_groups:
            first = plan.invalid_groups[0]
            self.group_mode_combo.setCurrentIndex(0)
            for row in range(self.group_list.count()):
                item = self.group_list.item(row)
                if item.data(Qt.ItemDataRole.UserRole) is first:
                    self.group_list.setCurrentRow(row)
                    break
            QMessageBox.warning(
                self,
                "需要至少保留一个文件",
                f"当前筛选结果中有 {len(plan.invalid_groups)} 组没有勾选保留文件。请先应用保留规则或手动勾选。",
            )
            return

        selected_to_move = plan.records_to_process
        if not selected_to_move:
            QMessageBox.information(
                self,
                "没有要处理的文件",
                "当前筛选结果中没有可移动/回收的文件。保护目录内文件会自动跳过。",
            )
            return

        action_label = self.action_combo.currentText()
        risk_summary = build_processing_risk_summary(
            plan,
            action_label,
            self.destination,
        )
        reply = QMessageBox.question(
            self,
            "确认处理",
            format_processing_confirmation(risk_summary),
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            moved_targets: list[Path] = []
            if action_label == "移动到回收站":
                moved_paths = trash_records(selected_to_move, self.database)
                moved_count = len(moved_paths)
                moved_sources = set(moved_paths)
            else:
                moved = move_records(selected_to_move, self.destination, "当前筛选结果", self.database)
                moved_count = len(moved)
                moved_sources = {source for source, _target in moved}
                moved_targets = [target for _source, target in moved]
        except Exception as exc:
            QMessageBox.critical(self, "处理失败", str(exc))
            return

        report = build_processing_result_report(plan, action_label, moved_sources, moved_targets)
        self.status_label.setText(f"处理完成：{moved_count}/{report.planned_count} 个文件")
        QMessageBox.information(self, "处理完成", format_operation_result_report(report))
        self.refresh_operations()
        self._remove_moved_sources_from_groups(moved_sources)

    def refresh_operations(self) -> None:
        all_operations, operations = self._filtered_operations()
        summary = summarize_operations(all_operations, operations)
        self.operation_summary_label.setText(format_operation_filter_summary(summary))
        self.operations_table.setRowCount(len(operations))
        for row, operation in enumerate(operations):
            self._set_operation_row(row, operation)
        self.operations_table.resizeRowsToContents()

    def _filtered_operations(self) -> tuple[list[OperationRecord], list[OperationRecord]]:
        all_operations = self.database.list_operations(limit=10000)
        return all_operations, filter_operations(
            all_operations,
            self.operation_action_filter_combo.currentText(),
            self.operation_status_filter_combo.currentText(),
            self.operation_search_input.text(),
        )

    def refresh_history(self) -> None:
        sessions = self._sorted_scan_sessions(self.database.list_scan_sessions())
        self.scan_session_summaries = list(sessions)
        self.session_table.setRowCount(len(sessions))
        for row, session in enumerate(sessions):
            created = datetime.fromtimestamp(session.created_at).strftime("%Y-%m-%d %H:%M:%S")
            roots = ", ".join(str(path) for path in session.root_paths[:2])
            if len(session.root_paths) > 2:
                roots += " ..."
            values = [
                created,
                roots,
                str(session.group_count),
                str(session.file_count),
                format_size(session.duplicate_bytes),
                format_size(session.reclaimable_bytes),
                str(session.id),
            ]
            for column, value in enumerate(values):
                self.session_table.setItem(row, column, QTableWidgetItem(value))
        self.session_table.resizeRowsToContents()
        self.apply_session_filter()

        records = self.database.list_scanned_files()
        self.history_table.setRowCount(len(records))
        for row, record in enumerate(records):
            modified = datetime.fromtimestamp(record.modified_at).strftime("%Y-%m-%d %H:%M")
            dimensions = f"{record.width}x{record.height}" if record.width and record.height else "-"
            values = [
                record.name,
                format_size(record.size),
                modified,
                dimensions,
                record.sha256 or "",
                str(record.image_hash) if record.image_hash is not None else "",
                str(record.path),
            ]
            for column, value in enumerate(values):
                self.history_table.setItem(row, column, QTableWidgetItem(value))
        self.history_table.resizeRowsToContents()

    def _sorted_scan_sessions(self, sessions):
        sort_label = self.session_sort_combo.currentText()
        if sort_label == "按时间：最早优先":
            return sorted(sessions, key=lambda item: item.created_at)
        if sort_label == "按预估可省：从大到小":
            return sorted(sessions, key=lambda item: item.reclaimable_bytes, reverse=True)
        if sort_label == "按占用空间：从大到小":
            return sorted(sessions, key=lambda item: item.duplicate_bytes, reverse=True)
        if sort_label == "按分组数：从多到少":
            return sorted(sessions, key=lambda item: item.group_count, reverse=True)
        return sorted(sessions, key=lambda item: item.created_at, reverse=True)

    def _scan_session_matches_filter(self, session) -> bool:
        keyword = self.session_filter_input.text().strip().lower()
        if keyword:
            created = datetime.fromtimestamp(session.created_at).strftime("%Y-%m-%d %H:%M:%S")
            row_values = [
                created,
                " ".join(str(path) for path in session.root_paths),
                str(session.group_count),
                str(session.file_count),
                format_size(session.duplicate_bytes),
                format_size(session.reclaimable_bytes),
            ]
            if keyword not in " | ".join(row_values).lower():
                return False

        range_label = self.session_range_combo.currentText()
        now_ts = datetime.now().timestamp()
        if range_label == "最近 7 天":
            return (now_ts - session.created_at) <= 7 * 24 * 60 * 60
        if range_label == "最近 30 天":
            return (now_ts - session.created_at) <= 30 * 24 * 60 * 60
        return True

    def apply_session_filter(self) -> None:
        keyword = self.session_filter_input.text().strip().lower()
        range_label = self.session_range_combo.currentText()
        now_ts = datetime.now().timestamp()
        max_age_seconds: float | None = None
        if range_label == "最近 7 天":
            max_age_seconds = 7 * 24 * 60 * 60
        elif range_label == "最近 30 天":
            max_age_seconds = 30 * 24 * 60 * 60

        visible_count = 0
        for row in range(self.session_table.rowCount()):
            row_values: list[str] = []
            for column in range(6):
                item = self.session_table.item(row, column)
                if item is not None:
                    row_values.append(item.text().lower())

            keyword_matched = not keyword or keyword in " | ".join(row_values)
            time_matched = True
            if max_age_seconds is not None:
                created_item = self.session_table.item(row, 0)
                if created_item is not None:
                    try:
                        created_at = datetime.strptime(created_item.text(), "%Y-%m-%d %H:%M:%S").timestamp()
                        time_matched = (now_ts - created_at) <= max_age_seconds
                    except ValueError:
                        time_matched = False

            matched = keyword_matched and time_matched
            self.session_table.setRowHidden(row, not matched)
            if matched:
                visible_count += 1

        if keyword or max_age_seconds is not None:
            parts: list[str] = []
            if keyword:
                parts.append(f"关键词“{self.session_filter_input.text().strip()}”")
            if max_age_seconds is not None:
                parts.append(range_label)
            self.session_summary_label.setText(f"筛选结果：{visible_count} 条扫描任务匹配 {' + '.join(parts)}。")
        elif self.session_table.currentRow() < 0:
            self.session_summary_label.setText("这里会显示历史扫描任务，双击任务可直接加载。")

    def on_session_selection_changed(self) -> None:
        row = self.session_table.currentRow()
        if row < 0:
            return
        created_item = self.session_table.item(row, 0)
        roots_item = self.session_table.item(row, 1)
        groups_item = self.session_table.item(row, 2)
        files_item = self.session_table.item(row, 3)
        size_item = self.session_table.item(row, 4)
        save_item = self.session_table.item(row, 5)
        if not all([created_item, roots_item, groups_item, files_item, size_item, save_item]):
            return
        self.session_summary_label.setText(
            f"扫描时间：{created_item.text()} | 目录：{roots_item.text()} | 重复分组：{groups_item.text()} | 重复文件：{files_item.text()} | 当前占用：{size_item.text()} | 预估可省：{save_item.text()}"
        )

    def on_session_item_double_clicked(self, _item) -> None:
        self.load_selected_scan_session()

    def load_selected_scan_session(self) -> None:
        row = self.session_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "没有选中任务", "请先在扫描历史中选中一条扫描任务。")
            return
        id_item = self.session_table.item(row, 6)
        if id_item is None:
            return
        session_id = int(id_item.text())
        self.restored_session_id = session_id
        self.restored_loaded_groups = 0
        self.group_status_label.setText("正在加载选中的历史扫描任务...")
        self.status_label.setText("正在加载历史扫描任务")
        self.group_list.clear()
        self.directory_tree.clear()
        self.directory_detail_table.setRowCount(0)
        self.file_table.setRowCount(0)
        self.group_detail_summary_label.setText("正在加载历史扫描任务...")
        self.thumbnail_list.clear()
        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText("选择图片以预览")
        self.detail_label.clear()
        self.media_player.stop()
        self.preview_stack.setCurrentWidget(self.preview_label)
        self.start_restore_session(session_id=session_id, offset_groups=0)

    def delete_selected_scan_session(self) -> None:
        row = self.session_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "没有选中任务", "请先在扫描历史中选中一条扫描任务。")
            return
        id_item = self.session_table.item(row, 6)
        created_item = self.session_table.item(row, 0)
        if id_item is None:
            return
        session_id = int(id_item.text())
        created_text = created_item.text() if created_item is not None else str(session_id)
        reply = QMessageBox.question(
            self,
            "确认删除",
            f"将删除这条扫描任务记录及其保存的重复分组结果：\n{created_text}\n\n此操作不会删除你的实际文件，是否继续？",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.database.delete_scan_session(session_id)
        if self.restored_session_id == session_id:
            self.restored_session_id = None
            self.restored_total_groups = 0
            self.restored_loaded_groups = 0
            self.groups = []
            self.group_list.clear()
            self.directory_tree.clear()
            self.directory_detail_table.setRowCount(0)
            self.file_table.setRowCount(0)
            self.thumbnail_list.clear()
            self.group_detail_summary_label.setText("")
            self.group_status_label.setText(f"已删除当前扫描任务，当前没有可恢复的扫描结果。\n{EMPTY_SCAN_GUIDANCE}")
            self.load_more_groups_button.setEnabled(False)
            self.load_more_groups_button.setText("继续加载更多")
        self.refresh_history()
        self.session_summary_label.setText("这里会显示历史扫描任务。")
        self.status_label.setText("已删除选中的扫描任务")

    def clear_history(self) -> None:
        reply = QMessageBox.question(
            self,
            "确认清空",
            "这会清空扫描历史和缓存的文件指纹，不会删除你的实际文件。是否继续？",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.database.clear_scanned_files()
        self.database.clear_scan_sessions()
        self.groups = []
        self.group_status_label.setText(f"扫描历史已清空，当前没有可恢复的扫描结果。\n{EMPTY_SCAN_GUIDANCE}")
        self.group_list.clear()
        self.directory_tree.clear()
        self.directory_detail_table.setRowCount(0)
        self.session_table.setRowCount(0)
        self.session_summary_label.setText("这里会显示历史扫描任务。")
        self.file_table.setRowCount(0)
        self.thumbnail_list.clear()
        self.group_detail_summary_label.setText("")
        self.refresh_history()
        self.status_label.setText("扫描历史已清空")

    def _set_operation_row(self, row: int, operation: OperationRecord) -> None:
        status = "已撤销" if operation.is_undone else ("不可撤销" if operation.action == "trash" else "可撤销")
        created = datetime.fromtimestamp(operation.created_at).strftime("%Y-%m-%d %H:%M:%S")
        values = [
            status,
            "回收站" if operation.action == "trash" else "移动",
            created,
            str(operation.source_path),
            str(operation.target_path),
            str(operation.id),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, operation.id)
            self.operations_table.setItem(row, column, item)

    def selected_operation(self) -> OperationRecord | None:
        selected = self.operations_table.selectedItems()
        if not selected:
            return None
        row = selected[0].row()
        id_item = self.operations_table.item(row, 5)
        if not id_item:
            return None
        operation = self.database.get_operation(int(id_item.text()))
        if operation is None:
            QMessageBox.warning(self, "记录不存在", "这条操作记录已经不存在。")
            self.refresh_operations()
        return operation

    def undo_selected_operation(self) -> None:
        operation = self.selected_operation()
        if operation is None:
            QMessageBox.information(self, "没有选中记录", "请先在操作记录中选中一条移动记录。")
            return
        if operation.action != "move":
            QMessageBox.information(self, "不可撤销", "移动到回收站的记录请在 Windows 回收站中还原。")
            return

        reply = QMessageBox.question(
            self,
            "确认撤销",
            f"将文件移回原位置：\n{operation.source_path}",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            restored = undo_move(operation, self.database)
        except Exception as exc:
            QMessageBox.critical(self, "撤销失败", str(exc))
            return

        QMessageBox.information(self, "撤销完成", f"文件已恢复到：\n{restored}")
        self.refresh_operations()

    def reveal_selected_operation(self, location: str) -> None:
        operation = self.selected_operation()
        if operation is None:
            QMessageBox.information(self, "没有选中记录", "请先在操作记录中选中一条记录。")
            return
        target = operation_reveal_path(operation, location)
        try:
            reveal_in_file_manager(target)
        except Exception as exc:
            QMessageBox.warning(self, "无法打开位置", str(exc))
            return
        label = "当前位置" if location == "current" else "原目录"
        self.status_label.setText(f"已打开操作记录{label}：{target}")

    def export_operations(self) -> None:
        target, _ = QFileDialog.getSaveFileName(self, "导出操作记录", "operations.csv", "CSV Files (*.csv)")
        if not target:
            return
        try:
            _all_operations, operations = self._filtered_operations()
            if not operations:
                QMessageBox.information(self, "没有可导出内容", "当前操作记录筛选结果为空。")
                return
            export_operations_csv(operations, Path(target))
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出完成", target)

    def export_scan_sessions(self) -> None:
        target, _ = QFileDialog.getSaveFileName(self, "导出扫描任务记录", "scan-sessions.csv", "CSV Files (*.csv)")
        if not target:
            return
        try:
            sessions = self._sorted_scan_sessions(self.database.list_scan_sessions(limit=10000))
            visible_sessions = [session for session in sessions if self._scan_session_matches_filter(session)]
            if not visible_sessions:
                QMessageBox.information(self, "没有可导出内容", "当前扫描任务筛选结果为空。")
                return
            export_scan_sessions_csv(visible_sessions, Path(target))
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出完成", target)

    def export_history(self) -> None:
        target, _ = QFileDialog.getSaveFileName(self, "导出文件缓存", "scanned-files.csv", "CSV Files (*.csv)")
        if not target:
            return
        try:
            export_files_csv(self.database.list_scanned_files(limit=100000), Path(target))
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出完成", target)

    def selected_record(self) -> FileRecord | None:
        items = self.file_table.selectedItems()
        if not items:
            return None
        keep_item = self.file_table.item(items[0].row(), 0)
        if not keep_item:
            return None
        record = keep_item.data(Qt.ItemDataRole.UserRole)
        return record if isinstance(record, FileRecord) else None

    def reveal_selected_file(self) -> None:
        record = self.selected_record()
        if record is None:
            QMessageBox.information(self, "没有选中文件", "请先在重复处理页选中一个文件。")
            return
        try:
            reveal_in_file_manager(record.path)
        except Exception as exc:
            QMessageBox.warning(self, "无法打开所在文件夹", str(exc))
            return
        self.status_label.setText(f"已打开所在文件夹：{record.path.parent}")

    def _file_preview_detail_text(self, record: FileRecord, details: FilePreviewDetails) -> str:
        lines = [
            f"文件：{record.name}",
            f"类型：{details.category_label} · {record.suffix or '无扩展名'}",
            f"大小：{format_size(details.size)}",
            f"修改时间：{datetime.fromtimestamp(details.modified_at).strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if details.width and details.height:
            megapixels = details.megapixels
            megapixel_text = f" · {megapixels:.1f} MP" if megapixels is not None else ""
            lines.append(f"尺寸：{details.width} x {details.height}{megapixel_text}")
        if details.taken_at is not None:
            lines.append(f"拍摄时间：{details.taken_at.strftime('%Y-%m-%d %H:%M:%S')}")
        if details.camera:
            lines.append(f"相机：{details.camera}")
        if is_under_any(record.path, self.protected_roots):
            lines.append("保护目录：是，批量处理会跳过此文件")
        if not details.exists:
            lines.append("状态：文件当前不存在，可能已被移动、删除或磁盘未连接")
        lines.append(f"路径：{record.path}")
        return "\n".join(lines)

    def analyze_selected_image(self) -> None:
        record = self.selected_record()
        if record is None or not is_image(record.path) or not record.path.exists():
            QMessageBox.information(self, "需要图片", "请先在重复处理页选中一张图片。")
            return
        self.start_ai_analysis([record.path])

    def analyze_current_group(self) -> None:
        current = self.group_list.currentItem()
        if not current:
            QMessageBox.information(self, "需要分组", "请先选择一个重复分组。")
            return
        group = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(group, DuplicateGroup):
            return
        paths = [record.path for record in group.records if is_image(record.path) and record.path.exists()]
        if not paths:
            QMessageBox.information(self, "没有图片", "当前分组没有可分析的图片。")
            return
        self.start_ai_analysis(paths)

    def start_ai_analysis(self, paths: list[Path]) -> None:
        validation = self._current_ai_config_validation()
        if not validation.ok:
            QMessageBox.information(self, "需要 AI 配置", format_ai_config_validation(validation))
            return
        if self.ai_worker and self.ai_worker.isRunning():
            return

        if validation.normalized_base_url != self.ai_base_url_input.text().strip():
            self.ai_base_url_input.setText(validation.normalized_base_url)
        base_url = validation.normalized_base_url
        model = self.ai_model_input.text().strip()
        api_key = self.ai_key_input.text().strip()
        self._save_ai_settings_silent()
        self.ai_analyze_button.setEnabled(False)
        self.ai_batch_button.setEnabled(False)
        self.status_label.setText(f"AI 准备分析 {len(paths)} 张图片")
        self.ai_worker = AiWorker(paths, AiConfig(base_url=base_url, api_key=api_key, model=model))
        self.ai_worker.progress_changed.connect(self.on_ai_progress)
        self.ai_worker.finished_analysis.connect(self.on_ai_finished)
        self.ai_worker.finished_batch.connect(self.on_ai_batch_finished)
        self.ai_worker.failed.connect(self.on_ai_failed)
        self.ai_worker.start()

    def on_ai_progress(self, message: str, current: int, total: int) -> None:
        self.status_label.setText(message)
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(current)

    def on_ai_finished(self, path: Path, result) -> None:
        model = self.ai_model_input.text().strip()
        self.database.save_ai_analysis(path, model, result.summary, result.score, result.raw_response)
        self.refresh_ai_results()

    def on_ai_batch_finished(self, count: int) -> None:
        self.ai_analyze_button.setEnabled(True)
        self.ai_batch_button.setEnabled(True)
        self.status_label.setText(f"AI 分析完成：{count} 张图片")
        QMessageBox.information(self, "AI 分析完成", f"已完成 {count} 张图片分析。")

    def on_ai_failed(self, message: str) -> None:
        self.ai_analyze_button.setEnabled(True)
        self.ai_batch_button.setEnabled(True)
        self.status_label.setText("AI 分析失败")
        QMessageBox.critical(self, "AI 分析失败", message)

    def refresh_ai_results(self) -> None:
        max_score = self.ai_score_filter_spin.value()
        records = self.database.list_ai_analysis(max_score=float(max_score) if max_score < 100 else None)
        self.ai_table.setRowCount(len(records))
        for row, record in enumerate(records):
            created = datetime.fromtimestamp(record.created_at).strftime("%Y-%m-%d %H:%M:%S")
            values = [
                created,
                record.model,
                "" if record.score is None else f"{record.score:.0f}",
                record.summary,
                str(record.path),
            ]
            for column, value in enumerate(values):
                self.ai_table.setItem(row, column, QTableWidgetItem(value))
        self.ai_table.resizeRowsToContents()

    def export_ai_results(self) -> None:
        target, _ = QFileDialog.getSaveFileName(self, "导出 AI 结果", "ai-results.csv", "CSV Files (*.csv)")
        if not target:
            return
        try:
            export_ai_analysis_csv(self.database.list_ai_analysis(limit=100000), Path(target))
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出完成", target)

    def organize_by_rule(self) -> None:
        roots = self._folder_paths()
        if not roots:
            QMessageBox.information(self, "需要目录", "请先添加至少一个整理目录。")
            return
        rule_label = self.organize_rule_combo.currentText()
        rule_map = {
            "按拍摄日期": "date_day",
            "按文件类型": "type",
            "按相机型号": "camera",
        }
        rule = rule_map.get(rule_label, "date_month")

        reply = QMessageBox.question(
            self,
            "确认整理",
            f"将按“{rule_label}”移动文件到：\n{self.destination}",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        total_moved = 0
        try:
            for root in roots:
                moved = organize_files(root, self.destination, rule, exclude_roots=self.protected_roots)
                total_moved += len(moved)
        except Exception as exc:
            QMessageBox.critical(self, "整理失败", str(exc))
            return

        self._save_settings()
        QMessageBox.information(self, "整理完成", f"已整理 {total_moved} 个文件。")
        self.status_label.setText(f"整理完成：{total_moved} 个文件")


def run() -> None:
    log_path = setup_logging()
    install_exception_logging()
    logger = logging.getLogger(__name__)
    safe_mode = "--safe-mode" in sys.argv or "/safe-mode" in sys.argv
    logger.info("Application startup requested; log=%s safe_mode=%s", log_path, safe_mode)
    set_windows_app_user_model_id()
    try:
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(True)
        instance_lock = QLockFile(str(default_lock_path()))
        instance_lock.setStaleLockTime(0)
        if not instance_lock.tryLock(100):
            logger.warning("Startup blocked because another instance is already running")
            QMessageBox.information(None, APP_NAME, "Photo Manage GPT 已经在运行，请切换到已打开的窗口。")
            return
        icon = load_app_icon()
        if not icon.isNull():
            app.setWindowIcon(icon)
        window = MainWindow(safe_mode=safe_mode)
        logger.info("Main window created")
        window.show()
        window.raise_()
        window.activateWindow()
        logger.info("Main window shown")
        exit_code = app.exec()
        logger.info("Application exited with code %s", exit_code)
        sys.exit(exit_code)
    except Exception:
        logger.exception("Fatal startup failure")
        raise
