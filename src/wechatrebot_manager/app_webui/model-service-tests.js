/* Shared model probes for the editor and cards; results belong to saved configurations. */
window.createModelServiceTests = function (helpers) {
  "use strict";
  const {$, $$, api, escapeHtml, reportError, state, toast, updateProviderDirty, visibleProviderServices} = helpers;
  const pending = new Map();
  const revisions = new Map();
  let editorGeneration = 0;
  let editorBusy = false;
  let batch = null;
  const slots = [];
  let activeRequests = 0;

  function acquireSlot() {
    if (activeRequests < 2) { activeRequests += 1; return Promise.resolve(); }
    return new Promise((resolve) => slots.push(resolve));
  }

  function releaseSlot() {
    if (slots.length) slots.shift()();
    else activeRequests -= 1;
  }

  const services = () => state.bootstrap?.providers || [];
  const serviceFor = (id) => services().find((item) => item.id === id);
  const onModelPage = () => state.activePage === "settings" && state.settingsSection === "model";
  const seconds = (ms) => `${(Math.max(0, Number(ms) || 0) / 1000).toFixed(1)} 秒`;

  function clearProviderTest() {
    editorGeneration += 1;
    $("#providerTestResult").hidden = true;
    $("#providerTestOutput").textContent = "";
  }

  function invalidate(id) {
    revisions.set(id, (revisions.get(id) || 0) + 1);
    const service = serviceFor(id);
    if (service) service.last_test = null;
  }

  function cardMarkup(service) {
    const testing = pending.has(service.id);
    const result = service.last_test;
    const status = testing ? "pending" : result?.status || "untested";
    const label = {pending: "测试中…", success: seconds(result?.elapsed_ms), error: "失败", untested: "—"}[status] || "—";
    const date = result?.tested_at ? new Date(result.tested_at) : null;
    const when = date && Number.isFinite(date.getTime())
      ? date.toLocaleString("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"}) : "";
    const meta = testing ? "正在测试模型" : result
      ? [status === "success" ? "上次测试成功" : "上次测试失败", `耗时 ${seconds(result.elapsed_ms)}`, when, result.error_code ? `错误 ${result.error_code}` : ""].filter(Boolean).join(" · ") : "尚未测试";
    return `<span class="provider-card-test" data-status="${escapeHtml(status)}" role="status" aria-label="${escapeHtml(meta)}" title="${escapeHtml(meta)}"><span class="provider-test-label">${label}</span></span>`;
  }

  function refresh() {
    $$("[data-provider-card]").forEach((card) => {
      const service = serviceFor(card.dataset.providerCard);
      if (!service) return;
      const result = $(".provider-card-test", card);
      if (result) result.outerHTML = cardMarkup(service);
      const button = $("[data-provider-test]", card);
      if (button) {
        button.disabled = !service.configured || pending.has(service.id) || Boolean(batch?.running);
        button.textContent = pending.has(service.id) ? "测试中…" : "测试";
      }
    });
    const button = $("[data-provider-test-all]");
    if (button) {
      button.textContent = batch?.running ? (batch.cancelled ? "正在停止…" : "停止测试") : "测试所有模型";
      button.disabled = batch?.running ? batch.cancelled : !visibleProviderServices().some((item) => item.configured);
    }
    const status = $(".provider-batch-status");
    if (status) status.textContent = batch
      ? `${batch.running ? "已测试" : batch.cancelled ? "已停止" : "测试完成"} ${batch.completed}/${batch.total} · 成功 ${batch.success} · 失败 ${batch.failed}` : "";
  }

  async function requestTest(id, canStart = () => true) {
    if (pending.has(id)) return pending.get(id);
    const revision = revisions.get(id) || 0;
    let started;
    const remember = (payload, fallback) => {
      const service = serviceFor(id);
      if (!service || revision !== (revisions.get(id) || 0)) return;
      service.last_test = Object.hasOwn(payload || {}, "last_test") ? payload.last_test : fallback;
      if (payload?.history_warning && onModelPage()) toast(payload.history_warning, "warning");
    };
    const request = (async () => {
      await acquireSlot();
      started = performance.now();
      try {
        if (!canStart() || !serviceFor(id)?.configured || revision !== (revisions.get(id) || 0)) return null;
        const data = await api("test_provider", id);
        if (data?.status !== "available" || data.id !== id
            || (data.test_kind === "chat" && !data.response_text?.trim())) {
          throw new Error("未收到有效的模型测试结果，请更新后重试。");
        }
        remember(data, {status: "success", elapsed_ms: data.elapsed_ms, tested_at: new Date().toISOString()});
        return data;
      } catch (error) {
        remember(error.product, {status: "error", elapsed_ms: performance.now() - started,
          tested_at: new Date().toISOString(), error_code: error.product?.code || ""});
        throw error;
      } finally {
        releaseSlot();
        pending.delete(id);
        refresh();
      }
    })();
    pending.set(id, request);
    refresh();
    return request;
  }

  async function testProviderCard(id) {
    if (!serviceFor(id)?.configured || pending.has(id) || batch?.running) return;
    const revision = revisions.get(id) || 0;
    try { await requestTest(id); }
    catch (error) {
      if (onModelPage() && state.providerScreen === "overview" && serviceFor(id)
          && revision === (revisions.get(id) || 0)) reportError(error);
    }
  }

  async function testAllProviders() {
    if (batch?.running) { batch.cancelled = true; refresh(); return; }
    const queue = visibleProviderServices().filter((item) => item.configured).map((item) => item.id);
    if (!queue.length) return;
    batch = {running: true, cancelled: false, total: queue.length, completed: 0, success: 0, failed: 0};
    const run = batch;
    const worker = async () => {
      while (queue.length && !run.cancelled) {
        const id = queue.shift();
        if (!serviceFor(id)?.configured) { run.completed += 1; refresh(); continue; }
        try {
          if (!await requestTest(id, () => !run.cancelled)) continue;
          run.success += 1;
        }
        catch (_) { run.failed += 1; }
        run.completed += 1;
        refresh();
      }
    };
    refresh();
    await Promise.all([worker(), worker()]);
    run.running = false;
    refresh();
  }

  async function testSelectedProvider() {
    const form = $("#providerForm");
    const providerId = form.elements.provider_id.value;
    if (!providerId || editorBusy || pending.has(providerId) || batch?.running) return;
    updateProviderDirty();
    if (state.providerDirty) {
      $("#providerNote").textContent = "请先保存修改，再测试当前模型。";
      return;
    }
    const button = $("#testProvider");
    const generation = ++editorGeneration;
    const model = form.elements.model.value;
    const current = () => generation === editorGeneration && state.activePage === "settings" && state.settingsSection === "model"
      && state.providerScreen === "editor" && form.elements.provider_id.value === providerId;
    const result = $("#providerTestResult");
    const title = $("#providerTestTitle");
    const meta = $("#providerTestMeta");
    const output = $("#providerTestOutput");
    const started = performance.now();
    editorBusy = true;
    button.disabled = true;
    button.textContent = "测试中…";
    $("#providerNote").textContent = "";
    result.hidden = false;
    result.dataset.status = "pending";
    title.textContent = "正在请求模型…";
    output.textContent = "";
    const updateElapsed = () => {
      if (current()) meta.textContent = `${model} · 已等待 ${seconds(performance.now() - started)}`;
    };
    updateElapsed();
    const timer = window.setInterval(updateElapsed, 500);
    try {
      const data = await requestTest(providerId);
      if (!current()) return;
      if (!data) { clearProviderTest(); return; }
      result.dataset.status = "success";
      title.textContent = "本次模型测试成功";
      meta.textContent = `${data.model || model} · ${seconds(data.elapsed_ms)}`;
      output.textContent = data.response_text || "已完成该模型用途的实际请求。";
    } catch (error) {
      if (!current()) return;
      result.dataset.status = "error";
      title.textContent = error.product?.title || "模型测试失败";
      meta.textContent = `${model} · ${seconds(performance.now() - started)}${error.product?.code ? ` · ${error.product.code}` : ""}`;
      output.textContent = error.product?.detail || error.message;
      reportError(error);
    } finally {
      window.clearInterval(timer);
      editorBusy = false;
      button.disabled = false;
      button.textContent = "测试模型";
    }
  }

  return {cardMarkup, clearProviderTest, invalidate, isBusy: () => pending.size > 0 || Boolean(batch?.running),
    refresh, testAllProviders, testProviderCard, testSelectedProvider};
};
