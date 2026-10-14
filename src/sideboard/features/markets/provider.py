"""Optional market quotes.

Kept off by default and isolated behind the provider interface. The listing mapping is
**explicit**: an instrument identifier maps to one configured symbol, and nothing is
guessed here. `VRC.WA` is only a candidate mapping for one symbol until the listing,
currency and venue have been checked.

``yfinance`` is an unofficial, best-effort source for personal use with upstream-data
restrictions; the extra is optional, so registration does no import. The library
performs its own HTTP inside a bounded worker thread, which is why this module states
that its requests do not pass through the shared :class:`HttpReader`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from ...core.contracts import (
    Cadence,
    Capability,
    Coverage,
    Precision,
    ProviderBuild,
    SourcePayload,
    SourceSpec,
)
from ...core.errors import BlockedError, ParseError
from .models import MarketQuoteRecord, MarketsPayload, MarketsSettings

log = logging.getLogger("sideboard.markets")

__all__ = ["MarketQuoteData", "YahooProvider", "demo_quotes", "make_spec"]


@dataclass(frozen=True, slots=True)
class MarketQuoteData:
    """One quote as returned by the library, before payload validation."""

    currency: str
    price: float
    as_of: date


QUOTE_TIMEOUT_SECONDS = 20.0


class YahooProvider:
    """Best-effort quotes through the optional ``markets`` extra."""

    def __init__(self, *, settings: MarketsSettings) -> None:
        self.settings = settings
        self._yf: object | None = None

    def _library(self) -> object:
        if self._yf is None:
            try:
                import yfinance  # type: ignore[import-not-found]
            except ImportError as exc:  # optional extra is absent
                raise BlockedError(
                    "the markets extra is not installed; run `uv sync --locked --extra markets`",
                    extra="markets",
                ) from exc
            self._yf = yfinance
        return self._yf

    async def fetch(self, context: ProviderBuild) -> MarketsPayload:
        settings = context.settings
        assert isinstance(settings, MarketsSettings)
        if settings.mode != "yahoo":
            raise BlockedError(
                "market quote fetching is disabled; enable it explicitly in configuration"
            )
        if not settings.symbol_map:
            raise BlockedError(
                "no symbol mapping configured; a ticker string is not a unique asset, "
                "so nothing is guessed"
            )
        library = self._library()
        requested = [
            (instrument_id, symbol)
            for instrument_id, symbol in settings.symbol_map.items()
            if instrument_id in settings.instruments or not settings.instruments
        ]
        records: list[MarketQuoteRecord] = []
        failed: list[str] = []
        for instrument_id, symbol in requested:
            try:
                record = await asyncio.wait_for(
                    asyncio.to_thread(self._one_quote, library, symbol), QUOTE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                failed.append(instrument_id)
                continue
            except Exception as error:  # a single symbol must not fail the refresh
                log.info("market quote failed for %s: %s", instrument_id, type(error).__name__)
                failed.append(instrument_id)
                continue
            records.append(
                MarketQuoteRecord(
                    instrument_id=instrument_id,
                    symbol=symbol,
                    currency=record.currency,
                    price=str(record.price),
                    as_of=record.as_of,
                    fetched_at=context.clock.now(),
                    source="yfinance",
                    venue_note=settings.symbol_venues.get(instrument_id),
                )
            )
        if not records:
            raise ParseError("no market quote could be retrieved for the configured symbols")
        now = context.clock.now()
        newest = max((record.as_of for record in records if record.as_of is not None), default=None)
        return MarketsPayload(
            quotes=tuple(records),
            as_of=datetime.combine(newest, time.min, tzinfo=UTC) if newest else None,
            precision=Precision.DATE,
            valid_until=now + timedelta(hours=max(2, settings.refresh_seconds / 3600 * 2)),
            provenance="yfinance (unofficial, best effort)",
            coverage=Coverage(
                expected=len(requested),
                observed=len(records),
                missing=tuple(failed),
                notes="unofficial source; not guaranteed market coverage",
            ),
        )

    @staticmethod
    def _one_quote(library: object, symbol: str) -> MarketQuoteData:
        """Blocking yfinance call, run in a worker thread with a bounded deadline.

        Values are narrowed into typed locals rather than a ``dict[str, object]``, so a
        missing currency or price is a typed failure instead of a surprise downstream.
        """
        ticker = library.Ticker(symbol)  # type: ignore[attr-defined]
        currency: str = ""
        price: float | None = None
        observed: date | None = None
        try:
            raw_info = ticker.fast_info
            currency = str(getattr(raw_info, "currency", "") or "")
            last_price = getattr(raw_info, "last_price", None)
            if isinstance(last_price, int | float) and not isinstance(last_price, bool):
                price = float(last_price)
        except Exception:
            currency = ""
            price = None
        if price is None:
            history = ticker.history(period="5d")
            if history is None or len(history) == 0:
                raise ParseError(f"no quote data for {symbol}")
            price = float(history["Close"].iloc[-1])
            index = history.index[-1]
            observed = index.date() if hasattr(index, "date") else date.today()
        if not currency:
            raise ParseError(f"quote for {symbol} has no currency")
        return MarketQuoteData(
            currency=currency,
            price=price,
            as_of=observed or date.today(),
        )

    async def aclose(self) -> None:
        return None


def make_spec() -> SourceSpec:
    return SourceSpec(
        type_id="markets.quotes",
        payload_model=MarketsPayload,
        settings_model=MarketsSettings,
        payload_schema="markets.quotes",
        payload_schema_version=1,
        factory=lambda build: YahooProvider(settings=_settings_of(build)),
        capabilities=frozenset({Capability.HTTP_PUBLIC}),
        cadence=Cadence(active_seconds=3600, ambient_seconds=3600, background_seconds=4 * 3600),
        retention_seconds=7 * 24 * 3600,
        description=(
            "Optional market quotes through yfinance. Off by default; explicit symbol "
            "mapping required; no listing is ever guessed."
        ),
        requires_setup=True,
        setup_hint=(
            "Install the optional extra (uv sync --locked --extra markets), validate a "
            'symbol mapping, then set mode = "yahoo" and enabled = true.'
        ),
        demo_builder=demo_quotes,
        setup_check=lambda settings: bool(settings.symbol_map) and settings.mode == "yahoo",
    )


def _settings_of(build: ProviderBuild) -> MarketsSettings:
    settings = build.settings
    assert isinstance(settings, MarketsSettings)
    return settings


def demo_quotes() -> SourcePayload:
    return MarketsPayload(
        quotes=(
            MarketQuoteRecord(
                instrument_id="DEMO-EQUITY",
                symbol="DEMO.EQ",
                currency="EUR",
                price="12.34",
                as_of=date(2026, 10, 12),
                source="demo",
            ),
        ),
        precision=Precision.DATE,
        provenance="demo",
        coverage=Coverage(observed=1, notes="synthetic demo quote"),
    )
