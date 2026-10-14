"""Pure presentation for the portfolio.

Presenters here receive validated snapshots and derive views. Money arithmetic lives in
:mod:`valuation`, not in a generic string formatter.

Honesty rules enforced:

* "tracked value" is the sum of the *tracked* positions, and it is labelled incomplete
  when any holding has no quote or no FX rate
* a missing quote or FX rate is never rendered as zero
* every row states its price basis (manual, statement, market) and the FX direction
* manual/statement values win over a market quote, and a conflict is reported for
  explicit reconciliation rather than silently overwritten
* a change in value is never called an investment return
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from ...core.contracts import (
    Item,
    Metric,
    PresentInput,
    Privacy,
    Severity,
    StatusView,
    TableColumn,
    TableRow,
    TableView,
    ViewModel,
)
from ...core.present import status_from_envelope
from .config import (
    PortfolioEventsSettings,
    PortfolioSummarySettings,
    PortfolioTableSettings,
    VercomSettings,
)
from .models import FxPayload, HoldingsPayload, PriceRecord, PricesPayload
from .valuation import (
    FxRate,
    Position,
    Quote,
    Valuation,
    value_portfolio,
)

__all__ = [
    "present_events",
    "present_fx",
    "present_summary",
    "present_table",
    "present_vercom",
    "valuation_from_inputs",
]

PRICE_PRECEDENCE = {"manual": 0, "statement": 1, "import": 2, "market": 3}


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation, ValueError:
        return None


def _positions(payload: HoldingsPayload | None) -> tuple[Position, ...]:
    if payload is None:
        return ()
    out: list[Position] = []
    for record in payload.positions:
        quantity = _decimal(record.quantity)
        if quantity is None:
            continue
        out.append(
            Position(
                account=record.account,
                instrument_id=record.instrument_id,
                quantity=quantity,
                currency=record.currency,
                isin=record.isin,
                venue=record.venue,
                cost_basis_total=_decimal(record.cost_basis_total),
                cost_basis_currency=record.cost_basis_currency,
                as_of=record.as_of,
                source=record.source,
                name=record.name,
            )
        )
    return tuple(out)


def _quote_from_prices(record: PriceRecord) -> Quote | None:
    price = _decimal(record.price)
    if price is None:
        return None
    return Quote(
        instrument_id=record.instrument_id,
        currency=record.currency,
        price=price,
        price_unit_multiplier=_decimal(record.price_unit_multiplier) or Decimal(1),
        as_of=record.as_of,
        fetched_at=record.fetched_at,
        source=record.source,
        quote_type=record.quote_type,
    )


def _market_quotes(envelope_payload: object) -> dict[str, Quote]:
    quotes: dict[str, Quote] = {}
    records = getattr(envelope_payload, "quotes", ())
    for record in records:
        price = _decimal(getattr(record, "price", None))
        if price is None:
            continue
        quotes[str(record.instrument_id)] = Quote(
            instrument_id=str(record.instrument_id),
            currency=str(record.currency),
            price=price,
            price_unit_multiplier=_decimal(getattr(record, "price_unit_multiplier", 1))
            or Decimal(1),
            as_of=getattr(record, "as_of", None),
            fetched_at=getattr(record, "fetched_at", None),
            source=str(getattr(record, "source", "market")),
            quote_type="market",
            venue_note=getattr(record, "venue_note", None),
        )
    return quotes


def _fx_rates(payload: FxPayload | None) -> dict[tuple[str, str], FxRate]:
    if payload is None:
        return {}
    out: dict[tuple[str, str], FxRate] = {}
    for record in payload.rates:
        rate = _decimal(record.rate)
        if rate is None or rate <= 0:
            continue
        out[(record.base, record.quote)] = FxRate(
            base=record.base,
            quote=record.quote,
            rate=rate,
            as_of=record.as_of,
            source=record.source,
        )
    return out


def valuation_from_inputs(
    inputs: PresentInput, *, base: str = "EUR", prefer_manual: bool = True
) -> tuple[Valuation, tuple[str, ...], tuple[str, ...]]:
    """Build a valuation from bound snapshots.

    Returns ``(valuation, conflicts, notes)``. ``conflicts`` lists holdings where a
    manual/statement price and a market quote disagree, so the UI can ask for an
    explicit reconciliation instead of silently overwriting the manual value.
    """
    holdings_payload = inputs.payload("positions")
    prices_payload = inputs.payload("prices")
    fx_payload = inputs.payload("fx")
    markets_payload = inputs.payload("markets")

    positions = _positions(
        holdings_payload if isinstance(holdings_payload, HoldingsPayload) else None
    )
    quotes: dict[str, Quote] = {}
    conflicts: list[str] = []
    notes: list[str] = []

    stored: dict[str, Quote] = {}
    if isinstance(prices_payload, PricesPayload):
        for record in prices_payload.quotes:
            quote = _quote_from_prices(record)
            if quote is not None:
                stored[quote.instrument_id] = quote
    market = _market_quotes(markets_payload)

    for instrument_id, quote in stored.items():
        competitor = market.get(instrument_id)
        if competitor is not None and competitor.price != quote.price:
            conflicts.append(
                f"{instrument_id}: stored {quote.quote_type} {quote.price} vs "
                f"market {competitor.price} ({competitor.source})"
            )
    for instrument_id, quote in market.items():
        stored_quote = stored.get(instrument_id)
        if stored_quote is None:
            quotes[instrument_id] = quote
            continue
        keep_stored = (
            stored_quote.quote_type in {"manual", "statement"} and prefer_manual
        ) or stored_quote.quote_type == "manual"
        quotes[instrument_id] = stored_quote if keep_stored else quote

    closes: dict[str, Decimal] = {}
    if isinstance(prices_payload, PricesPayload):
        for key, value in prices_payload.previous_closes.items():
            parsed = _decimal(value)
            if parsed is not None:
                closes[key] = parsed

    if isinstance(holdings_payload, HoldingsPayload) and holdings_payload.unpriced_instruments:
        # `unpriced_instruments` lists what has no *stored* (manual/statement) quote. An
        # instrument priced by a market quote is not unpriced, so only report the ones
        # with no price from any source.
        covered = set(stored) | set(market)
        without_any_price = [i for i in holdings_payload.unpriced_instruments if i not in covered]
        if without_any_price:
            notes.append(
                "no price at all (manual, statement or market) for: " + ", ".join(without_any_price)
            )
    valuation = value_portfolio(
        positions,
        quotes,
        _fx_rates(fx_payload if isinstance(fx_payload, FxPayload) else None),
        base=base,
        previous_closes=closes,
    )
    notes.extend(valuation.notes)
    return valuation, tuple(conflicts), tuple(notes)


def _money(value: Decimal | None, currency: str, *, digits: int = 2) -> str:
    if value is None:
        return "—"
    quantised = value.quantize(Decimal(10) ** -digits)
    return f"{quantised:,f} {currency}"


def _pct(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{value.quantize(Decimal('0.1'))}%"


def present_summary(inputs: PresentInput) -> ViewModel:
    settings = inputs.settings
    assert isinstance(settings, PortfolioSummarySettings)
    status = _status(inputs)
    holdings_envelope = inputs.envelope("positions")
    fx_envelope = inputs.envelope("fx")
    if holdings_envelope is None or holdings_envelope.payload is None:
        return ViewModel(title="Portfolio", status=status or _unconfigured())
    valuation, conflicts, notes = valuation_from_inputs(
        inputs, prefer_manual=settings.prefer_manual_quotes
    )
    if not valuation.holdings:
        return ViewModel(
            title="Portfolio",
            status=StatusView(
                severity=Severity.INFO,
                headline="No positions recorded",
                detail="Add a position manually or import a broker export.",
                hints=("sideboard import holdings positions.csv",),
            ),
        )

    priced = [holding for holding in valuation.holdings if holding.priced]
    metrics: list[Metric] = [
        Metric(
            id="tracked_value",
            label="tracked value",
            value_text=_money(valuation.priced_subtotal, valuation.base_currency),
            basis=("complete" if valuation.complete else valuation.total_label),
            privacy=Privacy.PRIVATE,
            note=(
                None
                if valuation.complete
                else f"{valuation.unpriced_count} holding(s) excluded from the subtotal"
            ),
            severity=Severity.OK if valuation.complete else Severity.WARNING,
        ),
        Metric(
            id="priced",
            label="priced",
            value_text=f"{valuation.priced_count}/{len(valuation.holdings)}",
            basis="holdings with a quote and an FX rate",
        ),
    ]
    if settings.show_weights and priced:
        top = max(priced, key=lambda h: h.value_eur or Decimal(0))
        metrics.append(
            Metric(
                id="largest",
                label="largest",
                value_text=top.instrument_id,
                basis=_pct(valuation.shares.get(top.instrument_id)),
                privacy=Privacy.PRIVATE,
            )
        )
    if settings.show_concentration and settings.concentration_instrument:
        weight = valuation.shares.get(settings.concentration_instrument)
        metrics.append(
            Metric(
                id="concentration",
                label=f"{settings.concentration_instrument} share",
                value_text=_pct(weight),
                basis="share of tracked value",
                privacy=Privacy.PRIVATE,
                available=weight is not None,
                note=None if weight is not None else "not priced",
                severity=(
                    Severity.WARNING
                    if weight is not None and weight >= Decimal(50)
                    else Severity.OK
                ),
            )
        )

    rows: list[TableRow] = []
    for holding in sorted(valuation.holdings, key=lambda h: -(h.value_eur or Decimal(-1)))[
        : settings.max_holdings
    ]:
        change = (
            _money(holding.daily_change_native, holding.currency) if settings.show_change else "—"
        )
        rows.append(
            TableRow(
                id=f"holding:{holding.account}:{holding.instrument_id}",
                cells={
                    "instrument": holding.instrument_name or holding.instrument_id,
                    "quantity": f"{holding.quantity:,f}",
                    "price": _money(holding.price, holding.currency),
                    "value": _money(holding.native_value, holding.currency),
                    "eur": _money(holding.value_eur, valuation.base_currency),
                    "weight": _pct(valuation.shares.get(holding.instrument_id)),
                    "change": change,
                    "basis": holding.price_basis,
                },
                privacy=Privacy.PRIVATE,
                severity=Severity.UNKNOWN if holding.priced else Severity.WARNING,
            )
        )

    footnote_parts = [
        "price change is not investment return; deposits and withdrawals also change value"
    ]
    shared = shared_instrument_footnote(valuation)
    if shared:
        footnote_parts.append(shared)
    if conflicts:
        footnote_parts.append("price conflicts: " + "; ".join(conflicts[:2]))
    if notes:
        footnote_parts.append(" · ".join(notes[:2]))
    fx_payload = fx_envelope.payload if fx_envelope is not None else None
    if isinstance(fx_payload, FxPayload) and valuation.fx_dates:
        footnote_parts.append(
            "FX basis: "
            + ", ".join(
                f"{base}/{quote} {when.isoformat() if when else 'undated'}"
                for base, quote, when in valuation.fx_dates
            )
        )
    return ViewModel(
        title="Portfolio",
        metrics=tuple(metrics),
        table=TableView(
            columns=(
                TableColumn(id="instrument", label="holding", privacy=Privacy.PRIVATE),
                TableColumn(id="quantity", label="qty", align="right", privacy=Privacy.PRIVATE),
                TableColumn(id="price", label="price", align="right", privacy=Privacy.PRIVATE),
                TableColumn(id="value", label="native", align="right", privacy=Privacy.PRIVATE),
                TableColumn(
                    id="eur", label=valuation.base_currency, align="right", privacy=Privacy.PRIVATE
                ),
                TableColumn(id="weight", label="share", align="right", privacy=Privacy.PRIVATE),
                TableColumn(id="change", label="chg", align="right", privacy=Privacy.PRIVATE),
                TableColumn(id="basis", label="basis", privacy=Privacy.PRIVATE),
            ),
            rows=tuple(rows),
            footnote="share is a share of the tracked total, not of the whole portfolio",
        ),
        status=status,
        footnote=" · ".join(footnote_parts),
        privacy=Privacy.PRIVATE,
    )


def present_table(inputs: PresentInput) -> ViewModel:
    settings = inputs.settings
    assert isinstance(settings, PortfolioTableSettings)
    valuation, conflicts, notes = valuation_from_inputs(inputs)
    holdings_payload = inputs.payload("positions")
    if not isinstance(holdings_payload, HoldingsPayload) or not valuation.holdings:
        return ViewModel(title="Holdings", status=_unconfigured())
    rows: list[TableRow] = []
    for holding in valuation.holdings[: settings.limit]:
        cells = {
            "account": holding.account,
            "instrument": holding.instrument_id,
            "isin": next(
                (
                    record.isin or ""
                    for record in holdings_payload.positions
                    if record.instrument_id == holding.instrument_id
                ),
                "",
            ),
            "quantity": f"{holding.quantity:,f}",
            "value": _money(holding.value_eur, valuation.base_currency),
            "reported": _money(
                holding.reported_market_value,
                holding.reported_currency or holding.currency,
            )
            if settings.show_reported_values
            else "—",
            "basis": holding.price_basis,
            "fx": holding.fx_basis,
        }
        if settings.show_cost_basis:
            cells["cost"] = _money(holding.cost_basis_total, holding.cost_basis_currency or "")
        rows.append(
            TableRow(
                id=f"holding:{holding.account}:{holding.instrument_id}",
                cells=cells,
                privacy=Privacy.PRIVATE,
                severity=Severity.UNKNOWN if holding.priced else Severity.WARNING,
            )
        )
    columns = [
        TableColumn(id="account", label="account", privacy=Privacy.PRIVATE),
        TableColumn(id="instrument", label="instrument", privacy=Privacy.PRIVATE),
        TableColumn(id="isin", label="isin", privacy=Privacy.PRIVATE),
        TableColumn(id="quantity", label="qty", align="right", privacy=Privacy.PRIVATE),
    ]
    if settings.show_cost_basis:
        columns.append(
            TableColumn(id="cost", label="cost basis", align="right", privacy=Privacy.PRIVATE)
        )
    columns.extend(
        [
            TableColumn(
                id="value", label=valuation.base_currency, align="right", privacy=Privacy.PRIVATE
            ),
            TableColumn(id="reported", label="reported", align="right", privacy=Privacy.PRIVATE),
            TableColumn(id="basis", label="price basis", privacy=Privacy.PRIVATE),
            TableColumn(id="fx", label="fx basis", privacy=Privacy.PRIVATE),
        ]
    )
    footnote = ["reported market values come from a statement, not a derived unit price"]
    if notes:
        footnote.append(" · ".join(notes[:2]))
    if conflicts:
        footnote.append("conflicts: " + "; ".join(conflicts[:2]))
    return ViewModel(
        title="Holdings",
        table=TableView(columns=tuple(columns), rows=tuple(rows), footnote="; ".join(footnote)),
        footnote=None if settings.show_basis else "basis columns hidden by settings",
        privacy=Privacy.PRIVATE,
    )


def present_events(inputs: PresentInput) -> ViewModel:
    settings = inputs.settings
    assert isinstance(settings, PortfolioEventsSettings)
    payload = inputs.payload("positions")
    if not isinstance(payload, HoldingsPayload):
        return ViewModel(title="Events", status=_unconfigured())
    items: list[Item] = []
    for dividend in payload.dividends:
        items.append(
            Item(
                id=f"dividend:{dividend.dividend_id}",
                title=f"{dividend.kind} dividend · {dividend.instrument_id}",
                subtitle=(
                    f"{dividend.gross or '—'} {dividend.currency or ''}".strip()
                    + (f" · ex {dividend.ex_date.isoformat()}" if dividend.ex_date else "")
                    + (f" · paid {dividend.pay_date.isoformat()}" if dividend.pay_date else "")
                ),
                category="dividend",
                published_at=None,
                meta=(("kind", dividend.kind),),
                privacy=Privacy.PRIVATE,
                source_id=dividend.instrument_id,
                badge=dividend.kind,
            )
        )
    items.sort(
        key=lambda item: (
            item.published_at is None,
            -(item.published_at.timestamp() if item.published_at else 0.0),
        )
    )
    if settings.show_links:
        for link in payload.links:
            items.append(
                Item(
                    id=f"link:{link.link_id}",
                    title=link.label,
                    subtitle=link.url,
                    url=link.url,
                    category="link",
                    privacy=Privacy.PRIVATE,
                    badge=link.kind,
                )
            )
    if not items:
        return ViewModel(
            title="Events",
            status=StatusView(
                severity=Severity.INFO,
                headline="No dividend or calendar events recorded",
                detail="Add an official report link or a declared/received dividend.",
                hints=("declared and received dividends are kept separate",),
            ),
        )
    return ViewModel(
        title="Events",
        items=tuple(items[: settings.limit]),
        footnote="declared, received and estimated amounts are distinct; no forecast is invented",
        privacy=Privacy.PRIVATE,
    )


def present_vercom(inputs: PresentInput) -> ViewModel:
    settings = inputs.settings
    assert isinstance(settings, VercomSettings)
    valuation, conflicts, notes = valuation_from_inputs(inputs)
    target = settings.instrument_id
    metrics: list[Metric] = []
    items: list[Item] = []
    if target:
        holding = next((h for h in valuation.holdings if h.instrument_id == target), None)
        if holding is None:
            metrics.append(
                Metric.unavailable(
                    "holding", "position", f"{target} is not in the tracked positions"
                )
            )
        else:
            metrics.extend(
                [
                    Metric(
                        id="quantity",
                        label="quantity",
                        value_text=f"{holding.quantity:,f}",
                        basis=f"{holding.currency} native",
                        privacy=Privacy.PRIVATE,
                    ),
                    Metric(
                        id="price",
                        label="price",
                        value_text=_money(holding.price, holding.currency),
                        basis=holding.price_basis,
                        privacy=Privacy.PRIVATE,
                    ),
                    Metric(
                        id="value",
                        label="value",
                        value_text=_money(holding.value_eur, valuation.base_currency),
                        basis=holding.fx_basis,
                        privacy=Privacy.PRIVATE,
                        available=holding.value_eur is not None,
                    ),
                ]
            )
        if settings.show_weight:
            weight = valuation.shares.get(target)
            metrics.append(
                Metric(
                    id="weight",
                    label="share of tracked",
                    value_text=_pct(weight),
                    basis="concentration, not a recommendation",
                    privacy=Privacy.PRIVATE,
                    available=weight is not None,
                    severity=Severity.WARNING
                    if weight is not None and weight >= Decimal(50)
                    else Severity.OK,
                )
            )
    payload = inputs.payload("positions")
    payload_links = payload.links if isinstance(payload, HoldingsPayload) else ()
    dividends = (
        payload.dividends
        if settings.show_dividends and isinstance(payload, HoldingsPayload)
        else ()
    )
    if True:
        for dividend in dividends:
            if target and dividend.instrument_id != target:
                continue
            items.append(
                Item(
                    id=f"dividend:{dividend.dividend_id}",
                    title=f"{dividend.kind} {dividend.gross or '—'} {dividend.currency or ''}".strip(),
                    subtitle=" · ".join(
                        part
                        for part in (
                            f"ex {dividend.ex_date.isoformat()}" if dividend.ex_date else "",
                            f"record {dividend.record_date.isoformat()}"
                            if dividend.record_date
                            else "",
                            f"pay {dividend.pay_date.isoformat()}" if dividend.pay_date else "",
                        )
                        if part
                    )
                    or None,
                    category="dividend",
                    privacy=Privacy.PRIVATE,
                    badge=dividend.kind,
                    source_id=dividend.instrument_id,
                )
            )
        if settings.show_links:
            for link in payload_links:
                if link.instrument_id and target and link.instrument_id != target:
                    continue
                items.append(
                    Item(
                        id=f"link:{link.link_id}",
                        title=link.label,
                        subtitle=link.url,
                        url=link.url,
                        category="link",
                        privacy=Privacy.PRIVATE,
                        badge=link.kind,
                    )
                )
    if not metrics and not items:
        return ViewModel(
            title=settings.label,
            status=StatusView(
                severity=Severity.INFO,
                headline="No instrument mapped",
                detail=(
                    "Set instrument_id in this widget's settings to the identifier you use for "
                    "the holding, then add the company's official report links."
                ),
            ),
        )
    footnote = ["official investor-relations links only; no forecast is invented"]
    if notes:
        footnote.append(" · ".join(notes[:2]))
    if conflicts:
        footnote.append("conflicts: " + "; ".join(conflicts[:2]))
    return ViewModel(
        title=settings.label,
        metrics=tuple(metrics),
        items=tuple(items[:12]),
        footnote=" · ".join(footnote),
        privacy=Privacy.PRIVATE,
    )


def present_fx(inputs: PresentInput) -> ViewModel:
    payload = inputs.payload("primary")
    status = status_from_envelope(
        inputs.envelope("primary"), offline=inputs.context.offline, demo=inputs.context.demo
    )
    if isinstance(payload, FxPayload) and payload.rates:
        rows = tuple(
            TableRow(
                id=f"fx:{record.base}{record.quote}{record.as_of}",
                cells={
                    "pair": f"{record.base}/{record.quote}",
                    "rate": record.rate,
                    "as_of": record.as_of.isoformat() if isinstance(record.as_of, date) else "—",
                    "source": record.source,
                },
            )
            for record in payload.rates
        )
        directions = {(record.base, record.quote) for record in payload.rates}
        missing = [
            f"{quote}/{base}"
            for base, quote in sorted(directions)
            if (quote, base) not in directions
        ]
        return ViewModel(
            title="FX",
            table=TableView(
                columns=(
                    TableColumn(id="pair", label="direction"),
                    TableColumn(id="rate", label="rate", align="right"),
                    TableColumn(id="as_of", label="as of"),
                    TableColumn(id="source", label="source"),
                ),
                rows=rows,
                footnote="rates are directed: PLN/EUR ≠ EUR/PLN",
            ),
            footnote=(
                "reverse direction not stored for: " + ", ".join(missing) if missing else None
            ),
            status=status,
        )
    return ViewModel(
        title="FX",
        status=status
        or StatusView(
            severity=Severity.INFO,
            headline="No FX rates stored",
            detail="Add a directed rate manually or import a rates CSV.",
            hints=("sideboard import fx rates.csv",),
        ),
    )


def _unconfigured() -> StatusView:
    return StatusView(
        severity=Severity.INFO,
        headline="Portfolio not configured",
        detail="Add a position manually or import a broker export to see a valuation.",
        hints=("sideboard import holdings positions.csv",),
    )


def _status(inputs: PresentInput) -> StatusView | None:
    """Surface the least flattering bound input, so a partial portfolio never looks whole."""
    statuses = [
        status_from_envelope(envelope, offline=inputs.context.offline, demo=inputs.context.demo)
        for envelope in inputs.inputs.values()
    ]
    present = [status for status in statuses if status is not None]
    if not present:
        return None
    return max(
        present,
        key=lambda status: {
            Severity.OK: 0,
            Severity.INFO: 1,
            Severity.UNKNOWN: 2,
            Severity.WARNING: 3,
            Severity.ERROR: 4,
        }.get(status.severity, 2),
    )


def shared_instrument_footnote(valuation: Valuation) -> str | None:
    """Flag one instrument held as several positions.

    A fund bought on two exchanges is two positions with one ISIN. Each row's "share"
    then looks half as large as the real exposure, which would misstate concentration.
    The rows stay separate — different accounts and venues — and the combined figure is
    stated instead of being implied.
    """
    by_isin: dict[str, list[str]] = {}
    for holding in valuation.holdings:
        if holding.isin:
            by_isin.setdefault(holding.isin, []).append(holding.instrument_id)
    repeated = {isin: ids for isin, ids in by_isin.items() if len(ids) > 1}
    if not repeated:
        return None
    parts: list[str] = []
    for isin, ids in sorted(repeated.items()):
        # sum() would start from int 0 and widen the result type; the start value keeps
        # the arithmetic in Decimal all the way through.
        combined = sum(
            ((valuation.shares.get(instrument_id) or Decimal("0")) for instrument_id in ids),
            Decimal("0"),
        )
        parts.append(
            f"{isin} held as {len(ids)} positions ({', '.join(sorted(ids))}): "
            f"combined share {_pct(combined)}"
        )
    return " · ".join(parts)
