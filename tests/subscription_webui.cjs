const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname,
  '../src/wechatrebot_manager/app_webui/subscription.js'), 'utf8');
const escapeHtml = value => String(value ?? '').replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness(overrides = {}) {
  const elements = new Map(), calls = [], intervals = [], confirmations = [], routes = [];
  function element(id) {
    if (!elements.has(id)) {
      const child = {textContent: ''};
      const attributes = {};
      const node = {id, value: '', hidden: false, disabled: false, innerHTML: '', textContent: '',
        dataset: {}, listeners: {}, lastChild: child, classList: {toggle() {}},
        querySelector: () => child, focus() {},
        setAttribute: (name, value) => { attributes[name] = value; },
        removeAttribute: name => { delete attributes[name]; },
        addEventListener: (name, fn) => { node.listeners[name] = fn; }};
      elements.set(id, node);
    }
    return elements.get(id);
  }
  const snapshot = {enabled: true, configured: true, state: 'unactivated', authorized: false,
    can_run: false, can_connect: false, account: '', current_account: '', license_id: '', device_id: '',
    plan: '', valid_until: 0, remaining_seconds: 0, qq_group: '', qq_url: '',
    plans: [{id: 'trial', name: '一小时试用', price_fen: 0},
      {id: 'month', name: '月付', price_fen: 6900}, {id: 'year', name: '年付', price_fen: 69900}], ...overrides};
  const handlers = {};
  const context = vm.createContext({window: {setInterval: fn => intervals.push(fn)},
    document: {querySelector: element, querySelectorAll: () => [], visibilityState: 'visible'},
    Intl, Date, Set});
  vm.runInContext(script, context);
  const ui = context.window.createSubscriptionPage({escapeHtml,
    api: async (method, ...args) => {
      calls.push([method, ...args]);
      if (handlers[method]) return handlers[method](...args);
      if (method === 'get_license_status') return structuredClone(snapshot);
      throw new Error(`Unexpected API call: ${method}`);
    },
    confirmAction: config => confirmations.push(config),
    copyText: async (text, label) => calls.push(['copy', text, label]),
    navigate: async page => routes.push(page),
    openMaintenance: async () => routes.push('maintenance'), refreshRuntime: async () => {},
  });
  return {ui, snapshot, handlers, calls, element, intervals, confirmations, routes,
    click: id => element(id).listeners.click({}),
    async input(code) { element('#licenseCode').value = code; element('#licenseCode').listeners.input(); },
    async preview(code) {
      await this.input(code);
      element('#licenseCodeForm').listeners.submit({preventDefault() {}});
      await flush();
    },
  };
}

async function activationAndRecovery() {
  const h = harness();
  await h.ui.initialize();
  assert.equal(h.ui.needsEntry(), true);
  assert.equal(h.element('#licenseOpenGroup').disabled, true);
  assert.equal(h.element('#licenseCopyGroup').disabled, true);
  assert.equal(h.element('#licensePrepareDevice').hidden, false);
  assert.match(h.element('#licensePlans').innerHTML, />69<\/strong>/);
  assert.match(h.element('#licensePlans').innerHTML, />699<\/strong>/);
  assert.equal(h.calls.every(([method]) => method === 'get_license_status'), true);

  h.handlers.preview_activation_code = async () => ({preview_id: 'migration', plan: 'year', mode: 'migration',
    valid_until: 1800000000, license_id: 'old-paid-license', replaces_license_id: 'local-trial-license'});
  await h.preview('WRB-MIGRATE-CODE');
  assert.match(h.element('#licenseCodePreviewDetails').innerHTML, /local-trial-license/);
  assert.match(h.element('#licenseCodePreviewDetails').innerHTML, /old-paid-license/);
  assert.match(h.element('#licenseCodePreviewMessage').textContent, /原试用或到期授权将被停用/);
  assert.equal(h.element('#licenseConfirmRedeem').textContent, '确认迁移');
  h.click('#licenseCancelRedeem');

  h.handlers.preview_activation_code = async () => ({preview_id: 'preview-one', plan: 'month',
    mode: 'new', valid_until: 1800000000, already_redeemed: false, message: '<img src=x onerror=alert(1)>'});
  await h.preview('WRB-EXAMPLE-CODE');
  assert.equal(h.element('#licenseCodePreview').hidden, false);
  assert.equal(h.element('#licenseCodePreviewMessage').textContent, '<img src=x onerror=alert(1)>');
  assert.equal(h.calls.some(([method]) => method === 'redeem_activation_code'), false);
  h.intervals[0](); await flush();
  assert.equal(h.element('#licenseCode').value, 'WRB-EXAMPLE-CODE');
  assert.equal(h.element('#licenseCodePreview').hidden, false);

  let release;
  h.handlers.redeem_activation_code = async (code, previewId) => {
    assert.equal(code, 'WRB-EXAMPLE-CODE'); assert.equal(previewId, 'preview-one');
    await new Promise(resolve => { release = resolve; });
    Object.assign(h.snapshot, {state: 'active_unbound', authorized: true, can_connect: true,
      license_id: 'license-one', valid_until: 1800000000, plan: 'month'});
    return structuredClone(h.snapshot);
  };
  const redeem = h.click('#licenseConfirmRedeem');
  await h.click('#licenseConfirmRedeem');
  assert.equal(h.calls.filter(([method]) => method === 'redeem_activation_code').length, 1);
  release(); await redeem;
  assert.equal(h.element('#licenseCode').value, '');
  assert.equal(h.element('#licenseCodePreview').hidden, true);
  assert.equal(h.element('#licenseConnect').hidden, false);
  assert.equal(h.ui.needsEntry(), true, 'An unbound but valid lease still needs account confirmation');
  assert.equal(h.calls.some(([method]) => method === 'start_robot'), false);

  Object.assign(h.snapshot, {current_account: 'wxid_account_a'});
  await h.ui.onNavigate('subscription');
  h.click('#licenseBind');
  assert.match(h.confirmations[0].message, /wxid_account_a/);
  h.snapshot.current_account = 'wxid_account_b';
  h.handlers.bind_license_account = async account => {
    assert.equal(account, 'wxid_account_a', 'The confirmed account must be sent to reject identity races');
    throw new Error('微信账号已变化，请重新确认。');
  };
  await assert.rejects(h.confirmations[0].onConfirm(), /账号已变化/);

  Object.assign(h.snapshot, {state: 'ready', account: 'wxid_account_a', current_account: 'wxid_account_a'});
  await h.ui.onNavigate('subscription');
  assert.equal(h.element('#licenseResume').hidden, false);
  assert.equal(h.element('#licenseResume').disabled, false);
  assert.equal(await h.ui.beforeStart(), true, 'A valid matching lease need not already have a run grant');
  h.handlers.start_robot = async () => { h.snapshot.can_run = true; return structuredClone(h.snapshot); };
  await h.click('#licenseResume');
  assert.equal(h.calls.filter(([method]) => method === 'start_robot').length, 1);
}

async function expiryAndUnconfigured() {
  const h = harness({state: 'expired', license_id: 'license-one', account: 'wxid_account_a',
    current_account: 'wxid_account_a', message: '订阅已到期，自动化已暂停。'});
  await h.ui.initialize();
  assert.equal(h.element('#licensePausedInfo').hidden, false);
  assert.equal(h.element('#homeLicenseNotice').hidden, false);
  assert.equal(h.element('#licenseOpenHistory').disabled, false);
  assert.equal(h.element('#licenseOpenMaintenance').disabled, false);
  assert.equal(await h.ui.beforeStart(), false);
  assert.equal(h.routes.at(-1), 'subscription');
  await h.click('#licenseOpenHistory'); await h.click('#licenseOpenMaintenance');
  assert.deepEqual(h.routes.slice(-2), ['history', 'maintenance']);
  assert.equal(h.calls.every(([method]) => method === 'get_license_status'), true);

  h.handlers.preview_activation_code = async () => ({preview_id: 'renew-one', plan: 'year',
    mode: 'renewal', valid_until: 1800000000, already_redeemed: false});
  await h.preview('WRB-RENEW-CODE');
  h.handlers.redeem_activation_code = async () => {
    Object.assign(h.snapshot, {state: 'paused', authorized: true, can_connect: true});
    return structuredClone(h.snapshot);
  };
  await h.click('#licenseConfirmRedeem');
  assert.equal(h.element('#licenseResume').textContent, '恢复机器人');
  assert.equal(h.element('#licenseResume').disabled, false);
  assert.equal(h.calls.some(([method]) => method === 'start_robot'), false, 'Renewal must not start automation');

  const off = harness({enabled: false, configured: false, state: 'legacy'});
  await off.ui.initialize();
  assert.equal(off.ui.needsEntry(), false);
  assert.equal(await off.ui.beforeStart(), true);
  assert.equal(off.element('#licenseCode').disabled, true);
  assert.equal(off.element('#homeLicenseNotice').hidden, true);
  assert.equal(off.element('#licenseStateTitle').textContent, '尚未启用商业授权');
  const pending = harness({configured: false, state: 'configuration_pending'});
  await pending.ui.initialize();
  assert.equal(pending.element('#licenseCode').disabled, true);
  assert.equal(pending.element('#licenseStateTitle').textContent, '授权服务待配置');
}

async function rejectedStartIsNotReportedAsSuccess() {
  const h = harness({state: 'active', license_id: 'license-one', authorized: true,
    can_connect: true, account: 'wxid_account_a', current_account: 'wxid_account_a'});
  await h.ui.initialize();
  h.handlers.start_robot = async () => ({accepted: false});
  await h.click('#licenseResume');
  assert.equal(h.element('#licenseFeedback').dataset.tone, 'error');
  assert.match(h.element('#licenseFeedback').textContent, /操作尚未开始/);
  assert.equal(h.snapshot.can_run, false);
}

function runCenterCanStartWithBridgeConnected() {
  const app = fs.readFileSync(path.join(__dirname, '../src/wechatrebot_manager/app_webui/app.js'), 'utf8');
  const start = app.indexOf('  function renderStatus(');
  const end = app.indexOf('  async function refreshStatus(', start);
  assert.ok(start >= 0 && end > start);
  const h = harness();
  const context = vm.createContext({state: {historyAccount: 'wxid_a', historyReadyAccount: 'wxid_a', activePage: 'home', mandatoryUpdate: false},
    $: h.element, subscription: {updateRuntime() {}}, reconcileStartPending: status => status,
    escapeHtml, icon: id => id, setStage() {}, setHistoryActionsEnabled() {}, Date,
  });
  vm.runInContext(app.slice(start, end), context);
  const status = {state: 'LICENSE_READY', title: '微信已连接', message: '请启动机器人',
    can_start: true, can_stop: true, online: false, busy: false, environment: {ready: true},
    runtime: {wechat_identity: 'wxid_a', engine_ready: false, connection_ready: true}};
  context.renderStatus(status);
  assert.equal(h.element('#primaryAction').dataset.action, 'start');
  assert.equal(h.element('#primaryAction').disabled, false);
  assert.match(h.element('#globalStatus').innerHTML, /微信已连接/);
  context.renderStatus({...status, state: 'RUNNING', can_start: false, online: true});
  assert.equal(h.element('#primaryAction').dataset.action, 'stop');
  assert.equal(h.element('#primaryAction').disabled, false);
}

(async () => {
  await activationAndRecovery();
  await expiryAndUnconfigured();
  await rejectedStartIsNotReportedAsSuccess();
  runCenterCanStartWithBridgeConnected();
  process.stdout.write('Subscription UI activation, expiry and confirmation checks passed.\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
