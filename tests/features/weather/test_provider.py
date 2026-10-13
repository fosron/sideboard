"""Provider tests: offline HTTP, defensive parsing, validators and typed failures."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from sideboard.core.capabilities import HttpReader
from sideboard.core.contracts import (
    Connectivity,
    Precision,
    Provider,
    ProviderBuild,
    SourceEnvelope,
)
from sideboard.core.errors import CapabilityError, ParseError, UnavailableError
from sideboard.features.weather.config import WeatherSettings
from sideboard.features.weather.models import WeatherPayload
from sideboard.features.weather.provider import FORECAST_URL, WeatherProvider, demo_builder


def test_provider_satisfies_the_contract() -> None:
    assert isinstance(WeatherProvider(), Provider)


async def test_fetch_parses_the_synthetic_fixture(
    http_reader: HttpReader,
    make_build: Callable[..., ProviderBuild],
    mock_router: respx.MockRouter,
    fixture_body: dict[str, Any],
) -> None:
    route = mock_router.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json=fixture_body, headers={"etag": '"v1"'})
    )

    payload = await WeatherProvider().fetch(make_build(http_reader))

    assert isinstance(payload, WeatherPayload)
    assert payload.location_label == "Home"
    assert payload.timezone == "Europe/Madrid"
    assert payload.provenance == "open-meteo"
    assert payload.precision is Precision.INSTANT
    assert payload.current is not None
    assert payload.current.temperature_c == pytest.approx(18.4)
    assert payload.current.apparent_c == pytest.approx(17.9)
    assert payload.current.precipitation_mm == pytest.approx(0.0)
    assert payload.current.weather_code == 2
    assert payload.current.wind_speed_kmh == pytest.approx(11.2)
    assert payload.current.is_day is True
    # 12:00 Europe/Madrid (CEST) is 10:00 UTC; a naive stamp would be wrong.
    assert payload.current.observed_at == datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    as_of = payload.as_of
    assert as_of is not None
    assert as_of == datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    assert payload.valid_until == as_of + timedelta(minutes=20)
    assert len(payload.hourly) == 24
    assert payload.hourly[0].at == as_of
    assert len(payload.daily) == 7
    assert payload.daily[0].date.isoformat() == "2026-10-13"
    assert payload.daily[0].sunrise == datetime(2026, 10, 13, 6, 24, tzinfo=UTC)
    assert payload.coverage.is_partial is False

    assert len(route.calls) == 1  # exactly one bounded request per fetch
    params = route.calls[0].request.url.params
    assert params["latitude"] == "40.4168"
    assert params["longitude"] == "-3.7038"
    assert params["timezone"] == "Europe/Madrid"  # never "auto": we do not guess a place
    assert "temperature_2m" in params["current"]
    assert "precipitation_probability" in params["hourly"]
    assert "sunrise" in params["daily"]
    assert params["forecast_days"] == "7"
    assert params["forecast_hours"] == "24"
    assert params["wind_speed_unit"] == "kmh"


async def test_not_modified_returns_the_identical_previous_payload(
    http_reader: HttpReader,
    make_build: Callable[..., ProviderBuild],
    mock_router: respx.MockRouter,
    fixture_body: dict[str, Any],
) -> None:
    route = mock_router.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json=fixture_body, headers={"etag": '"v1"'})
    )
    provider = WeatherProvider()
    payload = await provider.fetch(make_build(http_reader))
    previous = SourceEnvelope(
        source_id="weather_home",
        payload_schema="weather.forecast",
        payload_schema_version=1,
        payload=payload,
        connectivity=Connectivity.OK,
        validator_etag='"v1"',
    )

    route.mock(return_value=httpx.Response(304))
    result = await provider.fetch(make_build(http_reader, previous=previous))

    assert result is payload  # identity, so the host does not advance the source date
    assert route.calls[1].request.headers["if-none-match"] == '"v1"'


async def test_not_modified_uses_the_envelope_validators(
    http_reader: HttpReader,
    make_build: Callable[..., ProviderBuild],
    mock_router: respx.MockRouter,
    sample_payload: WeatherPayload,
) -> None:
    route = mock_router.get(FORECAST_URL).mock(return_value=httpx.Response(304))
    previous = SourceEnvelope(
        source_id="weather_home",
        payload_schema="weather.forecast",
        payload_schema_version=1,
        payload=sample_payload,
        connectivity=Connectivity.OK,
        validator_etag='"old"',
        validator_last_modified="Mon, 13 Oct 2026 10:00:00 GMT",
    )

    result = await WeatherProvider().fetch(make_build(http_reader, previous=previous))

    assert result is sample_payload
    request = route.calls[0].request
    assert request.headers["if-none-match"] == '"old"'
    assert request.headers["if-modified-since"] == "Mon, 13 Oct 2026 10:00:00 GMT"


async def test_not_modified_without_a_cached_payload_is_a_parse_error(
    http_reader: HttpReader,
    make_build: Callable[..., ProviderBuild],
    mock_router: respx.MockRouter,
) -> None:
    mock_router.get(FORECAST_URL).mock(return_value=httpx.Response(304))

    with pytest.raises(ParseError):
        await WeatherProvider().fetch(make_build(http_reader))


async def test_malformed_json_raises_parse_error(
    http_reader: HttpReader,
    make_build: Callable[..., ProviderBuild],
    mock_router: respx.MockRouter,
) -> None:
    mock_router.get(FORECAST_URL).mock(
        return_value=httpx.Response(
            200,
            content=b"<html>not json</html>",
            headers={"content-type": "application/json"},
        )
    )

    with pytest.raises(ParseError):
        await WeatherProvider().fetch(make_build(http_reader))


async def test_non_object_json_raises_parse_error(
    http_reader: HttpReader,
    make_build: Callable[..., ProviderBuild],
    mock_router: respx.MockRouter,
) -> None:
    mock_router.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=[1, 2, 3]))

    with pytest.raises(ParseError):
        await WeatherProvider().fetch(make_build(http_reader))


async def test_missing_location_raises_unavailable_without_any_request(
    http_reader: HttpReader,
    make_build: Callable[..., ProviderBuild],
    mock_router: respx.MockRouter,
) -> None:
    settings = WeatherSettings(label="Home")  # no coordinates configured

    with pytest.raises(UnavailableError):
        await WeatherProvider().fetch(make_build(http_reader, settings=settings))

    assert len(mock_router.calls) == 0  # never guess a location or geocode silently


async def test_missing_http_capability_raises_capability_error(
    make_build: Callable[..., ProviderBuild],
    weather_settings: WeatherSettings,
) -> None:
    with pytest.raises(CapabilityError):
        await WeatherProvider().fetch(make_build(None, settings=weather_settings))


async def test_partial_payload_records_coverage_instead_of_raising(
    http_reader: HttpReader,
    make_build: Callable[..., ProviderBuild],
    mock_router: respx.MockRouter,
    fixture_body: dict[str, Any],
) -> None:
    body = {
        "timezone": "Europe/Madrid",
        "current": fixture_body["current"],
        "hourly": fixture_body["hourly"],
        # no "daily" section at all
    }
    mock_router.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=body))

    payload = await WeatherProvider().fetch(make_build(http_reader))

    assert payload.daily == ()
    assert payload.current is not None
    assert len(payload.hourly) == 24
    assert "daily" in payload.coverage.missing
    assert payload.coverage.notes is not None
    assert "daily" in payload.coverage.notes
    assert payload.coverage.is_partial is True


async def test_invalid_types_become_none_instead_of_raising(
    http_reader: HttpReader,
    make_build: Callable[..., ProviderBuild],
    mock_router: respx.MockRouter,
    weather_settings: WeatherSettings,
) -> None:
    body = {
        "timezone": 7,
        "current": {
            "time": "yesterday",
            "temperature_2m": "warm",
            "apparent_temperature": {"c": 1},
            "precipitation": [1],
            "weather_code": "rain",
            "wind_speed_10m": True,
            "is_day": "yes",
        },
        "hourly": {
            "time": ["soon"],
            "temperature_2m": [None],
            "precipitation_probability": {},
            "precipitation": "wet",
            "weather_code": [400],
        },
        "daily": {"time": [1, 2], "weather_code": "rain", "sunrise": {"a": 1}},
    }
    mock_router.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=body))

    payload = await WeatherProvider().fetch(make_build(http_reader, settings=weather_settings))

    assert payload.current is not None
    assert payload.current.temperature_c is None  # "warm" is not a measurement
    assert payload.current.apparent_c is None
    assert payload.current.precipitation_mm is None
    assert payload.current.weather_code is None
    assert payload.current.wind_speed_kmh is None  # bool is not a temperature
    assert payload.current.is_day is None
    assert payload.current.observed_at is None
    assert payload.hourly == ()  # no usable timestamp means no usable point
    assert payload.daily == ()
    assert payload.as_of is None
    assert payload.valid_until is None
    assert payload.timezone == "Europe/Madrid"  # unusable echo replaced by config
    assert payload.coverage.is_partial is True


async def test_oversized_lists_are_capped_at_the_payload_limits(
    http_reader: HttpReader,
    make_build: Callable[..., ProviderBuild],
    mock_router: respx.MockRouter,
) -> None:
    times = [
        (datetime(2026, 10, 13, 10, 0) + timedelta(hours=offset)).isoformat(timespec="minutes")
        for offset in range(200)
    ]
    days = [
        (datetime(2026, 10, 13) + timedelta(days=offset)).date().isoformat() for offset in range(60)
    ]
    body = {
        "hourly": {
            "time": times,
            "temperature_2m": [10.0] * 200,
            "precipitation_probability": [0] * 200,
            "precipitation": [0.0] * 200,
            "weather_code": [2] * 200,
        },
        "daily": {
            "time": days,
            "weather_code": [2] * 60,
            "temperature_2m_max": [15.0] * 60,
            "temperature_2m_min": [5.0] * 60,
            "precipitation_sum": [0.0] * 60,
            "precipitation_probability_max": [10] * 60,
            "sunrise": [f"{day}T07:30" for day in days],
            "sunset": [f"{day}T18:30" for day in days],
        },
    }
    mock_router.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=body))

    payload = await WeatherProvider().fetch(make_build(http_reader))

    assert len(payload.hourly) == 24
    assert len(payload.daily) == 7
    notes = payload.coverage.notes or ""
    assert "kept the first 24" in notes
    assert "kept the first 7" in notes


def test_demo_builder_is_synthetic_and_complete() -> None:
    payload = demo_builder()

    assert payload.location_label == "Demo City"
    assert payload.provenance.startswith("demo")
    assert payload.model_note.startswith("synthetic")
    assert payload.current is not None
    assert payload.current.temperature_c is not None
    assert len(payload.hourly) == 24
    assert len(payload.daily) == 7
    assert payload.as_of is not None
