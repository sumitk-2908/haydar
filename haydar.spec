# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Haydar. Produces two executables:

  * haydar.exe      -- windowed (no console); the GUI + global hotkey.
  * haydar-cli.exe  -- console; the full Typer CLI (init/search/watch/...).

Before building, run ``python scripts/pull-rg.py`` so ``src/haydar/bin/rg.exe``
exists; it is bundled so keyword search works in the packaged app.
"""

import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

# --- Collect data, binaries, and hidden imports for heavy dependencies ---------
datas = []
binaries = []
hiddenimports = [
    "chromadb",
    "chromadb.db.impl",
    "chromadb.db.impl.sqlite",
    "tokenizers",
    "sentence_transformers",
    "onnxruntime",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "watchdog.observers.read_directory_changes",
]

for pkg in (
    "chromadb",
    "sentence_transformers",
    "tokenizers",
    "onnxruntime",
    "huggingface_hub",
    "tqdm",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        # Package may not expose data/binaries; keep going.
        pass

# Some packages ship importlib metadata that libraries query at runtime.
for pkg in ("sentence_transformers", "chromadb", "tqdm"):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

# Bundle the ripgrep binary if it has been fetched (scripts/pull-rg.py).
_rg = os.path.join("src", "haydar", "bin", "rg.exe")
if os.path.exists(_rg):
    binaries += [(_rg, os.path.join("haydar", "bin"))]

# --- Analysis (shared by both executables) -------------------------------------
a = Analysis(
    ["src/haydar/__main__.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

gui_a = Analysis(
    ["src/haydar/gui_main.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)
gui_pyz = PYZ(gui_a.pure, gui_a.zipped_data)

# Console CLI executable.
exe_cli = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="haydar-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# Windowed GUI executable.
exe_gui = EXE(
    gui_pyz,
    gui_a.scripts,
    gui_a.binaries,
    gui_a.datas,
    [],
    name="haydar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
