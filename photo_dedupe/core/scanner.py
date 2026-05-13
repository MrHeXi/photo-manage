from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Callable

from photo_dedupe.core.hashers import average_image_hash, hamming_distance, is_image, is_video, sha256_file
from photo_dedupe.core.models import DuplicateGroup, FileRecord
from photo_dedupe.core.storage import AppDatabase
from photo_dedupe.core.video_hash import average_video_hash, video_hash_available

ProgressCallback = Callable[[str, int, int], None]
CancelCallback = Callable[[], bool]


class DuplicateScanner:
    def __init__(self, database: AppDatabase | None = None) -> None:
        self.database = database

    def scan(
        self,
        roots: list[Path],
        image_similarity_threshold: int = 8,
        progress: ProgressCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ) -> list[DuplicateGroup]:
        files = self._collect_files(roots)
        total = len(files) or 1
        records: list[FileRecord] = []

        for index, path in enumerate(files, start=1):
            if should_cancel and should_cancel():
                return []
            if progress:
                progress(f"读取文件信息：{path.name}", index, total)
            try:
                stat = path.stat()
            except OSError:
                continue
            cached = self.database.get_cached_file(path, stat.st_size, stat.st_mtime) if self.database else None
            if cached:
                records.append(cached)
            else:
                records.append(FileRecord(path=path, size=stat.st_size, modified_at=stat.st_mtime))

        exact_groups = self._exact_duplicate_groups(records, progress, should_cancel)
        if should_cancel and should_cancel():
            return []
        exact_paths = {record.path for group in exact_groups for record in group.records}
        image_records = [record for record in records if record.path not in exact_paths]
        image_groups = self._similar_image_groups(image_records, image_similarity_threshold, progress, should_cancel)
        video_groups = self._similar_video_groups(image_records, image_similarity_threshold, progress, should_cancel)

        if self.database:
            self.database.upsert_files(records)

        groups = exact_groups + image_groups + video_groups
        groups.sort(key=lambda group: (group.kind != "exact", -group.reclaimable_bytes, group.title))
        return groups

    def _similar_video_groups(
        self,
        records: list[FileRecord],
        threshold: int,
        progress: ProgressCallback | None,
        should_cancel: CancelCallback | None,
    ) -> list[DuplicateGroup]:
        if not video_hash_available():
            return []

        videos = [record for record in records if is_video(record.path)]
        total = len(videos) or 1
        hashed: list[FileRecord] = []
        for index, record in enumerate(videos, start=1):
            if should_cancel and should_cancel():
                return []
            if record.video_hash is None:
                if progress:
                    progress(f"计算视频指纹：{record.name}", index, total)
                record.video_hash = average_video_hash(record.path)
            if record.video_hash is not None:
                hashed.append(record)

        buckets: list[list[FileRecord]] = []
        used: set[Path] = set()
        for record in hashed:
            if record.path in used or record.video_hash is None:
                continue
            group = [record]
            for other in hashed:
                if other.path == record.path or other.path in used or other.video_hash is None:
                    continue
                if hamming_distance(record.video_hash, other.video_hash) <= threshold:
                    group.append(other)
            if len(group) > 1:
                for item in group:
                    used.add(item.path)
                buckets.append(group)

        groups: list[DuplicateGroup] = []
        for bucket in buckets:
            groups.append(
                DuplicateGroup(
                    kind="video",
                    title="相似视频",
                    records=sorted(bucket, key=lambda item: item.size, reverse=True),
                    score=1.0,
                )
            )
        return groups

    def _collect_files(self, roots: list[Path]) -> list[Path]:
        collected: list[Path] = []
        for root in roots:
            if root.is_file():
                collected.append(root)
                continue
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.is_file():
                    collected.append(path)
        return collected

    def _exact_duplicate_groups(
        self,
        records: list[FileRecord],
        progress: ProgressCallback | None,
        should_cancel: CancelCallback | None,
    ) -> list[DuplicateGroup]:
        by_size: dict[int, list[FileRecord]] = defaultdict(list)
        for record in records:
            by_size[record.size].append(record)

        candidates = [record for group in by_size.values() if len(group) > 1 for record in group]
        total = len(candidates) or 1
        for index, record in enumerate(candidates, start=1):
            if should_cancel and should_cancel():
                return []
            if record.sha256:
                continue
            if progress:
                progress(f"计算精确哈希：{record.name}", index, total)
            try:
                record.sha256 = sha256_file(record.path)
            except OSError:
                record.sha256 = None

        by_hash: dict[str, list[FileRecord]] = defaultdict(list)
        for record in candidates:
            if record.sha256:
                by_hash[record.sha256].append(record)

        groups: list[DuplicateGroup] = []
        for digest, items in by_hash.items():
            if len(items) > 1:
                groups.append(
                    DuplicateGroup(
                        kind="exact",
                        title=f"完全重复 · {digest[:12]}",
                        records=sorted(items, key=lambda item: item.modified_at, reverse=True),
                    )
                )
        return groups

    def _similar_image_groups(
        self,
        records: list[FileRecord],
        threshold: int,
        progress: ProgressCallback | None,
        should_cancel: CancelCallback | None,
    ) -> list[DuplicateGroup]:
        images = [record for record in records if is_image(record.path)]
        total = len(images) or 1
        hashed: list[FileRecord] = []
        for index, record in enumerate(images, start=1):
            if should_cancel and should_cancel():
                return []
            if record.image_hash is None:
                if progress:
                    progress(f"计算图片指纹：{record.name}", index, total)
                result = average_image_hash(record.path)
                if result is None:
                    continue
                record.image_hash, record.width, record.height = result
            hashed.append(record)

        if should_cancel and should_cancel():
            return []

        parent = list(range(len(hashed)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        pair_distances: dict[tuple[int, int], int] = {}
        for left_index, left in enumerate(hashed):
            if should_cancel and should_cancel():
                return []
            if left.image_hash is None:
                continue
            for right_index in range(left_index + 1, len(hashed)):
                right = hashed[right_index]
                if right.image_hash is None:
                    continue
                distance = hamming_distance(left.image_hash, right.image_hash)
                if distance <= threshold:
                    union(left_index, right_index)
                    pair_distances[(left_index, right_index)] = distance

        buckets: dict[int, list[int]] = defaultdict(list)
        for index in range(len(hashed)):
            buckets[find(index)].append(index)

        groups: list[DuplicateGroup] = []
        for indexes in buckets.values():
            if len(indexes) <= 1:
                continue
            distances = []
            for left_pos, left_index in enumerate(indexes):
                for right_index in indexes[left_pos + 1 :]:
                    key = (min(left_index, right_index), max(left_index, right_index))
                    if key in pair_distances:
                        distances.append(pair_distances[key])
            members = [hashed[index] for index in indexes]
            average_distance = sum(distances) / len(distances) if distances else 0
            score = max(0.0, 1.0 - average_distance / 64)
            groups.append(
                DuplicateGroup(
                    kind="similar",
                    title=f"相似图片 · {score:.0%}",
                    records=sorted(members, key=lambda item: (item.width or 0) * (item.height or 0), reverse=True),
                    score=score,
                )
            )
        return groups
