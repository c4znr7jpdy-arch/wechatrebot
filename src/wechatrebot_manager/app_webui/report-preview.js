"use strict";

// Reuse the manager's native dialog pattern for Escape and focus restoration.
window.openReportPreview = (() => {
  let dialog, image, viewport, status, zoom, fit = true, scale = 1;
  function layout() {
    if (!dialog.open || !image.naturalWidth) return;
    const width = fit ? Math.min(image.naturalWidth, viewport.clientWidth - 32) : image.naturalWidth * scale;
    image.style.width = `${Math.max(1, width)}px`;
    zoom.textContent = `${Math.round(width / image.naturalWidth * 100)}%`;
  }
  function initialize() {
    dialog = document.createElement("dialog");
    dialog.className = "report-preview-dialog";
    dialog.setAttribute("aria-labelledby", "reportPreviewTitle");
    dialog.innerHTML = `<header class="report-preview-header"><div><h2 id="reportPreviewTitle">报告样式预览</h2><p>示例数据 · 向下滚动查看完整报告</p></div><button type="button" class="button secondary" data-action="close" autofocus>关闭</button></header>
      <div class="report-preview-tools" aria-label="图片显示控制"><button type="button" class="button ghost small" data-action="out" aria-label="缩小预览">−</button><output aria-live="polite">100%</output><button type="button" class="button ghost small" data-action="in" aria-label="放大预览">＋</button><button type="button" class="button secondary small" data-action="fit">适应宽度</button><button type="button" class="button secondary small" data-action="maximize" aria-pressed="false">铺满窗口</button></div>
      <p class="report-preview-status" role="status"></p><div class="report-preview-viewport" tabindex="0" aria-label="完整报告，可滚动查看"><img alt="完整报告样式预览（示例数据）" hidden></div>`;
    document.body.append(dialog);
    image = dialog.querySelector("img");
    viewport = dialog.querySelector(".report-preview-viewport");
    status = dialog.querySelector(".report-preview-status");
    zoom = dialog.querySelector("output");
    image.addEventListener("load", () => {
      image.hidden = false;
      status.hidden = true;
      layout();
      viewport.scrollTo(0, 0);
    });
    image.addEventListener("error", () => {
      image.hidden = true;
      status.hidden = false;
      status.textContent = "预览图片加载失败，请关闭后重新打开。";
    });
    dialog.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const targets = [...dialog.querySelectorAll('button, [tabindex="0"]')];
      const first = targets[0], last = targets[targets.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    });
    dialog.addEventListener("click", (event) => {
      const action = event.target.closest("button")?.dataset.action;
      if (action === "close") dialog.close();
      if (action === "fit") fit = true;
      if (action === "in" || action === "out") {
        scale = Math.max(.2, Math.min(3, image.getBoundingClientRect().width / (image.naturalWidth || 1) * (action === "in" ? 1.25 : .8)));
        fit = false;
      }
      if (action === "maximize") {
        const full = dialog.classList.toggle("is-maximized");
        event.target.textContent = full ? "还原窗口" : "铺满窗口";
        event.target.setAttribute("aria-pressed", String(full));
      }
      if (event.target === dialog) {
        const rect = dialog.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
      }
      layout();
    });
    dialog.addEventListener("close", () => {
      image.removeAttribute("src");
      image.hidden = true;
    });
    new ResizeObserver(layout).observe(viewport);
  }
  return (src, name) => {
    if (!/^\/assets\/report-preview-[\w-]+\.webp$/.test(src)) return;
    if (!dialog) initialize();
    fit = true;
    dialog.classList.remove("is-maximized");
    const maximize = dialog.querySelector('[data-action="maximize"]');
    maximize.textContent = "铺满窗口";
    maximize.setAttribute("aria-pressed", "false");
    dialog.querySelector("h2").textContent = `${name || "报告样式"} · 完整预览`;
    status.textContent = "正在加载完整预览…";
    status.hidden = false;
    image.hidden = true;
    zoom.textContent = "—";
    dialog.showModal();
    image.src = src;
  };
})();
