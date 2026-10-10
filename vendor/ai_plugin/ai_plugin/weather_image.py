"""天气图片卡片渲染模块——竖版实时天气、灾害预警和七日预报。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html as html_lib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta

try:
    from .weather_svg import get_svg_icon
except ImportError:
    from weather_svg import get_svg_icon

_HTML_PATH = os.path.join(os.path.dirname(__file__), "weather_card.html")
_TARGET_HTML_PATH = os.path.join(os.path.dirname(__file__), "weather_target_card.html")
_CARD_VERSION = "mobile-weather-v8-available-observations"
_TARGET_CARD_VERSION = "target-weather-v2-cropped"
_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _load_html_template() -> str:
    with open(_HTML_PATH, "r", encoding="utf-8") as file:
        return file.read()


def _load_target_html_template() -> str:
    with open(_TARGET_HTML_PATH, "r", encoding="utf-8") as file:
        return file.read()


def _icon_img_tag(weather_code: str, size: int = 64) -> str:
    """生成 SVG 天气图标并以内联 data URI 嵌入卡片。"""
    svg = get_svg_icon(weather_code, size)
    if not svg:
        return ""
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return (
        f'<img src="data:image/svg+xml;base64,{encoded}" '
        f'width="{size}" height="{size}" alt="" />'
    )


def _clean_temp(value: object, fallback: str = "--") -> str:
    number = _number(value)
    return str(int(round(number))) if number is not None else fallback


def _missing_value(value: object) -> bool:
    return value in (None, "", [], {}) or str(value).strip().lower() in {
        "", "--", "null", "none", "nan", "9999", "9999.0",
    }


def _number(value: object) -> float | None:
    text = str(value if value is not None else "").replace("℃", "").replace("°", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    number = float(match.group(0)) if match else None
    return None if _missing_value(number) else number


def _first_value(data: dict | None, *keys: str, default: object = "") -> object:
    if not isinstance(data, dict):
        return default
    for key in keys:
        value = data.get(key)
        if not _missing_value(value):
            return value
    return default


def _as_text(value: object) -> str:
    if _missing_value(value):
        return ""
    if isinstance(value, (list, tuple)):
        return "转".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _city_label(value: object) -> str:
    city = re.sub(r"\s+", "", _as_text(value)) or "当前位置"
    if city.endswith(("市", "县", "区", "旗", "州", "盟", "地区")):
        return city
    return f"{city}市"


def _format_updated(value: object) -> str:
    text = _as_text(value)
    if not text:
        return "刚刚更新"

    normalized = text.replace("T", " ").replace("Z", "")
    candidates = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    )
    for pattern in candidates:
        try:
            update_time = datetime.strptime(normalized[:19], pattern)
            if abs((datetime.now() - update_time).total_seconds()) <= 15 * 60:
                return "刚刚更新"
            return f"{update_time:%H:%M} 更新"
        except ValueError:
            continue

    match = re.search(r"(\d{1,2}:\d{2})", text)
    return f"{match.group(1)} 更新" if match else "刚刚更新"


def _normalize_percent(value: object) -> str:
    text = _as_text(value)
    if not text:
        return "--"
    return text if "%" in text else f"{_clean_temp(text)}%"


def _normalize_visibility(value: object) -> str:
    text = _as_text(value)
    if not text:
        return "暂缺"
    lower = text.lower().replace(" ", "")
    if "km" in lower or "千米" in text or "公里" in text:
        return text.replace("千米", "km").replace("公里", "km").replace(" ", "")
    number = _number(text)
    if number is None:
        return text
    if number > 100:
        number /= 1000
    return f"{number:g}km"


def _air_level(value: object, supplied_level: object = "") -> str:
    level = _as_text(supplied_level)
    if level:
        return level
    number = _number(value)
    if number is None:
        return "--"
    if number <= 50:
        return "优"
    if number <= 100:
        return "良"
    if number <= 150:
        return "轻度"
    if number <= 200:
        return "中度"
    return "重度"


def _weather_theme(weather_code: str, weather_text: str) -> str:
    code = (weather_code or "").lower()
    text = weather_text or ""
    if any(word in code for word in ("lei", "thunder")) or "雷" in text:
        return "thunder"
    if any(word in code for word in ("xue", "snow")) or "雪" in text:
        return "snow"
    if (
        ("yu" in code and "duoyun" not in code)
        or any(word in code for word in ("rain", "shower"))
        or "雨" in text
    ):
        return "rain"
    if any(word in code for word in ("wu", "mai", "fog", "haze")) or any(
        word in text for word in ("雾", "霾", "沙尘")
    ):
        return "fog"
    if any(word in code for word in ("yin", "overcast")) or "阴" in text:
        return "overcast"
    if any(word in code for word in ("qing", "sun", "clear")) or "晴" in text:
        return "sunny"
    return "partly"


def _extract_alarm_brief(title: str) -> str:
    match = re.search(r"发布(.+)$", title or "")
    brief = match.group(1) if match else (title or "")
    brief = re.sub(r"\s+", " ", brief).strip()
    warning_match = re.search(r"(.+?预警(?:信号)?)", brief)
    if warning_match:
        return warning_match.group(1).replace("信号", "")
    return brief[:18] if brief else ""


def _alert_payload(apihz_data: dict | None) -> tuple[str, str, str]:
    alarms = []
    if isinstance(apihz_data, dict) and apihz_data.get("code") == 200:
        alarms = apihz_data.get("alarm") or []
    if not alarms:
        return "none", "当前无灾害预警", "气象风险平稳，出行前仍请关注天气变化"

    alarm = alarms[0] if isinstance(alarms[0], dict) else {"title": str(alarms[0])}
    title = _as_text(_first_value(alarm, "title", "headline", "signaltype"))
    brief = _extract_alarm_brief(title) or "气象灾害预警"
    detail = _as_text(
        _first_value(alarm, "description", "content", "detail", "text", "issueContent")
    )
    detail = re.sub(r"\s+", " ", detail).strip()
    if not detail:
        detail = "预警已生效，请关注最新气象信息并做好防范"
    if len(detail) > 44:
        detail = f"{detail[:43]}…"

    severity = "active"
    if "红色" in title:
        severity = "red"
    elif "橙色" in title:
        severity = "orange"
    elif "黄色" in title:
        severity = "yellow"
    elif "蓝色" in title:
        severity = "blue"
    return severity, brief, detail


def _build_daily_days(apihz_data: dict | None, card_data: dict) -> list[dict]:
    days: list[dict] = []
    trend_by_date = {
        _as_text(item.get("date")): item
        for item in (card_data.get("daily_trends") or [])
        if isinstance(item, dict) and item.get("date")
    }
    if isinstance(apihz_data, dict) and apihz_data.get("code") == 200:
        for index in range(1, 8):
            source = apihz_data if index == 1 else apihz_data.get(f"weatherday{index}") or {}
            if not source:
                continue
            weather = _as_text(_first_value(source, "weather1", "dayText"))
            image = _as_text(_first_value(source, "weather1img", "dayCode"))
            date_key = _as_text(source.get("date")).replace("/", "-")
            if not date_key:
                date_key = (datetime.now() + timedelta(days=index - 1)).strftime("%Y-%m-%d")
            trend = trend_by_date.get(date_key) or {}
            days.append(
                {
                    "weather": weather or card_data.get("weather", ""),
                    "weather_code": _map_apihz_code(image, weather),
                    "high": _first_value(
                        trend,
                        "high",
                        default=_first_value(source, "wd1", "high", default="--"),
                    ),
                    "low": _first_value(
                        trend,
                        "low",
                        default=_first_value(source, "wd2", "low", default="--"),
                    ),
                    "hourly_temps": trend.get("hourly_temps") or [],
                }
            )

    while len(days) < 7:
        days.append(
            {
                "weather": card_data.get("weather", "实时天气"),
                "weather_code": card_data.get("weather_code", "duoyun"),
                "high": card_data.get("temp", "--"),
                "low": card_data.get("temp", "--"),
                "hourly_temps": [],
            }
        )
    return days[:7]


def _hour_label(value: object, fallback_hour: int) -> str:
    text = _as_text(value)
    match = re.search(r"(?:\s|T|^)(\d{1,2})(?::\d{2})", text)
    if match:
        return f"{int(match.group(1)):02d}时"
    return f"{fallback_hour % 24:02d}时"


def _build_hourly_items(card_data: dict) -> str:
    raw_hours = card_data.get("hour") or card_data.get("hours") or []
    hours = [item for item in raw_hours if isinstance(item, dict)][:5]
    current_hour = datetime.now().hour
    items: list[dict] = []
    for index, item in enumerate(hours):
        temperature = _clean_temp(_first_value(item, "temp", "tem", "temperature"))
        if temperature == "--":
            continue
        weather_text = _as_text(_first_value(item, "wea", "weather", "text"))
        raw_code = _as_text(_first_value(item, "wea_code", "weather_code", "code"))
        items.append(
            {
                "time": "现在" if index == 0 else _hour_label(
                    _first_value(item, "time", "datetime", "date"),
                    current_hour + index * 2,
                ),
                "temp": temperature,
                "code": _map_apihz_code(raw_code, weather_text) if raw_code or weather_text else "",
            }
        )

    if len(items) < 2:
        return '<div class="hourly-unavailable">逐小时天气暂不可用</div>'

    html_items = []
    for item in items[:5]:
        html_items.append(
            '<div class="hour-item">'
            f'<div class="hour-time">{html_lib.escape(item["time"])}</div>'
            f'<div class="hour-icon">{_icon_img_tag(item["code"], 76) if item["code"] else ""}</div>'
            f'<div class="hour-temp">{html_lib.escape(item["temp"])}°</div>'
            "</div>"
        )
    return "".join(html_items)


def _trend_svg(
    hourly_temperatures: object,
    minimum: float,
    temperature_range: float,
) -> str:
    """按真实逐小时温度绘制一天的平滑折线。"""
    if not isinstance(hourly_temperatures, (list, tuple)):
        return ""
    values = [
        value
        for item in hourly_temperatures
        if (value := _number(item)) is not None
    ]
    if len(values) < 2:
        return ""

    def y(value: float) -> float:
        normalized = (value - minimum) / temperature_range if temperature_range else 0.5
        normalized = max(0.0, min(1.0, normalized))
        return 34 - normalized * 25

    points = [
        (8 + index * 204 / (len(values) - 1), y(value))
        for index, value in enumerate(values)
    ]
    path_parts = [f"M{points[0][0]:.1f} {points[0][1]:.1f}"]
    for previous, current in zip(points, points[1:]):
        middle_x = (previous[0] + current[0]) / 2
        path_parts.append(
            f"C{middle_x:.1f} {previous[1]:.1f},"
            f"{middle_x:.1f} {current[1]:.1f},"
            f"{current[0]:.1f} {current[1]:.1f}"
        )
    path = " ".join(path_parts)
    start_x, start_y = points[0]
    end_x, end_y = points[-1]
    return (
        '<svg viewBox="0 0 220 42" preserveAspectRatio="none" aria-hidden="true">'
        f'<path d="{path}" '
        'fill="none" stroke="#7ad8ff" stroke-width="3" stroke-linecap="round"/>'
        f'<circle cx="{start_x:.1f}" cy="{start_y:.1f}" r="4" fill="#eaf9ff" stroke="#38bdf8" stroke-width="3"/>'
        f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="4" fill="#eaf9ff" stroke="#38bdf8" stroke-width="3"/>'
        "</svg>"
    )


def _build_daily_rows(days: list[dict], selected_offset: int | None = None) -> str:
    values = [
        value
        for day in days
        for value in (_number(day.get("low")), _number(day.get("high")))
        if value is not None
    ]
    minimum = min(values) if values else 0
    maximum = max(values) if values else 1
    temperature_range = max(1, maximum - minimum)
    today = datetime.now()
    rows = []
    for index, day in enumerate(days[:7]):
        date = today + timedelta(days=index)
        is_selected = index == selected_offset
        day_name = (
            f"{date.month}/{date.day}"
            if is_selected
            else ("今天" if index == 0 else _WEEKDAYS[date.weekday()])
        )
        weather = _as_text(day.get("weather")) or "--"
        code = _as_text(day.get("weather_code")) or "duoyun"
        low = _clean_temp(day.get("low"))
        high = _clean_temp(day.get("high"))
        rows.append(
            f'<div class="day-row{" selected" if is_selected else ""}">'
            f'<div class="day-name">{html_lib.escape(day_name)}</div>'
            f'<div class="day-icon">{_icon_img_tag(code, 57)}</div>'
            f'<div class="day-weather">{html_lib.escape(weather)}</div>'
            f'<div class="day-low">{html_lib.escape(low)}°</div>'
            f'<div class="trend">{_trend_svg(day.get("hourly_temps"), minimum, temperature_range)}</div>'
            f'<div class="day-high">{html_lib.escape(high)}°</div>'
            "</div>"
        )
    return "".join(rows)


def _build_html(card_data: dict, apihz_data: dict | None) -> str:
    html = _load_html_template()
    daily_days = _build_daily_days(apihz_data, card_data)
    today = daily_days[0]

    city = _city_label(card_data.get("city"))
    weather = _as_text(card_data.get("weather")) or _as_text(today.get("weather")) or "实时天气"
    weather_code = _as_text(card_data.get("weather_code")) or _as_text(today.get("weather_code")) or "duoyun"
    live_temp = _clean_temp(card_data.get("temp"))
    feels = _clean_temp(card_data.get("feelst") or card_data.get("temp"))
    high = _clean_temp(today.get("high"))
    low = _clean_temp(today.get("low"))
    air_value = _as_text(card_data.get("air"))
    air_level = _air_level(air_value, card_data.get("air_level")) if air_value else ""
    air_value = air_value or "暂缺"
    humidity = _normalize_percent(card_data.get("humidity"))
    wind_direction = _as_text(card_data.get("wind_direction"))
    wind_scale = _as_text(card_data.get("wind_scale"))
    wind = " ".join(part for part in (wind_direction, wind_scale) if part) or "风力 --"
    visibility = _normalize_visibility(card_data.get("visibility"))
    alert_severity, alert_title, alert_detail = _alert_payload(apihz_data)
    selected_offset = card_data.get("selected_offset")
    if not isinstance(selected_offset, int) or not 0 <= selected_offset < 7:
        selected_offset = None
    selected_date = datetime.now() + timedelta(days=selected_offset or 0)
    daily_title = (
        f"指定日期 · {selected_date.month}月{selected_date.day}日"
        if selected_offset is not None
        else "未来 7 天"
    )

    replacements = {
        "WEATHER_THEME": _weather_theme(weather_code, weather),
        "CITY": city,
        "UPDATED": _format_updated(card_data.get("update_time")),
        "LIVE_TEMP": live_temp,
        "LIVE_WEATHER": weather,
        "FEELS": feels,
        "HIGH": high,
        "LOW": low,
        "HERO_ICON": _icon_img_tag(weather_code, 286),
        "AIR_LEVEL": air_level,
        "AIR_VALUE": air_value,
        "HUMIDITY": humidity,
        "WIND": wind,
        "VISIBILITY": visibility,
        "ALERT_SEVERITY": alert_severity,
        "ALERT_TITLE": alert_title,
        "ALERT_DETAIL": alert_detail,
        "HOURLY_ITEMS": _build_hourly_items(card_data),
        "DAILY_TITLE": daily_title,
        "DAILY_ROWS": _build_daily_rows(daily_days, selected_offset),
    }

    raw_html_keys = {"HERO_ICON", "HOURLY_ITEMS", "DAILY_ROWS"}
    for key, value in replacements.items():
        rendered = value if key in raw_html_keys else html_lib.escape(str(value))
        html = html.replace(f"{{{{{key}}}}}", rendered)
    return html


if "_playwright" not in globals():
    _playwright = None
if "_browser" not in globals():
    _browser = None
if "_browser_context" not in globals():
    _browser_context = None
if "_renderer_start_lock" not in globals():
    _renderer_start_lock: asyncio.Lock | None = None
if "_render_lock" not in globals():
    _render_lock: asyncio.Lock | None = None


async def _get_browser_context():
    """惰性启动并复用 Chromium，避免每次天气命令重复冷启动。"""
    global _playwright, _browser, _browser_context, _renderer_start_lock

    if _browser is not None and _browser.is_connected() and _browser_context is not None:
        return _browser_context

    if _renderer_start_lock is None:
        _renderer_start_lock = asyncio.Lock()
    async with _renderer_start_lock:
        if _browser is not None and _browser.is_connected() and _browser_context is not None:
            return _browser_context
        if _playwright is not None or _browser is not None or _browser_context is not None:
            await _reset_browser()

        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 864, "height": 1705},
                device_scale_factor=1,
            )
        except Exception:
            await playwright.stop()
            raise

        _playwright = playwright
        _browser = browser
        _browser_context = context
        return context


async def _reset_browser() -> None:
    global _playwright, _browser, _browser_context

    context, browser, playwright = _browser_context, _browser, _playwright
    _browser_context = None
    _browser = None
    _playwright = None
    if context is not None:
        try:
            await context.close()
        except Exception:
            pass
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            pass
    if playwright is not None:
        try:
            await playwright.stop()
        except Exception:
            pass


async def warm_weather_renderer() -> None:
    """在插件加载后预热浏览器。"""
    await _get_browser_context()


async def _render_to_png(
    html: str,
    output_path: str,
    viewport_height: int = 1705,
) -> None:
    global _render_lock

    if _render_lock is None:
        _render_lock = asyncio.Lock()
    async with _render_lock:
        for attempt in range(2):
            page = None
            try:
                context = await _get_browser_context()
                page = await context.new_page()
                await page.set_viewport_size({"width": 864, "height": viewport_height})
                await page.set_content(html, wait_until="load")
                await page.evaluate("document.fonts && document.fonts.ready")
                await page.screenshot(path=output_path, full_page=True)
                return
            except Exception:
                if attempt:
                    raise
                await _reset_browser()
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass


_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 600


def _get_cache(cache_key: str) -> str | None:
    import time

    if cache_key in _cache:
        timestamp, path = _cache[cache_key]
        if time.time() - timestamp < _CACHE_TTL and os.path.exists(path):
            return path
    path = os.path.join(
        tempfile.gettempdir(),
        f"weather_{hashlib.md5(cache_key.encode('utf-8')).hexdigest()[:12]}.png",
    )
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < _CACHE_TTL:
        _cache[cache_key] = (time.time(), path)
        return path
    return None


def _set_cache(cache_key: str, path: str) -> None:
    import time

    _cache[cache_key] = (time.time(), path)


def _cache_payload(card_data: dict, apihz_data: dict | None) -> dict:
    daily = []
    if isinstance(apihz_data, dict):
        for index in range(1, 8):
            source = apihz_data if index == 1 else apihz_data.get(f"weatherday{index}") or {}
            daily.append(
                {
                    key: source.get(key, "")
                    for key in ("weather1", "weather1img", "wd1", "wd2")
                }
            )
    alarms = (apihz_data or {}).get("alarm") or []
    return {
        "version": _CARD_VERSION,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "card": {
            key: card_data.get(key, "")
            for key in (
                "city",
                "weather",
                "weather_code",
                "temp",
                "feelst",
                "humidity",
                "wind_direction",
                "wind_scale",
                "visibility",
                "air",
                "air_level",
                "update_time",
                "hour",
                "daily_trends",
                "selected_offset",
            )
        },
        "alarms": alarms[:2],
        "daily": daily,
    }


async def render_weather_image(card_data: dict, apihz_data: dict | None = None) -> str:
    cache_json = json.dumps(
        _cache_payload(card_data, apihz_data),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    cached = _get_cache(cache_json)
    if cached:
        return cached

    html = _build_html(card_data, apihz_data)
    file_key = hashlib.md5(cache_json.encode("utf-8")).hexdigest()[:12]
    path = os.path.join(tempfile.gettempdir(), f"weather_{file_key}.png")
    await _render_to_png(html, path)
    _set_cache(cache_json, path)
    return path


def _optional_degree(value: object) -> str:
    cleaned = _clean_temp(value, fallback="")
    return f"{cleaned}°" if cleaned else ""


def _build_target_html(target_data: dict, fallback_city: str = "") -> str:
    """构造第 8—31 天的独立指定日期卡片，缺失字段保持空白。"""
    html = _load_target_html_template()
    raw_date = _as_text(target_data.get("date"))
    try:
        parsed_date = datetime.strptime(raw_date, "%Y-%m-%d")
        date_label = f"{parsed_date.year}年{parsed_date.month}月{parsed_date.day}日"
    except ValueError:
        date_label = raw_date

    humidity = _as_text(target_data.get("humidity"))
    if humidity and "%" not in humidity:
        humidity = f"{_clean_temp(humidity, fallback='')}%"
    wind = " ".join(
        value
        for value in (
            _as_text(target_data.get("wind_direction")),
            _as_text(target_data.get("wind_scale")),
        )
        if value
    )
    weather_text = _as_text(target_data.get("weather"))
    weather_code = _map_apihz_code(
        _as_text(target_data.get("weather_code")),
        weather_text,
    )
    replacements = {
        "CITY": _city_label(target_data.get("city") or fallback_city),
        "DATE": date_label,
        "KIND": _as_text(target_data.get("forecast_kind")),
        "WEATHER": weather_text,
        "HIGH": _optional_degree(target_data.get("high")),
        "LOW": _optional_degree(target_data.get("low")),
        "ICON": _icon_img_tag(weather_code, 300),
        "HUMIDITY": humidity,
        "WIND": wind,
        "PRECIPITATION_LABEL": _as_text(target_data.get("precipitation_label")),
        "PRECIPITATION": _as_text(target_data.get("precipitation")),
        "VISIBILITY": _as_text(target_data.get("visibility")),
        "NOTE": _as_text(target_data.get("forecast_note")),
    }
    for key, value in replacements.items():
        rendered = value if key == "ICON" else html_lib.escape(str(value))
        html = html.replace(f"{{{{{key}}}}}", rendered)
    return html


async def render_weather_target_image(
    target_data: dict,
    fallback_city: str = "",
) -> str:
    """渲染一个月内、七日预报范围之外的指定日期卡片。"""
    payload = {
        "version": _TARGET_CARD_VERSION,
        "city": fallback_city,
        "target": target_data,
    }
    cache_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    cached = _get_cache(cache_json)
    if cached:
        return cached
    html = _build_target_html(target_data, fallback_city)
    file_key = hashlib.md5(cache_json.encode("utf-8")).hexdigest()[:12]
    path = os.path.join(tempfile.gettempdir(), f"weather_target_{file_key}.png")
    await _render_to_png(html, path, viewport_height=1080)
    _set_cache(cache_json, path)
    return path


def build_weather_card_data(
    apihz_data: dict,
    fallback_city: str = "",
    supplemental_data: dict | None = None,
) -> dict:
    """合并七日天气与可选实况数据，构造竖版卡片所需字段。"""
    supplemental = supplemental_data if isinstance(supplemental_data, dict) else {}
    nowinfo = apihz_data.get("nowinfo") or {}
    aqi = supplemental.get("aqi")
    air_data = aqi if isinstance(aqi, dict) and not _missing_value(aqi.get("air")) else supplemental
    weather_text = _as_text(
        _first_value(supplemental, "weather", "wea", default=apihz_data.get("weather1", ""))
    )
    image = _as_text(
        _first_value(supplemental, "weather_code", "wea_code", default=apihz_data.get("weather1img", ""))
    )
    city = fallback_city or _as_text(
        _first_value(supplemental, "city", default=apihz_data.get("shi") or apihz_data.get("name"))
    )

    return {
        "city": city,
        "weather": weather_text,
        "weather_code": _map_apihz_code(image, weather_text),
        "temp": _first_value(
            supplemental,
            "temp",
            "tem",
            default=_first_value(nowinfo, "temperature", default=apihz_data.get("wd1", "")),
        ),
        "feelst": _first_value(
            supplemental,
            "feelst",
            "feels_like",
            default=_first_value(nowinfo, "feelst", "feelsLike", default=""),
        ),
        "humidity": _first_value(
            supplemental,
            "humidity",
            default=_first_value(nowinfo, "humidity", default=""),
        ),
        "wind_direction": _as_text(
            _first_value(supplemental, "wind", "wind_direction", default=_first_value(nowinfo, "windDirection"))
        ),
        "wind_scale": _as_text(
            _first_value(supplemental, "wind_power", "wind_scale", default=_first_value(nowinfo, "windScale"))
        ),
        "visibility": _first_value(
            supplemental,
            "visibility",
            default=_first_value(nowinfo, "visibility", default=""),
        ),
        "air": _first_value(air_data, "air") if isinstance(aqi, dict) else _first_value(air_data, "air", "aqi"),
        "air_level": _first_value(air_data, "air_level"),
        "update_time": _first_value(
            supplemental,
            "update_time",
            default=apihz_data.get("uptime", ""),
        ),
        "hour": _first_value(supplemental, "hour", "hours", default=[]),
        "daily_trends": _first_value(supplemental, "daily_trends", default=[]),
    }


def _map_apihz_code(img_url: str, weather_text: str = "") -> str:
    """将接口图标文件名或天气文字归一化为内置 SVG 图标代码。"""
    clean_url = re.split(r"[?#]", str(img_url or ""), maxsplit=1)[0]
    name = clean_url.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    named_mapping = {
        "qing": "qing",
        "duoyun": "duoyun",
        "shaoyun": "duoyun",
        "yin": "yin",
        "yu": "yu",
        "xiaoyu": "xiaoyu",
        "zhongyu": "yu",
        "dayu": "yu",
        "baoyu": "yu",
        "zhenyu": "zhenyu",
        "leizhenyu": "lei",
        "lei_zhengyu": "lei",
        "lei": "lei",
        "xue": "xue",
        "xiaoxue": "xue",
        "zhongxue": "xue",
        "daxue": "xue",
        "baoxue": "xue",
        "zhenxue": "xue",
        "yujiaxue": "xue",
        "wu": "wu",
        "mai": "mai",
        "fuchen": "wu",
        "yangsha": "wu",
        "shachenbao": "wu",
        "sun": "qing",
        "clear": "qing",
        "cloud": "duoyun",
        "cloudy": "duoyun",
        "overcast": "yin",
        "rain": "yu",
        "snow": "xue",
        "fog": "wu",
        "haze": "mai",
    }
    if name in named_mapping:
        return named_mapping[name]

    # 只有 wN 文件名属于中国天气现象编码。裸数字可能是 WMO 编码，
    # 两套编码含义不同，数据源必须先把 WMO 码归一化为上面的名称。
    numeric_match = re.fullmatch(r"w(\d+)", name)
    if numeric_match:
        code = int(numeric_match.group(1))
        if code == 0:
            return "qing"
        if code == 1:
            return "duoyun"
        if code == 2:
            return "yin"
        if code == 3:
            return "zhenyu"
        if code in (4, 5):
            return "lei"
        if code in (6, 13, 14, 15, 16, 17, 26, 27, 28):
            return "xue"
        if code == 7:
            return "xiaoyu"
        if code in (8, 9, 10, 11, 12, 19, 21, 22, 23, 24, 25):
            return "yu"
        if code in (18, 20, 29, 30, 31, 49, 53, 54, 55, 56, 57, 58):
            return "wu"

    text = str(weather_text or "")
    if "雷" in text:
        return "lei"
    if "雨夹雪" in text or "雪" in text:
        return "xue"
    if "阵雨" in text:
        return "zhenyu"
    if "小雨" in text:
        return "xiaoyu"
    if "雨" in text:
        return "yu"
    if any(word in text for word in ("雾", "霾", "浮尘", "扬沙", "沙尘")):
        return "wu"
    if "阴" in text:
        return "yin"
    if any(word in text for word in ("多云", "少云")):
        return "duoyun"
    if "晴" in text:
        return "qing"
    return "duoyun"
