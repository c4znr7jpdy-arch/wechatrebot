"""AstrBot 分类影视、体育搜索与微信播放卡片插件。"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Share
from astrbot.api.star import StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.message_event_result import MessageChain

from .search import (
    CollectionPage,
    Episode,
    SearchResult,
    ShortDramaSearcher,
    extract_media_query,
    extract_sports_query,
    is_media_help_command,
    is_recommendation_command,
    known_title_hints,
    parse_episode_request,
    parse_sports_collection_query,
    parse_variant_request,
    select_episode,
)
from .player_server import ShortDramaPlayerServer
from .selection_store import RemoteSelectionStore
from .schedule_store import (
    RecommendationScheduleStore,
    ScheduleConfigError,
    task_cron_expression,
    validate_task,
)
from .watch_url import (
    build_stateless_playlist_url,
    build_stateless_recommendations_url,
    build_stateless_search_url,
    build_watch_url,
)


logger = logging.getLogger("jiang_short_drama")
_DEFAULT_COVER = "https://duanjubaike.cn/favicon.ico"
_SCHEDULE_JOB_PREFIX = "short_drama_recommend_"
_ACTIVE_PLUGIN = None


def _recommendation_heading(media_type: str) -> str:
    if media_type == "全部":
        return "最新全分类推荐"
    return f"最新{media_type}推荐"


def _first_recommendation_cover(
    recommendations: tuple[SearchResult, ...],
) -> str:
    """优先使用推荐列表第一条有封面的作品；都没有时回退默认图。"""
    for result in recommendations:
        cover_url = (result.cover_url or "").strip()
        if cover_url:
            return cover_url
    return _DEFAULT_COVER


def _recommendation_cover_candidates(
    recommendations: tuple[SearchResult, ...],
    limit: int = 6,
) -> tuple[str, ...]:
    """Return ordered, unique HTTP(S) covers for a recommendation card."""
    candidates: list[str] = []
    seen: set[str] = set()
    for result in recommendations:
        cover_url = (result.cover_url or "").strip()
        parsed = urlsplit(cover_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or cover_url in seen
        ):
            continue
        seen.add(cover_url)
        candidates.append(cover_url)
        if len(candidates) >= max(1, int(limit)):
            break
    return tuple(candidates)


async def _scheduled_recommendation_callback(
    task_id: str = "",
    **_: object,
) -> None:
    plugin = _ACTIVE_PLUGIN
    if plugin is None:
        logger.warning("[SHORT_DRAMA] scheduled task skipped: plugin unavailable")
        return
    try:
        await plugin._execute_schedule_task(task_id)
    except Exception:
        logger.exception(
            "[SHORT_DRAMA] scheduled recommendation failed task=%s",
            task_id,
        )


def _strip_system_identity_prefix(text: str) -> str:
    value = text or ""
    if value.startswith("[系统身份提示：") and "]\n" in value:
        return value.split("]\n", 1)[1]
    return value


def _safe_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(float(value), maximum))
    except (TypeError, ValueError):
        return default


def _compact_play_card_text(
    result: SearchResult,
    episode: Episode,
) -> tuple[str, str]:
    """生成适合微信小卡片的精简标题和单行摘要。"""
    title_suffix = "完整版" if result.is_full_version else episode.name
    title_limit = 48
    title_prefix_limit = max(1, title_limit - len(title_suffix) - 1)
    title = f"{result.title[:title_prefix_limit]}｜{title_suffix}"
    details = [value for value in (result.year, result.category) if value]
    if result.is_full_version:
        minutes = max(1, round(result.duration_seconds / 60))
        details.append(f"约{minutes}分钟")
    else:
        details.append(f"共{len(result.episodes)}集")
    details.append("点击播放")
    return title, " · ".join(details)


def _compact_variants_card_text(
    query_title: str,
    variants: tuple[SearchResult, ...],
) -> tuple[str, str]:
    """生成精简的多版本卡片标题和年份预览。"""
    title_suffix = f"{len(variants)}个版本"
    title_limit = 48
    title_prefix_limit = max(1, title_limit - len(title_suffix) - 1)
    title = f"{query_title[:title_prefix_limit]}｜{title_suffix}"
    years: list[str] = []
    for result in variants:
        year = result.year.strip()
        if year and year not in years:
            years.append(year)
    if years:
        years.sort(
            key=lambda value: (
                not value.isdigit(),
                int(value) if value.isdigit() else value,
            )
        )
        preview = "、".join(years[:4])
        if len(years) > 4:
            preview += "等"
        description = f"{preview} · 点击选择版本"
    else:
        description = f"共{len(variants)}个可播放版本 · 点击选择"
    return title, description


def _compact_search_card_text(
    query_title: str,
    media_label: str,
    results: tuple[SearchResult, ...],
) -> tuple[str, str]:
    """生成普通搜索中间页卡片的标题和相关性说明。"""
    suffix = f"{len(results)}个结果"
    prefix = f"{media_label or '影视资源'}｜{query_title}"
    title_limit = 48
    title_prefix_limit = max(1, title_limit - len(suffix) - 1)
    title = f"{prefix[:title_prefix_limit]}｜{suffix}"
    exact_count = sum(1 for result in results if result.score >= 10_000)
    if exact_count:
        description = f"{exact_count}个精准匹配 · 按相关性排序 · 点击选择播放"
    else:
        description = "按标题相关性排序 · 点击选择播放"
    return title, description


def _compact_people_card_text(
    query_title: str,
    media_label: str,
    results: tuple[SearchResult, ...],
) -> tuple[str, str]:
    """生成演员/导演相关作品卡片标题和年份预览。"""
    title_suffix = f"{len(results)}部"
    prefix = f"{query_title}相关{media_label}"
    title_limit = 48
    title_prefix_limit = max(1, title_limit - len(title_suffix) - 1)
    title = f"{prefix[:title_prefix_limit]}｜{title_suffix}"
    years: list[str] = []
    for result in results:
        year = result.year.strip()
        if year and year not in years:
            years.append(year)
    if years:
        years.sort(
            key=lambda value: (
                value.isdigit(),
                int(value) if value.isdigit() else 0,
            ),
            reverse=True,
        )
        preview = "、".join(years[:4])
        if len(years) > 4:
            preview += "等"
        description = f"{preview} · 点击选择播放"
    else:
        description = f"共{len(results)}部可播放作品 · 点击选择"
    return title, description


def _media_help_text() -> str:
    return "\n".join(
        (
            "🎬 影视资源使用帮助",
            "命令与名称之间须留一个空格",
            "",
            "🔍 分类搜索",
            "• 短剧 剧名（含AI漫剧）",
            "• 电视剧 剧名",
            "• 电影 片名",
            "• 动漫 名称",
            "• 综艺 名称",
            "• 剧 名称（搜索全部分类）",
            "普通搜索会先进入按相关性排序的结果列表，再点击作品播放",
            "",
            "🏟️ 体育回放",
            "• 体育 足球",
            "• 体育 CBA2023",
            "• 体育 完整赛事名",
            "",
            "✨ 最新推荐",
            "• 短剧推荐（最新12部）",
            "",
            "⚙️ 选集与版本",
            "• 电视剧 剧名 第12集",
            "• 电影 片名 版本",
            "• 电影 片名 版本2",
        )
    )


class Main(star.Star):
    def __init__(
        self,
        context: star.Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        global _ACTIVE_PLUGIN
        super().__init__(context)
        self.context = context
        self.config = config or {}
        _ACTIVE_PLUGIN = self
        self.searcher = ShortDramaSearcher(
            timeout_seconds=_safe_float(
                self.config.get("request_timeout_seconds"), 10, 2, 30
            ),
            max_concurrent=_safe_int(
                self.config.get("max_concurrent_sources"), 4, 1, 8
            ),
            cache_ttl_seconds=_safe_int(
                self.config.get("cache_ttl_minutes"), 360, 0, 1440
            )
            * 60,
            full_version_min_minutes=_safe_float(
                self.config.get("full_version_min_minutes"), 30, 5, 300
            ),
            min_fuzzy_score=_safe_int(
                self.config.get("min_fuzzy_score"), 3000, 1, 9999
            ),
        )
        self.player_server = ShortDramaPlayerServer(
            bind_host=str(
                self.config.get("episode_player_bind_host") or "127.0.0.1"
            ),
            port=_safe_int(
                self.config.get("episode_player_port"), 6197, 1, 65_535
            ),
            public_base_url=self.config.get("episode_player_public_base_url"),
            token_ttl_seconds=_safe_int(
                self.config.get("episode_player_token_ttl_minutes"),
                360,
                1,
                1440,
            )
            * 60,
            max_playlists=_safe_int(
                self.config.get("episode_player_max_playlists"),
                200,
                10,
                2000,
            ),
        )
        self.player_server.collection_loader = self.searcher.search_collection_page
        self.selection_store = RemoteSelectionStore(
            api_url=self.config.get("selection_store_api_url"),
            api_token=self.config.get("selection_store_api_token"),
            player_url=self.config.get("stateless_player_url"),
            timeout_seconds=_safe_float(
                self.config.get("selection_store_timeout_seconds"), 8, 2, 20
            ),
        )
        data_dir = Path(StarTools.get_data_dir("jiang_short_drama"))
        self.schedule_store = RecommendationScheduleStore(
            data_dir / "recommendation_schedules.json"
        )
        self._schedule_lock = asyncio.Lock()
        self._register_page_apis()
        configured_base = self.config.get("episode_player_public_base_url")
        if configured_base and not self.player_server.configured:
            logger.warning(
                "[SHORT_DRAMA] episode_player_public_base_url 格式无效，"
                "分集卡片将使用单集播放器"
            )

    async def initialize(self) -> None:
        await self.player_server.start()
        await self._sync_schedule_jobs()

    async def terminate(self) -> None:
        global _ACTIVE_PLUGIN
        await self._remove_schedule_jobs()
        await self.player_server.stop()
        if _ACTIVE_PLUGIN is self:
            _ACTIVE_PLUGIN = None

    def _register_page_apis(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[SHORT_DRAMA] current AstrBot has no Plugin Pages support")
            return
        routes = (
            ("schedules", self.page_get_schedules, ["GET"], "List recommendation schedules"),
            ("schedules/save", self.page_save_schedule, ["POST"], "Save recommendation schedule"),
            ("schedules/delete", self.page_delete_schedule, ["POST"], "Delete recommendation schedule"),
            ("schedules/toggle", self.page_toggle_schedule, ["POST"], "Toggle recommendation schedule"),
            ("schedules/test", self.page_test_schedule, ["POST"], "Test recommendation schedule"),
        )
        for path, handler, methods, description in routes:
            self.context.register_web_api(
                f"/jiang_short_drama/page/{path}",
                handler,
                methods,
                description,
            )

    async def page_get_schedules(self):
        return json_response(
            {"status": "ok", "data": self.schedule_store.public_config()}
        )

    async def page_save_schedule(self):
        try:
            payload = await request.json(default={})
            async with self._schedule_lock:
                task = self.schedule_store.upsert((payload or {}).get("task", payload))
                await self._sync_schedule_jobs()
            return json_response(
                {
                    "status": "ok",
                    "data": {
                        **self.schedule_store.public_config(),
                        "saved_id": task["id"],
                    },
                }
            )
        except ScheduleConfigError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.exception("[SHORT_DRAMA] WebUI schedule save failed")
            return error_response(str(exc), status_code=500)

    async def page_delete_schedule(self):
        try:
            payload = await request.json(default={})
            task_id = str((payload or {}).get("id") or "").strip()
            async with self._schedule_lock:
                if not self.schedule_store.delete(task_id):
                    raise ScheduleConfigError("没有找到这个定时任务")
                await self._sync_schedule_jobs()
            return json_response(
                {"status": "ok", "data": self.schedule_store.public_config()}
            )
        except ScheduleConfigError as exc:
            return error_response(str(exc), status_code=404)
        except Exception as exc:
            logger.exception("[SHORT_DRAMA] WebUI schedule delete failed")
            return error_response(str(exc), status_code=500)

    async def page_toggle_schedule(self):
        try:
            payload = await request.json(default={})
            task_id = str((payload or {}).get("id") or "").strip()
            current = self.schedule_store.get(task_id)
            if current is None:
                raise ScheduleConfigError("没有找到这个定时任务")
            current["enabled"] = bool((payload or {}).get("enabled"))
            async with self._schedule_lock:
                self.schedule_store.upsert(current)
                await self._sync_schedule_jobs()
            return json_response(
                {"status": "ok", "data": self.schedule_store.public_config()}
            )
        except ScheduleConfigError as exc:
            return error_response(str(exc), status_code=404)
        except Exception as exc:
            logger.exception("[SHORT_DRAMA] WebUI schedule toggle failed")
            return error_response(str(exc), status_code=500)

    async def page_test_schedule(self):
        try:
            payload = await request.json(default={})
            task_id = str((payload or {}).get("id") or "").strip()
            if task_id:
                task = self.schedule_store.get(task_id)
                if task is None:
                    raise ScheduleConfigError("没有找到这个定时任务")
            else:
                task = validate_task((payload or {}).get("task", payload))
            result = await self._send_scheduled_recommendations(task)
            return json_response({"status": "ok", "data": result})
        except ScheduleConfigError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.exception("[SHORT_DRAMA] WebUI schedule test failed")
            return error_response(str(exc), status_code=502)

    async def _remove_schedule_jobs(self) -> None:
        try:
            jobs = await self.context.cron_manager.list_jobs(job_type="basic")
            for job in jobs:
                if job.name.startswith(_SCHEDULE_JOB_PREFIX):
                    await self.context.cron_manager.delete_job(job.job_id)
        except Exception:
            logger.exception("[SHORT_DRAMA] failed to remove schedule jobs")

    async def _sync_schedule_jobs(self) -> None:
        await self._remove_schedule_jobs()
        for task in self.schedule_store.load():
            if not task.get("enabled"):
                continue
            await self.context.cron_manager.add_basic_job(
                name=f"{_SCHEDULE_JOB_PREFIX}{task['id']}",
                cron_expression=task_cron_expression(task),
                handler=_scheduled_recommendation_callback,
                payload={"task_id": task["id"]},
                description=f"{task['media_type']}最新资源定时推荐",
                timezone="Asia/Shanghai",
                persistent=True,
            )
            logger.info(
                "[SHORT_DRAMA] schedule registered id=%s cron=%s session=%s",
                task["id"],
                task_cron_expression(task),
                task["session"],
            )

    async def _remote_selection_url(
        self,
        results: tuple[SearchResult, ...],
        *,
        heading: str,
        kind: str,
    ) -> str | None:
        store = getattr(self, "selection_store", None)
        if store is None:
            return None
        return await store.create_url(results, heading=heading, kind=kind)

    async def _execute_schedule_task(self, task_id: str) -> dict[str, object]:
        task = self.schedule_store.get(task_id)
        if task is None or not task.get("enabled"):
            logger.info("[SHORT_DRAMA] disabled/missing schedule skipped id=%s", task_id)
            return {"sent": False, "count": 0}
        return await self._send_scheduled_recommendations(task)

    async def _send_scheduled_recommendations(
        self,
        task: dict[str, object],
    ) -> dict[str, object]:
        media_type = str(task.get("media_type") or "短剧")
        search_media_type = None if media_type == "全部" else media_type
        results = await self.searcher.recommend_results(
            int(task.get("limit") or 12),
            media_type=search_media_type,
        )
        if not results:
            raise RuntimeError(f"当前资源站没有可推荐的{media_type}资源")

        heading = _recommendation_heading(media_type)
        recommendations_url = self.player_server.create_recommendations_url(
            results,
            heading,
        )
        if not recommendations_url:
            recommendations_url = await self._remote_selection_url(
                results,
                heading=heading,
                kind="recommendations",
            )
        if not recommendations_url:
            recommendations_url = build_stateless_recommendations_url(
                results,
                self.config.get("stateless_player_url"),
                heading,
            )
        if recommendations_url:
            cover_candidates = _recommendation_cover_candidates(results)
            card_cover = (
                self.player_server.create_recommendation_cover_url(cover_candidates)
                or (cover_candidates[0] if cover_candidates else _DEFAULT_COVER)
            )
            chain = MessageChain(
                [
                    Share(
                        url=recommendations_url,
                        title=f"{heading}｜{len(results)}部",
                        content="定时推荐 · 点击查看封面并选择播放",
                        image=card_cover,
                    )
                ]
            )
        else:
            chain = MessageChain(
                [Plain("\n".join(item.title for item in results))]
            )
        sent = await self.context.send_message(str(task["session"]), chain)
        if not sent:
            raise RuntimeError("没有找到该 UMO 对应的消息平台")
        logger.info(
            "[SHORT_DRAMA] scheduled recommendation sent id=%s media=%s count=%s session=%s",
            task.get("id") or "test",
            media_type,
            len(results),
            task["session"],
        )
        return {
            "sent": True,
            "count": len(results),
            "media_type": media_type,
            "session": task["session"],
        }

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def short_drama(self, event: AstrMessageEvent):
        """严格匹配分类影视或体育命令并发送微信链接卡片。"""
        text = _strip_system_identity_prefix(event.get_message_str())
        if is_recommendation_command(text):
            event.stop_event()
            logger.info(
                "[SHORT_DRAMA] recommendation request sender=%s group=%s",
                event.get_sender_id(),
                event.get_group_id() or "private",
            )
            recommendations = await self.searcher.recommend_results(12)
            if not recommendations:
                yield event.plain_result("当前资源站暂时没有可推荐的短剧")
                return
            recommendations_url = self.player_server.create_recommendations_url(
                recommendations
            )
            if not recommendations_url:
                recommendations_url = await self._remote_selection_url(
                    recommendations,
                    heading="最新短剧推荐",
                    kind="recommendations",
                )
            if not recommendations_url:
                recommendations_url = build_stateless_recommendations_url(
                    recommendations,
                    self.config.get("stateless_player_url"),
                )

            cover_candidates = _recommendation_cover_candidates(recommendations)
            card_cover = (
                self.player_server.create_recommendation_cover_url(cover_candidates)
                or (cover_candidates[0] if cover_candidates else _DEFAULT_COVER)
            )
            if recommendations_url and await self._send_recommendations_card(
                event,
                recommendations,
                recommendations_url,
                card_cover,
            ):
                logger.info(
                    "[SHORT_DRAMA] recommendation card sent items=%s",
                    len(recommendations),
                )
                return
            yield event.plain_result(
                "\n".join(result.title for result in recommendations)
            )
            return

        if is_media_help_command(text):
            event.stop_event()
            yield event.plain_result(_media_help_text())
            return

        sports_query = extract_sports_query(text)
        media_request = extract_media_query(text)
        if media_request is None and sports_query is None:
            return

        event.stop_event()
        if sports_query is not None:
            if len(sports_query) > 120:
                yield event.plain_result("赛事名称太长了，精简到 120 个字以内再搜")
                return
            sports_collection = parse_sports_collection_query(sports_query)
            if sports_collection is None:
                result = await self.searcher.search_sports_replay(sports_query)
                if result is None:
                    yield event.plain_result(
                        f"没找到精准赛事《{sports_query}》；"
                        "可发送“体育 足球”“体育 斯诺克”等分类进入回放页"
                    )
                    return
                episode = select_episode(result, None)
                if episode is None:
                    yield event.plain_result("这场赛事当前没有可播放线路")
                    return
                watch_url, player_can_select = self._watch_url(result, episode)
                if await self._send_sports_card(
                    event,
                    result,
                    episode,
                    watch_url,
                    player_can_select,
                ):
                    logger.info(
                        "[SHORT_DRAMA] sports card sent title=%r source=%s",
                        result.title,
                        result.source,
                    )
                    return
                yield event.plain_result(
                    f"《{result.title}》\n"
                    f"{self._result_summary(result, episode)}\n"
                    f"点击观看：{watch_url}"
                )
                return

            first_page = await self.searcher.search_collection_page(
                sports_query,
                1,
            )
            if not first_page.items:
                yield event.plain_result(
                    f"没找到《{sports_query}》的比赛回放，"
                    "可换个体育关键词或加上年份"
                )
                return
            collection_url = self.player_server.create_collection_url(
                sports_query,
                first_page,
            )
            if collection_url and await self._send_collection_card(
                event,
                sports_query,
                first_page,
                collection_url,
            ):
                logger.info(
                    "[SHORT_DRAMA] collection card sent query=%r first_page=%s",
                    sports_query,
                    len(first_page.items),
                )
                return
            lines = [item.title for item in first_page.items[:20]]
            lines.append("仅展示前20场；配置公网播放器后可分页浏览全部回放")
            yield event.plain_result("\n".join(lines))
            return

        assert media_request is not None
        media_type, query = media_request
        search_media_type = None if media_type == "剧" else media_type
        media_label = "全分类资源" if media_type == "剧" else f"{media_type}资源"
        people_media_label = "全分类资源" if media_type == "剧" else media_type

        variant_request = parse_variant_request(query)
        if variant_request is not None:
            title_query, variant_number = variant_request
            if len(title_query) > 60:
                yield event.plain_result("剧名太长了，精简到 60 个字以内再搜")
                return
            variants = await self.searcher.search_variants(
                title_query,
                6,
                media_type=search_media_type,
            )
            if not variants:
                yield event.plain_result(
                    f"没找到《{title_query}》的其他可播放版本"
                )
                return
            if variant_number is None:
                variants_url = self.player_server.create_variants_url(
                    title_query,
                    variants,
                )
                if variants_url and await self._send_variants_card(
                    event,
                    title_query,
                    variants,
                    variants_url,
                ):
                    return
                lines = [f"《{title_query}》可选版本："]
                lines.extend(
                    self._variant_line(index, result)
                    for index, result in enumerate(variants, start=1)
                )
                lines.append(
                    f"发送“{media_type} {title_query} 版本2”播放对应版本"
                )
                yield event.plain_result("\n".join(lines))
                return
            if variant_number > len(variants):
                yield event.plain_result(
                    f"《{title_query}》目前只有 {len(variants)} 个可选版本，"
                    f"请发送“{media_type} {title_query} 版本”重新查看"
                )
                return

            result = variants[variant_number - 1]
            episode = select_episode(result, None)
            if episode is None:
                yield event.plain_result("这个版本当前没有可播放线路")
                return
            watch_url, player_can_select = self._watch_url(result, episode)
            sent = await self._send_card(
                event,
                result,
                episode,
                watch_url,
                player_can_select,
            )
            if sent:
                logger.info(
                    "[SHORT_DRAMA] variant card sent title=%r variant=%s "
                    "source=%s episodes=%s",
                    result.title,
                    variant_number,
                    result.source,
                    len(result.episodes),
                )
                return
            yield event.plain_result(
                f"《{result.title}》\n"
                f"{self._result_summary(result, episode)}\n"
                f"点击观看 {episode.name}：{watch_url}"
            )
            return

        title_query, requested_episode = parse_episode_request(query)
        if len(title_query) > 60:
            yield event.plain_result("剧名太长了，精简到 60 个字以内再搜")
            return

        logger.info(
            "[SHORT_DRAMA] search media=%s query=%r sender=%s group=%s",
            media_type,
            title_query,
            event.get_sender_id(),
            event.get_group_id() or "private",
        )
        result = await self.searcher.search(
            title_query,
            media_type=search_media_type,
            # 普通搜索优先保留资源站的分集线路，避免一律被单线路长视频
            # 抢成“完整版”；需要指定集数时仍由 select_episode 精确选择。
            prefer_full=False,
        )
        if result is None:
            people_results = await self.searcher.search_people(
                title_query,
                10,
                media_type=search_media_type,
            )
            if people_results:
                people_url = self.player_server.create_people_url(
                    title_query,
                    people_media_label,
                    people_results,
                )
                if people_url and await self._send_people_card(
                    event,
                    title_query,
                    people_media_label,
                    people_results,
                    people_url,
                ):
                    logger.info(
                        "[SHORT_DRAMA] people card sent media=%s query=%r results=%s",
                        media_type,
                        title_query,
                        len(people_results),
                    )
                    return
                lines = [f"{title_query}相关{people_media_label}："]
                lines.extend(
                    self._people_line(index, item)
                    for index, item in enumerate(people_results, start=1)
                )
                yield event.plain_result("\n".join(lines))
                return
            hints = known_title_hints(title_query)
            if hints:
                names = "、".join(f"《{name}》" for name in hints)
                yield event.plain_result(
                    f"已按可能剧名 {names} 做了模糊检索，"
                    f"但当前资源站暂无可播放的{media_label}"
                )
            else:
                yield event.plain_result(
                    f"没搜到{media_label}《{title_query}》，换个完整名称再试试"
                )
            return

        if requested_episode is None:
            results = self.searcher.cached_variants(
                title_query,
                8,
                media_type=search_media_type,
            )
            if not results:
                results = (result,)
            elif all(
                not (
                    item.source == result.source
                    and item.title == result.title
                    and item.episodes[:1] == result.episodes[:1]
                )
                for item in results
            ):
                results = (result,) + results
            results = tuple(
                sorted(
                    results,
                    key=lambda item: (
                        item.score,
                        bool(item.year),
                        bool(item.category),
                        bool(item.actor),
                        len(item.episodes),
                    ),
                    reverse=True,
                )
            )[:8]
            search_url = self.player_server.create_search_url(
                title_query,
                media_label,
                results,
            )
            if not search_url:
                search_url = await self._remote_selection_url(
                    results,
                    heading=f"{media_label or '影视资源'}｜搜索“{title_query or '影视'}”",
                    kind="search",
                )
            if not search_url:
                search_url = build_stateless_search_url(
                    results,
                    self.config.get("stateless_player_url"),
                    title_query,
                    media_label,
                )
            if search_url and await self._send_search_card(
                event,
                title_query,
                media_label,
                results,
                search_url,
            ):
                logger.info(
                    "[SHORT_DRAMA] search card sent media=%s title=%r results=%s",
                    media_type,
                    title_query,
                    len(results),
                )
                return
            lines = [f"《{title_query}》搜索结果（按相关性排序）："]
            lines.extend(
                self._variant_line(index, item)
                for index, item in enumerate(results, start=1)
            )
            lines.append("中间播放页暂不可用，请稍后重试")
            yield event.plain_result("\n".join(lines))
            return

        episode = select_episode(result, requested_episode)
        if episode is None:
            yield event.plain_result(
                f"《{result.title}》当前找到 {len(result.episodes)} 个分集线路，"
                f"没有第 {requested_episode} 集"
            )
            return

        watch_url, player_can_select = self._watch_url(
            result,
            episode,
            force_episode=requested_episode is not None,
        )
        sent = await self._send_card(
            event,
            result,
            episode,
            watch_url,
            player_can_select,
        )
        if sent:
            logger.info(
                "[SHORT_DRAMA] card sent title=%r source=%s episodes=%s",
                result.title,
                result.source,
                len(result.episodes),
            )
            return

        # 仅在卡片 API 不可用时降级，正常情况不额外发送重复链接。
        yield event.plain_result(
            f"《{result.title}》\n"
            f"{self._result_summary(result, episode)}\n"
            f"点击观看 {episode.name}：{watch_url}"
        )

    def _watch_url(
        self,
        result: SearchResult,
        episode: Episode,
        *,
        force_episode: bool = False,
    ) -> tuple[str, bool]:
        playlist_url = self.player_server.create_watch_url(
            result,
            episode,
            force_episode=force_episode,
        )
        if playlist_url:
            return playlist_url, True

        stateless_url = build_stateless_playlist_url(
            result,
            self.config.get("stateless_player_url"),
            episode,
            force_episode=force_episode,
        )
        if stateless_url:
            return stateless_url, True

        configured = self.config.get("player_url_template")
        watch_url = build_watch_url(result, configured, episode)
        if configured and str(configured).strip() not in {"direct", "DIRECT"}:
            try:
                str(configured).format(url="", title="", episode="")
            except (KeyError, ValueError):
                logger.warning(
                    "[SHORT_DRAMA] player_url_template 格式无效，使用默认 HLS 播放页"
                )
        return watch_url, False

    @staticmethod
    def _result_summary(result: SearchResult, episode: Episode) -> str:
        metadata = " · ".join(
            value
            for value in (
                result.source,
                f"{result.year}年" if result.year else "",
                result.category,
            )
            if value
        )
        if result.is_full_version:
            minutes = max(1, round(result.duration_seconds / 60))
            return f"{metadata} · 完整版 · 约{minutes}分钟"
        return (
            f"{metadata} · 共{len(result.episodes)}集 · 当前{episode.name}"
        )

    @staticmethod
    def _variant_line(index: int, result: SearchResult) -> str:
        parts = [
            f"《{result.title}》",
            f"{result.year}年" if result.year else "年份未知",
            result.category or "类型未知",
            f"{len(result.episodes)}集",
        ]
        actors = [
            value.strip()
            for value in re.split(r"[,，、/]+", result.actor)
            if value.strip()
        ][:2]
        if actors:
            parts.append("、".join(actors))
        parts.append(result.source)
        return f"{index}. " + "｜".join(parts)

    @staticmethod
    def _people_line(index: int, result: SearchResult) -> str:
        parts = [
            f"《{result.title}》",
            f"{result.year}年" if result.year else "年份未知",
            result.category or "类型未知",
            f"{len(result.episodes)}集",
        ]
        role = "导演" if result.match_role == "director" else "演员"
        names = result.director if result.match_role == "director" else result.actor
        if names:
            parts.append(f"{role}：{names}")
        parts.append(result.source)
        return f"{index}. " + "｜".join(parts)

    @staticmethod
    async def _send_collection_card(
        event: AstrMessageEvent,
        query_title: str,
        first_page: CollectionPage,
        collection_url: str,
    ) -> bool:
        cover = next(
            (item.cover_url for item in first_page.items if item.cover_url),
            _DEFAULT_COVER,
        )
        description = (
            f"已找到首批 {len(first_page.items)} 场回放"
            " · 点击按页浏览，向下滚动继续加载"
        )
        message = [
            {
                "type": "wechat_link_card",
                "data": {
                    "title": f"赛事回放｜{query_title}"[:48],
                    "desc": description[:80],
                    "url": collection_url,
                    "image_url": cover,
                },
            }
        ]
        payload = {"message": message}
        try:
            if event.get_group_id():
                payload["group_id"] = event.get_group_id()
                response = await event.bot.call_action("send_group_msg", **payload)
            else:
                payload["user_id"] = event.get_sender_id()
                response = await event.bot.call_action("send_private_msg", **payload)
            if isinstance(response, dict):
                return response.get("retcode", 0) == 0 and response.get(
                    "status", "ok"
                ) in {"ok", "success"}
            return True
        except Exception as exc:
            logger.warning(
                "[SHORT_DRAMA] collection card send failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False

    @staticmethod
    async def _send_recommendations_card(
        event: AstrMessageEvent,
        recommendations: tuple[SearchResult, ...],
        recommendations_url: str,
        cover_url: str,
    ) -> bool:
        message = [
            {
                "type": "wechat_link_card",
                "data": {
                    "title": f"最新短剧推荐｜{len(recommendations)}部"[:48],
                    "desc": "点击查看封面并选择播放",
                    "url": recommendations_url,
                    "image_url": cover_url,
                },
            }
        ]
        payload = {"message": message}
        try:
            if event.get_group_id():
                payload["group_id"] = event.get_group_id()
                response = await event.bot.call_action("send_group_msg", **payload)
            else:
                payload["user_id"] = event.get_sender_id()
                response = await event.bot.call_action("send_private_msg", **payload)
            if isinstance(response, dict):
                return response.get("retcode", 0) == 0 and response.get(
                    "status", "ok"
                ) in {"ok", "success"}
            return True
        except Exception as exc:
            logger.warning(
                "[SHORT_DRAMA] recommendation card send failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False

    @staticmethod
    async def _send_sports_card(
        event: AstrMessageEvent,
        result: SearchResult,
        episode: Episode,
        watch_url: str,
        player_can_select: bool = False,
    ) -> bool:
        summary = Main._result_summary(result, episode)
        description = f"{summary} · 点击播放"
        if player_can_select and len(result.episodes) > 1:
            description += " · 播放器内可选线路"
        message = [
            {
                "type": "wechat_link_card",
                "data": {
                    "title": f"赛事回放｜{result.title}"[:48],
                    "desc": description[:80],
                    "url": watch_url,
                    "image_url": result.cover_url or _DEFAULT_COVER,
                },
            }
        ]
        payload = {"message": message}
        try:
            if event.get_group_id():
                payload["group_id"] = event.get_group_id()
                response = await event.bot.call_action("send_group_msg", **payload)
            else:
                payload["user_id"] = event.get_sender_id()
                response = await event.bot.call_action("send_private_msg", **payload)
            if isinstance(response, dict):
                return response.get("retcode", 0) == 0 and response.get(
                    "status", "ok"
                ) in {"ok", "success"}
            return True
        except Exception as exc:
            logger.warning(
                "[SHORT_DRAMA] sports card send failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False

    @staticmethod
    async def _send_people_card(
        event: AstrMessageEvent,
        query_title: str,
        media_label: str,
        results: tuple[SearchResult, ...],
        people_url: str,
    ) -> bool:
        title, description = _compact_people_card_text(
            query_title,
            media_label,
            results,
        )
        cover = next(
            (result.cover_url for result in results if result.cover_url),
            _DEFAULT_COVER,
        )
        message = [
            {
                "type": "wechat_link_card",
                "data": {
                    "title": title,
                    "desc": description[:80],
                    "url": people_url,
                    "image_url": cover,
                },
            }
        ]
        payload = {"message": message}
        try:
            if event.get_group_id():
                payload["group_id"] = event.get_group_id()
                response = await event.bot.call_action("send_group_msg", **payload)
            else:
                payload["user_id"] = event.get_sender_id()
                response = await event.bot.call_action("send_private_msg", **payload)
            if isinstance(response, dict):
                return response.get("retcode", 0) == 0 and response.get(
                    "status", "ok"
                ) in {"ok", "success"}
            return True
        except Exception as exc:
            logger.warning(
                "[SHORT_DRAMA] people card send failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False

    async def _send_search_card(
        self,
        event: AstrMessageEvent,
        query_title: str,
        media_label: str,
        results: tuple[SearchResult, ...],
        search_url: str,
    ) -> bool:
        title, description = _compact_search_card_text(
            query_title,
            media_label,
            results,
        )
        cover = next(
            (result.cover_url for result in results if result.cover_url),
            _DEFAULT_COVER,
        )
        cover_proxy_factory = getattr(
            self.player_server,
            "create_card_cover_url",
            None,
        )
        card_cover = (
            cover_proxy_factory(cover)
            if callable(cover_proxy_factory) and cover != _DEFAULT_COVER
            else None
        ) or cover
        message = [
            {
                "type": "wechat_link_card",
                "data": {
                    "title": title,
                    "desc": description[:80],
                    "url": search_url,
                    "image_url": card_cover,
                },
            }
        ]
        payload = {"message": message}
        try:
            if event.get_group_id():
                payload["group_id"] = event.get_group_id()
                response = await event.bot.call_action("send_group_msg", **payload)
            else:
                payload["user_id"] = event.get_sender_id()
                response = await event.bot.call_action("send_private_msg", **payload)
            if isinstance(response, dict):
                return response.get("retcode", 0) == 0 and response.get(
                    "status", "ok"
                ) in {"ok", "success"}
            return True
        except Exception as exc:
            logger.warning(
                "[SHORT_DRAMA] search card send failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False

    @staticmethod
    async def _send_variants_card(
        event: AstrMessageEvent,
        query_title: str,
        variants: tuple[SearchResult, ...],
        variants_url: str,
    ) -> bool:
        title, description = _compact_variants_card_text(
            query_title,
            variants,
        )
        cover = next(
            (result.cover_url for result in variants if result.cover_url),
            _DEFAULT_COVER,
        )
        message = [
            {
                "type": "wechat_link_card",
                "data": {
                    "title": title,
                    "desc": description[:80],
                    "url": variants_url,
                    "image_url": cover,
                },
            }
        ]
        payload = {"message": message}
        try:
            if event.get_group_id():
                payload["group_id"] = event.get_group_id()
                response = await event.bot.call_action("send_group_msg", **payload)
            else:
                payload["user_id"] = event.get_sender_id()
                response = await event.bot.call_action("send_private_msg", **payload)
            if isinstance(response, dict):
                return response.get("retcode", 0) == 0 and response.get(
                    "status", "ok"
                ) in {"ok", "success"}
            return True
        except Exception as exc:
            logger.warning(
                "[SHORT_DRAMA] variants card send failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False

    @staticmethod
    async def _send_card(
        event: AstrMessageEvent,
        result: SearchResult,
        episode: Episode,
        watch_url: str,
        player_can_select: bool = False,
    ) -> bool:
        title, description = _compact_play_card_text(result, episode)
        message = [
            {
                "type": "wechat_link_card",
                "data": {
                    "title": title,
                    "desc": description[:80],
                    "url": watch_url,
                    "image_url": result.cover_url or _DEFAULT_COVER,
                },
            }
        ]
        payload = {"message": message}
        try:
            if event.get_group_id():
                payload["group_id"] = event.get_group_id()
                response = await event.bot.call_action("send_group_msg", **payload)
            else:
                payload["user_id"] = event.get_sender_id()
                response = await event.bot.call_action("send_private_msg", **payload)
            if isinstance(response, dict):
                return response.get("retcode", 0) == 0 and response.get(
                    "status", "ok"
                ) in {"ok", "success"}
            return True
        except Exception as exc:
            logger.warning(
                "[SHORT_DRAMA] link card send failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False
