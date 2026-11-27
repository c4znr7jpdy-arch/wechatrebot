"""Reuse completed environment installations; verify files only during installation."""
from __future__ import annotations

import json
from pathlib import Path
import threading

from .payload import _checked_target
from .runtime_install import ENVIRONMENT_JOURNAL, install_environment
from .runtime_manifest import (
    ENGINE_PATH, ENVIRONMENT_MANIFEST, ENVIRONMENT_RECEIPT, REQUIRED_RUNTIME_FILES, load_environment,
)
from .update_manifest import UpdateError


class RuntimeApiMixin:
    def _set_runtime_status(self, **values):
        lock = self.__dict__.setdefault("_runtime_status_lock", threading.RLock())
        with lock:
            self._runtime_status = {**getattr(self, "_runtime_status", {}), **values}

    def _runtime_environment_status(self):
        lock = self.__dict__.setdefault("_runtime_status_lock", threading.RLock())
        with lock:
            if not hasattr(self, "_runtime_status"):
                self._check_runtime_environment()
            return dict(self._runtime_status)

    def _runtime_channel(self):
        channel = self._load_update_channel()
        if channel is None:
            raise UpdateError("运行环境校验配置不可用，请修复安装。")
        return channel

    def _check_runtime_environment(self):
        try:
            if (self._install_root / ENVIRONMENT_JOURNAL).exists():
                raise UpdateError("上次环境安装中断，请点击重试恢复。")
            if not (self._install_root / ENVIRONMENT_MANIFEST).exists() and not (self._install_root / ENGINE_PATH).exists():
                self._set_runtime_status(state="legacy", ready=True)
                return
            environment = load_environment(self._install_root, self._runtime_channel())
            if environment is None:
                raise UpdateError("运行环境清单缺失，请修复安装。")
            # The install transaction writes this receipt after verifying files.
            # Opening the manager or refreshing a license must not hash them again.
            receipt_path = _checked_target(self._install_root, ENVIRONMENT_RECEIPT)
            try:
                if receipt_path.stat().st_size > 4096:
                    raise UpdateError("运行环境安装记录无效，请重新安装运行环境。")
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
                receipt = None
            ready = (isinstance(receipt, dict) and type(receipt.get("schema_version")) is int
                     and receipt["schema_version"] == 1 and receipt.get("id") == environment.identity
                     and all(_checked_target(self._install_root, Path(path)).is_file()
                             for path in REQUIRED_RUNTIME_FILES))
            self._set_runtime_status(state="ready" if ready else "missing", ready=ready,
                                     download_size=0, percent=0,
                                     message="运行环境已就绪。" if ready else "请安装运行环境，已有环境会自动复用。")
        except Exception as exc:
            self._set_runtime_status(state="failed", ready=False, message=str(exc))

    def install_runtime_environment(self):
        if not self._update_lock.acquire(blocking=False):
            return self._ok({"accepted": False}, message="正在准备应用更新，请稍后重试。")
        if not self._action_lock.acquire(blocking=False):
            self._update_lock.release()
            return self._ok({"accepted": False}, message="正在处理机器人操作，请稍后重试。")
        try:
            status = self._runtime_environment_status()
            if (getattr(self, "_update_installing", False) or status["state"] == "installing"
                    or status["ready"]):
                raise UpdateError("正在安装或环境已就绪，请稍后重试。")
            if not self._lifecycle.maintenance_stopped():
                raise UpdateError("请先停止机器人，再安装运行环境。")
            channel = self._runtime_channel()
            self._set_runtime_status(state="installing", ready=False, percent=0, message="正在准备运行环境…")

            def worker():
                try:
                    install_environment(self._install_root, channel, progress_callback=self._runtime_progress)
                    self._check_runtime_environment()
                except Exception as exc:
                    self._set_runtime_status(state="failed", ready=False, message=str(exc))
                finally:
                    self._action_lock.release()
                    self._update_lock.release()

            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            if getattr(self, "_runtime_status", {}).get("state") == "installing":
                self._set_runtime_status(state="failed", ready=False, message=str(exc))
            self._action_lock.release()
            self._update_lock.release()
            return self._failed(exc)
        return self._ok({"accepted": True}, message="正在安装运行环境。")

    def _runtime_progress(self, phase, completed, total):
        labels = {"runtime_check": "正在检查可复用的环境", "runtime_download": "正在下载运行环境",
                  "prepare": "正在校验环境文件", "install": "正在安装运行环境", "remove": "正在整理旧环境"}
        self._set_runtime_status(state="installing", ready=False, phase=phase,
                                 percent=min(100, round(completed * 100 / total)) if total else 0,
                                 message=labels.get(phase, "正在准备运行环境"))
