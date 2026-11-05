"""Host deployment transactions against temporary files, never SSH or real nginx."""
from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

import activation_deploy_support as support


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def host_layout(tmp_path, monkeypatch):
    root = tmp_path / "host"
    release = root / "releases" / "aaaaaaaaaaaa"
    for source in support.SUPPORT_FILES:
        target = release / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, target)
    nginx = tmp_path / "nginx"
    nginx.mkdir()
    license = nginx / "wechatrebot-license.conf"
    license.write_text("# existing custom comment\nlocation ^~ /wechatrebot-license/ {\n    proxy_pass http://127.0.0.1:8091/;\n}\n")
    limits = nginx / "wechatrebot-license-limits.conf"
    limits.write_text("limit_req_zone $binary_remote_addr zone=wechatrebot_license:1m rate=2r/s;\n# preserve this\n")
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    (systemd / support.SERVICE).write_text("old unit\n")
    admin = tmp_path / "admin"
    admin.write_text("old command\n")
    backup = root / "deploy/activation-service/backup.py"
    backup.parent.mkdir(parents=True)
    backup.write_text("# old backup\n")
    monkeypatch.setattr(support, "SYSTEMD_DIR", systemd)
    monkeypatch.setattr(support, "ADMIN_COMMAND", admin)
    dump = f"# configuration file {license.as_posix()}:\n{license.read_text()}\n# configuration file {limits.as_posix()}:\n{limits.read_text()}"
    snapshot = root / "support-backups" / "123456789abcdef0"
    return SimpleNamespace(root=root, release=release, license=license, limits=limits, systemd=systemd,
                           admin=admin, backup=backup, dump=dump, snapshot=snapshot)


def test_host_support_apply_and_restore_preserve_existing_files(host_layout):
    host = host_layout
    original = {path: path.read_bytes() for path in (host.license, host.limits, host.admin, host.backup, host.systemd / support.SERVICE)}
    calls = []
    support.prepare(host.root, host.release, host.snapshot, command=lambda argv: host.dump)
    assert all(path.read_bytes() == old for path, old in original.items())
    support.apply(host.root, host.snapshot, command=calls.append)
    assert "127.0.0.1:8092:8092" in (host.systemd / support.SERVICE).read_text()
    assert "# existing custom comment" in host.license.read_text()
    assert "# preserve this" in host.limits.read_text()
    assert host.license.read_text().count("wechatrebot-admin.conf") == 1
    assert "zone=wechatrebot_admin_login:" in host.limits.read_text()
    assert "-it" in host.admin.read_text()
    assert calls == [["nginx", "-t"], ["systemctl", "daemon-reload"]]
    support.apply(host.root, host.snapshot, restore=True, command=calls.append)
    assert all(path.read_bytes() == old for path, old in original.items())
    assert not host.license.with_name("wechatrebot-admin.conf").exists()


def test_partial_apply_failure_can_restore_every_support_file(host_layout):
    host = host_layout
    support.prepare(host.root, host.release, host.snapshot, command=lambda argv: host.dump)
    original = host.license.read_bytes()

    def failed_nginx(argv):
        raise RuntimeError("nginx configuration invalid")

    with pytest.raises(RuntimeError, match="nginx"):
        support.apply(host.root, host.snapshot, command=failed_nginx)
    support.apply(host.root, host.snapshot, restore=True, command=lambda argv: "")
    assert host.license.read_bytes() == original
    assert (host.systemd / support.SERVICE).read_text() == "old unit\n"


def test_support_files_use_the_deployment_environment_and_container_options(host_layout):
    host = host_layout
    support.prepare(host.root, host.release, host.snapshot, command=lambda argv: host.dump,
                    env_file="/etc/custom-activation.env", container="custom-activation")
    support.apply(host.root, host.snapshot, command=lambda argv: "")
    unit = (host.systemd / support.SERVICE).read_text()
    assert "EnvironmentFile=/etc/custom-activation.env" in unit
    assert "--name custom-activation " in unit and "--time 30 custom-activation" in unit
    assert "custom-activation python" in host.admin.read_text()


def test_support_snapshot_can_capture_current_files_for_reversible_explicit_rollback(host_layout):
    host = host_layout
    support.prepare(host.root, host.release, host.snapshot, command=lambda argv: host.dump)
    support.apply(host.root, host.snapshot, command=lambda argv: "")
    current = host.admin.read_bytes()
    next_snapshot = host.snapshot.with_name("bbbbbbbbbbbbbbbb")
    support.capture(host.root, next_snapshot, host.snapshot)
    support.apply(host.root, host.snapshot, restore=True, command=lambda argv: "")
    assert host.admin.read_bytes() != current
    support.apply(host.root, next_snapshot, restore=True, command=lambda argv: "")
    assert host.admin.read_bytes() == current


@pytest.mark.parametrize("dump", ["", "# configuration file /etc/nginx/sites-enabled/site:\nserver { location ^~ /wechatrebot-license/ {} }"])
def test_missing_dedicated_nginx_includes_fail_before_writes(host_layout, dump):
    host = host_layout
    with pytest.raises(RuntimeError, match="uniquely"):
        support.prepare(host.root, host.release, host.snapshot, command=lambda argv: dump)
    assert not host.snapshot.exists()
    assert host.admin.read_text() == "old command\n"


def test_existing_admin_include_and_zones_are_not_duplicated(host_layout):
    host = host_layout
    admin_path = host.license.with_name("wechatrebot-admin.conf")
    host.license.write_text(host.license.read_text() + f'include "{admin_path.as_posix()}";\n')
    host.limits.write_text(host.limits.read_text() + "limit_req_zone $binary_remote_addr zone=wechatrebot_admin:1m rate=1r/s;\n")
    support.prepare(host.root, host.release, host.snapshot, command=lambda argv: host.dump)
    support.apply(host.root, host.snapshot, command=lambda argv: "")
    assert host.license.read_text().count("wechatrebot-admin.conf") == 1
    assert host.limits.read_text().count("zone=wechatrebot_admin:") == 1
    assert "zone=wechatrebot_admin:1m rate=10r/s" in host.limits.read_text()


@pytest.mark.parametrize("wildcard_first", [True, False])
def test_nginx_wildcard_overlap_is_rejected_regardless_of_dump_order(host_layout, wildcard_first):
    host = host_layout
    wildcard = f"# configuration file /etc/nginx/nginx.conf:\nhttp {{\n    include {host.license.parent.as_posix()}/*.conf;\n}}\n"
    dump = wildcard + host.dump if wildcard_first else host.dump + wildcard
    with pytest.raises(RuntimeError, match="wildcard"):
        support.prepare(host.root, host.release, host.snapshot, command=lambda argv: dump)
    assert not host.snapshot.exists()


@pytest.mark.parametrize("admin_enabled", [False, True])
def test_restored_admin_probe_depends_on_previous_unit(host_layout, monkeypatch, capsys, admin_enabled):
    host = host_layout
    if admin_enabled:
        (host.systemd / support.SERVICE).write_text("--publish 127.0.0.1:8092:8092\n")
    support.prepare(host.root, host.release, host.snapshot, command=lambda argv: host.dump)
    calls = []
    monkeypatch.setattr(support, "probe_admin", calls.append)
    support.probe_restored(host.root, host.snapshot, "https://example.invalid/wechatrebot-admin/login")
    assert len(calls) == int(admin_enabled)
    if not admin_enabled:
        assert "no admin listener" in capsys.readouterr().out


class Response(io.BytesIO):
    def __init__(self, status, body, headers):
        super().__init__(body.encode())
        self.status, self.headers = status, headers


@pytest.mark.parametrize("status,body", [(503, "后台未初始化"), (200, '<input name="password">'),
                                         (429, '登录已锁定 <input name="password">')])
def test_admin_probe_accepts_only_initialized_or_explicitly_uninitialized_login(monkeypatch, status, body, capsys):
    headers = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'self'"}
    monkeypatch.setattr(support.urllib.request, "urlopen", lambda *a, **kw: Response(status, body, headers))
    support.probe_admin()
    assert json.loads(capsys.readouterr().out)["initialized"] is (status in {200, 429})


@pytest.mark.parametrize("status,body,headers", [(200, "unrelated site", {}), (503, "unavailable", {}),
    (200, '<input name="password">', {"Cache-Control": "no-store"})])
def test_admin_probe_rejects_unrelated_pages_and_missing_security_headers(monkeypatch, status, body, headers):
    monkeypatch.setattr(support.urllib.request, "urlopen", lambda *a, **kw: Response(status, body, headers))
    with pytest.raises(RuntimeError, match="Admin"):
        support.probe_admin()
