"""Import-driven portfolio sources.

``portfolio.holdings`` and ``portfolio.prices`` read the local domain store. They never
poll a broker and never perform network I/O: an import or a manual edit is the update
event, which the host signals with ``mark_import_updated``.

``portfolio.prices`` supports an optional ``reference`` mode that refreshes the shared
``fx.rates`` store; quotes stay manual/imported unless the optional ``markets`` extra is
installed and explicitly enabled.
"""

from __future__ import annotations

from datetime import timedelta

from ...core.contracts import (
    Cadence,
    Capability,
    Coverage,
    Precision,
    ProviderBuild,
    SourcePayload,
    SourceSpec,
)
from ...core.errors import CapabilityError
from . import repo
from .config import HoldingsSettings, PricesSettings
from .models import (
    AccountRecord,
    DividendRecord,
    HoldingsPayload,
    LinkRecord,
    PositionRecord,
    PriceRecord,
    PricesPayload,
)

__all__ = ["HoldingsProvider", "PricesProvider", "demo_holdings", "demo_prices", "make_specs"]


class HoldingsProvider:
    """Reads positions, accounts, dividend events and investor links from the store."""

    def __init__(self, *, settings: HoldingsSettings) -> None:
        self.settings = settings

    async def fetch(self, context: ProviderBuild) -> HoldingsPayload:
        database = context.database
        if database is None:
            raise CapabilityError("the holdings source requires read-only repository access")
        settings = context.settings
        assert isinstance(settings, HoldingsSettings)
        positions = await repo.load_positions(database)
        accounts = await repo.load_accounts(database)
        dividends = await repo.load_dividends(database)
        links = await repo.load_links(database)
        imports = await repo.load_imports(database, limit=5)
        unpriced = await repo.next_missing_instruments(database)
        newest_import = max((item.imported_at for item in imports), default=None)
        now = context.clock.now()
        as_of = max(
            (position.as_of for position in positions if position.as_of is not None),
            default=None,
        )
        return HoldingsPayload(
            positions=tuple(
                PositionRecord(
                    account=position.account,
                    instrument_id=position.instrument_id,
                    quantity=str(position.quantity),
                    currency=position.currency,
                    isin=position.isin,
                    venue=position.venue,
                    name=position.name,
                    cost_basis_total=(
                        None
                        if position.cost_basis_total is None
                        else str(position.cost_basis_total)
                    ),
                    cost_basis_currency=position.cost_basis_currency,
                    as_of=position.as_of,
                    source=position.source,
                )
                for position in positions
            ),
            accounts=tuple(
                AccountRecord(
                    account=account.account,
                    as_of=account.as_of,
                    import_id=account.import_id,
                    updated_at=account.updated_at,
                    note=account.note,
                    position_count=account.position_count,
                )
                for account in accounts
            ),
            dividends=tuple(
                DividendRecord(
                    dividend_id=str(row["dividend_id"]),
                    instrument_id=str(row["instrument_id"]),
                    kind=str(row["kind"]),
                    gross=row["gross"],
                    currency=row["currency"],
                    ex_date=repo._date(row["ex_date"]),
                    record_date=repo._date(row["record_date"]),
                    pay_date=repo._date(row["pay_date"]),
                    source=row["source"],
                    notes=row["notes"],
                )
                for row in dividends
            ),
            links=tuple(
                LinkRecord(
                    link_id=str(row["link_id"]),
                    label=str(row["label"]),
                    url=str(row["url"]),
                    kind=str(row["kind"]),
                    instrument_id=row["instrument_id"],
                )
                for row in links
            ),
            unpriced_instruments=unpriced,
            last_import_at=newest_import,
            as_of=None,
            precision=Precision.DATE if as_of else Precision.UNKNOWN,
            valid_until=now + timedelta(days=7),
            provenance="local holdings store",
            coverage=Coverage(
                expected=None,
                observed=len(positions),
                missing=unpriced,
                notes=(
                    "import-driven: updated by a manual edit or a statement import"
                    if positions
                    else "no holdings recorded yet; add one manually or import an export"
                ),
            ),
        )

    async def aclose(self) -> None:
        return None


class PricesProvider:
    """Reads stored quotes and previous closes. Never invents a price."""

    def __init__(self, *, settings: PricesSettings) -> None:
        self.settings = settings

    async def fetch(self, context: ProviderBuild) -> PricesPayload:
        database = context.database
        if database is None:
            raise CapabilityError("the prices source requires read-only repository access")
        now = context.clock.now()
        quotes = await repo.load_quotes(database)
        closes = await repo.load_previous_closes(database)
        missing = await repo.next_missing_instruments(database)
        return PricesPayload(
            quotes=tuple(
                PriceRecord(
                    instrument_id=quote.instrument_id,
                    currency=quote.currency,
                    price=str(quote.price),
                    price_unit_multiplier=str(quote.price_unit_multiplier),
                    as_of=quote.as_of,
                    fetched_at=quote.fetched_at,
                    source=quote.source,
                    quote_type=quote.quote_type,
                )
                for quote in quotes.values()
            ),
            previous_closes={key: str(value) for key, value in closes.items()},
            as_of=None,
            precision=Precision.DATE if quotes else Precision.UNKNOWN,
            valid_until=now + timedelta(days=7),
            provenance="local quote store",
            coverage=Coverage(
                observed=len(quotes),
                missing=missing,
                notes=(
                    "manual and statement quotes only; an instrument without one may still "
                    "be priced by the optional market source, and a missing price is not zero"
                    if quotes
                    else "no manual or statement quote stored yet; the optional market "
                    "source may cover some instruments"
                ),
            ),
        )

    async def aclose(self) -> None:
        return None


def make_specs() -> tuple[SourceSpec, SourceSpec]:
    holdings = SourceSpec(
        type_id="portfolio.holdings",
        payload_model=HoldingsPayload,
        settings_model=HoldingsSettings,
        payload_schema="portfolio.holdings",
        payload_schema_version=1,
        factory=lambda build: HoldingsProvider(settings=_holdings_settings(build)),
        capabilities=frozenset({Capability.REPOSITORY}),
        cadence=Cadence(import_driven=True, active_seconds=3600, background_seconds=3600),
        retention_seconds=365 * 24 * 3600,
        description="Tracked positions, accounts, dividend events and investor links.",
        setup_hint="Add a position manually or import a broker export (sideboard import holdings).",
        demo_builder=demo_holdings,
    )
    prices = SourceSpec(
        type_id="portfolio.prices",
        payload_model=PricesPayload,
        settings_model=PricesSettings,
        payload_schema="portfolio.prices",
        payload_schema_version=1,
        factory=lambda build: PricesProvider(settings=_prices_settings(build)),
        capabilities=frozenset({Capability.REPOSITORY}),
        cadence=Cadence(import_driven=True, active_seconds=3600, background_seconds=3600),
        retention_seconds=365 * 24 * 3600,
        description="Stored quotes with their dates, types and sources. Manual-first.",
        setup_hint="Enter a price manually or import a price export (sideboard import prices).",
        demo_builder=demo_prices,
    )
    return holdings, prices


def _holdings_settings(build: ProviderBuild) -> HoldingsSettings:
    settings = build.settings
    assert isinstance(settings, HoldingsSettings)
    return settings


def _prices_settings(build: ProviderBuild) -> PricesSettings:
    settings = build.settings
    assert isinstance(settings, PricesSettings)
    return settings


def demo_holdings() -> SourcePayload:
    """Synthetic positions. Deliberately not a real instrument and not a real holding."""
    from datetime import date

    return HoldingsPayload(
        positions=(
            PositionRecord(
                account="demo-broker",
                instrument_id="DEMO-EQUITY",
                quantity="10",
                currency="EUR",
                venue="DEMO",
                name="Demo Equity",
                as_of=date(2026, 10, 12),
                source="statement",
            ),
            PositionRecord(
                account="demo-broker",
                instrument_id="DEMO-ETF",
                quantity="42.5",
                currency="EUR",
                venue="DEMO",
                name="Demo All-World ETF",
                as_of=date(2026, 10, 12),
                source="manual",
            ),
            PositionRecord(
                account="demo-broker",
                instrument_id="DEMO-PLN",
                quantity="7000",
                currency="PLN",
                venue="WSE",
                name="Demo Polish Equity",
                as_of=date(2026, 10, 12),
                source="statement",
            ),
        ),
        accounts=(
            AccountRecord(
                account="demo-broker",
                as_of=date(2026, 10, 12),
                import_id="demo",
                note="synthetic demo account",
                position_count=3,
            ),
        ),
        dividends=(
            DividendRecord(
                dividend_id="demo-dividend-1",
                instrument_id="DEMO-PLN",
                kind="received",
                gross="1500.00",
                currency="PLN",
                ex_date=date(2026, 6, 12),
                pay_date=date(2026, 6, 30),
                source="demo",
                notes="declared and received are separate records",
            ),
        ),
        links=(
            LinkRecord(
                link_id="demo-link-1",
                label="Demo investor relations",
                url="https://example.invalid/investors",
                kind="calendar",
            ),
        ),
        unpriced_instruments=(),
        as_of=None,
        precision=Precision.DATE,
        provenance="demo",
        coverage=Coverage(expected=3, observed=3, notes="synthetic demo portfolio"),
    )


def demo_prices() -> SourcePayload:
    """Synthetic quotes with explicit dates and quote types."""
    from datetime import date

    return PricesPayload(
        quotes=(
            PriceRecord(
                instrument_id="DEMO-EQUITY",
                currency="EUR",
                price="12.34",
                as_of=date(2026, 10, 12),
                source="demo",
                quote_type="market",
            ),
            PriceRecord(
                instrument_id="DEMO-PLN",
                currency="PLN",
                price="48.90",
                as_of=date(2026, 10, 12),
                source="demo",
                quote_type="market",
            ),
        ),
        previous_closes={"DEMO-EQUITY": "12.10", "DEMO-PLN": "49.30"},
        precision=Precision.DATE,
        provenance="demo",
        coverage=Coverage(
            expected=3, observed=2, missing=("DEMO-ETF",), notes="one holding has no quote"
        ),
    )
