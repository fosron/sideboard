"""Optional market quotes behind an explicit symbol mapping."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from ...core.contracts import SourcePayload

__all__ = ["MarketQuoteRecord", "MarketsPayload", "MarketsSettings"]


class MarketsSettings(BaseModel):
    """Market data is off by default and never guesses a listing."""

    model_config = ConfigDict(extra="forbid")

    mode: str = "disabled"
    provider: str = "yahoo"
    instruments: list[str] = Field(default_factory=list)
    symbol_map: dict[str, str] = Field(default_factory=dict)
    symbol_currencies: dict[str, str] = Field(default_factory=dict)
    currency: str = "EUR"
    refresh_seconds: float = 3600.0
    active_seconds: float | None = None
    ambient_seconds: float | None = None
    background_seconds: float | None = None

    @property
    def enabled(self) -> bool:
        return self.mode == "yahoo"


class MarketQuoteRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str
    symbol: str
    currency: str
    price: str
    as_of: date | None = None
    fetched_at: datetime | None = None
    source: str = "yfinance"


class MarketsPayload(SourcePayload):
    quotes: tuple[MarketQuoteRecord, ...] = ()
