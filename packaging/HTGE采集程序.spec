# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH).parent
app_name = "grab"
icon_path = project_root / "packaging" / "assets" / "grab.ico"

sdk_binaries = []
if os.environ.get("HTGE_INCLUDE_SDK", "1") != "0":
    sdk_dir = Path(os.environ.get("HTGE_SDK_X64", r"D:\HuaTengVision\SDK\X64"))
    if sdk_dir.exists():
        for dll_path in sdk_dir.glob("*.dll"):
            sdk_binaries.append((str(dll_path), "grab_app/camera"))

xy_stage_dir = project_root / "grab_app" / "xy_stage"
xy_vendor_dir = xy_stage_dir / "vendor" / "x64"
for dll_name in ("zauxdll.dll", "zmotion.dll"):
    dll_path = xy_vendor_dir / dll_name
    if dll_path.exists():
        sdk_binaries.append((str(dll_path), "grab_app/xy_stage/vendor/x64"))

datas = []
readme = project_root / "README.md"
if readme.exists():
    datas.append((str(readme), "."))
if icon_path.exists():
    datas.append((str(icon_path), "assets"))
xy_profile = xy_stage_dir / "device_profile.json"
if xy_profile.exists():
    datas.append((str(xy_profile), "grab_app/xy_stage"))


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
    icon=str(icon_path),
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
