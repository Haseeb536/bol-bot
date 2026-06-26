import json
import sys

ROOT = __file__.rsplit("\\", 1)[0]
sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT + "/..")

from bol_login import ensure_session
from bol_cart import _init_session_holder, _get_curl_session, _gql_headers, GRAPHQL_URL, _merge_cookies_from_response

QUERIES = [
    ("Basket", "query Basket { basket { id totalQuantity items { quantity } } }"),
    ("GetBasket", "query GetBasket { basket { id } }"),
    ("Cart", "query Cart { cart { id } }"),
    (
        "CreateBasket",
        "mutation CreateBasket { basket { createBasket { id } } }",
    ),
]


def main() -> None:
    s = ensure_session()
    _init_session_holder(s)
    cs = _get_curl_session(s)
    headers = _gql_headers("https://www.bol.com/nl/nl/basket/")
    xsrf = s.cookies.get("XSRF-TOKEN")
    if xsrf:
        headers["x-xsrf-token"] = xsrf

    for op, q in QUERIES:
        body = {"operationName": op, "variables": {}, "query": q}
        r = cs.post(GRAPHQL_URL, json=body, headers=headers, timeout=30)
        _merge_cookies_from_response(r, s)
        print(op, r.status_code)
        try:
            print(json.dumps(r.json(), ensure_ascii=False)[:600])
        except Exception:
            print(r.text[:200])


if __name__ == "__main__":
    main()
