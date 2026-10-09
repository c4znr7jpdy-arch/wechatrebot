"""
天气预报模块 — /xx天气 命令
中国气象局直连接口为主，接口盒子 (apihz) 为兜底，支持 1-7 天预报、实况和预警
"""
import asyncio
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta
from math import acos, asin, atan, cos, degrees, radians, sin, tan

import httpx

logger = logging.getLogger(__name__)

_CMA_SEARCH_URL = "https://weather.cma.cn/api/autocomplete"
_CMA_WEATHER_URL = "https://weather.cma.cn/api/weather/view"
_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
_CMA_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://weather.cma.cn/",
}
if "_cma_station_cache" not in globals():
    _cma_station_cache: dict[str, str] = {}
if "_cma_client" not in globals():
    _cma_client: httpx.AsyncClient | None = None
if "_cma_client_lock" not in globals():
    _cma_client_lock: asyncio.Lock | None = None
if "_alapi_cache" not in globals():
    _alapi_cache: dict[str, tuple[float, dict]] = {}
if "_open_meteo_cache" not in globals():
    _open_meteo_cache: dict[tuple[float, float], tuple[float, dict]] = {}
if "_target_forecast_cache" not in globals():
    _target_forecast_cache: dict[tuple[float, float, int], tuple[float, dict]] = {}

# apihz 负载均衡节点
_API_HOSTS = [
    "https://cn.apihz.cn",
    "http://81.69.163.176",
    "http://101.35.2.25",
    "http://124.222.204.22",
    "http://101.34.207.105",
    "http://43.142.65.209",
    "http://81.68.85.14",
    "http://124.222.2.141",
    "http://49.235.116.180",
    "http://129.211.70.28",
    "http://124.220.236.177",
]

_WEATHER_CACHE_TTL = 300
_weather_cache: dict[tuple[str, str, int], tuple[float, dict]] = {}


_WEATHER_EMOJI = {
    "晴": "☀️", "多云": "⛅", "阴": "☁️",
    "小雨": "🌦", "中雨": "🌧", "大雨": "🌧", "暴雨": "🌧",
    "雷阵雨": "⛈", "阵雨": "🌦", "雨": "🌧",
    "小雪": "🌨", "中雪": "❄️", "大雪": "❄️", "暴雪": "❄️", "雪": "🌨",
    "雾": "🌫", "霾": "🌫", "沙尘暴": "🌪",
}


def _weather_icon(text: str) -> str:
    for key, emoji in _WEATHER_EMOJI.items():
        if key in text:
            return emoji
    return "🌤"


def _compact_weather(w1: str, w2: str) -> str:
    if not w1:
        return w2 or ""
    if not w2 or w1 == w2:
        return w1
    return f"{w1}→{w2}"


_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _date_label(offset: int) -> str:
    dt = datetime.now() + timedelta(days=offset)
    weekday = _WEEKDAYS[dt.weekday()]
    return f"{dt.month}月{dt.day}日（{weekday}）"


def _extract_alarm_brief(title: str) -> str:
    """从预警标题中提取简短描述"""
    m = re.search(r"发布(.+)$", title)
    return m.group(1) if m else title


def _normalize_place_name(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    for suffix in ("特别行政区", "自治州", "自治县", "地区", "市", "县", "区", "盟"):
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[:-len(suffix)]
    return text


def _parse_station_entry(entry: object) -> tuple[str, str, str] | None:
    if isinstance(entry, str):
        parts = entry.split("|")
        if len(parts) >= 2 and parts[0] and parts[1]:
            return parts[0], parts[1], parts[3] if len(parts) > 3 else ""
    elif isinstance(entry, dict):
        station_id = str(entry.get("id") or entry.get("stationid") or "")
        name = str(entry.get("name") or "")
        path = str(entry.get("path") or entry.get("country") or "")
        if station_id and name:
            return station_id, name, path
    return None


def _select_station(entries: list, place: str, sheng: str = "") -> str | None:
    wanted = _normalize_place_name(place)
    province = _normalize_place_name(sheng)
    candidates = []
    for index, entry in enumerate(entries):
        parsed = _parse_station_entry(entry)
        if not parsed:
            continue
        station_id, name, path = parsed
        normalized_name = _normalize_place_name(name)
        score = 0
        if normalized_name == wanted:
            score += 100
        elif wanted and (wanted in normalized_name or normalized_name in wanted):
            score += 50
        if province and province in _normalize_place_name(path):
            score += 20
        if "中国" in path:
            score += 10
        candidates.append((score, -index, station_id))
    if not candidates:
        return None
    best_score, _, station_id = max(candidates)
    # 自动补全接口会返回同拼音开头的无关城市；没有任何名称/省份匹配时
    # 宁可回退到备用天气源，也不能把“上海”误识别成“上党”。
    return station_id if best_score >= 50 else None


async def _lookup_cma_station(
    client: httpx.AsyncClient,
    place: str,
    sheng: str = "",
) -> str | None:
    cache_key = f"{sheng}|{place}"
    cached = _cma_station_cache.get(cache_key)
    if cached:
        return cached

    response = await client.get(
        _CMA_SEARCH_URL,
        params={"q": place, "limit": 10},
    )
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("code")) != "0":
        return None
    station_id = _select_station(payload.get("data") or [], place, sheng)
    if station_id:
        _cma_station_cache[cache_key] = station_id
    return station_id


async def _get_cma_client() -> httpx.AsyncClient:
    global _cma_client, _cma_client_lock

    if _cma_client is not None and not _cma_client.is_closed:
        return _cma_client
    if _cma_client_lock is None:
        _cma_client_lock = asyncio.Lock()
    async with _cma_client_lock:
        if _cma_client is None or _cma_client.is_closed:
            timeout = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)
            _cma_client = httpx.AsyncClient(
                timeout=timeout,
                headers=_CMA_HEADERS,
                follow_redirects=True,
                trust_env=False,
            )
        return _cma_client


async def warm_weather_client() -> None:
    """预热天气服务连接，避免首条天气命令承担 TLS 建连耗时。"""
    client = await _get_cma_client()
    response = await client.get(
        _CMA_SEARCH_URL,
        params={"q": "北京", "limit": 1},
    )
    response.raise_for_status()
    await asyncio.gather(
        client.head("https://api.open-meteo.com/"),
        client.head("https://ensemble-api.open-meteo.com/"),
        client.head("https://v3.alapi.cn/"),
        return_exceptions=True,
    )


def _calculate_sunrise(
    day: datetime,
    latitude: float,
    longitude: float,
    timezone_hours: float,
) -> str:
    """使用 NOAA 近似算法计算当地日出时间。"""
    day_number = day.timetuple().tm_yday
    longitude_hour = longitude / 15.0
    approximate_time = day_number + ((6 - longitude_hour) / 24)
    mean_anomaly = (0.9856 * approximate_time) - 3.289
    true_longitude = (
        mean_anomaly
        + 1.916 * sin(radians(mean_anomaly))
        + 0.020 * sin(radians(2 * mean_anomaly))
        + 282.634
    ) % 360
    right_ascension = degrees(atan(0.91764 * tan(radians(true_longitude)))) % 360
    longitude_quadrant = int(true_longitude / 90) * 90
    ascension_quadrant = int(right_ascension / 90) * 90
    right_ascension = (
        right_ascension + longitude_quadrant - ascension_quadrant
    ) / 15
    sin_declination = 0.39782 * sin(radians(true_longitude))
    cos_declination = cos(asin(sin_declination))
    cos_hour_angle = (
        cos(radians(90.833))
        - sin_declination * sin(radians(latitude))
    ) / (cos_declination * cos(radians(latitude)))
    if cos_hour_angle < -1 or cos_hour_angle > 1:
        return "--"
    hour_angle = (360 - degrees(acos(cos_hour_angle))) / 15
    local_mean_time = (
        hour_angle
        + right_ascension
        - (0.06571 * approximate_time)
        - 6.622
    )
    utc_hour = (local_mean_time - longitude_hour) % 24
    local_hour = (utc_hour + timezone_hours) % 24
    hour = int(local_hour)
    minute = int(round((local_hour - hour) * 60))
    if minute == 60:
        hour = (hour + 1) % 24
        minute = 0
    return f"{hour:02d}:{minute:02d}"


def _normalize_cma_alarms(raw_alarms: object) -> list[dict]:
    alarms = []
    if not isinstance(raw_alarms, list):
        return alarms
    for item in raw_alarms:
        if isinstance(item, dict):
            alarm = dict(item)
            title = str(
                alarm.get("title")
                or alarm.get("headline")
                or alarm.get("signaltype")
                or ""
            ).strip()
            if title:
                alarm["title"] = title
                alarms.append(alarm)
        elif item:
            alarms.append({"title": str(item)})
    return alarms


def _adapt_cma_weather(payload: dict, days: int = 7) -> dict:
    data = payload.get("data") or {}
    location = data.get("location") or {}
    daily = (data.get("daily") or [])[:max(1, min(days, 7))]
    if not daily:
        raise ValueError("中国气象局未返回逐日天气")

    latitude = float(location.get("latitude") or 0)
    longitude = float(location.get("longitude") or 0)
    timezone_hours = float(location.get("timezone") or 8)
    path_parts = [
        part.strip()
        for part in re.split(r"[,，]", str(location.get("path") or ""))
        if part.strip()
    ]
    city = str(location.get("name") or (path_parts[-1] if path_parts else ""))
    province = path_parts[-2] if len(path_parts) >= 2 else ""
    result = {
        "code": 200,
        "msg": "success",
        "source": "cma",
        "sheng": province,
        "shi": city,
        "name": city,
        "latitude": latitude,
        "longitude": longitude,
        "timezone": timezone_hours,
        "nowinfo": data.get("now") or {},
        "alarm": _normalize_cma_alarms(data.get("alarm")),
        "uptime": data.get("lastUpdate") or "",
        "suntimes": [],
    }

    for index, forecast in enumerate(daily, start=1):
        date_text = str(forecast.get("date") or "")
        try:
            forecast_date = datetime.strptime(date_text, "%Y/%m/%d")
        except ValueError:
            forecast_date = datetime.now() + timedelta(days=index - 1)
        sunrise = _calculate_sunrise(
            forecast_date,
            latitude,
            longitude,
            timezone_hours,
        )
        converted = {
            "date": date_text,
            "weather1": forecast.get("dayText") or "",
            "weather2": forecast.get("nightText") or "",
            "weather1img": f"w{forecast.get('dayCode')}.png",
            "weather2img": f"w{forecast.get('nightCode')}.png",
            "wd1": forecast.get("high", ""),
            "wd2": forecast.get("low", ""),
            "winddirection1": forecast.get("dayWindDirection") or "",
            "winddirection2": forecast.get("nightWindDirection") or "",
            "windleve1": forecast.get("dayWindScale") or "",
            "windleve2": forecast.get("nightWindScale") or "",
            "sunrise": sunrise,
        }
        result["suntimes"].append({"sunrise": sunrise})
        if index == 1:
            result.update(converted)
        else:
            result[f"weatherday{index}"] = converted
    return result


async def _fetch_weather_cma(place: str, sheng: str = "", days: int = 7) -> dict:
    client = await _get_cma_client()
    station_id = await _lookup_cma_station(client, place, sheng)
    if not station_id:
        raise ValueError(f"中国气象局未找到城市: {place}")
    response = await client.get(
        _CMA_WEATHER_URL,
        params={"stationid": station_id},
    )
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("code")) != "0":
        raise ValueError(str(payload.get("msg") or "中国气象局返回失败"))
    return _adapt_cma_weather(payload, days)


async def _fetch_weather_apihz(place: str, sheng: str = "", days: int = 7) -> dict:
    """从 apihz 获取天气，作为中国气象局直连接口的兜底。"""
    api_id = os.getenv("JIANG_CONTENT_API_ID", "").strip()
    api_key = os.getenv("JIANG_CONTENT_API_KEY", "").strip()
    if not api_id or not api_key:
        raise RuntimeError("备用天气服务尚未配置")
    timeout = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)
    last_err = None
    for host in _API_HOSTS:
        try:
            url = f"{host}/api/tianqi/tqyb.php"
            params = {
                "id": api_id,
                "key": api_key,
                "place": place,
                "day": str(days),
                "hourtype": "0",
                "suntimetype": "1",
            }
            if sheng:
                params["sheng"] = sheng
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                if data.get("code") == 200:
                    data.setdefault("source", "apihz")
                    return data
        except (
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.ConnectError,
        ) as exc:
            last_err = exc
    raise RuntimeError(f"所有 apihz 节点均不可用: {last_err}")


async def fetch_weather(place: str, sheng: str = "", days: int = 7) -> dict:
    """获取天气预报：中国气象局直连优先，apihz 自动兜底。

    Args:
        place: 地点（市级名称）
        sheng: 省份名称（可选）
        days: 预报天数 1-7
    """
    cache_key = (place.strip(), sheng.strip(), days)
    cached = _weather_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _WEATHER_CACHE_TTL:
        return cached[1]

    try:
        data = await _fetch_weather_cma(place, sheng, days)
    except Exception as exc:
        logger.warning("中国气象局天气直连失败，回退 apihz: %s", exc)
        data = await _fetch_weather_apihz(place, sheng, days)
    _weather_cache[cache_key] = (time.monotonic(), data)
    return data


def _wmo_condition(code: object) -> tuple[str, str]:
    """把 Open-Meteo/WMO 天气码转换为中文描述和内置图标代码。"""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return "多云", "duoyun"
    if value == 0:
        return "晴", "qing"
    if value == 1:
        return "晴间多云", "duoyun"
    if value == 2:
        return "多云", "duoyun"
    if value == 3:
        return "阴", "yin"
    if value in (45, 48):
        return "雾", "wu"
    if value in (51, 53, 55, 56, 57):
        return "小雨", "xiaoyu"
    if value in (61, 63, 65, 66, 67):
        return "雨", "yu"
    if value in (71, 73, 75, 77, 85, 86):
        return "雪", "xue"
    if value in (80, 81, 82):
        return "阵雨", "zhenyu"
    if value in (95, 96, 99):
        return "雷阵雨", "lei"
    return "多云", "duoyun"


def _wind_direction_label(value: object) -> str:
    try:
        degree = float(value) % 360
    except (TypeError, ValueError):
        return ""
    labels = ("北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风")
    return labels[int((degree + 22.5) // 45) % 8]


def _wind_scale_label(value: object) -> str:
    """将 km/h 风速近似换算为蒲福风级。"""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return ""
    thresholds = (1, 6, 12, 20, 29, 39, 50, 62, 75, 89, 103, 118)
    scale = next((index for index, limit in enumerate(thresholds) if speed < limit), 12)
    return f"{scale}级"


def _ensemble_condition(
    cloud_cover: object,
    precipitation: object,
    snowfall: object,
) -> tuple[str, str]:
    """由集合均值生成保守的长期天气趋势描述。"""
    try:
        snow = float(snowfall)
    except (TypeError, ValueError):
        snow = 0
    try:
        rain = float(precipitation)
    except (TypeError, ValueError):
        rain = 0
    try:
        cloud = float(cloud_cover)
    except (TypeError, ValueError):
        cloud = 50
    if snow >= 0.1:
        return "有雪趋势", "xue"
    if rain >= 5:
        return "有雨趋势", "yu"
    if rain >= 0.5:
        return "可能有雨", "xiaoyu"
    if cloud >= 80:
        return "阴", "yin"
    if cloud >= 45:
        return "多云", "duoyun"
    return "晴到少云", "qing"


def _daily_value(daily: dict, key: str, index: int) -> object:
    values = daily.get(key) or []
    return values[index] if index < len(values) else ""


async def fetch_weather_target_date(
    weather_data: dict,
    target_offset: int,
) -> dict | None:
    """获取第 8—31 天的指定日期预报；远期使用 GFS 集合均值。"""
    if not 7 <= target_offset <= 30:
        return None
    try:
        latitude = float(weather_data.get("latitude"))
        longitude = float(weather_data.get("longitude"))
    except (TypeError, ValueError):
        return None

    cache_key = (round(latitude, 3), round(longitude, 3), target_offset)
    cached = _target_forecast_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _WEATHER_CACHE_TTL:
        return cached[1]

    target_date = (datetime.now() + timedelta(days=target_offset)).date().isoformat()
    client = await _get_cma_client()
    is_ensemble = target_offset > 15
    if is_ensemble:
        url = _OPEN_METEO_ENSEMBLE_URL
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": "auto",
            "forecast_days": 31,
            "models": "ncep_gefs05_ensemble_mean",
            "daily": (
                "temperature_2m_max,temperature_2m_min,cloud_cover_mean,"
                "precipitation_sum,snowfall_sum,relative_humidity_2m_mean,"
                "wind_speed_10m_max,wind_direction_10m_dominant"
            ),
        }
    else:
        url = _OPEN_METEO_URL
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": "auto",
            "forecast_days": 16,
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,relative_humidity_2m_mean,"
                "wind_speed_10m_max,wind_direction_10m_dominant"
            ),
        }

    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
        daily = (response.json().get("daily") or {})
        dates = daily.get("time") or []
        index = dates.index(target_date)
        high = _daily_value(daily, "temperature_2m_max", index)
        low = _daily_value(daily, "temperature_2m_min", index)
        humidity = _daily_value(daily, "relative_humidity_2m_mean", index)
        wind_speed = _daily_value(daily, "wind_speed_10m_max", index)
        wind_direction = _daily_value(daily, "wind_direction_10m_dominant", index)

        if is_ensemble:
            precipitation = _daily_value(daily, "precipitation_sum", index)
            snowfall = _daily_value(daily, "snowfall_sum", index)
            cloud_cover = _daily_value(daily, "cloud_cover_mean", index)
            weather_text, weather_code = _ensemble_condition(
                cloud_cover,
                precipitation,
                snowfall,
            )
            precipitation_label = "预计降水"
            precipitation_value = (
                f"{float(precipitation):g}mm"
                if precipitation not in (None, "")
                else ""
            )
            forecast_kind = "长期趋势"
            forecast_note = "集合模型趋势，日期越远变动越大"
        else:
            weather_text, weather_code = _wmo_condition(
                _daily_value(daily, "weather_code", index)
            )
            precipitation = _daily_value(
                daily,
                "precipitation_probability_max",
                index,
            )
            precipitation_label = "降雨概率"
            precipitation_value = (
                f"{float(precipitation):g}%"
                if precipitation not in (None, "")
                else ""
            )
            forecast_kind = "中期预报"
            forecast_note = "中期预报可能随最新模式更新而调整"

        result = {
            "source": "gfs_ensemble_mean" if is_ensemble else "open_meteo",
            "forecast_kind": forecast_kind,
            "forecast_note": forecast_note,
            "date": target_date,
            "weather": weather_text,
            "weather_code": weather_code,
            "high": high,
            "low": low,
            "humidity": humidity,
            "wind_direction": _wind_direction_label(wind_direction),
            "wind_scale": _wind_scale_label(wind_speed),
            "precipitation_label": precipitation_label,
            "precipitation": precipitation_value,
            # 指定日期源没有可靠的空气质量和逐日能见度，保持为空。
            "air": "",
            "visibility": "",
        }
        _target_forecast_cache[cache_key] = (time.monotonic(), result)
        return result
    except Exception as exc:
        logger.warning("指定日期天气获取失败(offset=%s): %s", target_offset, exc)
        return None


async def fetch_weather_open_meteo(weather_data: dict) -> dict | None:
    """按气象局经纬度获取当前天气与逐小时预报。"""
    try:
        latitude = float(weather_data.get("latitude"))
        longitude = float(weather_data.get("longitude"))
    except (TypeError, ValueError):
        return None

    cache_key = (round(latitude, 3), round(longitude, 3))
    cached = _open_meteo_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _WEATHER_CACHE_TTL:
        return cached[1]

    client = await _get_cma_client()
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": "auto",
        "forecast_days": 7,
        "current": (
            "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "weather_code,wind_speed_10m,wind_direction_10m,visibility"
        ),
        "hourly": (
            "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "weather_code,visibility"
        ),
        "daily": "temperature_2m_max,temperature_2m_min",
    }
    try:
        response = await client.get(_OPEN_METEO_URL, params=params)
        response.raise_for_status()
        payload = response.json()
        current = payload.get("current") or {}
        hourly = payload.get("hourly") or {}
        weather_text, weather_code = _wmo_condition(current.get("weather_code"))
        times = hourly.get("time") or []
        temperatures = hourly.get("temperature_2m") or []
        weather_codes = hourly.get("weather_code") or []
        visibility_values = hourly.get("visibility") or []
        current_time = str(current.get("time") or "")
        try:
            current_index = times.index(current_time)
        except ValueError:
            current_index = next(
                (index for index, value in enumerate(times) if str(value) >= current_time),
                0,
            )
        hour_items = []
        for index in (
            current_index,
            current_index + 2,
            current_index + 4,
            current_index + 6,
            current_index + 8,
        ):
            if index >= len(times) or index >= len(temperatures):
                continue
            hour_text, hour_code = _wmo_condition(
                weather_codes[index] if index < len(weather_codes) else None
            )
            hour_items.append(
                {
                    "time": times[index],
                    "temp": temperatures[index],
                    "wea": hour_text,
                    "wea_code": hour_code,
                    "code_system": "normalized",
                }
            )

        daily = payload.get("daily") or {}
        daily_times = daily.get("time") or []
        daily_highs = daily.get("temperature_2m_max") or []
        daily_lows = daily.get("temperature_2m_min") or []
        hourly_by_date: dict[str, list[float]] = {}
        for time_value, temperature in zip(times, temperatures):
            if temperature in (None, ""):
                continue
            date_key = str(time_value)[:10]
            hourly_by_date.setdefault(date_key, []).append(float(temperature))
        daily_trends = []
        for index, date_key in enumerate(daily_times[:7]):
            daily_trends.append(
                {
                    "date": date_key,
                    "high": daily_highs[index] if index < len(daily_highs) else "",
                    "low": daily_lows[index] if index < len(daily_lows) else "",
                    "hourly_temps": hourly_by_date.get(str(date_key), []),
                }
            )

        visibility = current.get("visibility")
        if visibility in (None, "") and visibility_values:
            visibility = visibility_values[0]
        try:
            # Open-Meteo 能见度单位固定为米；提前转成带单位的 km 文本，
            # 避免渲染层把低于 100 米的浓雾误判成几十公里。
            visibility = f"{float(visibility) / 1000:g}km"
        except (TypeError, ValueError):
            visibility = ""
        result = {
            "source": "open_meteo",
            "weather": weather_text,
            "weather_code": weather_code,
            "temp": current.get("temperature_2m", ""),
            "feelst": current.get("apparent_temperature", ""),
            "humidity": current.get("relative_humidity_2m", ""),
            "wind_direction": _wind_direction_label(current.get("wind_direction_10m")),
            "wind_scale": _wind_scale_label(current.get("wind_speed_10m")),
            "visibility": visibility,
            "update_time": current.get("time", ""),
            "hour": hour_items,
            "daily_trends": daily_trends,
        }
        _open_meteo_cache[cache_key] = (time.monotonic(), result)
        return result
    except Exception as exc:
        logger.warning("Open-Meteo 实况/小时天气获取失败: %s", exc)
        return None


async def fetch_weather_alapi(city: str) -> dict | None:
    """从 ALAPI 获取本地化实况、空气质量与能见度补充数据。"""
    token = os.getenv("JIANG_WEATHER_DETAIL_TOKEN", "").strip()
    if not token:
        return None
    cache_key = city.strip()
    cached = _alapi_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _WEATHER_CACHE_TTL:
        return cached[1]
    try:
        url = "https://v3.alapi.cn/api/tianqi"
        params = {"token": token, "city": city, "hour": "1"}
        client = await _get_cma_client()
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("success") and isinstance(data.get("data"), dict):
            result = data["data"]
            _alapi_cache[cache_key] = (time.monotonic(), result)
            return result
    except Exception as exc:
        logger.warning("ALAPI 实况补充数据获取失败: %s", exc)
    return None


def merge_weather_supplements(
    open_meteo_data: dict | None,
    alapi_data: dict | None,
) -> dict | None:
    """合并补充源：ALAPI 实况优先，Open-Meteo 负责可靠的小时预报。"""
    result = dict(open_meteo_data or {})
    if isinstance(alapi_data, dict):
        for key, value in alapi_data.items():
            if value in (None, "", [], {}):
                continue
            result[key] = value
    if open_meteo_data and open_meteo_data.get("hour"):
        result["hour"] = [dict(item) for item in open_meteo_data["hour"]]
        current_hour = result["hour"][0]
        current_hour["temp"] = result.get("temp", current_hour.get("temp", ""))
        current_hour["wea"] = result.get("weather", current_hour.get("wea", ""))
        current_hour["wea_code"] = result.get(
            "weather_code",
            current_hour.get("wea_code", ""),
        )
    if open_meteo_data and open_meteo_data.get("daily_trends"):
        result["daily_trends"] = [
            dict(item) for item in open_meteo_data["daily_trends"]
        ]
    return result or None


def format_weather(data: dict) -> str:
    """格式化天气预报为表格文本"""
    if data.get("code") != 200:
        return "没查到这个地方的天气，试试只说城市名？比如「青岛天气」「成都天气」\n省级地名查不了哦，得说具体的市"

    sheng = data.get("sheng", "")
    shi = data.get("shi", "")
    name = data.get("name", "")
    location = f"{sheng}{shi}" if sheng and shi else name

    today = datetime.now()
    end_day = today + timedelta(days=6)
    lines = [f"**{location}天气预报（{today.month}月{today.day}日 - {end_day.month}月{end_day.day}日）**"]
    lines.append("")

    # ── 实况 ──
    nowinfo = data.get("nowinfo")
    if nowinfo:
        temp = nowinfo.get("temperature", "")
        feelst = nowinfo.get("feelst", "")
        humidity = nowinfo.get("humidity", "")
        wind_dir = nowinfo.get("windDirection", "")
        wind_scale = nowinfo.get("windScale", "")
        parts = []
        if temp:
            parts.append(f"📍 实况：{temp}°")
        if feelst:
            parts.append(f"  体感 {feelst}°")
        if humidity:
            parts.append(f"  💧{humidity}%")
        if wind_dir or wind_scale:
            parts.append(f"  🌬{wind_dir}{wind_scale}")
        if parts:
            lines.append("".join(parts))
            lines.append("")

    # ── 预警 ──
    alarms = data.get("alarm") or []
    if alarms:
        for alarm in alarms:
            title = alarm.get("title", "")
            if title:
                lines.append(f"⚠️ {_extract_alarm_brief(title)}")
        lines.append("")

    # ── 表头 ──
    lines.append("| 日期 | 天气 |")
    lines.append("|------|------|")

    # ── 逐天 ──
    rain_count = 0
    for i in range(1, 8):
        if i == 1:
            w1 = data.get("weather1", "")
            w2 = data.get("weather2", "")
            t1 = data.get("wd1", "")
            t2 = data.get("wd2", "")
        else:
            dd = data.get(f"weatherday{i}")
            if not dd:
                continue
            w1 = dd.get("weather1", "")
            w2 = dd.get("weather2", "")
            t1 = dd.get("wd1", "")
            t2 = dd.get("wd2", "")

        weather_desc = _compact_weather(w1, w2)
        icon = _weather_icon(weather_desc)
        date_str = _date_label(i - 1)
        temp_range = f" {t2}~{t1}°" if t1 and t2 else ""
        lines.append(f"| {date_str} | {icon} {weather_desc}{temp_range} |")

        if any(r in weather_desc for r in ("雨", "雷")):
            rain_count += 1

    # ── 尾部建议 ──
    lines.append("")
    if rain_count >= 3:
        lines.append("最近几天阴雨天居多，出门记得带伞 ☂️")
    elif rain_count >= 1:
        lines.append("近几天有雨，出行注意备伞 ☂️")
    else:
        lines.append("近几天天气不错，适合出行 🌈")

    # ── 随机小 tips ──
    tips_pool = []
    if nowinfo:
        # 基于湿度的提示
        humidity_val = nowinfo.get("humidity", 0)
        try:
            h = int(humidity_val) if humidity_val else 0
        except (ValueError, TypeError):
            h = 0
        if h >= 80:
            tips_pool.append("湿度较高，注意防潮")
        elif h < 30:
            tips_pool.append("空气干燥，记得补水")
    if rain_count == 0:
        tips_pool.append("紫外线较强，注意防晒")
    if tips_pool:
        lines.append("")
        lines.append(f"💡 {random.choice(tips_pool)}")

    return "\n".join(lines)
