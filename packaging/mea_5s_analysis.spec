"""本文件定义无需管理员权限即可运行的PyInstaller onedir发布包。"""
# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
PANDAS_FORMAT_MODULES = collect_submodules("pandas.io.formats")
OPENPYXL_MODULES = collect_submodules("openpyxl")

a = Analysis(
    [str(ROOT / "scripts" / "start_gui.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "config"), "config"),
        (str(ROOT / "resources"), "resources"),
    ],
    hiddenimports=PANDAS_FORMAT_MODULES + OPENPYXL_MODULES,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "pytest_qt"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MEA5S缺陷分析",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MEA5S缺陷分析",
)
