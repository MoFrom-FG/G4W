# -*- mode: python ; coding: utf-8 -*-
# Portable PyInstaller spec: build from anywhere with
#   python -m PyInstaller --clean --noconfirm G4W.spec
# The venv used must have pywebview + pythonnet (clr) installed.
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files

HERE = os.path.abspath(SPECPATH)          # ...\desktop
ROOT = os.path.dirname(HERE)              # repository root

datas = [
    (os.path.join(HERE, 'icon.ico'), '.'),
    (os.path.join(HERE, 'loading.html'), '.'),
    (os.path.join(HERE, 'wizard.html'), '.'),
    (os.path.join(HERE, 'wizard_prepare.py'), '.'),
]
# pywebview ships its JS as package data; collect it from the active environment.
datas += collect_data_files('webview', includes=['js/*'])
binaries = []
hiddenimports = []
tmp_ret = collect_all('clr')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    [os.path.join(HERE, 'shell.py')],
    pathex=[],
    binaries=binaries,
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
    name='G4W',
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
    icon=[os.path.join(HERE, 'icon.ico')],
)
