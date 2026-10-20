# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['aqzrui_speedrun', 'aqzrui_api', 'aqzrui_login', 'ai_fallback',
                 'labsafe_client', 'labsafe_player']
hiddenimports += collect_submodules('Crypto')


a = Analysis(
    ['aqzrui_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('app.ico', '.')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['Crypto.SelfTest', 'Crypto.PublicKey', 'Crypto.Signature', 'Crypto.Protocol', 'numpy', 'PIL', 'matplotlib'],
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
    name='aqzrui-speedrun',
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
    icon=['app.ico'],
)
