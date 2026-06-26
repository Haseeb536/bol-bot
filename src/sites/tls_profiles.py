"""
TLS fingerprint profiles for bol.com Akamai bypass.

Akamai Bot Manager inspects:
  - JA3/JA4 TLS fingerprint (cipher suites, extensions, elliptic curves)
  - HTTP/2 SETTINGS frame (exact values + order)
  - HTTP/2 pseudo-header order (:method :authority :scheme :path)
  - Request header order (Akamai _abck sensor fingerprints this)
  - sec-ch-ua / Client Hints (browser identity)

These profiles match Chrome 120 exactly so Akamai scores the request as a
real browser, not a bot/scraper.
"""

from __future__ import annotations

BOL_HEADERS_NL = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "accept-language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "accept-encoding": "gzip, deflate, br",
    "upgrade-insecure-requests": "1",
    "sec-fetch-site": "none",
    "sec-fetch-mode": "navigate",
    "sec-fetch-user": "?1",
    "sec-fetch-dest": "document",
    "cache-control": "max-age=0",
}

# Header order matters — Akamai's _abck sensor fingerprints request header ordering.
# This matches Chrome 120's exact wire order for a navigation request.
BOL_HEADER_ORDER = [
    "cache-control",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "upgrade-insecure-requests",
    "user-agent",
    "accept",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-user",
    "sec-fetch-dest",
    "accept-encoding",
    "accept-language",
    "cookie",
]

TLS_PROFILES = {
    "chrome_120": {
        # HTTP/2 SETTINGS frame — exact Chrome 120 values (order matters too)
        "h2_settings": {
            "HEADER_TABLE_SIZE": 65536,
            "ENABLE_PUSH": 0,
            "INITIAL_WINDOW_SIZE": 6291456,
            "MAX_HEADER_LIST_SIZE": 262144,
        },
        "h2_settings_order": [
            "HEADER_TABLE_SIZE",
            "ENABLE_PUSH",
            "INITIAL_WINDOW_SIZE",
            "MAX_HEADER_LIST_SIZE",
        ],
        # TLS ClientHello signature algorithms — Chrome 120 exact list
        "supported_signature_algorithms": [
            "ecdsa_secp256r1_sha256",
            "ecdsa_secp384r1_sha384",
            "ecdsa_secp521r1_sha512",
            "rsa_pss_rsae_sha256",
            "rsa_pss_rsae_sha384",
            "rsa_pss_rsae_sha512",
            "rsa_pss_pss_sha256",
            "rsa_pss_pss_sha384",
            "rsa_pss_pss_sha512",
            "rsa_pkcs1_sha256",
            "rsa_pkcs1_sha384",
            "rsa_pkcs1_sha512",
            "0x402",
            "0x303",
            "0x301",
            "0x302",
            "ecdsa_sha1",
            "rsa_pkcs1_sha1",
            "0x202",
        ],
        # HTTP/2 pseudo-header order — Chrome 120 sends them in this exact order
        "pseudo_header_order": [":method", ":authority", ":scheme", ":path"],
        # Request header order for Akamai fingerprinting
        "header_order": BOL_HEADER_ORDER,
    }
}
