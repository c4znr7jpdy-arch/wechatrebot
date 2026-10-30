const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function node() {
  const classes = new Set();
  return {
    hidden: true, disabled: false, textContent: '', html: '', writes: 0, dataset: {}, events: {}, details: [],
    scrollTop: 140,
    classList: {toggle(key, enabled) { if (enabled) classes.add(key); else classes.delete(key); }, contains(key) { return classes.has(key); }},
    get innerHTML() { return this.html; },
    set innerHTML(value) {
      this.html = value; this.writes++;
      this.details = [...value.matchAll(/data-runtime-detail="([^"]+)"/g)].map((match) => ({dataset: {runtimeDetail: match[1]}, open: false}));
    },
    addEventListener(type, handler) { this.events[type] = handler; },
    querySelectorAll(selector) { return selector.startsWith('details') ? this.details.filter((item) => selector === 'details' || item.open) : []; },
    setAttribute(key, value) { this[key] = value; }, focus() {}, scrollIntoView() {},
  };
}

function harness() {
  const nodes = new Map();
  const $ = (selector) => { if (!nodes.has(selector)) nodes.set(selector, node()); return nodes.get(selector); };
  const item = {
    id: 'memory-issue', category: 'memory_initialization', title: '<img src=x onerror=alert(1)>', message: '向量连接失败',
    detail: '<script>unsafe</script>', report: '脱敏问题报告', level: 'warning', state: 'recovery_unconfirmed',
    timestamp: 1793325200, time: '2026-10-30T09:53:20+08:00', count: 4, hidden: false, action: 'models', action_label: '检查向量模型',
  };
  let response = {items: [item], categories: [{id: item.category, title: '长期记忆向量初始化失败', muted_at: ''}], closed: [], session_id: 'first-session', window_seconds: 3600};
  const requests = [];
  let pendingRead = null, failWrite = false, confirmation = null;
  const state = {activePage: 'home'};
  const actions = [];
  const context = vm.createContext({window: {}, document: {visibilityState: 'visible'}, Date, Intl, setTimeout, clearTimeout});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/wechatrebot_manager/app_webui/runtime-alerts.js'), 'utf8'), context);
  const view = context.window.createRuntimeAlerts({$, state,
    api: async (method, category, action, id) => {
      requests.push([method, category, action, id]);
      if (method === 'get_runtime_alerts') return pendingRead ? await pendingRead : structuredClone(response);
      if (failWrite) throw Error('偏好文件无法写入');
      const entry = response.items[0], type = response.categories[0];
      if (action === 'close') { entry.closed_at = '2026-10-30T10:20:30+08:00'; response.closed = [{id, category, closed_at: entry.closed_at}]; }
      if (action === 'reopen') { entry.closed_at = ''; response.closed = []; }
      if (action === 'mute') type.muted_at = entry.muted_at = '2026-10-30T10:21:31+08:00';
      if (action === 'restore') type.muted_at = entry.muted_at = '';
      entry.hidden = Boolean(entry.closed_at || entry.muted_at);
      return structuredClone(response);
    },
    confirmAction: (value) => { confirmation = value; },
    copyText: async (value) => { actions.push(['copy', value]); return true; },
    escapeHtml: (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'),
    navigate: async (page) => actions.push(['navigate', page]),
    openModels: async () => actions.push(['models']), refreshRuntime: async () => actions.push(['status']), reportError: (error) => actions.push(['error', error.message]),
  });
  const click = (action, category = item.category, id = item.id) => $('#runtimeAlerts').events.click({target: {closest: () => ({dataset: {runtimeReminderAction: action, category, alertId: id}})}});
  return {$, view, state, actions, requests, click, get response() { return response; },
    get confirmation() { return confirmation; }, set pendingRead(value) { pendingRead = value; }, set failWrite(value) { failWrite = value; }};
}

async function main() {
  const h = harness();
  await h.view.loadRuntimeAlerts();
  assert.equal(h.$('#runtimeAlerts').hidden, false);
  assert.match(h.$('#runtimeAlertList').innerHTML, /恢复待确认/);
  assert.doesNotMatch(h.$('#runtimeAlertList').innerHTML, /<img|<script>|已恢复/);
  assert.match(h.$('#runtimeAlertList').innerHTML, /&lt;img/);
  const writes = h.$('#runtimeAlertList').writes;
  await h.view.loadRuntimeAlerts({force: true});
  assert.equal(h.$('#runtimeAlertList').writes, writes, 'unchanged polling keeps the current DOM');
  h.$('#runtimeAlertList').details[0].open = true;
  h.response.items[0].count = 5;
  await h.view.loadRuntimeAlerts({force: true});
  assert.equal(h.$('#runtimeAlertList').details[0].open, true, 'updated count preserves expanded report');
  assert.equal(h.$('.content-scroll').scrollTop, 140);

  await h.click('handle');
  assert.deepEqual(h.actions.at(-1), ['models']);
  await h.click('copy');
  assert.deepEqual(h.actions.at(-1), ['copy', '脱敏问题报告']);

  h.failWrite = true;
  await h.click('close');
  assert.match(h.$('#runtimeAlertList').innerHTML, /向量连接失败/, 'failed close must keep the reminder');
  assert.match(h.$('#runtimeAlertFeedback').textContent, /无法写入/);
  h.failWrite = false;

  let resolveRead;
  const stale = structuredClone(h.response);
  h.pendingRead = new Promise((resolve) => { resolveRead = resolve; });
  const read = h.view.loadRuntimeAlerts({force: true});
  await h.click('close');
  assert.equal(h.$('#runtimeAlertList').innerHTML, '');
  assert.match(h.$('#runtimeAlertPreferenceList').innerHTML, /2026\/10\/30 10:20:30/);
  resolveRead(stale); await read; h.pendingRead = null;
  assert.equal(h.$('#runtimeAlertList').innerHTML, '', 'a pre-save polling response cannot undo dismissal');
  await h.view.loadRuntimeAlerts({force: true});
  assert.equal(h.$('#runtimeAlertList').innerHTML, '', 'same manager session stays dismissed after refresh');
  await h.click('reopen');
  assert.match(h.$('#runtimeAlertList').innerHTML, /向量连接失败/);

  let rejectRead;
  h.pendingRead = new Promise((_, reject) => { rejectRead = reject; });
  const failedRead = h.view.loadRuntimeAlerts({force: true});
  await h.click('close');
  const closeFeedback = h.$('#runtimeAlertFeedback').textContent;
  rejectRead(Error('旧轮询失败')); await failedRead; h.pendingRead = null;
  assert.equal(h.$('#runtimeAlertList').innerHTML, '');
  assert.equal(h.$('#runtimeAlertFeedback').textContent, closeFeedback, 'an old polling failure cannot replace successful dismissal feedback');
  await h.click('reopen');

  const savedBeforeConfirmation = h.requests.filter((entry) => entry[0] === 'set_runtime_alert_preference').length;
  await h.click('mute');
  assert.match(h.confirmation.message, /今后发生同类问题也不再提醒/);
  assert.equal(h.requests.filter((entry) => entry[0] === 'set_runtime_alert_preference').length, savedBeforeConfirmation, 'confirmation cancellation leaves preferences untouched');
  h.failWrite = true;
  await assert.rejects(h.confirmation.onConfirm(), /无法写入/);
  assert.match(h.$('#runtimeAlertList').innerHTML, /向量连接失败/);
  h.failWrite = false;
  await h.confirmation.onConfirm();
  assert.equal(h.$('#runtimeAlertList').innerHTML, '');
  assert.match(h.$('#runtimeAlertPreferenceList').innerHTML, /2026\/10\/30 10:21:31/);
  assert.match(h.$('#runtimeAlertPreferenceList').innerHTML, /永久不再提醒/);
  await h.click('restore');
  assert.match(h.$('#runtimeAlertList').innerHTML, /向量连接失败/);

  const reads = h.requests.length;
  h.state.activePage = 'history';
  await h.view.loadRuntimeAlerts({force: true});
  assert.equal(h.requests.length, reads, 'hidden run center does not poll logs');
  console.log('RUNTIME_ALERTS_WEBUI_COMPLETE');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
