from __future__ import annotations

import sys
from pathlib import Path


def get_app_root() -> Path:
    """Project root in dev; folder containing the .exe when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    main = sys.modules.get("__main__")
    main_file = getattr(main, "__file__", None) if main else None
    if main_file and not str(main_file).startswith("<"):
        root = Path(main_file).resolve().parent
        if root.is_dir():
            return root
    here = Path(__file__)
    if not str(here).startswith("<"):
        return here.resolve().parents[2]
    return Path.cwd()


def get_bundle_root() -> Path:
    """PyInstaller extract dir (_MEIPASS) when onefile/onedir; else app root."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", get_app_root()))
    return get_app_root()


def configure_playwright_browsers() -> Path | None:
    """
    Point Playwright at bundled Chromium inside the PyInstaller _internal folder.
    Must run before async_playwright().start().
    """
    import os

    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0":
        bundled = _playwright_local_browsers_dir()
        return bundled if bundled and bundled.is_dir() else None

    explicit = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if explicit and explicit != "0":
        return Path(explicit)

    bundled = _playwright_local_browsers_dir()
    if bundled and bundled.is_dir() and any(bundled.iterdir()):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
        return bundled

    # Legacy release layout (playwright-browsers next to exe)
    legacy = get_app_root() / "playwright-browsers"
    if legacy.is_dir() and any(legacy.iterdir()):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(legacy.resolve())
        return legacy
    return None


def _playwright_local_browsers_dir() -> Path:
    """Where frozen Playwright looks when PLAYWRIGHT_BROWSERS_PATH=0."""
    return (
        get_bundle_root()
        / "playwright"
        / "driver"
        / "package"
        / ".local-browsers"
    )


def ensure_bol_scripts_path() -> None:
    """Legacy no-op — bol modules live in src.bol and are imported directly."""


def bol_cart_script_path() -> Path:
    """Legacy path helper — cart logic is in src.bol.cart (use main.py --bol-cart)."""
    return get_app_root() / "main.py"
