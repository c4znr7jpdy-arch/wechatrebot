import os
import queue
import threading
from types import SimpleNamespace

import pytest

from wechatrebot_manager import update_progress
from wechatrebot_manager.update_errors import UpdateFailure


class Widget:
    def __init__(self):
        self.options = {}
        self.packed = False
        self.running = False

    def configure(self, **values):
        self.options.update(values)

    def pack(self, **_kwargs):
        self.packed = True

    def focus_set(self):
        pass

    def start(self, _interval):
        self.running = True

    def stop(self):
        self.running = False


def bare_window(operation):
    window = object.__new__(update_progress.UpdateProgressWindow)
    window.operation = operation
    window.events = queue.SimpleQueue()
    window._progress_lock = threading.Lock()
    window._pending_progress = None
    window._indeterminate = True
    window._started = False
    window.busy = True
    window.result = 1
    window.error = None
    window.progress, window.heading, window.hint, window.close_button = (Widget() for _ in range(4))
    state = {"destroyed": False, "status": "", "title": "", "polls": []}
    window.status = SimpleNamespace(set=lambda value: state.update(status=value))
    window.root = SimpleNamespace(
        destroy=lambda: state.update(destroyed=True),
        title=lambda value: state.update(title=value),
        after=lambda delay, callback: state["polls"].append((delay, callback)),
        lift=lambda: None,
    )
    return window, state


def test_success_closes_automatically_without_finish_button():
    window, state = bare_window(lambda progress: progress("install", 3, 3) or 0)
    window.close()
    assert not state["destroyed"]
    window._work()
    window._poll()
    assert not window.busy and window.result == 0
    assert state["destroyed"]
    assert not window.close_button.packed


@pytest.mark.parametrize("error", [
    RuntimeError("文件被占用；原版本已恢复。"),
    UpdateFailure("UPD-1010", "private diagnostic", public_message="恢复未完成，请联系技术支持。"),
])
def test_failure_stays_visible_until_closed_with_actual_outcome(error):
    def fail(_progress):
        raise error

    window, state = bare_window(fail)
    window._work()
    window._poll()
    assert not window.busy and window.result == 1
    assert not state["destroyed"]
    assert "更新未完成" in state["title"]
    assert "private diagnostic" not in state["status"]
    assert state["status"] == (error.public_message if isinstance(error, UpdateFailure) else str(error))
    assert window.close_button.packed
    window.close()
    assert state["destroyed"]


def test_nonzero_exit_does_not_invent_successful_recovery():
    window, state = bare_window(lambda _progress: 2)
    window._work()
    window._poll()
    assert window.result == 2
    assert "成功" not in state["status"]
    assert "已恢复" not in state["status"]
    assert not state["destroyed"]


def test_progress_reports_real_stage_counts_without_an_unbounded_queue():
    window, state = bare_window(lambda _progress: 0)
    for index in range(40_000):
        window._report_progress("install", index + 1, 40_000)
    assert window.events.empty()
    window._poll()
    assert state["status"] == "正在覆盖安装 · 40000 / 40000"
    assert window.progress.options == {"mode": "determinate", "value": 100}
    window._report_progress("awaiting_startup", 0, 0)
    window._poll()
    assert state["status"] == "正在等待新版窗口就绪"
    assert window.progress.running
    assert window._pending_progress is None


@pytest.mark.parametrize("failure_at", ["construct", "visibility", "ready"])
def test_handoff_failure_never_starts_mutating_operation(monkeypatch, failure_at):
    import tkinter as tk

    calls = []

    def fail_at(stage):
        calls.append(stage)
        if stage == failure_at:
            raise RuntimeError(stage)

    root = SimpleNamespace(
        winfo_viewable=lambda: False,
        after=lambda *_args: "visibility-timeout",
        after_cancel=lambda _timer: None,
        wait_visibility=lambda: fail_at("visibility"),
        mainloop=lambda: calls.append("mainloop"),
        destroy=lambda: calls.append("destroy"),
    )
    monkeypatch.setattr(tk, "Tk", lambda: root)

    def construct(_root, _operation):
        fail_at("construct")
        return SimpleNamespace(start=lambda: calls.append("start"))

    monkeypatch.setattr(update_progress, "UpdateProgressWindow", construct)
    with pytest.raises(RuntimeError, match=failure_at):
        update_progress.run_update_progress(lambda _progress: 0, on_ready=lambda: fail_at("ready"))
    assert "start" not in calls and "mainloop" not in calls
    assert calls[-1] == "destroy"


@pytest.mark.skipif(os.name != "nt", reason="Windows native updater UI")
def test_real_window_maps_before_handoff_and_finishes_without_user_input(monkeypatch):
    import tkinter as tk

    root = tk.Tk()
    real_window = update_progress.UpdateProgressWindow
    windows, ready, worker_threads, observations = [], [], [], []
    gate = threading.Event()
    timed_out = []

    def construct(*args, **kwargs):
        window = real_window(*args, **kwargs)
        windows.append(window)
        return window

    def operation(progress):
        assert ready == [True]
        worker_threads.append(threading.get_ident())
        progress("install", 1, 2)
        if not gate.wait(timeout=4):
            raise RuntimeError("UI did not respond")
        progress("awaiting_startup", 0, 0)
        return 0

    def observe_then_release():
        window = windows[0]
        observations.append((window.busy, window.close_button.winfo_ismapped()))
        window.close()
        observations.append(bool(root.winfo_exists()))
        gate.set()

    def timeout():
        timed_out.append(True)
        gate.set()
        root.destroy()

    monkeypatch.setattr(tk, "Tk", lambda: root)
    monkeypatch.setattr(update_progress, "UpdateProgressWindow", construct)
    root.after(200, observe_then_release)
    root.after(5000, timeout)
    try:
        result = update_progress.run_update_progress(
            operation, on_ready=lambda: ready.append(bool(root.winfo_viewable())),
        )
        assert result == 0
        assert observations == [(True, 0), True]
        assert not timed_out
        assert worker_threads and worker_threads[0] != threading.get_ident()
        assert windows[0].error is None
    finally:
        gate.set()
        try:
            root.destroy()
        except tk.TclError:
            pass
