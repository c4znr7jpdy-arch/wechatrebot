/* Subscription presentation. The service and runtime remain the authority. */
(function () {
  "use strict";

  window.createSubscriptionPage = function (helpers) {
    const {api, escapeHtml: esc, confirmAction, copyText, navigate, openMaintenance, refreshRuntime, toast} = helpers;
    const $ = (selector) => document.querySelector(selector);
    const state = {snapshot: null, preview: null, runtime: null, busy: false, loading: false, preparing: false, authorizedView: null,
      epoch: 0, activePage: "home", plansKey: "", readError: "", initialized: false,
      contact: null, contactLoading: false, contactReadAt: 0, contactExpanded: false, contactExpiryTimer: null,
      binding: null, bindingPromise: null, bindingError: "", bindingEpoch: 0,
      trial: null, trialAccount: null, trialLoading: false, trialPromise: null, trialEpoch: 0, trialError: ""};
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
      pending_account: ["订阅已生效", "已订阅", "success"],
      pending: ["订阅已生效", "已订阅", "success"],
      active_unbound: ["订阅已生效", "已订阅", "success"],
      active: ["授权有效", "已授权", "success"],
      ready: ["授权有效", "已授权", "success"],
      running: ["授权有效", "已授权", "success"],
      paused: ["授权有效", "已授权", "success"],
      offline: ["授权有效，等待联网验证", "离线可用", "warning"],
      expired: ["授权已到期", "已到期", "warning"],
      revoked: ["授权已撤销", "已撤销", "danger"],
      suspended: ["授权暂不可用", "不可用", "warning"],
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

    function bindingQuotaText(binding) {
      return `剩余可换绑 ${binding.rebind_remaining} 次（已用 ${binding.rebind_used} / ${binding.rebind_limit} 次）。首次绑定不扣换绑次数。`;
    }

    function renderBindingStatus() {
      const data = state.snapshot;
      const available = Boolean(data?.enabled && data?.configured && data?.license_id);
      const trial = (typeof data?.plan === "object" ? data.plan?.id : data?.plan) === "trial";
      const description = state.bindingError || (state.binding ? bindingQuotaText(state.binding)
        : state.bindingPromise ? "正在读取换绑次数…" : available ? "请读取当前套餐的换绑次数。" : "激活后可查看换绑次数。");
      $("#licenseRebindSummary").hidden = !available;
      $("#licenseRebindSummary").title = description;
      $("#licenseRebindQuota").textContent = state.binding ? String(state.binding.rebind_remaining) : "—";
      $("#licenseRebindQuota").setAttribute("aria-label", description);
      $("#licenseRebindHint").textContent = state.bindingError || (trial ? "试用不支持换绑" : "");
      $("#licenseRebindHint").hidden = !state.bindingError && !trial;
      $("#licenseRebindHint").dataset.tone = state.bindingError ? "error" : "neutral";
      $("#licenseRebindRefresh").hidden = !available;
      $("#licenseRebindRefresh").disabled = state.busy || state.preparing || Boolean(state.bindingPromise);
      $("#licenseRebind").hidden = !available || trial || !accountId(data?.account);
      $("#licenseRebind").disabled = state.busy || state.preparing || Boolean(state.bindingPromise) || !state.binding?.can_rebind;
    }

    async function loadBindingStatus({force = false, required = false} = {}) {
      const data = state.snapshot;
      if (!data?.enabled || !data.configured || !data.license_id) {
        if (required) throw new Error("当前授权尚未就绪，无法读取换绑次数。");
        return null;
      }
      if (!state.bindingPromise && (force || !state.binding)) {
        const epoch = state.bindingEpoch, license = data.license_id, account = accountId(data.account);
        state.bindingError = "";
        state.bindingPromise = (async () => {
          try {
            const result = await api("get_license_binding_status");
            if (epoch !== state.bindingEpoch) return;
            if (result?.license_id !== license || accountId(result.account) !== account
              || typeof result.can_rebind !== "boolean" || result.revision === undefined
              || ![result.rebind_limit, result.rebind_used, result.rebind_remaining].every((value) => Number.isInteger(value) && value >= 0)) {
              throw new Error("授权或换绑次数已变化，请重新读取后再试。");
            }
            state.binding = result;
          } catch (error) {
            if (epoch === state.bindingEpoch) {
              state.binding = null;
              state.bindingError = statusMessage(error?.product || {message: error?.message || "换绑次数暂时无法读取，请重试。"});
            }
          } finally { state.bindingPromise = null; renderBindingStatus(); }
        })();
        renderBindingStatus();
      }
      await state.bindingPromise;
      if (required && !state.binding) throw new Error(state.bindingError || "授权已变化，请重新读取换绑次数。");
      return state.binding;
    }

    function planName(plan, snapshot = state.snapshot) {
      if (plan && typeof plan === "object") return String(plan.name || plan.id || "未激活");
      const plans = Array.isArray(snapshot?.plans) ? snapshot.plans : [];
      return plans.find((item) => item.id === plan)?.name || String(plan || "未激活");
    }

    function planDescription(plan) {
      const description = String(plan?.description || (plan?.id === "trial"
        ? "每台设备和微信账号限用一次，激活后连续计时 1 小时。" : "1 个机器人账号，含订阅期内更新。"));
      return description.includes("模型费用自理") ? description : `${description} 模型费用自理。`;
    }

    function freeTrial(snapshot = state.snapshot) {
      const account = accountId(snapshot?.current_account);
      const plan = typeof snapshot?.plan === "object" ? snapshot.plan?.id : snapshot?.plan;
      if (plan && plan !== "trial" && snapshot?.authorized) return {state: "paid"};
      if (plan === "trial") {
        if (["expired", "subscription_expired"].includes(snapshot.state)) return {state: "ended"};
        if (snapshot.authorized && (!account || account === accountId(snapshot?.account))) return {state: "active", valid_until: snapshot.valid_until,
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
      if (state.trialLoading && status === "unavailable") return "正在查询免费试用资格…";
      if (state.trialError && status === "unavailable") return state.trialError;
      if (status === "active") {
        const end = freeTrialEnd(value);
        const remaining = Math.max(0, Math.floor(Number(value.remaining_seconds) || 0));
        if (end) return `免费试用进行中，结束时间：${end}`;
        if (remaining) return `免费试用进行中，剩余 ${Math.floor(remaining / 60)} 分 ${remaining % 60} 秒。`;
        return "免费试用进行中。";
      }
      if (!accountId(snapshot?.current_account)) return "激活时先确认试用提示，再登录用于试用的微信账号。";
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
      if (state.trialLoading) return state.trialPromise;
      if (!data?.enabled || !data?.configured || !account
        || (!force && state.trialAccount === account)) return;
      const epoch = ++state.trialEpoch;
      state.trialLoading = true; state.trialError = "";
      renderFreeTrialStatus(); renderControls();
      state.trialPromise = (async () => {
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
            state.trialError = statusMessage(error?.product || {message: error?.message || "免费试用资格未能读取，请重试。"});
            state.trialAccount = account;
          }
        } finally {
          if (epoch === state.trialEpoch) {
            state.trialLoading = false;
            state.trialPromise = null;
            renderFreeTrialStatus(); renderPlans(); renderControls();
          }
        }
      })();
      return state.trialPromise;
    }

    function hasValidAuthorization(data = state.snapshot) {
      return Boolean(data?.authorized && !pausedStates.has(data.state));
    }

    function readyToStart() {
      const current = accountId(state.snapshot?.current_account);
      return hasValidAuthorization() && Boolean(current) && accountId(state.snapshot.account) === current;
    }

    function statusMessage(data = state.snapshot, fallback = "") {
      const original = String(fallback || data?.message || "").replaceAll("自动化已暂停", "机器人未运行");
      if (fallback && (!data || hasValidAuthorization(data) || fallback !== data.message)) return original;
      if (hasValidAuthorization(data) && ["active", "ready", "paused", "running"].includes(data.state)) {
        return data.last_error === "service_unavailable"
          ? "授权服务暂不可达，当前有效离线凭证仍可使用。可照常启动机器人。"
          : data.state === "running" ? "授权权益已生效，机器人可正常运行。" : "授权权益已生效，随时可以启动机器人。";
      }
      const code = String(data?.state || data?.code || "").replace(/^LIC-/, "");
      const reason = ({subscription_expired: "expired", license_revoked: "revoked", lease_expired: "offline_expired",
        missing: "unactivated", pending_account: "active_unbound", pending: "active_unbound"})[code] || code;
      const plan = typeof data?.plan === "object" ? data.plan?.id : data?.plan;
      const messages = {
        unactivated: "当前未授权。请激活免费试用或兑换付费激活码，再启动机器人。",
        configuration_pending: "授权服务尚未配置。请联系管理员完成配置后重新读取授权状态。",
        expired: plan === "trial" ? "免费试用已结束，当前未授权。请联系管理员购买付费激活码后兑换。"
          : "订阅已到期，机器人未运行。请兑换续期码，续期后点击“启动机器人”。",
        revoked: "授权已撤销，当前未授权。请联系管理员或兑换新的付费激活码。",
        suspended: "授权暂不可用，机器人未运行。请联系管理员处理后重新验证。",
        license_unavailable: "当前设备授权不可用，机器人未运行。请联系管理员恢复或迁移授权。",
        offline_expired: "离线授权已过期，机器人未运行。请联网后点击“重新验证”，验证通过后再启动机器人。",
        clock_error: "系统时间或启动会话校验异常，机器人未运行。请校准时间并联网重新验证授权。",
        boot_identity: "无法确认系统启动会话，机器人未运行。请检查系统环境后联网重新验证授权。",
        account_mismatch: "当前微信与授权绑定账号不一致，机器人未运行。请切回已绑定的微信账号，确认后启动机器人。",
        active_unbound: "订阅已生效。首次在运行中心启动时，先确认绑定提示，再登录微信。",
        account_required: "请在运行中心点击“启动”，先确认绑定提示，再登录微信。",
        service_unavailable: "暂时无法连接授权服务。请检查网络后重新验证；有效的离线授权仍可使用。",
        invalid: "授权暂不可用，机器人未运行。请重新验证；仍无法使用时联系管理员。",
      };
      return messages[reason] || original || (data?.enabled === undefined ? "" : data?.authorized ? "授权已生效。确认模型设置后，由你启动机器人。"
        : !data?.enabled ? "此安装尚未启用商业授权，现有功能可按原方式使用。" : "领取激活码后，在下方查看并确认兑换。");
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
      if (hasValidAuthorization()) {
        return data.plan === "trial" ? ["免费试用已生效", "试用中", "success"] : ["订阅已生效", "已订阅", "success"];
      }
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
      feedback(statusMessage(error?.product || {message: error?.message || "操作未完成，请稍后重试。"}), "error", field);
    }

    function updateSnapshot(data) {
      if (!data || typeof data.enabled !== "boolean") throw new Error("授权状态未能读取，请重新验证。");
      const accountChanged = accountId(state.snapshot?.current_account) !== accountId(data.current_account);
      const licenseChanged = String(state.snapshot?.license_id || "") !== String(data.license_id || "")
        || String(state.snapshot?.plan?.id || state.snapshot?.plan || "") !== String(data.plan?.id || data.plan || "");
      if (licenseChanged || accountId(state.snapshot?.account) !== accountId(data.account)) {
        state.binding = null; state.bindingError = ""; state.bindingEpoch += 1;
      }
      state.snapshot = data;
      state.readError = "";
      if (accountChanged || licenseChanged) {
        state.trial = null; state.trialAccount = null; state.trialError = "";
        state.trialEpoch += 1; state.trialLoading = false;
      }
      render();
      if (accountChanged && state.activePage === "subscription") void loadFreeTrial();
    }

    async function load({silent = false} = {}) {
      if (state.loading || state.busy || state.preparing) return state.snapshot;
      state.loading = true;
      const epoch = state.epoch;
      try {
        const data = await api("get_license_status");
        if (epoch === state.epoch) updateSnapshot(data);
      } catch (error) {
        if (epoch === state.epoch) {
          state.readError = statusMessage(error?.product || {message: error?.message || "暂时无法读取授权状态。"});
          if (!silent || !state.snapshot) showError(error);
          renderHome();
        }
      } finally { state.loading = false; renderControls(); }
      return state.snapshot;
    }

    function renderHome() {
      const data = state.snapshot;
      const remaining = Number(data?.remaining_seconds);
      const expiring = hasValidAuthorization() && Number.isFinite(remaining) && remaining > 0 && remaining <= 7 * 86400;
      const visible = Boolean(data?.enabled && (!hasValidAuthorization() || expiring || state.readError || data.state === "offline" || data.last_error === "service_unavailable"));
      $("#homeLicenseNotice").hidden = !visible;
      if (!visible) return;
      const offline = data.state === "offline" || data.last_error === "service_unavailable";
      $("#homeLicenseTitle").textContent = state.readError ? "授权状态暂时无法读取" : expiring ? "授权即将到期"
        : offline && hasValidAuthorization() ? "当前使用离线授权" : description()[0];
      $("#homeLicenseMessage").textContent = state.readError || (expiring
        ? `有效期至 ${data.expires_text || expiryText(data.valid_until)}，可在“订阅与授权”兑换续期码。`
        : statusMessage());
      $("#homeLicenseAction").textContent = ["expired", "subscription_expired"].includes(data.state) ? "兑换续期码" : "查看授权";
      $("#homeLicenseAction").disabled = false;
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
        return `<article class="subscription-plan panel" data-plan="${esc(plan.id)}"><h3 class="subscription-plan-name">${esc(plan.name)}</h3><div class="subscription-plan-price">${knownPrice ? "<span>¥</span>" : ""}<strong>${esc(price)}</strong><small>${esc(unit)}</small></div><p>${esc(planDescription(plan))}</p>${trialButton}</article>`;
      }).join("") || '<p class="subtle">套餐暂时无法读取，请重新读取授权状态。</p>';
    }

    function renderControls() {
      const data = state.snapshot;
      const ready = Boolean(data?.enabled && data?.configured);
      const busy = state.busy || state.preparing;
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
      const trialButton = $("#licenseActivateTrial");
      if (trialButton) {
        const trial = freeTrial(data), status = freeTrialState(data);
        trialButton.disabled = busy || state.trialLoading || !ready
          || (current ? !(trial.eligible || trial.recoverable) : !data.can_connect || ["paid", "active", "ended", "used"].includes(status));
        trialButton.textContent = busy ? "处理中…" : trial.recoverable ? "恢复免费试用"
          : status === "active" ? "试用进行中" : status === "ended" ? "试用已结束" : status === "used" ? "已领取试用"
          : status === "paid" ? "已订阅" : "激活免费试用";
      }
      renderBindingStatus();
    }

    function render() {
      const data = state.snapshot;
      if (!data) return;
      const [title, tag, tone] = description();
      const authorized = hasValidAuthorization();
      $("#licenseStateTitle").textContent = title;
      $("#licenseStateTag").textContent = tag;
      $("#licenseStatusPanel").dataset.tone = tone;
      $("#licenseStateMessage").textContent = statusMessage();
      $("#licensePlan").textContent = planName(data.plan);
      const currentPlan = typeof data.plan === "object" ? data.plan
        : (Array.isArray(data.plans) ? data.plans : []).find((plan) => plan.id === data.plan);
      $("#licensePlanDescription").textContent = data.plan ? planDescription(currentPlan || {id: data.plan}) : "";
      $("#licenseExpires").textContent = data.expires_text || expiryText(data.valid_until);
      const seconds = Math.max(0, Math.floor(Number(data.remaining_seconds) || 0));
      $("#licenseRemaining").textContent = seconds > 0 && seconds <= 3600 ? `剩余 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : "";
      $("#licenseAccount").textContent = accountId(data.account) || "尚未绑定";
      $("#licenseIdText").textContent = data.license_id || "尚未激活";
      $("#licenseDeviceText").textContent = data.device_id || "尚未生成";
      if (state.authorizedView !== authorized) {
        $("#licenseOptions").open = !authorized;
        state.authorizedView = authorized;
      }
      $("#licenseOptionsTitle").textContent = authorized ? "续期或更换套餐" : "激活授权";
      $("#licensePausedInfo").hidden = !data.enabled || !pausedStates.has(data.state);
      $("#licenseRedeemTitle").textContent = data.license_id && !["revoked", "license_revoked"].includes(data.state)
        ? "兑换续期码" : "兑换激活码";
      $("#licenseCodeHelp").textContent = !data.enabled ? "商业授权尚未启用，暂不接受激活码。" : !data.configured
        ? "授权服务尚未配置，暂时无法兑换激活码。" : "请复制发码详情中的完整激活码，业务编号不能用于兑换。先查看套餐和有效期，确认后再兑换。";
      renderPlans(); renderFreeTrialStatus(); renderControls(); renderHome();
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
      if (state.busy || state.preparing || state.trialLoading || ["paid", "ended", "used"].includes(freeTrialState())) return;
      state.preparing = true; renderControls();
      try {
        await readForAction();
        const authorization = JSON.stringify([state.snapshot.license_id, state.snapshot.plan, state.snapshot.authorized]);
        const confirmed = await confirmAction({title: "确认激活免费试用",
          message: "接下来登录的微信号将用于免费试用。激活后立即连续计时 1 小时，每台设备和微信账号限领一次；恢复已有试用时，结束时间不变。",
          confirmText: "同意并继续", intent: "primary", onConfirm: async () => {
            await readForAction();
            if (authorization !== JSON.stringify([state.snapshot.license_id, state.snapshot.plan, state.snapshot.authorized])) {
              throw new Error("授权已变化，请取消后重新激活。");
            }
          }});
        if (!confirmed) return;
        const account = await connectAccount();
        if (!account) return;
        if (authorization !== JSON.stringify([state.snapshot.license_id, state.snapshot.plan, state.snapshot.authorized])) {
          throw new Error("授权已变化，请重新激活并确认。");
        }
        await loadFreeTrial({force: true});
        const trial = freeTrial();
        if (!(trial.eligible || trial.recoverable)) throw new Error(state.trialError || freeTrialStatusText());
        await transact(() => api("activate_free_trial", account), trial.recoverable
          ? "免费试用已恢复，结束时间不变。" : "免费试用已激活。请在运行中心启动机器人。");
        await loadFreeTrial({force: true});
      } catch (error) { showError(error); }
      finally { state.preparing = false; renderControls(); }
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
      if (/^WRB-\d{14}-[A-F0-9]{16}$/i.test(code)) {
        feedback("这是业务编号，请复制详情中的完整激活码。", "error", true);
        $("#licenseCode").focus();
        return;
      }
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
        }, "订阅已生效。请在运行中心点击“启动”；首次启动时先确认绑定提示，再登录微信。");
      } catch (error) { showError(error, true); }
    }

    async function readForAction() {
      state.epoch += 1;
      updateSnapshot(await api("get_license_status"));
      return state.snapshot;
    }

    async function checkConnectionRuntime(checkEnvironment = false) {
      const runtime = await api(checkEnvironment ? "check_environment" : "get_status");
      state.runtime = runtime;
      const environment = runtime?.environment;
      const problem = environment?.ready === false ? environment
        : runtime?.state === "ERROR" ? runtime.error : null;
      if (!problem) return;
      await navigate("home");
      await refreshRuntime();
      const message = [problem.title, problem.message, ...(problem.solutions || [])].filter(Boolean).join(" ");
      const error = new Error(message || "微信连接未完成，请在运行中心检查错误提示后重试。");
      error.product = {...problem, message: error.message};
      throw error;
    }

    async function connectAccount({differentFrom = "", forStart = false} = {}) {
      await readForAction();
      if (!differentFrom && accountId(state.snapshot.current_account)) return accountId(state.snapshot.current_account);
      const requestedConnection = !accountId(state.snapshot.current_account);
      let confirmedAccount = "";
      if (requestedConnection) {
        await checkConnectionRuntime(true);
        const response = await api("connect_license_wechat");
        if (response?.accepted === false) throw new Error(response.message || "微信连接尚未开始，请稍后重试。");
        await refreshRuntime();
        await checkConnectionRuntime();
        await readForAction();
      }
      if (differentFrom || requestedConnection) {
        const confirmed = await confirmAction({title: differentFrom ? "请切换微信账号" : "请登录微信",
          message: differentFrom ? "请在微信窗口切换并登录新的微信账号，再继续读取。取消不会更改绑定或扣除换绑次数。"
            : forStart ? "请在微信窗口完成登录，再点击“已登录，继续”。本次启动会继续加载机器人功能，无需再次点击启动；取消则结束本次启动。"
              : "微信连接请求已提交。请等待微信窗口打开，完成登录后再继续读取实际账号；取消不会绑定账号或启动机器人。",
          confirmText: differentFrom ? "已切换，继续" : "已登录，继续", cancelText: forStart ? "取消本次启动" : "取消",
          pendingText: "正在读取账号…", intent: "primary", dismissible: false, onConfirm: async () => {
            await checkConnectionRuntime();
            await readForAction();
            const account = accountId(state.snapshot.current_account);
            if (!account) throw new Error("尚未读取到登录的微信账号，请完成登录后重试。");
            if (account === differentFrom) throw new Error("仍是原绑定微信，请切换到新的微信账号后重试。");
            confirmedAccount = account;
          }});
        if (!confirmed) return "";
      }
      return confirmedAccount;
    }

    async function rebindAccount() {
      if (state.busy || state.preparing) return;
      state.preparing = true; renderControls();
      try {
        await readForAction();
        const binding = await loadBindingStatus({force: true, required: true});
        if (!binding.can_rebind || !accountId(binding.account)) throw new Error("当前套餐暂不支持换绑或剩余次数不足，请查看换绑次数。");
        const previous = accountId(binding.account), license = binding.license_id;
        const confirmed = await confirmAction({title: "更换套餐绑定微信", subject: {label: "当前绑定微信", value: previous},
          message: `接下来登录的新微信号将绑定当前套餐。${bindingQuotaText(binding)} 同意后将停止机器人回复功能，保留微信供你切换账号；换绑成功后请到运行中心启动。`,
          confirmText: "同意并切换账号", intent: "primary", onConfirm: async () => {
            await readForAction();
            const latest = await loadBindingStatus({force: true, required: true});
            if (latest.license_id !== license || latest.revision !== binding.revision
              || accountId(latest.account) !== previous || !latest.can_rebind) {
              throw new Error("授权或换绑次数已变化，请取消后重新办理。");
            }
          }});
        if (!confirmed) return;
        const stopped = await api("stop_robot");
        if (stopped?.accepted === false) throw new Error(stopped.message || "机器人正在处理其他操作，请完成停止后重试换绑。");
        await refreshRuntime();
        const account = await connectAccount({differentFrom: previous});
        if (!account) {
          feedback("已取消换绑，原绑定和换绑次数保留。机器人保持停止，可到运行中心启动。", "neutral");
          return;
        }
        if (state.snapshot.license_id !== license || accountId(state.snapshot.account) !== previous) {
          throw new Error("授权绑定已变化，请重新办理换绑。");
        }
        await transact(() => api("rebind_license_account", account, previous, binding.revision), "微信换绑已完成。机器人保持停止，请到运行中心点击“启动”。");
        await loadBindingStatus({force: true});
        await refreshRuntime();
      } catch (error) { showError(error); }
      finally { state.preparing = false; renderControls(); }
    }

    async function initialize() {
      if (state.initialized) return load();
      state.initialized = true;
      $("#homeLicenseAction").addEventListener("click", () => navigate("subscription"));
      $("#licenseRefresh").addEventListener("click", () => state.snapshot?.enabled && state.snapshot?.configured
        ? execute(() => api("refresh_license"), "已重新验证授权状态。") : load());
      $("#licenseCodeForm").addEventListener("submit", (event) => { event.preventDefault(); void previewCode(); });
      $("#licenseCode").addEventListener("input", () => { clearPreview(); feedback("", "neutral", true); });
      $("#licenseConfirmRedeem").addEventListener("click", redeemCode);
      $("#licenseCancelRedeem").addEventListener("click", () => { clearPreview(); $("#licenseCode").focus(); });
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
      $("#licenseRebindRefresh").addEventListener("click", () => loadBindingStatus({force: true}));
      $("#licenseRebind").addEventListener("click", rebindAccount);
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
      formatMessage: statusMessage,
      async onNavigate(page) {
        state.activePage = page;
        if (page === "subscription") { refreshContactIfNeeded(); await load(); await Promise.all([loadFreeTrial(), loadBindingStatus()]); }
      },
      updateRuntime(runtime) { state.runtime = runtime; if (state.initialized) renderControls(); },
      needsEntry() { return Boolean(state.snapshot?.enabled && !hasValidAuthorization()); },
      async beforeStart() {
        if (state.busy || state.preparing) return false;
        state.preparing = true; renderControls();
        try {
          await readForAction();
          if (!state.snapshot || state.readError || (state.snapshot.enabled && !hasValidAuthorization())) {
            const data = state.snapshot;
            const current = accountId(data?.current_account);
            const mismatched = data?.state === "account_mismatch" || (current && accountId(data?.account) && current !== accountId(data.account));
            const message = state.readError || (mismatched ? statusMessage({...data, state: "account_mismatch"})
              : data && pausedStates.has(data.state) ? statusMessage()
              : data && !data.configured ? statusMessage({...data, state: "configuration_pending"}) : statusMessage());
            if (typeof toast === "function") toast(message, "error");
            await navigate("subscription");
            feedback(message, "error");
            return false;
          }
          if (!state.snapshot.enabled) return true;
          const license = state.snapshot.license_id;
          const bound = accountId(state.snapshot.account);
          if (!bound) {
            const binding = await loadBindingStatus({force: true, required: true});
            const confirmed = await confirmAction({title: "确认绑定当前套餐",
              message: `接下来登录的微信号将绑定当前套餐。${bindingQuotaText(binding)} 同意后将连接微信，登录完成后绑定并启动机器人。`,
              confirmText: "同意并继续", intent: "primary", onConfirm: async () => {
                await readForAction();
                if (state.snapshot.license_id !== license || !hasValidAuthorization() || accountId(state.snapshot.account)) {
                  throw new Error("授权状态已变化，请取消后重新启动并确认。");
                }
              }});
            if (!confirmed) return false;
          }
          const account = await connectAccount({forStart: true});
          if (!account) return false;
          if (state.snapshot.license_id !== license || !hasValidAuthorization()) throw new Error("授权状态已变化，请重新启动并确认。");
          if (bound && bound !== account) throw new Error(statusMessage({...state.snapshot, state: "account_mismatch"}));
          if (!bound) {
            if (accountId(state.snapshot.account)) throw new Error("授权绑定已变化，请重新启动并确认。");
            await transact(() => api("bind_license_account", account));
          }
          await readForAction();
          if (state.snapshot.license_id !== license || !readyToStart() || accountId(state.snapshot.current_account) !== account) {
            throw new Error("微信账号或授权已变化，请重新启动并确认。");
          }
          return true;
        } finally { state.preparing = false; renderControls(); }
      },
    };
  };
})();
