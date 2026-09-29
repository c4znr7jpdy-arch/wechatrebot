import os
import queue
import threading
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native uninstaller UI")


def test_progress_remains_responsive_and_only_closes_after_completion() -> None:
    import tkinter as tk
    from wechatrebot_manager.uninstall_ui import UninstallProgressWindow

    root = tk.Tk()
    gate = threading.Event()
    worker_threads = []
    observations = []

    def operation(progress):
        worker_threads.append(threading.get_ident())
        progress("测试清理阶段")
        if not gate.wait(timeout=3):
            raise RuntimeError("UI did not respond")

    window = UninstallProgressWindow(root, operation)
    root.withdraw()

    def release_worker():
        observations.append((window.busy, str(window.close_button["state"]), window.status.get()))
        window.close()
        observations.append(bool(root.winfo_exists()))
        gate.set()

    def check_finished():
        if window.busy:
            root.after(20, check_finished)
        else:
            observations.append((root.title(), str(window.close_button["state"])))
            window.close()

    root.after(200, release_worker)
    root.after(250, check_finished)
    root.after(4000, root.destroy)
    try:
        root.mainloop()
        assert window.error is None
        assert observations == [
            (True, "disabled", "测试清理阶段"), True,
            ("微信机器人 · 卸载完成", "normal"),
        ]
        assert worker_threads[0] != threading.get_ident()
    finally:
        gate.set()
        try:
            root.destroy()
        except tk.TclError:
            pass


def test_worker_failure_reaches_visible_error_handler() -> None:
    from wechatrebot_manager.uninstall_ui import UninstallProgressWindow

    def fail(_progress):
        raise OSError("测试：文件被占用")

    window = object.__new__(UninstallProgressWindow)
    window.operation = fail
    window.events = queue.Queue()
    window.error = None
    window.busy = True
    window.progress = SimpleNamespace(stop=lambda: None)
    destroyed = []
    window.root = SimpleNamespace(destroy=lambda: destroyed.append(True))

    window._work()
    window._poll()

    assert isinstance(window.error, OSError)
    assert "文件被占用" in str(window.error)
    assert not window.busy
    assert destroyed == [True]
