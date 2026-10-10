import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest


SOURCE = Path(__file__).parents[1] / "vendor/ai_plugin/ai_plugin"


@pytest.fixture
def weather(monkeypatch):
    monkeypatch.syspath_prepend(str(SOURCE))
    modules = []
    for name in ("weather", "weather_image"):
        spec = importlib.util.spec_from_file_location(f"test_{name}", SOURCE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


def forecast():
    return {
        "current": {
            "time": "2026-10-10T09:30", "temperature_2m": 21,
            "weather_code": 0, "relative_humidity_2m": 60,
        },
        "hourly": {
            "time": [f"2026-10-10T{hour:02}:00" for hour in range(24)],
            "temperature_2m": list(range(24)),
            "weather_code": [0] * 24,
            "visibility": [50] * 9 + [400] + [16000] * 14,
        },
        "daily": {},
    }


def fetch_with_client(monkeypatch, service, handler, method, *args):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               timeout=httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)) as client:
            async def get_client():
                return client
            monkeypatch.setattr(service, "_get_cma_client", get_client)
            return await method(*args)
    return asyncio.run(run())


def test_hourly_single_request_keeps_observation_and_next_forecast_hour(weather, monkeypatch):
    service, _ = weather
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=forecast())

    result = fetch_with_client(monkeypatch, service, respond,
                               service.fetch_weather_open_meteo, {"latitude": 36.07, "longitude": 120.33})

    assert len(requests) == 1
    assert [hour["time"][-5:] for hour in result["hour"]] == ["09:30", "10:00", "12:00", "14:00", "16:00"]
    assert [hour["temp"] for hour in result["hour"]] == [21, 10, 12, 14, 16]
    assert result["visibility"] == "0.4km"
    assert requests[0].extensions["timeout"] == {"connect": 2.0, "read": 3.0, "write": 2.0, "pool": 2.0}


@pytest.mark.parametrize("at, expected", [
    ("2026-10-10T09:00", ["10:00", "12:00", "14:00", "16:00"]),
    ("2026-10-10T23:45", ["00:00", "02:00", "04:00", "06:00"]),
])
def test_hourly_selection_handles_exact_hours_and_midnight(weather, at, expected):
    service, _ = weather
    payload = forecast()
    payload["current"]["time"] = at
    payload["hourly"]["time"] += [f"2026-10-11T{hour:02}:00" for hour in range(8)]
    payload["hourly"]["temperature_2m"] += list(range(8))
    result = service._open_meteo_hours(payload["current"], payload["hourly"])
    assert [hour["time"][-5:] for hour in result[1:]] == expected


@pytest.mark.parametrize("provider", ["open_meteo", "alapi"])
@pytest.mark.parametrize("failure, attempts, reason", [
    ("timeout", 1, "ReadTimeout"), (503, 1, "HTTP 503"), (401, 1, "HTTP 401"),
])
def test_supplement_failures_are_bounded_and_diagnosable(weather, monkeypatch, caplog,
                                                       failure, attempts, reason, provider):
    service, _ = weather
    requests = []

    def respond(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("", request=request)
        return httpx.Response(failure)

    monkeypatch.setenv("JIANG_WEATHER_DETAIL_TOKEN", "test-weather-key")
    method = service.fetch_weather_open_meteo if provider == "open_meteo" else service.fetch_weather_alapi
    query = {"latitude": 36.07, "longitude": 120.33} if provider == "open_meteo" else "青岛"
    result = fetch_with_client(monkeypatch, service, respond, method, query)

    assert result is None
    assert len(requests) == attempts
    assert reason in caplog.text


def test_alapi_uses_post_without_credentials_in_url(weather, monkeypatch):
    service, _ = weather
    monkeypatch.setenv("JIANG_WEATHER_DETAIL_TOKEN", "test-weather-key")
    response_data = {"city": "青岛", "air": "37", "visibility": "16km", "hour": []}

    def respond(request):
        assert request.method == "POST"
        assert "test-weather-key" not in str(request.url)
        assert json.loads(request.content) == {"token": "test-weather-key", "city": "青岛", "hour": True}
        return httpx.Response(200, json={"success": True, "data": response_data})

    result = fetch_with_client(monkeypatch, service, respond, service.fetch_weather_alapi, "青岛")
    assert result == response_data


def test_missing_alapi_token_does_not_issue_a_request(weather, monkeypatch):
    service, _ = weather
    monkeypatch.delenv("JIANG_WEATHER_DETAIL_TOKEN", raising=False)

    async def unexpected_client():
        raise AssertionError("No request without credentials")

    monkeypatch.setattr(service, "_get_cma_client", unexpected_client)
    assert asyncio.run(service.fetch_weather_alapi("青岛")) is None


def test_shared_client_keeps_original_timeouts(weather):
    service, _ = weather

    async def run():
        client = await service._get_cma_client()
        try:
            assert client.timeout == httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["open_meteo", "alapi"])
def test_successful_supplements_keep_five_minute_cache(weather, monkeypatch, provider):
    service, _ = weather
    monkeypatch.setenv("JIANG_WEATHER_DETAIL_TOKEN", "test-weather-key")
    now = [1000.0]
    monkeypatch.setattr(service, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    requests = []

    def respond(request):
        requests.append(request)
        data = forecast() if provider == "open_meteo" else {"success": True, "data": {"temp": 21}}
        return httpx.Response(200, json=data)

    method = service.fetch_weather_open_meteo if provider == "open_meteo" else service.fetch_weather_alapi
    query = {"latitude": 36.07, "longitude": 120.33} if provider == "open_meteo" else "青岛"
    first = fetch_with_client(monkeypatch, service, respond, method, query)
    now[0] += 299
    assert fetch_with_client(monkeypatch, service, respond, method, query) == first
    assert len(requests) == 1
    now[0] += 2
    assert fetch_with_client(monkeypatch, service, respond, method, query) == first
    assert len(requests) == 2


def test_card_pipeline_keeps_supplement_requests_parallel(weather, monkeypatch):
    service, renderer = weather

    async def run():
        alapi_started = asyncio.Event()
        meteo_started = asyncio.Event()

        async def alapi(place):
            alapi_started.set()
            await meteo_started.wait()
            return {"air": "37"}

        async def daily(place):
            await alapi_started.wait()
            return {"nowinfo": {"temperature": 21}}

        async def meteo(data):
            meteo_started.set()
            return {"hour": service._open_meteo_hours(forecast()["current"], forecast()["hourly"])}

        async def render(card, data):
            assert card["air"] == "37"
            assert len(card["hour"]) == 5
            return "weather-card.png"

        package = types.ModuleType("weather_parallel_probe")
        package.__path__ = []
        package.weather = types.SimpleNamespace(
            fetch_weather=daily, fetch_weather_alapi=alapi, fetch_weather_open_meteo=meteo,
            merge_weather_supplements=service.merge_weather_supplements,
        )
        package.weather_image = types.SimpleNamespace(
            build_weather_card_data=renderer.build_weather_card_data, render_weather_image=render,
        )
        monkeypatch.setitem(sys.modules, package.__name__, package)
        path = SOURCE.parents[2] / "runtime/jiang_schedule/weather_content.py"
        spec = importlib.util.spec_from_file_location(f"{package.__name__}.scheduled_weather", path)
        pipeline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline)
        assert await asyncio.wait_for(pipeline.generate_weather("青岛"), timeout=1) == "weather-card.png"

    asyncio.run(run())


def test_error_reason_never_contains_a_token(weather):
    service, _ = weather
    response = httpx.Response(403, request=httpx.Request("GET", "https://example.test/?token=secret-value"))
    with pytest.raises(httpx.HTTPStatusError) as error:
        response.raise_for_status()
    assert service._supplement_failure_reason(error.value) == "HTTP 403"


def test_real_alapi_shape_and_empty_hours_keep_open_meteo_forecasts(weather):
    service, renderer = weather
    hours = service._open_meteo_hours(forecast()["current"], forecast()["hourly"])
    original = [dict(hour) for hour in hours]
    supplement = service.merge_weather_supplements(
        {"temp": 21, "weather": "晴", "weather_code": "qing", "hour": hours},
        {"weather": "多云", "weather_code": "01", "temp": 23, "air": "37",
         "aqi": {"air": "43", "air_level": "优"}, "visibility": "16km", "hour": []},
    )
    card = renderer.build_weather_card_data({"nowinfo": {}}, "青岛", supplement)

    assert (card["air"], card["air_level"], card["visibility"]) == ("43", "优", "16km")
    assert card["hour"][0]["temp"] == 23
    assert card["hour"][1]["time"].endswith("10:00")
    assert hours == original
    markup = renderer._build_hourly_items(card)
    assert markup.count('class="hour-item"') == 5
    assert "--°" not in markup


@pytest.mark.parametrize("aqi, expected", [(87, 87), ({"air": "87", "air_level": "良"}, "87"), ({}, "")])
def test_aqi_object_is_not_rendered_as_text(weather, aqi, expected):
    _, renderer = weather
    card = renderer.build_weather_card_data({}, "青岛", {"aqi": aqi})
    assert card["air"] == expected
    assert not isinstance(card["air"], dict)


def test_cma_missing_markers_are_filtered_and_zero_is_preserved(weather):
    _, renderer = weather
    card = renderer.build_weather_card_data({"nowinfo": {
        "temperature": 0, "humidity": 0, "windDirection": 9999,
        "windScale": "微风", "visibility": "9999.0",
    }}, "青岛", {"air": 0})
    assert card["wind_direction"] == ""
    assert card["wind_scale"] == "微风"
    assert card["visibility"] == ""
    markup = renderer._build_html(card, None)
    assert "空气 优 0" in markup
    assert "湿度 0%" in markup
    assert "能见度 暂缺" in markup
    assert "9999" not in markup


@pytest.mark.parametrize("hours", [[], [{"temp": 20}], [{"temp": 20}, {"temp": 9999}]])
def test_missing_hourly_data_does_not_invent_forecasts(weather, hours):
    _, renderer = weather
    markup = renderer._build_hourly_items({"temp": 20, "weather_code": "qing", "hour": hours})
    assert "逐小时天气暂不可用" in markup
    assert "hour-item" not in markup
    assert "<img" not in markup


def test_partial_hourly_data_accepts_alias_and_does_not_pad(weather):
    _, renderer = weather
    markup = renderer._build_hourly_items({"hour": [
        {"temp": 0, "time": "2026-10-10T09:30", "wea": "晴"},
        {"tem": 12, "time": "2026-10-10T10:00"},
        {"temp": None, "time": "2026-10-10T12:00"},
    ]})
    assert markup.count('class="hour-item"') == 2
    assert markup.count("<img") == 1
    assert "0°" in markup and "12°" in markup
