"""Editable release drafts and a session-bound, single-use publication confirmation."""
from __future__ import annotations

import asyncio
import hashlib
from html import escape
import secrets

from packaging.version import InvalidVersion, Version

from wechatrebot_manager.update_message import (
    MAX_MESSAGE_BYTES, UpdateMessageError, create_update_message, parse_update_message,
)
from .auth import PREFIX, private_json, read_private_json
from .github import GitHubError


def is_pending_version(version, stable):
    try:
        return Version(version) > Version(stable)
    except (InvalidVersion, TypeError):
        return False


class ReleaseEditor:
    def __init__(self, releases, auth, data_dir, *, clock, audit, render, redirect,
                 form, field, release_reads, transactions):
        self.releases, self.auth, self.directory = releases, auth, data_dir / "release-drafts"
        self.clock, self.audit, self.render, self.redirect = clock, audit, render, redirect
        self.form, self.field = form, field
        self.release_reads, self.transactions = release_reads, transactions
        self.confirmations = {}
        self.lock = asyncio.Lock()

    def panel(self, session):
        return ('<section class="panel" id="release-editor"><h2>填写更新弹窗</h2>'
            '<p>Codex 准备好版本后，你只需填写更新说明，预览并确认发布。</p>'
            '<p id="release-editor-status" class="hint" role="status">正在读取待发布版本…</p>'
            + self.form(session, "releases/preview",
                '<fieldset id="release-editor-fields" disabled>'
                '<label>待发布版本<select name="version" required><option value="">正在读取…</option></select></label>'
                '<input type="hidden" name="manifest_digest" value="">'
                '<label>弹窗标题<input name="title" maxlength="100" required placeholder="例如：✨ 新版本来了"></label>'
                '<label>更新说明<textarea name="notes" rows="9" maxlength="10000" required '
                'placeholder="✨ 新增功能&#10;修复了哪些问题？&#10;&#10;支持直接回车换行、空行和 emoji。"></textarea></label>'
                '<p class="hint">标题最多 100 字，说明最多 10000 字。按原文显示，支持换行、空行和 emoji。</p>'
                '<div class="release-actions"><button type="submit" formaction="' + PREFIX
                + 'releases/draft" class="secondary">保存草稿</button>'
                '<button type="submit">预览并发布</button></div></fieldset>')
            + '<noscript><p>请启用 JavaScript 后填写和发布更新说明。</p></noscript></section>')

    async def _candidate(self, version):
        raw, release = await self.releases.candidate(version)
        return release, hashlib.sha256(raw).hexdigest()

    def _draft_path(self, version):
        # Version is normalized by the signed candidate parser, never a request path.
        return self.directory / (str(Version(version)) + ".json")

    async def load(self, request):
        from aiohttp import web

        release, digest = await self._candidate(request.query.get("version", ""))
        try:
            value = await asyncio.to_thread(read_private_json, self._draft_path(release.version), maximum=MAX_MESSAGE_BYTES)
            value = parse_update_message(value, version=release.version, manifest_sha256=digest)
        except FileNotFoundError:
            value = {"schema_version": 1, "version": release.version, "manifest_sha256": digest,
                     "title": release.title, "notes": "\n".join(release.notes)}
        except (ValueError, UpdateMessageError):
            raise GitHubError("已保存草稿与当前候选不匹配，请重新核对候选版本。") from None
        return web.json_response(value)

    async def _save(self, request):
        form = request["admin_form"]
        release, digest = await self._candidate(str(form.get("version", "")))
        if form.get("manifest_digest") != digest:
            raise GitHubError("待发布版本已变化，请重新载入并核对更新说明。")
        try:
            message = create_update_message(version=release.version, manifest_sha256=digest,
                title=str(form.get("title", "")), notes=str(form.get("notes", "")))
        except UpdateMessageError as exc:
            raise GitHubError(str(exc)) from None
        await asyncio.to_thread(private_json, self._draft_path(release.version), message, replace=True)
        await self.audit("release_draft_save", release.version, manifest_sha256=digest)
        return message

    async def save(self, request):
        async with self.lock:
            message = await self._save(request)
        return await self.redirect(request, "releases", f'{message["version"]} 更新说明草稿已保存，尚未发布。')

    async def preview(self, request):
        async with self.lock:
            message = await self._save(request)
            stable = await self.releases.stable()
            if not is_pending_version(message["version"], stable["version"]):
                raise GitHubError("请选择高于当前稳定版的待发布版本。回滚请使用高级操作。")
            session = request["admin_session"]
            # One pending preview per session; expire old entries to bound memory.
            self.confirmations = {key: item for key, item in self.confirmations.items()
                                  if item["expires"] > self.clock()}
            if len(self.confirmations) >= 100:
                self.confirmations.clear()
            token = secrets.token_urlsafe(32)
            self.confirmations[session["csrf"]] = {
                "token": token, "message": message, "stable_digest": stable["digest"],
                "expires": self.clock() + 600,
            }
        body = ('<section class="panel"><h2>确认发布到线上</h2><p>当前稳定版：<strong>'
            + escape(stable["version"]) + '</strong> → 待发布：<strong>' + escape(message["version"])
            + '</strong></p><p>发布后，客户端检查更新时将看到以下内容：</p>'
            '<div class="release-preview"><h3>' + escape(message["title"]) + '</h3><div class="release-notes">'
            + escape(message["notes"]) + '</div></div>'
            '<p class="hint">将校验更新包并切换稳定版本。较大的更新可能需要几分钟，请等待结果。</p>'
            + self.form(session, "releases/publish", '<input type="hidden" name="confirmation" value="'
                + token + '">' + self.field("totp", "当前 6 位动态码",
                    extra='inputmode="numeric" pattern="[0-9]{6}" autocomplete="one-time-code"')
                + '<label><input type="checkbox" name="confirmed" value="yes" required>我已核对版本和更新说明，确认通知客户端更新</label>'
                '<div class="release-actions"><a href="' + PREFIX + 'releases">返回修改</a>'
                '<button type="submit" class="danger">确认一键发布线上</button></div>') + '</section>')
        return await self.render(request, "发布前二次确认", body
            + f'<script src="{PREFIX}release-editor.js" defer></script>')

    async def publish(self, request):
        session, form = request["admin_session"], request["admin_form"]
        async with self.lock:
            confirmation = self.confirmations.get(session["csrf"])
            submitted = str(form.get("confirmation", ""))
            if (not confirmation or confirmation["expires"] <= self.clock()
                    or not submitted.isascii() or not secrets.compare_digest(submitted, confirmation["token"])
                    or form.get("confirmed") != "yes"):
                raise GitHubError("发布确认已过期或尚未核对，请重新预览后发布。")
            result = await asyncio.to_thread(self.auth.authenticate, "", str(form.get("totp", "")),
                int(self.clock()), step_up=True)
            if result.status != "ok" or result.revision != session["revision"]:
                await self.audit("release_totp_locked" if result.status == "locked" else "release_totp_failed")
                raise GitHubError("动态码无效、已使用或账号已锁定。请重新预览并使用新的动态码。")
            self.confirmations.pop(session["csrf"])
            message = confirmation["message"]

            async def finish():
                try:
                    result = await self.releases.switch(message["version"], confirmation["stable_digest"],
                        message=message, expected_candidate_digest=message["manifest_sha256"])
                    await self.audit("release_publish", message["version"], changed=result["changed"],
                                     manifest_sha256=message["manifest_sha256"])
                    return f'已发布 {message["version"]}，更新说明和稳定清单均通过公网验证。'
                except GitHubError as exc:
                    await self.audit("release_publish_failed", message["version"])
                    return str(exc)
                finally:
                    self.release_reads.invalidate()

            task = asyncio.create_task(finish())
            self.transactions.add(task)
            task.add_done_callback(self.transactions.discard)
        # The shared GitHub lock serializes publication; disconnecting must not cancel it.
        result = await asyncio.shield(task)
        return await self.redirect(request, "releases", result)
