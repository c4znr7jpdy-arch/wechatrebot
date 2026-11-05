"""One-click upgrade of the activation service on the licensing server.

The entry packages the service Dockerfile inputs and versioned host support
files, deriving a 12-hex content identity from their deterministic tar. It uploads
the tar with BatchMode SSH, unpacks
it under ``/opt/wechatrebot-activation/releases/<identity>``, builds the image on
the server, triggers the existing backup unit, records the current image as the
rollback target, atomically rewrites ``/etc/wechatrebot-activation.env`` and the
``current`` link, updates the host support files, restarts the systemd unit and
proves the public and admin entries. Failed checks restore the image and support files and verify
``/health`` again. ``--plan`` only prints the steps; ``--rollback`` restores the
recorded previous image. The entry never reads or writes the data directory,
the signing private key or the database.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from wechatrebot_manager.licensing.config import PACKAGED_CHANNEL, _read_channel  # noqa: E402
from wechatrebot_manager.licensing.contact import CONTACT_ENDPOINT  # noqa: E402
from activation_deploy_support import SUPPORT_FILES  # noqa: E402

DEFAULT_HOST = "root@47.242.208.64"
DEFAULT_REMOTE_ROOT = "/opt/wechatrebot-activation"
DEFAULT_ENV_FILE = "/etc/wechatrebot-activation.env"
DEFAULT_BACKUP_DIR = "/var/backups/wechatrebot-activation"
DEFAULT_IMAGE_NAME = "wechatrebot-activation"
DEFAULT_CONTAINER = "wechatrebot-activation"
SERVICE_UNIT = "wechatrebot-activation.service"
BACKUP_UNIT = "wechatrebot-activation-backup.service"
DOCKERFILE = "deploy/activation-service/Dockerfile"
ENV_KEY = "ACTIVATION_IMAGE"
HEALTH_PATH = "/health"
# (label, method, path, body, expected status, expected error code) after the switch.
# An empty body is rejected by the JSON parser of every registered route, so the
# trial endpoint answers 409 invalid_data once it exists (the old image answered
# 404 invalid_endpoint). An empty JSON object passes parsing and is rejected as an
# unsigned request, which proves the paid preview route still enforces 401.
UPGRADE_PROBES = (
    ("trial-status", "POST", "/v1/trial-status", b"", 409, "invalid_data"),
    ("preview", "POST", "/v1/preview", b"{}", 401, "invalid_request"),
)
_HOST_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-@")
_PATH_CHARS = _HOST_CHARS | {"/"}
_URL_CHARS = _PATH_CHARS | {":"}
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}")
_IDENTITY = re.compile(r"[0-9a-f]{12}")
_NONCE = re.compile(r"[0-9a-f]{8,64}")
_SNAPSHOT = re.compile(r"backup-\d{8}T\d{12}Z-[0-9a-f]{12}")
_ENV_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


def packaged_service_url() -> str:
    return _read_channel(PACKAGED_CHANNEL).service_url


# ---------------------------------------------------------------- parameter boundaries

def safe_host(host: str) -> str:
    if not host or any(character not in _HOST_CHARS for character in host):
        raise ValueError("SSH 主机参数包含不安全字符")
    return host


def safe_remote_path(path: str, label: str) -> str:
    if (not path.startswith("/") or path == "/" or path.endswith("/") or "//" in path
            or ".." in path.split("/") or any(character not in _PATH_CHARS for character in path)):
        raise ValueError(f"{label}必须是明确的绝对路径")
    return path


def safe_name(name: str, label: str) -> str:
    if not _NAME.fullmatch(name):
        raise ValueError(f"{label}只能使用小写字母、数字、点、下划线和连字符")
    return name


def safe_image(value: str) -> str:
    if not _IMAGE.fullmatch(value):
        raise ValueError("镜像引用包含不安全字符")
    return value


def safe_service_url(url: str) -> str:
    if not url.startswith("https://") or any(character not in _URL_CHARS for character in url):
        raise ValueError("授权服务地址必须是不含查询参数的 HTTPS 地址")
    return url.rstrip("/")


def safe_nonce(nonce: str) -> str:
    if not _NONCE.fullmatch(nonce):
        raise ValueError("临时文件标识必须是十六进制")
    return nonce


# ---------------------------------------------------------------- image inputs

def copy_sources(text: str) -> list[str]:
    """Source patterns of every COPY instruction, in order of first appearance."""
    sources: list[str] = []
    for raw in re.sub(r"\\\r?\n", " ", text).splitlines():
        parts = raw.strip().split()
        if not parts or parts[0].startswith("#") or parts[0].upper() != "COPY":
            continue
        if any(part.startswith("[") for part in parts[1:]):
            raise ValueError("不支持 JSON 数组形式的 COPY 指令")
        operands = []
        for part in parts[1:]:
            if part.startswith("--from"):
                raise ValueError("不支持从其他构建阶段复制")
            if not part.startswith("--"):
                operands.append(part)
        if len(operands) < 2:
            raise ValueError("COPY 指令缺少来源或目标")
        sources.extend(pattern for pattern in operands[:-1] if pattern not in sources)
    return sources


def resolve_sources(root: Path, patterns) -> list[str]:
    """Expand COPY sources below the build context into sorted repository-relative files."""
    root = Path(root).resolve()
    files: set[str] = set()
    for pattern in patterns:
        relative = PurePosixPath(pattern)
        if relative.is_absolute() or ".." in relative.parts or "\\" in pattern or not relative.parts:
            raise ValueError(f"Dockerfile COPY 来源越界：{pattern}")
        matches = sorted(root.glob(pattern))
        if not matches:
            raise ValueError(f"Dockerfile COPY 来源不存在：{pattern}")
        for match in matches:
            if not match.is_file():
                raise ValueError(f"只支持复制普通文件：{pattern}")
            files.add(match.relative_to(root).as_posix())
    return sorted(files)


@dataclass(frozen=True)
class Payload:
    files: tuple[str, ...]
    archive: bytes
    sha256: str

    @property
    def identity(self) -> str:
        return self.sha256[:12]

    @property
    def size(self) -> int:
        return len(self.archive)


def build_payload(root: Path = ROOT, dockerfile: str = DOCKERFILE) -> Payload:
    """Deterministic tar of image inputs and host support files; identity = sha256[:12].

    Text is normalized to LF, the form the repository stores and a Linux checkout
    produces, so a Windows CRLF checkout yields the same identity and the same image.
    """
    root = Path(root)
    text = (root / dockerfile).read_text("utf-8")
    files = sorted(set(resolve_sources(root, copy_sources(text))) | {dockerfile} | set(SUPPORT_FILES))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for relative in files:
            data = (root / relative).read_bytes().replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(relative)
            info.size, info.mtime, info.mode, info.type = len(data), 0, 0o644, tarfile.REGTYPE
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    raw = buffer.getvalue()
    return Payload(tuple(files), raw, hashlib.sha256(raw).hexdigest())


# ---------------------------------------------------------------- environment file

def env_image(text: str) -> str:
    values = []
    for line in text.splitlines():
        match = _ENV_LINE.match(line.strip())
        if match and match.group("key") == ENV_KEY:
            values.append(match.group("value").strip())
    if len(values) != 1:
        raise ValueError(f"环境文件必须恰好包含一行 {ENV_KEY}=")
    value = values[0]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return safe_image(value)


def rewrite_env(text: str, image: str) -> str:
    """Replace only the ACTIVATION_IMAGE line, keeping every other byte of the file."""
    safe_image(image)
    env_image(text)
    lines = []
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        match = _ENV_LINE.match(content.strip())
        if match and match.group("key") == ENV_KEY:
            line = f"{ENV_KEY}={image}" + line[len(content):]
        lines.append(line)
    return "".join(lines)


def identity_of(image: str) -> str | None:
    tag = image.rsplit(":", 1)[-1] if ":" in image else ""
    return tag if _IDENTITY.fullmatch(tag) else None


# ---------------------------------------------------------------- server plan

def _q(value: str) -> str:
    return shlex.quote(value)


@dataclass(frozen=True)
class Settings:
    host: str
    remote_root: str
    env_file: str
    backup_dir: str
    image_name: str
    container: str
    service_url: str
    nonce: str

    @property
    def releases_dir(self) -> str:
        return f"{self.remote_root}/releases"

    @property
    def current_link(self) -> str:
        return f"{self.remote_root}/current"

    @property
    def previous_image_file(self) -> str:
        return f"{self.remote_root}/previous-image"

    @property
    def previous_support_file(self) -> str:
        return f"{self.remote_root}/previous-support"

    @property
    def support_snapshot(self) -> str:
        return f"{self.remote_root}/support-backups/{self.nonce}"

    def read_support_command(self) -> list[str]:
        return self.ssh(f"if [ -f {_q(self.previous_support_file)} ]; then cat {_q(self.previous_support_file)}; fi")

    def support_command(self, operation: str, snapshot: str | None = None, *, release: str | None = None,
                        previous: str | None = None, public: bool = False) -> list[str]:
        snapshot = snapshot or self.support_snapshot
        safe_remote_path(snapshot, "部署文件快照")
        if str(PurePosixPath(snapshot).parent) != f"{self.remote_root}/support-backups":
            raise ValueError("部署文件快照不在预期目录")
        if previous:
            safe_remote_path(previous, "部署文件快照")
            if str(PurePosixPath(previous).parent) != f"{self.remote_root}/support-backups":
                raise ValueError("部署文件快照不在预期目录")
        helper = f"{release}/scripts/activation_deploy_support.py" if release else f"{previous or snapshot}/tool.py"
        arguments = ["python3", helper, operation, "--root", self.remote_root, "--snapshot", snapshot]
        if release:
            arguments += ["--release", release, "--env-file", self.env_file, "--container", self.container]
        if previous:
            arguments += ["--previous", previous]
        if public:
            origin = urllib.parse.urlsplit(self.service_url)
            arguments += ["--url", f"{origin.scheme}://{origin.netloc}/wechatrebot-admin/login"]
        return self.ssh(" ".join(_q(value) for value in arguments))

    def reload_nginx_command(self) -> list[str]:
        return self.ssh("set -eu; nginx -t; systemctl reload nginx")

    @property
    def env_backup_dir(self) -> str:
        return f"{self.remote_root}/env-backups"

    @property
    def env_backup_file(self) -> str:
        return f"{self.env_backup_dir}/{self.nonce}.env"

    @property
    def env_upload_path(self) -> str:
        directory, _, name = self.env_file.rpartition("/")
        return f"{directory}/.{name}.{self.nonce}"

    def url(self, path: str) -> str:
        return self.service_url + path

    @property
    def health_url(self) -> str:
        return self.url(HEALTH_PATH)

    @property
    def contact_url(self) -> str:
        return self.url(CONTACT_ENDPOINT)

    def ssh(self, command: str) -> list[str]:
        return ["ssh.exe", "-o", "BatchMode=yes", self.host, command]

    def scp(self, local_path, remote_path: str) -> list[str]:
        return ["scp.exe", "-o", "BatchMode=yes", "-p", str(local_path), f"{self.host}:{remote_path}"]

    def read_env_command(self) -> list[str]:
        return self.ssh(f"cat {_q(self.env_file)}")

    def read_record_command(self) -> list[str]:
        return self.ssh(f"cat {_q(self.previous_image_file)}")

    def list_backups_command(self) -> list[str]:
        return self.ssh(f"ls -1 {_q(self.backup_dir)}")

    def backup_command(self) -> list[str]:
        return self.ssh(f"set -eu; systemctl start {BACKUP_UNIT}; "
                        f"test \"$(systemctl show -p Result --value {BACKUP_UNIT})\" = success; "
                        f"ls -1 {_q(self.backup_dir)}")

    def record_command(self, previous_image: str, support_snapshot: str = "") -> list[str]:
        record, temporary = self.previous_image_file, f"{self.previous_image_file}.{self.nonce}"
        support_temporary = f"{self.previous_support_file}.{self.nonce}"
        return self.ssh(f"set -eu; mkdir -p {_q(self.env_backup_dir)}; chmod 0700 {_q(self.env_backup_dir)}; "
                        f"cp -p {_q(self.env_file)} {_q(self.env_backup_file)}; chmod 0600 {_q(self.env_backup_file)}; "
                        f"printf '%s\\n' {_q(support_snapshot)} > {_q(support_temporary)}; "
                        f"mv -f {_q(support_temporary)} {_q(self.previous_support_file)}; "
                        f"printf '%s\\n' {_q(previous_image)} > {_q(temporary)}; mv -f {_q(temporary)} {_q(record)}")

    def install_env_command(self) -> list[str]:
        upload, target = _q(self.env_upload_path), _q(self.env_file)
        return self.ssh(f"set -eu; chmod --reference={target} {upload}; chown --reference={target} {upload}; "
                        f"mv -f {upload} {target}; cat {target}")

    def restart_command(self, identity: str | None, *, optional_link: bool = False) -> list[str]:
        parts = ["set -eu"]
        if identity:
            release, temporary = f"{self.releases_dir}/{identity}", f"{self.remote_root}/.current.{self.nonce}"
            switch = f"ln -sfn {_q('releases/' + identity)} {_q(temporary)}; mv -Tf {_q(temporary)} {_q(self.current_link)}"
            if optional_link:
                parts.append(f"if [ -d {_q(release)} ]; then {switch}; fi")
            else:
                parts.append(f"test -d {_q(release)}; {switch}")
        parts.append(f"systemctl restart {SERVICE_UNIT}")
        return self.ssh("; ".join(parts))

    def container_status_command(self) -> list[str]:
        return self.ssh(f"docker inspect --format '{{{{.State.Status}}}} {{{{.State.Health.Status}}}}' {_q(self.container)}")


@dataclass(frozen=True)
class Plan(Settings):
    payload: Payload

    @property
    def identity(self) -> str:
        return self.payload.identity

    @property
    def image(self) -> str:
        return f"{self.image_name}:{self.identity}"

    @property
    def release_dir(self) -> str:
        return f"{self.releases_dir}/{self.identity}"

    @property
    def staging_dir(self) -> str:
        return f"{self.releases_dir}/.staging-{self.identity}-{self.nonce}"

    @property
    def archive_upload(self) -> str:
        return f"{self.releases_dir}/.upload-{self.identity}-{self.nonce}.tar"

    @property
    def remote_dockerfile(self) -> str:
        return f"{self.release_dir}/{DOCKERFILE}"

    def upload_command(self, local_archive) -> list[str]:
        return self.scp(local_archive, self.archive_upload)

    def unpack_command(self) -> list[str]:
        archive, staging, release = _q(self.archive_upload), _q(self.staging_dir), _q(self.release_dir)
        return self.ssh(f"set -eu; mkdir -p {_q(self.releases_dir)}; "
                        f"test \"$(stat -c%s {archive})\" -eq {self.payload.size}; "
                        f"printf '%s  %s\\n' {self.payload.sha256} {archive} | sha256sum -c -; "
                        f"if [ -d {release} ]; then rm -f {archive}; "
                        f"else mkdir {staging}; tar -xf {archive} -C {staging}; rm -f {archive}; mv -T {staging} {release}; fi")

    def build_command(self) -> list[str]:
        return self.ssh(f"set -eu; docker build -t {self.image} -f {_q(self.remote_dockerfile)} {_q(self.release_dir)}; "
                        f"docker image inspect --format '{{{{.Id}}}}' {self.image}")


def build_settings(
    *, host: str = DEFAULT_HOST, remote_root: str = DEFAULT_REMOTE_ROOT, env_file: str = DEFAULT_ENV_FILE,
    backup_dir: str = DEFAULT_BACKUP_DIR, image_name: str = DEFAULT_IMAGE_NAME, container: str = DEFAULT_CONTAINER,
    service_url: str | None = None, nonce: str | None = None,
) -> Settings:
    return Settings(
        host=safe_host(host), remote_root=safe_remote_path(remote_root, "服务器发布目录"),
        env_file=safe_remote_path(env_file, "服务环境文件"), backup_dir=safe_remote_path(backup_dir, "备份目录"),
        image_name=safe_name(image_name, "镜像名称"), container=safe_name(container, "容器名称"),
        service_url=safe_service_url(service_url or packaged_service_url()),
        nonce=safe_nonce(nonce or uuid.uuid4().hex),
    )


def build_plan(root: Path = ROOT, dockerfile: str = DOCKERFILE, **options) -> Plan:
    settings = build_settings(**options)
    return Plan(**settings.__dict__, payload=build_payload(root, dockerfile))


# ---------------------------------------------------------------- transport

def run_command(argv: list[str]) -> str:
    print("+", subprocess.list2cmdline(argv), flush=True)
    completed = subprocess.run(argv, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-600:]
        raise RuntimeError(f"命令失败（退出码 {completed.returncode}）：{argv[0]}\n{detail}")
    return completed.stdout


def probe_http(method: str, url: str, body: bytes | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json", "Cache-Control": "no-cache",
               "User-Agent": "WeChatReBot-ActivationDeployer/1"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, raw = response.status, response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read(2 * 1024 * 1024)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError(f"{method} {url} 返回 HTTP {status}，正文不是 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} {url} 返回 HTTP {status}，正文不是 JSON 对象")
    return status, payload


# ---------------------------------------------------------------- checks

def error_code(payload: dict) -> str | None:
    error = payload.get("error")
    return error.get("code") if isinstance(error, dict) and isinstance(error.get("code"), str) else None


def summarize(status: int, payload: dict) -> str:
    code = error_code(payload)
    return f"{status} {code}" if code else f"{status} {'ok' if payload.get('ok') is True else 'unknown'}"


def check_health(status: int, payload: dict) -> None:
    data = payload.get("data")
    if status != 200 or payload.get("ok") is not True or not isinstance(data, dict) or data.get("status") != "ok":
        raise RuntimeError(f"{HEALTH_PATH} 返回 HTTP {summarize(status, payload)}，不是约定的健康 JSON")


def contact_data(status: int, payload: dict):
    if status != 200 or payload.get("ok") is not True or "data" not in payload or type(payload.get("server_time")) is not int:
        raise RuntimeError(f"{CONTACT_ENDPOINT} 返回 HTTP {summarize(status, payload)}，没有联系资料")
    return payload["data"]


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def wait_for_container(settings: Settings, run=run_command, sleep=time.sleep, attempts: int = 20, delay: float = 3.0) -> str:
    state = ""
    for attempt in range(attempts):
        state = run(settings.container_status_command()).strip()
        if state == "running healthy":
            return state
        if attempt + 1 < attempts:
            sleep(delay)
    raise RuntimeError(f"容器 {settings.container} 未达到 running/healthy（最后状态：{state or '无'}）")


def wait_for_health(settings: Settings, probe=probe_http, sleep=time.sleep, attempts: int = 20, delay: float = 3.0) -> dict:
    failure: Exception | None = None
    for attempt in range(attempts):
        try:
            status, payload = probe("GET", settings.health_url)
            check_health(status, payload)
            return payload
        except (RuntimeError, OSError, ValueError) as exc:
            failure = exc
            if attempt + 1 < attempts:
                sleep(delay)
    raise RuntimeError(f"公网 {HEALTH_PATH} 验证失败：{failure}")


def _snapshots(listing: str) -> set[str]:
    return {line.strip() for line in listing.splitlines() if _SNAPSHOT.fullmatch(line.strip())}


def run_backup(settings: Settings, run=run_command) -> str:
    before = _snapshots(run(settings.list_backups_command()))
    after = _snapshots(run(settings.backup_command()))
    created = sorted(after - before)
    if not created:
        raise RuntimeError(f"备份单元 {BACKUP_UNIT} 运行后备份目录没有新快照，切换中止")
    return created[-1]


def install_env(settings: Settings, run, text: str) -> None:
    with tempfile.TemporaryDirectory(prefix="wechatrebot-activation-env-") as directory:
        local = Path(directory) / "wechatrebot-activation.env"
        local.write_bytes(text.encode("utf-8"))
        run(settings.scp(local, settings.env_upload_path))
    if run(settings.install_env_command()) != text:
        raise RuntimeError(f"{settings.env_file} 写入后读回内容与预期不一致")


def verify_upgrade(plan: Plan, run, probe, sleep, attempts: int, delay: float, baseline) -> str:
    wait_for_container(plan, run, sleep, attempts, delay)
    wait_for_health(plan, probe, sleep, attempts, delay)
    wait_for_admin(plan, run, sleep, attempts, delay)
    observed = {}
    for label, method, path, body, status, code in UPGRADE_PROBES:
        result = probe(method, plan.url(path), body)
        observed[label] = summarize(*result)
        if (result[0], error_code(result[1])) != (status, code):
            raise RuntimeError(f"{method} {path} 返回 {observed[label]}，期望 {status} {code}")
    if _canonical(contact_data(*probe("GET", plan.contact_url))) != _canonical(baseline):
        raise RuntimeError(f"升级后 {CONTACT_ENDPOINT} 的联系资料与升级前不一致")
    return observed["trial-status"]


def wait_for_admin(settings: Settings, run, sleep, attempts: int, delay: float, *, snapshot: str | None = None,
                   restored: bool = False) -> None:
    failure = None
    operation = "probe-restored" if restored else "probe"
    for attempt in range(attempts):
        try:
            run(settings.support_command(operation, snapshot))
            run(settings.support_command(operation, snapshot, public=True))
            return
        except (RuntimeError, OSError, ValueError) as exc:
            failure = exc
            if attempt + 1 < attempts:
                sleep(delay)
    raise RuntimeError(f"管理端口和公网入口验证失败：{failure}")


def rollback_to(settings: Settings, run, probe, sleep, attempts: int, delay: float, image: str, env_text: str,
                support_snapshot: str = "") -> None:
    if support_snapshot:
        run(settings.support_command("restore", support_snapshot))
    install_env(settings, run, rewrite_env(env_text, image))
    run(settings.restart_command(identity_of(image), optional_link=True))
    if support_snapshot:
        run(settings.reload_nginx_command())
    wait_for_container(settings, run, sleep, attempts, delay)
    wait_for_health(settings, probe, sleep, attempts, delay)
    if support_snapshot:
        wait_for_admin(settings, run, sleep, attempts, delay, snapshot=support_snapshot, restored=True)


# ---------------------------------------------------------------- flows

def execute(plan: Plan, run=run_command, probe=probe_http, log=print, sleep=time.sleep,
            attempts: int = 20, delay: float = 3.0) -> dict:
    log("[1/9] 升级前核对公网入口")
    wait_for_health(plan, probe, sleep, attempts, delay)
    baseline = contact_data(*probe("GET", plan.contact_url))
    before = summarize(*probe("POST", plan.url("/v1/trial-status"), b""))
    log(f"      升级前 POST /v1/trial-status：{before}")
    log("[2/9] 读取服务器当前镜像")
    env_text = run(plan.read_env_command())
    previous = env_image(env_text)
    if previous == plan.image:
        raise RuntimeError(f"服务器 {ENV_KEY} 已是 {plan.image}，本次未做任何修改")
    log(f"      当前镜像 {previous}，目标镜像 {plan.image}")
    log("[3/9] 上传并解包发布目录")
    with tempfile.TemporaryDirectory(prefix="wechatrebot-activation-") as directory:
        archive = Path(directory) / f"activation-{plan.identity}.tar"
        archive.write_bytes(plan.payload.archive)
        run(plan.upload_command(archive))
    run(plan.unpack_command())
    log("[4/9] 在服务器构建镜像")
    run(plan.build_command())
    log("[5/9] 定位 nginx 配置并保存部署文件恢复点")
    run(plan.support_command("prepare", release=plan.release_dir))
    log("[6/9] 记录镜像和部署文件回滚目标")
    run(plan.record_command(previous, plan.support_snapshot))
    log("[7/9] 同步部署文件、备份数据并切换镜像")
    try:
        run(plan.support_command("apply"))
        snapshot = run_backup(plan, run)
        log(f"      新快照 {snapshot}")
        install_env(plan, run, rewrite_env(env_text, plan.image))
        run(plan.restart_command(plan.identity))
        run(plan.reload_nginx_command())
        log("[8/9] 验证容器与公网接口")
        after = verify_upgrade(plan, run, probe, sleep, attempts, delay, baseline)
    except (RuntimeError, OSError, ValueError) as failure:
        log(f"      验证失败：{failure}")
        log(f"[9/9] 自动回滚到 {previous}")
        try:
            rollback_to(plan, run, probe, sleep, attempts, delay, previous, env_text, plan.support_snapshot)
        except (RuntimeError, OSError, ValueError) as rollback_failure:
            raise RuntimeError(
                f"升级验证失败：{failure}\n回滚失败：{rollback_failure}\n"
                f"服务可能不可用，需要人工介入：检查 {plan.env_file} 与 systemctl status {SERVICE_UNIT}"
            ) from failure
        raise RuntimeError(f"升级验证失败：{failure}\n已回滚到 {previous} 并重新确认 {HEALTH_PATH} 正常") from failure
    log("[9/9] 验证通过，无需回滚")
    return {"ok": True, "identity": plan.identity, "image": plan.image, "previous_image": previous,
            "backup_snapshot": snapshot, "verified": True, "rolled_back": False,
            "trial_status_before": before, "trial_status_after": after}


def execute_rollback(settings: Settings, run=run_command, probe=probe_http, log=print, sleep=time.sleep,
                     attempts: int = 20, delay: float = 3.0) -> dict:
    log("[1/5] 读取记录的回滚目标")
    try:
        target = safe_image(run(settings.read_record_command()).strip())
    except (RuntimeError, ValueError) as exc:
        raise RuntimeError(f"服务器没有可用的回滚目标（{settings.previous_image_file}）：{exc}") from exc
    log("[2/5] 读取服务器当前镜像")
    env_text = run(settings.read_env_command())
    current = env_image(env_text)
    if current == target:
        log(f"      当前已是记录的镜像 {target}，无需回滚")
        return {"ok": True, "changed": False, "image": target, "replaced_image": target, "verified": False}
    previous_support = run(settings.read_support_command()).strip()
    if previous_support:
        run(settings.support_command("capture", previous=previous_support))
    log(f"[3/5] 记录当前镜像 {current} 作为下一次回滚目标")
    run(settings.record_command(current, settings.support_snapshot if previous_support else ""))
    log(f"[4/5] 切换到 {target} 并重启")
    try:
        rollback_to(settings, run, probe, sleep, attempts, delay, target, env_text, previous_support)
        log("[5/5] 核对公网联系资料接口")
        contact_data(*probe("GET", settings.contact_url))
    except (RuntimeError, OSError, ValueError) as failure:
        try:
            rollback_to(settings, run, probe, sleep, attempts, delay, current, env_text,
                        settings.support_snapshot if previous_support else "")
            run(settings.record_command(target, previous_support))
        except (RuntimeError, OSError, ValueError) as recovery_failure:
            raise RuntimeError(f"回滚验证失败：{failure}\n恢复当前版本失败：{recovery_failure}\n需要人工介入") from failure
        raise RuntimeError(f"回滚验证失败：{failure}\n已恢复操作前镜像 {current} 和部署文件") from failure
    return {"ok": True, "changed": True, "image": target, "replaced_image": current, "verified": True}


# ---------------------------------------------------------------- plan output

def _steps(label_commands) -> list[str]:
    lines = []
    for index, (label, argv) in enumerate(label_commands, start=1):
        lines.append(f"  {index}. {label}")
        if argv:
            lines.append("     " + subprocess.list2cmdline(argv))
    return lines


def describe(plan: Plan) -> str:
    lines = [
        f"内容标识：{plan.identity}（tar SHA-256 {plan.payload.sha256[:16]}…，{plan.payload.size} 字节）",
        f"目标镜像：{plan.image}",
        "发布输入（Dockerfile COPY 与部署支持文件）：",
        *(f"  - {relative}" for relative in plan.payload.files),
        f"服务器：{plan.host}，发布目录 {plan.release_dir}，环境文件 {plan.env_file}",
        f"备份目录：{plan.backup_dir}，回滚记录 {plan.previous_image_file}",
        f"部署文件恢复点：{plan.support_snapshot}；保留旧内容与权限，失败时恢复。",
        f"公网验证：{plan.service_url}",
        "切换后验证：容器 running/healthy；GET /health 200；"
        "POST /v1/trial-status（空请求体）409 invalid_data，旧版本为 404 invalid_endpoint；"
        "POST /v1/preview（{}）401 invalid_request；GET /v1/contact 的 data 与升级前一致。",
        "管理端口验证：127.0.0.1:8092/login 与公网 /wechatrebot-admin/login；未初始化 503 或登录页 200，须含安全响应头。",
        "通过 nginx -T 定位已启用的独立授权 snippet 与限流文件；不能唯一定位时在切换前停止。",
        "任一验证失败自动恢复部署文件与上一镜像并重新验证 /health；显式回滚使用 --rollback。",
        "将执行：",
    ]
    lines += _steps([
        ("升级前核对公网 /health、/v1/contact 与 /v1/trial-status", None),
        ("读取服务器当前镜像", plan.read_env_command()),
        ("上传发布 tar", plan.upload_command(f"<本地临时目录>/activation-{plan.identity}.tar")),
        ("校验摘要并原子解包到发布目录", plan.unpack_command()),
        ("在服务器构建镜像", plan.build_command()),
        ("保存部署文件旧版本并准备新版本", plan.support_command("prepare", release=plan.release_dir)),
        ("记录回滚目标并保存环境文件副本", plan.record_command("<上一镜像>", plan.support_snapshot)),
        ("原子同步支持文件，验证 nginx 并重载 systemd 配置", plan.support_command("apply")),
        ("切换前触发备份并核对新快照（包含管理凭据）", plan.backup_command()),
        ("上传改写后的环境文件", plan.scp("<本地临时目录>/wechatrebot-activation.env", plan.env_upload_path)),
        ("原子替换环境文件并读回核对", plan.install_env_command()),
        ("更新 current 链接并重启服务", plan.restart_command(plan.identity)),
        ("验证并重载 nginx", plan.reload_nginx_command()),
        ("等待容器 running/healthy", plan.container_status_command()),
        ("验证回环管理端口", plan.support_command("probe")),
        ("验证公网管理入口", plan.support_command("probe", public=True)),
        ("公网验证 /health、/v1/trial-status、/v1/preview、/v1/contact", None),
    ])
    return "\n".join(lines)


def describe_rollback(settings: Settings) -> str:
    lines = [
        f"服务器：{settings.host}，环境文件 {settings.env_file}，回滚记录 {settings.previous_image_file}",
        f"部署文件回滚记录：{settings.previous_support_file}；有记录时同时恢复配置与脚本。",
        f"公网验证：{settings.service_url}",
        "回滚后验证：容器 running/healthy；GET /health 200；GET /v1/contact 200。",
        "恢复点原本含 8092 管理端口时同时验证管理回环与公网入口；原始旧版没有后台时跳过该项。",
        "将执行：",
    ]
    lines += _steps([
        ("读取记录的回滚目标", settings.read_record_command()),
        ("读取服务器当前镜像", settings.read_env_command()),
        ("读取部署文件恢复点并保存当前文件", settings.read_support_command()),
        ("记录当前镜像作为下一次回滚目标", settings.record_command("<当前镜像>")),
        ("上传改写后的环境文件", settings.scp("<本地临时目录>/wechatrebot-activation.env", settings.env_upload_path)),
        ("原子替换环境文件并读回核对", settings.install_env_command()),
        ("若发布目录存在则切回 current 链接，并重启服务", settings.restart_command("<上一内容标识>", optional_link=True)),
        ("等待容器 running/healthy", settings.container_status_command()),
        ("公网验证 /health 与 /v1/contact", None),
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------- entry

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="升级授权服务镜像并从公网核对，失败自动回滚")
    parser.add_argument("--host", default=DEFAULT_HOST, help="授权服务器 SSH 目标")
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT, help="服务器发布目录")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="systemd 读取的服务环境文件")
    parser.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR, help="备份单元写入的快照目录")
    parser.add_argument("--image-name", default=DEFAULT_IMAGE_NAME, help="镜像名称，标签为内容标识")
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help="systemd 单元启动的容器名")
    parser.add_argument("--service-url", default=None, help="覆盖公网验证地址；默认读取包内授权通道配置")
    parser.add_argument("--plan", action="store_true", help="只计算内容标识并打印步骤，不连接服务器")
    parser.add_argument("--rollback", action="store_true", help="回滚到服务器记录的上一镜像")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    options = {"host": args.host, "remote_root": args.remote_root, "env_file": args.env_file,
               "backup_dir": args.backup_dir, "image_name": args.image_name, "container": args.container,
               "service_url": args.service_url}
    action = "回滚" if args.rollback else "升级"
    try:
        if args.rollback:
            settings = build_settings(**options)
            print(describe_rollback(settings))
        else:
            settings = build_plan(**options)
            print(describe(settings))
        if args.plan:
            print("计划模式：未连接服务器，未修改任何配置。")
            return 0
        summary = execute_rollback(settings) if args.rollback else execute(settings)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"{action}未完成：{exc}")
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"{action}完成。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
