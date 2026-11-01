# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("app\\templates", "app\\templates"),
    ("app\\static", "app\\static"),
    ("config", "config"),
]
binaries = []
hiddenimports = [
    "aiosqlite",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "greenlet",
    "yaml",
    "phonenumbers",
    "openpyxl",
    "rapidfuzz",
    "httpx",
    "structlog",
    "pydantic_settings",
]
hiddenimports += collect_submodules("app")
hiddenimports += collect_submodules("aiosqlite")

for pkg in (
    "uvicorn",
    "fastapi",
    "starlette",
    "jinja2",
    "aiosqlite",
    "sqlalchemy",
    "greenlet",
    "playwright",
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["app\\desktop.py"],
    pathex=["."],
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
    [],
    exclude_binaries=True,
    name="EgyptLeads",
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="EgyptLeads",
)
