from io import BytesIO
import json
import os

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
import pytest

from services.activation_service import app as app_module, cli as cli_module
from services.activation_service.app import create_app
from services.activation_service.cli import main
from services.activation_service.contact import CONTACT_FILENAME, parse_expiry
from wechatrebot_manager import json_io
from wechatrebot_manager.licensing.contact import (
    CONTACT_ENDPOINT, MAX_CONTACT_RESPONSE_BYTES, MAX_QR_BYTES, create_contact,
)
from wechatrebot_manager.licensing.protocol import LicenseError
from test_activation_service import env, timestamp  # Reuse the real ledger and clock fixture.


def image_bytes(format="PNG", color="black"):
    output = BytesIO()
    Image.new("RGB", (8, 8), color).save(output, format=format)
    return output.getvalue()


def write_record(path, *, group="微信群", expires=1792080000, image=None):
    record = create_contact(image or image_bytes(), group, expires)
    json_io.write_json_atomic(path, record)
    return record


def command(data_dir, image, *, group="微信群", expires="2026-10-16"):
    return ["--data-dir", str(data_dir), "set-contact", "--image", str(image),
            "--group-name", group, "--expires-at", expires]


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_path", [False, True])
async def test_contact_without_config_is_public_null_with_server_time(env, tmp_path, configured_path):
    path = tmp_path / CONTACT_FILENAME
    write_record(tmp_path / "unrelated.json", group="不应由请求参数读取的资料")
    if not configured_path:
        write_record(path)
    app = create_app(env.store, env.signer, "test-key", clock=lambda: env.now,
                     contact_path=path if configured_path else None)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(CONTACT_ENDPOINT, params={"path": str(tmp_path / "unrelated.json")})
        assert response.status == 200
        assert await response.json() == {"ok": True, "data": None, "server_time": env.now}
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    with env.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM nonces").fetchone()[0] == 0
    assert env.store.inspect() == {"licenses": 0, "codes": 0}


@pytest.mark.asyncio
async def test_contact_cli_replacement_clear_and_expiry_are_visible_without_restart(env, tmp_path, monkeypatch, capsys):
    directory = tmp_path / "private"
    directory.mkdir()
    retained = {"licensing-private.pem": b"existing private material", "activation.sqlite3": b"existing database"}
    for name, value in retained.items():
        (directory / name).write_bytes(value)
    first, second = tmp_path / "first.png", tmp_path / "second.jpg"
    first.write_bytes(image_bytes())
    second.write_bytes(image_bytes("JPEG", "white"))
    monkeypatch.setattr(cli_module.time, "time", lambda: env.now)
    assert main(command(directory, first, group="客户群一")) == 0
    assert "image_base64" not in capsys.readouterr().out
    app = create_app(env.store, env.signer, "test-key", clock=lambda: env.now,
                     contact_path=directory / CONTACT_FILENAME)
    async with TestClient(TestServer(app)) as client:
        initial = await (await client.get(CONTACT_ENDPOINT)).json()
        assert initial["data"]["group_name"] == "客户群一"
        assert initial["data"]["mime_type"] == "image/png"
        assert initial["data"]["expires_at"] == timestamp("2026-10-16T00:00:00")
        env.now += 10
        assert main(command(directory, second, group="客户群二", expires="2026-10-22T00:00:00+08:00")) == 0
        capsys.readouterr()
        replaced = await (await client.get(CONTACT_ENDPOINT)).json()
        assert replaced["server_time"] == env.now
        assert replaced["data"]["group_name"] == "客户群二" and replaced["data"]["mime_type"] == "image/jpeg"
        assert replaced["data"]["image_base64"] != initial["data"]["image_base64"]
        env.now = replaced["data"]["expires_at"]
        expired = await (await client.get(CONTACT_ENDPOINT)).json()
        assert expired["data"] == replaced["data"] and expired["server_time"] == env.now
        for _ in range(2):
            assert main(["--data-dir", str(directory), "clear-contact"]) == 0
            capsys.readouterr()
            cleared = await (await client.get(CONTACT_ENDPOINT)).json()
            assert cleared == {"ok": True, "data": None, "server_time": env.now}
    for name, value in retained.items():
        assert (directory / name).read_bytes() == value
    assert sorted(p.name for p in directory.iterdir()) == sorted(retained)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["json", "mime", "base64", "image_size", "record_size"])
async def test_contact_bad_record_returns_contact_error_without_leaking_content(env, tmp_path, damage, caplog):
    path = tmp_path / CONTACT_FILENAME
    record = write_record(path)
    if damage == "json":
        path.write_text('{"image_base64":"private-fixture-do-not-echo",', encoding="utf-8")
    elif damage == "record_size":
        path.write_bytes(b"x" * (MAX_CONTACT_RESPONSE_BYTES + 1))
    else:
        if damage == "mime":
            record["mime_type"] = "image/svg+xml"
        elif damage == "base64":
            record["image_base64"] = "private-fixture-do-not-echo"
        else:
            record["image_base64"] = "A" * (((MAX_QR_BYTES + 2) // 3) * 4 + 4)
        path.write_text(json.dumps(record), encoding="utf-8")
    app = create_app(env.store, env.signer, "test-key", clock=lambda: env.now, contact_path=path)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(CONTACT_ENDPOINT)
        assert response.status == 409
        body = await response.json()
        assert body["ok"] is False and body["error"]["code"] == "contact_invalid"
        assert "private-fixture-do-not-echo" not in json.dumps(body) + caplog.text


@pytest.mark.asyncio
async def test_contact_large_image_response_stays_within_public_read_limit(env, tmp_path):
    output = BytesIO()
    Image.frombytes("RGB", (400, 400), os.urandom(400 * 400 * 3)).save(output, format="PNG")
    image = output.getvalue()
    assert 400 * 1024 < len(image) < MAX_QR_BYTES
    path = tmp_path / CONTACT_FILENAME
    record = write_record(path, image=image)
    app = create_app(env.store, env.signer, "test-key", clock=lambda: env.now, contact_path=path)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(CONTACT_ENDPOINT)
        assert response.status == 200 and len(await response.read()) <= MAX_CONTACT_RESPONSE_BYTES
        assert (await response.json())["data"] == record


@pytest.mark.asyncio
async def test_contact_uses_rate_boundary_and_never_exposes_public_writes(env, tmp_path):
    path = tmp_path / CONTACT_FILENAME
    record = write_record(path)
    app = create_app(env.store, env.signer, "test-key", clock=lambda: env.now, contact_path=path)
    async with TestClient(TestServer(app)) as client:
        for method in ("post", "put", "delete"):
            response = await getattr(client, method)(CONTACT_ENDPOINT, json={"data": None})
            assert response.status == 405
        assert (await (await client.get(CONTACT_ENDPOINT)).json())["data"] == record
    # Another app instance shares the same durable socket-peer limit.
    app = create_app(env.store, env.signer, "test-key", clock=lambda: env.now, rate_requests=1, contact_path=path)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get(CONTACT_ENDPOINT)).status == 200
        response = await client.get(CONTACT_ENDPOINT)
        assert response.status == 429 and response.headers["Retry-After"] == "60"


@pytest.mark.parametrize("value", ["2026-10-16", "2026-10-16T00:00:00+08:00", "2026-10-15T16:00:00Z"])
def test_contact_expiry_date_is_explicitly_beijing_midnight(value):
    assert parse_expiry(value, timestamp("2026-10-09T00:00:00")) == timestamp("2026-10-16T00:00:00")


@pytest.mark.parametrize("value,code", [
    ("2026-10-16T00:00:00", "contact_invalid"), ("2026-02-30", "contact_invalid"),
    ("not-a-date", "contact_invalid"), ("2026-10-08", "contact_expired"),
    ("2026-10-09", "contact_expired"),
])
def test_contact_expiry_rejects_ambiguous_invalid_and_elapsed_values(value, code):
    with pytest.raises(LicenseError) as caught:
        parse_expiry(value, timestamp("2026-10-09T00:00:00"))
    assert caught.value.code == code


@pytest.mark.parametrize(
    "image", [b"", b"GIF89a", b"ordinary text", b"\x89PNG\r\n\x1a\n" + b"x" * MAX_QR_BYTES],
    ids=["empty", "gif", "text", "oversized"],
)
def test_contact_cli_invalid_image_preserves_previous_contact_and_creates_no_ledger(tmp_path, monkeypatch, capsys, image):
    directory = tmp_path / "private"
    path = directory / CONTACT_FILENAME
    write_record(path)
    before = path.read_bytes()
    source = tmp_path / "bad.png"
    source.write_bytes(image)
    monkeypatch.setattr(cli_module.time, "time", lambda: timestamp("2026-10-09T00:00:00"))
    assert main(command(directory, source)) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "contact_invalid"
    assert path.read_bytes() == before
    assert list(directory.iterdir()) == [path]


def test_contact_cli_atomic_write_failure_keeps_previous_record(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "private"
    path = directory / CONTACT_FILENAME
    write_record(path)
    before = path.read_bytes()
    image = tmp_path / "new.png"
    image.write_bytes(image_bytes())
    monkeypatch.setattr(cli_module.time, "time", lambda: timestamp("2026-10-09T00:00:00"))

    def fail_replace(*args):
        raise OSError("replacement failed")

    with monkeypatch.context() as patch:
        patch.setattr(json_io.os, "replace", fail_replace)
        assert main(command(directory, image)) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "local_setup_error"
    assert path.read_bytes() == before and list(directory.iterdir()) == [path]


def test_clear_contact_is_idempotent_without_creating_any_directory(tmp_path, capsys):
    directory = tmp_path / "not-created"
    assert main(["--data-dir", str(directory), "clear-contact"]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == {"contact_configured": False}
    assert not directory.exists()


def test_serve_wires_only_the_fixed_operator_contact_path(tmp_path, monkeypatch, capsys):
    args = ["--data-dir", str(tmp_path)]
    assert main([*args, "init"]) == 0
    capsys.readouterr()
    key = (tmp_path / "licensing-private.pem").read_bytes()
    seen = {}

    def capture_app(*args, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(app_module, "create_app", capture_app)
    monkeypatch.setattr(web, "run_app", lambda *args, **kwargs: None)
    assert main([*args, "serve"]) == 0
    assert seen["contact_path"] == tmp_path / CONTACT_FILENAME
    assert (tmp_path / "licensing-private.pem").read_bytes() == key
