(() => {
  "use strict";
  const STORAGE_KEY = "wechatrebot-panel-layout";
  const HISTORY_KEY = "page-history/history-layout";
  const SPLITS = [
    {selector: ".feature-layout", flexible: 1, minimums: [220, 360]},
    {selector: ".group-layout", flexible: 0, minimums: [250, 400]},
    {selector: ".contact-layout", flexible: 0, minimums: [250, 400]},
    {selector: ".history-layout", flexible: 1, minimums: [220, 360, 200]},
    {selector: ".core-layout", flexible: 1, minimums: [190, 360]},
  ];
  let initialized = false;

  window.initializePanelLayout = () => {
    if (initialized) return;
    initialized = true;
    const root = document.querySelector(".content-scroll");
    const resetButton = document.querySelector("#resetPanelLayout");
    const desktop = window.matchMedia("(min-width: 1101px)");
    const registered = new Map();
    let saved = {};
    let drag = null;
    let scanFrame = 0;
    try {
      const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}");
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) saved = parsed;
    } catch (_error) { /* Layout preferences are optional in WebView2. */ }
    // The model grid follows its container; old manual sizes must not constrain it.
    if (Object.hasOwn(saved, "page-settings/providerOverview")) {
      delete saved["page-settings/providerOverview"];
      persist();
    }

    function persist() {
      try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(saved)); }
      catch (_error) { /* Keep the current layout when storage is unavailable. */ }
      resetButton.hidden = !Object.keys(saved).length;
    }

    function splitFor(panel) {
      const definition = SPLITS.find((split) => panel.parentElement?.matches(split.selector));
      if (!definition || !desktop.matches) return null;
      const parent = panel.parentElement;
      const columns = [...parent.children].filter((child) => !child.matches(".panel-resize-handle"));
      const visible = columns.flatMap((child, index) => getComputedStyle(child).display === "none" ? [] : [index]);
      const index = columns.indexOf(panel);
      const track = index === definition.flexible ? visible.filter((column) => column !== index).at(-1) : index;
      return {...definition, parent, columns, visible, index, track};
    }

    function bounds(panel) {
      const split = splitFor(panel);
      if (!split) return {minWidth: Math.min(260, panel.parentElement.clientWidth), maxWidth: panel.parentElement.clientWidth};
      const gap = Number.parseFloat(getComputedStyle(split.parent).columnGap) || 0;
      const others = split.visible.reduce((total, index) => {
        if (index === split.index) return total;
        const minimum = split.minimums[index];
        return total + (index === split.flexible || index === split.track ? minimum : Math.max(minimum, split.columns[index].getBoundingClientRect().width));
      }, 0);
      return {minWidth: split.minimums[split.index], maxWidth: Math.max(split.minimums[split.index], split.parent.clientWidth - others - gap * (split.visible.length - 1))};
    }

    function applySize(panel, value, axis = "both") {
      if (!desktop.matches || !panel.getBoundingClientRect().width || !Array.isArray(value) || value.length !== 2 || !value.every(Number.isFinite)) return;
      const limit = bounds(panel);
      const width = Math.round(Math.max(limit.minWidth, Math.min(value[0], limit.maxWidth)));
      const height = Math.round(Math.max(160, Math.min(value[1], 6000)));
      const split = splitFor(panel);
      const history = split?.parent.matches(".history-layout");
      if (history && axis === "height") {
        setHistoryHeight(split.parent, height + split.parent.offsetHeight - split.parent.clientHeight);
        return;
      }
      if (split) {
        const {parent, index, flexible, minimums, visible, columns, track} = split;
        const gap = Number.parseFloat(getComputedStyle(parent).columnGap) || 0;
        // Freeze the other tracks for this gesture; remember() restores fluid shares.
        if (history) {
          const widths = columns.map((column) => column.getBoundingClientRect().width);
          for (const column of visible) parent.style.setProperty(`--layout-track-${column}`, `${widths[column]}px`);
        }
        let trackWidth = width;
        if (index === flexible) {
          const otherWidth = visible.reduce((total, column) => total + (column === index || column === track ? 0 : columns[column].getBoundingClientRect().width), 0);
          trackWidth = Math.max(minimums[track], parent.clientWidth - width - otherWidth - gap * (visible.length - 1));
        }
        // Leave one fluid column so resizing never creates a horizontal scroll trap.
        if (minimums.length === 2) parent.style.removeProperty(`--layout-track-${1 - track}`);
        if (history) parent.style.setProperty(`--layout-track-${flexible}`, `minmax(${minimums[flexible]}px, 1fr)`);
        parent.style.setProperty(`--layout-track-${track}`, `${Math.round(trackWidth)}px`);
        panel.style.removeProperty("--panel-user-width");
      } else {
        panel.style.setProperty("--panel-user-width", `${width}px`);
      }
      if (history) {
        if (axis !== "width") setHistoryHeight(split.parent, height + split.parent.offsetHeight - split.parent.clientHeight);
        return;
      }
      panel.style.setProperty("--panel-user-height", `${height}px`);
      panel.classList.add("has-panel-size");
    }

    function setHistoryHeight(parent, height) {
      parent.style.setProperty("--panel-user-height", `${Math.round(height)}px`);
      if (!parent.classList.contains("has-panel-size")) parent.classList.add("has-panel-size");
    }

    function historyAvailableHeight(parent) {
      const style = getComputedStyle(root);
      const page = parent.closest(".page");
      const heading = parent.getBoundingClientRect().top - page.getBoundingClientRect().top;
      return Math.max(540, root.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom) - heading);
    }

    function historySize(split) {
      const value = saved[HISTORY_KEY];
      if (value && Array.isArray(value.widths) && value.widths.length === 3 && value.widths.every((width) => Number.isFinite(width) && width > 0)) {
        const total = value.widths.reduce((sum, width) => sum + width, 0);
        return {widths: value.widths.map((width) => width / total * 100), heightRatio: Number.isFinite(value.heightRatio) && value.heightRatio > 0 ? value.heightRatio : null};
      }
      const style = getComputedStyle(split.parent);
      return {widths: split.minimums.map((_, index) => parseFloat(style.getPropertyValue(`--history-track-${index}`))), heightRatio: null};
    }

    function captureHistoryWidths(split, value) {
      const total = split.visible.reduce((sum, index) => sum + split.columns[index].getBoundingClientRect().width, 0);
      const share = split.visible.reduce((sum, index) => sum + value.widths[index], 0);
      for (const index of split.visible) value.widths[index] = split.columns[index].getBoundingClientRect().width / total * share;
    }

    function applyHistoryWidths(split, value) {
      for (const [index, width] of value.widths.entries()) {
        split.parent.style.setProperty(`--layout-track-${index}`, `minmax(${split.minimums[index]}px, ${width}fr)`);
      }
    }

    function restoreHistory() {
      const panel = root.querySelector("#conversationList");
      if (!panel || !panel.getBoundingClientRect().width) return;
      const split = splitFor(panel);
      if (!split) return;
      // Hidden columns keep their old preference until they can be measured.
      const legacy = [...registered].filter(([key, other]) => other.parentElement === split.parent && other.getBoundingClientRect().width && Array.isArray(saved[key]));
      const value = historySize(split);
      applyHistoryWidths(split, value);
      if (legacy.length) {
        // Old entries also locked height after width-only edits. Keep widths and release that lock.
        for (const [key, other] of legacy) applySize(other, saved[key], "width");
        captureHistoryWidths(split, value);
        saved[HISTORY_KEY] = value;
        for (const [key] of legacy) delete saved[key];
        persist();
        applyHistoryWidths(split, value);
      }
      if (value.heightRatio !== null) setHistoryHeight(split.parent, Math.min(6000, Math.max(160, historyAvailableHeight(split.parent) * value.heightRatio)));
    }

    function clearSize(panel) {
      panel.style.removeProperty("--panel-user-width");
      panel.style.removeProperty("--panel-user-height");
      panel.classList.remove("has-panel-size");
      const split = SPLITS.find((item) => panel.parentElement?.matches(item.selector));
      if (split) {
        split.minimums.forEach((_, index) => panel.parentElement.style.removeProperty(`--layout-track-${index}`));
        if (panel.parentElement.matches(".history-layout")) {
          panel.parentElement.style.removeProperty("--panel-user-height");
          panel.parentElement.classList.remove("has-panel-size");
        }
      }
    }

    function remember(panel, key, axis) {
      const split = splitFor(panel);
      if (split?.parent.matches(".history-layout")) {
        const value = historySize(split);
        if (axis !== "height") captureHistoryWidths(split, value);
        if (axis !== "width") value.heightRatio = split.parent.getBoundingClientRect().height / historyAvailableHeight(split.parent);
        saved[HISTORY_KEY] = value;
        restoreHistory();
        persist();
        return;
      }
      const rect = panel.getBoundingClientRect();
      saved[key] = [Math.round(rect.width), Math.round(rect.height)];
      persist();
    }

    function finishDrag() {
      const current = drag;
      drag = null;
      document.documentElement.classList.remove("panel-resizing");
      document.documentElement.style.removeProperty("--panel-resize-cursor");
      if (!current) return;
      if (current.handle.hasPointerCapture(current.pointerId)) current.handle.releasePointerCapture(current.pointerId);
      if (current.panel.isConnected && current.panel.getBoundingClientRect().width) remember(current.panel, current.key, current.axis);
    }

    function bindHandle(panel, key, axis = "both") {
      if (panel.querySelector(`:scope > [data-resize-axis="${axis}"]`)) return;
      const handle = document.createElement("button");
      handle.type = "button";
      handle.className = axis === "both" ? "panel-resize-handle" : "panel-resize-edge";
      handle.dataset.resizeAxis = axis;
      if (axis !== "both") handle.tabIndex = -1;
      handle.title = "拖动调整大小；双击恢复";
      handle.setAttribute("aria-label", "调整面板大小，方向键调整，双击或按 Home 恢复");
      handle.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || !desktop.matches) return;
        event.preventDefault();
        finishDrag();
        const rect = panel.getBoundingClientRect();
        drag = {panel, key, handle, axis, pointerId: event.pointerId, x: event.clientX, y: event.clientY, width: rect.width, height: rect.height};
        handle.setPointerCapture(event.pointerId);
        document.documentElement.style.setProperty("--panel-resize-cursor", axis === "width" ? "ew-resize" : axis === "height" ? "ns-resize" : "nwse-resize");
        document.documentElement.classList.add("panel-resizing");
      });
      handle.addEventListener("lostpointercapture", () => { if (drag?.handle === handle) finishDrag(); });
      const reset = () => {
        clearSize(panel);
        delete saved[key];
        if (panel.parentElement.matches(".history-layout")) delete saved[HISTORY_KEY];
        // Split widths are shared, so restore the neighbouring panels together.
        for (const [otherKey, other] of registered) if (splitFor(panel) && other.parentElement === panel.parentElement && other !== panel) {
          delete saved[otherKey];
          clearSize(other);
        }
        persist();
      };
      handle.addEventListener("dblclick", reset);
      handle.addEventListener("keydown", (event) => {
        if (event.key === "Home") { event.preventDefault(); reset(); return; }
        if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
        event.preventDefault();
        const rect = panel.getBoundingClientRect();
        const step = event.shiftKey ? 80 : 20;
        const dimension = ["ArrowLeft", "ArrowRight"].includes(event.key) ? "width" : "height";
        applySize(panel, [rect.width + (event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0), rect.height + (event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0)], dimension);
        remember(panel, key, dimension);
      });
      panel.append(handle);
    }

    window.addEventListener("pointermove", (event) => {
      if (!drag) return;
      if (!drag.handle.isConnected || !drag.panel.getClientRects().length || (event.pointerType === "mouse" && event.buttons === 0)) { finishDrag(); return; }
      if (event.pointerId !== drag.pointerId) return;
      applySize(drag.panel, [drag.width + (drag.axis === "height" ? 0 : event.clientX - drag.x), drag.height + (drag.axis === "width" ? 0 : event.clientY - drag.y)], drag.axis);
    });
    for (const type of ["pointerup", "pointercancel"]) window.addEventListener(type, (event) => {
      if (drag?.pointerId === event.pointerId) finishDrag();
    }, true);
    window.addEventListener("blur", finishDrag);
    document.addEventListener("visibilitychange", () => { if (document.hidden) finishDrag(); });

    function scan() {
      scanFrame = 0;
      if (drag && (!drag.handle.isConnected || !drag.panel.getClientRects().length)) finishDrag();
      for (const [key, panel] of registered) if (!panel.isConnected) registered.delete(key);
      for (const page of root.querySelectorAll(".page")) {
        const panels = page.querySelectorAll(".panel, .conversation-list, .message-pane, .conversation-inspector");
        for (const [index, panel] of [...panels].entries()) {
          if (panel.matches(".history-toolbar, .history-layout, .core-heading, .core-subtabs, .loading-row, .provider-service, #providerOverview")) continue;
          const key = `${page.id}/${panel.id || `${[...panel.classList].filter((name) => !["layout-adjustable", "has-panel-size", "empty"].includes(name)).join(".")}/${index}`}`;
          if (registered.get(key) !== panel) {
            registered.set(key, panel);
            panel.classList.add("layout-adjustable");
            panel.dataset.panelLayoutKey = key;
            if (!panel.parentElement.matches(".history-layout")) applySize(panel, saved[key]);
          }
          if (!panel.parentElement.matches(".history-layout") && !panel.classList.contains("has-panel-size")) applySize(panel, saved[key]);
          bindHandle(panel, key);
          bindHandle(panel, key, "width");
          bindHandle(panel, key, "height");
        }
      }
      if (!drag) restoreHistory();
      resetButton.hidden = !Object.keys(saved).length;
    }

    function scheduleScan() {
      if (!scanFrame) scanFrame = window.requestAnimationFrame(scan);
    }
    new MutationObserver(scheduleScan).observe(root, {childList: true, subtree: true, attributes: true, attributeFilter: ["class", "hidden"]});
    const reflow = () => {
      finishDrag();
      for (const panel of registered.values()) clearSize(panel);
      if (desktop.matches) for (const [key, panel] of registered) {
        if (panel.isConnected && panel.getBoundingClientRect().width && !panel.parentElement.matches(".history-layout")) applySize(panel, saved[key]);
      }
      restoreHistory();
    };
    new ResizeObserver(reflow).observe(root);
    desktop.addEventListener("change", reflow);
    resetButton.addEventListener("click", () => {
      saved = {};
      for (const panel of registered.values()) clearSize(panel);
      persist();
    });
    scan();
  };
})();
