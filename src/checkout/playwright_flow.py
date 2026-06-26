from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from src.config.settings import get_settings
from src.models.session import CheckoutResult
from src.models.task import ProfileConfig, TaskConfig
from src.sites.bol_session import persist_playwright_cookies_to_token
from src.sites.bol_urls import BOL_BASKET_URL, BOL_CHECKOUT_URL
from src.utils.logging import get_logger
from src.utils.profile_resolve import resolve_profile

log = get_logger("checkout")

BOL_HOME = "https://www.bol.com/nl/nl/"
BOL_BASKET = "https://www.bol.com/nl/nl/basket/"
BOL_CHECKOUT_URL = "https://www.bol.com/nl/nl/checkout/"
BOL_CHECKOUT_BUY_NOW = "https://www.bol.com/nl/nl/checkout/?entryPoint=BUY_NOW"

# bol basket "Overzicht" CTA (user-confirmed DOM)
BASKET_CHECKOUT_XPATH = (
    '//*[@id="mainContent"]/div/section/div[1]/div[3]/div/button'
)
BASKET_CHECKOUT_SELECTORS: tuple[str, ...] = (
    BASKET_CHECKOUT_XPATH,
    "#mainContent section button:has-text('Verder naar bestellen')",
    "#mainContent button:has-text('Verder naar bestellen')",
    'button:has-text("Verder naar bestellen")',
)

IDEAL_URL_MARKERS = (
    "ideal.ing",
    "ideal.nl",
    "ideal.betalen",
    "rabobank.nl/ideal",
    "ing.nl/ideal",
    "abnamro.nl/ideal",
    "bunq.com/ideal",
    "triodos.nl/ideal",
    "snsbank.nl/ideal",
    "asn.nl/ideal",
    "regiobank.nl/ideal",
    "paymentrequest",
    "issuer",
)

# Adyen test card (bol.com sandbox / test mode)
DEFAULT_TEST_CARD = {
    "card_number": "4111111111111111",
    "expiry": "03/30",
    "cvv": "737",
    "holder": "Jan Jansen",
}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


class BrowserPool:
    """Manages persistent Playwright browser contexts per profile."""

    def __init__(self, headless: bool = True) -> None:
        self._headless = headless
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._contexts: Dict[str, BrowserContext] = {}
        self._settings = get_settings()
        self._started = False

    async def ensure_started(self) -> None:
        """Launch Chromium on first use (skipped for HTTP-only bol checkout)."""
        if self._started:
            return
        from src.utils.app_root import configure_playwright_browsers

        configure_playwright_browsers()
        self._playwright = await async_playwright().start()
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ]
        last_exc: Exception | None = None
        for mode in ("bundled", "chrome", "msedge"):
            try:
                if mode == "bundled":
                    self._browser = await self._playwright.chromium.launch(
                        headless=self._headless,
                        args=launch_args,
                    )
                else:
                    log.warning(
                        f"Bundled Chromium unavailable — trying system {mode}"
                    )
                    self._browser = await self._playwright.chromium.launch(
                        channel=mode,
                        headless=self._headless,
                        args=launch_args,
                    )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
        if self._browser is None:
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            from src.utils.app_root import get_app_root

            root = get_app_root()
            raise RuntimeError(
                "Playwright browser is not available. "
                f"Expected bundled browsers in: {root / 'playwright-browsers'} "
                "(re-download the full BOL-BOT-Release zip). "
                "Or install Google Chrome / Edge, or run: playwright install chromium"
            ) from last_exc
        self._started = True

    async def start(self) -> None:
        await self.ensure_started()

    async def _new_context(
        self,
        *,
        use_storage: bool,
        profile_name: str,
        proxy_url: Optional[str] = None,
    ) -> BrowserContext:
        assert self._browser is not None
        from src.sites.bol_session import _playwright_proxy

        storage = self._settings.browser_data_dir / profile_name / "state.json"
        ctx_kwargs: dict = {
            "storage_state": str(storage) if use_storage and storage.is_file() else None,
            "viewport": {"width": 1280, "height": 800},
            "locale": "nl-NL",
            "timezone_id": "Europe/Amsterdam",
            "user_agent": _USER_AGENT,
            "ignore_https_errors": True,
        }
        pw_proxy = _playwright_proxy(proxy_url)
        if pw_proxy:
            ctx_kwargs["proxy"] = pw_proxy
            from src.proxy.bol_proxy import proxy_label

            log.info(f"Checkout browser proxy: {proxy_label(proxy_url)}")
        ctx = await self._browser.new_context(**ctx_kwargs)
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        return ctx

    async def get_context(self, profile_name: str) -> BrowserContext:
        await self.ensure_started()
        if profile_name in self._contexts:
            return self._contexts[profile_name]
        ctx = await self._new_context(use_storage=True, profile_name=profile_name)
        self._contexts[profile_name] = ctx
        return ctx

    async def get_checkout_context(
        self, profile_name: str, *, proxy_url: Optional[str] = None
    ) -> BrowserContext:
        """Context with saved storage_state (Akamai sensor) + bol_token cookies."""
        await self.ensure_started()
        if profile_name in self._contexts:
            await self._contexts[profile_name].close()
            del self._contexts[profile_name]
        storage = self._settings.browser_data_dir / profile_name / "state.json"
        use_storage = storage.is_file() and os.environ.get(
            "BOL_CHECKOUT_NO_STORAGE", ""
        ).strip().lower() not in ("1", "true", "yes")
        if use_storage:
            log.info(f"Checkout browser: loading storage_state ({profile_name})")
        ctx = await self._new_context(
            use_storage=use_storage,
            profile_name=profile_name,
            proxy_url=proxy_url,
        )
        self._contexts[profile_name] = ctx
        return ctx

    async def save_context(self, profile_name: str) -> None:
        ctx = self._contexts.get(profile_name)
        if not ctx:
            return
        path = self._settings.browser_data_dir / profile_name / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(path))

    async def close(self) -> None:
        for ctx in self._contexts.values():
            await ctx.close()
        self._contexts.clear()
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._started = False


def extract_ideal_url_from_payload(payload: Any) -> Optional[str]:
    """Walk JSON (GraphQL / firefly) for iDEAL redirectUrl."""
    if isinstance(payload, str):
        if payload.startswith("http") and is_ideal_payment_url(payload):
            return payload
        return None
    if isinstance(payload, dict):
        for key in (
            "redirectUrl",
            "redirect_url",
            "paymentUrl",
            "payment_url",
            "issuerUrl",
            "issuer_url",
        ):
            val = payload.get(key)
            if isinstance(val, str) and is_ideal_payment_url(val):
                return val
        for val in payload.values():
            found = extract_ideal_url_from_payload(val)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = extract_ideal_url_from_payload(item)
            if found:
                return found
    return None


def is_ideal_payment_url(url: str) -> bool:
    """True for real iDEAL / bank issuer pages — not bol checkout stubs."""
    u = (url or "").lower()
    if not u.startswith("http"):
        return False
    if "bol.com" in u:
        return False
    if "adyen" in u:
        return True
    return any(m in u for m in IDEAL_URL_MARKERS) or "ideal" in u


class PlaywrightCheckout:
    def __init__(self, context: BrowserContext, *, profile_name: str = "") -> None:
        self._context = context
        self._profile_name = profile_name

    def _attach_payment_response_listener(
        self, page: Page, captured_urls: list[str]
    ) -> None:
        async def _on_response(response: Any) -> None:
            url = response.url.lower()
            if "graphql" not in url and "firefly.bol.com" not in url:
                return
            try:
                if response.status < 200 or response.status >= 400:
                    return
                data = await response.json()
            except Exception:
                return
            found = extract_ideal_url_from_payload(data)
            if found and found not in captured_urls:
                captured_urls.append(found)
                log.info(f"capture_ideal: payment API redirect {found[:100]}")

        page.on("response", lambda r: __import__("asyncio").create_task(_on_response(r)))

    async def capture_ideal_payment_url(
        self,
        profile: ProfileConfig,
        *,
        product_id: str = "",
        offering_id: str = "",
    ) -> Optional[str]:
        """
        After HTTP GraphQL set iDEAL on the offering: open checkout in-browser,
        walk steps, submit payment, return real bank/iDEAL redirect URL.
        """
        profile = resolve_profile(profile)
        page = await self._context.new_page()
        captured_urls: list[str] = []

        def _note_url(url: str) -> None:
            if url and is_ideal_payment_url(url) and url not in captured_urls:
                captured_urls.append(url)
                log.info(f"capture_ideal: saw bank URL {url[:100]}")

        page.on(
            "framenavigated",
            lambda frame: _note_url(frame.url) if frame == page.main_frame else None,
        )
        self._context.on("page", lambda p: p.on("framenavigated", lambda f: _note_url(f.url)))
        self._attach_payment_response_listener(page, captured_urls)

        try:
            await self.reload_bol_cookies(prefer_fresh_token=True)
            await self._seed_akamai_in_context(page, BOL_HOME)
            if not await self._prime_session(page):
                log.warning("capture_ideal: browser not logged in / Akamai blocked")
                if not await self._seed_checkout_via_playwright_fetch():
                    return None
                await self.reload_bol_cookies(prefer_fresh_token=True)
                if not await self._prime_session(page):
                    return None

            if not await self._enter_checkout_for_capture(
                page, product_id, offering_id=offering_id
            ):
                log.warning("capture_ideal: could not open checkout (redirect loop or stub)")
                return None

            log.info(
                f"capture_ideal: checkout page {page.url[:90]} "
                f"(html {len(await page.content())} chars)"
            )
            if captured_urls:
                await persist_playwright_cookies_to_token(self._context)
                return captured_urls[0]

            await self._advance_checkout(page, profile)
            payment_url = await self._complete_payment(page, profile)
            if payment_url and is_ideal_payment_url(payment_url):
                await persist_playwright_cookies_to_token(self._context)
                return payment_url

            for p in self._context.pages:
                if is_ideal_payment_url(p.url):
                    await persist_playwright_cookies_to_token(self._context)
                    return p.url

            extracted = await self._extract_payment_url(page)
            if extracted and is_ideal_payment_url(extracted):
                await persist_playwright_cookies_to_token(self._context)
                return extracted

            if captured_urls:
                await persist_playwright_cookies_to_token(self._context)
                return captured_urls[0]

            log.warning(f"capture_ideal: no issuer URL (last page {page.url[:80]})")
            return None
        except Exception as exc:
            log.error(f"capture_ideal failed: {exc}")
            return None
        finally:
            await page.close()

    async def _safe_goto(
        self,
        page: Page,
        url: str,
        *,
        timeout: int = 60_000,
        label: str = "",
    ) -> bool:
        """Navigate to bol.com; retry on chrome-error / interrupted navigation."""
        last_err: Optional[Exception] = None
        for attempt in range(4):
            try:
                if attempt:
                    log.info(f"Retry navigation to {url[:70]} ({attempt + 1}/4)")
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                await page.wait_for_timeout(1500)
                cur = page.url.lower()
                if "chromewebdata" in cur or cur.startswith("chrome://"):
                    last_err = RuntimeError(f"Chrome error page: {page.url}")
                    await page.wait_for_timeout(2000)
                    continue
                if "bol.com" in cur:
                    return True
            except Exception as exc:
                last_err = exc
                msg = str(exc).lower()
                if "interrupted" in msg or "chromewebdata" in msg:
                    await page.wait_for_timeout(2000)
                    cur = page.url.lower()
                    if "bol.com" in cur and "chromewebdata" not in cur:
                        return True
                    if attempt < 3:
                        continue
                if "err_too_many_redirects" in msg:
                    last_err = exc
                    log.warning(f"Redirect loop on {url[:60]} — reloading cookies")
                    await self.reload_bol_cookies()
                    if "entrypoint=buy_now" in url.lower() and attempt == 0:
                        log.info(
                            "BUY_NOW redirect loop — will try basket page on next step"
                        )
                        os.environ["BOL_CHECKOUT_BASKET_FIRST"] = "1"
                    if attempt < 3:
                        continue
                    return False
                if attempt < 3:
                    await page.wait_for_timeout(2000)
                    continue
                raise
        if last_err:
            log.warning(f"Navigation failed ({label or url[:50]}): {last_err}")
        return False

    async def run_bol_checkout(
        self, task: TaskConfig, profile: ProfileConfig
    ) -> CheckoutResult:
        profile = resolve_profile(profile)
        product_id = str(task.metadata.get("product_id") or "").strip()
        product_referer = str(task.product_url or BOL_HOME).strip() or BOL_HOME
        page = await self._context.new_page()
        captured_urls: list[str] = []
        self._attach_payment_response_listener(page, captured_urls)
        try:
            await self.reload_bol_cookies(prefer_fresh_token=True)
            await self._seed_akamai_in_context(page, BOL_HOME)
            if not await self._prime_session(page):
                await self._seed_checkout_via_playwright_fetch()
                await self.reload_bol_cookies()
                if not await self._prime_session(page):
                    return CheckoutResult(
                        success=False,
                        checkout_url=page.url,
                        stage="login",
                        message=(
                            "Browser blocked by Akamai — refresh login.txt from Chrome "
                            "on bol.com checkout"
                        ),
                    )

            entered = False
            basket_first = os.environ.get("BOL_CHECKOUT_BASKET_FIRST", "").strip().lower() in {
                "1",
                "true",
                "yes",
            }
            if basket_first:
                log.info(
                    "Stale basket recovery — opening basket page before checkout URL..."
                )
                entered = await self._open_basket_and_enter_checkout(page, product_id)
            if not entered and os.environ.get("BOL_CHECKOUT_DIRECT", "1") != "0":
                log.info("Trying checkout BUY_NOW URL (same as standalone bot)...")
                entered = await self._goto_checkout_buy_now(
                    page, product_id, referer=product_referer
                )
            if not entered:
                entered = await self._goto_checkout_fallback(
                    page, product_id, referer=product_referer
                )
            if not entered:
                entered = await self._open_basket_and_enter_checkout(page, product_id)
            if await self._is_login_wall(page):
                await self.reload_bol_cookies(prefer_fresh_token=False)
                entered = await self._goto_checkout_buy_now(
                    page, product_id, referer=product_referer
                )
            if await self._is_login_wall(page):
                return CheckoutResult(
                    success=False,
                    checkout_url=page.url,
                    stage="login",
                    message=(
                        "Browser redirected to bol login — export fresh cookies.txt "
                        "from Chrome while logged in on bol.com checkout (same proxy IP)"
                    ),
                )
            if not entered:
                body_snip = (await page.content())[:500].lower()
                empty = await self._basket_is_empty(page, product_id)
                return CheckoutResult(
                    success=False,
                    checkout_url=page.url,
                    stage="basket",
                    message=(
                        "Could not enter checkout — basket empty in browser"
                        if empty
                        else "Could not open checkout — try basket link manually"
                    ),
                    raw={"empty": empty, "url": page.url, "snippet": body_snip[:200]},
                )

            await self._advance_checkout(page, profile)

            pay_method = self._normalize_payment_method(
                task.payment_method or profile.payment_method
            )
            payment_url = await self._complete_payment(page, profile)
            if pay_method == "afterpay" and await self._afterpay_order_confirmed(page):
                await persist_playwright_cookies_to_token(self._context)
                log.success("Afterpay order placed (browser confirmation)")
                return CheckoutResult(
                    success=True,
                    payment_url=None,
                    checkout_url=page.url,
                    stage="afterpay_order",
                    message="Afterpay order submitted in browser",
                )
            if pay_method == "afterpay":
                log.info(
                    "Afterpay not available or order not confirmed — "
                    "falling back to iDEAL in browser"
                )
                profile_ideal = profile.model_copy(update={"payment_method": "ideal"})
                payment_url = await self._complete_payment(page, profile_ideal)
            if payment_url and is_ideal_payment_url(payment_url):
                await persist_playwright_cookies_to_token(self._context)
                log.success(f"iDEAL / payment URL: {payment_url[:100]}")
                return CheckoutResult(
                    success=True,
                    payment_url=payment_url,
                    checkout_url=page.url,
                    stage="ideal_payment",
                    message="Checkout reached iDEAL payment page",
                )

            if captured_urls and is_ideal_payment_url(captured_urls[0]):
                await persist_playwright_cookies_to_token(self._context)
                return CheckoutResult(
                    success=True,
                    payment_url=captured_urls[0],
                    checkout_url=page.url,
                    stage="ideal_payment",
                    message="iDEAL URL from payment API response",
                )

            if await self._payment_step_ready(page):
                payment_url = page.url
                extracted = await self._extract_payment_url(page)
                if extracted:
                    payment_url = extracted
                await persist_playwright_cookies_to_token(self._context)
                return CheckoutResult(
                    success=True,
                    payment_url=payment_url,
                    checkout_url=page.url,
                    stage="payment_ready",
                    message="Checkout reached payment step (confirm iDEAL manually)",
                )

            payment_url = await self._extract_payment_url(page)
            if payment_url and self._is_ideal_payment_url(payment_url):
                await persist_playwright_cookies_to_token(self._context)
                return CheckoutResult(
                    success=True,
                    payment_url=payment_url,
                    checkout_url=page.url,
                    stage="ideal_payment",
                    message="Checkout reached payment provider",
                )

            if "adyen" in page.url.lower() or self._is_ideal_payment_url(page.url):
                await persist_playwright_cookies_to_token(self._context)
                return CheckoutResult(
                    success=True,
                    payment_url=page.url,
                    checkout_url=page.url,
                    stage="payment_redirect",
                    message="Redirected to payment provider",
                )

            return CheckoutResult(
                success=False,
                checkout_url=page.url,
                stage="checkout",
                message="Checkout did not reach iDEAL payment — check profile / bol checkout UI",
            )
        except Exception as exc:
            log.exception(f"Checkout failed: {exc}")
            return CheckoutResult(success=False, message=str(exc), stage="error")
        finally:
            await page.close()

    async def reload_bol_cookies(self, *, prefer_fresh_token: bool = False) -> int:
        """bol_token.json + optional login.txt / browser_cookies.txt."""
        from pathlib import Path
        import time

        from src.config.settings import get_settings
        from src.sites.akamai import login_txt_path, parse_login_txt_cookie_header
        from src.sites.bol_cookies import merge_cookie_dict, parse_cookie_header
        from src.sites.bol_session import load_cookie_dict

        cookies = load_cookie_dict()
        state_path = get_settings().browser_data_dir / (self._profile_name or "bol_main") / "state.json"
        state_fresh = (
            state_path.is_file()
            and (time.time() - state_path.stat().st_mtime) < 600
        )
        skip_login_txt = prefer_fresh_token or state_fresh
        if skip_login_txt:
            log.debug(
                "Skipping login.txt merge"
                + (" (fresh Playwright seed)" if prefer_fresh_token else " (recent storage_state)")
            )
        else:
            cookies = merge_cookie_dict(
                cookies,
                parse_login_txt_cookie_header(login_txt_path()),
            )
        browser_path = Path(
            os.environ.get(
                "BOL_BROWSER_COOKIES_TXT",
                str(get_settings().bol_token_path.parent / "browser_cookies.txt"),
            )
        )
        if browser_path.is_file():
            raw = browser_path.read_text(encoding="utf-8", errors="replace").strip()
            if raw.lower().startswith("cookie:"):
                raw = raw.split(":", 1)[1].strip()
            if raw and "_abck=" in raw:
                cookies = merge_cookie_dict(cookies, parse_cookie_header(raw))
                log.info(f"Merged cookies from {browser_path.name}")
        await self.load_cookies_from_dict(cookies)
        return len(cookies)

    async def _is_akamai_stub_page(self, page: Page) -> bool:
        try:
            html = await page.content()
        except Exception:
            return True
        if len(html) < 20_000:
            return True
        low = html.lower()
        return not any(
            k in low
            for k in (
                "winkel van ons allemaal",
                "bezorgadres",
                "betaalmethode",
                "verder naar bestellen",
                "bestellen en betalen",
            )
        )

    async def _persist_browser_state(self) -> None:
        await persist_playwright_cookies_to_token(self._context)
        if not self._profile_name:
            return
        from src.config.settings import get_settings

        path = get_settings().browser_data_dir / self._profile_name / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(path))
        log.info(f"Saved browser storage_state ({self._profile_name})")

    async def _seed_akamai_in_context(
        self, page: Page, url: str = BOL_HOME
    ) -> bool:
        """Run Akamai bot-manager JS in this browser (static login.txt cookies are not enough)."""
        log.info(f"Running Akamai sensor in checkout browser ({url[:50]}...)")
        await self.reload_bol_cookies(prefer_fresh_token=True)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as exc:
            log.warning(f"Akamai seed goto: {exc}")

        for wait_ms in (3000, 5000, 7000):
            await page.wait_for_timeout(wait_ms)
            if not await self._is_akamai_stub_page(page):
                await self._persist_browser_state()
                log.info(
                    f"Akamai sensor OK ({len(await page.content())} chars, {page.url[:60]})"
                )
                return True

        try:
            await page.reload(wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(5000)
        except Exception as exc:
            log.warning(f"Akamai seed reload: {exc}")

        if not await self._is_akamai_stub_page(page):
            await self._persist_browser_state()
            return True

        log.warning(
            f"Akamai sensor failed — still stub ({len(await page.content())} chars). "
            "Export fresh login.txt from Chrome on www.bol.com/checkout."
        )
        return False

    async def _prime_session(self, page: Page) -> bool:
        log.info("Priming bol.com session (homepage)...")
        await self.reload_bol_cookies(prefer_fresh_token=True)

        for attempt in range(4):
            if attempt:
                await self.reload_bol_cookies(prefer_fresh_token=attempt >= 2)
            if not await self._safe_goto(page, BOL_HOME, label="prime"):
                continue
            await page.wait_for_timeout(1500)
            await self._dismiss_overlays(page)
            if not await self._is_akamai_stub_page(page):
                break
            log.warning(
                f"Homepage Akamai stub ({len(await page.content())} chars) — "
                f"attempt {attempt + 1}/4"
            )
            if attempt >= 1 and await self._seed_akamai_in_context(page):
                if not await self._is_akamai_stub_page(page):
                    break
        else:
            if await self._seed_akamai_in_context(page, BOL_CHECKOUT_URL):
                if await self._is_akamai_stub_page(page):
                    return False
            else:
                return False

        if await self._is_logged_in(page):
            log.info("Browser session logged in (BUI cookie present)")
            return True
        log.warning("Browser session appears logged out after cookie load")
        return False

    async def _seed_checkout_via_playwright_fetch(self) -> bool:
        """Separate Chromium window — saves storage_state then reload into this context."""
        from src.sites.bol_session import fetch_product_page_playwright

        log.info("Seeding Akamai via standalone Playwright fetch...")
        proxy_url = os.environ.get("BOL_PROXY_URL", "").strip() or None
        for url in (BOL_HOME, BOL_CHECKOUT_URL):
            status, html = await fetch_product_page_playwright(
                url, proxy_url=proxy_url
            )
            if status == 200 and len(html) > 50_000:
                log.info(f"Standalone seed OK on {url[:40]} ({len(html)} chars)")
                await self.reload_bol_cookies()
                return True
        log.warning("Standalone Playwright seed did not clear Akamai")
        return False

    async def _enter_checkout_for_capture(
        self, page: Page, product_id: str, *, offering_id: str = ""
    ) -> bool:
        """Same path as manual: basket → Verder naar bestellen → checkout."""
        log.info("capture_ideal: basket → checkout (like continueOrdering click)")
        if await self._safe_goto(page, BOL_BASKET_URL, label="basket-capture"):
            await self._dismiss_overlays(page)
            if await self._wait_for_basket_ready(page, product_id, timeout_ms=12_000):
                if await self._start_checkout_from_basket(page):
                    if not await self._is_akamai_stub_page(page):
                        return True

        for sel in (
            'a[href*="/nl/nl/checkout"]',
            'a[href="/nl/nl/checkout/"]',
            '[data-test*="checkout"]',
        ):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    log.info(f"capture_ideal: following checkout link ({sel[:40]})")
                    await loc.click(timeout=12_000)
                    await page.wait_for_timeout(3000)
                    if "checkout" in page.url.lower() and not await self._is_akamai_stub_page(
                        page
                    ):
                        return True
            except Exception:
                pass

        if not await self._basket_is_empty(page, product_id):
            log.info("Basket has items in browser — opening checkout URL")
        else:
            log.warning("Basket empty in browser — API cart may not be visible yet")

        await self.reload_bol_cookies()
        log.info("capture_ideal: opening checkout URL (referer=basket)")
        try:
            await page.goto(
                BOL_CHECKOUT_URL,
                wait_until="domcontentloaded",
                timeout=60_000,
                referer=BOL_BASKET_URL,
            )
            await page.wait_for_timeout(2000)
            if not await self._is_akamai_stub_page(page) and "checkout" in page.url.lower():
                return True
        except Exception as exc:
            if "too_many_redirects" not in str(exc).lower():
                log.warning(f"Checkout goto: {exc}")

        if await self._safe_goto(page, BOL_CHECKOUT_URL, label="checkout-safe"):
            if not await self._is_akamai_stub_page(page):
                return True

        if await self._seed_checkout_via_playwright_fetch():
            if await self._safe_goto(page, BOL_BASKET_URL, label="basket-after-seed"):
                await self._dismiss_overlays(page)
                if await self._start_checkout_from_basket(page):
                    return True
            return await self._goto_checkout_fallback(page, product_id)

        return False

    async def _is_logged_in(self, page: Page) -> bool:
        names = {c["name"] for c in await page.context.cookies()}
        if "BUI" in names and "DYN_USER_ID" in names:
            return True
        body = (await page.content()).lower()
        return "uitloggen" in body or "/rnwy/account/overzicht" in body

    async def _open_basket_and_enter_checkout(
        self, page: Page, product_id: str
    ) -> bool:
        """Load basket (React), confirm items visible, then basket button or /checkout/."""
        log.info("Opening basket...")
        if not await self._safe_goto(page, BOL_BASKET_URL, label="basket"):
            log.warning("Basket navigation failed — trying checkout URL")
            return await self._goto_checkout_fallback(page, product_id)
        await self._dismiss_overlays(page)

        if not await self._wait_for_basket_ready(page, product_id, timeout_ms=25_000):
            log.warning("Basket did not show items yet — reloading once...")
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await self._dismiss_overlays(page)
            if not await self._wait_for_basket_ready(page, product_id, timeout_ms=20_000):
                if await self._basket_is_empty(page, product_id):
                    return False
                log.info("Items not detected in HTML — trying checkout URL anyway")

        if await self._start_checkout_from_basket(page):
            return True

        log.info("No basket checkout button — opening checkout URL directly")
        return await self._goto_checkout_fallback(page, product_id)

    async def _wait_for_basket_ready(
        self, page: Page, product_id: str, *, timeout_ms: int
    ) -> bool:
        import time

        deadline = time.monotonic() + timeout_ms / 1000.0
        selectors = BASKET_CHECKOUT_SELECTORS + (
            'a[href*="/checkout"]',
            'button:has-text("Naar de kassa")',
            'a:has-text("Naar de kassa")',
        )
        while time.monotonic() < deadline:
            await self._dismiss_overlays(page)
            if product_id and product_id in (await page.content()):
                if not await self._basket_is_empty(page, product_id):
                    return True
            for sel in selectors:
                try:
                    loc = self._page_locator(page, sel)
                    if await loc.count() and await loc.is_visible():
                        return True
                except Exception:
                    pass
            await page.wait_for_timeout(800)
        return False

    async def _goto_checkout_buy_now(
        self, page: Page, product_id: str, *, referer: str = BOL_HOME
    ) -> bool:
        try:
            await page.set_extra_http_headers({"Referer": referer})
            ok = await self._safe_goto(
                page, BOL_CHECKOUT_BUY_NOW, label="checkout-buy-now"
            )
            await page.set_extra_http_headers({})
        except Exception:
            ok = False
        if not ok or await self._is_login_wall(page):
            return False
        await page.wait_for_timeout(2500)
        await self._dismiss_overlays(page)
        if await self._is_akamai_stub_page(page):
            return False
        if product_id and product_id in (await page.content()):
            return True
        body = (await page.content()).lower()
        return any(
            k in body
            for k in ("betaalmethode", "bezorgadres", "bestellen en betalen")
        )

    async def _goto_checkout_fallback(
        self, page: Page, product_id: str, *, referer: str = BOL_HOME
    ) -> bool:
        if await self._goto_checkout_buy_now(page, product_id, referer=referer):
            return True
        if not await self._safe_goto(page, BOL_CHECKOUT_URL, label="checkout-fallback"):
            log.warning("Checkout URL failed — not retrying via basket (cart is API-only)")
            return False

        await page.wait_for_timeout(3500)
        await self._dismiss_overlays(page)
        if await self._is_login_wall(page):
            return False
        url = page.url.lower()
        if "checkout" not in url and "bestellen" not in url:
            return False
        if await self._basket_is_empty(page, product_id):
            return False
        if product_id and product_id in (await page.content()):
            return True
        return "betaal" in (await page.content()).lower() or await self._payment_step_ready(
            page
        )

    async def _basket_is_empty(self, page: Page, product_id: Optional[str] = None) -> bool:
        body = (await page.content()).lower()
        if product_id and product_id in body:
            return False
        return any(
            phrase in body
            for phrase in (
                "winkelwagen is leeg",
                "winkelwagentje is leeg",
                "je winkelwagen is leeg",
                "je mandje is leeg",
            )
        )

    async def _is_login_wall(self, page: Page) -> bool:
        url = (page.url or "").lower()
        if "login.bol.com" in url or "/account/login" in url:
            return True
        try:
            body = (await page.content()).lower()
        except Exception:
            return False
        return bool(
            re.search(r"<title[^>]*>[^<]*inloggen[^<]*</title>", body)
            or "wsp/login" in url
        )

    async def _left_basket_or_in_checkout(self, page: Page) -> bool:
        """True after basket CTA — URL may be /checkout/, /bestellen/, or next step."""
        if await self._is_login_wall(page):
            return False
        url = page.url.lower()
        if "/basket" not in url:
            return "checkout" in url or "bestellen" in url or "bezorg" in (
                await page.content()
            ).lower()
        if any(k in url for k in ("checkout", "bestellen", "rnwy")):
            return True
        body = (await page.content()).lower()
        return any(
            k in body
            for k in ("bezorgadres", "afleveradres", "betaalmethode", "bezorging")
        )

    async def _click_basket_checkout_button(self, page: Page) -> bool:
        """Click 'Verder naar bestellen' in basket Overzicht panel."""
        for selector in BASKET_CHECKOUT_SELECTORS:
            try:
                loc = self._page_locator(page, selector)
                if not await loc.count():
                    continue
                await loc.wait_for(state="visible", timeout=8000)
                log.info(
                    'Clicking basket checkout ("Verder naar bestellen")...'
                )
                await loc.click(timeout=12_000)
                await page.wait_for_timeout(3000)
                return await self._left_basket_or_in_checkout(page)
            except Exception:
                continue
        try:
            btn = page.get_by_role(
                "button", name=re.compile(r"verder naar bestellen", re.I)
            ).first
            if await btn.is_visible(timeout=2000):
                log.info('Clicking basket checkout (role=button)...')
                await btn.click(timeout=12_000)
                await page.wait_for_timeout(3000)
                return await self._left_basket_or_in_checkout(page)
        except Exception:
            pass
        return False

    async def _start_checkout_from_basket(self, page: Page) -> bool:
        for attempt in range(3):
            if await self._click_basket_checkout_button(page):
                log.info(f"Basket checkout entered — {page.url[:90]}")
                return True

            for role, pat in (
                ("button", r"verder naar bestellen"),
                ("link", r"verder naar bestellen"),
                ("link", r"naar de kassa"),
                ("button", r"naar de kassa"),
            ):
                try:
                    el = page.get_by_role(role, name=re.compile(pat, re.I)).first
                    if await el.is_visible(timeout=1500):
                        await el.click(timeout=10_000)
                        await page.wait_for_timeout(2500)
                        if await self._left_basket_or_in_checkout(page):
                            return True
                except Exception:
                    continue

            if await self._click_first(
                page,
                list(BASKET_CHECKOUT_SELECTORS)
                + [
                    'a[href*="/checkout"]',
                    'button:has-text("Naar de kassa")',
                    '[data-test*="checkout"]',
                    '[data-test*="order-button"]',
                ],
                optional=True,
            ):
                await page.wait_for_timeout(2500)
                if await self._left_basket_or_in_checkout(page):
                    return True

            if attempt < 2:
                await page.wait_for_timeout(1000)

        return False

    @staticmethod
    def _normalize_payment_method(method: Optional[str]) -> str:
        raw = (method or "ideal").strip().lower()
        if raw in ("afterpay", "bnpl", "achteraf", "bol_krediet", "pay_later"):
            return "afterpay"
        return raw

    async def _complete_payment(self, page: Page, profile: ProfileConfig) -> Optional[str]:
        """Select payment method and submit until iDEAL issuer page opens."""
        method = self._normalize_payment_method(profile.payment_method)
        if method in ("card", "creditcard", "credit_card"):
            pay = {**DEFAULT_TEST_CARD, **profile.payment, **profile.extra.get("payment", {})}
            await self._select_card(page)
            await self._fill_card(page, pay, profile)
        elif method == "afterpay":
            await self._select_afterpay(page)
            await self._submit_afterpay_order(page)
            return None
        else:
            await self._select_ideal(page)

        await page.wait_for_timeout(1500)

        pay_patterns = (
            r"bestellen en betalen",
            r"betalen met ideal",
            r"naar betalen",
            r"afrekenen",
        )
        for pat in pay_patterns:
            try:
                btn = page.get_by_role("button", name=re.compile(pat, re.I)).first
                if not await btn.is_visible(timeout=1000):
                    continue
                log.info(f"Submitting payment (button /{pat}/)...")
                try:
                    async with page.context.expect_page(timeout=50_000) as popup_info:
                        await btn.click(timeout=12_000)
                    popup = await popup_info.value
                    await popup.wait_for_load_state("domcontentloaded", timeout=50_000)
                    await popup.wait_for_timeout(2000)
                    if self._is_ideal_payment_url(popup.url):
                        return popup.url
                except Exception:
                    await btn.click(timeout=12_000)
                    found = await self._wait_for_ideal_url(page, timeout_ms=50_000)
                    if found:
                        return found
            except Exception:
                continue

        for selector in (
            'button:has-text("Bestellen en betalen")',
            'button:has-text("Betalen met iDEAL")',
            'button:has-text("Naar betalen")',
        ):
            if await self._click_first(page, [selector], optional=True):
                found = await self._wait_for_ideal_url(page, timeout_ms=45_000)
                if found:
                    return found

        return await self._wait_for_ideal_url(page, timeout_ms=15_000)

    @staticmethod
    def _is_ideal_payment_url(url: str) -> bool:
        return is_ideal_payment_url(url)

    async def _wait_for_ideal_url(self, page: Page, *, timeout_ms: int) -> Optional[str]:
        deadline = timeout_ms / 1000.0
        waited = 0.0
        while waited < deadline:
            for p in page.context.pages:
                if self._is_ideal_payment_url(p.url):
                    return p.url
            extracted = await self._extract_payment_url(page)
            if extracted and self._is_ideal_payment_url(extracted):
                return extracted
            await page.wait_for_timeout(1000)
            waited += 1.0
        return None

    async def _advance_checkout(self, page: Page, profile: ProfileConfig) -> None:
        """Walk bol checkout steps: address -> delivery -> payment."""
        method = self._normalize_payment_method(profile.payment_method)
        pay = {**DEFAULT_TEST_CARD, **profile.payment, **profile.extra.get("payment", {})}
        prev_url = ""
        stuck = 0

        for step in range(8):
            await page.wait_for_timeout(1500)
            url = page.url
            log.info(f"Checkout step {step + 1}: {url[:90]}")

            if await self._is_login_wall(page):
                log.warning("Checkout hit login wall — stopping advance")
                break

            if await self._payment_step_ready(page):
                if method in ("card", "creditcard", "credit_card"):
                    await self._select_card(page)
                    await self._fill_card(page, pay, profile)
                elif method == "afterpay":
                    await self._select_afterpay(page)
                else:
                    await self._select_ideal(page)
                break

            await self._dismiss_overlays(page)
            step_name = await self._checkout_step(page)
            log.debug(f"Detected checkout sub-step: {step_name}")

            if step_name in ("address", "unknown"):
                await self._fill_shipping(page, profile)

            if step_name == "payment":
                if method in ("card", "creditcard", "credit_card"):
                    await self._select_card(page)
                    await self._fill_card(page, pay, profile)
                elif method == "afterpay":
                    await self._select_afterpay(page)
                else:
                    await self._select_ideal(page)

            clicked = await self._click_continue(page)
            await page.wait_for_timeout(2000)

            if url == page.url:
                stuck += 1
                if stuck >= 3:
                    log.warning("Checkout URL unchanged after 3 attempts — stopping advance")
                    break
            else:
                stuck = 0
            prev_url = page.url

            if not clicked and await self._payment_step_ready(page):
                break

    async def _checkout_step(self, page: Page) -> str:
        if await self._has_adyen_iframes(page):
            return "payment"
        body = (await page.content()).lower()
        if "betaalmethode" in body or "creditcard" in body or "ideal" in body:
            return "payment"
        if "bezorgadres" in body or "afleveradres" in body:
            return "address"
        if "bezorg" in body and "lever" in body:
            return "delivery"
        return "unknown"

    async def _has_adyen_iframes(self, page: Page) -> bool:
        return await page.locator(
            'iframe[src*="adyen" i], iframe[title*="card" i], iframe[title*="kaart" i]'
        ).count() > 0

    async def _payment_step_ready(self, page: Page) -> bool:
        if await self._has_adyen_iframes(page):
            return True
        url = page.url.lower()
        if any(k in url for k in ("adyen", "ideal", "checkout.ideal")):
            return True
        body = (await page.content()).lower()
        return "betaalmethode" in body and (
            "creditcard" in body or "ideal" in body or "adyen" in body
        )

    async def _click_continue(self, page: Page) -> bool:
        if "/basket" in page.url.lower():
            if await self._click_basket_checkout_button(page):
                return True

        patterns = (
            r"naar de kassa",
            r"prima.*bestellen",
            r"verder naar bestellen",
            r"bestellen en betalen",
            r"naar betalen",
            r"doorgaan",
            r"bevestig",
        )
        for pat in patterns:
            try:
                btn = page.get_by_role("button", name=re.compile(pat, re.I)).first
                if await btn.is_visible(timeout=800):
                    await btn.click(timeout=8000)
                    return True
            except Exception:
                continue
        return await self._click_first(
            page,
            list(BASKET_CHECKOUT_SELECTORS)
            + [
                'button:has-text("Naar de kassa")',
                'button:has-text("Prima, verder naar bestellen")',
                'button:has-text("Verder naar bestellen")',
                'button:has-text("Bestellen en betalen")',
                'button:has-text("Naar betalen")',
                'button:has-text("Doorgaan")',
                'button[type="submit"]:visible',
            ],
            optional=True,
        )

    async def _dismiss_overlays(self, page: Page) -> None:
        await self._click_first(
            page,
            [
                'button:has-text("Accepteren")',
                'button:has-text("Akkoord")',
                'button:has-text("Alles accepteren")',
                '[data-test="consent-accept"]',
            ],
            optional=True,
        )

    async def _fill_shipping(self, page: Page, profile: ProfileConfig) -> None:
        ship = profile.shipping
        mapping = {
            'input[name*="email" i]': profile.email,
            'input[type="email"]': profile.email,
            'input[name*="firstName" i]': ship.get("first_name"),
            'input[name*="lastName" i]': ship.get("last_name"),
            'input[name*="postalCode" i]': ship.get("postal_code"),
            'input[name*="houseNumber" i]': ship.get("house_number"),
            'input[name*="street" i]': ship.get("street"),
            'input[name*="city" i]': ship.get("city"),
            'input[name*="phone" i]': ship.get("phone"),
        }
        for selector, value in mapping.items():
            if not value or "${" in str(value):
                continue
            loc = page.locator(selector).first
            try:
                if await loc.count() and await loc.is_visible():
                    await loc.fill(str(value))
            except Exception:
                continue

    async def _open_payment_method_picker(self, page: Page) -> bool:
        """bol.com 2026: tap 'Wijzig betaalmethode' before choosing iDEAL."""
        for selector in (
            'button:has-text("Wijzig betaalmethode")',
            'a:has-text("Wijzig betaalmethode")',
            'button:has-text("Wijzig betaalmethode")',
            '[data-test*="change-payment"]',
            '[data-test*="payment-method-change"]',
        ):
            if await self._click_first(page, [selector], optional=True):
                log.info("Opened payment method picker (Wijzig betaalmethode)")
                await page.wait_for_timeout(700)
                return True
        return False

    async def _select_ideal(self, page: Page) -> None:
        await self._open_payment_method_picker(page)
        for selector in (
            'input[type="radio"][value*="ideal" i]',
            '[data-test*="ideal" i]',
            '[data-test*="payment-ideal"]',
            'input[value*="ideal" i]',
            'label:has-text("iDEAL")',
            'text=iDEAL',
        ):
            if await self._click_first(page, [selector], optional=True):
                log.info("Selected iDEAL payment")
                await page.wait_for_timeout(800)
                return
        try:
            ideal = page.get_by_text("iDEAL", exact=False).first
            if await ideal.is_visible(timeout=2000):
                await ideal.click(timeout=5000)
                log.info("Selected iDEAL payment (text)")
        except Exception:
            pass

    async def _select_afterpay(self, page: Page) -> None:
        """Keep/use Afterpay (BNPL) — faster 1-step checkout on selected products."""
        body = (await page.content()).lower()
        if any(
            x in body
            for x in ("afterpay", "achteraf betalen", "bol krediet", "betaal later")
        ):
            log.info("Afterpay/BNPL already active on checkout — no switch needed")
            return
        await self._open_payment_method_picker(page)
        for selector in (
            'input[type="radio"][value*="bnpl" i]',
            '[data-test*="bnpl" i]',
            '[data-test*="afterpay" i]',
            'label:has-text("Achteraf betalen")',
            'label:has-text("AfterPay")',
            'label:has-text("bol krediet")',
            'text=Achteraf betalen',
        ):
            if await self._click_first(page, [selector], optional=True):
                log.info("Selected Afterpay/BNPL payment")
                await page.wait_for_timeout(800)
                return

    async def _afterpay_order_confirmed(self, page: Page) -> bool:
        await page.wait_for_timeout(2500)
        url = (page.url or "").lower()
        if any(
            x in url
            for x in (
                "order-confirmation",
                "bedankt",
                "bestelling-bevestig",
                "order/confirm",
            )
        ):
            return True
        try:
            body = (await page.content()).lower()
        except Exception:
            return False
        for pat in (
            "bestelling is geplaatst",
            "bedankt voor je bestelling",
            "je bestelling is bevestigd",
            "bestelnummer",
        ):
            if pat in body:
                return True
        return False

    async def _submit_afterpay_order(self, page: Page) -> bool:
        for pat in (
            r"bestellen en betalen",
            r"plaats je bestelling",
            r"bestelling plaatsen",
            r"afrekenen",
        ):
            try:
                btn = page.get_by_role("button", name=re.compile(pat, re.I)).first
                if await btn.is_visible(timeout=1500):
                    await btn.click(timeout=12_000)
                    log.info("Submitted Afterpay/BNPL order")
                    return True
            except Exception:
                continue
        return False

    async def _select_card(self, page: Page) -> None:
        for selector in (
            '[data-test*="creditcard" i]',
            '[data-test*="credit-card" i]',
            'input[value*="card" i]',
            'label:has-text("Creditcard")',
            'label:has-text("Credit card")',
            'label:has-text("Betaalpas")',
        ):
            if await self._click_first(page, [selector], optional=True):
                log.info("Selected card payment")
                await page.wait_for_timeout(1000)
                return

    async def _fill_card(
        self,
        page: Page,
        payment: Dict[str, Any],
        profile: ProfileConfig,
    ) -> None:
        card = str(payment.get("card_number", DEFAULT_TEST_CARD["card_number"])).replace(" ", "")
        expiry = str(payment.get("expiry", DEFAULT_TEST_CARD["expiry"]))
        cvv = str(payment.get("cvv", DEFAULT_TEST_CARD["cvv"]))
        ship = profile.shipping
        holder = payment.get("holder") or (
            f"{ship.get('first_name', 'Jan')} {ship.get('last_name', 'Jansen')}".strip()
        )

        if not await self._has_adyen_iframes(page):
            return

        log.info("Filling test card details (Adyen fields)...")

        for sel in (
            'input[name*="holder" i]',
            'input[name*="cardHolder" i]',
            'input[autocomplete="cc-name"]',
        ):
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    await loc.fill(str(holder))
                    break
            except Exception:
                continue

        await self._fill_adyen_field(
            page,
            titles=("card number", "kaartnummer", "nummer"),
            value=card,
        )
        await self._fill_adyen_field(
            page,
            titles=("expiry", "vervaldatum", "expiration"),
            value=expiry.replace(" ", ""),
        )
        await self._fill_adyen_field(
            page,
            titles=("security code", "cvc", "cvv", "beveiligingscode"),
            value=cvv,
        )

    async def _fill_adyen_field(
        self, page: Page, *, titles: tuple[str, ...], value: str
    ) -> bool:
        for title in titles:
            try:
                frame = page.frame_locator(f'iframe[title*="{title}" i]').first
                inp = frame.locator("input").first
                if await inp.count():
                    await inp.fill(value)
                    return True
            except Exception:
                pass
        for frame in page.frames:
            name = (frame.name or "").lower()
            furl = frame.url.lower()
            if not any(t in name or t in furl for t in titles):
                continue
            try:
                inp = frame.locator("input").first
                if await inp.count():
                    await inp.fill(value)
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _page_locator(page: Page, selector: str):
        if selector.startswith("//") or selector.startswith("(//"):
            return page.locator(f"xpath={selector}").first
        return page.locator(selector).first

    async def _click_first(
        self, page: Page, selectors: List[str], *, optional: bool = False
    ) -> bool:
        for selector in selectors:
            try:
                loc = self._page_locator(page, selector)
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=5000)
                    return True
            except Exception:
                continue
        if not optional:
            log.debug(f"No clickable match for: {selectors[:3]}")
        return False

    async def _extract_payment_url(self, page: Page) -> Optional[str]:
        await page.wait_for_timeout(1500)
        url = page.url
        if any(k in url.lower() for k in ("ideal", "adyen", "payment", "checkout.ideal")):
            return url
        for selector in ('a[href*="ideal"]', 'a[href*="adyen"]', 'a[href*="payment"]'):
            try:
                href = await page.locator(selector).first.get_attribute("href")
                if href and href.startswith("http"):
                    return href
            except Exception:
                pass
        content = await page.content()
        m = re.search(
            r'https?://[^\s"\']+(?:ideal|adyen|payment)[^\s"\']*',
            content,
            re.I,
        )
        return m.group(0) if m else None

    async def load_cookies_from_dict(
        self, cookies: Dict[str, str], domain: str = ".bol.com"
    ) -> None:
        from src.sites.bol_cookies import cookie_domains

        await self._context.clear_cookies()
        pw_cookies: list[dict] = []
        for k, v in cookies.items():
            if not v:
                continue
            for dom in cookie_domains(k) or [domain]:
                pw_cookies.append(
                    {"name": k, "value": v, "domain": dom, "path": "/"}
                )
        if pw_cookies:
            await self._context.add_cookies(pw_cookies)
