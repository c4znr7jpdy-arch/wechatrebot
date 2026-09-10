from __future__ import annotations

import asyncio
import importlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace


PROJECT_ROOT = Path(r"E:\Project")
BRIDGE_ROOT = PROJECT_ROOT / "hook_41827_astrbot_bridge"
PATCH_PATH = (
    Path(__file__).resolve().parents[1]
    / "patches"
    / "hook-bridge-prelogin-safety.patch"
)


def test_prelogin_group_poll_never_calls_hook(tmp_path: Path) -> None:
    copied_bridge = tmp_path / "hook_41827_astrbot_bridge"
    package_dir = copied_bridge / "src" / "hook_bridge"
    shutil.copytree(BRIDGE_ROOT / "src" / "hook_bridge", package_dir)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    applied = subprocess.run(
        ["git", "apply", "--whitespace=error-all", str(PATCH_PATH)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert applied.returncode == 0, applied.stderr

    package_name = "_isolated_prelogin_bridge"
    package_spec = importlib.util.spec_from_file_location(
        package_name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    assert package_spec is not None and package_spec.loader is not None
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)
    app_module = importlib.import_module(f"{package_name}.app")
    try:
        application = object.__new__(app_module.BridgeApplication)
        application.state = SimpleNamespace(bot_wxid="")

        class Hook:
            async def get_groups(self):
                raise AssertionError("pre-login group query reached the HOOK DLL")

        application.hook = Hook()
        assert asyncio.run(application._poll_group_member_changes_once()) == 0
    finally:
        for module_name in list(sys.modules):
            if module_name == package_name or module_name.startswith(f"{package_name}."):
                sys.modules.pop(module_name, None)
