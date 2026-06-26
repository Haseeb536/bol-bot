"""One-off probe for bol basket id — delete after use."""
import json
import re
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
sys.path.insert(0, __file__.rsplit("\\", 1)[0] + "/..")

from bol_login import ensure_session
from bol_cart import _page_get, _init_session_holder, _graphql

UUID_RE = re.compile(
    r'"(?:basketId|id)"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
    re.I,
)


def main() -> None:
    s = ensure_session()
    _init_session_holder(s)
    product = (
        "https://www.bol.com/nl/nl/p/pokemon-tcg-mega-evolution-perfect-order-booster-10-kaarten-per-pakje/9300000271683065/"
    )
    r = _page_get(s, product, referer="https://www.bol.com/nl/nl/")
    print("product", r.status_code, len(r.text))
    for kw in ("basketId", "currentBasket", '"basket"', "shoppingBasket"):
        print(f"  {kw}:", r.text.count(kw))
    m = re.search(r"__NEXT_DATA__", r.text)
    print("  __NEXT_DATA__:", bool(m))
    if m:
        start = r.text.find("__NEXT_DATA__")
        print(r.text[start : start + 200])
    for url in (
        "https://www.bol.com/nl/rnwy/account/basket",
        "https://www.bol.com/nl/nl/basket/",
    ):
        r = _page_get(s, url, referer="https://www.bol.com/nl/nl/")
        print(url, r.status_code, len(r.text))
        ids = UUID_RE.findall(r.text)
        if ids:
            print("  uuids:", ids[:5])
        m = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>',
            r.text,
            re.I,
        )
        if m:
            blob = m.group(1)
            for pat in (
                r'"basket"\s*:\s*\{[^{}]*"id"\s*:\s*"([^"]+)"',
                r'"basketId"\s*:\s*"([^"]+)"',
                r'"currentBasket"\s*:\s*\{[^{}]*"id"\s*:\s*"([^"]+)"',
            ):
                m2 = re.search(pat, blob)
                if m2:
                    print("  next_data:", pat[:40], m2.group(1))
            if '"items"' in blob:
                print("  next_data has items key")
            idx = blob.find('"basket"')
            if idx >= 0:
                print("  basket snippet:", blob[idx : idx + 400])

    hashes = [
        ("Basket", "sha256:4e9e3ba7db4b876cfa4af8e5a2f1741fc03e3b892b8e1c6a3b5a8a20f6c7d9e2", {}),
        ("Basket", "sha256:71c1f026bbdeae61770a43353581a99ffcf53584fb5bfc127c0211311d56e108", {}),
        ("BasketOverview", "sha256:71c1f026bbdeae61770a43353581a99ffcf53584fb5bfc127c0211311d56e108", {}),
        ("Cart", "sha256:71c1f026bbdeae61770a43353581a99ffcf53584fb5bfc127c0211311d56e108", {}),
    ]
    for op, h, vars_ in hashes:
        try:
            d = _graphql(s, op, h, vars_, label=f"probe_{op}")
            print(op, json.dumps(d, ensure_ascii=False)[:500])
        except Exception as exc:
            print(op, "ERR", exc)


if __name__ == "__main__":
    main()
