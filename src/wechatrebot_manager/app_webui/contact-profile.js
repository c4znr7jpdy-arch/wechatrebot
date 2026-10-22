window.createContactProfile = function (helpers) {
  "use strict";
  const {api, avatarMarkup, bindAvatarFallbacks, copyText, escapeHtml, state} = helpers;
  let dialog, content, retry, opener, selection = null, generation = 0, loading = false;

  function factsMarkup(contact) {
    return [
      ["昵称", contact.nickname], ["备注", contact.remark],
      ["微信号", contact.alias, true], ["微信标识", contact.user_id, true],
      ["性别", contact.gender], ["地区", [contact.country, contact.province, contact.city].filter(Boolean).join(" · ")],
      ["个性签名", contact.signature],
    ].map(([label, value, code]) => {
      const text = String(value || "").trim();
      if (!text) return "";
      return `<div><dt>${escapeHtml(label)}</dt><dd>${code ? `<code>${escapeHtml(text)}</code>` : escapeHtml(text)}</dd></div>`;
    }).join("");
  }

  function clear() {
    generation += 1;
    loading = false;
    selection = null;
    content.replaceChildren();
    retry.hidden = true;
    const current = opener?.isConnected ? opener : [...document.querySelectorAll("[data-contact-profile]")]
      .find((button) => button.dataset.contactProfile === opener?.dataset.contactProfile);
    current?.focus();
  }

  function initialize() {
    dialog = document.createElement("dialog");
    dialog.className = "contact-profile-dialog";
    dialog.setAttribute("aria-labelledby", "contactProfileTitle");
    dialog.innerHTML = '<header class="contact-profile-heading"><h2 id="contactProfileTitle">好友资料</h2><button class="button ghost small" type="button" data-close aria-label="关闭好友资料">关闭</button></header><div class="contact-profile-content" aria-live="polite"></div><footer class="contact-profile-actions"><button class="button secondary small" type="button" data-retry hidden>重新查询</button></footer>';
    document.body.append(dialog);
    content = dialog.querySelector(".contact-profile-content");
    retry = dialog.querySelector("[data-retry]");
    dialog.querySelector("[data-close]").addEventListener("click", close);
    retry.addEventListener("click", () => void read());
    dialog.addEventListener("close", () => { if (!dialog.open) clear(); });
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) close();
    });
  }

  function close() {
    if (!dialog?.open) return;
    dialog.close();
    clear();
  }

  function currentAccount() {
    if (state.status?.runtime?.account_status_available === false) return "";
    return String(state.status?.runtime?.wechat_identity || "").trim();
  }

  function updateAccount(accountId) {
    if (dialog?.open && selection?.accountId !== accountId) close();
  }

  async function read() {
    if (loading || !selection || !dialog.open) return;
    const snapshot = selection;
    const request = ++generation;
    loading = true;
    retry.hidden = true;
    content.innerHTML = '<p class="profile-state" role="status">正在查询好友资料…</p>';
    try {
      const result = await api("get_contact_network_profile", snapshot.accountId, snapshot.contactId);
      if (!dialog.open || selection !== snapshot || request !== generation) return;
      if (currentAccount() !== snapshot.accountId) { close(); return; }
      if (result?.account_id !== snapshot.accountId) throw new Error("微信账号已变化，请关闭后重新查询。");
      const contact = result?.item;
      if (!contact || contact.user_id !== snapshot.contactId) throw new Error("未读取到该联系人的资料，请重试。");
      const name = contact.remark || contact.nickname || contact.name || snapshot.name || contact.user_id;
      content.innerHTML = `<div class="contact-profile-person">${avatarMarkup("contact-avatar large", name, contact.avatar)}<div><strong>${escapeHtml(name)}</strong><button class="row-action" type="button" data-copy>复制微信标识</button></div></div><dl class="contact-facts">${factsMarkup(contact)}</dl>`;
      content.querySelector("[data-copy]").addEventListener("click", () => copyText(snapshot.contactId, "微信标识"));
      bindAvatarFallbacks(content);
    } catch (error) {
      if (!dialog.open || selection !== snapshot || request !== generation) return;
      if (currentAccount() !== snapshot.accountId) { close(); return; }
      content.innerHTML = `<p class="profile-state error" role="alert">${escapeHtml(error?.message || "好友资料暂时无法查询，请重试。")}</p>`;
      retry.hidden = false;
    } finally {
      if (request === generation) loading = false;
    }
  }

  function open({accountId, contactId, name = ""}) {
    if (!accountId || !contactId) return;
    if (!dialog) initialize();
    if (dialog.open && selection?.accountId === accountId && selection?.contactId === contactId) return;
    generation += 1;
    loading = false;
    selection = {accountId, contactId, name};
    opener = document.activeElement;
    if (!dialog.open) dialog.showModal();
    if (currentAccount() !== accountId) {
      content.innerHTML = '<p class="profile-state error" role="alert">请连接此微信账号后查询好友资料。</p>';
      retry.hidden = true;
      return;
    }
    void read();
  }

  return {open, updateAccount, factsMarkup};
};
