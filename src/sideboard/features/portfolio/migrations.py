"""Portfolio domain persistence, owned by this feature and registered centrally.

Money and quantities are stored as decimal **strings**: SQLite REAL would silently
lose precision. Positions are replaced per account atomically by a snapshot import;
the previous state is archived so a rollback is possible, and every manual change is
recorded in an audit trail.
"""

from __future__ import annotations

from ...core.contracts import Migration

__all__ = ["PORTFOLIO_MIGRATIONS"]

# Owners: the directed FX rate table belongs to the money domain, together with the
# ``fx.rates`` payload model and its repository functions. The fx feature depends on the
# money domain for its payload and store, never the reverse, so there is no cycle.

PORTFOLIO_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=10,
        name="portfolio_domain",
        owner="portfolio",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS instruments (
                instrument_id TEXT PRIMARY KEY,
                isin TEXT,
                name TEXT,
                venue TEXT,
                currency TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS position_accounts (
                account TEXT PRIMARY KEY,
                as_of TEXT,
                import_id TEXT,
                updated_at TEXT NOT NULL,
                note TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS positions (
                account TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                quantity TEXT NOT NULL,
                cost_basis_total TEXT,
                cost_basis_currency TEXT,
                as_of TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                import_id TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (account, instrument_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS prices (
                instrument_id TEXT PRIMARY KEY,
                currency TEXT NOT NULL,
                price TEXT NOT NULL,
                price_unit_multiplier TEXT NOT NULL DEFAULT '1',
                as_of TEXT,
                fetched_at TEXT NOT NULL,
                source TEXT NOT NULL,
                quote_type TEXT NOT NULL DEFAULT 'manual'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS price_history (
                instrument_id TEXT NOT NULL,
                as_of TEXT NOT NULL,
                price TEXT NOT NULL,
                currency TEXT NOT NULL,
                price_unit_multiplier TEXT NOT NULL DEFAULT '1',
                source TEXT NOT NULL,
                PRIMARY KEY (instrument_id, as_of)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS reported_values (
                account TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                value TEXT NOT NULL,
                currency TEXT NOT NULL,
                as_of TEXT NOT NULL,
                import_id TEXT,
                PRIMARY KEY (account, instrument_id, as_of)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS portfolio_imports (
                import_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                path TEXT,
                file_hash TEXT,
                statement_date TEXT,
                imported_at TEXT NOT NULL,
                account TEXT,
                rows_ok INTEGER NOT NULL DEFAULT 0,
                rows_rejected INTEGER NOT NULL DEFAULT 0,
                mapping_json TEXT,
                status TEXT NOT NULL,
                warnings_json TEXT,
                supersedes TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS import_rows (
                import_id TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                raw_json TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                PRIMARY KEY (import_id, row_index)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS import_rollback (
                import_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                entity TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (import_id, seq)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS manual_edits (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_json TEXT,
                at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS dividends (
                dividend_id TEXT PRIMARY KEY,
                instrument_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                gross TEXT,
                currency TEXT,
                ex_date TEXT,
                record_date TEXT,
                pay_date TEXT,
                source TEXT,
                notes TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS investor_links (
                link_id TEXT PRIMARY KEY,
                instrument_id TEXT,
                label TEXT NOT NULL,
                url TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'report',
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=21,
        name="fx_rates",
        owner="portfolio",
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
