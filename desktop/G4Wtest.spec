# -*- mode: python ; coding: utf-8 -*-
# Minimal probe build (no clr/webview data) used to smoke-test shell.py.
#   python -m PyInstaller --clean --noconfirm G4Wtest.spec
import os

HERE = os.path.abspath(SPECPATH)


a = Analysis(
    [os.path.join(HERE, 'shell.py')],
    pathex=[],
    binaries=[],
    datas=[
        (os.path.join(HERE, 'wizard.html'), '.'),
        (os.path.join(HERE, 'wizard_prepare.py'), '.'),
    ],
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
    a.binaries,
    a.datas,
    [],
    name='G4Wtest',
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
