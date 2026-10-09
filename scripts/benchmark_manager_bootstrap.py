"""Measure bootstrap's local work using synthetic data and a stopped runtime.

Run this same command before and after a change to compare reports. This is a
warm-filesystem microbenchmark, not installed-app or real-WeChat startup timing.
It never reads an installation, starts services, or sends network requests.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wechatrebot_manager import app_shell
from wechatrebot_manager.experience import ExperienceService
from wechatrebot_manager.plugin_center import PluginCenterService


def _write_fixture(root: Path, providers: int, plugins: int) -> None:
    data = root / "app/astrbot/data"
    configs = data / "config"
    configs.mkdir(parents=True)
    provider_ids = [f"benchmark/model-{number}" for number in range(providers)]
    engine = {
        "admins_id": ["wxid_benchmark"],
        "provider_settings": {"default_provider_id": provider_ids[0]},
        "provider_sources": [{
            "id": "benchmark", "api_base": "https://benchmark.invalid/v1",
            "key": ["synthetic-benchmark-key"],
        }],
        "provider": [{
            "id": provider_id, "provider_source_id": "benchmark",
            "model": f"model-{number}", "enable": True,
        } for number, provider_id in enumerate(provider_ids)],
    }
    (data / "cmd_config.json").write_text(json.dumps(engine), encoding="utf-8")
    for number in range(plugins):
        payload = {
            "provider": provider_ids[number % providers],
            "nested": {
                "fallbacks": [provider_ids[(number + 1) % providers]],
                "unrelated_values": [f"setting-{item}" for item in range(100)],
            },
            "description": "Synthetic plugin settings. " * 40,
        }
        (configs / f"benchmark_{number:03d}_config.json").write_text(
            json.dumps(payload), encoding="utf-8",
        )


def _offline_api(root: Path, probes: Counter) -> app_shell.ApplicationApi:
    # Bypass app construction: it has lifecycle adoption and installation
    # maintenance responsibilities that are outside this offline measurement.
    api = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    api._install_root = root
    api._development = True
    api._update_result = None
    api._experience = ExperienceService(root)
    api._features = PluginCenterService(root)
    api._features.engine = SimpleNamespace(is_available=lambda: False)

    def refresh():
        probes["lifecycle_refresh"] += 1
        return SimpleNamespace(to_dict=lambda: {
            "state": "STOPPED", "online": False,
            "environment": {"ready": False},
            "runtime": {"wechat_identity": "", "account_status_available": False},
        })

    api._lifecycle = SimpleNamespace(refresh=refresh)
    return api


def measure(*, providers: int, plugins: int, samples: int, warmups: int) -> dict:
    reads: Counter = Counter()
    probes: Counter = Counter()
    original_read = Path.read_text
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bootstrap-benchmark-", dir=artifacts) as directory:
        root = Path(directory).resolve()
        _write_fixture(root, providers, plugins)
        api = _offline_api(root, probes)

        def read_text(path, *args, **kwargs):
            if path.is_relative_to(root):
                category = "plugin_config" if path.name.endswith("_config.json") else "other"
                if path.name == "cmd_config.json":
                    category = "engine_config"
                reads[category] += 1
            return original_read(path, *args, **kwargs)

        def bootstrap():
            result = api.bootstrap()
            if not result.get("ok"):
                raise RuntimeError("Synthetic bootstrap failed")
            data = result["data"]
            if len(data["providers"]) != providers or not data["provider"]["configured"]:
                raise RuntimeError("Synthetic provider fixture was not loaded")
            return data

        timings = []
        stage_timings: dict[str, list[float]] = {}
        with patch(
            "socket.socket.connect", side_effect=AssertionError("Network is forbidden in this benchmark"),
        ):
            for _ in range(warmups):
                bootstrap()
            reads.clear()
            probes.clear()
            for _ in range(samples):
                started = time.perf_counter()
                data = bootstrap()
                timings.append((time.perf_counter() - started) * 1000)
                for name, value in data["startup_timing_ms"].items():
                    stage_timings.setdefault(name, []).append(value)
            # Count I/O in a separate run so instrumentation does not inflate
            # the latency being compared, especially for the slower baseline.
            probes.clear()
            with patch.object(Path, "read_text", read_text):
                bootstrap()
        # Report only an anonymous public-result digest, never configuration.
        for name in ("startup_timing_ms", "install_root"):
            data.pop(name, None)
        result_digest = hashlib.sha256(
            json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        ).hexdigest()

    return {
        "scope": "synthetic_offline_bootstrap_warm_filesystem",
        "python": platform.python_version(),
        "platform": platform.system(),
        "fixture": {"providers": providers, "plugins": plugins},
        "samples": samples,
        "warmups": warmups,
        "wall_ms": {
            "median": round(statistics.median(timings), 3),
            "min": round(min(timings), 3), "max": round(max(timings), 3),
        },
        "stage_median_ms": {name: statistics.median(values) for name, values in stage_timings.items()},
        "reads_per_bootstrap": dict(sorted(reads.items())),
        "probes_per_bootstrap": dict(sorted(probes.items())),
        "public_result_sha256": result_digest,
        "source_sha256": {
            name: hashlib.sha256((ROOT / "src/wechatrebot_manager" / name).read_bytes()).hexdigest()
            for name in ("app_shell.py", "customer_config.py")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--providers", type=int, default=12)
    parser.add_argument("--plugins", type=int, default=40)
    parser.add_argument("--samples", type=int, default=15)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.providers <= 200 or not 0 <= args.plugins <= 500:
        parser.error("providers must be 1..200 and plugins must be 0..500")
    if not 1 <= args.samples <= 200 or not 0 <= args.warmups <= 20:
        parser.error("samples must be 1..200 and warmups must be 0..20")
    report = measure(providers=args.providers, plugins=args.plugins, samples=args.samples, warmups=args.warmups)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
