# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['ftp_server_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('config_manager.py', '.'), ('tray_manager.py', '.'), ('app_icon.ico', '.')],
    hiddenimports=['pystray._win32', 'pystray._util.win32'],
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
    name='FTP服务器',
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
    icon=['app_icon.ico'],
)
