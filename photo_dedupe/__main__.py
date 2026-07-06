from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if "--release-check" in sys.argv:
        from photo_dedupe.core.release_check import format_release_check_report, run_release_check

        report = run_release_check()
        print(format_release_check_report(report))
        raise SystemExit(0 if report.passed else 1)

    if "--write-release-manifest" in sys.argv:
        from photo_dedupe import __version__
        from photo_dedupe.core.release_manifest import write_release_manifest

        index = sys.argv.index("--write-release-manifest")
        try:
            release_dir = Path(sys.argv[index + 1])
        except IndexError:
            print("缺少发布目录参数：--write-release-manifest <release-dir>")
            raise SystemExit(2)
        target = write_release_manifest(release_dir, __version__)
        print(f"Release manifest: {target}")
        raise SystemExit(0)

    if "--startup-smoke-test" in sys.argv:
        from photo_dedupe.ui.main_window import startup_smoke_test

        print(startup_smoke_test())
        raise SystemExit(0)

    from photo_dedupe.ui.main_window import run

    run()


if __name__ == "__main__":
    main()
