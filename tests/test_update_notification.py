from __future__ import annotations
from webui_source import manager_script

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]


def test_sidebar_version_status_follows_update_checks() -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required to exercise the update controller")
    script = (ROOT / "src/wechatrebot_manager/app_webui/updates.js").read_text("utf-8")
    harness = r'''
const assert = require('node:assert/strict');
const nodes = new Map();
const $ = selector => {
  if (!nodes.has(selector)) nodes.set(selector, {
    hidden: true, textContent: '', className: '', dataset: {},
    classList: {toggle() {}}, setAttribute(name, value) {this[name] = value;},
    addEventListener() {},
  });
  return nodes.get(selector);
};
$('#versionText').textContent = 'v1.0.15';
const state = {bootstrap: {update: {enabled: true}}};
let response = {enabled: true, release: null}, failure = null;
const controller = window.createUpdateNotifications({$, state,
  api: async method => {
    if (method === 'get_update_status') return {state: 'idle'};
    if (failure) throw failure;
    return response;
  },
  syncShellInteractivity() {}, renderStatus() {}, toast() {}, reportError() {},
});
async function check(mode, label) {
  await controller.refreshUpdate(false);
  assert.equal($('#versionStatusDot').className, `health-dot${mode ? ` ${mode}` : ''}`);
  assert.equal($('#versionStatus').title, label);
  assert.equal($('#versionStatus')['aria-label'], `v1.0.15 · ${label}`);
}
(async () => {
  await check('online', '已是最新版本');
  failure = new Error('offline');
  await check('', '更新检查失败');
  failure = null;
  response = {enabled: true, release: {version: '1.0.16', release_id: 'next'}};
  await check('warning', '有可用更新 v1.0.16');
  response = {busy: true};
  await check('warning', '有可用更新 v1.0.16');
  failure = new Error('offline');
  await check('warning', '有可用更新 v1.0.16，本次检查失败');
  failure = null;
  response = {enabled: true, release: null};
  await check('online', '已是最新版本');
  response = {enabled: false};
  await check('', '在线更新未启用');
  failure = {product: {code: 'UPD-1016'}};
  await check('warning', '当前版本已停止支持，暂未获取到可安装更新');
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    setup = "const window = {setTimeout() {return 1;}, clearTimeout() {}}; const document = {body: {classList: {toggle() {}}}};\n"
    result = subprocess.run(
        ["node"], input=setup + script + harness, text=True, encoding="utf-8",
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_update_popup_describes_release_and_offers_later_action() -> None:
    script = manager_script()

    assert 'cancelText: mandatory ? "稍后更新" : "稍后提醒"' in script
    assert 'intent: "primary"' in script
    assert "formatBytes(release.package_size)" in script
    assert 'release.notes.join("\\n")' in script
    assert '].filter(Boolean).join("\\n\\n")' in script
    assert "更新会强制关闭机器人程序及当前连接的微信，是否确认？" in script
    assert 'confirmText: closing ? "再次关闭管理器" : "确认关闭并安装"' in script
    assert "scheduleAutomaticUpdateCheck(1800)" in script
    assert "configured : 600" in script


def test_update_catalog_has_explicit_running_robot_prerequisite() -> None:
    catalog = json.loads(
        (ROOT / "src" / "wechatrebot_manager" / "error-codes.zh-CN.json").read_text(
            encoding="utf-8"
        )
    )

    error = catalog["errors"]["14004"]
    assert error["title"] == "请先停止机器人"
    assert "停止机器人" in error["message"]
