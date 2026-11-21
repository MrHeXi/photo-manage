from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from photo_dedupe.core.build_info import BUILD_INFO_FILENAME, BuildInfo, get_build_info, write_build_info
from photo_dedupe.core.diagnostics import build_support_diagnostic_report
from photo_dedupe.core.release_check import (
    STATUS_ERROR, STATUS_OK, _check_build_info, _check_release_zip, _latest_release_directory,
)
from photo_dedupe.core.release_manifest import MANIFEST_FILENAME, verify_release_manifest, write_release_manifest


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40
STAMP = "2026-11-21T08:30:01.123456+00:00"


class BuildIdentityTests(unittest.TestCase):
    def test_frozen_identity_uses_embedded_metadata_not_local_git(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_build_info(root / BUILD_INFO_FILENAME, version="1.2.3", commit=COMMIT, built_at_utc=STAMP)
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(root), create=True), patch(
                "photo_dedupe.core.build_info._git_commit", side_effect=AssertionError("Frozen builds must not invoke Git")
            ):
                info = get_build_info("0.0.0")
            self.assertEqual(info.commit, COMMIT)
            self.assertEqual(info.version, "1.2.3")
            self.assertEqual(info.built_at_utc, STAMP)
            self.assertRegex(info.build_id, r"^v1\.2\.3-aaaaaaaaaaaa-20261121T083001123456Z$")
            self.assertIn("aaaaaaaaaaaa", info.label)

    def test_missing_metadata_does_not_invent_a_build_date(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("photo_dedupe.core.build_info._git_commit", return_value=COMMIT):
            info = get_build_info("0.1.0")
        self.assertEqual(info.built_at_utc, "")
        self.assertIn("源码运行", info.label)

    def test_sidecar_and_embedded_metadata_must_agree(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_build_info(root / BUILD_INFO_FILENAME, version="0.1.0", commit=COMMIT, built_at_utc=STAMP)
            write_build_info(root / "_internal" / BUILD_INFO_FILENAME, version="0.1.0", commit="b" * 40, built_at_utc=STAMP)
            items = []
            _check_build_info(root, "0.1.0", items)
            self.assertEqual(items[-1].status, STATUS_ERROR)
            shutil.copy2(root / BUILD_INFO_FILENAME, root / "_internal" / BUILD_INFO_FILENAME)
            _check_build_info(root, "0.1.0", items)
            self.assertEqual(items[-1].status, STATUS_OK)
            write_release_manifest(root, "0.1.0")
            manifest_path = root / MANIFEST_FILENAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["build"]["commit"] = "c" * 40
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            _check_build_info(root, "0.1.0", items)
            self.assertEqual(items[-1].status, STATUS_ERROR)

    def test_support_report_exposes_identity(self) -> None:
        identity = BuildInfo("0.1.0", COMMIT, STAMP, "frozen")
        with tempfile.TemporaryDirectory() as folder, patch("photo_dedupe.core.diagnostics.get_build_info", return_value=identity):
            root = Path(folder)
            report = build_support_diagnostic_report(
                app_version="0.1.0", database_path=root / "db", log_path=root / "log",
                ffmpeg_available=False, history_state="完成",
            )
            self.assertIn(COMMIT[:12], report)
            self.assertIn(STAMP, report)


class ReleaseIntegrityTests(unittest.TestCase):
    def make_release(self, root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        for filename in (
            "PhotoManageGPT.exe", "README.md", "QUICKSTART.md", "CHANGELOG.md",
            "PhotoManageGPT-safe-mode.cmd", "install-shortcut.ps1", "remove-shortcut.ps1",
        ):
            (root / filename).write_bytes(b"verified content")
        write_build_info(root / BUILD_INFO_FILENAME, version="0.1.0", commit=COMMIT, built_at_utc=STAMP)
        write_release_manifest(root, "0.1.0")
        return root

    def test_manifest_catches_changed_same_size_missing_and_unlisted_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = self.make_release(Path(folder))
            self.assertEqual(verify_release_manifest(root), [])
            (root / "PhotoManageGPT.exe").write_bytes(b"x" * len(b"verified content"))
            (root / "README.md").unlink()
            (root / "unexpected.dll").write_bytes(b"new")
            problems = "\n".join(verify_release_manifest(root))
            self.assertIn("PhotoManageGPT.exe", problems)
            self.assertIn("README.md", problems)
            self.assertIn("unexpected.dll", problems)

    def test_manifest_rejects_traversal_and_empty_or_corrupt_lists(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root / MANIFEST_FILENAME
            manifest.write_text(json.dumps({"files": [{"path": "../outside", "size": 1, "sha256": "a"}]}), encoding="utf-8")
            self.assertIn("超出发布目录", "\n".join(verify_release_manifest(root)))
            manifest.write_text('{"files": []}', encoding="utf-8")
            self.assertTrue(verify_release_manifest(root))
            manifest.write_text("{broken", encoding="utf-8")
            self.assertTrue(verify_release_manifest(root))

    def test_zip_content_is_checked_against_manifest_even_with_valid_crc(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "photo_dedupe").mkdir()
            (project / "photo_dedupe" / "__init__.py").write_text('__version__ = "0.1.0"', encoding="utf-8")
            release = self.make_release(project / "release" / "candidate")
            archive_path = project / "candidate.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                for path in release.iterdir():
                    archive.writestr(path.name, b"tampered binary" if path.suffix == ".exe" else path.read_bytes())
            items = []
            _check_release_zip(project, items, archive_path)
            self.assertEqual(items[-1].status, STATUS_ERROR)
            self.assertIn("校验失败", items[-1].detail)

    def test_latest_release_selection_supports_build_identity(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "photo_dedupe").mkdir()
            (root / "photo_dedupe" / "__init__.py").write_text('__version__ = "0.1.0"', encoding="utf-8")
            self.make_release(root / "release" / "PhotoManageGPT-v0.1.0-win64")
            current = self.make_release(root / "release" / f"PhotoManageGPT-{BuildInfo('0.1.0', COMMIT, STAMP).build_id}-win64")
            self.assertEqual(_latest_release_directory(root), current)


@unittest.skipUnless(os.name == "nt", "PowerShell build tooling is Windows-specific")
class PowerShellBuildSafetyTests(unittest.TestCase):
    def run_ps(self, command: str, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, timeout=30, **kwargs,
        )

    def test_scripts_parse_and_native_wrapper_stops_on_failure(self) -> None:
        for filename, function in (("build.ps1", "Invoke-CheckedNative"), ("package.ps1", "Invoke-PackageNative")):
            source = str(ROOT / filename).replace("'", "''")
            command = (
                "$ErrorActionPreference='Stop'; $tokens=$null; $parseErrors=$null; "
                f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{source}',[ref]$tokens,[ref]$parseErrors); "
                "if($parseErrors.Count){throw ($parseErrors | Out-String)}; "
                f"$fn=$ast.Find({{param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq '{function}'}},$true); "
                "Invoke-Expression $fn.Extent.Text; "
                f"{function} 'cmd.exe' @('/c','exit','23'); Write-Output 'SHOULD_NOT_RUN'"
            )
            result = self.run_ps(command)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("SHOULD_NOT_RUN", result.stdout)
            self.assertIn("23", result.stderr)
            self.assertNotIn("ParserError", result.stderr)

    def test_failed_git_command_does_not_replace_existing_build(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copy2(ROOT / "build.ps1", root / "build.ps1")
            old = root / "dist" / "PhotoManageGPT"
            old.mkdir(parents=True)
            exe = old / "PhotoManageGPT.exe"
            exe.write_bytes(b"known-good old executable")
            python = root / ".venv" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"not invoked with SkipInstall before Git check")
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            (fake_bin / "git.cmd").write_text("@echo off\nexit /b 17\n", encoding="ascii")
            env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"])
            command = "& '" + str(root / "build.ps1").replace("'", "''") + "' -SkipInstall"
            result = self.run_ps(command, env=env)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual(exe.read_bytes(), b"known-good old executable")
            self.assertFalse((root / "dist" / "backups").exists())
            self.assertIn("Cannot resolve", result.stderr)

    def test_pyinstaller_path_isolation_excludes_external_dlls_and_restores_environment(self) -> None:
        script = str(ROOT / "build.ps1").replace("'", "''")
        python = sys.executable.replace("'", "''")
        with tempfile.TemporaryDirectory() as folder:
            contaminated = str(Path(folder) / "external-tools" / "Library" / "bin")
            env = dict(os.environ, PATH=contaminated + os.pathsep + os.environ["PATH"])
            definitions = (
                "$ErrorActionPreference='Stop'; $tokens=$null; $parseErrors=$null; "
                f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{script}',[ref]$tokens,[ref]$parseErrors); "
                "$functions=$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] "
                "-and $n.Name -in @('Invoke-CheckedNative','Invoke-IsolatedPyInstaller')},$true); "
                "foreach($fn in $functions){Invoke-Expression $fn.Extent.Text}; $originalPath=$env:PATH; "
            )
            success = self.run_ps(
                definitions
                + f"Invoke-IsolatedPyInstaller '{python}' @('-c','import json,os; print(json.dumps(os.get_exec_path()))'); "
                + "if($env:PATH -cne $originalPath){throw 'PATH restoration failed'}; 'RESTORED'",
                env=env,
            )
            self.assertEqual(success.returncode, 0, success.stderr)
            lines = success.stdout.strip().splitlines()
            child_paths = json.loads(lines[0])
            self.assertNotIn(contaminated, child_paths)
            expected = {
                os.path.normcase(str(Path(sys.executable).parent)),
                os.path.normcase(sys.base_prefix),
                os.path.normcase(str(Path(os.environ["SystemRoot"]) / "System32")),
                os.path.normcase(os.environ["SystemRoot"]),
            }
            self.assertEqual({os.path.normcase(path) for path in child_paths}, expected)
            self.assertEqual(lines[-1], "RESTORED")

            failed = self.run_ps(
                definitions
                + f"try {{ Invoke-IsolatedPyInstaller '{python}' @('-c','raise SystemExit(29)'); throw 'Expected native failure' }} "
                + "catch {if($_.Exception.Message -notmatch 'exit code 29'){throw}; 'FAILED_AS_EXPECTED'}; "
                + "if($env:PATH -cne $originalPath){throw 'PATH restoration failed'}; 'RESTORED'",
                env=env,
            )
            self.assertEqual(failed.returncode, 0, failed.stderr)
            self.assertIn("FAILED_AS_EXPECTED", failed.stdout)
            self.assertIn("RESTORED", failed.stdout)

    def test_promotion_guard_rechecks_real_git_head_and_source_changes(self) -> None:
        def git(root: Path, *args: str) -> str:
            result = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, check=True, timeout=10,
            )
            return result.stdout.strip()

        for filename, function in (
            ("build.ps1", "Assert-BuildSourceState"),
            ("package.ps1", "Assert-PackageSourceState"),
        ):
            with self.subTest(script=filename), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source_dir = root / "photo_dedupe"
                source_dir.mkdir()
                tracked = source_dir / "main.py"
                tracked.write_text("print('original')\n", encoding="utf-8")
                git(root, "init", "--quiet")
                git(root, "-c", "user.name=Build Test", "-c", "user.email=build@example.invalid", "add", ".")
                git(root, "-c", "user.name=Build Test", "-c", "user.email=build@example.invalid", "commit", "--quiet", "-m", "initial")
                expected_commit = git(root, "rev-parse", "HEAD")
                source = str(ROOT / filename).replace("'", "''")
                command = (
                    "$ErrorActionPreference='Stop'; $tokens=$null; $parseErrors=$null; "
                    f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{source}',[ref]$tokens,[ref]$parseErrors); "
                    f"$fn=$ast.Find({{param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq '{function}'}},$true); "
                    f"Invoke-Expression $fn.Extent.Text; {function} '{expected_commit}'; 'READY_TO_PROMOTE'"
                )
                # A screenshot outside application sources does not contaminate the build.
                (root / "user-screenshot.png").write_bytes(b"preserve me")
                clean = self.run_ps(command, cwd=root)
                self.assertEqual(clean.returncode, 0, clean.stderr)
                self.assertIn("READY_TO_PROMOTE", clean.stdout)

                tracked.write_text("print('changed while compiling')\n", encoding="utf-8")
                dirty = self.run_ps(command, cwd=root)
                self.assertNotEqual(dirty.returncode, 0)
                self.assertNotIn("READY_TO_PROMOTE", dirty.stdout)
                self.assertIn("Tracked source changed", dirty.stderr)

                tracked.write_text("print('original')\n", encoding="utf-8")
                new_source = source_dir / "added.py"
                new_source.write_text("print('new module')\n", encoding="utf-8")
                untracked = self.run_ps(command, cwd=root)
                self.assertNotEqual(untracked.returncode, 0)
                self.assertNotIn("READY_TO_PROMOTE", untracked.stdout)
                self.assertIn("uncommitted", untracked.stderr)

                git(root, "add", "photo_dedupe")
                git(root, "-c", "user.name=Build Test", "-c", "user.email=build@example.invalid", "commit", "--quiet", "-m", "new head")
                changed_head = self.run_ps(command, cwd=root)
                self.assertNotEqual(changed_head.returncode, 0)
                self.assertNotIn("READY_TO_PROMOTE", changed_head.stdout)
                self.assertIn("Git HEAD", changed_head.stderr)
                self.assertEqual((root / "user-screenshot.png").read_bytes(), b"preserve me")

    def test_smoke_wait_is_bounded_and_terminates_only_started_process(self) -> None:
        for filename, function in (
            ("build.ps1", "Invoke-BuildSmokeTest"), ("package.ps1", "Invoke-PackageSmokeTest"),
        ):
            with self.subTest(script=filename), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                child_script = root / "slow_smoke.py"
                child_script.write_text("import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(30)\n", encoding="utf-8")
                script = str(ROOT / filename).replace("'", "''")
                python = sys.executable.replace("'", "''")
                location = str(root).replace("'", "''")
                argument = ('"' + str(child_script) + '"').replace("'", "''")
                command = (
                    "$ErrorActionPreference='Stop'; $tokens=$null; $parseErrors=$null; "
                    f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{script}',[ref]$tokens,[ref]$parseErrors); "
                    f"$fn=$ast.Find({{param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq '{function}'}},$true); "
                    f"Invoke-Expression $fn.Extent.Text; {function} -Executable '{python}' -WorkingDirectory '{location}' "
                    f"-LogDirectory '{location}' -Arguments @('{argument}') -TimeoutMilliseconds 1000"
                )
                started = time.monotonic()
                result = self.run_ps(command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("timed out", result.stderr)
                self.assertLess(time.monotonic() - started, 15)
                child_pid = int((root / "smoke.stdout.log").read_text().strip())
                check = self.run_ps(f"if(Get-Process -Id {child_pid} -ErrorAction SilentlyContinue){{exit 1}}")
                self.assertEqual(check.returncode, 0, "Timed-out smoke test left its child running")
                self.assertTrue((root / "smoke.stderr.log").exists())


if __name__ == "__main__":
    unittest.main()
