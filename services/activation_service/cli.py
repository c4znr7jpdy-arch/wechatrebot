"""Local administrator commands. No HTTP administrator credentials exist."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from wechatrebot_manager.licensing.protocol import LicenseError, fingerprint, public_key
from .store import LicenseStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QQ 群人工发码授权服务及本地管理工具")
    parser.add_argument("--data-dir", required=True, type=Path, help="服务器专用私有数据目录")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="初始化数据库及独立授权私钥；已存在时拒绝覆盖")
    serve = commands.add_parser("serve", help="运行仅含客户接口的授权服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8091)
    serve.add_argument("--rate-per-minute", type=int, default=120)
    for command in ("issue", "renew", "recover", "migrate"):
        child = commands.add_parser(command, help={
            "issue": "生成新开通码", "renew": "生成原授权的续期码",
            "recover": "为原设备补领原授权，不延长时间",
            "migrate": "核实身份后迁移到指定设备，可申请重新绑定微信",
        }[command])
        child.add_argument("--owner-ref", required=True, help="管理员核实的稳定客户参考号（仅保存哈希）")
        child.add_argument("--reference", required=True, help="唯一人工业务编号，重复编号拒绝再次发码")
        child.add_argument("--valid-days", type=int, default=30, help="未兑换码的有效天数，不是订阅时长")
        if command in {"issue", "renew"}:
            child.add_argument("--plan", choices=("trial", "month", "year") if command == "issue" else ("month", "year"), required=True)
        if command != "issue":
            child.add_argument("--license-id", required=True)
        if command == "migrate":
            child.add_argument("--device", required=True, help="客户新安装显示的 64 位设备指纹")
            child.add_argument("--reset-account", action="store_true", help="仅在核实换微信申请后清除原账号绑定")
    revoke = commands.add_parser("revoke", help="停用授权或撤销尚未兑换的码")
    choice = revoke.add_mutually_exclusive_group(required=True)
    choice.add_argument("--license-id")
    choice.add_argument("--code-id")
    inspect = commands.add_parser("inspect", help="查询授权及码状态，不回显秘密")
    choice = inspect.add_mutually_exclusive_group()
    choice.add_argument("--license-id")
    choice.add_argument("--owner-ref")
    return parser


def _initialize(data_dir: Path) -> dict:
    key = Ed25519PrivateKey.generate()
    key_file = data_dir / "licensing-private.pem"
    content = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())
    descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return {"key_id": fingerprint(public_key(key))[:16], "public_key": public_key(key),
            "private_key_file": str(key_file), "service_configured": False}


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    try:
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        store = LicenseStore(data_dir / "activation.sqlite3")
        now = int(time.time())
        if args.command == "init":
            result = _initialize(data_dir)
        elif args.command == "serve":
            from aiohttp import web
            from .app import create_app

            key = serialization.load_pem_private_key((data_dir / "licensing-private.pem").read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise LicenseError("invalid_key", "授权服务必须使用独立的 Ed25519 私钥。")
            app = create_app(store, key, fingerprint(public_key(key))[:16], rate_requests=args.rate_per_minute)
            web.run_app(app, host=args.host, port=args.port, access_log=None)
            return 0
        elif args.command == "revoke":
            result = store.revoke(now=now, license_id=args.license_id or "", code_id=args.code_id or "")
        elif args.command == "inspect":
            result = store.inspect(license_id=args.license_id or "", owner_ref=args.owner_ref or "")
        else:
            if not 1 <= args.valid_days <= 365:
                raise LicenseError("invalid_data", "兑换期限须为 1 至 365 天。")
            result = store.issue_code(
                owner_ref=args.owner_ref, reference=args.reference, now=now,
                plan=getattr(args, "plan", None), mode={
                    "issue": "new", "renew": "renewal", "recover": "recovery", "migrate": "migration",
                }[args.command], license_id=getattr(args, "license_id", ""),
                target_device=getattr(args, "device", ""), reset_account=getattr(args, "reset_account", False),
                redeem_by=now + args.valid_days * 86400,
            )
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False, indent=2))
        return 0
    except LicenseError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False))
        return 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        # Local errors still avoid printing key material or activation secrets.
        print(json.dumps({"ok": False, "error": {"code": "local_setup_error", "message": type(exc).__name__}}, ensure_ascii=False))
        return 1
