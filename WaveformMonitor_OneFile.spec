# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import snap7

snap7_dll = Path(snap7.__file__).resolve().parent / "lib" / "snap7.dll"

a = Analysis(
    ['waveform_app.py'],
    pathex=[],
    binaries=[(str(snap7_dll), 'snap7/lib')],
    datas=[],
    hiddenimports=['qtpy', 'qtpy.QtCore', 'qtpy.QtGui', 'qtpy.QtWidgets', 'matplotlib.backends.backend_qt5agg', 'PIL.Image', 'snap7', 'snap7.util'],
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
    name='WaveformMonitor_OneFile',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
