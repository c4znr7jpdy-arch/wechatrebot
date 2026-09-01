from pathlib import Path


def _load_guard():
    namespace: dict[str, object] = {}
    script = Path(__file__).parents[1] / "scripts" / "release_guard.py"
    exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), namespace)
    return namespace["scan_release"]


def test_guard_accepts_clean_release(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    findings = _load_guard()(tmp_path)
    assert findings == []


def test_guard_rejects_legacy_and_runtime_data(tmp_path: Path) -> None:
    legacy = tmp_path / "Py"
    legacy.mkdir()
    (legacy / "main.py").write_text("PORT = 6199\n", encoding="utf-8")
    (tmp_path / "chat.db").write_bytes(b"db")
    findings = _load_guard()(tmp_path)
    reasons = {item["reason"] for item in findings}
    assert "forbidden-directory" in reasons
    assert "legacy-port" in reasons
    assert "runtime-data" in reasons


def test_guard_rejects_build_metadata(tmp_path: Path) -> None:
    metadata = tmp_path / "package.egg-info"
    metadata.mkdir()
    (metadata / "PKG-INFO").write_text("metadata\n", encoding="utf-8")
    findings = _load_guard()(tmp_path)
    assert {item["reason"] for item in findings} == {"build-metadata"}


def test_guard_rejects_wechat_client_and_installer(tmp_path: Path) -> None:
    (tmp_path / "Weixin.exe").write_bytes(b"client")
    (tmp_path / "WeChatSetup.exe").write_bytes(b"installer")
    findings = _load_guard()(tmp_path)
    matching = [item for item in findings if item["reason"] == "wechat-distributable"]
    assert {item["path"] for item in matching} == {"WeChatSetup.exe", "Weixin.exe"}


def test_guard_allows_product_archive_name(tmp_path: Path) -> None:
    (tmp_path / "wechatrebot.zip").write_bytes(b"product")
    findings = _load_guard()(tmp_path)
    assert findings == []


def test_guard_allows_locked_vendor_examples_but_scans_patched_astrbot(
    tmp_path: Path,
) -> None:
    vendor = tmp_path / "runtime" / "python" / "Lib" / "site-packages"
    third_party = vendor / "some_dependency" / "tests"
    third_party.mkdir(parents=True)
    (third_party / "example.py").write_text(
        "PORT = 6199\nTOKEN = 'sk-examplefixture'\n",
        encoding="utf-8",
    )
    (vendor / "botocore" / "data" / "logs").mkdir(parents=True)
    (vendor / "botocore" / "data" / "logs" / "service.json").write_text(
        "{}\n", encoding="utf-8"
    )
    assert _load_guard()(tmp_path) == []

    astrbot = vendor / "astrbot" / "core"
    astrbot.mkdir(parents=True)
    (astrbot / "adapter.py").write_text("PORT = 6199\n", encoding="utf-8")
    findings = _load_guard()(tmp_path)
    assert {item["reason"] for item in findings} == {"legacy-port"}


def test_guard_rejects_logs_outside_locked_vendor_boundary(tmp_path: Path) -> None:
    logs = tmp_path / "app" / "logs"
    logs.mkdir(parents=True)
    (logs / "service.txt").write_text("runtime output\n", encoding="utf-8")
    findings = _load_guard()(tmp_path)
    assert {item["reason"] for item in findings} == {"forbidden-directory"}


def test_guard_rejects_legacy_loader_even_inside_vendor_boundary(tmp_path: Path) -> None:
    loader = (
        tmp_path
        / "runtime"
        / "python"
        / "Lib"
        / "site-packages"
        / "dependency"
        / "NoveLoader.dll"
    )
    loader.parent.mkdir(parents=True)
    loader.write_bytes(b"legacy")
    findings = _load_guard()(tmp_path)
    assert {item["reason"] for item in findings} == {"forbidden-filename"}
