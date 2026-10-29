const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const webui = path.join(__dirname, '../src/wechatrebot_manager/app_webui');

function element() {
  return {value: '', hidden: true, textContent: '', innerHTML: '', scrollTop: 900,
    scrollHeight: 1000, clientHeight: 100, listeners: {},
    classList: {toggle() {}}, addEventListener(type, fn) { this.listeners[type] = fn; },
    attributes: {}, setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
    insertAdjacentHTML(position, html) { assert.equal(position, 'afterbegin'); this.innerHTML = html + this.innerHTML; this.scrollHeight += 200; },
    focus() {}, close() {}, showModal() {},
    click() { this.listeners.click({}); }};
}

function dates() {
  const elements = new Map();
  const get = (id) => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  let applied = 0;
  let now = new Date('2026-10-05T12:00:00');
  class Clock extends Date { constructor(...args) { super(...(args.length ? args : [now])); } }
  const context = vm.createContext({window: {}, Date: Clock, document: {getElementById: get}});
  vm.runInContext(fs.readFileSync(path.join(webui, 'date-range.js'), 'utf8'), context);
  context.window.initializeHistoryDateRange(() => applied++);
  assert.equal(get('historyStart').value, '');
  assert.equal(get('historyEnd').value, '');
  assert.match(get('historyDateRange').textContent, /最新消息/);
  now = new Date('2026-11-08T12:00:00');
  get('historyDateRange').click();
  assert.match(get('historyCalendar').innerHTML, /2026 年 11 月/);
  get('rangeWeek').click();
  get('rangeCancel').click();
  assert.equal(get('historyStart').value, '');
  assert.equal(applied, 0);
  get('historyDateRange').click(); get('rangeWeek').click(); get('rangeApply').click();
  assert.equal(get('historyStart').value, '2026-11-01');
  assert.equal(get('historyEnd').value, '2026-11-08');
  get('historyDateRange').click(); get('rangeClear').click();
  assert.equal(get('historyStart').value, ''); assert.equal(get('historyEnd').value, '');
  assert.equal(applied, 2);
  // Choosing an end date first from the empty state still yields a valid range.
  get('historyDateRange').click(); get('rangeEnd').click();
  get('historyCalendar').listeners.click({target: {closest: () => ({dataset: {date: '2026-11-03'}})}});
  get('rangeApply').click();
  assert.equal(get('historyStart').value, '2026-11-03');
  assert.equal(get('historyEnd').value, '2026-11-03');
}

function historyContext() {
  const script = require('./js/webui_source.cjs').managerScript(webui);
  const elements = new Map();
  const $ = (id) => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const state = {historyAccount: 'account-a', conversations: [], selectedMessages: [],
    selectedConversation: null, historyMessageQuery: '', historyMessageCache: new Map(),
    historyMessageRequests: new Map(), historyMessageCacheEpoch: 0, historyOlderLoad: 0,
    messageLoad: 0, historyLoad: 0, historyHasMore: false, historyHasNewer: false, historyKind: 'all',
    historyManualRefresh: false, historyReadyAccount: 'account-a'};
  const calls = [];
  const context = vm.createContext({state, $, Date, Map, Set, JSON, Number, String,
    window: {setTimeout, clearTimeout},
    api: async (method, id, filters) => { calls.push({method, id, filters}); return context.response(id); },
    renderConversations() {}, renderConversationHeader() {}, renderConversationInspector() {},
    bindAvatarFallbacks() {}, bindMessageSenderSelection() {}, updateSelectedSenderButtons() {},
    messageMarkup(message) { return `${message.content}|`; },
    reportError(error) { throw error; },
    renderMessages(messages, options = {}) {
      state.selectedMessages = messages;
      $('#messageList').innerHTML = messages.map(m => m.content).join('|');
      if (options.forceBottom || options.nearBottom) $('#messageList').scrollTop = 900;
    }, renderHistoryAccountGate() {},
  });
  const names = ['requestHistoryRead', 'refreshHistoryRecords', 'loadConversations', 'canEditHistory', 'setHistoryActionsEnabled',
    'historyFilters', 'pagedHistoryFilters', 'normalizeMessagePage', 'mergeMessages',
    'historyMessageCacheKey', 'getHistoryMessageCacheEntry', 'setHistoryMessageCacheEntry',
    'saveSelectedHistoryMessages', 'clearHistoryMessageCache', 'requestHistoryMessagePage',
    'showHistoryNewerMessages', 'messagesSignature', 'selectConversation', 'prependOlderMessages', 'loadOlderMessages',
    'invalidateHistoryState', 'pollHistory'];
  for (const name of names) {
    const start = script.search(new RegExp(`^  (?:async )?function ${name}\\(`, 'm'));
    assert.ok(start >= 0, name);
    const remaining = script.slice(start + 1);
    const end = remaining.search(/^  (?:async )?function /m);
    vm.runInContext(script.slice(start, start + 1 + end), context);
  }
  vm.runInContext(script.match(/  const HISTORY_PAGE_SIZE = .*;/)[0] + '\n'
    + script.match(/  const HISTORY_MESSAGE_CACHE_LIMIT = .*;/)[0] + '\n'
    + script.match(/  const HISTORY_MESSAGE_WINDOW_LIMIT = .*;/)[0] + '\n'
    + script.match(/  const HISTORY_MESSAGE_CACHE_TTL_MS = .*;/)[0] + '\n'
    + script.match(/  const HISTORY_PRELOAD_THRESHOLD = .*;/)[0], context);
  return {context, state, calls, $};
}

const messages = (first, last) => Array.from({length: last - first + 1}, (_, i) => ({
  id: first + i, timestamp: first + i, content: `message-${first + i}`, role: 'user', sender_id: 'member',
}));

async function history() {
  const {context: c, state: s, calls, $} = historyContext();
  s.conversations = ['a', 'b'].map(id => ({conversation_id: id, message_count: 40, last_timestamp: 40}));
  c.response = () => ({account_id: 'account-a', items: messages(21, 40), has_more: true});
  await c.selectConversation('a');
  assert.equal(s.selectedMessages.length, 20);
  assert.equal(calls[0].filters.start, ''); assert.equal(calls[0].filters.end, '');
  assert.equal(calls[0].filters.page_size, 20);
  await c.selectConversation('b'); await c.selectConversation('a');
  assert.equal(calls.length, 2, 'fresh cached conversation should not query again');
  const before = $('#messageList').innerHTML;
  let resolve;
  c.response = () => new Promise(r => { resolve = r; });
  const updating = c.selectConversation('a', {forceRefresh: true});
  assert.equal($('#messageList').innerHTML, before, 'refresh must retain the displayed messages');
  resolve({account_id: 'account-a', items: messages(22, 41), has_more: true});
  await updating;
  assert.equal(s.selectedMessages.at(-1).id, 41);
  // A changed conversation summary invalidates the short-lived cache immediately.
  s.conversations[0].message_count = 42;
  c.response = () => ({account_id: 'account-a', items: messages(23, 42), has_more: true});
  await c.selectConversation('a'); assert.equal(calls.length, 4);
  // Saving scroll/sender state must not extend cache freshness.
  const entry = [...s.historyMessageCache.values()].find(e => e.conversationId === 'a');
  entry.fetchedAt = 1; c.saveSelectedHistoryMessages();
  await c.selectConversation('a'); assert.equal(calls.length, 5);
  // Long absence: never merge disconnected pages and silently omit their gap.
  c.response = () => ({account_id: 'account-a', items: messages(481, 500), has_more: true});
  await c.selectConversation('a', {forceRefresh: true});
  assert.equal(s.selectedMessages[0].id, 21);
  assert.equal(s.historyHasNewer, true);
  await c.selectConversation('a', {latest: true});
  assert.equal(s.selectedMessages.length, 20); assert.equal(s.selectedMessages[0].id, 481);
  // Clearing the time filter explicitly returns to a single latest page.
  $('#historyStart').value = '2026-10-01'; $('#historyEnd').value = '2026-10-02';
  await c.selectConversation('a', {latest: true});
  $('#historyStart').value = ''; $('#historyEnd').value = '';
  await c.selectConversation('a', {latest: true});
  assert.equal(s.selectedMessages.length, 20);
  // At most 200 rendered/cached messages; older rows remain pageable.
  s.selectedMessages = messages(301, 500); c.saveSelectedHistoryMessages();
  c.response = () => ({account_id: 'account-a', items: messages(482, 501), has_more: true});
  await c.selectConversation('a', {forceRefresh: true});
  assert.equal(s.selectedMessages.length, 200); assert.equal(s.selectedMessages[0].id, 302);
  assert.equal(s.historyHasMore, true);
  $('#messageList').scrollTop = 0;
  c.response = () => ({account_id: 'account-a', items: messages(600, 619), has_more: true});
  await c.selectConversation('a', {forceRefresh: true});
  assert.equal(s.historyHasNewer, true); assert.equal(s.selectedMessages[0].id, 302);
  await c.selectConversation('a', {latest: true});
  assert.equal(s.selectedMessages.length, 20); assert.equal(s.historyHasNewer, false);
  // A delayed request from the previous account must never repopulate its cache.
  c.response = () => new Promise(r => { resolve = r; });
  const pending = c.selectConversation('a', {forceRefresh: true});
  s.historyAccount = 'account-b'; c.clearHistoryMessageCache();
  resolve({account_id: 'account-a', items: messages(1, 20), has_more: false});
  await pending; assert.equal(s.historyMessageCache.size, 0);
  for (let i = 0; i < 40; i++) c.setHistoryMessageCacheEntry(String(i), {messages: []});
  assert.equal(s.historyMessageCache.size, 32);
}

async function retainMessagesAfterLongAbsence() {
  const {context: c, state: s, calls, $} = historyContext();
  s.conversations = [{conversation_id: 'a', message_count: 40, last_timestamp: 40}];
  c.response = () => ({account_id: 'account-a', items: messages(21, 40), has_more: true});
  await c.selectConversation('a');
  s.selectedSenderKey = 'member';
  c.saveSelectedHistoryMessages();
  const before = $('#messageList').innerHTML;
  const scrollTop = $('#messageList').scrollTop;
  let resolve;
  c.response = () => new Promise(done => { resolve = done; });
  const pending = c.selectConversation('a', {silent: true, forceRefresh: true});
  assert.equal($('#messageList').innerHTML, before);
  resolve({account_id: 'account-a', items: messages(481, 500), has_more: true});
  await pending;
  assert.equal($('#messageList').innerHTML, before, 'long absence must retain the visible DOM even at the bottom');
  assert.equal($('#messageList').scrollTop, scrollTop);
  assert.equal(s.selectedSenderKey, 'member');
  assert.equal(s.selectedMessages[0].id, 21);
  assert.equal(s.historyMessagesLoading, false);
  assert.equal($('#newMessageButton').hidden, false);
  assert.match($('#newMessageButton').textContent, /回到最新消息/);
  await c.selectConversation('a', {silent: true, forceRefresh: true});
  assert.equal(calls.length, 2, 'automatic polling must retain the pending newer-message prompt');
  c.response = () => ({account_id: 'account-a', items: messages(481, 500), has_more: true});
  await c.selectConversation('a', {latest: true});
  assert.equal(s.selectedMessages.length, 20);
  assert.equal(s.selectedMessages[0].id, 481);
  assert.equal(s.historyHasNewer, false);
  assert.equal($('#newMessageButton').hidden, true);
  c.response = () => ({account_id: 'account-a', items: [], has_more: false});
  await c.selectConversation('a', {silent: true, forceRefresh: true});
  assert.equal(s.selectedMessages.length, 0, 'an empty response must still clear deleted records');
}

async function olderMessagesDuringRefresh() {
  const {context: c, state: s, calls, $} = historyContext();
  const conversation = {conversation_id: 'a', message_count: 60, last_timestamp: 60};
  s.conversations = [conversation];
  c.response = () => ({account_id: 'account-a', items: messages(41, 60), has_more: true});
  await c.selectConversation('a');
  let resolveDirectory;
  c.response = () => new Promise(resolve => { resolveDirectory = resolve; });
  const refreshing = c.loadConversations({silent: true, forceRefresh: true});
  let resolveOlder;
  c.response = () => new Promise(resolve => { resolveOlder = resolve; });
  $('#messageList').scrollTop = 0;
  const older = c.loadOlderMessages();
  const olderCount = calls.length;
  assert.equal(calls.at(-1).filters.before.id, 41);
  await c.loadOlderMessages();
  assert.equal(calls.length, olderCount, 'repeated scroll events must not duplicate the page request');
  c.response = () => ({account_id: 'account-a', items: messages(41, 60), has_more: true});
  resolveDirectory({account_id: 'account-a', items: [conversation], summary: {}});
  await refreshing;
  resolveOlder({account_id: 'account-a', items: messages(21, 40), has_more: true});
  await older;
  assert.equal(s.selectedMessages[0].id, 21, 'a background directory refresh must not discard the older page');
  assert.equal(s.selectedMessages.length, 40);
  assert.equal($('#messageList').scrollTop, 200, 'prepending must preserve the visible message position');
  assert.equal(s.historyLoadingOlder, false);
  assert.equal($('#olderMessagesStatus').hidden, true);
  assert.equal($('#messageList').attributes['aria-busy'], undefined);
  $('#messageList').scrollTop = 0;
  c.response = () => ({account_id: 'account-a', items: messages(1, 20), has_more: false});
  await c.loadOlderMessages();
  assert.equal(s.selectedMessages.length, 60, 'the next upward scroll must load the next page');
  assert.equal(s.historyHasMore, false);
}

async function olderMessagesRecovery() {
  const {context: c, state: s, $} = historyContext();
  s.conversations = ['a', 'b'].map(id => ({conversation_id: id, message_count: 40, last_timestamp: 40}));
  c.response = () => ({account_id: 'account-a', items: messages(21, 40), has_more: true});
  await c.selectConversation('a');
  const timers = new Map();
  c.window.setTimeout = (callback, delay) => { timers.set(delay, callback); return delay; };
  c.window.clearTimeout = id => timers.delete(id);
  const errors = [];
  c.reportError = error => errors.push(error.message);
  let resolveTimedOut;
  c.response = () => new Promise(resolve => { resolveTimedOut = resolve; });
  $('#messageList').scrollTop = 0;
  const timedOut = c.loadOlderMessages();
  timers.get(300)();
  assert.equal($('#olderMessagesStatus').hidden, false);
  timers.get(20000)();
  await timedOut;
  assert.equal(s.historyLoadingOlder, false);
  assert.equal($('#olderMessagesStatus').hidden, true);
  assert.equal($('#messageList').attributes['aria-busy'], undefined);
  assert.equal(s.selectedMessages.length, 20, 'a timeout must retain existing messages');
  assert.equal(errors.length, 1);
  assert.match(errors[0], /读取记录超时/);
  c.response = () => ({account_id: 'account-a', items: messages(1, 20), has_more: false});
  await c.loadOlderMessages();
  assert.equal(s.selectedMessages.length, 40, 'paging must recover after a timeout');
  resolveTimedOut({account_id: 'account-a', items: messages(1, 20), has_more: false});
  await Promise.resolve(); await Promise.resolve();
  assert.equal(s.selectedMessages.length, 40, 'late timed-out responses must not duplicate messages');

  s.historyHasMore = true;
  $('#messageList').scrollTop = 0;
  let resolveStale;
  c.response = () => new Promise(resolve => { resolveStale = resolve; });
  const stale = c.loadOlderMessages();
  c.response = () => ({account_id: 'account-a', items: messages(81, 100), has_more: true});
  await c.selectConversation('b');
  resolveStale({account_id: 'account-a', items: messages(-19, 0), has_more: false});
  await stale;
  assert.equal(s.selectedMessages[0].id, 81, 'switching chats must still reject the previous chat page');
  assert.equal(s.historyHasMore, true);
  assert.equal(s.historyLoadingOlder, false);
  assert.equal($('#olderMessagesStatus').hidden, true);
}

async function manualRefresh() {
  const {context: c, state: s, calls, $} = historyContext();
  s.conversations = [{conversation_id: 'a', message_count: 1, last_timestamp: 1}];
  let resolveOld;
  c.response = () => new Promise(resolve => { resolveOld = resolve; });
  const oldRequest = c.selectConversation('a');
  assert.match($('#messageList').innerHTML, /若长时间未读到记录，点击刷新/);
  c.response = id => typeof id === 'object'
    ? {account_id: 'account-a', items: [{conversation_id: 'a', message_count: 2, last_timestamp: 2}], summary: {}}
    : {account_id: 'account-a', items: messages(2, 2), has_more: false};
  await c.refreshHistoryRecords();
  assert.equal(calls.length, 3, 'manual refresh must bypass the pending request');
  assert.equal(s.selectedMessages[0].id, 2);
  assert.equal(s.historyManualRefresh, false);
  assert.equal($('#refreshHistory').disabled, false);
  resolveOld({account_id: 'account-a', items: messages(1, 1), has_more: false});
  await oldRequest;
  assert.equal(s.selectedMessages[0].id, 2, 'the old response must not replace refreshed records');

  let timeoutCallback;
  c.window.setTimeout = callback => { timeoutCallback = callback; return 1; };
  c.window.clearTimeout = () => {};
  c.response = () => new Promise(() => {});
  const timeoutRequest = c.requestHistoryRead(c.api('get_messages', 'a', {}));
  timeoutCallback();
  await assert.rejects(timeoutRequest, /读取记录超时，请点击刷新/);
}

function offlineHistoryActions() {
  const {context: c, state: s, $} = historyContext();
  s.status = {history_offline: true, runtime: {account_status_available: false}};
  c.setHistoryActionsEnabled(true);
  assert.equal($('#exportHistory').disabled, false);
  assert.equal($('#refreshHistory').disabled, false);
  assert.equal($('#cleanHistory').disabled, true);
  assert.equal($('#deleteConversation').disabled, true);
  assert.equal(c.canEditHistory(), false);
  s.status.history_offline = false;
  c.setHistoryActionsEnabled(false);
  assert.equal($('#exportHistory').disabled, true);
  assert.equal($('#refreshHistory').disabled, true);
  s.status.runtime = {account_status_available: true, wechat_identity: 'account-a'};
  c.setHistoryActionsEnabled(true);
  assert.equal($('#cleanHistory').disabled, false);
  assert.equal($('#deleteConversation').disabled, false);
  assert.equal(c.canEditHistory('account-b'), false);
  s.status.history_offline = true;
  c.setHistoryActionsEnabled(true);
  assert.equal($('#deleteConversation').disabled, true, 'a previously rendered delete button must become read-only');
}

async function unknownAccountPolling() {
  const {context: c, state: s} = historyContext();
  s.activePage = 'history';
  s.status = {runtime: {account_status_available: false}};
  let reads = 0;
  c.loadConversations = async () => { reads++; };
  await c.pollHistory();
  assert.equal(reads, 0, 'unknown identity must not generate repeated history failures');
  s.status.runtime.account_status_available = true;
  await c.pollHistory();
  assert.equal(reads, 1, 'history polling resumes after the account probe recovers');
  s.status.runtime.account_status_available = false;
  s.status.history_offline = true;
  await c.pollHistory();
  assert.equal(reads, 2, 'verified offline history remains available');
}

(async () => { dates(); await history(); await retainMessagesAfterLongAbsence(); await olderMessagesDuringRefresh(); await olderMessagesRecovery(); await manualRefresh(); offlineHistoryActions(); await unknownAccountPolling(); console.log('history date, cache, pagination, refresh and account isolation: passed'); })()
  .catch(error => { console.error(error); process.exitCode = 1; });
