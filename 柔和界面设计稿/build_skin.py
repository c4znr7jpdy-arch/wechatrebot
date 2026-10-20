"""Assemble the soft UI skin from its layers into the shipped stylesheet.

The skin is authored as ordered layers so the pages can be worked on
independently; what ships is a single reviewable stylesheet.

    python 柔和界面设计稿/build_skin.py
"""

from __future__ import annotations

import hashlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PARTS = HERE / "layers"
TARGET = ROOT / "src" / "wechatrebot_manager" / "app_webui" / "soft-theme.css"

HEADER = """/* Soft UI skin for the manager web UI — GENERATED, do not edit here.
 *
 * Sources: 柔和界面设计稿/layers/  (see 设计说明.md)
 * Rebuild: python 柔和界面设计稿/build_skin.py
 *
 * Activated by html[data-skin="soft"] through the manager's skin selector.
 * The default skin is unchanged while the attribute is absent.
 * Native Date, Select and Listbox controls stay native (release/premium-ui.json).
 *
 * Layers: {layers}
 */
"""


def main() -> int:
    parts = sorted(PARTS.glob("*.css"))
    if not parts:
        print("no layers found in", PARTS)
        return 1

    body = "\n\n".join(p.read_text(encoding="utf-8").rstrip() + "\n" for p in parts)
    text = HEADER.format(layers=", ".join(p.name for p in parts)) + body
    TARGET.write_text(text, encoding="utf-8")

    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    print("layers :", ", ".join(p.name for p in parts))
    print("target :", TARGET)
    print(f"size   : {len(text):,} chars, {text.count(chr(10)):,} lines, sha256:{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
