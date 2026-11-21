from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionComboBox

from photo_dedupe.core.models import DuplicateGroup, FileRecord
from photo_dedupe.core.batch_operations import BatchResult, BatchItemResult
from photo_dedupe.core.selection import build_processing_plan
from photo_dedupe.core.onboarding import OnboardingSnapshot, build_onboarding_steps, STATUS_DONE
from photo_dedupe.core.scanner import ScanCancelled
from photo_dedupe.core.organizer import OrganizePreviewItem, summarize_organize_preview, describe_organize_preview_item
from photo_dedupe.ui.main_window import MainWindow, ScanWorker, RestoreSessionWorker


class OnboardingStateTests(unittest.TestCase):
    def test_completed_scan_with_no_duplicates_is_done(self):
        state = OnboardingSnapshot(1, 1, 0, 0, 4, 0, 0, scan_completed=True, valid_keep_group_count=0)
        steps = {step.key: step for step in build_onboarding_steps(state)}
        self.assertEqual(steps["scan"].status, STATUS_DONE)
        self.assertEqual(steps["keep_rule"].status, STATUS_DONE)
        self.assertIn("没有重复文件", steps["keep_rule"].detail)

    def test_one_group_selected_does_not_finish_other_groups(self):
        state = OnboardingSnapshot(1, 1, 3, 10, 4, 0, 0, scan_completed=True, valid_keep_group_count=1)
        steps = {step.key: step for step in build_onboarding_steps(state)}
        self.assertNotEqual(steps["keep_rule"].status, STATUS_DONE)
        self.assertIn("1/3", steps["keep_rule"].detail)


class UiImprovementsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        font = Path("C:/Windows/Fonts/msyh.ttc")
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.messages = patch("photo_dedupe.ui.main_window.QMessageBox.information")
        self.messages.start()
        self.window = MainWindow(safe_mode=True)
        self.window._confirm_action = lambda *_: True

    def tearDown(self):
        for thread in self.window._active_threads():
            thread.requestInterruption()
            if hasattr(thread, "request_cancel"):
                thread.request_cancel()
        self.pump(lambda: not self.window._active_threads())
        self.app.processEvents()
        self.window.close()
        self.app.processEvents()
        self.messages.stop()
        self.env.stop()
        self.temp.cleanup()

    def pump(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(predicate(), "后台任务未按期完成")

    def group(self, suffix=""):
        records = []
        for index in range(3):
            path = self.root / f"file{suffix}-{index}.txt"
            path.write_bytes(b"duplicate content")
            stat = path.stat()
            records.append(FileRecord(path, stat.st_size, stat.st_mtime, keep_selected=index == 0))
        return DuplicateGroup("exact", f"group{suffix}", records)

    def load_group(self):
        group = self.group()
        self.window._add_folder_item(self.root)
        self.window.current_roots = [self.root]
        self.window.on_scan_finished([group])
        return group

    def test_cancel_scan_preserves_display_session_and_history(self):
        group = self.load_group()
        session = self.window.restored_session_id
        entered = threading.Event()

        def cancelled_scan(_scanner, *_args, should_cancel, **_kwargs):
            entered.set()
            while not should_cancel():
                time.sleep(0.005)
            raise ScanCancelled()

        with patch("photo_dedupe.ui.main_window.DuplicateScanner.scan", cancelled_scan):
            self.window._confirm_scan_roots_are_safe = lambda _roots: True
            self.window.start_scan()
            self.pump(entered.is_set)
            self.assertFalse(self.window.main_tabs.isEnabled())
            self.assertIs(self.window.groups[0], group)
            self.window.cancel_active_task()
            self.pump(lambda: not self.window._active_threads())
        self.assertEqual(self.window.restored_session_id, session)
        self.assertEqual(len(self.window.database.list_scan_sessions()), 1)
        self.assertIs(self.window.groups[0], group)
        self.assertIn("取消", self.window.status_label.text())
        self.assertTrue(self.window.main_tabs.isEnabled())

    def test_new_scans_append_and_save_failure_preserves_old_result(self):
        previous = self.load_group()
        first_id = self.window.restored_session_id
        self.window.on_scan_finished([self.group("new")])
        self.assertEqual(len(self.window.database.list_scan_sessions()), 2)
        current = self.window.groups
        current_id = self.window.restored_session_id
        with patch.object(self.window.database, "save_scan_session", side_effect=OSError("disk full")), \
                patch("photo_dedupe.ui.main_window.QMessageBox.critical"):
            self.window.on_scan_finished([])
        self.assertIs(self.window.groups, current)
        self.assertEqual(self.window.restored_session_id, current_id)
        self.assertIsNotNone(self.window.database.get_scan_session(first_id))
        self.assertTrue(previous.records[0].path.exists())

    def test_empty_restore_clears_old_groups_and_marks_completed(self):
        self.load_group()
        session_id = self.window.database.save_scan_session([self.root], [])
        session = self.window.database.get_scan_session(session_id)
        self.window.on_restore_finished(session, {"groups": [], "total_groups": 0, "offset_groups": 0})
        self.assertEqual(self.window.groups, [])
        self.assertTrue(self.window.scan_completed)
        self.assertEqual(self.window.restored_session_id, session_id)
        self.assertTrue(self.window._onboarding_snapshot().scan_completed)

    def test_changed_default_keep_strategy_persists_final_selection(self):
        group = self.load_group()
        group.records[1].size = 100
        self.window.group_list.setCurrentRow(0)
        self.window.strategy_combo.setCurrentText("保留最大文件")
        restored = self.window.database.load_scan_session_groups(self.window.restored_session_id)[0]
        self.assertEqual([record.keep_selected for record in restored.records], [False, True, False])
        self.assertFalse(self.window.keep_state_dirty)

    def test_worker_database_open_failure_emits_failure(self):
        for worker in (ScanWorker([self.root], self.root / "db.sqlite", 8),
                       RestoreSessionWorker(self.root / "db.sqlite")):
            errors = []
            worker.failed.connect(errors.append)
            with patch("photo_dedupe.ui.main_window.AppDatabase", side_effect=OSError("locked")):
                worker.run()
            self.assertEqual(errors, ["locked"])

    def test_retry_rejects_changed_keep_and_protection_decisions(self):
        group = self.load_group()
        plan = build_processing_plan([group], set())
        result = BatchResult(
            tuple(BatchItemResult(record.path, None, "cancelled", record.size, "cancelled")
                  for record in plan.records_to_process), self.root / "journal.jsonl"
        )
        for change in ("keep", "protection"):
            self.window._retry_batch = ("duplicates", plan, "移动到目标目录", self.root / "out", result)
            self.window._retry_signature = self.window._batch_safety_signature()
            if change == "keep":
                group.records[1].keep_selected = True
            else:
                self.window.protected_roots.add(self.root)
            with patch.object(self.window, "_launch_batch") as launch:
                self.window.retry_failed_batch()
                launch.assert_not_called()
            self.assertIsNone(self.window._retry_batch)

    def test_completed_zero_scan_has_distinct_empty_state(self):
        self.window.current_roots = [self.root]
        self.window.on_scan_finished([])
        self.assertIn("本次扫描已完成", self.window.group_status_label.text())

    def test_paged_keep_edits_and_processing_preserve_unloaded_groups(self):
        groups = [self.group(str(index)) for index in range(3)]
        session_id = self.window.database.save_scan_session([self.root], groups)
        self.window.on_restore_finished(
            self.window.database.get_scan_session(session_id),
            {"groups": self.window.database.load_scan_session_groups(session_id, limit_groups=1),
             "total_groups": 3, "offset_groups": 0},
        )
        first = self.window.groups[0]
        first.records[1].keep_selected = True
        self.window.keep_state_dirty = True
        self.window._persist_keep_state()
        self.window._remove_moved_sources_from_groups({first.records[2].path})
        restored = self.window.database.load_scan_session_groups(session_id)
        self.assertEqual(len(restored), 3)
        self.assertEqual(len(restored[0].records), 2)
        self.assertTrue(restored[0].records[1].keep_selected)
        self.assertEqual(len(restored[1].records), 3)
        self.assertTrue(self.window.load_more_groups_button.isEnabled())

    def test_background_file_processing_saves_report_and_keeps_survivor(self):
        group = self.load_group()
        kept = group.records[0].path
        copies = [record.path for record in group.records[1:]]
        self.window.destination = self.root / "destination"
        reports = []
        self.window._show_operation_result_report = lambda _title, report: reports.append(report)
        self.window.move_unchecked()
        self.assertIsNotNone(self.window.background_worker)
        self.pump(lambda: not self.window._active_threads())
        self.assertTrue(kept.exists())
        self.assertTrue(all(not path.exists() for path in copies))
        self.assertEqual(reports[0].completed_count, 2)
        self.assertEqual(len(self.window.database.list_operations()), 2)
        self.assertEqual(self.window.groups, [])
        self.assertIsNotNone(self.window.database.get_scan_session(self.window.restored_session_id))
        self.assertTrue(self.window.main_tabs.isEnabled())

    def test_organize_preview_pages_display_without_truncating_execution_plan(self):
        preview = [OrganizePreviewItem(self.root / f"{i}.txt", "document", "test", self.root / "out" / f"{i}.txt")
                   for i in range(1201)]
        payload = preview, summarize_organize_preview(preview), [
            describe_organize_preview_item(item) for item in preview
        ]
        self.window._display_organize_preview(payload, self.window._current_organize_preview_signature())
        self.assertEqual(len(self.window.organize_preview_items), 1201)
        self.assertEqual(self.window.organize_preview_table.rowCount(), 500)
        self.window._load_more_organize_rows()
        self.assertEqual(self.window.organize_preview_table.rowCount(), 1000)
        self.window._load_more_organize_rows()
        self.assertEqual(self.window.organize_preview_table.rowCount(), 1201)
        self.assertFalse(self.window.organize_more_button.isVisible())

    def test_large_image_group_decodes_only_current_thumbnail_page(self):
        group = DuplicateGroup("exact", "large", [
            FileRecord(self.root / f"{i}.jpg", 1, 0, keep_selected=i == 0) for i in range(201)
        ])
        with patch.object(self.window, "_add_thumbnail") as thumbnail:
            self.window.current_roots = [self.root]
            self.window.on_scan_finished([group])
            self.assertEqual(self.window.file_table.rowCount(), 201)
            self.assertEqual(thumbnail.call_count, 24)
            self.window._load_more_thumbnails()
            self.assertEqual(thumbnail.call_count, 48)

    def test_async_preview_can_be_cancelled_without_losing_previous_preview(self):
        self.window._add_folder_item(self.root)
        old = [object()]
        self.window.organize_preview_items = old
        entered = threading.Event()

        def preview(*_args, should_cancel, **_kwargs):
            from photo_dedupe.core.organizer import PreviewCancelled
            entered.set()
            while not should_cancel():
                time.sleep(0.005)
            raise PreviewCancelled()

        with patch("photo_dedupe.ui.main_window.build_organize_preview", preview):
            self.window.preview_organize_plan()
            self.pump(entered.is_set)
            self.window.cancel_active_task()
            self.pump(lambda: not self.window._active_threads())
        self.assertIs(self.window.organize_preview_items, old)
        self.window.organize_preview_items = []

    def test_compact_combo_actual_edit_rect_fits_text_with_and_without_focus(self):
        self.window.resize(1024, 680)
        self.window.show()
        self.window.switch_to_page("duplicates")
        self.window.advanced_filter_toggle.setChecked(True)
        self.app.processEvents()
        for combo in (self.window.group_min_size_unit_combo, self.window.group_max_size_unit_combo,
                      self.window.bulk_keep_rule_combo):
            for focused in (False, True):
                if focused:
                    combo.setFocus()
                else:
                    self.window.file_table.setFocus()
                self.app.processEvents()
                option = QStyleOptionComboBox()
                combo.initStyleOption(option)
                rect = combo.style().subControlRect(
                    QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, combo
                )
                self.assertGreaterEqual(rect.width(), combo.fontMetrics().horizontalAdvance(combo.currentText()),
                                        f"{combo.currentText()} 文字区域过窄: {rect.width()}")
        self.window.scan_panel_toggle.setChecked(False)
        self.app.processEvents()
        self.assertFalse(self.window.scan_settings_panel.isVisible())
        self.assertGreater(self.window.file_table.width(), 500)

    def test_close_waits_for_live_worker_before_destroying_database(self):
        entered = threading.Event()
        release = threading.Event()

        def task(database, _progress, should_cancel):
            entered.set()
            while not release.is_set():
                time.sleep(0.005)
            self.assertTrue(should_cancel())
            return database.get_latest_scan_session()

        self.window.show()
        self.window._start_background_task(task, lambda _result: None, "test")
        self.pump(entered.is_set)
        self.window.close()
        self.assertTrue(self.window.isVisible())
        self.assertTrue(self.window._close_pending)
        self.window.database.get_latest_scan_session()
        release.set()
        self.pump(lambda: not self.window._active_threads())
        self.pump(lambda: not self.window.isVisible())
        # tearDown must not close an already-closed database again.
        self.window.close = lambda: None

    def test_close_waits_for_queued_completion_after_worker_thread_exits(self):
        entered, release = threading.Event(), threading.Event()
        completed = []

        def task(_database, _progress, _cancel):
            entered.set()
            release.wait(3)

        def apply_result(_result):
            completed.append(self.window.database.get_latest_scan_session())

        self.window.show()
        self.window._start_background_task(task, apply_result, "test")
        self.pump(entered.is_set)
        self.window.close()
        release.set()
        self.assertTrue(self.window.background_worker.wait(3000))
        self.window._finish_pending_close()
        self.assertTrue(self.window.isVisible())
        self.pump(lambda: not self.window.isVisible())
        self.assertEqual(completed, [None])
        self.window.close = lambda: None


if __name__ == "__main__":
    unittest.main()
