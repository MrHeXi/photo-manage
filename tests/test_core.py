from __future__ import annotations

import os
import tempfile
import unittest
import csv
import hashlib
import json
import logging
import zipfile
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QLockFile

from photo_dedupe import __version__
from photo_dedupe.core.ai_client import AiConfig, format_ai_config_validation, parse_chat_content, parse_score, validate_ai_config
from photo_dedupe.core.diagnostics import (
    DiagnosticItem,
    build_health_items,
    build_support_diagnostic_report,
    default_log_path,
    format_health_report,
    setup_logging,
)
from photo_dedupe.core.demo_workspace import DEMO_WORKSPACE_NAME, create_demo_workspace
from photo_dedupe.core.directory_index import build_directory_duplicate_index
from photo_dedupe.core.exporter import (
    build_duplicate_report_html,
    export_ai_analysis_csv,
    export_duplicate_report_html,
    export_files_csv,
    export_operations_csv,
    export_scan_sessions_csv,
)
from photo_dedupe.core.file_details import inspect_file_preview
from photo_dedupe.core.file_ops import move_records, undo_move, undo_move_operations, unique_target_path
from photo_dedupe.core.file_reveal import build_reveal_command, operation_reveal_path
from photo_dedupe.core.group_summary import summarize_duplicate_group
from photo_dedupe.core.image_compare import build_image_compare_items
from photo_dedupe.core.models import DuplicateGroup, FileRecord, OperationRecord, ScanSessionSummaryRecord
from photo_dedupe.core.organizer import (
    OrganizePreviewItem,
    OrganizePreviewSummary,
    OrganizeTargetConfig,
    build_organize_preview,
    category_label,
    describe_organize_preview_item,
    execute_organize_preview,
    organize_by_type,
    organize_files,
    organize_images_by_date,
    summarize_organize_preview,
)
from photo_dedupe.core.operation_report import (
    build_organize_result_report,
    build_processing_result_report,
    export_operation_result_report,
    format_operation_result_report,
    save_operation_result_report,
)
from photo_dedupe.core.operation_filters import (
    ACTION_MOVE,
    ACTION_TRASH,
    STATUS_UNDONE,
    filter_operations,
    format_operation_filter_summary,
    summarize_operations,
)
from photo_dedupe.core.organize_profiles import (
    OrganizeProfile,
    build_template_profile,
    clean_profile_name,
    decode_profiles,
    encode_profiles,
    get_builtin_template,
    list_builtin_templates,
    profile_summary,
    remove_profile,
    upsert_profile,
)
from photo_dedupe.core.onboarding import (
    STATUS_DONE,
    STATUS_NEXT,
    STATUS_RECOMMENDED,
    STATUS_WAITING,
    OnboardingSnapshot,
    build_onboarding_steps,
    onboarding_primary_guidance,
    onboarding_summary,
)
from photo_dedupe.core.progress_status import (
    format_progress_message,
    format_restore_status,
    format_scan_start,
    progress_percent,
)
from photo_dedupe.core.processing_risk import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    available_destination_free_bytes,
    build_processing_risk_summary,
    format_processing_confirmation,
)
from photo_dedupe.core.release_check import STATUS_ERROR, STATUS_OK, STATUS_WARN, format_release_check_report, run_release_check
from photo_dedupe.core.release_manifest import MANIFEST_FILENAME, build_release_manifest, write_release_manifest
from photo_dedupe.core.scanner import DuplicateScanner
from photo_dedupe.core.selection import build_processing_plan, choose_keep_records, describe_keep_reasons
from photo_dedupe.core.safety import SEVERITY_ERROR, SEVERITY_WARNING, validate_scan_roots
from photo_dedupe.core.single_instance import default_lock_path
from photo_dedupe.core.storage import AppDatabase
from photo_dedupe.core.video_hash import combine_hashes


class ScanSafetyTests(unittest.TestCase):
    def test_scan_safety_rejects_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-drive"

            issues = validate_scan_roots([missing])

        self.assertTrue(any(issue.severity == SEVERITY_ERROR and "不存在" in issue.title for issue in issues))

    def test_scan_safety_rejects_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "photo.jpg"
            file_path.write_text("x", encoding="utf-8")

            issues = validate_scan_roots([file_path])

        self.assertTrue(any(issue.severity == SEVERITY_ERROR and "不是文件夹" in issue.title for issue in issues))

    def test_scan_safety_warns_about_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty"
            empty.mkdir()

            issues = validate_scan_roots([empty])

        self.assertTrue(any(issue.severity == SEVERITY_WARNING and "为空" in issue.title for issue in issues))

    def test_scan_safety_rejects_drive_root(self) -> None:
        drive_root = Path(Path.cwd().anchor or "/")

        issues = validate_scan_roots([drive_root])

        self.assertTrue(any(issue.severity == SEVERITY_ERROR and "整盘根目录" in issue.title for issue in issues))

    def test_scan_safety_rejects_windows_system_directory(self) -> None:
        with patch.dict(os.environ, {"WINDIR": "C:\\Windows"}):
            issues = validate_scan_roots([Path("C:/Windows/System32")])

        self.assertTrue(any(issue.severity == SEVERITY_ERROR and "系统目录" in issue.title for issue in issues))

    def test_scan_safety_rejects_target_inside_scan_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photos = root / "Photos"
            photos.mkdir()

            issues = validate_scan_roots([root], target_roots=[photos])

        self.assertTrue(any(issue.severity == SEVERITY_ERROR and "处理目标目录" in issue.title for issue in issues))

    def test_scan_safety_allows_normal_user_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Pictures"
            target = Path(directory) / "Sorted"
            root.mkdir()
            target.mkdir()
            (root / "photo.jpg").write_text("x", encoding="utf-8")

            issues = validate_scan_roots([root], target_roots=[target])

        self.assertEqual(issues, [])

    def test_scan_safety_warns_about_nested_scan_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Pictures"
            child = root / "Trip"
            child.mkdir(parents=True)

            issues = validate_scan_roots([root, child])

        self.assertTrue(any(issue.severity == SEVERITY_WARNING and "父目录" in issue.message for issue in issues))


class BrandingAssetTests(unittest.TestCase):
    def test_app_icon_assets_exist(self) -> None:
        assets = Path(__file__).resolve().parents[1] / "photo_dedupe" / "assets"

        for filename in ("app_icon.ico", "app_icon.png"):
            path = assets / filename
            self.assertTrue(path.exists(), filename)
            self.assertGreater(path.stat().st_size, 0, filename)

    def test_package_version_matches_pyproject(self) -> None:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"

        self.assertIn(f'version = "{__version__}"', pyproject.read_text(encoding="utf-8"))

    def test_safe_mode_script_is_available(self) -> None:
        script = Path(__file__).resolve().parents[1] / "run-safe.ps1"

        self.assertTrue(script.exists())
        self.assertIn("--safe-mode", script.read_text(encoding="utf-8"))

    def test_release_package_script_includes_safe_launcher(self) -> None:
        script = Path(__file__).resolve().parents[1] / "package.ps1"
        content = script.read_text(encoding="utf-8")

        self.assertIn("CreateFromDirectory", content)
        self.assertIn("photo_dedupe.__version__", content)
        self.assertIn("PhotoManageGPT-safe-mode.cmd", content)
        self.assertIn("install-shortcut.ps1", content)
        self.assertIn("remove-shortcut.ps1", content)
        self.assertIn("QUICKSTART.md", content)
        self.assertIn("CHANGELOG.md", content)
        self.assertIn("Get-FileHash", content)
        self.assertIn(".sha256", content)
        self.assertIn("--release-check", content)
        self.assertIn("--write-release-manifest", content)
        self.assertIn("--startup-smoke-test", content)

    def test_shortcut_scripts_only_touch_desktop_shortcuts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        install_script = root / "install-shortcut.ps1"
        remove_script = root / "remove-shortcut.ps1"
        install_content = install_script.read_text(encoding="utf-8")
        remove_content = remove_script.read_text(encoding="utf-8")

        self.assertTrue(install_script.exists())
        self.assertTrue(remove_script.exists())
        self.assertIn("WScript.Shell", install_content)
        self.assertIn("PhotoManageGPT.exe", install_content)
        self.assertIn("--safe-mode", install_content)
        self.assertIn(".lnk", remove_content)
        self.assertIn("Remove-Item -LiteralPath $shortcutPath", remove_content)
        self.assertNotIn("-Recurse", remove_content)

    def test_changelog_describes_release_safety(self) -> None:
        changelog = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
        content = changelog.read_text(encoding="utf-8")

        self.assertIn("Windows 推广版", content)
        self.assertIn("默认不做永久删除", content)
        self.assertIn("启动烟测", content)
        self.assertIn(".zip.sha256", content)
        self.assertIn("桌面快捷方式", content)

    def test_quickstart_guides_safe_first_run(self) -> None:
        quickstart = Path(__file__).resolve().parents[1] / "QUICKSTART.md"
        content = quickstart.read_text(encoding="utf-8")

        self.assertIn("三分钟上手", content)
        self.assertIn("设为保护目录", content)
        self.assertIn("勾选", content)
        self.assertIn("默认不做永久删除", content)
        self.assertIn("install-shortcut.ps1", content)

    def test_build_script_runs_release_check(self) -> None:
        script = Path(__file__).resolve().parents[1] / "build.ps1"

        self.assertIn("--release-check", script.read_text(encoding="utf-8"))

    def test_pyinstaller_spec_includes_icon_and_assets(self) -> None:
        spec = Path(__file__).resolve().parents[1] / "PhotoManageGPT.spec"
        content = spec.read_text(encoding="utf-8")
        normalized = content.replace("\\\\", "/").replace("\\", "/")

        self.assertIn("app_icon.ico", content)
        self.assertIn("photo_dedupe/assets", normalized)

    def test_release_check_passes_minimal_release_tree_with_zip_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._create_release_tree(Path(directory))

            report = run_release_check(root)
            text = format_release_check_report(report)

        self.assertTrue(report.passed, text)
        self.assertTrue(any(item.name == "发布 zip" and item.status == STATUS_WARN for item in report.items))
        self.assertIn("结论：通过", text)

    def test_release_manifest_records_files_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._create_release_tree(Path(directory))
            release_dir = root / "release" / "PhotoManageGPT-v0.1.0-win64"
            manifest_path = write_release_manifest(release_dir, "0.1.0")
            manifest = build_release_manifest(release_dir, "0.1.0")

        self.assertEqual(manifest_path.name, MANIFEST_FILENAME)
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertTrue(any(item["path"] == "PhotoManageGPT.exe" and len(item["sha256"]) == 64 for item in manifest["files"]))
        self.assertTrue(any("默认不做永久删除" in note for note in manifest["safety"]))

    def test_release_check_fails_if_existing_release_dir_lacks_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._create_release_tree(Path(directory), with_manifest=False)

            report = run_release_check(root)

        self.assertFalse(report.passed)
        self.assertTrue(any(item.name == "发布 Manifest" and item.status == STATUS_ERROR for item in report.items))

    def test_release_check_validates_zip_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._create_release_tree(Path(directory))
            self._create_release_zip(root, include_manifest=False)

            report = run_release_check(root)

        self.assertFalse(report.passed)
        self.assertTrue(any(item.name == "发布 zip" and item.status == STATUS_ERROR for item in report.items))

    def test_release_check_validates_zip_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._create_release_tree(Path(directory))
            zip_path = self._create_release_zip(root)
            self._write_release_checksum(zip_path)

            report = run_release_check(root)

        self.assertTrue(report.passed, format_release_check_report(report))
        self.assertTrue(any(item.name == "发布校验" and item.status == STATUS_OK for item in report.items))

    def test_release_check_fails_on_bad_zip_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._create_release_tree(Path(directory))
            zip_path = self._create_release_zip(root)
            Path(str(zip_path) + ".sha256").write_text(f"0  {zip_path.name}\n", encoding="ascii")

            report = run_release_check(root)

        self.assertFalse(report.passed)
        self.assertTrue(any(item.name == "发布校验" and item.status == STATUS_ERROR for item in report.items))

    def test_release_check_fails_without_exe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._create_release_tree(Path(directory))
            (root / "dist" / "PhotoManageGPT" / "PhotoManageGPT.exe").unlink()

            report = run_release_check(root)

        self.assertFalse(report.passed)
        self.assertTrue(any(item.name == "EXE 产物" and item.status == STATUS_ERROR for item in report.items))

    def _create_release_tree(self, root: Path, with_manifest: bool = True) -> Path:
        (root / "photo_dedupe" / "assets").mkdir(parents=True)
        (root / "photo_dedupe" / "ui").mkdir(parents=True)
        (root / "dist" / "PhotoManageGPT").mkdir(parents=True)
        release_dir = root / "release" / "PhotoManageGPT-v0.1.0-win64"
        release_dir.mkdir(parents=True)
        (root / "README.md").write_text(
            "安全模式\n打包 EXE\n发布自检\n默认不做永久删除\n处理结果报告\n操作记录\nRELEASE-MANIFEST.json\n启动烟测\n桌面快捷方式\n",
            encoding="utf-8",
        )
        (root / "QUICKSTART.md").write_text("三分钟上手\n设为保护目录\n勾选\n默认不做永久删除\n", encoding="utf-8")
        (root / "CHANGELOG.md").write_text("Windows 推广版\n默认不做永久删除\n启动烟测\n", encoding="utf-8")
        (root / "build.ps1").write_text("PyInstaller PhotoManageGPT.spec\n--release-check\n", encoding="utf-8")
        (root / "package.ps1").write_text(
            "photo_dedupe.__version__\nPhotoManageGPT-safe-mode.cmd\ninstall-shortcut.ps1\nremove-shortcut.ps1\nREADME.md\nQUICKSTART.md\nCHANGELOG.md\nCreateFromDirectory\nGet-FileHash\n.sha256\n--release-check\n--write-release-manifest\n--startup-smoke-test\n",
            encoding="utf-8",
        )
        (root / "run-safe.ps1").write_text("--safe-mode\n", encoding="utf-8")
        (root / "install-shortcut.ps1").write_text("WScript.Shell\nPhotoManageGPT.exe\n--safe-mode\n", encoding="utf-8")
        (root / "remove-shortcut.ps1").write_text("Remove-Item -LiteralPath $shortcutPath\n", encoding="utf-8")
        (root / "PhotoManageGPT.spec").write_text(
            "datas=[('photo_dedupe\\\\assets','photo_dedupe\\\\assets')]\nconsole=False\nicon='photo_dedupe\\\\assets\\\\app_icon.ico'\n",
            encoding="utf-8",
        )
        (root / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
        (root / "photo_dedupe" / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
        (root / "photo_dedupe" / "__main__.py").write_text("", encoding="utf-8")
        (root / "photo_dedupe" / "ui" / "main_window.py").write_text("", encoding="utf-8")
        (root / "photo_dedupe" / "assets" / "app_icon.ico").write_bytes(b"icon")
        (root / "photo_dedupe" / "assets" / "app_icon.png").write_bytes(b"png")
        (root / "dist" / "PhotoManageGPT" / "PhotoManageGPT.exe").write_bytes(b"exe")
        (release_dir / "PhotoManageGPT.exe").write_bytes(b"exe")
        (release_dir / "README.md").write_text("readme", encoding="utf-8")
        (release_dir / "QUICKSTART.md").write_text("quickstart", encoding="utf-8")
        (release_dir / "CHANGELOG.md").write_text("changelog", encoding="utf-8")
        (release_dir / "PhotoManageGPT-safe-mode.cmd").write_text("--safe-mode", encoding="ascii")
        (release_dir / "install-shortcut.ps1").write_text("shortcut", encoding="ascii")
        (release_dir / "remove-shortcut.ps1").write_text("remove", encoding="ascii")
        if with_manifest:
            write_release_manifest(release_dir, "0.1.0")
        return root

    def _create_release_zip(self, root: Path, include_manifest: bool = True) -> Path:
        release_dir = root / "release" / "PhotoManageGPT-v0.1.0-win64"
        zip_path = root / "release" / "PhotoManageGPT-v0.1.0-win64.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            for path in release_dir.rglob("*"):
                if not path.is_file():
                    continue
                if not include_manifest and path.name == MANIFEST_FILENAME:
                    continue
                archive.write(path, path.relative_to(release_dir).as_posix())
        return zip_path

    def _write_release_checksum(self, zip_path: Path) -> Path:
        digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        target = Path(str(zip_path) + ".sha256")
        target.write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")
        return target


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
                app_version=__version__,
                loaded_groups=500,
                total_groups=1200,
            )

        by_name = {item.name: item for item in items}
        self.assertEqual(by_name["应用版本"].detail, __version__)
        self.assertEqual(by_name["数据库"].status, "正常")
        self.assertEqual(by_name["启动日志"].status, "正常")
        self.assertEqual(by_name["视频指纹"].status, "可选缺失")
        self.assertIn("500/1200", by_name["历史缓存"].detail)
        self.assertEqual(by_name["后台残留排查"].status, "提示")
        self.assertIn("安全模式", by_name["后台残留排查"].detail)

    def test_format_health_report_is_plain_text(self) -> None:
        report = format_health_report(
            [
                DiagnosticItem("应用版本", "正常", "0.1.0"),
                DiagnosticItem("启动日志", "正常", "C:/logs/app.log"),
            ]
        )

        self.assertIn("Photo Manage GPT 健康诊断报告", report)
        self.assertIn("应用版本: 正常", report)
        self.assertIn("C:/logs/app.log", report)

    def test_support_diagnostic_report_includes_runtime_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "photo_manage.db"
            log_path = root / "logs" / "app.log"
            database_path.write_text("db", encoding="utf-8")
            log_path.parent.mkdir()
            log_path.write_text("log", encoding="utf-8")

            report = build_support_diagnostic_report(
                app_version="0.1.0",
                database_path=database_path,
                log_path=log_path,
                ffmpeg_available=True,
                history_state="已恢复历史结果",
                safe_mode=True,
                loaded_groups=12,
                total_groups=30,
                data_dir=root,
                executable="PhotoManageGPT.exe",
                cwd=root / "cwd",
                pid=1234,
                frozen=True,
            )

        self.assertIn("Photo Manage GPT 诊断信息", report)
        self.assertIn("版本：0.1.0", report)
        self.assertIn("安全模式：是", report)
        self.assertIn("打包运行：是", report)
        self.assertIn("进程 PID：1234", report)
        self.assertIn("PhotoManageGPT.exe", report)
        self.assertIn("已加载 12/30 组", report)
        self.assertIn("ffmpeg：已检测到", report)
        self.assertIn("不包含 AI API Key", report)


class ProgressStatusTests(unittest.TestCase):
    def test_progress_percent_clamps_values(self) -> None:
        self.assertEqual(progress_percent(5, 10), 50)
        self.assertEqual(progress_percent(15, 10), 100)
        self.assertEqual(progress_percent(-1, 10), 0)
        self.assertEqual(progress_percent(1, 0), 0)

    def test_scan_progress_message_includes_count_and_percent(self) -> None:
        message = format_progress_message("正在读取文件", 5, 10)

        self.assertIn("5/10", message)
        self.assertIn("50%", message)

    def test_scan_progress_message_handles_unknown_total(self) -> None:
        self.assertIn("处理中", format_progress_message("正在读取文件", 1, 0))

    def test_restore_status_without_total_reads_cache_index(self) -> None:
        self.assertIn("读取缓存索引", format_restore_status(0, 0))

    def test_restore_status_includes_count_and_percent(self) -> None:
        status = format_restore_status(500, 1200)

        self.assertIn("500/1200", status)
        self.assertIn("42%", status)

    def test_scan_start_mentions_root_count(self) -> None:
        self.assertIn("3 个目录", format_scan_start(3))


class OrganizeProfileTests(unittest.TestCase):
    def sample_profile(self, name: str = "照片库整理") -> OrganizeProfile:
        return OrganizeProfile(
            name=name,
            scan_roots=(Path("D:/Photos"), Path("E:/Backup")),
            protected_roots=(Path("D:/Photos/Library"),),
            duplicate_destination=Path("D:/Duplicates"),
            keep_strategy="保留最高质量",
            bulk_keep_rule="保留保护目录内文件",
            bulk_keep_keyword="原图",
            processing_action="移动到目标目录",
            similarity_threshold=8,
            same_ratio_only=True,
            same_resolution_only=False,
            rotate_hint=True,
            organize_rule="按拍摄年月",
            organize_keep_only=True,
            photo_target=Path("D:/Library/Photos"),
            video_target=Path("D:/Library/Videos"),
            document_target=Path("D:/Library/Documents"),
            other_target=Path("D:/Library/Others"),
        )

    def test_profiles_round_trip_as_json(self) -> None:
        profile = self.sample_profile()

        restored = decode_profiles(encode_profiles([profile]))

        self.assertEqual(restored, [profile])

    def test_profile_name_is_trimmed_and_required(self) -> None:
        self.assertEqual(clean_profile_name("  照片   整理  "), "照片 整理")
        with self.assertRaises(ValueError):
            clean_profile_name("   ")

    def test_upsert_replaces_existing_profile_name(self) -> None:
        original = self.sample_profile("Photos")
        replacement = self.sample_profile("photos")

        profiles = upsert_profile([original], replacement)

        self.assertEqual(profiles, [replacement])

    def test_remove_profile_uses_name(self) -> None:
        profile = self.sample_profile("照片库")

        self.assertEqual(remove_profile([profile], "照片库"), [])

    def test_decode_profiles_ignores_invalid_payload(self) -> None:
        self.assertEqual(decode_profiles("not json"), [])
        self.assertEqual(decode_profiles('[{"name": ""}, {"name": "可用"}]')[0].name, "可用")

    def test_profile_summary_mentions_core_counts_and_rules(self) -> None:
        summary = profile_summary(self.sample_profile())

        self.assertIn("扫描目录 2 个", summary)
        self.assertIn("保护目录 1 个", summary)
        self.assertIn("保留最高质量", summary)

    def test_builtin_templates_are_available_for_common_workflows(self) -> None:
        templates = list_builtin_templates()

        names = {template.name for template in templates}
        self.assertIn("家庭照片整理", names)
        self.assertIn("多盘照片汇总", names)
        self.assertIn("工作文档归档", names)
        self.assertIn("聊天缓存复核", names)
        self.assertTrue(all(template.description for template in templates))

    def test_build_template_profile_preserves_user_roots(self) -> None:
        base_root = Path("D:/PhotoManageGPT_整理库")
        profile = build_template_profile(
            "family_photo_library",
            base_root,
            scan_roots=(Path("D:/Photos"),),
            protected_roots=(Path("D:/Photos/Library"),),
        )

        self.assertEqual(profile.name, "家庭照片整理")
        self.assertEqual(profile.scan_roots, (Path("D:/Photos"),))
        self.assertEqual(profile.protected_roots, (Path("D:/Photos/Library"),))
        self.assertEqual(profile.bulk_keep_rule, "保留保护目录内文件")
        self.assertEqual(profile.organize_rule, "按拍摄年月")
        self.assertTrue(profile.organize_keep_only)
        self.assertEqual(profile.duplicate_destination, base_root / "重复待复核")
        self.assertEqual(profile.photo_target, base_root / "照片")

    def test_unknown_template_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            get_builtin_template("missing")


class DemoWorkspaceTests(unittest.TestCase):
    def test_create_demo_workspace_builds_unique_safe_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            first = create_demo_workspace(parent)
            second = create_demo_workspace(parent)

            library_photo = first.scan_roots[0] / "Photos" / "2026" / "family-original.jpg"
            backup_photo = first.scan_roots[1] / "Backup" / "family-copy.jpg"
            library_doc = first.scan_roots[0] / "Documents" / "project-notes.txt"
            backup_doc = first.scan_roots[1] / "Backup" / "project-notes-copy.txt"

            self.assertEqual(first.root.name, DEMO_WORKSPACE_NAME)
            self.assertEqual(second.root.name, f"{DEMO_WORKSPACE_NAME}-2")
            self.assertEqual(len(first.scan_roots), 2)
            self.assertEqual(first.protected_roots, (first.scan_roots[0],))
            self.assertGreaterEqual(first.file_count, 7)
            self.assertEqual(first.duplicate_pair_count, 3)
            self.assertEqual(library_photo.read_bytes(), backup_photo.read_bytes())
            self.assertEqual(library_doc.read_text(encoding="utf-8"), backup_doc.read_text(encoding="utf-8"))


class OnboardingTests(unittest.TestCase):
    def test_empty_state_points_to_adding_scan_roots(self) -> None:
        steps = build_onboarding_steps(
            OnboardingSnapshot(
                scan_root_count=0,
                protected_root_count=0,
                duplicate_group_count=0,
                keep_selected_count=0,
                organize_target_count=4,
                organize_preview_count=0,
                profile_count=0,
            )
        )

        by_key = {step.key: step for step in steps}
        self.assertEqual(by_key["scan_roots"].status, STATUS_NEXT)
        self.assertEqual(by_key["protected_roots"].status, STATUS_WAITING)
        self.assertIn("选择扫描目录", onboarding_summary(steps))
        self.assertIn("演示样例", onboarding_primary_guidance(steps))

    def test_selected_roots_recommend_protection_then_scan(self) -> None:
        steps = build_onboarding_steps(
            OnboardingSnapshot(
                scan_root_count=2,
                protected_root_count=0,
                duplicate_group_count=0,
                keep_selected_count=0,
                organize_target_count=4,
                organize_preview_count=0,
                profile_count=0,
            )
        )

        by_key = {step.key: step for step in steps}
        self.assertEqual(by_key["scan_roots"].status, STATUS_DONE)
        self.assertEqual(by_key["protected_roots"].status, STATUS_RECOMMENDED)
        self.assertEqual(by_key["scan"].status, STATUS_NEXT)
        self.assertIn("保护目录", onboarding_primary_guidance(steps))

    def test_ready_state_marks_all_required_steps_done(self) -> None:
        steps = build_onboarding_steps(
            OnboardingSnapshot(
                scan_root_count=2,
                protected_root_count=1,
                duplicate_group_count=12,
                keep_selected_count=12,
                organize_target_count=4,
                organize_preview_count=30,
                profile_count=1,
            )
        )

        self.assertTrue(all(step.status == STATUS_DONE for step in steps))
        self.assertIn("7/7", onboarding_summary(steps))
        self.assertIn("已经就绪", onboarding_primary_guidance(steps))


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


class FileRevealTests(unittest.TestCase):
    def test_windows_reveal_command_selects_file(self) -> None:
        command = build_reveal_command(Path("D:/Photos/a.jpg"), platform="win32")

        self.assertEqual(command[0], "explorer.exe")
        self.assertTrue(command[1].startswith("/select,"))
        self.assertIn("a.jpg", command[1])

    def test_linux_reveal_command_opens_parent_directory(self) -> None:
        command = build_reveal_command(Path("D:/Photos/a.jpg"), platform="linux")

        self.assertEqual(command[0], "xdg-open")
        self.assertTrue(command[1].endswith("Photos"))

    def test_macos_reveal_command_reveals_file(self) -> None:
        command = build_reveal_command(Path("D:/Photos/a.jpg"), platform="darwin")

        self.assertEqual(command[0:2], ["open", "-R"])
        self.assertTrue(command[2].endswith("a.jpg"))

    def test_operation_reveal_path_prefers_existing_move_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "a.jpg"
            target = root / "target" / "a.jpg"
            target.parent.mkdir()
            target.write_text("x", encoding="utf-8")
            operation = OperationRecord(1, "move", source, target, 1.0)

            current = operation_reveal_path(operation, "current")
            original = operation_reveal_path(operation, "original")

        self.assertEqual(current, target)
        self.assertEqual(original, source.parent)

    def test_operation_reveal_path_opens_original_directory_for_trash(self) -> None:
        operation = OperationRecord(1, "trash", Path("D:/Photos/a.jpg"), Path(""), 1.0)

        self.assertEqual(operation_reveal_path(operation, "current"), Path("D:/Photos"))


class OperationFilterTests(unittest.TestCase):
    def test_filter_operations_by_action_status_and_keyword(self) -> None:
        move = OperationRecord(1, "move", Path("D:/Camera/a.jpg"), Path("E:/Sorted/a.jpg"), 1.0)
        undone = OperationRecord(2, "move", Path("D:/Camera/b.jpg"), Path("E:/Sorted/b.jpg"), 2.0, undone_at=3.0)
        trash = OperationRecord(3, "trash", Path("D:/Cache/c.jpg"), Path(""), 4.0)
        operations = [move, undone, trash]

        self.assertEqual(filter_operations(operations, action_filter=ACTION_TRASH), [trash])
        self.assertEqual(filter_operations(operations, status_filter=STATUS_UNDONE), [undone])
        self.assertEqual(filter_operations(operations, action_filter=ACTION_MOVE, keyword="sorted"), [move, undone])

    def test_operation_filter_summary_mentions_visible_counts(self) -> None:
        move = OperationRecord(1, "move", Path("D:/Camera/a.jpg"), Path("E:/Sorted/a.jpg"), 1.0)
        trash = OperationRecord(2, "trash", Path("D:/Cache/c.jpg"), Path(""), 4.0)
        visible = [move]

        summary = summarize_operations([move, trash], visible)
        text = format_operation_filter_summary(summary)

        self.assertEqual(summary.total_count, 2)
        self.assertEqual(summary.visible_count, 1)
        self.assertEqual(summary.move_count, 1)
        self.assertIn("当前显示 1/2 条", text)
        self.assertIn("可撤销 1", text)

    def test_filtered_operations_can_be_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "filtered-operations.csv"
            move = OperationRecord(1, "move", Path("D:/Camera/a.jpg"), Path("E:/Sorted/a.jpg"), 1.0)
            trash = OperationRecord(2, "trash", Path("D:/Cache/c.jpg"), Path(""), 4.0)
            visible = filter_operations([move, trash], action_filter=ACTION_TRASH)

            export_operations_csv(visible, target)
            with target.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.reader(file))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1], "不可撤销")
        self.assertEqual(rows[1][2], "回收站")
        self.assertEqual(rows[1][3], "trash")
        self.assertIn("D:\\Cache\\c.jpg", rows[1][4])


class MainWindowStartupTests(unittest.TestCase):
    def test_startup_smoke_test_initializes_window_and_restores_environment(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from photo_dedupe.ui.main_window import startup_smoke_test

        with patch.dict(os.environ, {"LOCALAPPDATA": "D:/RealUserData"}):
            result = startup_smoke_test()
            restored = os.environ.get("LOCALAPPDATA")

        self.assertIn("启动烟测通过", result)
        self.assertIn("Photo Manage GPT", result)
        self.assertEqual(restored, "D:/RealUserData")

    def test_safe_mode_skips_restore_worker(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QScrollArea
        from photo_dedupe.ui.main_window import MainWindow, find_support_document

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LOCALAPPDATA": directory}):
            quickstart = Path(directory) / "QUICKSTART.md"
            quickstart.write_text("quickstart", encoding="utf-8")
            window = MainWindow(safe_mode=True)
            try:
                self.assertIsNone(window.restore_worker)
                self.assertIn("安全模式", window.status_label.text())
                self.assertIn("跳过历史结果加载", window.group_status_label.text())
                self.assertIn("添加扫描目录", window.group_status_label.text())
                tab_names = [window.main_tabs.tabText(index) for index in range(window.main_tabs.count())]
                self.assertIn("新手向导", tab_names)
                self.assertEqual(window.ai_check_button.text(), "检查 AI 配置")
                self.assertEqual(window.ai_key_input.echoMode(), QLineEdit.EchoMode.Password)
                self.assertFalse(window.ai_show_key_checkbox.isChecked())
                self.assertEqual(window.health_data_dir_button.text(), "打开数据目录")
                self.assertEqual(window.health_log_dir_button.text(), "打开日志目录")
                self.assertIsNotNone(window.findChild(QScrollArea, "SidebarScroll"))
                help_action = next((action for action in window.menuBar().actions() if action.text() == "帮助"), None)
                self.assertIsNotNone(help_action)
                help_menu = help_action.menu()
                self.assertIsNotNone(help_menu)
                help_actions = [action.text() for action in help_menu.actions()]
                self.assertIn("打开三分钟上手", help_actions)
                self.assertIn("打开健康诊断", help_actions)
                self.assertEqual(find_support_document("QUICKSTART.md", [Path(directory)]), quickstart)
                self.assertTrue(window.switch_to_tab("健康诊断"))
                self.assertEqual(window.main_tabs.tabText(window.main_tabs.currentIndex()), "健康诊断")
                window.refresh_onboarding()
                self.assertGreaterEqual(window.onboarding_table.rowCount(), 6)
                self.assertIn("选择扫描目录", window.onboarding_summary_label.text())
                self.assertIn("添加", window.onboarding_guidance_label.text())
                guide_buttons = {button.text() for button in window.findChildren(QPushButton)}
                self.assertIn("创建演示样例", guide_buttons)
                window.refresh_group_list()
                self.assertIn("清空筛选", window.group_status_label.text())
                self.assertIn("当前没有可预览的文件", window.detail_label.text())
                self.assertEqual(window._close_warning_reasons(), [])
                window.organize_preview_items = [
                    OrganizePreviewItem(Path("D:/source/photo.jpg"), "photo", "图片格式", Path("E:/target/photo.jpg"))
                ]
                close_reasons = window._close_warning_reasons()
                self.assertTrue(any("整理预览" in reason and "1 个文件" in reason for reason in close_reasons))
                window.organize_preview_items = []
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

    def test_file_preview_details_reads_image_size_and_size(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_path = root / "photo.png"
            Image.new("RGB", (40, 20), "green").save(image_path)
            record = FileRecord(path=image_path, size=0, modified_at=0)

            details = inspect_file_preview(record)

        self.assertTrue(details.exists)
        self.assertEqual(details.category, "photo")
        self.assertEqual((details.width, details.height), (40, 20))
        self.assertGreater(details.size, 0)
        self.assertAlmostEqual(details.megapixels or 0, 0.0008)

    def test_file_preview_details_uses_cached_values_when_missing(self) -> None:
        record = FileRecord(
            path=Path("D:/missing/photo.jpg"),
            size=123,
            modified_at=456.0,
            width=10,
            height=20,
        )

        details = inspect_file_preview(record)

        self.assertFalse(details.exists)
        self.assertEqual(details.category, "photo")
        self.assertEqual(details.size, 123)
        self.assertEqual(details.modified_at, 456.0)
        self.assertEqual((details.width, details.height), (10, 20))

    def test_image_compare_items_filter_images_and_sort_recommended_first(self) -> None:
        protected = Path("D:/library")
        safe = FileRecord(path=protected / "photo.jpg", size=10, modified_at=1.0, width=100, height=100)
        larger = FileRecord(path=Path("E:/backup/photo.jpg"), size=100, modified_at=2.0, width=300, height=300)
        note = FileRecord(path=Path("E:/backup/note.txt"), size=1000, modified_at=3.0)

        items = build_image_compare_items([larger, note, safe], {protected}, "保留最大文件")

        self.assertEqual([item.record.path for item in items], [safe.path, larger.path])
        self.assertTrue(items[0].recommended)
        self.assertTrue(items[0].protected)
        self.assertEqual(items[0].reason, "建议保留：保护目录内文件")

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

    def test_undo_move_operations_continues_after_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_ok = root / "source-ok.txt"
            target_ok = root / "moved" / "source-ok.txt"
            source_missing = root / "source-missing.txt"
            target_missing = root / "moved" / "missing.txt"
            target_ok.parent.mkdir()
            target_ok.write_text("content", encoding="utf-8")
            database = AppDatabase(root / "test.db")
            try:
                ok_id = database.log_move(source_ok, target_ok)
                missing_id = database.log_move(source_missing, target_missing)
                trash_id = database.log_trash(root / "trash.txt")
                operations = [
                    database.get_operation(ok_id),
                    database.get_operation(missing_id),
                    database.get_operation(trash_id),
                ]
                result = undo_move_operations([operation for operation in operations if operation is not None], database)
                refreshed = database.get_operation(ok_id)
            finally:
                database.close()

            self.assertEqual(result.completed_count, 1)
            self.assertEqual(result.failed_count, 1)
            self.assertEqual(result.skipped_count, 1)
            self.assertTrue(source_ok.exists())
            self.assertFalse(target_ok.exists())
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

    def test_summarize_organize_preview_counts_size_and_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            source_dir.mkdir()
            photo = source_dir / "photo.jpg"
            document = source_dir / "notes.pdf"
            missing = source_dir / "missing.mp4"
            photo.write_bytes(b"12345")
            document.write_bytes(b"abc")

            preview = [
                OrganizePreviewItem(photo, "photo", "图片格式", root / "photos" / photo.name),
                OrganizePreviewItem(document, "document", "文档格式", root / "docs" / document.name),
                OrganizePreviewItem(missing, "video", "视频格式", root / "videos" / missing.name),
            ]

            summary = summarize_organize_preview(preview)

        self.assertEqual(summary.file_count, 3)
        self.assertEqual(summary.total_bytes, 8)
        self.assertEqual(summary.category_counts["photo"], 1)
        self.assertEqual(summary.category_counts["document"], 1)
        self.assertEqual(summary.category_counts["video"], 1)
        self.assertEqual(summary.missing_count, 1)
        self.assertEqual(len(summary.target_directories), 3)

    def test_describe_organize_preview_item_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            target_dir = root / "target"
            source_dir.mkdir()
            target_dir.mkdir()
            ready_source = source_dir / "ready.jpg"
            conflict_source = source_dir / "conflict.jpg"
            conflict_target = target_dir / "conflict.jpg"
            already_target = target_dir / "already.jpg"
            missing_source = source_dir / "missing.jpg"
            ready_source.write_text("ready", encoding="utf-8")
            conflict_source.write_text("source", encoding="utf-8")
            conflict_target.write_text("target", encoding="utf-8")
            already_target.write_text("already", encoding="utf-8")

            ready = describe_organize_preview_item(
                OrganizePreviewItem(ready_source, "photo", "图片格式", target_dir / "ready.jpg")
            )
            conflict = describe_organize_preview_item(
                OrganizePreviewItem(conflict_source, "photo", "图片格式", conflict_target)
            )
            already = describe_organize_preview_item(
                OrganizePreviewItem(already_target, "photo", "图片格式", already_target)
            )
            missing = describe_organize_preview_item(
                OrganizePreviewItem(missing_source, "photo", "图片格式", target_dir / "missing.jpg")
            )

        self.assertEqual(ready.state, "ready")
        self.assertTrue(ready.will_move)
        self.assertEqual(conflict.state, "target_conflict")
        self.assertTrue(conflict.will_move)
        self.assertEqual(already.state, "already_in_target")
        self.assertFalse(already.will_move)
        self.assertEqual(missing.state, "missing")
        self.assertFalse(missing.will_move)

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

    def test_execute_organize_preview_skips_file_already_at_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target_dir = root / "photos"
            target_dir.mkdir()
            source = target_dir / "photo.jpg"
            source.write_text("x", encoding="utf-8")
            preview = [OrganizePreviewItem(source, "photo", "图片格式", source)]

            moved = execute_organize_preview(preview)
            source_exists = source.exists()
            renamed_exists = (target_dir / "photo (1).jpg").exists()

        self.assertEqual(moved, [])
        self.assertTrue(source_exists)
        self.assertFalse(renamed_exists)

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

    def test_duplicate_report_html_contains_summary_and_escaped_paths(self) -> None:
        group = DuplicateGroup(
            kind="exact",
            title="raw <group>",
            records=[
                FileRecord(path=Path("D:/photos/a&b.jpg"), size=10, modified_at=1.0, keep_selected=True),
                FileRecord(path=Path("D:/backup/a&b.jpg"), size=10, modified_at=2.0),
            ],
        )

        html = build_duplicate_report_html([group], "报告 <test>")

        self.assertIn("报告 &lt;test&gt;", html)
        self.assertIn("重复分组", html)
        self.assertIn("完全重复", html)
        self.assertIn("候选处理", html)
        self.assertIn("a&amp;b.jpg", html)

    def test_export_duplicate_report_html_writes_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.html"
            group = DuplicateGroup(
                kind="similar",
                title="photo group",
                records=[
                    FileRecord(path=Path("D:/photos/a.jpg"), size=1024, modified_at=1.0, width=100, height=100),
                    FileRecord(path=Path("D:/photos/b.jpg"), size=2048, modified_at=2.0, width=120, height=120),
                ],
                score=0.85,
            )

            export_duplicate_report_html([group], target)
            content = target.read_text(encoding="utf-8")

        self.assertIn("相似图片", content)
        self.assertIn("85%", content)
        self.assertIn("3.0 KB", content)

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

    def test_scan_sessions_can_be_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "scan-sessions.csv"
            sessions = [
                ScanSessionSummaryRecord(
                    id=7,
                    created_at=123.0,
                    root_paths=[Path("D:/Photos"), Path("E:/Backup")],
                    group_count=3,
                    file_count=9,
                    duplicate_bytes=1200,
                    reclaimable_bytes=800,
                )
            ]

            export_scan_sessions_csv(sessions, target)
            with target.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.reader(file))

        self.assertEqual(
            rows[0],
            [
                "id",
                "created_time",
                "created_at",
                "root_paths",
                "group_count",
                "file_count",
                "duplicate_size",
                "duplicate_bytes",
                "reclaimable_size",
                "reclaimable_bytes",
            ],
        )
        self.assertEqual(rows[1][0], "7")
        self.assertTrue(rows[1][1])
        self.assertIn("D:\\Photos", rows[1][3])
        self.assertIn("E:\\Backup", rows[1][3])
        self.assertEqual(rows[1][6], "1.2 KB")
        self.assertEqual(rows[1][8], "800 B")

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

    def test_keep_rule_prefers_library_like_directory(self) -> None:
        root = Path("D:/demo")
        library = FileRecord(path=root / "照片资料库" / "photo.jpg", size=10, modified_at=1.0, width=100, height=100)
        download = FileRecord(path=root / "Downloads" / "photo.jpg", size=1000, modified_at=10.0, width=200, height=200)

        result = choose_keep_records([library, download], "保留资料库目录")

        self.assertEqual([record.path for record in result.keep_records], [library.path])

    def test_keep_rule_avoids_cache_like_directory(self) -> None:
        root = Path("D:/demo")
        clean = FileRecord(path=root / "album" / "photo.jpg", size=10, modified_at=1.0, width=100, height=100)
        cached = FileRecord(path=root / "wechat_cache" / "photo.jpg", size=1000, modified_at=10.0, width=200, height=200)

        result = choose_keep_records([clean, cached], "保留非缓存目录")

        self.assertEqual([record.path for record in result.keep_records], [clean.path])

    def test_keep_rule_prefers_drive_with_most_duplicates(self) -> None:
        c_old = FileRecord(path=Path("C:/photos/a.jpg"), size=10, modified_at=1.0, width=100, height=100)
        c_new = FileRecord(path=Path("C:/photos/b.jpg"), size=20, modified_at=2.0, width=120, height=120)
        d_large = FileRecord(path=Path("D:/backup/a.jpg"), size=1000, modified_at=10.0, width=300, height=300)

        result = choose_keep_records([c_old, c_new, d_large], "保留同磁盘最多项")

        self.assertEqual([record.path for record in result.keep_records], [c_new.path])

    def test_keep_rule_prefers_path_keyword(self) -> None:
        album = FileRecord(path=Path("D:/album/photo.jpg"), size=10, modified_at=1.0)
        backup = FileRecord(path=Path("D:/backup/photo.jpg"), size=100, modified_at=2.0)

        result = choose_keep_records([album, backup], "保留路径包含关键词", keyword="album")

        self.assertEqual([record.path for record in result.keep_records], [album.path])

    def test_keep_rule_prefers_name_keyword(self) -> None:
        original = FileRecord(path=Path("D:/photos/photo-original.jpg"), size=10, modified_at=1.0)
        copy = FileRecord(path=Path("D:/photos/photo-copy.jpg"), size=100, modified_at=2.0)

        result = choose_keep_records([original, copy], "保留文件名包含关键词", keyword="original")

        self.assertEqual([record.path for record in result.keep_records], [original.path])

    def test_keep_rule_prefers_path_without_keyword(self) -> None:
        clean = FileRecord(path=Path("D:/album/photo.jpg"), size=10, modified_at=1.0)
        cache = FileRecord(path=Path("D:/cache/photo.jpg"), size=100, modified_at=2.0)

        result = choose_keep_records([clean, cache], "保留路径不含关键词", keyword="cache")

        self.assertEqual([record.path for record in result.keep_records], [clean.path])

    def test_describe_keep_reasons_marks_suggested_and_processable_files(self) -> None:
        small = FileRecord(path=Path("D:/album/photo-small.jpg"), size=10, modified_at=1.0)
        large = FileRecord(path=Path("D:/album/photo-large.jpg"), size=100, modified_at=2.0)

        reasons = describe_keep_reasons([small, large], "保留最大文件")

        self.assertEqual(reasons[large.path], "建议保留：文件最大")
        self.assertEqual(reasons[small.path], "未选保留：可处理")

    def test_describe_keep_reasons_marks_protected_files(self) -> None:
        protected = Path("D:/library")
        safe = FileRecord(path=protected / "photo.jpg", size=10, modified_at=1.0)
        duplicate = FileRecord(path=Path("D:/temp/photo.jpg"), size=100, modified_at=2.0)

        reasons = describe_keep_reasons([safe, duplicate], "保留最大文件", {protected})

        self.assertEqual(reasons[safe.path], "建议保留：保护目录内文件")
        self.assertEqual(reasons[duplicate.path], "未选保留：可处理")

    def test_duplicate_group_summary_includes_space_locations_and_recommendation(self) -> None:
        protected = Path("D:/library")
        safe = FileRecord(path=protected / "photo.jpg", size=10, modified_at=1.0, width=100, height=100)
        copy = FileRecord(path=Path("E:/backup/photo-copy.jpg"), size=100, modified_at=2.0, width=200, height=200)
        group = DuplicateGroup(kind="exact", title="photo", records=[safe, copy])

        summary = summarize_duplicate_group(group, {protected}, "保留最大文件")

        self.assertEqual(summary.kind_label, "完全重复")
        self.assertEqual(summary.category_label, "照片")
        self.assertEqual(summary.file_count, 2)
        self.assertEqual(summary.total_bytes, 110)
        self.assertEqual(summary.reclaimable_bytes, 10)
        self.assertEqual(summary.directory_count, 2)
        self.assertEqual(summary.disk_count, 2)
        self.assertEqual(summary.protected_count, 1)
        self.assertEqual(summary.recommended_names, ["photo.jpg"])
        self.assertEqual(summary.recommended_reason, "保护目录内文件")

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

    def test_processing_risk_summary_is_low_for_small_move(self) -> None:
        root = Path("D:/demo")
        keep = FileRecord(path=root / "keep" / "photo.jpg", size=20, modified_at=2.0, keep_selected=True)
        remove = FileRecord(path=root / "temp" / "photo-copy.jpg", size=30, modified_at=3.0, keep_selected=False)
        group = DuplicateGroup(kind="exact", title="group", records=[keep, remove])
        plan = build_processing_plan([group], set())

        summary = build_processing_risk_summary(plan, "移动到目标目录", Path("D:/duplicates"), destination_free_bytes=1024)
        text = format_processing_confirmation(summary)

        self.assertEqual(summary.risk_level, RISK_LOW)
        self.assertIn("风险等级：低", text)
        self.assertIn("目标目录", text)
        self.assertIn("目标磁盘可用空间：1.0 KB", text)

    def test_processing_risk_summary_warns_when_destination_space_is_low(self) -> None:
        root = Path("D:/demo")
        keep = FileRecord(path=root / "keep" / "photo.jpg", size=20, modified_at=2.0, keep_selected=True)
        remove = FileRecord(path=root / "temp" / "photo-copy.jpg", size=2048, modified_at=3.0, keep_selected=False)
        group = DuplicateGroup(kind="exact", title="group", records=[keep, remove])
        plan = build_processing_plan([group], set())

        summary = build_processing_risk_summary(plan, "移动到目标目录", Path("D:/duplicates"), destination_free_bytes=1024)
        text = format_processing_confirmation(summary)

        self.assertEqual(summary.risk_level, RISK_HIGH)
        self.assertTrue(any("可用空间不足" in warning for warning in summary.warnings))
        self.assertIn("预计移动后剩余：不足 1.0 KB", text)

    def test_available_destination_free_bytes_uses_existing_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "missing" / "target"

            free_bytes = available_destination_free_bytes(destination)

        self.assertIsNotNone(free_bytes)
        self.assertGreater(free_bytes or 0, 0)

    def test_processing_risk_summary_warns_about_protected_skips(self) -> None:
        root = Path("D:/demo")
        protected = root / "library"
        safe = FileRecord(path=protected / "photo.jpg", size=10, modified_at=1.0, keep_selected=False)
        keep = FileRecord(path=root / "keep" / "photo.jpg", size=20, modified_at=2.0, keep_selected=True)
        remove = FileRecord(path=root / "temp" / "photo-copy.jpg", size=30, modified_at=3.0, keep_selected=False)
        group = DuplicateGroup(kind="exact", title="group", records=[safe, keep, remove])
        plan = build_processing_plan([group], {protected})

        summary = build_processing_risk_summary(plan, "移动到目标目录", Path("D:/duplicates"))

        self.assertEqual(summary.risk_level, RISK_MEDIUM)
        self.assertTrue(any("保护目录" in warning for warning in summary.warnings))

    def test_processing_risk_summary_marks_trash_as_high_risk(self) -> None:
        root = Path("D:/demo")
        keep = FileRecord(path=root / "keep" / "photo.jpg", size=20, modified_at=2.0, keep_selected=True)
        remove = FileRecord(path=root / "temp" / "photo-copy.jpg", size=30, modified_at=3.0, keep_selected=False)
        group = DuplicateGroup(kind="exact", title="group", records=[keep, remove])
        plan = build_processing_plan([group], set())

        summary = build_processing_risk_summary(plan, "移动到回收站", Path("D:/duplicates"))
        text = format_processing_confirmation(summary)

        self.assertEqual(summary.risk_level, RISK_HIGH)
        self.assertIsNone(summary.destination)
        self.assertIn("系统回收站", text)

    def test_processing_result_report_summarizes_completed_and_skipped_files(self) -> None:
        root = Path("D:/demo")
        protected = root / "library"
        safe = FileRecord(path=protected / "photo.jpg", size=10, modified_at=1.0, keep_selected=False)
        keep = FileRecord(path=root / "keep" / "photo.jpg", size=20, modified_at=2.0, keep_selected=True)
        moved = FileRecord(path=root / "temp" / "photo-copy.jpg", size=30, modified_at=3.0, keep_selected=False)
        skipped = FileRecord(path=root / "temp" / "missing.jpg", size=40, modified_at=4.0, keep_selected=False)
        group = DuplicateGroup(kind="exact", title="group", records=[safe, keep, moved, skipped])
        plan = build_processing_plan([group], {protected})

        report = build_processing_result_report(
            plan,
            "移动到目标目录",
            {moved.path},
            [Path("E:/duplicates/photo-copy.jpg")],
            moved_pairs=[(moved.path, Path("E:/duplicates/photo-copy.jpg"))],
        )
        text = format_operation_result_report(report)
        detailed_text = format_operation_result_report(report, include_items=True, item_limit=None, generated_at=0)

        self.assertEqual(report.planned_count, 2)
        self.assertEqual(report.completed_count, 1)
        self.assertEqual(report.skipped_count, 1)
        self.assertEqual(report.protected_skip_count, 1)
        self.assertEqual(report.completed_bytes, 30)
        self.assertEqual(len(report.items), 2)
        self.assertEqual(report.items[0].status, "completed")
        self.assertEqual(report.items[0].target_path, Path("E:/duplicates/photo-copy.jpg"))
        self.assertEqual(report.items[1].status, "skipped")
        self.assertIn("操作记录", text)
        self.assertIn("E:\\duplicates", text)
        self.assertIn("文件明细", detailed_text)
        self.assertIn("D:\\demo\\temp\\photo-copy.jpg", detailed_text)
        self.assertIn("已完成", detailed_text)

    def test_processing_result_report_uses_recycle_bin_restore_hint(self) -> None:
        root = Path("D:/demo")
        keep = FileRecord(path=root / "keep" / "photo.jpg", size=20, modified_at=2.0, keep_selected=True)
        trashed = FileRecord(path=root / "temp" / "photo-copy.jpg", size=30, modified_at=3.0, keep_selected=False)
        group = DuplicateGroup(kind="exact", title="group", records=[keep, trashed])
        plan = build_processing_plan([group], set())

        report = build_processing_result_report(plan, "移动到回收站", {trashed.path})
        text = format_operation_result_report(report)

        self.assertEqual(report.completed_count, 1)
        self.assertIn("Windows 回收站", report.restore_hint)
        self.assertIn("Windows 回收站", text)

    def test_operation_result_report_can_be_exported_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path("D:/demo")
            keep = FileRecord(path=root / "keep" / "photo.jpg", size=20, modified_at=2.0, keep_selected=True)
            moved = FileRecord(path=root / "temp" / "photo-copy.jpg", size=30, modified_at=3.0, keep_selected=False)
            group = DuplicateGroup(kind="exact", title="group", records=[keep, moved])
            plan = build_processing_plan([group], set())
            report = build_processing_result_report(
                plan,
                "移动到目标目录",
                {moved.path},
                moved_pairs=[(moved.path, Path("E:/duplicates/photo-copy.jpg"))],
            )
            report_dir = Path(directory) / "reports"
            manual_target = report_dir / "manual.txt"

            export_operation_result_report(report, manual_target, generated_at=0)
            first_saved = save_operation_result_report(report, report_dir, generated_at=0)
            second_saved = save_operation_result_report(report, report_dir, generated_at=0)

            manual_text = manual_target.read_text(encoding="utf-8")
            saved_text = first_saved.read_text(encoding="utf-8")
            first_exists = first_saved.exists()
            second_exists = second_saved.exists()
            first_name = first_saved.name
            saved_paths_differ = first_saved != second_saved

        self.assertTrue(first_exists)
        self.assertTrue(second_exists)
        self.assertTrue(saved_paths_differ)
        self.assertIn("-dedupe-operation-report", first_name)
        self.assertIn("生成时间：", manual_text)
        self.assertIn("文件明细", saved_text)
        self.assertIn("E:\\duplicates\\photo-copy.jpg", saved_text)

    def test_organize_result_report_summarizes_move_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "photo.jpg"
            target = root / "photos" / "photo.jpg"
            target.parent.mkdir()
            target.write_bytes(b"photo")
            preview = [OrganizePreviewItem(source, "photo", "图片格式", target)]
            summary = OrganizePreviewSummary(
                file_count=2,
                total_bytes=15,
                category_counts={"photo": 2},
                source_directories=[source.parent],
                target_directories=[target.parent],
                missing_count=1,
                already_in_target_count=0,
            )

            report = build_organize_result_report(preview, [(source, target)], summary)
            text = format_operation_result_report(report)

        self.assertEqual(report.planned_count, 2)
        self.assertEqual(report.completed_count, 1)
        self.assertEqual(report.skipped_count, 1)
        self.assertEqual(report.completed_bytes, 5)
        self.assertIn("整理执行报告", text)
        self.assertIn("操作记录", text)
        self.assertEqual(report.items[0].target_path, target)
        self.assertIn("普通移动", report.restore_hint)

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
        self.assertEqual(rows[0][:4], ["id", "status", "action_label", "action"])
        self.assertEqual(rows[1][1], "不可撤销")
        self.assertEqual(rows[1][2], "回收站")
        self.assertEqual(rows[1][3], "trash")

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

    def test_ai_config_endpoint_accepts_base_url_or_full_endpoint(self) -> None:
        base_config = AiConfig("https://api.example.com/v1", "key", "model")
        endpoint_config = AiConfig("https://api.example.com/v1/chat/completions", "key", "model")

        self.assertEqual(base_config.endpoint, "https://api.example.com/v1/chat/completions")
        self.assertEqual(endpoint_config.endpoint, "https://api.example.com/v1/chat/completions")

    def test_ai_config_validation_reports_missing_fields(self) -> None:
        validation = validate_ai_config("", "", "")
        text = format_ai_config_validation(validation)

        self.assertFalse(validation.ok)
        self.assertTrue(any("API 地址" in error for error in validation.errors))
        self.assertIn("需要补充配置", text)

    def test_ai_config_validation_warns_but_allows_common_variants(self) -> None:
        http_validation = validate_ai_config("http://localhost:8000/v1", "vision", "12345678")
        full_endpoint_validation = validate_ai_config(
            "https://api.example.com/v1/chat/completions",
            "vision",
            "12345678",
        )

        self.assertTrue(http_validation.ok)
        self.assertTrue(any("HTTP" in warning for warning in http_validation.warnings))
        self.assertTrue(full_endpoint_validation.ok)
        self.assertEqual(full_endpoint_validation.endpoint, "https://api.example.com/v1/chat/completions")
        self.assertTrue(any("chat/completions" in warning for warning in full_endpoint_validation.warnings))

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
