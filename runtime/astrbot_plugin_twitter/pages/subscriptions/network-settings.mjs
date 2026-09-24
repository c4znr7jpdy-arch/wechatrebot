// 一个页面只有一个查询循环；概览刷新不取消网络状态查询。
export function createStatusWatcher(read, render, { schedule = setTimeout, cancel = clearTimeout } = {}) {
  let active = false, timer = null, generation = 0;
  async function poll(current) {
    try {
      const status = await read();
      if (!active || current !== generation) return;
      render(status);
    } catch (error) {
      if (!active || current !== generation) return;
      render({ error: error?.message || "读取数据源状态失败" });
    }
    if (active && current === generation) timer = schedule(() => poll(current), 3000);
  }
  return {
    start() {
      if (active) return;
      active = true;
      poll(++generation);
    },
    stop() { active = false; ++generation; cancel(timer); },
    refresh() { this.stop(); this.start(); },
  };
}

export async function initNetworkSettings(bridge, onSettings) {
  const form = document.getElementById("network-form");
  const status = document.getElementById("network-status");
  const route = document.getElementById("network-route");
  const errorBox = document.getElementById("network-error");
  if (!bridge) return;
  let pending = false, dirty = false, loaded = false;
  const controls = [...form.elements];
  const lock = (value) => {
    pending = value;
    form.setAttribute("aria-busy", String(value));
    controls.forEach((control) => {
      control.disabled = value || (!loaded && control.id !== "network-reload");
    });
  };
  const showError = (message, field) => {
    errorBox.textContent = message;
    errorBox.hidden = !message;
    for (const control of controls) {
      control.removeAttribute("aria-invalid");
      control.removeAttribute("aria-describedby");
    }
    if (field) {
      field.setAttribute("aria-invalid", "true");
      field.setAttribute("aria-describedby", "network-error");
      field.focus();
    }
  };
  const render = (data) => {
    if (data.error) { status.textContent = data.error; status.dataset.state = "error"; return; }
    const p = data.provider;
    const name = p.name === "fxtwitter" ? "FxTwitter" : "Nitter";
    status.dataset.state = p.checking ? "checking" : p.ready ? "ready" : "error";
    status.textContent = p.checking ? `正在检查 ${name}（最长 20 秒）`
      : p.ready ? `${name} 已连接` : p.error || `${name} 不可用`;
    route.textContent = `本次检查使用：${p.route || "未读取"}`;
    const runtime = document.getElementById("runtime-status");
    runtime.classList.toggle("is-ready", p.ready);
    runtime.classList.toggle("is-error", !p.ready && !p.checking);
    runtime.lastElementChild.textContent = `${name} · ${p.checking ? "检查中" : p.ready ? "已连接" : "不可用"}`;
  };
  const watch = createStatusWatcher(() => bridge.apiGet("settings/network/status"), render);
  const read = async () => {
    if (pending) return;
    lock(true);
    watch.stop();
    try {
      const data = await bridge.apiGet("settings/network");
      for (const [key, value] of Object.entries(data.settings)) {
        const input = form.elements.namedItem(key);
        if (!input) continue;
        if (input.type === "checkbox") input.checked = value;
        else input.value = value;
      }
      loaded = true;
      dirty = false;
      showError("");
      onSettings(data.settings);
      render(data);
    } catch (error) {
      showError(error?.message || "读取设置失败，请重新读取");
    } finally {
      lock(false);
      watch.start();
    }
  };
  form.addEventListener("input", () => { dirty = true; });
  const reveal = document.getElementById("proxy-reveal");
  reveal.addEventListener("click", () => {
    const shown = form.elements.proxy.type === "password";
    form.elements.proxy.type = shown ? "text" : "password";
    reveal.textContent = shown ? "隐藏" : "显示";
    reveal.setAttribute("aria-label", shown ? "隐藏代理地址" : "显示代理地址");
    reveal.setAttribute("aria-pressed", String(shown));
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (pending || !loaded) return;
    const payload = Object.fromEntries(new FormData(form));
    payload.pre_download_media = form.elements.pre_download_media.checked;
    for (const key of ["fxtwitter_api_base", "nitter_url", "proxy"]) {
      const value = String(payload[key] || "").trim();
      if (!value && key !== "fxtwitter_api_base") continue;
      try {
        const url = new URL(value);
        const protocols = key === "proxy" ? ["http:", "https:", "socks5:"] : ["http:", "https:"];
        if (!protocols.includes(url.protocol) || !url.hostname || url.search || url.hash || /\s/.test(value)) throw new Error();
      } catch {
        showError(key === "proxy" ? "请填写有效的代理地址，例如 http://127.0.0.1:7892；留空为直连。" : "请填写有效的 HTTP(S) 数据源地址。", form.elements[key]);
        return;
      }
    }
    showError("");
    lock(true);
    watch.stop();
    status.textContent = "正在保存并应用设置…";
    try {
      const result = await bridge.apiPost("settings/network/save", payload);
      dirty = false;
      onSettings(payload);
      render(result);
    } catch (error) {
      showError(error?.message || "未能确认保存结果，请重新读取后重试");
    } finally {
      lock(false);
      watch.start();
    }
  });
  document.getElementById("network-reload").addEventListener("click", read);
  window.addEventListener("beforeunload", (event) => {
    if (!dirty && !pending) return;
    event.preventDefault();
    event.returnValue = "";
  });
  window.addEventListener("pagehide", () => watch.stop());
  window.addEventListener("pageshow", () => watch.start());
  await read();
}
