import hashlib
import json
import sys

ROOT = __file__.rsplit("\\", 1)[0]
sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT + "/..")

from bol_login import ensure_session
from bol_cart import (
    _init_session_holder,
    _get_curl_session,
    _gql_headers,
    GRAPHQL_URL,
    _merge_cookies_from_response,
)


def pq_hash(query: str) -> str:
    return "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest()


QUERIES = [
    "query Basket { basket { id } }",
    "query Basket { basket { id totalQuantity } }",
    "query Basket { basket { id items { id } } }",
    "query GetBasket { basket { id } }",
    "query basket { basket { id } }",
    "query BasketQuery { basket { id } }",
    "query { basket { id } }",
    "mutation AddItem($input: AddItemInput!) { basket { addItem(input: $input) { id } } }",
]


def gql(s, op: str, query: str, variables: dict | None = None) -> dict:
    cs = _get_curl_session(s)
    headers = _gql_headers("https://www.bol.com/nl/nl/basket/")
    xsrf = s.cookies.get("XSRF-TOKEN")
    if xsrf:
        headers["x-xsrf-token"] = str(xsrf)
    body = {
        "operationName": op,
        "variables": variables or {},
        "query": query,
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": pq_hash(query)}},
    }
    r = cs.post(GRAPHQL_URL, json=body, headers=headers, timeout=30)
    _merge_cookies_from_response(r, s)
    return r.json()


def main() -> None:
    s = ensure_session()
    _init_session_holder(s)
    for q in QUERIES:
        op = q.split()[1]
        try:
            data = gql(s, op, q)
            if data.get("data"):
                print("OK", op, json.dumps(data["data"])[:300])
            else:
                print("ERR", op, data.get("errors"))
        except Exception as exc:
            print("FAIL", op, exc)


if __name__ == "__main__":
    main()
