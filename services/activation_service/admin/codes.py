"""Presentation and signed issuance identifiers for the activation ledger UI."""
from __future__ import annotations

from datetime import datetime
from html import escape
import hmac
import re
import secrets
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from wechatrebot_manager.licensing.protocol import LicenseError
from ..store import MIGRATION_CREDITS
from .auth import PREFIX

MODES = {"new": "新开通", "renewal": "续期", "recovery": "补领", "migration": "迁移"}
PLANS = {"month": "月卡", "year": "年卡", "trial": "试用"}
STATUSES = {"unredeemed": "未兑换", "redeemed": "已兑换", "expired": "兑换已过期", "revoked": "已撤销"}
LICENSE_STATUSES = {"active": "有效", "expired": "已到期", "revoked": "已停用"}
FILTERS = {"owner_ref": "客户参考号", "reference": "业务编号", "license_id": "授权编号", "code_id": "码编号"}
_REFERENCE = re.compile(r"WRB-\d{14}-[A-F0-9]{16}")


def _signature(session: dict, reference: str) -> str:
    return hmac.digest(session["csrf"].encode(), ("issue:" + reference).encode(), "sha256").hex()


def reference_fields(session: dict, now: int) -> str:
    stamp = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d%H%M%S")
    reference = f"WRB-{stamp}-{secrets.token_hex(8).upper()}"
    return (f'<label>业务编号（系统自动生成）<input name="reference" value="{reference}" readonly></label>'
            f'<input type="hidden" name="reference_signature" value="{_signature(session, reference)}">')


def checked_reference(form, session: dict) -> str:
    reference = str(form.get("reference", ""))
    signature = str(form.get("reference_signature", ""))
    if (not _REFERENCE.fullmatch(reference) or not re.fullmatch(r"[a-f0-9]{64}", signature)
            or not hmac.compare_digest(signature, _signature(session, reference))):
        raise LicenseError("invalid_data", "发码表单已失效，请刷新页面使用系统生成的业务编号。")
    return reference


def _value(value) -> str:
    return escape(str(value)) if value is not None and value != "" else "—"


def _time(value) -> str:
    if value is None:
        return "—"
    return datetime.fromtimestamp(value, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def quota_text(record: dict) -> str:
    plan = record.get("license_plan", record.get("plan", ""))
    if not plan:
        if record.get("plan") == "trial":
            return "尚未关联授权；试用不增加换绑次数"
        return "尚未关联授权，兑换开通后增加次数"
    if plan == "trial":
        return "试用授权沿用人工恢复流程，不计付费换绑次数"
    total, used, remaining = (record[name] for name in ("migration_total", "migration_used", "migration_remaining"))
    text = f"换绑剩余 {remaining} 次（累计 {total} 次，已用 {used} 次）"
    forgiven = remaining + used - total
    return text + (f"；历史超额 {forgiven} 次不抵扣新额度" if forgiven > 0 else "")


def migration_help() -> str:
    return ('<details class="migration-help"><summary>迁移 / 换绑怎么操作</summary><ol>'
            '<li>在发码历史中查原授权，确认客户参考号、授权编号和剩余换绑次数。</li>'
            '<li>客户在目标电脑的管理器打开“订阅与授权”→“授权编号与换机”，复制设备编号；'
            '尚未生成时先点“获取本机设备编号”。</li>'
            '<li>用途选“迁移”，套餐沿用原套餐，填写原客户参考号、原授权编号和目标设备编号。'
            '同一电脑只换微信时填当前设备编号，并勾选清除原账号绑定；只换电脑保留微信时不勾选。</li>'
            '<li>生成迁移码交给客户，客户先停止机器人，在目标电脑预览并确认兑换；需要换微信时再连接、确认绑定新账号。'
            '原授权编号和到期时间保留，迁移不赠送时长。</li></ol>'
            f'<p class="hint">每成功开通或续费一期：月付增加 {MIGRATION_CREDITS["month"]} 次，'
            f'年付增加 {MIGRATION_CREDITS["year"]} 次，剩余可累计。'
            '迁移码首次成功兑换扣 1 次，发码、预览、重复兑换和补领不扣；次数用完需续费增加。'
            '试用授权沿用人工恢复流程，不产生付费额度。</p></details>')


def history_html(result: dict, filters: dict) -> str:
    inputs = ''.join(f'<label>{label}<input name="{key}" maxlength="128" value="{escape(filters.get(key, ""), quote=True)}"></label>'
                     for key, label in FILTERS.items())
    options = '<option value="">全部兑换状态</option>' + ''.join(
        f'<option value="{key}" {"selected" if filters.get("status") == key else ""}>{label}</option>'
        for key, label in STATUSES.items())
    body = ('<section class="panel" id="history"><h2>发码历史</h2>'
            '<p class="hint">按最新发码时间排序，编号与客户参考号精确匹配。已兑换代表已使用该码，授权是否有效请看授权状态。时间均为北京时间。</p>'
            f'<form method="get" action="{PREFIX}codes#history" class="history-filters">{inputs}'
            f'<label>兑换状态<select name="status">{options}</select></label>'
            f'<div class="filter-actions"><button type="submit">查询历史</button><a href="{PREFIX}codes#history">清空条件</a></div></form>'
            f'<p>共 {result["total"]} 条记录 · 第 {result["page"]} 页</p>')
    if not result["items"]:
        return body + '<p class="hint">没有符合条件的发码记录。</p></section>'
    body += '<div class="scroll"><table class="code-history"><thead><tr><th>客户 / 业务编号</th><th>用途 / 套餐</th><th>兑换状态 / 授权状态</th><th>发码 / 兑换截止</th><th>授权编号 / 操作</th></tr></thead><tbody>'
    for row in result["items"]:
        customer = _value(row["owner_ref"]) if row["history_available"] else "旧记录未保存客户参考号"
        license_id = row["redeemed_license"] or row["target_id"]
        body += (f'<tr><td>{customer}<br><span class="hint">{_value(row["reference"])}</span></td>'
                 f'<td>{_value(MODES.get(row["mode"], row["mode"]))}<br>{_value(PLANS.get(row["plan"], row["plan"]))}</td>'
                 f'<td>{_value(STATUSES.get(row["status"], row["status"]))}<br><span class="hint">'
                 f'{_value(LICENSE_STATUSES.get(row["license_status"], "尚未关联授权"))}</span><br>'
                 f'<span class="hint">{_value(quota_text(row))}</span></td>'
                 f'<td>{_time(row["issued_at"])}<br><span class="hint">{_time(row["redeem_by"])}</span></td>'
                 f'<td>{_value(license_id)}<br><a href="{PREFIX}codes/{escape(row["id"], quote=True)}">查看详情 / 激活码</a></td></tr>')
    body += '</tbody></table></div><div class="pagination">'
    pages = max(1, (result["total"] + result["page_size"] - 1) // result["page_size"])
    for page, label in ((result["page"] - 1, "上一页"), (result["page"] + 1, "下一页")):
        if 1 <= page <= pages:
            query = urlencode({**{key: value for key, value in filters.items() if value}, "page": page})
            body += f'<a href="{PREFIX}codes?{escape(query, quote=True)}#history">{label}</a>'
    return body + '</div></section>'


def detail_html(row: dict) -> str:
    body = f'<p><a href="{PREFIX}codes#history">返回发码历史</a></p><section class="panel"><h2>激活码</h2>'
    if row["history_available"]:
        body += (f'<label>完整激活码<input id="activation-code" value="{escape(row["code"], quote=True)}" readonly autocomplete="off" spellcheck="false"></label>'
                 '<button type="button" id="copy-code">复制激活码</button><span id="copy-status" role="status"></span>'
                 '<p class="hint">可从历史记录再次查看。请通过私密渠道交付客户；已兑换码再次使用不会增加时长。</p>'
                 f'<script src="{PREFIX}codes.js" defer></script>')
    else:
        body += '<p class="hint">旧记录未保存激活码、客户参考号和业务编号明文，无法恢复；原兑换与授权记录保留。</p>'
    fields = [
        ("客户参考号", row["owner_ref"]), ("业务编号", row["reference"]), ("码编号", row["id"]),
        ("用途", MODES.get(row["mode"], row["mode"])), ("套餐", PLANS.get(row["plan"], row["plan"])),
        ("兑换状态", STATUSES.get(row["status"], row["status"])),
        ("原授权编号", row["target_id"]), ("新设备指纹", row["target_device"]),
        ("清除原微信绑定", "是" if row["reset_account"] else "否"),
        ("发码时间", _time(row["issued_at"])),
        ("激活码可兑换天数", (row["redeem_by"] - row["issued_at"]) // 86400),
        ("兑换截止时间", _time(row["redeem_by"])), ("兑换时间", _time(row["redeemed_at"])),
        ("撤销时间", _time(row["revoked_at"])), ("兑换后授权编号", row["redeemed_license"]),
        ("当前授权状态", LICENSE_STATUSES.get(row["license_status"], "尚未关联授权")),
        ("当前授权到期时间", _time(row["valid_until"])),
        ("当前绑定微信", row["account"]), ("当前设备指纹", row["device"]),
        ("换绑额度", quota_text(row)),
    ]
    body += '</section><section class="panel"><h2>发码与授权信息</h2><dl class="code-details">'
    return body + ''.join(f'<dt>{label}</dt><dd>{_value(value)}</dd>' for label, value in fields) + '</dl></section>'
