"""Every table a repository reads must exist after initialisation.

This is the regression test for a real bug: the directed FX rate table was read by a
repository and written by the import command, but no migration created it, so `import fx`
worked on a developer machine (where the table had been created by hand) and failed on a
fresh database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sideboard.core.items import ItemStore
from sideboard.core.storage import CORE_MIGRATIONS, Database
from sideboard.features import build_feature_registry
from sideboard.features.portfolio import repo

TABLES_READ_BY_REPOSITORIES = (
    "source_state",
    "items",
    "item_state",
    "saved_links",
    "metric_samples",
    "events",
    "app_state",
    "instruments",
    "position_accounts",
    "positions",
    "prices",
    "price_history",
    "reported_values",
    "portfolio_imports",
    "import_rows",
    "import_rollback",
    "manual_edits",
    "dividends",
    "investor_links",
    "fx_rates",
)


def test_migration_versions_are_unique_and_ordered() -> None:
    registry = build_feature_registry()
    migrations = tuple(CORE_MIGRATIONS) + registry.migrations()
    versions = [migration.version for migration in migrations]
    assert versions == sorted(versions), "migrations must apply in order"
    assert len(set(versions)) == len(versions), "two owners may not claim one version"
    for migration in migrations:
        assert migration.name
        assert migration.owner
        assert migration.statements


async def test_fresh_database_has_every_table_a_repository_uses(tmp_path: Path) -> None:
    registry = build_feature_registry()
    database = await Database.open(tmp_path / "fresh.sqlite3")
    try:
        await database.initialize(tuple(CORE_MIGRATIONS) + registry.migrations())
        rows = await database.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        present = {str(row["name"]) for row in rows}
        missing = [name for name in TABLES_READ_BY_REPOSITORIES if name not in present]
        assert not missing, f"no migration creates: {', '.join(missing)}"
    finally:
        await database.aclose()


async def test_repositories_run_against_a_fresh_database(tmp_path: Path) -> None:
    """Each read path must work before any data exists, not raise 'no such table'."""
    registry = build_feature_registry()
    database = await Database.open(tmp_path / "fresh.sqlite3")
    try:
        await database.initialize(tuple(CORE_MIGRATIONS) + registry.migrations())
        assert await repo.load_positions(database) == ()
        assert await repo.load_accounts(database) == ()
        assert await repo.load_quotes(database) == {}
        assert await repo.load_previous_closes(database) == {}
        assert await repo.load_fx(database) == {}
        assert await repo.load_reported_values(database) == {}
        assert await repo.load_dividends(database) == ()
        assert await repo.load_links(database) == ()
        assert await repo.load_imports(database) == ()
        assert await repo.next_missing_instruments(database) == ()
        store = ItemStore(database)
        assert await store.saved_items() == ()
        assert await store.states([]) == {}
        await store.purge_expired(datetime.now(UTC))
    finally:
        await database.aclose()


async def test_import_of_every_kind_works_on_a_fresh_database(tmp_path: Path) -> None:
    """The CLI's import path initialises core plus every registered feature migration."""
    from sideboard.features.portfolio.importer import (
        apply_preview,
        preview_fx,
        preview_positions,
        preview_prices,
    )

    registry = build_feature_registry()
    database = await Database.open(tmp_path / "fresh.sqlite3")
    try:
        await database.initialize(tuple(CORE_MIGRATIONS) + registry.migrations())
        now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
        positions = await apply_preview(
            database,
            preview_positions(
                "account,instrument_id,quantity,currency,as_of\n"
                "demo-broker,DEMO-EQUITY,10,EUR,2026-10-12\n"
            ),
            at=now,
        )
        assert positions and positions[0].inserted == 1
        await apply_preview(
            database,
            preview_prices(
                "instrument_id,currency,price,as_of\nDEMO-EQUITY,EUR,12.34,2026-10-12\n"
            ),
            at=now,
        )
        await apply_preview(
            database,
            preview_fx("base_currency,quote_currency,rate,as_of\nPLN,EUR,0.22805,2026-10-12\n"),
            at=now,
        )
        quotes = await repo.load_quotes(database)
        assert quotes["DEMO-EQUITY"].price == Decimal("12.34")
        assert ("PLN", "EUR") in await repo.load_fx(database)
    finally:
        await database.aclose()
