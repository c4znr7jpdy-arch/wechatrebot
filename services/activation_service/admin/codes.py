"""Presentation and signed issuance identifiers for activation management."""
from __future__ import annotations

from datetime import datetime
from html import escape
import hmac
import re
import secrets
from zoneinfo import ZoneInfo

from wechatrebot_manager.licensing.protocol import LicenseError
from ..store import MIGRATION_CREDITS
from .auth import PREFIX

MODES = {"new": "历史开通码", "renewal": "历史续期码", "recovery": "恢复授权", "migration": "换电脑",
         "duration": "时长激活码", "rebind_credit": "微信换绑次数码"}
PLANS = {"month": "月卡", "year": "年卡", "trial": "试用"}
STATUSES = {"unredeemed": "未兑换", "redeemed": "已兑换", "expired": "兑换已过期", "revoked": "已撤销"}
LICENSE_STATUSES = {"active": "有效", "expired": "已到期", "revoked": "已停用"}
FILTERS = {"owner_ref": "历史客户参考号", "reference": "业务编号", "license_id": "授权编号", "code_id": "码编号"}
_REFERENCE = re.compile(r"WRB-\d{14}-[A-F0-9]{16}")


def _signature(session: dict, reference: str) -> str:
    return hmac.digest(session["csrf"].encode(), ("issue:" + reference).encode(), "sha256").hex()


def reference_fields(session: dict, now: int) -> str:
    stamp = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d%H%M%S")
    reference = f"WRB-{stamp}-{secrets.token_hex(8).upper()}"
    return (f'<input type="hidden" name="reference" value="{reference}">'
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
        return "尚未关联授权；试用不增加换电脑次数" if record.get("plan") == "trial" else "尚未关联授权"
    if plan == "trial":
        return "试用授权沿用人工恢复流程，不计付费换电脑次数"
    total, used, remaining = (record[name] for name in ("migration_total", "migration_used", "migration_remaining"))
    text = f"换电脑剩余 {remaining} 次（累计 {total} 次，已用 {used} 次）"
    forgiven = remaining + used - total
    return text + (f"；历史超额 {forgiven} 次不抵扣新额度" if forgiven > 0 else "")


def migration_help() -> str:
    return ('<p class="hint">客户在新电脑打开“订阅与授权”→“授权编号与换机”，复制设备编号发给你；'
            '尚未生成时先点“获取本机设备编号”。生成换机码后，客户使用现有激活码输入框兑换。</p>'
            '<p class="hint">保留原到期时间和绑定微信，不消耗微信换绑次数。旧电脑下次联网校验后失效，'
            '已有离线凭证最长仍可使用 24 小时。</p>'
            f'<p class="hint">月付增加 {MIGRATION_CREDITS["month"]} 次换电脑额度，年付增加 {MIGRATION_CREDITS["year"]} 次。'
            '只在迁移首次成功兑换时扣 1 次，发码、预览和重复兑换不扣。</p>')


def tabs_html(active: str) -> str:
    return '<nav class="code-tabs" aria-label="授权管理">' + ''.join(
        f'<a href="{PREFIX}codes?tab={key}" {"aria-current=page" if key == active else ""}>{label}</a>'
        for key, label in (("issue", "发码"), ("licenses", "客户授权"), ("history", "发码记录"))) + '</nav>'


def _customer(row: dict) -> str:
    old_ref = row.get("owner_ref", "")
    label = row.get("customer_note") or row.get("note") or (old_ref if old_ref and not old_ref.startswith("auto:") else "未添加备注")
    return _value(label)


def product_text(row: dict) -> str:
    return f'微信换绑 +{row["rebind_credits"]} 次' if row["mode"] == "rebind_credit" else PLANS.get(row["plan"], row["plan"])


def history_html(result: dict) -> str:
    body = ('<section class="panel" id="history"><h2>发码记录</h2>'
            '<p class="hint">已兑换表示这个码已使用，客户是否还能使用请看授权状态。时间均为北京时间。</p>'
            f'<p>共 {result["total"]} 条记录 · 第 {result["page"]} 页</p>')
    if not result["items"]:
        return body + '<p class="hint">没有符合条件的发码记录。</p></section>'
    body += '<div class="scroll"><table class="code-history"><thead><tr><th>客户 / 备注</th><th>激活码内容</th><th>兑换 / 授权状态</th><th>生成 / 兑换截止</th><th>操作</th></tr></thead><tbody>'
    for row in result["items"]:
        license_id = row["redeemed_license"] or row["target_id"]
        linked = f'<a href="{PREFIX}licenses/{escape(license_id, quote=True)}">查看授权</a>' if license_id else ''
        body += (f'<tr><td>{_customer(row)}<br><span class="hint">{_value(row["account"])}</span></td>'
                 f'<td>{_value(product_text(row))}<br><span class="hint">{_value(MODES.get(row["mode"], row["mode"]))}</span></td>'
                 f'<td>{_value(STATUSES[row["status"]])}<br><span class="hint">'
                 f'{_value(LICENSE_STATUSES.get(row["license_status"], "尚未关联授权"))}</span></td>'
                 f'<td>{_time(row["issued_at"])}<br><span class="hint">{_time(row["redeem_by"])}</span></td>'
                 f'<td><a href="{PREFIX}codes/{escape(row["id"], quote=True)}">查看激活码</a><br>{linked}</td></tr>')
    return body + '</tbody></table></div></section>'


def detail_html(row: dict) -> str:
    service = row["mode"] in {"recovery", "migration"}
    title = ({"recovery": "恢复码 · ", "migration": "换机码 · "}.get(row["mode"], "") + product_text(row))
    if service:
        effect = f'恢复原授权，到期时间不变：{_time(row["valid_until"])}。'
    elif row["mode"] == "rebind_credit":
        effect = f'增加 {row["rebind_credits"]} 次微信换绑机会，不延长套餐，也不会立即更换微信。'
    elif row["mode"] == "duration":
        effect = "新用户首次兑换后开始计时；已有授权接续剩余时间，已到期则从兑换时起算。"
    elif row["mode"] == "renewal":
        effect = "延长指定的原授权：未到期接续剩余时间，已到期从兑换时起算。"
    else:
        effect = "首次成功兑换后开通授权并开始计时。"
    body = (f'<p><a href="{PREFIX}codes?tab=history">返回发码记录</a></p><section class="panel">'
            f'<h2>{_value(title)}</h2><p>{_value(STATUSES[row["status"]])} · '
            f'兑换截止：{_time(row["redeem_by"])}</p><p class="hint">{escape(effect)}</p>')
    if row["history_available"]:
        body += (f'<label>完整激活码<input id="activation-code" value="{escape(row["code"], quote=True)}" readonly autocomplete="off" spellcheck="false"></label>'
                 '<button type="button" id="copy-code">复制激活码</button><span id="copy-status" role="status"></span>'
                 '<p class="hint">可从历史记录再次查看。请私下发给客户；同一码只增加一次时长或次数。</p>')
    else:
        body += '<p class="hint">旧记录未保存激活码、客户参考号和业务编号明文，无法恢复；原兑换与授权记录保留。</p>'
    fields = [
        ("历史客户参考号", "系统自动关联" if row["owner_ref"].startswith("auto:") else row["owner_ref"]),
        ("业务编号", row["reference"]), ("码编号", row["id"]),
        ("用途", MODES.get(row["mode"], row["mode"])),
        ("套餐", "不改变原套餐" if row["mode"] == "rebind_credit" else PLANS.get(row["plan"], row["plan"])),
        ("原授权编号", row["target_id"]), ("新设备指纹", row["target_device"]),
        ("清除原微信绑定", "是" if row["reset_account"] else "否"),
        ("发码时间", _time(row["issued_at"])), ("兑换时间", _time(row["redeemed_at"])),
        ("撤销时间", _time(row["revoked_at"])), ("兑换后授权编号", row["redeemed_license"]),
        ("当前授权状态", LICENSE_STATUSES.get(row["license_status"], "尚未关联授权")),
        ("当前授权到期时间", _time(row["valid_until"])), ("当前绑定微信", row["account"]),
        ("当前设备指纹", row["device"]), ("换电脑额度", quota_text(row)),
    ]
    body += (f'<div class="code-actions"><a href="{PREFIX}codes">再生成一个</a>'
             + (f'<a href="{PREFIX}licenses/{escape(row["redeemed_license"] or row["target_id"], quote=True)}">查看客户授权</a>'
                if row["redeemed_license"] or row["target_id"] else '') + '</div></section>')
    body += '<details class="panel"><summary>查看发码与授权编号</summary><dl class="code-details">'
    return body + ''.join(f'<dt>{label}</dt><dd>{_value(value)}</dd>' for label, value in fields) + '</dl></details>'


def licenses_html(result: dict) -> str:
    body = f'<section class="panel"><h2>客户授权</h2><p>共 {result["total"]} 条授权 · 第 {result["page"]} 页</p>'
    if not result["items"]:
        return body + '<p class="hint">没有找到客户授权。未兑换的码请到“发码记录”查询。</p></section>'
    body += '<div class="scroll"><table class="code-history"><thead><tr><th>客户 / 绑定微信</th><th>套餐 / 状态</th><th>到期时间</th><th>微信换绑</th><th>操作</th></tr></thead><tbody>'
    for row in result["items"]:
        body += (f'<tr><td>{_customer(row)}<br><span class="hint">{_value(row["account"])}</span></td>'
                 f'<td>{_value(PLANS[row["plan"]])}<br>{_value(LICENSE_STATUSES[row["license_status"]])}</td>'
                 f'<td>{_time(row["valid_until"])}</td><td>剩余 {row["rebind_remaining"]} 次</td>'
                 f'<td><a href="{PREFIX}licenses/{escape(row["id"], quote=True)}">查看 / 管理</a></td></tr>')
    return body + '</tbody></table></div></section>'


def license_html(row: dict) -> str:
    fields = (("客户备注", row["note"]), ("绑定微信", row["account"]), ("套餐", PLANS[row["plan"]]),
              ("授权状态", LICENSE_STATUSES[row["license_status"]]), ("到期时间", _time(row["valid_until"])),
              ("微信换绑次数", f'剩余 {row["rebind_remaining"]} 次，已用 {row["rebind_used"]} 次'))
    body = f'<p><a href="{PREFIX}codes?tab=licenses">返回客户授权</a></p><section class="panel"><h2>客户授权</h2><dl class="code-details">'
    body += ''.join(f'<dt>{label}</dt><dd>{_value(value)}</dd>' for label, value in fields) + '</dl>'
    return body + (f'<details class="technical-details"><summary>授权编号与当前设备</summary><dl class="code-details">'
                   f'<dt>授权编号</dt><dd>{_value(row["id"])}</dd><dt>当前设备编号</dt><dd>{_value(row["device"])}</dd></dl></details></section>')
