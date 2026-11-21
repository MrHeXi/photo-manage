from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from photo_dedupe.core import batch_operations as batch
from photo_dedupe.core.batch_operations import process_duplicates, process_organize
from photo_dedupe.core.file_ops import undo_move_operations
from photo_dedupe.core.models import DuplicateGroup, FileRecord
from photo_dedupe.core.operation_report import build_batch_result_report, format_operation_result_report
from photo_dedupe.core.organizer import (
    OrganizeTargetConfig, PreviewCancelled, build_organize_preview, build_target_path,
    describe_organize_preview_item,
)
from photo_dedupe.core.selection import build_processing_plan
from photo_dedupe.core.storage import AppDatabase


class SafeBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = AppDatabase(self.root / "state.db")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.db.close)
        self.targets = OrganizeTargetConfig(*(self.root / "organized" / name for name in ("photo", "video", "docs", "other")))

    def record(self, name, contents=b"matching content", keep=False):
        path = self.root / "source" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        stat = path.stat()
        return FileRecord(path, stat.st_size, stat.st_mtime, sha256=hashlib.sha256(contents).hexdigest(), keep_selected=keep)

    def plan(self, count=2):
        kept = self.record("kept.txt", keep=True)
        others = [self.record(f"copy-{i}.txt") for i in range(count)]
        group = DuplicateGroup("exact", "same", [kept, *others])
        return kept, others, build_processing_plan([group], set(), validate_files=True)

    def run_plan(self, plan, **kwargs):
        return process_duplicates(plan, self.root / "destination", "move", self.db, **kwargs)

    def test_missing_survivor_blocks_plan_and_executor(self):
        kept, candidates, plan = self.plan(1)
        kept.path.unlink()
        invalid = build_processing_plan(plan.groups, set(), validate_files=True)
        self.assertEqual(len(invalid.invalid_groups), 1)
        self.assertEqual(invalid.records_to_process, [])
        result = self.run_plan(plan)
        self.assertEqual(result.skipped_count, 1)
        self.assertTrue(candidates[0].path.exists())
        self.assertEqual(self.db.list_operations(), [])

    def test_changed_survivor_with_same_size_and_timestamp_is_rejected_by_hash(self):
        kept, candidates, plan = self.plan(1)
        stat = kept.path.stat()
        kept.path.write_bytes(b"x" * kept.size)
        os.utime(kept.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        result = self.run_plan(plan)
        self.assertEqual(result.skipped_count, 1)
        self.assertIn("不一致", result.items[0].message)
        self.assertTrue(candidates[0].path.exists())

    def test_changed_source_with_same_size_and_timestamp_is_rejected(self):
        _kept, candidates, plan = self.plan(1)
        source = candidates[0]
        stat = source.path.stat()
        source.path.write_bytes(b"x" * source.size)
        os.utime(source.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        result = self.run_plan(plan)
        self.assertEqual(result.skipped_count, 1)
        self.assertIn("扫描指纹", result.items[0].message)
        self.assertTrue(source.path.exists())

    def test_partial_failure_continues_and_retry_only_retries_failed(self):
        kept, candidates, plan = self.plan(3)
        original = batch._move_file_safely
        attempts = []

        def fail_middle(source, target, cancel, before_remove=None):
            attempts.append(source)
            if source == candidates[1].path:
                raise PermissionError("locked file")
            return original(source, target, cancel, before_remove)

        with patch.object(batch, "_move_file_safely", side_effect=fail_middle):
            first = self.run_plan(plan)
        self.assertEqual([item.status for item in first.items], ["completed", "failed", "completed"])
        self.assertEqual(len(attempts), 3)
        with patch.object(batch, "_move_file_safely", wraps=original) as moved:
            second = self.run_plan(plan, previous_result=first)
        self.assertEqual(moved.call_count, 1)
        self.assertEqual(second.completed_count, 3)
        self.assertEqual(len(self.db.list_operations()), 3)
        self.assertTrue(kept.path.exists())
        self.assertEqual(second.journal_path, first.journal_path)

    def test_cancel_then_resume_preserves_completed_item(self):
        _kept, candidates, plan = self.plan(3)
        cancelled = False

        def progress(message, current, total):
            nonlocal cancelled
            if current == 1:
                cancelled = True

        first = self.run_plan(plan, progress=progress, should_cancel=lambda: cancelled)
        self.assertEqual([item.status for item in first.items], ["completed", "cancelled", "cancelled"])
        self.assertEqual(first.retryable_sources, {candidates[1].path, candidates[2].path})
        second = self.run_plan(plan, previous_result=first)
        self.assertEqual(second.completed_count, 3)
        self.assertEqual(len(self.db.list_operations()), 3)

    def test_successful_move_stays_completed_if_database_log_fails(self):
        _kept, candidates, plan = self.plan(1)
        with patch.object(self.db, "log_move", side_effect=OSError("disk full")):
            result = self.run_plan(plan)
        self.assertEqual(result.completed_count, 1)
        self.assertIn("记录写入失败", result.items[0].message)
        self.assertFalse(candidates[0].path.exists())
        self.assertTrue(result.items[0].target_path.exists())
        rows = [json.loads(line) for line in result.journal_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["state"] for row in rows], ["intent", "completed"])
        second = self.run_plan(plan, previous_result=result)
        self.assertEqual(second.items, result.items)
        self.assertEqual(self.db.list_operations(), [])

    def test_intent_journal_failure_prevents_any_file_change(self):
        _kept, candidates, plan = self.plan(1)
        with patch.object(batch, "_append_journal", side_effect=OSError("read only")):
            result = self.run_plan(plan)
        self.assertEqual(result.failed_count, 1)
        self.assertTrue(candidates[0].path.exists())
        self.assertEqual(self.db.list_operations(), [])

    def test_completion_journal_failure_does_not_hide_success(self):
        _kept, candidates, plan = self.plan(1)
        append = batch._append_journal

        def fail_after_intent(path, **entry):
            if entry["state"] != "intent":
                raise OSError("disk full")
            return append(path, **entry)

        with patch.object(batch, "_append_journal", side_effect=fail_after_intent):
            result = self.run_plan(plan)
        self.assertEqual(result.completed_count, 1)
        self.assertIn("恢复日志写入失败", result.items[0].message)
        self.assertFalse(candidates[0].path.exists())
        self.assertTrue(result.items[0].target_path.exists())
        intent = json.loads(result.journal_path.read_text(encoding="utf-8"))
        self.assertEqual(intent["target"], str(result.items[0].target_path))

    def test_destination_collision_never_overwrites(self):
        _kept, candidates, plan = self.plan(1)
        target = self.root / "destination" / "当前筛选结果" / candidates[0].name
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing")
        result = self.run_plan(plan)
        self.assertEqual(result.completed_count, 1)
        self.assertEqual(target.read_bytes(), b"existing")
        self.assertNotEqual(target, result.items[0].target_path)
        self.assertEqual(result.items[0].target_path.read_bytes(), b"matching content")

    def test_racing_destination_creation_fails_without_overwrite(self):
        _kept, candidates, plan = self.plan(1)
        original = batch._move_file_safely

        def race(source, target, cancel, before_remove=None):
            target.write_bytes(b"another application")
            return original(source, target, cancel, before_remove)

        with patch.object(batch, "_move_file_safely", side_effect=race):
            result = self.run_plan(plan)
        self.assertEqual(result.failed_count, 1)
        self.assertTrue(candidates[0].path.exists())
        self.assertEqual(result.items[0].target_path.read_bytes(), b"another application")

    def test_copy_fallback_cancellation_keeps_source_and_cleans_partial_target(self):
        _kept, candidates, plan = self.plan(1)
        digest = batch._digest
        cancel = False

        def cancel_after_target_read(path, should_cancel):
            nonlocal cancel
            value = digest(path, should_cancel)
            if "destination" in path.parts:
                cancel = True
            return value

        with patch.object(batch.os, "link", side_effect=OSError("cross volume")), patch.object(batch, "_digest", side_effect=cancel_after_target_read):
            result = self.run_plan(plan, should_cancel=lambda: cancel)
        self.assertEqual(result.cancelled_count, 1)
        self.assertTrue(candidates[0].path.exists())
        self.assertFalse(result.items[0].target_path.exists())

    def test_survivor_removed_during_copy_keeps_source(self):
        kept, candidates, plan = self.plan(1)
        digest = batch._digest

        def remove_survivor_after_copy(path, should_cancel):
            value = digest(path, should_cancel)
            if "destination" in path.parts:
                kept.path.unlink()
            return value

        with patch.object(batch.os, "link", side_effect=OSError("cross volume")), patch.object(batch, "_digest", side_effect=remove_survivor_after_copy):
            result = self.run_plan(plan)
        self.assertEqual(result.skipped_count, 1)
        self.assertTrue(candidates[0].path.exists())
        self.assertFalse(result.items[0].target_path.exists())

    def test_protected_candidates_are_rechecked_even_for_manually_modified_plan(self):
        _kept, candidates, plan = self.plan(1)
        plan.protected_roots = frozenset({candidates[0].path.parent})
        result = self.run_plan(plan)
        self.assertEqual(result.skipped_count, 1)
        self.assertTrue(candidates[0].path.exists())

    def test_trash_validates_survivor_and_reports_partial_failure(self):
        kept, candidates, plan = self.plan(2)

        def trash(path):
            if Path(path) == candidates[1].path:
                raise PermissionError("locked")
            Path(path).unlink()

        with patch("send2trash.send2trash", side_effect=trash) as recycler:
            result = process_duplicates(plan, self.root / "unused", "trash", self.db)
        self.assertEqual(recycler.call_count, 2)
        self.assertEqual([item.status for item in result.items], ["completed", "failed"])
        self.assertTrue(kept.path.exists())
        self.assertTrue(candidates[1].path.exists())
        self.assertEqual(self.db.list_operations()[0].action, "trash")

    def test_organize_snapshot_change_is_skipped(self):
        source = self.record("note.txt")
        preview = [build_target_path(source.path, self.targets)]
        source.path.write_bytes(b"new content changed")
        self.assertEqual(describe_organize_preview_item(preview[0]).state, "changed")
        result = process_organize(preview, self.db)
        self.assertEqual(result.skipped_count, 1)
        self.assertTrue(source.path.exists())

    def test_preview_cancellable_and_overlapping_roots_classified_once(self):
        source = self.record("sub/note.txt")
        other = self.record("ignore/note.txt")
        from photo_dedupe.core import organizer

        with patch.object(organizer, "build_target_path", wraps=organizer.build_target_path) as builder:
            preview = build_organize_preview(
                [source.path.parent.parent, source.path.parent], self.targets,
                exclude_roots={other.path.parent},
            )
        self.assertEqual([item.source for item in preview], [source.path])
        self.assertEqual(builder.call_count, 1)
        with self.assertRaises(PreviewCancelled):
            build_organize_preview([source.path.parent], self.targets, should_cancel=lambda: True)

    def test_report_keeps_failures_cancellation_and_full_details(self):
        _kept, _candidates, plan = self.plan(2)
        result = self.run_plan(plan, should_cancel=lambda: True)
        report = build_batch_result_report(result, "移动到目标目录")
        self.assertEqual(report.cancelled_count, 2)
        self.assertEqual(report.skipped_count, 0)
        text = format_operation_result_report(report, include_items=True, item_limit=None)
        self.assertIn("取消未执行：2", text)
        self.assertIn(str(result.journal_path), text)
        self.assertIn("已取消", text)

    def test_undo_log_failure_stays_restored_and_retry_does_not_repeat(self):
        _kept, _candidates, plan = self.plan(1)
        moved = self.run_plan(plan)
        operations = self.db.list_operations()
        with patch.object(self.db, "mark_operation_undone", side_effect=OSError("database unavailable")):
            first = undo_move_operations(operations, self.db)
        self.assertEqual(first.completed_count, 1)
        self.assertIn("记录写入失败", first.items[0].message)
        self.assertTrue(first.items[0].restored_path.exists())
        second = undo_move_operations(operations, self.db, previous_result=first)
        self.assertEqual(second.items, first.items)
        self.assertFalse(moved.items[0].target_path.exists())

    def test_undo_cancel_has_no_filesystem_changes_and_is_retryable(self):
        _kept, _candidates, plan = self.plan(2)
        moved = self.run_plan(plan)
        operations = self.db.list_operations()
        first = undo_move_operations(operations, self.db, should_cancel=lambda: True)
        self.assertEqual(first.cancelled_count, 2)
        self.assertTrue(first.journal_path.exists())
        self.assertTrue(all(item.target_path.exists() for item in moved.items))
        second = undo_move_operations(operations, self.db, previous_result=first)
        self.assertEqual(second.completed_count, 2)
        self.assertTrue(all(item.source_path.exists() for item in moved.items))

    def test_undo_database_read_failure_preserves_prior_results(self):
        _kept, _candidates, plan = self.plan(2)
        self.run_plan(plan)
        operations = self.db.list_operations()
        original = self.db.get_operation

        def fail_second(operation_id):
            if operation_id == operations[1].id:
                raise OSError("read failure")
            return original(operation_id)

        with patch.object(self.db, "get_operation", side_effect=fail_second):
            result = undo_move_operations(operations, self.db)
        self.assertEqual([item.status for item in result.items], ["restored", "failed"])
        self.assertTrue(operations[0].source_path.exists())
        self.assertTrue(operations[1].target_path.exists())


if __name__ == "__main__":
    unittest.main()
