"""Server-rendered, single-maintainer UI on a separate aiohttp application."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import hmac
from html import escape
import json
import logging
from pathlib import Path
import time

from aiohttp import web

from wechatrebot_manager.json_io import write_json_atomic
from wechatrebot_manager.licensing.contact import MAX_QR_BYTES, create_contact, load_contact
from wechatrebot_manager.licensing.protocol import LicenseError
from ..contact import CONTACT_FILENAME, clear_contact, parse_expiry
from ..store import LicenseStore
from .auth import AuthStore, COOKIE, PREFIX, SESSION_SECONDS
from .codes import FILTERS, checked_reference, detail_html, history_html, reference_fields
from .github import GitHubError, GitHubReleases
from .release_cache import ReleaseReadCache
from .release_editor import ReleaseEditor, is_pending_version
from .upload import MAX_METADATA_BYTES, MAX_UPLOAD_BYTES, UPLOAD_ROUTE, authenticate_upload, receive_candidate

_LOG = logging.getLogger(__name__)
_TEMPLATES = Path(__file__).parent / "templates"


def _field(name: str, label: str, *, kind: str = "text", required: bool = True, extra: str = "") -> str:
    return (f'<label>{label}<input name="{name}" type="{kind}" maxlength="1024" '
            f'{"required" if required else ""} {extra}></label>')


def _csrf(session: dict) -> str:
    return f'<input type="hidden" name="csrf" value="{escape(session["csrf"], quote=True)}">'


def _form(session: dict, path: str, body: str, *, multipart: bool = False) -> str:
    encoding = ' enctype="multipart/form-data"' if multipart else ""
    return f'<form method="post" action="{PREFIX}{path}"{encoding}>{_csrf(session)}{body}</form>'


def _page(title: str, body: str, *, session: dict | None = None, message: str = "", status: int = 200) -> web.Response:
    navigation = ""
    if session and session["authenticated"]:
        navigation = ('<nav>' + "".join(f'<a href="{PREFIX}{path}">{name}</a>' for path, name in (
            ("", "概览"), ("contact", "联系二维码"), ("codes", "激活码"), ("releases", "发布版本")))
            + _form(session, "logout", '<button type="submit">退出</button>') + '</nav>')
    text = (_TEMPLATES / "layout.html").read_text("utf-8").format(
        title=escape(title), navigation=navigation, body=body,
        message=f'<p class="message" role="status">{escape(message)}</p>' if message else "")
    return web.Response(text=text, content_type="text/html", status=status)


def _set_cookie(response: web.Response, token: str, *, authenticated: bool = False) -> None:
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS if authenticated else 600,
                        secure=True, httponly=True, samesite="Strict", path=PREFIX)


def _json(value) -> str:
    return '<pre>' + escape(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre>'


def _time(value: int | float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).astimezone().isoformat(timespec="seconds")


def _release_status(session: dict, scope: str) -> str:
    return (f'<div id="release-data" data-release-scope="{scope}">'
            '<p>当前稳定版本：<strong id="stable-version">正在读取…</strong></p>'
            '<p id="stable-status" class="hint" role="status">版本信息在后台读取，可继续使用其他功能。</p>'
            f'<button id="refresh-releases" type="button" data-csrf="{escape(session["csrf"], quote=True)}">刷新版本信息</button>'
            '<noscript><p>请启用 JavaScript 后查看版本信息和切换稳定版本。</p></noscript>')


def _release_script() -> str:
    return f'<script src="{PREFIX}releases.js" defer></script>'


def create_admin_app(store: LicenseStore, data_dir: Path, *, clock=time.time,
                     github: GitHubReleases | None = None) -> web.Application:
    auth = AuthStore(data_dir)
    releases = github or GitHubReleases(data_dir)
    release_reads = ReleaseReadCache(releases)
    transactions: set[asyncio.Task] = set()
    upload_lock = asyncio.Lock()

    async def audit(event: str, object_id: str = "", **details):
        await asyncio.to_thread(store.admin_audit, int(clock()), event, object_id, **details)

    @web.middleware
    async def boundary(request: web.Request, handler):
        try:
            credentials = await asyncio.to_thread(auth.credentials)
            if credentials is None:
                response = _page("未初始化", '<p>请在服务器本机执行 admin-init 完成管理员初始化。</p>', status=503)
            else:
                token = request.cookies.get(COOKIE, "")
                session = await asyncio.to_thread(auth.session, token, int(clock()))
                request["admin_token"], request["admin_session"] = token, session
                if request.path not in {"/login", "/style.css"} and not (session and session["authenticated"]):
                    response = web.HTTPSeeOther(PREFIX + "login")
                elif request.path == UPLOAD_ROUTE and request.method == "POST":
                    # This route authenticates its first two multipart fields before reading files.
                    response = await handler(request)
                elif request.method not in {"GET", "HEAD"}:
                    form = await request.post()
                    submitted = form.get("csrf", "")
                    if (not session or not isinstance(submitted, str) or not submitted.isascii()
                            or not hmac.compare_digest(submitted, session["csrf"])):
                        response = _page("请求已过期", '<p>请刷新页面后重试。</p>', status=403)
                    else:
                        request["admin_form"] = form
                        response = await handler(request)
                else:
                    response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        except (LicenseError, GitHubError) as exc:
            response = _page("操作未完成", f'<p>{escape(str(exc))}</p><a href="{PREFIX}">返回概览</a>', status=409)
        except Exception as exc:
            _LOG.error("Admin request failed (%s)", type(exc).__name__)
            response = _page("暂时不可用", '<p>后台暂不可用，请稍后重试。客户授权接口独立运行。</p>', status=503)
        if isinstance(response, web.HTTPException):
            redirect_response = web.Response(status=response.status, headers=response.headers)
            redirect_response.cookies.update(response.cookies)
            response = redirect_response
        response.headers.update({
            "Cache-Control": "no-store", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Content-Security-Policy":
                "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        })
        return response

    async def render(request, title, body):
        message = await asyncio.to_thread(auth.flash, request["admin_token"])
        return _page(title, body, session=request["admin_session"], message=message)

    async def redirect(request, path, message):
        await asyncio.to_thread(auth.flash, request["admin_token"], message)
        return web.HTTPSeeOther(PREFIX + path)

    async def login(request):
        session = request["admin_session"]
        if session and session["authenticated"]:
            return web.HTTPSeeOther(PREFIX)
        message = ""
        if request.method == "POST":
            form = request["admin_form"]
            result = await asyncio.to_thread(auth.authenticate, str(form.get("password", "")), str(form.get("totp", "")), int(clock()))
            await audit("login_success" if result.status == "ok" else "login_locked" if result.status == "locked" else "login_failed")
            if result.status == "ok":
                await asyncio.to_thread(auth.logout, request["admin_token"])
                token, _ = await asyncio.to_thread(auth.new_session, int(clock()), authenticated=True, revision=result.revision)
                response = web.HTTPSeeOther(PREFIX)
                _set_cookie(response, token, authenticated=True)
                return response
            message = "密码或动态码无效。动态码不可重复使用。"
        token = ""
        if session is None:
            token, session = await asyncio.to_thread(auth.new_session, int(clock()))
        remaining = await asyncio.to_thread(auth.locked_seconds, int(clock()))
        if remaining:
            message = f"登录已锁定，请在 {remaining} 秒后重试。"
        body = '<section class="panel login"><h2>维护者登录</h2><p class="hint">使用管理密码和手机验证器动态码。</p>' + _form(
            session, "login", _field("password", "管理密码", kind="password", extra='autocomplete="current-password"')
            + _field("totp", "6 位动态码", extra='inputmode="numeric" pattern="[0-9]{6}" autocomplete="one-time-code"')
            + '<button type="submit">登录</button>') + '</section>'
        response = _page("登录", body, message=message, status=429 if remaining else 200)
        if token:
            _set_cookie(response, token)
        return response

    async def overview(request):
        counts = await asyncio.to_thread(store.admin_counts)
        cards = ''.join(f'<section class="panel"><h2>{name}</h2><p class="metric">{counts[key]}</p></section>'
                        for key, name in (("licenses", "授权数"), ("codes", "激活码数"), ("trials", "试用标识数")))
        backup = "尚无备份时间记录"
        try:
            value = json.loads((data_dir / "backup-status.json").read_text("utf-8"))
            backup = _time(int(value["completed_at"]))
        except FileNotFoundError:
            pass
        release_reads.snapshot("stable")
        body = (f'<p class="status">授权数据库连接正常</p><div class="grid">{cards}</div>'
                '<section class="panel">' + _release_status(request["admin_session"], "overview")
                + f'</div><p>最近备份：{escape(backup)}</p></section>' + _release_script())
        return await render(request, "概览", body)

    async def contact(request):
        session = request["admin_session"]
        if request.method == "POST":
            form = request["admin_form"]
            try:
                if form.get("action") == "clear":
                    await asyncio.to_thread(clear_contact, data_dir)
                    await audit("contact_clear")
                    return await redirect(request, "contact", "已清空联系二维码。")
                image = form.get("image")
                if not isinstance(image, web.FileField):
                    raise LicenseError("contact_invalid", "请选择 PNG 或 JPEG 图片。")
                raw = image.file.read(MAX_QR_BYTES + 1)
                expiry = parse_expiry(str(form.get("expires_at", "")), int(clock()))
                record = create_contact(raw, str(form.get("group_name", "")), expiry)
                await asyncio.to_thread(write_json_atomic, data_dir / CONTACT_FILENAME, record)
                await audit("contact_replace", group_name=record["group_name"], expires_at=expiry)
                return await redirect(request, "contact", "联系二维码已更新，客户端下次刷新即可看到。")
            except LicenseError as exc:
                return await redirect(request, "contact", str(exc))
        record = await asyncio.to_thread(load_contact, data_dir / CONTACT_FILENAME)
        preview = '<p>尚未配置二维码。</p>'
        if record:
            state = "已过期" if record["expires_at"] <= clock() else "有效"
            preview = (f'<p>{escape(record["group_name"])} · {state}</p><p>截止：{escape(_time(record["expires_at"]))}</p>'
                       f'<img class="qr" src="{PREFIX}contact/image" alt="当前联系二维码">')
        body = '<div class="grid"><section class="panel"><h2>当前二维码</h2>' + preview + _form(
            session, "contact", '<input type="hidden" name="action" value="clear"><button class="danger" type="submit">清空二维码</button>')
        body += '</section><section class="panel"><h2>替换二维码</h2><p class="hint">PNG 或 JPEG，最大 512 KiB。日期按北京时间当日零点到期。</p>' + _form(
            session, "contact", _field("image", "二维码图片", kind="file", extra='accept="image/png,image/jpeg"')
            + _field("group_name", "微信群名称") + _field("expires_at", "截止日期", kind="date")
            + '<button type="submit">保存二维码</button>', multipart=True) + '</section></div>'
        return await render(request, "联系二维码", body)

    async def contact_image(request):
        record = await asyncio.to_thread(load_contact, data_dir / CONTACT_FILENAME)
        if record is None:
            raise web.HTTPNotFound()
        return web.Response(body=base64.b64decode(record["image_base64"]), content_type=record["mime_type"])

    async def codes(request):
        session = request["admin_session"]
        if request.method == "POST":
            form = request["admin_form"]
            action = str(form.get("action", ""))
            try:
                if action == "issue":
                    reference = checked_reference(form, session)
                    try:
                        days = int(str(form.get("valid_days", "")))
                    except ValueError:
                        days = 0
                    if not 1 <= days <= 365:
                        raise LicenseError("invalid_data", "兑换期限须为 1 至 365 天。")
                    result = await asyncio.to_thread(store.issue_code, owner_ref=str(form.get("owner_ref", "")),
                        reference=reference, now=int(clock()), mode=str(form.get("mode", "")),
                        plan=str(form.get("plan", "")) or None, license_id=str(form.get("license_id", "")),
                        target_device=str(form.get("device", "")), reset_account=form.get("reset_account") == "on",
                        redeem_by=int(clock()) + days * 86400, actor="admin-web")
                    return await redirect(request, "codes/" + result["code_id"], "激活码已生成，可复制交付客户。")
                elif action == "revoke":
                    result = await asyncio.to_thread(store.revoke, now=int(clock()),
                        license_id=str(form.get("license_id", "")), code_id=str(form.get("code_id", "")), actor="admin-web")
                    message = "已撤销：" + (result["license_id"] or result["code_id"])
                elif action == "inspect":
                    result = await asyncio.to_thread(store.inspect, license_id=str(form.get("license_id", "")), owner_ref=str(form.get("owner_ref", "")))
                    message = "查询结果（不包含明文激活码）：\n" + json.dumps(result, ensure_ascii=False, indent=2)
                else:
                    raise LicenseError("invalid_data", "请选择有效操作。")
            except LicenseError as exc:
                message = str(exc)
            return await redirect(request, "codes", message)
        issue = ('<input type="hidden" name="action" value="issue"><label>用途<select name="mode">'
                 '<option value="new">新开通</option><option value="renewal">续期</option><option value="recovery">补领</option>'
                 '<option value="migration">迁移</option></select></label>'
                 '<label>套餐<select name="plan"><option value="month">月卡</option><option value="year">年卡</option>'
                 '<option value="trial">试用（仅新开通）</option><option value="">沿用原套餐（补领/迁移）</option></select></label>'
                 + _field("owner_ref", "客户参考号", extra='placeholder="例如 KH0001，同一客户始终沿用"')
                 + reference_fields(session, int(clock()))
                 + _field("license_id", "原授权编号（续期/补领/迁移必填）", required=False)
                 + _field("device", "新设备指纹（迁移必填）", required=False)
                 + _field("valid_days", "激活码可兑换天数", kind="number", extra='value="30" min="1" max="365"')
                 + '<label><input type="checkbox" name="reset_account">已核实换微信申请，清除原账号绑定（仅迁移）</label>'
                 + '<button type="submit">生成激活码</button>')
        revoke = ('<input type="hidden" name="action" value="revoke"><p class="hint">两个编号只填写一个。</p>'
                  + _field("license_id", "授权编号", required=False) + _field("code_id", "未兑换的码编号", required=False)
                  + '<button class="danger" type="submit">确认撤销</button>')
        inspect = ('<input type="hidden" name="action" value="inspect">'
                   + _field("license_id", "授权编号", required=False) + _field("owner_ref", "客户参考号", required=False)
                   + '<button type="submit">查询状态</button>')
        body = '<div class="grid"><section class="panel"><h2>人工发码</h2>' + _form(session, "codes", issue)
        body += '</section><div><section class="panel"><h2>撤销</h2>' + _form(session, "codes", revoke)
        body += '</section><section class="panel"><h2>查询</h2>' + _form(session, "codes", inspect) + '</section></div></div>'
        filters = {key: request.query.get(key, "").strip() for key in (*FILTERS, "status")}
        try:
            page = int(request.query.get("page", "1"))
        except ValueError:
            raise LicenseError("invalid_data", "历史记录页码无效。")
        history = await asyncio.to_thread(store.admin_code_history, now=int(clock()), page=page, **filters)
        return await render(request, "激活码", body + history_html(history, filters))

    async def code_detail(request):
        record = await asyncio.to_thread(store.admin_code_detail, request.match_info["code_id"], now=int(clock()))
        await audit("code_history_view", record["id"])
        return await render(request, "激活码详情", detail_html(record))

    async def release_page(request):
        session = request["admin_session"]
        if request.method == "POST":
            form = request["admin_form"]
            result = await asyncio.to_thread(auth.authenticate, "", str(form.get("totp", "")), int(clock()), step_up=True)
            if result.status != "ok" or result.revision != session["revision"]:
                await audit("release_totp_locked" if result.status == "locked" else "release_totp_failed")
                return await redirect(request, "releases", "动态码无效、已使用或账号已锁定。请等待验证器生成新动态码后重试。")
            async def switch_and_audit():
                try:
                    result = await releases.switch(str(form.get("version", "")), str(form.get("stable_digest", "")))
                    await audit("stable_switch", result["version"], previous_version=result["previous_version"], changed=result["changed"])
                    return "稳定清单已切换并通过公网读回校验：" + result["version"]
                except GitHubError as exc:
                    await audit("stable_switch_failed")
                    return str(exc)
                finally:
                    release_reads.invalidate()

            task = asyncio.create_task(switch_and_audit())
            transactions.add(task)
            task.add_done_callback(transactions.discard)
            message = await asyncio.shield(task)
            return await redirect(request, "releases", message)
        upload_form = ('<details class="panel"><summary>高级操作：手动上传更新包</summary><h2>上传已签名更新包</h2>'
            '<p class="hint">选择本地发布入口生成的候选记录、签名清单和全部更新 ZIP。总大小最多 4 GiB。上传通过后，再在下方确认切换稳定版本才会通知客户端。</p>'
            + _form(session, "releases/upload", _field("totp", "上传动态码", extra='inputmode="numeric" pattern="[0-9]{6}" autocomplete="one-time-code"')
                + _field("candidate", "发布候选记录", kind="file", extra='accept=".json"')
                + _field("manifest", "签名稳定清单", kind="file", extra='accept=".json"')
                + _field("packages", "更新 ZIP（选择清单要求的全部包）", kind="file", extra='accept=".zip" multiple')
                + '<button type="submit">上传并验证</button>', multipart=True) + '</details>')
        release_reads.snapshot("stable")
        release_reads.snapshot("listing")
        body = (editor.panel(session) + '<section class="panel">' + _release_status(session, "releases")
                + '<details><summary>高级操作：切换或回滚稳定清单</summary>'
                + '<p class="hint">选择较早版本即为回滚发布清单；已经升级的客户端不会自动降级。</p>'
                '<p class="hint">切换前会下载并校验更新包，可能需要几分钟。登录使用过的动态码不可重复使用。</p>'
                + _form(session, "releases", '<fieldset id="release-switch-fields" disabled><input type="hidden" name="stable_digest" value="">'
                    + '<label>目标版本<select name="version"><option value="">正在读取版本…</option></select></label>'
                    + _field("totp", "重新输入当前动态码", extra='inputmode="numeric" pattern="[0-9]{6}" autocomplete="one-time-code"')
                    + '<button type="submit">确认切换稳定版本</button></fieldset>') + '</details>'
                + '<p id="release-listing-status" class="hint" role="status">正在读取发布记录…</p>'
                + '<div class="scroll"><h2>最近 100 个发布记录</h2><table><thead><tr><th>版本</th><th>发布时间</th><th>资产</th><th>签名</th></tr></thead>'
                + '<tbody id="release-rows"></tbody></table></div></div></section>' + upload_form
                + f'<script src="{PREFIX}release-editor.js" defer></script>' + _release_script())
        return await render(request, "发布版本", body)

    async def release_status(request):
        scope = (str(request["admin_form"].get("scope", "releases")) if request.method == "POST"
                 else request.query.get("scope", "releases"))
        if scope not in {"overview", "releases"}:
            raise web.HTTPBadRequest(text="版本查询范围无效。")
        kinds = ("stable", "listing") if scope == "releases" else ("stable",)
        if request.method == "POST":
            release_reads.invalidate(*kinds)
        values = await asyncio.gather(*(release_reads.read(kind) for kind in kinds))
        data = dict(zip(kinds, values))
        stable = data["stable"]["value"]
        listing = data.get("listing", {}).get("value")
        if stable and listing is not None:
            data["listing"] = {**data["listing"], "value": [
                {**item, "pending": bool(item.get("available", item.get("verified", False))
                    and is_pending_version(item["version"], stable["version"]))}
                for item in listing]}
        return web.json_response(data)

    async def upload(request):
        if upload_lock.locked():
            return await redirect(request, "releases", "已有上传正在处理，请等待完成后重试。")
        async with upload_lock:
            try:
                reader = await authenticate_upload(request, auth, clock, audit)
                async with receive_candidate(request, reader, releases, data_dir, auth, clock) as (candidate, manifest, paths, release):
                    async def publish_and_audit():
                        try:
                            result = await releases.upload_candidate(candidate, manifest, paths)
                            await audit("release_upload", result["version"], source_commit=result["source_commit"], package_count=result["package_count"])
                            return result
                        except Exception:
                            await audit("release_upload_failed", release.version)
                            raise
                        finally:
                            release_reads.invalidate()

                    task = asyncio.create_task(publish_and_audit())
                    transactions.add(task)
                    task.add_done_callback(transactions.discard)
                    try:
                        result = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        # Keep its files until an already-authorized GitHub operation ends.
                        await task
                        raise
                    message = f'上传并公网验证完成：{result["version"]}。尚未通知客户端，请在下方确认切换稳定版本。'
            except (GitHubError, OSError, ValueError) as exc:
                message = str(exc) if isinstance(exc, GitHubError) else "上传未完成，临时文件已清理。请检查磁盘和文件后重试。"
            return await redirect(request, "releases", message)

    async def logout(request):
        await asyncio.to_thread(auth.logout, request["admin_token"])
        await audit("logout")
        response = web.HTTPSeeOther(PREFIX + "login")
        response.set_cookie(COOKIE, "", max_age=0, path=PREFIX, secure=True, httponly=True, samesite="Strict")
        return response

    async def style(request):
        return web.Response(text=(_TEMPLATES / "style.css").read_text("utf-8"), content_type="text/css")

    async def code_script(request):
        return web.Response(text=(_TEMPLATES / "codes.js").read_text("utf-8"), content_type="application/javascript")

    async def release_script(request):
        return web.Response(text=(_TEMPLATES / "releases.js").read_text("utf-8"), content_type="application/javascript")

    async def editor_script(request):
        return web.Response(text=(_TEMPLATES / "release-editor.js").read_text("utf-8"), content_type="application/javascript")

    editor = ReleaseEditor(releases, auth, data_dir, clock=clock, audit=audit, render=render,
        redirect=redirect, form=_form, field=_field, release_reads=release_reads, transactions=transactions)

    # The upload handler enforces the package and metadata limits while
    # streaming. Keep aiohttp's outer limit aligned so it does not reject a
    # valid multi-gigabyte multipart request before the handler can inspect CSRF
    # and TOTP fields.
    app = web.Application(middlewares=[boundary], client_max_size=MAX_UPLOAD_BYTES + 3 * MAX_METADATA_BYTES)
    app.router.add_get("/", overview)
    app.router.add_get("/style.css", style)
    app.router.add_get("/codes.js", code_script)
    app.router.add_get("/releases.js", release_script)
    app.router.add_get("/release-editor.js", editor_script)
    app.router.add_get("/releases/editor", editor.load)
    app.router.add_post("/releases/draft", editor.save)
    app.router.add_post("/releases/preview", editor.preview)
    app.router.add_post("/releases/publish", editor.publish)
    app.router.add_get("/releases/status", release_status)
    app.router.add_post("/releases/refresh", release_status)
    app.router.add_get("/codes/{code_id}", code_detail)
    app.router.add_get("/contact/image", contact_image)
    for path, handler in (("login", login), ("contact", contact), ("codes", codes), ("releases", release_page)):
        app.router.add_get("/" + path, handler)
        app.router.add_post("/" + path, handler)
    app.router.add_post("/logout", logout)
    app.router.add_post(UPLOAD_ROUTE, upload)
    async def finish_transactions(application):
        await release_reads.close()
        if transactions:
            await asyncio.gather(*transactions, return_exceptions=True)
    app.on_cleanup.append(finish_transactions)
    return app
