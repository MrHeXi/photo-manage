"""Render isolated demo windows for manual UI review at a selected Qt DPI scale."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
output = root / "build" / "ui-optimization-review" / os.environ.get("QT_SCALE_FACTOR", "1")
output.mkdir(parents=True, exist_ok=True)
os.environ["LOCALAPPDATA"] = str(output / "data")

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionComboBox
from photo_dedupe.core.demo_workspace import create_demo_workspace
from photo_dedupe.core.scanner import DuplicateScanner
from photo_dedupe.ui.main_window import MainWindow

app = QApplication([])
QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
window = MainWindow(safe_mode=True)
window._confirm_action = lambda *_: True
sample = create_demo_workspace(output)
window.protected_roots = set(sample.protected_roots)
for path in sample.scan_roots:
    window._add_folder_item(path)
window.current_roots = sample.scan_roots
window.on_scan_finished(DuplicateScanner().scan(sample.scan_roots))
window.switch_to_page("duplicates")
window.show()
for width, height in [(1024, 680), (1360, 820)]:
    window.resize(width, height)
    window.advanced_filter_toggle.setChecked(True)
    app.processEvents()
    for combo in (window.group_min_size_unit_combo, window.group_max_size_unit_combo, window.bulk_keep_rule_combo):
        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        rect = combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
                                           QStyle.SubControl.SC_ComboBoxEditField, combo)
        assert rect.width() >= combo.fontMetrics().horizontalAdvance(combo.currentText()), combo.currentText()
    path = output / f"duplicates-{width}.png"
    window.grab().save(str(path))
    print(path)
    window.bulk_keep_rule_combo.showPopup()
    app.processEvents()
    window.bulk_keep_rule_combo.view().window().grab().save(str(output / f"dropdown-{width}.png"))
    window.bulk_keep_rule_combo.hidePopup()
window.close()
app.processEvents()
