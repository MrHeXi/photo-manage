"""Build identity shared by the application, diagnostics and release tooling."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import argparse
from importlib.metadata import PackageNotFoundError, version as package_version
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


BUILD_INFO_FILENAME = "build-info.json"


@dataclass(frozen=True, slots=True)
class BuildInfo:
    version: str
    commit: str
    built_at_utc: str
    runtime: str = "source"

    @property
    def short_commit(self) -> str:
        return self.commit[:12] or "unknown"

    @property
    def label(self) -> str:
        return f"v{self.version} · {self.short_commit} · {self.built_at_utc or '源码运行'}"

    @property
    def build_id(self) -> str:
        timestamp = (
            datetime.fromisoformat(self.built_at_utc.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            if self.built_at_utc else "source"
        )
        return f"v{self.version}-{self.short_commit}-{timestamp or 'source'}"

    def as_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "commit": self.commit,
            "built_at_utc": self.built_at_utc,
            "runtime": self.runtime,
            "build_id": self.build_id,
        }


def _source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _candidate_info_paths() -> tuple[Path, ...]:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.extend(
            [
                Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / BUILD_INFO_FILENAME,
                Path(sys.executable).resolve().parent / BUILD_INFO_FILENAME,
            ]
        )
    else:
        explicit = os.environ.get("PHOTO_MANAGE_BUILD_INFO")
        if explicit:
            candidates.append(Path(explicit))
    # Preserve ordering while avoiding duplicate reads when cwd is the source.
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def _read_sidecar() -> dict[str, Any] | None:
    for path in _candidate_info_paths():
        try:
            if not path.is_file():
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return None


@lru_cache(maxsize=1)
def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_source_root(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def get_build_info(version: str | None = None) -> BuildInfo:
    """Return the best available identity for this running application."""

    if version is None:
        from photo_dedupe import __version__
        version = __version__
    sidecar = _read_sidecar() or {}
    resolved_version = str(sidecar.get("version") or version)
    frozen = bool(getattr(sys, "frozen", False))
    commit = str(sidecar.get("commit") or ("" if frozen else _git_commit()))
    built_at = str(sidecar.get("built_at_utc") or "")
    runtime = str(sidecar.get("runtime") or ("frozen" if getattr(sys, "frozen", False) else "source"))
    return BuildInfo(resolved_version, commit, built_at, runtime)


def write_build_info(path: Path, *, version: str, commit: str, built_at_utc: str | None = None, runtime: str = "frozen") -> Path:
    """Write a deterministic build sidecar used by packaged runtime code."""

    timestamp = built_at_utc or datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = BuildInfo(version, commit, timestamp, runtime).as_dict()
    payload["python_version"] = sys.version.split()[0]
    dependencies = {}
    for name in ("PySide6", "Pillow", "Send2Trash", "PyInstaller"):
        try:
            dependencies[name] = package_version(name)
        except PackageNotFoundError:
            dependencies[name] = "unavailable"
    payload["dependencies"] = dependencies
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Write immutable build metadata before packaging.")
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--built-at")
    args = parser.parse_args()
    from photo_dedupe import __version__

    path = write_build_info(args.write, version=__version__, commit=args.commit, built_at_utc=args.built_at)
    print(path)


if __name__ == "__main__":
    main()
