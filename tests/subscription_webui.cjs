const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname,
  '../src/wechatrebot_manager/app_webui/subscription.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(__dirname,
  '../src/wechatrebot_manager/app_webui/index.html'), 'utf8');
const bundledContact = JSON.parse(fs.readFileSync(path.join(__dirname,
  '../src/wechatrebot_manager/licensing-contact.json'), 'utf8'));
const escapeHtml = value => String(value ?? '').replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness(overrides = {}) {
  const elements = new Map(), calls = [], intervals = [], confirmations = [], routes = [], toasts = [];
  const clock = {now: Date.now(), monotonic: 0}, timeouts = new Map(), documentListeners = {};
  let timerId = 0;
  function element(id) {
    if (!elements.has(id)) {
      const child = {textContent: '', getAttribute: () => null};
      const attributes = {}, classes = new Set();
      const node = {id, value: '', hidden: false, disabled: false, innerHTML: '', textContent: '',
        dataset: {}, listeners: {}, lastChild: child, style: {},
        classList: {toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
          add: name => classes.add(name), remove: name => classes.delete(name),
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
  const bindingStatus = {revision: 17, rebind_limit: 5, rebind_used: 2, rebind_remaining: 3, can_rebind: true};
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
      if (method === 'get_license_binding_status') {
        const plan = snapshot.plan?.id || snapshot.plan;
        return structuredClone({...bindingStatus, license_id: snapshot.license_id,
          account: snapshot.account, plan,
          ...(plan === 'trial' ? {rebind_limit: 0, rebind_used: 0, rebind_remaining: 0, can_rebind: false} : {})});
      }
      throw new Error(`Unexpected API call: ${method}`);
    },
    confirmAction: config => new Promise(resolve => {
      const dialog = {...config, settled: false, busy: false,
        async confirm() {
          if (dialog.settled || dialog.busy) return;
          dialog.busy = true;
          try {
            await config.onConfirm?.();
            dialog.settled = true;
            resolve(true);
          } finally { dialog.busy = false; }
        },
        async cancel() {
          if (dialog.settled || dialog.busy) return;
          dialog.settled = true;
          try { await config.onCancel?.(); } finally { resolve(false); }
        },
      };
      confirmations.push(dialog);
    }),
    copyText: async (text, label) => calls.push(['copy', text, label]),
    navigate: async page => routes.push(page),
    toast: (message, type) => toasts.push({message, type}),
    openMaintenance: async () => routes.push('maintenance'), refreshRuntime: async () => {},
  });
  return {ui, snapshot, contact, bindingStatus, handlers, calls, element, intervals, confirmations, routes, toasts, clock,
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
  assert.equal(await h.ui.beforeStart(), false);
  assert.deepEqual(h.toasts.at(-1), {message: '当前未授权。请激活免费试用或兑换付费激活码，再启动机器人。', type: 'error'});
  assert.equal(h.routes.at(-1), 'subscription');
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
  assert.equal(h.element('#licenseOptions').open, false);
  assert.equal(h.ui.needsEntry(), false, 'A valid unbound lease is confirmed from the run center');
  assert.equal(h.calls.some(([method]) => method === 'start_robot'), false);

  Object.assign(h.snapshot, {state: 'ready', account: 'wxid_account_a', current_account: 'wxid_account_a'});
  await h.ui.onNavigate('subscription');
  assert.equal(h.element('#licenseStateTitle').textContent, '订阅已生效');
  assert.equal(h.element('#licenseStatusPanel').dataset.tone, 'success');
  assert.equal(h.element('#licensePausedInfo').hidden, true);
  assert.equal(await h.ui.beforeStart(), true, 'A valid matching lease need not already have a run grant');
  assert.equal(h.confirmations.length, 0, 'An already-bound matching account requires no repeated consent');
  assert.equal(h.calls.some(([method]) => method === 'start_robot'), false);
}

async function validSubscriptionStaysActiveWhenStoppedOrDisconnected() {
  for (const current_account of ['', 'wxid_account_a']) {
    const h = harness({state: 'paused', license_id: 'license-one', authorized: true,
      can_connect: true, account: 'wxid_account_a', current_account, plan: 'year',
      remaining_seconds: 86400 * 300, message: '授权有效，自动化已暂停；请手动启动恢复。'});
    await h.ui.initialize();
    assert.equal(h.ui.needsEntry(), false, 'A valid subscription does not require a live connection to enter home');
    assert.equal(h.element('#homeLicenseNotice').hidden, true);
    assert.equal(h.element('#licenseStatusPanel').dataset.tone, 'success');
    assert.equal(h.element('#licenseStateTitle').textContent, '订阅已生效');
    assert.equal(h.element('#licenseStateTag').textContent, '已订阅');
    assert.equal(h.element('#licenseRedeemTitle').textContent, '兑换续期码');
    assert.doesNotMatch(h.element('#licenseStateMessage').textContent, /暂停|恢复/);
    assert.equal(h.element('#licenseOptions').open, false);
    h.handlers.connect_license_wechat = () => {
      h.snapshot.current_account = 'wxid_account_a';
      return {accepted: true};
    };
    assert.equal(await h.ui.beforeStart(), true, 'The backend reconnects and verifies the actual account before granting a run');
    assert.equal(h.confirmations.length, 0);
    assert.equal(h.calls.filter(([method]) => method === 'connect_license_wechat').length, current_account ? 0 : 1);
    assert.equal(h.calls.some(([method]) => method === 'start_robot'), false);
  }
  const mismatch = harness({state: 'account_mismatch', license_id: 'license-one', authorized: true,
    can_connect: true, account: 'wxid_account_a', current_account: 'wxid_account_b'});
  await mismatch.ui.initialize();
  assert.equal(mismatch.ui.needsEntry(), true);
  assert.equal(await mismatch.ui.beforeStart(), false);
  assert.match(mismatch.toasts.at(-1).message, /不一致/);
  assert.equal(mismatch.element('#licenseStatusPanel').dataset.tone, 'warning');
  assert.equal(mismatch.element('#licensePausedInfo').hidden, false);
  const offline = harness({state: 'paused', license_id: 'license-one', authorized: true,
    can_connect: true, account: 'wxid_account_a', current_account: '', plan: 'year',
    remaining_seconds: 86400 * 300, last_error: 'service_unavailable',
    message: '授权有效，自动化已暂停；请手动启动恢复。 授权服务暂不可达，当前有效离线凭证仍可使用。'});
  await offline.ui.initialize();
  assert.equal(offline.element('#homeLicenseNotice').hidden, false);
  assert.equal(offline.element('#homeLicenseTitle').textContent, '当前使用离线授权');
  assert.doesNotMatch(offline.element('#homeLicenseMessage').textContent, /暂停|恢复/);
  assert.match(offline.element('#licenseStateMessage').textContent, /离线凭证/);
}

async function revokedSubscriptionCanConfirmNewPaidActivationWithoutStarting() {
  for (const state of ['revoked', 'license_revoked']) {
    const h = harness({state, license_id: 'revoked-year', plan: 'year',
      account: 'wxid_account_a', current_account: 'wxid_account_a'});
    await h.ui.initialize();
    assert.equal(h.element('#licenseRedeemTitle').textContent, '兑换激活码');
    h.handlers.preview_activation_code = async () => ({preview_id: 'new-month', plan: 'month', mode: 'new',
      valid_until: 1800000000, license_id: '', already_redeemed: false,
      message: '旧授权保持撤销；确认后新开通授权，从本次兑换起算，不继承旧授权的剩余时长。兑换后需重新绑定微信并手动恢复机器人。'});
    await h.preview('NEW-MONTH-CODE');
    assert.equal(h.element('#licenseCodePreview').hidden, false);
    assert.match(h.element('#licenseCodePreviewDetails').innerHTML, /月付/);
    assert.match(h.element('#licenseCodePreviewDetails').innerHTML, /新开通/);
    assert.match(h.element('#licenseCodePreviewMessage').textContent, /旧授权保持撤销/);
    assert.match(h.element('#licenseCodePreviewMessage').textContent, /从本次兑换起算/);
    assert.match(h.element('#licenseCodePreviewMessage').textContent, /手动恢复机器人/);
    assert.equal(h.calls.some(([method]) => method === 'redeem_activation_code'), false);
    h.handlers.redeem_activation_code = async () => {
      Object.assign(h.snapshot, {state: 'active_unbound', license_id: 'new-month', plan: 'month',
        authorized: true, can_connect: true, account: ''});
      return structuredClone(h.snapshot);
    };
    await h.click('#licenseConfirmRedeem');
    assert.equal(h.calls.filter(([method]) => method === 'redeem_activation_code').length, 1);
    assert.equal(h.calls.some(([method]) => method === 'start_robot'), false);
    assert.equal(h.element('#licenseOptions').open, false);
    assert.match(h.element('#licenseFeedback').textContent, /运行中心.*首次启动.*先确认绑定提示，再登录微信/);
  }
}

async function expiryAndUnconfigured() {
  const h = harness({state: 'expired', license_id: 'license-one', account: 'wxid_account_a',
    current_account: 'wxid_account_a', message: '订阅已到期，自动化已暂停。'});
  await h.ui.initialize();
  assert.equal(h.element('#licensePausedInfo').hidden, false);
  assert.equal(h.element('#licenseRedeemTitle').textContent, '兑换续期码');
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
  assert.equal(h.element('#licenseOptions').open, false);
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

function startupHarness(overrides = {}) {
  return harness({state: 'active_unbound', license_id: 'license-one', plan: 'month',
    authorized: true, can_connect: true, current_account: 'wxid_account_a', ...overrides});
}

async function startupConsentPrecedesAccountConnection() {
  const h = startupHarness({current_account: ''});
  await h.ui.initialize();
  const pending = h.ui.beforeStart();
  await flush();
  assert.equal(h.confirmations.length, 1);
  const dialog = h.confirmations[0];
  assert.equal(dialog.title, '确认绑定当前套餐');
  assert.equal(dialog.subject, undefined, 'Consent before login must not predict a WeChat account');
  assert.equal(dialog.confirmText, '同意并继续');
  assert.match(dialog.message, /接下来登录的微信号将绑定当前套餐/);
  assert.equal(h.calls.some(([method]) => method === 'get_license_binding_status'), true,
    'The first binding consent must read the server binding policy');
  assert.equal(h.calls.some(([method]) => ['connect_license_wechat', 'bind_license_account', 'start_robot'].includes(method)), false);
  assert.equal(await h.ui.beforeStart(), false, 'A pending confirmation must reject a duplicate start attempt');
  assert.equal(h.confirmations.length, 1);
  h.handlers.connect_license_wechat = () => {
    h.snapshot.current_account = 'wxid_account_a';
    return {accepted: true};
  };
  h.handlers.bind_license_account = account => {
    assert.equal(account, 'wxid_account_a');
    Object.assign(h.snapshot, {state: 'ready', account});
    return structuredClone(h.snapshot);
  };
  await dialog.confirm();
  assert.equal(await pending, true);
  assert.equal(h.calls.filter(([method]) => method === 'bind_license_account').length, 1);
  assert.equal(h.calls.some(([method]) => method === 'start_robot'), false,
    'The subscription gate returns permission; only the run center starts the robot');
  assert.equal(await h.ui.beforeStart(), true);
  assert.equal(h.confirmations.length, 1, 'A bound matching account must not ask again');
  assert.equal(h.element('#licenseRefresh').disabled, false);

  const canceled = startupHarness({current_account: ''});
  await canceled.ui.initialize();
  const stopped = canceled.ui.beforeStart();
  await flush();
  await canceled.confirmations[0].cancel();
  assert.equal(await stopped, false);
  assert.equal(canceled.calls.some(([method]) => ['connect_license_wechat', 'bind_license_account', 'start_robot'].includes(method)), false);
  assert.equal(canceled.element('#licenseRefresh').disabled, false);
}

async function startupBindingQuotaMustBeLiveAndFailClosed() {
  const h = startupHarness({current_account: ''});
  await h.ui.initialize();
  const initialReads = h.calls.filter(([method]) => method === 'get_license_binding_status').length;
  const pending = h.ui.beforeStart();
  await flush();
  assert.ok(h.calls.filter(([method]) => method === 'get_license_binding_status').length > initialReads);
  assert.match(h.confirmations[0].message, /剩余(?:可换绑)?\s*3\s*次/);
  await h.confirmations[0].cancel();
  assert.equal(await pending, false);
  Object.assign(h.bindingStatus, {revision: 18, rebind_used: 4, rebind_remaining: 1});
  const retried = h.ui.beforeStart();
  await flush();
  assert.match(h.confirmations[1].message, /剩余(?:可换绑)?\s*1\s*次/,
    'Each attempt must display the latest response rather than cached quota');
  await h.confirmations[1].cancel();
  assert.equal(await retried, false);

  h.handlers.get_license_binding_status = () => { throw new Error('换绑次数读取失败'); };
  await assert.rejects(h.ui.beforeStart(), /换绑次数读取失败/);
  assert.equal(h.confirmations.length, 2, 'Unavailable quota must not offer consent with invented counts');
  assert.equal(h.calls.some(([method]) => ['connect_license_wechat', 'bind_license_account', 'start_robot'].includes(method)), false);
  assert.equal(h.element('#licenseRefresh').disabled, false);

  const mismatched = startupHarness({current_account: ''});
  await mismatched.ui.initialize();
  mismatched.handlers.get_license_binding_status = () => ({...mismatched.bindingStatus,
    license_id: mismatched.snapshot.license_id, plan: mismatched.snapshot.plan, account: 'wxid_old_binding'});
  await assert.rejects(mismatched.ui.beforeStart(), /授权或换绑次数已变化/);
  assert.equal(mismatched.confirmations.length, 0,
    'An old binding returned for an unbound lease must not be presented as the next actual account');
  assert.equal(mismatched.calls.some(([method]) => ['connect_license_wechat', 'bind_license_account', 'start_robot'].includes(method)), false);
}

async function startupConnectionFailuresAndLoginCancellation() {
  for (const response of [{accepted: false, message: '微信连接暂未开始'}, new Error('连接请求失败')]) {
    const h = startupHarness({current_account: ''});
    await h.ui.initialize();
    h.handlers.connect_license_wechat = () => {
      if (response instanceof Error) throw response;
      return response;
    };
    const pending = h.ui.beforeStart();
    const rejected = assert.rejects(pending, /连接暂未开始|连接请求失败/);
    await flush();
    assert.equal(h.calls.some(([method]) => method === 'connect_license_wechat'), false);
    await h.confirmations[0].confirm();
    await rejected;
    assert.equal(h.confirmations.length, 1);
    assert.equal(h.calls.some(([method]) => ['bind_license_account', 'start_robot'].includes(method)), false);
    assert.equal(h.element('#licenseRefresh').disabled, false);
  }

  const h = startupHarness({current_account: ''});
  await h.ui.initialize();
  h.handlers.connect_license_wechat = () => ({accepted: true});
  const pending = h.ui.beforeStart();
  await flush();
  await h.confirmations[0].cancel();
  assert.equal(await pending, false);
  assert.equal(h.calls.some(([method]) => method === 'connect_license_wechat'), false);

  const waiting = h.ui.beforeStart();
  await flush();
  await h.confirmations[1].confirm();
  await flush();
  assert.equal(h.confirmations[2].title, '请登录微信');
  assert.equal(h.confirmations[2].subject, undefined, 'No cached account may be offered for binding');
  await assert.rejects(h.confirmations[2].confirm(), /尚未读取到登录的微信账号/);
  assert.equal(h.confirmations[2].settled, false, 'An unsuccessful login check leaves cancellation available');
  await h.confirmations[2].cancel();
  assert.equal(await waiting, false);
  assert.equal(h.calls.some(([method]) => ['bind_license_account', 'start_robot'].includes(method)), false);
  assert.equal(h.element('#licenseRefresh').disabled, false);

  h.handlers.connect_license_wechat = () => {
    h.snapshot.current_account = 'wxid_account_a';
    return {accepted: true};
  };
  const retried = h.ui.beforeStart();
  await flush();
  assert.equal(h.confirmations[3].title, '确认绑定当前套餐');
  await h.confirmations[3].cancel();
  assert.equal(await retried, false, 'Cancellation must release the gate for another attempt');
}

async function startupRejectsIdentityOrLicenseChanges() {
  for (const change of [
    {license_id: 'license-two'},
    {account: 'wxid_account_b'},
    {state: 'revoked', authorized: false},
  ]) {
    const h = startupHarness();
    await h.ui.initialize();
    const pending = h.ui.beforeStart();
    await flush();
    Object.assign(h.snapshot, change);
    await assert.rejects(h.confirmations[0].confirm(), /授权状态已变化/);
    assert.equal(h.calls.some(([method]) => ['connect_license_wechat', 'bind_license_account', 'start_robot'].includes(method)), false);
    await h.confirmations[0].cancel();
    assert.equal(await pending, false);
    assert.equal(h.element('#licenseRefresh').disabled, false);
  }

  const h = startupHarness();
  await h.ui.initialize();
  const pending = h.ui.beforeStart();
  const rejected = assert.rejects(pending, /账号或授权已变化/);
  await flush();
  h.handlers.bind_license_account = account => {
    Object.assign(h.snapshot, {account, current_account: 'wxid_account_b'});
    return structuredClone(h.snapshot);
  };
  await h.confirmations[0].confirm();
  await rejected;
  assert.equal(h.calls.some(([method]) => method === 'start_robot'), false,
    'A switch during the remote bind must be rejected by the final fresh read');

  const failed = startupHarness();
  await failed.ui.initialize();
  failed.handlers.bind_license_account = () => { throw new Error('绑定请求失败'); };
  const attempted = failed.ui.beforeStart();
  const bindingFailure = assert.rejects(attempted, /绑定请求失败/);
  await flush();
  await failed.confirmations[0].confirm();
  await bindingFailure;
  assert.equal(failed.confirmations[0].settled, true, 'Binding happens after consent has completed');
  assert.equal(failed.element('#licenseRefresh').disabled, false);
  const retried = failed.ui.beforeStart();
  await flush();
  await failed.confirmations[1].cancel();
  assert.equal(await retried, false);
}

async function startupFreshReadCannotRaceAnotherEntry() {
  const h = startupHarness({account: 'wxid_account_a'});
  await h.ui.initialize();
  let release;
  h.handlers.get_license_status = () => new Promise(resolve => { release = resolve; });
  const pending = h.ui.beforeStart();
  await flush();
  assert.equal(await h.ui.beforeStart(), false, 'The first fresh read must hold the gate');
  await clickTrial(h);
  assert.equal(h.confirmations.length, 0);
  h.handlers.get_license_status = () => structuredClone(h.snapshot);
  release(structuredClone(h.snapshot));
  assert.equal(await pending, true);

  h.handlers.get_license_status = () => { throw new Error('实时账号读取失败'); };
  await assert.rejects(h.ui.beforeStart(), /实时账号读取失败/);
  assert.equal(h.element('#licenseRefresh').disabled, false);
  assert.equal(h.calls.some(([method]) => ['bind_license_account', 'start_robot'].includes(method)), false);
}

async function subscriptionOptionsFollowEntitlementAndPreserveExpansion() {
  for (const id of ['licenseConnect', 'licenseBind', 'licenseResume', 'licenseSteps', 'licenseCurrentAccountRow']) {
    assert.doesNotMatch(indexHtml, new RegExp(`id=["']${id}["']`), `${id} must not remain in the subscription page`);
    assert.doesNotMatch(script, new RegExp(`#${id}\\b`), `${id} must have no stale renderer or listener`);
  }
  const h = startupHarness();
  await h.ui.initialize();
  assert.equal(h.element('#licenseOptions').open, false);
  assert.equal(h.element('#licenseOptionsTitle').textContent, '续期或更换套餐');
  h.element('#licenseOptions').open = true;
  await h.ui.onNavigate('subscription');
  assert.equal(h.element('#licenseOptions').open, true, 'A background refresh must preserve user expansion');
  Object.assign(h.snapshot, {state: 'expired', authorized: false});
  await h.ui.onNavigate('subscription');
  assert.equal(h.element('#licenseOptions').open, true);
  assert.equal(h.element('#licenseOptionsTitle').textContent, '激活授权');
}

async function rebindConsentCanBeRefusedWithoutRuntimeChanges() {
  const h = startupHarness({state: 'ready', account: 'wxid_account_a'});
  await h.ui.initialize();
  await h.ui.onNavigate('subscription');
  assert.equal(h.element('#licenseRebind').hidden, false);
  assert.equal(h.element('#licenseRebind').disabled, false);
  assert.equal(h.element('#licenseRebindQuota').textContent, '3');
  assert.match(h.element('#licenseRebindQuota').getAttribute('aria-label'), /剩余可换绑\s*3\s*次/);
  const pending = h.click('#licenseRebind');
  await flush();
  const dialog = h.confirmations[0];
  assert.equal(dialog.title, '更换套餐绑定微信');
  assert.equal(dialog.subject.value, 'wxid_account_a');
  assert.match(dialog.message, /接下来登录的新微信号将绑定当前套餐/);
  assert.match(dialog.message, /剩余可换绑\s*3\s*次/);
  assert.equal(h.calls.some(([method]) => ['stop_robot', 'connect_license_wechat', 'rebind_license_account', 'start_robot'].includes(method)), false);
  await dialog.cancel();
  await pending;
  assert.equal(h.calls.some(([method]) => ['stop_robot', 'connect_license_wechat', 'rebind_license_account', 'start_robot'].includes(method)), false);
  assert.equal(h.bindingStatus.rebind_remaining, 3);
  assert.equal(h.element('#licenseRebind').disabled, false);
}

async function rebindWaitsForADifferentActualAccountAndDoesNotStart() {
  const h = startupHarness({state: 'ready', account: 'wxid_account_a'});
  await h.ui.initialize();
  await h.ui.onNavigate('subscription');
  h.handlers.stop_robot = () => ({accepted: true});
  h.handlers.rebind_license_account = (account, previous, revision) => {
    assert.equal(account, 'wxid_account_b');
    assert.equal(previous, 'wxid_account_a');
    assert.equal(revision, 17);
    Object.assign(h.snapshot, {account, current_account: account, can_run: false});
    Object.assign(h.bindingStatus, {revision: 18, rebind_used: 3, rebind_remaining: 2});
    return structuredClone(h.snapshot);
  };
  const pending = h.click('#licenseRebind');
  await flush();
  await h.confirmations[0].confirm();
  await flush();
  assert.equal(h.calls.filter(([method]) => method === 'stop_robot').length, 1);
  assert.equal(h.confirmations[1].title, '请切换微信账号');
  await assert.rejects(h.confirmations[1].confirm(), /仍是原绑定微信/);
  assert.equal(h.calls.some(([method]) => method === 'rebind_license_account'), false);
  assert.equal(h.bindingStatus.rebind_remaining, 3);
  h.snapshot.current_account = 'wxid_account_b';
  await h.confirmations[1].confirm();
  await pending;
  assert.equal(h.calls.filter(([method]) => method === 'rebind_license_account').length, 1);
  assert.equal(h.calls.some(([method]) => method === 'start_robot'), false);
  assert.match(h.element('#licenseFeedback').textContent, /换绑已完成.*保持停止.*运行中心/);
  assert.equal(h.element('#licenseRebindQuota').textContent, '2');
  assert.equal(h.element('#licenseRebind').disabled, false);

  const canceled = startupHarness({state: 'ready', account: 'wxid_account_a'});
  await canceled.ui.initialize();
  canceled.handlers.stop_robot = () => ({accepted: true});
  const waiting = canceled.click('#licenseRebind');
  await flush();
  await canceled.confirmations[0].confirm();
  await flush();
  await canceled.confirmations[1].cancel();
  await waiting;
  assert.equal(canceled.calls.some(([method]) => ['bind_license_account', 'rebind_license_account', 'start_robot'].includes(method)), false);
  assert.equal(canceled.bindingStatus.rebind_remaining, 3);
  assert.equal(canceled.element('#licenseRefresh').disabled, false);
}

async function rebindInvalidQuotaOrChangedRevisionBlocksRuntimeActions() {
  for (const failure of ['quota', 'read', 'trial']) {
    const h = startupHarness({state: 'ready', account: 'wxid_account_a', plan: failure === 'trial' ? 'trial' : 'month'});
    await h.ui.initialize();
    if (failure === 'quota') Object.assign(h.bindingStatus, {rebind_used: 5, rebind_remaining: 0, can_rebind: false});
    if (failure === 'read') h.handlers.get_license_binding_status = () => { throw new Error('换绑次数读取失败'); };
    await h.ui.onNavigate('subscription');
    assert.equal(h.element('#licenseRebind').hidden, failure === 'trial');
    await h.click('#licenseRebind');
    assert.equal(h.confirmations.length, 0);
    assert.equal(h.calls.some(([method]) => ['stop_robot', 'connect_license_wechat', 'rebind_license_account', 'start_robot'].includes(method)), false);
    assert.match(h.element('#licenseFeedback').textContent, /次数不足|不支持换绑|换绑次数读取失败/);
    assert.equal(h.element('#licenseRefresh').disabled, false);
  }

  for (const change of [{revision: 18}, {rebind_remaining: 0, can_rebind: false}]) {
    const h = startupHarness({state: 'ready', account: 'wxid_account_a'});
    await h.ui.initialize();
    const pending = h.click('#licenseRebind');
    await flush();
    Object.assign(h.bindingStatus, change);
    await assert.rejects(h.confirmations[0].confirm(), /授权或换绑次数已变化/);
    await h.confirmations[0].cancel();
    await pending;
    assert.equal(h.calls.some(([method]) => ['stop_robot', 'connect_license_wechat', 'rebind_license_account', 'start_robot'].includes(method)), false);
  }
}

async function rebindStopOrWriteFailurePreservesTheOldBinding() {
  const stopped = startupHarness({state: 'ready', account: 'wxid_account_a'});
  await stopped.ui.initialize();
  stopped.handlers.stop_robot = () => ({accepted: false, message: '正在停止，请稍后重试'});
  const rejected = stopped.click('#licenseRebind');
  await flush();
  await stopped.confirmations[0].confirm();
  await rejected;
  assert.match(stopped.element('#licenseFeedback').textContent, /正在停止/);
  assert.equal(stopped.calls.some(([method]) => ['connect_license_wechat', 'rebind_license_account', 'start_robot'].includes(method)), false);
  assert.equal(stopped.element('#licenseRefresh').disabled, false);

  const h = startupHarness({state: 'ready', account: 'wxid_account_a'});
  await h.ui.initialize();
  h.handlers.stop_robot = () => ({accepted: true});
  h.handlers.rebind_license_account = () => { throw new Error('换绑请求失败'); };
  const pending = h.click('#licenseRebind');
  await flush();
  await h.confirmations[0].confirm();
  await flush();
  h.snapshot.current_account = 'wxid_account_b';
  await h.confirmations[1].confirm();
  await pending;
  assert.match(h.element('#licenseFeedback').textContent, /换绑请求失败/);
  assert.equal(h.snapshot.account, 'wxid_account_a');
  assert.equal(h.bindingStatus.rebind_remaining, 3);
  assert.equal(h.calls.some(([method]) => method === 'start_robot'), false);
  assert.equal(h.element('#licenseRebind').disabled, false);
}

async function authorizationMessagesExplainTheReasonAcrossEveryEntry() {
  const cases = [
    ['revoked', /授权已撤销.*当前未授权.*付费激活码/],
    ['license_revoked', /授权已撤销.*当前未授权.*付费激活码/],
    ['expired', /订阅已到期.*兑换续期码/],
    ['subscription_expired', /订阅已到期.*兑换续期码/],
    ['offline_expired', /离线授权已过期.*重新验证/],
    ['lease_expired', /离线授权已过期.*重新验证/],
    ['clock_error', /校准时间.*重新验证/],
    ['account_mismatch', /绑定账号不一致.*切回已绑定/],
    ['license_unavailable', /当前设备授权不可用.*恢复或迁移授权/],
    ['suspended', /授权暂不可用.*联系管理员/],
    ['invalid', /授权暂不可用.*重新验证/],
  ];
  for (const [state, expected] of cases) {
    const h = harness({state, license_id: 'old-license', plan: 'month', account: 'wxid_a',
      current_account: state === 'account_mismatch' ? 'wxid_b' : 'wxid_a',
      authorized: state === 'account_mismatch', message: '旧客户端提示：自动化已暂停。'});
    await h.ui.initialize();
    const message = h.element('#licenseStateMessage').textContent;
    assert.match(message, expected, state);
    assert.doesNotMatch(message, /自动化|暂停/);
    assert.equal(h.element('#homeLicenseMessage').textContent, message);
    assert.equal(h.ui.formatMessage(undefined, h.snapshot.message), message);
    const pauseFailure = '授权暂停未能写入，已停止后台服务；请稍后重新连接微信。';
    assert.equal(h.ui.formatMessage(undefined, pauseFailure), pauseFailure,
      'A distinct runtime failure must not be replaced by the stored entitlement state');
    assert.equal(await h.ui.beforeStart(), false);
    assert.equal(h.toasts.at(-1).message, message);
    assert.equal(h.element('#licenseFeedback').textContent, message);
    for (const code of [state, `LIC-${state}`]) {
      h.handlers.refresh_license = () => { throw Object.assign(new Error('自动化已暂停'),
        {product: {code, message: '旧客户端提示：自动化已暂停。'}}); };
      await h.click('#licenseRefresh');
      assert.equal(h.element('#licenseFeedback').textContent, message, code);
      assert.equal(h.ui.formatMessage({code, message: '自动化已暂停'}), message, code);
    }
  }
  const trial = harness({state: 'expired', plan: 'trial'});
  await trial.ui.initialize();
  assert.match(trial.element('#licenseStateMessage').textContent, /免费试用已结束.*付费激活码/);
  const unknown = harness({state: 'storage_error', message: '授权存储暂不可用，请稍后重试。'});
  await unknown.ui.initialize();
  assert.equal(unknown.ui.formatMessage(undefined, '授权暂停未能写入，已停止后台服务。'),
    '授权暂停未能写入，已停止后台服务。', 'Unknown runtime failures retain their actual detail');
  assert.equal(unknown.ui.formatMessage({code: 'LIC-code_unavailable', message: '激活码不可用，请检查输入。'}),
    '激活码不可用，请检查输入。');
  assert.equal(unknown.ui.formatMessage({code: '19000', message: ''}), '');
  const markup = fs.readFileSync(path.join(__dirname, '../src/wechatrebot_manager/app_webui/index.html'), 'utf8');
  assert.match(markup, /id="licensePausedInfo" hidden>机器人未运行。微信和本机数据保留，处理授权问题后可手动启动；未运行期间的任务不会补发。<\/p>/);
}

async function globalErrorsReuseAuthorizationMessagesAndRetainErrorCodes() {
  const h = harness();
  await h.ui.initialize();
  const source = fs.readFileSync(path.join(__dirname, '../src/wechatrebot_manager/app_webui/app.js'), 'utf8');
  const body = source.slice(source.indexOf('  function reportError('), source.indexOf('  function debounce('));
  const messages = [];
  const report = new Function('subscription', 'toast', 'window', 'state', `${body}; return reportError;`)(
    h.ui, (message, type) => messages.push({message, type}), {}, {});
  for (const code of ['LIC-revoked', 'LIC-license_revoked']) {
    report({product: {code, message: '授权已撤销，自动化已暂停，请联系管理员。', recorded: true}});
    assert.match(messages.at(-1).message, /授权已撤销.*当前未授权.*付费激活码/);
    assert.ok(messages.at(-1).message.includes(`（${code}）`));
    assert.doesNotMatch(messages.at(-1).message, /自动化|暂停/);
  }
  report({product: {code: '19000', detail: '原始诊断详情', recorded: true}});
  assert.equal(messages.at(-1).message, '操作未完成：原始诊断详情（19000）');
  const beforeInitialize = new Function('toast', 'window', 'state', `${body}; const result = reportError;
    result({product: {code: '19000', message: '初始化资源未就绪', recorded: true}}); let subscription;`);
  beforeInitialize((message) => messages.push({message}), {}, {});
  assert.equal(messages.at(-1).message, '操作未完成：初始化资源未就绪（19000）');
}

async function businessNumbersCannotBeSubmittedAsActivationCodes() {
  const h = harness();
  await h.ui.initialize();
  const initialCalls = h.calls.length;
  assert.match(h.element('#licenseCodeHelp').textContent, /完整激活码.*业务编号不能用于兑换/);
  for (const code of ['WRB-20991231235959-0123456789ABCDEF', ' wrb-20991231235959-0123456789abcdef ']) {
    await h.preview(code);
    assert.equal(h.element('#licenseCodeFeedback').textContent, '这是业务编号，请复制详情中的完整激活码。');
    assert.equal(h.element('#licenseCodeFeedback').dataset.tone, 'error');
    assert.equal(h.element('#licenseCode').value, code);
    assert.equal(h.element('#licenseCodePreview').hidden, true);
    assert.equal(h.calls.length, initialCalls, 'A business number must not make an API request');
  }
  h.handlers.preview_activation_code = () => ({preview_id: 'actual-code', plan: 'month', mode: 'new'});
  await h.preview('WRB-ABCDEFGH-ABCDEFGH-ABCDEFGH-ABCDEFGH');
  assert.equal(h.element('#licenseCodePreview').hidden, false);
  assert.equal(h.calls.filter(([method]) => method === 'preview_activation_code').length, 1);
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
  assert.match(h.element('#globalStatus').innerHTML, /等待启动/);
  context.state.startPreparing = true;
  context.renderStatus(status);
  assert.equal(h.element('#primaryAction').disabled, true,
    'Runtime polling must not unlock the start button during account confirmation');
  context.state.startPreparing = false;
  context.renderStatus({...status, state: 'RUNNING', can_start: false, online: true});
  assert.equal(h.element('#primaryAction').dataset.action, 'stop');
  assert.equal(h.element('#primaryAction').disabled, false);
}

async function runCenterStartCancellationAndFailureUnlockTheButton() {
  const app = require('./js/webui_source.cjs').managerScript(path.join(__dirname, '../src/wechatrebot_manager/app_webui'));
  const start = app.indexOf('  async function runPrimaryAction(');
  const end = app.indexOf('  function runForceStop(', start);
  assert.ok(start >= 0 && end > start);
  const h = harness(), errors = [], calls = [];
  const stopped = {state: 'STOPPED', can_start: true, can_stop: false, busy: false};
  const state = {status: stopped, startPreparing: false, startPending: false};
  let release, beforeCalls = 0, gate = () => new Promise(resolve => { release = resolve; });
  let action = () => ({accepted: true});
  h.element('#primaryAction').dataset.action = 'start';
  const renderStatus = status => {
    state.status = status;
    h.element('#primaryAction').disabled = Boolean(state.startPreparing || status.busy);
  };
  const context = vm.createContext({state, $: h.element,
    subscription: {beforeStart: () => { beforeCalls += 1; return gate(); }},
    api: async method => { calls.push(method); return action(); },
    startingStatus: status => ({...status, state: 'CORE_STARTING', busy: true}),
    renderStatus, reportError: error => errors.push(error),
    refreshStatus: async () => { calls.push('get_status'); renderStatus(stopped); },
    window: {setTimeout() {}},
  });
  vm.runInContext(app.slice(start, end), context);
  const pending = context.runPrimaryAction();
  assert.equal(h.element('#primaryAction').disabled, true);
  await context.runPrimaryAction();
  assert.equal(beforeCalls, 1, 'Repeated starts must share a single pending gate');
  release(false); await pending;
  assert.equal(h.element('#primaryAction').disabled, false);
  assert.equal(state.startPreparing, false);
  assert.equal(state.startPending, false);
  assert.deepEqual(calls, []);

  gate = () => { throw new Error('确认账号读取失败'); };
  await context.runPrimaryAction();
  assert.equal(errors.at(-1).message, '确认账号读取失败');
  assert.equal(h.element('#primaryAction').disabled, false);
  assert.equal(state.startPreparing, false);
  assert.equal(state.startPending, false);
  assert.deepEqual(calls, ['get_status']);

  gate = () => true;
  action = () => ({accepted: false});
  await context.runPrimaryAction();
  assert.equal(state.startPending, false, 'A rejected queue request must not retain a pending startup');
  action = () => { throw new Error('启动请求失败'); };
  await context.runPrimaryAction();
  assert.equal(errors.at(-1).message, '启动请求失败');
  assert.equal(state.startPreparing, false);
  assert.equal(state.startPending, false);
  assert.equal(h.element('#primaryAction').disabled, false);
}

async function confirmationPromiseSettlesOnCancelAndAllowsFailureRecovery() {
  const app = require('./js/webui_source.cjs').managerScript(path.join(__dirname, '../src/wechatrebot_manager/app_webui'));
  const start = app.indexOf('  let modalResolve = null;');
  const end = app.indexOf('  function requiredValues(', start);
  assert.ok(start >= 0 && end > start);
  const h = harness(), errors = [];
  const focus = {focus() { focus.restored = true; }};
  const context = vm.createContext({$: h.element, escapeHtml, document: {activeElement: focus},
    reportError: error => errors.push(error)});
  vm.runInContext(app.slice(start, end), context);
  const resolve = confirmed => vm.runInContext(`modalResolve(${confirmed})`, context);
  let canceled = 0;
  const cancel = context.confirmAction({title: '确认账号', message: '请核对',
    onConfirm: async () => assert.fail('Cancellation must not run the confirmed operation'),
    onCancel: () => { canceled += 1; }});
  assert.equal(h.element('.app-shell').inert, true);
  await resolve(false);
  assert.equal(await cancel, false);
  assert.equal(canceled, 1);
  assert.equal(h.element('#modalBackdrop').hidden, true);
  assert.equal(h.element('.app-shell').inert, false);
  assert.equal(focus.restored, true);

  let settled = false;
  const failed = context.confirmAction({title: '确认账号', message: '原始说明',
    onConfirm: async () => { throw new Error('微信账号已变化'); }});
  failed.then(() => { settled = true; });
  await resolve(true);
  assert.equal(settled, false, 'Failure must leave a pending decision with retry and cancel available');
  assert.equal(h.element('#modalError').hidden, false);
  assert.match(h.element('#modalError').textContent, /微信账号已变化/);
  assert.equal(h.element('#modalConfirm').disabled, false);
  assert.equal(h.element('#modalCancel').disabled, false);
  assert.equal(h.element('#modalBackdrop').dataset.busy, 'false');
  assert.equal(errors.length, 1);
  await resolve(false);
  assert.equal(await failed, false);

  let release;
  const confirmed = context.confirmAction({title: '确认账号', message: '请核对',
    subject: {label: '微信账号', value: 'wxid_account_a'},
    onConfirm: () => new Promise(done => { release = done; })});
  const confirming = resolve(true);
  assert.equal(h.element('#modalSubjectValue').textContent, 'wxid_account_a');
  assert.equal(h.element('#modalConfirm').disabled, true);
  assert.equal(h.element('#modalCancel').disabled, true);
  await resolve(false);
  assert.equal(h.element('#modalBackdrop').hidden, false, 'An accepted write cannot be canceled halfway through');
  release(); await confirming;
  assert.equal(await confirmed, true);
  assert.equal(h.element('#modalBackdrop').hidden, true);
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
  assert.equal(h.element('#licenseActivateTrial').disabled, false,
    'The free-trial button connects WeChat when no account is logged in');
  assert.match(h.element('#licenseTrialStatus').textContent, /激活时先确认试用提示，再登录/);
  assert.equal(h.calls.some(([method]) => method === 'get_trial_status'), false);

  h.handlers.get_trial_status = async () => ({free_trial: {
    state: 'eligible', eligible: true, recoverable: false, account: h.snapshot.current_account,
  }});
  h.handlers.connect_license_wechat = () => ({accepted: true});
  const pending = clickTrial(h);
  await flush();
  assert.equal(h.confirmations[0].title, '确认激活免费试用');
  assert.equal(h.confirmations[0].subject, undefined);
  assert.equal(h.confirmations[0].confirmText, '同意并继续');
  assert.match(h.confirmations[0].message, /接下来登录的微信号将用于免费试用/);
  assert.equal(h.calls.some(([method]) => ['connect_license_wechat', 'get_trial_status', 'activate_free_trial'].includes(method)), false);
  await h.confirmations[0].confirm();
  await flush();
  assert.equal(h.confirmations[1].title, '请登录微信');
  assert.equal(h.calls.some(([method]) => method === 'activate_free_trial'), false);
  let release;
  h.handlers.activate_free_trial = async account => {
    assert.equal(account, 'wxid_trial_a');
    await new Promise(resolve => { release = resolve; });
    Object.assign(h.snapshot, {state: 'ready', authorized: true, can_connect: true,
      license_id: 'trial-license', account, plan: 'trial', valid_until: 1800000000, remaining_seconds: 3600});
    return structuredClone(h.snapshot);
  };
  h.snapshot.current_account = 'wxid_trial_a';
  const confirmed = h.confirmations[1].confirm();
  await flush();
  await clickTrial(h);
  assert.equal(h.element('#licenseActivateTrial').disabled, true);
  assert.equal(h.calls.filter(([method]) => method === 'activate_free_trial').length, 1);
  release(); await confirmed; await pending;
  assert.equal(h.element('#licenseActivateTrial').textContent, '试用进行中');
  assert.match(h.element('#licenseTrialStatus').textContent, /免费试用进行中，结束时间/);
  assert.equal(h.calls.some(([method]) => method === 'start_robot'), false, 'Trial activation must not start automation');
  const trialQueries = h.calls.filter(([method]) => method === 'get_trial_status').length;
  await h.ui.onNavigate('subscription');
  h.intervals.find(fn => fn.delay === 4000)(); await flush();
  assert.equal(h.calls.filter(([method]) => method === 'get_trial_status').length, trialQueries,
    'Repeated navigation and the four-second local poll must not query trial eligibility');

  Object.assign(h.snapshot, {state: 'expired', authorized: false, remaining_seconds: 0});
  h.intervals.find(fn => fn.delay === 4000)(); await flush();
  assert.equal(h.element('#licenseTrialStatus').textContent, '你的试用已结束，可联系群主购买');
  assert.equal(h.element('#licenseActivateTrial').disabled, true);
  assert.equal(h.calls.filter(([method]) => method === 'get_trial_status').length, trialQueries,
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
  const failedActivation = clickTrial(h);
  await flush();
  assert.equal(h.confirmations[0].title, '确认激活免费试用');
  await h.confirmations[0].confirm();
  await failedActivation;
  assert.match(h.element('#licenseFeedback').textContent, /微信登录账号已变化/);
  assert.equal(h.element('#licenseFeedback').dataset.tone, 'error');
  assert.equal(h.element('#licenseActivateTrial').disabled, false);

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

async function trialCancellationAndChangedConsentCannotActivate() {
  const setup = async overrides => {
    const h = harness({can_connect: true, current_account: 'wxid_trial_a', ...overrides});
    h.handlers.get_trial_status = () => ({free_trial: {state: 'eligible', eligible: true,
      account: h.snapshot.current_account}});
    await h.ui.initialize();
    await h.ui.onNavigate('subscription');
    return h;
  };
  const canceled = await setup();
  const pendingCancel = clickTrial(canceled);
  await flush();
  await canceled.confirmations[0].cancel();
  await pendingCancel;
  assert.equal(canceled.calls.some(([method]) => ['connect_license_wechat', 'activate_free_trial', 'bind_license_account', 'start_robot'].includes(method)), false);
  assert.equal(canceled.element('#licenseActivateTrial').disabled, false);

  for (const change of [
    {license_id: 'paid-license', plan: 'month', authorized: true, state: 'ready'},
  ]) {
    const h = await setup();
    const pending = clickTrial(h);
    await flush();
    Object.assign(h.snapshot, change);
    await assert.rejects(h.confirmations[0].confirm(), /授权.*变化/);
    assert.equal(h.calls.some(([method]) => method === 'activate_free_trial'), false);
    await h.confirmations[0].cancel();
    await pending;
    assert.equal(h.calls.some(([method]) => ['bind_license_account', 'start_robot'].includes(method)), false);
  }

  const loginCanceled = harness({can_connect: true});
  await loginCanceled.ui.initialize();
  loginCanceled.handlers.connect_license_wechat = () => ({accepted: true});
  const waiting = clickTrial(loginCanceled);
  await flush();
  await loginCanceled.confirmations[0].confirm();
  await flush();
  assert.equal(loginCanceled.confirmations[1].title, '请登录微信');
  await loginCanceled.confirmations[1].cancel();
  await waiting;
  assert.equal(loginCanceled.calls.some(([method]) => ['get_trial_status', 'activate_free_trial', 'start_robot'].includes(method)), false);
  assert.equal(loginCanceled.element('#licenseActivateTrial').disabled, false);
}

async function trialConnectionAwaitsEligibilityAndRecoversFromFailure() {
  const h = harness({can_connect: true});
  await h.ui.initialize();
  await h.ui.onNavigate('subscription');
  h.handlers.connect_license_wechat = () => {
    h.snapshot.current_account = 'wxid_trial_a';
    return {accepted: true};
  };
  let release;
  h.handlers.get_trial_status = () => new Promise(resolve => { release = resolve; });
  const pending = clickTrial(h);
  await flush();
  assert.equal(h.calls.some(([method]) => method === 'connect_license_wechat'), false);
  await h.confirmations[0].confirm();
  await flush();
  assert.equal(h.confirmations.length, 1, 'Account discovery must await its in-flight eligibility request');
  assert.equal(h.calls.filter(([method]) => method === 'get_trial_status').length, 1,
    'Automatic account discovery and the trial action must share the pending eligibility request');
  assert.equal(h.element('#licenseActivateTrial').disabled, true);
  h.handlers.activate_free_trial = account => {
    assert.equal(account, 'wxid_trial_a');
    Object.assign(h.snapshot, {state: 'ready', authorized: true, plan: 'trial',
      account, license_id: 'trial-license', valid_until: 1800000000});
    return structuredClone(h.snapshot);
  };
  h.handlers.get_trial_status = () => ({free_trial: {state: 'active', account: 'wxid_trial_a'}});
  release({free_trial: {state: 'eligible', eligible: true, account: 'wxid_trial_a'}});
  await pending;
  assert.equal(h.confirmations.length, 1, 'The accepted trial consent must not be asked again after login');
  assert.equal(h.calls.filter(([method]) => method === 'activate_free_trial').length, 1);
  assert.equal(h.element('#licenseActivateTrial').disabled, true);

  for (const response of [{accepted: false, message: '微信连接正在处理'}, new Error('微信连接失败')]) {
    const failed = harness({can_connect: true});
    await failed.ui.initialize();
    failed.handlers.connect_license_wechat = () => {
      if (response instanceof Error) throw response;
      return response;
    };
    const attempted = clickTrial(failed);
    await flush();
    assert.equal(failed.calls.some(([method]) => method === 'connect_license_wechat'), false);
    await failed.confirmations[0].confirm();
    await attempted;
    assert.match(failed.element('#licenseFeedback').textContent, /微信连接正在处理|微信连接失败/);
    assert.equal(failed.element('#licenseFeedback').dataset.tone, 'error');
    assert.equal(failed.element('#licenseActivateTrial').disabled, false);
    assert.equal(failed.confirmations.length, 1);
    assert.equal(failed.calls.some(([method]) => ['get_trial_status', 'activate_free_trial', 'start_robot'].includes(method)), false);
  }
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

const completionTimer = setTimeout(() => {
  console.error('Subscription UI checks did not finish; a confirmation or API promise may be unresolved.');
  process.exitCode = 1;
}, 10000);

(async () => {
  await activationAndRecovery();
  await expiryAndUnconfigured();
  await validSubscriptionStaysActiveWhenStoppedOrDisconnected();
  await revokedSubscriptionCanConfirmNewPaidActivationWithoutStarting();
  await startupConsentPrecedesAccountConnection();
  await startupBindingQuotaMustBeLiveAndFailClosed();
  await startupConnectionFailuresAndLoginCancellation();
  await startupRejectsIdentityOrLicenseChanges();
  await startupFreshReadCannotRaceAnotherEntry();
  await subscriptionOptionsFollowEntitlementAndPreserveExpansion();
  await rebindConsentCanBeRefusedWithoutRuntimeChanges();
  await rebindWaitsForADifferentActualAccountAndDoesNotStart();
  await rebindInvalidQuotaOrChangedRevisionBlocksRuntimeActions();
  await rebindStopOrWriteFailurePreservesTheOldBinding();
  await authorizationMessagesExplainTheReasonAcrossEveryEntry();
  await globalErrorsReuseAuthorizationMessagesAndRetainErrorCodes();
  await businessNumbersCannotBeSubmittedAsActivationCodes();
  await wechatContactHasItsOwnRefreshCycle();
  await wechatContactExpiryClearsTheImageAndExpansion();
  await qrExpiryUsesElapsedTimeInsteadOfTheLocalClock();
  await contactFailureDoesNotBlockAnExistingCode();
  await invalidOrBrokenQrImagesAreHidden();
  await validBundledFallbackShowsTheRefreshWarning();
  await freeTrialRequiresLiveAccountAndQueriesOnlyWhenNeeded();
  await freeTrialStatesFailuresAndAccountChanges();
  await trialCancellationAndChangedConsentCannotActivate();
  await trialConnectionAwaitsEligibilityAndRecoversFromFailure();
  await paidRedemptionInvalidatesOutstandingTrialQuery();
  runCenterCanStartWithBridgeConnected();
  await runCenterStartCancellationAndFailureUnlockTheButton();
  await confirmationPromiseSettlesOnCancelAndAllowsFailureRecovery();
  process.stdout.write('Subscription UI activation, expiry, confirmation and WeChat QR checks passed.\n');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => clearTimeout(completionTimer));
