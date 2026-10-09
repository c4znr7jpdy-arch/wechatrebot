const bridge = window.AstrBotPluginPage;
const state = { config: null, editingId: null, deleteArmed: null };

const grid = document.getElementById("task-grid");
const modal = document.getElementById("modal-backdrop");
const form = document.getElementById("task-form");
const toast = document.getElementById("toast");

function unwrap(response) {
  if (response?.status === "error") throw new Error(response.message || "请求失败");
  if (response?.status === "ok" && Object.hasOwn(response, "data")) return response.data;
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

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.add("hidden"), 4200);
}

function mediaLabel(value) {
  return state.config?.media_options.find((item) => item.value === value)?.label || value;
}

function scheduleLabel(task) {
  const time = `${String(task.hour).padStart(2, "0")}:${String(task.minute).padStart(2, "0")}`;
  if (!task.days?.length) return `每天 ${time}`;
  const labels = task.days.map((day) => state.config.day_options.find((item) => item.value === day)?.label || day);
  return `${labels.join("、")} ${time}`;
}

function render() {
  const config = state.config || { tasks: [], media_options: [], day_options: [] };
  document.getElementById("enabled-count").textContent = config.tasks.filter((task) => task.enabled).length;
  document.getElementById("task-count").textContent = config.tasks.length;
  document.getElementById("timezone").textContent = config.timezone || "Asia/Shanghai";

  if (!config.tasks.length) {
    grid.innerHTML = `
      <div class="empty-state">
        <span>◷</span>
        <strong>还没有推荐任务</strong>
        <p>点击“新建任务”，选择群聊、分类和发送时间。</p>
      </div>`;
    return;
  }

  grid.innerHTML = config.tasks.map((task) => `
    <article class="task-card ${task.enabled ? "" : "disabled"}">
      <div class="task-topline">
        <span class="media-badge">${escapeHtml(mediaLabel(task.media_type))}</span>
        <span class="status ${task.enabled ? "online" : ""}">${task.enabled ? "运行中" : "已停用"}</span>
      </div>
      <h3>${escapeHtml(task.name)}</h3>
      <div class="schedule-time"><b>${String(task.hour).padStart(2, "0")}:${String(task.minute).padStart(2, "0")}</b><span>${escapeHtml(scheduleLabel(task))}</span></div>
      <dl>
        <dt>目标群聊</dt><dd title="${escapeHtml(task.session)}">${escapeHtml(task.session)}</dd>
        <dt>推荐数量</dt><dd>${task.limit} 部</dd>
      </dl>
      <div class="task-actions">
        <button class="button small primary" data-action="test" data-id="${task.id}">立即测试</button>
        <button class="button small secondary" data-action="edit" data-id="${task.id}">编辑</button>
        <button class="button small secondary" data-action="toggle" data-id="${task.id}">${task.enabled ? "停用" : "启用"}</button>
        <button class="button small danger" data-action="delete" data-id="${task.id}">${state.deleteArmed === task.id ? "再次确认" : "删除"}</button>
      </div>
    </article>
  `).join("");
}

function setupOptions() {
  document.getElementById("media-type").innerHTML = state.config.media_options
    .map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`)
    .join("");
  document.getElementById("day-grid").innerHTML = state.config.day_options
    .map((item) => `<label><input type="checkbox" name="days" value="${item.value}" /><span>${escapeHtml(item.label)}</span></label>`)
    .join("");
}

function setValue(name, value) {
  const field = form.elements.namedItem(name);
  if (!field) return;
  if (field.type === "checkbox") field.checked = Boolean(value);
  else field.value = value ?? "";
}

function openModal(task = null) {
  state.editingId = task?.id || null;
  document.getElementById("modal-title").textContent = task ? "编辑推荐任务" : "新建推荐任务";
  setValue("name", task?.name || "晚间短剧推荐");
  setValue("session", task?.session || "");
  setValue("media_type", task?.media_type || "短剧");
  setValue("time", `${String(task?.hour ?? 20).padStart(2, "0")}:${String(task?.minute ?? 0).padStart(2, "0")}`);
  setValue("limit", String(task?.limit || 12));
  setValue("enabled", task?.enabled ?? true);
  const selectedDays = new Set(task?.days || []);
  form.querySelectorAll('input[name="days"]').forEach((field) => {
    field.checked = selectedDays.has(field.value);
  });
  modal.classList.remove("hidden");
}

function closeModal() {
  modal.classList.add("hidden");
  state.editingId = null;
}

function collectTask() {
  const [hour, minute] = form.elements.namedItem("time").value.split(":").map(Number);
  return {
    id: state.editingId || "",
    name: form.elements.namedItem("name").value.trim(),
    session: form.elements.namedItem("session").value.trim(),
    media_type: form.elements.namedItem("media_type").value,
    hour,
    minute,
    limit: Number(form.elements.namedItem("limit").value),
    days: [...form.querySelectorAll('input[name="days"]:checked')].map((field) => field.value),
    enabled: form.elements.namedItem("enabled").checked,
  };
}

async function loadConfig() {
  state.config = await apiGet("schedules");
  setupOptions();
  render();
}

grid.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const { action, id } = button.dataset;
  const task = state.config.tasks.find((item) => item.id === id);
  if (!task) return;
  if (action === "edit") {
    openModal(task);
    return;
  }
  try {
    button.disabled = true;
    if (action === "test") {
      const result = await apiPost("schedules/test", { id });
      showToast(`发送成功：${result.media_type} ${result.count} 部`);
    } else if (action === "toggle") {
      state.config = await apiPost("schedules/toggle", { id, enabled: !task.enabled });
      showToast(task.enabled ? "任务已停用" : "任务已启用");
    } else if (action === "delete") {
      if (state.deleteArmed !== id) {
        state.deleteArmed = id;
        render();
        setTimeout(() => {
          if (state.deleteArmed === id) {
            state.deleteArmed = null;
            render();
          }
        }, 12000);
        return;
      }
      state.config = await apiPost("schedules/delete", { id });
      state.deleteArmed = null;
      showToast("任务已删除");
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
    state.config = await apiPost("schedules/save", { task: collectTask() });
    closeModal();
    render();
    showToast("任务已保存并应用");
  } catch (error) {
    showToast(error.message || String(error), true);
  } finally {
    saveButton.disabled = false;
  }
});

document.getElementById("add-button").addEventListener("click", () => openModal());
document.getElementById("close-modal").addEventListener("click", closeModal);
document.getElementById("cancel-button").addEventListener("click", closeModal);
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
