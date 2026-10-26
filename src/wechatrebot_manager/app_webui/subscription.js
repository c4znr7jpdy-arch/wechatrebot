/* Subscription presentation. The service and runtime remain the authority. */
(function () {
  "use strict";

  window.createSubscriptionPage = function (helpers) {
    const {api, escapeHtml: esc, confirmAction, copyText, navigate, openMaintenance, refreshRuntime} = helpers;
    const $ = (selector) => document.querySelector(selector);
    const state = {snapshot: null, preview: null, runtime: null, busy: false, loading: false,
      epoch: 0, activePage: "home", plansKey: "", readError: "", expiredSeen: false, initialized: false,
      contact: null, contactLoading: false, contactReadAt: 0, contactExpanded: false, contactExpiryTimer: null,
      trial: null, trialAccount: null, trialLoading: false, trialEpoch: 0, trialError: ""};
    const contactRefreshMs = 60000;
    const monotonicNow = () => typeof performance !== "undefined" && typeof performance.now === "function"
      ? performance.now() : Date.now();
    const pausedStates = new Set(["expired", "subscription_expired", "revoked", "license_revoked", "license_unavailable",
      "suspended", "offline_expired", "lease_expired", "account_mismatch", "clock_error", "invalid"]);
    const labels = {
      disabled: ["尚未启用商业授权", "未启用", "neutral"],
      unconfigured: ["授权服务待配置", "待配置", "warning"],
      unactivated: ["选择试用或兑换授权", "未激活", "neutral"],
      missing: ["选择试用或兑换授权", "未激活", "neutral"],
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

    function freeTrial(snapshot = state.snapshot) {
      const account = accountId(snapshot?.current_account);
      const plan = typeof snapshot?.plan === "object" ? snapshot.plan?.id : snapshot?.plan;
      if (plan && plan !== "trial" && snapshot?.authorized) return {state: "paid"};
      if (plan === "trial") {
        if (["expired", "subscription_expired"].includes(snapshot.state)) return {state: "ended"};
        if (snapshot.authorized && account && account === accountId(snapshot?.account)) return {state: "active", valid_until: snapshot.valid_until,
          remaining_seconds: snapshot.remaining_seconds};
      }
      return snapshot?.free_trial || (state.trialAccount === account ? state.trial : null) || {};
    }

    function freeTrialState(snapshot = state.snapshot) {
      const value = freeTrial(snapshot);
      const stateName = String(value.state || value.status || "").trim().toLowerCase();
      return ["eligible", "active", "ended", "used", "paid"].includes(stateName) ? stateName : "unavailable";
    }

    function freeTrialEnd(value) {
      const seconds = Number(value?.valid_until ?? value?.expires_at ?? value?.ends_at);
      return Number.isFinite(seconds) && seconds > 0 ? expiryText(seconds) : "";
    }

    function freeTrialStatusText(snapshot = state.snapshot) {
      const value = freeTrial(snapshot);
      const status = freeTrialState(snapshot);
      if (!snapshot?.enabled || !snapshot?.configured) return "免费试用暂不可用。";
      if (status === "ended") return "你的试用已结束，可联系群主购买";
      if (status === "used") return "当前设备或微信账号已领取过试用，可联系群主购买";
      if (status === "paid") return "当前已有付费授权。";
      if (!accountId(snapshot?.current_account)) return "请先连接微信，再激活免费试用。";
      if (state.trialLoading && status === "unavailable") return "正在查询免费试用资格…";
      if (state.trialError && status === "unavailable") return state.trialError;
      if (status === "active") {
        const end = freeTrialEnd(value);
        const remaining = Math.max(0, Math.floor(Number(value.remaining_seconds) || 0));
        if (end) return `免费试用进行中，结束时间：${end}`;
        if (remaining) return `免费试用进行中，剩余 ${Math.floor(remaining / 60)} 分 ${remaining % 60} 秒。`;
        return "免费试用进行中。";
      }
      return status === "eligible" ? "可激活 1 小时免费试用，激活后立即开始计时。" : "免费试用资格暂未读取。";
    }

    function renderFreeTrialStatus() {
      const node = $("#licenseTrialStatus");
      if (!node) return;
      const status = freeTrialState();
      node.textContent = freeTrialStatusText();
      node.dataset.state = status;
      $("#licenseTrialRetry").hidden = !state.trialError;
      $("#licenseTrialRetry").disabled = state.trialLoading || state.busy;
    }

    async function loadFreeTrial({force = false} = {}) {
      const data = state.snapshot;
      const account = accountId(data?.current_account);
      if (!data?.enabled || !data?.configured || !account || state.trialLoading
        || (!force && state.trialAccount === account)) return;
      const epoch = ++state.trialEpoch;
      state.trialLoading = true; state.trialError = "";
      renderFreeTrialStatus(); renderControls();
      try {
        const result = await api("get_trial_status");
        if (epoch !== state.trialEpoch || account !== accountId(state.snapshot?.current_account)) return;
        if (!result?.free_trial || typeof result.free_trial !== "object") throw new Error("免费试用资格未能读取，请重试。");
        if (accountId(result.free_trial.account) && accountId(result.free_trial.account) !== account) {
          throw new Error("微信登录账号已变化，请重新查询试用资格。");
        }
        state.trial = result.free_trial;
        state.trialAccount = account;
      } catch (error) {
        if (epoch === state.trialEpoch) {
          state.trialError = error?.product?.message || error?.message || "免费试用资格未能读取，请重试。";
          state.trialAccount = account;
        }
      } finally {
        if (epoch === state.trialEpoch) {
          state.trialLoading = false;
          renderFreeTrialStatus(); renderPlans(); renderControls();
        }
      }
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

    function contactExpiryText(value) {
      const date = new Date(Number(value) * 1000);
      if (!Number.isFinite(date.getTime()) || Number(value) <= 0) return "";
      const text = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", year: "numeric", month: "long",
        day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"}).format(date);
      return `${text.replace(/\s+00:00:00$/, "")}前有效（北京时间）`;
    }

    function normalizeContact(data) {
      const contact = {state: "unavailable", group_name: String(data?.group_name || "").trim(),
        expires_at: Number(data?.expires_at) || 0, image_data_url: "", message: String(data?.message || ""),
        source: ["bundled", "remote"].includes(data?.source) ? data.source : ""};
      const remaining = Number(data?.remaining_seconds);
      if (data?.state === "expired" || (data?.state === "ready" && Number.isFinite(remaining) && remaining <= 0)) {
        contact.state = "expired";
      } else if (data?.state === "ready" && Number.isFinite(contact.expires_at) && contact.expires_at > 0
        && Number.isFinite(remaining) && remaining > 0
        && typeof data.image_data_url === "string" && data.image_data_url.length < 4 * 1024 * 1024
        && /^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/.test(data.image_data_url)) {
        contact.state = "ready";
        contact.image_data_url = data.image_data_url;
        contact.hide_at = monotonicNow() + remaining * 1000;
      }
      return contact;
    }

    function renderContact() {
      if (state.contact?.state === "ready" && state.contact.hide_at <= monotonicNow()) {
        state.contact = {...state.contact, state: "expired", image_data_url: ""};
      }
      const data = state.contact;
      const ready = data?.state === "ready";
      if (!ready) state.contactExpanded = false;
      const image = $("#licenseQrImage");
      if (ready) {
        if (image.getAttribute("src") !== data.image_data_url) image.setAttribute("src", data.image_data_url);
        image.alt = `${data.group_name || "微信群"}入群二维码`;
      } else {
        image.removeAttribute("src");
        image.alt = "";
      }
      image.hidden = !ready;
      $("#licenseQrFrame").hidden = !ready;
      $("#licenseContactPanel").classList.toggle("is-expanded", state.contactExpanded);
      $("#licenseContactPanel").dataset.contactState = data?.state || "loading";
      $("#licenseGroupName").textContent = data?.group_name || (state.contactLoading ? "正在读取" : "待更新");
      $("#licenseContactExpiry").textContent = contactExpiryText(data?.expires_at);
      $("#licenseContactExpiry").hidden = !$("#licenseContactExpiry").textContent;
      $("#licenseContactMessage").textContent = data?.state === "expired"
        ? "入群二维码已过期，请刷新获取最新二维码或联系管理员。已有激活码仍可兑换。"
        : ready ? data.message || "微信扫码入群后，联系管理员领取激活码。"
        : !data && state.contactLoading ? "正在加载入群二维码…"
        : `${data?.message || "暂时无法获取入群二维码，请刷新重试或联系管理员。"} 已有激活码仍可兑换。`;
      $("#licenseQrToggle").hidden = !ready;
      $("#licenseQrToggle").textContent = state.contactExpanded ? "收起二维码" : "放大二维码";
      $("#licenseQrToggle").setAttribute("aria-expanded", String(state.contactExpanded));
      $("#licenseQrRefresh").disabled = state.contactLoading;
      $("#licenseQrRefresh").textContent = state.contactLoading ? "刷新中…" : "刷新二维码";
    }

    function scheduleContactExpiry() {
      window.clearTimeout(state.contactExpiryTimer);
      state.contactExpiryTimer = null;
      if (state.contact?.state !== "ready") return;
      const delay = Math.max(0, state.contact.hide_at - monotonicNow());
      state.contactExpiryTimer = window.setTimeout(() => {
        renderContact();
        scheduleContactExpiry();
      }, Math.min(delay + 1, 2147483647));
    }

    async function loadContact(force = false) {
      if (state.contactLoading) return;
      state.contactLoading = true;
      state.contactReadAt = monotonicNow();
      renderContact();
      try { state.contact = normalizeContact(await api("get_license_contact", force)); }
      catch (error) {
        state.contact = {state: "unavailable", image_data_url: "", message:
          error?.product?.message || "暂时无法获取入群二维码，请刷新重试或联系管理员。"};
      } finally {
        state.contactLoading = false;
        renderContact();
        scheduleContactExpiry();
      }
    }

    function refreshContactIfNeeded() {
      renderContact();
      if (!state.contact || monotonicNow() - state.contactReadAt >= contactRefreshMs) void loadContact();
    }

    function showGroup() {
      refreshContactIfNeeded();
      $("#licenseContactPanel").scrollIntoView({block: "start"});
      $("#licenseContactPanel").focus({preventScroll: true});
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
      const accountChanged = accountId(state.snapshot?.current_account) !== accountId(data.current_account);
      const licenseChanged = String(state.snapshot?.license_id || "") !== String(data.license_id || "")
        || String(state.snapshot?.plan?.id || state.snapshot?.plan || "") !== String(data.plan?.id || data.plan || "");
      state.snapshot = data;
      state.readError = "";
      if (accountChanged || licenseChanged) {
        state.trial = null; state.trialAccount = null; state.trialError = "";
        state.trialEpoch += 1; state.trialLoading = false;
      }
      if (["expired", "subscription_expired"].includes(data.state)) state.expiredSeen = true;
      render();
      if (accountChanged && state.activePage === "subscription") void loadFreeTrial();
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
      const key = JSON.stringify(plans);
      if (key === state.plansKey) return;
      state.plansKey = key;
      $("#licensePlans").innerHTML = plans.map((plan) => {
        const amount = Number(plan.price_fen);
        const knownPrice = Number.isFinite(amount) && amount >= 0;
        const price = knownPrice ? new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2}).format(amount / 100) : "待确认";
        const isTrial = plan.id === "trial";
        const unit = isTrial ? " / 1 小时" : ["yearly", "year"].includes(plan.id) ? " / 年" : " / 月";
        const trialButton = isTrial
          ? '<p class="subscription-trial-status" id="licenseTrialStatus" role="status" aria-live="polite"></p><button class="button ghost small" id="licenseTrialRetry" data-trial-retry type="button" hidden>重新查询试用资格</button><button class="button ghost" id="licenseActivateTrial" data-free-trial type="button" disabled>激活免费试用</button>'
          : `<button class="button secondary" data-license-plan type="button">扫码入群领取</button>`;
        return `<article class="subscription-plan panel" data-plan="${esc(plan.id)}"><h3 class="subscription-plan-name">${esc(plan.name)}</h3><div class="subscription-plan-price">${knownPrice ? "<span>¥</span>" : ""}<strong>${esc(price)}</strong><small>${esc(unit)}</small></div><p>${esc(plan.description || (isTrial ? "每台设备和微信账号限用一次，激活后连续计时 1 小时。" : "1 个机器人账号，含订阅期内更新。"))}</p>${trialButton}</article>`;
      }).join("") || '<p class="subtle">套餐暂时无法读取，请重新读取授权状态。</p>';
    }

    function renderControls() {
      const data = state.snapshot;
      const ready = Boolean(data?.enabled && data?.configured);
      const busy = state.busy;
      const bound = accountId(data?.account);
      const current = accountId(data?.current_account);
      $("#licenseRefresh").disabled = busy || state.loading;
      $("#licenseRefresh").textContent = busy ? "处理中…" : ready ? "重新验证" : "重新读取";
      $("#licenseCode").disabled = busy || !ready;
      $("#licensePreviewCode").disabled = busy || !ready || !$("#licenseCode").value.trim();
      $("#licenseConfirmRedeem").disabled = busy || !ready || !state.preview;
      $("#licenseCancelRedeem").disabled = busy;
      $("#licenseCopyId").disabled = !data?.license_id;
      $("#licenseCopyDevice").disabled = !data?.device_id;
      $("#licensePrepareDevice").hidden = !ready || Boolean(data?.device_id);
      $("#licensePrepareDevice").disabled = busy || !ready;
      $("#licenseConnect").hidden = !ready || !data.can_connect || Boolean(current);
      $("#licenseConnect").disabled = busy || Boolean(state.runtime?.busy);
      $("#licenseBind").hidden = !ready || !data.can_connect || !data.license_id || Boolean(bound) || !current;
      $("#licenseBind").disabled = busy;
      const running = Boolean(state.runtime?.online && data?.can_run);
      $("#licenseResume").hidden = !ready || !readyToStart();
      $("#licenseResume").disabled = busy || running || Boolean(state.runtime?.busy);
      $("#licenseResume").textContent = running ? "自动化运行中" : state.expiredSeen ? "恢复机器人" : "启动机器人";
      const trialButton = $("#licenseActivateTrial");
      if (trialButton) {
        const trial = freeTrial(data), status = freeTrialState(data);
        trialButton.disabled = busy || state.trialLoading || !ready || !current || !(trial.eligible || trial.recoverable);
        trialButton.textContent = busy ? "处理中…" : trial.recoverable ? "恢复免费试用"
          : status === "active" ? "试用进行中" : status === "ended" ? "试用已结束" : status === "used" ? "已领取试用"
          : status === "paid" ? "已订阅" : "激活免费试用";
      }
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
      $("#licenseCurrentAccountRow").hidden = !data.enabled || (!data.license_id && !accountId(data.current_account));
      $("#licensePausedInfo").hidden = !data.enabled || !pausedStates.has(data.state);
      $("#licenseRedeemTitle").textContent = data.license_id ? "兑换续期码" : "兑换激活码";
      $("#licenseCodeHelp").textContent = !data.enabled ? "商业授权尚未启用，暂不接受激活码。" : !data.configured
        ? "授权服务尚未配置，暂时无法兑换激活码。" : "先查看套餐和有效期，确认后再兑换。";
      renderSteps(); renderPlans(); renderFreeTrialStatus(); renderControls(); renderHome();
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

    async function activateFreeTrial() {
      const trial = freeTrial();
      if (state.busy || state.trialLoading || !(trial.eligible || trial.recoverable)) return;
      const account = accountId(state.snapshot?.current_account);
      if (!account) return;
      await execute(() => api("activate_free_trial", account), trial.recoverable
        ? "免费试用已恢复，结束时间不变。" : "免费试用已激活，连续计时 1 小时。请确认模型设置后启动机器人。");
      await loadFreeTrial({force: true});
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
      $("#licenseQrRefresh").addEventListener("click", () => loadContact(true));
      $("#licenseQrToggle").addEventListener("click", () => {
        renderContact();
        if (state.contact?.state !== "ready") return;
        state.contactExpanded = !state.contactExpanded;
        renderContact();
        $("#licenseQrImage").scrollIntoView({block: "nearest"});
      });
      $("#licenseQrImage").addEventListener("error", () => {
        if (state.contact?.state !== "ready") return;
        state.contact = {...state.contact, state: "unavailable", image_data_url: "",
          message: "二维码暂时无法显示，请刷新重试。"};
        renderContact(); scheduleContactExpiry();
      });
      $("#licenseCopyId").addEventListener("click", () => copyText(state.snapshot?.license_id, "授权编号"));
      $("#licenseCopyDevice").addEventListener("click", () => copyText(state.snapshot?.device_id, "设备编号"));
      $("#licensePrepareDevice").addEventListener("click", () => execute(() => api("prepare_license_device"), "本机设备编号已生成，可复制给群管理员办理授权迁移。"));
      $("#licensePlans").addEventListener("click", (event) => {
        if (event.target.closest("[data-trial-retry]")) return loadFreeTrial({force: true});
        if (event.target.closest("[data-free-trial]")) return activateFreeTrial();
        if (event.target.closest("[data-license-plan]")) showGroup();
      });
      $("#licenseOpenHistory").addEventListener("click", () => navigate("history"));
      $("#licenseOpenMaintenance").addEventListener("click", openMaintenance);
      window.setInterval(() => {
        if (["subscription", "home"].includes(state.activePage) && document.visibilityState !== "hidden") void load({silent: true});
      }, 4000);
      window.setInterval(() => {
        if (state.activePage === "subscription" && document.visibilityState !== "hidden") void loadContact(true);
      }, contactRefreshMs);
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState !== "hidden" && state.activePage === "subscription") refreshContactIfNeeded();
      });
      void loadContact();
      return load();
    }

    return {
      initialize,
      async onNavigate(page) { state.activePage = page; if (page === "subscription") { refreshContactIfNeeded(); await load(); await loadFreeTrial(); } },
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
