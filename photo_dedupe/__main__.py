from __future__ import annotations

import sys


def main() -> None:
    if "--release-check" in sys.argv:
        from photo_dedupe.core.release_check import format_release_check_report, run_release_check

        report = run_release_check()
        print(format_release_check_report(report))
        raise SystemExit(0 if report.passed else 1)

    from photo_dedupe.ui.main_window import run

    run()


if __name__ == "__main__":
    main()
