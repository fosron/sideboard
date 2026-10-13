"""Validated payloads for the portfolio, prices and FX sources.

These are ``import_driven`` sources: they read the local domain store and never poll a
broker. A payload always distinguishes manual, statement and market values, and keeps
the statement date separate from the fetch time.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from ...core.contracts import SourcePayload

__all__ = [
    "AccountRecord",
    "DividendRecord",
    "FxPayload",
    "FxRateRecord",
    "HoldingsPayload",
    "LinkRecord",
    "PositionRecord",
    "PriceRecord",
    "PricesPayload",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PositionRecord(_Frozen):
    account: str
    instrument_id: str
    quantity: str
    currency: str
    isin: str | None = None
    venue: str | None = None
    name: str | None = None
    cost_basis_total: str | None = None
    cost_basis_currency: str | None = None
    as_of: date | None = None
    source: str = "manual"


class AccountRecord(_Frozen):
    account: str
    as_of: date | None = None
    import_id: str | None = None
    updated_at: str | None = None
    note: str | None = None
    position_count: int = 0


class DividendRecord(_Frozen):
    dividend_id: str
    instrument_id: str
    kind: str
    gross: str | None = None
    currency: str | None = None
    ex_date: date | None = None
    record_date: date | None = None
    pay_date: date | None = None
    source: str | None = None
    notes: str | None = None


class LinkRecord(_Frozen):
    link_id: str
    label: str
    url: str
    kind: str = "report"
    instrument_id: str | None = None


class HoldingsPayload(SourcePayload):
    positions: tuple[PositionRecord, ...] = ()
    accounts: tuple[AccountRecord, ...] = ()
    dividends: tuple[DividendRecord, ...] = ()
    links: tuple[LinkRecord, ...] = ()
    unpriced_instruments: tuple[str, ...] = ()
    last_import_at: datetime | None = None


class PriceRecord(_Frozen):
    instrument_id: str
    currency: str
    price: str
    price_unit_multiplier: str = "1"
    as_of: date | None = None
    fetched_at: datetime | None = None
    source: str = "manual"
    quote_type: str = "manual"


class PricesPayload(SourcePayload):
    quotes: tuple[PriceRecord, ...] = ()
    previous_closes: dict[str, str] = {}


class FxRateRecord(_Frozen):
    base: str
    quote: str
    rate: str
    as_of: date | None = None
    source: str = "manual"


class FxPayload(SourcePayload):
    rates: tuple[FxRateRecord, ...] = ()
