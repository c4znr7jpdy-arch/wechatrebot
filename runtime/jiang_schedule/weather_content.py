"""Shared weather-card pipeline for manual commands and scheduled deliveries.

Installed as ai_plugin.scheduled_weather; imports are delayed so registry checks
and text-only schedules never start a browser or fetch public content.
"""
from __future__ import annotations

import asyncio


async def generate_weather(place: str, target_offset: int | None = None) -> str:
    from . import weather as service
    from . import weather_image as renderer

    if target_offset is not None and not 0 <= target_offset <= 45:
        raise ValueError("天气日期超出可查询范围")
    if target_offset is not None and target_offset > 6:
        daily = await service.fetch_weather(place)
        target = await service.fetch_weather_target_date(daily, target_offset)
        if not target:
            raise ValueError("指定日期天气数据暂不可用")
        target["city"] = daily.get("shi") or daily.get("name") or place
        return await renderer.render_weather_target_image(target, place)

    supplemental = asyncio.create_task(service.fetch_weather_alapi(place))
    try:
        daily = await service.fetch_weather(place)
        open_meteo, alapi = await asyncio.gather(
            service.fetch_weather_open_meteo(daily), supplemental,
            return_exceptions=True,
        )
    finally:
        if not supplemental.done():
            supplemental.cancel()
            await asyncio.gather(supplemental, return_exceptions=True)
    merged = service.merge_weather_supplements(
        open_meteo if isinstance(open_meteo, dict) else None,
        alapi if isinstance(alapi, dict) else None,
    )
    card = renderer.build_weather_card_data(daily, place, merged)
    card["selected_offset"] = target_offset
    return await renderer.render_weather_image(card, daily)
