/* Shared tab entrance and grid reflow motion, after their state changes succeed. */
(() => {
  "use strict";
  const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)");
  const panels = new WeakMap();
  const active = new Set();

  function play(panel, frames, id) {
    if (!panel?.isConnected || typeof panel.animate !== "function") return;
    panels.get(panel)?.cancel();
    if (reducedMotion?.matches || !panel.getClientRects().length) return;
    const animation = panel.animate(frames, {duration: 220, easing: "cubic-bezier(0.2, 0.7, 0.2, 1)"});
    animation.id = id;
    panels.set(panel, animation);
    active.add(animation);
    const release = () => {
      active.delete(animation);
      if (panels.get(panel) === animation) panels.delete(panel);
    };
    animation.addEventListener("finish", release, {once: true});
    animation.addEventListener("cancel", release, {once: true});
  }

  function enter(panel) {
    play(panel, [
      {opacity: 0.45, transform: "translateY(8px)"},
      {opacity: 1, transform: "none"},
    ], "manager-tab-enter");
  }

  function trackGrid(container) {
    if (!container || typeof ResizeObserver !== "function") return;
    let positions = new Map();
    const visible = () => container.isConnected && container.getClientRects().length > 0;
    const cancel = () => positions.forEach(({node}) => panels.get(node)?.cancel());

    function measure() {
      if (!visible()) return new Map();
      return new Map([...container.querySelectorAll("[data-motion-key]")].map((node) => [
        node.dataset.motionKey, {node, x: node.offsetLeft, y: node.offsetTop},
      ]));
    }

    function capture() {
      if (!visible()) return new Map();
      return new Map([...positions].map(([key, position]) => {
        // Continue an interrupted movement from its current visual position.
        // Layout offsets exclude page entrance transforms and scrolling.
        const shift = panels.has(position.node)
          ? getComputedStyle(position.node).translate.split(" ").map(Number.parseFloat) : [];
        return [key, {x: position.x + (shift[0] || 0), y: position.y + (shift[1] || 0)}];
      }));
    }

    function refresh(before) {
      const after = measure();
      const changed = positions.size !== after.size || [...after].some(([key, next]) => {
        const previous = positions.get(key);
        return !previous || previous.node !== next.node || previous.x !== next.x || previous.y !== next.y;
      });
      if (!changed) return;
      before ??= capture();
      cancel();
      positions = after;
      if (reducedMotion?.matches) return;
      after.forEach(({node, x, y}, key) => {
        const previous = before.get(key);
        if (!previous) return; // First paint and newly inserted cards stay in place.
        const dx = previous.x - x, dy = previous.y - y;
        if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
        // Individual translate leaves each skin's hover transform intact.
        play(node, [{translate: `${dx}px ${dy}px`}, {translate: "0px 0px"}], "manager-grid-move");
      });
    }

    const resize = new ResizeObserver(() => refresh());
    resize.observe(container);
    const visibility = new MutationObserver(() => {
      if (!visible()) {
        cancel();
        positions.clear();
      } else refresh();
    });
    for (let node = container.parentElement; node; node = node.parentElement) {
      visibility.observe(node, {attributes: true, attributeFilter: ["class", "hidden"]});
    }
    return {capture, refresh};
  }

  reducedMotion?.addEventListener?.("change", (event) => {
    if (event.matches) active.forEach((animation) => animation.cancel());
  });
  window.managerMotion = Object.freeze({enter, trackGrid});
})();
