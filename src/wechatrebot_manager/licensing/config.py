from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .protocol import LicenseError, decode, parse_json

PACKAGED_CHANNEL = Path(__file__).resolve().parents[1] / "licensing-channel.json"

@dataclass(frozen=True)
class LicenseChannel:
    enabled: bool
    service_url: str
    public_keys: dict[str, str]

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.service_url and self.public_keys)


def _read_channel(path: Path) -> LicenseChannel:
    try:
        value = parse_json(path.read_bytes())
        if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
                or type(value.get("enabled")) is not bool):
            raise ValueError()
        url = str(value.get("service_url", "")).rstrip("/")
        parsed = urlsplit(url)
        if url and (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment
                    or parsed.port is not None and not 1 <= parsed.port <= 65535):
            raise ValueError()
        keys = value.get("public_keys", {})
        if not isinstance(keys, dict):
            raise ValueError()
        for key_id, key in keys.items():
            if not isinstance(key_id, str) or not key_id:
                raise ValueError()
            decode(key, 32)
        return LicenseChannel(value["enabled"], url, keys)
    except (OSError, ValueError, TypeError) as exc:
        raise LicenseError("configuration_invalid", "授权配置无效，请联系维护者。") from exc


def load_channel(root: Path) -> LicenseChannel:
    packaged = _read_channel(PACKAGED_CHANNEL)
    # Commercial distributions pin both the trust root and enforcement in the
    # packaged channel. A customer backup or local setting cannot replace them.
    if packaged.enabled:
        return packaged
    path = root / "config/licensing.json"
    if path.exists():
        return _read_channel(path)
    return packaged
