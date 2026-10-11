/* Full conversation-core settings, using the runtime's existing configuration APIs. */
(function () {
  "use strict";

  window.createRobotCoreSettings = function (helpers) {
    const {read, request, save, applyBrand, schemaMarkup, configFromForm, escapeHtml: esc, confirmAction, toast, reportError, growTextarea} = helpers;
    const $ = (selector) => document.querySelector(selector);
    const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
    const state = {detail: null, stale: false, view: "global", templateId: null, templates: [], bindings: {}, config: {}, identity: {}, dirty: false, busy: false, loading: false, profilesAvailable: false, capabilityWarning: "", feedback: ""};
    const root = () => $("#coreDetail");
    const clone = (value) => structuredClone(value || {});
    const currentTemplate = () => state.templates.find((item) => item.id === state.templateId);
    const schema = () => state.view === "template" ? state.detail.template_schema : state.detail.advanced_schema;

    function remainingSchema(source, ...displayed) {
      return Object.fromEntries(Object.entries(source || {}).flatMap(([key, field]) => {
        const prior = displayed.map((part) => part?.[key]).filter(Boolean);
        if (!prior.length) return [[key, field]];
        if (field.type !== "object") return [];
        const items = remainingSchema(field.items, ...prior.map((part) => part.items));
        return Object.keys(items).length ? [[key, {...field, items}]] : [];
      }));
    }

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
        state.profilesAvailable = false; state.capabilityWarning = "";
        if (!detail.engine_online) { state.view = "global"; state.templateId = null; }
        if (detail.engine_online) await loadProfiles();
        if (!state.profilesAvailable) { state.view = "global"; state.templateId = null; }
        if (state.view === "template" && !currentTemplate()) { state.view = "global"; state.templateId = null; }
        resetDraft(); render();
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
    }

    function renderNavigation() {
      const online = state.detail?.engine_online && state.profilesAvailable;
      $("#coreStatus").textContent = state.detail?.engine_online ? "在线 · 保存后应用" : "离线 · 启动时使用";
      $("#newCoreTemplate").disabled = state.busy || !online || !Object.keys(state.detail?.template_schema || {}).length;
      $("#coreTemplateSource").disabled = $("#newCoreTemplate").disabled;
      $("#refreshCore").disabled = state.busy;
      const count = (id) => Object.values(state.bindings).filter((value) => value === id).length;
      $("#coreTemplateList").innerHTML = `<label class="template-picker"><span>正在编辑</span><select id="activeCoreTemplate" ${state.busy ? "disabled" : ""}><option value="">全局默认</option>${state.templates.map((item) => `<option value="${esc(item.id)}" ${state.templateId === item.id ? "selected" : ""} ${!online ? "disabled" : ""}>${esc(item.name)} · ${count(item.id)} 个聊天使用</option>`).join("")}</select></label><p class="template-help">${state.view === "template" ? `修改仅影响使用此模板的 ${count(state.templateId)} 个聊天，其他聊天不受影响。` : state.templates.length ? `已有 ${state.templates.length} 个模板，可在上方选择查看和编辑。` : "还没有模板。大多数聊天使用全局默认即可；需要不同回复风格时再新建。"}</p>`;
      $("#activeCoreTemplate").addEventListener("change", (event) => {
        const id = event.target.value;
        event.target.value = state.templateId || "";
        void selectView(id ? "template" : "global", id || null);
      });
    }

    async function selectView(view, templateId = null) {
      if (guard(() => selectView(view, templateId))) return;
      state.view = view; state.templateId = templateId; resetDraft(); render();
    }

    function note() {
      if (state.feedback) return state.feedback;
      if (state.dirty) return "有修改尚未保存。";
      if (state.view === "template") return "打开“单独设置”来修改这一项；关闭时跟随全局，后续全局调整也会同步。";
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
      if (state.view === "global") {
        const nameInput = $("#coreForm")?.elements.robot_name;
        const adminInput = $("#coreForm")?.elements.admin_id;
        if (nameInput) {
          const robotName = String(nameInput.value || "").trim();
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
      const template = currentTemplate();
      const schemas = {basic: state.detail.basic_schema || {}, models: state.detail.model_schema || {}, advanced: state.detail.advanced_schema || {}};
      const templateBasic = Object.fromEntries(Object.entries(state.detail.template_schema || {}).filter(([key]) => key in schemas.basic));
      const templateMore = remainingSchema(state.detail.template_schema, templateBasic);
      const identityMarkup = state.view === "global" ? `<section class="core-identity-card" aria-labelledby="coreIdentityTitle"><div class="core-section-heading"><div><h3 id="coreIdentityTitle">机器人名称与管理员</h3></div></div><div class="config-section-grid"><label class="form-field"><span>机器人名称</span><input name="robot_name" maxlength="32" placeholder="${esc(state.identity.wechat_nickname || "连接微信后读取昵称")}" value="${esc(state.identity.robot_name || "")}"><span class="field-error" role="alert" hidden></span></label><label class="form-field"><span>管理员微信号</span><input name="admin_id" autocomplete="username" placeholder="wxid_..." value="${esc(state.identity.admin_id || "")}"><span class="field-error" role="alert" hidden></span></label></div></section>` : "";
      root().innerHTML = `<div class="core-detail-heading"><div><h2>${esc(state.view === "template" ? template?.name || "配置模板" : "机器人配置")}</h2></div>${state.view === "template" ? '<div class="button-row"><button class="button ghost small" id="renameCoreTemplate" type="button">重命名</button><button class="button danger small" id="deleteCoreTemplate" type="button">删除</button></div>' : ""}</div>
        <form class="feature-form core-form" id="coreForm" novalidate>${state.view === "template" ? `<button class="row-action" id="backCoreGlobal" type="button">返回全局机器人配置</button><label class="form-field full"><span>模板说明</span><textarea class="resize-none" name="template_description" rows="2">${esc(template?.description || "")}</textarea></label>${schemaMarkup(templateBasic, state.config, [], "core-")}${Object.keys(templateMore).length ? `<details class="core-advanced"><summary>更多模板设置</summary>${schemaMarkup(templateMore, state.config, [], "core-")}</details>` : ""}` : `${identityMarkup}${schemaMarkup(schemas.basic, state.config, [], "core-")}<details class="core-advanced"><summary>模型设置</summary>${schemaMarkup(remainingSchema(schemas.models, schemas.basic), state.config, [], "core-")}</details><details class="core-advanced"><summary>高级设置</summary>${schemaMarkup(remainingSchema(schemas.advanced, schemas.basic, schemas.models), state.config, [], "core-")}</details>`}</form>
        <p class="form-note" id="coreNote" role="status">${esc(note())}</p><div class="core-savebar"><button class="button primary" id="saveCore" type="button">${state.view === "template" ? "保存模板" : state.detail.engine_online ? "保存并应用" : "保存设置"}</button></div>`;
      if (state.view === "template") attachOverrides();
      else attachResets();
      $("#backCoreGlobal")?.addEventListener("click", () => selectView("global"));
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
        // schemaMarkup uses a label for the field; avoid nesting another label.
        if (row.tagName === "LABEL") {
          const fieldLabel = row.firstElementChild;
          if (control.id) { const name = document.createElement("label"); name.htmlFor = control.id; name.textContent = fieldLabel.textContent; fieldLabel.replaceWith(name); }
          const wrapper = document.createElement("div"); wrapper.className = row.className;
          wrapper.append(...row.childNodes); row.replaceWith(wrapper); row = wrapper;
        }
        label.innerHTML = `<input type="checkbox" data-path="${esc(control.dataset.path)}" ${active ? "checked" : ""}><span>单独设置</span>`;
        row.append(label);
        const sync = () => {
          const customized = label.querySelector('input').checked;
          $$('.config-input, .config-list-option', row).forEach((input) => { input.disabled = !customized; });
          row.classList.toggle('inherited', !customized);
          label.querySelector("span").textContent = customized ? "单独设置" : "跟随全局 · 单独设置";
          if (!customized) {
            const inherited = state.detail.advanced_config[path[0]]?.[path[1]];
            if (control.dataset.type === 'bool') control.checked = Boolean(inherited);
            else if (control.dataset.type === 'json') control.value = JSON.stringify(inherited || [], null, 2);
            else control.value = inherited ?? "";
            const caption = control.closest('.toggle')?.querySelector('span:last-child');
            if (caption) caption.textContent = control.checked ? "已启用" : "已停用";
          }
        };
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
      if (error.input) { let ancestor = error.input.parentElement; while (ancestor) { if (ancestor.tagName === 'DETAILS') ancestor.open = true; ancestor = ancestor.parentElement; } error.input.setAttribute('aria-invalid', 'true'); const message = error.input.closest('.form-field').querySelector('.field-error'); message.textContent = error.message; message.hidden = false; error.input.focus(); }
    }

    async function busy(action) {
      if (state.busy) return;
      state.busy = true; renderNavigation();
      $$('input, select, textarea, button', root()).forEach((input) => { input.dataset.coreDisabled = String(input.disabled); input.disabled = true; });
      try { await action(); window.dispatchEvent(new Event("chat-profiles-changed")); }
      catch (error) { showError(error); }
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
      confirmAction({title: '新建配置模板', message: fromGlobal ? '从当前已保存的全局配置复制，创建后可编辑覆盖项，再到群聊&私聊管理选择使用。' : '创建空白模板，所有参数先跟随全局；打开需要单独设置的项后，再到聊天中选择此模板。', confirmText: '创建', intent: 'primary', field: {label: '模板名称', type: 'text', value: '默认模板'}, onConfirm: async (name) => {
        if (!String(name || '').trim()) throw new Error('请填写模板名称。');
        await busy(async () => { const item = await request('POST', 'profiles/create', {name: name.trim(), from_global: fromGlobal}); await loadProfiles(true); state.view = 'template'; state.templateId = item.id; resetDraft(); render(); $(".template-create").open = false; $("#coreDetail h2").setAttribute("tabindex", "-1"); $("#coreDetail h2").focus({preventScroll: true}); toast('模板已创建。'); });
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

    function init() {
      window.addEventListener("chat-profiles-changed", () => { if (!state.busy) state.stale = true; });
      $("#refreshCore").addEventListener('click', () => load(true));
      $("#newCoreTemplate").addEventListener('click', createTemplate);
    }
    return {init, load, guard, invalidate() { state.stale = true; }, get dirty() { return state.dirty; }, get busy() { return state.busy || state.loading; }};
  };
})();
