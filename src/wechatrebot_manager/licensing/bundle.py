"""The small, shared authorization package copied into the portable runtime."""
from __future__ import annotations

import hashlib
from pathlib import Path


RUNTIME_FILES = (
    "json_io.py", "single_instance.py", "licensing-channel.json",
    "licensing/__init__.py", "licensing/protocol.py", "licensing/config.py",
    "licensing/client.py", "licensing/cache.py", "licensing/device.py",
    "licensing/runtime_guard.py",
)
_PACKAGE_INIT = b'"""Shared authorization runtime for WeChat ReBot."""\n'


def _contents(commercial_root: Path) -> dict[str, bytes]:
    source = commercial_root / "src/wechatrebot_manager"
    return {"__init__.py": _PACKAGE_INIT, **{name: (source / name).read_bytes() for name in RUNTIME_FILES}}


def bundle_fingerprint(commercial_root: Path) -> str:
    digest = hashlib.sha256()
    for name, data in sorted(_contents(commercial_root).items()):
        digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest().upper()


def install_runtime_bundle(commercial_root: Path, site_packages: Path) -> str:
    target = site_packages / "wechatrebot_manager"
    for name, data in _contents(commercial_root).items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return bundle_fingerprint(commercial_root)


def validate_runtime_bundle(commercial_root: Path, site_packages: Path, fingerprint: object) -> None:
    if fingerprint != bundle_fingerprint(commercial_root):
        raise RuntimeError("Runtime authorization package is stale; rebuild the runtime")
    for name, data in _contents(commercial_root).items():
        path = site_packages / "wechatrebot_manager" / name
        if not path.is_file() or path.read_bytes() != data:
            raise RuntimeError(f"Runtime authorization file is incomplete or stale: {name}")
