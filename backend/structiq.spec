# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for StructIQ desktop app.
Run from the backend/ folder:
    pyinstaller structiq.spec
"""
import os
import certifi
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# Collect all fastapi / uvicorn / starlette sub-modules
hidden = []
for pkg in ('fastapi', 'uvicorn', 'starlette', 'pydantic', 'comtypes', 'anyio', 'h11'):
    hidden += collect_submodules(pkg)

a = Analysis(
    ['launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('frontend', 'frontend'),          # HTML / CSS / JS
        ('etabs_api', 'etabs_api'),        # ETABS COM bridge
        ('database.py', '.'),              # Auth DB module
        ('config.py', '.'),                # App config
        (certifi.where(), 'certifi'),      # SSL CA bundle — needed for HTTPS in .exe
    ],
    hiddenimports=hidden + [
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'email.mime.text',
        'email.mime.multipart',
        'comtypes.client',
        'comtypes.server',
        'win32com.client',
        'sqlite3',
        'requests',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'PIL'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='StructIQ',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,          # set False to hide the terminal window
    icon='icon.ico',       # StructIQ brand icon
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='StructIQ',
)
