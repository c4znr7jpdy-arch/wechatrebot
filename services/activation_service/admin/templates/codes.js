"use strict";

const issueForm = document.querySelector('input[name="action"][value="issue"]')?.form;
if (issueForm) {
  const mode = issueForm.elements.namedItem("mode");
  const plan = issueForm.elements.namedItem("plan");
  const license = issueForm.elements.namedItem("license_id");
  const device = issueForm.elements.namedItem("device");
  const updateFields = () => {
    const restore = mode.value === "recovery" || mode.value === "migration";
    if (restore) plan.value = "";
    else if (!plan.value) plan.value = "month";
    license.required = mode.value !== "new";
    device.required = mode.value === "migration";
  };
  mode.addEventListener("change", updateFields);
  updateFields();
}

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
