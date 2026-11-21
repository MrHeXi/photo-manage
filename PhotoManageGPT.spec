# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

build_info = os.environ.get("PHOTO_MANAGE_BUILD_INFO")
if not build_info or not Path(build_info).is_file():
    raise RuntimeError("Use build.ps1 to supply immutable build-info.json metadata.")

a = Analysis(
    ['photo_dedupe\\__main__.py'],
    pathex=[],
    binaries=[],
    datas=[('photo_dedupe\\assets', 'photo_dedupe\\assets'), (build_info, '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PhotoManageGPT',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='photo_dedupe\\assets\\app_icon.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PhotoManageGPT',
)
