from __future__ import annotations

import random
import sqlite3
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import Mock, patch

from photo_dedupe.core.hashers import sha256_file
from photo_dedupe.core.models import DuplicateGroup, FileRecord
from photo_dedupe.core.scanner import DuplicateScanner, ScanCancelled
from photo_dedupe.core.storage import AppDatabase
from photo_dedupe.core.video_hash import average_video_hash


def image_record(index: int, value: int, dimensions=(100, 100)) -> FileRecord:
    return FileRecord(Path(f"image-{index}.jpg"), index + 1, 1.0,
                      image_hash=value, width=dimensions[0], height=dimensions[1])


def brute_force_images(records, threshold, same_ratio=False, same_resolution=False):
    """Independent reference of the original graph semantics and edge score."""
    adjacency = defaultdict(set)
    distances = {}
    for left, record in enumerate(records):
        for right in range(left + 1, len(records)):
            other = records[right]
            if same_resolution and (record.width, record.height) != (other.width, other.height):
                continue
            if same_ratio and (
                not all((record.width, record.height, other.width, other.height))
                or record.width * other.height != other.width * record.height
            ):
                continue
            distance = (record.image_hash ^ other.image_hash).bit_count()
            if distance <= threshold:
                adjacency[left].add(right)
                adjacency[right].add(left)
                distances[left, right] = distance
    visited = set()
    results = {}
    for seed in range(len(records)):
        if seed in visited:
            continue
        stack = [seed]
        component = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(adjacency[current] - component)
        visited.update(component)
        if len(component) < 2:
            continue
        edges = [distance for (left, right), distance in distances.items()
                 if left in component and right in component]
        results[frozenset(records[index].path for index in component)] = 1 - sum(edges) / len(edges) / 64
    return results


class ScannerImprovementTests(unittest.TestCase):
    def test_index_matches_pairwise_groups_and_scores_with_filters(self):
        randomizer = random.Random(426)
        values = [randomizer.getrandbits(64) for _ in range(40)]
        # Repeated hashes and chains exercise edge-weight aggregation and
        # connected components where endpoint distance exceeds the threshold.
        values += [0, 0, 255, 255, 65535, 0xFFFFFF]
        values += [values[index] ^ ((1 << (index % 8)) - 1) for index in range(30)]
        records = [image_record(index, value, [(100, 100), (200, 200), (100, 50), (None, None)][index % 4])
                   for index, value in enumerate(values)]
        for threshold in (0, 1, 8, 24, 64):
            for ratio, resolution in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(threshold=threshold, ratio=ratio, resolution=resolution):
                    actual = DuplicateScanner()._similar_image_groups(
                        records, threshold, None, None,
                        same_ratio_only=ratio, same_resolution_only=resolution,
                    )
                    expected = brute_force_images(records, threshold, ratio, resolution)
                    self.assertEqual({frozenset(item.path for item in group.records) for group in actual}, set(expected))
                    for group in actual:
                        self.assertAlmostEqual(group.score, expected[frozenset(item.path for item in group.records)])

    def test_sparse_hashes_avoid_most_pairwise_comparisons(self):
        randomizer = random.Random(17)
        records = [image_record(index, randomizer.getrandbits(64)) for index in range(1600)]
        scanner = DuplicateScanner()
        scanner._similar_image_groups(records, 8, None, None)
        pairwise_count = len(records) * (len(records) - 1) // 2
        self.assertLess(scanner.comparison_count, pairwise_count // 10)

    def test_dense_identical_hashes_do_not_enumerate_file_pairs(self):
        records = [image_record(index, 0) for index in range(10000)]
        scanner = DuplicateScanner()
        groups = scanner._similar_image_groups(records, 8, None, None)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].records), 10000)
        self.assertEqual(groups[0].score, 1.0)
        self.assertEqual(scanner.comparison_count, 0)

    def test_video_index_preserves_greedy_seed_matching(self):
        randomizer = random.Random(18)
        values = [0, 255, 65535, 0xFFFFFF, 0, 0xFFFFFFFF] + [randomizer.getrandbits(64) for _ in range(40)]
        records = [FileRecord(Path(f"video-{index}.mp4"), index + 1, 1.0, video_hash=value)
                   for index, value in enumerate(values)]
        for threshold in (0, 8, 24, 64):
            used = set()
            expected = []
            for left, value in enumerate(values):
                if left in used:
                    continue
                members = [left] + [right for right, other in enumerate(values)
                                   if right != left and right not in used and (value ^ other).bit_count() <= threshold]
                if len(members) > 1:
                    used.update(members)
                    expected.append(frozenset(records[index].path for index in members))
            with patch("photo_dedupe.core.scanner.video_hash_available", return_value=True):
                actual = DuplicateScanner()._similar_video_groups(records, threshold, None, None)
            self.assertEqual([frozenset(record.path for record in group.records) for group in actual], expected)

    def test_overlapping_roots_do_not_invent_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            source = nested / "single.txt"
            source.write_bytes(b"only copy")
            scanner = DuplicateScanner()
            self.assertEqual(scanner._collect_files([root, nested, source, root]), [source.resolve()])
            self.assertEqual(scanner.scan([root, nested, source]), [])

    def test_traversal_cancellation_raises_instead_of_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(200):
                (root / str(index)).write_bytes(b"file")
            checks = 0

            def cancelled():
                nonlocal checks
                checks += 1
                return checks > 20

            with self.assertRaises(ScanCancelled):
                DuplicateScanner().scan([root], should_cancel=cancelled)
            self.assertLess(checks, 30)

    def test_chunk_hash_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.bin"
            path.write_bytes(b"x" * 4096)
            checks = 0

            def check_cancel():
                nonlocal checks
                checks += 1
                if checks == 3:
                    raise ScanCancelled()

            with self.assertRaises(ScanCancelled):
                sha256_file(path, chunk_size=128, check_cancel=check_cancel)
            self.assertEqual(checks, 3)

    def test_comparison_cancellation(self):
        records = [image_record(index, index) for index in range(500)]
        cancelled = False

        def progress(message, current, total):
            nonlocal cancelled
            if message == "比较相似图片" and current >= 3:
                cancelled = True

        with self.assertRaises(ScanCancelled):
            DuplicateScanner()._similar_image_groups(records, 8, progress, lambda: cancelled)

    def test_cancelled_scan_keeps_previous_cache_and_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            (data / "a.txt").write_bytes(b"same")
            (data / "b.txt").write_bytes(b"same")
            database = AppDatabase(root / "test.db")
            try:
                previous = FileRecord(root / "old.txt", 1, 1, sha256="old")
                database.upsert_files([previous])
                previous_id = database.save_scan_session([root], [
                    DuplicateGroup("exact", "old", [previous, FileRecord(root / "copy.txt", 1, 1)])
                ])
                cancelled = False

                def progress(message, current, total):
                    nonlocal cancelled
                    if message.startswith("计算精确哈希"):
                        cancelled = True

                with self.assertRaises(ScanCancelled):
                    DuplicateScanner(database).scan([data], progress=progress, should_cancel=lambda: cancelled)
                self.assertEqual(database.get_latest_scan_session().id, previous_id)
                self.assertEqual([record.path for record in database.list_scanned_files()], [previous.path])
            finally:
                database.close()

    def test_video_cancellation_terminates_and_reaps_ffmpeg(self):
        process = Mock()
        process.poll.return_value = None
        checks = 0

        def check_cancel():
            nonlocal checks
            checks += 1
            if checks == 2:
                raise ScanCancelled()

        with patch("photo_dedupe.core.video_hash.shutil.which", return_value="ffmpeg"), \
             patch("photo_dedupe.core.video_hash.subprocess.Popen", return_value=process):
            with self.assertRaises(ScanCancelled):
                average_video_hash(Path("sample.mp4"), check_cancel=check_cancel)
        process.kill.assert_called_once()
        process.wait.assert_called_once_with()


class StorageImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = AppDatabase(self.root / "test.db")

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def group(self, name="group", count=2):
        return DuplicateGroup("exact", name, [
            FileRecord(self.root / f"{name}-{index}.jpg", index + 1, 1.0, keep_selected=index == count - 1)
            for index in range(count)
        ])

    def test_large_group_roundtrip_preserves_all_members_and_last_keep(self):
        group = self.group(count=1205)
        session = self.database.save_scan_session([self.root], [group])
        restored = self.database.load_scan_session_groups(session)
        self.assertEqual(len(restored[0].records), 1205)
        self.assertTrue(restored[0].records[-1].keep_selected)
        self.assertEqual(restored[0].storage_id, group.storage_id)
        self.assertEqual(restored[0].reclaimable_bytes, sum(range(1, 1205)))
        summary = self.database.list_scan_sessions()[0]
        self.assertEqual(summary.file_count, 1205)
        self.assertEqual(summary.reclaimable_bytes, restored[0].reclaimable_bytes)

    def test_paged_keep_update_preserves_unseen_groups_and_other_sessions(self):
        first = self.database.save_scan_session([self.root], [self.group("old")])
        current = self.database.save_scan_session([self.root], [self.group(f"g{i}") for i in range(510)])
        page = self.database.load_scan_session_groups(current, limit_groups=500)
        page[0].records[0].keep_selected = True
        page[0].records[1].keep_selected = False
        self.database.update_scan_session_keep_state(current, page)
        self.assertEqual(self.database.count_scan_session_groups(current), 510)
        tail = self.database.load_scan_session_groups(current, offset_groups=500)
        self.assertEqual(len(tail), 10)
        self.assertTrue(tail[-1].records[-1].keep_selected)
        self.assertEqual(self.database.count_scan_session_groups(first), 1)
        saved = self.database.load_scan_session_groups(current, limit_groups=1)[0]
        self.assertTrue(saved.records[0].keep_selected)
        self.assertFalse(saved.records[1].keep_selected)

    def test_partial_member_update_does_not_clear_unseen_selection(self):
        session = self.database.save_scan_session([self.root], [self.group(count=201)])
        partial = self.database.load_scan_session_groups(session, limit_items_per_group=2)
        partial[0].records[0].keep_selected = True
        self.database.update_scan_session_keep_state(session, partial)
        records = self.database.load_scan_session_groups(session)[0].records
        self.assertTrue(records[0].keep_selected)
        self.assertTrue(records[-1].keep_selected)
        self.assertEqual(len(records), 201)

    def test_atomic_replace_rolls_back_failure_without_stale_group_ids(self):
        old = self.group("old")
        session = self.database.save_scan_session([self.root], [old])
        valid = self.group("new")
        invalid = self.group("invalid")
        invalid.records[0].size = None
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.replace_latest_scan_session([self.root], [valid, invalid])
        self.assertEqual(self.database.get_latest_scan_session().id, session)
        self.assertEqual(self.database.load_scan_session_groups(session)[0].title, "old")
        self.assertIsNone(valid.storage_id)
        self.assertIsNone(invalid.storage_id)
        self.assertEqual(len(self.database.list_scan_sessions()), 1)

    def test_failed_new_save_leaves_no_partial_session(self):
        invalid = self.group()
        invalid.records[1].size = None
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.save_scan_session([self.root], [invalid])
        self.assertEqual(self.database.list_scan_sessions(), [])
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM scan_groups").fetchone()[0], 0)

    def test_wrong_session_update_is_rejected_and_rolls_back_other_updates(self):
        first = self.database.save_scan_session([self.root], [self.group("one")])
        second = self.database.save_scan_session([self.root], [self.group("two")])
        current = self.database.load_scan_session_groups(second)[0]
        wrong = self.database.load_scan_session_groups(first)[0]
        current.records[0].keep_selected = True
        with self.assertRaises(ValueError):
            self.database.update_scan_session_keep_state(second, [current, wrong])
        self.assertFalse(self.database.load_scan_session_groups(second)[0].records[0].keep_selected)

    def test_remove_paths_preserves_unseen_pages_and_zero_result_session(self):
        groups = [self.group(f"g{i}", count=3) for i in range(4)]
        session = self.database.save_scan_session([self.root], groups)
        removed = {record.path for record in groups[0].records[1:]}
        self.database.remove_scan_session_paths(session, removed)
        self.assertEqual(self.database.count_scan_session_groups(session), 3)
        self.assertEqual([group.title for group in self.database.load_scan_session_groups(session)], ["g1", "g2", "g3"])
        self.database.remove_scan_session_paths(session, {record.path for group in groups for record in group.records})
        self.assertIsNotNone(self.database.get_scan_session(session))
        self.assertEqual(self.database.load_scan_session_groups(session), [])

    def test_loading_500_groups_uses_two_queries(self):
        session = self.database.save_scan_session([self.root], [self.group(f"g{i}") for i in range(510)])
        queries = []
        self.database.connection.set_trace_callback(queries.append)
        self.assertEqual(len(self.database.load_scan_session_groups(session, limit_groups=500)), 500)
        self.database.connection.set_trace_callback(None)
        selects = [query for query in queries if query.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 2)

    def test_cache_cancel_rolls_back_updated_and_inserted_records(self):
        existing = FileRecord(self.root / "a", 1, 1, sha256="old")
        self.database.upsert_files([existing])
        checks = 0

        def check_cancel():
            nonlocal checks
            checks += 1
            if checks >= 2:
                raise ScanCancelled()

        with self.assertRaises(ScanCancelled):
            self.database.upsert_files(
                [FileRecord(existing.path, 2, 2, sha256="new"), FileRecord(self.root / "b", 3, 3)],
                check_cancel=check_cancel,
            )
        cached = self.database.list_scanned_files()
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0].sha256, "old")

    def test_reclaimable_bytes_follows_multiple_kept_files(self):
        group = self.group(count=4)
        group.records[1].keep_selected = True
        self.assertEqual(group.reclaimable_bytes, 1 + 3)
        for record in group.records:
            record.keep_selected = True
        self.assertEqual(group.reclaimable_bytes, 0)


if __name__ == "__main__":
    unittest.main()
