# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH).parent
app_name = "grab"

sdk_binaries = []
if os.environ.get("HTGE_INCLUDE_SDK", "1") != "0":
    sdk_dir = Path(os.environ.get("HTGE_SDK_X64", r"D:\HuaTengVision\SDK\X64"))
    if sdk_dir.exists():
        for dll_path in sdk_dir.glob("*.dll"):
            sdk_binaries.append((str(dll_path), "grab_app/camera"))

datas = []
readme = project_root / "README.md"
if readme.exists():
    datas.append((str(readme), "."))


a = Analysis(
    [str(project_root / "run_app.py")],
    pathex=[str(project_root)],
    binaries=sdk_binaries,
    datas=datas,
    hiddenimports=collect_submodules("grab_app"),
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
    name=app_name,
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=app_name,
)
