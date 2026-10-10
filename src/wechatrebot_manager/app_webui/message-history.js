/* Message history controller; the shell supplies shared services explicitly. */
window.createMessageHistory = function (helpers) {
  "use strict";
  const {
    $,
    $$,
    HISTORY_MESSAGE_CACHE_LIMIT,
    HISTORY_MESSAGE_CACHE_TTL_MS,
    HISTORY_MESSAGE_WINDOW_LIMIT,
    HISTORY_PAGE_SIZE,
    HISTORY_PRELOAD_THRESHOLD,
    api,
    avatarMarkup,
    bindAvatarFallbacks,
    confirmAction,
    copyText,
    escapeHtml,
    fullTime,
    reportError,
    state,
    toast,
  } = helpers;

  async function requestHistoryRead(request) {
    let timer;
    try {
      return await Promise.race([
        request,
        new Promise((_, reject) => {
          timer = window.setTimeout(() => reject(new Error("读取记录超时，请点击刷新。")), 20000);
        }),
      ]);
    } finally { window.clearTimeout(timer); }
  }

  async function refreshHistoryRecords() {
    if (!state.historyAccount || state.historyManualRefresh) return;
    state.historyManualRefresh = true;
    clearHistoryMessageCache();
    state.messageLoad += 1;
    state.historyOlderLoad += 1;
    state.historyLoadingOlder = false;
    $("#olderMessagesStatus").hidden = true;
    $("#messageList").removeAttribute("aria-busy");
    setHistoryActionsEnabled(false);
    try {
      await loadConversations({forceRefresh: true, refreshGroups: true, latest: true});
    } finally {
      state.historyManualRefresh = false;
      setHistoryActionsEnabled(state.historyReadyAccount === state.historyAccount);
    }
  }

  async function loadConversations(options = {}) {
    const silent = options?.silent === true;
    const requestAccount = state.historyAccount;
    if (!requestAccount) {
      invalidateHistoryState();
      renderHistoryAccountGate("");
      return;
    }
    const load = ++state.historyLoad;
    const filters = historyFilters();
    const query = JSON.stringify([requestAccount, filters]);
    const cached = state.historyConversationSnapshot;
    const reuse = options?.localFilter === true && cached?.query === query;
    state.historyConversationsLoading = true;
    try {
      const data = reuse ? cached.data : await requestHistoryRead(api("get_conversations", {
        account_id: requestAccount,
        kind: "all",
        ...filters,
        refresh_groups: options?.refreshGroups === true,
      }));
      if (load !== state.historyLoad || requestAccount !== state.historyAccount) return;
      if (data.requires_login || data.account_changed || data.account_id !== requestAccount) {
        invalidateHistoryState();
        renderHistoryAccountGate(data.requires_login ? "" : state.historyAccount);
        return;
      }
      saveSelectedHistoryMessages();
      state.historyConversationSnapshot = {query, data};
      state.conversations = (data.items || []).filter((item) => state.historyKind === "all" || item.type === state.historyKind);
      state.historyReadyAccount = requestAccount;
      setHistoryActionsEnabled(data.offline === true || state.status?.history_offline === true || state.status?.runtime?.account_status_available !== false);
      if (state.selectedConversation) {
        const refreshed = state.conversations.find((item) => item.conversation_id === state.selectedConversation.conversation_id);
        state.selectedConversation = refreshed || null;
      }
      const summary = data.summary || {};
      const visible = state.conversations.length;
      const visibleMessages = state.conversations.reduce((sum, item) => sum + (item.message_count || 0), 0);
      $("#historySummary").textContent = `${data.offline ? "离线记录 · " : ""}${visible} 个聊天 · ${visibleMessages} 条消息 · 个人消息 ${state.conversations.filter((item) => item.type === "private").reduce((sum, item) => sum + (item.message_count || 0), 0)} · 群聊消息 ${state.conversations.filter((item) => item.type === "group").reduce((sum, item) => sum + (item.message_count || 0), 0)} · 群目录 ${summary.directory_groups || 0}`;
      renderConversations();
      if (state.selectedConversation) {
        await selectConversation(state.selectedConversation.conversation_id, {silent, cachedOnly: reuse, latest: options.latest, forceRefresh: options.forceRefresh});
      } else if (state.conversations.length) {
        await selectConversation(state.conversations[0].conversation_id, {silent: true, cachedOnly: reuse, latest: options.latest, forceRefresh: options.forceRefresh});
      } else {
        ++state.messageLoad;
        state.historyMessagesLoading = false;
        state.historyOlderLoad += 1;
        state.historyHasMore = false;
        state.historyLoadingOlder = false;
        state.historyMessageQuery = "";
        state.selectedMessages = [];
        state.selectedSenderKey = "";
        $("#conversationHeader").textContent = "没有匹配的聊天";
        $("#messageList").removeAttribute("aria-busy");
        $("#messageList").innerHTML = '<div class="empty-state compact"><h2>没有找到匹配消息</h2><p>调整昵称、微信号或日期后再试。</p></div>';
        $("#olderMessagesStatus").hidden = true;
        $("#conversationInspector").innerHTML = '<span class="eyebrow">聊天信息</span><p class="subtle">当前筛选没有匹配的聊天。</p>';
        $("#newMessageButton").hidden = true;
      }
    } catch (error) { if (!silent) reportError(error); }
    finally {
      if (load === state.historyLoad) state.historyConversationsLoading = false;
    }
  }

  function canEditHistory(accountId = state.historyAccount) {
    const runtime = state.status?.runtime || {};
    return Boolean(accountId && accountId === state.historyAccount && accountId === state.historyReadyAccount
      && state.status?.history_offline !== true && runtime.account_status_available !== false
      && String(runtime.wechat_identity || "").trim() === accountId);
  }

  function setHistoryActionsEnabled(enabled) {
    const exportButton = $("#exportHistory");
    if (exportButton) exportButton.disabled = !enabled;
    for (const id of ["#cleanHistory", "#deleteConversation"]) {
      const button = $(id);
      if (button) button.disabled = !enabled || !canEditHistory();
    }
    const refreshButton = $("#refreshHistory");
    if (refreshButton) {
      refreshButton.disabled = !state.historyAccount || (state.status?.runtime?.account_status_available === false && !state.status?.history_offline) || state.historyManualRefresh;
      refreshButton.textContent = state.historyManualRefresh ? "刷新中…" : "刷新";
    }
  }

  function invalidateHistoryState() {
    clearHistoryMessageCache();
    state.historyLoad += 1;
    state.historyConversationsLoading = false;
    state.messageLoad += 1;
    state.historyMessagesLoading = false;
    state.historyOlderLoad += 1;
    state.conversations = [];
    state.selectedConversation = null;
    state.selectedMessages = [];
    state.selectedSenderKey = "";
    state.historyHasMore = false;
    state.historyHasNewer = false;
    state.historyLoadingOlder = false;
    state.historyMessageQuery = "";
    state.historyReadyAccount = "";
    state.historyConversationSnapshot = null;
  }

  function renderHistoryAccountGate(accountId) {
    const pending = Boolean(String(accountId || "").trim());
    const title = pending ? "正在确认当前微信账号" : "登录后查看聊天记录";
    const message = pending
      ? "账号切换完成后，将只显示这个机器人微信号的聊天记录。"
      : "聊天记录只展示当前机器人微信账号的内容。";
    $("#historySummary").textContent = pending ? "正在读取当前账号的聊天记录…" : "请先登录机器人微信账号";
    $("#conversationList").innerHTML = `<div class="empty-state compact"><div class="empty-symbol">空</div><h2>${title}</h2><p>${message}</p></div>`;
    $("#conversationHeader").textContent = pending ? "正在读取聊天记录…" : "尚未登录微信";
    $("#messageList").removeAttribute("aria-busy");
    $("#messageList").innerHTML = `<div class="empty-state compact"><h2>${title}</h2><p>${message}</p></div>`;
    $("#olderMessagesStatus").hidden = true;
    $("#conversationInspector").innerHTML = `<span class="eyebrow">聊天信息</span><p class="subtle">${message}</p>`;
    $("#newMessageButton").hidden = true;
    setHistoryActionsEnabled(false);
  }

  function historyFilters() {
    return {
      search: $("#historySearch").value,
      start: $("#historyStart").value,
      end: $("#historyEnd").value,
    };
  }

  function pagedHistoryFilters(before = null) {
    return {
      account_id: state.historyAccount,
      ...historyFilters(),
      paged: true,
      page_size: HISTORY_PAGE_SIZE,
      ...(before ? {before: {id: before.id, timestamp: before.timestamp}} : {}),
    };
  }

  function normalizeMessagePage(value) {
    if (Array.isArray(value)) {
      return {items: value.slice(-HISTORY_PAGE_SIZE), has_more: value.length > HISTORY_PAGE_SIZE, account_id: ""};
    }
    return {
      items: Array.isArray(value?.items) ? value.items : [],
      has_more: Boolean(value?.has_more),
      account_id: String(value?.account_id || "").trim(),
      requires_login: Boolean(value?.requires_login),
      account_changed: Boolean(value?.account_changed),
    };
  }

  function mergeMessages(...pages) {
    const messages = new Map();
    pages.flat().forEach((message) => messages.set(String(message.id), message));
    return [...messages.values()].sort((left, right) => (
      Number(left.timestamp || 0) - Number(right.timestamp || 0)
      || Number(left.id || 0) - Number(right.id || 0)
    ));
  }

  function historyMessageCacheKey(accountId, conversationId, query) {
    return JSON.stringify([String(accountId || ""), String(conversationId || ""), String(query || "")]);
  }

  function getHistoryMessageCacheEntry(key) {
    const entry = state.historyMessageCache.get(key);
    if (!entry) return null;
    state.historyMessageCache.delete(key);
    state.historyMessageCache.set(key, entry);
    return entry;
  }

  function setHistoryMessageCacheEntry(key, entry) {
    state.historyMessageCache.delete(key);
    state.historyMessageCache.set(key, entry);
    while (state.historyMessageCache.size > HISTORY_MESSAGE_CACHE_LIMIT) {
      const oldest = state.historyMessageCache.keys().next().value;
      state.historyMessageCache.delete(oldest);
    }
  }

  function saveSelectedHistoryMessages() {
    const selected = state.selectedConversation;
    if (!selected || !state.historyAccount || !state.historyMessageQuery) return;
    const key = historyMessageCacheKey(state.historyAccount, selected.conversation_id, state.historyMessageQuery);
    const previous = state.historyMessageCache.get(key);
    if (!previous && !state.selectedMessages.length) return;
    setHistoryMessageCacheEntry(key, {
      accountId: state.historyAccount,
      conversationId: selected.conversation_id,
      query: state.historyMessageQuery,
      messages: state.selectedMessages.slice(),
      hasMore: state.historyHasMore,
      hasNewer: state.historyHasNewer,
      scrollTop: $("#messageList").scrollTop,
      senderKey: state.selectedSenderKey,
      fetchedAt: previous?.fetchedAt || 0,
      revision: previous?.revision || "",
    });
  }

  function updateHistoryMessageScrollPosition() {
    const selected = state.selectedConversation;
    if (!selected || !state.historyAccount || !state.historyMessageQuery) return;
    const key = historyMessageCacheKey(state.historyAccount, selected.conversation_id, state.historyMessageQuery);
    const entry = state.historyMessageCache.get(key);
    if (entry) entry.scrollTop = $("#messageList").scrollTop;
  }

  function clearHistoryMessageCache() {
    state.historyMessageCacheEpoch += 1;
    state.historyMessageCache.clear();
    state.historyMessageRequests.clear();
  }

  function showHistoryNewerMessages() {
    const button = $("#newMessageButton");
    button.textContent = "回到最新消息";
    button.hidden = false;
  }

  function requestHistoryMessagePage(key, conversationId, filters) {
    const pending = state.historyMessageRequests.get(key);
    if (pending) return pending;
    const request = requestHistoryRead(api("get_messages", conversationId, filters)).then(normalizeMessagePage);
    state.historyMessageRequests.set(key, request);
    request.then(
      () => { if (state.historyMessageRequests.get(key) === request) state.historyMessageRequests.delete(key); },
      () => { if (state.historyMessageRequests.get(key) === request) state.historyMessageRequests.delete(key); },
    );
    return request;
  }

  function renderConversations() {
    const list = $("#conversationList");
    const previousScroll = list.scrollTop;
    list.innerHTML = state.conversations.length ? state.conversations.map((item) => `
      <button class="conversation-item ${state.selectedConversation?.conversation_id === item.conversation_id ? "active" : ""}" data-conversation="${escapeHtml(item.conversation_id)}" data-type="${escapeHtml(item.type)}">
        ${avatarMarkup("conversation-avatar", item.display_name, item.avatar)}
        <span class="conversation-main"><span class="conversation-line"><strong>${escapeHtml(item.display_name)}</strong><time>${item.last_timestamp ? escapeHtml(new Date(item.last_timestamp * 1000).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", hour12: false})) : "—"}</time></span><small class="conversation-id">${escapeHtml(item.conversation_id)}</small><small class="conversation-preview">${escapeHtml(item.last_content ? displayMessageContent(item.last_content) : (item.message_count ? `${item.message_count} 条本机消息` : "暂无本机消息"))}</small></span>
      </button>`).join("") : '<div class="empty-state compact"><div class="empty-symbol">空</div><h2>没有找到聊天记录</h2><p>调整筛选条件或启动机器人接收新消息。</p></div>';
    $$(".conversation-item", list).forEach((button) => button.addEventListener("click", () => selectConversation(button.dataset.conversation)));
    bindAvatarFallbacks(list);
    list.scrollTop = previousScroll;
  }

  function renderConversationHeader(item) {
    $("#conversationHeader").innerHTML = `<div class="conversation-header-main">${avatarMarkup("conversation-avatar", item.display_name, item.avatar)}<div><strong>${escapeHtml(item.display_name)}</strong><span>${escapeHtml(item.conversation_id)}</span></div></div><span class="status-badge online"><span></span>${item.message_count} 条消息${item.message_count ? " · 自动更新中" : " · 暂无消息"}</span>`;
    bindAvatarFallbacks($("#conversationHeader"));
  }

  function messagesSignature(messages) {
    return JSON.stringify(messages.map((message) => [
      message.id, message.timestamp, message.role, message.sender_id,
      message.sender_name, message.group_nickname, message.wechat_name, message.wechat_id,
      message.avatar, message.content, message.message_kind,
      message.quote_sender, message.quote_content,
    ]));
  }

  function xmlFragment(content, tag) {
    const match = content.match(new RegExp(`<${tag}\\b[^>]*>([\\s\\S]*?)<\\/${tag}>`, "i"));
    return match ? match[1] : "";
  }

  function xmlText(content, tag, limit = 80) {
    let text = xmlFragment(content, tag).trim();
    if (text.startsWith("<![CDATA[") && text.endsWith("]]>")) text = text.slice(9, -3);
    if (/[<>]/.test(text)) return "";
    const decoded = text
      .replace(/&amp;/gi, "&")
      .replace(/&quot;/gi, '"')
      .replace(/&apos;/gi, "'");
    return /[<>]/.test(decoded) ? "" : decoded.replace(/\s+/g, " ").trim().slice(0, limit);
  }

  function quotedMessageContent(refermsg) {
    const labels = {3: "[图片]", 34: "[语音]", 42: "[名片]", 43: "[视频]", 47: "[表情]", 48: "[位置]", 49: "[卡片消息]"};
    const quoteType = xmlText(refermsg, "type");
    return labels[quoteType] || xmlText(refermsg, "content", 160) || "原消息不可用";
  }

  function rawMessagePresentation(value) {
    const content = String(value ?? "");
    const structuredXml = /^\s*(?:<\?xml[^>]*>\s*)?<(?:msg|appmsg|sysmsg)\b/i;
    const plain = {content, messageKind: "", quoteSender: "", quoteContent: "", preview: content};
    if (!structuredXml.test(content)) {
      if (/^\s*\[链接卡片\]/.test(content)) {
        const readable = content.replace(/https?:\/\/\S+/gi, "").replace(/\s*[·|]\s*$/g, "").replace(/\s+/g, " ").trim();
        return {...plain, content: readable || "[链接卡片]", preview: readable || "[链接卡片]"};
      }
      return plain;
    }
    const folded = content.toLowerCase();
    const summary = (text) => ({...plain, content: text, preview: text});
    if (/<revokemsg\b/i.test(content)) {
      const notice = xmlText(content, "replacemsg", 160) || "消息已撤回";
      return {...plain, content: notice, messageKind: "revoke", preview: `[撤回] ${notice}`};
    }
    if (/<appmsg\b/i.test(content)) {
      const withTitle = (label) => {
        const title = xmlText(content, "title");
        return summary(title ? `${label} ${title}` : label);
      };
      const appType = xmlText(content, "type");
      if (appType === "57" || folded.includes("<refermsg")) {
        const reply = xmlText(content, "title", 160) || "回复了一条消息";
        const refermsg = xmlFragment(content, "refermsg");
        const quoteSender = xmlText(refermsg, "displayname");
        const quoteContent = quotedMessageContent(refermsg);
        const attribution = quoteSender ? `引用 ${quoteSender}` : "引用消息";
        return {content: reply, messageKind: "quote", quoteSender, quoteContent, preview: `${reply} · ${attribution}：${quoteContent}`};
      }
      if (appType === "6") return withTitle("[文件]");
      if (appType === "5") return withTitle("[链接卡片]");
      if (["33", "36"].includes(appType)) return withTitle("[小程序]");
      if (appType === "19" || folded.includes("<recorditem")) return withTitle("[聊天记录]");
      return withTitle("[卡片消息]");
    }
    if (/<emoji\b/i.test(content)) return summary("[表情]");
    if (/<img\b/i.test(content)) return summary("[图片]");
    if (/<voicemsg\b/i.test(content)) return summary("[语音]");
    if (/<videomsg\b/i.test(content)) return summary("[视频]");
    if (/<location\b/i.test(content)) return summary("[位置]");
    return summary("[特殊消息]");
  }

  function messagePresentation(message) {
    if (message?.message_kind === "quote") {
      const content = String(message.content || "").trim() || "回复了一条消息";
      const quoteSender = String(message.quote_sender || "").trim();
      const quoteContent = String(message.quote_content || "").trim() || "原消息不可用";
      const attribution = quoteSender ? `引用 ${quoteSender}` : "引用消息";
      return {content, messageKind: "quote", quoteSender, quoteContent, preview: `${content} · ${attribution}：${quoteContent}`};
    }
    if (message?.message_kind === "revoke") {
      const content = String(message.content || "").trim() || "消息已撤回";
      return {content, messageKind: "revoke", quoteSender: "", quoteContent: "", preview: `[撤回] ${content}`};
    }
    return rawMessagePresentation(message?.content);
  }

  function displayMessageContent(value) {
    return rawMessagePresentation(value).preview;
  }

  function messageSenderKey(message) {
    if (!message || message.role === "assistant") return "";
    return String(message.sender_id || message.wechat_id || message.sender_name || message.group_nickname || "").trim();
  }

  function messageSenderName(message) {
    return message?.group_nickname || message?.wechat_name || message?.sender_name || message?.sender_id || "用户";
  }

  function messageSenderIdentity(message) {
    return message?.wechat_id || message?.sender_id || "";
  }

  function messageBubbleMarkup(message) {
    const presentation = messagePresentation(message);
    if (presentation.messageKind === "quote") {
      const attribution = presentation.quoteSender ? `引用 · ${presentation.quoteSender}` : "引用消息";
      return `<div class="message-bubble message-bubble-quote"><p>${escapeHtml(presentation.content)}</p><blockquote><strong>${escapeHtml(attribution)}</strong><span>${escapeHtml(presentation.quoteContent)}</span></blockquote></div>`;
    }
    if (presentation.messageKind === "revoke") {
      const detail = presentation.content === "消息已撤回" ? "" : `<span>${escapeHtml(presentation.content)}</span>`;
      return `<div class="message-bubble message-bubble-notice" role="note"><strong>消息已撤回</strong>${detail}</div>`;
    }
    return `<div class="message-bubble">${escapeHtml(presentation.content)}</div>`;
  }

  function messageMarkup(message) {
    const assistant = message.role === "assistant";
    const name = assistant ? (message.sender_name || "机器人") : messageSenderName(message);
    const identity = !assistant ? messageSenderIdentity(message) : "";
    const senderKey = messageSenderKey(message);
    const selected = !assistant && senderKey && senderKey === state.selectedSenderKey;
    const avatar = assistant || !senderKey
      ? avatarMarkup("message-avatar", name, message.avatar)
      : `<button class="message-avatar-button${selected ? " selected" : ""}" type="button" data-sender-key="${escapeHtml(senderKey)}" aria-label="选中消息发送者：${escapeHtml(name)}" aria-pressed="${selected ? "true" : "false"}">${avatarMarkup("message-avatar", name, message.avatar)}</button>`;
    return `<article class="message ${assistant ? "assistant" : ""}">${avatar}<div class="message-content"><div class="message-meta"><span class="message-author"><span class="message-author-name">${escapeHtml(name)}</span>${identity ? `<small class="message-author-id">${escapeHtml(identity)}</small>` : ""}</span><time>${escapeHtml(fullTime(message.timestamp))}</time></div>${messageBubbleMarkup(message)}</div></article>`;
  }

  function updateSelectedSenderButtons() {
    $$(".message-avatar-button", $("#messageList")).forEach((button) => {
      const selected = Boolean(state.selectedSenderKey && button.dataset.senderKey === state.selectedSenderKey);
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }

  function selectMessageSender(senderKey) {
    if (!senderKey) return;
    const sender = [...state.selectedMessages].reverse().find((message) => messageSenderKey(message) === senderKey);
    if (!sender) return;
    state.selectedSenderKey = senderKey;
    saveSelectedHistoryMessages();
    updateSelectedSenderButtons();
    if (state.selectedConversation) renderConversationInspector(state.selectedConversation);
  }

  function bindMessageSenderSelection(root = $("#messageList")) {
    $$(".message-avatar-button", root).forEach((button) => {
      if (button.dataset.senderSelectionBound === "true") return;
      button.dataset.senderSelectionBound = "true";
      button.addEventListener("click", () => selectMessageSender(button.dataset.senderKey || ""));
    });
  }

  function renderMessages(messages, {forceBottom = false, nearBottom = true, restoreScrollTop = null} = {}) {
    const list = $("#messageList");
    const previousIds = new Set(state.selectedMessages.map((message) => message.id));
    const added = messages.filter((message) => !previousIds.has(message.id)).length;
    const previousScroll = list.scrollTop;
    list.innerHTML = messages.length ? messages.map(messageMarkup).join("") : '<div class="empty-state compact"><p>这个聊天暂无消息；群聊仍会保留在左侧目录中。</p></div>';
    bindAvatarFallbacks(list);
    state.selectedMessages = messages;
    bindMessageSenderSelection(list);
    updateSelectedSenderButtons();
    const newButton = $("#newMessageButton");
    if (restoreScrollTop !== null && restoreScrollTop !== undefined) {
      list.scrollTop = Math.max(0, Math.min(Number(restoreScrollTop) || 0, list.scrollHeight - list.clientHeight));
      newButton.hidden = true;
    } else if (forceBottom || nearBottom) {
      list.scrollTop = list.scrollHeight;
      newButton.hidden = true;
    } else {
      list.scrollTop = previousScroll;
      if (added > 0) {
        newButton.textContent = `${added} 条新消息 · 查看`;
        newButton.hidden = false;
      }
    }
  }

  function prependOlderMessages(messages) {
    const existing = new Set(state.selectedMessages.map((message) => String(message.id)));
    const older = messages.filter((message) => !existing.has(String(message.id)));
    if (!older.length) return;
    const list = $("#messageList");
    const previousHeight = list.scrollHeight;
    const previousScroll = list.scrollTop;
    list.insertAdjacentHTML("afterbegin", older.map(messageMarkup).join(""));
    bindAvatarFallbacks(list);
    list.scrollTop = previousScroll + (list.scrollHeight - previousHeight);
    state.selectedMessages = mergeMessages(older, state.selectedMessages);
    if (state.selectedMessages.length > HISTORY_MESSAGE_WINDOW_LIMIT) {
      state.selectedMessages = state.selectedMessages.slice(0, HISTORY_MESSAGE_WINDOW_LIMIT);
      while (list.children.length > HISTORY_MESSAGE_WINDOW_LIMIT) list.lastElementChild.remove();
      state.historyHasNewer = true;
      showHistoryNewerMessages();
    }
    bindMessageSenderSelection(list);
    updateSelectedSenderButtons();
    saveSelectedHistoryMessages();
  }

  async function selectConversation(conversationId, options = {}) {
    const item = state.conversations.find((value) => value.conversation_id === conversationId);
    if (!item) return;
    const silent = options?.silent === true;
    const latest = options?.latest === true;
    const query = JSON.stringify(historyFilters());
    const changed = state.selectedConversation?.conversation_id !== conversationId || state.historyMessageQuery !== query;
    const list = $("#messageList");
    if (changed) saveSelectedHistoryMessages();
    state.selectedConversation = item;
    const requestAccount = state.historyAccount;
    const key = historyMessageCacheKey(requestAccount, conversationId, query);
    const cacheEntry = getHistoryMessageCacheEntry(key);
    if (changed) {
      state.selectedMessages = cacheEntry?.messages.slice() || [];
      state.selectedSenderKey = cacheEntry?.senderKey || "";
      state.historyHasMore = cacheEntry?.hasMore || false;
      state.historyHasNewer = cacheEntry?.hasNewer || false;
      state.historyLoadingOlder = false;
      state.historyMessageQuery = query;
      state.historyOlderLoad += 1;
      list.removeAttribute("aria-busy");
      $("#olderMessagesStatus").hidden = true;
    }
    renderConversations();
    renderConversationHeader(item);
    $("#newMessageButton").hidden = true;
    if (changed && cacheEntry) {
      renderMessages(state.selectedMessages, {restoreScrollTop: cacheEntry.scrollTop});
      renderConversationInspector(item);
    } else if (changed) {
      list.innerHTML = '<div class="loading-row message-read-status"><span>正在读取消息…</span><small>若长时间未读到记录，点击刷新。</small></div>';
    }
    const load = ++state.messageLoad;
    const revision = JSON.stringify([item.message_count, item.last_timestamp]);
    const fresh = cacheEntry && Date.now() - cacheEntry.fetchedAt < HISTORY_MESSAGE_CACHE_TTL_MS
      && cacheEntry.revision === revision;
    if (!latest && (state.historyHasNewer || (options.cachedOnly && (cacheEntry || !changed))
        || (fresh && !options.forceRefresh))) {
      state.historyMessagesLoading = false;
      renderConversationInspector(item);
      if (state.historyHasNewer) showHistoryNewerMessages();
      return;
    }
    state.historyMessagesLoading = true;
    const cacheEpoch = state.historyMessageCacheEpoch;
    const filters = pagedHistoryFilters();
    try {
      const page = await requestHistoryMessagePage(key, conversationId, filters);
      if (requestAccount !== state.historyAccount || cacheEpoch !== state.historyMessageCacheEpoch) return;
      if (page.requires_login || page.account_changed || page.account_id !== requestAccount) {
        invalidateHistoryState();
        renderHistoryAccountGate(page.requires_login ? "" : state.historyAccount);
        return;
      }
      const previous = getHistoryMessageCacheEntry(key);
      const current = load === state.messageLoad
        && state.selectedConversation?.conversation_id === conversationId
        && state.historyMessageQuery === query;
      const priorMessages = previous?.messages || (current ? state.selectedMessages : []);
      const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
      const overlaps = !priorMessages.length || page.items.some((message) => priorMessages.some((prior) => prior.id === message.id));
      let messages = latest || !overlaps ? page.items : mergeMessages(priorMessages, page.items);
      let hasMore = latest || !overlaps || priorMessages.length <= page.items.length ? page.has_more : (previous?.hasMore ?? state.historyHasMore);
      // Do not insert a disconnected latest page into an older window being read.
      if (current && !latest && priorMessages.length && page.items.length && !nearBottom
          && (!overlaps || messages.length > HISTORY_MESSAGE_WINDOW_LIMIT)) {
        state.historyHasNewer = true;
        saveSelectedHistoryMessages();
        showHistoryNewerMessages();
        return;
      }
      if (messages.length > HISTORY_MESSAGE_WINDOW_LIMIT) {
        messages = messages.slice(-HISTORY_MESSAGE_WINDOW_LIMIT);
        hasMore = true;
      }
      const scrollTop = current ? list.scrollTop : (previous?.scrollTop || cacheEntry?.scrollTop || 0);
      const senderKey = current ? state.selectedSenderKey : (previous?.senderKey || cacheEntry?.senderKey || "");
      setHistoryMessageCacheEntry(key, {
        accountId: requestAccount,
        conversationId,
        query,
        messages,
        hasMore,
        hasNewer: false,
        scrollTop,
        senderKey,
        fetchedAt: Date.now(),
        revision,
      });
      if (current) {
        state.historyHasMore = hasMore;
        state.historyHasNewer = false;
        if (latest || (changed && !cacheEntry) || messagesSignature(messages) !== messagesSignature(state.selectedMessages)) {
          renderMessages(messages, {nearBottom, forceBottom: latest});
        }
        renderConversationInspector(item);
      }
    } catch (error) {
      if (changed && !cacheEntry && load === state.messageLoad && requestAccount === state.historyAccount
          && state.selectedConversation?.conversation_id === conversationId) {
        list.removeAttribute("aria-busy");
        list.innerHTML = '<div class="empty-state compact"><h2>暂时无法读取消息</h2><p>请点击刷新重试。</p></div>';
      }
      if (!silent && !cacheEntry && load === state.messageLoad) reportError(error);
    } finally {
      if (load === state.messageLoad) {
        state.historyMessagesLoading = false;
        if (state.historyHasNewer) showHistoryNewerMessages();
      }
    }
  }

  async function loadOlderMessages() {
    const list = $("#messageList");
    const selected = state.selectedConversation;
    const oldest = state.selectedMessages[0];
    if (!selected || !oldest || !state.historyHasMore || state.historyLoadingOlder || state.historyMessagesLoading || list.scrollTop > HISTORY_PRELOAD_THRESHOLD) return;
    state.historyLoadingOlder = true;
    const request = ++state.historyOlderLoad;
    const messageGeneration = state.messageLoad;
    const requestAccount = state.historyAccount;
    const status = $("#olderMessagesStatus");
    list.setAttribute("aria-busy", "true");
    const feedbackTimer = window.setTimeout(() => {
      if (request === state.historyOlderLoad) status.hidden = false;
    }, 300);
    try {
      const page = normalizeMessagePage(await requestHistoryRead(api("get_messages", selected.conversation_id, pagedHistoryFilters(oldest))));
      if (request !== state.historyOlderLoad || messageGeneration !== state.messageLoad || requestAccount !== state.historyAccount || state.selectedConversation?.conversation_id !== selected.conversation_id) return;
      if (page.requires_login || page.account_changed || page.account_id !== requestAccount) {
        invalidateHistoryState();
        renderHistoryAccountGate(page.requires_login ? "" : state.historyAccount);
        return;
      }
      state.historyHasMore = page.has_more;
      prependOlderMessages(page.items);
    } catch (error) {
      if (request === state.historyOlderLoad) reportError(error);
    } finally {
      window.clearTimeout(feedbackTimer);
      if (request === state.historyOlderLoad) {
        state.historyLoadingOlder = false;
        status.hidden = true;
        list.removeAttribute("aria-busy");
      }
    }
  }

  async function pollHistory() {
    if (state.activePage !== "history" || state.historyPolling || state.historyLoadingOlder
        || state.historyConversationsLoading || state.historyMessagesLoading) return;
    state.historyPolling = true;
    try {
      await loadConversations({silent: true});
    } finally { state.historyPolling = false; }
  }

  function renderConversationInspector(item) {
    const latestSender = [...state.selectedMessages].reverse().find((message) => message.role !== "assistant");
    const selectedSender = state.selectedSenderKey
      ? [...state.selectedMessages].reverse().find((message) => messageSenderKey(message) === state.selectedSenderKey)
      : null;
    const currentSender = selectedSender || latestSender;
    const senderName = currentSender ? messageSenderName(currentSender) : "尚无发送者";
    const senderId = currentSender ? (messageSenderIdentity(currentSender) || "标识未提供") : "标识未提供";
    const identityFacts = item.type === "group"
      ? `<div><dt>群昵称</dt><dd>${escapeHtml(senderName)}</dd></div><div><dt>微信号</dt><dd>${escapeHtml(senderId)}</dd></div>`
      : `<div><dt>微信号</dt><dd>${escapeHtml(senderId)}</dd></div>`;
    $("#conversationInspector").innerHTML = `<span class="eyebrow">聊天信息</span><div class="inspector-conversation-title">${avatarMarkup("conversation-avatar", item.display_name, item.avatar)}<div><h2>${escapeHtml(item.display_name)}</h2><p>${escapeHtml(item.conversation_id)}</p></div></div><section class="current-sender"><span>当前消息发送者</span><div>${avatarMarkup("message-avatar", senderName, currentSender?.avatar)}<div><strong>${escapeHtml(senderName)}</strong><small>${escapeHtml(senderId)}</small></div><button class="row-action" type="button" id="copySenderId">复制${currentSender?.wechat_id && currentSender.wechat_id !== currentSender.sender_id ? "微信号" : "微信标识"}</button></div></section><dl class="inspector-list">${identityFacts}<div><dt>该发送者消息</dt><dd>${Number.isInteger(currentSender?.sender_message_count) ? `${currentSender.sender_message_count} 条` : "未统计"}</dd></div><div><dt>该发送者最近消息</dt><dd>${escapeHtml(fullTime(currentSender?.sender_last_timestamp))}</dd></div><div><dt>本会话消息</dt><dd>${item.message_count} 条</dd></div></dl>${item.message_count ? '<div class="inspector-actions"><button class="button danger-ghost small" id="deleteConversation">删除这段记录</button></div>' : '<p class="subtle">群聊当前没有本机消息副本，因此无需清理。</p>'}`;
    $("#copySenderId")?.addEventListener("click", () => copyText(senderId, currentSender?.wechat_id && currentSender.wechat_id !== currentSender.sender_id ? "微信号" : "微信标识"));
    bindAvatarFallbacks($("#conversationInspector"));
    if ($("#deleteConversation")) $("#deleteConversation").disabled = !canEditHistory();
    $("#deleteConversation")?.addEventListener("click", () => {
      const accountId = state.historyAccount;
      if (!canEditHistory(accountId)) return;
      confirmAction({
        title: "删除这段聊天记录？", message: "仅删除管理软件中的本机副本，不会影响微信原始聊天，也不会损伤机器人的记忆数据。", confirmText: "删除记录",
        onConfirm: async () => {
          if (!canEditHistory(accountId)) { toast("请连接原微信账号后再清理记录。"); return; }
          const result = await api("delete_conversation", item.conversation_id, accountId);
          if (result.requires_login || result.account_changed || accountId !== state.historyAccount) {
            invalidateHistoryState();
            renderHistoryAccountGate(state.historyAccount);
            return;
          }
          clearHistoryMessageCache();
          state.messageLoad += 1;
          state.historyMessagesLoading = false;
          state.historyOlderLoad += 1;
          state.historyHasMore = false;
          state.historyLoadingOlder = false;
          state.historyMessageQuery = "";
          state.selectedMessages = [];
          state.selectedConversation = null;
          state.selectedSenderKey = "";
          $("#messageList").innerHTML = '<div class="empty-state compact"><p>请选择聊天记录</p></div>';
          $("#conversationInspector").innerHTML = '<span class="eyebrow">聊天信息</span><h2>尚未选择</h2>';
          await loadConversations(); toast("聊天记录副本已删除。 ");
        },
      });
    });
  }

  return {
    canEditHistory,
    clearHistoryMessageCache,
    invalidateHistoryState,
    loadConversations,
    loadOlderMessages,
    pollHistory,
    refreshHistoryRecords,
    renderHistoryAccountGate,
    selectConversation,
    setHistoryActionsEnabled,
    updateHistoryMessageScrollPosition,
  };
};
