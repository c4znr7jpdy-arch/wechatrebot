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
  async function loadActivity() {
    try {
      state.activityItems = await api("get_activity");
      renderActivity();
    } catch (error) { reportError(error); }
  }

  function renderActivity() {
      const pages = Math.max(1, Math.ceil(state.activityItems.length / activityPageSize));
      state.activityPage = Math.min(state.activityPage, pages);
      const items = state.activityItems.slice((state.activityPage - 1) * activityPageSize, state.activityPage * activityPageSize);
      $("#state.activityPage").textContent = `第 ${state.activityPage} / ${pages} 页 · 共 ${state.activityItems.length} 条`;
      $("#activityPrev").disabled = state.activityPage <= 1;
      $("#activityNext").disabled = state.activityPage >= pages;
      $("#activityList").innerHTML = items.length ? items.map((item) => `<article class="activity-item ${escapeHtml(item.status)}"><time class="activity-time">${escapeHtml(fullTime(item.time))}</time><span class="activity-node" aria-hidden="true"></span><div class="activity-title"><strong>${escapeHtml(item.title)}</strong></div><span class="activity-result ${item.status === "failed" ? "failed" : ""}"><i></i>${item.status === "failed" ? "已恢复" : "成功"}</span><span class="activity-code">${escapeHtml(item.code || "")}</span></article>`).join("") : '<div class="empty-state compact"><p>当前还没有运行记录</p></div>';
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
          uncertain ? "结果暂未确认；请先检查微信团队私聊，避免重复发送" : "本次发送未成功",
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
    badge.innerHTML = `<span></span>${hasFailure ? "发送失败" : hasPending ? "结果待确认" : hasSkipped ? "未发送" : traces.length ? "发送完成" : "等待生成"}`;
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
    const resultLabel = hasFailure ? "发送失败" : hasPending ? "结果待确认" : hasSkipped ? "未发送" : "已发送到微信";
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
