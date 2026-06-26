from __future__ import annotations

from typing import Optional

import aiohttp

from src.utils.logging import get_logger

log = get_logger("discord")


def _post_payload(
    *,
    title: str,
    content: str,
    description: str,
    color: int,
    url: Optional[str] = None,
) -> dict:
    embed: dict = {
        "title": title,
        "description": description,
        "color": color,
    }
    if url:
        embed["url"] = url
    return {
        "username": "BOL-BOT",
        "content": content,
        "embeds": [embed],
    }


async def _send_webhook(webhook_url: str, payload: dict, *, task_id: str) -> bool:
    import asyncio

    for attempt in range(3):
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(webhook_url, json=payload) as resp:
                    if resp.status in (200, 204):
                        log.success(f"Discord webhook sent for {task_id}")
                        return True
                    body = await resp.text()
                    log.error(
                        f"Discord webhook HTTP {resp.status} (attempt {attempt + 1}): "
                        f"{body[:200]}"
                    )
        except Exception as exc:
            log.error(f"Discord webhook failed (attempt {attempt + 1}): {exc}")
        if attempt < 2:
            await asyncio.sleep(1.5 * (attempt + 1))
    return False


async def send_atc_discord_notification(
    webhook_url: str,
    *,
    task_id: str,
    product_url: str,
    product_id: str,
    basket_url: str,
    checkout_url: str,
    basket_id: Optional[str] = None,
    quantity: int = 1,
    payment_method: str = "ideal",
) -> bool:
    """Short alert while checkout automation runs."""
    method = (payment_method or "ideal").strip().lower()
    is_afterpay = method in ("afterpay", "bnpl", "achteraf", "bol_krediet", "pay_later")
    checkout_label = "Afterpay (iDEAL backup)" if is_afterpay else "iDEAL"
    lines = [
        f"**Task:** `{task_id}`",
        f"**Product ID:** `{product_id or '—'}`",
        f"**Quantity:** {quantity}",
        f"**Payment:** {checkout_label}",
        "",
        f"**Product**\n{product_url}",
        "",
        f"Automated checkout started ({checkout_label})…",
    ]
    if basket_id:
        lines.append(f"\n**Basket ID:** `{basket_id}`")

    payload = _post_payload(
        title="bol.com — Added to cart",
        content=f"@everyone Item in cart — running checkout ({checkout_label})…",
        description="\n".join(lines),
        color=0x00B070,
        url=product_url,
    )
    return await _send_webhook(webhook_url, payload, task_id=task_id)


async def send_checkout_discord_notification(
    webhook_url: str,
    *,
    task_id: str,
    product_url: str,
    product_id: str,
    payment_url: Optional[str] = None,
    checkout_url: Optional[str] = None,
    basket_url: Optional[str] = None,
    basket_id: Optional[str] = None,
    stage: str = "ideal_payment",
    quantity: int = 1,
    partial: bool = False,
) -> bool:
    """Primary drop alert: iDEAL payment URL or Afterpay order placed."""
    lines = [
        f"**Task:** `{task_id}`",
        f"**Product ID:** `{product_id or '—'}`",
        f"**Quantity:** {quantity}",
        f"**Stage:** `{stage}`",
        "",
        f"**Product page**\n{product_url}",
    ]
    if stage == "afterpay_order":
        lines.extend(
            [
                "",
                "**Afterpay / achteraf betalen** — order placed (no iDEAL bank step).",
            ]
        )
        title = "bol.com — ORDER PLACED (Afterpay)"
        content = "@everyone Checkout successful — Afterpay order placed"
        color = 0x57F287
        link_url = checkout_url or product_url
    elif partial:
        lines.extend(
            [
                "",
                "**Checkout reached payment step — open link manually**",
            ]
        )
        if payment_url:
            lines.append(f"\n**Payment / bol redirect**\n{payment_url}")
        title = "bol.com — Checkout ready (manual pay)"
        content = "@everyone Item in checkout — finish payment via link below"
        color = 0xFEE75C
        link_url = payment_url or checkout_url or basket_url or product_url
    else:
        lines.extend(["", f"**iDEAL / payment link (pay now)**\n{payment_url}"])
        title = "bol.com — iDEAL payment link"
        content = "@everyone Pay now — iDEAL link ready"
        color = 0x5865F2
        link_url = payment_url
    if checkout_url:
        lines.extend(["", f"**bol checkout**\n{checkout_url}"])
    if basket_url:
        lines.extend(["", f"**Basket**\n{basket_url}"])
    if basket_id:
        lines.append(f"\n**Basket ID:** `{basket_id}`")

    payload = _post_payload(
        title=title,
        content=content,
        description="\n".join(lines),
        color=color,
        url=link_url,
    )
    return await _send_webhook(webhook_url, payload, task_id=task_id)


async def send_atc_failed_discord(
    webhook_url: str,
    *,
    task_id: str,
    product_url: str,
    product_id: str,
    error: str,
    basket_url: str,
) -> bool:
    lines = [
        f"**Task:** `{task_id}`",
        f"**Product ID:** `{product_id or '—'}`",
        f"**Error:** {error[:500]}",
        "",
        f"**Product**\n{product_url}",
        "",
        "Stock was detected but add-to-cart failed. Monitor is still running for redrops.",
        "",
        f"**Basket**\n{basket_url}",
    ]
    payload = _post_payload(
        title="bol.com — ATC failed",
        content="@everyone Stock detected but ATC failed — monitor still active",
        description="\n".join(lines),
        color=0xE67E22,
        url=product_url,
    )
    return await _send_webhook(webhook_url, payload, task_id=task_id)


async def send_checkout_failed_discord(
    webhook_url: str,
    *,
    task_id: str,
    product_url: str,
    product_id: str,
    error: str,
    basket_url: str,
    checkout_url: str,
    basket_id: Optional[str] = None,
) -> bool:
    lines = [
        f"**Task:** `{task_id}`",
        f"**Product ID:** `{product_id or '—'}`",
        f"**Error:** {error[:500]}",
        "",
        f"**Product**\n{product_url}",
        "",
        f"**Basket (open manually)**\n{basket_url}",
        "",
        f"**Checkout**\n{checkout_url}",
    ]
    if basket_id:
        lines.append(f"\n**Basket ID:** `{basket_id}`")

    payload = _post_payload(
        title="bol.com — Checkout failed",
        content="@everyone ATC OK but checkout failed — finish via basket link.",
        description="\n".join(lines),
        color=0xE74C3C,
        url=basket_url,
    )
    return await _send_webhook(webhook_url, payload, task_id=task_id)


async def send_stock_detected_discord(
    webhook_url: str,
    *,
    task_id: str,
    product_url: str,
    product_id: str,
    status_summary: str,
) -> bool:
    lines = [
        f"**Task:** `{task_id}`",
        f"**Product ID:** `{product_id or '—'}`",
        f"**Status:** {status_summary}",
        "",
        f"**Product**\n{product_url}",
        "",
        "Running ATC + checkout now…",
    ]
    payload = _post_payload(
        title="bol.com — STOCK DETECTED",
        content="@everyone Stock detected — bot is attempting ATC",
        description="\n".join(lines),
        color=0xF1C40F,
        url=product_url,
    )
    return await _send_webhook(webhook_url, payload, task_id=task_id)
