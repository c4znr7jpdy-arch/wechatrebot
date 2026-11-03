"""Developer one-click upgrade of the activation service, without touching the server."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import urllib.request

from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from services.activation_service.app import create_app
from services.activation_service.contact import CONTACT_FILENAME, parse_expiry
from services.activation_service.store import LicenseStore
from wechatrebot_manager import json_io
from wechatrebot_manager.licensing.contact import create_contact
import deploy_activation_service as tool

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "https://license.example.invalid/wechatrebot-license"
NONCE = "0123456789abcdef0123456789abcdef"
OLD_IMAGE = "wechatrebot-activation:acf59eb8e6c5"
ENV_TEXT = ("# WeChatReBot activation service\nACTIVATION_IMAGE=wechatrebot-activation:acf59eb8e6c5\n"
            "ACTIVATION_USER=10001:10001\n")
CONTACT = {"schema_version": 1, "group_name": "WXRebot", "expires_at": 1798646400, "mime_type": "image/png",
           "image_base64": "aGVsbG8="}
DATA_DIR = "/var/lib/wechatrebot-activation"


def plan_for(**overrides):
    options = {"service_url": SERVICE, "nonce": NONCE}
    options.update(overrides)
    return tool.build_plan(**options)


def new_env(identity):
    return ENV_TEXT.replace("acf59eb8e6c5", identity)


def kind(argv, settings):
    command = argv[-1]
    if argv[0] == "scp.exe":
        return "upload-env" if settings.env_upload_path in command else "upload-archive"
    if command == f"cat {settings.env_file}":
        return "read-env"
    if command == f"cat {settings.previous_image_file}":
        return "read-record"
    if "tar -xf" in command:
        return "unpack"
    if "docker build" in command:
        return "build"
    if f"systemctl start {tool.BACKUP_UNIT}" in command:
        return "backup"
    if command == f"ls -1 {settings.backup_dir}":
        return "list-backups"
    if settings.previous_image_file in command:
        return "record"
    if settings.env_upload_path in command:
        return "install-env"
    if f"systemctl restart {tool.SERVICE_UNIT}" in command:
        return "switch"
    if "docker inspect" in command:
        return "health"
    return "other:" + command[:40]


class FakeServer:
    """Interprets the generated ssh/scp/HTTP calls against an in-memory server state."""

    def __init__(self, settings, *, env_text=ENV_TEXT, record=None, after_switch=None, fail=None):
        self.settings = settings
        self.env = env_text
        self.record = record
        self.backups = ["backup-20261101T042000000000Z-aaaaaaaaaaaa"]
        self.calls, self.probes, self.uploads = [], [], {}
        self.switched, self.restarts = False, 0
        self.after_switch = after_switch or {}
        self.fail = fail or (lambda argv: None)
        self.backup_adds_snapshot = True

    def run(self, argv):
        self.calls.append(argv)
        self.fail(argv)
        settings = self.settings
        if argv[0] == "scp.exe":
            local, remote = argv[-2], argv[-1].split(":", 1)[1]
            self.uploads[remote] = Path(local).read_bytes()
            return ""
        assert argv[:4] == ["ssh.exe", "-o", "BatchMode=yes", settings.host], argv
        command = argv[-1]
        name = kind(argv, settings)
        if name == "read-env":
            return self.env
        if name == "read-record":
            if self.record is None:
                raise RuntimeError("命令失败（退出码 1）：ssh.exe\ncat: No such file or directory")
            return self.record + "\n"
        if name == "health":
            return "running healthy\n"
        if name == "list-backups":
            return "\n".join(self.backups) + "\n"
        if name == "backup":
            if self.backup_adds_snapshot:
                self.backups.append("backup-20261103T100000000000Z-bbbbbbbbbbbb")
            return "\n".join(self.backups) + "\n"
        if name == "record":
            self.record = re.search(r"printf '%s\\n' (\S+) >", command).group(1)
            return ""
        if name == "install-env":
            self.env = self.uploads[settings.env_upload_path].decode("utf-8")
            return self.env
        if name == "switch":
            self.switched, self.restarts = True, self.restarts + 1
            return ""
        return ""

    def probe(self, method, url, body=None):
        assert url.startswith(SERVICE + "/"), url
        path = url[len(SERVICE):]
        self.probes.append((method, path, body, self.switched))
        key = (method, path)
        if self.switched and key in self.after_switch:
            return self.after_switch[key]
        if key == ("GET", "/health"):
            return 200, {"ok": True, "data": {"status": "ok", "schema_version": 1}}
        if key == ("GET", "/v1/contact"):
            return 200, {"ok": True, "data": dict(CONTACT), "server_time": 1793668585}
        if key == ("POST", "/v1/trial-status"):
            if self.switched:
                return 409, {"ok": False, "error": {"code": "invalid_data", "message": "授权数据格式无效。"}}
            return 404, {"ok": False, "error": {"code": "invalid_endpoint", "message": "请求方法或地址无效。"}}
        if key == ("POST", "/v1/preview"):
            return 401, {"ok": False, "error": {"code": "invalid_request", "message": "设备请求校验失败。"}}
        raise AssertionError(f"unexpected probe {method} {path}")

    def kinds(self):
        return [kind(argv, self.settings) for argv in self.calls]


def independent_copy_sources(text):
    """A second, deliberately naive reading of the Dockerfile COPY lines."""
    joined = re.sub(r"\\\r?\n", " ", text)
    files = set()
    for line in joined.splitlines():
        parts = line.split()
        if not parts or parts[0].upper() != "COPY":
            continue
        for pattern in [p for p in parts[1:-1] if not p.startswith("--")]:
            matches = sorted(ROOT.glob(pattern))
            assert matches, pattern
            files.update(m.relative_to(ROOT).as_posix() for m in matches if m.is_file())
    return files


# ---------------------------------------------------------------- payload identity

def test_payload_files_match_dockerfile_copy_lines():
    dockerfile = ROOT / tool.DOCKERFILE
    payload = tool.build_payload()
    assert set(payload.files) == independent_copy_sources(dockerfile.read_text("utf-8")) | {tool.DOCKERFILE}
    assert list(payload.files) == sorted(payload.files)


def test_payload_contains_exactly_the_image_inputs():
    files = set(tool.build_payload().files)
    service = {p.relative_to(ROOT).as_posix() for p in (ROOT / "services/activation_service").glob("*.py")}
    assert service <= files and "services/activation_service/requirements.txt" in files
    assert {"src/wechatrebot_manager/__init__.py", "src/wechatrebot_manager/json_io.py",
            "src/wechatrebot_manager/licensing/__init__.py", "src/wechatrebot_manager/licensing/protocol.py",
            "src/wechatrebot_manager/licensing/contact.py", "deploy/activation-service/Dockerfile"} <= files
    assert not any(p.startswith(("vendor/", "runtime/", "config/", "data/")) for p in files)
    assert not any("README" in p or p.endswith(".service") or p.endswith("backup.py") for p in files)


def copy_inputs(destination):
    for relative in tool.build_payload().files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return destination


def test_identity_is_stable_across_paths_and_timestamps_but_tracks_content(tmp_path):
    first, second = copy_inputs(tmp_path / "a"), copy_inputs(tmp_path / "b")
    for path in second.rglob("*"):
        if path.is_file():
            path.touch()
    reference = tool.build_payload()
    assert re.fullmatch(r"[0-9a-f]{12}", reference.identity)
    assert reference.identity == hashlib.sha256(reference.archive).hexdigest()[:12] == reference.sha256[:12]
    assert tool.build_payload(first).identity == tool.build_payload(second).identity == reference.identity
    assert tool.build_payload(first).archive == reference.archive
    (second / "services/activation_service/app.py").write_bytes(b"# changed\n")
    assert tool.build_payload(second).identity != reference.identity
    with tarfile.open(fileobj=io.BytesIO(reference.archive), mode="r:") as archive:
        members = archive.getmembers()
    assert [m.name for m in members] == list(reference.files)
    assert all(m.isfile() and m.mtime == 0 and m.uid == m.gid == 0 and m.mode == 0o644 for m in members)
    assert all(m.uname == "" and m.gname == "" for m in members)
    assert reference.size == len(reference.archive)


def test_identity_ignores_checkout_line_endings(tmp_path):
    """A Windows CRLF checkout must build the same image as a Linux LF checkout."""
    crlf, lf = copy_inputs(tmp_path / "crlf"), copy_inputs(tmp_path / "lf")
    for base in (crlf, lf):
        for path in base.rglob("*"):
            if path.is_file():
                normalized = path.read_bytes().replace(b"\r\n", b"\n")
                path.write_bytes(normalized.replace(b"\n", b"\r\n") if base is crlf else normalized)
    assert tool.build_payload(crlf).identity == tool.build_payload(lf).identity == tool.build_payload().identity
    with tarfile.open(fileobj=io.BytesIO(tool.build_payload(crlf).archive), mode="r:") as archive:
        dockerfile = archive.extractfile(tool.DOCKERFILE).read()
    assert b"\r" not in dockerfile and dockerfile == (lf / tool.DOCKERFILE).read_bytes()


def test_copy_parser_handles_flags_continuations_and_rejects_unsupported_forms(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/a.py").write_text("a", encoding="utf-8")
    (tmp_path / "pkg/b.py").write_text("b", encoding="utf-8")
    (tmp_path / "req.txt").write_text("x", encoding="utf-8")
    text = ("FROM python\n# COPY commented out\nCOPY --chown=1:1 req.txt req.txt\n"
            "copy pkg/*.py \\\n    pkg/\nRUN echo hi\n")
    assert tool.copy_sources(text) == ["req.txt", "pkg/*.py"]
    assert tool.resolve_sources(tmp_path, tool.copy_sources(text)) == ["pkg/a.py", "pkg/b.py", "req.txt"]
    for bad in ('COPY ["a", "b"]\n', "COPY onlydest\n", "COPY --from=builder /a /b\n"):
        with pytest.raises(ValueError):
            tool.copy_sources(bad)
    for pattern in (["missing.txt"], ["pkg"], ["/etc/passwd"], ["../x"], ["pkg/*.json"]):
        with pytest.raises(ValueError):
            tool.resolve_sources(tmp_path, pattern)


# ---------------------------------------------------------------- plan mode

def test_plan_mode_prints_every_step_without_commands_or_network(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("plan mode must not run commands"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("plan mode must not fetch"))
    plan = plan_for()
    assert plan.image == f"wechatrebot-activation:{plan.identity}"
    assert plan.release_dir == f"{tool.DEFAULT_REMOTE_ROOT}/releases/{plan.identity}"
    assert plan.health_url == SERVICE + "/health" and plan.contact_url == SERVICE + "/v1/contact"
    assert tool.main(["--service-url", SERVICE, "--plan"]) == 0
    output = capsys.readouterr().out
    assert "计划模式" in output and plan.identity in output and plan.image in output
    for expected in ("scp.exe", "tar -xf", "docker build", f"systemctl start {tool.BACKUP_UNIT}",
                     tool.DEFAULT_ENV_FILE, f"systemctl restart {tool.SERVICE_UNIT}", "docker inspect",
                     "/health", "/v1/trial-status", "/v1/preview", "/v1/contact", "409 invalid_data",
                     "401 invalid_request", "回滚", "deploy/activation-service/Dockerfile",
                     "services/activation_service/app.py"):
        assert expected in output, expected
    assert DATA_DIR not in output
    assert tool.main(["--service-url", SERVICE, "--plan", "--rollback"]) == 0
    output = capsys.readouterr().out
    assert "计划模式" in output and plan.previous_image_file in output and "docker build" not in output


# ---------------------------------------------------------------- upgrade execution

def test_execute_runs_steps_in_order_and_verifies_from_public_entry():
    plan = plan_for()
    server = FakeServer(plan)
    summary = tool.execute(plan, run=server.run, probe=server.probe, log=lambda *_: None, sleep=lambda _: None)
    assert server.kinds() == ["read-env", "upload-archive", "unpack", "build", "list-backups", "backup",
                              "record", "upload-env", "install-env", "switch", "health"]
    assert [(m, p, switched) for m, p, _, switched in server.probes] == [
        ("GET", "/health", False), ("GET", "/v1/contact", False), ("POST", "/v1/trial-status", False),
        ("GET", "/health", True), ("POST", "/v1/trial-status", True), ("POST", "/v1/preview", True),
        ("GET", "/v1/contact", True),
    ]
    bodies = {(m, p): body for m, p, body, switched in server.probes if switched}
    assert bodies[("POST", "/v1/trial-status")] == b"" and bodies[("POST", "/v1/preview")] == b"{}"
    archive = server.uploads[plan.archive_upload]
    assert hashlib.sha256(archive).hexdigest() == plan.payload.sha256 and plan.archive_upload.endswith(".tar")
    unpack = server.calls[2][-1]
    assert plan.payload.sha256 in unpack and str(plan.payload.size) in unpack and "mv -T" in unpack
    build = server.calls[3][-1]
    assert f"docker build -t {plan.image} -f {plan.release_dir}/{tool.DOCKERFILE} {plan.release_dir}" in build
    assert "docker image inspect" in build
    assert server.env == new_env(plan.identity) and server.record == OLD_IMAGE
    assert f"ln -sfn releases/{plan.identity}" in server.calls[9][-1]
    assert summary == {
        "ok": True, "identity": plan.identity, "image": plan.image, "previous_image": OLD_IMAGE,
        "backup_snapshot": "backup-20261103T100000000000Z-bbbbbbbbbbbb", "verified": True, "rolled_back": False,
        "trial_status_before": "404 invalid_endpoint", "trial_status_after": "409 invalid_data",
    }


def test_verification_failure_rolls_back_to_previous_image_and_reports():
    plan = plan_for()
    server = FakeServer(plan, after_switch={
        ("POST", "/v1/trial-status"): (404, {"ok": False, "error": {"code": "invalid_endpoint", "message": "x"}}),
    })
    with pytest.raises(RuntimeError) as failure:
        tool.execute(plan, run=server.run, probe=server.probe, log=lambda *_: None, sleep=lambda _: None)
    message = str(failure.value)
    assert "已回滚" in message and OLD_IMAGE in message and "trial-status" in message and "invalid_endpoint" in message
    assert server.kinds()[-6:] == ["switch", "health", "upload-env", "install-env", "switch", "health"]
    assert server.env == ENV_TEXT and server.record == OLD_IMAGE and server.restarts == 2
    assert server.probes[-1][:2] == ("GET", "/health")
    assert "ln -sfn releases/acf59eb8e6c5" in server.calls[-2][-1]


def test_contact_mismatch_after_switch_rolls_back():
    plan = plan_for()
    changed = dict(CONTACT, group_name="其他群")
    server = FakeServer(plan, after_switch={("GET", "/v1/contact"): (200, {"ok": True, "data": changed, "server_time": 1})})
    with pytest.raises(RuntimeError, match="联系资料"):
        tool.execute(plan, run=server.run, probe=server.probe, log=lambda *_: None, sleep=lambda _: None)
    assert server.env == ENV_TEXT and server.kinds()[-1] == "health"


def test_rollback_failure_demands_manual_intervention():
    plan = plan_for()
    restarts = []

    def fail(argv):
        if f"systemctl restart {tool.SERVICE_UNIT}" in argv[-1]:
            restarts.append(argv)
            if len(restarts) == 2:
                raise RuntimeError("命令失败（退出码 1）：ssh.exe\nJob failed")

    server = FakeServer(plan, fail=fail, after_switch={
        ("GET", "/health"): (502, {"ok": False, "error": {"code": "bad_gateway", "message": "x"}}),
    })
    with pytest.raises(RuntimeError) as failure:
        tool.execute(plan, run=server.run, probe=server.probe, log=lambda *_: None, sleep=lambda _: None,
                     attempts=2)
    message = str(failure.value)
    assert "人工" in message and "回滚失败" in message and "Job failed" in message and "502" in message


def test_backup_without_new_snapshot_stops_before_switching():
    plan = plan_for()
    server = FakeServer(plan)
    server.backup_adds_snapshot = False
    with pytest.raises(RuntimeError, match="备份"):
        tool.execute(plan, run=server.run, probe=server.probe, log=lambda *_: None, sleep=lambda _: None)
    assert server.kinds()[-1] == "backup" and server.env == ENV_TEXT and not server.switched


def test_server_already_on_target_image_refuses_without_uploading():
    plan = plan_for()
    server = FakeServer(plan, env_text=new_env(plan.identity))
    with pytest.raises(RuntimeError, match="已是"):
        tool.execute(plan, run=server.run, probe=server.probe, log=lambda *_: None, sleep=lambda _: None)
    assert server.kinds() == ["read-env"] and not server.uploads


def test_preflight_failure_runs_no_server_command():
    plan = plan_for()
    server = FakeServer(plan)
    broken = lambda method, url, body=None: (503, {"ok": False, "error": {"code": "service_unavailable", "message": "x"}})
    with pytest.raises(RuntimeError, match="/health"):
        tool.execute(plan, run=server.run, probe=broken, log=lambda *_: None, sleep=lambda _: None, attempts=1)
    assert server.calls == []


def test_container_health_wait_retries_then_fails():
    plan = plan_for()
    states = iter(["running starting\n", "running starting\n", "running healthy\n"])
    assert tool.wait_for_container(plan, run=lambda argv: next(states), sleep=lambda _: None, attempts=3, delay=0)
    with pytest.raises(RuntimeError, match="healthy"):
        tool.wait_for_container(plan, run=lambda argv: "running unhealthy\n", sleep=lambda _: None, attempts=2, delay=0)


# ---------------------------------------------------------------- explicit rollback

def test_explicit_rollback_restores_recorded_image():
    plan = plan_for()
    server = FakeServer(plan, env_text=new_env(plan.identity), record=OLD_IMAGE)
    summary = tool.execute_rollback(plan, run=server.run, probe=server.probe, log=lambda *_: None, sleep=lambda _: None)
    assert server.kinds() == ["read-record", "read-env", "record", "upload-env", "install-env", "switch", "health"]
    assert [(m, p) for m, p, _, _ in server.probes] == [("GET", "/health"), ("GET", "/v1/contact")]
    assert server.env == ENV_TEXT and server.record == plan.image
    assert summary == {"ok": True, "changed": True, "image": OLD_IMAGE, "replaced_image": plan.image, "verified": True}


def test_explicit_rollback_is_a_noop_when_already_on_recorded_image():
    plan = plan_for()
    server = FakeServer(plan, record=OLD_IMAGE)
    summary = tool.execute_rollback(plan, run=server.run, probe=server.probe, log=lambda *_: None, sleep=lambda _: None)
    assert server.kinds() == ["read-record", "read-env"] and server.probes == []
    assert summary == {"ok": True, "changed": False, "image": OLD_IMAGE, "replaced_image": OLD_IMAGE, "verified": False}


def test_explicit_rollback_without_record_explains():
    plan = plan_for()
    server = FakeServer(plan, record=None)
    with pytest.raises(RuntimeError, match="回滚目标"):
        tool.execute_rollback(plan, run=server.run, probe=server.probe, log=lambda *_: None, sleep=lambda _: None)


# ---------------------------------------------------------------- boundaries

@pytest.mark.parametrize("options", [
    {"host": "root@host;rm"}, {"host": ""}, {"remote_root": "relative/dir"}, {"remote_root": "/"},
    {"remote_root": "/opt/x/"}, {"env_file": "etc/env"}, {"env_file": "/etc/env file"},
    {"backup_dir": "/var/backups/x/"}, {"image_name": "Bad Name"}, {"image_name": "wechatrebot-activation:tag"},
    {"image_name": "$(x)"}, {"container": "a b"}, {"service_url": "http://plain.example.invalid"},
    {"service_url": "https://x.example.invalid/path?x=1"}, {"nonce": "not-hex"},
])
def test_rejects_unsafe_parameters(options):
    with pytest.raises(ValueError):
        plan_for(**options)


def test_env_rewrite_preserves_other_lines_and_rejects_ambiguity():
    assert tool.env_image(ENV_TEXT) == OLD_IMAGE
    assert tool.env_image('ACTIVATION_IMAGE="wechatrebot-activation:abc"\n') == "wechatrebot-activation:abc"
    rewritten = tool.rewrite_env(ENV_TEXT, "wechatrebot-activation:0123456789ab")
    assert rewritten == ENV_TEXT.replace("acf59eb8e6c5", "0123456789ab")
    assert tool.env_image(rewritten) == "wechatrebot-activation:0123456789ab"
    for text in ("ACTIVATION_USER=1\n", ENV_TEXT + "ACTIVATION_IMAGE=other\n", "ACTIVATION_IMAGE=bad image\n"):
        with pytest.raises(ValueError):
            tool.env_image(text)
    with pytest.raises(ValueError):
        tool.rewrite_env(ENV_TEXT, "bad image")
    for bad in ("", "-leading", "a;b", "a b"):
        with pytest.raises(ValueError):
            tool.safe_image(bad)


def test_commands_never_touch_data_private_key_or_containers_directly():
    plan = plan_for()
    server = FakeServer(plan)
    tool.execute(plan, run=server.run, probe=server.probe, log=lambda *_: None, sleep=lambda _: None)
    text = "\n".join(subprocess.list2cmdline(argv) for argv in server.calls) + tool.describe(plan)
    for forbidden in (DATA_DIR, "licensing-private", "activation.sqlite3", "wechat-contact.json", "docker exec",
                      "docker rm", "docker rmi", "rm -r", "generate-key", "/deploy/activation-service/backup.py"):
        assert forbidden not in text, forbidden
    assert all(argv[0] in {"ssh.exe", "scp.exe"} for argv in server.calls)
    assert all("BatchMode=yes" in argv for argv in server.calls)


def test_main_reports_failures_as_exit_code_one(capsys):
    assert tool.main(["--service-url", "http://plain.example.invalid"]) == 1
    assert "升级未完成" in capsys.readouterr().out


# ---------------------------------------------------------------- probe expectations against the real service

@pytest.mark.asyncio
async def test_probe_expectations_match_the_real_service(tmp_path):
    contact = tmp_path / CONTACT_FILENAME
    image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    json_io.write_json_atomic(contact, create_contact(image, "WXRebot", parse_expiry("2026-12-31", 1793668585)))
    app = create_app(LicenseStore(tmp_path / "activation.sqlite3"), Ed25519PrivateKey.generate(), "test-key",
                     contact_path=contact)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        async def probe(method, url, body=None):
            response = await client.request(method, url, data=body)
            return response.status, await response.json()

        for label, method, path, body, status, code in tool.UPGRADE_PROBES:
            observed_status, payload = await probe(method, path, body)
            assert (observed_status, payload["error"]["code"]) == (status, code), label
        status, payload = await probe("GET", "/health")
        assert status == 200 and tool.check_health(status, payload) is None
        status, payload = await probe("GET", "/v1/contact")
        assert status == 200 and tool.contact_data(status, payload)["group_name"] == "WXRebot"
        with pytest.raises(RuntimeError):
            tool.check_health(200, {"ok": True, "data": {"status": "degraded"}})
        with pytest.raises(RuntimeError):
            tool.contact_data(404, {"ok": False, "error": {"code": "invalid_endpoint", "message": "x"}})
    finally:
        await client.close()
