from __future__ import annotations

import sys

from src.utils.logging import get_logger

log = get_logger("http_backend")


def log_bol_http_backends() -> None:
    """Log which Akamai-bypass backends are available (especially in frozen exe)."""
    curl_ok = False
    tls_ok = False
    try:
        from curl_cffi import requests as _curl  # noqa: F401

        curl_ok = True
    except Exception as exc:
        log.warning(f"curl_cffi unavailable: {exc}")

    try:
        import tls_client

        sess = tls_client.Session(client_identifier="chrome_120")
        tls_ok = sess is not None
    except Exception as exc:
        log.warning(f"tls_client unavailable: {exc}")

    if curl_ok and tls_ok:
        log.info("Akamai bypass: tls_client + curl_cffi loaded")
    elif tls_ok:
        log.info("Akamai bypass: tls_client loaded (curl_cffi missing)")
    elif curl_ok:
        log.info("Akamai bypass: curl_cffi loaded (tls_client missing)")
    else:
        msg = (
            "Akamai bypass backends MISSING — bol.com requests will be blocked. "
            "Rebuild with scripts/build_exe.py."
        )
        if getattr(sys, "frozen", False):
            log.error(msg)
        else:
            log.warning(msg)
