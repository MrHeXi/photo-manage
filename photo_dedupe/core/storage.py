from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Callable

from photo_dedupe.core.models import (
    AiAnalysisRecord,
    DuplicateGroup,
    FileRecord,
    OperationRecord,
    ScanSessionRecord,
    ScanSessionSummaryRecord,
)


def default_database_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        root = Path(base) / "PhotoManageGPT"
    else:
        root = Path.home() / ".photo_manage_gpt"
    root.mkdir(parents=True, exist_ok=True)
    return root / "photo_manage.db"


class AppDatabase:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_database_path()
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                modified_at REAL NOT NULL,
                sha256 TEXT,
                image_hash TEXT,
                video_hash TEXT,
                width INTEGER,
                height INTEGER,
                scanned_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                source_path TEXT NOT NULL,
                target_path TEXT NOT NULL,
                created_at REAL NOT NULL,
                undone_at REAL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ai_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                model TEXT NOT NULL,
                summary TEXT NOT NULL,
                score REAL,
                raw_response TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                root_paths TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                score REAL NOT NULL,
                sort_index INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES scan_sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS scan_group_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                modified_at REAL NOT NULL,
                sha256 TEXT,
                image_hash TEXT,
                video_hash TEXT,
                width INTEGER,
                height INTEGER,
                keep_selected INTEGER NOT NULL DEFAULT 0,
                sort_index INTEGER NOT NULL,
                FOREIGN KEY(group_id) REFERENCES scan_groups(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS scan_groups_session_order
                ON scan_groups(session_id, sort_index, id);
            CREATE INDEX IF NOT EXISTS scan_group_items_group_order
                ON scan_group_items(group_id, sort_index, id);
            CREATE INDEX IF NOT EXISTS scan_group_items_path
                ON scan_group_items(path, group_id);
            """
        )
        self.connection.execute("PRAGMA foreign_keys = ON")
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(operations)").fetchall()
        }
        if "undone_at" not in columns:
            self.connection.execute("ALTER TABLE operations ADD COLUMN undone_at REAL")
        file_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(files)").fetchall()
        }
        if "video_hash" not in file_columns:
            self.connection.execute("ALTER TABLE files ADD COLUMN video_hash TEXT")
        scan_group_item_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(scan_group_items)").fetchall()
        }
        if "keep_selected" not in scan_group_item_columns:
            self.connection.execute(
                "ALTER TABLE scan_group_items ADD COLUMN keep_selected INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.commit()

    def upsert_files(
        self, records: list[FileRecord], *, check_cancel: Callable[[], None] | None = None,
    ) -> None:
        def rows():
            for record in records:
                if check_cancel:
                    check_cancel()
                yield (
                    str(record.path),
                    record.size,
                    record.modified_at,
                    record.sha256,
                    str(record.image_hash) if record.image_hash is not None else None,
                    str(record.video_hash) if record.video_hash is not None else None,
                    record.width,
                    record.height,
                    time.time(),
                )
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO files (
                    path, size, modified_at, sha256, image_hash, video_hash, width, height, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,
                    modified_at=excluded.modified_at,
                    sha256=excluded.sha256,
                    image_hash=excluded.image_hash,
                    video_hash=excluded.video_hash,
                    width=excluded.width,
                    height=excluded.height,
                    scanned_at=excluded.scanned_at
                """,
                rows(),
            )
            if check_cancel:
                check_cancel()

    def log_move(self, source: Path, target: Path) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO operations (action, source_path, target_path, created_at)
            VALUES ('move', ?, ?, ?)
            """,
            (str(source), str(target), time.time()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def log_trash(self, source: Path) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO operations (action, source_path, target_path, created_at)
            VALUES ('trash', ?, '', ?)
            """,
            (str(source), time.time()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def list_operations(self, limit: int = 200) -> list[OperationRecord]:
        rows = self.connection.execute(
            """
            SELECT id, action, source_path, target_path, created_at, undone_at
            FROM operations
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            OperationRecord(
                id=int(row["id"]),
                action=str(row["action"]),
                source_path=Path(str(row["source_path"])),
                target_path=Path(str(row["target_path"])),
                created_at=float(row["created_at"]),
                undone_at=float(row["undone_at"]) if row["undone_at"] is not None else None,
            )
            for row in rows
        ]

    def get_operation(self, operation_id: int) -> OperationRecord | None:
        row = self.connection.execute(
            """
            SELECT id, action, source_path, target_path, created_at, undone_at
            FROM operations
            WHERE id = ?
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        return OperationRecord(
            id=int(row["id"]),
            action=str(row["action"]),
            source_path=Path(str(row["source_path"])),
            target_path=Path(str(row["target_path"])),
            created_at=float(row["created_at"]),
            undone_at=float(row["undone_at"]) if row["undone_at"] is not None else None,
        )

    def mark_operation_undone(self, operation_id: int) -> None:
        self.connection.execute(
            "UPDATE operations SET undone_at = ? WHERE id = ?",
            (time.time(), operation_id),
        )
        self.connection.commit()

    def list_scanned_files(self, limit: int = 500) -> list[FileRecord]:
        rows = self.connection.execute(
            """
            SELECT path, size, modified_at, sha256, image_hash, video_hash, width, height
            FROM files
            ORDER BY scanned_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            FileRecord(
                path=Path(str(row["path"])),
                size=int(row["size"]),
                modified_at=float(row["modified_at"]),
                sha256=str(row["sha256"]) if row["sha256"] is not None else None,
                image_hash=int(row["image_hash"]) if row["image_hash"] is not None else None,
                video_hash=int(row["video_hash"]) if row["video_hash"] is not None else None,
                width=int(row["width"]) if row["width"] is not None else None,
                height=int(row["height"]) if row["height"] is not None else None,
            )
            for row in rows
        ]

    def clear_scanned_files(self) -> None:
        self.connection.execute("DELETE FROM files")
        self.connection.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return default
        return str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        self.connection.commit()

    def get_cached_file(self, path: Path, size: int, modified_at: float) -> FileRecord | None:
        row = self.connection.execute(
            """
            SELECT path, size, modified_at, sha256, image_hash, video_hash, width, height
            FROM files
            WHERE path = ? AND size = ? AND modified_at = ?
            """,
            (str(path), size, modified_at),
        ).fetchone()
        if row is None:
            return None
        image_hash = int(row["image_hash"]) if row["image_hash"] is not None else None
        return FileRecord(
            path=Path(str(row["path"])),
            size=int(row["size"]),
            modified_at=float(row["modified_at"]),
            sha256=str(row["sha256"]) if row["sha256"] is not None else None,
            image_hash=image_hash,
            video_hash=int(row["video_hash"]) if row["video_hash"] is not None else None,
            width=int(row["width"]) if row["width"] is not None else None,
            height=int(row["height"]) if row["height"] is not None else None,
        )

    def save_ai_analysis(
        self,
        path: Path,
        model: str,
        summary: str,
        score: float | None,
        raw_response: str,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO ai_analysis (path, model, summary, score, raw_response, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(path), model, summary, score, raw_response, time.time()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def list_ai_analysis(self, limit: int = 200, max_score: float | None = None) -> list[AiAnalysisRecord]:
        where = ""
        params: list[object] = []
        if max_score is not None:
            where = "WHERE score IS NOT NULL AND score <= ?"
            params.append(max_score)
        params.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT id, path, model, summary, score, raw_response, created_at
            FROM ai_analysis
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [
            AiAnalysisRecord(
                id=int(row["id"]),
                path=Path(str(row["path"])),
                model=str(row["model"]),
                summary=str(row["summary"]),
                score=float(row["score"]) if row["score"] is not None else None,
                raw_response=str(row["raw_response"]),
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

    def save_scan_session(self, roots: list[Path], groups: list[DuplicateGroup]) -> int:
        with self.connection:
            session_id, group_ids = self._insert_scan_session(roots, groups)
        for group, group_id in zip(groups, group_ids):
            group.storage_id = group_id
        return session_id

    def _insert_scan_session(
        self, roots: list[Path], groups: list[DuplicateGroup]
    ) -> tuple[int, list[int]]:
        now = time.time()
        cursor = self.connection.execute(
            """
            INSERT INTO scan_sessions (root_paths, created_at)
            VALUES (?, ?)
            """,
            ("|".join(str(root) for root in roots), now),
        )
        session_id = int(cursor.lastrowid)
        group_ids: list[int] = []
        for group_index, group in enumerate(groups):
            group_cursor = self.connection.execute(
                """
                INSERT INTO scan_groups (session_id, kind, title, score, sort_index)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, group.kind, group.title, group.score, group_index),
            )
            group_id = int(group_cursor.lastrowid)
            group_ids.append(group_id)
            self.connection.executemany(
                """
                INSERT INTO scan_group_items (
                    group_id, path, size, modified_at, sha256, image_hash, video_hash, width, height, keep_selected, sort_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        group_id,
                        str(record.path),
                        record.size,
                        record.modified_at,
                        record.sha256,
                        str(record.image_hash) if record.image_hash is not None else None,
                        str(record.video_hash) if record.video_hash is not None else None,
                        record.width,
                        record.height,
                        1 if record.keep_selected else 0,
                        item_index,
                    )
                    for item_index, record in enumerate(group.records)
                ),
            )
        return session_id, group_ids

    def replace_latest_scan_session(self, roots: list[Path], groups: list[DuplicateGroup]) -> int:
        with self.connection:
            latest = self.get_latest_scan_session()
            session_id, group_ids = self._insert_scan_session(roots, groups)
            if latest is not None:
                self.connection.execute("DELETE FROM scan_sessions WHERE id = ?", (latest.id,))
        for group, group_id in zip(groups, group_ids):
            group.storage_id = group_id
        return session_id

    def update_scan_session_keep_state(self, session_id: int, groups: list[DuplicateGroup]) -> None:
        """Update loaded selections without rewriting an incomplete, paged session."""
        with self.connection:
            valid_ids = {
                int(row[0])
                for row in self.connection.execute(
                    "SELECT id FROM scan_groups WHERE session_id = ?", (session_id,)
                )
            }
            for group in groups:
                if group.storage_id not in valid_ids:
                    raise ValueError("扫描分组不属于当前历史记录，请重新加载后再保存。")
                self.connection.executemany(
                    "UPDATE scan_group_items SET keep_selected = ? WHERE group_id = ? AND path = ?",
                    ((int(record.keep_selected), group.storage_id, str(record.path)) for record in group.records),
                )

    def remove_scan_session_paths(self, session_id: int, paths: set[Path]) -> None:
        """Remove completed operation sources, including groups outside the visible page."""
        with self.connection:
            self.connection.executemany(
                """
                DELETE FROM scan_group_items WHERE path = ? AND group_id IN
                    (SELECT id FROM scan_groups WHERE session_id = ?)
                """,
                ((str(path), session_id) for path in paths),
            )
            self.connection.execute(
                """
                DELETE FROM scan_groups WHERE session_id = ? AND
                    (SELECT COUNT(*) FROM scan_group_items WHERE group_id = scan_groups.id) < 2
                """,
                (session_id,),
            )

    def get_latest_scan_session(self) -> ScanSessionRecord | None:
        row = self.connection.execute(
            """
            SELECT id, root_paths, created_at
            FROM scan_sessions
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        root_paths = [Path(item) for item in str(row["root_paths"]).split("|") if item]
        return ScanSessionRecord(
            id=int(row["id"]),
            created_at=float(row["created_at"]),
            root_paths=root_paths,
        )

    def get_scan_session(self, session_id: int) -> ScanSessionRecord | None:
        row = self.connection.execute(
            """
            SELECT id, root_paths, created_at
            FROM scan_sessions
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        root_paths = [Path(item) for item in str(row["root_paths"]).split("|") if item]
        return ScanSessionRecord(
            id=int(row["id"]),
            created_at=float(row["created_at"]),
            root_paths=root_paths,
        )

    def load_scan_session_groups(
        self,
        session_id: int,
        limit_groups: int | None = None,
        offset_groups: int = 0,
        limit_items_per_group: int | None = None,
    ) -> list[DuplicateGroup]:
        group_limit_sql = ""
        group_params: list[object] = [session_id]
        if limit_groups is not None:
            group_limit_sql = "LIMIT ? OFFSET ?"
            group_params.append(limit_groups)
            group_params.append(max(offset_groups, 0))
        elif offset_groups:
            group_limit_sql = "LIMIT -1 OFFSET ?"
            group_params.append(max(offset_groups, 0))
        group_rows = self.connection.execute(
            f"""
            SELECT id, kind, title, score
            FROM scan_groups
            WHERE session_id = ?
            ORDER BY sort_index ASC, id ASC
            {group_limit_sql}
            """,
            group_params,
        ).fetchall()
        if not group_rows:
            return []
        # Use the same indexed page query to fetch all its members in one call.
        # This avoids both N+1 round trips and SQLite's parameter-count limit.
        item_rows = self.connection.execute(
            f"""
            SELECT i.*
            FROM scan_group_items i
            JOIN (
                SELECT id, sort_index FROM scan_groups WHERE session_id = ?
                ORDER BY sort_index ASC, id ASC {group_limit_sql}
            ) page ON page.id = i.group_id
            ORDER BY page.sort_index ASC, page.id ASC, i.sort_index ASC, i.id ASC
            """,
            group_params,
        ).fetchall()
        items_by_group: dict[int, list[sqlite3.Row]] = {}
        for item in item_rows:
            items_by_group.setdefault(int(item["group_id"]), []).append(item)
        groups: list[DuplicateGroup] = []
        for group_row in group_rows:
            members = items_by_group.get(int(group_row["id"]), [])
            if limit_items_per_group is not None:
                members = members[:max(limit_items_per_group, 0)]
            records = [
                FileRecord(
                    path=Path(str(item["path"])),
                    size=int(item["size"]),
                    modified_at=float(item["modified_at"]),
                    sha256=str(item["sha256"]) if item["sha256"] is not None else None,
                    image_hash=int(item["image_hash"]) if item["image_hash"] is not None else None,
                    video_hash=int(item["video_hash"]) if item["video_hash"] is not None else None,
                    width=int(item["width"]) if item["width"] is not None else None,
                    height=int(item["height"]) if item["height"] is not None else None,
                    keep_selected=bool(int(item["keep_selected"])) if item["keep_selected"] is not None else False,
                )
                for item in members
            ]
            if len(records) > 1:
                groups.append(
                    DuplicateGroup(
                        kind=str(group_row["kind"]),
                        title=str(group_row["title"]),
                        records=records,
                        score=float(group_row["score"]),
                        storage_id=int(group_row["id"]),
                    )
                )
        return groups

    def count_scan_session_groups(self, session_id: int) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM scan_groups WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def list_scan_sessions(self, limit: int = 50) -> list[ScanSessionSummaryRecord]:
        session_rows = self.connection.execute(
            """
            WITH recent AS (
                SELECT id, root_paths, created_at FROM scan_sessions ORDER BY id DESC LIMIT ?
            ), group_totals AS (
                SELECT g.session_id, g.id,
                       COUNT(i.id) AS file_count,
                       COALESCE(SUM(i.size), 0) AS duplicate_bytes,
                       CASE WHEN COUNT(i.id) < 2 THEN 0
                            WHEN SUM(i.keep_selected) > 0
                            THEN SUM(CASE WHEN i.keep_selected = 0 THEN i.size ELSE 0 END)
                            ELSE SUM(i.size) - MAX(i.size)
                       END AS reclaimable_bytes
                FROM scan_groups g
                JOIN recent s ON s.id = g.session_id
                LEFT JOIN scan_group_items i ON i.group_id = g.id
                GROUP BY g.id
            )
            SELECT s.id, s.root_paths, s.created_at,
                   COUNT(g.id) AS group_count,
                   COALESCE(SUM(g.file_count), 0) AS file_count,
                   COALESCE(SUM(g.duplicate_bytes), 0) AS duplicate_bytes,
                   COALESCE(SUM(g.reclaimable_bytes), 0) AS reclaimable_bytes
            FROM recent s LEFT JOIN group_totals g ON g.session_id = s.id
            GROUP BY s.id ORDER BY s.id DESC
            """,
            (limit,),
        ).fetchall()
        sessions: list[ScanSessionSummaryRecord] = []
        for row in session_rows:
            session_id = int(row["id"])
            root_paths = [Path(item) for item in str(row["root_paths"]).split("|") if item]
            sessions.append(
                ScanSessionSummaryRecord(
                    id=session_id,
                    created_at=float(row["created_at"]),
                    root_paths=root_paths,
                    group_count=int(row["group_count"]),
                    file_count=int(row["file_count"]),
                    duplicate_bytes=int(row["duplicate_bytes"]),
                    reclaimable_bytes=int(row["reclaimable_bytes"]),
                )
            )
        return sessions

    def clear_scan_sessions(self) -> None:
        self.connection.execute("DELETE FROM scan_group_items")
        self.connection.execute("DELETE FROM scan_groups")
        self.connection.execute("DELETE FROM scan_sessions")
        self.connection.commit()

    def delete_scan_session(self, session_id: int) -> None:
        self.connection.execute("DELETE FROM scan_sessions WHERE id = ?", (session_id,))
        self.connection.commit()
