"""Account-bound scheduler and Plugin Page API for explicitly created tasks."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

from .content import ACTION_NAMES, ContentService, ContentSkipped
from .group_analysis_schedule import require_analysis_ownership
from .task_store import (
    TaskStore,
    SCHEDULED_COMMAND_VERSION,
    normalize_arguments,
    normalize_task,
    schedule_text,
    scheduled_command_catalog,
)


logger = logging.getLogger("jiang_schedule.unified")
PROTOCOL_VERSION = 1


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RuntimeLease:
    """An OS-owned lock prevents reloads/processes from recovering live runs."""
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None


class UnifiedRuntime:
    def __init__(self, context, *, install_root=None, store=None, content=None,
                 connection=None, sender=None, clock=None):
        self.context = context
        self.root = Path(install_root) if install_root else Path(__file__).resolve().parents[5]
        self.store = store or TaskStore(self.root / "data/manager/scheduled-tasks.db")
        self.content = content or ContentService(context, self.root)
        self.connection = connection or self._connection
        self.sender = sender or self._send
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._poller = None
        self._active = False
        self._lease = RuntimeLease(self.root / "data/manager/scheduled-tasks.lock")
        self._workers: dict[str, asyncio.Task] = {}
        self._run_slots = asyncio.Semaphore(3)
        self._analysis_slots = asyncio.Semaphore(1)

    def _bridge_health(self) -> dict:
        try:
            config = json.loads((self.root / "app/bridge/config.json").read_text("utf-8-sig"))
            port = config.get("callback_port", 5000)
            if type(port) is not int or not 1 <= port <= 65535:
                return {}
            if config.get("callback_host", "127.0.0.1") not in {"127.0.0.1", "localhost", "::1"}:
                return {}
            request = Request(f"http://127.0.0.1:{port}/api/health", headers={"Cache-Control": "no-cache"})
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=2) as response:
                raw = response.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                return {}
            health = json.loads(raw.decode("utf-8"))
            required = ("service_ready", "status_available", "hook_ready", "wechat_logged_in",
                        "upstream_connected", "heartbeat_fresh")
            if not isinstance(health, dict) or not all(health.get(key) is True for key in required):
                return {}
            return health
        except (OSError, ValueError, AttributeError):
            return {}

    async def _connection(self):
        health = await asyncio.to_thread(self._bridge_health)
        account = str(health.get("wechat_identity") or "").strip()
        if not account:
            return "", None
        platforms = getattr(getattr(self.context, "platform_manager", None), "platform_insts", [])
        for platform in platforms:
            metadata = platform.meta()
            if getattr(metadata, "name", "") != "aiocqhttp":
                continue
            client = getattr(platform, "bot", None)
            if client is None:
                continue
            try:
                login = await asyncio.wait_for(client.get_login_info(self_id=account), timeout=5)
            except Exception:
                continue
            if isinstance(login, dict) and str(login.get("user_id") or "") == account:
                return account, client
        return "", None

    async def _require_account(self, expected: str):
        account, client = await self.connection()
        if not account or account != expected or client is None:
            raise ValueError("微信账号未连接或已经切换，请重新连接原账号后操作。")
        return client

    async def _target_exists(self, client, account: str, target: dict) -> bool:
        """Confirm the stable recipient against the live account immediately before send."""
        try:
            if target["type"] == "group":
                item = await asyncio.wait_for(
                    client.get_group_info(group_id=target["id"], self_id=account), timeout=10,
                )
                identity = item.get("group_id") if isinstance(item, dict) else ""
            else:
                item = await asyncio.wait_for(
                    client.get_user_info(user_id=target["id"], self_id=account), timeout=10,
                )
                identity = item.get("user_id") if isinstance(item, dict) else ""
            return str(identity or "") == target["id"]
        except Exception:
            return False

    def _require_content(self, task: dict):
        self.content.require_ready(task["action"])
        if task["action"] == "weather" and task["arguments"].get("output", "image") != "image":
            raise ValueError("当前定时天气使用与手动查询一致的天气卡片。")

    async def health(self) -> dict:
        account, client = await self.connection()
        ready = bool(self._active and account and client is not None)
        actions = {action: self.content.readiness(action) for action in ACTION_NAMES}
        return {"version": PROTOCOL_VERSION, "protocol_version": PROTOCOL_VERSION,
                "ready": ready, "runtime_ready": ready, "account_id": account,
                "message": "" if ready else "定时任务服务正在等待微信连接。",
                "actions": actions,
                "command_catalog": scheduled_command_catalog(),
                "command_definition_version": SCHEDULED_COMMAND_VERSION,
                "capabilities": [{"id": action, "name": name, **actions[action]}
                                 for action, name in ACTION_NAMES.items()]}

    async def save(self, payload: dict) -> dict:
        if not self._active:
            raise ValueError("定时任务服务尚未就绪，请稍后重试。")
        task = normalize_task(payload, now=self.clock())
        if task.get("definition_version") != SCHEDULED_COMMAND_VERSION:
            raise ValueError("命令定义已更新，请重新检查任务参数和依赖后再启用。")
        await self._require_account(task["account_id"])
        if not task["draft"]:
            self._require_content(task)
        # Checking account again after readiness prevents a configuration read
        # from authorizing a task against a newly logged-in account.
        await self._require_account(task["account_id"])
        if task["action"] == "group_analysis" and task["enabled"]:
            require_analysis_ownership(self.root, self.store, task["account_id"], claim=True)
        return self.store.save(payload, now=self.clock())

    async def toggle(self, payload: dict) -> dict:
        account = payload.get("account_id")
        await self._require_account(account)
        if type(payload.get("enabled")) is not bool:
            raise ValueError("任务启用状态格式不正确。")
        task = self.store.get(account, payload.get("id"))
        if payload["enabled"]:
            if not self._active:
                raise ValueError("定时任务服务尚未就绪，请稍后重试。")
            self._require_content(task)
            if task["action"] == "group_analysis":
                require_analysis_ownership(self.root, self.store, account, claim=True)
        return self.store.set_enabled(account, task["id"], payload["enabled"], payload.get("revision"))

    async def delete(self, payload: dict) -> dict:
        await self._require_account(payload.get("account_id"))
        self.store.delete(payload["account_id"], payload.get("id"), payload.get("revision"))
        return {"deleted": True}

    async def preview(self, payload: dict) -> dict:
        account = payload.get("account_id")
        client = await self._require_account(account)
        task = {"action": payload.get("action"), "account_id": account,
                "arguments": normalize_arguments(payload.get("action"), payload.get("arguments", {}))}
        if task["action"] == "group_analysis":
            targets = payload.get("targets", [])
            target = payload.get("preview_target")
            if target is None and isinstance(targets, list) and len(targets) == 1:
                target = targets[0]
            if not isinstance(target, dict):
                raise ValueError("请选择一个需要预览分析报告的群聊。")
            task = normalize_task({**task, "targets": [target], "draft": False,
                                   "schedule": {"kind": "daily", "time": "23:59"}}, now=self.clock())
            if not await self._target_exists(client, account, task["targets"][0]):
                raise ValueError("该群已不在当前微信账号目录。")
        self._require_content(task)
        content = await asyncio.wait_for(self.content.generate(task), timeout=self._generation_timeout(task))
        client = await self._require_account(account)
        if task["action"] == "group_analysis" and not await self._target_exists(client, account, task["targets"][0]):
            raise ValueError("该群已不在当前微信账号目录。")
        return content.preview()

    async def test(self, payload: dict) -> dict:
        raw_target = payload.get("test_target")
        if not isinstance(raw_target, dict):
            raise ValueError("请明确选择一个测试发送对象。")
        account = payload.get("account_id")
        await self._require_account(account)
        current = self.clock()
        future_clock = current.astimezone(timezone(timedelta(hours=8))) + timedelta(minutes=10)
        display_name = str(payload.get("name") or ACTION_NAMES.get(payload.get("action"), "定时任务"))
        task_payload = {
            **payload,
            "id": "test_" + uuid.uuid4().hex,
            "name": ("测试 · " + display_name)[:100],
            "targets": [raw_target],
            "schedule": {"kind": "daily", "time": future_clock.strftime("%H:%M"), "timezone": "Asia/Shanghai"},
            "enabled": True,
            "draft": False,
            "revision": 0,
        }
        task = normalize_task(task_payload, now=current, _allow_test=True)
        self._require_content(task)
        await self._require_account(account)
        saved = self.store.save(task_payload, now=current, _allow_test=True)
        run = self.store.create_run_now(account, saved["id"], raw_target, now=current)
        try:
            await self.execute_run(run)
            result = self.store.runs(account, saved["id"])[0]["deliveries"][0]
        finally:
            try:
                latest = self.store.get(account, saved["id"])
                self.store.delete(account, saved["id"], latest["revision"])
            except ValueError:
                pass
        status = result["status"]
        message = {
            "submitted": "测试消息已提交微信发送。",
            "unknown": "测试消息结果待确认，不会自动重发。",
            "failed": "测试消息发送失败，请检查聊天权限。",
            "skipped": result.get("message") or "测试消息已取消。",
        }.get(status, "测试发送已结束。")
        return {"status": status, "message": message, "target": raw_target}

    def register_apis(self):
        if not hasattr(self.context, "register_web_api"):
            return
        from astrbot.api.web import error_response, json_response, request

        def endpoint(operation):
            async def handler():
                try:
                    payload = {} if operation == "health" else await request.json(default={})
                    if not isinstance(payload, dict):
                        raise ValueError("任务配置格式不正确。")
                    result = await getattr(self, operation)() if operation == "health" else await getattr(self, operation)(payload)
                    if operation in {"save", "toggle"}:
                        result = {"item": result, "message": "任务已保存。"}
                    return json_response({"status": "ok", "data": result})
                except ValueError as exc:
                    return error_response(str(exc), status_code=400)
                except Exception as exc:
                    # Upstream URLs and credentials can occur in exception text.
                    logger.warning("统一定时任务接口 %s 失败 (%s)", operation, type(exc).__name__)
                    return error_response("本次操作未完成，请检查功能配置或连接后重试。", status_code=502)
            handler.__name__ = "unified_" + operation
            return handler

        for operation in ("health", "save", "delete", "toggle", "preview", "test"):
            self.context.register_web_api(
                f"/jiang_schedule/page/unified/{operation}", endpoint(operation),
                ["GET"] if operation == "health" else ["POST"],
                "Unified scheduled tasks " + operation,
            )

    def start(self):
        if self._poller is None or self._poller.done():
            self._poller = asyncio.get_running_loop().create_task(self._poll(), name="jiang-unified-schedules")

    async def stop(self):
        self._active = False
        if self._poller is not None:
            self._poller.cancel()
            await asyncio.gather(self._poller, return_exceptions=True)
            self._poller = None
        await self._stop_workers()

    async def _stop_workers(self):
        workers = list(self._workers.items())
        for _run_id, task in workers:
            task.cancel()
        await asyncio.gather(*(task for _run_id, task in workers), return_exceptions=True)
        # A worker cancelled before its coroutine starts cannot run finally.
        for run_id, _task in workers:
            self.store.finish_run(run_id)
        self._workers.clear()

    async def _poll(self):
        try:
            while not self._lease.acquire():
                await asyncio.sleep(2)
            self.store.recover_interrupted()
            self._active = True
            while True:
                try:
                    await self._dispatch_due()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("统一定时任务检查失败 (%s)", type(exc).__name__)
                await asyncio.sleep(5)
        finally:
            self._active = False
            try:
                await self._stop_workers()
            finally:
                self._lease.release()

    async def run_once(self):
        workers = await self._dispatch_due()
        try:
            await asyncio.gather(*workers.values(), return_exceptions=True)
        finally:
            for run_id in workers:
                self.store.finish_run(run_id)

    async def _dispatch_due(self) -> dict[str, asyncio.Task]:
        """Claim quickly so a long report never blocks the next scheduler poll."""
        account, client = await self.connection()
        if not account or client is None:
            return {}
        runs = self.store.claim_due(self.clock(), account)
        workers = {}
        for run in runs:
            task = asyncio.create_task(self._run_limited(run), name="unified-task-" + run["id"])
            self._workers[run["id"]] = workers[run["id"]] = task
            task.add_done_callback(lambda task, run_id=run["id"]: self._worker_finished(run_id, task))
        return workers

    async def _run_limited(self, run: dict):
        try:
            slots = self._analysis_slots if run["task"]["action"] == "group_analysis" else self._run_slots
            async with slots:
                await self.execute_run(run)
        finally:
            self.store.finish_run(run["id"])

    def _worker_finished(self, run_id: str, task: asyncio.Task):
        self._workers.pop(run_id, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("本次定时任务未完成 (%s)", type(error).__name__)

    def _current(self, run: dict) -> bool:
        try:
            task = self.store.get(run["account_id"], run["task_id"])
        except ValueError:
            return False
        return bool(task["enabled"] and not task["draft"] and task["revision"] == run["task"]["revision"])

    def _mark(self, run: dict, target: dict, status: str, message: str = ""):
        try:
            self.store.set_delivery(run["id"], target["type"], target["id"], status, message)
            return True
        except ValueError:
            # Pause/delete can have atomically finalized the pending delivery.
            return False

    @staticmethod
    def _generation_timeout(task: dict) -> int:
        return 1800 if task["action"] == "group_analysis" else 120

    async def _delivery_client(self, run: dict, target: dict):
        if not self._current(run):
            self._mark(run, target, "skipped", "任务已暂停、删除或修改，本次未发送。")
            return None
        try:
            client = await self._require_account(run["account_id"])
            self._require_content(run["task"])
        except ValueError:
            self._mark(run, target, "skipped", "原账号未连接或所需功能已关闭，本次未发送。")
            return None
        if not await self._target_exists(client, run["account_id"], target):
            self._mark(run, target, "skipped", "发送对象已不在当前账号目录，本次未发送。")
            return None
        try:
            return await self._require_account(run["account_id"])
        except ValueError:
            self._mark(run, target, "skipped", "核对发送对象后账号发生变化，本次未发送。")
            return None

    async def execute_run(self, run: dict):
        try:
            if not self._current(run):
                return
            per_target = run["task"]["action"] == "group_analysis"
            content = None
            try:
                await self._require_account(run["account_id"])
                self._require_content(run["task"])
                if per_target and not run["task_id"].startswith("test_"):
                    require_analysis_ownership(self.root, self.store, run["account_id"])
                if not per_target:
                    content = await asyncio.wait_for(self.content.generate(run["task"]), timeout=120)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("定时内容生成失败 (%s)", type(exc).__name__)
                for target in run["deliveries"]:
                    self._mark(run, target, "failed", "内容生成失败，请检查功能配置和服务连接。")
                return
            for target in run["deliveries"]:
                if target["status"] != "pending":
                    continue
                client = await self._delivery_client(run, target)
                if client is None:
                    continue
                if per_target:
                    try:
                        task = {**run["task"], "targets": [{"type": target["type"], "id": target["id"]}]}
                        content = await asyncio.wait_for(self.content.generate(task), timeout=self._generation_timeout(task))
                    except ContentSkipped as exc:
                        self._mark(run, target, "skipped", str(exc))
                        continue
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("群分析内容生成失败 (%s)", type(exc).__name__)
                        self._mark(run, target, "failed", "该群报告生成失败，请检查模型、群权限和消息记录。")
                        continue
                    # Analysis may take several minutes. Recheck identity,
                    # directory and task revision after all generation awaits.
                    client = await self._delivery_client(run, target)
                    if client is None:
                        continue
                if not self._current(run) or not self._mark(run, target, "sending"):
                    continue
                if not self.store.authorize_delivery(run["account_id"], run["task_id"], run["id"], target["type"], target["id"]):
                    self._mark(run, target, "skipped", "任务权限已变化，本次未发送。")
                    continue
                try:
                    response = await asyncio.wait_for(self.sender(client, run, target, content), timeout=180)
                except asyncio.CancelledError:
                    self._mark(run, target, "unknown", "发送时服务中断，结果待确认，不会自动重发。")
                    raise
                except Exception as exc:
                    status = "failed" if getattr(exc, "retcode", None) in {1403, 10003, 10004} else "unknown"
                    message = "发送被拒绝，请检查聊天权限或发送对象。" if status == "failed" else "发送结果待确认，不会自动重发。"
                    self._mark(run, target, status, message)
                    continue
                if isinstance(response, dict) and (response.get("status") == "failed" or response.get("retcode", 0) != 0):
                    self._mark(run, target, "failed", "发送被拒绝，请检查聊天权限或发送对象。")
                elif isinstance(response, dict) and (response.get("message_id") is not None or
                      isinstance(response.get("data"), dict) and response["data"].get("message_id") is not None):
                    self._mark(run, target, "submitted", "已提交微信发送。")
                else:
                    self._mark(run, target, "unknown", "未收到明确的发送回执，不会自动重发。")
        finally:
            self.store.finish_run(run["id"])

    @staticmethod
    async def _send(client, run: dict, target: dict, content):
        kwargs = {"message": content.segments(), "self_id": run["account_id"],
                  "_wechatrebot_delivery": "scheduled", "_wechatrebot_task_id": run["task_id"],
                  "_wechatrebot_run_id": run["id"], "_wechatrebot_account": run["account_id"]}
        if target["type"] == "group":
            return await client.send_group_msg(group_id=target["id"], **kwargs)
        return await client.send_private_msg(user_id=target["id"], **kwargs)

    async def command_add(self, event, action: str, location: str, hour: int, minute: int, day: str = "") -> str:
        # Main checks reliable group-admin/private-sender permissions first.
        account, _client = await self.connection()
        if not account:
            raise ValueError("请先连接微信，再创建当前聊天的定时任务。")
        actual = str(getattr(getattr(event, "message_obj", None), "self_id", "") or "")
        if actual != account:
            raise ValueError("消息所属账号与当前微信不一致，本次没有创建。")
        kind = "private" if event.is_private_chat() else "group"
        identity = str(event.get_sender_id() if kind == "private" else event.get_group_id())
        args = {"city": location, "day": "today", "output": "image"} if action == "weather" else {"province": location} if action == "oilprice" else {"count": 20} if action == "news" else {}
        schedule = {"kind": "weekly" if day else "daily", "time": f"{hour:02d}:{minute:02d}", "timezone": "Asia/Shanghai"}
        if day:
            schedule["weekdays"] = [["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(day)]
        event_id = str(getattr(event, "get_message_id", lambda: "")() or "")
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"jiang-schedule:{account}:{kind}:{identity}:{event_id}")) if event_id else str(uuid.uuid4())
        task = {"id": task_id, "account_id": account, "action": action, "arguments": args,
                "targets": [{"type": kind, "id": identity}], "schedule": schedule, "enabled": True, "draft": False,
                "name": ACTION_NAMES[action] + (f"（{location}）" if location else "")}
        task = await self.save(task)
        return f"已添加「{task['name']}」→ {schedule_text(task['schedule'])}，仅发送到当前聊天。可在管理器定时任务中编辑、复制和删除。"

    async def command_tasks(self, event) -> list[dict]:
        account, _client = await self.connection()
        actual = str(getattr(getattr(event, "message_obj", None), "self_id", "") or "")
        if not account or actual != account:
            return []
        kind = "private" if event.is_private_chat() else "group"
        identity = str(event.get_sender_id() if kind == "private" else event.get_group_id())
        return [task for task in self.store.list(account) if {"type": kind, "id": identity} in task["targets"]]

    async def command_delete(self, event, action: str) -> bool:
        kind = "private" if event.is_private_chat() else "group"
        identity = str(event.get_sender_id() if kind == "private" else event.get_group_id())
        matched = [task for task in await self.command_tasks(event) if task["action"] == action]
        for task in matched:
            remaining = [target for target in task["targets"] if target != {"type": kind, "id": identity}]
            if remaining:
                # A chat administrator may remove their chat's recipient, never
                # cancel a multi-chat task on behalf of other recipients.
                task["targets"] = remaining
                self.store.save(task, now=self.clock())
            else:
                self.store.delete(task["account_id"], task["id"], task["revision"])
        return bool(matched)
