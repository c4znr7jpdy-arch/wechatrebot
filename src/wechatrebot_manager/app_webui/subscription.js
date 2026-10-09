/* Subscription presentation. The service and runtime remain the authority. */
(function () {
  "use strict";

  window.createSubscriptionPage = function (helpers) {
    const {api, escapeHtml: esc, confirmAction, copyText, navigate, openMaintenance, refreshRuntime} = helpers;
    const $ = (selector) => document.querySelector(selector);
    const state = {snapshot: null, preview: null, runtime: null, busy: false, loading: false,
      epoch: 0, activePage: "home", plansKey: "", readError: "", expiredSeen: false, initialized: false};
    const pausedStates = new Set(["expired", "subscription_expired", "revoked", "license_revoked", "license_unavailable",
      "suspended", "offline_expired", "lease_expired", "account_mismatch", "clock_error", "invalid"]);
    const labels = {
      disabled: ["尚未启用商业授权", "未启用", "neutral"],
      unconfigured: ["授权服务待配置", "待配置", "warning"],
      unactivated: ["领取激活码，开始使用", "未激活", "neutral"],
      missing: ["领取激活码，开始使用", "未激活", "neutral"],
      pending_account: ["请连接并确认微信账号", "待绑定", "warning"],
      pending: ["请连接并确认微信账号", "待绑定", "warning"],
      active_unbound: ["请连接并确认微信账号", "待绑定", "warning"],
      active: ["授权有效", "已授权", "success"],
      ready: ["授权有效", "已授权", "success"],
      running: ["授权有效", "已授权", "success"],
      paused: ["授权有效，等待恢复", "已暂停", "warning"],
      offline: ["授权有效，等待联网验证", "离线可用", "warning"],
      expired: ["授权已到期", "已到期", "warning"],
      revoked: ["授权已撤销", "已暂停", "danger"],
      suspended: ["授权已暂停", "已暂停", "warning"],
      offline_expired: ["请联网重新验证授权", "待验证", "warning"],
      lease_expired: ["请联网重新验证授权", "待验证", "warning"],
      account_mismatch: ["当前微信与授权不一致", "需处理", "warning"],
      service_unavailable: ["暂时无法验证授权", "待验证", "warning"],
      clock_error: ["请联网重新验证时间", "待验证", "warning"],
      invalid: ["授权暂不可用", "需处理", "danger"],
    };
    labels.subscription_expired = labels.expired;
    labels.license_revoked = labels.revoked;

    function accountId(value) {
      if (typeof value === "string") return value.trim();
      return String(value?.wxid || value?.account_id || value?.wechat_identity || value?.id || "").trim();
    }

    function planName(plan, snapshot = state.snapshot) {
      if (plan && typeof plan === "object") return String(plan.name || plan.id || "未激活");
      const plans = Array.isArray(snapshot?.plans) ? snapshot.plans : [];
      return plans.find((item) => item.id === plan)?.name || String(plan || "未激活");
    }

    function readyToStart() {
      const data = state.snapshot;
      return Boolean(data?.authorized && accountId(data.account) && accountId(data.account) === accountId(data.current_account));
    }

    function expiryText(value) {
      const seconds = Number(value);
      if (!Number.isFinite(seconds) || seconds <= 0) return "—";
      const date = new Date(seconds * 1000);
      if (!Number.isFinite(date.getTime())) return "—";
      return new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit",
        day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false}).format(date);
    }

    function description() {
      const data = state.snapshot;
      if (!data) return ["授权状态暂未读取", "待读取", "neutral"];
      if (!data.enabled) return labels.disabled;
      if (!data.configured) return labels.unconfigured;
      return labels[data.state] || (data.authorized ? labels.active : ["授权暂不可用", "需处理", "warning"]);
    }

    function feedback(message, tone = "neutral", field = false) {
      const node = $(field ? "#licenseCodeFeedback" : "#licenseFeedback");
      node.textContent = message;
      node.hidden = !message;
      node.dataset.tone = tone;
      node.setAttribute("role", tone === "error" ? "alert" : "status");
    }

    function showError(error, field = false) {
      feedback(error?.product?.message || error?.message || "操作未完成，请稍后重试。", "error", field);
    }

    function updateSnapshot(data) {
      if (!data || typeof data.enabled !== "boolean") throw new Error("授权状态未能读取，请重新验证。");
      state.snapshot = data;
      state.readError = "";
      if (["expired", "subscription_expired"].includes(data.state)) state.expiredSeen = true;
      render();
    }

    async function load({silent = false} = {}) {
      if (state.loading || state.busy) return state.snapshot;
      state.loading = true;
      const epoch = state.epoch;
      try {
        const data = await api("get_license_status");
        if (epoch === state.epoch) updateSnapshot(data);
      } catch (error) {
        if (epoch === state.epoch) {
          state.readError = error?.message || "暂时无法读取授权状态。";
          if (!silent || !state.snapshot) showError(error);
          renderHome();
        }
      } finally { state.loading = false; renderControls(); }
      return state.snapshot;
    }

    function renderHome() {
      const data = state.snapshot;
      const remaining = Number(data?.remaining_seconds);
      const expiring = readyToStart() && Number.isFinite(remaining) && remaining > 0 && remaining <= 7 * 86400;
      const visible = Boolean(data?.enabled && (!readyToStart() || expiring || state.readError || data.state === "offline" || data.last_error === "service_unavailable"));
      $("#homeLicenseNotice").hidden = !visible;
      if (!visible) return;
      $("#homeLicenseTitle").textContent = state.readError ? "授权状态暂时无法读取" : expiring ? "授权即将到期" : description()[0];
      $("#homeLicenseMessage").textContent = state.readError || (expiring
        ? `有效期至 ${data.expires_text || expiryText(data.valid_until)}，可在“订阅与授权”兑换续期码。`
        : data.message || "进入订阅与授权，查看下一步。配置和数据仍可访问。");
      $("#homeLicenseAction").textContent = ["expired", "subscription_expired"].includes(data.state) ? "兑换续期码" : "查看授权";
      $("#homeLicenseAction").disabled = false;
    }

    function renderSteps() {
      const data = state.snapshot;
      const hasCode = Boolean($("#licenseCode").value.trim() || data?.license_id);
      const done = [hasCode, Boolean(data?.license_id), Boolean(accountId(data?.account))];
      ["#licenseStepCode", "#licenseStepRedeem", "#licenseStepAccount"].forEach((id, index) => {
        const node = $(id);
        node.classList.toggle("is-done", done[index]);
        node.querySelector("span").textContent = done[index] ? "✓" : String(index + 1);
        if (!done[index] && (index === 0 || done[index - 1])) node.setAttribute("aria-current", "step");
        else node.removeAttribute("aria-current");
      });
    }

    function renderPlans() {
      const data = state.snapshot;
      const plans = Array.isArray(data?.plans) ? data.plans : [];
      const key = JSON.stringify([plans, data?.qq_group, data?.qq_url]);
      if (key === state.plansKey) return;
      state.plansKey = key;
      $("#licensePlans").innerHTML = plans.map((plan) => {
        const amount = Number(plan.price_fen);
        const knownPrice = Number.isFinite(amount) && amount >= 0;
        const price = knownPrice ? new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2}).format(amount / 100) : "待确认";
        const trial = plan.id === "trial";
        const unit = trial ? " / 1 小时" : ["yearly", "year"].includes(plan.id) ? " / 年" : " / 月";
        return `<article class="subscription-plan panel" data-plan="${esc(plan.id)}"><h3 class="subscription-plan-name">${esc(plan.name)}</h3><div class="subscription-plan-price">${knownPrice ? "<span>¥</span>" : ""}<strong>${esc(price)}</strong><small>${esc(unit)}</small></div><p>${esc(plan.description || (trial ? "领取试用码后激活，体验完整功能。" : "1 个机器人账号，含订阅期内更新。"))}</p><button class="button ${trial ? "ghost" : "secondary"}" data-license-plan type="button" disabled>加群领取</button></article>`;
      }).join("") || '<p class="subtle">套餐暂时无法读取，请重新读取授权状态。</p>';
    }

    function renderControls() {
      const data = state.snapshot;
      const ready = Boolean(data?.enabled && data?.configured);
      const busy = state.busy;
      const bound = accountId(data?.account);
      const current = accountId(data?.current_account);
      const hasGroup = Boolean(data?.qq_group || data?.qq_url);
      $("#licenseRefresh").disabled = busy || state.loading;
      $("#licenseRefresh").textContent = busy ? "处理中…" : ready ? "重新验证" : "重新读取";
      $("#licenseCode").disabled = busy || !ready;
      $("#licensePreviewCode").disabled = busy || !ready || !$("#licenseCode").value.trim();
      $("#licenseConfirmRedeem").disabled = busy || !ready || !state.preview;
      $("#licenseCancelRedeem").disabled = busy;
      $("#licenseOpenGroup").disabled = busy || !data?.qq_url;
      $("#licenseCopyGroup").disabled = !hasGroup;
      $("#licenseCopyId").disabled = !data?.license_id;
      $("#licenseCopyDevice").disabled = !data?.device_id;
      $("#licensePrepareDevice").hidden = !ready || Boolean(data?.device_id);
      $("#licensePrepareDevice").disabled = busy || !ready;
      $("#licenseCopyGroup").lastChild.textContent = !data?.qq_group && data?.qq_url ? "复制加群链接" : "复制群号";
      document.querySelectorAll("[data-license-plan]").forEach((button) => {
        button.disabled = busy || !hasGroup;
        button.textContent = !hasGroup ? "QQ 群待配置" : data.qq_url ? "加群领取" : "复制群号领取";
      });
      $("#licenseConnect").hidden = !ready || !data.can_connect || !data.license_id || Boolean(current);
      $("#licenseConnect").disabled = busy || Boolean(state.runtime?.busy);
      $("#licenseBind").hidden = !ready || !data.can_connect || !data.license_id || Boolean(bound) || !current;
      $("#licenseBind").disabled = busy;
      const running = Boolean(state.runtime?.online && data?.can_run);
      $("#licenseResume").hidden = !ready || !readyToStart();
      $("#licenseResume").disabled = busy || running || Boolean(state.runtime?.busy);
      $("#licenseResume").textContent = running ? "自动化运行中" : state.expiredSeen ? "恢复机器人" : "启动机器人";
    }

    function render() {
      const data = state.snapshot;
      if (!data) return;
      const [title, tag, tone] = description();
      $("#licenseStateTitle").textContent = title;
      $("#licenseStateTag").textContent = tag;
      $("#licenseStatusPanel").dataset.tone = tone;
      $("#licenseStateMessage").textContent = data.message || (data.authorized
        ? "授权已生效。确认模型设置后，由你启动机器人。"
        : !data.enabled ? "此安装尚未启用商业授权，现有功能可按原方式使用。" : "领取激活码后，在下方查看并确认兑换。");
      $("#licensePlan").textContent = planName(data.plan);
      $("#licenseExpires").textContent = data.expires_text || expiryText(data.valid_until);
      const seconds = Math.max(0, Math.floor(Number(data.remaining_seconds) || 0));
      $("#licenseRemaining").textContent = seconds > 0 && seconds <= 3600 ? `剩余 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : "";
      $("#licenseAccount").textContent = accountId(data.account) || "尚未绑定";
      $("#licenseIdText").textContent = data.license_id || "尚未激活";
      $("#licenseDeviceText").textContent = data.device_id || "尚未生成";
      $("#licenseCurrentAccount").textContent = accountId(data.current_account) || "尚未连接";
      $("#licenseCurrentAccountRow").hidden = !data.enabled || !data.license_id;
      $("#licensePausedInfo").hidden = !data.enabled || !pausedStates.has(data.state);
      $("#licenseGroupNumber").textContent = data.qq_group || (data.qq_url ? "通过加群链接加入" : "待配置");
      $("#licenseGroupNote").textContent = data.qq_group || data.qq_url ? "向管理员说明所需套餐，领取激活码后在此页面兑换。" : "加群方式尚未配置，请联系软件提供者。";
      $("#licenseRedeemTitle").textContent = data.license_id ? "兑换续期码" : "兑换激活码";
      $("#licenseCodeHelp").textContent = !data.enabled ? "商业授权尚未启用，暂不接受激活码。" : !data.configured
        ? "授权服务尚未配置，暂时无法兑换激活码。" : "先查看套餐和有效期，确认后再兑换。";
      renderSteps(); renderPlans(); renderControls(); renderHome();
    }

    async function transact(action, message = "") {
      if (state.busy) throw new Error("上一项操作尚未完成，请稍候。");
      state.busy = true; state.epoch += 1;
      renderControls();
      try {
        const result = await action();
        if (typeof result?.enabled === "boolean") updateSnapshot(result);
        else if (typeof result?.snapshot?.enabled === "boolean") updateSnapshot(result.snapshot);
        if (message) feedback(message, "success");
        return result;
      } finally {
        state.busy = false;
        await load({silent: true});
        renderControls();
      }
    }

    async function execute(action, message = "") {
      try { return await transact(action, message); }
      catch (error) { showError(error); return null; }
    }

    function clearPreview() {
      state.preview = null;
      $("#licenseCodePreview").hidden = true;
      $("#licenseCodePreviewDetails").textContent = "";
      renderControls();
    }

    async function previewCode() {
      const code = $("#licenseCode").value.trim();
      if (!code || state.busy) return;
      clearPreview(); feedback("", "neutral", true);
      try {
        const data = await transact(() => api("preview_activation_code", code));
        if (!data?.preview_id) throw new Error("激活码预览未完成，请重新查看。");
        state.preview = {code, data};
        const mode = data.already_redeemed ? "恢复已有授权" : ({new: "新开通", activation: "新开通", renewal: "续期", recovery: "补领授权", migration: "迁移到本机"}[data.mode] || "开通授权");
        const rows = [["套餐", planName(data.plan)], ["操作", mode], ["兑换后到期", expiryText(data.valid_until)]];
        if (data.replaces_license_id) rows.push([data.already_redeemed ? "已停用授权" : "将停用授权", data.replaces_license_id], ["迁入授权", data.license_id]);
        $("#licenseCodePreviewDetails").innerHTML = rows.map(([name, value]) => `<div><dt>${esc(name)}</dt><dd>${esc(value)}</dd></div>`).join("");
        const replacement = !data.replaces_license_id ? "" : data.already_redeemed
          ? "目标电脑原授权已停用，本次只恢复已迁入的授权，不重复兑换或延长有效期。"
          : "目标电脑原试用或到期授权将被停用，迁入原授权的到期时间不变。";
        const defaultMessage = data.already_redeemed ? "本次仅恢复已兑换授权，不重复增加有效期。"
          : data.plan === "trial" ? "试用从确认兑换成功开始，连续计时 1 小时。" : "请核对套餐与有效期，确认后兑换到当前设备。";
        $("#licenseCodePreviewMessage").textContent = [data.message || defaultMessage, replacement].filter(Boolean).join(" ");
        $("#licenseConfirmRedeem").textContent = data.already_redeemed ? "确认恢复授权" : data.mode === "migration" ? "确认迁移" : "确认兑换";
        $("#licenseCodePreview").hidden = false;
        renderControls(); $("#licenseConfirmRedeem").focus();
      } catch (error) { showError(error, true); }
    }

    async function redeemCode() {
      const preview = state.preview;
      if (!preview || state.busy) return;
      if (preview.code !== $("#licenseCode").value.trim()) { clearPreview(); return; }
      try {
        await transact(async () => {
          const result = await api("redeem_activation_code", preview.code, preview.data.preview_id);
          $("#licenseCode").value = "";
          clearPreview(); feedback("", "neutral", true);
          return result;
        }, "激活码已兑换。请根据授权状态完成微信绑定，或确认恢复机器人。");
        const next = ["#licenseConnect", "#licenseBind", "#licenseResume"].map($).find((button) => !button.hidden && !button.disabled);
        next?.focus();
      } catch (error) { showError(error, true); }
    }

    function bindAccount() {
      const account = accountId(state.snapshot?.current_account);
      if (!account || state.busy) return;
      confirmAction({title: "确认授权微信账号", message: `将当前设备的授权绑定到微信账号 ${account}。请核对这是你要用于机器人的账号。`,
        confirmText: "确认绑定此账号", intent: "primary", onConfirm: async () => {
          await transact(() => api("bind_license_account", account), "微信账号已绑定。确认模型设置后，可以启动机器人。");
        }});
    }

    async function openGroup() {
      const data = state.snapshot;
      if (data?.qq_url) await execute(() => api("open_license_group"));
      else if (data?.qq_group) await copyText(data.qq_group, "QQ 群号");
    }

    async function runtimeAction(method, message) {
      const result = await execute(async () => {
        const response = await api(method);
        if (response?.accepted === false) throw new Error("操作尚未开始，请按运行提示完成处理后重试。");
        return response;
      }, message);
      if (result !== null) { await refreshRuntime(); await load({silent: true}); }
    }

    async function initialize() {
      if (state.initialized) return load();
      state.initialized = true;
      $("#homeLicenseAction").addEventListener("click", () => navigate("subscription"));
      $("#licenseRefresh").addEventListener("click", () => state.snapshot?.enabled && state.snapshot?.configured
        ? execute(() => api("refresh_license"), "已重新验证授权状态。") : load());
      $("#licenseCodeForm").addEventListener("submit", (event) => { event.preventDefault(); void previewCode(); });
      $("#licenseCode").addEventListener("input", () => { clearPreview(); feedback("", "neutral", true); renderSteps(); });
      $("#licenseConfirmRedeem").addEventListener("click", redeemCode);
      $("#licenseCancelRedeem").addEventListener("click", () => { clearPreview(); $("#licenseCode").focus(); });
      $("#licenseConnect").addEventListener("click", () => runtimeAction("connect_license_wechat", "请在微信完成登录，然后在此确认实际账号。"));
      $("#licenseBind").addEventListener("click", bindAccount);
      $("#licenseResume").addEventListener("click", () => runtimeAction("start_robot", "已提交启动请求，请在运行中心查看启动进度。"));
      $("#licenseOpenGroup").addEventListener("click", openGroup);
      $("#licenseCopyGroup").addEventListener("click", () => copyText(state.snapshot?.qq_group || state.snapshot?.qq_url, state.snapshot?.qq_group ? "QQ 群号" : "加群链接"));
      $("#licenseCopyId").addEventListener("click", () => copyText(state.snapshot?.license_id, "授权编号"));
      $("#licenseCopyDevice").addEventListener("click", () => copyText(state.snapshot?.device_id, "设备编号"));
      $("#licensePrepareDevice").addEventListener("click", () => execute(() => api("prepare_license_device"), "本机设备编号已生成，可复制给群管理员办理授权迁移。"));
      $("#licensePlans").addEventListener("click", (event) => { if (event.target.closest("[data-license-plan]")) void openGroup(); });
      $("#licenseOpenHistory").addEventListener("click", () => navigate("history"));
      $("#licenseOpenMaintenance").addEventListener("click", openMaintenance);
      window.setInterval(() => {
        if (["subscription", "home"].includes(state.activePage) && document.visibilityState !== "hidden") void load({silent: true});
      }, 4000);
      return load();
    }

    return {
      initialize,
      async onNavigate(page) { state.activePage = page; if (page === "subscription") await load(); },
      updateRuntime(runtime) { state.runtime = runtime; if (state.initialized) renderControls(); },
      needsEntry() { return Boolean(state.snapshot?.enabled && !readyToStart()); },
      async beforeStart() {
        await load({silent: true});
        if (!state.snapshot || state.readError || (state.snapshot.enabled && !readyToStart())) {
          await navigate("subscription");
          if (state.readError) feedback(state.readError, "error");
          return false;
        }
        return true;
      },
    };
  };
})();
