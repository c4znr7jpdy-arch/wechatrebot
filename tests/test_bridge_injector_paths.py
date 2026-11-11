"""Native injector path aliases stay compatible and identify the original files."""

import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_license_runtime_patches import bridge, patched  # noqa: F401


@pytest.fixture
def launcher(bridge):
    return importlib.import_module(bridge.app.__package__ + ".launcher")


class ShortPathAPI:
    def __init__(self, aliases, *, initial_capacity=None):
        self.aliases = aliases
        self.initial_capacity = initial_capacity
        self.calls = []

    def __call__(self, original, buffer, capacity):
        self.calls.append((original, buffer is None, capacity))
        alias = self.aliases[original]
        needed = len(alias.encode("utf-16-le")) // 2 + 1
        if buffer is None:
            return needed if self.initial_capacity is None else self.initial_capacity
        if capacity < needed:
            return needed
        buffer.value = alias
        return needed - 1


def install_api(monkeypatch, launcher, api):
    load = Mock(return_value=SimpleNamespace(GetShortPathNameW=api))
    monkeypatch.setattr(launcher.ctypes, "WinDLL", load, raising=False)
    return load


def native_file(tmp_path, name="微信😀.exe", alias="WEIXIN~1.EXE"):
    original = tmp_path / name
    original.write_bytes(b"fixture only; never execute")
    compatible = tmp_path / alias
    os.link(original, compatible)
    return original, compatible


def settings(wechat, dll, injector):
    return SimpleNamespace(
        wechat_exe_path=wechat, hook_dll_path=dll, injector_path=injector,
        hook_api_port=19088, callback_url="http://127.0.0.1:5000/api/recvMsg",
        preflight=lambda **_: [],
    )


def test_ascii_arguments_and_shell_free_launch_stay_unchanged(launcher, monkeypatch, tmp_path):
    config = settings(tmp_path / "Weixin.exe", tmp_path / "hook.dll", tmp_path / "x64 inject.exe")
    api = install_api(monkeypatch, launcher, Mock(side_effect=AssertionError("ASCII needs no alias")))
    popen = Mock(return_value=object())
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)

    assert launcher.start_injector(config) is popen.return_value
    command = popen.call_args.args[0]
    assert command[:3] == [str(config.injector_path), str(config.wechat_exe_path), str(config.hook_dll_path)]
    assert popen.call_args.kwargs == {
        "cwd": config.injector_path.parent, "stdout": launcher.subprocess.PIPE,
        "stderr": launcher.subprocess.PIPE, "shell": False,
    }
    api.assert_not_called()


def test_only_native_file_arguments_use_verified_aliases(launcher, monkeypatch, tmp_path):
    wechat, wechat_alias = native_file(tmp_path)
    dll, dll_alias = native_file(tmp_path, "机器人.dll", "ROBOT~1.DLL")
    injector = tmp_path / "安装目录" / "x64 inject.exe"
    config = settings(wechat, dll, injector)
    api = ShortPathAPI({str(wechat): str(wechat_alias), str(dll): str(dll_alias)})
    load = install_api(monkeypatch, launcher, api)
    popen = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)

    launcher.start_injector(config)
    command = popen.call_args.args[0]
    assert command[:3] == [str(injector), str(wechat_alias), str(dll_alias)]
    assert json.loads(command[3]) == {
        "recivemode": "http", "tcp_ip": "127.0.0.1", "tcp_port": 61108,
        "http_server_port": 19088, "http_callback_url": config.callback_url,
        "usedefault": False, "start_server_while_login": True,
    }
    assert config.wechat_exe_path == wechat and config.hook_dll_path == dll
    assert popen.call_args.kwargs["cwd"] == injector.parent
    assert popen.call_args.kwargs["shell"] is False
    assert all(call.args == ("kernel32",) and call.kwargs == {"use_last_error": True}
               for call in load.call_args_list)
    assert api.argtypes == (launcher.wintypes.LPCWSTR, launcher.wintypes.LPWSTR, launcher.wintypes.DWORD)
    assert api.restype is launcher.wintypes.DWORD


def test_buffer_uses_api_character_capacity_and_retries_growth(launcher, monkeypatch, tmp_path):
    original, alias = native_file(tmp_path)
    api = ShortPathAPI({str(original): str(alias)}, initial_capacity=1)
    install_api(monkeypatch, launcher, api)

    assert launcher._native_file_argument(original, "微信程序") == str(alias)
    needed = len(str(alias).encode("utf-16-le")) // 2 + 1
    assert api.calls == [(str(original), True, 0), (str(original), False, 1),
                         (str(original), False, needed)]


@pytest.mark.parametrize("failure", [
    "load", "query-zero", "query-oversize", "fill-zero", "unstable-size",
    "unicode-alias", "relative-alias", "missing-alias", "different-file", "identity-unavailable",
])
def test_unusable_alias_fails_in_chinese_before_native_launch(launcher, monkeypatch, tmp_path, failure):
    original, alias = native_file(tmp_path)
    if failure == "unicode-alias":
        alias = original
    elif failure == "relative-alias":
        monkeypatch.chdir(tmp_path)
        alias = Path(alias.name)
    elif failure == "missing-alias":
        alias = tmp_path / "missing.exe"
    elif failure == "different-file":
        alias = tmp_path / "different.exe"
        alias.write_bytes(original.read_bytes())  # Equal content is not equal identity.
    api = ShortPathAPI({str(original): str(alias)})
    if failure == "query-zero":
        api = Mock(return_value=0)
    elif failure == "query-oversize":
        api = Mock(return_value=32769)
    elif failure == "fill-zero":
        api = Mock(side_effect=[64, 0])
    elif failure == "unstable-size":
        api = Mock(side_effect=[1, 2, 3])
    load = install_api(monkeypatch, launcher, api)
    if failure == "load":
        load.side_effect = OSError("synthetic missing Windows API")
    if failure == "identity-unavailable":
        monkeypatch.setattr(Path, "samefile", Mock(side_effect=OSError("synthetic identity failure")))
    popen = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)

    with pytest.raises(launcher.SettingsError, match="微信程序路径含有.*不支持的字符.*兼容短路径") as caught:
        launcher.start_injector(settings(original, tmp_path / "hook.dll", tmp_path / "inject.exe"))
    assert "请调整为仅含英文、数字和常规符号的路径后重试" in str(caught.value)
    assert str(original) not in str(caught.value)
    popen.assert_not_called()


def test_dll_alias_failure_names_robot_installation(launcher, monkeypatch, tmp_path):
    dll, _ = native_file(tmp_path, "机器人.dll", "ROBOT~1.DLL")
    install_api(monkeypatch, launcher, Mock(return_value=0))
    popen = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)

    with pytest.raises(launcher.SettingsError, match="机器人安装目录路径含有.*不支持的字符"):
        launcher.start_injector(settings(tmp_path / "Weixin.exe", dll, tmp_path / "inject.exe"))
    popen.assert_not_called()
