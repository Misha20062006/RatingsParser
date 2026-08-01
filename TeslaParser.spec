# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from runpy import run_path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project_root = Path(SPECPATH)
version_helpers = run_path(str(project_root / "scripts" / "windows_version_info.py"))
version_file = version_helpers["generate_version_info"](
    project_root / "pyproject.toml",
    project_root / "build" / "TeslaParser-version-info.txt",
    kind="cli",
)

datas = collect_data_files("patchright")
hiddenimports = collect_submodules("patchright")

a = Analysis(
    ["teslaparser.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
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
    a.binaries,
    a.datas,
    [],
    name="TeslaParser",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["assets/icon.ico"],
    version=str(version_file),
)
