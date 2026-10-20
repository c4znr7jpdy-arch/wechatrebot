"""Capture the manager UI in the soft skin for the design deliverable.

Serves src/wechatrebot_manager/app_webui over loopback, drives the built-in
?preview fixtures and writes PNGs into screens/ next to this script.

No robot is started and no installed product is touched: every page renders from
preview.js sample data.

    python 柔和界面设计稿/capture_soft_skin.py
    python 柔和界面设计稿/capture_soft_skin.py --skin before    # shipped look, for comparison
"""

from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SOURCE = ROOT / "src"
WEB = SOURCE / "wechatrebot_manager" / "app_webui"
CONTACT = SOURCE / "wechatrebot_manager" / "licensing-contact.json"
sys.path.insert(0, str(SOURCE))

PAGES = [
    ("home", "运行中心", ""),
    ("features", "功能中心", ""),
    ("groups", "群聊与私聊配置", ""),
    ("history", "消息中心", ""),
    ("schedules", "定时任务", ""),
    ("activity", "运行记录", ""),
    ("subscription", "订阅与授权", "&demo=subscription&license=active"),
    ("help", "帮助", ""),
    ("settings", "机器人总设置", ""),
]

# 1600x1000 matches the existing design folder; 1080x680 is the minimum window
# UX-CONTRACT.md declares, so the narrow pass is a real boundary test.
WIDE = (1600, 1000)
NARROW = (1080, 680)
NARROW_KEYS = {"home", "features", "settings"}


class Quiet(http.server.SimpleHTTPRequestHandler):
    """Static handler plus the licensing route the dev scaffold normally provides.

    preview.js requests /assets/preview/licensing-contact.json, which dev_manager.py
    maps to src/wechatrebot_manager/licensing-contact.json. Without that route the
    console shows 404s that do not exist in the installed product.
    """

    def log_message(self, *args):
        pass

    def do_GET(self):
        route = self.path.split("?")[0]
        if route in {"/", "/index.html"}:
            html = (Path(self.directory) / "index.html").read_text(encoding="utf-8")
            if "<!-- manager-skins -->" in html:
                from wechatrebot_manager.plugin_center_web import _skin_catalog_markup
                from wechatrebot_manager.ui_skins import load_skin_catalog

                skin_markup = _skin_catalog_markup(load_skin_catalog(Path(self.directory))).decode("utf-8")
                html = html.replace("<!-- manager-skins -->", skin_markup, 1)
            self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if route == "/assets/preview/licensing-contact.json":
            if not CONTACT.is_file():
                self.send_error(404)
                return
            self._send(CONTACT.read_bytes(), "application/json; charset=utf-8")
            return
        super().do_GET()

    def _send(self, body: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve() -> int:
    handler = functools.partial(Quiet, directory=str(WEB))
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    server.allow_reuse_address = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skin", default="soft", choices=["soft", "before"])
    parser.add_argument("--out", default="screens")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    target = HERE / args.out
    target.mkdir(parents=True, exist_ok=True)
    style = "&style=soft" if args.skin == "soft" else ""
    prefix = "" if args.skin == "soft" else "改造前-"
    port = serve()
    base = f"http://127.0.0.1:{port}/?preview{style}"

    console: list[tuple[str, str]] = []
    overflow: list[str] = []
    problems: list[str] = []
    written = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": WIDE[0], "height": WIDE[1]})
        page.on(
            "console",
            lambda m: console.append((m.type, m.text[:200]))
            if m.type in ("error", "warning")
            else None,
        )
        page.on("pageerror", lambda e: console.append(("pageerror", str(e)[:200])))

        def landing(label: str | None = None, query: str = "") -> None:
            """Load a page, optionally photographing the first-run guide first."""
            page.goto(base + query, wait_until="networkidle", timeout=45000)
            shown = page.locator("#onboardingBackdrop:not([hidden])").count() > 0
            if shown and label:
                shot(label)
            if shown:
                page.locator("#onboardingRecommended").click()
                page.locator("#onboardingBackdrop").wait_for(state="hidden")
            page.wait_for_timeout(250)

        def go(key: str, query: str, width: int, height: int, name: str) -> None:
            page.set_viewport_size({"width": width, "height": height})
            page.goto(base + query, wait_until="networkidle", timeout=45000)
            if page.locator("#onboardingBackdrop:not([hidden])").count():
                page.locator("#onboardingRecommended").click()
                page.locator("#onboardingBackdrop").wait_for(state="hidden")
            page.locator(f"[data-page={key}]").click()
            page.wait_for_timeout(700)
            if args.skin == "soft" and page.evaluate(
                "()=>document.documentElement.dataset.skin||''"
            ) != "soft":
                console.append(("skin", f"{key}: data-skin missing"))
            page.locator(".content-scroll").evaluate("n=>n.scrollTo(0,0)")
            page.wait_for_timeout(250)
            bounds = page.evaluate(
                "()=>({viewport:innerWidth,document:document.documentElement.scrollWidth})"
            )
            if bounds["document"] > bounds["viewport"]:
                overflow.append(f"{name} {bounds}")
            shot(name)

        def shot(name: str) -> None:
            nonlocal written
            path = target / f"{prefix}{name}.png"
            # Overwriting an existing PNG makes Playwright's writer fail with
            # OSError EINVAL on this setup, while create and delete both work, so
            # the target is removed first.
            path.unlink(missing_ok=True)
            page.screenshot(path=str(path))
            written += 1

        def to_dark() -> None:
            if page.evaluate("()=>document.documentElement.dataset.theme") != "dark":
                page.locator("#themeToggle").click()
                page.wait_for_function("()=>document.documentElement.dataset.theme==='dark'")

        # Light pass over every page.
        landing()
        for index, (key, label, query) in enumerate(PAGES, start=1):
            go(key, query, *WIDE, name=f"{index:02d}-{label}")

        # Feature centre with a real configuration form open, so the form surface
        # (sections, fields, option grid, save bar) is part of the deliverable too.
        try:
            page.set_viewport_size({"width": WIDE[0], "height": WIDE[1]})
            page.goto(base, wait_until="networkidle", timeout=45000)
            if page.locator("#onboardingBackdrop:not([hidden])").count():
                page.locator("#onboardingRecommended").click()
                page.locator("#onboardingBackdrop").wait_for(state="hidden")
            page.locator("[data-page=features]").click()
            page.get_by_text("群聊分析", exact=True).first.click()
            page.locator("#saveFeature").wait_for(state="visible", timeout=15000)
            page.locator(".content-scroll").evaluate("n=>n.scrollTo(0,0)")
            page.wait_for_timeout(300)
            shot("02-功能中心-配置示例")
        except Exception as exc:
            problems.append(f"功能中心-配置示例: {type(exc).__name__}")

        # First-run guide, forced through the settings entry (#openOnboarding calls
        # showOnboarding(true)) so the capture does not depend on whether the
        # profile still counts as first run.
        landing()
        try:
            page.locator("[data-page=settings]").click()
            page.locator("[data-settings-tab=system]").click()
            page.wait_for_function(
                "()=>{const b=document.querySelector('#openOnboarding');return b&&!b.disabled;}",
                timeout=15000,
            )
            page.locator("#openOnboarding").click()
            page.locator("#onboardingBackdrop:not([hidden])").wait_for(timeout=10000)
            page.wait_for_timeout(300)
            shot("弹窗-首次使用引导")
            page.locator("#onboardingRecommended").click()
            page.locator("#onboardingBackdrop").wait_for(state="hidden")
        except Exception as exc:
            problems.append(f"弹窗-首次使用引导: {type(exc).__name__}")

        # Overlay states. Each is optional: a control that is not ready must not
        # abort the remaining passes. Every path is reached from the page that
        # owns the control, because an inactive .page is display:none.
        landing()
        try:
            # Reached from the message centre, whose fixtures always contain a
            # conversation, so the confirm layer is reliably reachable.
            page.locator("[data-page=history]").click()
            page.locator(".conversation-item").first.click()
            page.wait_for_timeout(700)
            page.get_by_text("删除这段记录", exact=True).first.click()
            page.locator("#modalBackdrop:not([hidden])").wait_for(timeout=10000)
            page.wait_for_timeout(300)
            shot("弹窗-删除本机记录")
            page.locator("#modalCancel").click()
            page.locator("#modalBackdrop").wait_for(state="hidden")
        except Exception as exc:
            problems.append(f"弹窗-删除本机记录: {type(exc).__name__}")

        landing()
        try:
            page.locator("[data-page=history]").click()
            page.locator("#historyDateRange").click()
            page.locator("#historyDateDialog[open]").wait_for(timeout=10000)
            page.wait_for_timeout(300)
            shot("弹窗-日期范围")
            page.keyboard.press("Escape")
            page.locator("#historyDateDialog").wait_for(state="hidden")
        except Exception as exc:
            problems.append(f"弹窗-日期范围: {type(exc).__name__}")

        # Dark pass over every page.
        to_dark()
        for index, (key, label, query) in enumerate(PAGES, start=1):
            go(key, query, *WIDE, name=f"暗色-{index:02d}-{label}")
            if page.evaluate("()=>document.documentElement.dataset.theme") != "dark":
                to_dark()

        # Narrow pass at the declared minimum window.
        for index, (key, label, query) in enumerate(PAGES, start=1):
            if key in NARROW_KEYS:
                go(key, query, *NARROW, name=f"窄屏-{index:02d}-{label}")

        browser.close()

    print("skin   :", args.skin)
    print("shots  :", written, "->", target)
    print("--- console error/warning ---")
    for kind, text in console[:20]:
        print(f"  [{kind}] {text}")
    print(f"  ({len(console)} total)" if console else "  clean")
    print("--- horizontal overflow ---")
    print("  " + "\n  ".join(overflow) if overflow else "  none")
    print("--- optional overlays ---")
    print("  " + "\n  ".join(problems) if problems else "  all captured")
    return 1 if console or overflow or problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
