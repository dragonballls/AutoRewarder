# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

is_windows = sys.platform == 'win32'
app_icon = os.path.join('assets', 'icon.ico') if is_windows else os.path.join('assets', 'icon.png')

hidden_imports = [
    'selenium.webdriver.edge.webdriver',
    'pystray',
    'PIL',
    'PIL.Image',
]
if is_windows:
    hidden_imports.append('pystray._win32')
else:
    hidden_imports += ['PySide6', 'gi']


def optional_data_files(package):
    """Data files of an optional dependency; empty when it isn't installed."""
    try:
        return collect_data_files(package)
    except Exception:
        print(f"[spec] {package} data files unavailable; building without them.")
        return []


def optional_metadata(package):
    """Distribution metadata of an optional dependency; empty when unavailable.

    nlpaug reads package metadata at import time and raises PackageNotFoundError
    when it is absent. The portable build is a --onefile bundle, so it needs the
    same metadata the installed build collects or the static-query fallback
    crashes there too (issue #160).
    """
    try:
        return copy_metadata(package)
    except Exception:
        print(f"[spec] {package} metadata unavailable; building without it.")
        return []


a = Analysis(
    ['AutoRewarder.py'],
    pathex=[],
    binaries=[],
    datas=[('gui', 'gui'), ('assets', 'assets')]
        + optional_data_files('nlpaug')
        + optional_metadata('gdown')
        + optional_metadata('nlpaug'),
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

a.datas = [x for x in a.datas if not (x[0].startswith('assets/screenshots') or x[0].startswith('assets\\screenshots'))]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AutoRewarder',
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
    icon=[app_icon],
)
