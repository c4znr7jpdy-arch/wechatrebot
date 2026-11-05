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

  let promptedReleaseId = "";
  let promptedReadyToken = "";
  let pendingPrompt = null;
  let promptTimer = 0;
  let latestStatus = null;
  let statusRequest = null;
  let downloadStarting = false;
  let statusRevision = 0;

  function isReadyForRelease(release, progress) {
    return progress?.state === "ready" && Boolean(progress.confirmation_token)
      && Boolean(release?.release_id) && progress.release_id === release.release_id;
  }

  function matchesRelease(release, progress) {
    return Boolean(release?.release_id) && progress?.release_id === release.release_id;
  }

  function renderUpdateCard(release, progress) {
    const card = $("#updateCard");
    const matching = matchesRelease(release, progress);
    card.hidden = !matching || !["running", "ready", "failed", "restarting"].includes(progress.state);
    document.body.classList.toggle("has-update-card", !card.hidden);
    if (card.hidden) return;
    const ready = isReadyForRelease(release, progress);
    const failed = progress.state === "failed" || (progress.state === "ready" && !ready);
    const percent = Math.min(100, Math.max(0, Number(progress.percent) || 0));
    const indeterminate = ["starting", "stopping_services", "waiting_wechat", "launching"].includes(progress.phase);
    card.dataset.state = failed ? "failed" : progress.state;
    $("#updateCardTitle").textContent = failed ? "更新未完成" : ready ? "更新已准备就绪" : `正在更新 v${release.version}`;
    $("#updateCardMessage").textContent = failed
      ? progress.error?.message || "更新包未完成校验，请重新下载。"
      : ready ? `v${release.version} 已下载并校验，可稍后安装。`
        : progress.message || "正在后台准备更新";
    $("#updateCardPercent").textContent = failed ? "" : `${percent}%`;
    const bar = $("#updateCardProgress");
    bar.classList.toggle("is-indeterminate", indeterminate && !failed);
    bar.setAttribute("aria-valuenow", String(percent));
    bar.setAttribute("aria-valuetext", failed ? "更新未完成" : `${progress.message || "更新进度"} ${percent}%`);
    $("#updateCardProgressBar").style.width = `${indeterminate ? 36 : percent}%`;
    const action = $("#updateCardAction");
    action.hidden = !ready && !failed;
    action.disabled = downloadStarting;
    action.textContent = ready ? "立即安装" : "重试下载";
    $("#updateStatus").textContent = failed ? `更新准备失败 · ${progress.error?.code || "UPD-1003"}`
      : ready ? `v${release.version} 已下载，等待安装`
        : progress.state === "restarting" ? "正在重新启动管理器" : `${progress.message || "后台下载"} · ${percent}%`;
    $("#checkUpdate").textContent = failed ? "重试更新" : ready ? "安装更新" : "查看更新";
  }

  async function waitForUpdate(release, targetState) {
    $("#modalProgress").hidden = false;
    while (true) {
      const progress = await api("get_update_status");
      if (progress.release_id !== release.release_id) {
        throw new Error("更新包已变更，请取消并重新检查更新。 ");
      }
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
      if (progress.state === targetState) return progress;
      if (progress.state === "idle") throw new Error("更新任务已停止，请重试。 ");
      await new Promise((resolve) => window.setTimeout(resolve, 400));
    }
  }

  function queueUpdatePrompt(release, ready = null) {
    pendingPrompt = {release, ready};
    window.clearTimeout(promptTimer);
    const showWhenFree = () => {
      const prompt = pendingPrompt;
      if (!prompt) return;
      if (state.availableUpdate?.release_id !== prompt.release.release_id) {
        pendingPrompt = null;
        return;
      }
      if (prompt.ready && (!isReadyForRelease(prompt.release, latestStatus)
          || latestStatus.confirmation_token !== prompt.ready.confirmation_token)) {
        pendingPrompt = null;
        return;
      }
      if (!prompt.ready && matchesRelease(prompt.release, latestStatus)
          && ["running", "ready", "restarting"].includes(latestStatus.state)) {
        pendingPrompt = null;
        return;
      }
      // A confirmAction callback closes its own dialog after it returns.
      // Wait for that close, and never replace another active confirmation.
      if (!$("#modalBackdrop").hidden || !$("#onboardingBackdrop").hidden || document.querySelector("dialog[open]")) {
        promptTimer = window.setTimeout(showWhenFree, 200);
        return;
      }
      pendingPrompt = null;
      promptedReleaseId = prompt.release.release_id;
      if (prompt.ready) showInstallConfirmation(prompt.release, prompt.ready);
      else showUpdatePrompt(prompt.release);
    };
    promptTimer = window.setTimeout(showWhenFree, 0);
  }

  async function beginUpdateDownload(release) {
    if (downloadStarting) return;
    downloadStarting = true;
    try {
      const accepted = await api("download_update", release.version, release.release_id);
      if (!accepted?.accepted) {
        const progress = await pollUpdateStatus();
        if (!accepted?.busy || !matchesRelease(release, progress)
            || !["running", "ready", "restarting"].includes(progress.state)) {
          throw new Error("另一个更新任务正在处理中，请稍后重试。 ");
        }
        return;
      }
      statusRevision += 1;
      latestStatus = {state: "running", phase: "starting", percent: 0,
        release_id: release.release_id, message: "正在后台准备更新"};
      renderUpdateCard(release, latestStatus);
      // Only the API acceptance is awaited. Downloading never keeps the dialog open.
      void pollUpdateStatus();
    } finally {
      downloadStarting = false;
    }
  }

  function showInstallConfirmation(release, ready) {
    promptedReadyToken = ready.confirmation_token;
    confirmAction({
      title: `安装 v${release.version}？`,
      message: "更新包已下载并校验完成。更新会强制关闭机器人程序和所有微信窗口，是否确认？正在处理的消息和微信中未发送的内容可能丢失。安装完成后会自动打开新版管理器。",
      confirmText: "确认关闭并安装",
      cancelText: "稍后安装",
      intent: "danger",
      pendingText: "正在安装…",
      pendingMessage: "正在关闭机器人程序和微信，准备安装更新。请保持管理器打开。",
      onConfirm: async () => {
        const accepted = await api("install_update", release.version, ready.confirmation_token);
        if (!accepted?.accepted) throw new Error("更新任务已经在处理中。 ");
        await waitForUpdate(release, "restarting");
      },
    });
  }

  function showUpdatePrompt(release) {
    const mandatory = Boolean(release.mandatory);
    confirmAction({
      title: mandatory ? `必须更新到 v${release.version}` : `发现新版本 v${release.version}`,
      message: `${release.title || "新版本已就绪"}${release.notes?.length ? `：${release.notes.join("；")}` : ""}。更新包约 ${formatBytes(release.package_size)}。点击更新后开始下载，下载完成后会请你确认关闭机器人和微信，再安装更新。`,
      confirmText: "立即更新",
      cancelText: mandatory ? "稍后更新" : "稍后提醒",
      intent: "primary",
      pendingText: "正在下载…",
      pendingMessage: `正在开始下载 v${release.version}，稍后可在右下角查看进度。`,
      onConfirm: () => beginUpdateDownload(release),
    });
  }

  async function refreshUpdate(manual = true) {
    if (state.updateChecking) return;
    state.updateChecking = true;
    const button = $("#checkUpdate");
    button.disabled = true;
    button.textContent = "检查中…";
    try {
      if (manual && state.availableUpdate) {
        const progress = await pollUpdateStatus();
        if (matchesRelease(state.availableUpdate, progress)) {
          if (progress.state === "running" || progress.state === "restarting") {
            $("#updateCard").focus();
            return;
          }
          if (isReadyForRelease(state.availableUpdate, progress)) {
            queueUpdatePrompt(state.availableUpdate, progress);
            return;
          }
        }
      }
      const data = await api("check_update");
      if (data.busy) { await pollUpdateStatus(); return; }
      state.availableUpdate = data.release || null;
      state.mandatoryUpdate = Boolean(data.release?.mandatory);
      if (state.status) renderStatus(state.status);
      if (!data.enabled) {
        renderUpdateCard(null, null);
        $("#updateStatus").textContent = "在线更新未启用";
      } else if (!data.release) {
        renderUpdateCard(null, null);
        $("#updateStatus").textContent = "已是最新版本";
        promptedReleaseId = "";
        if (manual) toast("当前已经是最新版本。 ");
      } else {
        const release = data.release;
        $("#updateStatus").textContent = release.mandatory
          ? `必须更新至 v${release.version}`
          : `发现 v${release.version}`;
        const progress = await pollUpdateStatus();
        const matching = matchesRelease(release, progress);
        if (isReadyForRelease(release, progress)) {
          if (manual || promptedReadyToken !== progress.confirmation_token) queueUpdatePrompt(release, progress);
        } else if (!(matching && ["running", "ready", "restarting"].includes(progress.state))
            && (manual || promptedReleaseId !== release.release_id)) {
          queueUpdatePrompt(release);
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
      if ($("#updateCard").hidden) button.textContent = state.availableUpdate ? "查看更新" : "检查更新";
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
    if (statusRequest) return statusRequest;
    const revision = statusRevision;
    statusRequest = (async () => {
      try {
        const status = await api("get_update_status");
        if (revision !== statusRevision) return latestStatus;
        latestStatus = status;
        const release = state.availableUpdate;
        renderUpdateCard(release, status);
        if (isReadyForRelease(release, status) && promptedReadyToken !== status.confirmation_token) {
          queueUpdatePrompt(release, status);
        }
        return status;
      } catch (_error) {
        // Update status is optional background work; it must not disturb normal use.
        return null;
      }
    })();
    try { return await statusRequest; }
    finally { statusRequest = null; }
  }

  $("#updateCardAction").addEventListener("click", async () => {
    const release = state.availableUpdate;
    if (!matchesRelease(release, latestStatus)) return;
    if (isReadyForRelease(release, latestStatus)) queueUpdatePrompt(release, latestStatus);
    else {
      try { await beginUpdateDownload(release); }
      catch (error) { reportError(error); }
    }
  });

  return {
    pollUpdateStatus,
    refreshUpdate,
    scheduleAutomaticUpdateCheck,
  };
};
