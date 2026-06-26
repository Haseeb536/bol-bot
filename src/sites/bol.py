from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import aiohttp

from src.models.product import ProductState, StockStatus
from src.models.session import CartResult, CheckoutResult
from src.models.task import MonitorMode, ProfileConfig, TaskConfig
from src.monitors.detector import DetectionContext, ProductDetector
from src.sites.base import SiteAdapter
from src.sites.akamai import is_readable_product_page
from src.sites.bol_session import fetch_product_page, load_basket_id
from src.sites.bol_urls import resolve_product_url
from src.utils.logging import get_logger

log = get_logger("bol")

from src.utils.app_root import get_app_root

ROOT_DIR = get_app_root()
_OFFER_UID_RE = re.compile(
    r'"offerUid"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
    re.I,
)


class BolSiteAdapter(SiteAdapter):
    """bol.com — HTML monitoring + in-process cart for reliable ATC."""

    name = "bol"

    def __init__(self) -> None:
        self._cached_pdp_html: Optional[str] = None
        self._cached_offer_uid: Optional[str] = None
        self._last_poll_proxy: Optional[str] = None
        self._http_checkout_cache: Optional[dict] = None

    def clear_http_checkout_cache(self) -> None:
        self._http_checkout_cache = None

    def _resolve_offer_uid(self, task: TaskConfig) -> str:
        from src.bol.login import _load_json_file

        creds = _load_json_file(str(ROOT_DIR / "bol_credentials.json")) or {}
        return (
            (self._cached_offer_uid or "").strip()
            or os.environ.get("BOL_OFFER_UID", "").strip()
            or str(task.metadata.get("offer_uid") or creds.get("offer_uid") or "")
        )

    @staticmethod
    def configure_checkout_proxy(proxy_url: Optional[str]) -> None:
        """Keep checkout on the same IP as ATC (proxy session + bol_token cookies)."""
        if proxy_url:
            os.environ["BOL_PROXY_URL"] = proxy_url
            os.environ.pop("BOL_NO_PROXY", None)
        else:
            os.environ.setdefault("BOL_NO_PROXY", "1")
            os.environ.pop("BOL_PROXY_URL", None)

    def _product_id(self, url: str) -> Optional[str]:
        m = re.search(r"/(\d{10,})/?", url)
        return m.group(1) if m else None

    @staticmethod
    def _embedded_availability(html: str) -> Optional[Tuple[StockStatus, bool]]:
        """Parse bol PDP embedded JSON — only trust bestSellingOffer, not marketplace noise."""
        best = re.search(
            r'"bestSellingOffer"\s*:\s*\{[\s\S]{0,1600}?\}',
            html,
            re.I,
        )
        if best:
            chunk = best.group(0)
            if re.search(r'"buyable"\s*:\s*true', chunk, re.I):
                return StockStatus.IN_STOCK, True
            if re.search(r'"buyable"\s*:\s*false', chunk, re.I):
                # Pre-drop: offer listed on live PDP but cart not open yet → watch fast
                return StockStatus.ONLINE, False
            if re.search(r'"availability"\s*:\s*"InStock"', chunk, re.I):
                return StockStatus.IN_STOCK, True
            if re.search(r'"availability"\s*:\s*"OutOfStock"', chunk, re.I):
                return StockStatus.ONLINE, False
            if re.search(r'"deliveredWithin48Hours"\s*:\s*true', chunk, re.I):
                return StockStatus.IN_STOCK, True
            if re.search(r'"deliveredWithin48Hours"\s*:\s*false', chunk, re.I):
                return StockStatus.ONLINE, False
            return StockStatus.ONLINE, False

        if re.search(r'"buyable"\s*:\s*false', html, re.I):
            return StockStatus.ONLINE, False
        if re.search(r'"availability"\s*:\s*"OutOfStock"', html, re.I):
            return StockStatus.ONLINE, False
        return None

    @classmethod
    def _extract_offer_uid(cls, html: str, product_id: str) -> Optional[str]:
        best = re.search(
            r'"bestSellingOffer"\s*:\s*\{[\s\S]{0,2500}?\}',
            html,
            re.I,
        )
        if best:
            m = re.search(
                r'"offerUid"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
                best.group(0),
                re.I,
            )
            if m:
                return m.group(1).lower()
        pid = re.escape(product_id)
        for pat in (
            rf"/{pid}/\?offerUid=([0-9a-f-]{{36}})",
            rf"productId={pid}(?:&amp;|&)offerUid=([0-9a-f-]{{36}})",
            rf"productId={pid}[^\"'<>]{{0,160}}offerUid=([0-9a-f-]{{36}})",
        ):
            m = re.search(pat, html, re.I)
            if m:
                return m.group(1).lower()
        idx = html.find(product_id)
        if idx >= 0:
            chunk = html[max(0, idx - 250) : idx + 250]
            m = re.search(r"offerUid=([0-9a-f-]{36})", chunk, re.I)
            if m:
                return m.group(1).lower()
        m2 = _OFFER_UID_RE.search(html)
        return m2.group(1).lower() if m2 else None

    def _cache_readable_pdp(
        self,
        html: str,
        product_id: str,
        *,
        proxy_url: Optional[str] = None,
    ) -> None:
        if len(html) < 5000:
            return
        self._cached_pdp_html = html
        if proxy_url:
            self._last_poll_proxy = proxy_url
        embedded = self._embedded_availability(html)
        uid = self._extract_offer_uid(html, product_id)
        if uid:
            self._cached_offer_uid = uid
            if embedded and embedded[0] == StockStatus.IN_STOCK and embedded[1]:
                log.info(f"Cached offerUid from PDP: {uid}")
            else:
                log.debug(f"Cached offerUid from PDP (pre-buyable): {uid[:8]}…")

    def _state_from_html(self, url: str, http_status: int, text: str) -> ProductState:
        pid = self._product_id(url)
        from src.sites.akamai import (
            is_product_placeholder_block,
            is_readable_product_page,
        )

        if is_product_placeholder_block(text, http_status, pid, url):
            return ProductState(
                url=url,
                status=StockStatus.OFFLINE,
                can_add_to_cart=False,
                http_status=http_status,
                error=(
                    "Product page not public yet (403 placeholder). "
                    "Monitor will detect when the listing goes live."
                ),
                raw={"placeholder": True, "html_len": len(text)},
            )

        from src.sites.akamai import is_akamai_challenge_page

        if is_akamai_challenge_page(text, http_status) or not is_readable_product_page(
            text, http_status, pid
        ):
            from src.sites.bol_urls import is_placeholder_product_url

            msg = (
                "Akamai block — refresh login.txt from Chrome on this exact product URL "
                "(DevTools → Cookie header on www.bol.com). Cookies must match monitor IP "
                "(export while using the same proxy if BOL uses RoundProxies)."
            )
            if is_placeholder_product_url(url):
                msg = (
                    "Akamai block — use the full slug product URL in tasks.yaml, "
                    "then import Chrome cookies (login.txt)."
                )
            return ProductState(
                url=url,
                status=StockStatus.UNKNOWN,
                can_add_to_cart=False,
                http_status=403 if http_status == 200 else http_status,
                error=msg,
                raw={"akamai_block": True, "html_len": len(text)},
            )
        if http_status in (403, 429):
            return ProductState(
                url=url,
                status=StockStatus.UNKNOWN,
                can_add_to_cart=False,
                http_status=http_status,
                error="blocked — import Chrome cookies (login.txt) for this product URL",
                raw={"akamai_block": True},
            )
        if http_status in (404, 410):
            return ProductState(
                url=url,
                status=StockStatus.OFFLINE,
                can_add_to_cart=False,
                http_status=http_status,
                raw={"offline": True},
            )
        embedded = self._embedded_availability(text)
        if embedded:
            status, can_atc = embedded
            return ProductState(
                url=url,
                status=status,
                can_add_to_cart=can_atc,
                http_status=http_status,
                raw={"source": "embedded_json"},
            )
        ctx = DetectionContext(
            url=url,
            http_status=http_status,
            body=text,
            content_type="text/html",
        )
        return ProductDetector.from_http(ctx)

    async def fetch_state(
        self,
        session: aiohttp.ClientSession,
        task: TaskConfig,
        proxy_url: Optional[str],
        *,
        cached_html: Optional[str] = None,
        cached_status: Optional[int] = None,
    ) -> ProductState:
        pid = (
            str(task.metadata.get("product_id") or "").strip()
            or self._product_id(str(task.product_url))
            or ""
        )
        monitor_url = resolve_product_url(
            pid, str(task.product_url), task.metadata
        )
        fallback_url = monitor_url or str(task.product_url)

        # ── Fast path: caller (engine) already has a live Playwright response ──
        if cached_html and cached_status is not None:
            state = self._state_from_html(monitor_url, cached_status, cached_html)
            if is_readable_product_page(cached_html, cached_status, pid or None):
                log.info(f"fetch_state: using cached Playwright HTML ({len(cached_html)} chars)")
                if pid:
                    self._cache_readable_pdp(cached_html, pid, proxy_url=proxy_url)
                return state

        use_gql_first = task.monitor_mode in (
            MonitorMode.API_FIRST,
            MonitorMode.BROWSER_FALLBACK,
        )
        if use_gql_first and pid:
            from src.sites.bol_monitor_gql import fetch_state_via_graphql

            gql_state = await fetch_state_via_graphql(pid, monitor_url, proxy_url)
            if gql_state is not None and not gql_state.raw.get("akamai_block"):
                uid = gql_state.raw.get("offer_uid")
                if uid:
                    self._cached_offer_uid = str(uid)
                if gql_state.is_available:
                    self._last_poll_proxy = proxy_url
                    if not self._cached_pdp_html:
                        try:
                            http_status, text = await fetch_product_page(
                                monitor_url, proxy_url=proxy_url
                            )
                            if pid and is_readable_product_page(
                                text, http_status, pid
                            ):
                                self._cache_readable_pdp(
                                    text, pid, proxy_url=proxy_url
                                )
                        except Exception as warm_exc:
                            log.debug(
                                f"PDP warm after GQL buyable: {warm_exc}"
                            )
                    return gql_state
            elif gql_state is not None and gql_state.raw.get("akamai_block"):
                log.debug(
                    "GraphQL blocked — falling back to tls_client/curl product page"
                )

        try:
            http_status, text = await fetch_product_page(
                monitor_url, proxy_url=proxy_url
            )
            if pid and is_readable_product_page(text, http_status, pid):
                self._cache_readable_pdp(text, pid, proxy_url=proxy_url)
            return self._state_from_html(monitor_url, http_status, text)
        except Exception as exc:
            log.debug(f"Product page fetch failed: {exc}")
            return ProductState(url=fallback_url, status=StockStatus.UNKNOWN, error=str(exc))


    async def add_to_cart(
        self,
        session: aiohttp.ClientSession,
        task: TaskConfig,
        profile: ProfileConfig,
        proxy_url: Optional[str],
    ) -> CartResult:
        pid = (
            str(task.metadata.get("product_id") or "").strip()
            or self._product_id(str(task.product_url))
            or ""
        )
        if not pid:
            return CartResult(success=False, message="Missing bol product_id")

        # Resolve offerUid from the live product page the monitor just read — never from
        # pasted config (quantity alone must not be mistaken for offerUid).
        offer_uid = self._cached_offer_uid or ""
        if not offer_uid and self._cached_pdp_html:
            offer_uid = self._extract_offer_uid(self._cached_pdp_html, pid) or ""
            if offer_uid:
                self._cached_offer_uid = offer_uid

        max_units = max(
            1,
            int(
                task.metadata.get("max_units_per_item")
                or os.environ.get("BOL_MAX_UNITS_PER_ITEM", "2")
                or 2
            ),
        )
        max_items = max(
            1,
            int(
                task.metadata.get("max_items_per_checkout")
                or os.environ.get("BOL_MAX_ITEMS_PER_CHECKOUT", "4")
                or 4
            ),
        )
        atc_qty = min(max(1, int(task.quantity or 1)), max_units)
        self._last_atc_quantity = atc_qty

        env = os.environ.copy()
        env["BOL_AUTO_CART"] = "1"
        env["BOL_USE_MAX_QUANTITY"] = "1"
        env["BOL_QUANTITY"] = str(atc_qty)
        env["BOL_MAX_UNITS_PER_ITEM"] = str(max_units)
        env["BOL_MAX_ITEMS_PER_CHECKOUT"] = str(max_items)
        env["BOL_PAYMENT_METHOD"] = (task.payment_method or "ideal").strip().lower()
        env.setdefault("BOL_SKIP_WARM", "1")
        pid_meta = str(task.metadata.get("product_id") or pid or "")
        env["BOL_PRODUCT_URL"] = resolve_product_url(
            pid_meta, str(task.product_url), task.metadata
        )
        if self._cached_pdp_html and len(self._cached_pdp_html) > 5000:
            html_path = Path(tempfile.gettempdir()) / f"bol_pdp_{pid}.html"
            html_path.write_text(self._cached_pdp_html, encoding="utf-8")
            env["BOL_PRODUCT_HTML_FILE"] = str(html_path)
            log.info(f"Passing cached PDP HTML to bol_cart ({len(self._cached_pdp_html)} chars)")
        if not offer_uid:
            offer_uid = self._cached_offer_uid or ""
        if offer_uid:
            env["BOL_OFFER_UID"] = offer_uid
            log.info(f"offerUid from product page: {offer_uid}")
            if not self._cached_pdp_html:
                env["BOL_SKIP_PRODUCT_PAGE"] = "1"
        else:
            log.warning(
                "No offerUid in cached PDP — bol_cart will extract from product page"
            )
        cart_proxy = self._last_poll_proxy or proxy_url
        cart_no_proxy = os.environ.get("BOL_CART_USE_PROXY", "").strip().lower() in {
            "0",
            "false",
            "no",
        }
        if cart_proxy and not cart_no_proxy:
            env["BOL_PROXY_URL"] = cart_proxy
            env.pop("BOL_NO_PROXY", None)
            env.pop("BOL_PROXY_FALLBACK_URL", None)
            log.info(f"bol_cart using monitor proxy ({cart_proxy.split('@')[-1][:40]})")
        else:
            env["BOL_NO_PROXY"] = "1"
            env.pop("BOL_PROXY_URL", None)
            env.pop("BOL_USE_PROXY_FALLBACK", None)
            if cart_proxy:
                env["BOL_PROXY_FALLBACK_URL"] = cart_proxy
            log.info("bol_cart using home IP (no proxy) for cart GraphQL")
        if self._cached_pdp_html:
            try:
                from src.bol.cart import _dehydrated_ctx_from_html

                ctx = _dehydrated_ctx_from_html(self._cached_pdp_html)
                if ctx.get("page_id"):
                    env["BOL_PAGE_ID"] = ctx["page_id"]
            except Exception:
                pass

        log.info(f"Running add-to-cart for product {pid}")

        def _run_cart() -> tuple[str, int]:
            import io
            from contextlib import redirect_stderr, redirect_stdout

            from src.bol.cart import main as cart_main

            buf = io.StringIO()
            bol_keys = [k for k in os.environ if k.startswith("BOL_")]
            saved_bol = {k: os.environ[k] for k in bol_keys}
            try:
                for k in bol_keys:
                    os.environ.pop(k, None)
                os.environ.update(env)
                with redirect_stdout(buf), redirect_stderr(buf):
                    cart_main(argv=[str(pid)])
                return buf.getvalue(), 0
            except RuntimeError:
                return buf.getvalue(), 1
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                return buf.getvalue(), code
            finally:
                for k in list(os.environ):
                    if k.startswith("BOL_"):
                        os.environ.pop(k, None)
                os.environ.update(saved_bol)

        text, exit_code = await asyncio.to_thread(_run_cart)
        ok = exit_code == 0 and any(
            marker in text
            for marker in (
                "[ok]",
                "proceeding to checkout",
                "already in basket",
                "already in account basket",
                "skipping AddItem",
            )
        )
        if not ok:
            log.error(f"Add-to-cart failed (exit {exit_code})\n{text[-2000:]}")
        elif "proceeding to checkout" in text and "[ok]" not in text:
            log.info("ATC skipped add — product already in cart, continuing to checkout")
        basket_id = load_basket_id()
        return CartResult(
            success=ok,
            verified=ok,
            cart_id=basket_id,
            message="Added via cart" if ok else text.strip()[-500:],
            raw={"exit_code": exit_code, "output": text},
        )

    async def verify_cart(
        self,
        session: aiohttp.ClientSession,
        task: TaskConfig,
        proxy_url: Optional[str],
    ) -> bool:
        if not load_basket_id():
            return False
        pid = str(task.metadata.get("product_id") or "").strip()
        if not pid:
            return True
        offer_uid = self._cached_offer_uid or None

        def _check() -> bool:
            from src.bol.login import ensure_session
            from src.bol.cart import _basket_contains_product_live

            bol_session = ensure_session()
            return _basket_contains_product_live(bol_session, pid, offer_uid)

        try:
            return await asyncio.to_thread(_check)
        except Exception as exc:
            log.debug(f"verify_cart live check failed: {exc}")
            return bool(load_basket_id())

    async def checkout(
        self,
        browser_context: Any,
        task: TaskConfig,
        profile: ProfileConfig,
    ) -> CheckoutResult:
        use_playwright = os.environ.get("BOL_CHECKOUT_PLAYWRIGHT", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        if use_playwright:
            from src.checkout.playwright_flow import PlaywrightCheckout

            flow = PlaywrightCheckout(browser_context)
            return await flow.run_bol_checkout(task, profile)

        pay = (task.payment_method or profile.payment_method or "ideal").strip().lower()
        afterpay = pay in ("afterpay", "bnpl", "achteraf", "bol_krediet", "pay_later")

        rnwy = await self._checkout_rnwy_http(task, profile)
        if rnwy.success:
            return rnwy
        if browser_context is None:
            if afterpay:
                log.warning(
                    "Afterpay checkout needs a browser to place the order — "
                    "items may still be in cart only. Enable browser checkout "
                    "(Afterpay tasks auto-start browser when available)."
                )
            return rnwy

        log.info(
            "HTTP checkout incomplete — trying browser"
            + (f" ({rnwy.message})" if rnwy.message else "")
        )
        if afterpay:
            from src.checkout.playwright_flow import PlaywrightCheckout

            flow = PlaywrightCheckout(browser_context)
            return await flow.run_bol_checkout(task, profile)
        return await self._checkout_hybrid(browser_context, task, profile)

    async def _checkout_rnwy_http(
        self,
        task: TaskConfig,
        profile: ProfileConfig,
    ) -> CheckoutResult:
        """
        Standalone-bot checkout: checkout page → iDEAL → execute-payment-plan
        → payment-execution (303 → pay.ideal.nl). No Playwright required.
        """
        from src.checkout.playwright_flow import is_ideal_payment_url
        from src.sites.bol_urls import BOL_CHECKOUT_URL, resolve_product_url
        from src.utils.profile_resolve import resolve_profile

        profile = resolve_profile(profile)
        payment_method = (task.payment_method or profile.payment_method or "ideal").strip()
        os.environ["BOL_PAYMENT_METHOD"] = payment_method.lower()
        pid = str(task.metadata.get("product_id") or "").strip()
        product_url = resolve_product_url(
            pid, str(task.product_url), task.metadata
        )
        log.info(f"Checkout payment method: {payment_method}")

        def _http_setup() -> dict:
            from src.bol.login import _load_json_file, ensure_session
            from src.bol.cart import _init_session_holder
            from src.bol.checkout import run_ideal_checkout

            session = ensure_session()
            _init_session_holder(session)
            basket_id = load_basket_id()
            bank_id = os.environ.get("BOL_IDEAL_BANK_ID", "").strip() or None
            offer_uid = self._resolve_offer_uid(task)
            return run_ideal_checkout(
                session,
                basket_id,
                bank_id=bank_id,
                product_referer=product_url,
                product_id=pid or None,
                offer_uid=offer_uid or None,
                quantity=max(1, int(task.quantity or 1)),
                payment_method=payment_method,
            )

        try:
            http_result = await asyncio.to_thread(_http_setup)
        except Exception as exc:
            log.exception("HTTP rnwy checkout failed")
            return CheckoutResult(
                success=False,
                checkout_url=BOL_CHECKOUT_URL,
                message=str(exc),
            )

        if http_result.get("success") and http_result.get("stage") == "afterpay_order":
            self.clear_http_checkout_cache()
            log.success(
                "Afterpay/BNPL checkout complete (1-step — no iDEAL bank redirect)"
            )
            return CheckoutResult(
                success=True,
                payment_url=None,
                checkout_url=BOL_CHECKOUT_URL,
                stage="afterpay_order",
                message=http_result.get("via", "bnpl_order_placed"),
                raw=http_result,
            )

        payment_url = http_result.get("payment_url")
        if payment_url and is_ideal_payment_url(payment_url):
            self.clear_http_checkout_cache()
            via = http_result.get("via", "rnwy")
            if "ideal_backup" in str(via):
                log.success(
                    f"iDEAL backup URL (Afterpay unavailable): {payment_url[:90]}"
                )
            else:
                log.success(
                    f"iDEAL URL via HTTP rnwy ({via}): {payment_url[:90]}"
                )
            return CheckoutResult(
                success=True,
                payment_url=payment_url,
                checkout_url=BOL_CHECKOUT_URL,
                stage="ideal_payment",
                message=http_result.get("via", "execute-payment-plan + payment-execution"),
                raw=http_result,
            )

        msg = http_result.get("message") or "HTTP rnwy checkout failed"
        if "400055" in msg or "transition not allowed" in msg.lower():
            os.environ["BOL_CHECKOUT_BASKET_FIRST"] = "1"
            log.info(
                "HTTP checkout stale basket (400055) — browser will open basket first"
            )
        if payment_method.lower() in (
            "afterpay",
            "bnpl",
            "achteraf",
            "bol_krediet",
            "pay_later",
        ):
            self._http_checkout_cache = http_result
            log.info(
                "Afterpay HTTP did not confirm order — browser will submit "
                "'Bestellen en betalen' if available"
            )
        return CheckoutResult(
            success=False,
            checkout_url=BOL_CHECKOUT_URL,
            message=msg,
            raw=http_result,
        )

    async def _checkout_hybrid(
        self,
        browser_context: Any,
        task: TaskConfig,
        profile: ProfileConfig,
    ) -> CheckoutResult:
        """
        HTTP: payment offering + iDEAL choice (fast, same session as ATC).
        Browser: submit payment and capture real iDEAL/bank redirect URL.
        """
        from src.checkout.playwright_flow import PlaywrightCheckout, is_ideal_payment_url
        from src.sites.bol_urls import BOL_CHECKOUT_URL
        from src.utils.profile_resolve import resolve_profile

        profile = resolve_profile(profile)
        payment_method = (task.payment_method or profile.payment_method or "ideal").strip()
        profile = profile.model_copy(update={"payment_method": payment_method})
        os.environ["BOL_PAYMENT_METHOD"] = payment_method.lower()

        if self._http_checkout_cache is not None:
            http_result = self._http_checkout_cache
            log.info("Reusing HTTP checkout setup (offering + iDEAL already selected)")
        else:

            def _http_setup() -> dict:
                from src.bol.login import ensure_session
                from src.bol.cart import _init_session_holder
                from src.bol.checkout import run_ideal_checkout

                session = ensure_session()
                _init_session_holder(session)
                basket_id = load_basket_id()
                bank_id = os.environ.get("BOL_IDEAL_BANK_ID", "").strip() or None
                pid = str(task.metadata.get("product_id") or "").strip()
                product_url = resolve_product_url(
                    pid, str(task.product_url), task.metadata
                )
                offer_uid = self._resolve_offer_uid(task) or None
                return run_ideal_checkout(
                    session,
                    basket_id,
                    bank_id=bank_id,
                    product_referer=product_url,
                    product_id=pid or None,
                    offer_uid=offer_uid,
                    payment_method=payment_method,
                )

            try:
                http_result = await asyncio.to_thread(_http_setup)
                self._http_checkout_cache = http_result
            except Exception as exc:
                log.exception("HTTP checkout setup failed")
                return CheckoutResult(
                    success=False,
                    checkout_url=BOL_CHECKOUT_URL,
                    message=str(exc),
                )

        browser_viable = bool(
            http_result.get("browser_viable")
            or http_result.get("offering_id")
            or (http_result.get("checkout_html_len") or 0) > 50_000
        )
        if not browser_viable and http_result.get("success") is False:
            msg = http_result.get("message") or "HTTP checkout setup failed"
            if "403" in msg or "blocked" in msg.lower():
                log.warning(
                    "HTTP GraphQL blocked — continuing with browser-only checkout "
                    "(refresh browser_cookies.txt from Chrome basket/checkout if this fails)"
                )
                browser_viable = True

        if http_result.get("success") and http_result.get("stage") == "afterpay_order":
            self.clear_http_checkout_cache()
            return CheckoutResult(
                success=True,
                payment_url=None,
                checkout_url=BOL_CHECKOUT_URL,
                stage="afterpay_order",
                message=http_result.get("via", "bnpl_order_placed"),
                raw=http_result,
            )

        payment_url = http_result.get("payment_url")
        if payment_url and is_ideal_payment_url(payment_url):
            self.clear_http_checkout_cache()
            return CheckoutResult(
                success=True,
                payment_url=payment_url,
                checkout_url=BOL_CHECKOUT_URL,
                stage="ideal_payment",
                message=http_result.get("via", "http"),
                raw=http_result,
            )

        if browser_context is None:
            pay_method = (task.payment_method or "ideal").strip().lower()
            if pay_method in ("afterpay", "bnpl", "achteraf", "bol_krediet", "pay_later"):
                return CheckoutResult(
                    success=False,
                    checkout_url=BOL_CHECKOUT_URL,
                    message=http_result.get("message") or "Afterpay HTTP checkout failed",
                    raw=http_result,
                )
            return CheckoutResult(
                success=False,
                checkout_url=BOL_CHECKOUT_URL,
                message=(
                    "iDEAL bank link requires browser capture — "
                    "browser pool not available"
                ),
                raw=http_result,
            )

        if not browser_viable and not http_result.get("offering_id"):
            return CheckoutResult(
                success=False,
                checkout_url=BOL_CHECKOUT_URL,
                message=http_result.get("message") or "Checkout session not viable",
                raw=http_result,
            )

        product_id = str(task.metadata.get("product_id") or "").strip()
        offering_id = str(http_result.get("offering_id") or "").strip()
        flow = PlaywrightCheckout(
            browser_context, profile_name=profile.name or "bol_main"
        )
        log.info(
            "Capturing real iDEAL URL via checkout browser"
            + (" (HTTP offering ready)" if offering_id else " (browser-only)")
        )
        await flow.reload_bol_cookies(prefer_fresh_token=True)
        payment_url = None
        try:
            payment_url = await flow.capture_ideal_payment_url(
                profile,
                product_id=product_id,
                offering_id=offering_id,
            )
        except Exception as exc:
            log.exception("Browser iDEAL capture error")
            http_result = {**http_result, "browser_error": str(exc)}

        if not payment_url or not is_ideal_payment_url(payment_url):
            log.info("capture_ideal failed — trying full Playwright checkout...")
            try:
                pw_result = await flow.run_bol_checkout(task, profile)
                if pw_result.success and pw_result.payment_url:
                    if is_ideal_payment_url(pw_result.payment_url):
                        payment_url = pw_result.payment_url
                    else:
                        log.warning(
                            f"Playwright checkout URL not a bank link: "
                            f"{pw_result.payment_url[:80]}"
                        )
            except Exception as exc:
                log.exception(f"Playwright checkout fallback: {exc}")

        if payment_url and is_ideal_payment_url(payment_url):
            self.clear_http_checkout_cache()
            log.success(f"Real iDEAL URL captured: {payment_url[:90]}")
            return CheckoutResult(
                success=True,
                payment_url=payment_url,
                checkout_url=BOL_CHECKOUT_URL,
                stage="ideal_payment",
                message="HTTP setup + browser iDEAL capture",
                raw=http_result,
            )

        fail_msg = http_result.get("browser_error") or http_result.get("message")
        if not payment_url and "Akamai" not in (fail_msg or ""):
            if http_result.get("message"):
                fail_msg = (
                    f"{http_result.get('message')} — "
                    "checkout browser blocked by Akamai (refresh login.txt from Chrome)"
                )
            else:
                fail_msg = (
                    "Could not capture iDEAL bank redirect — "
                    "refresh login.txt from Chrome on bol checkout, then retry"
                )
        elif not fail_msg:
            fail_msg = "Could not capture iDEAL bank redirect — open bol checkout manually"
        return CheckoutResult(
            success=False,
            checkout_url=BOL_CHECKOUT_URL,
            message=fail_msg,
            raw=http_result,
        )
