/* Chat management controller; the shell supplies shared services explicitly. */
window.createChatManagement = function (helpers) {
  "use strict";
  const {
    $,
    $$,
    CONTACT_PAGE_SIZE,
    GROUP_DIRECTORY_RETRY_DELAYS_MS,
    GROUP_MEMBER_COUNTS_STORAGE_PREFIX,
    GROUP_MEMBER_REQUEST_CONCURRENCY,
    GROUP_MEMBER_RETRY_DELAYS_MS,
    api,
    avatarMarkup,
    bindAvatarFallbacks,
    confirmAction,
    copyText,
    escapeHtml,
    fullTime,
    icon,
    openScheduledForChat,
    openCoreTemplates,
    readHomeSetupAlertDismissals,
    reportError,
    state,
    toast,
    updatePolicyToggle,
  } = helpers;

  const chatProfiles = window.createChatProfiles({api, escapeHtml, confirmAction, openCoreTemplates, state});

  let groupDrafts = {};
  let groupMemberRequestsActive = 0;
  const groupMemberRequestQueue = [];
  const groupMemberLoadsInFlight = new Set();
  state.dismissedHomeSetupAlerts = readHomeSetupAlertDismissals();
  function hasGroupMemberCount(value) {
    return Number.isSafeInteger(value) && value > 0;
  }

  function readGroupMemberCounts(accountId) {
    if (!accountId) return {};
    try {
      const saved = JSON.parse(window.localStorage.getItem(
        `${GROUP_MEMBER_COUNTS_STORAGE_PREFIX}${encodeURIComponent(accountId)}`,
      ) || "{}");
      if (!saved || typeof saved !== "object" || Array.isArray(saved)) return {};
      return Object.fromEntries(Object.entries(saved).filter(([groupId, count]) =>
        groupId.endsWith("@chatroom") && hasGroupMemberCount(count)));
    } catch (_error) {
      return {};
    }
  }

  function persistGroupMemberCounts() {
    if (!state.groupAccount) return;
    try {
      window.localStorage.setItem(
        `${GROUP_MEMBER_COUNTS_STORAGE_PREFIX}${encodeURIComponent(state.groupAccount)}`,
        JSON.stringify(state.groupMemberCounts),
      );
    } catch (_error) { /* The live page cache still works when storage is unavailable. */ }
  }

  function rememberGroupMemberCount(groupId, count) {
    if (!groupId || !hasGroupMemberCount(count)) return false;
    state.groupMemberCounts[groupId] = count;
    const group = state.groups.find((item) => item.group_id === groupId);
    if (group) group.member_count = count;
    persistGroupMemberCounts();
    return true;
  }

  async function selectChatManagementTab(tab, focus = false) {
    if (!new Set(["groups", "private"]).has(tab)) return;
    state.chatManagementTab = tab;
    $$('[data-chat-management-tab]').forEach((button) => {
      const active = button.dataset.chatManagementTab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      if (active && focus) button.focus();
    });
    $("#groupManagementPanel").hidden = tab !== "groups";
    $("#privateManagementPanel").hidden = tab !== "private";
    $("#refreshGroups").hidden = tab !== "groups";
    $("#refreshContacts").hidden = tab !== "private";
    if (tab === "groups" && !state.groupsLoaded) await loadGroups(true);
    if (tab === "private" && !state.contactsLoaded) await loadContacts(false);
  }

  function groupDirectoryStatus(data, failed = false) {
    const panel = $("#groupDirectoryStatus");
    const stale = Boolean(data?.stale || failed);
    panel.className = `directory-status ${stale ? "warning" : "success"}`;
    const title = stale
      ? (state.groups.length ? "本次未更新，正在显示上次结果" : "群聊暂时无法读取")
      : "群聊列表已更新";
    const message = data?.message || (failed ? "请确认机器人已连接微信，再重新刷新。" : "查看和刷新不会向任何群发送消息。");
    panel.innerHTML = `<div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span></div>`;
  }

  function clearGroupRefreshRetry() {
    window.clearTimeout(state.groupRefreshRetryTimer);
    state.groupRefreshRetryTimer = 0;
  }

  function scheduleGroupRefreshRetry(retryAttempt) {
    if (retryAttempt >= GROUP_DIRECTORY_RETRY_DELAYS_MS.length
        || state.activePage !== "groups" || state.chatManagementTab !== "groups") return;
    clearGroupRefreshRetry();
    state.groupRefreshRetryTimer = window.setTimeout(() => {
      state.groupRefreshRetryTimer = 0;
      if (state.activePage !== "groups" || state.chatManagementTab !== "groups") return;
      void loadGroups(true, {background: true, retryAttempt: retryAttempt + 1});
    }, GROUP_DIRECTORY_RETRY_DELAYS_MS[retryAttempt]);
  }

  function clearPendingGroupMemberLoading() {
    Object.entries(state.groupMembers).forEach(([groupId, memberData]) => {
      if (memberData?.loading) delete state.groupMembers[groupId];
    });
  }

  async function loadGroups(refresh = false, options = {}) {
    if (state.groupsBusy) return;
    const background = options.background === true;
    const retryAttempt = Number.isInteger(options.retryAttempt) ? options.retryAttempt : 0;
    if (refresh && retryAttempt === 0) clearGroupRefreshRetry();
    state.groupsBusy = true;
    const button = $("#refreshGroups");
    if (!background) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.innerHTML = `${icon("i-refresh")}<span>${refresh ? "正在刷新…" : "正在读取…"}</span>`;
    }
    if (!state.groups.length) $("#groupRows").innerHTML = '<tr><td colspan="4" class="table-empty">正在读取当前微信的群聊…</td></tr>';
    try {
      const data = await api("get_groups", refresh);
      const previousAccount = state.groupAccount;
      const previousGroups = JSON.stringify(state.groups);
      const previousStale = state.groupStale;
      const nextGroups = Array.isArray(data.items) ? data.items : [];
      state.groupAccount = data.account_id || "";
      if (previousAccount !== state.groupAccount) {
        state.selectedGroup = null;
        state.groupMembers = {};
        state.groupPolicies = {};
        groupDrafts = {};
        state.groupMemberCounts = readGroupMemberCounts(state.groupAccount);
      }
      let countsChanged = false;
      state.groups = nextGroups.map((item) => {
        const serverCount = item.member_count;
        const memberDataCount = state.groupMembers[item.group_id]?.total;
        const cachedCount = state.groupMemberCounts[item.group_id]
          ?? (hasGroupMemberCount(memberDataCount) ? memberDataCount : null);
        if (hasGroupMemberCount(serverCount)) {
          if (state.groupMemberCounts[item.group_id] !== serverCount) countsChanged = true;
          state.groupMemberCounts[item.group_id] = serverCount;
          return {...item, member_count: serverCount};
        }
        return {...item, member_count: hasGroupMemberCount(cachedCount) ? cachedCount : null};
      });
      const currentGroupIds = new Set(state.groups.map((item) => item.group_id));
      Object.keys(state.groupMemberCounts).forEach((groupId) => {
        if (!currentGroupIds.has(groupId)) {
          delete state.groupMemberCounts[groupId];
          countsChanged = true;
        }
      });
      if (countsChanged) persistGroupMemberCounts();
      state.groupUpdatedAt = data.updated_at || "";
      state.groupStale = Boolean(data.stale);
      if (state.groupStale) scheduleGroupRefreshRetry(retryAttempt);
      else clearGroupRefreshRetry();
      state.groupsLoaded = true;
      state.groupMembers = Object.fromEntries(
        Object.entries(state.groupMembers).filter(([groupId]) => currentGroupIds.has(groupId)),
      );
      if (state.selectedGroup && !state.groups.some((item) => item.group_id === state.selectedGroup)) {
        state.selectedGroup = null;
      }
      const previousMemberLoad = state.groupMemberLoad;
      const memberLoad = background && previousGroups === JSON.stringify(state.groups)
        && previousAccount === state.groupAccount
        ? state.groupMemberLoad : ++state.groupMemberLoad;
      if (memberLoad !== previousMemberLoad) clearPendingGroupMemberLoading();
      const retryMemberCounts = refresh && retryAttempt === 0;
      const groupsNeedingCounts = state.groups.filter((item) =>
        !Number.isInteger(item.member_count)
        && !state.groupMembers[item.group_id]?.loading
        && (!state.groupMembers[item.group_id]?.error || retryMemberCounts));
      groupsNeedingCounts.forEach((item) => {
        const previous = state.groupMembers[item.group_id] || {};
        state.groupMembers[item.group_id] = {
          ...previous,
          loading: true,
          error: "",
          items: Array.isArray(previous.items) ? previous.items : [],
        };
      });
      groupDirectoryStatus(data);
      if (!background || refresh || previousGroups !== JSON.stringify(state.groups)
          || previousAccount !== state.groupAccount || previousStale !== state.groupStale
          || groupsNeedingCounts.length) {
        renderGroups({preserveInspector: background});
      } else {
        $("#groupUpdated").textContent = state.groupUpdatedAt
          ? `${state.groupStale ? "上次成功" : "更新于"} ${fullTime(state.groupUpdatedAt)}` : "尚无可用结果";
      }
      if (groupsNeedingCounts.length) {
        void hydrateGroupMemberCounts(groupsNeedingCounts.map((item) => item.group_id), memberLoad, {preMarked: true});
      }
    } catch (error) {
      state.groupStale = true;
      scheduleGroupRefreshRetry(retryAttempt);
      groupDirectoryStatus({message: error?.message}, true);
      renderGroups({preserveInspector: background});
      if (!background) reportError(error);
    } finally {
      state.groupsBusy = false;
      if (!background) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.innerHTML = `${icon("i-refresh")}<span>刷新群列表</span>`;
      }
    }
  }

  function renderGroups(options = {}) {
    const scroll = $(".group-table-scroll");
    const previousScroll = scroll?.scrollTop || 0;
    const keyword = $("#groupSearch").value.trim().toLocaleLowerCase("zh-CN");
    const items = state.groups.filter((item) => {
      const haystack = `${item.name || ""}\n${item.group_id || ""}`.toLocaleLowerCase("zh-CN");
      return !keyword || haystack.includes(keyword);
    });
    const countUnavailable = state.groupStale && !state.groupUpdatedAt && !state.groups.length;
    $("#groupCount").textContent = countUnavailable
      ? "群聊数量暂不可用"
      : keyword ? `${items.length} / ${state.groups.length} 个群聊` : `${state.groups.length} 个群聊`;
    $("#groupUpdated").textContent = state.groupUpdatedAt ? `${state.groupStale ? "上次成功" : "更新于"} ${fullTime(state.groupUpdatedAt)}` : "尚无可用结果";
    const empty = state.groups.length
      ? "没有符合条件的群聊"
      : (state.groupStale ? "尚未读取到群聊，请连接微信后重试" : "当前账号没有可显示的群聊");
    $("#groupRows").innerHTML = items.length ? items.map((item) => {
      const selected = item.group_id === state.selectedGroup;
      const memberData = state.groupMembers[item.group_id];
      const cachedCount = hasGroupMemberCount(item.member_count) ? item.member_count : memberData?.total;
      const memberCount = hasGroupMemberCount(cachedCount)
        ? `${escapeHtml(cachedCount)} 人`
        : memberData?.error
          ? '<span class="member-count-state error">读取失败</span>'
          : '<span class="member-count-state loading">读取中</span>';
      const avatarTone = Math.abs([...String(item.group_id || "")].reduce((sum, char) => sum + char.charCodeAt(0), 0)) % 3;
      return `<tr class="group-row ${selected ? "selected" : ""}" data-group-id="${escapeHtml(item.group_id)}" tabindex="0" aria-selected="${selected}">
        <td><span class="group-name-cell">${avatarMarkup(`group-avatar tone-${avatarTone}`, item.name || "群", item.avatar)}<span><strong class="group-name">${escapeHtml(item.name || "未命名群聊")}</strong><small class="group-list-count">${memberCount}</small></span></span></td>
        <td><code class="group-id">${escapeHtml(item.group_id)}</code></td>
        <td>${memberCount}</td>
        <td><div class="row-actions"><button class="row-action copy-group" type="button" data-group-id="${escapeHtml(item.group_id)}">复制</button><button class="row-action inspect-group ${selected ? "active" : ""}" type="button" data-group-id="${escapeHtml(item.group_id)}">${selected ? "正在查看" : "查看"}</button></div></td>
      </tr>`;
    }).join("") : `<tr><td colspan="4" class="table-empty">${escapeHtml(empty)}</td></tr>`;
    $$(".group-row").forEach((row) => {
      const select = () => selectGroup(row.dataset.groupId);
      row.addEventListener("click", (event) => { if (!event.target.closest("button")) select(); });
      row.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); select(); } });
    });
    $$(".inspect-group").forEach((button) => button.addEventListener("click", () => selectGroup(button.dataset.groupId)));
    $$(".copy-group").forEach((button) => button.addEventListener("click", () => copyText(button.dataset.groupId)));
    bindAvatarFallbacks($("#groupRows"));
    if (!options.preserveInspector || !state.selectedGroup) renderGroupInspector();
    if (scroll) scroll.scrollTop = previousScroll;
  }

  function selectGroup(groupId) {
    if (state.selectedGroup === groupId) return;
    state.selectedGroup = groupId;
    state.groupInspectorView = "policy";
    renderGroups();
    loadGroupPolicy(groupId);
  }

  async function loadGroupPolicy(groupId, force = false) {
    if (!state.groupAccount || !groupId) return;
    const account = state.groupAccount;
    const policies = state.groupPolicies;
    if (policies[groupId]?.loading || (!force && policies[groupId] && !policies[groupId].error)) return;
    policies[groupId] = {loading: true};
    if (state.selectedGroup === groupId) renderGroupInspector();
    try {
      const result = await api("get_group_policy", account, groupId);
      if (state.groupAccount !== account || state.groupPolicies !== policies) return;
      policies[groupId] = result;
    } catch (error) {
      if (state.groupAccount !== account || state.groupPolicies !== policies) return;
      policies[groupId] = {error: error?.message || "群聊规则暂时无法读取。"};
      reportError(error);
    }
    if (state.selectedGroup === groupId) renderGroupInspector();
    return !policies[groupId]?.error;
  }

  function refreshGroupPolicy(groupId) {
    const account = state.groupAccount;
    const policies = state.groupPolicies;
    const refresh = () => {
      if (state.groupAccount !== account || state.groupPolicies !== policies) throw new Error("微信账号已变化，请重新选择群聊。");
      delete groupDrafts[groupId]; chatProfiles.invalidate();
      return loadGroupPolicy(groupId, true);
    };
    if (groupDrafts[groupId]) {
      confirmAction({title: "重新读取群规则？", message: "本群未保存的修改将被丢弃。", confirmText: "丢弃并重新读取", onConfirm: refresh});
    } else void refresh();
  }

  function renderGroupInspector() {
    const panel = $("#groupInspector");
    const group = state.groups.find((item) => item.group_id === state.selectedGroup);
    panel.classList.toggle("empty", !group);
    if (!group) {
      const message = state.groups.length ? "选择左侧群聊后，可查看群规则和群成员。" : "刷新并读取到群聊后，可在这里查看群规则和群成员。";
      panel.innerHTML = `<div class="group-inspector-empty"><div class="empty-symbol">群</div><span class="eyebrow">群聊详情</span><h2>${state.groups.length ? "选择一个群聊" : "暂时没有群聊"}</h2><p>${message}</p></div>`;
      return;
    }
    const memberData = state.groupMembers[group.group_id];
    const policyData = groupDrafts[group.group_id] || state.groupPolicies[group.group_id];
    const cachedCount = hasGroupMemberCount(group.member_count) ? group.member_count : memberData?.total;
    const memberCount = hasGroupMemberCount(cachedCount) ? `${cachedCount} 人`
      : memberData?.error ? "读取失败" : "读取中";
    const memberMarkup = !memberData ? '<div class="member-empty">点击“重新读取”后显示当前微信缓存中的成员。</div>'
      : memberData.loading ? '<div class="member-empty">正在读取成员…</div>'
      : memberData.error ? `<div class="member-empty error">${escapeHtml(memberData.error)}</div>`
      : memberData.items.length ? `<div class="member-list">${memberData.items.map((item) => `<article>${avatarMarkup("member-avatar", item.group_nickname || item.name || "群", item.avatar)}<div><strong>${escapeHtml(item.group_nickname || item.name || "未命名成员")}</strong><div class="member-identities"><small><span>微信号</span>${escapeHtml(item.wechat_id || "未提供")}</small><small><span>微信标识</span>${escapeHtml(item.user_id || "未提供")}</small></div></div></article>`).join("")}</div>${memberData.truncated ? '<p class="member-note">成员较多，本次仅显示前 500 人。</p>' : ""}`
      : '<div class="member-empty">当前接口没有返回成员。</div>';
    const header = `<header class="inspector-hero"><div class="inspector-group-title">${avatarMarkup("group-avatar tone-0", group.name || "群", group.avatar)}<div><h2>${escapeHtml(group.name || "未命名群聊")}</h2><code class="inspector-id">${escapeHtml(group.group_id)} · ${escapeHtml(memberCount)}</code></div></div><button class="row-action chat-scheduled-entry" type="button">本群的定时任务</button></header>`;
    const tabs = `<div class="inspector-detail-actions"><button class="row-action" id="refreshGroupPolicy" type="button" ${state.groupPolicies[group.group_id]?.loading ? "disabled" : ""}>重新读取规则</button><button class="row-action" type="button" data-inspector-view="${state.groupInspectorView === "members" ? "policy" : "members"}">${state.groupInspectorView === "members" ? "返回群聊规则" : "查看群成员"}</button><button class="row-action copy-selected-group" type="button">复制群标识</button></div>`;
    const policyBody = `<div class="inspector-scroll">${groupPolicyMarkup(policyData)}</div><footer class="inspector-footer"><span id="groupPolicyNote">${groupDrafts[group.group_id] ? "有未保存修改，切换群聊会保留。" : "在线保存会立即应用到当前群。"}</span><button class="button primary" type="submit" form="groupPolicyForm" ${policyData?.loading || policyData?.error ? "disabled" : ""}>保存群聊规则</button></footer>`;
    const membersBody = `<div class="inspector-scroll members-view"><section class="members-section"><div class="members-heading"><div><strong>群成员</strong><span>按需读取，最多展示 500 人</span></div><button class="button ghost small" id="loadGroupMembers" type="button" ${memberData?.loading ? "disabled" : ""}>${memberData?.loading ? "正在读取…" : "重新读取"}</button></div>${memberMarkup}<div class="policy-summary">微信未提供的信息显示为“未知”。</div></section></div><footer class="inspector-footer"><span>成员列表不会写入配置。</span><button class="button ghost" type="button" data-inspector-view="policy">返回群规则</button></footer>`;
    panel.innerHTML = `${header}${tabs}${state.groupInspectorView === "members" ? membersBody : policyBody}`;
    $(".copy-selected-group", panel)?.addEventListener("click", () => copyText(group.group_id, "群标识"));
    $(".chat-scheduled-entry", panel)?.addEventListener("click", () => openScheduledForChat({type: "group", id: group.group_id, name: group.name}, state.groupAccount));
    $$('[data-inspector-view]', panel).forEach((button) => button.addEventListener("click", () => {
      state.groupInspectorView = button.dataset.inspectorView;
      renderGroupInspector();
      if (state.groupInspectorView === "members" && !state.groupMembers[group.group_id]) {
        void hydrateGroupMemberCounts([group.group_id], state.groupMemberLoad);
      }
    }));
    $("#loadGroupMembers")?.addEventListener("click", () => loadGroupMembers(group.group_id));
    $("#refreshGroupPolicy")?.addEventListener("click", () => refreshGroupPolicy(group.group_id));
    $("#loadGroupPolicy")?.addEventListener("click", () => loadGroupPolicy(group.group_id));
    bindAvatarFallbacks(panel);
    const policyForm = $("#groupPolicyForm");
    if (policyForm) {
      void chatProfiles.mount($("#groupChatProfile"), group.group_id, "group", state.groupAccount);
      policyForm.addEventListener("submit", saveGroupPolicy);
      const rememberDraft = (event) => {
        if (!event.target.name) return;
        groupDrafts[group.group_id] = {...policyData, policy: {...policyData.policy, ...readGroupPolicyForm(policyForm), daily_limit: policyForm.elements.daily_limit.value}};
        $("#groupPolicyNote").textContent = "有未保存修改，切换群聊会保留。";
      };
      policyForm.addEventListener("input", rememberDraft);
      policyForm.addEventListener("change", rememberDraft);
      $$(".toggle input", policyForm).forEach((input) => input.addEventListener("change", updatePolicyToggle));
    }
  }

  function groupPolicyMarkup(data) {
    if (!data) return '<section class="group-policy-form"><h3>本群机器人规则</h3><button class="button secondary small" id="loadGroupPolicy" type="button">读取群聊规则</button></section>';
    if (data.loading) return '<section class="group-policy-form"><h3>本群机器人规则</h3><div class="loading-row">正在读取规则…</div></section>';
    if (data.error) return `<section class="group-policy-form"><h3>本群机器人规则</h3><div class="member-empty error">${escapeHtml(data.error)}</div></section>`;
    const policy = data.policy || {};
    const quiet = policy.quiet_hours || {};
    const allowed = new Set(policy.allowed_features || []);
    const featureChoices = (data.features || []).map((feature) => `<label class="option-check"><input name="allowed_features" type="checkbox" value="${escapeHtml(feature.id)}" ${allowed.has(feature.id) ? "checked" : ""}><span>${escapeHtml(feature.name)}</span></label>`).join("");
    return `<form class="group-policy-form" id="groupPolicyForm" data-revision="${escapeHtml(policy.revision || "")}" novalidate>
      <div id="groupChatProfile" class="chat-profile"></div>
      <div class="toggle-line"><span><strong>在本群使用机器人</strong></span><span class="toggle"><input id="groupBotEnabled" name="bot_enabled" type="checkbox" ${policy.bot_enabled ? "checked" : ""}><label class="toggle-track" for="groupBotEnabled" aria-label="切换本群机器人"></label><span>${policy.bot_enabled ? "已启用" : "已停用"}</span></span></div>
      <label><span>回复方式</span><select name="reply_mode"><option value="mention" ${policy.reply_mode === "mention" ? "selected" : ""}>仅在点名或引用时回复（推荐）</option><option value="natural" ${policy.reply_mode === "natural" ? "selected" : ""}>相关话题可自然参与</option><option value="off" ${policy.reply_mode === "off" ? "selected" : ""}>不回复</option></select></label>
      <details class="advanced-details"><summary>限制本群可以使用的功能</summary><div class="option-grid compact-options group-feature-options">${featureChoices || "<span class='subtle'>当前没有可分配的功能</span>"}</div></details>
      <div class="toggle-line"><span><strong>接收已配置的定时任务</strong></span><span class="toggle"><input id="groupScheduledEnabled" name="scheduled_enabled" type="checkbox" ${policy.scheduled_enabled !== false ? "checked" : ""}><label class="toggle-track" for="groupScheduledEnabled" aria-label="切换本群定时任务"></label><span>${policy.scheduled_enabled !== false ? "已允许" : "已暂停"}</span></span></div>
      <details class="advanced-details"><summary>更多群规则</summary><div class="toggle-line"><span><strong>定时任务安静时段</strong></span><span class="toggle"><input id="groupQuietEnabled" name="quiet_enabled" type="checkbox" aria-controls="groupQuietHoursFields" ${quiet.enabled ? "checked" : ""}><label class="toggle-track" for="groupQuietEnabled" aria-label="切换本群安静时段"></label><span>${quiet.enabled ? "已启用" : "已关闭"}</span></span></div>
      <div class="field-pair quiet-hours-fields" id="groupQuietHoursFields" ${quiet.enabled ? "" : "hidden"}><label><span>开始</span><input name="quiet_start" type="time" value="${escapeHtml(quiet.start || "22:00")}"></label><label><span>结束</span><input name="quiet_end" type="time" value="${escapeHtml(quiet.end || "08:00")}"></label></div>
      <label><span>每日定时任务上限（0 为暂停）</span><input name="daily_limit" type="number" min="0" max="100" value="${escapeHtml(policy.daily_limit ?? 3)}"></label>
      </details>
    </form>`;
  }

  function readGroupPolicyForm(form) {
    return {
      bot_enabled: form.elements.bot_enabled.checked,
      reply_mode: form.elements.reply_mode.value,
      scheduled_enabled: form.elements.scheduled_enabled.checked,
      allowed_features: $$('input[name="allowed_features"]:checked', form).map((input) => input.value),
      quiet_hours: {
        enabled: form.elements.quiet_enabled.checked,
        start: form.elements.quiet_start.value,
        end: form.elements.quiet_end.value,
        timezone: "Asia/Shanghai",
      },
      daily_limit: Number(form.elements.daily_limit.value),
    };
  }

  async function saveGroupPolicy(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $('button[form="groupPolicyForm"]');
    if (!button) return;
    const groupId = state.selectedGroup;
    const account = state.groupAccount;
    const policies = state.groupPolicies;
    button.disabled = true;
    button.textContent = "正在检查影响…";
    try {
      const values = readGroupPolicyForm(form);
      const preview = await api("preview_group_policy", account, groupId, values, form.dataset.revision || "");
      const impact = Array.isArray(preview.impact) ? preview.impact : ["本次修改只影响当前群的机器人规则。"];
      button.disabled = false;
      button.textContent = "保存群聊规则";
      confirmAction({
        title: "确认应用本群规则？",
        message: impact.join(" ") + " 保存只更新本机配置，不会向群内发送消息。",
        confirmText: "保存并回读",
        intent: "primary",
        onConfirm: async () => {
          if (state.groupAccount !== account || state.groupPolicies !== policies) throw new Error("微信账号已变化，请重新选择群聊。");
          await api("save_group_policy", account, groupId, values, form.dataset.revision || "");
          if (state.groupAccount !== account || state.groupPolicies !== policies) return;
          delete groupDrafts[groupId];
          if (!await loadGroupPolicy(groupId, true)) throw new Error("规则已保存，但未能重新读取，请重试后核对。");
          toast("群聊规则已保存并回读确认。");
        },
      });
      return;
    } catch (error) { reportError(error); }
    finally { button.disabled = false; button.textContent = "保存群聊规则"; }
  }

  async function hydrateGroupMemberCounts(groupIds, generation, options = {}) {
    const pending = [...new Set(groupIds)];
    let nextIndex = 0;
    const workerCount = Math.min(GROUP_MEMBER_REQUEST_CONCURRENCY, pending.length);
    const workers = Array.from({length: workerCount}, async () => {
      while (generation === state.groupMemberLoad) {
        const index = nextIndex++;
        if (index >= pending.length) return;
        await loadGroupMembers(pending[index], {
          generation,
          suppressInitialRender: options.preMarked === true,
          silentErrors: options.preMarked === true,
        });
      }
    });
    await Promise.all(workers);
  }

  function pumpGroupMemberRequestQueue() {
    while (groupMemberRequestsActive < GROUP_MEMBER_REQUEST_CONCURRENCY && groupMemberRequestQueue.length) {
      const request = groupMemberRequestQueue.shift();
      if (request.generation !== state.groupMemberLoad
          || !state.groups.some((item) => item.group_id === request.groupId)
          || (request.background && (state.activePage !== "groups" || state.chatManagementTab !== "groups"))) {
        request.resolve(null);
        continue;
      }
      groupMemberRequestsActive += 1;
      Promise.resolve()
        .then(() => api("get_group_members", request.groupId))
        .then(request.resolve, request.reject)
        .finally(() => {
          groupMemberRequestsActive -= 1;
          pumpGroupMemberRequestQueue();
        });
    }
  }

  function queueGroupMemberRequest(groupId, generation, background) {
    return new Promise((resolve, reject) => {
      groupMemberRequestQueue.push({groupId, generation, background, resolve, reject});
      pumpGroupMemberRequestQueue();
    });
  }

  async function loadGroupMembers(groupId, options = {}) {
    const background = options?.background === true;
    const generation = options?.generation ?? state.groupMemberLoad;
    if (generation !== state.groupMemberLoad || !state.groups.some((item) => item.group_id === groupId)) return;
    const requestKey = `${generation}:${groupId}`;
    if (groupMemberLoadsInFlight.has(requestKey)) return;
    groupMemberLoadsInFlight.add(requestKey);
    try {
      const previous = state.groupMembers[groupId] || {};
      state.groupMembers[groupId] = {
        ...previous,
        loading: true,
        error: "",
        items: Array.isArray(previous.items) ? previous.items : [],
      };
      if (!background && !options.suppressInitialRender) renderGroups();
      let lastError = null;
      for (let attempt = 0; attempt <= GROUP_MEMBER_RETRY_DELAYS_MS.length; attempt += 1) {
        if (attempt) {
          await new Promise((resolve) => window.setTimeout(resolve, GROUP_MEMBER_RETRY_DELAYS_MS[attempt - 1]));
        }
        if (generation !== state.groupMemberLoad
            || !state.groups.some((item) => item.group_id === groupId)
            || (background && (state.activePage !== "groups" || state.chatManagementTab !== "groups"))) return;
        try {
          const data = await queueGroupMemberRequest(groupId, generation, background);
          if (!data) return;
          if (generation !== state.groupMemberLoad || !state.groups.some((item) => item.group_id === groupId)) return;
          const items = Array.isArray(data.items) ? data.items : [];
          const total = Number.isSafeInteger(data.total) ? data.total : items.length;
          const cachedCount = state.groupMemberCounts[groupId];
          const resolvedCount = hasGroupMemberCount(total) ? total : cachedCount;
          if (!hasGroupMemberCount(resolvedCount)) {
            throw new Error("群成员接口没有返回有效人数，未覆盖已有记录。");
          }
          state.groupMembers[groupId] = {
            loading: false,
            items,
            total: resolvedCount,
            truncated: Boolean(data.truncated),
          };
          rememberGroupMemberCount(groupId, resolvedCount);
          lastError = null;
          break;
        } catch (error) {
          lastError = error;
        }
      }
      if (lastError) {
        if (generation !== state.groupMemberLoad || !state.groups.some((item) => item.group_id === groupId)) return;
        state.groupMembers[groupId] = {loading: false, items: [], error: lastError?.message || "成员暂时无法读取，请稍后重试。"};
        if (!background && !options.silentErrors) reportError(lastError);
      }
      renderGroups({preserveInspector: background});
    } finally {
      groupMemberLoadsInFlight.delete(requestKey);
    }
  }

  function contactDisplayName(contact) {
    return contact?.remark || contact?.nickname || contact?.name || contact?.alias || contact?.user_id || "未命名联系人";
  }

  function contactRegion(contact) {
    return [contact?.country, contact?.province, contact?.city].filter(Boolean).join(" · ") || "未提供";
  }

  function contactDirectoryStatus(data, failed = false) {
    const panel = $("#contactDirectoryStatus");
    const stale = Boolean(data?.stale || failed);
    panel.className = `directory-status ${stale ? "warning" : "success"}`;
    const title = stale
      ? (state.contacts.length ? "本次未更新，正在显示上次结果" : "联系人暂时无法读取")
      : "联系人列表已更新";
    const message = data?.message || (failed
      ? "请确认机器人已连接微信，再重新刷新。"
      : "查看和刷新不会向任何联系人发送消息。");
    panel.innerHTML = `<div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span></div>`;
  }

  async function loadContacts(refresh = false) {
    if (state.contactsBusy) return;
    state.contactsBusy = true;
    const generation = ++state.contactLoad;
    const button = $("#refreshContacts");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.innerHTML = `${icon("i-refresh")}<span>${refresh ? "正在刷新…" : "正在读取…"}</span>`;
    if (!state.contacts.length) $("#contactRows").innerHTML = '<tr><td colspan="4" class="table-empty">正在读取当前微信通讯录…</td></tr>';
    try {
      const data = await api("get_contacts", refresh);
      if (generation !== state.contactLoad) return;
      const previousAccount = state.contactAccount;
      state.contacts = Array.isArray(data.items) ? data.items : [];
      state.contactAccount = data.account_id || "";
      if (previousAccount && previousAccount !== state.contactAccount) {
        state.selectedContact = null;
        state.contactDetails = {};
        state.privatePolicies = {};
      }
      state.contactUpdatedAt = data.updated_at || "";
      state.contactStale = Boolean(data.stale);
      state.contactTruncated = Boolean(data.truncated);
      state.contactsLoaded = true;
      state.contactPage = 1;
      if (state.selectedContact && !state.contacts.some((item) => item.user_id === state.selectedContact)) {
        state.selectedContact = null;
      }
      contactDirectoryStatus(data);
      renderContacts();
    } catch (error) {
      if (generation !== state.contactLoad) return;
      state.contactStale = true;
      contactDirectoryStatus({message: error?.message}, true);
      renderContacts();
      reportError(error);
    } finally {
      if (generation === state.contactLoad) {
        state.contactsBusy = false;
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.innerHTML = `${icon("i-refresh")}<span>刷新联系人</span>`;
      }
    }
  }

  function filteredContacts() {
    const keyword = $("#contactSearch").value.trim().toLocaleLowerCase("zh-CN");
    return state.contacts.filter((item) => {
      const haystack = [
        item.name, item.nickname, item.remark, item.alias, item.user_id,
        item.country, item.province, item.city, item.contact_type,
      ].filter(Boolean).join("\n").toLocaleLowerCase("zh-CN");
      return !keyword || haystack.includes(keyword);
    });
  }

  function renderContacts() {
    const scroll = $(".contact-table-scroll");
    const previousScroll = scroll?.scrollTop || 0;
    const items = filteredContacts();
    const pageCount = Math.max(1, Math.ceil(items.length / CONTACT_PAGE_SIZE));
    state.contactPage = Math.min(Math.max(1, state.contactPage), pageCount);
    const start = (state.contactPage - 1) * CONTACT_PAGE_SIZE;
    const visible = items.slice(start, start + CONTACT_PAGE_SIZE);
    const keyword = Boolean($("#contactSearch").value.trim());
    const countUnavailable = state.contactStale && !state.contactUpdatedAt && !state.contacts.length;
    $("#contactCount").textContent = countUnavailable
      ? "联系人数量暂不可用"
      : keyword ? `${items.length} / ${state.contacts.length} 位联系人` : `${state.contacts.length} 位联系人`;
    $("#contactUpdated").textContent = state.contactUpdatedAt
      ? `${state.contactStale ? "上次成功" : "更新于"} ${fullTime(state.contactUpdatedAt)}`
      : "尚无可用结果";
    const empty = state.contacts.length
      ? "没有符合条件的联系人"
      : (state.contactStale ? "尚未读取到联系人，请连接微信后重试" : "当前账号没有可显示的联系人");
    $("#contactRows").innerHTML = visible.length ? visible.map((item) => {
      const selected = item.user_id === state.selectedContact;
      const displayName = contactDisplayName(item);
      const account = item.alias || "未设置微信号";
      return `<tr class="contact-row ${selected ? "selected" : ""}" data-contact-id="${escapeHtml(item.user_id)}" tabindex="0" aria-selected="${selected}">
        <td><span class="group-name-cell">${avatarMarkup("contact-avatar", displayName, item.avatar)}<span><strong class="group-name">${escapeHtml(displayName)}</strong><small>${escapeHtml(item.contact_type || item.nickname || "微信联系人")}</small></span></span></td>
        <td><span class="contact-account">${escapeHtml(account)}</span><code class="contact-id">${escapeHtml(item.user_id)}</code></td>
        <td>${escapeHtml(contactRegion(item))}</td>
        <td><button class="row-action inspect-contact ${selected ? "active" : ""}" type="button" data-contact-id="${escapeHtml(item.user_id)}">${selected ? "正在查看" : "查看"}</button></td>
      </tr>`;
    }).join("") : `<tr><td colspan="4" class="table-empty">${escapeHtml(empty)}</td></tr>`;
    const first = items.length ? start + 1 : 0;
    const last = Math.min(start + visible.length, items.length);
    $("#contactRange").textContent = `第 ${first}–${last} 项，共 ${items.length} 项${state.contactTruncated ? " · 接口结果已截断" : ""}`;
    $("#contactPrevious").disabled = state.contactPage <= 1;
    $("#contactNext").disabled = state.contactPage >= pageCount;
    $$(".contact-row").forEach((row) => {
      const select = () => selectContact(row.dataset.contactId);
      row.addEventListener("click", (event) => { if (!event.target.closest("button")) select(); });
      row.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); select(); } });
    });
    $$(".inspect-contact").forEach((button) => button.addEventListener("click", () => selectContact(button.dataset.contactId)));
    bindAvatarFallbacks($("#contactRows"));
    renderContactInspector();
    if (scroll) scroll.scrollTop = previousScroll;
  }

  async function selectContact(contactId) {
    state.selectedContact = contactId;
    renderContacts();
    $("#contactInspector").scrollIntoView({block: "nearest", behavior: "smooth"});
    await Promise.all([loadContactDetail(contactId), loadPrivatePolicy(contactId)]);
  }

  async function loadContactDetail(contactId) {
    state.contactDetails[contactId] = {loading: true};
    renderContactInspector();
    try {
      const data = await api("get_contact_detail", contactId);
      if (state.selectedContact !== contactId) return;
      const detail = data?.item || {};
      state.contactDetails[contactId] = detail;
      const index = state.contacts.findIndex((item) => item.user_id === contactId);
      if (index >= 0) state.contacts[index] = {...state.contacts[index], ...detail};
    } catch (error) {
      if (state.selectedContact !== contactId) return;
      state.contactDetails[contactId] = {error: error?.message || "联系人详情暂时无法读取。"};
      reportError(error);
    }
    if (state.selectedContact === contactId) renderContactInspector();
  }

  async function loadPrivatePolicy(contactId) {
    if (!state.contactAccount || !contactId) return;
    state.privatePolicies[contactId] = {loading: true};
    renderContactInspector();
    try {
      state.privatePolicies[contactId] = await api("get_private_policy", state.contactAccount, contactId);
    } catch (error) {
      state.privatePolicies[contactId] = {error: error?.message || "联系人可用功能暂时无法读取。"};
      reportError(error);
    }
    if (state.selectedContact === contactId) renderContactInspector();
  }

  function contactFact(label, value, options = {}) {
    const text = String(value || "").trim();
    if (!text) return "";
    const rendered = options.code ? `<code>${escapeHtml(text)}</code>` : escapeHtml(text);
    return `<div><dt>${escapeHtml(label)}</dt><dd>${rendered}</dd></div>`;
  }

  function privatePolicyMarkup(data) {
    if (!data || data.loading) return '<section class="private-feature-policy"><h3>该联系人聊天规则</h3><div class="loading-row">正在读取聊天规则…</div></section>';
    if (data.error) return `<section class="private-feature-policy"><h3>该联系人聊天规则</h3><div class="member-empty error">${escapeHtml(data.error)}</div></section>`;
    const policy = data.policy || {};
    const quiet = policy.quiet_hours || {};
    const isWechatTeam = String(policy.contact_id || "").trim().toLowerCase() === "weixin";
    const choices = (data.features || []).map((feature) => `<label class="option-check"><input name="allowed_features" type="checkbox" value="${escapeHtml(feature.id)}" ${feature.allowed ? "checked" : ""}><span><strong>${escapeHtml(feature.name)}</strong></span></label>`).join("");
    return `<form class="private-feature-policy" id="privatePolicyForm" data-revision="${escapeHtml(policy.revision || "")}" novalidate>
      <div id="privateChatProfile" class="chat-profile"></div><div class="toggle-line"><span><strong>在此私聊使用机器人</strong></span><span class="toggle"><input id="privateBotEnabled" name="bot_enabled" type="checkbox" ${policy.bot_enabled !== false ? "checked" : ""}><label class="toggle-track" for="privateBotEnabled" aria-label="切换此私聊机器人"></label><span>${policy.bot_enabled !== false ? "已启用" : "已停用"}</span></span></div>
      ${isWechatTeam ? '<div class="toggle-line"><span><strong>微信团队私聊的模型回复</strong></span><span class="toggle"><input id="privateModelReplyDisabled" type="checkbox" disabled><label class="toggle-track" for="privateModelReplyDisabled" aria-label="微信团队私聊模型回复已关闭"></label><span>已关闭</span></span></div>' : ""}
      <div class="toggle-line"><span><strong>接收已配置的定时任务</strong></span><span class="toggle"><input id="privateScheduledEnabled" name="scheduled_enabled" type="checkbox" ${policy.scheduled_enabled !== false ? "checked" : ""}><label class="toggle-track" for="privateScheduledEnabled" aria-label="切换此私聊定时任务"></label><span>${policy.scheduled_enabled !== false ? "已允许" : "已暂停"}</span></span></div>
      <details class="advanced-details"><summary>更多私聊规则</summary><div class="toggle-line"><span><strong>安静时段</strong></span><span class="toggle"><input id="privateQuietEnabled" name="quiet_enabled" type="checkbox" aria-controls="privateQuietHoursFields" ${quiet.enabled ? "checked" : ""}><label class="toggle-track" for="privateQuietEnabled" aria-label="切换此私聊安静时段"></label><span>${quiet.enabled ? "已启用" : "已关闭"}</span></span></div>
      <div class="field-pair quiet-hours-fields" id="privateQuietHoursFields" ${quiet.enabled ? "" : "hidden"}><label><span>开始</span><input name="quiet_start" type="time" value="${escapeHtml(quiet.start || "22:00")}"></label><label><span>结束</span><input name="quiet_end" type="time" value="${escapeHtml(quiet.end || "08:00")}"></label></div>
      <label class="private-scheduled-limit"><span>每日定时消息上限</span><input name="daily_limit" type="number" min="0" max="100" value="${escapeHtml(policy.daily_limit ?? 3)}"><small>填 0 表示暂停该联系人的定时推送。</small></label>
      </details><details class="advanced-details"><summary>可用功能</summary>
      <p>勾选机器人在这段私聊中可以使用的功能。未单独配置的联系人沿用新安装默认启用范围。</p>
      <div class="option-grid compact-options private-feature-options">${choices || "<span class='subtle'>当前没有可分配的功能</span>"}</div></details>
    </form>`;
  }

  function renderContactInspector() {
    const panel = $("#contactInspector");
    const contact = state.contacts.find((item) => item.user_id === state.selectedContact);
    panel.classList.toggle("empty", !contact);
    if (!contact) {
      const message = state.contacts.length
        ? "选择左侧联系人后，可查看微信资料并配置可用功能。"
        : "刷新并读取到联系人后，可在这里查看个人资料和可用功能。";
      panel.innerHTML = `<div class="group-inspector-empty"><div class="empty-symbol">友</div><span class="eyebrow">个人详情</span><h2>${state.contacts.length ? "选择一个联系人" : "暂时没有联系人"}</h2><p>${message}</p></div>`;
      return;
    }
    const detailState = state.contactDetails[contact.user_id];
    const detail = detailState && !detailState.loading && !detailState.error ? {...contact, ...detailState} : contact;
    const displayName = contactDisplayName(detail);
    const facts = [
      contactFact("昵称", detail.nickname),
      contactFact("备注", detail.remark),
      contactFact("微信号", detail.alias, {code: true}),
      contactFact("微信标识", detail.user_id, {code: true}),
      contactFact("性别", detail.gender),
      contactFact("地区", contactRegion(detail) === "未提供" ? "" : contactRegion(detail)),
      contactFact("联系人类型", detail.contact_type),
      contactFact("个性签名", detail.signature),
    ].filter(Boolean).join("");
    const detailsMessage = detailState?.loading
      ? '<div class="profile-state">正在读取最新个人资料…</div>'
      : detailState?.error
        ? `<div class="profile-state error">${escapeHtml(detailState.error)}</div>`
        : facts ? `<dl class="contact-facts">${facts}</dl>` : '<div class="profile-state">当前接口没有返回更多个人资料。</div>';
    const policyData = state.privatePolicies[contact.user_id];
    const saveDisabled = !policyData || policyData.loading || policyData.error;
    panel.innerHTML = `<header class="inspector-hero"><div class="inspector-group-title">${avatarMarkup("contact-avatar large", displayName, detail.avatar)}<div><h2>${escapeHtml(displayName)}</h2><code class="inspector-id">${escapeHtml(detail.alias || detail.user_id)}</code></div></div><button class="row-action chat-scheduled-entry" type="button">该联系人的定时任务</button></header>
      <div class="inspector-scroll contact-detail-scroll"><details class="contact-profile"><summary>个人详情</summary><div class="contact-section-heading"><button class="row-action copy-contact" type="button">复制微信标识</button></div>${detailsMessage}</details>${privatePolicyMarkup(policyData)}</div>
      <footer class="inspector-footer"><span>资料只在当前运行期间保存在内存中。</span><button class="button primary" type="submit" form="privatePolicyForm" ${saveDisabled ? "disabled" : ""}>保存私聊规则</button></footer>`;
    $(".copy-contact", panel)?.addEventListener("click", () => copyText(contact.user_id, "微信标识"));
    $(".chat-scheduled-entry", panel)?.addEventListener("click", () => openScheduledForChat({type: "private", id: contact.user_id, name: displayName}, state.contactAccount));
    bindAvatarFallbacks(panel);
    void chatProfiles.mount($("#privateChatProfile"), contact.user_id, "private", state.contactAccount);
    $("#privatePolicyForm")?.addEventListener("submit", savePrivatePolicy);
    $$(".toggle input", panel).forEach((input) => input.addEventListener("change", updatePolicyToggle));
  }

  async function savePrivatePolicy(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $('button[form="privatePolicyForm"]');
    if (!button || !state.selectedContact) return;
    const contactId = state.selectedContact;
    const accountId = state.contactAccount;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "正在保存…";
    try {
      await api("save_private_policy",
        accountId,
        contactId,
        {allowed_features: $$('input[name="allowed_features"]:checked', form).map((input) => input.value), bot_enabled: form.elements.bot_enabled.checked, scheduled_enabled: form.elements.scheduled_enabled.checked, quiet_hours: {enabled: form.elements.quiet_enabled.checked, start: form.elements.quiet_start.value, end: form.elements.quiet_end.value, timezone: "Asia/Shanghai"}, daily_limit: Number(form.elements.daily_limit.value)},
        form.dataset.revision || "",
      );
      if (state.selectedContact === contactId && state.contactAccount === accountId) {
        await loadPrivatePolicy(contactId);
      }
    } catch (error) { reportError(error); }
    finally {
      if (button.isConnected) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = "保存私聊规则";
      }
    }
  }

  return {
    clearPendingGroupMemberLoading,
    hasGroupDrafts: () => Object.keys(groupDrafts).length > 0,
    loadContacts,
    loadGroups,
    renderContacts,
    renderGroupInspector,
    renderGroups,
    selectChatManagementTab,
  };
};
