const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname,
  '../src/wechatrebot_manager/app_webui/subscription.js'), 'utf8');
const bundledContact = JSON.parse(fs.readFileSync(path.join(__dirname,
  '../src/wechatrebot_manager/licensing-contact.json'), 'utf8'));
const escapeHtml = value => String(value ?? '').replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness(overrides = {}) {
  const elements = new Map(), calls = [], intervals = [], confirmations = [], routes = [];
  const clock = {now: Date.now(), monotonic: 0}, timeouts = new Map(), documentListeners = {};
  let timerId = 0;
  function element(id) {
    if (!elements.has(id)) {
      const child = {textContent: ''};
      const attributes = {}, classes = new Set();
      const node = {id, value: '', hidden: false, disabled: false, innerHTML: '', textContent: '',
        dataset: {}, listeners: {}, lastChild: child,
        classList: {toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
          contains: name => classes.has(name)},
        querySelector: () => child, focus() { node.focused = true; }, scrollIntoView() { node.scrolled = true; },
        setAttribute: (name, value) => { attributes[name] = value; },
        getAttribute: name => attributes[name] ?? null,
        removeAttribute: name => { delete attributes[name]; },
        addEventListener: (name, fn) => { node.listeners[name] = fn; }};
      elements.set(id, node);
    }
    return elements.get(id);
  }
  const snapshot = {enabled: true, configured: true, state: 'unactivated', authorized: false,
    can_run: false, can_connect: false, account: '', current_account: '', license_id: '', device_id: '',
    plan: '', valid_until: 0, remaining_seconds: 0,
    plans: [{id: 'trial', name: '一小时试用', price_fen: 0},
      {id: 'month', name: '月付', price_fen: 6900}, {id: 'year', name: '年付', price_fen: 69900}], ...overrides};
  const contact = {state: 'ready', group_name: bundledContact.group_name,
    expires_at: Math.floor(clock.now / 1000) + 3600, remaining_seconds: 3600,
    image_data_url: `data:${bundledContact.mime_type};base64,${bundledContact.image_base64}`, source: 'bundled', message: ''};
  const handlers = {};
  const context = vm.createContext({window: {
    setInterval(fn, delay) { fn.delay = delay; intervals.push(fn); },
    setTimeout(fn, delay) { timeouts.set(++timerId, {fn, at: clock.monotonic + delay}); return timerId; },
    clearTimeout: id => timeouts.delete(id)},
    document: {querySelector: element, querySelectorAll: () => [], visibilityState: 'visible',
      addEventListener: (name, fn) => { documentListeners[name] = fn; }},
    Intl, Date: class extends Date { static now() { return clock.now; } }, Set,
    performance: {now: () => clock.monotonic}});
  vm.runInContext(script, context);
  const ui = context.window.createSubscriptionPage({escapeHtml,
    api: async (method, ...args) => {
      calls.push([method, ...args]);
      if (handlers[method]) return handlers[method](...args);
      if (method === 'get_license_status') return structuredClone(snapshot);
      if (method === 'get_license_contact') return structuredClone(contact);
      throw new Error(`Unexpected API call: ${method}`);
    },
    confirmAction: config => confirmations.push(config),
    copyText: async (text, label) => calls.push(['copy', text, label]),
    navigate: async page => routes.push(page),
    openMaintenance: async () => routes.push('maintenance'), refreshRuntime: async () => {},
  });
  return {ui, snapshot, contact, handlers, calls, element, intervals, confirmations, routes, clock,
    documentListeners,
    async advance(ms) {
      clock.now += ms;
      clock.monotonic += ms;
      for (const [id, timer] of timeouts) {
        if (timer.at <= clock.monotonic) { timeouts.delete(id); timer.fn(); }
      }
      await flush();
    },
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
  assert.equal(h.element('#licenseQrFrame').hidden, false);
  assert.equal(h.element('#licenseGroupName').textContent, 'WXRebot');
  assert.equal(h.element('#licensePrepareDevice').hidden, false);
  assert.match(h.element('#licensePlans').innerHTML, />69<\/strong>/);
  assert.match(h.element('#licensePlans').innerHTML, />699<\/strong>/);
  assert.equal(h.calls.every(([method]) => ['get_license_status', 'get_license_contact'].includes(method)), true);

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
  assert.equal(h.calls.every(([method]) => ['get_license_status', 'get_license_contact'].includes(method)), true);

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

async function wechatContactHasItsOwnRefreshCycle() {
  const h = harness();
  h.clock.now = Date.parse('2026-10-09T08:00:00Z');
  h.contact.expires_at = bundledContact.expires_at;
  let remoteContact = structuredClone(h.contact), cachedContact, fetchedAt = -Infinity;
  const downloads = [];
  h.handlers.get_license_contact = async force => {
    if (force || h.clock.monotonic - fetchedAt >= 60000) {
      downloads.push(h.clock.monotonic);
      await h.advance(200);
      cachedContact = structuredClone(remoteContact);
      fetchedAt = h.clock.monotonic;
    }
    return cachedContact === null
      ? {state: 'unavailable', image_data_url: '', source: 'remote', message: '微信群二维码尚未配置。'}
      : {...structuredClone(cachedContact), source: 'remote'};
  };
  await h.ui.initialize();
  await flush();
  assert.equal(h.element('#licenseQrImage').hidden, false);
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), h.contact.image_data_url);
  assert.match(h.element('#licenseContactExpiry').textContent, /2026年10月16日前有效（北京时间）/);
  assert.equal(h.calls.filter(([method]) => method === 'get_license_contact').length, 1);
  await h.ui.onNavigate('subscription');
  await h.ui.onNavigate('subscription');
  await h.advance(4000);
  h.intervals.find(fn => fn.delay === 4000)(); await flush();
  assert.equal(h.calls.filter(([method]) => method === 'get_license_contact').length, 1,
    'License polling and repeated navigation must not re-download the QR image');
  assert.equal(downloads.length, 1);

  h.handlers.preview_activation_code = async () => ({preview_id: 'qr-preview', plan: 'month', valid_until: 1800000000});
  await h.preview('WRB-KEEP-INPUT');
  h.element('#licensePlans').listeners.click({target: {closest: selector => selector === '[data-license-plan]' ? {} : null}});
  assert.equal(h.element('#licenseContactPanel').scrolled, true);
  assert.equal(h.element('#licenseContactPanel').focused, true);
  remoteContact = {...h.contact, group_name: 'Updated WeChat group',
    image_data_url: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII='};
  // The server response completed at 200 ms, so its cache is only 59.8 seconds
  // old when the page's first 60-second timer fires.
  await h.advance(60000 - h.clock.monotonic);
  h.intervals.find(fn => fn.delay === 60000)(); await flush();
  assert.equal(h.element('#licenseGroupName').textContent, remoteContact.group_name,
    'The first refresh must display a replacement QR despite the response-completion cache');
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), remoteContact.image_data_url);
  assert.deepEqual(downloads, [0, 60000]);
  assert.equal(h.element('#licenseCode').value, 'WRB-KEEP-INPUT');
  assert.equal(h.element('#licenseCodePreview').hidden, false);

  remoteContact = null;
  await h.advance(120000 - h.clock.monotonic);
  h.intervals.find(fn => fn.delay === 60000)(); await flush();
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), null,
    'The next refresh must remove a QR cleared on the server');
  assert.equal(h.element('#licenseQrFrame').hidden, true);
  assert.deepEqual(downloads, [0, 60000, 120000]);
  assert.equal(h.element('#licenseCode').value, 'WRB-KEEP-INPUT');
  assert.equal(h.element('#licenseCodePreview').hidden, false);

  remoteContact = {...h.contact, group_name: 'Restored WeChat group'};
  await h.click('#licenseQrRefresh');
  assert.equal(h.element('#licenseGroupName').textContent, remoteContact.group_name);
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), remoteContact.image_data_url);
  assert.equal(downloads.length, 4, 'An explicit manual refresh must fetch the latest QR');
  assert.equal(h.calls.some(([method]) => method === 'redeem_activation_code' || method === 'start_robot'), false);
}

async function wechatContactExpiryClearsTheImageAndExpansion() {
  const h = harness();
  h.contact.expires_at = h.clock.now / 1000 + 2;
  h.contact.remaining_seconds = 2;
  await h.ui.initialize();
  await h.click('#licenseQrToggle');
  assert.equal(h.element('#licenseQrToggle').getAttribute('aria-expanded'), 'true');
  assert.equal(h.element('#licenseContactPanel').classList.contains('is-expanded'), true);
  await h.advance(2001);
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), null);
  assert.equal(h.element('#licenseQrImage').hidden, true);
  assert.equal(h.element('#licenseQrFrame').hidden, true);
  assert.equal(h.element('#licenseQrToggle').hidden, true);
  assert.equal(h.element('#licenseQrToggle').getAttribute('aria-expanded'), 'false');
  assert.equal(h.element('#licenseContactPanel').classList.contains('is-expanded'), false);
  assert.match(h.element('#licenseContactMessage').textContent, /二维码已过期/);
  assert.equal(h.element('#licenseCode').disabled, false);

  h.contact.expires_at = h.clock.now / 1000 + 3600;
  h.contact.remaining_seconds = 3600;
  await h.click('#licenseQrRefresh');
  assert.equal(h.element('#licenseQrFrame').hidden, false);
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), h.contact.image_data_url);
  h.contact.state = 'expired';
  await h.click('#licenseQrRefresh');
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), null,
    'An expired API result must remove even an image with a future timestamp');

  h.contact.state = 'ready';
  h.contact.remaining_seconds = 0;
  await h.click('#licenseQrRefresh');
  assert.equal(h.element('#licenseQrFrame').hidden, true, 'A ready response with no remaining time must not display');
}

async function qrExpiryUsesElapsedTimeInsteadOfTheLocalClock() {
  const h = harness();
  h.contact.expires_at = bundledContact.expires_at;
  h.contact.remaining_seconds = 10;
  h.clock.now = (bundledContact.expires_at + 86400) * 1000;
  await h.ui.initialize();
  await h.ui.onNavigate('subscription');
  assert.equal(h.element('#licenseQrFrame').hidden, false,
    'A fast local clock must not hide a QR that the corrected backend says remains valid');
  h.clock.now += 365 * 86400000;
  h.documentListeners.visibilitychange();
  assert.equal(h.element('#licenseQrFrame').hidden, false);
  h.clock.now -= 2 * 365 * 86400000;
  await h.advance(9999);
  assert.equal(h.element('#licenseQrFrame').hidden, false);
  await h.advance(2);
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), null,
    'Moving the local clock back must not extend the corrected remaining lifetime');
  assert.equal(h.calls.filter(([method]) => method === 'get_license_contact').length, 1,
    'Wall-clock adjustments must not trigger additional contact downloads');
}

async function contactFailureDoesNotBlockAnExistingCode() {
  const h = harness();
  let releaseContact;
  h.handlers.get_license_contact = () => new Promise(resolve => { releaseContact = resolve; });
  await h.ui.initialize();
  assert.equal(h.element('#licenseQrRefresh').disabled, true);
  assert.equal(h.element('#licenseCode').disabled, false, 'A pending QR download must not block activation');
  h.handlers.preview_activation_code = async () => ({preview_id: 'pending-qr', plan: 'month', valid_until: 1800000000});
  await h.preview('WRB-EXISTING-CODE');
  assert.equal(h.element('#licenseCodePreview').hidden, false);
  h.handlers.redeem_activation_code = async () => {
    Object.assign(h.snapshot, {state: 'active_unbound', authorized: true, can_connect: true, license_id: 'new-license'});
    return structuredClone(h.snapshot);
  };
  await h.click('#licenseConfirmRedeem');
  assert.equal(h.element('#licenseCode').value, '');
  assert.equal(h.element('#licenseConnect').hidden, false);
  releaseContact({state: 'unavailable', image_data_url: '', message: '二维码正在更新，请稍后重试。'});
  await flush();
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), null);
  assert.match(h.element('#licenseContactMessage').textContent, /已有激活码仍可兑换/);
  assert.equal(h.element('#licenseFeedback').dataset.tone, 'success', 'QR failure must not overwrite redemption feedback');
  assert.equal(h.calls.some(([method]) => method === 'start_robot'), false);

  h.handlers.get_license_contact = () => { throw new Error('contact service unavailable'); };
  await h.click('#licenseQrRefresh');
  assert.equal(h.element('#licenseQrRefresh').disabled, false);
  assert.equal(h.element('#licenseCode').disabled, false);
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), null);
}

async function invalidOrBrokenQrImagesAreHidden() {
  const h = harness();
  h.contact.image_data_url = 'https://unexpected.example/qr.png';
  await h.ui.initialize();
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), null);
  h.contact.image_data_url = 'data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=';
  await h.click('#licenseQrRefresh');
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), null);
  h.contact.image_data_url = `data:${bundledContact.mime_type};base64,${bundledContact.image_base64}`;
  await h.click('#licenseQrRefresh');
  await h.click('#licenseQrToggle');
  h.element('#licenseQrImage').listeners.error();
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), null);
  assert.equal(h.element('#licenseQrToggle').hidden, true);
  assert.equal(h.element('#licenseContactPanel').classList.contains('is-expanded'), false);
  assert.match(h.element('#licenseContactMessage').textContent, /二维码暂时无法显示/);
}

async function validBundledFallbackShowsTheRefreshWarning() {
  const h = harness();
  h.contact.message = '二维码刷新失败，当前显示安装包内置的有效微信群二维码。<b>稍后可重试</b>';
  await h.ui.initialize();
  assert.equal(h.element('#licenseContactMessage').textContent, h.contact.message);
  assert.equal(h.element('#licenseContactMessage').innerHTML, '');
  assert.equal(h.element('#licenseQrImage').getAttribute('src'), h.contact.image_data_url);
  assert.equal(h.element('#licenseQrToggle').hidden, false);
  assert.equal(h.element('#licenseCode').disabled, false);
}

function runCenterCanStartWithBridgeConnected() {
  const app = require('./js/webui_source.cjs').managerScript(path.join(__dirname, '../src/wechatrebot_manager/app_webui'));
  const start = app.indexOf('  function renderStatus(');
  const end = app.indexOf('  async function refreshStatus(', start);
  assert.ok(start >= 0 && end > start);
  const h = harness();
  const context = vm.createContext({state: {historyAccount: 'wxid_a', historyReadyAccount: 'wxid_a', activePage: 'home', mandatoryUpdate: false},
    $: h.element, subscription: {updateRuntime() {}}, contactProfile: {updateAccount() {}},
    reconcileStartPending: status => status,
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

function clickTrial(h) {
  return h.element('#licensePlans').listeners.click({target: {
    closest: selector => selector === '[data-free-trial]' ? {} : null,
  }});
}

function retryTrial(h) {
  return h.element('#licensePlans').listeners.click({target: {
    closest: selector => selector === '[data-trial-retry]' ? {} : null,
  }});
}

async function freeTrialRequiresLiveAccountAndQueriesOnlyWhenNeeded() {
  const h = harness({can_connect: true});
  await h.ui.initialize();
  await h.ui.onNavigate('subscription');
  assert.equal(h.element('#licenseConnect').hidden, false, 'An unactivated install must allow connecting WeChat');
  assert.equal(h.element('#licenseActivateTrial').disabled, true);
  assert.match(h.element('#licenseTrialStatus').textContent, /请先连接微信/);
  assert.equal(h.calls.some(([method]) => method === 'get_trial_status'), false);

  h.handlers.get_trial_status = async () => ({free_trial: {
    state: 'eligible', eligible: true, recoverable: false, account: h.snapshot.current_account,
  }});
  h.snapshot.current_account = 'wxid_trial_a';
  h.intervals.find(fn => fn.delay === 4000)(); await flush();
  assert.equal(h.element('#licenseActivateTrial').disabled, false);
  assert.equal(h.element('#licenseActivateTrial').textContent, '激活免费试用');
  assert.equal(h.element('#licenseCurrentAccountRow').hidden, false);
  await h.ui.onNavigate('subscription');
  h.intervals.find(fn => fn.delay === 4000)(); await flush();
  assert.equal(h.calls.filter(([method]) => method === 'get_trial_status').length, 1,
    'Repeated navigation and the four-second local status poll must not query trial eligibility');

  let release;
  h.handlers.activate_free_trial = async account => {
    assert.equal(account, 'wxid_trial_a');
    await new Promise(resolve => { release = resolve; });
    Object.assign(h.snapshot, {state: 'ready', authorized: true, can_connect: true,
      license_id: 'trial-license', account, plan: 'trial', valid_until: 1800000000, remaining_seconds: 3600});
    return structuredClone(h.snapshot);
  };
  const pending = clickTrial(h);
  await clickTrial(h);
  assert.equal(h.element('#licenseActivateTrial').disabled, true);
  assert.equal(h.calls.filter(([method]) => method === 'activate_free_trial').length, 1);
  assert.equal(h.confirmations.length, 0, 'Self-service trial activation must not show a confirmation dialog');
  release(); await pending;
  assert.equal(h.element('#licenseActivateTrial').textContent, '试用进行中');
  assert.match(h.element('#licenseTrialStatus').textContent, /免费试用进行中，结束时间/);
  assert.equal(h.calls.some(([method]) => method === 'start_robot'), false, 'Trial activation must not start automation');
  assert.equal(h.calls.filter(([method]) => method === 'get_trial_status').length, 2);

  Object.assign(h.snapshot, {state: 'expired', authorized: false, remaining_seconds: 0});
  h.intervals.find(fn => fn.delay === 4000)(); await flush();
  assert.equal(h.element('#licenseTrialStatus').textContent, '你的试用已结束，可联系群主购买');
  assert.equal(h.element('#licenseActivateTrial').disabled, true);
  assert.equal(h.calls.filter(([method]) => method === 'get_trial_status').length, 2,
    'Signed local expiry must render without another remote eligibility query');
}

async function freeTrialStatesFailuresAndAccountChanges() {
  for (const trialState of ['used', 'ended', 'paid']) {
    const h = harness({current_account: 'wxid_trial'});
    h.handlers.get_trial_status = () => ({free_trial: {state: trialState, eligible: false, recoverable: false}});
    await h.ui.initialize(); await h.ui.onNavigate('subscription');
    assert.equal(h.element('#licenseActivateTrial').disabled, true);
    assert.equal(h.element('#licenseTrialStatus').textContent, trialState === 'paid'
      ? '当前已有付费授权。' : trialState === 'used' ? '当前设备或微信账号已领取过试用，可联系群主购买'
        : '你的试用已结束，可联系群主购买');
    await clickTrial(h);
    assert.equal(h.calls.some(([method]) => method === 'activate_free_trial'), false);
  }

  const h = harness({current_account: 'wxid_trial_a'});
  h.handlers.get_trial_status = () => { throw new Error('试用服务暂不可达'); };
  await h.ui.initialize(); await h.ui.onNavigate('subscription');
  assert.equal(h.element('#licenseActivateTrial').disabled, true);
  assert.equal(h.element('#licenseTrialRetry').hidden, false);
  assert.equal(h.element('#licenseTrialStatus').textContent, '试用服务暂不可达');
  h.intervals.find(fn => fn.delay === 4000)(); await flush();
  assert.equal(h.calls.filter(([method]) => method === 'get_trial_status').length, 1);

  h.handlers.get_trial_status = () => ({free_trial: {state: 'active', eligible: false,
    recoverable: true, account: 'wxid_trial_a', valid_until: 1800000000, remaining_seconds: 500}});
  await retryTrial(h);
  assert.equal(h.element('#licenseActivateTrial').textContent, '恢复免费试用');
  assert.equal(h.element('#licenseActivateTrial').disabled, false);
  h.handlers.activate_free_trial = () => { throw new Error('微信登录账号已变化'); };
  await clickTrial(h);
  assert.match(h.element('#licenseFeedback').textContent, /微信登录账号已变化/);
  assert.equal(h.element('#licenseFeedback').dataset.tone, 'error');

  let releaseOld, releaseNew;
  h.snapshot.current_account = 'wxid_trial_b';
  h.handlers.get_trial_status = () => new Promise(resolve => { releaseOld = resolve; });
  h.intervals.find(fn => fn.delay === 4000)(); await flush();
  assert.equal(h.element('#licenseActivateTrial').disabled, true);
  h.snapshot.current_account = 'wxid_trial_c';
  h.handlers.get_trial_status = () => new Promise(resolve => { releaseNew = resolve; });
  h.intervals.find(fn => fn.delay === 4000)(); await flush();
  releaseOld({free_trial: {state: 'eligible', eligible: true, account: 'wxid_trial_b'}}); await flush();
  assert.equal(h.element('#licenseActivateTrial').disabled, true, 'A response for the previous account must not restore eligibility');
  releaseNew({free_trial: {state: 'used', eligible: false, account: 'wxid_trial_c'}}); await flush();
  assert.equal(h.element('#licenseTrialStatus').textContent, '当前设备或微信账号已领取过试用，可联系群主购买');
  assert.equal(h.element('#licenseActivateTrial').disabled, true);
}

async function paidRedemptionInvalidatesOutstandingTrialQuery() {
  const h = harness({current_account: 'wxid_trial'});
  let release;
  h.handlers.get_trial_status = () => new Promise(resolve => { release = resolve; });
  await h.ui.initialize();
  const pending = h.ui.onNavigate('subscription');
  await flush();
  h.handlers.preview_activation_code = () => ({preview_id: 'paid', plan: 'month', valid_until: 1800000000});
  await h.preview('PAID-CODE');
  h.handlers.redeem_activation_code = () => {
    Object.assign(h.snapshot, {state: 'ready', authorized: true, account: 'wxid_trial',
      license_id: 'paid-license', plan: 'month', valid_until: 1800000000});
    return structuredClone(h.snapshot);
  };
  await h.click('#licenseConfirmRedeem');
  release({free_trial: {state: 'eligible', eligible: true, account: 'wxid_trial'}});
  await pending; await flush();
  assert.equal(h.element('#licenseTrialStatus').textContent, '当前已有付费授权。');
  assert.equal(h.element('#licenseActivateTrial').disabled, true);
  await clickTrial(h);
  assert.equal(h.calls.some(([method]) => method === 'activate_free_trial'), false);

  const ended = harness({state: 'expired', plan: 'trial', license_id: 'trial-license',
    account: 'wxid_old', current_account: ''});
  await ended.ui.initialize();
  assert.equal(ended.element('#licenseTrialStatus').textContent, '你的试用已结束，可联系群主购买');
}

(async () => {
  await activationAndRecovery();
  await expiryAndUnconfigured();
  await rejectedStartIsNotReportedAsSuccess();
  await wechatContactHasItsOwnRefreshCycle();
  await wechatContactExpiryClearsTheImageAndExpansion();
  await qrExpiryUsesElapsedTimeInsteadOfTheLocalClock();
  await contactFailureDoesNotBlockAnExistingCode();
  await invalidOrBrokenQrImagesAreHidden();
  await validBundledFallbackShowsTheRefreshWarning();
  await freeTrialRequiresLiveAccountAndQueriesOnlyWhenNeeded();
  await freeTrialStatesFailuresAndAccountChanges();
  await paidRedemptionInvalidatesOutstandingTrialQuery();
  runCenterCanStartWithBridgeConnected();
  process.stdout.write('Subscription UI activation, expiry, confirmation and WeChat QR checks passed.\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
