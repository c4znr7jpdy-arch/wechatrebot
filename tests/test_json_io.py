import json

import pytest

from wechatrebot_manager import json_io


def windows_lock():
    error = PermissionError("temporarily locked")
    error.winerror = 32
    return error


def test_atomic_config_save_retries_temporary_windows_lock(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"value":"old"}', encoding="utf-8")
    replace = json_io.os.replace
    calls, waits = [], []
    def locked_once(source, destination):
        calls.append(source)
        assert json.loads(path.read_text("utf-8"))["value"] == "old"
        if len(calls) == 1:
            raise windows_lock()
        replace(source, destination)
    monkeypatch.setattr(json_io.os, "replace", locked_once)
    monkeypatch.setattr(json_io.time, "sleep", waits.append)
    json_io.write_json_atomic(path, {"value": "new"})
    assert waits == [0.05]
    assert json.loads(path.read_text("utf-8")) == {"value": "new"}
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("transient", [True, False])
def test_atomic_config_save_failure_keeps_original_and_is_bounded(tmp_path, monkeypatch, transient):
    path = tmp_path / "config.json"
    path.write_text('{"value":"old"}', encoding="utf-8")
    waits, calls = [], []
    def denied(*args):
        calls.append(args)
        raise windows_lock() if transient else PermissionError("not writable")
    monkeypatch.setattr(json_io.os, "replace", denied)
    monkeypatch.setattr(json_io.time, "sleep", waits.append)
    with pytest.raises(PermissionError):
        json_io.write_json_atomic(path, {"value": "new"})
    assert len(calls) == (5 if transient else 1)
    assert len(waits) == (4 if transient else 0)
    assert json.loads(path.read_text("utf-8")) == {"value": "old"}
    assert list(tmp_path.iterdir()) == [path]
