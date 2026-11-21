from __future__ import annotations

import os
from collections import defaultdict
from math import gcd
from pathlib import Path
from typing import Callable

from photo_dedupe.core.hashers import average_image_hash, hamming_distance, is_image, is_video, sha256_file
from photo_dedupe.core.models import DuplicateGroup, FileRecord
from photo_dedupe.core.storage import AppDatabase
from photo_dedupe.core.video_hash import average_video_hash, video_hash_available

ProgressCallback = Callable[[str, int, int], None]
CancelCallback = Callable[[], bool]


class ScanCancelled(Exception):
    """The scan was stopped; its previous saved results must remain intact."""


def _check_cancel(should_cancel: CancelCallback | None) -> None:
    if should_cancel and should_cancel():
        raise ScanCancelled("扫描已取消")


class _HammingIndex:
    """Exact radius candidates for 64-bit hashes, with no approximate pruning.

    Split the bits into radius + 1 disjoint segments. A pair at distance <=
    radius must share at least one segment (pigeonhole principle). Only those
    candidates need a full Hamming calculation. Each distinct hash is stored
    once; callers aggregate repeated hashes without enumerating file pairs.
    """

    def __init__(self, radius: int) -> None:
        self.radius = radius
        self.segments = []
        count = min(max(radius + 1, 1), 64)
        for segment in range(count):
            start = segment * 64 // count
            end = (segment + 1) * 64 // count
            self.segments.append((start, (1 << (end - start)) - 1))
        self.buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.values: set[int] = set()

    def add(self, value: int) -> None:
        if value in self.values:
            return
        self.values.add(value)
        for index, (shift, mask) in enumerate(self.segments):
            self.buckets[index, (value >> shift) & mask].append(value)

    def candidates(self, value: int, check_cancel: Callable[[], None]) -> set[int]:
        if self.radius < 0:
            return set()
        if self.radius >= 64:
            return self.values.copy()
        candidates: set[int] = set()
        for index, (shift, mask) in enumerate(self.segments):
            check_cancel()
            for offset, candidate in enumerate(self.buckets.get((index, (value >> shift) & mask), ())):
                if offset % 256 == 0:
                    check_cancel()
                candidates.add(candidate)
        return candidates


class DuplicateScanner:
    def __init__(self, database: AppDatabase | None = None) -> None:
        self.database = database
        # Useful for diagnostics and non-timing-based performance verification.
        self.comparison_count = 0

    def scan(
        self,
        roots: list[Path],
        image_similarity_threshold: int = 8,
        image_same_ratio_only: bool = False,
        image_same_resolution_only: bool = False,
        progress: ProgressCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ) -> list[DuplicateGroup]:
        self.comparison_count = 0
        files = self._collect_files(roots, progress, should_cancel)
        total = len(files) or 1
        records: list[FileRecord] = []

        for index, path in enumerate(files, start=1):
            _check_cancel(should_cancel)
            if progress:
                progress(f"读取文件信息：{path.name}", index, total)
            try:
                stat = path.stat()
            except OSError:
                continue
            cached = self.database.get_cached_file(path, stat.st_size, stat.st_mtime) if self.database else None
            records.append(cached or FileRecord(path=path, size=stat.st_size, modified_at=stat.st_mtime))

        exact_groups = self._exact_duplicate_groups(records, progress, should_cancel)
        _check_cancel(should_cancel)
        exact_paths = {record.path for group in exact_groups for record in group.records}
        image_records = [record for record in records if record.path not in exact_paths]
        image_groups = self._similar_image_groups(
            image_records, image_similarity_threshold, progress, should_cancel,
            same_ratio_only=image_same_ratio_only,
            same_resolution_only=image_same_resolution_only,
        )
        video_groups = self._similar_video_groups(image_records, image_similarity_threshold, progress, should_cancel)
        groups = exact_groups + image_groups + video_groups
        groups.sort(key=lambda group: (group.kind != "exact", -group.reclaimable_bytes, group.title))
        _check_cancel(should_cancel)
        if self.database:
            self.database.upsert_files(records, check_cancel=lambda: _check_cancel(should_cancel))
        return groups

    def _collect_files(
        self, roots: list[Path], progress: ProgressCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ) -> list[Path]:
        collected: set[Path] = set()
        visited: set[Path] = set()
        pending = list(reversed(roots))
        if progress:
            progress("正在遍历扫描目录", 0, 0)
        while pending:
            _check_cancel(should_cancel)
            path = pending.pop()
            try:
                path = path.expanduser().resolve()
                if path in visited:
                    continue
                visited.add(path)
                if path.is_file():
                    collected.add(path)
                elif path.is_dir():
                    with os.scandir(path) as entries:
                        for entry in entries:
                            _check_cancel(should_cancel)
                            # Resolve aliases on the next iteration and visit each
                            # real directory once, including junction/symlink loops.
                            pending.append(Path(entry.path))
                if progress and len(visited) % 128 == 0:
                    progress(f"已发现 {len(collected)} 个文件", len(collected), 0)
            except (OSError, RuntimeError):
                # Inaccessible/disappearing paths and symlink loops are skipped.
                continue
        _check_cancel(should_cancel)
        return sorted(collected, key=lambda path: str(path).lower())

    def _exact_duplicate_groups(
        self, records: list[FileRecord], progress: ProgressCallback | None,
        should_cancel: CancelCallback | None,
    ) -> list[DuplicateGroup]:
        by_size: dict[int, list[FileRecord]] = defaultdict(list)
        for record in records:
            _check_cancel(should_cancel)
            by_size[record.size].append(record)
        candidates = [record for group in by_size.values() if len(group) > 1 for record in group]
        total = len(candidates) or 1
        for index, record in enumerate(candidates, start=1):
            _check_cancel(should_cancel)
            if record.sha256:
                continue
            if progress:
                progress(f"计算精确哈希：{record.name}", index, total)
            try:
                before = record.path.stat()
                if before.st_size != record.size or before.st_mtime != record.modified_at:
                    continue
                record.sha256 = sha256_file(record.path, check_cancel=lambda: _check_cancel(should_cancel))
                after = record.path.stat()
                if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                    record.sha256 = None
            except OSError:
                record.sha256 = None
        by_hash: dict[str, list[FileRecord]] = defaultdict(list)
        for record in candidates:
            _check_cancel(should_cancel)
            if record.sha256:
                by_hash[record.sha256].append(record)
        return [
            DuplicateGroup(
                kind="exact", title=f"完全重复 · {digest[:12]}",
                records=sorted(items, key=lambda item: item.modified_at, reverse=True),
            )
            for digest, items in by_hash.items() if len(items) > 1
        ]

    def _similar_image_groups(
        self, records: list[FileRecord], threshold: int, progress: ProgressCallback | None,
        should_cancel: CancelCallback | None, same_ratio_only: bool = False,
        same_resolution_only: bool = False,
    ) -> list[DuplicateGroup]:
        images = [record for record in records if is_image(record.path)]
        total = len(images) or 1
        hashed: list[FileRecord] = []
        for index, record in enumerate(images, start=1):
            _check_cancel(should_cancel)
            if record.image_hash is None:
                if progress:
                    progress(f"计算图片指纹：{record.name}", index, total)
                result = average_image_hash(record.path)
                _check_cancel(should_cancel)
                if result is None:
                    continue
                record.image_hash, record.width, record.height = result
            hashed.append(record)
        # Filters define equivalence classes, so comparisons across them can be
        # omitted without changing the old pairwise matching semantics.
        partitions: dict[tuple, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
        for index, record in enumerate(hashed):
            _check_cancel(should_cancel)
            if same_ratio_only and (not record.width or not record.height):
                continue
            if same_resolution_only:
                key = (record.width, record.height)
            elif same_ratio_only:
                factor = gcd(record.width, record.height)
                key = (record.width // factor, record.height // factor)
            else:
                key = ()
            partitions[key][record.image_hash].append(index)

        components: list[tuple[list[int], int, int]] = []
        processed = 0
        for hash_members in partitions.values():
            values = list(hash_members)
            parent = {value: value for value in values}
            distance_sums = {value: 0 for value in values}
            pair_counts = {
                value: len(members) * (len(members) - 1) // 2 if threshold >= 0 else 0
                for value, members in hash_members.items()
            }

            def find(value: int) -> int:
                while parent[value] != value:
                    parent[value] = parent[parent[value]]
                    value = parent[value]
                return value

            index = _HammingIndex(threshold)
            for value in values:
                _check_cancel(should_cancel)
                for other in index.candidates(value, lambda: _check_cancel(should_cancel)):
                    _check_cancel(should_cancel)
                    self.comparison_count += 1
                    distance = hamming_distance(value, other)
                    if distance > threshold:
                        continue
                    root, other_root = find(value), find(other)
                    if root != other_root:
                        parent[other_root] = root
                        distance_sums[root] += distance_sums[other_root]
                        pair_counts[root] += pair_counts[other_root]
                    weight = len(hash_members[value]) * len(hash_members[other])
                    distance_sums[root] += distance * weight
                    pair_counts[root] += weight
                index.add(value)
                processed += len(hash_members[value])
                if progress:
                    progress("比较相似图片", processed, len(hashed))
            members_by_root: dict[int, list[int]] = defaultdict(list)
            for value, members in hash_members.items():
                _check_cancel(should_cancel)
                members_by_root[find(value)].extend(members)
            for root, members in members_by_root.items():
                if len(members) > 1 and pair_counts[root]:
                    components.append((sorted(members), distance_sums[root], pair_counts[root]))
        groups = []
        for indexes, distance_sum, pair_count in sorted(components, key=lambda item: item[0][0]):
            _check_cancel(should_cancel)
            score = max(0.0, 1.0 - distance_sum / pair_count / 64)
            groups.append(DuplicateGroup(
                kind="similar", title=f"相似图片 · {score:.0%}",
                records=sorted(
                    [hashed[index] for index in indexes],
                    key=lambda item: (item.width or 0) * (item.height or 0), reverse=True,
                ), score=score,
            ))
        return groups

    def _similar_video_groups(
        self, records: list[FileRecord], threshold: int, progress: ProgressCallback | None,
        should_cancel: CancelCallback | None,
    ) -> list[DuplicateGroup]:
        _check_cancel(should_cancel)
        if not video_hash_available():
            return []
        videos = [record for record in records if is_video(record.path)]
        hashed: list[FileRecord] = []
        by_hash: dict[int, list[int]] = defaultdict(list)
        index = _HammingIndex(threshold)
        for position, record in enumerate(videos, start=1):
            _check_cancel(should_cancel)
            if record.video_hash is None:
                if progress:
                    progress(f"计算视频指纹：{record.name}", position, len(videos))
                record.video_hash = average_video_hash(
                    record.path, check_cancel=lambda: _check_cancel(should_cancel),
                )
            if record.video_hash is not None:
                by_hash[record.video_hash].append(len(hashed))
                hashed.append(record)
                index.add(record.video_hash)
        groups: list[DuplicateGroup] = []
        used: set[int] = set()
        for position, record in enumerate(hashed):
            _check_cancel(should_cancel)
            if position in used:
                continue
            matches = {position}
            for other_hash in index.candidates(record.video_hash, lambda: _check_cancel(should_cancel)):
                _check_cancel(should_cancel)
                self.comparison_count += 1
                if hamming_distance(record.video_hash, other_hash) <= threshold:
                    matches.update(other for other in by_hash[other_hash] if other not in used)
            if len(matches) > 1:
                used.update(matches)
                # Original behavior is greedy seed matching, not image-style
                # transitive merging. Preserve its input order for ties.
                members = [hashed[position], *(hashed[item] for item in sorted(matches) if item != position)]
                groups.append(DuplicateGroup(
                    kind="video", title="相似视频",
                    records=sorted(members, key=lambda item: item.size, reverse=True), score=1.0,
                ))
            if progress:
                progress("比较相似视频", position + 1, len(hashed))
        return groups

    def _same_image_ratio(self, left: FileRecord, right: FileRecord) -> bool:
        if not left.width or not left.height or not right.width or not right.height:
            return False
        return left.width * right.height == right.width * left.height
