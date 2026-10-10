/* Updates controller; the shell supplies shared services explicitly. */
window.createUpdateNotifications = function (helpers) {
  "use strict";
  const {
    $,
    api,
    confirmAction,
    formatBytes,
    renderStatus,
    reportError,
    state,
    toast,
  } = helpers;

  async function beginUpdateInstall(release) {
    const accepted = await api("install_update", release.version);
    if (!accepted?.accepted) throw new Error("更新任务已经在处理中。 ");
    $("#modalProgress").hidden = false;
    while (true) {
      const progress = await api("get_update_status");
      const percent = Number.isFinite(Number(progress.percent)) ? Number(progress.percent) : 0;
      const bar = $("#modalProgressBar");
      const indeterminate = ["stopping_services", "waiting_wechat", "launching"].includes(progress.phase);
      bar.classList.toggle("is-indeterminate", indeterminate);
      bar.style.width = indeterminate ? "36%" : `${Math.min(100, Math.max(0, percent))}%`;
      const percentPhases = ["download", "extract", "verify", "prepare", "install"];
      const phaseSuffix = percentPhases.includes(progress.phase) ? ` · ${percent}%` : "";
      $("#modalMessage").textContent = `${progress.message || "正在准备更新"}${phaseSuffix}。请保持管理器打开。`;
      if (progress.state === "failed") {
        const error = new Error(progress.error?.message || progress.message || "更新未能完成。 ");
        error.product = progress.error || null;
        throw error;
      }
      if (progress.state === "restarting") return;
      await new Promise((resolve) => window.setTimeout(resolve, 400));
    }
  }

  function showUpdatePrompt(release) {
    const mandatory = Boolean(release.mandatory);
    confirmAction({
      title: mandatory ? `必须更新到 v${release.version}` : `发现新版本 v${release.version}`,
      message: `${release.title || "新版本已就绪"}${release.notes?.length ? `：${release.notes.join("；")}` : ""}。更新包约 ${formatBytes(release.package_size)}。更新期间会等待机器人微信安全退出，再停止后台服务；失败时会自动恢复原版本。`,
      confirmText: mandatory ? "立即更新" : "下载并安装",
      cancelText: mandatory ? "稍后更新" : "稍后提醒",
      intent: "primary",
      pendingText: "正在下载…",
      pendingMessage: `正在下载并校验 v${release.version}；校验通过后会安全停止机器人并安装。请保持管理器打开。`,
      onConfirm: () => beginUpdateInstall(release),
    });
  }

  async function refreshUpdate(manual = true) {
    if (state.updateChecking) return;
    state.updateChecking = true;
    const button = $("#checkUpdate");
    button.disabled = true;
    button.textContent = "检查中…";
    try {
      const data = await api("check_update");
      if (data.busy) return;
      state.availableUpdate = data.release || null;
      state.mandatoryUpdate = Boolean(data.release?.mandatory);
      if (state.status) renderStatus(state.status);
      if (!data.enabled) {
        $("#updateStatus").textContent = "在线更新未启用";
      } else if (!data.release) {
        $("#updateStatus").textContent = "已是最新版本";
        state.mandatoryPromptVersion = "";
        if (manual) toast("当前已经是最新版本。 ");
      } else {
        const release = data.release;
        api("download_update", release.version).catch((error) => {
          if (manual) reportError(error);
        });
        $("#updateStatus").textContent = release.mandatory
          ? `必须更新至 v${release.version}`
          : `发现 v${release.version}`;
        if (release.mandatory && state.mandatoryPromptVersion !== release.version) {
          state.mandatoryPromptVersion = release.version;
          showUpdatePrompt(release);
        } else if (manual) {
          showUpdatePrompt(release);
        }
      }
    } catch (error) {
      const mandatoryUnavailable = error?.product?.code === "UPD-1016";
      if (mandatoryUnavailable) {
        state.availableUpdate = null;
        state.mandatoryUpdate = true;
        if (state.status) renderStatus(state.status);
      }
      $("#updateStatus").textContent = mandatoryUnavailable
        ? "当前版本已停止支持，暂未获取到可安装更新"
        : "检查失败";
      if (manual) reportError(error);
    } finally {
      state.updateChecking = false;
      button.disabled = !(state.bootstrap?.update?.enabled);
      button.textContent = state.availableUpdate ? "查看更新" : "检查更新";
    }
  }

  function automaticUpdateIntervalMs() {
    const configured = Number(state.bootstrap?.update?.check_interval_seconds);
    const seconds = Number.isFinite(configured) && configured >= 300 ? configured : 600;
    return seconds * 1000;
  }

  function scheduleAutomaticUpdateCheck(delayMs) {
    window.clearTimeout(state.updateCheckTimer);
    state.updateCheckTimer = window.setTimeout(async () => {
      await refreshUpdate(false);
      scheduleAutomaticUpdateCheck(automaticUpdateIntervalMs());
    }, delayMs);
  }

  async function pollUpdateStatus() {
    if (!state.bootstrap?.update?.enabled) return;
    try {
      const status = await api("get_update_status");
      const label = $("#updateStatus");
      const button = $("#checkUpdate");
      if (status.state === "running" && state.availableUpdate) {
        label.textContent = `${state.mandatoryUpdate ? "必须更新" : "后台下载"} v${state.availableUpdate.version} · ${Math.max(0, Number(status.percent) || 0)}%`;
        button.textContent = "查看更新";
      } else if (status.state === "ready" && state.availableUpdate) {
        label.textContent = `v${state.availableUpdate.version} 已下载，等待安装`;
        button.textContent = state.mandatoryUpdate ? "立即更新" : "安装更新";
      } else if (status.state === "failed" && status.error) {
        label.textContent = `更新准备失败 · ${status.error.code || "UPD-1002"}`;
        button.textContent = "重试更新";
      } else if (status.state === "restarting") {
        label.textContent = "正在重新启动管理器";
      }
    } catch (_error) {
      // Update status is optional background work; it must not disturb normal use.
    }
  }

  return {
    pollUpdateStatus,
    refreshUpdate,
    scheduleAutomaticUpdateCheck,
  };
};
