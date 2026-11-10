/* Updates controller; the shell supplies shared services explicitly. */
window.createUpdateNotifications = function (helpers) {
  "use strict";
  const {
    $,
    api,
    confirmAction,
    formatBytes,
    managerCloseState,
    renderStatus,
    reportError,
    state,
    syncShellInteractivity,
    toast,
  } = helpers;

  let promptedReleaseId = "";
  let promptedReadyToken = "";
  let dismissedReadyToken = "";
  let pendingPrompt = null;
  let promptTimer = 0;
  let latestStatus = null;
  let statusRequest = null;
  let downloadStarting = false;
  let statusRevision = 0;

  function renderVersionStatus(mode, label) {
    $("#versionStatusDot").className = `health-dot${mode ? ` ${mode}` : ""}`;
    const version = $("#versionStatus");
    version.title = label;
    version.setAttribute("aria-label", `${$("#versionText").textContent} · ${label}`);
  }

  function isReadyForRelease(release, progress) {
    return progress?.state === "ready" && Boolean(progress.confirmation_token)
      && Boolean(release?.release_id) && progress.release_id === release.release_id;
  }

  function matchesRelease(release, progress) {
    return Boolean(release?.release_id) && progress?.release_id === release.release_id;
  }

  function renderUpdateCard(release, progress) {
    state.updateInstalling = Boolean(progress?.state === "restarting" || progress?.close_pending
      || (progress?.state === "running" && ["prepare", "stopping_services", "launching", "install"].includes(progress.phase)));
    syncShellInteractivity();
    const card = $("#updateCard");
    const matching = matchesRelease(release, progress);
    const ready = isReadyForRelease(release, progress);
    card.hidden = !matching || !["running", "ready", "failed", "restarting"].includes(progress.state)
      || (ready && dismissedReadyToken === progress.confirmation_token);
    document.body.classList.toggle("has-update-card", !card.hidden);
    if (card.hidden) return;
    const failed = progress.state === "failed" || (progress.state === "ready" && !ready);
    const closing = ready && progress.close_pending;
    const percent = Math.min(100, Math.max(0, Number(progress.percent) || 0));
    const indeterminate = ["starting", "stopping_services", "waiting_wechat", "launching"].includes(progress.phase);
    card.dataset.state = failed || closing ? "failed" : progress.state;
    $("#updateCardTitle").textContent = closing ? "管理器未能关闭" : failed ? "更新未完成" : ready ? "更新已准备就绪" : `正在更新 v${release.version}`;
    $("#updateCardMessage").textContent = failed || closing
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
    $("#updateCardDismiss").hidden = !ready || closing;
    action.hidden = !ready && !failed;
    action.disabled = downloadStarting;
    action.textContent = closing ? "再次关闭管理器" : ready ? "立即安装" : "重试下载";
    $("#updateStatus").textContent = closing ? "更新正在等待管理器退出" : failed ? `更新准备失败 · ${progress.error?.code || "UPD-1003"}`
      : ready ? `v${release.version} 已下载，等待安装`
        : progress.state === "restarting" ? "正在重新启动管理器" : `${progress.message || "后台下载"} · ${percent}%`;
    $("#checkUpdate").textContent = closing ? "继续更新" : failed ? "重试更新" : ready ? "安装更新" : "查看更新";
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
    const closing = Boolean(ready.close_pending);
    dismissedReadyToken = "";
    renderUpdateCard(release, ready);
    promptedReadyToken = ready.confirmation_token;
    confirmAction({
      title: closing ? "再次关闭管理器以继续更新？" : `安装 v${release.version}？`,
      message: closing ? "上次未能自动关闭管理器，更新程序仍在等待。确认后会再次尝试关闭本窗口，继续同一次更新；无需重新下载。请先保存正在编辑的设置。"
        : "更新包已下载并校验完成。更新会强制关闭机器人程序及当前连接的微信，是否确认？该微信中正在处理的消息和未发送的内容可能丢失。管理器会关闭并重新打开；请先保存正在编辑的设置。安装完成后会自动打开新版管理器。",
      confirmText: closing ? "再次关闭管理器" : "确认关闭并安装",
      cancelText: closing ? "返回管理器" : "稍后安装",
      intent: "danger",
      pendingText: closing ? "正在关闭管理器…" : "正在安装…",
      pendingMessage: closing ? "正在再次关闭管理器以继续更新。" : "正在关闭机器人程序及当前连接的微信，准备安装更新。请保持管理器打开。",
      onConfirm: async () => {
        // This confirmation is already busy; keep checking other operations.
        const pending = managerCloseState({ignoreModalBusy: true});
        if (pending.busy) throw new Error("机器人正在启动、停止或处理其他操作，请稍候；也可选择“稍后安装”，更新包会保留。");
        if (pending.dirty) throw new Error("有设置尚未保存。请选择“稍后安装”，保存或处理修改后再安装；更新包会保留，无需重新下载。");
        const accepted = await api("install_update", release.version, ready.confirmation_token);
        if (!accepted?.accepted) throw new Error("机器人或更新操作尚未完成，请稍后再次确认安装；更新包会保留。");
        // Consent is consumed once accepted; progress reads must never retry it.
        statusRevision += 1;
        latestStatus = {...ready, state: closing ? "restarting" : "running",
          phase: closing ? "restarting" : "prepare", confirmation_token: "", close_pending: false,
          error: null, message: closing ? "正在再次关闭管理器以继续更新。请保持管理器打开。"
            : "安装请求已接受，正在确认更新进度。请保持管理器打开。"};
        renderUpdateCard(release, latestStatus);
        void pollUpdateStatus();
      },
    });
  }

  function showUpdatePrompt(release) {
    const mandatory = Boolean(release.mandatory);
    confirmAction({
      title: mandatory ? `必须更新到 v${release.version}` : `发现新版本 v${release.version}`,
      message: [
        release.title || "新版本已就绪",
        release.notes?.length ? release.notes.join("\n") : "",
        `更新包约 ${formatBytes(release.package_size)}。`,
        "点击更新后开始下载。下载完成后，会再次请你确认关闭机器人及当前连接的微信，再安装更新。",
      ].filter(Boolean).join("\n\n"),
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
        const progress = await pollUpdateStatus()
          || (state.updateInstalling && ["running", "restarting"].includes(latestStatus?.state) ? latestStatus : null);
        if (matchesRelease(state.availableUpdate, progress)) {
          if (progress.state === "running" || progress.state === "restarting") {
            renderUpdateCard(state.availableUpdate, progress);
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
        renderVersionStatus("", "在线更新未启用");
        renderUpdateCard(null, null);
        $("#updateStatus").textContent = "在线更新未启用";
      } else if (!data.release) {
        renderVersionStatus("online", "已是最新版本");
        renderUpdateCard(null, null);
        $("#updateStatus").textContent = "已是最新版本";
        promptedReleaseId = "";
        if (manual) toast("当前已经是最新版本。 ");
      } else {
        const release = data.release;
        renderVersionStatus("warning", `${release.mandatory ? "必须更新至" : "有可用更新"} v${release.version}`);
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
      renderVersionStatus(state.availableUpdate || state.mandatoryUpdate ? "warning" : "",
        mandatoryUnavailable ? "当前版本已停止支持，暂未获取到可安装更新"
          : state.availableUpdate ? `有可用更新 v${state.availableUpdate.version}，本次检查失败`
            : state.mandatoryUpdate ? "当前版本需要更新，本次检查失败" : "更新检查失败");
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

  $("#updateCardDismiss").addEventListener("click", () => {
    if (!isReadyForRelease(state.availableUpdate, latestStatus)) return;
    dismissedReadyToken = latestStatus.confirmation_token;
    renderUpdateCard(state.availableUpdate, latestStatus);
    toast("更新包已保留，可在“机器人总设置 → 系统与更新”中点击“安装更新”。");
  });

  return {
    pollUpdateStatus,
    refreshUpdate,
    scheduleAutomaticUpdateCheck,
  };
};
