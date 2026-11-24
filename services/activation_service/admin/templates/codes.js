"use strict";

const issueForm = document.querySelector('input[name="action"][value="issue"]')?.form;
if (issueForm) {
  const plan = issueForm.elements.namedItem("plan");
  const credits = issueForm.elements.namedItem("rebind_credits");
  const updateFields = () => {
    const topup = issueForm.elements.namedItem("mode").value === "rebind_credit";
    document.getElementById("duration-fields").hidden = topup;
    document.getElementById("credit-fields").hidden = !topup;
    plan.disabled = topup;
    credits.disabled = !topup;
  };
  issueForm.querySelectorAll('input[name="mode"]').forEach(control => control.addEventListener("change", updateFields));
  issueForm.querySelectorAll("[data-credits]").forEach(button => button.addEventListener("click", () => {
    credits.value = button.dataset.credits;
  }));
  updateFields();
}

document.querySelectorAll('form[method="post"]').forEach(form => form.addEventListener("submit", () => {
  form.querySelectorAll('button[type="submit"]').forEach(button => { button.disabled = true; });
}));

const codeInput = document.getElementById("activation-code");
const copyButton = document.getElementById("copy-code");
const copyStatus = document.getElementById("copy-status");

if (codeInput && copyButton && copyStatus) {
  codeInput.addEventListener("click", () => codeInput.select());
  copyButton.addEventListener("click", async () => {
    codeInput.select();
    try {
      await navigator.clipboard.writeText(codeInput.value);
      copyStatus.textContent = "已复制";
    } catch {
      copyStatus.textContent = "已选中激活码，请按 Ctrl+C 或长按复制";
    }
  });
}
