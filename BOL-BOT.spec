# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

import curl_cffi
import tls_client
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

root = Path(SPECPATH)

# Native TLS backends — required for Akamai bypass (missing = 54-byte GraphQL stubs).
_curl_root = Path(curl_cffi.__file__).parent
_tls_root = Path(tls_client.__file__).parent
_native_binaries = [
    (str(_curl_root / "_wrapper.pyd"), "curl_cffi"),
    (str(_tls_root / "dependencies" / "tls-client-64.dll"), "tls_client/dependencies"),
]
for pkg in ("curl_cffi", "tls_client"):
    try:
        _native_binaries.extend(collect_dynamic_libs(pkg))
    except Exception:
        pass

_playwright_datas = []
try:
    _playwright_datas = collect_data_files("playwright")
except Exception:
    pass

a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=_native_binaries,
    datas=collect_data_files("certifi") + _playwright_datas,
    hiddenimports=[
        "curl_cffi",
        "curl_cffi.requests",
        "curl_cffi._wrapper",
        "tls_client",
        "tls_client.sessions",
        "tls_client.cffi",
        "aiohttp",
        "aiohttp_socks",
        "aiodns",
        "pydantic",
        "pydantic_settings",
        "loguru",
        "watchfiles",
        "tenacity",
        "orjson",
        "yaml",
        "requests",
        "certifi",
        "playwright",
        "playwright.async_api",
        "playwright._impl",
        "greenlet",
        "yarl",
        "multidict",
        "charset_normalizer",
        "idna",
        "urllib3",
    ],
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
    name="BOL-BOT",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
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
    upx=False,
    upx_exclude=[],
    name="BOL-BOT",
)
