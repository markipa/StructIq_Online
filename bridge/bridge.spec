# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for StructIQ Bridge.
Bundles: launcher.py + server.py + backend modules (pmm_engine, etabs_api)
Output: dist/StructIQ-Bridge/StructIQ-Bridge.exe  (one-folder build)
"""
import os, sys

_ROOT    = os.path.abspath(os.path.join(SPECPATH, '..'))
_BACKEND = os.path.join(_ROOT, 'backend')
_BRIDGE  = os.path.join(_ROOT, 'bridge')

block_cipher = None

a = Analysis(
    [os.path.join(_BRIDGE, 'launcher.py')],
    pathex=[_BRIDGE, _BACKEND],
    binaries=[],
    datas=[
        # Bundle the ETABS API package
        (os.path.join(_BACKEND, 'etabs_api'), 'etabs_api'),
        # Bundle pmm_engine so the bridge can hot-reload it
        (os.path.join(_BACKEND, 'pmm_engine.py'), '.'),
        # Bundle server.py alongside launcher
        (os.path.join(_BRIDGE, 'server.py'), '.'),
    ],
    hiddenimports=[
        # FastAPI / uvicorn
        'fastapi', 'uvicorn', 'uvicorn.logging', 'uvicorn.loops',
        'uvicorn.loops.auto', 'uvicorn.protocols', 'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        # ASGI / h11
        'h11', 'anyio', 'anyio.abc', 'starlette',
        # WebSockets
        'websockets', 'websockets.legacy', 'websockets.legacy.client',
        # HTTP client
        'httpx', 'httpcore',
        # Windows COM for ETABS
        'comtypes', 'comtypes.client',
        # Tray icon
        'pystray', 'PIL', 'PIL.Image', 'PIL.ImageDraw',
        # Stdlib extras
        'email.mime.text', 'email.mime.multipart',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas'],
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
    name='StructIQ-Bridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,           # silent — no terminal window
    icon=None,               # replace with .ico path if you have one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='StructIQ-Bridge',
)
