/* Shared content entrance for internal tabs, after their state change succeeds. */
(() => {
  "use strict";
  const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)");
  const panels = new WeakMap();
  const active = new Set();

  function enter(panel) {
    if (!panel?.isConnected || typeof panel.animate !== "function") return;
    panels.get(panel)?.cancel();
    if (reducedMotion?.matches || !panel.getClientRects().length) return;
    const animation = panel.animate([
      {opacity: 0.45, transform: "translateY(8px)"},
      {opacity: 1, transform: "none"},
    ], {duration: 220, easing: "cubic-bezier(0.2, 0.7, 0.2, 1)"});
    animation.id = "manager-tab-enter";
    panels.set(panel, animation);
    active.add(animation);
    const release = () => {
      active.delete(animation);
      if (panels.get(panel) === animation) panels.delete(panel);
    };
    animation.addEventListener("finish", release, {once: true});
    animation.addEventListener("cancel", release, {once: true});
  }

  reducedMotion?.addEventListener?.("change", (event) => {
    if (event.matches) active.forEach((animation) => animation.cancel());
  });
  window.managerMotion = Object.freeze({enter});
})();
