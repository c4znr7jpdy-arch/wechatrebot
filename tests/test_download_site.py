from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_download_page_has_safe_progressive_download_ui() -> None:
    page = (ROOT / "deploy" / "download-site" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'lang="zh-CN"' in page
    assert 'id="downloadLink"' in page
    assert 'aria-disabled="true"' in page
    assert 'role="alert"' in page
    assert 'aria-live="polite"' in page
    assert 'fetch("./download.json"' in page
    assert 'endsWith(".zip")' in page
    assert "trustedDownloadOrigins" in page
    assert '"https://download.xiuxianjyj.xin"' in page
    assert 'url.protocol !== "https:"' in page
    assert "SHA-256" in page
    assert "完整解压" in page
    assert "不要直接在压缩包内运行" in page
    assert "@media (prefers-reduced-motion: reduce)" in page
    assert '"https://jiangxiaomei-releases-bj.oss-cn-beijing.aliyuncs.com"' in page
    assert 'installer.kind === "inno-online"' in page
    assert "payload_size" in page
    assert "安装时需联网" in page


def test_nginx_hides_page_but_exposes_update_metadata_and_archives() -> None:
    config = (ROOT / "deploy" / "nginx" / "wechatrebot-updates.conf").read_text(
        encoding="utf-8"
    )

    assert "location = /wechatrebot-updates {" in config
    assert config.count("return 404;") == 5
    assert "return 301 /wechatrebot-updates/;" not in config
    assert "alias /srv/wechatrebot-updates/site/;" not in config
    assert "location = /wechatrebot-updates/index.html {" in config
    assert "alias /srv/wechatrebot-updates/site/index.html;" not in config
    assert "location = /wechatrebot-updates/site {" in config
    assert "location ^~ /wechatrebot-updates/site/ {" in config
    assert "location = /wechatrebot-updates/download.json {" in config
    assert "alias /srv/wechatrebot-updates/download.json;" in config
    assert "location ^~ /wechatrebot-updates/downloads/ {" in config
    assert "alias /srv/wechatrebot-updates/downloads/;" in config
    assert "location = /wechatrebot-updates/stable.json {" in config
    assert "alias /srv/wechatrebot-updates/stable.json;" in config
