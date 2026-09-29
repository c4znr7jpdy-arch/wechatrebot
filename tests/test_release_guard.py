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


def test_guard_recognizes_vendor_boundary_inside_distribution_payload(
    tmp_path: Path,
) -> None:
    vendor = (
        tmp_path
        / "payload"
        / "runtime"
        / "python"
        / "Lib"
        / "site-packages"
        / "dependency"
    )
    vendor.mkdir(parents=True)
    (vendor / "fixture.py").write_text(
        "PORT = 6199\nTOKEN = 'sk-examplefixture'\n", encoding="utf-8"
    )
    assert _load_guard()(tmp_path) == []


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


def test_guard_distinguishes_secret_literals_from_variable_assignment(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    source = app / "settings.py"
    source.write_text("api_key=self.api_key.strip()\n", encoding="utf-8")
    assert _load_guard()(tmp_path) == []

    source.write_text("api_key='private-production-value'\n", encoding="utf-8")
    findings = _load_guard()(tmp_path)
    assert {item["reason"] for item in findings} == {"assigned-secret"}


def test_guard_rejects_embedded_cookie_authorization_and_session_literals(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "accounts.py").write_text(
        "ACCOUNTS = [{\"cookie\": \"private-cookie-value-123456\", "
        "\"authorization\": \"Bearer private-token-value-123456\"}]\n"
        "session_token = \"private-session-value-123456\"\n",
        encoding="utf-8",
    )

    findings = _load_guard()(tmp_path)

    assert {item["reason"] for item in findings} == {"assigned-secret"}


def test_guard_rejects_embedded_api_token_literal(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "jiang_short_drama_config.json").write_text(
        '{"selection_store_api_token":"private-write-token-123456"}',
        encoding="utf-8",
    )

    findings = _load_guard()(tmp_path)

    assert {item["reason"] for item in findings} == {"assigned-secret"}


def test_guard_allows_empty_and_documented_credential_placeholders(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "accounts.py").write_text(
        "cookie = \"\"\n"
        "authorization = \"your-authorization-token\"\n"
        "session_token = \"${SESSION_TOKEN}\"\n",
        encoding="utf-8",
    )

    assert _load_guard()(tmp_path) == []


def test_guard_allows_environment_variable_name_mappings(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "settings.py").write_text(
        'MAPPING = {"content_api_key": "JIANG_CONTENT_API_KEY"}\n',
        encoding="utf-8",
    )

    assert _load_guard()(tmp_path) == []


def test_guard_rejects_developer_paths_in_payload_manifest(tmp_path: Path) -> None:
    (tmp_path / "release-manifest.json").write_text(
        '{"copied":[{"source":"E:\\\\workspace\\\\private\\\\file.py"}]}',
        encoding="utf-8",
    )

    findings = _load_guard()(tmp_path)

    assert {item["reason"] for item in findings} == {"developer-absolute-path"}


def test_guard_rejects_developer_paths_in_customer_documents(tmp_path: Path) -> None:
    document = tmp_path / "START-HERE.txt"
    document.write_text(
        "Internal build path: C:\\Users\\builder\\workspace\\release.zip\n",
        encoding="utf-8",
    )

    findings = _load_guard()(tmp_path)

    assert {item["reason"] for item in findings} == {"developer-path"}


def test_guard_allows_legacy_references_in_corresponding_source(tmp_path: Path) -> None:
    source = tmp_path / "legal" / "corresponding-source" / "historical.patch"
    source.parent.mkdir(parents=True)
    source.write_text("- port = 6199\n- NoveLoader\n", encoding="utf-8")

    assert _load_guard()(tmp_path) == []


def test_guard_allows_native_plugin_placeholders_and_rendered_session_label(tmp_path: Path) -> None:
    (tmp_path / "initializer.py").write_text(
        "# Non-ASCII path example: C:\\Users\\<中文名>\\...\n", encoding="utf-8"
    )
    (tmp_path / "recall.js").write_text(
        "html += '<span>Session: ' + esc(String(mem.metadata.session_id)) + '</span>';\n",
        encoding="utf-8",
    )
    assert _load_guard()(tmp_path) == []

    # The same source still rejects real paths and credentials, including keys
    # with application-specific prefixes and secrets assigned in HTML attributes.
    (tmp_path / "unsafe.js").write_text(
        "// C:\\Users\\builder\\release\n"
        'const config = {selection_store_api_token: "private-write-token-123456"};\n'
        '<input session="private-session-value-123456">\n',
        encoding="utf-8",
    )
    findings = _load_guard()(tmp_path)
    assert {item["reason"] for item in findings} == {"developer-path", "assigned-secret"}
