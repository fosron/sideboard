"""Shared fixtures for the weather feature tests.

Every test runs offline: HTTP goes through respx routes mounted on an httpx
``MockTransport``, so no socket is opened and no live Open-Meteo call is possible.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from httpx._config import UNSET as _HTTPX_UNSET

from sideboard.core.capabilities import HttpReader, HttpScope
from sideboard.core.clock import FakeClock
from sideboard.core.contracts import Connectivity, ProviderBuild, SourceEnvelope, SourcePayload
from sideboard.features.weather.config import WeatherSettings
from sideboard.features.weather.models import (
    CurrentConditions,
    DailyPoint,
    HourlyPoint,
    WeatherPayload,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "open-meteo.json"

#: Fixed instant so assertions never depend on the wall clock.
TEST_NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
#: 12:00 Europe/Madrid (CEST) in UTC, matching the fixture's ``current.time``.
TEST_AS_OF = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)

RAIN_AMOUNT_PER_HOUR = 0.5


class _CompatTimeout(httpx.Timeout):
    """``httpx.Timeout`` that tolerates the core call site omitting ``pool``.

    ``core.capabilities.HttpReader._stream`` builds
    ``httpx.Timeout(connect=..., read=..., write=...)`` with neither a default nor
    ``pool``; httpx 0.28 rejects that while building the request. Patching the name
    in the test process keeps these tests on the real HttpReader (allowlist,
    streaming, status handling) without editing repository code outside this
    feature. Remove once core passes ``pool`` (or a default) to ``httpx.Timeout``.
    """

    def __init__(
        self,
        timeout: Any = _HTTPX_UNSET,
        *,
        connect: Any = _HTTPX_UNSET,
        read: Any = _HTTPX_UNSET,
        write: Any = _HTTPX_UNSET,
        pool: Any = _HTTPX_UNSET,
    ) -> None:
        if timeout is _HTTPX_UNSET and pool is _HTTPX_UNSET:
            pool = 10.0
        super().__init__(timeout, connect=connect, read=read, write=write, pool=pool)


@pytest.fixture(autouse=True)
def _compat_httpx_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "Timeout", _CompatTimeout)


@pytest.fixture
def fixture_body() -> dict[str, Any]:
    """The synthetic Open-Meteo response body stored in ``fixtures/``."""
    parsed = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture
def weather_settings() -> WeatherSettings:
    return WeatherSettings(
        label="Home",
        latitude=40.4168,
        longitude=-3.7038,
        timezone="Europe/Madrid",
        forecast_days=7,
    )


@pytest.fixture
def mock_router() -> respx.MockRouter:
    """A local respx router; each test registers exactly the routes it expects."""
    return respx.MockRouter(assert_all_called=False)


@pytest.fixture
async def http_reader(mock_router: respx.MockRouter) -> AsyncIterator[HttpReader]:
    """A real HttpReader over a mocked transport, so URL validation and caps still run."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_router.async_handler))
    reader = HttpReader(client=client, scope=HttpScope.PUBLIC)
    try:
        yield reader
    finally:
        await reader.aclose()


@pytest.fixture
def make_build() -> Callable[..., ProviderBuild]:
    """ProviderBuild factory; pass ``http=None`` for the missing-capability case."""

    def build(
        http: HttpReader | None = None,
        *,
        settings: WeatherSettings | None = None,
        previous: SourceEnvelope | None = None,
        offline: bool = False,
    ) -> ProviderBuild:
        return ProviderBuild(
            source_id="weather_home",
            settings=settings
            or WeatherSettings(latitude=40.4168, longitude=-3.7038, timezone="Europe/Madrid"),
            clock=FakeClock(TEST_NOW),
            http=http,
            previous=previous,
            offline=offline,
        )

    return build


@pytest.fixture
def make_envelope() -> Callable[..., SourceEnvelope]:
    """Wrap a payload the way the host would before a presenter runs."""

    def build(
        payload: SourcePayload | None,
        *,
        connectivity: Connectivity = Connectivity.OK,
    ) -> SourceEnvelope:
        return SourceEnvelope(
            source_id="weather_home",
            payload_schema="weather.forecast",
            payload_schema_version=1,
            payload=payload,
            connectivity=connectivity,
        )

    return build


@pytest.fixture
def sample_payload() -> WeatherPayload:
    """A complete, synthetic payload with known values for assertions."""
    hourly = tuple(
        HourlyPoint(
            at=TEST_AS_OF + timedelta(hours=offset),
            temperature_c=18.0 - offset * 0.2,
            precipitation_probability=(offset * 7) % 60,
            precipitation_mm=RAIN_AMOUNT_PER_HOUR if offset < 6 else 0.0,
            weather_code=61 if offset < 6 else 2,
        )
        for offset in range(24)
    )
    daily = tuple(
        DailyPoint(
            date=date(2026, 10, 13) + timedelta(days=offset),
            tmin_c=8.0 + offset,
            tmax_c=16.0 + offset,
            precipitation_mm=1.2,
            precipitation_probability=40 + offset,
            sunrise=datetime(2026, 10, 13, 6, 0, tzinfo=UTC) + timedelta(days=offset),
            sunset=datetime(2026, 10, 13, 17, 45, tzinfo=UTC) + timedelta(days=offset),
            weather_code=3,
        )
        for offset in range(7)
    )
    return WeatherPayload(
        location_label="Home",
        timezone="Europe/Madrid",
        latitude=40.4168,
        longitude=-3.7038,
        current=CurrentConditions(
            temperature_c=18.4,
            apparent_c=17.9,
            precipitation_mm=0.0,
            weather_code=2,
            wind_speed_kmh=11.2,
            is_day=True,
            observed_at=TEST_AS_OF,
        ),
        hourly=hourly,
        daily=daily,
        as_of=TEST_AS_OF,
        valid_until=TEST_AS_OF + timedelta(minutes=20),
        provenance="open-meteo",
    )
