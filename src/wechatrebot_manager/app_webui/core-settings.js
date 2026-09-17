/* Full conversation-core settings, using the runtime's existing configuration APIs. */
(function () {
  "use strict";

  window.createRobotCoreSettings = function (helpers) {
    const {read, request, save, applyBrand, schemaMarkup, configFromForm, escapeHtml: esc, confirmAction, toast, reportError, growTextarea} = helpers;
    const $ = (selector) => document.querySelector(selector);
    const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
    const state = {detail: null, stale: false, view: "global", tab: "basic", templateId: null, templates: [], bindings: {}, chats: [], statuses: [], config: {}, identity: {}, dirty: false, busy: false, loading: false, profilesAvailable: false, capabilityWarning: "", monitorBusy: false, feedback: "", kind: "all"};
    const root = () => $("#coreDetail");
    const visible = () => $("#page-settings").classList.contains("active") && !$("#coreWorkspace").hidden;
    const clone = (value) => structuredClone(value || {});
    const currentTemplate = () => state.templates.find((item) => item.id === state.templateId);
    const schema = () => state.view === "template" ? state.detail.template_schema : ({basic: state.detail.basic_schema, models: state.detail.model_schema, advanced: state.detail.advanced_schema}[state.tab] || {});

    function guard(next) {
      if (state.busy) return true;
      if (!state.dirty) return false;
      confirmAction({title: "离开前丢弃核心配置修改？", message: "这些修改尚未保存。留在此页可继续编辑或保存。", confirmText: "丢弃修改并继续", onConfirm: async () => { resetDraft(); render(); await next(); }});
      return true;
    }

    function resetDraft() {
      state.config = clone(state.view === "template" ? currentTemplate()?.config : state.detail?.advanced_config);
      state.identity = clone(state.detail?.identity);
      state.dirty = false;
      state.feedback = "";
    }

    async function load(force = false) {
      if (state.loading) return;
      if (state.detail && !force && (!state.stale || state.dirty)) return;
      if (force && guard(() => load(true))) return;
      state.loading = true;
      try {
        const detail = await read();
        state.detail = detail; state.stale = false;
        if (!Object.keys(detail.basic_schema || {}).length) state.tab = "advanced";
        state.profilesAvailable = false; state.capabilityWarning = "";
        if (!detail.engine_online) { state.view = "global"; state.templateId = null; }
        if (detail.engine_online) await loadProfiles();
        if (!state.profilesAvailable) { state.view = "global"; state.templateId = null; }
        if (state.view === "template" && !currentTemplate()) { state.view = "global"; state.templateId = null; }
        resetDraft(); render();
        if (state.view === "monitor") await refreshMonitor();
      } catch (error) {
        root().innerHTML = '<div class="empty-state"><h2>核心配置暂时无法读取</h2><p>请确认完整程序已安装，再点击“重新读取”。</p></div>';
        reportError(error);
      } finally { state.loading = false; renderNavigation(); }
    }

    async function loadProfiles(strict = false) {
      try {
        const profiles = await request("GET", "profiles");
        state.templates = profiles.templates || [];
        state.bindings = profiles.bindings || {};
        state.profilesAvailable = true; state.capabilityWarning = "";
      } catch (error) {
        state.profilesAvailable = false;
        state.capabilityWarning = `模板接口暂不可用：${error.message}。全局配置仍可编辑和保存。`;
        if (strict) throw error;
        return;
      }
      try { state.chats = await request("GET", "chats") || []; }
      catch (error) { state.capabilityWarning = `联系人列表读取失败：${error.message}。模板与全局配置仍可编辑。`; }
    }

    function renderNavigation() {
      const online = state.detail?.engine_online && state.profilesAvailable;
      $("#coreStatus").textContent = state.detail?.engine_online ? "在线 · 保存后应用" : "离线 · 启动时使用";
      $$('[data-core-view]').forEach((button) => { button.classList.toggle("active", button.dataset.coreView === state.view); button.setAttribute("aria-pressed", String(button.dataset.coreView === state.view)); button.disabled = state.busy || (button.dataset.coreView === "monitor" && !online); });
      $("#newCoreTemplate").disabled = state.busy || !online || !Object.keys(state.detail?.template_schema || {}).length;
      $("#coreTemplateSource").disabled = $("#newCoreTemplate").disabled;
      $("#refreshCore").disabled = state.busy;
      $("#coreTemplateList").innerHTML = state.templates.map((item) => `<button class="core-nav ${state.view === "template" && state.templateId === item.id ? "active" : ""}" type="button" data-core-template="${esc(item.id)}" ${!online || state.busy ? "disabled" : ""}>${esc(item.name)}<span>${esc(item.description || "按聊天绑定使用")}</span></button>`).join("") || '<p class="core-sidebar-note">暂无配置模板</p>';
      $$('[data-core-template]').forEach((button) => button.addEventListener("click", () => selectView("template", button.dataset.coreTemplate)));
    }

    async function selectView(view, templateId = null) {
      if (guard(() => selectView(view, templateId))) return;
      state.view = view; state.templateId = templateId; resetDraft(); render();
      if (view === "monitor") await refreshMonitor();
    }

    function note() {
      if (state.feedback) return state.feedback;
      if (state.dirty) return "有修改尚未保存。";
      if (state.view === "template") return "勾选“覆盖全局”后填写该模板的值；未勾选项跟随全局配置。";
      if (state.capabilityWarning) return state.capabilityWarning;
      if (!state.detail.engine_online) return "未连接到机器人：仅保存到本机，启动后加载。若机器人仍在运行，请恢复连接后重新保存。";
      if (!state.detail.advanced_config.analyzer_model) return "群聊分析模型尚未绑定；请到“模型设置”选择已配置模型，才能完整使用接话分析。";
      return "保存后自动应用并核验。正在执行的工作可能被中断。";
    }

    function markDirty() {
      state.dirty = true; state.feedback = "";
      $("#coreNote").textContent = note();
    }

    function draftFromForm() {
      if (state.view === "global" && state.tab === "basic") {
        const nameInput = $("#coreForm")?.elements.robot_name;
        const adminInput = $("#coreForm")?.elements.admin_id;
        if (nameInput) {
          const robotName = String(nameInput.value || "").trim();
          if (!robotName) {
            const error = new Error("机器人名称不能为空。");
            error.input = nameInput;
            throw error;
          }
          state.identity = {robot_name: robotName, admin_id: String(adminInput?.value || "").trim()};
        }
      }
      const draft = configFromForm($("#coreForm"), state.config);
      if (state.view === "template") {
        $$('.core-override input', root()).forEach((input) => {
          const [group, key] = JSON.parse(input.dataset.path);
          if (!input.checked && draft[group]) delete draft[group][key];
        });
        Object.keys(draft).forEach((group) => { if (!Object.keys(draft[group]).length) delete draft[group]; });
      }
      return draft;
    }

    function render() {
      if (!state.detail) return;
      renderNavigation();
      if (state.view === "monitor") { renderMonitor(); return; }
      const template = currentTemplate();
      const schemas = {basic: state.detail.basic_schema || {}, models: state.detail.model_schema || {}, advanced: state.detail.advanced_schema || {}};
      const identityMarkup = state.view === "global" && state.tab === "basic" ? `<section class="core-identity-card" aria-labelledby="coreIdentityTitle"><div class="core-section-heading"><div><span class="eyebrow">常用设置</span><h3 id="coreIdentityTitle">机器人名称与管理员</h3><p>机器人名称同时用于窗口、导航、自我介绍和首个点名昵称。</p></div><span class="status-badge">统一身份</span></div><div class="config-section-grid"><label class="form-field"><span>机器人名称</span><input name="robot_name" maxlength="32" required value="${esc(state.identity.robot_name || "小妹")}"><small>默认名称为“小妹”；保存后不再保留旧名称。</small><span class="field-error" role="alert" hidden></span></label><label class="form-field"><span>管理员微信号</span><input name="admin_id" autocomplete="username" placeholder="wxid_..." value="${esc(state.identity.admin_id || "")}"><small>留空表示暂不设置管理员；管理员权限在机器人重启后生效。</small><span class="field-error" role="alert" hidden></span></label></div></section>` : "";
      root().innerHTML = `<div class="core-detail-heading"><div><h2>${esc(state.view === "template" ? template?.name || "配置模板" : "全局配置")}</h2><p>${state.view === "template" ? "身份、唤醒、离场应答、回复长度、能量与节奏；其余参数跟随全局。" : "完整对话核心配置，机器人名称、管理员和群聊身份都从这里统一管理。"}</p></div>${state.view === "template" ? '<div class="button-row"><button class="button ghost small" id="renameCoreTemplate" type="button">重命名</button><button class="button danger small" id="deleteCoreTemplate" type="button">删除</button></div>' : '<span class="status-badge online">统一加载</span>'}</div>
        ${state.view === "global" ? `<div class="config-tabs" aria-label="核心配置分类">${[["basic", "常用设置"], ["models", "模型设置"], ["advanced", "专业设置（全部）"]].filter(([key]) => Object.keys(schemas[key]).length).map(([key, label]) => `<button type="button" data-core-tab="${key}" aria-pressed="${state.tab === key}" class="${state.tab === key ? "active" : ""}">${label}</button>`).join("")}</div>${state.tab === "models" ? '<p class="feature-prerequisite">群聊分析模型是完整使用所必需的；这里的绑定不会改变日常回复模型。</p>' : ''}` : ''}
        <form class="feature-form core-form" id="coreForm" novalidate>${state.view === "template" ? `<label class="form-field full">模板说明<textarea name="template_description" rows="2">${esc(template?.description || "")}</textarea></label>` : ''}${identityMarkup}${schemaMarkup(schema(), state.config, [], "core-")}</form>
        <div class="core-savebar"><span class="form-note" id="coreNote" role="status">${esc(note())}</span><button class="button primary" id="saveCore" type="button">${state.view === "template" || state.detail.engine_online ? "保存并应用" : "保存设置"}</button></div>`;
      if (state.view === "template") attachOverrides();
      else attachResets();
      $$('[data-core-tab]', root()).forEach((button) => button.addEventListener("click", () => {
        try { state.config = draftFromForm(); state.tab = button.dataset.coreTab; render(); $("[data-core-tab='" + state.tab + "']")?.focus(); }
        catch (error) { showError(error); }
      }));
      $("#coreForm").addEventListener("input", markDirty);
      $("#coreForm").addEventListener("change", markDirty);
      $("#coreForm").addEventListener("submit", (event) => { event.preventDefault(); saveCurrent(); });
      $$('.config-input[data-type="bool"]', root()).forEach((input) => input.addEventListener("change", () => { input.closest('.toggle').querySelector('span:last-child').textContent = input.checked ? "已启用" : "已停用"; }));
      $$('textarea', root()).forEach(growTextarea);
      $("#saveCore").addEventListener("click", saveCurrent);
      $("#renameCoreTemplate")?.addEventListener("click", renameTemplate);
      $("#deleteCoreTemplate")?.addEventListener("click", deleteTemplate);
    }

    function attachOverrides() {
      $$('.form-field', root()).forEach((row) => {
        const control = row.querySelector('[data-path]');
        if (!control) return;
        const path = JSON.parse(control.dataset.path);
        const value = state.config[path[0]]?.[path[1]];
        const active = value !== undefined;
        const label = document.createElement('label'); label.className = "core-override";
        label.innerHTML = `<input type="checkbox" data-path="${esc(control.dataset.path)}" ${active ? "checked" : ""}><span>覆盖全局</span>`;
        row.append(label);
        const sync = () => { $$('.config-input, .config-list-option', row).forEach((input) => { input.disabled = !label.querySelector('input').checked; }); row.classList.toggle('inherited', !label.querySelector('input').checked); };
        if (!active) {
          const global = state.detail.advanced_config[path[0]]?.[path[1]];
          if (control.dataset.type === 'bool') control.checked = Boolean(global);
          else if (control.dataset.type === 'json') control.value = JSON.stringify(global || [], null, 2);
          else control.value = global ?? "";
          const caption = control.closest('.toggle')?.querySelector('span:last-child');
          if (caption) caption.textContent = control.checked ? "已启用" : "已停用";
        }
        label.querySelector('input').addEventListener('change', () => { sync(); markDirty(); }); sync();
      });
    }

    function attachResets() {
      Object.entries(schema()).forEach(([key, section]) => {
        if (section.type !== 'object') return;
        const field = $$('[data-path]', root()).find((input) => JSON.parse(input.dataset.path)[0] === key);
        let container = field?.closest('.config-section');
        while (container?.parentElement.closest('.config-section')) container = container.parentElement.closest('.config-section');
        if (!container) return;
        const button = document.createElement('button'); button.type = 'button'; button.className = 'row-action core-reset'; button.textContent = '恢复此组默认值';
        const resetValues = (items, values) => Object.entries(items || {}).forEach(([name, meta]) => { if (meta.type === 'object') { values[name] ||= {}; resetValues(meta.items, values[name]); } else if (meta.default !== undefined) values[name] = structuredClone(meta.default); });
        button.addEventListener('click', () => { try { state.config = draftFromForm(); state.config[key] ||= {}; resetValues(section.items, state.config[key]); state.dirty = true; state.feedback = ''; render(); } catch (error) { showError(error); } });
        container.append(button);
      });
    }

    function showError(error) {
      reportError(error);
      $("#coreNote") && ($("#coreNote").textContent = error.message);
      if (error.input) { error.input.setAttribute('aria-invalid', 'true'); const message = error.input.closest('.form-field').querySelector('.field-error'); message.textContent = error.message; message.hidden = false; error.input.focus(); }
    }

    async function busy(action) {
      if (state.busy) return;
      state.busy = true; renderNavigation();
      $$('input, select, textarea, button', root()).forEach((input) => { input.dataset.coreDisabled = String(input.disabled); input.disabled = true; });
      try { await action(); }
      catch (error) { if (state.view === 'monitor') { state.feedback = `操作失败：${error.message}；绑定仍按上次读取的配置显示。`; renderMonitor(); } showError(error); }
      finally { state.busy = false; renderNavigation(); $$('[data-core-disabled]', root()).forEach((input) => { input.disabled = input.dataset.coreDisabled === 'true'; delete input.dataset.coreDisabled; }); }
    }

    async function saveCurrent() {
      let config;
      try { config = draftFromForm(); } catch (error) { showError(error); return; }
      await busy(async () => {
        $("#coreNote").textContent = '正在保存并确认生效…';
        const result = state.view === 'template'
          ? await request('POST', 'profiles/update', {id: state.templateId, updated_at: currentTemplate().updated_at, description: $("#coreForm").elements.template_description.value, config})
          : await save(config, state.detail.revision, state.identity);
        if (result?.brand) applyBrand(result.brand);
        // Always read back the authoritative persisted/runtime result.
        if (state.view === 'template') await loadProfiles(true);
        else state.detail = await read();
        resetDraft(); state.feedback = result?.message || (state.view === 'template' ? '模板已保存，绑定聊天的后续消息使用新配置。' : '已保存，请确认生效状态。');
        render(); toast(state.feedback);
      });
    }

    function createTemplate() {
      if (guard(createTemplate)) return;
      const fromGlobal = $("#coreTemplateSource").value === 'global';
      confirmAction({title: '新建配置模板', message: fromGlobal ? '从当前已保存的全局配置复制，创建后可编辑覆盖项，再到联系人监控中绑定。' : '创建空白模板，所有参数先跟随全局；勾选需要覆盖的项后再绑定聊天。', confirmText: '创建', intent: 'primary', field: {label: '模板名称', type: 'text', value: ''}, onConfirm: async (name) => {
        if (!String(name || '').trim()) throw new Error('请填写模板名称。');
        await busy(async () => { const item = await request('POST', 'profiles/create', {name: name.trim(), from_global: fromGlobal}); await loadProfiles(true); state.view = 'template'; state.templateId = item.id; resetDraft(); render(); toast('模板已创建。'); });
      }});
    }

    function renameTemplate() {
      if (guard(renameTemplate)) return;
      confirmAction({title: '重命名配置模板', message: '绑定关系不变。', confirmText: '保存', intent: 'primary', field: {label: '模板名称', type: 'text', value: currentTemplate().name}, onConfirm: async (name) => {
        if (!String(name || '').trim()) throw new Error('请填写模板名称。');
        await busy(async () => { await request('POST', 'profiles/update', {id: state.templateId, updated_at: currentTemplate().updated_at, name: name.trim()}); await loadProfiles(true); render(); });
      }});
    }

    function deleteTemplate() {
      if (guard(deleteTemplate)) return;
      confirmAction({title: '删除配置模板？', message: '所有绑定此模板的聊天会自动回到全局配置。删除后无法撤销。', confirmText: '删除模板', onConfirm: async () => {
        await busy(async () => { await request('POST', 'profiles/delete', {id: state.templateId}); await loadProfiles(true); state.view = 'global'; state.templateId = null; resetDraft(); render(); toast('模板已删除，绑定聊天已回到全局配置。'); });
      }});
    }

    const privateOrigin = (id) => /:FriendMessage:|:PrivateMessage:/.test(id);
    function bindingKey(id) {
      if (Object.hasOwn(state.bindings, id) || privateOrigin(id)) return id;
      const suffix = id.split(':').at(-1);
      if (Object.hasOwn(state.bindings, suffix)) return suffix;
      return Object.keys(state.bindings).find((key) => !privateOrigin(key) && key.split(':').at(-1) === suffix) || id;
    }

    function renderMonitor() {
      const byId = new Map(state.chats.map((item) => [item.chat_id, {...item, status: {current_status: 'Unknown'}}]));
      state.statuses.forEach((item) => byId.set(item.chat_id, item));
      Object.keys(state.bindings).forEach((id) => { if (![...byId.keys()].some((key) => bindingKey(key) === id)) byId.set(id, {chat_id: id, status: {current_status: 'Unknown'}}); });
      const items = [...byId.values()].filter((item) => { const kind = item.kind || (/:FriendMessage:|:PrivateMessage:/.test(item.chat_id) ? 'private' : 'group'); return state.kind === 'all' || state.kind === kind; });
      root().innerHTML = `<div class="core-detail-heading"><div><h2>联系人监控与绑定</h2><p>每 3 秒读取一次在场状态、能量与最近决策，不向聊天发送消息。</p></div><button class="button secondary small" id="refreshCoreMonitor" type="button">刷新状态</button></div><div class="config-tabs" aria-label="联系人类型">${[['all', '全部'], ['group', '群聊'], ['private', '私聊']].map(([kind, label]) => `<button type="button" data-core-kind="${kind}" aria-pressed="${state.kind === kind}" class="${state.kind === kind ? 'active' : ''}">${label}</button>`).join('')}</div><div class="core-monitor-grid">${items.map((item) => {
        const status = item.status?.current_status;
        const patrol = item.patrol || {};
        return `<article class="core-chat-card"><div class="core-chat-heading"><strong>${esc(item.display_name || '未命名联系人')}</strong><span class="status-badge ${status === 'OBSERVATION' ? 'online' : ''}">${status === 'OBSERVATION' ? '在场' : status === 'NOT_PRESENT' ? '离场' : '状态待确认'}</span></div><small class="core-chat-id">${esc(item.chat_id)}</small><p>能量 ${item.energy == null ? '—' : Math.round(item.energy)} · ${esc({secretary: '巡检中', assistant: '点名等待', rest: '休息中'}[patrol.waiting] || '空闲')}${patrol.waiting ? ` ${Math.round(patrol.remaining || 0)}/${Math.round(patrol.total || 0)} 秒` : ''}</p><p>最近决策：${item.last_decision ? `${item.last_decision.should_reply ? '回复' : '不回复'} · ${esc(item.last_decision.summary || '无说明')}` : '暂无'}</p><label>绑定配置模板<select data-core-binding="${esc(item.chat_id)}"><option value="">全局配置（默认）</option>${state.templates.map((tpl) => `<option value="${esc(tpl.id)}" ${state.bindings[bindingKey(item.chat_id)] === tpl.id ? 'selected' : ''}>${esc(tpl.name)}</option>`).join('')}</select></label></article>`;
      }).join('') || '<div class="empty-state compact"><p>暂无联系人；产生聊天消息后会显示，也会保留已有绑定关系。</p></div>'}</div><p class="form-note" id="coreMonitorNote" role="status">${esc(state.feedback)}</p>`;
      $$('[data-core-kind]', root()).forEach((button) => button.addEventListener('click', () => { state.kind = button.dataset.coreKind; renderMonitor(); }));
      $("#refreshCoreMonitor").addEventListener('click', () => refreshMonitor(true));
      $$('[data-core-binding]', root()).forEach((select) => select.addEventListener('change', () => busy(async () => { await request('POST', 'bindings/set', {chat_id: bindingKey(select.dataset.coreBinding), template_id: select.value}); await loadProfiles(true); state.feedback = '绑定已生效，未向联系人发送消息。'; await refreshMonitor(true); renderMonitor(); })));
    }

    async function refreshMonitor(force = false) {
      if (!visible() || state.view !== 'monitor' || state.monitorBusy || !state.profilesAvailable || !force && (state.busy || root().contains(document.activeElement) && document.activeElement.tagName === 'SELECT')) return;
      state.monitorBusy = true;
      try {
        const [profiles, statuses] = await Promise.all([request('GET', 'profiles'), request('GET', 'chat_status')]);
        if (state.busy && !force) return;
        state.templates = profiles.templates || []; state.bindings = profiles.bindings || {}; state.statuses = statuses || [];
        if (visible() && state.view === 'monitor') { renderNavigation(); renderMonitor(); }
      }
      catch (error) { state.feedback = `状态读取失败：${error.message}；当前显示上次读取的数据。`; if (state.view === 'monitor' && $("#coreMonitorNote")) $("#coreMonitorNote").textContent = state.feedback; }
      finally { state.monitorBusy = false; }
    }

    function init() {
      $$('[data-core-view]').forEach((button) => button.addEventListener('click', () => selectView(button.dataset.coreView)));
      $("#refreshCore").addEventListener('click', () => load(true));
      $("#newCoreTemplate").addEventListener('click', createTemplate);
      window.setInterval(() => { if (!state.busy) void refreshMonitor(); }, 3000);
    }
    return {init, load, guard, invalidate() { state.stale = true; }, get dirty() { return state.dirty; }, get busy() { return state.busy || state.loading; }};
  };
})();
