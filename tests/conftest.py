from pathlib import Path
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import hashlib

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SOURCE = ROOT / "vendor/data/plugins/jiang_image"


def _chromium_socket_launch_options(arguments):
    disabled = [arg for arg in arguments if arg.startswith("--disable-features=")]
    if len(disabled) > 1:
        raise RuntimeError("Expected at most one Chromium --disable-features default")
    features = disabled[0].split("=", 1)[1].split(",") if disabled else []
    features = list(dict.fromkeys([*filter(None, features), "TcpPortRandomizationWin"]))
    return {"ignore_default_args": disabled, "args": ["--disable-features=" + ",".join(features)]}


@pytest.fixture(scope="session")
def manager_browser_launch_options():
    if sys.platform != "win32":
        return {}
    from playwright.sync_api import sync_playwright

    # Backport https://github.com/microsoft/playwright/pull/43605 without
    # replacing Playwright's other disabled features or reading driver internals.
    # A single short-lived probe captures the installed version's real defaults.
    with sync_playwright() as runtime:
        instance = runtime.chromium.launch(args=["--enable-automation"])
        try:
            arguments = instance.new_browser_cdp_session().send("Browser.getBrowserCommandLine")["arguments"]
        finally:
            instance.close()
    return _chromium_socket_launch_options(arguments)


@pytest.fixture(autouse=True)
def manager_browser_failure_evidence(request, monkeypatch):
    """Keep startup evidence even when an early assertion skips final error checks."""
    # These servers contain synthetic preview data; native/account sessions are
    # intentionally outside automatic screenshot and browser-event capture.
    if "browser" not in request.fixturenames or not {"manager_url", "upload_server"}.intersection(request.fixturenames):
        return
    tmp_path = request.getfixturevalue("tmp_path")
    browser = request.getfixturevalue("browser")
    new_page = browser.new_page
    page_number = 0

    def observed_page(*args, **kwargs):
        nonlocal page_number
        page_number += 1
        page = new_page(*args, **kwargs)
        events = []
        page.add_init_script("""(() => {
          const failures = [];
          window.__managerTestErrors = failures;
          Object.defineProperty(window, 'managerErrorReporter', {
            configurable: true,
            set(value) {
              const capture = value?.capture;
              if (capture) value.capture = function(error, operation) {
                failures.push({message: String(error?.message || error),
                  stack: String(error?.stack || ''), operation});
                return capture.apply(this, arguments);
              };
              Object.defineProperty(window, 'managerErrorReporter', {value, writable: true, configurable: true});
            }
          });
        })();""")
        page.on("console", lambda message: events.append({"kind": "console", "level": message.type, "text": message.text})
                if message.type in {"error", "warning"} else None)
        page.on("pageerror", lambda error: events.append({"kind": "pageerror", "text": str(error)}))
        page.on("requestfailed", lambda item: events.append({"kind": "requestfailed", "url": item.url,
                                                            "resource_type": item.resource_type, "error": item.failure}))
        page.on("response", lambda response: events.append({"kind": "response", "url": response.url, "status": response.status})
                if response.status >= 400 else None)
        close = page.close
        number = page_number

        def report(text):
            try:
                for phase in ("setup", "call", "teardown"):
                    request.node.add_report_section(phase, "manager browser failure", text)
            except Exception:
                # Diagnostics must never replace the test's original failure.
                return

        def close_with_evidence(*close_args, **close_kwargs):
            original_error = sys.exc_info()[1]
            result = None
            try:
                if original_error is not None and not page.is_closed():
                    directory = tmp_path / "browser-failure" / str(number)
                    directory.mkdir(parents=True, exist_ok=True)
                    evidence = {"url": page.url, "events": events}
                    try:
                        evidence["state"] = page.evaluate("""() => ({
                          readyState: document.readyState,
                          reportedErrors: window.__managerTestErrors,
                          text: Object.fromEntries(['versionText', 'stateTitle', 'stateMessage', 'toastRegion']
                            .map(id => [id, document.getElementById(id)?.textContent])),
                          scripts: [...document.scripts].map(script => ({src: script.src})),
                          resources: performance.getEntriesByType('resource').map(item => ({
                            name: item.name, startTime: item.startTime, duration: item.duration,
                            transferSize: item.transferSize, responseStatus: item.responseStatus}))
                        })""")
                        page.screenshot(path=str(directory / "page.png"), timeout=5000)
                    except Exception as error:
                        evidence["capture_error"] = f"{type(error).__name__}: {error}"
                    serialized = json.dumps(evidence, ensure_ascii=False, indent=2)
                    (directory / "browser.json").write_text(serialized, encoding="utf-8")
                    report(serialized)
            except Exception as error:
                report(f"Evidence capture failed: {type(error).__name__}: {error}")
            finally:
                try:
                    result = close(*close_args, **close_kwargs)
                except Exception as error:
                    if original_error is None:
                        raise
                    report(f"Page close failed: {type(error).__name__}: {error}")
            return result

        monkeypatch.setattr(page, "close", close_with_evidence)
        return page

    monkeypatch.setattr(browser, "new_page", observed_page)


@pytest.fixture(autouse=True)
def packaged_channel_disabled(tmp_path_factory, monkeypatch):
    """Tests drive licensing through config/licensing.json; the shipped commercial
    switch would otherwise make the loader ignore that override everywhere."""
    from wechatrebot_manager.licensing import config as licensing_config

    channel = tmp_path_factory.mktemp("channel") / "licensing-channel.json"
    channel.write_text(json.dumps({"schema_version": 1, "enabled": False, "service_url": "", "public_keys": {}}),
                       encoding="utf-8")
    monkeypatch.setattr(licensing_config, "PACKAGED_CHANNEL", channel)


@pytest.fixture
def vendor_inputs():
    def create(repository):
        source = repository / "vendor"
        source.mkdir(parents=True)
        raw = b"value = 1\n"
        (source / "module.py").write_bytes(raw)
        (source / "manifest.json").write_text(json.dumps({"schema_version": 1, "files": [
            {"path": "module.py", "sha256": hashlib.sha256(raw).hexdigest()},
        ]}), encoding="utf-8")
        return source
    return create


@pytest.fixture(scope="session")
def patched_plugins():
    """Apply the product's actual plugin assembly to pinned sources, without a runtime."""
    from build_staging import _apply_plugin_patches, _copy_source_entry

    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    # Keep deep plugin template paths below Windows' legacy 260-character limit.
    with tempfile.TemporaryDirectory(prefix="p-", dir=artifacts) as directory:
        output = Path(directory)
        entries = json.loads((ROOT / "release/source-manifest.json").read_text("utf-8"))["entries"]
        for entry in entries:
            if not entry["target"].startswith("app/astrbot/data/plugins/"):
                continue
            base = ROOT if entry["origin"] == "commercial" else ROOT / "vendor"
            _copy_source_entry(base / entry["source"], output / entry["target"], entry)
        _apply_plugin_patches(ROOT, output)
        yield output / "app/astrbot/data/plugins"


@pytest.fixture
def patched_image(tmp_path):
    for name in ("main.py", "model_manager.py", "_conf_schema.json"):
        shutil.copy2(SOURCE / name, tmp_path / name)
    shutil.copytree(SOURCE / "pages", tmp_path / "pages")
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True)
    for name in (
        "astrbot-plugin-jiang_image.patch",
        "astrbot-plugin-jiang_image--network-errors.patch",
        "astrbot-plugin-jiang_image--z-local-proxy.patch",
        "astrbot-plugin-jiang_image--zz-model-experience.patch",
        "astrbot-plugin-jiang_image--zzz-stop-propagation.patch",
        "astrbot-plugin-jiang_image--zzzz-chat-scope.patch",
        "astrbot-plugin-jiang_image--zzzzz-complete-delivery.patch",
    ):
        result = subprocess.run(
            ["git", "apply", "--check", "--ignore-space-change", str(ROOT / "patches" / name)],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        subprocess.run(["git", "apply", "--ignore-space-change", str(ROOT / "patches" / name)],
                       cwd=tmp_path, check=True, capture_output=True)
    schema = json.loads((tmp_path / "_conf_schema.json").read_text("utf-8"))
    assert schema["use_system_proxy"]["type"] == "bool"
    assert schema["use_system_proxy"]["default"] is True
    assert schema["local_proxy_port"]["default"] == ""
    assert schema["secondary_enabled"]["default"] is False
    assert "tertiary_enabled" not in schema
    spec = importlib.util.spec_from_file_location("patched_image_model_manager", tmp_path / "model_manager.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return (tmp_path / "main.py").read_text("utf-8"), module
