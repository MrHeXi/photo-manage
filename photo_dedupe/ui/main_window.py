from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QTimer, Signal, QUrl
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from photo_dedupe.core.ai_client import AiConfig, analyze_image
from photo_dedupe.core.exporter import export_ai_analysis_csv, export_files_csv, export_operations_csv
from photo_dedupe.core.file_ops import move_records, trash_records, undo_move
from photo_dedupe.core.hashers import is_image, is_video
from photo_dedupe.core.models import DuplicateGroup, FileRecord, OperationRecord
from photo_dedupe.core.organizer import organize_files
from photo_dedupe.core.scanner import DuplicateScanner
from photo_dedupe.core.storage import AppDatabase


def format_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


class ScanWorker(QThread):
    progress_changed = Signal(str, int, int)
    finished_groups = Signal(object)
    failed = Signal(str)

    def __init__(self, roots: list[Path], database_path: Path, similarity_threshold: int) -> None:
        super().__init__()
        self.roots = roots
        self.database_path = database_path
        self.similarity_threshold = similarity_threshold
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
                progress=self.progress_changed.emit,
                should_cancel=self.should_cancel,
            )
            self.finished_groups.emit(groups)
        except Exception as exc:
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
            self.failed.emit(str(exc))


class RestoreSessionWorker(QThread):
    finished_restore = Signal(object, object)
    failed = Signal(str)

    def __init__(self, database_path: Path) -> None:
        super().__init__()
        self.database_path = database_path

    def run(self) -> None:
        database = AppDatabase(self.database_path)
        try:
            session = database.get_latest_scan_session()
            if session is None:
                self.finished_restore.emit(None, {"groups": [], "total_groups": 0})
                return
            total_groups = database.count_scan_session_groups(session.id)
            groups = database.load_scan_session_groups(session.id, limit_groups=500, limit_items_per_group=200)
            self.finished_restore.emit(session, {"groups": groups, "total_groups": total_groups})
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            database.close()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Photo Manage GPT")
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
        self.destination = Path(self.database.get_setting("destination", str(self._default_destination())))

        self.folder_list = QListWidget()
        self.group_list = QListWidget()
        self.thumbnail_list = QListWidget()
        self.file_table = QTableWidget(0, 7)
        self.operations_table = QTableWidget(0, 6)
        self.history_table = QTableWidget(0, 7)
        self.ai_table = QTableWidget(0, 5)
        self.action_combo = QComboBox()
        self.organize_rule_combo = QComboBox()
        self.strategy_combo = QComboBox()
        self.ai_provider_combo = QComboBox()
        self.ai_score_filter_spin = QSpinBox()
        self.threshold_spin = QSpinBox()
        self.ai_base_url_input = QLineEdit()
        self.ai_model_input = QLineEdit()
        self.ai_key_input = QLineEdit()
        self.ai_analyze_button = QPushButton("AI 判断当前图片")
        self.ai_batch_button = QPushButton("AI 判断当前分组")
        self.destination_label = QLabel("")
        self.retention_hint_label = QLabel("勾选的文件会保留在原位置；未勾选的文件会按下方处理方式移动。")
        self.scan_button = QPushButton("开始扫描")
        self.cancel_button = QPushButton("停止扫描")
        self.preview_label = QLabel("选择图片以预览")
        self.video_widget = QVideoWidget()
        self.preview_stack = QStackedWidget()
        self.detail_label = QLabel("")
        self.status_label = QLabel("准备就绪")
        self.progress = QProgressBar()
        self.media_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.setVideoOutput(self.video_widget)

        for list_widget in (self.folder_list, self.group_list, self.thumbnail_list):
            list_widget.setAlternatingRowColors(False)
            list_widget.setSpacing(3)

        self._build_ui()
        self._load_settings()
        self._apply_styles()
        self._update_destination_label()
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
        self.start_restore_session()

    def start_restore_session(self) -> None:
        if self.restore_worker and self.restore_worker.isRunning():
            return
        self.restore_worker = RestoreSessionWorker(self.database.path)
        self.restore_worker.finished_restore.connect(self.on_restore_finished)
        self.restore_worker.failed.connect(self.on_restore_failed)
        self.restore_worker.start()

    def on_restore_finished(self, session, payload) -> None:
        if session is None:
            self.status_label.setText("准备就绪")
            return
        groups = payload["groups"]
        total_groups = int(payload["total_groups"])
        self.current_roots = session.root_paths
        existing = {self.folder_list.item(index).text() for index in range(self.folder_list.count())}
        for root in session.root_paths:
            root_text = str(root)
            if root_text not in existing:
                self.folder_list.addItem(root_text)
                existing.add(root_text)
        if groups:
            self.on_scan_finished(groups, restored=True)
            if total_groups > len(groups):
                self.status_label.setText(
                    f"已恢复最近扫描的前 {len(groups)} 组，共 {total_groups} 组。建议重新扫描以获取完整结果。"
                )
        else:
            self.status_label.setText("准备就绪")

    def on_restore_failed(self, message: str) -> None:
        self.status_label.setText("启动恢复失败")

    def _default_destination(self) -> Path:
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
        return desktop / "PhotoManageGPT_重复文件"

    def _build_ui(self) -> None:
        add_button = QPushButton("添加文件夹")
        add_button.clicked.connect(self.add_folder)
        remove_button = QPushButton("移除")
        remove_button.clicked.connect(self.remove_folder)
        dest_button = QPushButton("目标目录")
        dest_button.clicked.connect(self.choose_destination)
        self.scan_button.clicked.connect(self.start_scan)
        self.cancel_button.clicked.connect(self.cancel_scan)
        self.cancel_button.setEnabled(False)
        move_button = QPushButton("移动未勾选项")
        move_button.clicked.connect(self.move_unchecked)
        self.action_combo.addItems(["移动到目标目录", "移动到回收站"])
        self.organize_rule_combo.addItems(["按拍摄年月", "按拍摄日期", "按文件类型", "按相机型号"])
        organize_button = QPushButton("执行整理规则")
        organize_button.clicked.connect(self.organize_by_rule)
        refresh_ops_button = QPushButton("刷新记录")
        refresh_ops_button.clicked.connect(self.refresh_operations)
        undo_button = QPushButton("撤销选中移动")
        undo_button.clicked.connect(self.undo_selected_operation)
        export_ops_button = QPushButton("导出操作记录")
        export_ops_button.clicked.connect(self.export_operations)
        refresh_history_button = QPushButton("刷新历史")
        refresh_history_button.clicked.connect(self.refresh_history)
        clear_history_button = QPushButton("清空扫描历史")
        clear_history_button.clicked.connect(self.clear_history)
        export_history_button = QPushButton("导出扫描历史")
        export_history_button.clicked.connect(self.export_history)
        save_ai_button = QPushButton("保存 AI 配置")
        save_ai_button.clicked.connect(self.save_ai_settings)
        self.ai_analyze_button.clicked.connect(self.analyze_selected_image)
        self.ai_batch_button.clicked.connect(self.analyze_current_group)
        export_ai_button = QPushButton("导出 AI 结果")
        export_ai_button.clicked.connect(self.export_ai_results)
        filter_ai_button = QPushButton("筛选低分")
        filter_ai_button.clicked.connect(self.refresh_ai_results)

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

        folder_buttons = QHBoxLayout()
        folder_buttons.addWidget(add_button)
        folder_buttons.addWidget(remove_button)

        left = QWidget()
        left.setObjectName("Sidebar")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(16, 16, 16, 16)
        left_layout.setSpacing(8)
        left_layout.addWidget(QLabel("扫描目录"))
        left_layout.addWidget(self.folder_list)
        left_layout.addLayout(folder_buttons)
        left_layout.addWidget(dest_button)
        left_layout.addWidget(self.destination_label)
        left_layout.addWidget(QLabel("图片相似阈值"))
        left_layout.addWidget(self.threshold_spin)
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

        self.group_list.currentItemChanged.connect(self.populate_group)
        groups_panel = QWidget()
        groups_panel.setObjectName("GroupsPanel")
        groups_layout = QVBoxLayout(groups_panel)
        groups_layout.setContentsMargins(16, 16, 16, 16)
        groups_layout.setSpacing(8)
        groups_layout.addWidget(QLabel("重复分组"))
        groups_layout.addWidget(self.group_list)

        self.file_table.setHorizontalHeaderLabels(["保留", "文件名", "大小", "修改时间", "尺寸", "相似度", "路径"])
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.file_table.setAlternatingRowColors(True)
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.verticalHeader().setDefaultSectionSize(30)
        self.file_table.itemSelectionChanged.connect(self.preview_selected_file)

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
        right_layout.addWidget(self.file_table)
        right_layout.addWidget(self.thumbnail_list)
        right_layout.addWidget(self.retention_hint_label)
        right_layout.addWidget(self.action_combo)
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
        ops_buttons.addWidget(export_ops_button)

        operations_panel = QWidget()
        operations_panel.setObjectName("ContentPanel")
        operations_layout = QVBoxLayout(operations_panel)
        operations_layout.setContentsMargins(14, 14, 14, 14)
        operations_layout.setSpacing(10)
        operations_layout.addWidget(self.operations_table)
        operations_layout.addLayout(ops_buttons)

        self.history_table.setHorizontalHeaderLabels(["文件名", "大小", "修改时间", "尺寸", "SHA-256", "图片指纹", "路径"])
        self.history_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.verticalHeader().setDefaultSectionSize(30)

        history_buttons = QHBoxLayout()
        history_buttons.addWidget(refresh_history_button)
        history_buttons.addWidget(clear_history_button)
        history_buttons.addWidget(export_history_button)

        history_panel = QWidget()
        history_panel.setObjectName("ContentPanel")
        history_layout = QVBoxLayout(history_panel)
        history_layout.setContentsMargins(14, 14, 14, 14)
        history_layout.setSpacing(10)
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
        ai_buttons = QHBoxLayout()
        ai_buttons.addWidget(save_ai_button)
        ai_buttons.addWidget(self.ai_analyze_button)
        ai_buttons.addWidget(self.ai_batch_button)
        ai_layout.addLayout(ai_buttons)
        ai_filter_buttons = QHBoxLayout()
        ai_filter_buttons.addWidget(self.ai_score_filter_spin)
        ai_filter_buttons.addWidget(filter_ai_button)
        ai_filter_buttons.addWidget(export_ai_button)
        ai_layout.addLayout(ai_filter_buttons)
        ai_layout.addWidget(self.ai_table)

        self.main_tabs = QTabWidget()
        tabs = self.main_tabs
        tabs.addTab(right, "重复处理")
        tabs.addTab(operations_panel, "操作记录")
        tabs.addTab(history_panel, "扫描历史")
        tabs.addTab(ai_panel, "AI 判断")

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
        existing = {self.folder_list.item(index).text() for index in range(self.folder_list.count())}
        for folder in folders:
            if folder not in existing:
                self.folder_list.addItem(folder)
                existing.add(folder)

    def remove_folder(self) -> None:
        for item in self.folder_list.selectedItems():
            self.folder_list.takeItem(self.folder_list.row(item))

    def choose_destination(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择重复文件移动目标", str(self.destination))
        if folder:
            self.destination = Path(folder)
            self._update_destination_label()
            self._save_settings()
            self.status_label.setText(f"目标目录：{self.destination}")

    def _update_destination_label(self) -> None:
        self.destination_label.setWordWrap(True)
        self.destination_label.setText(f"移动到：{self.destination}")

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

    def _save_settings(self) -> None:
        self.database.set_setting("destination", str(self.destination))
        self.database.set_setting("similarity_threshold", str(self.threshold_spin.value()))
        self.database.set_setting("keep_strategy", self.strategy_combo.currentText())
        self.database.set_setting("organize_rule", self.organize_rule_combo.currentText())

    def restore_latest_scan_session(self) -> None:
        session = self.database.get_latest_scan_session()
        if session is None:
            return
        self.current_roots = session.root_paths
        existing = {self.folder_list.item(index).text() for index in range(self.folder_list.count())}
        for root in session.root_paths:
            root_text = str(root)
            if root_text not in existing:
                self.folder_list.addItem(root_text)
                existing.add(root_text)
        groups = self.database.load_scan_session_groups(session.id)
        if groups:
            self.on_scan_finished(groups, restored=True)

    def on_tab_changed(self, index: int) -> None:
        if index == 1 and not self.operations_loaded:
            self.refresh_operations()
            self.operations_loaded = True
        elif index == 2 and not self.history_loaded:
            self.refresh_history()
            self.history_loaded = True
        elif index == 3 and not self.ai_loaded:
            self.refresh_ai_results()
            self.ai_loaded = True

    def save_ai_settings(self) -> None:
        self._save_ai_settings_silent()
        QMessageBox.information(self, "已保存", "AI 配置已保存到本机数据库。")

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
        roots = [Path(self.folder_list.item(index).text()) for index in range(self.folder_list.count())]
        if not roots:
            QMessageBox.information(self, "需要目录", "请先添加至少一个扫描目录。")
            return
        if self.worker and self.worker.isRunning():
            return

        self.group_list.clear()
        self.file_table.setRowCount(0)
        self.preview_label.setText("扫描中")
        self.detail_label.clear()
        self.progress.setValue(0)
        self.status_label.setText("正在扫描")
        self.media_player.stop()
        self.scan_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.current_roots = roots

        self.worker = ScanWorker(roots, self.database.path, self.threshold_spin.value())
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
        self.status_label.setText(message)
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(current)

    def on_scan_finished(self, groups: list[DuplicateGroup], restored: bool = False) -> None:
        self.groups = groups
        self.group_list.clear()
        for group in groups:
            item = QListWidgetItem(
                f"{group.title}\n{len(group.records)} 个文件 · 可整理 {format_size(group.reclaimable_bytes)}"
            )
            item.setData(Qt.ItemDataRole.UserRole, group)
            self.group_list.addItem(item)
        if not restored and self.current_roots:
            self.database.replace_latest_scan_session(self.current_roots, groups)
        if restored:
            self.status_label.setText(f"已恢复最近一次扫描：{len(groups)} 组重复/相似文件")
        else:
            self.status_label.setText(f"扫描完成：发现 {len(groups)} 组重复/相似文件")
        self.preview_label.setText("选择图片以预览")
        self.progress.setValue(self.progress.maximum())
        self.scan_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if self.history_loaded and not restored:
            self.refresh_history()

    def on_scan_failed(self, message: str) -> None:
        QMessageBox.critical(self, "扫描失败", message)
        self.status_label.setText("扫描失败")
        self.scan_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def populate_group(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        self.file_table.setRowCount(0)
        self.thumbnail_list.clear()
        if not current:
            return
        group = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(group, DuplicateGroup):
            return

        keep_path = self._suggest_keep(group.records).path
        self.file_table.setRowCount(len(group.records))
        for row, record in enumerate(group.records):
            keep_item = QTableWidgetItem()
            keep_item.setFlags(keep_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            keep_item.setCheckState(Qt.CheckState.Checked if record.path == keep_path else Qt.CheckState.Unchecked)
            keep_item.setData(Qt.ItemDataRole.UserRole, record)
            self.file_table.setItem(row, 0, keep_item)
            self.file_table.setItem(row, 1, QTableWidgetItem(record.name))
            self.file_table.setItem(row, 2, QTableWidgetItem(format_size(record.size)))
            modified = datetime.fromtimestamp(record.modified_at).strftime("%Y-%m-%d %H:%M")
            self.file_table.setItem(row, 3, QTableWidgetItem(modified))
            dimensions = f"{record.width}x{record.height}" if record.width and record.height else "-"
            self.file_table.setItem(row, 4, QTableWidgetItem(dimensions))
            self.file_table.setItem(row, 5, QTableWidgetItem(f"{group.score:.0%}" if group.kind == "similar" else "100%"))
            self.file_table.setItem(row, 6, QTableWidgetItem(str(record.path)))
            self._add_thumbnail(record, row)
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

    def reapply_keep_strategy(self) -> None:
        current = self.group_list.currentItem()
        if current:
            self.populate_group(current, None)

    def _suggest_keep(self, records: list[FileRecord]) -> FileRecord:
        strategy = self.strategy_combo.currentText()
        if strategy == "保留最新修改":
            return max(records, key=lambda item: item.modified_at)
        if strategy == "保留最大文件":
            return max(records, key=lambda item: item.size)
        return max(records, key=lambda item: ((item.width or 0) * (item.height or 0), item.size, item.modified_at))

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

        self.detail_label.setText(str(record.path))
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
        current = self.group_list.currentItem()
        if not current:
            return
        group = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(group, DuplicateGroup):
            return

        selected_to_move: list[FileRecord] = []
        for row in range(self.file_table.rowCount()):
            item = self.file_table.item(row, 0)
            if not item:
                continue
            record = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(record, FileRecord) and item.checkState() != Qt.CheckState.Checked:
                selected_to_move.append(record)

        if not selected_to_move:
            QMessageBox.information(self, "没有要移动的文件", "请取消勾选需要移动的重复文件。")
            return

        reply = QMessageBox.question(
            self,
            "确认处理",
            (
                f"勾选的文件会保留在原位置。\n"
                f"未勾选的 {len(selected_to_move)} 个文件将按“{self.action_combo.currentText()}”处理。\n\n"
                f"目标目录：\n{self.destination}"
            ),
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            if self.action_combo.currentText() == "移动到回收站":
                moved_paths = trash_records(selected_to_move, self.database)
                moved_count = len(moved_paths)
            else:
                moved = move_records(selected_to_move, self.destination, group.title, self.database)
                moved_count = len(moved)
        except Exception as exc:
            QMessageBox.critical(self, "处理失败", str(exc))
            return

        QMessageBox.information(self, "处理完成", f"已处理 {moved_count} 个文件。")
        self.refresh_operations()
        remaining = [record for record in group.records if record not in selected_to_move]
        if len(remaining) <= 1:
            self.group_list.takeItem(self.group_list.row(current))
            self.groups = [item for item in self.groups if item is not group]
            self.file_table.setRowCount(0)
            self.thumbnail_list.clear()
            self.preview_label.setText("选择图片以预览")
        else:
            group.records = remaining
            self.populate_group(current, None)
        if self.current_roots:
            self.database.replace_latest_scan_session(self.current_roots, self.groups)

    def refresh_operations(self) -> None:
        operations = self.database.list_operations()
        self.operations_table.setRowCount(len(operations))
        for row, operation in enumerate(operations):
            self._set_operation_row(row, operation)
        self.operations_table.resizeRowsToContents()

    def refresh_history(self) -> None:
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
        self.group_list.clear()
        self.file_table.setRowCount(0)
        self.thumbnail_list.clear()
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

    def undo_selected_operation(self) -> None:
        selected = self.operations_table.selectedItems()
        if not selected:
            QMessageBox.information(self, "没有选中记录", "请先在操作记录中选中一条移动记录。")
            return
        row = selected[0].row()
        id_item = self.operations_table.item(row, 5)
        if not id_item:
            return
        operation = self.database.get_operation(int(id_item.text()))
        if operation is None:
            QMessageBox.warning(self, "记录不存在", "这条操作记录已经不存在。")
            self.refresh_operations()
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

    def export_operations(self) -> None:
        target, _ = QFileDialog.getSaveFileName(self, "导出操作记录", "operations.csv", "CSV Files (*.csv)")
        if not target:
            return
        try:
            export_operations_csv(self.database.list_operations(limit=10000), Path(target))
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出完成", target)

    def export_history(self) -> None:
        target, _ = QFileDialog.getSaveFileName(self, "导出扫描历史", "scan-history.csv", "CSV Files (*.csv)")
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
        base_url = self.ai_base_url_input.text().strip()
        model = self.ai_model_input.text().strip()
        api_key = self.ai_key_input.text().strip()
        if not base_url or not model or not api_key:
            QMessageBox.information(self, "需要 AI 配置", "请填写 API 地址、模型名和 API Key。")
            return
        if self.ai_worker and self.ai_worker.isRunning():
            return

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
        roots = [Path(self.folder_list.item(index).text()) for index in range(self.folder_list.count())]
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
                moved = organize_files(root, self.destination, rule)
                total_moved += len(moved)
        except Exception as exc:
            QMessageBox.critical(self, "整理失败", str(exc))
            return

        self._save_settings()
        QMessageBox.information(self, "整理完成", f"已整理 {total_moved} 个文件。")
        self.status_label.setText(f"整理完成：{total_moved} 个文件")


def run() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    window = MainWindow()
    window.show()
    window.raise_()
    window.activateWindow()
    sys.exit(app.exec())
