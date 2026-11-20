/* Model services controller; the shell supplies shared services explicitly. */
window.createModelServices = function (helpers) {
  "use strict";
  const {
    $,
    $$,
    api,
    confirmAction,
    escapeHtml,
    icon,
    renderHomeSetupAlerts,
    reportError,
    requiredValues,
    state,
    toast,
    getCoreSettings,
  } = helpers;

  let providerGridMotion;
  let providerModelDiscoveryGeneration = 0;
  let resetProviderModelDiscovery = null;
  let providerEditorGeneration = 0;
  let providerSaveBusy = false;
  let providerReadGeneration = 0;
  let providerReadBusy = false;
  let providerMutationBusy = false;
  let providerReadbackResult = "模型已保存";

  const providerTests = window.createModelServiceTests({
    $, $$, api, escapeHtml, reportError, state, toast, updateProviderDirty, visibleProviderServices,
  });
  const {clearProviderTest, testSelectedProvider} = providerTests;

  const providerIconFiles = {
    deepseek: "deepseek.ico",
    glm: "glm.png",
    xiaomi_mimo: "xiaomi_mimo.ico",
    qwen: "qwen.png",
    volcengine: "volcengine.png",
    hunyuan: "hunyuan.png",
    gemini_proxy: "gemini_proxy.png",
  };

  function providerFormSnapshot(submitted) {
    const form = $("#providerForm");
    if (!form) return "";
    const values = {...(submitted || Object.fromEntries(new FormData(form)))};
    delete values.api_key;
    return JSON.stringify(values);
  }

  function markProviderClean(status = "尚未保存", snapshot = providerFormSnapshot()) {
    state.providerBaseline = snapshot;
    state.providerDirty = false;
    state.providerCleanStatus = status;
    const badge = $("#providerEditorStatus");
    if (badge) {
      badge.className = `status-badge ${status === "已保存" ? "success" : "checking"}`;
      badge.textContent = status;
    }
  }

  function updateProviderDirty(event) {
    if (state.providerScreen !== "editor") return;
    if (["api_base", "api_key"].includes(event?.target?.name)) {
      clearProviderModels();
      $("#providerModelHint").textContent = "地址或密钥已修改，可重新获取可用模型。";
    }
    clearProviderTest();
    syncProviderIcon();
    const form = $("#providerForm");
    const keyChanged = Boolean(form?.elements.api_key.value);
    state.providerDirty = keyChanged || providerFormSnapshot() !== state.providerBaseline;
    const badge = $("#providerEditorStatus");
    badge.className = `status-badge ${state.providerDirty ? "warning" : state.providerCleanStatus === "已保存" ? "success" : "checking"}`;
    badge.textContent = state.providerDirty ? "有未保存修改" : state.providerCleanStatus;
  }

  function showProviderOverview(focus = false) {
    providerEditorGeneration += 1;
    clearProviderTest();
    clearProviderModels();
    state.providerScreen = "overview";
    state.providerDirty = false;
    $("#providerOverview").hidden = false;
    $("#providerEditorPage").hidden = true;
    if (focus) $("#providerOverviewTitle")?.focus();
  }

  function showProviderEditor(focus = true) {
    providerEditorGeneration += 1;
    clearProviderTest();
    state.providerScreen = "editor";
    $("#providerOverview").hidden = true;
    $("#providerEditorPage").hidden = false;
    if (focus) $("#providerEditorTitle")?.focus();
  }

  function guardProviderDraft(next) {
    if (!state.providerDirty || state.providerScreen !== "editor") return false;
    confirmAction({
      title: "放弃模型配置？",
      message: "Base URL、模型名称或 API Key / Token Plan 还没有保存。返回后这些修改会被清空。",
      confirmText: "放弃修改",
      onConfirm: async () => {
        showProviderOverview();
        await next();
      },
    });
    return true;
  }

  function requestProviderOverview() {
    if (guardProviderDraft(() => showProviderOverview(true))) return;
    showProviderOverview(true);
  }

  function applyProviderServices(services) {
    state.bootstrap.providers = services;
    state.bootstrap.provider = services.find((item) => item.is_default) || null;
    state.featureDetails = {};
    getCoreSettings().invalidate();
    renderProviderServices();
  }

  function acceptSavedProvider(data, submitted, generation) {
    const services = state.bootstrap.providers || [];
    const existing = services.find((item) => item.id === data.id);
    const updated = services
      .filter((item) => item.id !== data.id)
      .map((item) => data.is_default ? {...item, is_default: false} : item);
    updated.push({...existing, ...data, last_test: data.last_test ?? null});
    providerTests.invalidate(submitted.provider_id);
    providerTests.invalidate(data.id);
    applyProviderServices(updated);
    if (generation !== providerEditorGeneration || state.providerScreen !== "editor") return;
    const form = $("#providerForm");
    form.elements.provider_id.value = data.id;
    const savedBase = typeof data.api_base === "string" ? data.api_base : submitted.api_base;
    if (form.elements.api_base.value === submitted.api_base) {
      form.elements.api_base.value = savedBase;
    }
    $("#providerEditorTitle").textContent = data.name;
    if (form.elements.api_key.value === submitted.api_key) {
      form.elements.api_key.value = "";
      form.elements.api_key.type = "password";
      $("#revealProviderKey").textContent = "显示";
      $("#revealProviderKey").setAttribute("aria-pressed", "false");
    }
    $("#apiKeyHint").textContent = "已安全保存；留空不会覆盖";
    $("#testProvider").hidden = !data.configured;
    $("#deleteProvider").hidden = false;
    markProviderClean("已保存", providerFormSnapshot({...submitted, provider_id: data.id, api_base: savedBase}));
    updateProviderDirty();
    $("#providerNote").textContent = state.providerDirty
      ? "提交时的配置已保存；之后的修改仍未保存，请再次保存。" : "模型服务已保存。";
  }

  function beginProviderMutation() {
    providerMutationBusy = true;
    providerReadGeneration += 1;
    providerReadBusy = false;
    $("#providerReadbackFeedback").hidden = true;
    $("#reloadProviderServices").disabled = true;
  }

  function finishProviderMutation() {
    providerMutationBusy = false;
    $("#reloadProviderServices").disabled = providerReadBusy || providerSaveBusy;
  }

  async function refreshProviderServices(confirmedResult = providerReadbackResult) {
    const request = ++providerReadGeneration;
    const feedback = $("#providerReadbackFeedback");
    const button = $("#reloadProviderServices");
    providerReadBusy = true;
    providerReadbackResult = confirmedResult;
    feedback.hidden = false;
    button.disabled = true;
    $("#providerReadbackMessage").textContent = `${confirmedResult}，正在重新读取模型列表…`;
    try {
      const providers = await api("get_providers");
      if (request !== providerReadGeneration) return false;
      applyProviderServices(providers);
      feedback.hidden = true;
      return true;
    } catch (error) {
      if (request !== providerReadGeneration) return false;
      $("#providerReadbackMessage").textContent = `${confirmedResult}，但列表暂时未能刷新。请重新读取模型列表，已完成的操作无需再次执行。`;
      const readError = new Error(`${confirmedResult}，列表刷新失败：${error.message}`, {cause: error});
      if (error.product) readError.product = {...error.product, title: `${confirmedResult}，列表刷新失败`};
      reportError(readError);
      return false;
    } finally {
      if (request === providerReadGeneration) {
        providerReadBusy = false;
        button.disabled = providerSaveBusy || providerMutationBusy;
      }
    }
  }

  async function reloadProviderServices() {
    if (providerReadBusy || providerSaveBusy || providerMutationBusy) return;
    if (await refreshProviderServices()) toast("模型列表已重新读取。");
  }

  async function saveProvider(event) {
    event.preventDefault();
    const form = event.currentTarget;
    if (providerSaveBusy || providerMutationBusy || !requiredValues(form)) return;
    const generation = providerEditorGeneration;
    const submitted = Object.fromEntries(new FormData(form));
    const button = $('button[type="submit"]', form);
    const originalText = button.textContent;
    providerSaveBusy = true;
    beginProviderMutation();
    button.disabled = true;
    button.textContent = "正在保存并应用…";
    $("#providerNote").textContent = "正在更新统一模型配置…";
    try {
      const values = {...submitted};
      if (values.service_kind === "chat") {
        values.thinking_enabled = form.elements.thinking_enabled.checked;
      } else {
        delete values.thinking_enabled;
      }
      if (values.service_kind === "chat") {
        values.modalities = "text,image,audio";
      } else {
        values.modalities = inferProviderModalities(values.service_kind, values.model).join(",");
      }
      const data = await api("save_provider", values);
      acceptSavedProvider(data, submitted, generation);
      toast(data.is_default
        ? (data.apply_state === "applied"
          ? "模型服务已保存，并已设为当前回复模型。"
          : "模型服务已保存，机器人启动时会使用它。")
        : "模型服务已保存，当前回复模型保持不变。");
      await refreshProviderServices("模型已保存");
      if (generation === providerEditorGeneration && state.providerScreen === "editor") {
        if (state.providerDirty) {
          $("#providerNote").textContent = "提交时的配置已保存；之后的修改仍未保存，请再次保存。";
        }
      }
    } catch (error) {
      reportError(error);
      if (generation === providerEditorGeneration) $("#providerNote").textContent = error.message;
    } finally {
      providerSaveBusy = false;
      button.disabled = false;
      finishProviderMutation();
      if (generation === providerEditorGeneration && state.providerScreen === "editor") {
        renderProviderSaveIntent(
          providerWillBeDefault(form.elements.service_kind.value, form.elements.provider_id.value),
          form.elements.service_kind.value,
        );
      } else if (state.providerScreen === "overview") button.textContent = originalText;
    }
  }

  function providerPresetFor(service) {
    if (!service) return "deepseek";
    // Model names are also used by relays; only an official endpoint gets a brand.
    const host = providerEndpointHost(service.api_base);
    return (state.bootstrap?.provider_presets || []).find((preset) =>
      host && providerIconFiles[preset.id] && providerEndpointHost(preset.api_base) === host,
    )?.id || "custom";
  }

  function providerEndpointHost(apiBase) {
    try {
      const endpoint = new URL(String(apiBase || ""));
      return endpoint.protocol === "https:" && !endpoint.username && !endpoint.password
        ? endpoint.host.toLowerCase() : "";
    } catch (_) { return ""; }
  }

  function providerIconMarkup(presetId) {
    const file = providerIconFiles[presetId];
    return file
      ? `<img src="assets/provider-${file}" alt="" width="24" height="24">`
      : icon("i-grid");
  }

  function syncProviderIcon() {
    const form = $("#providerForm");
    const target = $("#providerPresetIcon");
    if (!form || !target) return;
    const selected = form.elements.preset.value;
    const actual = providerPresetFor({
      api_base: form.elements.api_base.value,
      service_kind: form.elements.service_kind.value,
    });
    const preset = selected === actual ? selected : "custom";
    if (target.dataset.preset === preset) return;
    target.dataset.preset = preset;
    target.innerHTML = providerIconMarkup(preset);
  }

  function providerCardIdentity(service) {
    const preset = providerPresetFor(service);
    return {mark: providerIconMarkup(preset), className: "provider-brand-logo"};
  }

  function providerChatModalityValue(modalities) {
    const source = Array.isArray(modalities) ? modalities : String(modalities || "").split(",");
    const values = new Set(source.map((item) => String(item).trim().toLowerCase()));
    return ["text", "image", "audio"].filter((item) => values.has(item)).join(",");
  }

  function providerCapabilityLabel(serviceKind) {
    return {embedding: "向量", image: "图片", voice: "语音", rerank: "重排序"}[serviceKind] || "对话";
  }

  function inferProviderModalities(serviceKind, model, declared) {
    if (serviceKind === "embedding") return ["embedding"];
    if (serviceKind === "image") return ["image"];
    if (serviceKind === "voice") return ["audio"];
    const name = String(model || "").trim().toLowerCase();
    const declaredValue = providerChatModalityValue(declared);
    if (declaredValue) return declaredValue.split(",");
    if (name === "grok-4.6") return ["text", "image", "audio"];
    const leaf = name.split("/").pop() || "";
    if (["-v", "vision", "vl", "gemini", "doubao-seed"].some((marker) => leaf.includes(marker))) {
      return leaf.includes("gemini") || leaf.includes("doubao")
        ? ["text", "image", "audio"]
        : ["text", "image"];
    }
    return ["text", "image", "audio"];
  }

  function providerThinkingSupported(apiBase, model) {
    try {
      const endpoint = new URL(String(apiBase || ""));
      const host = endpoint.hostname.toLowerCase();
      const name = String(model || "").trim().toLowerCase();
      const leaf = name.split("/").pop();
      const on = (...domains) => domains.some((domain) => host === domain || host.endsWith(`.${domain}`));
      const deepseek = !/(reasoner|speciale|-r1|r1-)/.test(leaf) && /^(deepseek-chat|deepseek-flash|deepseek-v3|deepseek-v4)/.test(leaf);
      const qwen = /^(qwen3|qwen-3)/.test(leaf) && !/(thinking|qwq)/.test(leaf);
      const glm = /^glm-(?:4\.(?:5|6|7)|5(?:[.\-]|$))/.test(leaf);
      const kimi = /^(kimi-k2\.5|kimi-k2\.6)/.test(leaf);
      const openai = /^gpt-5\.(?:[1-9]|\d{2,})/.test(leaf) && !leaf.includes("pro");
      const claude = /^claude-(?:3\.7|(?:opus|sonnet|haiku)-4(?:[.\-]|$))/.test(leaf);
      const ollama = endpoint.port === "11434" || ["localhost", "127.0.0.1", "[::1]", "::1"].includes(host);
      return (on("xiaomimimo.com") && ["mimo-v2.5", "mimo-v2.5-pro"].includes(leaf))
        || (on("deepseek.com") && deepseek)
        || (on("dashscope.aliyuncs.com") && !leaf.includes("thinking") && (/^qwen-plus/.test(leaf) || qwen || deepseek || kimi || glm))
        || (on("volces.com") && /^(doubao-seed-1-6|doubao-seed-2)/.test(leaf))
        || (on("bigmodel.cn", "z.ai") && glm)
        || (on("siliconflow.cn", "siliconflow.com") && (qwen || deepseek || glm || /^hunyuan-a13b/.test(leaf)))
        || (on("moonshot.cn", "moonshot.ai") && kimi)
        || (on("openrouter.ai") && (qwen || deepseek || glm || kimi || openai || /^gemini-(?:2\.5|3\.8)-flash(?:-lite)?/.test(leaf) || claude))
        || (on("together.xyz", "together.ai") && /^(qwen3\.5|qwen-3\.5|deepseek-v4)/.test(leaf))
        || (on("fireworks.ai") && (qwen || deepseek || glm))
        || (on("groq.com") && /^(qwen3\.6-27b|qwen3\.8-27b)/.test(leaf))
        || (on("cerebras.ai") && leaf === "zai-glm-4.7")
        || (ollama && !leaf.includes("gpt-oss") && /^(qwen3|qwen-3|deepseek-v3\.1|deepseek-r1|glm-4\.7)/.test(leaf))
        || (on("x.ai", "xiuxianjyj.xin") && /^(grok-4\.5|grok-4\.6)/.test(leaf))
        || (on("googleapis.com") && /^gemini-(?:2\.5|3\.8)-flash(?:-lite)?/.test(leaf))
        || (on("openai.com", "openai.azure.com", "services.ai.azure.com", "models.inference.ai.azure.com") && openai);
    } catch (_) { return false; }
  }

  function syncProviderThinkingControl(initialValue = null) {
    const form = $("#providerForm");
    const input = form.elements.thinking_enabled;
    if (initialValue !== null) input.checked = Boolean(initialValue);
    input.disabled = false;
    $("#providerThinkingValue").textContent = input.checked ? "已开启" : "已关闭";
  }

  function applyProviderPreset(presetId) {
    const form = $("#providerForm");
    const preset = (state.bootstrap?.provider_presets || []).find((item) => item.id === presetId);
    if (!preset) return;
    form.elements.api_base.value = preset.api_base || "";
    form.elements.model.value = form.elements.service_kind.value === "chat" ? preset.model || "" : "";
    form.elements.service_kind.disabled = false;
    syncProviderIcon();
    syncProviderServiceKind();
  }

  function syncProviderServiceKind() {
    const form = $("#providerForm");
    const kind = form.elements.service_kind.value;
    $("#providerThinkingRow").hidden = kind !== "chat";
    clearProviderModels();
    $("#providerModelHint").textContent = "";
    syncProviderThinkingControl();
    renderProviderSaveIntent(
      providerWillBeDefault(kind, form.elements.provider_id.value), kind,
    );
  }

  function isFactoryProvider(service) {
    return Boolean(
      service?.is_recommended
      && String(service.model || "").toLowerCase() === "grok-4.6"
    );
  }

  function visibleProviderServices() {
    return (state.bootstrap?.providers || []).filter((item) => !isFactoryProvider(item));
  }

  function providerWillBeDefault(serviceKind, providerId = "") {
    if (serviceKind !== "chat") return false;
    const services = visibleProviderServices();
    const selected = services.find((item) => item.id === providerId);
    return !services.some((item) => (item.service_kind || "chat") === "chat")
      || Boolean(selected?.is_default);
  }

  function renderProviderSaveIntent(makeDefault, serviceKind = "chat") {
    if (serviceKind !== "chat") {
      const label = serviceKind === "image" ? "图片模型" : serviceKind === "voice" ? "语音模型" : "向量模型";
      $("#saveProvider").textContent = `保存${label}`;
      return;
    }
    $("#saveProvider").textContent = makeDefault ? "保存并设为回复模型" : "保存模型服务";
  }

  function beginNewProvider(presetId = "deepseek") {
    const form = $("#providerForm");
    form.reset();
    form.elements.provider_id.value = "";
    form.elements.preset.value = presetId;
    applyProviderPreset(presetId);
    $("#providerEditorTitle").textContent = "新建模型服务";
    $("#apiKeyHint").textContent = "API Key / Token Plan 只保存在本机，不会回显";
    $("#testProvider").hidden = true;
    $("#deleteProvider").hidden = true;
    $("#providerNote").textContent = "";
    renderProviderSaveIntent(
      providerWillBeDefault(form.elements.service_kind.value, ""),
      form.elements.service_kind.value,
    );
    markProviderClean("尚未保存");
    showProviderEditor();
  }

  function editProvider(providerId) {
    const service = (state.bootstrap?.providers || []).find((item) => item.id === providerId);
    if (!service) { beginNewProvider(); return; }
    const form = $("#providerForm");
    form.elements.provider_id.value = service.id;
    form.elements.preset.value = providerPresetFor(service);
    form.elements.service_kind.value = service.service_kind || "chat";
    $("#providerThinkingRow").hidden = (service.service_kind || "chat") !== "chat";
    form.elements.api_base.value = service.api_base || "";
    form.elements.model.value = service.model || "";
    form.elements.api_key.value = "";
    syncProviderIcon();
    syncProviderThinkingControl(service.thinking_enabled !== false);
    clearProviderModels();
    $("#providerModelHint").textContent = "";
    $("#providerEditorTitle").textContent = service.name;
    $("#apiKeyHint").textContent = service.api_key_configured ? "已安全保存；留空不会覆盖" : "尚未配置 API Key / Token Plan";
    $("#testProvider").hidden = !service.configured;
    $("#deleteProvider").hidden = false;
    $("#providerNote").textContent = "";
    renderProviderSaveIntent(Boolean(service.is_default), service.service_kind || "chat");
    markProviderClean("已保存");
    showProviderEditor();
  }

  function renderProviderServices() {
    const list = $("#providerServiceList");
    providerGridMotion ??= window.managerMotion?.trackGrid(list);
    const before = providerGridMotion?.capture();
    const items = visibleProviderServices();
    const displayItems = [...items].sort((left, right) => Number(Boolean(right.is_default || right.is_default_embedding)) - Number(Boolean(left.is_default || left.is_default_embedding)));
    const status = $("#providerStatus");
    const defaultProvider = items.find((item) => item.is_default);
    status.className = `status-badge ${defaultProvider ? "success" : "checking"}`;
    status.textContent = `${items.length} 项服务`;
    list.innerHTML = items.length ? `
      <div class="provider-list-toolbar"><div><strong>${escapeHtml(defaultProvider ? `${defaultProvider.model || defaultProvider.name} 正在回复` : "请选择回复模型")}</strong></div><div class="provider-batch-actions"><button class="button secondary small" type="button" data-action-bound disabled data-provider-test-all>测试所有模型</button><span class="provider-batch-status" role="status"></span></div></div>
      <div class="provider-service-cards">${displayItems.map((item) => {
        const identity = providerCardIdentity(item);
        const active = item.is_default || item.is_default_embedding;
        const statusText = active ? (item.is_default_embedding ? "默认向量" : "使用中")
          : !item.configured ? "待完善" : "备用";
        const kind = item.service_kind || "chat";
        const switchAction = !active && ["chat", "embedding"].includes(kind)
          ? `<button class="provider-card-action primary" type="button" data-action-bound disabled data-provider-${kind === "embedding" ? "embedding-default" : "default"}="${escapeHtml(item.id)}">${kind === "embedding" ? "设为默认" : "切换使用"}</button>` : "";
        return `
        <article class="provider-service ${active ? "active" : ""}" data-provider-card="${escapeHtml(item.id)}" data-motion-key="provider:${escapeHtml(item.id)}">
          <header class="provider-card-header">
            <div class="provider-card-title">
              <span class="provider-logo ${identity.className}" aria-hidden="true">${identity.mark}</span>
              <div class="provider-service-copy"><strong>${escapeHtml(item.model || item.name)}</strong></div>
            </div>
            <div class="provider-card-status-row">
              <details class="provider-card-menu">
                <summary aria-label="打开 ${escapeHtml(item.name)} 操作菜单" title="更多操作">${icon("i-more")}</summary>
                <div class="provider-card-menu-popover" role="menu">
                  <button type="button" role="menuitem" data-action-bound disabled data-provider-edit="${escapeHtml(item.id)}">编辑服务</button>
                  <button class="danger" type="button" role="menuitem" data-action-bound disabled data-provider-delete="${escapeHtml(item.id)}">删除服务</button>
                </div>
              </details>
            </div>
          </header>
          <div class="provider-service-body">
            <div class="provider-card-tags">
              <span class="provider-kind-tag" data-provider-kind="${escapeHtml(kind)}">${escapeHtml(providerCapabilityLabel(kind))}</span>
              <span class="provider-card-status ${active ? "active" : item.configured ? "ready" : "incomplete"}">${statusText}</span>
              ${item.configured ? "" : '<span>缺少 API Key / Token Plan</span>'}
            </div>
            ${item.service_kind === "chat" ? `<div class="provider-thinking-control">
              <span><strong>思考模式</strong></span>
              <button class="provider-thinking-switch" type="button" role="switch" aria-label="${escapeHtml(item.name)}思考模式" aria-checked="${Boolean(item.thinking_enabled)}" data-action-bound disabled data-provider-thinking="${escapeHtml(item.id)}"><span aria-hidden="true"></span></button>
            </div>` : ""}
          </div>
          <footer class="provider-service-actions">
            <button class="provider-card-action" type="button" data-action-bound disabled data-provider-edit="${escapeHtml(item.id)}">编辑</button>
            <button class="provider-card-action" type="button" data-action-bound disabled data-provider-test="${escapeHtml(item.id)}">测试</button>
            ${switchAction}
            <button class="provider-card-action danger" type="button" data-action-bound disabled data-provider-delete="${escapeHtml(item.id)}">删除</button>
            ${providerTests.cardMarkup(item)}
          </footer>
        </article>`;
      }).join("")}
        <button class="provider-add-card" type="button" data-motion-key="provider-add" data-action-bound disabled data-provider-create>
          <span class="provider-add-icon" aria-hidden="true">${icon("i-plus")}</span>
          <strong>添加模型服务</strong>
          <small>连接其他兼容模型</small>
        </button>
      </div>` : `
      <div class="provider-empty-state">
        <div class="provider-empty-visual" aria-hidden="true"><span></span><i></i><span></span></div>

        <h3>还没有模型服务</h3>

        <button class="button primary" type="button" data-action-bound disabled data-provider-create>新建模型服务</button>

      </div>`;
    $$('[data-action-bound]', $("#providerServiceList")).forEach((button) => { button.disabled = false; });
    providerTests.refresh();
    providerGridMotion?.refresh(before);
    renderHomeSetupAlerts();
  }

  async function setProviderDefault(providerId, button) {
    if (button.disabled || providerMutationBusy) return;
    const original = button.textContent;
    beginProviderMutation();
    button.disabled = true;
    button.textContent = "切换中…";
    try {
      const result = await api("set_default_provider", providerId);
      applyProviderServices(state.bootstrap.providers.map((item) => ({...item, is_default: item.id === result.id})));
      toast(result.apply_state === "applied" ? "回复模型已切换并立即生效。" : "回复模型已切换，将在启动时生效。");
      await refreshProviderServices("回复模型已切换");
    } catch (error) { reportError(error); }
    finally { button.disabled = false; button.textContent = original; finishProviderMutation(); }
  }

  async function setDefaultEmbeddingProvider(providerId, button) {
    if (button.disabled || providerMutationBusy) return;
    const original = button.textContent;
    beginProviderMutation();
    button.disabled = true;
    button.textContent = "切换中…";
    try {
      const result = await api("set_default_embedding_provider", providerId);
      applyProviderServices(state.bootstrap.providers.map((item) => ({...item, is_default_embedding: item.id === result.id})));
      toast({applied: "默认向量模型已切换。", pending_enable: "默认向量模型已保存，启用长期记忆后使用。",
        pending_setup: "默认向量模型已保存，长期记忆准备就绪后使用。"}[result.apply_state]
        || "默认向量模型已保存，将在启动时生效。");
      await refreshProviderServices("默认向量模型已切换");
    } catch (error) { reportError(error); }
    finally { button.disabled = false; button.textContent = original; finishProviderMutation(); }
  }

  async function discoverProviderModels() {
    const form = $("#providerForm");
    const button = $("#discoverProviderModels");
    if (button.disabled) return;
    const generation = ++providerModelDiscoveryGeneration;
    const values = Object.fromEntries(new FormData(form));
    const current = () => generation === providerModelDiscoveryGeneration
      && state.activePage === "settings" && state.settingsSection === "model"
      && state.providerScreen === "editor"
      && ["provider_id", "preset", "service_kind", "api_base", "api_key"].every(
        (name) => form.elements[name].value === values[name],
      );
    const original = button.textContent;
    resetProviderModelDiscovery = () => { button.disabled = false; button.textContent = original; };
    button.disabled = true;
    button.textContent = "读取中…";
    $("#providerModelHint").textContent = "正在验证地址和密钥，并读取模型列表…";
    try {
      const data = await api("discover_provider_models", values);
      if (!current()) return;
      if (typeof data.api_base === "string" && data.api_base !== form.elements.api_base.value) {
        form.elements.api_base.value = data.api_base;
        updateProviderDirty();
      }
      const items = Array.isArray(data.items) ? data.items : [];
      state.providerModels = [...new Set(items.map((model) => String(model).trim()).filter(Boolean))];
      state.providerModelFiltering = false;
      state.providerModelFocus = -1;
      renderProviderModelOptions(true);
      $("#providerModelHint").textContent = `已读取 ${state.providerModels.length} 个模型；可从下方完整列表选择，也可以继续手动填写。`;
    } catch (error) {
      if (!current()) return;
      reportError(error);
      $("#providerModelHint").textContent = error.message;
    } finally {
      if (generation === providerModelDiscoveryGeneration) {
        resetProviderModelDiscovery?.();
        resetProviderModelDiscovery = null;
      }
    }
  }

  function clearProviderModels() {
    providerModelDiscoveryGeneration += 1;
    resetProviderModelDiscovery?.();
    resetProviderModelDiscovery = null;
    state.providerModels = [];
    state.providerModelFiltering = false;
    state.providerModelFocus = -1;
    const options = $("#providerModelOptions");
    const input = $("#providerModelInput");
    if (options) {
      options.innerHTML = "";
      options.hidden = true;
    }
    $("#providerModelCombobox")?.classList.remove("open-above");
    input?.setAttribute("aria-expanded", "false");
  }

  function providerModelMatches() {
    const keyword = state.providerModelFiltering
      ? $("#providerModelInput")?.value.trim().toLocaleLowerCase("zh-CN") || ""
      : "";
    return state.providerModels.filter((model) => !keyword || model.toLocaleLowerCase("zh-CN").includes(keyword));
  }

  function renderProviderModelOptions(open = false) {
    const input = $("#providerModelInput");
    const options = $("#providerModelOptions");
    const combobox = $("#providerModelCombobox");
    if (!input || !options) return;
    const wasExpanded = input.getAttribute("aria-expanded") === "true";
    const items = providerModelMatches();
    state.providerModelFocus = Math.min(state.providerModelFocus, items.length - 1);
    options.innerHTML = items.length ? items.map((model, index) => `
      <button type="button" role="option" aria-selected="${input.value === model}" class="model-option ${state.providerModelFocus === index ? "focused" : ""}" data-model="${escapeHtml(model)}">${escapeHtml(model)}</button>
    `).join("") : '<span class="model-option-empty">没有匹配的模型，可继续手动填写</span>';
    const expanded = open && state.providerModels.length > 0;
    options.hidden = !expanded;
    input.setAttribute("aria-expanded", String(expanded));
    if (!expanded) {
      combobox?.classList.remove("open-above");
    } else if (!wasExpanded && combobox) {
      const triggerRect = input.getBoundingClientRect();
      const scrollOwnerRect = input.closest(".content-scroll")?.getBoundingClientRect();
      const boundaryTop = Math.max(0, scrollOwnerRect?.top ?? 0);
      const boundaryBottom = Math.min(window.innerHeight, scrollOwnerRect?.bottom ?? window.innerHeight);
      const listHeight = Math.min(options.scrollHeight, 252);
      const spaceAbove = triggerRect.top - boundaryTop - 6;
      const spaceBelow = boundaryBottom - triggerRect.bottom - 6;
      combobox.classList.toggle("open-above", spaceBelow < listHeight && spaceAbove > spaceBelow);
    }
    if (expanded && state.providerModelFocus >= 0) {
      $(".model-option.focused", options)?.scrollIntoView({block: "nearest"});
    }
  }

  function chooseProviderModel(model) {
    const input = $("#providerModelInput");
    if (!input) return;
    input.value = model;
    input.dispatchEvent(new Event("input", {bubbles: true}));
    state.providerModelFiltering = false;
    state.providerModelFocus = -1;
    renderProviderModelOptions(false);
    input.focus();
  }

  function bindProviderModelCombobox() {
    const input = $("#providerModelInput");
    const options = $("#providerModelOptions");
    if (!input || !options) return;
    input.addEventListener("focus", () => {
      state.providerModelFiltering = false;
      state.providerModelFocus = -1;
      renderProviderModelOptions(true);
    });
    input.addEventListener("click", () => {
      if (!options.hidden) return;
      state.providerModelFiltering = false;
      state.providerModelFocus = -1;
      renderProviderModelOptions(true);
    });
    input.addEventListener("input", () => {
      state.providerModelFiltering = true;
      state.providerModelFocus = -1;
      renderProviderModelOptions(true);
    });
    input.addEventListener("keydown", (event) => {
      if (event.isComposing) return;
      if (!["ArrowDown", "ArrowUp", "Enter", "Escape"].includes(event.key)) return;
      if (event.key === "Escape") {
        renderProviderModelOptions(false);
        return;
      }
      const items = providerModelMatches();
      if (!items.length) return;
      if (event.key === "Enter") {
        if (state.providerModelFocus < 0) return;
        event.preventDefault();
        chooseProviderModel(items[state.providerModelFocus]);
        return;
      }
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      state.providerModelFocus = (state.providerModelFocus + direction + items.length) % items.length;
      renderProviderModelOptions(true);
    });
    options.addEventListener("mousedown", (event) => event.preventDefault());
    options.addEventListener("click", (event) => {
      const option = event.target.closest("[data-model]");
      if (option) {
        event.preventDefault();
        chooseProviderModel(option.dataset.model);
      }
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest("#providerModelCombobox")) renderProviderModelOptions(false);
    });
  }

  function deleteProvider(providerId) {
    const service = (state.bootstrap?.providers || []).find((item) => item.id === providerId);
    if (!service) return;
    const replacementKind = service.is_default ? "chat" : (service.service_kind || "chat");
    const alternatives = (state.bootstrap?.providers || []).filter((item) => item.id !== providerId && item.configured && (item.service_kind || "chat") === replacementKind);
    const references = Array.isArray(service.references) ? service.references : [];
    const needsReplacement = service.is_default || service.is_default_embedding || references.length > 0;
    if (needsReplacement && !alternatives.length) {
      toast("当前模型仍在被机器人或功能使用。请先添加另一个可用模型，再删除它。", "error");
      return;
    }
    const replacementUsers = [...new Set([...(service.is_default ? ["机器人回复"] : []), ...(service.is_default_embedding ? ["长期记忆"] : []), ...references])].join("、");
    confirmAction({
      title: "删除模型服务？",
      subject: {label: "将删除的模型", value: service.name},
      message: needsReplacement
        ? "此模型正在使用中。确认后先切换到下方的接替模型，再删除上方模型。"
        : "仅删除上方模型服务，不会删除功能或聊天记录。",
      confirmText: needsReplacement ? "切换并删除" : "确认删除",
      field: needsReplacement ? {
        label: `删除后，${replacementUsers}改用`,
        type: "select",
        value: alternatives[0]?.id || "",
        options: alternatives.map((item) => ({value: item.id, label: item.name})),
      } : null,
      onConfirm: async (replacementId) => {
        if (providerMutationBusy) throw new Error("模型配置正在更新，请稍后重试。");
        beginProviderMutation();
        try {
          await api("delete_provider", providerId, replacementId || "");
          providerTests.invalidate(providerId);
          applyProviderServices(state.bootstrap.providers
            .filter((item) => item.id !== providerId)
            .map((item) => item.id === replacementId ? {
              ...item,
              is_default: service.is_default || item.is_default,
              is_default_embedding: service.is_default_embedding || item.is_default_embedding,
              references: [...new Set([...(item.references || []), ...references])],
            } : item));
          markProviderClean();
          showProviderOverview(true);
          toast("模型服务已删除。");
          await refreshProviderServices("模型服务已删除");
        } finally { finishProviderMutation(); }
      },
    });
  }

  async function setProviderThinking(providerId, button) {
    const service = (state.bootstrap?.providers || []).find((item) => item.id === providerId);
    if (!service || button.disabled || providerMutationBusy) return;
    beginProviderMutation();
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      const result = await api("set_provider_thinking", providerId, !service.thinking_enabled);
      providerTests.invalidate(providerId);
      applyProviderServices(state.bootstrap.providers.map((item) => item.id === result.id ? {
        ...item, thinking_enabled: result.thinking_enabled,
        thinking_supported: result.thinking_supported ?? item.thinking_supported,
      } : item));
      await refreshProviderServices("思考模式已保存");
    } catch (error) {
      reportError(error);
      renderProviderServices();
    } finally { finishProviderMutation(); }
  }

  function deleteSelectedProvider() {
    deleteProvider($("#providerForm").elements.provider_id.value);
  }

  return {
    applyProviderPreset,
    beginNewProvider,
    bindProviderModelCombobox,
    clearProviderModels,
    clearProviderTest,
    deleteProvider,
    deleteSelectedProvider,
    discoverProviderModels,
    editProvider,
    guardProviderDraft,
    isProviderSaving: () => providerSaveBusy || providerMutationBusy || providerReadBusy || providerTests.isBusy(),
    inferProviderModalities,
    providerCapabilityLabel,
    providerThinkingSupported,
    reloadProviderServices,
    renderProviderServices,
    requestProviderOverview,
    saveProvider,
    setProviderDefault,
    setProviderThinking,
    showProviderOverview,
    syncProviderServiceKind,
    syncProviderThinkingControl,
    testSelectedProvider,
    testProviderCard: providerTests.testProviderCard,
    testAllProviders: providerTests.testAllProviders,
    setDefaultEmbeddingProvider,
    updateProviderDirty,
  };
};
