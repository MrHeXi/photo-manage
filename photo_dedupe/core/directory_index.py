from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from photo_dedupe.core.models import DuplicateGroup


@dataclass(slots=True)
class FileDuplicateLink:
    path: Path
    group_indices: list[int]
    peer_paths: list[Path]


def build_directory_duplicate_index(
    groups: list[DuplicateGroup],
) -> tuple[dict[Path, FileDuplicateLink], dict[Path, list[Path]]]:
    file_groups: dict[Path, set[int]] = {}
    file_peers: dict[Path, set[Path]] = {}
    directory_files: dict[Path, set[Path]] = {}

    for group_index, group in enumerate(groups):
        group_paths = [record.path for record in group.records]
        for path in group_paths:
            file_groups.setdefault(path, set()).add(group_index)
            peers = file_peers.setdefault(path, set())
            peers.update(peer for peer in group_paths if peer != path)
            directory_files.setdefault(path.parent, set()).add(path)

    file_index = {
        path: FileDuplicateLink(
            path=path,
            group_indices=sorted(group_indices),
            peer_paths=sorted(peer_paths, key=lambda item: str(item).lower()),
        )
        for path, group_indices in file_groups.items()
        for peer_paths in [file_peers.get(path, set())]
    }
    directory_index = {
        directory: sorted(paths, key=lambda item: str(item).lower())
        for directory, paths in directory_files.items()
    }
    return file_index, directory_index
