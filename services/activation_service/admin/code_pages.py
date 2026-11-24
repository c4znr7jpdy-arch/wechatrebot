"""Authenticated activation forms, customer search and contextual after-sales actions."""
from __future__ import annotations

import asyncio
from html import escape
import json

from wechatrebot_manager.licensing.protocol import LicenseError
from .auth import PREFIX
from .codes import (FILTERS, STATUSES, checked_reference, detail_html, history_html, license_html,
                    licenses_html, migration_help, quota_text, reference_fields, tabs_html)


class CodePages:
    def __init__(self, store, *, clock, render, redirect, form, field, audit):
        self.store, self.clock, self.render, self.redirect = store, clock, render, redirect
        self.form, self.field, self.audit = form, field, audit

    @staticmethod
    def _hidden(name, value):
        return f'<input type="hidden" name="{name}" value="{escape(str(value), quote=True)}">'

    @staticmethod
    def _number(value, label, *, minimum=1, maximum=1000000):
        try:
            number = int(str(value))
        except ValueError:
            number = 0
        if not minimum <= number <= maximum:
            raise LicenseError("invalid_data", f"{label}须为 {minimum} 至 {maximum} 的整数。")
        return number

    def _issue_form(self, session):
        fields = (self._hidden("action", "issue") + reference_fields(session, int(self.clock()))
                  + '<fieldset class="code-kind"><legend>发码类型</legend>'
                    '<label><input type="radio" name="mode" value="duration" checked>时长激活码</label>'
                    '<label><input type="radio" name="mode" value="rebind_credit">微信换绑次数码</label></fieldset>'
                    '<div id="duration-fields"><label>套餐<select name="plan">'
                    '<option value="month">月卡 · 1 个自然月</option><option value="year">年卡 · 12 个自然月</option></select></label>'
                    '<p class="hint">新老用户都用这个码。有原授权自动续费，首次成功兑换后开始计时。</p></div>'
                    '<div id="credit-fields" hidden><label>增加微信换绑次数<input name="rebind_credits" type="number" '
                    'min="1" max="1000" step="1" value="1" disabled required></label>'
                    '<div class="code-presets"><button type="button" class="secondary" data-credits="1">+1 次</button>'
                    '<button type="button" class="secondary" data-credits="5">+5 次</button>'
                    '<button type="button" class="secondary" data-credits="10">+10 次</button></div>'
                    '<p class="hint">需要已有付费授权。只增加微信换绑次数，到期时间不变。</p></div>'
                  + self.field("note", "客户备注（选填）", required=False, max_length=256,
                               extra='placeholder="例如聊天备注或订单尾号，方便以后查找"')
                  + '<details class="technical-details"><summary>高级设置</summary>'
                  + self.field("valid_days", "多少天内兑换", kind="number", extra='value="30" min="1" max="365"')
                  + '<p class="hint">默认 30 天内兑换。这是领取期限，不是套餐使用时长；具体截止时间会显示在生成结果中。</p></details>'
                    '<button type="submit" class="issue-primary">生成激活码</button>')
        return ('<section class="panel issue-panel"><h2>生成激活码</h2><p class="hint">选好内容，生成后复制发给客户即可。</p>'
                + self.form(session, "codes", fields) + '</section>')

    def _search_form(self, session, tab, values):
        controls = (self._hidden("action", "search") + self._hidden("tab", tab)
                    + self.field("search", "查找客户或激活码", required=False, max_length=256,
                                 extra=f'value="{escape(values.get("search", ""), quote=True)}" '
                                       'placeholder="微信号、客户备注、授权编号或完整激活码" autocomplete="off"'))
        if tab == "history":
            for name, label, options in (
                ("status", "兑换状态", {"": "全部状态", **STATUSES}),
                ("kind", "类型", {"": "全部类型", "duration": "时长码", "rebind_credit": "次数码", "service": "恢复 / 换机"}),
            ):
                controls += f'<label>{label}<select name="{name}">' + ''.join(
                    f'<option value="{key}" {"selected" if values.get(name, "") == key else ""}>{text}</option>'
                    for key, text in options.items()) + '</select></label>'
        controls += '<div class="filter-actions"><button type="submit">查询</button>' + f'<a href="{PREFIX}codes?tab={tab}">清空条件</a></div>'
        return '<section class="panel">' + self.form(session, "codes", '<div class="history-filters">' + controls + '</div>') + '</section>'

    def _pagination(self, session, tab, result, values):
        body = '<div class="pagination">'
        pages = max(1, (result["total"] + result["page_size"] - 1) // result["page_size"])
        for page, label in ((result["page"] - 1, "上一页"), (result["page"] + 1, "下一页")):
            if 1 <= page <= pages:
                fields = {**values, "tab": tab, "action": "search", "page": page}
                controls = ''.join(self._hidden(key, value) for key, value in fields.items())
                body += self.form(session, "codes", controls + f'<button class="secondary" type="submit">{label}</button>')
        return body + '</div>'

    async def show(self, request):
        if request.method == "POST" and request["admin_form"].get("action") != "search":
            return await self._mutate(request)
        values = request["admin_form"] if request.method == "POST" else request.query
        tab = values.get("tab", "history" if any(values.get(key) for key in (*FILTERS, "status")) else "issue")
        if tab not in {"issue", "licenses", "history"}:
            raise LicenseError("invalid_data", "请选择发码、客户授权或发码记录。")
        page = self._number(values.get("page", "1"), "页码")
        session, now = request["admin_session"], int(self.clock())
        # Search uses POST so a complete activation code never enters a URL.
        search = str(values.get("search", "")) if request.method == "POST" else ""
        filters = {key: str(values.get(key, "")).strip() for key in (*FILTERS, "status", "kind")}
        body = tabs_html(tab)
        if tab == "issue":
            body += self._issue_form(session)
        else:
            body += self._search_form(session, tab, {**filters, "search": search})
        if tab == "licenses":
            result = await asyncio.to_thread(self.store.admin_licenses, now=now, search=search, page=page)
            body += licenses_html(result)
        else:
            result = await asyncio.to_thread(self.store.admin_code_history, now=now, page=page, search=search, **filters)
            body += history_html(result)
        body += self._pagination(session, tab, result, {**filters, "search": search})
        return await self.render(request, "激活码与授权", body + self._script())

    async def _mutate(self, request):
        form, session, now = request["admin_form"], request["admin_session"], int(self.clock())
        action, destination = str(form.get("action", "")), "codes"
        try:
            if action in {"issue", "service"}:
                mode = str(form.get("mode", "duration"))
                if action == "service" and mode not in {"migration", "recovery"}:
                    raise LicenseError("invalid_data", "请选择恢复授权或更换电脑。")
                result = await asyncio.to_thread(self.store.issue_code,
                    owner_ref=str(form.get("owner_ref", "")) if action == "issue" else "",
                    reference=checked_reference(form, session), now=now, mode=mode,
                    plan=str(form.get("plan", "")) or None, license_id=str(form.get("license_id", "")),
                    target_device=str(form.get("device", "")), reset_account=action == "issue" and form.get("reset_account") == "on",
                    rebind_credits=self._number(form.get("rebind_credits", "1"), "增加次数", maximum=1000) if mode == "rebind_credit" else 0,
                    note=str(form.get("note", "")),
                    redeem_by=now + self._number(form.get("valid_days", "30"), "兑换期限", maximum=365) * 86400,
                    actor="admin-web")
                return await self.redirect(request, "codes/" + result["code_id"], "激活码已生成，可复制交付客户。")
            if action == "note":
                code_id, license_id = str(form.get("code_id", "")), str(form.get("license_id", ""))
                await asyncio.to_thread(self.store.admin_update_note, now=now, note=str(form.get("note", "")),
                                        code_id=code_id, license_id=license_id)
                destination = "licenses/" + license_id if license_id else "codes/" + code_id
                message = "客户备注已保存。"
            elif action == "revoke":
                if form.get("confirm") != "on":
                    raise LicenseError("confirmation_required", "请确认当前记录及停用影响后再操作。")
                result = await asyncio.to_thread(self.store.revoke, now=now, license_id=str(form.get("license_id", "")),
                                                code_id=str(form.get("code_id", "")), actor="admin-web")
                destination = "licenses/" + result["license_id"] if result["license_id"] else "codes/" + result["code_id"]
                message = "授权已停用。" if result["license_id"] else "该未兑换码已撤销。"
            elif action == "inspect":
                result = await asyncio.to_thread(self.store.inspect, license_id=str(form.get("license_id", "")),
                                                owner_ref=str(form.get("owner_ref", "")))
                message = "查询结果：\n" + json.dumps(result, ensure_ascii=False, indent=2)
            else:
                raise LicenseError("invalid_data", "请选择有效操作。")
        except LicenseError as exc:
            message = str(exc)
        return await self.redirect(request, destination, message)

    def _note_form(self, session, record, *, license=False):
        key = "license_id" if license else "code_id"
        controls = self._hidden("action", "note") + self._hidden(key, record["id"])
        controls += self.field("note", "客户备注（选填）", required=False, max_length=256,
                               extra=f'value="{escape(record["note"], quote=True)}"')
        return '<section class="panel"><h2>客户备注</h2>' + self.form(session, "codes", controls + '<button type="submit">保存备注</button>') + '</section>'

    def _revoke_form(self, session, identifier, *, license=False):
        label, key = ("停用此授权", "license_id") if license else ("撤销此未兑换码", "code_id")
        warning = "停用后客户将无法继续使用此授权。" if license else "撤销后这个码将无法兑换。"
        controls = self._hidden("action", "revoke") + self._hidden(key, identifier)
        controls += f'<p>{warning}</p><label><input type="checkbox" name="confirm" required>我已核对当前记录，确认{label}</label>'
        return '<details class="panel"><summary>' + label + '</summary>' + self.form(
            session, "codes", controls + f'<button type="submit" class="danger">确认{label}</button>') + '</details>'

    async def code_detail(self, request):
        record = await asyncio.to_thread(self.store.admin_code_detail, request.match_info["code_id"], now=int(self.clock()))
        await self.audit("code_history_view", record["id"])
        body = tabs_html("history") + detail_html(record)
        if record["history_available"]:
            body += self._note_form(request["admin_session"], record)
        if record["redeemed_at"] is None and record["revoked_at"] is None:
            body += self._revoke_form(request["admin_session"], record["id"])
        return await self.render(request, "激活码详情", body + self._script())

    async def license_detail(self, request):
        session, now = request["admin_session"], int(self.clock())
        record = await asyncio.to_thread(self.store.admin_license_detail, request.match_info["license_id"], now=now)
        history = await asyncio.to_thread(self.store.admin_code_history, now=now, license_id=record["id"])
        body = tabs_html("licenses") + license_html(record) + self._note_form(session, record, license=True)
        if record["status"] != "revoked":
            body += '<details class="panel" id="change-computer"><summary>更换电脑</summary>' + migration_help()
            body += '<p>' + escape(quota_text(record)) + '</p>' + self._service_form(session, record, "migration") + '</details>'
            body += ('<details class="panel"><summary>恢复原授权</summary><p class="hint">原码无法找回时使用。'
                     '只恢复当前设备上的原授权，不增加时长；换电脑或重装后设备编号改变，请使用“更换电脑”。</p>'
                     + self._service_form(session, record, "recovery") + '</details>')
            body += self._revoke_form(session, record["id"], license=True)
        body += history_html(history) + self._pagination(session, "history", history, {"license_id": record["id"]})
        return await self.render(request, "客户授权详情", body + self._script())

    def _service_form(self, session, record, mode):
        fields = ''.join(self._hidden(key, value) for key, value in {
            "action": "service", "mode": mode, "license_id": record["id"], "valid_days": 30}.items())
        fields += reference_fields(session, int(self.clock()))
        if mode == "migration":
            fields += self.field("device", "新电脑设备编号", max_length=64,
                                 extra='pattern="[a-f0-9]{64}" autocomplete="off" placeholder="粘贴客户从新电脑复制的设备编号"')
        fields += '<button type="submit">' + ("生成换机码" if mode == "migration" else "生成恢复码") + '</button>'
        return self.form(session, "codes", fields)

    @staticmethod
    def _script():
        return f'<script src="{PREFIX}codes.js" defer></script>'
