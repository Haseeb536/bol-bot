# BOL-BOT — Setup Guide (Windows .exe)

Everything you need is in **this folder**. Do not move `BOL-BOT.exe` out on its own — keep all files together.

---

## Quick start

1. Complete the steps below (URLs, login, proxies, Discord).
2. Double-click **`START-BOT.bat`** or run **`BOL-BOT.exe`** from this folder.
3. When a product goes live: bot adds to cart → checkout → Discord sends the **iDEAL payment link** or **Afterpay confirmation**.

---

## 1. Change product URLs (most important)

Edit **`tasks\tasks.yaml`** with Notepad or any text editor.

### Payment per product

| List | Payment | When to use |
|------|---------|-------------|
| `afterpay_products` | Afterpay first, **iDEAL backup** if Afterpay unavailable | Drop-critical URLs (max **5**) — Pokémon TCG, limited releases |
| `products` | iDEAL only | Everything else |

For `afterpay_products`, the bot tries Afterpay/BNPL first. If the product does not offer Afterpay (common on some Pokémon items), it **automatically switches to iDEAL** so checkout still completes.

```yaml
defaults:
  quantity: 1                  # units per add-to-cart (1 = safe for testing)
  max_units_per_item: 2        # bol max per product line
  max_items_per_checkout: 4    # max different products in one checkout

# Afterpay first + iDEAL backup — drop-critical URLs (max 5)
afterpay_products:
  - https://www.bol.com/nl/nl/p/drop-critical-item/9300000123456789/

# iDEAL only
products:
  - https://www.bol.com/nl/nl/p/another-product/9300000987654321/
```

- Paste full bol.com product links (one per line).
- **Hot-reload:** save the file while the bot runs — changes apply in ~5 seconds.
- Use **qty 1** on your main account while testing to avoid cancelled orders.

### Optional per-product settings

```yaml
products:
  - url: https://www.bol.com/nl/nl/p/example/9300000123456789/
    quantity: 1
    enabled: true
```

### Changing limits

Edit the `defaults:` block in `tasks.yaml`:

| Setting | Default | Meaning |
|---------|---------|---------|
| `quantity` | 1 | How many units the bot adds per ATC |
| `max_units_per_item` | 2 | Hard cap per product (bol limit) |
| `max_items_per_checkout` | 4 | Max different products in one checkout |
| `polling.online_min_sec` | 1.0 | Fast poll when page is live (waiting for cart / in stock) |
| `polling.online_max_sec` | 2.0 | Fast poll upper bound |
| `polling.offline_min_sec` | 3.0 | Slow poll when page offline or blocked |
| `polling.offline_max_sec` | 7.0 | Slow poll upper bound |

When a product page is **live** but the cart button is not open yet, the bot polls every **1–2 seconds**. Offline or blocked pages poll every **3–7 seconds**.

---

## 2. bol.com login (`bol_credentials.json`)

Open **`bol_credentials.json`** and set:

| Field | What to put |
|-------|-------------|
| `username` | Your bol.com email |
| `password` | Your bol.com password |
| `twocaptcha_api_key` | API key from [2captcha.com](https://2captcha.com) (for auto re-login on blocks) |

Example:

```json
{
  "username": "you@email.com",
  "password": "your-password",
  "twocaptcha_api_key": "your-2captcha-key"
}
```

---

## 3. Akamai cookies (`login.txt`)

bol.com uses bot protection. Export cookies from Chrome:

1. Log in to **bol.com** in Chrome.
2. Use the **same RoundProxies NL IP** you will run the bot with (important on drop day).
3. Open DevTools → Network → copy the **Cookie** header from any `www.bol.com` request.
4. Paste the full cookie string into **`login.txt`** (one line, no extra text).

If `login.txt` is missing or stale, the bot may get HTTP 403 errors.

---

## 4. RoundProxies (`config\roundproxies.yaml`)

Edit **`config\roundproxies.yaml`** with your [RoundProxies](https://app.roundproxies.com) residential credentials:

```yaml
enabled: true
host: residential.roundproxies.com
port: 5000
client_id: YOUR_CLIENT_ID
password: YOUR_PASSWORD
country: Netherlands
session_count: 10
session_prefix: bol
```

- Use **Netherlands** only for bol.nl.
- Monitor and add-to-cart use these proxies; checkout runs on your home IP.

Test proxies:

```powershell
.\BOL-BOT.exe --health-check-proxies
```

---

## 5. Discord alerts (`config\discord.yaml`)

Create or edit **`config\discord.yaml`**:

```yaml
webhook_url: "https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN"
```

You get ATC success + **iDEAL payment URL** (`pay.ideal.nl`) or **Afterpay order placed** on checkout success.

---

## 6. Shipping profile (`config\profiles.yaml`)

Profile **`bol_main`** is used by default. Set your name, address, and phone under `bol_main` → `shipping` if checkout needs them.

---

## Folder layout

```
BOL-BOT-Release/
  BOL-BOT.exe          ← run this
  START-BOT.bat        ← double-click to start
  SETUP-GUIDE.md       ← this file
  tasks/
    tasks.yaml         ← product URLs + payment + limits
  config/
    discord.yaml       ← Discord webhook
    roundproxies.yaml  ← proxy credentials
    profiles.yaml      ← shipping / account profile
    proxies.yaml       ← leave as-is
  bol_credentials.json ← bol login + 2captcha
  login.txt            ← Akamai cookies from Chrome
  bol_token.json       ← auto-created session (do not delete)
  logs/                ← bot log files
  _internal/           ← required runtime (do not delete)
```

---

## Playwright / browser error?

Chromium is **embedded inside `_internal`** (path: `_internal\playwright\driver\package\.local-browsers`). Do **not** delete the `_internal` folder.

If you see `headless_shell.exe` missing:

1. Re-download the **full** release zip and extract everything (exe + `_internal` together).
2. Do not copy only `BOL-BOT.exe` — the browser lives inside `_internal`.
3. Or install Google Chrome / Edge (the bot tries those as fallback).

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Playwright / headless_shell crash | Do **not** set `BOL_CHECKOUT_PLAYWRIGHT=1` (HTTP checkout is default) |
| HTTP 403 / Akamai block | Refresh `login.txt` from Chrome on the **same proxy IP** |
| ATC fails | Check `logs\bot.log`; verify `bol_token.json` and basket session |
| No Discord message | Check `config\discord.yaml` webhook URL |
| Wrong quantity / limits | Edit `defaults:` in `tasks.yaml` |
| Afterpay failed but iDEAL works | Normal on Pokémon — bot should auto-fallback; check log for `ideal_backup` |

---

## Stop the bot

Close the console window or press **Ctrl+C**.
