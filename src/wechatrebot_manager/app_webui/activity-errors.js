/* Installed before page controllers so render failures survive navigation. */
(() => {
  "use strict";
  const queue = [];
  const seen = new Map();
  let flushing = false;

  function notifyFailure() {
    let note = document.getElementById("activityCaptureFailure");
    if (!document.body) return;
    if (!note) {
      note = document.createElement("div");
      note.id = "activityCaptureFailure";
      note.className = "toast error";
      note.setAttribute("role", "alert");
      document.body.append(note);
    }
    note.textContent = "页面错误记录尚未保存，请截图联系技术支持。";
  }

  async function flush() {
    const write = window.pywebview?.api?.record_ui_error;
    if (!write || flushing) return;
    flushing = true;
    try {
      while (queue.length) {
        let timer;
        let result;
        try {
          result = await Promise.race([
            write(queue[0]),
            new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("记录等待超时")), 5000); }),
          ]);
        } finally { clearTimeout(timer); }
        if (!result?.ok) { notifyFailure(); break; }
        queue.shift();
      }
      if (!queue.length) document.getElementById("activityCaptureFailure")?.remove();
    } catch (_error) { notifyFailure(); }
    finally { flushing = false; }
  }

  function capture(error, operation = "页面运行") {
    const now = Date.now();
    for (const [key, time] of seen) if (now - time >= 60000) seen.delete(key);
    const message = String(error?.message || error || "未提供异常内容").slice(0, 4000);
    const stack = String(error?.stack || "未提供堆栈").slice(0, 12000);
    const signature = `${message}\n${stack}`;
    if (seen.has(signature) || queue.length >= 20) return;
    seen.set(signature, now);
    queue.push({message, stack, operation, page: document.querySelector(".page.active")?.id || "页面初始化"});
    void flush();
  }

  window.managerErrorReporter = {capture, flush};
  window.addEventListener("error", (event) => capture(event.error || new Error(event.message || "页面资源加载失败")));
  window.addEventListener("unhandledrejection", (event) => capture(event.reason, "异步页面操作"));
  window.addEventListener("pywebviewready", () => { void flush(); });
})();
