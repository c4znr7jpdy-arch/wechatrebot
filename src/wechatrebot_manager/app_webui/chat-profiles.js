/* Conversation template selection uses the existing core profile API. */
window.createChatProfiles = function (helpers) {
  "use strict";
  const {api, escapeHtml: esc, confirmAction, openCoreTemplates, state} = helpers;
  const request = (method, endpoint, payload) => api("request_robot_core", method, endpoint, payload);
  const privateOrigin = (id) => /:FriendMessage:|:PrivateMessage:/.test(id);

  function bindingKey(id, kind, profiles, chats) {
    const bindings = profiles.bindings || {};
    const candidates = [...new Set([...Object.keys(bindings), ...(chats || []).map((chat) => chat.chat_id)])]
      .filter((key) => key.split(":").at(-1) === id && (kind === "private" ? privateOrigin(key) : !privateOrigin(key)));
    if (kind === "group" && Object.hasOwn(bindings, id)) return id;
    // Private origins cannot safely be invented from a contact ID.
    if (candidates.length > 1) throw new Error("存在多个聊天来源，请先确认当前机器人连接。");
    return candidates[0] || (kind === "group" ? id : "");
  }

  async function mount(container, id, kind, account) {
    if (!container || !account) return;
    const current = () => container.isConnected && account === (kind === "group" ? state.groupAccount : state.contactAccount)
      && id === (kind === "group" ? state.selectedGroup : state.selectedContact);
    container.innerHTML = '<p class="subtle" role="status">正在读取配置方案…</p>';
    try {
      const [profiles, chats, statuses] = await Promise.all([
        request("GET", "profiles"), request("GET", "chats"), request("GET", "chat_status").catch(() => null),
      ]);
      if (!current()) return;
      const key = bindingKey(id, kind, profiles, chats);
      const saved = profiles.bindings?.[key] || "";
      const templates = profiles.templates || [];
      const missing = saved && !templates.some((template) => template.id === saved);
      const status = (statuses || []).find((item) => item.chat_id === key);
      const decision = status?.last_decision;
      container.innerHTML = `<label class="chat-profile-field"><span>配置方案</span><select aria-label="配置方案" ${!key || missing ? "disabled" : ""}><option value="">全局默认</option>${templates.map((template) => `<option value="${esc(template.id)}" ${template.id === saved ? "selected" : ""}>${esc(template.name)}</option>`).join("")}${missing ? `<option value="${esc(saved)}" selected>原配置方案暂不可用</option>` : ""}<option value="__manage_templates__">管理配置模板…</option></select></label><p class="chat-profile-note" role="status">${!key ? "此联系人尚无对话记录，使用全局默认；产生消息后可绑定模板。" : missing ? "保留原绑定，请恢复连接后重新读取。" : "对话参数跟随所选方案；切换时确认应用。"}</p><p class="chat-decision">最近决策：${decision ? `${decision.should_reply ? "回复" : "不回复"} · ${esc(decision.summary || "暂无说明")}` : "暂无可用记录"}</p>`;
      const select = container.querySelector("select");
      const note = container.querySelector('[role="status"]');
      let binding = saved;
      select.addEventListener("change", () => {
        const next = select.value;
        select.value = binding;
        if (next === "__manage_templates__") { void openCoreTemplates(); return; }
        if (next === binding || !current()) return;
        confirmAction({title: "应用配置方案？", message: "只更新此聊天的对话参数方案，群聊或私聊的开关、功能权限和定时规则保持原样。", confirmText: "应用方案", intent: "primary", onConfirm: async () => {
          if (!current()) throw new Error("聊天或微信账号已变化，请重新选择。");
          select.disabled = true;
          try {
            const latest = await request("GET", "profiles");
            if (!current()) throw new Error("聊天或微信账号已变化，请重新选择。");
            if ((latest.bindings?.[key] || "") !== binding) throw new Error("配置方案已被其他窗口修改，请重新选择聊天后重试。");
            await request("POST", "bindings/set", {chat_id: key, template_id: next});
            const verified = await request("GET", "profiles");
            if ((verified.bindings?.[key] || "") !== next) throw new Error("未确认方案生效，请重新读取后核对。");
            if (!current()) return;
            binding = next; select.value = binding;
            note.textContent = "配置方案已应用并回读确认。";
          } catch (error) {
            if (current()) note.textContent = error.message || "应用失败，请重新读取后核对。";
            throw error;
          } finally { if (current()) select.disabled = false; }
        }});
      });
    } catch (error) {
      if (!current()) return;
      container.innerHTML = `<p class="subtle" role="status">配置方案暂不可用：${esc(error.message || "请连接机器人后重试")}。聊天规则仍可编辑。</p>`;
    }
  }

  return {mount};
};
