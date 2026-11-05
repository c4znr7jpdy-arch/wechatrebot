"use strict";

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
