const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = require('./webui_source.cjs').managerScript(require('node:path').dirname(process.argv[2]));

function fixture() {
  const draft = {account_id: 'account-a', arguments: {text: 'unsaved reminder'}};
  const state = {scheduledLoad: 0, activePage: 'schedules', scheduledDirty: true,
    scheduledEditor: draft, scheduledContext: {account_id: 'account-a', items: [{id: 'task-a'}]},
    scheduledCommandCatalog: [], schedules: [{id: 'old-a'}],
    legacyMigrations: [{key: 'pending-a', account_id: 'account-a'}], legacyMigrationLoading: false};
  const elements = new Map();
  const $ = (id) => {
    if (!elements.has(id)) elements.set(id, {hidden: id === '#modalBackdrop', disabled: false,
      isConnected: true, textContent: '', innerHTML: '', attributes: {}, focused: false,
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; }, focus() { this.focused = true; }});
    return elements.get(id);
  };
  $('#scheduleRows').innerHTML = 'old-a|pending-a';
  $('#taskRows').innerHTML = 'task-a';
  const controls = [$('old-control')], calls = [], dialogs = [], errors = [], toasts = [];
  const responses = {
    get_scheduled_task_context: {account_id: 'account-a', items: [{id: 'task-a'}], groups: [], contacts: []},
    get_schedules: {account_id: 'account-a', items: [{id: 'old-a'}]},
    get_legacy_schedule_migrations: {items: []},
    preview_legacy_schedule_migration: {token: 'token-a', account_id: 'account-a',
      action: 'news', targets: [{id: 'group-a@chatroom', type: 'group'}],
      arguments: {count: 20}, schedule_text: '每天 09:00', message: '保存为暂停草稿'},
  };
  const context = vm.createContext({state, $, $$: () => controls, Promise, Boolean, Array,
    api: async (method, ...args) => {
      calls.push({method, args});
      const value = responses[method];
      if (value instanceof Error) throw value;
      return typeof value === 'function' ? value(...args) : value;
    },
    renderSchedules() {
      $('#scheduleRows').innerHTML = [...state.schedules.map(item => item.id),
        ...state.legacyMigrations.map(item => item.key)].join('|');
    },
    renderScheduledTasks() { $('#taskRows').innerHTML = (state.scheduledContext?.items || []).map(item => item.id).join('|'); },
    renderGroupAnalysisMigration() {},
    updateScheduledSummary() {},
    reportError(error) { errors.push(error); },
    scheduledName() { return '热点资讯'; },
    toast(message) { toasts.push(message); },
    confirmAction(options) { dialogs.push(options); $('#modalBackdrop').hidden = false; },
  });
  for (const [startMarker, endMarker] of [
    ['  async function loadSchedules(', '  function renderGroupAnalysisMigration('],
    ['  async function beginLegacyMigration(', '  async function scheduleAction('],
  ]) {
    const start = script.indexOf(startMarker), end = script.indexOf(endMarker, start);
    assert.ok(start >= 0 && end > start, startMarker);
    vm.runInContext(script.slice(start, end), context);
  }
  return {context, state, $, draft, controls, calls, dialogs, errors, responses, toasts};
}

function assertDraftRetained(f) {
  assert.equal(f.state.scheduledEditor, f.draft);
  assert.equal(f.state.scheduledEditor.arguments.text, 'unsaved reminder');
  assert.equal(f.state.scheduledDirty, true);
}

const cases = {
  async progressive_task_list() {
    const f = fixture();
    let finishFull, fullStarted;
    const started = new Promise(resolve => { fullStarted = resolve; });
    f.responses.get_scheduled_task_context = (lightweight) => lightweight
      ? {account_id: 'account-a', items: [{id: 'local-a'}], loading: true}
      : new Promise(resolve => { finishFull = resolve; fullStarted(); });
    const pending = f.context.loadSchedules();
    await started;
    assert.equal(f.$('#taskRows').innerHTML, 'local-a', 'Local tasks render while directories are still pending');
    assert.equal(f.state.scheduledContext.loading, true);
    assert.equal(f.$('#refreshSchedules').disabled, true);
    assertDraftRetained(f);
    finishFull({account_id: 'account-a', items: [{id: 'verified-a'}]});
    await pending;
    assert.equal(f.$('#taskRows').innerHTML, 'verified-a');
    assert.equal(f.state.scheduledContextRequest, null);
    assertDraftRetained(f);
  },
  async progressive_account_change() {
    for (const viaRuntime of [false, true]) {
      const f = fixture();
      let finishFull, fullStarted;
      const started = new Promise(resolve => { fullStarted = resolve; });
      f.state.historyAccount = 'account-a';
      f.responses.get_scheduled_task_context = (lightweight) => lightweight
        ? {account_id: 'account-a', items: [{id: 'local-a'}], loading: true}
        : new Promise(resolve => { finishFull = resolve; fullStarted(); });
      f.responses.get_legacy_schedule_migrations = {items: [{key: 'pending-a', account_id: 'account-a'}]};
      const pending = f.context.loadSchedules();
      await started;
      if (viaRuntime) f.state.historyAccount = 'account-b';
      finishFull({account_id: viaRuntime ? 'account-a' : 'account-b', items: [{id: 'stale-task'}]});
      await pending;
      assert.equal(f.$('#taskRows').innerHTML, '');
      assert.equal(f.$('#scheduleRows').innerHTML, '', 'Account mismatch must also hide legacy migration records');
      assert.match(f.$('#scheduleContextMessage').textContent, /账号已变化/);
      assertDraftRetained(f);
    }
  },
  async progressive_overlapping_refresh() {
    const f = fixture();
    let finishFirst, finishSecond;
    let firstStarted, secondStarted;
    const firstReady = new Promise(resolve => { firstStarted = resolve; });
    const secondReady = new Promise(resolve => { secondStarted = resolve; });
    f.responses.get_scheduled_task_context = (lightweight) => lightweight
      ? {account_id: 'account-a', items: [{id: 'local-a'}], loading: true}
      : new Promise(resolve => { finishFirst = resolve; firstStarted(); });
    const first = f.context.loadSchedules();
    await firstReady;
    f.responses.get_scheduled_task_context = (lightweight) => lightweight
      ? {account_id: 'account-b', items: [{id: 'local-b'}], loading: true}
      : new Promise(resolve => { finishSecond = resolve; secondStarted(); });
    const second = f.context.loadSchedules();
    await secondReady;
    const request = f.state.scheduledContextRequest;
    finishFirst({account_id: 'account-a', items: [{id: 'old-a'}]});
    await first;
    assert.equal(f.$('#taskRows').innerHTML, 'local-b');
    assert.equal(f.state.scheduledContextRequest, request, 'Old completion must not clear the current pending request');
    assert.equal(f.$('#refreshSchedules').disabled, true);
    finishSecond({account_id: 'account-b', items: [{id: 'verified-b'}]});
    await second;
    assert.equal(f.$('#taskRows').innerHTML, 'verified-b');
    assertDraftRetained(f);
  },
  async invalidate_context_keeps_draft() {
    const f = fixture();
    let finishFull, fullStarted;
    const started = new Promise(resolve => { fullStarted = resolve; });
    f.responses.get_scheduled_task_context = (lightweight) => lightweight
      ? {account_id: 'account-a', items: [{id: 'local-a'}], loading: true}
      : new Promise(resolve => { finishFull = resolve; fullStarted(); });
    const pending = f.context.loadSchedules();
    await started;
    f.context.invalidateScheduledContext();
    assert.equal(f.$('#taskRows').innerHTML, '');
    assert.equal(f.$('#scheduleRows').innerHTML, '');
    assertDraftRetained(f);
    finishFull({account_id: 'account-a', items: [{id: 'stale-a'}]});
    await pending;
    assert.equal(f.$('#taskRows').innerHTML, '');
    assert.equal(f.state.scheduledContextRequest, null);
    assertDraftRetained(f);
  },
  async editor_waits_for_full_context() {
    const f = fixture();
    f.state.scheduledDirty = false;
    f.state.scheduledEditor = null;
    Object.assign(f.context, {
      structuredClone, crypto: require('node:crypto'),
      SCHEDULED_DEFAULTS: {weather: {}},
      guardScheduledDraft: () => false,
      renderScheduledEditor() {},
      scheduledTaskItems: () => f.state.scheduledContext?.items || [],
    });
    const start = script.indexOf('  async function openScheduledEditor(');
    vm.runInContext(script.slice(start, script.indexOf('  function scheduledArgumentsMarkup(', start)), f.context);
    let finishFull, fullStarted;
    const started = new Promise(resolve => { fullStarted = resolve; });
    f.responses.get_scheduled_task_context = (lightweight) => lightweight
      ? {account_id: 'account-a', items: [], loading: true}
      : new Promise(resolve => { finishFull = resolve; fullStarted(); });
    const loading = f.context.loadSchedules();
    await started;
    const editing = f.context.openScheduledEditor();
    assert.equal(f.state.scheduledEditor, null);
    finishFull({account_id: 'account-a', items: []});
    await Promise.all([loading, editing]);
    assert.equal(f.state.scheduledEditor.account_id, 'account-a');
  },
  async failed_context_keeps_draft() {
    const f = fixture();
    f.responses.get_scheduled_task_context = new Error('请登录原微信账号');
    await f.context.loadSchedules();
    assert.equal(f.$('#scheduleRows').innerHTML, '');
    assert.equal(f.$('#taskRows').innerHTML, '');
    assert.match(f.$('#scheduleContextMessage').textContent, /请登录原微信账号/);
    assert.equal(f.state.scheduledContext.legacy_group_analysis, undefined);
    assert.equal(f.$('#refreshSchedules').disabled, false);
    assertDraftRetained(f);
  },
  async unreadable_migration_blocks_legacy() {
    const f = fixture();
    f.responses.get_legacy_schedule_migrations = new Error('migration journal unavailable');
    await f.context.loadSchedules();
    assert.equal(f.$('#scheduleRows').innerHTML, '');
    assert.equal(f.$('#taskRows').innerHTML, 'task-a');
    assert.match(f.$('#scheduleContextMessage').textContent, /迁移记录暂时无法读取/);
    assertDraftRetained(f);
  },
  async cross_account_rows() {
    const f = fixture();
    f.responses.get_scheduled_task_context = {account_id: 'account-b', items: []};
    f.responses.get_legacy_schedule_migrations.items = [
      {key: 'pending-a', account_id: 'account-a'}, {key: 'pending-b', account_id: 'account-b'},
    ];
    await f.context.loadSchedules();
    assert.equal(f.$('#scheduleRows').innerHTML, 'pending-b');
    assert.match(f.$('#scheduleContextMessage').textContent, /其他账号的旧任务已隐藏/);
    assertDraftRetained(f);
  },
  async offline_recovery_completion() {
    const f = fixture();
    f.responses.get_scheduled_task_context = new Error('机器人已断开');
    f.responses.get_legacy_schedule_migrations.items = [{key: 'pending-a', account_id: 'account-a'}];
    await f.context.loadSchedules();
    assert.equal(f.$('#scheduleRows').innerHTML, 'pending-a');
    assert.match(f.$('#scheduleContextMessage').textContent, /可继续完成下方已确认的旧任务迁移/);
    f.responses.get_legacy_schedule_migrations.items = [];
    await f.context.loadSchedules();
    assert.equal(f.$('#scheduleRows').innerHTML, '');
    assertDraftRetained(f);
  },
  async overlapping_refresh() {
    const f = fixture();
    let completeFirst;
    f.responses.get_scheduled_task_context = () => new Promise(resolve => { completeFirst = resolve; });
    const first = f.context.loadSchedules();
    assert.equal(f.controls[0].disabled, true, 'Old controls must stop accepting input while refreshing');
    f.responses.get_scheduled_task_context = {account_id: 'account-b', items: [{id: 'task-b'}]};
    f.responses.get_schedules = {account_id: 'account-b', items: [{id: 'old-b'}]};
    await f.context.loadSchedules();
    completeFirst({account_id: 'account-a', items: [{id: 'task-a'}]});
    await first;
    assert.equal(f.$('#scheduleRows').innerHTML, 'old-b');
    assert.equal(f.$('#taskRows').innerHTML, 'task-b');
    assert.equal(f.$('#refreshSchedules').disabled, false);
    assertDraftRetained(f);
  },
  async duplicate_preview() {
    const f = fixture(), plan = f.responses.preview_legacy_schedule_migration;
    let complete;
    f.responses.preview_legacy_schedule_migration = () => new Promise(resolve => { complete = resolve; });
    const button = f.$('preview-control');
    const first = f.context.beginLegacyMigration('old-a', '', button);
    await f.context.beginLegacyMigration('old-a', '', button);
    assert.equal(f.calls.length, 1);
    assert.equal(button.disabled, true);
    complete(plan); await first;
    assert.equal(f.dialogs.length, 1);
    assert.equal(button.focused, true);
    assert.equal(button.disabled, false);
    assert.equal(f.state.legacyMigrationLoading, false);
    await f.context.beginLegacyMigration('old-a', '', button);
    assert.equal(f.calls.length, 1, 'An open confirmation must keep its token');
    assert.equal(f.calls.filter(call => call.method === 'migrate_legacy_schedule').length, 0);
  },
  async stale_preview() {
    for (const change of [f => { f.state.scheduledLoad += 1; }, f => { f.state.activePage = 'settings'; }]) {
      const f = fixture(), plan = f.responses.preview_legacy_schedule_migration;
      let complete;
      f.responses.preview_legacy_schedule_migration = () => new Promise(resolve => { complete = resolve; });
      const pending = f.context.beginLegacyMigration('old-a', '', f.$('preview-control'));
      change(f); complete(plan); await pending;
      assert.equal(f.dialogs.length, 0);
      assert.equal(f.state.legacyMigrationLoading, false);
      assert.equal(f.$('preview-control').disabled, false);
    }
  },
  async preview_error() {
    const f = fixture();
    f.responses.preview_legacy_schedule_migration = new Error('账号已变化');
    await f.context.beginLegacyMigration('old-a', '', f.$('preview-control'));
    assert.equal(f.errors.length, 1);
    assert.equal(f.dialogs.length, 0);
    assert.equal(f.state.legacyMigrationLoading, false);
    assert.equal(f.$('preview-control').disabled, false);
    assert.equal(f.$('preview-control').attributes['aria-busy'], undefined);
  },
  async malformed_response() {
    const f = fixture();
    f.responses.get_legacy_schedule_migrations = {items: {invalid: true}};
    await f.context.loadSchedules();
    assert.equal(f.$('#scheduleRows').innerHTML, '');
    assert.equal(f.$('#taskRows').innerHTML, '');
    assert.equal(f.errors.length, 1);
    assertDraftRetained(f);
  },
  async product_error_reason() {
    const f = fixture();
    const start = script.indexOf('  function reportError('), end = script.indexOf('  function debounce(', start);
    vm.runInContext(script.slice(start, end), f.context);
    f.context.reportError({product: {title: '暂时无法操作', code: 'SCHEDULE_INVALID', message: '微信账号已变化，请刷新后重试。'}});
    assert.match(f.toasts[0], /暂时无法操作：微信账号已变化，请刷新后重试。/);
    f.context.reportError({product: {title: '相同标题', message: '相同标题'}});
    assert.equal(f.toasts[1], '相同标题');
    f.context.reportError({product: {title: '操作未完成', detail: '详细处理方式', message: '其他原因'}});
    assert.equal(f.toasts[2], '操作未完成：详细处理方式');
  },
};

(async () => {
  const scenario = process.argv[3];
  assert.ok(Object.hasOwn(cases, scenario), `Unknown scenario: ${scenario}`);
  await cases[scenario]();
  console.log(`passed: ${scenario}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
