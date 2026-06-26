from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.utils._licence_data import BUILD_UNIX, EXPIRY_ENCODED, TRIAL_DAYS

# XOR key baked into the exe — expiry timestamp stored encoded at build time.
_DECODE_KEY = 0x5A3C91E7
# Allow small clock skew / DST adjustments (seconds).
_CLOCK_TOLERANCE_SEC = 7200


def _expiry_unix() -> int:
    if not EXPIRY_ENCODED:
        return 0
    return int(EXPIRY_ENCODED) ^ _DECODE_KEY


def _hwm_path() -> Path:
    from src.utils.app_root import get_app_root

    path = get_app_root() / "data" / ".trial_clock_hwm"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_hwm() -> int:
    path = _hwm_path()
    if not path.is_file():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip()) ^ _DECODE_KEY
    except (OSError, ValueError):
        return 0


def _write_hwm(ts: int) -> None:
    path = _hwm_path()
    try:
        path.write_text(str(int(ts) ^ _DECODE_KEY), encoding="utf-8")
    except OSError:
        pass


def _fmt_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def enforce_licence() -> None:
    """
    Hard 14-day trial for frozen exe only.

    Expiry is compiled into BOL-BOT.exe at build time. Editing LICENCE-INFO.txt,
    bol_token.json, or any file in the release folder cannot extend or bypass it.
    """
    if not getattr(sys, "frozen", False):
        return

    expiry = _expiry_unix()
    if not expiry:
        _die("This build has no licence data. Rebuild with scripts/build_exe.py.")

    now = int(time.time())

    if BUILD_UNIX and now < BUILD_UNIX - _CLOCK_TOLERANCE_SEC:
        _die(
            "System clock is set before this build was created.\n"
            f"Build date: {_fmt_utc(BUILD_UNIX)}.\n"
            "Trial cannot run with an incorrect system clock."
        )

    if now >= expiry:
        built = _fmt_utc(BUILD_UNIX) if BUILD_UNIX else "unknown"
        _die(
            f"BOL-BOT trial expired on {_fmt_utc(expiry)} ({TRIAL_DAYS}-day limit).\n"
            f"Build date: {built}.\n"
            "This executable will not run after the trial period — "
            "no setting, file edit, or reinstall can extend it.\n"
            "Contact the distributor for a new build."
        )

    hwm = _read_hwm()
    if hwm and now < hwm - _CLOCK_TOLERANCE_SEC:
        _die(
            "System clock rollback detected — trial integrity check failed.\n"
            f"Last verified run: {_fmt_utc(hwm)}.\n"
            "Setting the PC clock backwards does not extend the trial."
        )

    if now > hwm:
        _write_hwm(now)


def _die(message: str) -> None:
    print(f"\n[licence] {message}\n", file=sys.stderr)
    raise SystemExit(1)
