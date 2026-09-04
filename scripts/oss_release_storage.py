from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Callable, Sequence
import urllib.parse


COMMERCIAL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = COMMERCIAL_ROOT / "release" / "oss-publish.json"
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_ENDPOINT_PATTERN = re.compile(r"^oss-[a-z0-9-]+\.aliyuncs\.com$")
_PREFIX_PATTERN = re.compile(r"^[a-zA-Z0-9._/-]*$")
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class OssReleaseStorage:
    enabled: bool
    bucket: str
    region: str
    endpoint: str
    object_prefix: str
    public_base_url: str

    def object_key(self, category: str, filename: str) -> str:
        if category not in {"downloads", "releases"}:
            raise ValueError(f"不支持的 OSS 发布目录：{category}")
        if not filename or Path(filename).name != filename:
            raise ValueError(f"OSS 发布文件名无效：{filename}")
        parts = tuple(
            part.strip("/")
            for part in (self.object_prefix, category, filename)
            if part.strip("/")
        )
        return "/".join(parts)

    def object_uri(self, object_key: str) -> str:
        return f"oss://{self.bucket}/{object_key}"

    def public_url(self, category: str, filename: str) -> str:
        # public_base_url already maps to object_prefix on the CDN/download domain.
        relative = "/".join((category, filename))
        encoded = urllib.parse.quote(relative, safe="/-._~")
        return f"{self.public_base_url.rstrip('/')}/{encoded}"


@dataclass(frozen=True, slots=True)
class OssReleaseObject:
    local_path: Path
    category: str
    sha256: str
    content_type: str
    cache_control: str


def _validated_public_base_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("OSS 公网下载地址格式错误")
    url = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("OSS 公网下载地址必须是无账号和参数的 HTTPS 地址")
    return url


def load_oss_release_storage(path: str | Path = DEFAULT_CONFIG_PATH) -> OssReleaseStorage:
    config_path = Path(path).expanduser().resolve()
    payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("OSS 发布配置版本无效")
    bucket = str(payload.get("bucket") or "").strip()
    region = str(payload.get("region") or "").strip()
    endpoint = str(payload.get("endpoint") or "").strip().casefold()
    prefix = str(payload.get("object_prefix") or "").strip().strip("/")
    if not _BUCKET_PATTERN.fullmatch(bucket):
        raise ValueError("OSS Bucket 名称无效")
    if not region.startswith("cn-") or not re.fullmatch(r"[a-z0-9-]+", region):
        raise ValueError("OSS 地域无效")
    if not _ENDPOINT_PATTERN.fullmatch(endpoint) or endpoint != f"oss-{region}.aliyuncs.com":
        raise ValueError("OSS Endpoint 与地域不匹配")
    if not _PREFIX_PATTERN.fullmatch(prefix) or ".." in prefix.split("/"):
        raise ValueError("OSS 对象前缀无效")
    return OssReleaseStorage(
        enabled=payload.get("enabled") is True,
        bucket=bucket,
        region=region,
        endpoint=endpoint,
        object_prefix=prefix,
        public_base_url=_validated_public_base_url(payload.get("public_base_url")),
    )


def find_ossutil() -> str:
    for name in ("ossutil.exe", "ossutil64.exe", "ossutil"):
        executable = shutil.which(name)
        if executable:
            return executable
    raise FileNotFoundError("找不到 ossutil；请先安装并配置 RAM 用户凭证")


def _parse_stat(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = key.strip().casefold()
        if normalized:
            values[normalized] = value.strip()
    return values


def _stat_matches(
    storage: OssReleaseStorage,
    item: OssReleaseObject,
    *,
    executable: str,
    runner: Runner,
) -> bool:
    object_key = storage.object_key(item.category, item.local_path.name)
    result = runner(
        [
            executable,
            "stat",
            storage.object_uri(object_key),
            "-e",
            storage.endpoint,
        ],
        cwd=COMMERCIAL_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = _parse_stat(result.stdout)
    return (
        int(metadata.get("content-length", "0")) == item.local_path.stat().st_size
        and metadata.get("x-oss-meta-sha256", "").upper() == item.sha256.upper()
    )


def upload_release_objects(
    storage: OssReleaseStorage,
    objects: Sequence[OssReleaseObject],
    *,
    executable: str | None = None,
    runner: Runner = subprocess.run,
) -> None:
    """Upload verified release objects. Callers must never invoke this during dry-run."""
    tool = executable or find_ossutil()
    for item in objects:
        local_path = item.local_path.expanduser().resolve()
        if not local_path.is_file() or local_path.stat().st_size <= 0:
            raise ValueError(f"OSS 待上传文件不存在或为空：{local_path}")
        normalized_sha = item.sha256.strip().upper()
        if not re.fullmatch(r"[A-F0-9]{64}", normalized_sha):
            raise ValueError(f"OSS 待上传文件摘要无效：{local_path.name}")
        normalized_item = OssReleaseObject(
            local_path=local_path,
            category=item.category,
            sha256=normalized_sha,
            content_type=item.content_type,
            cache_control=item.cache_control,
        )
        try:
            if _stat_matches(
                storage,
                normalized_item,
                executable=tool,
                runner=runner,
            ):
                print(f"OSS 已存在摘要一致的文件，跳过上传：{local_path.name}", flush=True)
                continue
        except subprocess.CalledProcessError:
            pass

        object_key = storage.object_key(item.category, local_path.name)
        metadata = "#".join(
            (
                f"Content-Type:{item.content_type}",
                f"Cache-Control:{item.cache_control}",
                f"x-oss-meta-sha256:{normalized_sha}",
            )
        )
        command = [
            tool,
            "cp",
            str(local_path),
            storage.object_uri(object_key),
            "-e",
            storage.endpoint,
            "--meta",
            metadata,
            "--acl",
            "private",
            "--force",
        ]
        print("+", subprocess.list2cmdline(command), flush=True)
        runner(command, cwd=COMMERCIAL_ROOT, check=True)
        if not _stat_matches(
            storage,
            normalized_item,
            executable=tool,
            runner=runner,
        ):
            raise RuntimeError(f"OSS 上传后大小或 SHA-256 元数据不匹配：{local_path.name}")
