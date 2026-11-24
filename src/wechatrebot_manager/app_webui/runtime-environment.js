/* A single environment action; individual dependencies stay internal. */
window.createRuntimeEnvironment = function (helpers) {
  "use strict";
  const {$, api, formatBytes, refreshStatus, reportError} = helpers;
  let pending = false;
  const panel = $("#runtimeEnvironment");
  const button = $("#installRuntimeEnvironment");
  function renderRuntimeEnvironment(status) {
    panel.hidden = !status || status.state === "legacy" || status.ready;
    if (panel.hidden) return;
    const busy = pending || ["checking", "installing"].includes(status.state);
    $("#runtimeEnvironmentTitle").textContent = status.state === "failed" ? "运行环境安装未完成" : "准备运行环境";
    $("#runtimeEnvironmentMessage").textContent = status.message || "首次使用前，请安装运行环境。";
    $("#runtimeEnvironmentSize").textContent = status.download_size ? `预计下载 ${formatBytes(status.download_size)}，已有环境会自动复用。` : "包含运行所需的依赖和音视频工具，后续只补齐变化的部分。";
    const progress = $("#runtimeEnvironmentProgress");
    progress.hidden = status.state !== "installing";
    progress.value = Math.min(100, Math.max(0, Number(status.percent) || 0));
    button.disabled = busy;
    button.textContent = busy ? status.state === "checking" ? "正在检查…" : "正在安装…" : status.state === "failed" ? "重试安装" : "安装运行环境";
  }
  button.addEventListener("click", async () => {
    if (pending) return;
    pending = true;
    button.disabled = true;
    try {
      const result = await api("install_runtime_environment");
      if (!result?.accepted) throw new Error(result?.message || "暂时无法安装，请稍后重试。");
    } catch (error) { reportError(error); }
    finally { pending = false; await refreshStatus(); }
  });
  return {renderRuntimeEnvironment};
};
