"""One home-screen environment action; verification never blocks status polling."""
from __future__ import annotations

import threading

from .runtime_deployment import missing_packages
from .runtime_install import ENVIRONMENT_JOURNAL, install_environment
from .runtime_manifest import ENGINE_PATH, ENVIRONMENT_MANIFEST, load_environment
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
                if not (self._install_root / ENVIRONMENT_MANIFEST).exists() and not (self._install_root / ENGINE_PATH).exists():
                    self._runtime_status = {"state": "legacy", "ready": True}
                else:
                    self._runtime_status = {"state": "checking", "ready": False,
                                            "message": "正在检查运行环境…"}
                    try:
                        threading.Thread(target=self._check_runtime_environment, daemon=True).start()
                    except RuntimeError as exc:
                        self._set_runtime_status(state="failed", message=str(exc))
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
            environment = load_environment(self._install_root, self._runtime_channel())
            if environment is None:
                raise UpdateError("运行环境清单缺失，请修复安装。")
            missing = missing_packages(environment, self._install_root)
            self._set_runtime_status(state="missing" if missing else "ready", ready=not missing,
                                     download_size=sum(item.size for item in missing), percent=0,
                                     message="首次使用前，请安装运行环境。" if missing else "运行环境已就绪。")
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
            if (getattr(self, "_update_installing", False) or status["state"] in {"checking", "installing"}
                    or status["ready"]):
                raise UpdateError("正在检查、安装或环境已就绪，请稍后重试。")
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
