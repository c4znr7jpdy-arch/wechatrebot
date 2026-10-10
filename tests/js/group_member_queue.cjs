const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = require("./webui_source.cjs").managerScript(require("node:path").dirname(process.argv[2]));
const start = source.indexOf("  function pumpGroupMemberRequestQueue() {");
const end = source.indexOf("  async function loadGroupMembers(", start);
assert.ok(start >= 0 && end > start);
const limit = Number(source.match(/const GROUP_MEMBER_REQUEST_CONCURRENCY = (\d+);/)[1]);
const ids = Array.from({length: limit + 3}, (_, i) => `${i}@chatroom`);
const calls = [];
const pending = [];
let active = 0;
let peak = 0;
const context = {
  state: {groupMemberLoad: 1, groups: ids.map(group_id => ({group_id})), activePage: "groups", chatManagementTab: "groups"},
  api: (_method, groupId) => {
    calls.push(groupId);
    peak = Math.max(peak, ++active);
    return new Promise(resolve => pending.push(() => { active -= 1; resolve(groupId); }));
  },
};
vm.runInNewContext(`
  const GROUP_MEMBER_REQUEST_CONCURRENCY = ${limit};
  let groupMemberRequestsActive = 0;
  const groupMemberRequestQueue = [];
  ${source.slice(start, end)}
  globalThis.enqueue = queueGroupMemberRequest;
`, context);
const settle = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  // Foreground and background requests share the same concurrency budget.
  const results = ids.map((id, i) => context.enqueue(id, 1, i % 2 === 0));
  await settle();
  assert.equal(limit, 4);
  assert.equal(calls.length, 4);
  assert.equal(peak, 4);
  pending.shift()();
  await settle();
  assert.equal(calls.length, 5);
  assert.equal(peak, 4);

  // A changed directory generation must not issue queued requests for old data.
  context.state.groupMemberLoad = 2;
  pending.splice(0).forEach(resolve => resolve());
  const values = await Promise.all(results);
  await settle();
  assert.equal(calls.length, 5);
  assert.deepEqual(values.slice(0, 5), ids.slice(0, 5));
  assert.deepEqual(values.slice(5), [null, null]);

  context.state.activePage = "home";
  assert.equal(await context.enqueue(ids[0], 2, true), null);
  assert.equal(await context.enqueue("missing@chatroom", 2, false), null);
  assert.equal(calls.length, 5);
  const foreground = context.enqueue(ids[0], 2, false);
  await settle();
  pending.shift()();
  assert.equal(await foreground, ids[0]);
  assert.equal(active, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
