"""bol.com stock monitor via GraphQL (works when PDP HTML is Akamai-blocked)."""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import Any, Dict, Optional, Tuple

from src.models.product import ProductState, StockStatus
from src.utils.app_root import get_app_root
from src.utils.logging import get_logger

log = get_logger("bol.gql")

ROOT_DIR = get_app_root()
_gql_session_ready = False

# Persisted hashes from working monitor bots / product-web-fe HAR
HASH_PRODUCT = "19a9e78148968e88bb63ef930b33d63b788c66d287ae658c413fe670389bcce4"
HASH_RETAILER = "sha256:5c82e256f671fb54f6775707b4cf11a857243a01109e10130daf3bb0320cc3d4"
HASH_OFFER = "sha256:1463b809d2f60b211d3aa1ba11127a18025caf9b62677a413aca3d0a008d6c2c"
BOL_SELLER_NAME = "bol"
BOL_RETAILER_ID = "0"


def _require_bol_seller() -> bool:
    return os.environ.get("BOL_REQUIRE_BOL_SELLER", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _is_bol_seller(seller_name: Optional[str], seller_id: Optional[str]) -> bool:
    name = (seller_name or "").strip().casefold()
    sid = (seller_id or "").strip()
    return name == BOL_SELLER_NAME or sid == BOL_RETAILER_ID


def _offer_is_buyable(offer: Any) -> bool:
    """True only when bol GraphQL indicates the offer is actually orderable now."""
    if not isinstance(offer, dict):
        return False
    if offer.get("__typename") not in (None, "SellingOffer"):
        return False
    if offer.get("buyable") is True or offer.get("available") is True:
        return True
    if offer.get("buyable") is False or offer.get("available") is False:
        return False
    if offer.get("deliveredWithin48Hours") is True:
        return True
    bdo = offer.get("bestDeliveryOption") or {}
    if isinstance(bdo, dict):
        desc = (bdo.get("deliveryDescription") or "").lower()
        if "niet op voorraad" in desc or "uitverkocht" in desc:
            return False
        if "op voorraad" in desc:
            return True
        if "voor 23:59" in desc or "morgen in huis" in desc:
            return True
    return False


def _seller_from_offer(offer: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    retailer = offer.get("retailer") or {}
    if not isinstance(retailer, dict):
        return None, None
    seller_name = retailer.get("name")
    rid = retailer.get("id")
    seller_id = str(rid) if rid is not None else None
    return seller_name, seller_id


def _load_known_offer_uid(product_id: str) -> Optional[str]:
    """Env-only override — never use bol_credentials.json (wrong product / stale)."""
    return os.environ.get("BOL_OFFER_UID", "").strip() or None


def _info_from_offer(
    offer: Dict[str, Any],
    *,
    product_id: str,
    offer_uid: Optional[str],
) -> Dict[str, Any]:
    seller_name, seller_id = _seller_from_offer(offer)
    uid = offer_uid or offer.get("offerUid")
    return {
        "product_id": product_id,
        "offer_uid": uid,
        "seller_name": seller_name,
        "seller_id": seller_id,
        "is_bol": _is_bol_seller(seller_name, seller_id),
        "buyable": _offer_is_buyable(offer),
    }


def _offer_monitor_sync(
    session: Any,
    offer_uid: str,
    product_id: str,
    product_url: str,
    page_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    from src.bol.cart import _graphql

    referer = product_url or f"https://www.bol.com/nl/nl/p/-/{product_id}/"
    try:
        offer_data = _graphql(
            session,
            "Offer",
            HASH_OFFER,
            variables={"offerUid": offer_uid},
            page_id=page_id,
            label="monitor_offer",
            referer=referer,
            client_app="product-web-fe",
        )
    except Exception as exc:
        return None, str(exc)

    selling = offer_data.get("sellingOffer") or {}
    if not isinstance(selling, dict) or selling.get("__typename") != "SellingOffer":
        return {
            "product_id": product_id,
            "offer_uid": None,
            "seller_name": None,
            "seller_id": None,
            "is_bol": False,
            "buyable": False,
        }, None

    info = _info_from_offer(selling, product_id=product_id, offer_uid=offer_uid)
    if _require_bol_seller() and not info.get("is_bol"):
        try:
            seller_data = _graphql(
                session,
                "retailerInfo",
                HASH_RETAILER,
                variables={"offerUid": offer_uid},
                page_id=page_id,
                label="monitor_retailer",
                referer=referer,
                client_app="product-web-fe",
            )
            selling = seller_data.get("sellingOffer") or selling
            info = _info_from_offer(selling, product_id=product_id, offer_uid=offer_uid)
        except Exception as exc:
            log.debug(f"retailerInfo: {exc}")

    return info, None


def _gql_fetch_sync(
    product_id: str,
    product_url: str,
    proxy_url: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Returns (payload, error_message)."""
    from src.bol.login import ensure_session
    from src.bol.cart import _graphql, _init_session_holder, _prime_www

    global _gql_session_ready

    prev_proxy = os.environ.get("BOL_PROXY_URL")
    prev_no_proxy = os.environ.get("BOL_NO_PROXY")
    try:
        if proxy_url:
            os.environ["BOL_PROXY_URL"] = proxy_url
            os.environ.pop("BOL_NO_PROXY", None)
        else:
            os.environ.pop("BOL_PROXY_URL", None)
        session = ensure_session()
        if not _gql_session_ready:
            _init_session_holder(session)
            _gql_session_ready = True
        if not session.cookies.get("_abck"):
            from src.sites.akamai import (  # noqa: WPS433
                import_cookies_into_bol_token,
                login_txt_path,
                parse_login_txt_cookie_header,
            )

            txt = login_txt_path()
            if txt.is_file():
                imported = parse_login_txt_cookie_header(txt)
                if imported:
                    import_cookies_into_bol_token(imported, source="gql_monitor_startup")
            _prime_www(session)
        page_id = str(uuid.uuid4())
        referer = product_url or f"https://www.bol.com/nl/nl/p/-/{product_id}/"

        product_err = ""
        try:
            product_data = _graphql(
                session,
                "Product",
                HASH_PRODUCT,
                variables={"productId": product_id},
                page_id=page_id,
                label="monitor_product",
                referer=referer,
                client_app="product-web-fe",
            )
            product = product_data.get("product")
            if not isinstance(product, dict):
                return None, "product not found"

            best = product.get("bestSellingOffer")
            if not isinstance(best, dict):
                return {
                    "product_id": product_id,
                    "offer_uid": None,
                    "seller_name": None,
                    "seller_id": None,
                    "is_bol": False,
                    "buyable": False,
                }, None

            offer_uid = best.get("offerUid")
            info = _info_from_offer(best, product_id=product_id, offer_uid=offer_uid)

            if _require_bol_seller() and offer_uid and not info.get("is_bol"):
                try:
                    seller_data = _graphql(
                        session,
                        "retailerInfo",
                        HASH_RETAILER,
                        variables={"offerUid": offer_uid},
                        page_id=page_id,
                        label="monitor_retailer",
                        referer=referer,
                        client_app="product-web-fe",
                    )
                    selling = seller_data.get("sellingOffer") or best
                    info = _info_from_offer(
                        selling, product_id=product_id, offer_uid=offer_uid
                    )
                except Exception as exc:
                    log.debug(f"retailerInfo: {exc}")

            return info, None
        except Exception as exc:
            product_err = str(exc)
            if "persisted" not in product_err.lower():
                return None, product_err

        known_offer = _load_known_offer_uid(product_id)
        if known_offer:
            info, err = _offer_monitor_sync(
                session, known_offer, product_id, product_url, page_id
            )
            if info is not None:
                log.debug(
                    f"GraphQL monitor: Product APQ unavailable, using Offer "
                    f"fallback for {known_offer[:8]}…"
                )
                return info, None
            if err:
                return None, err

        return None, product_err or "Product GraphQL unavailable"
    except Exception as exc:
        return None, str(exc)
    finally:
        if prev_proxy is None:
            os.environ.pop("BOL_PROXY_URL", None)
        else:
            os.environ["BOL_PROXY_URL"] = prev_proxy
        if prev_no_proxy is None:
            os.environ.pop("BOL_NO_PROXY", None)
        else:
            os.environ["BOL_NO_PROXY"] = prev_no_proxy


def _state_from_gql(
    url: str, info: Dict[str, Any], *, blocked: bool = False, error: str = ""
) -> ProductState:
    if blocked:
        return ProductState(
            url=url,
            status=StockStatus.UNKNOWN,
            can_add_to_cart=False,
            http_status=403,
            error=error or "GraphQL blocked",
            raw={"akamai_block": True, "source": "graphql"},
        )
    offer_uid = info.get("offer_uid")
    if not offer_uid:
        return ProductState(
            url=url,
            status=StockStatus.ONLINE,
            can_add_to_cart=False,
            http_status=200,
            error="no offer in GraphQL — checking HTML PDP",
            raw={"source": "graphql", "offer_uid": None, "buyable": False},
        )
    if _require_bol_seller() and not info.get("is_bol"):
        seller = info.get("seller_name") or info.get("seller_id") or "unknown"
        return ProductState(
            url=url,
            status=StockStatus.OUT_OF_STOCK,
            can_add_to_cart=False,
            http_status=200,
            error=f"offer from seller {seller} (not bol)",
            raw={
                "source": "graphql",
                "offer_uid": offer_uid,
                "seller": seller,
                "buyable": False,
            },
        )
    if not info.get("buyable"):
        return ProductState(
            url=url,
            status=StockStatus.ONLINE,
            can_add_to_cart=False,
            http_status=200,
            error="offer listed — waiting for cart button (pre-release / not buyable yet)",
            raw={
                "source": "graphql",
                "offer_uid": offer_uid,
                "buyable": False,
            },
        )
    return ProductState(
        url=url,
        status=StockStatus.IN_STOCK,
        can_add_to_cart=True,
        http_status=200,
        raw={
            "source": "graphql",
            "offer_uid": offer_uid,
            "seller_name": info.get("seller_name"),
            "seller_id": info.get("seller_id"),
            "buyable": True,
        },
    )


async def fetch_state_via_graphql(
    product_id: str,
    product_url: str,
    proxy_url: Optional[str] = None,
) -> Optional[ProductState]:
    """
    GraphQL Product + optional retailerInfo — same approach as external monitor bots.
    Returns None if GraphQL failed entirely (caller should fall back to HTML).
    """
    info, err = await asyncio.to_thread(
        _gql_fetch_sync, product_id, product_url, proxy_url
    )
    url = product_url or f"https://www.bol.com/nl/nl/p/-/{product_id}/"
    if info is not None:
        state = _state_from_gql(url, info)
        if state.is_available:
            log.info(
                f"GraphQL monitor: IN STOCK offerUid={info.get('offer_uid')} "
                f"seller={info.get('seller_name') or 'bol'}"
            )
        elif info.get("offer_uid"):
            log.debug(
                f"GraphQL monitor: offer present but not buyable "
                f"({state.error or 'out of stock'})"
            )
        else:
            log.info("GraphQL monitor: stock out (no bestSellingOffer)")
        return state

    if err and any(
        x in err.lower()
        for x in ("403", "429", "blocked", "stub", "text/plain")
    ):
        log.debug(f"GraphQL monitor failed: {err[:200]}")
        return _state_from_gql(url, {}, blocked=True, error=err[:300])
    log.debug(f"GraphQL monitor unavailable: {err}")
    return None
