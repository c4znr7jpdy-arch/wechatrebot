from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat

import pytest

from services.activation_service.admin.auth import (
    AUTH_FILE, TOKEN_FILE, AuthStore, initialize, set_github_token, totp, totp_counter,
)
from services.activation_service import cli
from wechatrebot_manager.licensing.protocol import LicenseError

PASSWORD = "test-only-long-admin-password"
NOW = 1_790_000_000


def test_rfc6238_sha1_vector_and_window():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp(secret, 59 // 30) == "287082"
    assert totp_counter(secret, "287082", 89) == 1
    assert totp_counter(secret, "287082", 90) is None
    assert totp_counter(secret, "２８７０８２", 59) is None


def test_credentials_are_private_hashes_and_init_cannot_overwrite(tmp_path):
    result = initialize(tmp_path, PASSWORD)
    raw = (tmp_path / AUTH_FILE).read_text("utf-8")
    assert PASSWORD not in raw and json.loads(raw)["iterations"] == 600_000
    assert result["secret"] in result["provisioning_uri"]
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / AUTH_FILE).stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        initialize(tmp_path, PASSWORD)
    assert (tmp_path / AUTH_FILE).read_text("utf-8") == raw
    with pytest.raises(LicenseError):
        initialize(tmp_path, "too-short", reset=True)
    assert (tmp_path / AUTH_FILE).read_text("utf-8") == raw


def test_lockout_expiry_session_expiry_logout_and_reset(tmp_path):
    secret = initialize(tmp_path, PASSWORD)["secret"]
    auth = AuthStore(tmp_path)
    for _ in range(4):
        assert auth.authenticate("wrong", totp(secret, NOW // 30), NOW).status == "invalid"
    assert auth.authenticate(PASSWORD, "000000", NOW).status == "locked"
    assert auth.locked_seconds(NOW) == 900
    assert auth.authenticate(PASSWORD, totp(secret, NOW // 30), NOW).status == "locked"
    now = NOW + 900
    assert auth.authenticate(PASSWORD, totp(secret, now // 30), now).status == "ok"
    token, _ = auth.new_session(now, authenticated=True, revision=auth.credentials()["revision"])
    assert auth.session(token, now + 43199)["authenticated"]
    assert auth.session(token, now + 43200) is None
    assert auth.authenticate(PASSWORD, totp(secret, now // 30), now).status == "invalid"
    assert auth.authenticate("", totp(secret, (now + 30) // 30), now + 30, step_up=True).status == "ok"
    token2, _ = auth.new_session(now, authenticated=True, revision=auth.credentials()["revision"])
    auth.logout(token2)
    assert auth.session(token2, now) is None
    token3, _ = auth.new_session(now, authenticated=True, revision=auth.credentials()["revision"])
    initialize(tmp_path, PASSWORD + "-reset", reset=True)
    assert auth.session(token3, now) is None


def test_concurrent_dynamic_code_cannot_authenticate_twice(tmp_path):
    secret = initialize(tmp_path, PASSWORD)["secret"]
    auth = AuthStore(tmp_path)
    code = totp(secret, NOW // 30)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: auth.authenticate(PASSWORD, code, NOW), range(2)))
    assert sorted(result.status for result in results) == ["invalid", "ok"]


def test_reset_between_authentication_and_session_creation_rejects_old_login(tmp_path):
    secret = initialize(tmp_path, PASSWORD)["secret"]
    auth = AuthStore(tmp_path)
    result = auth.authenticate(PASSWORD, totp(secret, NOW // 30), NOW)
    assert result.status == "ok"
    initialize(tmp_path, PASSWORD + "reset", reset=True)
    with pytest.raises(LicenseError, match="重置"):
        auth.new_session(NOW, authenticated=True, revision=result.revision)


def test_flash_is_single_use_and_deleted_from_state(tmp_path):
    initialize(tmp_path, PASSWORD)
    auth = AuthStore(tmp_path)
    token, _ = auth.new_session(NOW, authenticated=True, revision=auth.credentials()["revision"])
    auth.flash(token, "test-only-code-secret")
    assert auth.flash(token) == "test-only-code-secret"
    assert auth.flash(token) == ""
    assert b"test-only-code-secret" not in auth.path.read_bytes()


def test_token_file_does_not_accept_arbitrary_or_classic_tokens(tmp_path):
    with pytest.raises(LicenseError):
        set_github_token(tmp_path, "ghp_" + "a" * 40)
    token = "github_pat_" + "a" * 40
    set_github_token(tmp_path, token)
    assert json.loads((tmp_path / TOKEN_FILE).read_text())["token"] == token


def test_cli_initializes_and_prints_qr_only_locally(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: PASSWORD)
    assert cli.main(["--data-dir", str(tmp_path), "admin-init"]) == 0
    text = capsys.readouterr().out
    assert "手输密钥" in text and PASSWORD not in text and "█" in text
    old = (tmp_path / AUTH_FILE).read_bytes()
    assert cli.main(["--data-dir", str(tmp_path), "admin-init"]) == 1
    assert (tmp_path / AUTH_FILE).read_bytes() == old
    assert cli.main(["--data-dir", str(tmp_path), "admin-reset"]) == 0
    assert (tmp_path / AUTH_FILE).read_bytes() != old
