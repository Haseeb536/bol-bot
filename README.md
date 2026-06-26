# BOL-BOT

**A fast bol.com monitor that watches products, adds to cart, and checks out — with Discord alerts when it matters.**

Built for drop day: when a product goes live, BOL-BOT detects stock, adds it to your cart, runs checkout (Afterpay or iDEAL), and pings your Discord with the payment link or order confirmation.

> **Note:** This project is not affiliated with bol.com. Use it responsibly and in line with bol.com’s terms of service. You are responsible for how you use this software.

**Author:** [Haseeb536](https://github.com/Haseeb536)

---

## What does it do?

In plain terms, BOL-BOT is a shopping assistant for [bol.com](https://www.bol.com):

1. **Monitors** product pages you configure (polls faster when a page is live, slower when offline).
2. **Detects** when an item becomes buyable (in stock, cart open, etc.).
3. **Adds to cart** automatically with retries tuned for drops.
4. **Checks out** via HTTP first (fast path), with browser fallback when needed.
5. **Notifies you on Discord** — iDEAL bank link, Afterpay confirmation, or failure alerts.

### Payment options

| Mode | How it works |
|------|----------------|
| **Afterpay** (`afterpay_products` in tasks) | Tries Afterpay/BNPL first. If the product doesn’t offer Afterpay, falls back to iDEAL. |
| **iDEAL** (`products` in tasks) | Standard iDEAL checkout — you get the bank redirect link on Discord. |

Up to **5** products can use the Afterpay-first list; everything else uses iDEAL-only.

---

## Why does this exist?

Product drops on bol.com are competitive — pages flip from “offline” to “buy now” in seconds. Manual refreshing rarely wins.

BOL-BOT automates the boring (and slow) parts:

- Constant polling without you staring at the page
- Fast add-to-cart when stock appears
- Checkout while the session is still warm
- A Discord ping so you can pay via iDEAL or confirm Afterpay

It’s aimed at people who already shop on bol.com and want a head start on limited releases — not at bypassing fair use or bol’s rules.

---

## How it works (high level)

```
Product URLs (tasks.yaml)
        │
        ▼
   Monitor loop ──► Stock detected?
        │                    │
        │                    ▼
        │              Add to cart (HTTP)
        │                    │
        │                    ▼
        │         HTTP checkout (Afterpay / iDEAL)
        │                    │
        │              Success? ──► Discord alert
        │                    │
        └──────── Browser fallback (Playwright) if HTTP incomplete
```

- **Proxies:** Netherlands residential proxies (e.g. RoundProxies) help avoid blocks; checkout uses the same IP as monitoring when possible.
- **Session:** Cookies from a logged-in browser (`login.txt`) plus optional `bol_credentials.json` for re-login.
- **Release build:** PyInstaller bundles everything into a Windows `.exe` with Chromium for browser checkout.

---

## Quick start (from source)

### 1. Clone and install

```powershell
git clone https://github.com/Haseeb536/bol-bot.git
cd bol-bot

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-bot.txt
playwright install chromium
```

### 2. Copy config templates

```powershell
copy bol_credentials.json.example bol_credentials.json
copy login.txt.example login.txt
copy config\discord.yaml.example config\discord.yaml
copy config\roundproxies.yaml.example config\roundproxies.yaml
copy config\profiles.yaml.example config\profiles.yaml
copy tasks\tasks.yaml.example tasks\tasks.yaml
```

Fill in **your own** credentials, cookies, webhook, and product URLs.  
**Never commit** `bol_credentials.json`, `login.txt`, `discord.yaml`, or `roundproxies.yaml` — they’re in `.gitignore`.

### 3. Generate `main.py` and run

```powershell
python _bundle_main.py
python main.py --tasks tasks/tasks.yaml
```

Other useful commands:

```powershell
python main.py --bol-login
python main.py --import-cookies cookies.txt
python main.py --health-check-proxies
```

### 4. Build Windows `.exe` (optional)

```powershell
python scripts/build_exe.py
```

Output lands in `BOL-BOT-Release/` (sibling folder). Zip the **entire** release folder — not just the `.exe`.

See **`release/SETUP-GUIDE.md`** for the full end-user setup (cookies, proxies, Discord, troubleshooting).

---

## Project layout

```
bol-bot/
├── src/                 # Core bot logic (monitor, cart, checkout, Discord)
├── scripts/             # Build, login helpers, dev probes
├── config/              # Proxies, profiles — use .example templates
├── tasks/               # Product URLs and polling defaults
├── release/             # Files copied into the Windows release folder
├── _bundle_main.py      # Merges src/ into single main.py
├── BOL-BOT.spec         # PyInstaller spec
├── requirements-bot.txt # Python dependencies
└── README.md            # You are here
```

---

## Configuration overview

| File | Purpose |
|------|---------|
| `tasks/tasks.yaml` | Product URLs, Afterpay vs iDEAL lists, poll intervals |
| `bol_credentials.json` | bol.com login + optional 2captcha key |
| `login.txt` | Akamai/session cookies from Chrome |
| `config/discord.yaml` | Discord webhook for alerts |
| `config/roundproxies.yaml` | Residential proxy credentials (NL recommended) |
| `config/profiles.yaml` | Shipping / profile details for checkout |

---

## Environment variables (optional)

| Variable | Effect |
|----------|--------|
| `ECOM_DISCORD_WEBHOOK_URL` | Override Discord webhook from yaml |
| `BOL_STARTUP_SEED=1` | Seed Akamai cookies via proxy on startup |
| `BOL_CHECKOUT_PLAYWRIGHT=1` | Force browser-only checkout |
| `BOL_AFTERPAY_FAST=0` | Disable fast Afterpay path (more retries, slower) |


## Disclaimer

- This software is provided **as-is** under the [MIT License](LICENSE).
- Automated purchasing may conflict with retailer policies — check bol.com’s terms before use.
- Do not share webhook URLs, proxy passwords, or cookie files publicly.
- The author is not responsible for missed drops, failed orders, or account issues.

---

## Contributing

Issues and pull requests are welcome on [GitHub](https://github.com/Haseeb536).  
If you improve checkout speed, monitoring, or docs, open a PR — friendly contributions appreciated.

---

**Happy hunting — and good luck on drop day.**
