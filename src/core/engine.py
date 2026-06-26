from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional


@asynccontextmanager
async def _null_async_context() -> AsyncIterator[None]:
    yield

import aiohttp

from src.checkout.playwright_flow import BrowserPool
from src.config.settings import get_settings
from src.core.events import BotEvent, BotEventType, EventBus
from src.models.product import ProductState, StockStatus
from src.models.session import CartResult
from src.models.task import ProfileConfig, TaskConfig
from src.monitors.adaptive import AdaptiveMonitor
from src.notifications.config import resolve_discord_webhook_url
from src.notifications.discord import (
    send_atc_discord_notification,
    send_checkout_discord_notification,
)
from src.proxy.manager import ProxyManager, ProxyState
from src.sites.registry import get_site_adapter
from src.tasks.loader import TaskStore
from src.utils.http import build_client_session
from src.utils.logging import get_logger, setup_logging
from src.utils.retry import RetryPolicy, with_retry

log = get_logger("engine")


class TaskRunner:
    """Runs a single product monitor + ATC + checkout pipeline."""

    def __init__(
        self,
        task: TaskConfig,
        profile: ProfileConfig,
        proxy_manager: ProxyManager,
        event_bus: EventBus,
        browser_pool: BrowserPool,
        semaphore: asyncio.Semaphore,
        bol_pipeline_lock: Optional[asyncio.Lock] = None,
    ) -> None:
        self.task = task
        self.profile = profile
        self.proxy_manager = proxy_manager
        self.event_bus = event_bus
        self.browser_pool = browser_pool
        self.semaphore = semaphore
        self._bol_pipeline_lock = bol_pipeline_lock
        self._log = get_logger(task.id)
        self._site = get_site_adapter(task.site)
        self._session: Optional[aiohttp.ClientSession] = None
        self._monitor: Optional[AdaptiveMonitor] = None
        self._cancelled = False
        self._bol_force_relogin_next = False
        self._bol_akamai_warned = False
        self._cart_verified = False
        self._atc_running = False
        self._last_atc_attempt = 0.0
        self._was_available = False
        self._block_streak = 0
        self._direct_fallback_paused_until = 0.0
        self._checkout_in_progress = False
        self._monitor_paused = False

    @staticmethod
    def _poll_stagger_sec(task_id: str) -> float:
        """Spread multi-task polls so monitors don't hit bol.com at once."""
        return (hash(task_id) % 1000) / 1000.0

    def _extra_poll_sleep(self) -> float:
        if self._block_streak < 3:
            return 0.0
        return min(30.0, 5.0 * (self._block_streak - 2))

    def _direct_fallback_allowed(self) -> bool:
        return time.monotonic() >= self._direct_fallback_paused_until

    def _note_poll_result(self, state: ProductState, *, used_direct: bool) -> None:
        if state.raw.get("source") == "graphql":
            self._block_streak = max(0, self._block_streak - 1)
            return
        if state.raw.get("akamai_block") or (
            state.status == StockStatus.UNKNOWN and state.error
        ):
            self._block_streak += 1
            if used_direct:
                pause = min(180.0, 30.0 + 15.0 * self._block_streak)
                self._direct_fallback_paused_until = time.monotonic() + pause
                if self._block_streak == 1 or self._block_streak % 5 == 0:
                    self._log.info(
                        f"Direct fetch blocked — pausing direct fallback {pause:.0f}s "
                        f"(use same IP as login.txt or BOL_MONITOR_DIRECT=1)"
                    )
        else:
            self._block_streak = max(0, self._block_streak - 1)

    async def _get_proxy(self) -> Optional[str]:
        import os

        if os.environ.get("BOL_MONITOR_DIRECT", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            return None
        proxy = await self.proxy_manager.acquire(self.task.proxy_group)
        if proxy:
            return proxy
        if self.task.site == "bol":
            return await self.proxy_manager.acquire("roundproxies")
        return None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = build_client_session(
                timeout_sec=self.task.retry.request_timeout_sec,
            )
            if self.task.site == "bol":
                from src.sites.bol_session import apply_cookies_to_session

                n = apply_cookies_to_session(self._session)
                if n:
                    self._log.debug(f"Loaded {n} bol.com cookies from bol_token.json")
                else:
                    self._log.warning(
                        "No bol_token.json cookies — run: python main.py --import-cookies"
                    )
        return self._session

    async def _refresh_bol_session_after_block(
        self, session: aiohttp.ClientSession
    ) -> Optional[ProductState]:
        from src.sites.bol_session import (
            may_run_proxy_seed,
            proxy_seed_cooldown_sec,
            seed_session_via_proxy,
        )
        from src.sites.bol_urls import resolve_product_url

        if not may_run_proxy_seed():
            if not self._bol_akamai_warned:
                self._bol_akamai_warned = True
                mins = int(proxy_seed_cooldown_sec() / 60)
                self._log.warning(
                    f"Product page blocked by Akamai (curl). "
                    f"Import cookies from Chrome — auto re-seed paused {mins}m. "
                    f"Set BOL_STARTUP_SEED=1 only if you need Playwright seed."
                )
            return

        pid = str(self.task.metadata.get("product_id") or "").strip()
        product_url = resolve_product_url(
            pid, str(self.task.product_url), self.task.metadata
        )
        proxy = await self._get_proxy()
        if proxy:
            self._log.info("Akamai block — one Playwright re-seed attempt (cooldown applies)...")
            seeded, seed_status, seed_html = await seed_session_via_proxy(product_url, proxy)
            if seeded and seed_html:
                # Playwright already fetched the live page — use it directly.
                # Do NOT re-fetch via tls_client/curl: they can't maintain Akamai's
                # sensor state without a real browser executing JavaScript, so they
                # will get 403 immediately after the seed.
                self._log.info(
                    f"Proxy seed succeeded — using Playwright HTML directly "
                    f"({len(seed_html)} chars, HTTP {seed_status})"
                )
                return await self._site.fetch_state(
                    session, self.task, proxy,
                    cached_html=seed_html, cached_status=seed_status,
                )
        return None  # no cached result — caller will re-fetch normally

    @staticmethod
    def _bol_poll_unreadable(state: ProductState) -> bool:
        if state.raw.get("akamai_block") or state.raw.get("placeholder"):
            return True
        return state.status == StockStatus.UNKNOWN

    @staticmethod
    def _bol_prefer_state(candidate: ProductState, current: ProductState) -> bool:
        if candidate.raw.get("akamai_block") and not current.raw.get("akamai_block"):
            return False
        if current.raw.get("akamai_block") and not candidate.raw.get("akamai_block"):
            return True
        if candidate.status == StockStatus.UNKNOWN:
            return False
        if current.status == StockStatus.UNKNOWN:
            return True
        if candidate.is_available and not current.is_available:
            return True
        if candidate.is_live and not current.is_live:
            return True
        return False

    async def _poll(self) -> ProductState:
        if self._monitor_paused:
            return ProductState(
                url=self.task.url,
                status=StockStatus.OUT_OF_STOCK,
                can_add_to_cart=False,
                raw={"skipped": "atc_or_checkout_in_progress"},
            )
        if self._cancelled:
            return ProductState(
                url=self.task.url,
                status=StockStatus.OUT_OF_STOCK,
                can_add_to_cart=False,
                raw={"skipped": "monitor_stopped"},
            )

        from src.sites.bol_fetch_gate import bol_fetch_gate

        proxy = await self._get_proxy()
        session = await self._ensure_session()
        started = time.monotonic()
        used_direct = False
        try:
            async with bol_fetch_gate():
                if proxy:
                    self._log.debug(f"Poll via {proxy.split('@')[-1][:50]}")
                state = await self._site.fetch_state(session, self.task, proxy)

                if self.task.site == "bol" and proxy and self._bol_poll_unreadable(state):
                    group = self.task.proxy_group or ""
                    self.proxy_manager.report_failure(
                        proxy, group, ProxyState.RATE_LIMITED
                    )
                    alt_proxy = await self._get_proxy()
                    if alt_proxy and alt_proxy != proxy:
                        self._log.info("Proxy stub/block — rotating to next proxy session...")
                        alt_state = await self._site.fetch_state(
                            session, self.task, alt_proxy
                        )
                        if self._bol_prefer_state(alt_state, state):
                            state = alt_state
                            proxy = alt_proxy

                if self.task.site == "bol" and proxy:
                    from src.sites.bol_session import direct_fallback_enabled

                    if (
                        direct_fallback_enabled()
                        and self._direct_fallback_allowed()
                        and self._bol_poll_unreadable(state)
                    ):
                        self._log.info(
                            "Proxy unreadable — retrying product page direct (no proxy)..."
                        )
                        used_direct = True
                        direct_state = await self._site.fetch_state(session, self.task, None)
                        if self._bol_prefer_state(direct_state, state):
                            state = direct_state

                from src.sites.bol_session import poll_reseed_enabled

                if (
                    poll_reseed_enabled()
                    and self.task.site == "bol"
                    and self._bol_poll_unreadable(state)
                    and proxy
                ):
                    cached = await self._refresh_bol_session_after_block(session)
                    if cached is not None and self._bol_prefer_state(cached, state):
                        state = cached

            if state.is_available:
                self._log.success(
                    f"BUYABLE — add-to-cart available | {state.status.value} | ATC=yes"
                )

            if self.task.site == "bol":
                self._note_poll_result(state, used_direct=used_direct)

            if proxy and not state.raw.get("akamai_block"):
                latency = (time.monotonic() - started) * 1000
                self.proxy_manager.report_success(
                    proxy, self.task.proxy_group or "", latency
                )
            self._schedule_atc_if_needed(state)
            return state
        except aiohttp.ClientResponseError as exc:
            if proxy and self.task.proxy_group:
                self.proxy_manager.report_failure(
                    proxy,
                    self.task.proxy_group,
                    self.proxy_manager.classify_error(exc.status),
                )
            raise
        except Exception as exc:
            if proxy and self.task.proxy_group:
                self.proxy_manager.report_failure(
                    proxy, self.task.proxy_group, ProxyState.TIMEOUT
                )
            raise exc

    @staticmethod
    def _atc_retry_cooldown_sec() -> float:
        raw = os.environ.get("BOL_ATC_RETRY_SEC", "8").strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 8.0

    def _schedule_atc_if_needed(self, state: ProductState) -> None:
        """Run bol_cart while buyable until cart is verified (with retry cooldown)."""
        if self._cancelled or self._monitor_paused or self._atc_running:
            return
        if not state.is_available:
            self._was_available = False
            return

        if not self._was_available:
            self._cart_verified = False
            self._last_atc_attempt = 0.0
        self._was_available = True

        if self._cart_verified or self._atc_running:
            return

        now = time.monotonic()
        cooldown = self._atc_retry_cooldown_sec()
        if self._last_atc_attempt and (now - self._last_atc_attempt) < cooldown:
            return

        self._last_atc_attempt = now
        self._atc_running = True
        asyncio.create_task(self._handle_stock())

    async def _on_state_change(
        self, current: ProductState, previous: Optional[ProductState]
    ) -> None:
        await self.event_bus.emit(
            BotEvent(
                type=BotEventType.STATE_CHANGE,
                task_id=self.task.id,
                payload={"status": current.status.value},
                state=current,
            )
        )
        if current.transitioned_to_available(previous):
            await self.event_bus.emit(
                BotEvent(
                    type=BotEventType.STOCK_FOUND,
                    task_id=self.task.id,
                    payload={},
                    state=current,
                )
            )

    def _atc_notification_payload(self, result: CartResult) -> dict:
        from src.sites.bol_urls import BOL_BASKET_URL, BOL_CHECKOUT_URL, resolve_product_url

        pid = str(self.task.metadata.get("product_id") or "").strip()
        product_url = resolve_product_url(
            pid, str(self.task.product_url), self.task.metadata
        )
        offer_uid = (
            getattr(self._site, "_cached_offer_uid", None)
            or os.environ.get("BOL_OFFER_UID", "").strip()
        )
        return {
            "verified": result.verified,
            "cart_id": result.cart_id,
            "product_url": product_url,
            "product_id": pid,
            "offer_uid": offer_uid or None,
            "basket_url": BOL_BASKET_URL,
            "checkout_url": BOL_CHECKOUT_URL,
            "payment_method": (self.task.payment_method or "ideal").strip().lower(),
            "quantity": getattr(self._site, "_last_atc_quantity", None)
            or self.task.quantity
            or 1,
        }

    def _checkout_notification_payload(
        self, result_payload: dict, checkout_result: Any
    ) -> dict:
        base = dict(result_payload)
        base.update(
            {
                "payment_url": checkout_result.payment_url,
                "checkout_url": checkout_result.checkout_url,
                "stage": checkout_result.stage,
                "payment_method": (self.task.payment_method or "ideal").strip().lower(),
            }
        )
        return base

    async def _handle_stock(self) -> None:
        if self._cart_verified:
            return
        self._monitor_paused = True
        if self.task.auto_checkout:
            self._checkout_in_progress = True
        pipeline_lock = (
            self._bol_pipeline_lock
            if self.task.site == "bol" and self._bol_pipeline_lock is not None
            else None
        )
        try:
            async with self.semaphore:
                if pipeline_lock is not None:
                    self._log.info("Waiting for bol ATC/checkout lock...")
                lock_ctx = pipeline_lock if pipeline_lock is not None else _null_async_context()
                async with lock_ctx:
                    if pipeline_lock is not None:
                        self._log.warning(
                            "STOCK — initiating ATC pipeline (bol lock acquired)"
                        )
                    else:
                        self._log.warning("STOCK — initiating ATC pipeline")
                    policy = RetryPolicy(
                        max_attempts=self.task.retry.atc_max_attempts,
                        base_delay_ms=self.task.retry.atc_base_delay_ms,
                        max_delay_ms=self.task.retry.atc_max_delay_ms,
                    )

                    async def _atc_only() -> dict:
                        proxy = (
                            getattr(self._site, "_last_poll_proxy", None)
                            or await self._get_proxy()
                        )
                        session = await self._ensure_session()
                        result = await self._site.add_to_cart(
                            session, self.task, self.profile, proxy
                        )
                        if not result.success:
                            raise RuntimeError(result.message)
                        verified = await self._site.verify_cart(
                            session, self.task, proxy
                        )
                        result.verified = verified
                        self._cart_verified = True
                        atc_payload = self._atc_notification_payload(result)
                        self._log.success(
                            f"ATC OK — item in cart"
                            + (f" (basket {result.cart_id})" if result.cart_id else "")
                        )
                        await self.event_bus.emit(
                            BotEvent(
                                type=BotEventType.ATC_SUCCESS,
                                task_id=self.task.id,
                                payload=atc_payload,
                            )
                        )
                        return atc_payload

                    atc_payload: Optional[dict] = None
                    try:
                        atc_payload = await with_retry(_atc_only, policy=policy)
                    except Exception as exc:
                        err = str(exc).lower()
                        cart_already = any(
                            k in err
                            for k in (
                                "already in cart",
                                "already in basket",
                                "proceeding to checkout",
                                "failedtoadditemtobasketproblem",
                                "itemisalreadyinbasketproblem",
                            )
                        )
                        if cart_already and self.task.auto_checkout:
                            self._log.warning(
                                "ATC add failed but product may be in cart — "
                                "proceeding to checkout"
                            )
                            from src.sites.bol import load_basket_id

                            atc_payload = self._atc_notification_payload(
                                CartResult(
                                    success=True,
                                    verified=True,
                                    cart_id=load_basket_id(),
                                    message="soft atc — already in cart",
                                )
                            )
                            atc_payload["soft_atc"] = True
                            self._cart_verified = True
                            await self.event_bus.emit(
                                BotEvent(
                                    type=BotEventType.ATC_SUCCESS,
                                    task_id=self.task.id,
                                    payload=atc_payload,
                                )
                            )
                        else:
                            self._log.error(f"ATC failed: {exc}")
                            await self.event_bus.emit(
                                BotEvent(
                                    type=BotEventType.ATC_FAILED,
                                    task_id=self.task.id,
                                    payload={
                                        **self._atc_notification_payload(
                                            CartResult(success=False, message=str(exc))
                                        ),
                                        "error": str(exc),
                                    },
                                )
                            )
                            self._resume_monitoring_after_atc_failure(
                                "ATC failed — resuming stock monitor"
                            )
                    if atc_payload and self.task.auto_checkout:
                        self._checkout_in_progress = True
                        try:
                            session = await self._ensure_session()
                            self._log.info("ATC done — starting checkout immediately")
                            await self._run_checkout(session, atc_payload)
                        finally:
                            self._checkout_in_progress = False
                    elif atc_payload and self._cart_verified:
                        self._monitor_paused = False
        finally:
            self._atc_running = False
            if not self._cart_verified:
                self._checkout_in_progress = False
                if self._monitor_paused:
                    self._monitor_paused = False

    def _resume_monitoring_after_atc_failure(self, reason: str) -> None:
        """Resume polling after ATC failed so the next drop can be caught."""
        self._checkout_in_progress = False
        self._monitor_paused = False
        self._cart_verified = False
        self._was_available = False
        self._log.warning(reason)

    def _resume_monitoring_after_checkout_failure(self, reason: str) -> None:
        """Resume polling after checkout failed (cart may still hold the item)."""
        self._checkout_in_progress = False
        self._monitor_paused = False
        self._cart_verified = False
        self._was_available = False
        self._last_atc_attempt = 0.0
        self._log.warning(reason)

    def _stop_monitoring(self) -> None:
        self._cancelled = True
        if self._monitor:
            self._monitor.stop()
        self._log.info("Monitor stopped")

    async def _open_checkout_browser(self, checkout_proxy: Optional[str]) -> Any:
        """Lazy Playwright context — only when HTTP checkout needs a browser fallback."""
        from src.checkout.playwright_flow import PlaywrightCheckout
        from src.sites.bol_session import fetch_product_page_playwright

        skip_seed = os.environ.get("BOL_SKIP_BROWSER_SEED", "").strip().lower() in {
            "1",
            "true",
            "yes",
        } or os.environ.get("BOL_SKIP_CHECKOUT_PRIME", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        st, html = 0, ""
        if skip_seed:
            self._log.info(
                "Skipping Playwright basket seed — HTTP checkout session already warm"
            )
        else:
            st, html = await fetch_product_page_playwright(
                "https://www.bol.com/nl/nl/basket/",
                proxy_url=checkout_proxy,
                save_token=False,
            )
            self._log.info(
                f"Pre-checkout Akamai seed (basket URL): HTTP {st}, {len(html)} chars"
            )
        ctx = await self.browser_pool.get_checkout_context(
            self.profile.name, proxy_url=checkout_proxy
        )
        pw = PlaywrightCheckout(ctx, profile_name=self.profile.name)
        prefer_token = not skip_seed and st == 200 and len(html) > 50_000
        await pw.reload_bol_cookies(prefer_fresh_token=prefer_token)
        self._log.info(
            "Loaded cookies into checkout browser"
            + (" (token + storage_state)" if prefer_token else " (token + login.txt)")
        )
        return ctx

    async def _run_checkout(
        self, http_session: aiohttp.ClientSession, atc_payload: dict
    ) -> None:
        checkout_proxy: Optional[str] = None
        if self.task.site == "bol":
            from src.proxy.bol_proxy import proxy_label
            from src.sites.bol import BolSiteAdapter

            if hasattr(self._site, "clear_http_checkout_cache"):
                self._site.clear_http_checkout_cache()
            checkout_proxy = (
                getattr(self._site, "_last_poll_proxy", None)
                or await self._get_proxy()
            )
            BolSiteAdapter.configure_checkout_proxy(checkout_proxy)
            os.environ["BOL_SKIP_CHECKOUT_PRIME"] = "1"
            if checkout_proxy:
                self._log.info(
                    f"Checkout using monitor proxy ({proxy_label(checkout_proxy)})"
                )
            else:
                self._log.warning(
                    "Checkout on home IP — use RoundProxies + login.txt from same IP"
                )

        use_playwright = os.environ.get("BOL_CHECKOUT_PLAYWRIGHT", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        pay_raw = (self.task.payment_method or "ideal").strip().lower()
        use_afterpay = pay_raw in (
            "afterpay",
            "bnpl",
            "achteraf",
            "bol_krediet",
            "pay_later",
        )
        if use_afterpay:
            pay_label = "Afterpay/BNPL (iDEAL backup if unavailable)"
        else:
            pay_label = "iDEAL (wijzig betaalmethode + bank)"
        if use_playwright:
            checkout_via = " via Playwright"
        elif self.task.site == "bol":
            checkout_via = " via HTTP (browser only if needed)"
        else:
            checkout_via = " via HTTP + browser"
        self._log.info(f"Starting checkout — {pay_label}{checkout_via}")
        browser_attempts = min(
            self.task.retry.checkout_max_attempts,
            int(os.environ.get("BOL_CHECKOUT_BROWSER_ATTEMPTS", "2")),
        )
        policy = RetryPolicy(
            max_attempts=browser_attempts,
            base_delay_ms=800,
            max_delay_ms=4000,
        )

        from src.utils.profile_resolve import resolve_profile

        profile = resolve_profile(self.profile)
        payment_method = (self.task.payment_method or profile.payment_method or "ideal").strip()
        profile = profile.model_copy(update={"payment_method": payment_method})
        ctx: Any = None
        if use_playwright:
            ctx = await self._open_checkout_browser(checkout_proxy)
        elif self.task.site != "bol":
            ctx = await self.browser_pool.get_checkout_context(
                self.profile.name, proxy_url=checkout_proxy
            )

        async def _checkout_once() -> None:
            nonlocal ctx
            result = await self._site.checkout(ctx, self.task, profile)
            if (
                not result.success
                and ctx is None
                and self.task.site == "bol"
                and not use_playwright
            ):
                self._log.info(
                    "HTTP checkout incomplete — opening browser for Bestellen en betalen"
                )
                ctx = await self._open_checkout_browser(checkout_proxy)
                result = await self._site.checkout(ctx, self.task, profile)
            if not result.success:
                raise RuntimeError(result.message or "Checkout failed")
            if ctx is not None:
                await self.browser_pool.save_context(self.profile.name)
            payload = self._checkout_notification_payload(atc_payload, result)
            await self.event_bus.emit(
                BotEvent(
                    type=BotEventType.CHECKOUT_SUCCESS,
                    task_id=self.task.id,
                    payload=payload,
                )
            )
            if result.stage == "afterpay_order":
                self._log.success("Checkout OK — Afterpay/BNPL order placed (no bank redirect)")
            else:
                self._log.success(
                    f"Checkout OK — stage={result.stage}"
                    + (f" | iDEAL: {result.payment_url}" if result.payment_url else "")
                )
            self._stop_monitoring()

        try:
            await with_retry(_checkout_once, policy=policy)
        except Exception as exc:
            self._log.error(f"Checkout failed: {exc}")
            fail_payload = {**atc_payload, "error": str(exc)}
            await self.event_bus.emit(
                BotEvent(
                    type=BotEventType.CHECKOUT_FAILED,
                    task_id=self.task.id,
                    payload=fail_payload,
                )
            )
            self._resume_monitoring_after_checkout_failure(
                "Checkout failed — resuming monitor (item may still be in cart)"
            )

    async def run(self) -> None:
        if self.task.site == "bol":
            from src.proxy.bol_proxy import get_roundproxies_config, get_roundproxies_pool
            from src.sites.bol_session import (
                has_akamai_cookie,
                seed_session_via_proxy,
                startup_playwright_seed_enabled,
            )
            from src.sites.bol_urls import resolve_product_url

            pool = get_roundproxies_pool()
            rp_cfg = get_roundproxies_config()
            country = rp_cfg.country if rp_cfg else "none"
            if pool:
                self._log.info(
                    f"RoundProxies enabled ({len(pool)} sessions, country={country})"
                )
                if country.lower().replace("-", "") != "netherlands":
                    self._log.warning(
                        f"Proxy country is {country} — bol.nl works best with Netherlands. "
                        "Create an NL proxy in RoundProxies dashboard."
                    )
            else:
                self._log.warning(
                    "No RoundProxies configured — add config/roundproxies.yaml"
                )
            proxy = await self._get_proxy()
            pid = str(self.task.metadata.get("product_id") or "").strip()
            monitor_url = resolve_product_url(
                pid, str(self.task.product_url), self.task.metadata
            )
            if pool and proxy and startup_playwright_seed_enabled():
                self._log.info("Seeding Akamai cookies through proxy (BOL_STARTUP_SEED=1)...")
                seeded, seed_status, seed_html = await seed_session_via_proxy(
                    monitor_url, proxy
                )
                if seeded:
                    self._log.info(
                        f"Startup seed OK — product page readable "
                        f"({len(seed_html)} chars, HTTP {seed_status})"
                    )
                else:
                    self._log.info(
                        "Startup seed done — monitor will use direct fallback if proxy stubbed"
                    )
            elif not has_akamai_cookie():
                self._log.info(
                    "No Akamai _abck yet — import login.txt or set BOL_STARTUP_SEED=1"
                )
        stagger = self._poll_stagger_sec(self.task.id)
        if stagger > 0:
            self._log.debug(f"Poll stagger {stagger:.1f}s (multi-task spacing)")
            await asyncio.sleep(stagger)
        self._monitor = AdaptiveMonitor(
            task_id=self.task.id,
            poll_fn=self._poll,
            polling=self.task.polling,
            on_state_change=self._on_state_change,
            extra_sleep_fn=self._extra_poll_sleep,
        )
        await self._monitor.run()

    async def stop(self) -> None:
        self._cancelled = True
        if self._monitor:
            self._monitor.stop()
        if self._session and not self._session.closed:
            await self._session.close()


class BotEngine:
    """Orchestrates concurrent task runners with hot-reload support."""

    def __init__(self, task_store: TaskStore) -> None:
        self.settings = get_settings()
        self.task_store = task_store
        self.event_bus = EventBus()
        self.proxy_manager = ProxyManager(task_store.proxy_groups)
        self.browser_pool = BrowserPool(headless=self.settings.headless)
        self._runners: Dict[str, TaskRunner] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrent_tasks)
        self._bol_pipeline_locks: Dict[str, asyncio.Lock] = {}
        self._running = False

    def _bol_lock_for_task(self, task: TaskConfig) -> Optional[asyncio.Lock]:
        if task.site != "bol":
            return None
        pid = str(task.metadata.get("product_id") or task.id).strip()
        if pid not in self._bol_pipeline_locks:
            self._bol_pipeline_locks[pid] = asyncio.Lock()
        return self._bol_pipeline_locks[pid]

    def _playwright_required(self) -> bool:
        if os.environ.get("BOL_CHECKOUT_PLAYWRIGHT", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            return True
        return any(t.site != "bol" for t in self.task_store.get_enabled_tasks())

    def _wire_events(self) -> None:
        async def log_stock(ev: BotEvent) -> None:
            get_logger(ev.task_id).success(f"Event: {ev.type.value}")

        async def notify_stock_discord(ev: BotEvent) -> None:
            webhook_url = resolve_discord_webhook_url()
            if not webhook_url or ev.state is None:
                return
            from src.notifications.discord import send_stock_detected_discord
            from src.sites.bol_urls import resolve_product_url

            runner = self._runners.get(ev.task_id)
            task = runner.task if runner else None
            pid = ""
            product_url = ev.state.url
            if task:
                pid = str(task.metadata.get("product_id") or "").strip()
                product_url = resolve_product_url(
                    pid, str(task.product_url), task.metadata
                )
            summary = (
                "IN STOCK — buyable"
                if ev.state.is_available
                else ev.state.status.value
            )
            await send_stock_detected_discord(
                webhook_url,
                task_id=ev.task_id,
                product_url=product_url,
                product_id=pid,
                status_summary=summary,
            )

        async def notify_atc_discord(ev: BotEvent) -> None:
            webhook_url = resolve_discord_webhook_url()
            if not webhook_url:
                return
            p = ev.payload
            await send_atc_discord_notification(
                webhook_url,
                task_id=ev.task_id,
                product_url=p.get("product_url", ""),
                product_id=p.get("product_id", ""),
                basket_url=p.get("basket_url", ""),
                checkout_url=p.get("checkout_url", ""),
                basket_id=p.get("cart_id"),
                quantity=int(p.get("quantity") or 1),
                payment_method=str(p.get("payment_method") or "ideal"),
            )

        async def notify_checkout_discord(ev: BotEvent) -> None:
            webhook_url = resolve_discord_webhook_url()
            if not webhook_url:
                return
            p = ev.payload
            stage = str(p.get("stage") or "ideal_payment").strip().lower()
            payment_url = (p.get("payment_url") or "").strip()
            afterpay_stages = {
                "afterpay_order",
                "bnpl_order_placed",
                "afterpay",
            }

            if stage in afterpay_stages:
                await send_checkout_discord_notification(
                    webhook_url,
                    task_id=ev.task_id,
                    product_url=p.get("product_url", ""),
                    product_id=p.get("product_id", ""),
                    payment_url=None,
                    checkout_url=p.get("checkout_url"),
                    basket_url=p.get("basket_url"),
                    basket_id=p.get("cart_id"),
                    stage="afterpay_order",
                    quantity=int(p.get("quantity") or 1),
                )
                return

            from src.checkout.playwright_flow import is_ideal_payment_url

            if payment_url and is_ideal_payment_url(payment_url):
                from src.utils.payment_log import append_payment_url

                append_payment_url(
                    pay_url=payment_url,
                    product_url=p.get("product_url", ""),
                    product_id=p.get("product_id", ""),
                    offer_uid=p.get("offer_uid"),
                    seller="bol",
                )
                await send_checkout_discord_notification(
                    webhook_url,
                    task_id=ev.task_id,
                    product_url=p.get("product_url", ""),
                    product_id=p.get("product_id", ""),
                    payment_url=payment_url,
                    checkout_url=p.get("checkout_url"),
                    basket_url=p.get("basket_url"),
                    basket_id=p.get("cart_id"),
                    stage=stage,
                    quantity=int(p.get("quantity") or 1),
                )
                return

            await send_checkout_discord_notification(
                webhook_url,
                task_id=ev.task_id,
                product_url=p.get("product_url", ""),
                product_id=p.get("product_id", ""),
                payment_url=payment_url or None,
                checkout_url=p.get("checkout_url"),
                basket_url=p.get("basket_url"),
                basket_id=p.get("cart_id"),
                stage="payment_ready" if payment_url else stage,
                quantity=int(p.get("quantity") or 1),
                partial=True,
            )

        async def notify_atc_failed_discord(ev: BotEvent) -> None:
            webhook_url = resolve_discord_webhook_url()
            if not webhook_url:
                return
            p = ev.payload
            from src.notifications.discord import send_atc_failed_discord

            await send_atc_failed_discord(
                webhook_url,
                task_id=ev.task_id,
                product_url=p.get("product_url", ""),
                product_id=p.get("product_id", ""),
                error=p.get("error", "ATC failed"),
                basket_url=p.get("basket_url", BOL_BASKET_URL),
            )

        async def notify_checkout_failed_discord(ev: BotEvent) -> None:
            webhook_url = resolve_discord_webhook_url()
            if not webhook_url:
                return
            p = ev.payload
            from src.notifications.discord import send_checkout_failed_discord

            await send_checkout_failed_discord(
                webhook_url,
                task_id=ev.task_id,
                product_url=p.get("product_url", ""),
                product_id=p.get("product_id", ""),
                error=p.get("error", "Checkout failed"),
                basket_url=p.get("basket_url", BOL_BASKET_URL),
                checkout_url=p.get("checkout_url", BOL_CHECKOUT_URL),
                basket_id=p.get("cart_id"),
            )

        from src.sites.bol_urls import BOL_BASKET_URL, BOL_CHECKOUT_URL

        self.event_bus.on(BotEventType.STOCK_FOUND, log_stock)
        self.event_bus.on(BotEventType.STOCK_FOUND, notify_stock_discord)
        self.event_bus.on(BotEventType.ATC_SUCCESS, notify_atc_discord)
        self.event_bus.on(BotEventType.ATC_FAILED, notify_atc_failed_discord)
        self.event_bus.on(BotEventType.CHECKOUT_SUCCESS, notify_checkout_discord)
        self.event_bus.on(BotEventType.CHECKOUT_FAILED, notify_checkout_failed_discord)

    async def start(self) -> None:
        setup_logging(self.settings.log_level, self.settings.log_file)
        if any(t.site == "bol" for t in self.task_store.get_enabled_tasks()):
            from src.utils.http_backend import log_bol_http_backends

            log_bol_http_backends()
        self._wire_events()
        if resolve_discord_webhook_url():
            log.info(
                "Discord webhook enabled — stock + ATC + checkout alerts"
            )
        else:
            log.warning(
                "No Discord webhook — set config/discord.yaml or ECOM_DISCORD_WEBHOOK_URL"
            )
        if self._playwright_required():
            log.info("Playwright browser pool will start when checkout needs it")
        else:
            log.info(
                "HTTP checkout mode — Playwright browser not required at startup"
            )
        self._running = True
        if any(t.site == "bol" for t in self.task_store.get_enabled_tasks()):
            from src.sites.bol_session import startup_bol_login

            login_msg = await startup_bol_login()
            log.info(f"Bol session: {login_msg}")
        log.info("Bot engine started")
        asyncio.create_task(self._hot_reload_loop())
        await self._sync_tasks()

    async def _sync_tasks(self) -> None:
        configs = self.task_store.get_enabled_tasks()
        active_ids = {t.id for t in configs}

        for tid in list(self._tasks.keys()):
            if tid not in active_ids:
                await self._stop_task(tid)

        for task in configs:
            if task.id not in self._tasks:
                await self._start_task(task)

    async def _start_task(self, task: TaskConfig) -> None:
        profile = self.task_store.get_profile(task.profile)
        runner = TaskRunner(
            task,
            profile,
            self.proxy_manager,
            self.event_bus,
            self.browser_pool,
            self._semaphore,
            bol_pipeline_lock=self._bol_lock_for_task(task),
        )
        self._runners[task.id] = runner
        self._tasks[task.id] = asyncio.create_task(
            runner.run(), name=f"task-{task.id}"
        )
        log.info(f"Started task {task.id} | {task.product_url}")

    async def _stop_task(self, task_id: str) -> None:
        runner = self._runners.pop(task_id, None)
        t = self._tasks.pop(task_id, None)
        if runner:
            await runner.stop()
        if t:
            t.cancel()
        log.info(f"Stopped task {task_id}")

    async def _hot_reload_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.settings.hot_reload_interval_sec)
            try:
                if await self.task_store.reload_if_changed():
                    log.info("Tasks config changed — hot-reloading")
                    await self._sync_tasks()
            except Exception as exc:
                log.warning(f"Hot reload error: {exc}")

    async def run_forever(self) -> None:
        from src.utils.licence import enforce_licence

        enforce_licence()
        await self.start()
        try:
            while self._running:
                enforce_licence()
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self._running = False
        for tid in list(self._tasks.keys()):
            await self._stop_task(tid)
        await self.browser_pool.close()
        log.info("Bot engine shutdown complete")
