from __future__ import annotations

import os
import tempfile
import unittest
import csv
import json
import logging
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QLockFile

from photo_dedupe.core.ai_client import parse_chat_content, parse_score
from photo_dedupe.core.diagnostics import build_health_items, default_log_path, setup_logging
from photo_dedupe.core.directory_index import build_directory_duplicate_index
from photo_dedupe.core.exporter import export_ai_analysis_csv, export_files_csv, export_operations_csv
from photo_dedupe.core.file_ops import move_records, undo_move, unique_target_path
from photo_dedupe.core.models import DuplicateGroup, FileRecord
from photo_dedupe.core.organizer import (
    OrganizeTargetConfig,
    build_organize_preview,
    category_label,
    execute_organize_preview,
    organize_by_type,
    organize_files,
    organize_images_by_date,
)
from photo_dedupe.core.scanner import DuplicateScanner
from photo_dedupe.core.selection import build_processing_plan, choose_keep_records
from photo_dedupe.core.single_instance import default_lock_path
from photo_dedupe.core.storage import AppDatabase
from photo_dedupe.core.video_hash import combine_hashes


class BrandingAssetTests(unittest.TestCase):
    def test_app_icon_assets_exist(self) -> None:
        assets = Path(__file__).resolve().parents[1] / "photo_dedupe" / "assets"

        for filename in ("app_icon.ico", "app_icon.png"):
            path = assets / filename
            self.assertTrue(path.exists(), filename)
            self.assertGreater(path.stat().st_size, 0, filename)


class DiagnosticsTests(unittest.TestCase):
    def test_default_log_path_uses_app_logs_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = default_log_path(Path(directory))

        self.assertEqual(path.name, "app.log")
        self.assertEqual(path.parent.name, "logs")
        self.assertEqual(path.parent.parent.name, "PhotoManageGPT")

    def test_setup_logging_writes_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "app.log"
            setup_logging(log_path)
            logging.getLogger("photo_dedupe.tests").info("diagnostic test message")
            for handler in logging.getLogger().handlers:
                handler.flush()

            content = log_path.read_text(encoding="utf-8")
            for handler in list(logging.getLogger().handlers):
                if getattr(handler, "_photo_manage_handler", False):
                    logging.getLogger().removeHandler(handler)
                    handler.close()

        self.assertIn("diagnostic test message", content)

    def test_build_health_items_reports_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "photo_manage.db"
            log_path = root / "app.log"
            database_path.write_text("db", encoding="utf-8")
            log_path.write_text("log", encoding="utf-8")

            items = build_health_items(
                database_path=database_path,
                log_path=log_path,
                ffmpeg_available=False,
                history_state="已恢复部分历史结果",
                loaded_groups=500,
                total_groups=1200,
            )

        by_name = {item.name: item for item in items}
        self.assertEqual(by_name["数据库"].status, "正常")
        self.assertEqual(by_name["启动日志"].status, "正常")
        self.assertEqual(by_name["视频指纹"].status, "可选缺失")
        self.assertIn("500/1200", by_name["历史缓存"].detail)


class SingleInstanceTests(unittest.TestCase):
    def test_lock_file_blocks_second_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = default_lock_path(Path(directory))
            first_lock = QLockFile(str(lock_path))
            second_lock = QLockFile(str(lock_path))
            first_lock.setStaleLockTime(0)
            second_lock.setStaleLockTime(0)

            self.assertTrue(first_lock.tryLock(100))
            try:
                self.assertFalse(second_lock.tryLock(100))
            finally:
                first_lock.unlock()


class MainWindowStartupTests(unittest.TestCase):
    def test_safe_mode_skips_restore_worker(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from photo_dedupe.ui.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LOCALAPPDATA": directory}):
            window = MainWindow(safe_mode=True)
            try:
                self.assertIsNone(window.restore_worker)
                self.assertIn("安全模式", window.status_label.text())
                self.assertIn("跳过历史结果加载", window.group_status_label.text())
            finally:
                window.close()
                app.processEvents()


class DuplicateScannerTests(unittest.TestCase):
    def test_finds_exact_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("same", encoding="utf-8")
            (root / "b.txt").write_text("same", encoding="utf-8")
            (root / "c.txt").write_text("different", encoding="utf-8")

            groups = DuplicateScanner().scan([root])

        exact_groups = [group for group in groups if group.kind == "exact"]
        self.assertEqual(len(exact_groups), 1)
        self.assertEqual(len(exact_groups[0].records), 2)

    def test_unique_target_path_adds_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "photo.jpg").write_text("x", encoding="utf-8")

            target = unique_target_path(root, "photo.jpg")

        self.assertEqual(target.name, "photo (1).jpg")

    def test_finds_similar_images(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (32, 32), "red").save(root / "red.png")
            Image.new("RGB", (32, 32), "blue").save(root / "blue.png")

            groups = DuplicateScanner().scan([root])

        similar_groups = [group for group in groups if group.kind == "similar"]
        self.assertEqual(len(similar_groups), 1)
        self.assertEqual(len(similar_groups[0].records), 2)

    def test_similar_images_can_require_same_resolution(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (32, 32), "red").save(root / "small.png")
            Image.new("RGB", (64, 64), "red").save(root / "large.png")

            groups = DuplicateScanner().scan([root], image_same_resolution_only=True)

        similar_groups = [group for group in groups if group.kind == "similar"]
        self.assertEqual(similar_groups, [])

    def test_similar_images_can_require_same_ratio(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (32, 32), "red").save(root / "square.png")
            Image.new("RGB", (64, 32), "red").save(root / "wide.png")

            groups = DuplicateScanner().scan([root], image_same_ratio_only=True)

        similar_groups = [group for group in groups if group.kind == "similar"]
        self.assertEqual(similar_groups, [])

    def test_undo_move_restores_file_and_marks_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("content", encoding="utf-8")
            destination = root / "duplicates"
            database = AppDatabase(root / "test.db")
            try:
                record = FileRecord(path=source, size=7, modified_at=source.stat().st_mtime)
                moved = move_records([record], destination, "group", database)
                self.assertEqual(len(moved), 1)
                self.assertFalse(source.exists())

                operation = database.list_operations()[0]
                restored = undo_move(operation, database)
                refreshed = database.get_operation(operation.id)
            finally:
                database.close()

            self.assertEqual(restored, source)
            self.assertTrue(source.exists())
            self.assertIsNotNone(refreshed)
            self.assertTrue(refreshed.is_undone)

    def test_organize_images_by_date_uses_year_month_folder(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            target_dir = root / "organized"
            source_dir.mkdir()
            image_path = source_dir / "photo.png"
            Image.new("RGB", (16, 16), "green").save(image_path)

            moved = organize_images_by_date(source_dir, target_dir)

        self.assertEqual(len(moved), 1)
        self.assertIn("organized", str(moved[0][1]))
        self.assertTrue(moved[0][1].parent.name.count("-") == 1)

    def test_organize_by_type_uses_extension_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            target_dir = root / "organized"
            source_dir.mkdir()
            (source_dir / "note.txt").write_text("hello", encoding="utf-8")

            moved = organize_by_type(source_dir, target_dir)

        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0][1].parent.name, "txt")

    def test_organize_files_day_rule(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            target_dir = root / "organized"
            source_dir.mkdir()
            Image.new("RGB", (16, 16), "green").save(source_dir / "photo.png")

            moved = organize_files(source_dir, target_dir, "date_day")

        self.assertEqual(len(moved), 1)
        self.assertEqual(len(moved[0][1].relative_to(target_dir).parts), 4)

    def test_build_organize_preview_routes_by_category(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            source_dir.mkdir()
            (source_dir / "photo.jpg").write_text("x", encoding="utf-8")
            (source_dir / "movie.mp4").write_text("x", encoding="utf-8")
            (source_dir / "notes.pdf").write_text("x", encoding="utf-8")
            (source_dir / "data.bin").write_text("x", encoding="utf-8")

            targets = OrganizeTargetConfig(
                photo_root=root / "photos",
                video_root=root / "videos",
                document_root=root / "docs",
                other_root=root / "others",
            )

            preview = build_organize_preview([source_dir], targets)

        mapped = {item.source.name: item for item in preview}
        self.assertEqual(mapped["photo.jpg"].target.parent.name, "photos")
        self.assertEqual(mapped["movie.mp4"].target.parent.name, "videos")
        self.assertEqual(mapped["notes.pdf"].target.parent.name, "docs")
        self.assertEqual(mapped["data.bin"].target.parent.name, "others")
        self.assertEqual(category_label(mapped["photo.jpg"].category), "照片")

    def test_build_organize_preview_can_limit_to_keep_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            source_dir.mkdir()
            keep_file = source_dir / "keep.jpg"
            skip_file = source_dir / "skip.jpg"
            keep_file.write_text("x", encoding="utf-8")
            skip_file.write_text("x", encoding="utf-8")

            targets = OrganizeTargetConfig(
                photo_root=root / "photos",
                video_root=root / "videos",
                document_root=root / "docs",
                other_root=root / "others",
            )

            preview = build_organize_preview([source_dir], targets, include_paths={keep_file})

        self.assertEqual([item.source.name for item in preview], ["keep.jpg"])

    def test_build_organize_preview_skips_protected_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            protected_dir = source_dir / "library"
            protected_dir.mkdir(parents=True)
            (source_dir / "loose.jpg").write_text("x", encoding="utf-8")
            (protected_dir / "safe.jpg").write_text("x", encoding="utf-8")

            targets = OrganizeTargetConfig(
                photo_root=root / "photos",
                video_root=root / "videos",
                document_root=root / "docs",
                other_root=root / "others",
            )

            preview = build_organize_preview([source_dir], targets, exclude_roots={protected_dir})

        self.assertEqual([item.source.name for item in preview], ["loose.jpg"])

    def test_organize_files_skips_protected_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            protected_dir = source_dir / "library"
            protected_dir.mkdir(parents=True)
            loose = source_dir / "loose.txt"
            safe = protected_dir / "safe.txt"
            loose.write_text("x", encoding="utf-8")
            safe.write_text("x", encoding="utf-8")

            moved = organize_files(source_dir, root / "organized", "type", exclude_roots={protected_dir})
            safe_exists = safe.exists()

        self.assertEqual([source.name for source, _target in moved], ["loose.txt"])
        self.assertTrue(safe_exists)

    def test_execute_organize_preview_moves_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "clip.mp4"
            source.write_text("x", encoding="utf-8")
            preview = [
                build_organize_preview(
                    [source],
                    OrganizeTargetConfig(
                        photo_root=root / "photos",
                        video_root=root / "videos",
                        document_root=root / "docs",
                        other_root=root / "others",
                    ),
                )[0]
            ]

            moved = execute_organize_preview(preview)
            moved_target = moved[0][1]
            source_exists = source.exists()
            target_exists = moved_target.exists()

        self.assertEqual(len(moved), 1)
        self.assertFalse(source_exists)
        self.assertTrue(target_exists)

    def test_build_directory_duplicate_index_groups_by_directory(self) -> None:
        root = Path("D:/library")
        file_a = FileRecord(path=root / "a" / "one.jpg", size=1, modified_at=1.0)
        file_b = FileRecord(path=root / "b" / "one-copy.jpg", size=1, modified_at=2.0)
        file_c = FileRecord(path=root / "c" / "two.jpg", size=1, modified_at=3.0)
        groups = [
            DuplicateGroup(kind="exact", title="g1", records=[file_a, file_b]),
            DuplicateGroup(kind="similar", title="g2", records=[file_a, file_c]),
        ]

        file_index, directory_index = build_directory_duplicate_index(groups)

        self.assertEqual(file_index[file_a.path].group_indices, [0, 1])
        self.assertEqual(
            {path.name for path in file_index[file_a.path].peer_paths},
            {"one-copy.jpg", "two.jpg"},
        )
        self.assertEqual([path.name for path in directory_index[file_a.path.parent]], ["one.jpg"])

    def test_settings_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.db"
            database = AppDatabase(db_path)
            try:
                database.set_setting("similarity_threshold", "9")
            finally:
                database.close()

            reopened = AppDatabase(db_path)
            try:
                value = reopened.get_setting("similarity_threshold")
            finally:
                reopened.close()

        self.assertEqual(value, "9")

    def test_cached_file_is_reused_when_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file_path = root / "a.txt"
            file_path.write_text("same", encoding="utf-8")
            database = AppDatabase(root / "test.db")
            try:
                stat = file_path.stat()
                record = FileRecord(path=file_path, size=stat.st_size, modified_at=stat.st_mtime, sha256="abc")
                database.upsert_files([record])
                cached = database.get_cached_file(file_path, stat.st_size, stat.st_mtime)
            finally:
                database.close()

        self.assertIsNotNone(cached)
        self.assertEqual(cached.sha256, "abc")

    def test_video_hash_cache_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file_path = root / "a.mp4"
            file_path.write_text("fake video", encoding="utf-8")
            database = AppDatabase(root / "test.db")
            try:
                stat = file_path.stat()
                record = FileRecord(
                    path=file_path,
                    size=stat.st_size,
                    modified_at=stat.st_mtime,
                    video_hash=123,
                )
                database.upsert_files([record])
                cached = database.get_cached_file(file_path, stat.st_size, stat.st_mtime)
            finally:
                database.close()

        self.assertIsNotNone(cached)
        self.assertEqual(cached.video_hash, 123)

    def test_combine_video_hashes_uses_majority_bits(self) -> None:
        combined = combine_hashes([0b1111, 0b1100, 0b0100])
        self.assertEqual(combined, 0b1100)

    def test_scanned_files_history_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            target = root / "files.csv"
            file_path = root / "a.txt"
            file_path.write_text("x", encoding="utf-8")
            database = AppDatabase(db_path)
            try:
                stat = file_path.stat()
                database.upsert_files([FileRecord(path=file_path, size=stat.st_size, modified_at=stat.st_mtime)])
                records = database.list_scanned_files()
                export_files_csv(records, target)
            finally:
                database.close()

            with target.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.reader(file))

        self.assertEqual(len(records), 1)
        self.assertEqual(rows[0][0], "path")
        self.assertIn("a.txt", rows[1][0])

    def test_clear_scanned_files_removes_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            file_path = root / "a.txt"
            file_path.write_text("x", encoding="utf-8")
            database = AppDatabase(db_path)
            try:
                stat = file_path.stat()
                database.upsert_files([FileRecord(path=file_path, size=stat.st_size, modified_at=stat.st_mtime)])
                database.clear_scanned_files()
                records = database.list_scanned_files()
            finally:
                database.close()

        self.assertEqual(records, [])

    def test_scan_session_can_be_saved_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            file_a = FileRecord(path=root / "a.jpg", size=10, modified_at=1.0, width=100, height=100, keep_selected=True)
            file_b = FileRecord(path=root / "b.jpg", size=12, modified_at=2.0, width=120, height=120)
            groups = [DuplicateGroup(kind="similar", title="group", records=[file_a, file_b], score=0.9)]
            database = AppDatabase(db_path)
            try:
                session_id = database.save_scan_session([root], groups)
                session = database.get_latest_scan_session()
                restored = database.load_scan_session_groups(session_id)
            finally:
                database.close()

        self.assertIsNotNone(session)
        self.assertEqual(session.root_paths, [root])
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].title, "group")
        self.assertEqual(len(restored[0].records), 2)
        self.assertTrue(restored[0].records[0].keep_selected)
        self.assertFalse(restored[0].records[1].keep_selected)

    def test_replace_latest_scan_session_keeps_only_latest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            database = AppDatabase(db_path)
            try:
                first_groups = [DuplicateGroup(kind="exact", title="first", records=[
                    FileRecord(path=root / "a.txt", size=1, modified_at=1.0),
                    FileRecord(path=root / "b.txt", size=1, modified_at=2.0),
                ])]
                second_groups = [DuplicateGroup(kind="exact", title="second", records=[
                    FileRecord(path=root / "c.txt", size=2, modified_at=3.0),
                    FileRecord(path=root / "d.txt", size=2, modified_at=4.0),
                ])]
                database.save_scan_session([root], first_groups)
                session_id = database.replace_latest_scan_session([root], second_groups)
                restored = database.load_scan_session_groups(session_id)
            finally:
                database.close()

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].title, "second")

    def test_scan_session_restore_can_be_limited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            groups = []
            for index in range(3):
                groups.append(
                    DuplicateGroup(
                        kind="exact",
                        title=f"group-{index}",
                        records=[
                            FileRecord(path=root / f"{index}-a.txt", size=1, modified_at=1.0),
                            FileRecord(path=root / f"{index}-b.txt", size=1, modified_at=2.0),
                        ],
                    )
                )
            database = AppDatabase(db_path)
            try:
                session_id = database.save_scan_session([root], groups)
                total_groups = database.count_scan_session_groups(session_id)
                restored = database.load_scan_session_groups(session_id, limit_groups=2, limit_items_per_group=2)
            finally:
                database.close()

        self.assertEqual(total_groups, 3)
        self.assertEqual(len(restored), 2)

    def test_scan_session_restore_can_use_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            groups = []
            for index in range(4):
                groups.append(
                    DuplicateGroup(
                        kind="exact",
                        title=f"group-{index}",
                        records=[
                            FileRecord(path=root / f"{index}-a.txt", size=1, modified_at=1.0),
                            FileRecord(path=root / f"{index}-b.txt", size=1, modified_at=2.0),
                        ],
                    )
                )
            database = AppDatabase(db_path)
            try:
                session_id = database.save_scan_session([root], groups)
                restored = database.load_scan_session_groups(session_id, limit_groups=2, offset_groups=2)
            finally:
                database.close()

        self.assertEqual([group.title for group in restored], ["group-2", "group-3"])

    def test_list_scan_sessions_includes_summary_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            groups = [
                DuplicateGroup(
                    kind="exact",
                    title="group-1",
                    records=[
                        FileRecord(path=root / "a.txt", size=10, modified_at=1.0),
                        FileRecord(path=root / "b.txt", size=10, modified_at=2.0),
                    ],
                )
            ]
            database = AppDatabase(db_path)
            try:
                database.save_scan_session([root], groups)
                sessions = database.list_scan_sessions()
            finally:
                database.close()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].group_count, 1)
        self.assertEqual(sessions[0].file_count, 2)
        self.assertEqual(sessions[0].duplicate_bytes, 20)
        self.assertEqual(sessions[0].reclaimable_bytes, 10)

    def test_delete_single_scan_session_keeps_other_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            database = AppDatabase(db_path)
            try:
                first_id = database.save_scan_session(
                    [root / "a"],
                    [
                        DuplicateGroup(
                            kind="exact",
                            title="first",
                            records=[
                                FileRecord(path=root / "a1.txt", size=10, modified_at=1.0),
                                FileRecord(path=root / "a2.txt", size=10, modified_at=2.0),
                            ],
                        )
                    ],
                )
                second_id = database.save_scan_session(
                    [root / "b"],
                    [
                        DuplicateGroup(
                            kind="exact",
                            title="second",
                            records=[
                                FileRecord(path=root / "b1.txt", size=20, modified_at=1.0),
                                FileRecord(path=root / "b2.txt", size=20, modified_at=2.0),
                            ],
                        )
                    ],
                )
                database.delete_scan_session(second_id)
                sessions = database.list_scan_sessions(limit=10)
                restored_first = database.load_scan_session_groups(first_id)
            finally:
                database.close()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].id, first_id)
        self.assertEqual(restored_first[0].title, "first")

    def test_keep_rule_prefers_protected_directory(self) -> None:
        root = Path("D:/demo")
        protected = root / "library"
        safe = FileRecord(path=protected / "photo.jpg", size=10, modified_at=1.0)
        duplicate = FileRecord(path=root / "temp" / "photo-copy.jpg", size=100, modified_at=10.0)

        result = choose_keep_records([safe, duplicate], "保留最大文件", {protected})

        self.assertEqual([record.path for record in result.keep_records], [safe.path])

    def test_processing_plan_skips_protected_unchecked_files(self) -> None:
        root = Path("D:/demo")
        protected = root / "library"
        safe = FileRecord(path=protected / "photo.jpg", size=10, modified_at=1.0, keep_selected=False)
        keep = FileRecord(path=root / "keep" / "photo.jpg", size=20, modified_at=2.0, keep_selected=True)
        remove = FileRecord(path=root / "temp" / "photo-copy.jpg", size=30, modified_at=3.0, keep_selected=False)
        group = DuplicateGroup(kind="exact", title="group", records=[safe, keep, remove])

        plan = build_processing_plan([group], {protected})

        self.assertEqual([record.path for record in plan.records_to_process], [remove.path])
        self.assertEqual(plan.protected_skip_count, 1)
        self.assertEqual(plan.reclaimable_bytes, 30)

    def test_trash_operation_is_logged_and_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            target = root / "operations.csv"
            source = root / "a.txt"
            database = AppDatabase(db_path)
            try:
                operation_id = database.log_trash(source)
                operation = database.get_operation(operation_id)
                operations = database.list_operations()
                export_operations_csv(operations, target)
            finally:
                database.close()

            with target.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.reader(file))

        self.assertIsNotNone(operation)
        self.assertEqual(operation.action, "trash")
        self.assertEqual(rows[1][1], "trash")

    def test_ai_response_parsing_and_storage(self) -> None:
        raw = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": "这张照片主体清晰，曝光正常。保留分数：82/100。"
                        }
                    }
                ]
            },
            ensure_ascii=False,
        )
        summary = parse_chat_content(raw)
        score = parse_score(summary)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            image_path = root / "photo.jpg"
            image_path.write_text("fake", encoding="utf-8")
            database = AppDatabase(db_path)
            try:
                database.save_ai_analysis(image_path, "vision-model", summary, score, raw)
                records = database.list_ai_analysis()
            finally:
                database.close()

        self.assertIn("主体清晰", summary)
        self.assertEqual(score, 82.0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].model, "vision-model")

    def test_ai_results_can_be_filtered_and_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            target = root / "ai.csv"
            high = root / "high.jpg"
            low = root / "low.jpg"
            database = AppDatabase(db_path)
            try:
                database.save_ai_analysis(high, "model", "good", 90.0, "{}")
                database.save_ai_analysis(low, "model", "bad", 30.0, "{}")
                filtered = database.list_ai_analysis(max_score=60)
                export_ai_analysis_csv(database.list_ai_analysis(), target)
            finally:
                database.close()

            with target.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.reader(file))

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].path.name, "low.jpg")
        self.assertEqual(rows[0][0], "id")


if __name__ == "__main__":
    unittest.main()
