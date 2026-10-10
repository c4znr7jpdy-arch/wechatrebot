"""Read the manager scripts in the same order as its HTML entry point."""
from pathlib import Path
import re
from urllib.parse import urlsplit


def manager_script(root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parents[1] / "src/wechatrebot_manager/app_webui"
    sources = re.findall(r'<script\s+src="([^"]+)"', (root / "index.html").read_text("utf-8"))
    paths = [root / urlsplit(source).path.lstrip("/") for source in sources]
    return "\n".join(path.read_text("utf-8") for path in paths)
