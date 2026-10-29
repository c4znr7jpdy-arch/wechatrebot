/* Activity controller; the shell supplies shared services explicitly. */
window.createActivityView = function (helpers) {
  "use strict";
  const {
    $,
    api,
    escapeHtml,
    fullTime,
    reportError,
    state,
  } = helpers;

  const activityPageSize = 15;
  let bound = false;
  let loading = false;
  let loaded = false;
  let renderedItems = null;
  let renderedSources = null;
  const labels = {error: "错误", warning: "警告", info: "提示"};

  async function boundedRequest(method) {
    let timer;
    try {
      return await Promise.race([
        api(method),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("诊断接口等待超时")), 15000); }),
      ]);
    } finally { clearTimeout(timer); }
  }

  function filteredItems() {
    const level = $("#activityLevel").value;
    const source = $("#activitySource").value;
    const query = $("#activitySearch").value.trim().toLowerCase();
    const hours = Number($("#activityRange").value);
    return state.activityItems.filter((item) => (
      (level === "all" || (level === "issues" ? item.level !== "info" : item.level === level))
      && (!source || item.source === source)
      && (!hours || (item.timestamp !== null && item.timestamp * 1000 >= Date.now() - hours * 3600000))
      && (!query || [item.code, item.title, item.message, item.detail, item.operation].join(" ").toLowerCase().includes(query))
    ));
  }

  function bindActivity() {
    if (bound) return;
    bound = true;
    for (const id of ["activityLevel", "activitySource", "activityRange", "activitySearch"]) {
      $(`#${id}`).addEventListener(id === "activitySearch" ? "input" : "change", () => {
        state.activityPage = 1;
        renderActivity();
      });
    }
    $("#activityPrev").addEventListener("click", () => { state.activityPage--; renderActivity(); });
    $("#activityNext").addEventListener("click", () => { state.activityPage++; renderActivity(); });
    $("#activityList").addEventListener("click", async (event) => {
      const button = event.target.closest("[data-copy-activity]");
      if (!button) return;
      const item = state.activityItems.find((entry) => entry.id === button.dataset.copyActivity);
      if (item) await copyReport(item, button.closest("article"));
    });
    $("#copyLatestActivity").addEventListener("click", async () => {
      const item = filteredItems().find((entry) => entry.level !== "info");
      if (item) await copyReport(item);
    });
    $("#exportActivity").addEventListener("click", exportActivity);
  }

  async function copyReport(item, card = null) {
    const copied = await helpers.copyText(item.report, "问题信息");
    if (copied === false) {
      const details = card?.querySelector("details");
      if (details) details.open = true;
      $("#activityFeedback").textContent = "19005 · 复制失败，请展开技术详情手动选择复制，或导出诊断文件。";
    } else {
      $("#activityFeedback").textContent = `已复制问题信息 · ${item.id}，可直接发送给技术支持。`;
    }
  }

  async function exportActivity() {
    const button = $("#exportActivity");
    button.disabled = true;
    $("#activityFeedback").textContent = "正在收集最近日志并导出…";
    try {
      const result = await boundedRequest("export_activity_diagnostics");
      $("#activityFeedback").textContent = `诊断文件已保存：${result.path || result.file_name}。文件包含最近日志片段，不受当前筛选条件限制。`;
    } catch (error) {
      $("#activityFeedback").textContent = "19004 · 未收到导出确认；后台可能仍在保存。可先复制单条问题信息。";
      reportError(error);
    } finally { button.disabled = false; }
  }

  async function loadActivity(options = {}) {
    bindActivity();
    if (loading) return;
    const background = options.background === true && loaded;
    loading = true;
    if (!background) {
      $("#refreshActivity").disabled = true;
      $("#activityReadError").hidden = true;
    }
    try {
      await window.managerErrorReporter?.flush();
      const items = await boundedRequest("get_activity");
      if (!Array.isArray(items)) throw new Error("运行记录返回格式无效");
      state.activityItems = items;
      const source = $("#activitySource");
      const selected = source.value;
      const sources = new Map(items.map((item) => [item.source, item.source_name]));
      if (selected && !sources.has(selected)) sources.set(selected, source.selectedOptions?.[0]?.textContent || selected);
      const markup = '<option value="">全部来源</option>' + [...sources].map(([key, name]) => `<option value="${escapeHtml(key)}">${escapeHtml(name)}</option>`).join("");
      if (markup !== renderedSources) {
        source.innerHTML = markup;
        source.value = selected;
        renderedSources = markup;
      }
      renderActivity();
      loaded = true;
      $("#activityReadError").hidden = true;
    } catch (error) {
      if (!background) {
        $("#activityReadError").hidden = false;
        $("#activityReadError").textContent = "19003 · 运行记录读取失败，请点击刷新重试。已有结果仍保留。";
        if (!loaded && !state.activityItems.length) $("#activityList").innerHTML = '<div class="empty-state compact"><p>暂时无法读取运行记录</p><p>请刷新重试，或截图联系技术支持。</p></div>';
        reportError(error);
      }
    } finally {
      loading = false;
      if (!background) $("#refreshActivity").disabled = false;
    }
  }

  function renderActivity() {
    const filtered = filteredItems();
    const pages = Math.max(1, Math.ceil(filtered.length / activityPageSize));
    state.activityPage = Math.max(1, Math.min(state.activityPage, pages));
    const items = filtered.slice((state.activityPage - 1) * activityPageSize, state.activityPage * activityPageSize);
    $("#activityPage").textContent = `第 ${state.activityPage} / ${pages} 页 · ${filtered.length} 条`;
    $("#activityPrev").disabled = state.activityPage <= 1;
    $("#activityNext").disabled = state.activityPage >= pages;
    $("#copyLatestActivity").disabled = !filtered.some((item) => item.level !== "info");
    const hours = Number($("#activityRange").value) || 48;
    const errors = filtered.filter((item) => item.level === "error").length;
    const warnings = filtered.filter((item) => item.level === "warning").length;
    $("#activitySummary").textContent = `当前筛选（最近 ${hours} 小时）：${filtered.length} 条记录 · ${errors} 条错误 · ${warnings} 条警告。间隔不超过 5 分钟的同类异常合并，保留次数和首次、最近时间。`;
    const markup = items.length ? items.map((item) => {
      const level = Object.hasOwn(labels, item.level) ? item.level : "info";
      return `<article class="diagnostic-card ${level}" data-activity-id="${escapeHtml(item.id)}">
        <header><span class="diagnostic-level ${level}">${labels[level]}</span><strong>${escapeHtml(item.title)}</strong><code>${escapeHtml(item.code || "")}</code></header>
        <div class="diagnostic-meta"><time>${escapeHtml(item.time ? fullTime(item.time) : "时间未记录")}</time><span>${escapeHtml(item.source_name)}</span>${item.count > 1 ? `<span>同类问题 ${item.count} 次</span>` : ""}</div>
        ${item.operation ? `<div class="diagnostic-operation">操作：${escapeHtml(item.operation)}</div>` : ""}
        <pre class="diagnostic-message">${escapeHtml(item.message)}</pre>
        ${item.suggestion && level !== "info" ? `<p class="diagnostic-suggestion">${escapeHtml(item.suggestion)}</p>` : ""}
        <div class="diagnostic-actions"><button class="button secondary small" type="button" data-copy-activity="${escapeHtml(item.id)}">复制问题信息</button><span>事件 ${escapeHtml(item.id)}</span></div>
        <details><summary>技术详情与问题报告</summary><pre class="diagnostic-report" tabindex="0">${escapeHtml(item.report)}</pre></details>
      </article>`;
    }).join("") : `<div class="empty-state compact"><p>${state.activityItems.length ? "没有符合筛选条件的记录" : "当前还没有可读取的运行记录"}</p><p>${state.activityItems.length ? "可切换全部记录，或调整来源、时间和关键词。" : "发生问题后刷新此页，复制问题信息或导出诊断文件。"}</p></div>`;
    if (markup === renderedItems) return;
    const list = $("#activityList");
    const scrollPositions = [$(".activity-panel"), $(".content-scroll")]
      .map((node) => [node, node.scrollTop]);
    const openIds = new Set([...list.querySelectorAll("[data-activity-id]")]
      .filter((card) => card.querySelector("details")?.open).map((card) => card.dataset.activityId));
    list.innerHTML = markup;
    for (const card of list.querySelectorAll("[data-activity-id]")) {
      if (openIds.has(card.dataset.activityId)) card.querySelector("details").open = true;
    }
    for (const [node, scrollTop] of scrollPositions) node.scrollTop = scrollTop;
    renderedItems = markup;
  }

  async function generateResponseTrace() {
    const button = $("#generateResponseTrace");
    if (button.disabled) return;
    let sendStartedAt = null;
    const makeTrace = (status, detail) => {
      const duration = Math.max(0, (performance.now() - sendStartedAt) / 1000);
      const durationText = duration < 0.1
        ? `${Math.max(1, Math.round(duration * 1000))} 毫秒`
        : `${duration.toFixed(duration < 10 ? 1 : 0)} 秒`;
      return {
        time: new Date().toISOString(),
        title: "微信团队私聊测试",
        duration_seconds: Number(duration.toFixed(3)),
        duration_text: durationText,
        stages: [{
          code: "wechat_test_send",
          label: "发送测试消息",
          status,
          detail: `私聊微信团队（wxid=weixin） · ${detail}`,
          duration_seconds: Number(duration.toFixed(3)),
          duration_text: durationText,
          offset_seconds: 0,
        }],
      };
    };
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "正在发送测试消息…";
    const badge = $("#traceStatus");
    badge.className = "status-badge checking";
    badge.innerHTML = "<span></span>正在发送";
    state.responseTraces = [];
    $("#responseTraceList").innerHTML = '<div class="loading-row">正在向微信团队发送测试消息…</div>';
    try {
      const context = await api("get_scheduled_task_context");
      if (!context?.runtime_ready || !context.account_id) {
        throw new Error(context?.message || "请先连接微信并启动机器人，再发送测试消息。");
      }
      sendStartedAt = performance.now();
      const result = await api("test_scheduled_task", {
        account_id: context.account_id,
        name: "甘特图发送测试",
        action: "text",
        arguments: {text: "发送路径测试"},
        targets: [{type: "private", id: "weixin"}],
        schedule: {kind: "daily", time: "23:59", timezone: "Asia/Shanghai"},
        enabled: true,
        draft: false,
        test_target: {type: "private", id: "weixin"},
      });
      const status = {submitted: "success", unknown: "pending", failed: "failed", skipped: "skipped"}[result?.status] || "failed";
      state.responseTraces = [makeTrace(status, result?.message || "发送结果已返回")];
      renderResponseTraces();
    } catch (error) {
      if (sendStartedAt !== null) {
        const uncertain = /等待超时|可能仍在处理|结果待确认/i.test(String(error?.message || ""));
        state.responseTraces = [makeTrace(
          uncertain ? "pending" : "failed",
          uncertain ? "结果暂未确认；请先检查微信团队私聊，避免重复发送" : String(error?.product?.message || error?.message || "本次发送未成功"),
        )];
        renderResponseTraces();
      } else {
        badge.className = "status-badge error";
        badge.innerHTML = "<span></span>测试未完成";
      }
      reportError(error);
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      const needsReview = state.responseTraces?.[0]?.stages?.some((stage) => stage.status === "pending");
      button.textContent = needsReview ? "先核对聊天再重试" : "再次发送测试";
    }
  }

  function renderResponseTraces() {
    const list = $("#responseTraceList");
    const traces = state.responseTraces || [];
    const latest = traces[0];
    const rawStages = Array.isArray(latest?.stages) ? latest.stages : [];
    const hasFailure = rawStages.some((stage) => stage.status === "failed");
    const hasPending = rawStages.some((stage) => stage.status === "pending");
    const hasSkipped = rawStages.some((stage) => stage.status === "skipped");
    const badge = $("#traceStatus");
    badge.className = `status-badge ${hasFailure ? "error" : hasPending || hasSkipped ? "warning" : traces.length ? "online" : "checking"}`;
    badge.innerHTML = `<span></span>${hasFailure ? "发送失败" : hasPending ? "结果待确认" : hasSkipped ? "未发送" : traces.length ? "已提交微信" : "等待生成"}`;
    if (!latest) {
      list.innerHTML = '<div class="empty-state compact"><p>尚无测试结果</p></div>';
      return;
    }

    const numericDuration = (stage) => {
      const raw = Number(stage.duration_seconds);
      if (Number.isFinite(raw)) return Math.max(0, raw);
      const text = String(stage.duration_text || "").trim();
      const match = text.match(/^(<)?\s*(\d+(?:\.\d+)?)\s*(毫秒|秒)$/);
      if (!match) return 0;
      const value = Number(match[2]) * (match[3] === "毫秒" ? .001 : 1);
      return match[1] ? value / 2 : value;
    };
    let fallbackOffset = 0;
    const stages = rawStages.map((stage) => {
      const duration = numericDuration(stage);
      const parsedOffset = Number(stage.offset_seconds);
      const offset = Number.isFinite(parsedOffset) ? Math.max(0, parsedOffset) : fallbackOffset;
      fallbackOffset = Math.max(fallbackOffset, offset + duration);
      return {...stage, duration, offset};
    });
    const reportedTotal = Number(latest.duration_seconds);
    const total = Math.max(
      Number.isFinite(reportedTotal) ? reportedTotal : 0,
      ...stages.map((stage) => stage.offset + stage.duration),
      0.1,
    );
    const conciseDuration = (seconds) => seconds <= 0
      ? "0 秒"
      : seconds < 0.1
      ? `${Math.max(1, Math.round(seconds * 1000))} 毫秒`
      : `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
    const statusLabel = {success: "完成", skipped: "已跳过", pending: "发送中", failed: "失败"};
    const ticks = [0, .25, .5, .75, 1].map((ratio) => (
      `<span style="left:${ratio * 100}%">${escapeHtml(conciseDuration(total * ratio))}</span>`
    )).join("");
    const rows = stages.map((stage, index) => {
      const status = ["success", "skipped", "pending", "failed"].includes(stage.status) ? stage.status : "success";
      const start = Math.min(98.5, (stage.offset / total) * 100);
      const proportionalWidth = (stage.duration / total) * 100;
      const width = Math.max(1.5, Math.min(100 - start, proportionalWidth));
      const durationText = stage.duration_text || (stage.duration ? conciseDuration(stage.duration) : "<0.1 秒");
      const detail = stage.detail || statusLabel[status];
      const label = stage.label || stage.code || "处理阶段";
      return `<div class="gantt-row ${escapeHtml(status)}">
        <div class="gantt-label"><span>${index + 1}</span><div><strong>${escapeHtml(label)}</strong><small>${escapeHtml(detail)}</small></div></div>
        <div class="gantt-track"><span class="gantt-bar ${escapeHtml(status)}" style="left:${start.toFixed(3)}%;width:${width.toFixed(3)}%" role="img" aria-label="${escapeHtml(`${label}，${statusLabel[status]}，耗时 ${durationText}`)}"></span></div>
        <strong class="gantt-duration">${escapeHtml(durationText)}</strong>
      </div>`;
    }).join("");
    const resultLabel = hasFailure ? "发送失败" : hasPending ? "结果待确认" : hasSkipped ? "未发送" : "已提交微信接口";
    list.innerHTML = `<article class="trace-gantt-card">
      <header class="trace-summary">
        <div><span>本次测试发送</span><strong>${escapeHtml(latest.title || "微信团队私聊测试")}</strong><time>${escapeHtml(fullTime(latest.time))}</time></div>
        <dl><div><dt>总耗时</dt><dd>${escapeHtml(latest.duration_text || conciseDuration(total))}</dd></div><div><dt>结果</dt><dd>${escapeHtml(resultLabel)}</dd></div></dl>
      </header>
      <div class="gantt-legend" aria-label="阶段状态说明"><span class="success">完成</span><span class="skipped">已跳过</span><span class="pending">发送中</span><span class="failed">失败</span></div>
      <div class="gantt-chart" aria-label="机器人消息发送阶段甘特图">
        <div class="gantt-axis"><span>处理阶段</span><div>${ticks}</div><span>耗时</span></div>
        ${rows || '<div class="empty-state compact">等待处理详情</div>'}
      </div>
      <footer>本图显示从测试发送请求到微信返回结果的耗时；每次点击只发送一条固定测试文本。</footer>
    </article>`;
  }

  return {
    generateResponseTrace,
    loadActivity,
    renderActivity,
  };
};
