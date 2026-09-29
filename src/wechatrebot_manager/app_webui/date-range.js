"use strict";

window.initializeHistoryDateRange = function (onApply) {
  const start = document.getElementById("historyStart");
  const end = document.getElementById("historyEnd");
  const trigger = document.getElementById("historyDateRange");
  const dialog = document.getElementById("historyDateDialog");
  const calendar = document.getElementById("historyCalendar");
  const format = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  const today = new Date();
  const week = new Date(today); week.setDate(week.getDate() - 7);
  start.value = format(week); end.value = format(today);
  let draftStart = start.value, draftEnd = end.value, selecting = "start";
  let month = new Date(today.getFullYear(), today.getMonth(), 1);
  const label = () => { trigger.textContent = `${start.value.replaceAll("-", "/")} — ${end.value.replaceAll("-", "/")}`; };
  function render() {
    document.getElementById("rangeStart").value = draftStart.replaceAll("-", "/");
    document.getElementById("rangeEnd").value = draftEnd.replaceAll("-", "/");
    document.getElementById("rangeStart").classList.toggle("active", selecting === "start");
    document.getElementById("rangeEnd").classList.toggle("active", selecting === "end");
    calendar.innerHTML = [0, 1].map((offset) => {
      const current = new Date(month.getFullYear(), month.getMonth() + offset, 1);
      const lead = (current.getDay() + 6) % 7;
      const days = new Date(current.getFullYear(), current.getMonth() + 1, 0).getDate();
      return `<section><h3>${current.getFullYear()} 年 ${current.getMonth() + 1} 月</h3><div class="range-days">${["一", "二", "三", "四", "五", "六", "日"].map(day => `<span>${day}</span>`).join("")}${"<span></span>".repeat(lead)}${Array.from({length: days}, (_, i) => {
        const value = format(new Date(current.getFullYear(), current.getMonth(), i + 1));
        const selected = value === draftStart || value === draftEnd;
        return `<button type="button" data-date="${value}" aria-label="${value}" aria-pressed="${selected}" class="${selected ? "selected" : value > draftStart && value < draftEnd ? "in-range" : ""}">${i + 1}</button>`;
      }).join("")}</div></section>`;
    }).join("");
  }
  trigger.addEventListener("click", () => {
    draftStart = start.value; draftEnd = end.value; selecting = "start";
    month = new Date(Number(draftStart.slice(0, 4)), Number(draftStart.slice(5, 7)) - 1, 1);
    render(); dialog.showModal(); document.getElementById("rangeStart").focus();
  });
  for (const field of ["start", "end"]) {
    document.getElementById(field === "start" ? "rangeStart" : "rangeEnd").addEventListener("click", () => { selecting = field; render(); });
  }
  calendar.addEventListener("click", (event) => {
    const value = event.target.closest("[data-date]")?.dataset.date;
    if (!value) return;
    if (selecting === "start") {
      draftStart = value;
      if (draftEnd < value) draftEnd = value;
      selecting = "end";
    } else {
      draftEnd = value;
      if (draftStart > value) draftStart = value;
    }
    render();
  });
  for (const [id, offset] of [["rangePrev", -1], ["rangeNext", 1]]) {
    document.getElementById(id).addEventListener("click", () => { month.setMonth(month.getMonth() + offset); render(); });
  }
  document.getElementById("rangeWeek").addEventListener("click", () => {
    const now = new Date(), from = new Date(now); from.setDate(from.getDate() - 7);
    draftStart = format(from); draftEnd = format(now); month = new Date(from.getFullYear(), from.getMonth(), 1); render();
  });
  document.getElementById("rangeCancel").addEventListener("click", () => dialog.close());
  document.getElementById("rangeApply").addEventListener("click", () => {
    start.value = draftStart; end.value = draftEnd; label(); dialog.close(); onApply();
  });
  label();
};
