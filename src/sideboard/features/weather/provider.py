"""Open-Meteo forecast provider.

One bounded request per fetch, no independent timer and no geocoding. Without
configured coordinates the provider reports setup as missing instead of guessing
a place, and it never sends ``timezone=auto``: the configured IANA timezone is a
display choice, not evidence of where the user is.

HTTP validators (``ETag``/``Last-Modified``) are remembered on the provider
instance so a repeated 304 is answered from the previous payload object without
advancing the source date.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ...core.clock import display_timezone
from ...core.contracts import Coverage, Precision, ProviderBuild
from ...core.errors import CapabilityError, ParseError, UnavailableError
from .config import WeatherSettings
from .models import CurrentConditions, DailyPoint, HourlyPoint, WeatherPayload

__all__ = ["FORECAST_URL", "WeatherProvider", "build_provider", "demo_builder"]

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

#: The plan's 20-minute refresh window; a forecast older than this is stale.
VALID_FOR_SECONDS = 20 * 60

_MAX_HOURLY = 24
_MAX_DAILY = 7

_CURRENT_FIELDS = (
    "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,is_day"
)
_HOURLY_FIELDS = "temperature_2m,precipitation_probability,precipitation,weather_code"
_DAILY_FIELDS = (
    "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
    "precipitation_probability_max,sunrise,sunset"
)


class WeatherProvider:
    """Acquires one ``weather.forecast`` payload per call."""

    __slots__ = ("_etag", "_last_modified")

    def __init__(self) -> None:
        self._etag: str | None = None
        self._last_modified: str | None = None

    async def fetch(self, context: ProviderBuild) -> WeatherPayload:
        """Fetch and defensively validate one forecast. Raises typed errors on failure."""
        settings = _coerce_settings(context.settings)
        latitude, longitude = _coordinates(settings)
        if context.http is None:
            raise CapabilityError("weather requires the http_public capability")

        etag, last_modified = self._validators(context)
        response = await context.http.get_json(
            FORECAST_URL,
            params=_request_params(settings, latitude, longitude),
            etag=etag,
            last_modified=last_modified,
        )
        if response.not_modified:
            return self._previous(context)
        self._etag = response.header("etag") or self._etag
        self._last_modified = response.header("last-modified") or self._last_modified
        body = response.json()
        if not isinstance(body, Mapping):
            raise ParseError("weather response was not a JSON object")
        return _parse_payload(body, settings)

    def _validators(self, context: ProviderBuild) -> tuple[str | None, str | None]:
        """Instance validators win; fall back to the host's cached envelope values."""
        etag = self._etag
        last_modified = self._last_modified
        if context.previous is not None:
            etag = etag or context.previous.validator_etag
            last_modified = last_modified or context.previous.validator_last_modified
        return etag, last_modified

    @staticmethod
    def _previous(context: ProviderBuild) -> WeatherPayload:
        payload = context.previous.payload if context.previous is not None else None
        if isinstance(payload, WeatherPayload):
            # Return the same object, not a copy: the host detects "not modified" by
            # identity and uses that to keep the source date unchanged.
            return payload
        raise ParseError("provider reported not-modified without a cached forecast")

    async def aclose(self) -> None:
        """The provider owns no resources; safe to call more than once."""
        return


def build_provider(context: ProviderBuild) -> WeatherProvider:
    """Source factory. The provider keeps no configured state, so ``context`` is unused."""
    _ = context
    return WeatherProvider()


# --------------------------------------------------------------------------- #
# Request shape
# --------------------------------------------------------------------------- #


def _coerce_settings(settings: BaseModel) -> WeatherSettings:
    """``ProviderBuild`` promises only ``BaseModel``; accept a duck-typed settings object."""
    if isinstance(settings, WeatherSettings):
        return settings
    return WeatherSettings.model_validate(settings.model_dump())


def _coordinates(settings: WeatherSettings) -> tuple[float, float]:
    latitude = settings.latitude
    longitude = settings.longitude
    if latitude is None or longitude is None:
        # Never infer a location from timezone, locale or IP address.
        raise UnavailableError(
            "weather location is not configured",
            hint="set latitude and longitude in the source settings",
        )
    return latitude, longitude


def _request_params(
    settings: WeatherSettings, latitude: float, longitude: float
) -> dict[str, str | int | float]:
    return {
        "latitude": latitude,
        "longitude": longitude,
        # Explicit IANA name, never "auto": the service must not guess a place.
        "timezone": settings.timezone,
        "current": _CURRENT_FIELDS,
        "hourly": _HOURLY_FIELDS,
        "daily": _DAILY_FIELDS,
        "forecast_days": settings.forecast_days,
        "forecast_hours": _MAX_HOURLY,
        "wind_speed_unit": "kmh",
    }


# --------------------------------------------------------------------------- #
# Defensive parsing
# --------------------------------------------------------------------------- #


def _parse_payload(data: Mapping[str, Any], settings: WeatherSettings) -> WeatherPayload:
    """Map an untrusted Open-Meteo body onto validated payload models.

    Only a body that is not a JSON object is rejected. Individually missing or
    unusable fields become ``None``/empty sections and are recorded in
    :class:`Coverage`, so a partial forecast is still shown honestly.
    """
    zone = display_timezone(settings.timezone)
    missing: list[str] = []
    notes: list[str] = []

    current_section = _section(data, "current")
    if current_section is None:
        missing.append("current")
    current_time = _as_time(current_section.get("time") if current_section else None, zone)
    current: CurrentConditions | None = None
    if current_section is not None:
        current = CurrentConditions(
            temperature_c=_as_float(current_section.get("temperature_2m")),
            apparent_c=_as_float(current_section.get("apparent_temperature")),
            precipitation_mm=_as_float(current_section.get("precipitation")),
            weather_code=_as_int(current_section.get("weather_code")),
            wind_speed_kmh=_as_float(current_section.get("wind_speed_10m")),
            is_day=_as_bool(current_section.get("is_day")),
            observed_at=current_time,
        )
        for field, parsed in (
            ("current.time", current_time),
            ("current.temperature_2m", current.temperature_c),
            ("current.apparent_temperature", current.apparent_c),
            ("current.precipitation", current.precipitation_mm),
            ("current.weather_code", current.weather_code),
            ("current.wind_speed_10m", current.wind_speed_kmh),
            ("current.is_day", current.is_day),
        ):
            if parsed is None:
                missing.append(field)

    hourly, hourly_missing, hourly_notes = _parse_hourly(data, zone)
    daily, daily_missing, daily_notes = _parse_daily(data, zone)
    missing.extend(hourly_missing)
    missing.extend(daily_missing)
    notes.extend(hourly_notes)
    notes.extend(daily_notes)

    timezone_name = data.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        # An unusable echo is worse than the name the user configured.
        timezone_name = settings.timezone

    unique_missing = tuple(dict.fromkeys(missing))
    if unique_missing:
        notes.append("not returned by the API: " + ", ".join(unique_missing))

    return WeatherPayload(
        location_label=settings.label,
        timezone=timezone_name,
        latitude=settings.latitude,
        longitude=settings.longitude,
        current=current,
        hourly=hourly,
        daily=daily,
        as_of=current_time,
        precision=Precision.INSTANT,
        valid_until=(current_time + timedelta(seconds=VALID_FOR_SECONDS) if current_time else None),
        provenance="open-meteo",
        coverage=Coverage(
            missing=unique_missing,
            notes="; ".join(notes)[:400] if notes else None,
        ),
    )


def _parse_hourly(
    data: Mapping[str, Any], zone: ZoneInfo
) -> tuple[tuple[HourlyPoint, ...], list[str], list[str]]:
    section = _section(data, "hourly")
    if section is None:
        return (), ["hourly"], []
    missing: list[str] = []
    notes: list[str] = []
    times = _as_list(section.get("time"))
    temperatures = _as_list(section.get("temperature_2m"))
    probabilities = _as_list(section.get("precipitation_probability"))
    amounts = _as_list(section.get("precipitation"))
    codes = _as_list(section.get("weather_code"))
    if not times:
        missing.append("hourly.time")

    points: list[HourlyPoint] = []
    for index in range(min(_MAX_HOURLY, len(times))):
        at = _as_time(times[index], zone)
        if at is None:
            # A point without a usable timestamp cannot be placed on the timeline.
            continue
        points.append(
            HourlyPoint(
                at=at,
                temperature_c=_as_float(_at(temperatures, index)),
                precipitation_probability=_as_int(_at(probabilities, index)),
                precipitation_mm=_as_float(_at(amounts, index)),
                weather_code=_as_int(_at(codes, index)),
            )
        )
    if len(times) > _MAX_HOURLY:  # hostile or unexpectedly long series
        notes.append(f"hourly: kept the first {_MAX_HOURLY} of {len(times)} hours")
    elif times and len(times) < _MAX_HOURLY:
        notes.append(f"hourly: {len(times)} of {_MAX_HOURLY} hours returned")
    for name, series, parser in (
        ("temperature_2m", temperatures, _as_float),
        ("precipitation_probability", probabilities, _as_int),
        ("precipitation", amounts, _as_float),
        ("weather_code", codes, _as_int),
    ):
        if not any(parser(item) is not None for item in series):
            missing.append(f"hourly.{name}")
    return tuple(points), missing, notes


def _parse_daily(
    data: Mapping[str, Any], zone: ZoneInfo
) -> tuple[tuple[DailyPoint, ...], list[str], list[str]]:
    section = _section(data, "daily")
    if section is None:
        return (), ["daily"], []
    missing: list[str] = []
    notes: list[str] = []
    dates = _as_list(section.get("time"))
    codes = _as_list(section.get("weather_code"))
    maxima = _as_list(section.get("temperature_2m_max"))
    minima = _as_list(section.get("temperature_2m_min"))
    amounts = _as_list(section.get("precipitation_sum"))
    probabilities = _as_list(section.get("precipitation_probability_max"))
    sunrises = _as_list(section.get("sunrise"))
    sunsets = _as_list(section.get("sunset"))
    if not dates:
        missing.append("daily.time")

    points: list[DailyPoint] = []
    for index in range(min(_MAX_DAILY, len(dates))):
        day = _as_date(dates[index])
        if day is None:
            # A day without a usable date cannot be shown as a forecast day.
            continue
        points.append(
            DailyPoint(
                date=day,
                tmin_c=_as_float(_at(minima, index)),
                tmax_c=_as_float(_at(maxima, index)),
                precipitation_mm=_as_float(_at(amounts, index)),
                precipitation_probability=_as_int(_at(probabilities, index)),
                sunrise=_as_time(_at(sunrises, index), zone),
                sunset=_as_time(_at(sunsets, index), zone),
                weather_code=_as_int(_at(codes, index)),
            )
        )
    if len(dates) > _MAX_DAILY:  # the payload contract caps the forecast at seven days
        notes.append(f"daily: kept the first {_MAX_DAILY} of {len(dates)} days")
    elif dates and len(dates) < _MAX_DAILY:
        notes.append(f"daily: {len(dates)} of {_MAX_DAILY} days returned")
    for name, series, parser in (
        ("weather_code", codes, _as_int),
        ("temperature_2m_max", maxima, _as_float),
        ("temperature_2m_min", minima, _as_float),
        ("precipitation_sum", amounts, _as_float),
        ("precipitation_probability_max", probabilities, _as_int),
    ):
        if not any(parser(item) is not None for item in series):
            missing.append(f"daily.{name}")
    if not any(_as_time(item, zone) is not None for item in sunrises):
        missing.append("daily.sunrise")
    if not any(_as_time(item, zone) is not None for item in sunsets):
        missing.append("daily.sunset")
    return tuple(points), missing, notes


# --------------------------------------------------------------------------- #
# Untrusted-JSON helpers
# --------------------------------------------------------------------------- #


def _section(data: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = data.get(key)
    return value if isinstance(value, Mapping) else None


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _at(series: Sequence[Any], index: int) -> Any:
    return series[index] if index < len(series) else None


def _as_float(value: Any) -> float | None:
    # ``bool`` is an ``int`` subclass; a JSON ``true`` is not a measurement.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return None


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _as_time(value: Any, zone: ZoneInfo) -> datetime | None:
    """Parse an Open-Meteo timestamp.

    Open-Meteo returns wall-clock time in the requested timezone without an
    offset, so a naive value is interpreted in the configured zone and then
    converted to UTC; stamping it as UTC directly would shift the instant.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=zone)
        except OverflowError, ValueError:  # pragma: no cover - defensive
            parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(UTC)
    except OverflowError, ValueError:  # pragma: no cover - defensive
        return None


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #


def demo_builder() -> WeatherPayload:
    """Synthetic forecast for ``--demo``: no network, obviously not real data.

    The hourly grid is aligned to the hour, but the payload is *issued* now, so a demo
    card reads `fresh` rather than `expired` (the real provider issues relative to its
    own fetch time in the same way).
    """
    issued = datetime.now(UTC)
    now = issued.replace(minute=0, second=0, microsecond=0)
    hourly = tuple(
        HourlyPoint(
            at=now + timedelta(hours=offset),
            temperature_c=round(10.0 + (offset % 6) * 0.7, 1),
            precipitation_probability=(offset * 7) % 60,
            precipitation_mm=0.4 if offset % 5 == 0 else 0.0,
            weather_code=61 if offset % 5 == 0 else 2,
        )
        for offset in range(_MAX_HOURLY)
    )
    daily = tuple(
        DailyPoint(
            date=now.date() + timedelta(days=offset),
            tmin_c=round(6.0 + (offset % 4) * 0.5, 1),
            tmax_c=round(12.0 + (offset % 5) * 0.6, 1),
            precipitation_mm=1.2 if offset % 3 == 0 else 0.0,
            precipitation_probability=(offset * 11) % 70,
            sunrise=now.replace(hour=7, minute=40) + timedelta(days=offset),
            sunset=now.replace(hour=18, minute=20) + timedelta(days=offset),
            weather_code=61 if offset % 3 == 0 else 2,
        )
        for offset in range(_MAX_DAILY)
    )
    return WeatherPayload(
        location_label="Demo City",
        timezone="UTC",
        latitude=0.0,
        longitude=0.0,
        current=CurrentConditions(
            temperature_c=10.4,
            apparent_c=9.6,
            precipitation_mm=0.0,
            weather_code=2,
            wind_speed_kmh=9.0,
            is_day=True,
            observed_at=now,
        ),
        hourly=hourly,
        daily=daily,
        as_of=now,
        precision=Precision.INSTANT,
        # Issued now, not at the start of the hour it was truncated to.
        valid_until=issued + timedelta(seconds=VALID_FOR_SECONDS),
        provenance="demo (synthetic)",
        coverage=Coverage(notes="synthetic demo payload; not a real forecast"),
        model_note="synthetic demo data, not a forecast",
    )
