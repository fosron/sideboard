"""Presenter tests: compact/detail rendering, honest gaps and contained failures."""

from __future__ import annotations

from datetime import UTC, datetime

from sideboard.core.contracts import (
    Capability,
    Connectivity,
    PresentInput,
    Privacy,
    SourceEnvelope,
    ViewContext,
    ViewMode,
    ViewModel,
)
from sideboard.features.weather.config import WeatherWidgetSettings
from sideboard.features.weather.feature import FEATURE
from sideboard.features.weather.models import CurrentConditions, WeatherPayload
from sideboard.features.weather.presenter import present, weather_text

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _context(mode: ViewMode = ViewMode.COMPACT) -> ViewContext:
    return ViewContext(mode=mode, width=60, height=24, now=NOW)


def _present(
    payload: WeatherPayload | None,
    envelope: SourceEnvelope | None,
    settings: WeatherWidgetSettings | None = None,
    mode: ViewMode = ViewMode.COMPACT,
) -> ViewModel:
    return present(
        PresentInput(
            settings=settings or WeatherWidgetSettings(),
            inputs={"primary": envelope},
            context=_context(mode),
        )
    )


# --------------------------------------------------------------------------- #
# Compact
# --------------------------------------------------------------------------- #


def test_compact_reports_the_core_metrics(sample_payload, make_envelope) -> None:
    view = _present(sample_payload, make_envelope(sample_payload))

    assert view.status is None
    assert view.title == "Home"
    assert "Europe/Madrid" in (view.subtitle or "")
    metrics = {metric.id: metric for metric in view.metrics}
    assert set(metrics) == {
        "temperature",
        "conditions",
        "wind",
        "rain_next_6h",
        "rain_chance_6h",
    }
    temperature = metrics["temperature"]
    assert temperature.value_text == "18.4"
    assert temperature.unit == "°C"
    assert temperature.numeric == 18.4
    assert temperature.basis == "forecast"
    assert temperature.as_of == sample_payload.as_of
    assert metrics["conditions"].value_text == "partly cloudy"
    assert metrics["wind"].value_text == "11"
    assert metrics["rain_next_6h"].value_text == "3.0"  # six hours of 0.5 mm
    assert metrics["rain_next_6h"].unit == "mm"
    assert metrics["rain_next_6h"].basis == "hourly forecast"
    assert metrics["rain_chance_6h"].value_text == "35"
    assert all(metric.privacy is Privacy.PUBLIC for metric in view.metrics)
    assert all(metric.basis in {"forecast", "hourly forecast"} for metric in view.metrics)
    # Attribution and forecast validity are on the detail view, not on the card.
    assert view.footnote is None
    detail = present(
        PresentInput(
            settings=WeatherWidgetSettings(),
            inputs={"primary": make_envelope(sample_payload)},
            context=ViewContext(
                now=datetime(2026, 10, 13, 12, 0, tzinfo=UTC), mode=ViewMode.DETAIL
            ),
        )
    )
    assert "Open-Meteo" in (detail.footnote or "")
    assert "model-derived" in (detail.footnote or "")


def test_missing_rain_stays_unavailable_not_zero(sample_payload, make_envelope) -> None:
    hourly = tuple(
        point.model_copy(update={"precipitation_mm": None, "precipitation_probability": None})
        for point in sample_payload.hourly
    )
    payload = sample_payload.model_copy(update={"hourly": hourly})

    view = _present(payload, make_envelope(payload))

    for metric_id in ("rain_next_6h", "rain_chance_6h"):
        metric = view.metric(metric_id)
        assert metric is not None
        assert metric.available is False
        assert metric.value_text == "—"
        assert metric.numeric is None


def test_partial_rain_window_keeps_the_sum_and_says_so(sample_payload, make_envelope) -> None:
    hourly = tuple(
        point.model_copy(update={"precipitation_mm": None if index == 0 else 0.5})
        for index, point in enumerate(sample_payload.hourly[:3])
    )
    payload = sample_payload.model_copy(update={"hourly": hourly})

    view = _present(payload, make_envelope(payload))

    metric = view.metric("rain_next_6h")
    assert metric is not None
    assert metric.available is True
    assert metric.value_text == "1.0"
    assert metric.note is not None
    assert "partial" in metric.note


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #


def test_detail_lists_hours_and_the_daily_table(sample_payload, make_envelope) -> None:
    settings = WeatherWidgetSettings(show_hours=24, show_days=7)

    view = _present(sample_payload, make_envelope(sample_payload), settings, ViewMode.DETAIL)

    assert view.status is None
    assert len(view.items) == 24
    assert view.items[0].id == "hour-20261013T1200"  # 10:00 UTC rendered in Madrid time
    assert "12:00" in view.items[0].title
    assert view.items[0].subtitle == "slight rain"
    assert dict(view.items[0].meta)["chance"] == "0%"
    assert view.table is not None
    assert [column.id for column in view.table.columns] == ["date", "min", "max", "rain", "chance"]
    assert len(view.table.rows) == 7
    assert view.table.rows[0].id == "2026-10-13"
    assert view.table.rows[0].cells["date"] == "Tue 13 Oct"
    assert view.table.rows[0].cells["min"] == "8°"
    assert view.table.rows[0].cells["max"] == "16°"
    assert view.table.rows[0].cells["rain"] == "1.2"
    assert view.table.rows[0].cells["chance"] == "40%"
    assert view.table.truncated is False
    assert "Open-Meteo" in (view.table.footnote or "")
    assert all(metric.privacy is Privacy.PUBLIC for metric in view.metrics)


def test_detail_respects_show_settings(sample_payload, make_envelope) -> None:
    settings = WeatherWidgetSettings(show_hours=3, show_days=2, show_rain=False)

    view = _present(sample_payload, make_envelope(sample_payload), settings, ViewMode.DETAIL)

    assert len(view.items) == 3
    assert view.items[0].meta == ()
    assert view.table is not None
    assert [column.id for column in view.table.columns] == ["date", "min", "max"]
    assert len(view.table.rows) == 2
    assert view.table.truncated is True


# --------------------------------------------------------------------------- #
# Missing input
# --------------------------------------------------------------------------- #


def test_unconfigured_source_shows_a_status_view(make_envelope) -> None:
    envelope = make_envelope(None, connectivity=Connectivity.UNCONFIGURED)

    view = _present(None, envelope)

    assert view.status is not None
    assert view.status.headline == "Not configured"
    assert view.metrics == ()


def test_missing_binding_shows_a_status_view_not_an_exception() -> None:
    view = _present(None, None)

    assert view.status is not None
    assert "not configured" in view.status.headline.lower()
    assert view.metrics == ()


def test_all_missing_values_are_unavailable_instead_of_zero(make_envelope) -> None:
    payload = WeatherPayload(
        location_label="Home",
        timezone="UTC",
        current=CurrentConditions(),
        hourly=(),
        daily=(),
    )

    view = _present(payload, make_envelope(payload))

    assert view.status is None
    assert view.metrics
    for metric in view.metrics:
        assert metric.available is False
        assert metric.value_text == "—"
        assert metric.numeric is None
        assert metric.basis in {"forecast", "hourly forecast"}


def test_all_missing_payload_still_renders_in_detail(make_envelope) -> None:
    payload = WeatherPayload(location_label="Home", timezone="UTC")

    view = _present(payload, make_envelope(payload), mode=ViewMode.DETAIL)

    assert view.status is None
    assert view.items == ()
    assert view.table is None
    assert view.metrics


def test_envelope_without_payload_is_contained(make_envelope) -> None:
    view = _present(None, make_envelope(None, connectivity=Connectivity.OK))

    assert view.status is not None
    assert view.metrics == ()


# --------------------------------------------------------------------------- #
# Mapping helpers and registration
# --------------------------------------------------------------------------- #


def test_weather_text_is_conservative() -> None:
    assert weather_text(0) == "clear sky"
    assert weather_text(61) == "slight rain"
    assert weather_text(95) == "thunderstorm"
    assert weather_text(999) == "unknown"
    assert weather_text(None) == "unknown"


def test_feature_registration_matches_the_contract() -> None:
    assert FEATURE.id == "weather"

    source = FEATURE.source("weather.forecast")
    assert source is not None
    assert source.payload_schema == "weather.forecast"
    assert source.payload_schema_version == 1
    assert source.capabilities == frozenset({Capability.HTTP_PUBLIC})
    assert source.cadence.active_seconds == 1200.0
    assert source.cadence.ambient_seconds == 1200.0
    assert source.cadence.background_seconds == 3600.0
    assert source.requires_setup is True
    assert source.setup_hint
    assert source.demo_builder is not None

    widget = FEATURE.widget("weather.summary")
    assert widget is not None
    assert widget.settings_model is WeatherWidgetSettings
    assert widget.default_category == "life"
    assert widget.min_width == 28
    assert widget.min_height == 4
    assert widget.inputs[0].name == "primary"
    assert widget.inputs[0].accepts == frozenset({"weather.forecast"})
    assert WeatherWidgetSettings().show_hours == 4
