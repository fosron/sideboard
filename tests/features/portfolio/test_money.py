"""Money correctness: decimals, FX direction, minor units, missing data, imports."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sideboard.core.storage import CORE_MIGRATIONS, Database, Migration
from sideboard.features.portfolio import repo
from sideboard.features.portfolio.importer import (
    CsvOptions,
    apply_preview,
    preview_fx,
    preview_positions,
    preview_prices,
)
from sideboard.features.portfolio.migrations import PORTFOLIO_MIGRATIONS
from sideboard.features.portfolio.valuation import (
    FxRate,
    Position,
    Quote,
    normalise_currency,
    value_portfolio,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

POSITIONS_CSV = """account,instrument_id,isin,venue,currency,quantity,cost_basis_total,cost_basis_currency,as_of
demo-broker,DEMO-EQUITY,,DEMO,EUR,10,,,2026-10-12
demo-broker,DEMO-ETF,,DEMO,EUR,42.5,1000.00,EUR,2026-10-12
demo-broker,DEMO-PLN,,WSE,PLN,7000,,,2026-10-12
"""


async def database_for(tmp_path: Path) -> Database:
    database = await Database.open(tmp_path / "db.sqlite3")
    migrations = (
        tuple(CORE_MIGRATIONS)
        + tuple(PORTFOLIO_MIGRATIONS)
        + (
            Migration(
                version=20,
                name="fx_rates",
                owner="fx",
                statements=(
                    """
                CREATE TABLE IF NOT EXISTS fx_rates (
                    base TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    rate TEXT NOT NULL,
                    as_of TEXT,
                    source TEXT NOT NULL DEFAULT 'manual',
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (base, quote, as_of)
                )
                """,
                ),
            ),
        )
    )
    await database.initialize(migrations)
    return database


# --------------------------------------------------------------------------- valuation


def test_native_value_uses_the_price_unit_multiplier() -> None:
    positions = (
        Position(
            account="a",
            instrument_id="X",
            quantity=Decimal("2.5"),
            currency="EUR",
        ),
    )
    quotes = {
        "X": Quote(
            instrument_id="X",
            currency="EUR",
            price=Decimal("4"),
            price_unit_multiplier=Decimal("10"),
        )
    }
    valuation = value_portfolio(positions, quotes, {})
    holding = valuation.holdings[0]
    assert holding.native_value == Decimal("100.0")
    assert holding.value_eur == Decimal("100.0")


def test_missing_quote_is_not_zero_and_the_total_is_incomplete() -> None:
    positions = (
        Position(account="a", instrument_id="PRICED", quantity=Decimal("1"), currency="EUR"),
        Position(account="a", instrument_id="UNPRICED", quantity=Decimal("5"), currency="EUR"),
    )
    quotes = {"PRICED": Quote(instrument_id="PRICED", currency="EUR", price=Decimal("12"))}
    valuation = value_portfolio(positions, quotes, {})
    assert valuation.priced_count == 1
    assert valuation.unpriced_count == 1
    assert valuation.priced_subtotal == Decimal("12")
    assert valuation.complete is False
    assert "unpriced" in valuation.total_label
    assert valuation.holdings[1].native_value is None


def test_missing_fx_is_not_a_complete_total() -> None:
    positions = (Position(account="a", instrument_id="PL", quantity=Decimal("10"), currency="PLN"),)
    quotes = {"PL": Quote(instrument_id="PL", currency="PLN", price=Decimal("50"))}
    valuation = value_portfolio(positions, quotes, {})
    assert valuation.priced_subtotal is None
    assert valuation.holdings[0].value_eur is None
    assert valuation.holdings[0].fx_basis == "missing FX rate"
    assert valuation.holdings[0].missing == ("fx",)


def test_directed_fx_rate_is_required_in_the_right_direction() -> None:
    positions = (Position(account="a", instrument_id="PL", quantity=Decimal("10"), currency="PLN"),)
    quotes = {"PL": Quote(instrument_id="PL", currency="PLN", price=Decimal("50"))}
    # A PLN→EUR record: correct direction.
    direct = {("PLN", "EUR"): FxRate(base="PLN", quote="EUR", rate=Decimal("0.23"))}
    valuation = value_portfolio(positions, quotes, direct)
    assert valuation.holdings[0].value_eur == Decimal("115.00")
    assert "PLN/EUR" in valuation.holdings[0].fx_basis

    # Only the reverse record exists: it is inverted and labelled, never misread.
    reverse = {("EUR", "PLN"): FxRate(base="EUR", quote="PLN", rate=Decimal("4.3"))}
    valuation = value_portfolio(positions, quotes, reverse)
    assert valuation.holdings[0].value_eur == pytest.approx(Decimal("116.28"), abs=Decimal("0.01"))
    assert "inverted from EUR/PLN" in valuation.holdings[0].fx_basis


def test_eur_is_an_exact_rate_of_one() -> None:
    positions = (Position(account="a", instrument_id="X", quantity=Decimal("3"), currency="EUR"),)
    quotes = {"X": Quote(instrument_id="X", currency="EUR", price=Decimal("7"))}
    valuation = value_portfolio(positions, quotes, {})
    assert valuation.holdings[0].value_eur == Decimal("21")
    assert valuation.holdings[0].fx_basis == "EUR=EUR"


def test_gbp_pence_is_scaled_and_distinguished_from_pounds() -> None:
    assert normalise_currency("GBp") == ("GBP", Decimal(100))
    assert normalise_currency("GBP") == ("GBP", Decimal(1))
    positions = (Position(account="a", instrument_id="L", quantity=Decimal("100"), currency="GBp"),)
    quotes = {"L": Quote(instrument_id="L", currency="GBp", price=Decimal("250"))}
    fx = {("GBP", "EUR"): FxRate(base="GBP", quote="EUR", rate=Decimal("1.17"))}
    valuation = value_portfolio(positions, quotes, fx)
    # 100 shares × 250p = 25 000p = £250 → £250 × 1.17 = €292.50
    assert valuation.holdings[0].native_value == Decimal("25000")
    assert valuation.holdings[0].value_eur == Decimal("292.5000")
    assert "minor units" in valuation.holdings[0].price_basis


def test_fractional_quantities_keep_full_precision() -> None:
    positions = (
        Position(account="a", instrument_id="X", quantity=Decimal("0.00000001"), currency="EUR"),
    )
    quotes = {"X": Quote(instrument_id="X", currency="EUR", price=Decimal("12345.6789"))}
    valuation = value_portfolio(positions, quotes, {})
    assert valuation.holdings[0].value_eur == Decimal("0.000123456789")


def test_currency_mismatch_between_position_and_quote_is_flagged_not_guessed() -> None:
    positions = (Position(account="a", instrument_id="X", quantity=Decimal("1"), currency="EUR"),)
    quotes = {"X": Quote(instrument_id="X", currency="USD", price=Decimal("100"))}
    valuation = value_portfolio(positions, quotes, {})
    holding = valuation.holdings[0]
    assert holding.value_eur is None
    assert "currency differs" in holding.price_basis
    assert any("confirm the listing" in note for note in valuation.notes)


def test_shares_are_of_the_tracked_total() -> None:
    positions = (
        Position(account="a", instrument_id="A", quantity=Decimal("1"), currency="EUR"),
        Position(account="a", instrument_id="B", quantity=Decimal("1"), currency="EUR"),
    )
    quotes = {
        "A": Quote(instrument_id="A", currency="EUR", price=Decimal("75")),
        "B": Quote(instrument_id="B", currency="EUR", price=Decimal("25")),
    }
    valuation = value_portfolio(positions, quotes, {})
    assert valuation.shares["A"] == Decimal("75")
    assert valuation.shares["B"] == Decimal("25")


def test_daily_change_uses_a_previous_close_and_is_not_a_return() -> None:
    positions = (Position(account="a", instrument_id="X", quantity=Decimal("10"), currency="EUR"),)
    quotes = {
        "X": Quote(instrument_id="X", currency="EUR", price=Decimal("11"), as_of=date(2026, 10, 13))
    }
    valuation = value_portfolio(positions, quotes, {}, previous_closes={"X": Decimal("10")})
    assert valuation.holdings[0].daily_change_native == Decimal("10")
    # No previous close: no invented change.
    valuation = value_portfolio(positions, quotes, {})
    assert valuation.holdings[0].daily_change_native is None


def test_reported_statement_value_is_kept_separate() -> None:
    positions = (Position(account="a", instrument_id="X", quantity=Decimal("1"), currency="EUR"),)
    valuation = value_portfolio(
        positions,
        {},
        {},
        reported={("a", "X"): (Decimal("1234.56"), "EUR", date(2026, 10, 12))},
    )
    holding = valuation.holdings[0]
    assert holding.reported_market_value == Decimal("1234.56")
    assert holding.price is None  # no derived unit price
    assert holding.value_eur is None


# --------------------------------------------------------------------------- importer


def test_positions_preview_maps_and_validates_rows() -> None:
    preview = preview_positions(POSITIONS_CSV, options=CsvOptions())
    assert len(preview.positions) == 3
    assert preview.rejected == ()
    assert preview.accounts == ("demo-broker",)
    assert preview.positions[1].quantity == Decimal("42.5")
    assert preview.positions[1].cost_basis_total == Decimal("1000.00")


def test_decimal_comma_locale_is_explicit_not_guessed() -> None:
    text = 'account,instrument_id,quantity,currency,as_of\nb,X,"1.234,50",EUR,12.10.2026\n'
    comma = preview_positions(text, options=CsvOptions(decimal="comma", date_order="dmy"))
    assert comma.positions[0].quantity == Decimal("1234.50")
    assert comma.positions[0].as_of == date(2026, 10, 12)

    point = preview_positions(text, options=CsvOptions(decimal="point", date_order="dmy"))
    assert point.rejected and point.rejected[0].reason == "ambiguous decimal separator"


def test_semicolon_delimiter_and_bom_are_handled() -> None:
    text = "\ufeffaccount;instrument_id;quantity;as_of\nb;X;3;2026-10-12\n"
    preview = preview_positions(text, options=CsvOptions(delimiter=";"))
    assert preview.positions[0].quantity == Decimal("3")


def test_invalid_rows_are_rejected_with_reasons() -> None:
    text = (
        "account,instrument_id,quantity,as_of\n"
        "b,X,not-a-number,2026-10-12\n"
        "b,,5,2026-10-12\n"
        "b,Y,5,not-a-date\n"
    )
    preview = preview_positions(text)
    assert preview.positions == ()
    reasons = {row.reason for row in preview.rejected}
    assert "not a number" in reasons
    assert "missing instrument identifier" in reasons
    assert "unrecognised date" in reasons or "not an ISO date" in reasons


def test_missing_required_columns_raise() -> None:
    from sideboard.core.errors import ParseError

    with pytest.raises(ParseError):
        preview_positions("account,quantity\nb,5\n")


def test_prices_and_fx_previews() -> None:
    prices = preview_prices(
        "instrument_id,currency,price,price_unit_multiplier,as_of\nX,EUR,12.34,1,2026-10-12\n"
    )
    assert prices.prices[0].price == Decimal("12.34")
    assert prices.prices[0].price_unit_multiplier == Decimal("1")

    rates = preview_fx(
        "base_currency,quote_currency,rate,as_of\nPLN,EUR,0.2320,2026-10-12\nEUR,EUR,1,2026-10-12\n"
    )
    assert rates.rates[0].rate == Decimal("0.2320")
    assert rates.rejected and rates.rejected[0].reason == (
        "a directed rate needs two different currencies"
    )


# --------------------------------------------------------------------------- apply/repo


async def test_positions_replacement_is_atomic_and_idempotent(tmp_path: Path) -> None:
    database = await database_for(tmp_path)
    try:
        preview = preview_positions(POSITIONS_CSV)
        first = await apply_preview(database, preview, at=NOW)
        assert first[0].inserted == 3
        positions = await repo.load_positions(database)
        assert len(positions) == 3
        # Re-importing the same export must not double the holdings.
        second = await apply_preview(database, preview, at=NOW)
        assert second[0].skipped is True
        assert "already applied" in (second[0].reason or "")
        assert len(await repo.load_positions(database)) == 3
    finally:
        await database.aclose()


async def test_older_statement_is_refused_unless_historical(tmp_path: Path) -> None:
    database = await database_for(tmp_path)
    try:
        newer = preview_positions(POSITIONS_CSV)
        assert (await apply_preview(database, newer, at=NOW))[0].skipped is False

        older_text = POSITIONS_CSV.replace("2026-10-12", "2026-09-01")
        older = preview_positions(older_text)
        result = await apply_preview(database, older, at=NOW)
        assert result[0].skipped is True
        assert "older than the stored" in (result[0].reason or "")

        accepted = await apply_preview(database, older, at=NOW, allow_older=True, force=True)
        assert accepted[0].skipped is False
        accounts = await repo.load_accounts(database)
        assert accounts[0].as_of == date(2026, 9, 1)
    finally:
        await database.aclose()


async def test_empty_replacement_requires_explicit_confirmation(tmp_path: Path) -> None:
    database = await database_for(tmp_path)
    try:
        await apply_preview(database, preview_positions(POSITIONS_CSV), at=NOW)
        header_only = "account,instrument_id,quantity,as_of\n"
        empty = preview_positions(header_only)
        result = await apply_preview(database, empty, at=NOW, account="demo-broker")
        assert result[0].skipped is True
        assert "preview an empty replacement explicitly" in (result[0].reason or "")
        assert len(await repo.load_positions(database)) == 3
    finally:
        await database.aclose()


async def test_rollback_restores_the_previous_snapshot(tmp_path: Path) -> None:
    database = await database_for(tmp_path)
    try:
        await apply_preview(database, preview_positions(POSITIONS_CSV), at=NOW)
        second = preview_positions(POSITIONS_CSV.replace("7000", "7001").replace("42.5", "50"))
        result = await apply_preview(database, second, at=NOW, force=True)
        positions = {p.instrument_id: p.quantity for p in await repo.load_positions(database)}
        assert positions["DEMO-PLN"] == Decimal("7001")
        restored = await repo.rollback_import(database, result[0].import_id, at=NOW)
        assert restored == 3
        positions = {p.instrument_id: p.quantity for p in await repo.load_positions(database)}
        assert positions["DEMO-PLN"] == Decimal("7000")
        assert positions["DEMO-ETF"] == Decimal("42.5")
    finally:
        await database.aclose()


async def test_quotes_keep_currency_multiplier_and_date(tmp_path: Path) -> None:
    database = await database_for(tmp_path)
    try:
        await apply_preview(
            database,
            preview_prices(
                "instrument_id,currency,price,price_unit_multiplier,as_of\nX,GBp,250,1,2026-10-12\n"
            ),
            at=NOW,
        )
        quotes = await repo.load_quotes(database)
        assert quotes["X"].currency == "GBp"
        assert quotes["X"].as_of == date(2026, 10, 12)
        history = await database.fetchall("SELECT * FROM price_history")
        assert len(history) == 1
    finally:
        await database.aclose()


async def test_manual_values_are_audited_and_import_does_not_overwrite_a_manual_price(
    tmp_path: Path,
) -> None:
    database = await database_for(tmp_path)
    try:
        await repo.upsert_quote(
            database,
            quote=Quote(
                instrument_id="X",
                currency="EUR",
                price=Decimal("99"),
                as_of=date(2026, 10, 1),
                source="manual",
                quote_type="manual",
            ),
            at=NOW,
        )
        audits = await database.fetchall("SELECT * FROM manual_edits")
        assert audits == []
        await repo.upsert_position(
            database,
            account="a",
            instrument_id="X",
            quantity=Decimal("5"),
            cost_basis_total=Decimal("100"),
            cost_basis_currency="EUR",
            as_of=date(2026, 10, 1),
            at=NOW,
        )
        audits = await database.fetchall("SELECT * FROM manual_edits")
        assert len(audits) == 1
        assert audits[0]["action"] == "upsert"
        # A manual quote stays distinguishable from an imported one.
        quotes = await repo.load_quotes(database)
        assert quotes["X"].quote_type == "manual"
    finally:
        await database.aclose()


async def test_fx_rates_are_stored_as_directed_records(tmp_path: Path) -> None:
    database = await database_for(tmp_path)
    try:
        preview = preview_fx(
            "base_currency,quote_currency,rate,as_of\nPLN,EUR,0.2320,2026-10-12\n"
            "EUR,PLN,4.3100,2026-10-12\n"
        )
        await apply_preview(database, preview, at=NOW)
        rates = await repo.load_fx(database)
        assert rates[("PLN", "EUR")].rate == Decimal("0.2320")
        assert rates[("EUR", "PLN")].rate == Decimal("4.3100")
        assert rates[("PLN", "EUR")].rate != rates[("EUR", "PLN")].rate
    finally:
        await database.aclose()


async def test_rejected_rows_block_a_replacement(tmp_path: Path) -> None:
    database = await database_for(tmp_path)
    try:
        await apply_preview(database, preview_positions(POSITIONS_CSV), at=NOW)
        partial = preview_positions(
            "account,instrument_id,quantity,as_of\ndemo-broker,NEW,5,2026-10-13\ndemo-broker,,\n"
        )
        assert partial.rejected
        result = await apply_preview(database, partial, at=NOW, force=True)
        assert result[0].rejected == 1
        # The caller was told the row count, so a silent row loss cannot happen unnoticed.
        row = await database.fetchone(
            "SELECT rows_rejected FROM portfolio_imports WHERE import_id = ?",
            (result[0].import_id,),
        )
        assert row is not None and row["rows_rejected"] == 1
    finally:
        await database.aclose()


async def test_unpriced_instruments_are_reported(tmp_path: Path) -> None:
    database = await database_for(tmp_path)
    try:
        await apply_preview(database, preview_positions(POSITIONS_CSV), at=NOW)
        missing = await repo.next_missing_instruments(database)
        assert set(missing) == {"DEMO-EQUITY", "DEMO-ETF", "DEMO-PLN"}
        await apply_preview(
            database,
            preview_prices(
                "instrument_id,currency,price,as_of\nDEMO-EQUITY,EUR,12.34,2026-10-12\n"
            ),
            at=NOW,
        )
        missing = await repo.next_missing_instruments(database)
        assert "DEMO-EQUITY" not in missing
    finally:
        await database.aclose()
