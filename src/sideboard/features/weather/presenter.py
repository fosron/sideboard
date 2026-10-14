"""Pure presentation for weather payloads.

No I/O and no clock beyond ``view_context.now``; every missing measurement is
reported with :class:`Metric.unavailable` instead of a plausible-looking zero.
A card with no usable input renders an honest status line rather than raising.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ...core.clock import display_timezone
from ...core.contracts import (
    Item,
    Metric,
    PresentInput,
    Privacy,
    Severity,
    SourceEnvelope,
    StatusView,
    TableColumn,
    TableRow,
    TableView,
    ViewContext,
    ViewMode,
    ViewModel,
    compact_metric,
)
from ...core.present import status_from_envelope
from .config import WeatherWidgetSettings
from .models import CurrentConditions, WeatherPayload

__all__ = ["present", "weather_text"]

#: How many future hours compact mode sums for its rain figure.
_RAIN_HOURS = 6
_HOUR_LIMIT = 24
_DAY_LIMIT = 7

#: Where a row links to: a public forecast page for the coordinates the source was
#: configured with. A weather row has no article behind it, and "no link on this row" on
#: every row of a forecast is not useful either.
FORECAST_URL = "https://www.yr.no/en/forecast/daily-table/{latitude:.4f},{longitude:.4f}"


def _forecast_url(payload: WeatherPayload) -> str | None:
    """The location's public forecast page, when the payload carries its coordinates."""
    if payload.latitude is None or payload.longitude is None:
        return None
    return FORECAST_URL.format(latitude=payload.latitude, longitude=payload.longitude)


#: Bases keep every metric honest about where the number came from.
_BASIS_CURRENT = "forecast"
_BASIS_HOURLY = "hourly forecast"
_BASIS_DAILY = "daily forecast"

#: One text-presentation Unicode symbol per WMO code group. Deliberately not emoji: a
#: double-width glyph breaks a metric grid, and the terminal requirements promise no Nerd
#: Font and no emoji. Where Unicode defaults a symbol to emoji presentation, U+FE0E
#: (variation selector-15) asks for the text form, which is one cell wide.
_WMO_ICON: dict[int, str] = {
    0: "\u2600",  # ☀ clear sky
    1: "\u263c",  # ☼ mainly clear
    2: "\u26c5\ufe0e",  # ⛅ partly cloudy
    3: "\u2601",  # ☁ overcast
    45: "\u2261",  # ≡ fog
    48: "\u2261",  # ≡ depositing rime fog
    51: "\u2602",  # ☂ light drizzle
    53: "\u2602",  # ☂ moderate drizzle
    55: "\u2602",  # ☂ dense drizzle
    56: "\u2602",  # ☂ light freezing drizzle
    57: "\u2602",  # ☂ dense freezing drizzle
    61: "\u2614\ufe0e",  # ☔ slight rain
    63: "\u2614\ufe0e",  # ☔ moderate rain
    65: "\u2614\ufe0e",  # ☔ heavy rain
    66: "\u2614\ufe0e",  # ☔ light freezing rain
    67: "\u2614\ufe0e",  # ☔ heavy freezing rain
    71: "\u2744",  # ❄ slight snow
    73: "\u2744",  # ❄ moderate snow
    75: "\u2744",  # ❄ heavy snow
    77: "\u2744",  # ❄ snow grains
    80: "\u2614\ufe0e",  # ☔ slight rain showers
    81: "\u2614\ufe0e",  # ☔ moderate rain showers
    82: "\u2614\ufe0e",  # ☔ violent rain showers
    85: "\u2744",  # ❄ slight snow showers
    86: "\u2744",  # ❄ heavy snow showers
    95: "\u26c8\ufe0e",  # ⛈ thunderstorm
    96: "\u26c8\ufe0e",  # ⛈ thunderstorm with slight hail
    99: "\u26c8\ufe0e",  # ⛈ thunderstorm with heavy hail
}

_WMO_TEXT: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snow",
    73: "moderate snow",
    75: "heavy snow",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

# Locale-independent labels: the dashboard must not depend on the host locale.
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def weather_text(code: int | None) -> str:
    """Short WMO weather-code description. Unknown codes stay visibly unknown."""
    if code is None:
        return "unknown"
    return _WMO_TEXT.get(code, "unknown")


def weather_icon(code: int | None) -> str:
    """One symbol for a WMO code, or an empty string when the code is unknown.

    An empty string rather than a default glyph: an icon that guesses is worse than no
    icon, and the words beside it already say what the sky is doing.
    """
    if code is None:
        return ""
    return _WMO_ICON.get(code, "")


def present(request: PresentInput) -> ViewModel:
    """Map one bound weather snapshot into a compact or detail view.

    The presenter never raises for missing input: an unbound, unconfigured or
    payload-less source becomes a status card describing what is missing.
    """
    settings = _coerce_settings(request.settings)
    context = request.context
    envelope = request.inputs.get("primary")
    payload = envelope.payload if envelope is not None else None
    if not isinstance(payload, WeatherPayload):
        return _status_view(envelope, context)

    zone = display_timezone(payload.timezone)
    title = payload.location_label or "Weather"
    if context.mode is ViewMode.DETAIL:
        return ViewModel(
            title=title,
            subtitle=_subtitle(payload, zone),
            metrics=_detail_metrics(payload),
            items=_hourly_items(payload, settings, zone, now=context.now),
            table=_daily_table(payload, settings),
            footnote=_footnote(payload, zone),
            privacy=Privacy.PUBLIC,
        )
    return ViewModel(
        title=title,
        subtitle=_subtitle(payload, zone),
        metrics=_compact_metrics(payload),
        # Attribution, model honesty and forecast validity are on the detail view: the
        # card is a glance at the weather, and "model-derived forecast · Open-Meteo ·
        # valid until 14:31" under two rows of numbers is not part of it.
        footnote=None,
        privacy=Privacy.PUBLIC,
    )


def _status_view(envelope: SourceEnvelope | None, context: ViewContext) -> ViewModel:
    status = status_from_envelope(
        envelope,
        offline=context.offline,
        demo=context.demo,
        privacy=context.privacy,
    )
    if status is None:  # pragma: no cover - defensive: payload was rejected above
        status = StatusView(severity=Severity.WARNING, headline="No weather data")
    # The widget settings carry no label; the place name only exists in the payload.
    return ViewModel(title="Weather", status=status, privacy=Privacy.PUBLIC)


def _coerce_settings(settings: BaseModel) -> WeatherWidgetSettings:
    if isinstance(settings, WeatherWidgetSettings):
        return settings
    return WeatherWidgetSettings.model_validate(settings.model_dump())


# --------------------------------------------------------------------------- #
# Compact
# --------------------------------------------------------------------------- #


def _compact_metrics(payload: WeatherPayload) -> tuple[Metric, ...]:
    current = payload.current
    code = current.weather_code if current else None
    total, total_note, chance, chance_note = _rain_window(payload, _RAIN_HOURS)
    return (
        _with_icon(
            _metric(
                "temperature",
                "Temp",
                current.temperature_c if current else None,
                basis=_BASIS_CURRENT,
                unit="°C",
                digits=1,
                as_of=payload.as_of,
                note="current conditions not returned" if current is None else None,
            ),
            code,
        ),
        _conditions_metric(current, payload.as_of),
        _metric(
            "wind",
            "Wind",
            current.wind_speed_kmh if current else None,
            basis=_BASIS_CURRENT,
            unit="km/h",
            digits=0,
            as_of=payload.as_of,
        ),
        _metric(
            "rain_next_6h",
            "Rain 6h",
            total,
            basis=_BASIS_HOURLY,
            unit="mm",
            digits=1,
            note=total_note,
        ),
        _metric(
            "rain_chance_6h",
            "Rain chance",
            chance,
            basis=_BASIS_HOURLY,
            unit="%",
            digits=0,
            note=chance_note,
        ),
    )


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #


def _detail_metrics(payload: WeatherPayload) -> tuple[Metric, ...]:
    current = payload.current
    code = current.weather_code if current else None
    total, total_note, chance, chance_note = _rain_window(payload, _RAIN_HOURS)
    return (
        _with_icon(
            _metric(
                "temperature",
                "Temp",
                current.temperature_c if current else None,
                basis=_BASIS_CURRENT,
                unit="°C",
                digits=1,
                as_of=payload.as_of,
                note="current conditions not returned" if current is None else None,
            ),
            code,
        ),
        _metric(
            "apparent",
            "Feels like",
            current.apparent_c if current else None,
            basis=_BASIS_CURRENT,
            unit="°C",
            digits=1,
            as_of=payload.as_of,
        ),
        _conditions_metric(current, payload.as_of),
        _metric(
            "wind",
            "Wind",
            current.wind_speed_kmh if current else None,
            basis=_BASIS_CURRENT,
            unit="km/h",
            digits=0,
            as_of=payload.as_of,
        ),
        _metric(
            "precipitation",
            "Rain now",
            current.precipitation_mm if current else None,
            basis=_BASIS_CURRENT,
            unit="mm",
            digits=1,
            as_of=payload.as_of,
        ),
        _metric(
            "rain_next_6h",
            "Rain 6h",
            total,
            basis=_BASIS_HOURLY,
            unit="mm",
            digits=1,
            note=total_note,
        ),
        _metric(
            "rain_chance_6h",
            "Rain chance",
            chance,
            basis=_BASIS_HOURLY,
            unit="%",
            digits=0,
            note=chance_note,
        ),
    )


def _hourly_items(
    payload: WeatherPayload,
    settings: WeatherWidgetSettings,
    zone: ZoneInfo,
    *,
    now: datetime | None = None,
) -> tuple[Item, ...]:
    """The next configured hours. The payload already caps this at 24 hours.

    An hour that is not today carries its weekday: a bare "09:00" under a table of dates
    reads as if it belonged to the last of them.
    """
    settings_hours = min(_HOUR_LIMIT, max(1, settings.show_hours))
    today = now.astimezone(zone).date() if now is not None else None
    url = _forecast_url(payload)
    items: list[Item] = []
    for point in payload.hourly[:settings_hours]:
        local = point.at.astimezone(zone)
        temperature = "—" if point.temperature_c is None else f"{point.temperature_c:.1f} °C"
        clock = f"{local:%H:%M}"
        if today is not None and local.date() != today:
            clock = f"{local:%a %H:%M}"
        meta: list[tuple[str, str]] = []
        if settings.show_rain:
            chance = (
                "—"
                if point.precipitation_probability is None
                else f"{point.precipitation_probability}%"
            )
            amount = "—" if point.precipitation_mm is None else f"{point.precipitation_mm:.1f} mm"
            meta.extend((("chance", chance), ("rain", amount)))
        items.append(
            Item(
                id=f"hour-{local:%Y%m%dT%H%M}",
                title=f"{clock} · {temperature}",
                subtitle=weather_text(point.weather_code)
                if point.weather_code is not None
                else None,
                url=url,
                published_at=point.at,
                meta=tuple(meta),
                privacy=Privacy.PUBLIC,
            )
        )
    return tuple(items)


def _daily_table(payload: WeatherPayload, settings: WeatherWidgetSettings) -> TableView | None:
    if not payload.daily:
        return None
    settings_days = min(_DAY_LIMIT, max(1, settings.show_days))
    columns = [
        TableColumn(id="date", label="Date"),
        TableColumn(id="min", label="Min °C", align="right"),
        TableColumn(id="max", label="Max °C", align="right"),
    ]
    if settings.show_rain:
        columns.extend(
            (
                TableColumn(id="rain", label="Rain mm", align="right"),
                TableColumn(id="chance", label="Chance %", align="right"),
            )
        )
    rows: list[TableRow] = []
    for point in payload.daily[:settings_days]:
        cells = {
            "date": _day_label(point.date),
            "min": _number(point.tmin_c, 0, "°"),
            "max": _number(point.tmax_c, 0, "°"),
        }
        if settings.show_rain:
            cells["rain"] = _number(point.precipitation_mm, 1)
            cells["chance"] = (
                "—"
                if point.precipitation_probability is None
                else f"{point.precipitation_probability}%"
            )
        rows.append(
            TableRow(
                id=point.date.isoformat(),
                cells=cells,
                privacy=Privacy.PUBLIC,
                url=_forecast_url(payload),
            )
        )
    return TableView(
        columns=tuple(columns),
        rows=tuple(rows),
        truncated=len(payload.daily) > settings_days,
        footnote=_BASIS_DAILY,
    )


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _metric(
    metric_id: str,
    label: str,
    value: float | int | str | None,
    *,
    basis: str,
    unit: str = "",
    digits: int = 0,
    note: str | None = None,
    as_of: datetime | None = None,
) -> Metric:
    """``compact_metric`` that also labels unavailable values with their basis."""
    metric = compact_metric(
        metric_id,
        label,
        value,
        unit=unit,
        basis=basis,
        digits=digits,
        note=note,
        as_of=as_of,
        privacy=Privacy.PUBLIC,
    )
    if metric.basis == basis:
        return metric
    return replace(metric, basis=basis)


def _with_icon(metric: Metric, code: int | None) -> Metric:
    """Prefix a metric's value with the weather symbol, when the code is known."""
    icon = weather_icon(code)
    if not icon or not metric.available:
        return metric
    return replace(metric, value_text=f"{icon} {metric.value_text}")


def _conditions_metric(current: CurrentConditions | None, as_of: datetime | None) -> Metric:
    if current is None or current.weather_code is None:
        return _metric(
            "conditions",
            "Sky",
            None,
            basis=_BASIS_CURRENT,
            note="weather code not returned",
            as_of=as_of,
        )
    return _metric(
        "conditions",
        "Sky",
        weather_text(current.weather_code),
        basis=_BASIS_CURRENT,
        as_of=as_of,
    )


def _rain_window(
    payload: WeatherPayload, hours: int
) -> tuple[float | None, str | None, int | None, str | None]:
    """Precipitation sum and peak probability over the next ``hours``.

    The sum covers only the hours the API returned. A partial window keeps that
    sum and says so, instead of treating the missing hours as dry (which would
    understate rain) or as a fabricated total.
    """
    points = payload.hourly[:hours]
    if not points:
        note = "no hourly forecast returned"
        return None, note, None, note
    amounts = [point.precipitation_mm for point in points if point.precipitation_mm is not None]
    chances = [
        point.precipitation_probability
        for point in points
        if point.precipitation_probability is not None
    ]
    total = sum(amounts) if amounts else None
    chance = max(chances) if chances else None

    amount_note: str | None = None
    if not amounts:
        amount_note = "precipitation not returned"
    elif len(amounts) < len(points):
        amount_note = f"partial: {len(amounts)} of {len(points)} hours"
    chance_note: str | None = None
    if not chances:
        chance_note = "probability not returned"
    elif len(chances) < len(points):
        chance_note = f"partial: {len(chances)} of {len(points)} hours"
    if len(points) < hours:
        shortfall = f"only {len(points)} of {hours} hours available"
        amount_note = f"{amount_note}; {shortfall}" if amount_note else shortfall
        chance_note = f"{chance_note}; {shortfall}" if chance_note else shortfall
    return total, amount_note, chance, chance_note


def _subtitle(payload: WeatherPayload, zone: ZoneInfo) -> str:
    """Which zone the forecast times are in. The "updated" time is the chrome's job."""
    _ = zone
    return payload.timezone


def _footnote(payload: WeatherPayload, zone: ZoneInfo) -> str:
    """Attribution, model honesty and forecast validity in one bounded line."""
    parts = [payload.model_note or "model-derived forecast", "Open-Meteo"]
    if payload.valid_until is not None:
        parts.append(f"valid until {payload.valid_until.astimezone(zone):%H:%M}")
    if payload.coverage.notes:
        parts.append(payload.coverage.notes[:160])
    return " · ".join(parts)


def _day_label(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.day:02d} {_MONTHS[day.month - 1]}"


def _number(value: float | None, digits: int, suffix: str = "") -> str:
    """Never render a missing number as 0: an em dash keeps the gap visible."""
    if value is None:
        return "—"
    return f"{value:,.{digits}f}{suffix}"
