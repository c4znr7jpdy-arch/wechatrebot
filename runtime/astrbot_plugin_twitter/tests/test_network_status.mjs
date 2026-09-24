import assert from "node:assert/strict";
import { createStatusWatcher } from "../pages/subscriptions/network-settings.mjs";

const timers = new Map();
let sequence = 0, calls = 0, rendered = [];
const watch = createStatusWatcher(async () => ({ provider: { checking: ++calls === 1 } }),
  (result) => rendered.push(result), {
    schedule: (cb) => { timers.set(++sequence, cb); return sequence; },
    cancel: (id) => timers.delete(id),
  });
watch.start();
await new Promise(setImmediate);
watch.start(); // 保存后的概览刷新不得取消已经排好的查询。
assert.equal(timers.size, 1);
const [id, cb] = [...timers][0]; timers.delete(id); await cb();
assert.equal(rendered.at(-1).provider.checking, false);
assert.equal(calls, 2);
watch.stop();
assert.equal(timers.size, 0);

let finish;
const stale = createStatusWatcher(() => new Promise((resolve) => { finish = resolve; }),
  () => assert.fail("停止后不能用旧请求刷新页面"));
stale.start(); stale.stop(); finish({ provider: { checking: true } });
await new Promise(setImmediate);
console.log("network status: repeated starts and stale responses passed");
