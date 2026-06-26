import json
import re
import sys

ROOT = __file__.rsplit("\\", 1)[0]
sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT + "/..")

from bol_login import ensure_session, COOKIE_FILE, _load_json_file
from bol_cart import (
    _init_session_holder,
    _page_get,
    _get_curl_session,
    _gql_headers,
    GRAPHQL_URL,
    HASH_ADD_ITEM,
    _merge_cookies_from_response,
)

# Candidate persisted-query operations to resolve basket id (hash, operation, variables)
CANDIDATES = [
    ("sha256:4e9e3ba7db4b876cfa4af8e5a2f1741fc03e3b892b8e1c6a3b5a8a20f6c7d9e2", "Basket", {}),
    ("sha256:7b4e488cbdaa08363d9ef5e879198267fe67ac501844208f618eeefd234588d8", "Basket", {}),
    ("sha256:311d598ff2df0d03c3703f99379c43166e87a53420150bf3af7c09beffd17abf", "Basket", {}),
    ("sha256:1463b809d2f60b211d3aa1ba11127a18025caf9b62677a413aca3d0a008d6c2c", "Basket", {}),
    ("sha256:71c1f026bbdeae61770a43353581a99ffcf53584fb5bfc127c0211311d56e108", "Basket", {}),
    ("sha256:0dcf32dfa29a4af579f0732b24ea3647fc38ec6d2cc4dd7ca0f7490d947103a5", "Basket", {}),
    ("sha256:9de0c385c149e6e5df7d1b8c7974ea99f7bdba148734f66c339592dbb6396227", "GetBasket", {}),
    ("sha256:4e9e3ba7db4b876cfa4af8e5a2f1741fc03e3b892b8e1c6a3b5a8a20f6c7d9e2", "GetOrCreateBasket", {}),
    ("sha256:4e9e3ba7db4b876cfa4af8e5a2f1741fc03e3b892b8e1c6a3b5a8a20f6c7d9e2", "EnsureBasket", {}),
    ("sha256:4e9e3ba7db4b876cfa4af8e5a2f1741fc03e3b892b8e1c6a3b5a8a20f6c7d9e2", "BasketOverview", {}),
    ("sha256:4e9e3ba7db4b876cfa4af8e5a2f1741fc03e3b892b8e1c6a3b5a8a20f6c7d9e2", "ShoppingBasket", {}),
]


def _xsrf(session) -> str:
    val = ""
    for c in session.cookies:
        if c.name == "XSRF-TOKEN":
            val = c.value
    return val


def gql(session, op: str, h: str, variables: dict) -> dict:
    cs = _get_curl_session(session)
    headers = _gql_headers("https://www.bol.com/nl/nl/basket/")
    token = _xsrf(session)
    if token:
        headers["x-xsrf-token"] = token
    body = {
        "operationName": op,
        "variables": variables,
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": h}},
    }
    r = cs.post(GRAPHQL_URL, json=body, headers=headers, timeout=30)
    _merge_cookies_from_response(r, session)
    return r.json()


def find_basket_id_in_obj(obj, found: list) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("id", "basketId") and isinstance(v, str) and re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", v
            ):
                found.append(v)
            find_basket_id_in_obj(v, found)
    elif isinstance(obj, list):
        for item in obj:
            find_basket_id_in_obj(item, found)


def main() -> None:
    s = ensure_session()
    _init_session_holder(s)

    print("=== GraphQL candidates ===")
    for h, op, vars_ in CANDIDATES:
        try:
            data = gql(s, op, h, vars_)
            if data.get("errors"):
                err = data["errors"][0].get("message", "")
                if err not in ("PersistedQueryNotFound", "PersistedOperationNotFound"):
                    print(op, h[:20], err)
            elif data.get("data"):
                found: list = []
                find_basket_id_in_obj(data["data"], found)
                print("HIT", op, found, json.dumps(data["data"])[:200])
        except Exception as exc:
            print(op, "exc", exc)

    print("=== AddItem without basketId ===")
    cs = _get_curl_session(s)
    headers = _gql_headers(
        "https://www.bol.com/nl/nl/p/pokemon-tcg-mega-evolution-perfect-order-booster-10-kaarten-per-pakje/9300000271683065/"
    )
    token = _xsrf(s)
    if token:
        headers["x-xsrf-token"] = token
    body = {
        "operationName": "AddItem",
        "variables": {
            "input": {
                "offerUid": "0f0afd14-0b89-4d26-b8f3-2e5f8cd53c9f",
                "productId": "9300000271683065",
                "quantity": 1,
            }
        },
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": HASH_ADD_ITEM}},
    }
    r = cs.post(GRAPHQL_URL, json=body, headers=headers, timeout=30)
    print(json.dumps(r.json(), indent=2)[:800])

    meta = _load_json_file(COOKIE_FILE)
    print("shopping_session_id", (meta.get("cookies") or {}).get("shopping_session_id", "")[:40])


if __name__ == "__main__":
    main()
