# -*- mode: python ; coding: utf-8 -*-
# Minimal PyInstaller spec for agent/probe_audio.py.
# Purpose: an isolated probe exe that imports ONLY pyaudiowpatch, to compare
# WASAPI loopback open() behavior between dev and frozen environments.

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("pyaudiowpatch")

a = Analysis(
    ["probe_audio.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="ProbeAudio",
    debug=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ProbeAudio",
)
