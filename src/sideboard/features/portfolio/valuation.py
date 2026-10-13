"""Decimal valuation math.

The formulas are the documented ones:

.. code-block:: text

    native_value = quantity × price × price_unit_multiplier
    value_eur    = native_value × FX(native_currency → EUR)

Rules enforced here:

* decimal arithmetic only; no float ever touches a money value
* a missing quote or a missing FX rate is **not** zero: the holding is excluded from
  the priced subtotal and the total is reported as incomplete
* a share of the tracked total is a share of the *tracked* total, not of the portfolio
* price change is not investment return
* GBp (pence) and ZAc (cents) are distinct from GBP/ZAR and are scaled explicitly
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

__all__ = [
    "FxRate",
    "HoldingValue",
    "Position",
    "Quote",
    "Valuation",
    "daily_change",
    "normalise_currency",
    "value_portfolio",
]

# Currencies quoted in minor units. GBp is a real listing convention (London), not a typo.
MINOR_UNITS: dict[str, tuple[str, Decimal]] = {
    "GBp": ("GBP", Decimal(100)),
    "GBX": ("GBP", Decimal(100)),
    "ZAc": ("ZAR", Decimal(100)),
}

SOURCE_MANUAL = "manual"
SOURCE_STATEMENT = "statement"
SOURCE_QUOTE = "quote"

MAX_REASONABLE_RATE = Decimal(1_000_000)
MIN_REASONABLE_RATE = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class Position:
    account: str
    instrument_id: str
    quantity: Decimal
    currency: str
    isin: str | None = None
    venue: str | None = None
    cost_basis_total: Decimal | None = None
    cost_basis_currency: str | None = None
    as_of: date | None = None
    source: str = SOURCE_MANUAL
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Quote:
    instrument_id: str
    currency: str
    price: Decimal
    price_unit_multiplier: Decimal = Decimal(1)
    as_of: date | None = None
    fetched_at: datetime | None = None
    source: str = SOURCE_MANUAL
    quote_type: str = SOURCE_MANUAL


@dataclass(frozen=True, slots=True)
class FxRate:
    base: str
    quote: str
    rate: Decimal
    as_of: date | None = None
    source: str = "manual"


@dataclass(frozen=True, slots=True)
class HoldingValue:
    account: str
    instrument_id: str
    quantity: Decimal
    currency: str
    price: Decimal | None = None
    price_basis: str = "missing quote"
    price_as_of: date | None = None
    native_value: Decimal | None = None
    value_eur: Decimal | None = None
    fx_basis: str = "missing FX rate"
    fx_as_of: date | None = None
    instrument_name: str | None = None
    reported_market_value: Decimal | None = None
    reported_currency: str | None = None
    daily_change_native: Decimal | None = None
    cost_basis_total: Decimal | None = None
    cost_basis_currency: str | None = None

    @property
    def priced(self) -> bool:
        return self.value_eur is not None

    @property
    def missing(self) -> tuple[str, ...]:
        problems: list[str] = []
        if self.price is None:
            problems.append("quote")
        if self.value_eur is None and self.price is not None:
            problems.append("fx")
        return tuple(problems)


@dataclass(frozen=True, slots=True)
class Valuation:
    base_currency: str = "EUR"
    holdings: tuple[HoldingValue, ...] = ()
    priced_subtotal: Decimal | None = None
    priced_count: int = 0
    unpriced_count: int = 0
    fx_dates: tuple[tuple[str, str, date | None], ...] = ()
    notes: tuple[str, ...] = ()
    shares: Mapping[str, Decimal] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.unpriced_count == 0 and self.priced_count > 0

    @property
    def total(self) -> Decimal | None:
        """The trackable total. ``None`` when nothing could be priced at all."""
        return self.priced_subtotal

    @property
    def total_label(self) -> str:
        if self.priced_count == 0:
            return "no priced holdings"
        if self.unpriced_count:
            return f"incomplete: {self.unpriced_count} of {self.priced_count + self.unpriced_count} unpriced"
        return "complete"


def normalise_currency(currency: str) -> tuple[str, Decimal]:
    """Return the FX currency and the divisor applied to a minor-unit quote."""
    entry = MINOR_UNITS.get(currency)
    if entry is None:
        return currency.upper(), Decimal(1)
    return entry


def native_value(quantity: Decimal, price: Decimal, multiplier: Decimal) -> Decimal:
    return quantity * price * multiplier


def _rate_lookup(
    fx: Mapping[tuple[str, str], FxRate], base: str
) -> Callable[[str], tuple[Decimal | None, str, date | None]]:
    def lookup(currency: str) -> tuple[Decimal | None, str, date | None]:
        fx_currency, _divisor = normalise_currency(currency)
        if fx_currency == base:
            return Decimal(1), f"{fx_currency}={base}", None
        direct = fx.get((fx_currency, base))
        if direct is not None:
            return direct.rate, f"{fx_currency}/{base} reference", direct.as_of
        inverse = fx.get((base, fx_currency))
        if inverse is not None and inverse.rate > 0:
            label = f"inverted from {base}/{fx_currency} reference"
            return Decimal(1) / inverse.rate, label, inverse.as_of
        return None, "missing FX rate", None

    return lookup


def value_portfolio(
    positions: tuple[Position, ...],
    quotes: Mapping[str, Quote],
    fx: Mapping[tuple[str, str], FxRate],
    *,
    base: str = "EUR",
    reported: Mapping[tuple[str, str], tuple[Decimal, str, date | None]] | None = None,
    previous_closes: Mapping[str, Decimal] | None = None,
) -> Valuation:
    """Value the tracked positions. Missing inputs stay visibly missing."""
    lookup = _rate_lookup(fx, base)
    reported = reported or {}
    closes = previous_closes or {}
    holdings: list[HoldingValue] = []
    used_fx: dict[tuple[str, str], date | None] = {}
    notes: list[str] = []

    for position in positions:
        quote = quotes.get(position.instrument_id)
        reported_entry = reported.get((position.account, position.instrument_id))
        _, divisor = normalise_currency(position.currency)
        entry = HoldingValue(
            account=position.account,
            instrument_id=position.instrument_id,
            quantity=position.quantity,
            currency=position.currency,
            instrument_name=position.name,
            cost_basis_total=position.cost_basis_total,
            cost_basis_currency=position.cost_basis_currency,
        )
        if reported_entry is not None:
            value, currency, as_of = reported_entry
            entry = _with_reported(entry, value, currency, as_of)

        if quote is None:
            holdings.append(entry)
            continue

        quote_currency_note = ""
        if quote.currency.upper() != position.currency.upper() and (
            normalise_currency(quote.currency)[0] != normalise_currency(position.currency)[0]
        ):
            # A statement that lists in one currency and a quote in another is a mapping
            # problem, not a number to guess. Keep the holding unpriced and say why.
            entry = _with_price_note(entry, "quote currency differs from the position currency")
            holdings.append(entry)
            notes.append(
                f"{position.instrument_id}: quote is in {quote.currency} but the position is in "
                f"{position.currency}; confirm the listing"
            )
            continue
        _ = quote_currency_note

        native = native_value(position.quantity, quote.price, quote.price_unit_multiplier)
        entry = _with_price(
            entry,
            price=quote.price,
            basis=f"{quote.quote_type} quote",
            as_of=quote.as_of,
            native=native,
            multiplier=quote.price_unit_multiplier,
        )
        if quote.currency in MINOR_UNITS:
            entry = _with_price_note(
                entry, f"{quote.currency} is quoted in minor units (÷{divisor:.0f})"
            )
        previous = closes.get(position.instrument_id)
        if previous is not None and quote.as_of is not None:
            change = (quote.price - previous) * position.quantity * quote.price_unit_multiplier
            entry = _with_change(entry, change)

        rate, fx_basis, fx_as_of = lookup(position.currency)
        if rate is None:
            holdings.append(entry)
            used_fx[(position.currency, base)] = None
            continue
        value_eur = native / divisor * rate
        entry = _with_fx(entry, value_eur, fx_basis, fx_as_of)
        if fx_as_of is not None:
            used_fx[(normalise_currency(position.currency)[0], base)] = fx_as_of
        holdings.append(entry)

    priced = [h for h in holdings if h.value_eur is not None]
    subtotal = sum((h.value_eur for h in priced if h.value_eur is not None), Decimal(0))
    shares: dict[str, Decimal] = {}
    if subtotal > 0:
        for holding in priced:
            assert holding.value_eur is not None
            shares[holding.instrument_id] = holding.value_eur / subtotal * 100
    return Valuation(
        base_currency=base,
        holdings=tuple(holdings),
        priced_subtotal=subtotal if priced else None,
        priced_count=len(priced),
        unpriced_count=len(holdings) - len(priced),
        fx_dates=tuple(
            (base_c, quote_c, when) for (base_c, quote_c), when in sorted(used_fx.items())
        ),
        notes=tuple(notes),
        shares=shares,
    )


def _with_price_note(entry: HoldingValue, note: str) -> HoldingValue:
    from dataclasses import replace

    return replace(entry, price_basis=note)


def _with_price(
    entry: HoldingValue,
    *,
    price: Decimal,
    basis: str,
    as_of: date | None,
    native: Decimal,
    multiplier: Decimal,
) -> HoldingValue:
    from dataclasses import replace

    scaled = price if multiplier == 1 else price * multiplier
    return replace(
        entry,
        price=scaled,
        price_basis=basis,
        price_as_of=as_of,
        native_value=native,
    )


def _with_fx(
    entry: HoldingValue, value_eur: Decimal, basis: str, as_of: date | None
) -> HoldingValue:
    from dataclasses import replace

    return replace(entry, value_eur=value_eur, fx_basis=basis, fx_as_of=as_of)


def _with_reported(
    entry: HoldingValue, value: Decimal, currency: str, as_of: date | None
) -> HoldingValue:
    from dataclasses import replace

    return replace(
        entry,
        reported_market_value=value,
        reported_currency=currency,
        price_as_of=as_of,
    )


def _with_change(entry: HoldingValue, change: Decimal) -> HoldingValue:
    from dataclasses import replace

    return replace(entry, daily_change_native=change)


def daily_change(
    previous_close: Decimal | None, price: Decimal | None, quantity: Decimal
) -> Decimal | None:
    """Native-price change for the tracked quantity, or ``None`` when unverifiable."""
    if previous_close is None or price is None:
        return None
    return (price - previous_close) * quantity
