const bridge = window.AstrBotPluginPage;
const state = { config: null, editingId: null, deleteArmed: null };

const grid = document.getElementById("model-grid");
const modal = document.getElementById("modal-backdrop");
const form = document.getElementById("model-form");
const toast = document.getElementById("toast");
const sourceType = document.getElementById("source-type");

function unwrap(response) {
  if (response && response.status === "error") {
    throw new Error(response.message || "请求失败");
  }
  if (response && response.status === "ok" && Object.prototype.hasOwnProperty.call(response, "data")) {
    return response.data;
  }
  return response;
}

async function apiGet(path) {
  if (!bridge) throw new Error("AstrBot Plugin Page Bridge 不可用");
  return unwrap(await bridge.apiGet(`page/${path}`, {}));
}

async function apiPost(path, body = {}) {
  if (!bridge) throw new Error("AstrBot Plugin Page Bridge 不可用");
  return unwrap(await bridge.apiPost(`page/${path}`, body));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message, error = false) {
  toast.textContent = message;
  toast.classList.toggle("error", error);
  toast.classList.remove("hidden");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 4200);
}

function protocolLabel(protocol) {
  return {
    openai_images: "OpenAI Images",
    openai_chat: "OpenAI Chat",
    minimax: "MiniMax",
  }[protocol] || protocol;
}

function sourceLabel(profile) {
  const source = profile.api_key_source || {};
  if (source.type === "environment") return `环境变量 · ${source.env_name || "未设置"}`;
  if (source.type === "astrbot_provider") return `Provider · ${source.provider_id || "未设置"}`;
  return "插件本地安全配置";
}

function render() {
  const config = state.config || { models: [], active_model: "" };
  const active = config.models.find((item) => item.id === config.active_model);
  document.getElementById("active-summary").textContent = active ? `${active.name} · ${active.model}` : "未配置";
  document.getElementById("count-summary").textContent = `${config.models.filter((item) => item.enabled).length} / ${config.models.length}`;

  if (!config.models.length) {
    grid.innerHTML = '<div class="empty-state">还没有生成模型，点击右上角添加。</div>';
    return;
  }

  grid.innerHTML = config.models.map((profile) => {
    const isActive = profile.id === config.active_model;
    const keyBadge = profile.has_api_key
      ? `<span class="badge">${escapeHtml(profile.api_key_mask || "Key 已配置")}</span>`
      : '<span class="badge warn">缺少 API Key</span>';
    return `
      <article class="model-card ${isActive ? "active" : ""} ${profile.enabled ? "" : "disabled"}">
        <div class="model-title-row">
          <div>
            <h3>${escapeHtml(profile.name)}</h3>
            <div class="model-id">${escapeHtml(profile.id)}</div>
          </div>
          <div class="badges">
            ${isActive ? '<span class="badge active">当前默认</span>' : ""}
            ${profile.enabled ? "" : '<span class="badge warn">已停用</span>'}
            ${keyBadge}
          </div>
        </div>
        <dl class="model-meta">
          <dt>模型</dt><dd>${escapeHtml(profile.model)}</dd>
          <dt>协议</dt><dd>${escapeHtml(protocolLabel(profile.protocol))}</dd>
          <dt>地址</dt><dd>${escapeHtml(profile.base_url)}</dd>
          <dt>密钥来源</dt><dd>${escapeHtml(sourceLabel(profile))}</dd>
          <dt>图生图</dt><dd>${profile.supports_edit ? "支持" : "关闭"}</dd>
          <dt>自动回退</dt><dd>${profile.fallback_priority > 0 ? `优先级 ${profile.fallback_priority}` : "不参与"}</dd>
        </dl>
        <div class="model-actions">
          ${!isActive && profile.enabled ? `<button class="button small primary" data-action="active" data-id="${escapeHtml(profile.id)}">设为默认</button>` : ""}
          <button class="button small secondary" data-action="test" data-id="${escapeHtml(profile.id)}">检查连接</button>
          <button class="button small secondary" data-action="edit" data-id="${escapeHtml(profile.id)}">编辑</button>
          <button class="button small danger" data-action="delete" data-id="${escapeHtml(profile.id)}">${state.deleteArmed === profile.id ? "再次点击确认" : "删除"}</button>
        </div>
      </article>
    `;
  }).join("");
}

function defaults() {
  return {
    id: "",
    name: "",
    enabled: true,
    protocol: "openai_images",
    base_url: "https://api.hostcentral.cc/",
    model: "gpt-image-2",
    generation_path: "images/generations",
    edit_path: "images/edits",
    models_path: "models",
    supports_edit: true,
    size_mode: "ratio_size",
    response_format: "",
    timeout_seconds: 300,
    trust_env: false,
    auth_type: "bearer",
    api_key_header: "X-API-Key",
    api_key_source: { type: "stored" },
    fallback_priority: 0,
    extra_body: {},
    extra_headers: {},
  };
}

function setField(name, value) {
  const field = form.elements.namedItem(name);
  if (!field) return;
  if (field.type === "checkbox") field.checked = Boolean(value);
  else field.value = value ?? "";
}

function openModal(profile = null) {
  const item = profile || defaults();
  state.editingId = profile?.id || null;
  document.getElementById("modal-title").textContent = profile ? "编辑生成模型" : "添加生成模型";
  [
    "id", "name", "protocol", "base_url", "model", "generation_path", "edit_path",
    "models_path", "size_mode", "response_format", "timeout_seconds",
    "fallback_priority", "auth_type", "api_key_header",
  ].forEach((name) => setField(name, item[name]));
  ["enabled", "supports_edit", "trust_env"].forEach((name) => setField(name, item[name]));
  setField("source_type", item.api_key_source?.type || "stored");
  setField("env_name", item.api_key_source?.env_name || "");
  setField("provider_id", item.api_key_source?.provider_id || "");
  setField("api_key", "");
  setField("extra_body", JSON.stringify(item.extra_body || {}, null, 2));
  setField("extra_headers", JSON.stringify(item.extra_headers || {}, null, 2));
  form.elements.namedItem("id").readOnly = Boolean(profile);
  updateSourceFields();
  modal.classList.remove("hidden");
}

function closeModal() {
  modal.classList.add("hidden");
  state.editingId = null;
}

function updateSourceFields() {
  document.querySelectorAll("[data-source]").forEach((element) => {
    element.classList.toggle("hidden", element.dataset.source !== sourceType.value);
  });
}

function parseJsonField(name) {
  const value = form.elements.namedItem(name).value.trim();
  if (!value) return {};
  const parsed = JSON.parse(value);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error(`${name === "extra_body" ? "额外请求参数" : "额外请求头"}必须是 JSON 对象`);
  }
  return parsed;
}

function collectForm() {
  const source = { type: sourceType.value };
  if (source.type === "environment") source.env_name = form.elements.namedItem("env_name").value.trim();
  if (source.type === "astrbot_provider") source.provider_id = form.elements.namedItem("provider_id").value.trim();
  const value = (name) => form.elements.namedItem(name).value.trim();
  return {
    id: value("id"),
    name: value("name"),
    enabled: form.elements.namedItem("enabled").checked,
    protocol: value("protocol"),
    base_url: value("base_url"),
    model: value("model"),
    generation_path: value("generation_path"),
    edit_path: value("edit_path"),
    models_path: value("models_path"),
    supports_edit: form.elements.namedItem("supports_edit").checked,
    size_mode: value("size_mode"),
    response_format: value("response_format"),
    timeout_seconds: Number(value("timeout_seconds")),
    trust_env: form.elements.namedItem("trust_env").checked,
    auth_type: value("auth_type"),
    api_key_header: value("api_key_header"),
    api_key_source: source,
    api_key: value("api_key"),
    fallback_priority: Number(value("fallback_priority")),
    extra_body: parseJsonField("extra_body"),
    extra_headers: parseJsonField("extra_headers"),
  };
}

async function loadConfig() {
  state.config = await apiGet("config");
  state.deleteArmed = null;
  render();
}

grid.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const { action, id } = button.dataset;
  const profile = state.config.models.find((item) => item.id === id);
  try {
    if (action === "edit") {
      openModal(profile);
      return;
    }
    button.disabled = true;
    if (action === "active") {
      state.config = await apiPost("models/active", { id });
      showToast(`默认模型已切换为 ${profile.name}`);
    } else if (action === "test") {
      const result = await apiPost("models/test", { id });
      const found = result.model_found === false ? "，但模型列表中未找到该模型名" : "";
      showToast(`连接成功：HTTP ${result.status_code}，${result.elapsed_ms} ms${found}`);
    } else if (action === "delete") {
      if (state.deleteArmed !== id) {
        state.deleteArmed = id;
        render();
        window.setTimeout(() => {
          if (state.deleteArmed === id) {
            state.deleteArmed = null;
            render();
          }
        }, 5000);
        return;
      }
      state.config = await apiPost("models/delete", { id });
      state.deleteArmed = null;
      showToast(`已删除 ${profile.name}`);
    }
    render();
  } catch (error) {
    showToast(error.message || String(error), true);
    render();
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const saveButton = document.getElementById("save-button");
  try {
    saveButton.disabled = true;
    state.config = await apiPost("models/save", { model: collectForm() });
    closeModal();
    render();
    showToast("模型已保存并热更新");
  } catch (error) {
    showToast(error.message || String(error), true);
  } finally {
    saveButton.disabled = false;
  }
});

document.getElementById("add-button").addEventListener("click", () => openModal());
document.getElementById("close-modal").addEventListener("click", closeModal);
document.getElementById("cancel-button").addEventListener("click", closeModal);
sourceType.addEventListener("change", updateSourceFields);

document.getElementById("reload-button").addEventListener("click", async (event) => {
  try {
    event.currentTarget.disabled = true;
    state.config = await apiPost("reload", {});
    render();
    showToast("已从磁盘热重载配置");
  } catch (error) {
    showToast(error.message || String(error), true);
  } finally {
    event.currentTarget.disabled = false;
  }
});

modal.addEventListener("click", (event) => {
  if (event.target === modal) closeModal();
});

try {
  await bridge.ready();
  await loadConfig();
} catch (error) {
  grid.innerHTML = `<div class="empty-state">${escapeHtml(error.message || String(error))}</div>`;
  showToast(error.message || String(error), true);
}
