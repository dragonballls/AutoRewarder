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
    # The window is opened through Qt (`webview.start(gui="qt")`), which
    # pywebview reaches via qtpy. `gi` is only used by the GTK backend, which
    # this build does not use, so bundling it just bloats the archive.
    hidden_imports += ['PySide6', 'qtpy']


def optional_data_files(package):
    """Data files of an optional dependency; empty when it isn't installed.

    The app survives a missing nlpaug (it searches the queries unchanged), so a
    build machine without it must not fail the build. Without this bundle the
    typo pass silently no-ops in the frozen app.
    """
    try:
        return collect_data_files(package)
    except Exception:
        print(f"[spec] {package} data files unavailable; building without them.")
        return []


def optional_metadata(package):
    """Distribution metadata of an optional dependency; empty when unavailable.

    nlpaug reads package metadata at import time and raises PackageNotFoundError
    when it is absent, which used to crash the static-query fallback in the
    frozen build (issue #160). Bundling the metadata is what keeps that import
    working; skipping it here only degrades to the un-augmented path.
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
    [],
    exclude_binaries=True,
    name='AutoRewarder',
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
    icon=[app_icon],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AutoRewarder',
)
