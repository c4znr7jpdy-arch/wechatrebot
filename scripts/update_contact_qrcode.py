"""One-click replacement of the public WeChat group QR code on the activation server.

The activation service only accepts the QR record through its server-local
``set-contact`` command. This entry validates the image on the developer
machine with the shared contact rules, copies it next to the service data,
runs the installed admin wrapper over SSH, removes the temporary upload and
finally reads the public ``/v1/contact`` endpoint back to prove the new code
is what customers will see. ``--plan`` only validates and prints the steps.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from services.activation_service.contact import parse_expiry  # noqa: E402
from wechatrebot_manager.licensing.config import PACKAGED_CHANNEL, _read_channel  # noqa: E402
from wechatrebot_manager.licensing.contact import CONTACT_ENDPOINT, MAX_QR_BYTES, create_contact  # noqa: E402
from wechatrebot_manager.licensing.protocol import LicenseError, display_time  # noqa: E402

DEFAULT_HOST = "root@47.242.208.64"
DEFAULT_REMOTE_DATA_DIR = "/var/lib/wechatrebot-activation"
DEFAULT_ADMIN_COMMAND = "wechatrebot-activation-admin"
DEFAULT_GROUP_NAME = "WXRebot"
CONTAINER_DATA_DIR = "/data"
DEFAULT_VALID_DAYS = 7
_BEIJING = ZoneInfo("Asia/Shanghai")
_HOST_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-@")
_PATH_CHARS = _HOST_CHARS | {"/"}
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg"}


def packaged_service_url() -> str:
    return _read_channel(PACKAGED_CHANNEL).service_url


def default_expiry(now: int, days: int = DEFAULT_VALID_DAYS) -> str:
    """WeChat group codes last seven days; the matching Beijing date expires slightly earlier."""
    return (datetime.fromtimestamp(now, _BEIJING) + timedelta(days=days)).date().isoformat()


def safe_host(host: str) -> str:
    if not host or any(character not in _HOST_CHARS for character in host):
        raise ValueError("SSH 主机参数包含不安全字符")
    return host


def safe_remote_dir(path: str) -> str:
    if (not path.startswith("/") or path == "/" or path.endswith("/")
            or any(character not in _PATH_CHARS for character in path)):
        raise ValueError("服务器数据目录必须是明确的绝对路径")
    return path


def safe_command_name(name: str) -> str:
    if not name or any(character not in _PATH_CHARS for character in name):
        raise ValueError("管理入口命令名包含不安全字符")
    return name


@dataclass(frozen=True)
class Plan:
    image_path: Path
    image: bytes
    sha256: str
    mime_type: str
    group_name: str
    expires_at: int
    expires_text: str
    host: str
    remote_data_dir: str
    admin_command: str
    service_url: str
    remote_name: str = field(init=False)

    def __post_init__(self) -> None:
        extension = _EXTENSIONS[self.mime_type]
        object.__setattr__(self, "remote_name", f"wechat-contact-upload-{self.sha256[:12]}.{extension}")

    @property
    def remote_path(self) -> str:
        return f"{self.remote_data_dir}/{self.remote_name}"

    @property
    def container_path(self) -> str:
        return f"{CONTAINER_DATA_DIR}/{self.remote_name}"

    @property
    def contact_url(self) -> str:
        return self.service_url + CONTACT_ENDPOINT

    def upload_command(self) -> list[str]:
        return ["scp.exe", "-o", "BatchMode=yes", "-p", str(self.image_path), f"{self.host}:{self.remote_path}"]

    def permission_command(self) -> list[str]:
        remote = (f"set -eu; chown --reference={shlex.quote(self.remote_data_dir)} {shlex.quote(self.remote_path)}; "
                  f"chmod 0640 {shlex.quote(self.remote_path)}")
        return ["ssh.exe", "-o", "BatchMode=yes", self.host, remote]

    def set_contact_command(self) -> list[str]:
        # The group name travels as base64 so Windows console code pages and the
        # remote shell cannot alter non-ASCII characters on the way.
        encoded = base64.b64encode(self.group_name.encode("utf-8")).decode("ascii")
        remote = (f"set -eu; {self.admin_command} set-contact --image {shlex.quote(self.container_path)} "
                  f"--group-name \"$(printf %s {encoded} | base64 -d)\" --expires-at {shlex.quote(self.expires_text)}")
        return ["ssh.exe", "-o", "BatchMode=yes", self.host, remote]

    def cleanup_command(self) -> list[str]:
        return ["ssh.exe", "-o", "BatchMode=yes", self.host, f"rm -f {shlex.quote(self.remote_path)}"]

    def steps(self) -> list[tuple[str, list[str]]]:
        return [
            ("上传图片到服务器数据目录", self.upload_command()),
            ("交给授权服务账号读取", self.permission_command()),
            ("原子替换微信群二维码资料", self.set_contact_command()),
            ("删除服务器上的临时图片", self.cleanup_command()),
        ]


def build_plan(
    image_path: Path, *, group_name: str = DEFAULT_GROUP_NAME, expires_at: str | None = None,
    host: str = DEFAULT_HOST, remote_data_dir: str = DEFAULT_REMOTE_DATA_DIR,
    admin_command: str = DEFAULT_ADMIN_COMMAND, service_url: str | None = None, now: int | None = None,
) -> Plan:
    now = int(time.time()) if now is None else now
    image_path = Path(image_path).expanduser()
    if not image_path.is_file():
        raise ValueError(f"二维码图片不存在：{image_path}")
    with image_path.open("rb") as handle:
        image = handle.read(MAX_QR_BYTES + 1)
    expires_text = expires_at or default_expiry(now)
    expires = parse_expiry(expires_text, now)
    record = create_contact(image, group_name, expires)
    service = service_url or packaged_service_url()
    if not service.startswith("https://"):
        raise ValueError("授权服务地址必须是 HTTPS")
    return Plan(
        image_path=image_path, image=image, sha256=hashlib.sha256(image).hexdigest(),
        mime_type=record["mime_type"], group_name=record["group_name"], expires_at=expires,
        expires_text=expires_text, host=safe_host(host), remote_data_dir=safe_remote_dir(remote_data_dir),
        admin_command=safe_command_name(admin_command), service_url=service.rstrip("/"),
    )


def run_command(argv: list[str]) -> str:
    completed = subprocess.run(argv, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-600:]
        raise RuntimeError(f"命令失败（退出码 {completed.returncode}）：{argv[0]}\n{detail}")
    return completed.stdout


def fetch_contact(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache",
                                                   "User-Agent": "WeChatReBot-ContactPublisher/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"公网二维码接口返回 HTTP {response.status}")
        return json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))


def parse_admin_result(output: str) -> dict:
    try:
        value = json.loads(output.strip() or "{}")
    except ValueError as exc:
        raise RuntimeError("管理入口没有返回可解析的 JSON 结果。") from exc
    if not isinstance(value, dict) or value.get("ok") is not True or not isinstance(value.get("data"), dict):
        error = value.get("error", {}) if isinstance(value, dict) else {}
        message = error.get("message", "未知错误") if isinstance(error, dict) else "未知错误"
        raise RuntimeError(f"服务器拒绝更新二维码：{message}")
    return value["data"]


def verify_record(plan: Plan, payload: dict) -> dict:
    record = payload.get("data") if isinstance(payload, dict) else None
    server_time = payload.get("server_time") if isinstance(payload, dict) else None
    if payload.get("ok") is not True or not isinstance(record, dict) or type(server_time) is not int:
        raise RuntimeError("公网二维码接口没有返回新资料。")
    try:
        image = base64.b64decode(record["image_base64"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("公网二维码资料不完整。") from exc
    problems = []
    if record.get("group_name") != plan.group_name:
        problems.append("群名称")
    if record.get("expires_at") != plan.expires_at:
        problems.append("有效期")
    if record.get("mime_type") != plan.mime_type or hashlib.sha256(image).hexdigest() != plan.sha256:
        problems.append("图片内容")
    if problems:
        raise RuntimeError("公网读回的二维码与本次上传不一致：" + "、".join(problems))
    if server_time >= plan.expires_at:
        raise RuntimeError("服务器时间已超过本次设置的有效期，客户端会隐藏图片。")
    return {"server_time": server_time, "remaining_seconds": plan.expires_at - server_time}


def verify(plan: Plan, fetch=fetch_contact, *, attempts: int = 3, delay: float = 2.0, sleep=time.sleep) -> dict:
    failure: Exception | None = None
    for attempt in range(attempts):
        try:
            return verify_record(plan, fetch(plan.contact_url))
        except (RuntimeError, OSError, ValueError) as exc:
            failure = exc
            if attempt + 1 < attempts:
                sleep(delay)
    raise RuntimeError(f"公网验证失败：{failure}")


def execute(plan: Plan, run=run_command, fetch=fetch_contact, log=print, **verify_options) -> dict:
    steps = plan.steps()
    log(f"[1/5] {steps[0][0]}")
    run(steps[0][1])
    try:
        log(f"[2/5] {steps[1][0]}")
        run(steps[1][1])
        log(f"[3/5] {steps[2][0]}")
        result = parse_admin_result(run(steps[2][1]))
    finally:
        log(f"[4/5] {steps[3][0]}")
        try:
            run(steps[3][1])
        except RuntimeError as exc:  # The record is already replaced; a leftover file is not a failure.
            log(f"临时图片未能删除，请稍后在服务器手动清理：{exc}")
    log("[5/5] 从公网读回二维码核对")
    checked = verify(plan, fetch, **verify_options)
    return {"ok": True, "group_name": plan.group_name, "expires_at": plan.expires_at,
            "expires_text": display_time(plan.expires_at), "mime_type": result.get("mime_type", plan.mime_type),
            "image_sha256": plan.sha256, "verified": True, "remaining_seconds": checked["remaining_seconds"]}


def describe(plan: Plan) -> str:
    lines = [
        f"图片：{plan.image_path}（{plan.mime_type}，{len(plan.image)} 字节，SHA-256 {plan.sha256[:16]}…）",
        f"群名称：{plan.group_name}",
        f"有效期：{display_time(plan.expires_at)} 北京时间截止（--expires-at {plan.expires_text}）",
        f"服务器：{plan.host}，数据目录 {plan.remote_data_dir}，管理入口 {plan.admin_command}",
        f"验证地址：{plan.contact_url}",
        "将执行：",
    ]
    for index, (label, argv) in enumerate(plan.steps(), start=1):
        lines.append(f"  {index}. {label}")
        lines.append("     " + subprocess.list2cmdline(argv))
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="替换授权服务上的微信群二维码并从公网核对")
    parser.add_argument("--image", required=True, type=Path, help="PNG 或 JPEG 二维码图片，不超过 512 KiB")
    parser.add_argument("--group-name", default=DEFAULT_GROUP_NAME, help="管理器显示的微信群名称")
    parser.add_argument("--expires-at", default=None,
                        help="YYYY-MM-DD（北京时间当日零点截止）或带时区 ISO 8601；默认 7 天后的北京日期")
    parser.add_argument("--host", default=DEFAULT_HOST, help="授权服务器 SSH 目标")
    parser.add_argument("--remote-data-dir", default=DEFAULT_REMOTE_DATA_DIR, help="服务器上映射为容器 /data 的目录")
    parser.add_argument("--admin-command", default=DEFAULT_ADMIN_COMMAND, help="服务器上的管理入口命令")
    parser.add_argument("--service-url", default=None, help="覆盖公网验证地址；默认读取包内授权通道配置")
    parser.add_argument("--plan", action="store_true", help="只校验图片并打印步骤，不连接服务器")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = build_plan(args.image, group_name=args.group_name, expires_at=args.expires_at, host=args.host,
                          remote_data_dir=args.remote_data_dir, admin_command=args.admin_command,
                          service_url=args.service_url)
        print(describe(plan))
        if args.plan:
            print("计划模式：未连接服务器，未修改任何资料。")
            return 0
        summary = execute(plan)
    except (LicenseError, ValueError, OSError, RuntimeError) as exc:
        print(f"二维码未更新：{exc}")
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("更新完成。客户端在下一次刷新（通常 60 秒内）显示新二维码。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
