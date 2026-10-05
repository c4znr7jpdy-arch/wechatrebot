"""Regenerate complete offline report samples (development Playwright required).

Uses the report test fixture and real templates, without starting the robot or
calling model services. Keep full_page=True: viewport-only capture cuts off
portraits, quotes and the report footer.
"""
import importlib.util
from pathlib import Path
import io
from concurrent.futures import ThreadPoolExecutor
import pytest
from PIL import Image
from playwright.sync_api import sync_playwright



def main() -> None:
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('report_test', root / 'tests/test_group_analysis_report.py')
    test = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(test)
    out = root / 'src/wechatrebot_manager/app_webui/assets'
    with pytest.MonkeyPatch.context() as patch, sync_playwright() as p:
        modules = test.modules.__wrapped__(patch)
        templates = modules('infrastructure.reporting.templates')
        cls = templates.HTMLTemplates
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1080,'height':1450},device_scale_factor=1)
        page.route('**/*', lambda route: route.abort() if route.request.url.startswith(('http:', 'https:')) else route.continue_())
        for folder in sorted((test.PLUGIN / 'src/infrastructure/reporting/templates').iterdir()):
            if not folder.is_dir(): continue
            def factory(config):
                config.values['report_template'] = folder.name
                return cls(config)
            patch.setattr(templates, 'HTMLTemplates', factory)
            with ThreadPoolExecutor(max_workers=1) as pool:
                html, _ = pool.submit(test.render_report, modules, days=1).result()
            page.set_content(html)
            page.evaluate('document.fonts.ready')
            page.evaluate("document.querySelectorAll('img').forEach(img => { if (!img.complete || !img.naturalWidth) img.style.visibility = 'hidden'; })")
            # The preview uses product copy, while source/author/license notices stay
            # in the delivered legal files. Only replace static sample text nodes.
            page.evaluate(r"""() => {
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                while (walker.nextNode()) {
                    const node = walker.currentNode;
                    if (['SCRIPT', 'STYLE'].includes(node.parentElement.tagName)) continue;
                    node.textContent = node.textContent.replace(/astrbot_plugin_qq_group_daily_analysis/gi, '群聊分析')
                        .replace(/AstrBot/gi, 'WxRebot').replace(/OneBot/gi, '微信连接');
                }
            }""")
            image = Image.open(io.BytesIO(page.screenshot(full_page=True)))
            height = page.evaluate('document.documentElement.scrollHeight')
            assert image.height >= height, (folder.name, image.height, height)
            image.save(out / f'report-preview-{folder.name}.webp', quality=85)
            print(f'{folder.name}: {image.width}x{image.height}')
        browser.close()


if __name__ == "__main__":
    main()
