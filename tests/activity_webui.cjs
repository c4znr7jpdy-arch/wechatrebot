const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../src/wechatrebot_manager/app_webui');

async function testDropdownFilters(view, state, $, reports, sample, now) {
  state.activityItems = [
    ['recent-error', 'error', 'manager', 1],
    ['old-error', 'error', 'manager', 30],
    ['recent-warning', 'warning', 'engine-runtime', 2],
    ['recent-info', 'info', 'engine-runtime', 3],
    ['unknown-time', 'error', 'manager', null],
    ['boundary-warning', 'warning', 'wechat-connection', 24],
    ['old-warning', 'warning', 'wechat-connection', 24 + 1 / 3600],
  ].map(([id, level, source, hours]) => ({...sample, id, level, source,
    message: id, report: `REPORT ${id}`, timestamp: hours === null ? null : now / 1000 - hours * 3600,
  }));
  view.renderActivity();
  assert.match($('#activitySummary').textContent, /最近 48 小时.*3 条错误.*3 条警告/);
  assert.match($('#activityList').innerHTML, /unknown-time/);

  const select = (id, value) => { $(id).value = value; $(id).events.change(); };
  state.activityPage = 2;
  select('#activityRange', '24');
  assert.equal(state.activityPage, 1);
  assert.match($('#activityPage').textContent, /第 1 \/ 1 页 · 3 条/);
  assert.match($('#activitySummary').textContent, /最近 24 小时.*3 条记录.*1 条错误.*2 条警告/);
  assert.match($('#activityList').innerHTML, /boundary-warning/);
  assert.doesNotMatch($('#activityList').innerHTML, /old-error|old-warning|unknown-time|recent-info/);

  select('#activitySource', 'manager');
  assert.match($('#activityPage').textContent, /· 1 条/);
  assert.match($('#activitySummary').textContent, /1 条错误.*0 条警告/);
  select('#activityLevel', 'warning');
  assert.match($('#activityList').innerHTML, /没有符合筛选/);
  assert.equal($('#copyLatestActivity').disabled, true);
  assert.match($('#activitySummary').textContent, /0 条记录.*0 条错误.*0 条警告/);
  select('#activitySource', 'engine-runtime');
  assert.match($('#activityPage').textContent, /· 1 条/);
  assert.match($('#activityList').innerHTML, /recent-warning/);
  assert.doesNotMatch($('#activityList').innerHTML, /recent-error|recent-info/);
  await $('#copyLatestActivity').events.click();
  assert.equal(reports.at(-1), 'REPORT recent-warning');

  select('#activityLevel', 'all');
  select('#activitySource', '');
  assert.match($('#activityPage').textContent, /· 4 条/);
  assert.match($('#activityList').innerHTML, /recent-info/);
  select('#activityRange', '0');
  assert.match($('#activitySummary').textContent, /最近 48 小时.*7 条记录.*3 条错误.*3 条警告/);
  assert.match($('#activityList').innerHTML, /old-error/);
  assert.match($('#activityList').innerHTML, /unknown-time/);
  select('#activityLevel', 'issues');
}

async function main() {
  const nodes = new Map();
  const $ = (key) => {
    if (!nodes.has(key)) nodes.set(key, {value: '', innerHTML: '', textContent: '', disabled: false, hidden: true,
      events: {}, addEventListener(type, handler) { this.events[type] = handler; }});
    return nodes.get(key);
  };
  $('#activityLevel').value = 'issues';
  $('#activityRange').value = '0';
  const state = {activityItems: [], activityPage: 1};
  const reports = [];
  const now = Date.now();
  let fail = false;
  let copyFails = false;
  const items = Array.from({length: 18}, (_, i) => ({
    id: `issue-${i}`, code: '19001', level: i === 17 ? 'info' : 'error',
    title: '<img src=x onerror=alert(1)>', source: 'manager', source_name: '管理器',
    message: `failure ${i}`, detail: 'stack.js:42', report: `REPORT ${i}\nFULL STACK`,
    time: '2026-10-10T10:00:00+08:00', timestamp: now / 1000, count: 1,
  }));
  const context = vm.createContext({window: {}, setTimeout, clearTimeout,
    Date: class extends Date { static now() { return now; } },
  });
  vm.runInContext(fs.readFileSync(path.join(root, 'activity.js'), 'utf8'), context);
  const view = context.window.createActivityView({$, state,
    api: async () => { if (fail) throw Error('read failed'); return items; },
    escapeHtml: (v) => String(v ?? '').replaceAll('<','&lt;').replaceAll('>','&gt;'),
    fullTime: (v) => v, reportError: () => {},
    copyText: async (v) => { reports.push(v); return !copyFails; },
  });
  await view.loadActivity();
  assert.match($('#activityPage').textContent, /17/);
  assert.ok(!$('#activityList').innerHTML.includes('<img'));
  assert.ok(!$('#activityList').innerHTML.includes('已恢复'));
  $('#activityNext').events.click();
  assert.match($('#activityPage').textContent, /2 \/ 2/);
  $('#activitySearch').value = 'no match';
  $('#activitySearch').events.input();
  assert.ok($('#copyLatestActivity').disabled);
  assert.match($('#activityList').innerHTML, /没有符合筛选/);
  $('#activitySearch').value = '';
  $('#activitySearch').events.input();
  await $('#copyLatestActivity').events.click();
  assert.equal(reports[0], items[0].report);
  copyFails = true;
  await $('#copyLatestActivity').events.click();
  assert.match($('#activityFeedback').textContent, /19005/);
  copyFails = false;
  await testDropdownFilters(view, state, $, reports, items[0], now);
  await view.loadActivity();
  const before = $('#activityList').innerHTML;
  fail = true;
  await view.loadActivity();
  assert.equal($('#activityList').innerHTML, before);
  assert.equal($('#activityReadError').hidden, false);
  assert.equal($('#refreshActivity').disabled, false);
  state.activityItems = [];
  await view.loadActivity();
  assert.ok(!$('#activityList').innerHTML.includes('正在读取'));

  const events = {};
  const saved = [];
  const win = {addEventListener: (key, handler) => { events[key] = handler; }};
  const reporter = vm.createContext({window: win, setTimeout, clearTimeout,
    document: {querySelector: () => ({id:'page-activity'}), getElementById: () => null, body: null},
  });
  vm.runInContext(fs.readFileSync(path.join(root, 'activity-errors.js'), 'utf8'), reporter);
  events.error({error: new Error('render failed')});
  win.pywebview = {api: {record_ui_error: async (payload) => { saved.push(payload); return {ok:true}; }}};
  await win.managerErrorReporter.flush();
  assert.equal(saved.length, 1);
  assert.match(saved[0].stack, /render failed/);
  assert.equal(saved[0].page, 'page-activity');
  console.log('ACTIVITY_WEBUI_COMPLETE');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
