"""Normalized CSV import with preview, explicit locale and row-level validation.

Design rules from the plan, implemented here:

* a preview happens before anything is committed, and it can be cancelled
* decimal separators and date order come from an **explicit locale choice**, never
  from guessing per row
* invalid rows are rejected with a reason; a replacement snapshot with invalid rows is
  refused rather than silently dropping a holding
* replacing an account is atomic and archived, so a rollback is possible
* re-importing the same export is idempotent
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ...core.errors import ParseError
from ...core.storage import Database
from .repo import ApplyResult, apply_positions_snapshot, content_hash, new_import_id
from .valuation import Position

__all__ = [
    "CsvOptions",
    "FxRow",
    "ImportPreview",
    "PositionRow",
    "PriceRow",
    "RejectedRow",
    "apply_preview",
    "preview_fx",
    "preview_positions",
    "preview_prices",
]

POSITION_COLUMNS = (
    "account",
    "instrument_id",
    "isin",
    "venue",
    "currency",
    "quantity",
    "cost_basis_total",
    "cost_basis_currency",
    "as_of",
    "reported_market_value",
)
PRICE_COLUMNS = ("instrument_id", "currency", "price", "price_unit_multiplier", "as_of")
FX_COLUMNS = ("base_currency", "quote_currency", "rate", "as_of")

MAX_ROWS = 5000


@dataclass(frozen=True, slots=True)
class CsvOptions:
    """Explicit parsing choices. Nothing here is inferred from the data."""

    delimiter: str = ","
    decimal: str = "point"  # "point" -> 1,234.56 · "comma" -> 1.234,56
    date_order: str = "iso"  # "iso" · "dmy" · "mdy"
    mapping: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RejectedRow:
    index: int
    reason: str
    raw: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PositionRow:
    account: str
    instrument_id: str
    quantity: Decimal
    currency: str | None = None
    isin: str | None = None
    venue: str | None = None
    cost_basis_total: Decimal | None = None
    cost_basis_currency: str | None = None
    as_of: date | None = None
    reported_market_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PriceRow:
    instrument_id: str
    currency: str
    price: Decimal
    price_unit_multiplier: Decimal = Decimal(1)
    as_of: date | None = None


@dataclass(frozen=True, slots=True)
class FxRow:
    base_currency: str
    quote_currency: str
    rate: Decimal
    as_of: date | None = None


@dataclass(frozen=True, slots=True)
class ImportPreview:
    """Everything the user must see before a commit."""

    kind: str
    path: str | None
    file_hash: str
    options: CsvOptions
    positions: tuple[PositionRow, ...] = ()
    prices: tuple[PriceRow, ...] = ()
    rates: tuple[FxRow, ...] = ()
    rejected: tuple[RejectedRow, ...] = ()
    warnings: tuple[str, ...] = ()
    accounts: tuple[str, ...] = ()
    statement_dates: tuple[tuple[str, date | None], ...] = ()
    replaces_positions: int = 0

    @property
    def ok(self) -> bool:
        return not self.rejected and bool(self.positions or self.prices or self.rates)

    def summary(self) -> str:
        parts = [
            f"{self.kind}: {len(self.positions)} positions, {len(self.prices)} prices, {len(self.rates)} rates"
        ]
        if self.rejected:
            parts.append(f"{len(self.rejected)} rejected")
        if self.accounts:
            parts.append("accounts: " + ", ".join(self.accounts))
        if self.replaces_positions:
            parts.append(f"replaces {self.replaces_positions} stored positions")
        return "; ".join(parts)


# --------------------------------------------------------------------------- parsing


def _normalise_header(value: str) -> str:
    return "".join(char for char in value.strip().lower().replace("\ufeff", "") if char.isalnum())


_HEADER_ALIASES = {
    "instrumentid": "instrument_id",
    "instrument": "instrument_id",
    "symbol": "instrument_id",
    "ticker": "instrument_id",
    "isin": "isin",
    "venue": "venue",
    "exchange": "venue",
    "currency": "currency",
    "quantity": "quantity",
    "shares": "quantity",
    "qty": "quantity",
    "costbasistotal": "cost_basis_total",
    "costbasis": "cost_basis_total",
    "totalcost": "cost_basis_total",
    "costbasiscurrency": "cost_basis_currency",
    "costcurrency": "cost_basis_currency",
    "asof": "as_of",
    "date": "as_of",
    "statementdate": "as_of",
    "reportedmarketvalue": "reported_market_value",
    "marketvalue": "reported_market_value",
    "value": "reported_market_value",
    "account": "account",
    "accountalias": "account",
    "price": "price",
    "priceunitmultiplier": "price_unit_multiplier",
    "multiplier": "price_unit_multiplier",
    "basecurrency": "base_currency",
    "quotecurrency": "quote_currency",
    "rate": "rate",
}


def guess_mapping(headers: Sequence[str]) -> dict[str, str]:
    """Best-effort header mapping. The result is shown in the preview and can be edited."""
    mapping: dict[str, str] = {}
    for header in headers:
        canonical = _HEADER_ALIASES.get(_normalise_header(header))
        if canonical and canonical not in mapping:
            mapping[canonical] = header
    return mapping


def parse_decimal(text: str, options: CsvOptions) -> Decimal:
    """Parse a decimal using the **chosen** locale, not per-row heuristics."""
    value = (text or "").strip()
    if not value:
        raise ParseError("empty number")
    has_point = "." in value
    has_comma = "," in value
    if options.decimal == "comma":
        # European convention: '.' groups thousands, ',' is the decimal separator.
        if has_comma and has_point:
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", ".")
    else:
        if has_comma and has_point:
            # Both separators present is ambiguous in the point locale: most likely a
            # European-formatted number read with the wrong locale choice.
            raise ParseError("ambiguous decimal separator")
        if has_comma:
            # A single comma is a thousands separator only in the unambiguous 1,234 form.
            head, _, tail = value.partition(",")
            if len(tail) == 3 and head.lstrip("-").isdigit():
                value = value.replace(",", "")
            else:
                raise ParseError("ambiguous decimal separator")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ParseError("not a number") from exc


def parse_date(text: str, options: CsvOptions) -> date | None:
    value = (text or "").strip()
    if not value:
        return None
    cleaned = value.replace("/", "-").replace(".", "-")
    if options.date_order == "iso" or (len(cleaned) >= 10 and cleaned[4] == "-"):
        try:
            return date.fromisoformat(cleaned[:10])
        except ValueError as exc:
            raise ParseError("not an ISO date") from exc
    parts = cleaned.split("-")
    if len(parts) != 3:
        raise ParseError("unrecognised date")
    third_len = len(parts[2])
    if options.date_order == "dmy":
        day, month, year = parts[0], parts[1], parts[2]
    else:
        month, day, year = parts[0], parts[1], parts[2]
    if third_len == 2:
        year = f"20{year}"
    try:
        return date(int(year), int(month), int(day))
    except ValueError as exc:
        raise ParseError("invalid calendar date") from exc


def _read_rows(text: str, options: CsvOptions) -> tuple[list[str], list[dict[str, str]]]:
    if len(text) > 4 * 1024 * 1024:
        raise ParseError("import file is too large; split it into smaller exports")
    sample = text[:4096]
    delimiter = options.delimiter
    if delimiter == "auto":
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [header or "" for header in (reader.fieldnames or [])]
    rows: list[dict[str, str]] = []
    for index, row in enumerate(reader):
        if index >= MAX_ROWS:
            raise ParseError(f"import file has more than {MAX_ROWS} rows")
        rows.append({str(k): (v or "") for k, v in row.items() if k is not None})
    return headers, rows


def _cell(row: Mapping[str, str], mapping: Mapping[str, str], field_name: str) -> str:
    header = mapping.get(field_name)
    if header is None:
        return ""
    return str(row.get(header, "")).strip()


def preview_positions(
    text: str,
    *,
    options: CsvOptions | None = None,
    path: str | None = None,
    mapping: Mapping[str, str] | None = None,
) -> ImportPreview:
    options = options or CsvOptions()
    headers, rows = _read_rows(text, options)
    resolved = dict(mapping or options.mapping or guess_mapping(headers))
    missing = [name for name in ("account", "instrument_id", "quantity") if name not in resolved]
    warnings: list[str] = []
    if missing:
        raise ParseError(f"missing required columns: {', '.join(missing)}")
    accepted: list[PositionRow] = []
    rejected: list[RejectedRow] = []
    for index, row in enumerate(rows, start=2):
        try:
            account = _cell(row, resolved, "account") or "default"
            instrument = _cell(row, resolved, "instrument_id")
            if not instrument:
                raise ParseError("missing instrument identifier")
            quantity = parse_decimal(_cell(row, resolved, "quantity"), options)
            basis_raw = _cell(row, resolved, "cost_basis_total")
            basis = parse_decimal(basis_raw, options) if basis_raw else None
            reported_raw = _cell(row, resolved, "reported_market_value")
            reported = parse_decimal(reported_raw, options) if reported_raw else None
            accepted.append(
                PositionRow(
                    account=account,
                    instrument_id=instrument,
                    quantity=quantity,
                    currency=_cell(row, resolved, "currency") or None,
                    isin=_cell(row, resolved, "isin") or None,
                    venue=_cell(row, resolved, "venue") or None,
                    cost_basis_total=basis,
                    cost_basis_currency=_cell(row, resolved, "cost_basis_currency") or None,
                    as_of=parse_date(_cell(row, resolved, "as_of"), options),
                    reported_market_value=reported,
                )
            )
        except ParseError as error:
            rejected.append(RejectedRow(index=index, reason=str(error), raw=row))
    accounts = tuple(sorted({row.account for row in accepted}))
    dates = tuple(
        (account, next((row.as_of for row in accepted if row.account == account), None))
        for account in accounts
    )
    if rejected:
        warnings.append(
            "rejected rows are listed below; a snapshot with any rejected row will not "
            "replace stored holdings"
        )
    if not accepted:
        warnings.append("no valid rows found")
    return ImportPreview(
        kind="positions",
        path=path,
        file_hash=content_hash(text),
        options=options,
        positions=tuple(accepted),
        rejected=tuple(rejected),
        warnings=tuple(warnings),
        accounts=accounts,
        statement_dates=dates,
    )


def preview_prices(
    text: str,
    *,
    options: CsvOptions | None = None,
    path: str | None = None,
    mapping: Mapping[str, str] | None = None,
) -> ImportPreview:
    options = options or CsvOptions()
    headers, rows = _read_rows(text, options)
    resolved = dict(mapping or options.mapping or guess_mapping(headers))
    if "instrument_id" not in resolved or "price" not in resolved:
        raise ParseError("missing required columns: instrument_id, price")
    accepted: list[PriceRow] = []
    rejected: list[RejectedRow] = []
    for index, row in enumerate(rows, start=2):
        try:
            instrument = _cell(row, resolved, "instrument_id")
            if not instrument:
                raise ParseError("missing instrument identifier")
            multiplier_raw = _cell(row, resolved, "price_unit_multiplier")
            accepted.append(
                PriceRow(
                    instrument_id=instrument,
                    currency=_cell(row, resolved, "currency") or "EUR",
                    price=parse_decimal(_cell(row, resolved, "price"), options),
                    price_unit_multiplier=(
                        parse_decimal(multiplier_raw, options) if multiplier_raw else Decimal(1)
                    ),
                    as_of=parse_date(_cell(row, resolved, "as_of"), options),
                )
            )
        except ParseError as error:
            rejected.append(RejectedRow(index=index, reason=str(error), raw=row))
    return ImportPreview(
        kind="prices",
        path=path,
        file_hash=content_hash(text),
        options=options,
        prices=tuple(accepted),
        rejected=tuple(rejected),
        warnings=(
            "prices carry currency and price_unit_multiplier; a GBp quote is not a GBP quote",
        ),
    )


def preview_fx(
    text: str,
    *,
    options: CsvOptions | None = None,
    path: str | None = None,
    mapping: Mapping[str, str] | None = None,
) -> ImportPreview:
    options = options or CsvOptions()
    headers, rows = _read_rows(text, options)
    resolved = dict(mapping or options.mapping or guess_mapping(headers))
    if "rate" not in resolved:
        raise ParseError("missing required column: rate")
    accepted: list[FxRow] = []
    rejected: list[RejectedRow] = []
    for index, row in enumerate(rows, start=2):
        try:
            base = (_cell(row, resolved, "base_currency") or "").upper()
            quote = (_cell(row, resolved, "quote_currency") or "").upper()
            if not base or not quote:
                raise ParseError("base_currency and quote_currency are both required")
            if base == quote:
                raise ParseError("a directed rate needs two different currencies")
            rate = parse_decimal(_cell(row, resolved, "rate"), options)
            if rate <= 0:
                raise ParseError("a rate must be positive")
            accepted.append(
                FxRow(
                    base_currency=base,
                    quote_currency=quote,
                    rate=rate,
                    as_of=parse_date(_cell(row, resolved, "as_of"), options),
                )
            )
        except ParseError as error:
            rejected.append(RejectedRow(index=index, reason=str(error), raw=row))
    return ImportPreview(
        kind="fx",
        path=path,
        file_hash=content_hash(text),
        options=options,
        rates=tuple(accepted),
        rejected=tuple(rejected),
        warnings=("rates are directed: EUR/PLN and PLN/EUR are separate records",),
    )


# --------------------------------------------------------------------------- apply


async def apply_preview(
    database: Database,
    preview: ImportPreview,
    *,
    at: datetime,
    allow_older: bool = False,
    allow_empty: bool = False,
    force: bool = False,
    account: str | None = None,
) -> list[ApplyResult]:
    """Commit a previewed import. Positions replace per account, atomically.

    An explicit ``account`` is required to replace an account with an empty snapshot:
    the preview of a header-only export cannot know which account it belonged to.
    """
    from .repo import upsert_quote, upsert_rate
    from .valuation import Quote

    results: list[ApplyResult] = []
    if preview.kind == "positions" or preview.positions:
        targets = tuple(dict.fromkeys(item for item in (account, *preview.accounts) if item))
        for target in targets:
            rows = [row for row in preview.positions if row.account == target]
            positions = tuple(
                Position(
                    account=row.account,
                    instrument_id=row.instrument_id,
                    quantity=row.quantity,
                    currency=row.currency or row.cost_basis_currency or "UNKNOWN",
                    isin=row.isin,
                    venue=row.venue,
                    cost_basis_total=row.cost_basis_total,
                    cost_basis_currency=row.cost_basis_currency,
                    as_of=row.as_of,
                    source="statement",
                )
                for row in rows
            )
            statement_date = next(
                (when for name, when in preview.statement_dates if name == target), None
            )
            result = await apply_positions_snapshot(
                database,
                account=target,
                positions=positions,
                import_id=new_import_id(),
                kind=preview.kind,
                path=preview.path,
                file_hash=preview.file_hash,
                statement_date=statement_date,
                rows_rejected=len(preview.rejected),
                warnings=preview.warnings,
                mapping=dict(preview.options.mapping),
                at=at,
                allow_older=allow_older,
                allow_empty=allow_empty,
                force=force,
            )
            results.append(result)
            if result.skipped:
                return results
            reported = [row for row in rows if row.reported_market_value is not None]
            for row in reported:
                assert row.reported_market_value is not None
                await database.execute(
                    """
                    INSERT OR REPLACE INTO reported_values(account, instrument_id, value,
                                                           currency, as_of, import_id)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        target,
                        row.instrument_id,
                        format(row.reported_market_value, "f"),
                        row.currency or "UNKNOWN",
                        (row.as_of or statement_date or at.date()).isoformat(),
                        result.import_id,
                    ),
                )
    if preview.prices:
        from .repo import upsert_quote

        for row in preview.prices:
            await upsert_quote(
                database,
                quote=Quote(
                    instrument_id=row.instrument_id,
                    currency=row.currency,
                    price=row.price,
                    price_unit_multiplier=row.price_unit_multiplier,
                    as_of=row.as_of,
                    fetched_at=at,
                    source="import",
                    quote_type="statement",
                ),
                at=at,
            )
    if preview.rates:
        for rate in preview.rates:
            await upsert_rate(
                database,
                base=rate.base_currency,
                quote=rate.quote_currency,
                rate=rate.rate,
                as_of=rate.as_of,
                source="import",
                at=at,
            )
    return results
