from __future__ import annotations

import re
import argparse
import hashlib
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from photo_dedupe.core.release_manifest import MANIFEST_FILENAME, sha256_file, verify_release_manifest
from photo_dedupe.core.build_info import BUILD_INFO_FILENAME, BuildInfo


STATUS_OK = "通过"
STATUS_WARN = "提醒"
STATUS_ERROR = "失败"


@dataclass(frozen=True, slots=True)
class ReleaseCheckItem:
    name: str
    status: str
    detail: str

    @property
    def blocking(self) -> bool:
        return self.status == STATUS_ERROR


@dataclass(frozen=True, slots=True)
class ReleaseCheckReport:
    items: tuple[ReleaseCheckItem, ...]

    @property
    def passed(self) -> bool:
        return not any(item.blocking for item in self.items)


def run_release_check(
    project_root: Path | None = None, *, dist_dir: Path | None = None,
    release_dir: Path | None = None, zip_path: Path | None = None,
    build_only: bool = False, require_build_info: bool = False,
) -> ReleaseCheckReport:
    root = (project_root or Path.cwd()).resolve()
    items: list[ReleaseCheckItem] = []

    _check_required_files(root, items)
    _check_assets(root, items)
    _check_version(root, items)
    _check_spec(root, items)
    _check_scripts(root, items)
    _check_readme(root, items)
    _check_dist(root, items, dist_dir)
    if require_build_info:
        _check_build_info(dist_dir or root / "dist" / "PhotoManageGPT", _current_version(root), items)
    if not build_only:
        selected_dir = release_dir or _latest_release_directory(root)
        selected_zip = zip_path or Path(str(selected_dir) + ".zip")
        _check_release_manifest(root, items, selected_dir)
        _check_release_zip(root, items, selected_zip)
        _check_release_checksum(root, items, selected_zip)
        if require_build_info:
            _check_build_info(selected_dir, _current_version(root), items)

    return ReleaseCheckReport(tuple(items))


def format_release_check_report(report: ReleaseCheckReport) -> str:
    lines = ["Photo Manage GPT 发布自检"]
    for item in report.items:
        lines.append(f"[{item.status}] {item.name}：{item.detail}")
    result = "通过" if report.passed else "失败"
    lines.append(f"结论：{result}")
    return "\n".join(lines)


def _check_required_files(root: Path, items: list[ReleaseCheckItem]) -> None:
    required = [
        "README.md",
        "QUICKSTART.md",
        "CHANGELOG.md",
        "build.ps1",
        "package.ps1",
        "run-safe.ps1",
        "install-shortcut.ps1",
        "remove-shortcut.ps1",
        "PhotoManageGPT.spec",
        "pyproject.toml",
        "photo_dedupe/__main__.py",
        "photo_dedupe/ui/main_window.py",
    ]
    missing = [path for path in required if not (root / path).exists()]
    if missing:
        items.append(ReleaseCheckItem("必需文件", STATUS_ERROR, "缺少：" + ", ".join(missing)))
    else:
        items.append(ReleaseCheckItem("必需文件", STATUS_OK, f"{len(required)} 个文件齐全"))


def _check_assets(root: Path, items: list[ReleaseCheckItem]) -> None:
    assets_root = root / "photo_dedupe" / "assets"
    required = (
        "app_icon.ico",
        "app_icon.png",
        "lucide-chevron-down.svg",
        "lucide-chevron-up.svg",
        "lucide-chevron-down-disabled.svg",
        "lucide-chevron-up-disabled.svg",
        "LUCIDE-LICENSE.txt",
    )
    missing = [
        filename
        for filename in required
        if not (assets_root / filename).exists() or (assets_root / filename).stat().st_size == 0
    ]
    if missing:
        items.append(ReleaseCheckItem("界面资源", STATUS_ERROR, "缺少或为空：" + ", ".join(missing)))
    else:
        items.append(ReleaseCheckItem("界面资源", STATUS_OK, "品牌图标、控件图标和许可证均存在"))


def _check_version(root: Path, items: list[ReleaseCheckItem]) -> None:
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8") if (root / "pyproject.toml").exists() else ""
    init_file = root / "photo_dedupe" / "__init__.py"
    init_text = init_file.read_text(encoding="utf-8") if init_file.exists() else ""
    project_version = _match_version(pyproject, r'version\s*=\s*"([^"]+)"')
    package_version = _match_version(init_text, r'__version__\s*=\s*"([^"]+)"')
    if not project_version or not package_version:
        items.append(ReleaseCheckItem("版本号", STATUS_ERROR, "无法读取 pyproject.toml 或 photo_dedupe.__version__"))
    elif project_version != package_version:
        items.append(ReleaseCheckItem("版本号", STATUS_ERROR, f"pyproject={project_version}，package={package_version}"))
    else:
        items.append(ReleaseCheckItem("版本号", STATUS_OK, project_version))


def _check_spec(root: Path, items: list[ReleaseCheckItem]) -> None:
    spec_path = root / "PhotoManageGPT.spec"
    content = spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""
    normalized = content.replace("\\\\", "/").replace("\\", "/")
    problems: list[str] = []
    if "app_icon.ico" not in content:
        problems.append("未配置 EXE 图标")
    if "photo_dedupe/assets" not in normalized:
        problems.append("未打包 assets")
    if "console=False" not in content:
        problems.append("未设置窗口模式")
    if problems:
        items.append(ReleaseCheckItem("PyInstaller 配置", STATUS_ERROR, "；".join(problems)))
    else:
        items.append(ReleaseCheckItem("PyInstaller 配置", STATUS_OK, "已配置图标、assets 和窗口模式"))


def _check_scripts(root: Path, items: list[ReleaseCheckItem]) -> None:
    build = (root / "build.ps1").read_text(encoding="utf-8") if (root / "build.ps1").exists() else ""
    package = (root / "package.ps1").read_text(encoding="utf-8") if (root / "package.ps1").exists() else ""
    problems: list[str] = []
    if "PhotoManageGPT.spec" not in build:
        problems.append("build.ps1 未使用 PhotoManageGPT.spec")
    if "--release-check" not in build:
        problems.append("build.ps1 未运行发布自检")
    if "PhotoManageGPT-safe-mode.cmd" not in package:
        problems.append("package.ps1 未生成安全模式启动器")
    if "install-shortcut.ps1" not in package:
        problems.append("package.ps1 未复制桌面快捷方式安装脚本")
    if "remove-shortcut.ps1" not in package:
        problems.append("package.ps1 未复制桌面快捷方式移除脚本")
    if "README.md" not in package:
        problems.append("package.ps1 未复制 README")
    if "QUICKSTART.md" not in package:
        problems.append("package.ps1 未复制 QUICKSTART")
    if "CHANGELOG.md" not in package:
        problems.append("package.ps1 未复制 CHANGELOG")
    if "Compress-Archive" not in package and "CreateFromDirectory" not in package:
        problems.append("package.ps1 未生成 zip")
    if "Get-FileHash" not in package or ".sha256" not in package:
        problems.append("package.ps1 未生成 zip SHA-256 校验文件")
    if "--write-release-manifest" not in package:
        problems.append("package.ps1 未生成发布 Manifest")
    if "--release-check" not in package:
        problems.append("package.ps1 未运行发布自检")
    if "--startup-smoke-test" not in package:
        problems.append("package.ps1 未运行打包 EXE 启动烟测")
    if problems:
        items.append(ReleaseCheckItem("发布脚本", STATUS_ERROR, "；".join(problems)))
    else:
        items.append(ReleaseCheckItem("发布脚本", STATUS_OK, "构建、发布包、安全模式入口、快捷方式脚本和启动烟测齐全"))


def _check_readme(root: Path, items: list[ReleaseCheckItem]) -> None:
    readme = (root / "README.md").read_text(encoding="utf-8") if (root / "README.md").exists() else ""
    required_phrases = [
        "安全模式",
        "打包 EXE",
        "发布自检",
        "默认不做永久删除",
        "处理结果报告",
        "操作记录",
        "RELEASE-MANIFEST.json",
        "启动烟测",
        "桌面快捷方式",
    ]
    missing = [phrase for phrase in required_phrases if phrase not in readme]
    if missing:
        items.append(ReleaseCheckItem("中文使用手册", STATUS_WARN, "建议补充：" + "、".join(missing)))
    else:
        items.append(ReleaseCheckItem("中文使用手册", STATUS_OK, "已包含安全、恢复、打包和发布说明"))


def _check_dist(root: Path, items: list[ReleaseCheckItem], dist_dir: Path | None = None) -> None:
    exe = (dist_dir or root / "dist" / "PhotoManageGPT") / "PhotoManageGPT.exe"
    if not exe.exists() or exe.stat().st_size == 0:
        items.append(ReleaseCheckItem("EXE 产物", STATUS_ERROR, "未找到 dist/PhotoManageGPT/PhotoManageGPT.exe"))
    else:
        items.append(ReleaseCheckItem("EXE 产物", STATUS_OK, f"{exe.stat().st_size} bytes"))


def _check_release_zip(root: Path, items: list[ReleaseCheckItem], zip_path: Path | None = None) -> None:
    version = _current_version(root)
    if not version:
        items.append(ReleaseCheckItem("发布 zip", STATUS_WARN, "无法确认版本号，跳过 zip 检查"))
        return
    zip_path = zip_path or root / "release" / f"PhotoManageGPT-v{version}-win64.zip"
    if not zip_path.exists() or zip_path.stat().st_size == 0:
        items.append(ReleaseCheckItem("发布 zip", STATUS_WARN, "尚未生成发布 zip，可运行 .\\package.ps1"))
        return

    required = {
        "PhotoManageGPT.exe",
        "README.md",
        "QUICKSTART.md",
        "CHANGELOG.md",
        "PhotoManageGPT-safe-mode.cmd",
        "install-shortcut.ps1",
        "remove-shortcut.ps1",
        MANIFEST_FILENAME,
    }
    try:
        with zipfile.ZipFile(zip_path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                items.append(ReleaseCheckItem("发布 zip", STATUS_ERROR, f"zip 内文件损坏：{bad_member}"))
                return
            names = {name.replace("\\", "/") for name in archive.namelist()}
            if len(names) != len(archive.namelist()):
                raise ValueError("zip 包含重复文件名")
            if MANIFEST_FILENAME in names:
                manifest = json.loads(archive.read(MANIFEST_FILENAME))
                records = manifest["files"]
                if not isinstance(records, list) or not records:
                    raise ValueError("zip Manifest 文件列表为空")
                listed = set()
                for record in records:
                    filename = record["path"]
                    if not isinstance(filename, str) or not filename or "\\" in filename or (
                        PurePosixPath(filename).is_absolute() or ".." in PurePosixPath(filename).parts or ":" in filename
                    ):
                        raise ValueError("zip Manifest 文件路径无效")
                    if filename in listed:
                        raise ValueError(f"Manifest 重复文件：{filename}")
                    listed.add(filename)
                    if filename not in names:
                        raise ValueError(f"zip 缺少 Manifest 文件：{filename}")
                    digest = hashlib.sha256()
                    with archive.open(filename) as payload:
                        for chunk in iter(lambda: payload.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if archive.getinfo(filename).file_size != record["size"] or digest.hexdigest() != record["sha256"]:
                        raise ValueError(f"zip 内文件校验失败：{filename}")
                actual = {name for name in names if not name.endswith("/") and name != MANIFEST_FILENAME}
                if actual != listed:
                    raise ValueError("zip 内容与 Manifest 文件列表不一致")
    except (OSError, zipfile.BadZipFile, ValueError, KeyError, TypeError) as exc:
        items.append(ReleaseCheckItem("发布 zip", STATUS_ERROR, f"无法读取发布 zip：{exc}"))
        return

    missing = sorted(required - names)
    if missing:
        items.append(ReleaseCheckItem("发布 zip", STATUS_ERROR, "zip 缺少：" + "、".join(missing)))
    else:
        items.append(ReleaseCheckItem("发布 zip", STATUS_OK, str(zip_path)))


def _check_release_checksum(root: Path, items: list[ReleaseCheckItem], zip_path: Path | None = None) -> None:
    version = _current_version(root)
    if not version:
        items.append(ReleaseCheckItem("发布校验", STATUS_WARN, "无法确认版本号，跳过校验文件检查"))
        return
    zip_path = zip_path or root / "release" / f"PhotoManageGPT-v{version}-win64.zip"
    checksum_path = Path(str(zip_path) + ".sha256")
    if not zip_path.exists() or zip_path.stat().st_size == 0:
        items.append(ReleaseCheckItem("发布校验", STATUS_WARN, "尚未生成发布 zip，跳过校验文件检查"))
        return
    if not checksum_path.exists() or checksum_path.stat().st_size == 0:
        items.append(ReleaseCheckItem("发布校验", STATUS_ERROR, f"缺少校验文件：{checksum_path.name}"))
        return

    content = checksum_path.read_text(encoding="ascii", errors="ignore").strip()
    parts = content.split()
    expected_name = zip_path.name
    if len(parts) < 2:
        items.append(ReleaseCheckItem("发布校验", STATUS_ERROR, "校验文件格式应为：<sha256>  <zip 文件名>"))
        return
    recorded_hash, recorded_name = parts[0].lower(), parts[-1]
    actual_hash = sha256_file(zip_path)
    if recorded_name != expected_name:
        items.append(ReleaseCheckItem("发布校验", STATUS_ERROR, f"校验文件名不匹配：{recorded_name}"))
    elif recorded_hash != actual_hash:
        items.append(ReleaseCheckItem("发布校验", STATUS_ERROR, "SHA-256 与当前 zip 不一致"))
    else:
        items.append(ReleaseCheckItem("发布校验", STATUS_OK, checksum_path.name))


def _check_release_manifest(root: Path, items: list[ReleaseCheckItem], release_dir: Path | None = None) -> None:
    version = _current_version(root)
    if not version:
        items.append(ReleaseCheckItem("发布 Manifest", STATUS_WARN, "无法确认版本号，跳过 Manifest 检查"))
        return
    release_dir = release_dir or root / "release" / f"PhotoManageGPT-v{version}-win64"
    manifest = release_dir / MANIFEST_FILENAME
    if not release_dir.exists():
        items.append(ReleaseCheckItem("发布 Manifest", STATUS_WARN, "尚未生成发布目录，可运行 .\\package.ps1"))
    elif not manifest.exists() or manifest.stat().st_size == 0:
        items.append(ReleaseCheckItem("发布 Manifest", STATUS_ERROR, f"发布目录缺少 {MANIFEST_FILENAME}"))
    else:
        problems = verify_release_manifest(release_dir)
        items.append(ReleaseCheckItem(
            "发布 Manifest", STATUS_ERROR if problems else STATUS_OK,
            "；".join(problems[:8]) if problems else f"全部文件大小与 SHA-256 一致：{manifest}",
        ))


def _latest_release_directory(root: Path) -> Path:
    version = _current_version(root)
    candidates = [
        path for path in (root / "release").glob(f"PhotoManageGPT-v{version}-*-win64")
        if path.is_dir() and (path / BUILD_INFO_FILENAME).is_file()
    ]
    if candidates:
        return max(candidates, key=lambda path: path.stat().st_mtime_ns)
    return root / "release" / f"PhotoManageGPT-v{version}-win64"


def _check_build_info(directory: Path, version: str, items: list[ReleaseCheckItem]) -> None:
    try:
        info = json.loads((directory / BUILD_INFO_FILENAME).read_text(encoding="utf-8"))
        embedded = json.loads((directory / "_internal" / BUILD_INFO_FILENAME).read_text(encoding="utf-8"))
        if info != embedded:
            raise ValueError("外部与内嵌构建身份不一致")
        if info["version"] != version or not re.fullmatch(r"[0-9a-f]{40,64}", info["commit"]):
            raise ValueError("版本或 Git 提交格式错误")
        stamp = datetime.fromisoformat(info["built_at_utc"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or info["runtime"] != "frozen":
            raise ValueError("构建时间或运行模式无效")
        expected_id = BuildInfo(info["version"], info["commit"], info["built_at_utc"], info["runtime"]).build_id
        if info["build_id"] != expected_id:
            raise ValueError("构建 ID 与版本、提交和时间不一致")
        manifest_path = directory / MANIFEST_FILENAME
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest["version"] != info["version"] or any(
                manifest["build"].get(key) != info[key]
                for key in ("version", "commit", "built_at_utc", "runtime", "python_version", "dependencies")
            ):
                raise ValueError("Manifest 与程序构建身份不一致")
        detail = f"{info['version']} · {info['commit'][:12]} · {info['built_at_utc']}"
        items.append(ReleaseCheckItem("构建身份", STATUS_OK, detail))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        items.append(ReleaseCheckItem("构建身份", STATUS_ERROR, f"{directory}：{exc}"))


def _current_version(root: Path) -> str:
    init_file = root / "photo_dedupe" / "__init__.py"
    if not init_file.exists():
        return ""
    return _match_version(init_file.read_text(encoding="utf-8"), r'__version__\s*=\s*"([^"]+)"')


def _match_version(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    return match.group(1) if match else ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a staged build or a complete release.")
    parser.add_argument("--release-check", action="store_true")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--dist-dir", type=Path)
    parser.add_argument("--release-dir", type=Path)
    parser.add_argument("--zip-path", type=Path)
    parser.add_argument("--require-build-info", action="store_true")
    args = parser.parse_args()
    report = run_release_check(
        dist_dir=args.dist_dir, release_dir=args.release_dir, zip_path=args.zip_path,
        build_only=args.build_only, require_build_info=args.require_build_info,
    )
    print(format_release_check_report(report))
    raise SystemExit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
