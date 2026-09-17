"use strict";

const state = {
  catalog: [],
  currentId: null,
  detail: null,
  config: {},
  revision: "",
  dirty: false,
  busy: false,
  activeTab: "basic",
  pendingAction: null,
};
const requestedFeatureId = new URLSearchParams(window.location.search).get("feature") || "";
const memeManagerFeatureId = "feature-222a5d37d93c5449";
const maxLocalMemeZipBytes = 119 * 1024 * 1024;

const el = (id) => document.getElementById(id);
const list = el("plugin-list");
const search = el("plugin-search");
const configSearch = el("config-search");
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
  "chat-config": "完整对话配置",
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
  node.querySelector("span").textContent = online ? "机器人在线 · 设置可立即应用" : "机器人未启动 · 设置会在下次启动时生效";
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

const pluginPageChannel = "astrbot-plugin-page";

function encodePluginEndpoint(value) {
  if (typeof value !== "string") throw new Error("高级配置页请求无效。");
  const parts = value.trim().replace(/^\/+|\/+$/g, "").split("/");
  if (!parts.length || parts.some((part) => !part || part === "." || part === ".." || !/^[A-Za-z0-9_.~-]+$/.test(part))) {
    throw new Error("高级配置页请求无效。");
  }
  return parts.map(encodeURIComponent).join("/");
}

function pluginRequestData(payload) {
  if (payload && payload.status === "error") {
    throw new Error(customerText(payload.message || "高级配置页请求失败。"));
  }
  if (payload && payload.status === "ok" && Object.prototype.hasOwnProperty.call(payload, "data")) {
    return payload.data;
  }
  return payload;
}

async function handlePluginPageRequest(message) {
  if (!state.currentId || !state.detail?.enabled) throw new Error("功能尚未加载，请重启机器人后再试。");
  const endpoint = encodePluginEndpoint(message.endpoint);
  const base = `/control/plugins/${encodeURIComponent(state.currentId)}/extension/${endpoint}`;
  let response;
  if (message.action === "api:get") {
    const url = new URL(base, window.location.origin);
    if (message.params && typeof message.params === "object" && !Array.isArray(message.params)) {
      Object.entries(message.params).forEach(([key, value]) => {
        if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
      });
    }
    response = await fetch(url);
  } else if (message.action === "api:post") {
    response = await fetch(base, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(message.body ?? {}),
    });
  } else if (message.action === "files:upload") {
    const buffer = message.fileBuffer;
    if (Object.prototype.toString.call(buffer) !== "[object ArrayBuffer]" && !ArrayBuffer.isView(buffer)) {
      throw new Error("请选择要上传的文件。");
    }
    if (buffer.byteLength > 12 * 1024 * 1024) {
      throw new Error("上传文件过大，请选择较小的文件。");
    }
    const fileName = String(message.fileName || "upload.bin");
    if (!fileName || fileName.length > 255 || /[\\/]/.test(fileName)) {
      throw new Error("上传文件名无效。");
    }
    const form = new FormData();
    form.append("file", new Blob([buffer], { type: message.fileType || "application/octet-stream" }), fileName);
    response = await fetch(base, { method: "POST", body: form });
  } else {
    throw new Error("当前高级配置页使用了尚未支持的操作。");
  }
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new Error("高级配置页返回了无法识别的数据。");
  }
  if (!response.ok) throw new Error(customerText(payload?.message || "高级配置页请求失败。"));
  return pluginRequestData(payload);
}

window.addEventListener("message", async (event) => {
  const frame = el("plugin-frame");
  const message = event.data;
  if (
    event.origin !== window.location.origin ||
    event.source !== frame.contentWindow ||
    !message ||
    message.channel !== pluginPageChannel ||
    message.kind !== "request" ||
    typeof message.requestId !== "string"
  ) return;
  try {
    const data = await handlePluginPageRequest(message);
    event.source.postMessage(
      { channel: pluginPageChannel, kind: "response", requestId: message.requestId, ok: true, data },
      event.origin,
    );
  } catch (error) {
    event.source.postMessage(
      {
        channel: pluginPageChannel,
        kind: "response",
        requestId: message.requestId,
        ok: false,
        error: customerText(error?.message || "高级配置页请求失败。"),
      },
      event.origin,
    );
  }
});

function setBusy(value, activeButton = null, busyText = "处理中…") {
  state.busy = value;
  [el("save-config"), el("reload-plugin"), el("open-full-config")].forEach((button) => {
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

function fieldRole(key, schema) {
  const special = String(schema._special || "").toLowerCase();
  const providerType = String(schema._provider_type || "").toLowerCase();
  const normalized = String(key || "").toLowerCase();
  if (special === "select_persona") return "persona";
  if (special === "select_embedding_provider" || providerType === "embedding" || normalized.includes("embedding_provider")) return "embedding";
  if (providerType === "rerank" || normalized.includes("rerank_provider")) return "rerank";
  if (special === "select_provider" || normalized.endsWith("provider_id")) return "chat";
  if (normalized.includes("persona") && (normalized.endsWith("_id") || normalized.endsWith("_name"))) return "persona";
  return "";
}

function choicesFor(key, schema, current) {
  let choices = null;
  if (schema.type !== "list" && Array.isArray(schema.options)) {
    choices = schema.options.map((value, index) => ({
      value,
      label: Array.isArray(schema.labels) && schema.labels[index] !== undefined ? schema.labels[index] : value,
    }));
  }
  const role = fieldRole(key, schema);
  if (role === "chat") choices = clone(state.detail.providers || []);
  if (role === "embedding") choices = clone(state.detail.embedding_providers || []);
  if (role === "rerank") choices = clone(state.detail.rerank_providers || []);
  if (role === "persona") choices = (state.detail.personas || []).map((value) => ({ value, label: value }));
  if (choices && current !== null && current !== undefined && current !== "" && !choices.some((item) => item.value === current)) {
    choices.unshift({ value: current, label: `当前值（模型列表中已不可用） · ${current}`, unavailable: true });
  }
  return { choices, role };
}

function blankChoiceLabel(role, schema = {}) {
  if (schema._blank_label) return customerText(schema._blank_label);
  if (role === "persona") return "跟随默认人格";
  if (["chat", "embedding", "rerank"].includes(role)) return "跟随机器人默认模型";
  return "请选择";
}

function fieldShell(key, schema, path, parentPath) {
  const row = document.createElement("div");
  row.className = "field";
  if (schema._ui_disclosure) row.classList.add("disclosure-controller");
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

function defaultValue(schema) {
  if (Object.prototype.hasOwnProperty.call(schema, "default")) return clone(schema.default);
  if (schema.type === "object") {
    const result = {};
    Object.entries(schema.items || {}).forEach(([key, field]) => { result[key] = defaultValue(field); });
    return result;
  }
  if (["list", "template_list"].includes(schema.type)) return [];
  if (schema.type === "bool") return false;
  if (["int", "float"].includes(schema.type)) return null;
  return "";
}

function renderListField(key, schema, path, parentPath) {
  const { row, label, control } = fieldShell(key, schema, path, parentPath);
  row.classList.add("list-field");
  const hint = control.querySelector(".field-hint");
  const values = Array.isArray(getAtPath(path)) ? clone(getAtPath(path)) : [];
  const options = Array.isArray(schema.options) ? schema.options : null;
  if (options) {
    const choices = document.createElement("div");
    choices.className = "multi-choice";
    options.forEach((value, index) => {
      const item = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = values.includes(value);
      input.disabled = schema._readonly === true;
      input.addEventListener("change", () => {
        const selected = options.filter((option) => choices.querySelector(`input[data-index="${options.indexOf(option)}"]`).checked);
        setAtPath(path, selected);
      });
      input.dataset.index = String(index);
      const text = document.createElement("span");
      text.textContent = customerText(Array.isArray(schema.labels) && schema.labels[index] !== undefined ? schema.labels[index] : value);
      item.append(input, text);
      choices.appendChild(item);
    });
    control.insertBefore(choices, hint);
  } else if (values.every((value) => ["string", "number"].includes(typeof value))) {
    const editor = document.createElement("div");
    editor.className = "simple-list-editor";
    const draw = () => {
      editor.replaceChildren();
      const currentValues = Array.isArray(getAtPath(path)) ? clone(getAtPath(path)) : [];
      currentValues.forEach((value, index) => {
        const item = document.createElement("div");
        item.className = "simple-list-row";
        const input = document.createElement("input");
        input.className = "text-input";
        input.value = value ?? "";
        input.disabled = schema._readonly === true;
        input.addEventListener("input", () => {
          const updated = Array.isArray(getAtPath(path)) ? clone(getAtPath(path)) : [];
          updated[index] = input.value;
          setAtPath(path, updated);
        });
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "icon-action danger-text";
        remove.textContent = "删除";
        remove.disabled = schema._readonly === true;
        remove.addEventListener("click", () => {
          const updated = Array.isArray(getAtPath(path)) ? clone(getAtPath(path)) : [];
          updated.splice(index, 1);
          setAtPath(path, updated);
          draw();
        });
        item.append(input, remove);
        editor.appendChild(item);
      });
      const add = document.createElement("button");
      add.type = "button";
      add.className = "inline-add";
      add.textContent = "+ 添加一项";
      add.disabled = schema._readonly === true;
      add.addEventListener("click", () => {
        const updated = Array.isArray(getAtPath(path)) ? clone(getAtPath(path)) : [];
        updated.push("");
        setAtPath(path, updated);
        draw();
        editor.querySelector(".simple-list-row:last-of-type input")?.focus();
      });
      editor.appendChild(add);
    };
    draw();
    control.insertBefore(editor, hint);
  } else {
    const input = document.createElement("textarea");
    input.className = "json-input";
    input.spellcheck = false;
    input.value = JSON.stringify(values, null, 2);
    input.dataset.json = "true";
    input.disabled = schema._readonly === true;
    input.addEventListener("input", () => setDirty(true));
    control.insertBefore(input, hint);
  }
  label.htmlFor = `field-${path.join("-")}`;
  return row;
}

function renderTemplateList(key, schema, path, parentPath) {
  const { row, control } = fieldShell(key, schema, path, parentPath);
  row.classList.add("template-list-field");
  const hint = control.querySelector(".field-hint");
  const templates = schema.templates && typeof schema.templates === "object" ? schema.templates : {};
  const toolbar = document.createElement("div");
  toolbar.className = "template-toolbar";
  const picker = document.createElement("select");
  picker.className = "select-input";
  Object.entries(templates).forEach(([templateKey, template]) => {
    const option = document.createElement("option");
    option.value = templateKey;
    option.textContent = customerText(template.name || templateKey);
    picker.appendChild(option);
  });
  const add = document.createElement("button");
  add.type = "button";
  add.className = "button secondary compact-button";
  add.textContent = "添加配置";
  add.disabled = schema._readonly === true || picker.options.length === 0;
  add.addEventListener("click", () => {
    const templateKey = picker.value;
    const template = templates[templateKey];
    if (!template || typeof template !== "object") return;
    const item = { __template_key: templateKey };
    Object.entries(template.items || {}).forEach(([itemKey, itemSchema]) => { item[itemKey] = defaultValue(itemSchema); });
    const updated = Array.isArray(getAtPath(path)) ? clone(getAtPath(path)) : [];
    updated.push(item);
    setAtPath(path, updated);
    renderConfigPanels();
  });
  toolbar.append(picker, add);
  control.insertBefore(toolbar, hint);

  const cards = document.createElement("div");
  cards.className = "template-cards";
  const values = Array.isArray(getAtPath(path)) ? getAtPath(path) : [];
  values.forEach((item, index) => {
    const templateKey = item && typeof item === "object" ? item.__template_key : "";
    const template = templates[templateKey];
    const card = document.createElement("section");
    card.className = "template-card";
    const header = document.createElement("div");
    header.className = "template-card-header";
    const title = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = customerText(template?.name || `自定义配置 ${index + 1}`);
    const badge = document.createElement("span");
    badge.textContent = `#${index + 1}`;
    title.append(strong, badge);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-action danger-text";
    remove.textContent = "删除";
    remove.disabled = schema._readonly === true;
    remove.addEventListener("click", () => {
      const updated = Array.isArray(getAtPath(path)) ? clone(getAtPath(path)) : [];
      updated.splice(index, 1);
      setAtPath(path, updated);
      renderConfigPanels();
    });
    header.append(title, remove);
    card.appendChild(header);
    const body = document.createElement("div");
    body.className = "template-card-body";
    if (template && template.items) {
      renderSchema(template.items, body, [...path, index]);
    } else {
      const message = document.createElement("p");
      message.className = "field-hint";
      message.textContent = "这是旧版本留下的自定义配置，会原样保留。";
      body.appendChild(message);
    }
    card.appendChild(body);
    cards.appendChild(card);
  });
  if (!values.length) {
    const empty = document.createElement("div");
    empty.className = "template-empty";
    empty.textContent = "还没有添加配置，请先选择类型。";
    cards.appendChild(empty);
  }
  control.insertBefore(cards, hint);
  return row;
}

function renderPrimitive(key, schema, path, parentPath) {
  if (schema.type === "template_list" && schema.templates) return renderTemplateList(key, schema, path, parentPath);
  if (schema.type === "list") return renderListField(key, schema, path, parentPath);
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
    const { choices, role } = choicesFor(key, schema, current);
    if (choices) {
      input = document.createElement("select");
      input.className = "select-input";
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = blankChoiceLabel(role, schema);
      input.appendChild(blank);
      choices.forEach(({ value, label: choiceLabel, unavailable }) => {
        const option = document.createElement("option");
        option.value = JSON.stringify(value);
        option.textContent = customerText(choiceLabel);
        if (unavailable) option.className = "unavailable-option";
        option.selected = current === value;
        input.appendChild(option);
      });
      if (current === null || current === undefined || current === "") blank.selected = true;
      const emptyValue = schema.default === null || current === null ? null : "";
      input.addEventListener("change", () => setAtPath(path, input.value ? JSON.parse(input.value) : emptyValue));
    } else if (isSecret(key, schema)) {
      input = document.createElement("input");
      input.className = "text-input";
      input.type = "password";
      input.value = current ?? "";
      input.addEventListener("input", () => setAtPath(path, input.value));
    } else if (schema.editor_mode === true) {
      input = document.createElement("textarea");
      input.className = "json-input";
      input.spellcheck = false;
      input.value = current ?? "";
      input.dataset.json = "false";
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
  let activeGroup = "";
  let activeFields = null;
  const groupTarget = (title, disclosure, path) => {
    if (activeFields && activeGroup === title) return activeFields;
    activeGroup = title;
    const section = document.createElement("section");
    section.className = `config-section${disclosure ? " optional-model-group" : ""}`;
    const heading = document.createElement(disclosure ? "div" : "h3");
    if (disclosure) {
      heading.className = "optional-group-heading";
      const copy = document.createElement("div");
      const titleNode = document.createElement("h3");
      titleNode.textContent = customerText(title);
      const note = document.createElement("p");
      note.textContent = "主模型不可用时才会尝试这一项。";
      copy.append(titleNode, note);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button secondary compact-button";
      const fields = document.createElement("div");
      fields.className = "section-fields";
      const update = () => {
        const enabled = Boolean(getAtPath(path));
        button.textContent = enabled ? "不使用备用模型" : customerText(disclosure);
        button.setAttribute("aria-expanded", String(enabled));
        fields.hidden = !enabled;
      };
      button.addEventListener("click", () => {
        const controller = fields.querySelector(".disclosure-controller input[type='checkbox']");
        if (!controller) return;
        controller.checked = !controller.checked;
        controller.dispatchEvent(new Event("change", { bubbles: true }));
        update();
      });
      heading.append(copy, button);
      section.appendChild(heading);
      activeFields = fields;
      section.appendChild(fields);
      container.appendChild(section);
      update();
      return activeFields;
    }
    heading.textContent = customerText(title);
    section.appendChild(heading);
    activeFields = document.createElement("div");
    activeFields.className = "section-fields";
    section.appendChild(activeFields);
    container.appendChild(section);
    return activeFields;
  };

  Object.entries(schema || {}).forEach(([key, field]) => {
    if (!field || typeof field !== "object" || field.invisible === true || field._invisible === true) return;
    const path = [...parentPath, key];
    const group = customerText(field._ui_group || "");
    const target = group ? groupTarget(group, field._ui_disclosure, path) : container;
    if (!group) {
      activeGroup = "";
      activeFields = null;
    }
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
      target.appendChild(section);
    } else {
      target.appendChild(renderPrimitive(key, field, path, parentPath));
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
    if (plugin.model_field_count) labels.push(`${plugin.model_field_count} 项模型`);
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
  summary.push(detail.enabled ? "统一加载" : "等待机器人加载");
  if (detail.configurable) summary.push(`${detail.field_count} 项基础配置`);
  if (detail.model_field_count) summary.push(`${detail.model_field_count} 项模型绑定`);
  if (detail.pages.length) summary.push(`${detail.pages.length} 个高级管理页`);
  el("plugin-summary").textContent = summary.join(" · ");
  el("reload-plugin").dataset.locked = (!detail.engine_online || !detail.enabled).toString();
  el("reload-plugin").disabled = !detail.engine_online || !detail.enabled;
  el("reload-plugin").title = !detail.engine_online ? "请先在运行中心启动机器人" : (!detail.enabled ? "请重启机器人完成加载" : "立即重新载入这项功能");
  setEngineState(detail.engine_online);
  el("save-config").textContent = detail.engine_online && detail.enabled ? "保存并立即应用" : "保存设置";
  const fullConfigButton = el("open-full-config");
  const fullConfigLocked = !detail.engine_online || !detail.enabled;
  fullConfigButton.dataset.locked = fullConfigLocked.toString();
  fullConfigButton.disabled = state.busy || fullConfigLocked;
  fullConfigButton.title = !detail.engine_online
    ? "请先在运行中心启动机器人"
    : (!detail.enabled ? "请重启机器人完成加载" : "打开这项功能的完整配置页");
}

function applyConfigSearch() {
  const query = configSearch.value.trim().toLocaleLowerCase("zh-CN");
  let visible = 0;
  document.querySelectorAll("#config-fields .field").forEach((row) => {
    const matched = !query || customerText(row.textContent).toLocaleLowerCase("zh-CN").includes(query);
    row.classList.toggle("search-hidden", !matched);
    if (matched) visible += 1;
  });
  [...document.querySelectorAll("#config-fields .config-section")].reverse().forEach((section) => {
    const hasMatch = [...section.querySelectorAll(":scope > .section-fields > .field, :scope > .section-fields > .config-section")]
      .some((child) => !child.classList.contains("search-hidden"));
    section.classList.toggle("search-hidden", Boolean(query) && !hasMatch);
  });
  el("config-search-result").textContent = query ? `找到 ${visible} 项` : "";
}

function renderConfigPanels() {
  const fields = el("config-fields");
  fields.replaceChildren();
  const basicSchema = state.detail.basic_schema || state.detail.schema || {};
  const modelSchema = state.detail.model_schema || {};
  const hasBasic = state.detail.configurable && Object.keys(basicSchema).length > 0;
  const configurationNote = customerText(state.detail.configuration_note || "");
  el("basic-config-intro").hidden = !hasBasic || !configurationNote;
  el("basic-tab").hidden = !hasBasic;
  el("basic-config-note").textContent = configurationNote;
  fields.hidden = !hasBasic;
  configSearch.closest(".config-toolbar").hidden = !hasBasic;
  el("no-config").hidden = hasBasic;
  if (hasBasic) renderSchema(basicSchema, fields);

  const modelFields = el("model-fields");
  modelFields.replaceChildren();
  const hasModels = Object.keys(modelSchema).length > 0;
  const hasRequiredModel = Object.values(modelSchema).some((field) =>
    customerText(field?._blank_label || "").includes("必需"),
  );
  el("model-config-note").textContent = hasRequiredModel
    ? "群聊分析模型是完整使用这项功能所必需的；图片理解模型可以按需留空。这里的选择不会改变日常回复模型。"
    : "这里只影响当前功能，不会改变机器人的日常对话模型。留空时跟随机器人默认设置。";
  el("models-tab").hidden = !hasModels;
  modelFields.hidden = !hasModels;
  el("no-model-config").hidden = hasModels;
  if (hasModels) renderSchema(modelSchema, modelFields);

  const hasConfig = hasBasic || hasModels;
  el("save-config").dataset.locked = (!hasConfig).toString();
  el("save-config").disabled = !hasConfig;
  el("full-config-callout").hidden = !(hasBasic && state.detail.pages?.length);
  refreshConditions();
  applyConfigSearch();
}

function renderDetail() {
  updateDetailHeader();
  setNotice("");
  configSearch.value = "";
  renderConfigPanels();
  renderPages();
  setDirty(false);
}

function renderPages() {
  const pages = state.detail.pages || [];
  el("pages-tab").hidden = pages.length === 0;
  el("pages-tab").textContent = state.detail.configurable ? "完整配置" : "专属配置";
  const picker = el("page-picker");
  picker.replaceChildren();
  const frame = el("plugin-frame");
  frame.hidden = true;
  frame.removeAttribute("src");
  el("page-placeholder").hidden = false;
  const isMemeManager = state.currentId === memeManagerFeatureId;
  el("meme-pack-import").hidden = !isMemeManager;
  if (isMemeManager) void refreshMemePackStorage();
  pages.forEach((pageName) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "page-button";
    button.dataset.pageName = pageName;
    button.textContent = pageLabels[pageName] || customerText(pageName.replace(/[-_]+/g, " "));
    button.addEventListener("click", () => openPluginPage(pageName, button));
    picker.appendChild(button);
  });
}

async function refreshMemePackStorage() {
  const featureId = state.currentId;
  try {
    const data = await api(`/control/plugins/${encodeURIComponent(featureId)}/pack-storage`);
    if (state.currentId === featureId) el("meme-pack-path").textContent = data.path;
  } catch (error) {
    if (state.currentId === featureId) el("meme-pack-path").textContent = `目录读取失败：${error.message}`;
  }
}

async function uploadLocalMemePack() {
  const status = el("meme-pack-status");
  if (state.currentId !== memeManagerFeatureId) return;
  if (!state.detail?.enabled) {
    status.textContent = "表情管理尚未加载，请重启机器人后再导入。";
    return;
  }
  const file = el("meme-pack-file").files?.[0];
  if (!file || !file.name.toLowerCase().endsWith(".zip")) {
    el("meme-pack-file").setAttribute("aria-invalid", "true");
    status.textContent = "请选择表情包 ZIP 文件。";
    el("meme-pack-file").focus();
    return;
  }
  if (file.size > maxLocalMemeZipBytes) {
    el("meme-pack-file").setAttribute("aria-invalid", "true");
    status.textContent = "ZIP 超过 119 MB，请选择较小的表情包。";
    el("meme-pack-file").focus();
    return;
  }
  el("meme-pack-file").removeAttribute("aria-invalid");
  const button = el("meme-pack-upload");
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("overwrite", String(el("meme-pack-overwrite").checked));
  form.append("set_as_default", String(el("meme-pack-default").checked));
  button.disabled = true;
  status.textContent = "正在导入，请等待；较大的表情包可能需要数分钟。";
  try {
    const response = await fetch(
      `/control/plugins/${encodeURIComponent(state.currentId)}/extension/packs/import`,
      { method: "POST", body: form },
    );
    const payload = await response.json();
    if (!response.ok) throw new Error(customerText(payload?.message || "导入失败。"));
    const data = pluginRequestData(payload);
    status.textContent = `导入成功：${customerText(data.pack_id || file.name)}。`;
    el("meme-pack-file").value = "";
    const activePage = el("page-picker").querySelector(".page-button.active");
    if (activePage) await openPluginPage(activePage.dataset.pageName, activePage);
  } catch (error) {
    status.textContent = `导入失败：${customerText(error?.message || "请重试。")}`;
  } finally {
    button.disabled = false;
  }
}

async function selectPlugin(moduleName, openFirstPage = false) {
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
    renderDetail();
    const shouldOpenFirstPage = Boolean(
      state.detail.pages?.length && (openFirstPage || !state.detail.configurable),
    );
    showTab(shouldOpenFirstPage ? "pages" : "basic");
    if (shouldOpenFirstPage) {
      const firstPageButton = el("page-picker").querySelector(".page-button");
      if (firstPageButton) await openPluginPage(state.detail.pages[0], firstPageButton);
    }
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
  state.activeTab = name;
  ["basic", "models", "pages"].forEach((tabName) => {
    const selected = name === tabName;
    el(`${tabName}-tab`).classList.toggle("active", selected);
    el(`${tabName}-tab`).setAttribute("aria-selected", selected.toString());
  });
  const pages = name === "pages";
  el("config-form").hidden = pages;
  el("basic-panel").hidden = name !== "basic";
  el("models-panel").hidden = name !== "models";
  el("pages-panel").hidden = !pages;
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
    setNotice("");
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function openFullConfigPage() {
  if (!state.detail?.pages?.length) return;
  if (!state.detail.engine_online || !state.detail.enabled) {
    setNotice(!state.detail.engine_online
      ? "完整配置页需要机器人在线，请先在运行中心启动机器人。"
      : "功能尚未加载，请重启机器人后再打开完整配置页。", "error");
    return;
  }
  const openPage = async () => {
    showTab("pages");
    const firstPageButton = el("page-picker").querySelector(".page-button");
    if (firstPageButton) await openPluginPage(state.detail.pages[0], firstPageButton);
  };
  if (state.dirty) {
    showDiscardModal(openPage);
    return;
  }
  await openPage();
}

async function loadCatalog() {
  loadingView.hidden = false;
  emptyView.hidden = true;
  detailView.hidden = true;
  try {
    const data = await api("/control/catalog");
    const plugins = data.plugins || [];
    const requested = requestedFeatureId ? plugins.find((item) => item.id === requestedFeatureId) : null;
    if (requestedFeatureId && !requested) {
      throw new Error("当前功能不在交付列表中，请返回功能中心重新打开专属配置页。");
    }
    state.catalog = requested ? [requested] : plugins;
    if (requested) {
      el("plugin-search").closest(".search-box").hidden = true;
      el("plugin-count").previousElementSibling.textContent = "当前功能";
      document.querySelector(".topbar p").textContent = "只管理当前功能的专属配置";
    }
    if (data.product_name) {
      el("product-title").textContent = `${customerText(data.product_name)} · 功能配置`;
      document.title = requested
        ? `${customerText(data.product_name)} · ${customerText(requested.display_name)}配置`
        : `${customerText(data.product_name)} · 功能配置`;
    }
    setEngineState(data.engine_online);
    renderCatalog();
    loadingView.hidden = true;
    if (state.catalog.length) {
      await selectPlugin(state.catalog[0].id, Boolean(requested));
    }
    else emptyView.hidden = false;
  } catch (error) {
    loadingView.hidden = true;
    emptyView.hidden = false;
    emptyView.querySelector("strong").textContent = "功能列表读取失败";
    emptyView.querySelector("span").textContent = error.message;
  }
}

async function refreshCurrentPluginState() {
  if (document.hidden || state.busy || !state.currentId || !state.detail) return;
  const selectedId = state.currentId;
  try {
    const detail = await api(`/control/plugins/${encodeURIComponent(selectedId)}`);
    if (state.currentId !== selectedId || !state.detail) return;
    state.detail.enabled = detail.enabled;
    state.detail.engine_online = detail.engine_online;
    const item = state.catalog.find((plugin) => plugin.id === selectedId);
    if (item) item.enabled = detail.enabled;
    renderCatalog();
    updateDetailHeader();
  } catch (error) {
    setNotice(error.message, "error");
  }
}

search.addEventListener("input", renderCatalog);
el("clear-search").addEventListener("click", () => { search.value = ""; renderCatalog(); search.focus(); });
configSearch.addEventListener("input", applyConfigSearch);
el("clear-config-search").addEventListener("click", () => { configSearch.value = ""; applyConfigSearch(); configSearch.focus(); });
el("config-form").addEventListener("submit", saveConfig);
el("reload-plugin").addEventListener("click", reloadPlugin);
el("open-full-config").addEventListener("click", openFullConfigPage);
el("basic-tab").addEventListener("click", () => showTab("basic"));
el("models-tab").addEventListener("click", () => showTab("models"));
el("pages-tab").addEventListener("click", () => showTab("pages"));
el("meme-pack-upload").addEventListener("click", () => { void uploadLocalMemePack(); });
el("meme-pack-file").addEventListener("change", () => {
  el("meme-pack-file").removeAttribute("aria-invalid");
  el("meme-pack-status").textContent = "";
});
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
window.addEventListener("focus", () => { refreshCurrentPluginState(); });

["clear-search", "clear-config-search", "basic-tab", "models-tab", "pages-tab", "modal-cancel", "modal-confirm"].forEach((id) => {
  el(id).disabled = false;
});

loadCatalog();
