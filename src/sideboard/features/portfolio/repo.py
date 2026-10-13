"""Decimal-safe repository functions for the portfolio domain.

UI code never issues SQL; these functions are the only access path. Quantities and
money are stored and read as decimal strings. A snapshot import replaces one account's
positions atomically and archives the previous state for a rollback.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from ...core.errors import StorageError
from ...core.storage import Database
from .valuation import FxRate, Position, Quote

__all__ = [
    "AccountState",
    "ApplyResult",
    "ImportAudit",
    "append_quote_history",
    "apply_positions_snapshot",
    "audit",
    "load_accounts",
    "load_dividends",
    "load_fx",
    "load_links",
    "load_positions",
    "load_previous_closes",
    "load_quotes",
    "load_reported_values",
    "next_missing_instruments",
    "record_manual_edit",
    "rollback_import",
    "upsert_dividend",
    "upsert_instrument",
    "upsert_position",
    "upsert_quote",
    "upsert_rate",
]


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation, ValueError:
        return None


def _sdec(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class AccountState:
    account: str
    as_of: date | None
    import_id: str | None
    updated_at: str | None
    note: str | None = None
    position_count: int = 0


@dataclass(frozen=True, slots=True)
class ImportAudit:
    import_id: str
    kind: str
    path: str | None
    file_hash: str | None
    statement_date: date | None
    imported_at: datetime
    account: str | None
    rows_ok: int
    rows_rejected: int
    status: str
    warnings: tuple[str, ...] = ()
    supersedes: str | None = None


@dataclass(frozen=True, slots=True)
class ApplyResult:
    import_id: str
    account: str
    replaced: int
    inserted: int
    rejected: int
    skipped: bool = False
    reason: str | None = None


# --------------------------------------------------------------------------- reads


async def load_positions(database: Database) -> tuple[Position, ...]:
    rows = await database.fetchall(
        """
        SELECT p.account, p.instrument_id, p.quantity, p.cost_basis_total,
               p.cost_basis_currency, p.as_of, p.source, i.isin, i.venue, i.currency,
               i.name
        FROM positions p LEFT JOIN instruments i ON i.instrument_id = p.instrument_id
        ORDER BY p.account, p.instrument_id
        """
    )
    out: list[Position] = []
    for row in rows:
        quantity = _dec(row["quantity"])
        if quantity is None:
            continue
        out.append(
            Position(
                account=str(row["account"]),
                instrument_id=str(row["instrument_id"]),
                quantity=quantity,
                currency=str(row["currency"] or row["cost_basis_currency"] or _UNKNOWN),
                isin=row["isin"],
                venue=row["venue"],
                cost_basis_total=_dec(row["cost_basis_total"]),
                cost_basis_currency=row["cost_basis_currency"],
                as_of=_date(row["as_of"]),
                source=str(row["source"] or "manual"),
                name=row["name"],
            )
        )
    return tuple(out)


_UNKNOWN = "UNKNOWN"


async def load_accounts(database: Database) -> tuple[AccountState, ...]:
    rows = await database.fetchall(
        """
        SELECT a.account, a.as_of, a.import_id, a.updated_at, a.note,
               (SELECT COUNT(*) FROM positions p WHERE p.account = a.account) AS n
        FROM position_accounts a ORDER BY a.account
        """
    )
    return tuple(
        AccountState(
            account=str(row["account"]),
            as_of=_date(row["as_of"]),
            import_id=row["import_id"],
            updated_at=row["updated_at"],
            note=row["note"],
            position_count=int(row["n"] or 0),
        )
        for row in rows
    )


async def load_quotes(database: Database) -> dict[str, Quote]:
    rows = await database.fetchall("SELECT * FROM prices")
    out: dict[str, Quote] = {}
    for row in rows:
        price = _dec(row["price"])
        if price is None:
            continue
        out[str(row["instrument_id"])] = Quote(
            instrument_id=str(row["instrument_id"]),
            currency=str(row["currency"]),
            price=price,
            price_unit_multiplier=_dec(row["price_unit_multiplier"]) or Decimal(1),
            as_of=_date(row["as_of"]),
            fetched_at=_date_time(row["fetched_at"]),
            source=str(row["source"]),
            quote_type=str(row["quote_type"]),
        )
    return out


async def load_previous_closes(database: Database) -> dict[str, Decimal]:
    """Most recent historical quote strictly before the current quote date."""
    rows = await database.fetchall(
        """
        SELECT h.instrument_id, h.as_of, h.price
        FROM price_history h
        JOIN prices p ON p.instrument_id = h.instrument_id
        WHERE p.as_of IS NULL OR h.as_of < p.as_of
        ORDER BY h.instrument_id, h.as_of DESC
        """
    )
    out: dict[str, Decimal] = {}
    for row in rows:
        key = str(row["instrument_id"])
        if key in out:
            continue
        value = _dec(row["price"])
        if value is not None:
            out[key] = value
    return out


async def load_fx(database: Database) -> dict[tuple[str, str], FxRate]:
    rows = await database.fetchall(
        "SELECT base, quote, rate, as_of, source FROM fx_rates ORDER BY as_of"
    )
    out: dict[tuple[str, str], FxRate] = {}
    for row in rows:
        rate = _dec(row["rate"])
        if rate is None or rate <= 0:
            continue
        out[(str(row["base"]), str(row["quote"]))] = FxRate(
            base=str(row["base"]),
            quote=str(row["quote"]),
            rate=rate,
            as_of=_date(row["as_of"]),
            source=str(row["source"] or "manual"),
        )
    return out


async def load_reported_values(
    database: Database,
) -> dict[tuple[str, str], tuple[Decimal, str, date | None]]:
    rows = await database.fetchall(
        """
        SELECT account, instrument_id, value, currency, as_of FROM reported_values
        ORDER BY as_of
        """
    )
    out: dict[tuple[str, str], tuple[Decimal, str, date | None]] = {}
    for row in rows:
        value = _dec(row["value"])
        if value is None:
            continue
        out[(str(row["account"]), str(row["instrument_id"]))] = (
            value,
            str(row["currency"]),
            _date(row["as_of"]),
        )
    return out


async def load_dividends(database: Database, instrument_id: str | None = None) -> tuple[dict, ...]:
    if instrument_id:
        rows = await database.fetchall(
            "SELECT * FROM dividends WHERE instrument_id = ? ORDER BY COALESCE(pay_date, ex_date)",
            (instrument_id,),
        )
    else:
        rows = await database.fetchall(
            "SELECT * FROM dividends ORDER BY COALESCE(pay_date, ex_date)"
        )
    return tuple(dict(row) for row in rows)


async def load_links(database: Database, instrument_id: str | None = None) -> tuple[dict, ...]:
    if instrument_id:
        rows = await database.fetchall(
            "SELECT * FROM investor_links WHERE instrument_id IS NULL OR instrument_id = ? "
            "ORDER BY label",
            (instrument_id,),
        )
    else:
        rows = await database.fetchall("SELECT * FROM investor_links ORDER BY label")
    return tuple(dict(row) for row in rows)


async def load_imports(database: Database, *, limit: int = 20) -> tuple[ImportAudit, ...]:
    rows = await database.fetchall(
        "SELECT * FROM portfolio_imports ORDER BY imported_at DESC LIMIT ?", (limit,)
    )
    return tuple(
        ImportAudit(
            import_id=str(row["import_id"]),
            kind=str(row["kind"]),
            path=row["path"],
            file_hash=row["file_hash"],
            statement_date=_date(row["statement_date"]),
            imported_at=_date_time(row["imported_at"]) or datetime.now(UTC),
            account=row["account"],
            rows_ok=int(row["rows_ok"] or 0),
            rows_rejected=int(row["rows_rejected"] or 0),
            status=str(row["status"]),
            warnings=tuple(json.loads(row["warnings_json"] or "[]")),
            supersedes=row["supersedes"],
        )
        for row in rows
    )


async def next_missing_instruments(database: Database) -> tuple[str, ...]:
    """Positions with no quote at all, so the UI can say what is unpriced."""
    rows = await database.fetchall(
        "SELECT DISTINCT p.instrument_id FROM positions p "
        "LEFT JOIN prices q ON q.instrument_id = p.instrument_id WHERE q.instrument_id IS NULL "
        "ORDER BY p.instrument_id"
    )
    return tuple(str(row["instrument_id"]) for row in rows)


# --------------------------------------------------------------------------- writes


async def audit(
    database: Database,
    *,
    entity: str,
    entity_id: str,
    action: str,
    payload: Mapping[str, object] | None,
    at: datetime,
) -> None:
    await database.execute(
        "INSERT INTO manual_edits(entity, entity_id, action, payload_json, at) VALUES (?,?,?,?,?)",
        (entity, entity_id, action, json.dumps(dict(payload or {}), default=str), at.isoformat()),
    )


async def upsert_instrument(
    database: Database,
    *,
    instrument_id: str,
    isin: str | None,
    venue: str | None,
    currency: str | None,
    name: str | None,
    at: datetime,
) -> None:
    await database.execute(
        """
        INSERT INTO instruments(instrument_id, isin, name, venue, currency, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(instrument_id) DO UPDATE SET
            isin=COALESCE(excluded.isin, instruments.isin),
            name=COALESCE(excluded.name, instruments.name),
            venue=COALESCE(excluded.venue, instruments.venue),
            currency=COALESCE(excluded.currency, instruments.currency),
            updated_at=excluded.updated_at
        """,
        (instrument_id, isin, name, venue, currency, at.isoformat(), at.isoformat()),
    )


async def upsert_position(
    database: Database,
    *,
    account: str,
    instrument_id: str,
    quantity: Decimal,
    currency: str | None = None,
    cost_basis_total: Decimal | None = None,
    cost_basis_currency: str | None = None,
    as_of: date | None = None,
    source: str = "manual",
    at: datetime,
) -> None:
    """Manual edit of one position. Never silently overwritten by a quote or an import."""
    await database.execute(
        """
        INSERT INTO positions(account, instrument_id, quantity, cost_basis_total,
                              cost_basis_currency, as_of, source, updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(account, instrument_id) DO UPDATE SET
            quantity=excluded.quantity,
            cost_basis_total=excluded.cost_basis_total,
            cost_basis_currency=excluded.cost_basis_currency,
            as_of=excluded.as_of,
            source=excluded.source,
            updated_at=excluded.updated_at
        """,
        (
            account,
            instrument_id,
            _sdec(quantity),
            _sdec(cost_basis_total),
            cost_basis_currency,
            _iso(as_of),
            source,
            at.isoformat(),
        ),
    )
    if currency:
        await upsert_instrument(
            database,
            instrument_id=instrument_id,
            isin=None,
            venue=None,
            currency=currency,
            name=None,
            at=at,
        )
    await record_manual_edit(
        database,
        entity="position",
        entity_id=f"{account}:{instrument_id}",
        action="upsert",
        payload={
            "quantity": _sdec(quantity),
            "cost_basis_total": _sdec(cost_basis_total),
            "as_of": _iso(as_of),
        },
        at=at,
    )


async def record_manual_edit(
    database: Database,
    *,
    entity: str,
    entity_id: str,
    action: str,
    payload: Mapping[str, object] | None,
    at: datetime,
) -> None:
    """Audit trail for every local manual change."""
    await audit(database, entity=entity, entity_id=entity_id, action=action, payload=payload, at=at)


async def upsert_quote(
    database: Database,
    *,
    quote: Quote,
    at: datetime,
    keep_original_date: bool = True,
) -> None:
    await database.execute(
        """
        INSERT INTO prices(instrument_id, currency, price, price_unit_multiplier, as_of,
                           fetched_at, source, quote_type)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(instrument_id) DO UPDATE SET
            currency=excluded.currency,
            price=excluded.price,
            price_unit_multiplier=excluded.price_unit_multiplier,
            as_of=excluded.as_of,
            fetched_at=excluded.fetched_at,
            source=excluded.source,
            quote_type=excluded.quote_type
        """,
        (
            quote.instrument_id,
            quote.currency,
            _sdec(quote.price),
            _sdec(quote.price_unit_multiplier),
            _iso(quote.as_of),
            at.isoformat(),
            quote.source,
            quote.quote_type,
        ),
    )
    if quote.as_of is not None and keep_original_date:
        await append_quote_history(database, quote=quote)


async def append_quote_history(database: Database, *, quote: Quote) -> None:
    """Adjusted history is a separate series and is never multiplied by today's quantity."""
    await database.execute(
        """
        INSERT OR REPLACE INTO price_history(instrument_id, as_of, price, currency,
                                             price_unit_multiplier, source)
        VALUES (?,?,?,?,?,?)
        """,
        (
            quote.instrument_id,
            _iso(quote.as_of) or datetime.now(UTC).date().isoformat(),
            _sdec(quote.price),
            quote.currency,
            _sdec(quote.price_unit_multiplier),
            quote.source,
        ),
    )


async def upsert_rate(
    database: Database,
    *,
    base: str,
    quote: str,
    rate: Decimal,
    as_of: date | None,
    source: str = "manual",
    at: datetime,
) -> None:
    """Store one **directed** rate. EUR/PLN and PLN/EUR are different records."""
    await database.execute(
        """
        INSERT OR REPLACE INTO fx_rates(base, quote, rate, as_of, source, fetched_at)
        VALUES (?,?,?,?,?,?)
        """,
        (base, quote, _sdec(rate), _iso(as_of), source, at.isoformat()),
    )


async def upsert_dividend(
    database: Database,
    *,
    dividend_id: str,
    instrument_id: str,
    kind: str,
    gross: Decimal | None,
    currency: str | None,
    ex_date: date | None,
    record_date: date | None,
    pay_date: date | None,
    source: str,
    notes: str | None,
    at: datetime,
) -> None:
    if kind not in {"declared", "received", "estimated"}:
        raise StorageError(f"unsupported dividend kind: {kind}")
    await database.execute(
        """
        INSERT OR REPLACE INTO dividends(dividend_id, instrument_id, kind, gross, currency,
                                         ex_date, record_date, pay_date, source, notes, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            dividend_id,
            instrument_id,
            kind,
            _sdec(gross),
            currency,
            _iso(ex_date),
            _iso(record_date),
            _iso(pay_date),
            source,
            notes,
            at.isoformat(),
        ),
    )


async def upsert_link(
    database: Database,
    *,
    link_id: str,
    instrument_id: str | None,
    label: str,
    url: str,
    kind: str,
    at: datetime,
) -> None:
    await database.execute(
        """
        INSERT OR REPLACE INTO investor_links(link_id, instrument_id, label, url, kind, updated_at)
        VALUES (?,?,?,?,?,?)
        """,
        (link_id, instrument_id, label, url, kind, at.isoformat()),
    )


# --------------------------------------------------------------------------- imports


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


async def apply_positions_snapshot(
    database: Database,
    *,
    account: str,
    positions: Sequence[Position],
    import_id: str,
    kind: str,
    path: str | None,
    file_hash: str | None,
    statement_date: date | None,
    rows_rejected: int,
    warnings: Sequence[str],
    mapping: Mapping[str, object] | None,
    at: datetime,
    allow_older: bool = False,
    allow_empty: bool = False,
    force: bool = False,
) -> ApplyResult:
    """Replace one account's positions atomically.

    * rejects an older statement unless explicitly accepted as historical
    * rejects an empty replacement unless explicitly previewed
    * an identical file hash is a no-op, so re-importing the same export does not
      double a holding
    * archives the previous rows so :func:`rollback_import` can restore them
    """
    if not positions and not allow_empty:
        return ApplyResult(
            import_id=import_id,
            account=account,
            replaced=0,
            inserted=0,
            rejected=rows_rejected,
            skipped=True,
            reason="the snapshot contains no valid rows; preview an empty replacement explicitly",
        )

    existing = await database.fetchone(
        "SELECT as_of, import_id FROM position_accounts WHERE account = ?", (account,)
    )
    previous_import = str(existing["import_id"]) if existing and existing["import_id"] else None
    previous_as_of = _date(existing["as_of"]) if existing else None

    if not force and file_hash is not None:
        duplicate = await database.fetchone(
            "SELECT import_id FROM portfolio_imports WHERE file_hash = ? AND account = ? "
            "AND status = 'applied' LIMIT 1",
            (file_hash, account),
        )
        if duplicate is not None:
            return ApplyResult(
                import_id=import_id,
                account=account,
                replaced=0,
                inserted=0,
                rejected=rows_rejected,
                skipped=True,
                reason="this exact export was already applied for this account",
            )

    older = (
        statement_date is not None
        and previous_as_of is not None
        and statement_date < previous_as_of
    )
    if older and not allow_older:
        assert statement_date is not None and previous_as_of is not None
        return ApplyResult(
            import_id=import_id,
            account=account,
            replaced=0,
            inserted=0,
            rejected=rows_rejected,
            skipped=True,
            reason=(
                f"statement date {statement_date.isoformat()} is older than the stored "
                f"{previous_as_of.isoformat()}; re-run with historical replacement to accept it"
            ),
        )

    old_rows = await database.fetchall("SELECT * FROM positions WHERE account = ?", (account,))

    async with database.transaction() as cursor:
        seq = 0
        for row in old_rows:
            await cursor.execute(
                "INSERT INTO import_rollback(import_id, seq, entity, payload_json) VALUES (?,?,?,?)",
                (
                    import_id,
                    seq,
                    "position",
                    json.dumps(
                        {
                            "account": row["account"],
                            "instrument_id": row["instrument_id"],
                            "quantity": row["quantity"],
                            "cost_basis_total": row["cost_basis_total"],
                            "cost_basis_currency": row["cost_basis_currency"],
                            "as_of": row["as_of"],
                            "source": row["source"],
                        }
                    ),
                ),
            )
            seq += 1
        await cursor.execute(
            "INSERT INTO import_rollback(import_id, seq, entity, payload_json) VALUES (?,?,?,?)",
            (
                import_id,
                seq,
                "account_state",
                json.dumps(
                    {
                        "as_of": existing["as_of"] if existing else None,
                        "import_id": previous_import,
                    }
                ),
            ),
        )
        await cursor.execute("DELETE FROM positions WHERE account = ?", (account,))
        await cursor.execute("DELETE FROM reported_values WHERE account = ?", (account,))
        for position in positions:
            await cursor.execute(
                """
                INSERT INTO positions(account, instrument_id, quantity, cost_basis_total,
                                      cost_basis_currency, as_of, source, import_id, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    account,
                    position.instrument_id,
                    _sdec(position.quantity),
                    _sdec(position.cost_basis_total),
                    position.cost_basis_currency,
                    _iso(position.as_of or statement_date),
                    "statement",
                    import_id,
                    at.isoformat(),
                ),
            )
            await cursor.execute(
                """
                INSERT INTO instruments(instrument_id, isin, name, venue, currency,
                                        created_at, updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(instrument_id) DO UPDATE SET
                    isin=COALESCE(excluded.isin, instruments.isin),
                    name=COALESCE(excluded.name, instruments.name),
                    venue=COALESCE(excluded.venue, instruments.venue),
                    currency=COALESCE(excluded.currency, instruments.currency),
                    updated_at=excluded.updated_at
                """,
                (
                    position.instrument_id,
                    position.isin,
                    position.name,
                    position.venue,
                    position.currency,
                    at.isoformat(),
                    at.isoformat(),
                ),
            )
        await cursor.execute(
            """
            INSERT INTO position_accounts(account, as_of, import_id, updated_at, note)
            VALUES (?,?,?,?,?)
            ON CONFLICT(account) DO UPDATE SET
                as_of=excluded.as_of, import_id=excluded.import_id,
                updated_at=excluded.updated_at, note=excluded.note
            """,
            (
                account,
                _iso(statement_date),
                import_id,
                at.isoformat(),
                f"replaced by {kind} import",
            ),
        )
        await cursor.execute(
            """
            INSERT OR REPLACE INTO portfolio_imports(import_id, kind, path, file_hash,
                statement_date, imported_at, account, rows_ok, rows_rejected, mapping_json,
                status, warnings_json, supersedes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                import_id,
                kind,
                path,
                file_hash,
                _iso(statement_date),
                at.isoformat(),
                account,
                len(positions),
                rows_rejected,
                json.dumps(dict(mapping or {}), default=str),
                "applied",
                json.dumps(list(warnings)),
                previous_import,
            ),
        )
    return ApplyResult(
        import_id=import_id,
        account=account,
        replaced=len(old_rows),
        inserted=len(positions),
        rejected=rows_rejected,
    )


async def rollback_import(database: Database, import_id: str, *, at: datetime) -> int:
    """Restore the state archived before an applied import. Returns restored rows."""
    rows = await database.fetchall(
        "SELECT seq, entity, payload_json FROM import_rollback WHERE import_id = ? ORDER BY seq",
        (import_id,),
    )
    if not rows:
        raise StorageError("no rollback record for this import")
    restored = 0
    async with database.transaction() as cursor:
        for row in rows:
            payload = json.loads(row["payload_json"])
            if row["entity"] == "position":
                await cursor.execute(
                    """
                    INSERT INTO positions(account, instrument_id, quantity, cost_basis_total,
                                          cost_basis_currency, as_of, source, updated_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(account, instrument_id) DO UPDATE SET
                        quantity=excluded.quantity,
                        cost_basis_total=excluded.cost_basis_total,
                        cost_basis_currency=excluded.cost_basis_currency,
                        as_of=excluded.as_of,
                        source=excluded.source,
                        updated_at=excluded.updated_at
                    """,
                    (
                        payload["account"],
                        payload["instrument_id"],
                        payload["quantity"],
                        payload.get("cost_basis_total"),
                        payload.get("cost_basis_currency"),
                        payload.get("as_of"),
                        payload.get("source") or "manual",
                        at.isoformat(),
                    ),
                )
                restored += 1
            elif row["entity"] == "account_state":
                await cursor.execute(
                    "UPDATE position_accounts SET as_of = ?, import_id = ?, updated_at = ? "
                    "WHERE account = (SELECT account FROM portfolio_imports WHERE import_id = ?)",
                    (
                        payload.get("as_of"),
                        payload.get("import_id"),
                        at.isoformat(),
                        import_id,
                    ),
                )
        await cursor.execute(
            "UPDATE portfolio_imports SET status = 'rolled_back' WHERE import_id = ?",
            (import_id,),
        )
    return restored


def new_import_id() -> str:
    return uuid.uuid4().hex[:16]


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _date_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iter_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
