"""Windows loopback mitigation preserves Chromium's existing launch defaults."""
import sys

import pytest
from playwright.sync_api import sync_playwright


@pytest.mark.parametrize("arguments,features", [
    (["--headless", "--disable-features=FeatureA,FeatureB"], ["FeatureA", "FeatureB", "TcpPortRandomizationWin"]),
    (["--disable-features=FeatureA,TcpPortRandomizationWin"], ["FeatureA", "TcpPortRandomizationWin"]),
    (["--headless"], ["TcpPortRandomizationWin"]),
])
def test_socket_options_keep_existing_disabled_features(arguments, features):
    from conftest import _chromium_socket_launch_options

    original = list(arguments)
    options = _chromium_socket_launch_options(arguments)
    assert options["ignore_default_args"] == [arg for arg in arguments if arg.startswith("--disable-features=")]
    assert options["args"] == ["--disable-features=" + ",".join(features)]
    assert arguments == original


def test_socket_options_reject_ambiguous_default_switches():
    from conftest import _chromium_socket_launch_options

    with pytest.raises(RuntimeError, match="disable-features"):
        _chromium_socket_launch_options(["--disable-features=FeatureA", "--disable-features=FeatureB"])


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_other_platforms_keep_default_launch_options(monkeypatch, platform):
    import conftest

    monkeypatch.setattr(conftest.sys, "platform", platform)
    assert conftest.manager_browser_launch_options.__wrapped__() == {}


def test_real_chromium_keeps_defaults_and_disables_randomized_windows_ports(manager_browser_launch_options):
    if sys.platform != "win32":
        assert manager_browser_launch_options == {}
        return
    options = manager_browser_launch_options
    assert len(options["ignore_default_args"]) == 1
    original = options["ignore_default_args"][0].split("=", 1)[1].split(",")
    # This CDP inspection requires the automation switch; normal manager tests
    # keep their launch defaults and only replace the combined feature argument.
    with sync_playwright() as runtime:
        instance = runtime.chromium.launch(**{**options, "args": [*options["args"], "--enable-automation"]})
        try:
            arguments = instance.new_browser_cdp_session().send("Browser.getBrowserCommandLine")["arguments"]
            disabled = [arg for arg in arguments if arg.startswith("--disable-features=")]
            assert disabled == options["args"]
            features = disabled[0].split("=", 1)[1].split(",")
            assert set(original) <= set(features)
            assert features.count("TcpPortRandomizationWin") == 1
        finally:
            instance.close()
