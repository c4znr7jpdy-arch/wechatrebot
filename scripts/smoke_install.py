from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

COMMERCIAL_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = COMMERCIAL_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from wechatrebot_manager.brand import BrandProfile
from wechatrebot_manager.installer import InstallRequest, perform_install
from wechatrebot_manager.wechat_path import validate_wechat_executable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Perform a non-starting full install smoke test inside artifacts/."
    )
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--wechat-exe", type=Path, required=True)
    args = parser.parse_args(argv)

    artifacts_root = (COMMERCIAL_ROOT / "artifacts").resolve()
    install_root = args.install_root.expanduser().resolve()
    if install_root == artifacts_root or artifacts_root not in install_root.parents:
        parser.error("install-root must stay inside the commercial artifacts directory")
    if install_root.exists() and any(install_root.iterdir()):
        parser.error(f"install-root must not already contain files: {install_root}")
    wechat = validate_wechat_executable(args.wechat_exe)
    if not wechat.supported:
        parser.error("; ".join(wechat.errors))

    phases: dict[str, int] = {}
    verified = perform_install(
        InstallRequest(
            install_root=install_root,
            payload_root=args.payload.expanduser().resolve(),
            wechat=wechat,
            brand=BrandProfile("验收机器人", "验收", "验收机器人"),
        ),
        progress_callback=lambda phase, current, total: phases.__setitem__(
            phase, current
        ),
        save_locator=False,
        create_shortcuts=False,
    )
    assert verified is not None
    manager_test = install_root / "manager-self-test.json"
    subprocess.run(
        [
            str(install_root / "manager" / "WeChatReBot.exe"),
            "self-test",
            "--output",
            str(manager_test),
        ],
        cwd=install_root,
        check=True,
        timeout=60,
    )
    runtime_test = (
        "import importlib.metadata as m,sys;"
        "import aiohttp,astrbot,PIL,websockets;"
        "assert sys.maxsize > 2**32;"
        "assert m.version('AstrBot') == '4.26.6'"
    )
    subprocess.run(
        [
            str(install_root / "runtime" / "python" / "python.exe"),
            "-B",
            "-I",
            "-c",
            runtime_test,
        ],
        cwd=install_root,
        check=True,
        timeout=60,
    )
    report = {
        "ok": True,
        "installed_files": len(verified.files),
        "phases": phases,
        "manager": json.loads(manager_test.read_text(encoding="utf-8")),
        "services_started": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
