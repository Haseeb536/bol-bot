#!/usr/bin/env python3
"""One-shot bundler: merges all src/ modules into a single self-contained main.py."""
from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

HEADER = '''#!/usr/bin/env python3
"""
BOL-BOT — single self-contained script (all modules inlined).

No other .py files are required. Run everything through this file:

    python main.py
    python main.py --tasks tasks/tasks.yaml
    python main.py --health-check-proxies
    python main.py --bol-login
    python main.py --bol-cart <productId> [offerUid] [quantity]
    python main.py --bol-checkout [basket_id] [--verbose]
    python main.py --import-cookies [cookies.txt]
    python main.py --import-browser-cookies [cookie_header]
    python main.py --seed-akamai

Optional env:
    BOL_CHECKOUT_PLAYWRIGHT=1  — force Playwright checkout instead of HTTP rnwy
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import types
from pathlib import Path

_BOOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
if str(_BOOT) not in sys.path:
    sys.path.insert(0, str(_BOOT))

# Frozen exe: Playwright driver expects browsers in _internal/.../.local-browsers
if getattr(sys, "frozen", False):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")


def _inline_modules() -> None:
    """Load all bundled src.* modules into sys.modules (dependency order)."""
    _chunks: list[tuple[str, str]] = [
'''


FOOTER = '''
    ]
    for mod_name, source in _chunks:
        mod = types.ModuleType(mod_name)
        parts = mod_name.split(".")
        mod.__package__ = ".".join(parts[:-1]) if len(parts) > 1 else ""
        mod.__file__ = f"<main.py:{mod_name}>"
        sys.modules[mod_name] = mod
        parent = ".".join(parts[:-1])
        if parent:
            pkg = sys.modules.get(parent)
            if pkg is not None and parts[-1] not in pkg.__dict__:
                setattr(pkg, parts[-1], mod)
        exec(compile(source, mod.__file__, "exec"), mod.__dict__)


_inline_modules()

from src.config.settings import get_settings
from src.core.engine import BotEngine
from src.tasks.loader import TaskStore
from src.utils.app_root import get_app_root, get_bundle_root, configure_playwright_browsers
from src.utils.logging import setup_logging, get_logger

configure_playwright_browsers()
ROOT = get_app_root()
BUNDLE = get_bundle_root()


def _run_import_cookies(cookie_file: Path | None) -> None:
    from src.bol.login import (
        ROOT_DIR,
        dedupe_cookies,
        ensure_session,
        save_session,
        _parse_cookie_string,
        get_cookie_value,
    )

    token_file = os.path.join(ROOT_DIR, "bol_token.json")

    if cookie_file and cookie_file.is_file():
        raw = cookie_file.read_text(encoding="utf-8").strip()
    else:
        login_txt = Path(ROOT_DIR) / "login.txt"
        if login_txt.is_file():
            from src.sites.akamai import parse_login_txt_cookie_header

            parsed = parse_login_txt_cookie_header(login_txt)
            if parsed:
                session = ensure_session()
                for name, value in parsed.items():
                    session.cookies.set(name, value, domain=".bol.com", path="/")
                dedupe_cookies(session)
                save_session(session, source="login_txt_import")
                print(f"Imported {len(parsed)} cookie(s) from login.txt -> {token_file}")
                if get_cookie_value(session, "_abck"):
                    print("  _abck: present")
                else:
                    print("  _abck: missing — export cookies from a loaded www.bol.com page")
                if get_cookie_value(session, "BUI"):
                    print("  BUI: present (logged in)")
                return
        print("Paste the Cookie header from Chrome (one line), then press Enter twice:")
        lines: list[str] = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if not line.strip() and lines:
                break
            lines.append(line)
        raw = " ".join(lines).strip()

    if not raw:
        print("No cookies provided.")
        raise SystemExit(1)

    parsed = _parse_cookie_string(raw)
    if not parsed:
        print("Could not parse any cookies.")
        raise SystemExit(1)

    session = ensure_session()
    for name, value in parsed.items():
        session.cookies.set(name, value, domain=".bol.com", path="/")
    dedupe_cookies(session)
    save_session(session, source="import_cookies")

    print(f"Imported {len(parsed)} cookie(s) into {token_file}")
    print("  names:", ", ".join(sorted(parsed.keys())))
    if get_cookie_value(session, "_abck"):
        print("  _abck: present")
    else:
        print("  _abck: still missing — copy cookies from a www.bol.com page after it loads fully")
    if get_cookie_value(session, "BUI"):
        print("  BUI: present (logged in)")


def _run_import_browser_cookies(cookie_header: str | None) -> None:
    from src.bol.login import ensure_session, save_session, dedupe_cookies
    from src.sites.bol_cookies import parse_cookie_header

    settings = get_settings()

    if cookie_header:
        raw = cookie_header.strip()
    else:
        path = settings.bol_token_path.parent / "browser_cookies.txt"
        if not path.is_file():
            print(f"Paste Cookie header into {path} or pass as argument")
            raise SystemExit(1)
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        if raw.lower().startswith("cookie:"):
            raw = raw.split(":", 1)[1].strip()

    cookies = parse_cookie_header(raw)
    if "_abck" not in cookies:
        print("Warning: _abck missing — export cookies from www.bol.com while on basket/checkout")
    session = ensure_session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".bol.com", path="/")
    dedupe_cookies(session)
    save_session(session, source="browser_cookies_import")
    out = settings.bol_token_path.parent / "browser_cookies.txt"
    out.write_text(raw if not raw.startswith("cookie") else raw, encoding="utf-8")
    print(f"Imported {len(cookies)} cookies → bol_token.json and {out.name}")


async def _run_seed_akamai() -> None:
    from src.proxy.bol_proxy import get_roundproxies_config, get_roundproxies_pool
    from src.sites.akamai import has_valid_akamai_cookies
    from src.sites.bol_session import seed_session_via_proxy
    from src.sites.bol_urls import resolve_product_url

    pool = get_roundproxies_pool()
    cfg = get_roundproxies_config()
    product_url = resolve_product_url(
        "9300000256665012",
        "https://www.bol.com/nl/nl/p/-/9300000256665012/",
        {"product_slug": "pokemon-me02-5-ascended-heroes-elite-trainer-box"},
    )

    if pool:
        print(f"[akamai] RoundProxies country={cfg.country if cfg else '?'}")
        if cfg and cfg.country.lower().replace("-", "") != "netherlands":
            print("[warn] bol.nl prefers Netherlands proxies — Uganda may stay blocked")
        ok = await seed_session_via_proxy(product_url, pool[0])
        if ok:
            print("[ok] Product page loaded via proxy -> bol_token.json updated")
            print("Run: python main.py")
            return
        if has_valid_akamai_cookies():
            print("[ok] Akamai cookies saved (_abck present). Product PDP may still be 403 pre-drop.")
            print("Run: python main.py")
            return
        print("[warn] Homepage seeded via proxy; product PDP still 403 (normal before drop).")
        print("  www.bol.com + NL proxy are working — run: python main.py")
        print("  Optional: paste Chrome cookies into login.txt or: python main.py --import-cookies")
        return

    if has_valid_akamai_cookies():
        print("[ok] bol_token.json already has _abck (no proxy configured)")
        return

    print("[error] Configure config/roundproxies.yaml first, then re-run: python main.py --seed-akamai")
    raise SystemExit(1)


async def run_bot(tasks_path: Path | None, health_check_proxies: bool) -> None:
    settings = get_settings()
    if tasks_path:
        settings.tasks_path = tasks_path

    setup_logging(settings.log_level, settings.log_file)
    log = get_logger("main")

    store = TaskStore(
        settings.tasks_path,
        settings.profiles_path,
        settings.proxies_path,
        roundproxies_path=settings.roundproxies_path,
        credentials_path=settings.credentials_path,
    )

    engine = BotEngine(store)

    if health_check_proxies:
        from src.proxy.manager import ProxyManager

        pm = ProxyManager(store.proxy_groups)
        log.info("Running proxy health checks...")
        await pm.health_check_all()
        return

    log.info(
        f"Loaded {len(store.get_enabled_tasks())} tasks | "
        f"max concurrent={settings.max_concurrent_tasks}"
    )

    try:
        await engine.run_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        await engine.shutdown()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BOL-BOT — unified monitoring, cart, checkout, and session helper"
    )
    parser.add_argument("--tasks", type=Path, help="Path to tasks YAML")
    parser.add_argument("--health-check-proxies", action="store_true")
    parser.add_argument("--bol-login", action="store_true", help="Run bol.com session login")
    parser.add_argument(
        "--bol-cart",
        nargs=argparse.REMAINDER,
        metavar="ARGS",
        help="Add to cart: <productId> [offerUid] [quantity]",
    )
    parser.add_argument(
        "--bol-checkout",
        nargs="*",
        metavar="ARGS",
        help="HTTP checkout to iDEAL: [basket_id] [--verbose]",
    )
    parser.add_argument(
        "--import-cookies",
        nargs="?",
        const="",
        metavar="FILE",
        help="Import Chrome cookies into bol_token.json",
    )
    parser.add_argument(
        "--import-browser-cookies",
        nargs="?",
        const="",
        metavar="COOKIE_HEADER",
        help="Import browser Cookie header into bol_token.json",
    )
    parser.add_argument(
        "--seed-akamai",
        action="store_true",
        help="Seed Akamai _abck cookies via RoundProxies + Playwright",
    )
    return parser.parse_args(argv)


def _dispatch(args: argparse.Namespace) -> None:
    if args.bol_login:
        from src.bol.login import main as bol_login_main

        bol_login_main()
        return

    if args.bol_cart is not None:
        from src.bol.cart import main as bol_cart_main

        cart_argv = [a for a in args.bol_cart if a != "--"]
        try:
            bol_cart_main(argv=cart_argv)
        except RuntimeError as exc:
            msg = str(exc).encode("ascii", errors="replace").decode("ascii")
            print(f"\\n[error] {msg}")
            raise SystemExit(1) from exc
        return

    if args.bol_checkout is not None:
        from src.bol.checkout import main as bol_checkout_main

        checkout_argv = [a for a in args.bol_checkout if a != "--"]
        bol_checkout_main(argv=checkout_argv)
        return

    if args.import_cookies is not None:
        cookie_file = Path(args.import_cookies) if args.import_cookies else None
        _run_import_cookies(cookie_file)
        return

    if args.import_browser_cookies is not None:
        header = args.import_browser_cookies or None
        _run_import_browser_cookies(header)
        return

    if args.seed_akamai:
        asyncio.run(_run_seed_akamai())
        return

    asyncio.run(run_bot(args.tasks, args.health_check_proxies))


if __name__ == "__main__":
    from src.utils.licence import enforce_licence

    enforce_licence()

    if len(sys.argv) >= 2 and sys.argv[1] == "--bol-cart":
        sys.argv = [sys.argv[0], "--bol-cart", *sys.argv[2:]]
        _dispatch(_parse_args())
        raise SystemExit(0)

    _dispatch(_parse_args())
'''


def path_to_module(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    return "src." + ".".join(rel.parts)


def strip_main_guard(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    lines = source.splitlines(keepends=True)
    for node in reversed(tree.body):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            start = node.lineno - 1
            end = node.end_lineno or node.lineno
            del lines[start:end]
    return "".join(lines)


def clean_source(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        if line.startswith("#!"):
            continue
        if line.strip() == "from __future__ import annotations":
            continue
        out.append(line)
    source = "\n".join(out)
    if not source.endswith("\n"):
        source += "\n"
    return strip_main_guard(source)


def find_src_imports(source: str) -> set[str]:
    deps: set[str] = set()
    for m in re.finditer(r"^\s*from\s+(src\.[\w.]+)\s+import\s+", source, re.M):
        deps.add(m.group(1))
    for m in re.finditer(r"^\s*import\s+(src\.[\w.]+)\s*$", source, re.M):
        deps.add(m.group(1))
    return deps


def topo_sort(modules: dict[str, str]) -> list[str]:
    deps: dict[str, set[str]] = {}
    for name, source in modules.items():
        d = {x for x in find_src_imports(source) if x in modules and x != name}
        deps[name] = d

    ordered: list[str] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(n: str) -> None:
        if n in seen:
            return
        if n in visiting:
            return
        visiting.add(n)
        for dep in sorted(deps.get(n, ())):
            visit(dep)
        visiting.remove(n)
        seen.add(n)
        ordered.append(n)

    for name in sorted(modules):
        visit(name)
    return ordered


def escape_triple_quoted(source: str) -> str:
    return source.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def main() -> None:
    files = sorted(SRC.rglob("*.py"))
    modules: dict[str, str] = {}
    for path in files:
        mod = path_to_module(path)
        modules[mod] = clean_source(path)

    order = topo_sort(modules)

    parts = [HEADER]
    for mod in order:
        src = modules[mod]
        rel = mod.removeprefix("src.")
        parts.append(f'        ("{mod}", """\\n# --- src/{rel.replace(".", "/")}.py ---\\n')
        parts.append(escape_triple_quoted(src))
        parts.append('"""),\n')
    parts.append(FOOTER)

    out = ROOT / "main.py"
    out.write_text("".join(parts), encoding="utf-8")
    line_count = out.read_text(encoding="utf-8").count("\n")
    print(f"Wrote {out} ({line_count:,} lines, {len(order)} modules)")


if __name__ == "__main__":
    main()
