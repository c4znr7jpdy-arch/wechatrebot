const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture() {
  const elements = new Map();
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      value: '', innerHTML: '', textContent: '', hidden: false, disabled: false, scrollTop: 0,
      attributes: {}, classList: {toggle() {}}, addEventListener() {},
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; },
    });
    return elements.get(selector);
  };
  const state = {
    activePage: 'groups', chatManagementTab: 'groups', groups: [], contacts: [],
    groupsLoaded: false, contactsLoaded: false, groupsBusy: false, contactsBusy: false,
    groupAccount: '', contactAccount: '', selectedGroup: null, selectedContact: null,
    groupMembers: {}, groupPolicies: {}, groupMemberCounts: {}, contactDetails: {}, privatePolicies: {},
    groupMemberLoad: 0, contactLoad: 0, contactPage: 1,
  };
  const calls = [], errors = [];
  let response = () => { throw new Error('missing fixture response'); };
  const context = {
    window: {
      createChatProfiles: () => ({mount() {}, invalidate() {}}),
      clearTimeout() {}, setTimeout() { return 1; },
      localStorage: {getItem() { return null; }, setItem() {}},
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/wechatrebot_manager/app_webui/chat-management.js'), 'utf8'), context);
  const controller = context.window.createChatManagement({
    $, $$: () => [], state, CONTACT_PAGE_SIZE: 2,
    GROUP_DIRECTORY_RETRY_DELAYS_MS: [1500, 4000], GROUP_MEMBER_RETRY_DELAYS_MS: [800, 2000],
    GROUP_MEMBER_REQUEST_CONCURRENCY: 4, GROUP_MEMBER_COUNTS_STORAGE_PREFIX: 'test:',
    api: (...args) => { calls.push(args); return response(...args); },
    readHomeSetupAlertDismissals: () => [], escapeHtml: value => String(value ?? ''),
    fullTime: value => value, icon: () => '', avatarMarkup: () => '', bindAvatarFallbacks() {},
    contactFactsMarkup: () => '', reportError: error => errors.push(error),
  });
  return {controller, state, $, calls, errors, respond: value => { response = value; }};
}

const contactItems = Array.from({length: 5}, (_, index) => ({user_id: `person-${index}`, name: `Test ${index}`}));
const groupItems = [{group_id: 'test@chatroom', name: 'Test group', member_count: 3}];
const directory = items => ({account_id: 'account-a', items, updated_at: 'now'});

async function retainedContacts() {
  const {controller: c, state: s, $, calls, errors, respond} = fixture();
  respond(() => directory(contactItems));
  await c.loadContacts();
  s.selectedContact = 'person-2';
  s.contactPage = 2;
  $('#contactSearch').value = 'Test';
  $('.contact-table-scroll').scrollTop = 175;
  $('#contactInspector').innerHTML = '<details open><input value="unsaved rule"></details>';
  c.renderContacts({preserveInspector: true});
  const inspector = $('#contactInspector').innerHTML;
  const rows = $('#contactRows').innerHTML;
  const button = $('#refreshContacts').innerHTML;
  let resolve;
  respond(() => new Promise(done => { resolve = done; }));
  const refresh = c.loadContacts(true, {background: true});
  await c.loadContacts(true, {background: true});
  assert.equal(calls.length, 2, 'overlapping background refreshes must share the active load');
  assert.equal($('#contactRows').innerHTML, rows);
  assert.equal($('#contactInspector').innerHTML, inspector);
  assert.equal($('#refreshContacts').innerHTML, button);
  resolve(directory(contactItems.map(item => ({...item, alias: 'new-alias'}))));
  await refresh;
  assert.equal(s.selectedContact, 'person-2');
  assert.equal(s.contactPage, 2);
  assert.equal($('#contactSearch').value, 'Test');
  assert.equal($('.contact-table-scroll').scrollTop, 175);
  assert.equal($('#contactInspector').innerHTML, inspector, 'background data must not recreate an edited or expanded inspector');
  assert.match($('#contactRows').innerHTML, /new-alias/);

  respond(() => { throw new Error('temporarily unavailable'); });
  await c.loadContacts(true, {background: true});
  assert.equal(s.contacts.length, 5);
  assert.equal(s.contactPage, 2);
  assert.equal($('#contactInspector').innerHTML, inspector);
  assert.equal(errors.length, 0, 'background failure must retain data without an error popup');
  assert.equal(s.contactStale, true);
  respond(() => directory(contactItems));
  await c.loadContacts(true);
  assert.equal($('#contactInspector').innerHTML, inspector, 'refreshing the directory must not discard private rule edits');
}

async function retainedGroupsAndEmptyDirectories() {
  const {controller: c, state: s, $, respond} = fixture();
  respond(() => directory(groupItems));
  await c.loadGroups();
  s.selectedGroup = groupItems[0].group_id;
  $('#groupInspector').innerHTML = '<details open><input value="unsaved group rule"></details>';
  $('.group-table-scroll').scrollTop = 98;
  const inspector = $('#groupInspector').innerHTML;
  respond(() => directory(groupItems.map(item => ({...item, name: 'Renamed group'}))));
  await c.loadGroups(true, {background: true});
  assert.equal($('#groupInspector').innerHTML, inspector);
  assert.equal($('.group-table-scroll').scrollTop, 98);
  assert.equal(s.selectedGroup, groupItems[0].group_id);
  assert.match($('#groupRows').innerHTML, /Renamed group/);

  for (const [method, rows] of [['loadGroups', '#groupRows'], ['loadContacts', '#contactRows']]) {
    c.invalidateChatManagementContext();
    respond(() => directory([]));
    await c[method]();
    const empty = $(rows).innerHTML;
    let resolve;
    respond(() => new Promise(done => { resolve = done; }));
    const pending = c[method](true, {background: true});
    assert.equal($(rows).innerHTML, empty, 'a loaded empty directory is a cached result, not an initial load');
    resolve(directory([]));
    await pending;
  }
}

async function accountIsolation() {
  const {controller: c, state: s, $, respond} = fixture();
  let oldGroup, oldContacts;
  respond(method => new Promise(done => { if (method === 'get_groups') oldGroup = done; else oldContacts = done; }));
  const groups = c.loadGroups();
  const contacts = c.loadContacts();
  c.invalidateChatManagementContext();
  assert.equal(s.groupsBusy, false);
  assert.equal(s.contactsBusy, false);
  assert.equal($('#refreshContacts').disabled, false);
  respond(method => ({account_id: 'account-b', items: method === 'get_groups' ? [] : [{user_id: 'new-account-person'}]}));
  await c.loadContacts();
  await c.loadGroups();
  oldGroup(directory(groupItems));
  oldContacts(directory(contactItems));
  await Promise.all([groups, contacts]);
  assert.equal(s.groupAccount, 'account-b');
  assert.equal(s.contactAccount, 'account-b');
  assert.equal(s.groups.length, 0);
  assert.equal(s.contacts[0].user_id, 'new-account-person');
  assert.equal(s.contactPage, 1);
}

async function unknownAccountRetainsDirectories() {
  const {controller: c, state: s, $, calls, respond} = fixture();
  respond(method => directory(method === 'get_groups' ? groupItems : contactItems));
  await c.loadGroups();
  await c.loadContacts();
  const groupRows = $('#groupRows').innerHTML, contactRows = $('#contactRows').innerHTML;
  s.status = {runtime: {account_status_available: false}};
  await c.loadGroups(true, {background: true});
  await c.loadContacts(true, {background: true});
  assert.equal(calls.length, 2, 'an unknown account must not trigger directory reads');
  assert.equal($('#groupRows').innerHTML, groupRows);
  assert.equal($('#contactRows').innerHTML, contactRows);
  s.status.runtime.account_status_available = true;
  let resolve;
  respond(() => new Promise(done => { resolve = done; }));
  const pending = c.loadContacts(true, {background: true});
  s.status.runtime.account_status_available = false;
  resolve({account_id: '', items: [], stale: true});
  await pending;
  assert.equal(s.contacts.length, 5, 'a probe becoming unknown during a read must not replace the cache');
  assert.equal(s.contactAccount, 'account-a');
}

async function groupPolicyPollingStopsOutsidePolicyTab() {
  const assert = require('assert');
  const fs = require('fs');
  const path = require('path');
  const vm = require('vm');

  const elements = new Map();
  const $ = (selector) => {
    if (!elements.has(selector)) {
      elements.set(selector, {
        value: '',
        innerHTML: '',
        textContent: '',
        hidden: selector === '#modalBackdrop',
        disabled: false,
        className: '',
        classList: {toggle() {}, add() {}, remove() {}},
        elements: {},
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener() {},
        setAttribute() {},
        removeAttribute() {},
      });
    }
    return elements.get(selector);
  };

  let nextTimerId = 1;
  const scheduled = [];
  const activeTimers = new Map();
  const doc = {hidden: false, addEventListener() {}, removeEventListener() {}};
  const windowObj = {
    document: doc,
    addEventListener() {},
    removeEventListener() {},
    localStorage: {getItem: () => null, setItem() {}},
    managerMotion: {enter() {}},
    setTimeout(callback, delay) {
      const id = nextTimerId++;
      const entry = {id, callback, delay};
      scheduled.push(entry);
      activeTimers.set(id, entry);
      return id;
    },
    clearTimeout(id) {
      activeTimers.delete(id);
    },
  };
  windowObj.window = windowObj;
  const context = {window: windowObj, document: doc, setTimeout: windowObj.setTimeout, clearTimeout: windowObj.clearTimeout, Map, Set, URLSearchParams, console, Promise};

  const webuiDir = path.resolve(__dirname, '..', 'src', 'wechatrebot_manager', 'app_webui');
  for (const name of ['chat-profiles.js', 'chat-management.js']) {
    vm.runInNewContext(fs.readFileSync(path.join(webuiDir, name), 'utf8'), context);
  }

  const state = {
    activePage: 'groups',
    chatManagementTab: 'groups',
    chatManagementAccount: 'wxid_1',
    groupAccount: 'wxid_1',
    contactAccount: 'wxid_1',
    status: {runtime: {account_status_available: true}},
    groups: [{group_id: 'room@chatroom', name: 'Test Room', member_count: 3}],
    groupsLoaded: true,
    groupPolicies: {'room@chatroom': {policy: {bot_enabled: true}, pending_bot_control_request: null}},
    selectedGroup: 'room@chatroom',
    groupInspectorView: 'policy',
    contacts: [],
    contactsLoaded: true,
    contactPolicies: {},
    selectedContact: '',
    contactFilter: '',
    groupFilter: '',
  };

  const controller = context.window.createChatManagement({
    $,
    $$: () => [],
    state,
    CONTACT_PAGE_SIZE: 2,
    GROUP_DIRECTORY_RETRY_DELAYS_MS: [1500, 4000],
    GROUP_MEMBER_RETRY_DELAYS_MS: [800, 2000],
    GROUP_MEMBER_REQUEST_CONCURRENCY: 4,
    GROUP_MEMBER_COUNTS_STORAGE_PREFIX: 'test:',
    api: async () => ({policy: {bot_enabled: true}, pending_bot_control_request: null}),
    readHomeSetupAlertDismissals: () => [],
    escapeHtml: (value) => String(value ?? ''),
    fullTime: (value) => value,
    icon: () => '',
    avatarMarkup: () => '',
    bindAvatarFallbacks() {},
    contactFactsMarkup: () => '',
    reportError() {},
    confirmAction: async () => true,
    openCoreTemplates() {},
  });

  // Active groups policy tab schedules a single pollSelectedGroupPolicy timer
  // and repeated sync calls must not reset or starve the countdown.
  controller.syncGroupPolicyPolling();
  assert.strictEqual(activeTimers.size, 1);
  assert.strictEqual(scheduled.length, 1);
  assert.strictEqual(scheduled[0].callback.name, 'pollSelectedGroupPolicy');
  const firstTimerId = scheduled[0].id;
  controller.syncGroupPolicyPolling();
  assert.strictEqual(activeTimers.size, 1);
  assert.strictEqual(scheduled.length, 1);
  assert.strictEqual(activeTimers.has(firstTimerId), true);

  // Switching away to private tab cancels the timer and schedules nothing new.
  controller.selectChatManagementTab('private');
  assert.strictEqual(activeTimers.size, 0);

  // Returning to groups tab with an active selected policy re-arms polling.
  controller.selectChatManagementTab('groups');
  assert.strictEqual(activeTimers.size, 1);

  // Navigating away from chat page cancels polling on sync.
  state.activePage = 'dashboard';
  controller.syncGroupPolicyPolling();
  assert.strictEqual(activeTimers.size, 0);

  // Hidden document keeps polling stopped even back on groups page.
  state.activePage = 'groups';
  doc.hidden = true;
  controller.syncGroupPolicyPolling();
  assert.strictEqual(activeTimers.size, 0);

  // Becoming visible again resumes polling; invalidating chat context stops it.
  doc.hidden = false;
  controller.syncGroupPolicyPolling();
  assert.strictEqual(activeTimers.size, 1);
  controller.invalidateChatManagementContext({nextAccount: ''});
  assert.strictEqual(activeTimers.size, 0);
}

(async () => {
  await retainedContacts();
  await retainedGroupsAndEmptyDirectories();
  await accountIsolation();
  await unknownAccountRetainsDirectories();
  await groupPolicyPollingStopsOutsidePolicyTab();
  console.log('chat directories retain cached views and edits; account isolation; polling lifecycle: passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
