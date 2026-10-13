"""Markets source tests: it is off by default and never guesses a listing.

The optional ``yfinance`` extra is not installed in the test environment, so the
"extra is absent" path is the real path here. Quotes themselves are unverified against
the upstream service; see docs/source-validation.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sideboard.core.clock import FakeClock
from sideboard.core.contracts import ProviderBuild
from sideboard.core.errors import BlockedError
from sideboard.features.markets.models import MarketsPayload, MarketsSettings
from sideboard.features.markets.provider import YahooProvider, demo_quotes

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def build(settings: MarketsSettings) -> ProviderBuild:
    return ProviderBuild(source_id="markets", settings=settings, clock=FakeClock(NOW))


async def test_disabled_mode_never_touches_the_network() -> None:
    settings = MarketsSettings(mode="disabled", symbol_map={"DEMO": "DEMO.EQ"})
    with pytest.raises(BlockedError) as info:
        await YahooProvider(settings=settings).fetch(build(settings))
    assert "disabled" in info.value.message


async def test_missing_symbol_map_is_refused_rather_than_guessed() -> None:
    settings = MarketsSettings(mode="yahoo", symbol_map={})
    with pytest.raises(BlockedError) as info:
        await YahooProvider(settings=settings).fetch(build(settings))
    assert "not a unique asset" in info.value.message


async def test_enabled_mode_without_the_optional_extra_names_the_extra() -> None:
    try:
        import yfinance  # type: ignore[import-not-found]  # noqa: F401

        pytest.skip("the optional markets extra is installed in this environment")
    except ImportError:
        pass
    settings = MarketsSettings(mode="yahoo", symbol_map={"DEMO": "DEMO.EQ"})
    with pytest.raises(BlockedError) as info:
        await YahooProvider(settings=settings).fetch(build(settings))
    assert "extra markets" in info.value.message


def test_settings_defaults_stay_off() -> None:
    settings = MarketsSettings()
    assert settings.mode == "disabled"
    assert settings.enabled is False
    assert settings.symbol_map == {}


def test_demo_quotes_are_labelled_synthetic() -> None:
    payload = demo_quotes()
    assert isinstance(payload, MarketsPayload)
    assert payload.provenance == "demo"
    assert payload.quotes[0].source == "demo"
