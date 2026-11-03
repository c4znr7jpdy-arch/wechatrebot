"""Developer one-click WeChat group QR replacement, without touching the server."""
import base64
import hashlib
import json
import subprocess

import pytest

from services.activation_service.contact import parse_expiry
from wechatrebot_manager.licensing.contact import MAX_QR_BYTES
from wechatrebot_manager.licensing.protocol import LicenseError
import update_contact_qrcode as tool

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
NOW = 1793668585  # 2026-11-03 09:16:25 +08:00
SERVICE = "https://license.example.invalid/wechatrebot-license"


def png_file(tmp_path, data=PNG, name="group.png"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def plan_for(tmp_path, **overrides):
    options = {"group_name": "微信群 测试", "expires_at": "2026-12-31", "service_url": SERVICE, "now": NOW}
    options.update(overrides)
    return tool.build_plan(png_file(tmp_path), **options)


def record_for(plan, **overrides):
    record = {"schema_version": 1, "group_name": plan.group_name, "expires_at": plan.expires_at,
              "mime_type": plan.mime_type, "image_base64": base64.b64encode(plan.image).decode("ascii")}
    record.update(overrides)
    return {"ok": True, "data": record, "server_time": NOW}


def test_plan_validates_locally_and_prints_steps_without_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("plan mode must not run commands"))
    monkeypatch.setattr(tool, "fetch_contact", lambda url: pytest.fail("plan mode must not fetch"))
    plan = plan_for(tmp_path)
    assert plan.sha256 == hashlib.sha256(PNG).hexdigest()
    assert plan.mime_type == "image/png" and plan.group_name == "微信群 测试"
    assert plan.expires_at == parse_expiry("2026-12-31", NOW)
    assert plan.remote_name == f"wechat-contact-upload-{plan.sha256[:12]}.png"
    assert plan.remote_path == f"{tool.DEFAULT_REMOTE_DATA_DIR}/{plan.remote_name}"
    assert plan.contact_url == SERVICE + "/v1/contact"
    upload, permission, replace, cleanup = (argv for _, argv in plan.steps())
    assert upload[0] == "scp.exe" and upload[-1] == f"{tool.DEFAULT_HOST}:{plan.remote_path}"
    assert permission[:4] == ["ssh.exe", "-o", "BatchMode=yes", tool.DEFAULT_HOST]
    assert f"chown --reference={tool.DEFAULT_REMOTE_DATA_DIR} {plan.remote_path}" in permission[-1]
    assert f"{tool.DEFAULT_ADMIN_COMMAND} set-contact --image /data/{plan.remote_name}" in replace[-1]
    assert "--expires-at 2026-12-31" in replace[-1]
    encoded = base64.b64encode("微信群 测试".encode("utf-8")).decode("ascii")
    assert f'--group-name "$(printf %s {encoded} | base64 -d)"' in replace[-1]
    assert "微信群" not in replace[-1]
    assert cleanup[-1] == f"rm -f {plan.remote_path}"

    code = tool.main(["--image", str(tmp_path / "group.png"), "--group-name", "微信群 测试",
                      "--expires-at", "2026-12-31", "--service-url", SERVICE, "--plan"])
    output = capsys.readouterr().out
    assert code == 0
    assert "计划模式" in output and "微信群 测试" in output and "2026-12-31" in output
    assert "scp.exe" in output and "set-contact" in output


def test_jpeg_uses_jpg_extension(tmp_path):
    plan = tool.build_plan(png_file(tmp_path, JPEG, "group.jpg"), expires_at="2026-12-31",
                           service_url=SERVICE, now=NOW)
    assert plan.mime_type == "image/jpeg" and plan.remote_name.endswith(".jpg")
    assert plan.group_name == tool.DEFAULT_GROUP_NAME


def test_default_expiry_is_seven_days_later_at_beijing_midnight():
    text = tool.default_expiry(NOW)
    assert text == "2026-11-10"
    expires = parse_expiry(text, NOW)
    assert expires == 1794240000  # 2026-11-10T00:00:00+08:00
    assert NOW < expires < NOW + 7 * 86400


@pytest.mark.parametrize("image, code", [
    pytest.param(b"GIF89a" + b"\x00" * 32, "contact_invalid", id="gif"),
    pytest.param(PNG + b"\x00" * MAX_QR_BYTES, "contact_invalid", id="oversize"),
])
def test_rejects_wrong_or_oversized_images(tmp_path, image, code):
    with pytest.raises(LicenseError) as failure:
        tool.build_plan(png_file(tmp_path, image), expires_at="2026-12-31", service_url=SERVICE, now=NOW)
    assert failure.value.code == code


def test_rejects_past_expiry_missing_file_and_unsafe_remote_inputs(tmp_path):
    with pytest.raises(LicenseError) as failure:
        plan_for(tmp_path, expires_at="2020-01-01")
    assert failure.value.code == "contact_expired"
    with pytest.raises(ValueError):
        tool.build_plan(tmp_path / "missing.png", expires_at="2026-12-31", service_url=SERVICE, now=NOW)
    for options in ({"host": "root@host;rm"}, {"remote_data_dir": "relative/dir"}, {"remote_data_dir": "/"},
                    {"admin_command": "admin $(x)"}, {"service_url": "http://plain.example.invalid"}):
        with pytest.raises(ValueError):
            plan_for(tmp_path, **options)


def test_execute_runs_steps_in_order_cleans_up_and_verifies(tmp_path):
    plan = plan_for(tmp_path)
    calls = []

    def run(argv):
        calls.append(argv)
        if "set-contact" in argv[-1]:
            return json.dumps({"ok": True, "data": {"contact_configured": True, "mime_type": "image/png"}})
        return ""

    fetched = []

    def fetch(url):
        fetched.append(url)
        return record_for(plan)

    summary = tool.execute(plan, run=run, fetch=fetch, log=lambda *_: None, sleep=lambda _: None)
    assert [argv[0] for argv in calls] == ["scp.exe", "ssh.exe", "ssh.exe", "ssh.exe"]
    assert "chown" in calls[1][-1] and "set-contact" in calls[2][-1] and calls[3][-1].startswith("rm -f ")
    assert fetched == [plan.contact_url]
    assert summary["ok"] and summary["verified"] and summary["image_sha256"] == plan.sha256
    assert summary["remaining_seconds"] == plan.expires_at - NOW
    assert summary["expires_text"] == "2026-12-31 00:00:00"


def test_server_rejection_stops_before_verification_but_still_cleans_up(tmp_path):
    plan = plan_for(tmp_path)
    calls = []

    def run(argv):
        calls.append(argv)
        if "set-contact" in argv[-1]:
            return json.dumps({"ok": False, "error": {"code": "contact_expired", "message": "二维码有效期必须晚于当前服务器时间。"}})
        return ""

    with pytest.raises(RuntimeError, match="服务器拒绝更新二维码：二维码有效期必须晚于当前服务器时间。"):
        tool.execute(plan, run=run, fetch=lambda url: pytest.fail("must not verify"), log=lambda *_: None)
    assert len(calls) == 4 and calls[-1][-1].startswith("rm -f ")


def test_upload_failure_runs_nothing_else(tmp_path):
    plan = plan_for(tmp_path)
    calls = []

    def run(argv):
        calls.append(argv)
        raise RuntimeError("命令失败（退出码 255）：scp.exe")

    with pytest.raises(RuntimeError, match="scp.exe"):
        tool.execute(plan, run=run, fetch=lambda url: pytest.fail("must not verify"), log=lambda *_: None)
    assert len(calls) == 1


@pytest.mark.parametrize("override, problem", [
    ({"group_name": "另一个群"}, "群名称"),
    ({"expires_at": NOW + 10}, "有效期"),
    ({"image_base64": base64.b64encode(JPEG).decode("ascii")}, "图片内容"),
])
def test_verification_detects_mismatched_public_record(tmp_path, override, problem):
    plan = plan_for(tmp_path)
    with pytest.raises(RuntimeError, match=problem):
        tool.verify_record(plan, record_for(plan, **override))


def test_verification_rejects_missing_record_or_expired_server_time(tmp_path):
    plan = plan_for(tmp_path)
    with pytest.raises(RuntimeError, match="没有返回新资料"):
        tool.verify_record(plan, {"ok": True, "data": None, "server_time": NOW})
    payload = record_for(plan)
    payload["server_time"] = plan.expires_at
    with pytest.raises(RuntimeError, match="已超过本次设置的有效期"):
        tool.verify_record(plan, payload)


def test_verify_retries_then_reports_last_failure(tmp_path):
    plan = plan_for(tmp_path)
    attempts = []
    responses = [OSError("connection reset"), record_for(plan)]

    def fetch(url):
        attempts.append(url)
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    assert tool.verify(plan, fetch, attempts=3, delay=0, sleep=lambda _: None)["remaining_seconds"] > 0
    assert len(attempts) == 2
    with pytest.raises(RuntimeError, match="公网验证失败"):
        tool.verify(plan, lambda url: (_ for _ in ()).throw(OSError("down")), attempts=2, delay=0, sleep=lambda _: None)


def test_main_reports_failures_as_exit_code_one(tmp_path, capsys):
    assert tool.main(["--image", str(tmp_path / "missing.png"), "--plan"]) == 1
    assert "二维码未更新" in capsys.readouterr().out
