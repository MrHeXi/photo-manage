from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from photo_dedupe.core.release_manifest import MANIFEST_FILENAME


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


def run_release_check(project_root: Path | None = None) -> ReleaseCheckReport:
    root = (project_root or Path.cwd()).resolve()
    items: list[ReleaseCheckItem] = []

    _check_required_files(root, items)
    _check_assets(root, items)
    _check_version(root, items)
    _check_spec(root, items)
    _check_scripts(root, items)
    _check_readme(root, items)
    _check_dist(root, items)
    _check_release_manifest(root, items)
    _check_release_zip(root, items)

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
        "CHANGELOG.md",
        "build.ps1",
        "package.ps1",
        "run-safe.ps1",
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
    icon = root / "photo_dedupe" / "assets" / "app_icon.ico"
    png = root / "photo_dedupe" / "assets" / "app_icon.png"
    missing = [path.name for path in (icon, png) if not path.exists() or path.stat().st_size == 0]
    if missing:
        items.append(ReleaseCheckItem("品牌图标", STATUS_ERROR, "缺少或为空：" + ", ".join(missing)))
    else:
        items.append(ReleaseCheckItem("品牌图标", STATUS_OK, "ICO 和 PNG 均存在"))


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
    if "README.md" not in package:
        problems.append("package.ps1 未复制 README")
    if "CHANGELOG.md" not in package:
        problems.append("package.ps1 未复制 CHANGELOG")
    if "Compress-Archive" not in package and "CreateFromDirectory" not in package:
        problems.append("package.ps1 未生成 zip")
    if "--write-release-manifest" not in package:
        problems.append("package.ps1 未生成发布 Manifest")
    if "--release-check" not in package:
        problems.append("package.ps1 未运行发布自检")
    if "--startup-smoke-test" not in package:
        problems.append("package.ps1 未运行打包 EXE 启动烟测")
    if problems:
        items.append(ReleaseCheckItem("发布脚本", STATUS_ERROR, "；".join(problems)))
    else:
        items.append(ReleaseCheckItem("发布脚本", STATUS_OK, "构建、发布包、安全模式入口和启动烟测齐全"))


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
    ]
    missing = [phrase for phrase in required_phrases if phrase not in readme]
    if missing:
        items.append(ReleaseCheckItem("中文使用手册", STATUS_WARN, "建议补充：" + "、".join(missing)))
    else:
        items.append(ReleaseCheckItem("中文使用手册", STATUS_OK, "已包含安全、恢复、打包和发布说明"))


def _check_dist(root: Path, items: list[ReleaseCheckItem]) -> None:
    exe = root / "dist" / "PhotoManageGPT" / "PhotoManageGPT.exe"
    if not exe.exists() or exe.stat().st_size == 0:
        items.append(ReleaseCheckItem("EXE 产物", STATUS_ERROR, "未找到 dist/PhotoManageGPT/PhotoManageGPT.exe"))
    else:
        items.append(ReleaseCheckItem("EXE 产物", STATUS_OK, f"{exe.stat().st_size} bytes"))


def _check_release_zip(root: Path, items: list[ReleaseCheckItem]) -> None:
    version = _current_version(root)
    if not version:
        items.append(ReleaseCheckItem("发布 zip", STATUS_WARN, "无法确认版本号，跳过 zip 检查"))
        return
    zip_path = root / "release" / f"PhotoManageGPT-v{version}-win64.zip"
    if not zip_path.exists() or zip_path.stat().st_size == 0:
        items.append(ReleaseCheckItem("发布 zip", STATUS_WARN, "尚未生成发布 zip，可运行 .\\package.ps1"))
        return

    required = {"PhotoManageGPT.exe", "README.md", "CHANGELOG.md", "PhotoManageGPT-safe-mode.cmd", MANIFEST_FILENAME}
    try:
        with zipfile.ZipFile(zip_path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                items.append(ReleaseCheckItem("发布 zip", STATUS_ERROR, f"zip 内文件损坏：{bad_member}"))
                return
            names = {name.replace("\\", "/") for name in archive.namelist()}
    except (OSError, zipfile.BadZipFile) as exc:
        items.append(ReleaseCheckItem("发布 zip", STATUS_ERROR, f"无法读取发布 zip：{exc}"))
        return

    missing = sorted(required - names)
    if missing:
        items.append(ReleaseCheckItem("发布 zip", STATUS_ERROR, "zip 缺少：" + "、".join(missing)))
    else:
        items.append(ReleaseCheckItem("发布 zip", STATUS_OK, str(zip_path)))


def _check_release_manifest(root: Path, items: list[ReleaseCheckItem]) -> None:
    version = _current_version(root)
    if not version:
        items.append(ReleaseCheckItem("发布 Manifest", STATUS_WARN, "无法确认版本号，跳过 Manifest 检查"))
        return
    release_dir = root / "release" / f"PhotoManageGPT-v{version}-win64"
    manifest = release_dir / MANIFEST_FILENAME
    if not release_dir.exists():
        items.append(ReleaseCheckItem("发布 Manifest", STATUS_WARN, "尚未生成发布目录，可运行 .\\package.ps1"))
    elif not manifest.exists() or manifest.stat().st_size == 0:
        items.append(ReleaseCheckItem("发布 Manifest", STATUS_ERROR, f"发布目录缺少 {MANIFEST_FILENAME}"))
    else:
        items.append(ReleaseCheckItem("发布 Manifest", STATUS_OK, str(manifest)))


def _current_version(root: Path) -> str:
    init_file = root / "photo_dedupe" / "__init__.py"
    if not init_file.exists():
        return ""
    return _match_version(init_file.read_text(encoding="utf-8"), r'__version__\s*=\s*"([^"]+)"')


def _match_version(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    return match.group(1) if match else ""
