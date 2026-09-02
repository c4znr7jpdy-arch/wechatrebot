"use strict";

const state = {
  catalog: [],
  currentId: null,
  detail: null,
  config: {},
  revision: "",
  dirty: false,
  busy: false,
  pendingAction: null,
};

const el = (id) => document.getElementById(id);
const list = el("plugin-list");
const search = el("plugin-search");
const detailView = el("detail-view");
const emptyView = el("empty-view");
const loadingView = el("loading-view");
const notice = el("notice");
let toastTimer = 0;
const pageLabels = {
  dashboard: "数据管理",
  settings: "专属设置",
  catalog: "内容目录",
  "model-manager": "模型管理",
  "chat-config": "对话设置",
  "checkin-manager": "签到任务",
  "recommendation-schedules": "推荐计划",
  subscriptions: "订阅管理",
  a_manage: "内容管理",
};

function customerText(value) {
  return String(value ?? "")
    .replace(/[\u{1F300}-\u{1FAFF}\u2600-\u27BF]/gu, "")
    .trim();
}

function setEngineState(online) {
  const node = el("engine-state");
  node.classList.toggle("online", Boolean(online));
  node.querySelector("span").textContent = online ? "回复引擎在线 · 支持热重载" : "回复引擎未启动 · 可离线配置";
}

function setNotice(message = "", kind = "") {
  notice.textContent = customerText(message);
  notice.className = `notice ${kind}`.trim();
  notice.hidden = !message;
}

function showToast(message) {
  const node = el("toast");
  window.clearTimeout(toastTimer);
  node.textContent = customerText(message);
  node.hidden = false;
  toastTimer = window.setTimeout(() => { node.hidden = true; }, 3200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new Error("本地配置服务返回了无法识别的数据。");
  }
  if (!response.ok || !payload.ok) {
    const error = new Error(customerText(payload.message || "操作未完成，请重试。"));
    error.status = response.status;
    throw error;
  }
  return payload.data;
}

function setBusy(value, activeButton = null, busyText = "处理中…") {
  state.busy = value;
  [el("save-config"), el("toggle-plugin"), el("reload-plugin")].forEach((button) => {
    button.disabled = value || button.dataset.locked === "true";
  });
  if (activeButton) {
    if (value) {
      activeButton.dataset.originalText = activeButton.textContent;
      activeButton.textContent = busyText;
    } else if (activeButton.dataset.originalText) {
      activeButton.textContent = activeButton.dataset.originalText;
      delete activeButton.dataset.originalText;
    }
  }
}

function setDirty(value) {
  state.dirty = value;
  el("save-hint").textContent = value ? "有未保存的修改" : "当前配置已保存";
}

function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function getAtPath(path) {
  return path.reduce((current, key) => current && typeof current === "object" ? current[key] : undefined, state.config);
}

function setAtPath(path, value) {
  let current = state.config;
  path.slice(0, -1).forEach((key) => {
    if (!current[key] || typeof current[key] !== "object" || Array.isArray(current[key])) current[key] = {};
    current = current[key];
  });
  current[path[path.length - 1]] = value;
  setDirty(true);
  refreshConditions();
}

function conditionMatches(condition, parentPath) {
  if (!condition || typeof condition !== "object") return true;
  return Object.entries(condition).every(([key, expected]) => {
    const actual = getAtPath([...parentPath, key]);
    return Array.isArray(expected) ? expected.includes(actual) : actual === expected;
  });
}

function refreshConditions() {
  document.querySelectorAll("[data-condition]").forEach((node) => {
    const condition = JSON.parse(node.dataset.condition || "{}");
    const parentPath = JSON.parse(node.dataset.parentPath || "[]");
    node.hidden = !conditionMatches(condition, parentPath);
  });
}

function isSecret(key, schema) {
  return schema._secret === true || /(?:api.?key|password|secret|token|credential|cookie)/i.test(key);
}

function choicesFor(schema) {
  if (Array.isArray(schema.options)) {
    return schema.options.map((value, index) => ({
      value,
      label: Array.isArray(schema.labels) && schema.labels[index] !== undefined ? schema.labels[index] : value,
    }));
  }
  if (schema._special === "select_provider") return state.detail.providers || [];
  if (schema._special === "select_embedding_provider") return state.detail.embedding_providers || [];
  if (schema._special === "select_persona") return (state.detail.personas || []).map((value) => ({ value, label: value }));
  return null;
}

function fieldShell(key, schema, path, parentPath) {
  const row = document.createElement("div");
  row.className = "field";
  row.dataset.fieldPath = JSON.stringify(path);
  if (schema.condition) {
    row.dataset.condition = JSON.stringify(schema.condition);
    row.dataset.parentPath = JSON.stringify(parentPath);
  }
  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = customerText(schema.description || key);
  row.appendChild(label);
  const control = document.createElement("div");
  control.className = "field-control";
  row.appendChild(control);
  if (schema.hint) {
    const hint = document.createElement("p");
    hint.className = "field-hint";
    hint.textContent = customerText(schema.hint);
    control.appendChild(hint);
  }
  return { row, label, control };
}

function addError(row, message) {
  row.classList.add("invalid");
  const error = document.createElement("div");
  error.className = "field-error";
  error.textContent = message;
  row.querySelector(".field-control").appendChild(error);
}

function clearErrors() {
  document.querySelectorAll(".field.invalid").forEach((node) => node.classList.remove("invalid"));
  document.querySelectorAll(".field-error").forEach((node) => node.remove());
}

function renderPrimitive(key, schema, path, parentPath) {
  const { row, label, control } = fieldShell(key, schema, path, parentPath);
  const hint = control.querySelector(".field-hint");
  const current = getAtPath(path);
  let input;

  if (schema.type === "bool") {
    const wrap = document.createElement("div");
    wrap.className = "switch-row";
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(current);
    input.disabled = schema._readonly === true;
    input.addEventListener("change", () => {
      status.textContent = input.checked ? "已开启" : "已关闭";
      setAtPath(path, input.checked);
    });
    const status = document.createElement("span");
    status.className = "switch-state";
    status.textContent = input.checked ? "已开启" : "已关闭";
    wrap.append(input, status);
    control.insertBefore(wrap, hint);
  } else {
    const choices = choicesFor(schema);
    if (choices) {
      input = document.createElement("select");
      input.className = "select-input";
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "跟随默认设置";
      input.appendChild(blank);
      choices.forEach(({ value, label: choiceLabel }) => {
        const option = document.createElement("option");
        option.value = JSON.stringify(value);
        option.textContent = customerText(choiceLabel);
        option.selected = current === value;
        input.appendChild(option);
      });
      if (current === null || current === undefined || current === "") blank.selected = true;
      input.addEventListener("change", () => setAtPath(path, input.value ? JSON.parse(input.value) : null));
    } else if (isSecret(key, schema)) {
      input = document.createElement("input");
      input.className = "text-input";
      input.type = "password";
      input.value = current ?? "";
      input.addEventListener("input", () => setAtPath(path, input.value));
    } else if (["list", "template_list"].includes(schema.type) || schema.editor_mode === true) {
      input = document.createElement("textarea");
      input.className = "json-input";
      input.spellcheck = false;
      input.value = typeof current === "string" && schema.editor_mode === true
        ? current
        : JSON.stringify(current ?? (schema.type === "list" || schema.type === "template_list" ? [] : ""), null, 2);
      input.dataset.json = schema.type === "list" || schema.type === "template_list" ? "true" : "false";
      input.addEventListener("input", () => {
        if (input.dataset.json !== "true") setAtPath(path, input.value);
        else setDirty(true);
      });
    } else if (schema.type === "text") {
      input = document.createElement("textarea");
      input.className = "json-input";
      input.value = current ?? "";
      input.addEventListener("input", () => setAtPath(path, input.value));
    } else {
      input = document.createElement("input");
      input.className = "text-input";
      input.type = schema.type === "int" || schema.type === "float" ? "number" : (isSecret(key, schema) ? "password" : "text");
      input.value = current ?? "";
      if (schema.slider) {
        if (schema.slider.min !== undefined) input.min = schema.slider.min;
        if (schema.slider.max !== undefined) input.max = schema.slider.max;
        if (schema.slider.step !== undefined) input.step = schema.slider.step;
      } else if (schema.type === "int") input.step = "1";
      else if (schema.type === "float") input.step = "any";
      input.addEventListener("input", () => {
        let value = input.value;
        if (schema.type === "int") value = input.value === "" ? null : Number.parseInt(input.value, 10);
        if (schema.type === "float") value = input.value === "" ? null : Number.parseFloat(input.value);
        setAtPath(path, value);
      });
      if (isSecret(key, schema)) {
        const wrap = document.createElement("div");
        wrap.className = "secret-wrap";
        const reveal = document.createElement("button");
        reveal.className = "secret-toggle";
        reveal.type = "button";
        reveal.textContent = "显示";
        reveal.addEventListener("click", () => {
          const showing = input.type === "text";
          input.type = showing ? "password" : "text";
          reveal.textContent = showing ? "显示" : "隐藏";
          input.focus();
        });
        wrap.append(input, reveal);
        control.insertBefore(wrap, hint);
        label.htmlFor = input.id = `field-${path.join("-")}`;
        input.disabled = schema._readonly === true;
        return row;
      }
    }
    input.disabled = schema._readonly === true;
    control.insertBefore(input, hint);
  }
  label.htmlFor = input.id = `field-${path.join("-")}`;
  return row;
}

function renderSchema(schema, container, parentPath = []) {
  Object.entries(schema || {}).forEach(([key, field]) => {
    if (!field || typeof field !== "object" || field.invisible === true || field._invisible === true) return;
    const path = [...parentPath, key];
    if (field.type === "object" && field.items && typeof field.items === "object") {
      const section = document.createElement("section");
      section.className = "config-section";
      if (field.condition) {
        section.dataset.condition = JSON.stringify(field.condition);
        section.dataset.parentPath = JSON.stringify(parentPath);
      }
      const title = document.createElement("h3");
      title.textContent = customerText(field.description || key);
      section.appendChild(title);
      if (field.hint) {
        const hint = document.createElement("p");
        hint.className = "section-hint";
        hint.textContent = customerText(field.hint);
        section.appendChild(hint);
      }
      const fields = document.createElement("div");
      fields.className = "section-fields";
      section.appendChild(fields);
      renderSchema(field.items, fields, path);
      container.appendChild(section);
    } else {
      container.appendChild(renderPrimitive(key, field, path, parentPath));
    }
  });
}

function materializeJsonInputs() {
  let ok = true;
  clearErrors();
  document.querySelectorAll("textarea[data-json='true']").forEach((input) => {
    const row = input.closest(".field");
    try {
      const value = JSON.parse(input.value || "[]");
      if (!Array.isArray(value)) throw new Error();
      setAtPath(JSON.parse(row.dataset.fieldPath), value);
    } catch (_error) {
      addError(row, "请输入有效的 JSON 列表，例如：[\"项目一\", \"项目二\"]");
      ok = false;
    }
  });
  return ok;
}

function renderCatalog() {
  const query = search.value.trim().toLocaleLowerCase("zh-CN");
  const filtered = state.catalog.filter((plugin) => customerText(plugin.display_name).toLocaleLowerCase("zh-CN").includes(query));
  list.replaceChildren();
  filtered.forEach((plugin) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `plugin-item${plugin.id === state.currentId ? " active" : ""}`;
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", plugin.id === state.currentId ? "true" : "false");
    button.innerHTML = `<span class="plugin-name"></span><span class="plugin-meta"></span><i class="status-dot${plugin.enabled ? " enabled" : ""}" aria-hidden="true"></i>`;
    button.querySelector(".plugin-name").textContent = customerText(plugin.display_name);
    const labels = [];
    if (plugin.configurable) labels.push(`${plugin.field_count} 项配置`);
    if (plugin.pages.length) labels.push(`${plugin.pages.length} 个高级页`);
    button.querySelector(".plugin-meta").textContent = labels.join(" · ") || "无需配置";
    button.addEventListener("click", () => requestSelection(plugin.id));
    list.appendChild(button);
  });
  el("plugin-count").textContent = `${state.catalog.length} 项`;
  el("plugin-empty").hidden = filtered.length > 0;
}

function updateDetailHeader() {
  const detail = state.detail;
  el("plugin-title").textContent = customerText(detail.display_name);
  const summary = [];
  summary.push(detail.enabled ? "当前已启用" : "当前已停用");
  if (detail.configurable) summary.push(`${detail.field_count} 项基础配置`);
  if (detail.pages.length) summary.push(`${detail.pages.length} 个高级管理页`);
  el("plugin-summary").textContent = summary.join(" · ");
  el("toggle-plugin").textContent = detail.enabled ? "停用功能" : "启用功能";
  el("toggle-plugin").dataset.locked = "false";
  el("toggle-plugin").disabled = state.busy;
  el("reload-plugin").dataset.locked = (!detail.engine_online || !detail.enabled).toString();
  el("reload-plugin").disabled = !detail.engine_online || !detail.enabled;
  el("reload-plugin").title = !detail.engine_online ? "请先在运行中心启动机器人" : (!detail.enabled ? "请先启用功能" : "立即重新载入这项功能");
  setEngineState(detail.engine_online);
  el("save-config").textContent = detail.engine_online && detail.enabled ? "保存并热重载" : "保存配置";
}

function renderDetail() {
  updateDetailHeader();
  setNotice("");
  const fields = el("config-fields");
  fields.replaceChildren();
  const hasConfig = state.detail.configurable && Object.keys(state.detail.schema || {}).length > 0;
  fields.hidden = !hasConfig;
  el("no-config").hidden = hasConfig;
  el("save-config").dataset.locked = (!hasConfig).toString();
  el("save-config").disabled = !hasConfig;
  if (hasConfig) renderSchema(state.detail.schema, fields);
  refreshConditions();
  renderPages();
  setDirty(false);
}

function renderPages() {
  const pages = state.detail.pages || [];
  el("pages-tab").hidden = pages.length === 0;
  const picker = el("page-picker");
  picker.replaceChildren();
  pages.forEach((pageName) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "page-button";
    button.textContent = pageLabels[pageName] || customerText(pageName.replace(/[-_]+/g, " "));
    button.addEventListener("click", () => openPluginPage(pageName, button));
    picker.appendChild(button);
  });
}

async function selectPlugin(moduleName) {
  if (state.busy) return;
  state.currentId = moduleName;
  renderCatalog();
  detailView.hidden = true;
  emptyView.hidden = true;
  loadingView.hidden = false;
  try {
    const detail = await api(`/control/plugins/${encodeURIComponent(moduleName)}`);
    state.detail = detail;
    state.config = clone(detail.config || {});
    state.revision = detail.revision;
    loadingView.hidden = true;
    detailView.hidden = false;
    showTab("basic");
    renderDetail();
  } catch (error) {
    loadingView.hidden = true;
    emptyView.hidden = false;
    setNotice(error.message, "error");
  }
}

function requestSelection(moduleName) {
  if (moduleName === state.currentId) return;
  if (!state.dirty) {
    selectPlugin(moduleName);
    return;
  }
  showDiscardModal(() => selectPlugin(moduleName));
}

function showDiscardModal(action) {
  state.pendingAction = action;
  el("modal-backdrop").hidden = false;
  el("modal-cancel").focus();
}

function closeDiscardModal() {
  state.pendingAction = null;
  el("modal-backdrop").hidden = true;
}

function showTab(name) {
  const advanced = name === "pages";
  el("basic-tab").classList.toggle("active", !advanced);
  el("pages-tab").classList.toggle("active", advanced);
  el("basic-tab").setAttribute("aria-selected", (!advanced).toString());
  el("pages-tab").setAttribute("aria-selected", advanced.toString());
  el("basic-panel").hidden = advanced;
  el("pages-panel").hidden = !advanced;
}

async function saveConfig(event) {
  event.preventDefault();
  if (state.busy || !state.detail) return;
  if (!materializeJsonInputs()) {
    setNotice("有些列表格式不正确，请修改标红的字段。", "error");
    return;
  }
  const button = el("save-config");
  setBusy(true, button, "正在保存…");
  setNotice("");
  try {
    const data = await api(`/control/plugins/${encodeURIComponent(state.currentId)}/config`, {
      method: "PUT",
      body: JSON.stringify({ config: state.config, revision: state.revision }),
    });
    state.revision = data.revision;
    setDirty(false);
    setNotice(data.message, "success");
    showToast(data.message);
  } catch (error) {
    setNotice(error.status === 409 ? `${error.message} 左侧重新选择当前功能即可刷新。` : error.message, "error");
  } finally {
    setBusy(false, button);
    updateDetailHeader();
  }
}

async function togglePlugin() {
  if (state.busy || !state.detail) return;
  const button = el("toggle-plugin");
  const enabled = !state.detail.enabled;
  setBusy(true, button, enabled ? "正在启用…" : "正在停用…");
  setNotice("");
  try {
    const data = await api(`/control/plugins/${encodeURIComponent(state.currentId)}/enabled`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    });
    state.detail.enabled = enabled;
    const item = state.catalog.find((plugin) => plugin.id === state.currentId);
    if (item) item.enabled = enabled;
    renderCatalog();
    setNotice(data.message, "success");
    showToast(data.message);
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    setBusy(false, button);
    updateDetailHeader();
  }
}

async function reloadPlugin() {
  if (state.busy || !state.detail) return;
  const button = el("reload-plugin");
  setBusy(true, button, "正在重载…");
  setNotice("");
  try {
    const data = await api(`/control/plugins/${encodeURIComponent(state.currentId)}/reload`, { method: "POST", body: "{}" });
    setNotice(data.message, "success");
    showToast(data.message);
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    setBusy(false, button);
    updateDetailHeader();
  }
}

async function openPluginPage(pageName, button) {
  if (!state.detail.engine_online) {
    setNotice("高级页面需要回复引擎在线，请先在运行中心启动机器人。", "error");
    return;
  }
  document.querySelectorAll(".page-button").forEach((node) => node.classList.remove("active"));
  button.classList.add("active");
  button.disabled = true;
  const originalText = button.textContent;
  button.textContent = "正在打开…";
  try {
    const data = await api(`/control/plugins/${encodeURIComponent(state.currentId)}/pages/${encodeURIComponent(pageName)}/entry`);
    const frame = el("plugin-frame");
    frame.src = data.content_path;
    frame.hidden = false;
    el("page-placeholder").hidden = true;
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function loadCatalog() {
  loadingView.hidden = false;
  emptyView.hidden = true;
  detailView.hidden = true;
  try {
    const data = await api("/control/catalog");
    state.catalog = data.plugins || [];
    if (data.product_name) {
      el("product-title").textContent = `${customerText(data.product_name)} · 功能配置`;
      document.title = `${customerText(data.product_name)} · 功能配置`;
    }
    setEngineState(data.engine_online);
    renderCatalog();
    loadingView.hidden = true;
    if (state.catalog.length) selectPlugin(state.catalog[0].id);
    else emptyView.hidden = false;
  } catch (error) {
    loadingView.hidden = true;
    emptyView.hidden = false;
    emptyView.querySelector("strong").textContent = "功能列表读取失败";
    emptyView.querySelector("span").textContent = error.message;
  }
}

search.addEventListener("input", renderCatalog);
el("clear-search").addEventListener("click", () => { search.value = ""; renderCatalog(); search.focus(); });
el("config-form").addEventListener("submit", saveConfig);
el("toggle-plugin").addEventListener("click", togglePlugin);
el("reload-plugin").addEventListener("click", reloadPlugin);
el("basic-tab").addEventListener("click", () => showTab("basic"));
el("pages-tab").addEventListener("click", () => showTab("pages"));
el("modal-cancel").addEventListener("click", closeDiscardModal);
el("modal-confirm").addEventListener("click", () => {
  const action = state.pendingAction;
  closeDiscardModal();
  setDirty(false);
  if (action) action();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !el("modal-backdrop").hidden) closeDiscardModal();
});
window.addEventListener("beforeunload", (event) => {
  if (state.dirty) event.preventDefault();
});

["clear-search", "basic-tab", "pages-tab", "modal-cancel", "modal-confirm"].forEach((id) => {
  el(id).disabled = false;
});

loadCatalog();
