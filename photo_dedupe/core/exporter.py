from __future__ import annotations

import csv
import html
from pathlib import Path

from photo_dedupe.core.models import AiAnalysisRecord, DuplicateGroup, FileRecord, OperationRecord, ScanSessionSummaryRecord


def _format_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def _group_kind_label(kind: str) -> str:
    return {
        "exact": "完全重复",
        "similar": "相似图片",
        "video": "相似视频",
    }.get(kind, kind)


def export_files_csv(records: list[FileRecord], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["path", "size", "modified_at", "sha256", "image_hash", "video_hash", "width", "height"])
        for record in records:
            writer.writerow(
                [
                    str(record.path),
                    record.size,
                    record.modified_at,
                    record.sha256 or "",
                    record.image_hash if record.image_hash is not None else "",
                    record.video_hash if record.video_hash is not None else "",
                    record.width if record.width is not None else "",
                    record.height if record.height is not None else "",
                ]
            )


def export_operations_csv(records: list[OperationRecord], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["id", "action", "source_path", "target_path", "created_at", "undone_at"])
        for record in records:
            writer.writerow(
                [
                    record.id,
                    record.action,
                    str(record.source_path),
                    str(record.target_path),
                    record.created_at,
                    record.undone_at if record.undone_at is not None else "",
                ]
            )


def export_ai_analysis_csv(records: list[AiAnalysisRecord], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["id", "path", "model", "score", "summary", "created_at"])
        for record in records:
            writer.writerow(
                [
                    record.id,
                    str(record.path),
                    record.model,
                    "" if record.score is None else record.score,
                    record.summary,
                    record.created_at,
                ]
            )


def export_scan_sessions_csv(records: list[ScanSessionSummaryRecord], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["id", "created_at", "root_paths", "group_count", "file_count", "duplicate_bytes", "reclaimable_bytes"])
        for record in records:
            writer.writerow(
                [
                    record.id,
                    record.created_at,
                    " | ".join(str(path) for path in record.root_paths),
                    record.group_count,
                    record.file_count,
                    record.duplicate_bytes,
                    record.reclaimable_bytes,
                ]
            )


def build_duplicate_report_html(groups: list[DuplicateGroup], title: str = "Photo Manage GPT 扫描报告") -> str:
    total_files = sum(len(group.records) for group in groups)
    total_bytes = sum(record.size for group in groups for record in group.records)
    reclaimable_bytes = sum(group.reclaimable_bytes for group in groups)

    group_sections: list[str] = []
    for index, group in enumerate(groups, start=1):
        rows: list[str] = []
        for record in sorted(group.records, key=lambda item: str(item.path).lower()):
            dimensions = f"{record.width}x{record.height}" if record.width and record.height else "-"
            keep_label = "保留" if record.keep_selected else "候选处理"
            rows.append(
                "<tr>"
                f"<td>{html.escape(keep_label)}</td>"
                f"<td>{html.escape(record.name)}</td>"
                f"<td>{_format_size(record.size)}</td>"
                f"<td>{html.escape(dimensions)}</td>"
                f"<td>{html.escape(str(record.path))}</td>"
                "</tr>"
            )
        group_sections.append(
            "<section>"
            f"<h2>#{index} {html.escape(group.title)}</h2>"
            "<p>"
            f"{html.escape(_group_kind_label(group.kind))} · "
            f"{len(group.records)} 个文件 · "
            f"预估可省 {_format_size(group.reclaimable_bytes)} · "
            f"相似度 {group.score:.0%}"
            "</p>"
            "<table><thead><tr><th>状态</th><th>文件名</th><th>大小</th><th>尺寸</th><th>路径</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            "</section>"
        )

    escaped_title = html.escape(title)
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{escaped_title}</title>"
        "<style>"
        "body{font-family:'Microsoft YaHei UI','Segoe UI',sans-serif;margin:32px;color:#1f2328;background:#f6f7f9;}"
        "h1{font-size:24px;margin:0 0 8px;}h2{font-size:18px;margin:24px 0 4px;}"
        ".summary{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0 24px;}"
        ".summary div{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:12px 14px;min-width:140px;}"
        "section{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:16px;margin-bottom:16px;}"
        "table{border-collapse:collapse;width:100%;font-size:13px;}th,td{border-top:1px solid #d8dee4;padding:8px;text-align:left;vertical-align:top;}"
        "th{background:#f6f8fa;}td:last-child{word-break:break-all;}"
        "</style></head><body>"
        f"<h1>{escaped_title}</h1>"
        "<p>这份报告仅用于查看和核对扫描结果，不会移动或删除任何文件。</p>"
        "<div class=\"summary\">"
        f"<div><strong>重复分组</strong><br>{len(groups)}</div>"
        f"<div><strong>重复文件</strong><br>{total_files}</div>"
        f"<div><strong>当前占用</strong><br>{_format_size(total_bytes)}</div>"
        f"<div><strong>预估可省</strong><br>{_format_size(reclaimable_bytes)}</div>"
        "</div>"
        f"{''.join(group_sections) if group_sections else '<p>当前没有可导出的重复分组。</p>'}"
        "</body></html>"
    )


def export_duplicate_report_html(groups: list[DuplicateGroup], target: Path, title: str = "Photo Manage GPT 扫描报告") -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_duplicate_report_html(groups, title), encoding="utf-8")
