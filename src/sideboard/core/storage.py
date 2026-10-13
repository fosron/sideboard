"""Shared persistence.

One SQLite database, WAL mode, owner-only permissions, **one serialized write
path** (:meth:`Database.transaction`). Feature modules own namespaced repository
functions; UI code never issues SQL.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Self

import aiosqlite

from .contracts import (
    Connectivity,
    Coverage,
    Migration,
    Precision,
    SourceEnvelope,
    SourcePayload,
    freshness_for,
    to_jsonable,
)
from .errors import ErrorCategory, SourceErrorInfo, StorageError

__all__ = ["Database", "connection_factory", "ensure_private_dir"]

CORE_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="core_state",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                owner TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS source_state (
                source_id TEXT PRIMARY KEY,
                payload_schema TEXT NOT NULL,
                payload_schema_version INTEGER NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT,
                connectivity TEXT NOT NULL,
                freshness TEXT NOT NULL,
                last_attempt_at TEXT,
                last_success_at TEXT,
                fetched_at TEXT,
                as_of TEXT,
                precision TEXT NOT NULL DEFAULT 'unknown',
                valid_until TEXT,
                stale_after_seconds REAL,
                content_hash TEXT,
                validator_etag TEXT,
                validator_last_modified TEXT,
                provenance TEXT NOT NULL DEFAULT '',
                coverage_json TEXT,
                error_json TEXT,
                retention_seconds INTEGER NOT NULL DEFAULT 604800,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS items (
                item_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                subtitle TEXT,
                url TEXT,
                category TEXT,
                published_at TEXT,
                meta_json TEXT,
                privacy TEXT NOT NULL DEFAULT 'public',
                fetched_at TEXT NOT NULL,
                retention_until TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS items_source ON items(source_id, published_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS item_state (
                item_id TEXT PRIMARY KEY,
                saved INTEGER NOT NULL DEFAULT 0,
                read INTEGER NOT NULL DEFAULT 0,
                dismissed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS saved_links (
                item_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT,
                saved_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS metric_samples (
                source_id TEXT NOT NULL,
                metric_id TEXT NOT NULL,
                ts REAL NOT NULL,
                value REAL NOT NULL,
                PRIMARY KEY (source_id, metric_id, ts)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT,
                event_date TEXT,
                date_kind TEXT NOT NULL DEFAULT 'unknown',
                source_id TEXT,
                notes TEXT,
                privacy TEXT NOT NULL DEFAULT 'public',
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
)

_MIGRATION_TABLE = "schema_migrations"


def ensure_private_dir(path: Path) -> Path:
    """Create a directory with owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def ensure_private_file(path: Path) -> None:
    """Tighten permissions on a private file.

    Synchronous on purpose: every caller runs it through ``asyncio.to_thread`` so no
    blocking filesystem call happens on the event loop.
    """
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def resolve_target(path: Path | str) -> Path:
    """Expand a user-supplied database path. Also called through a worker thread."""
    return Path(path).expanduser()


@dataclass(slots=True)
class Database:
    """Async SQLite handle with a single serialized write path."""

    path: Path
    _connection: aiosqlite.Connection | None = None
    _write_lock: asyncio.Lock | None = None
    _read_lock: asyncio.Lock | None = None
    _closed: bool = False

    @classmethod
    async def open(cls, path: Path | str, *, create: bool = True) -> Self:
        target = await asyncio.to_thread(resolve_target, path)
        if str(target) != ":memory:":
            # Filesystem work stays off the event loop; opening happens once.
            await asyncio.to_thread(ensure_private_dir, target.parent)
            exists = await asyncio.to_thread(target.exists)
            if not create and not exists:
                raise StorageError("database file does not exist")
        database = cls(path=target)
        await database._connect()
        return database

    async def _connect(self) -> None:
        try:
            connection = await aiosqlite.connect(str(self.path))
        except sqlite3.Error as exc:
            raise StorageError(f"could not open database: {exc}") from exc
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA synchronous=NORMAL")
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute("PRAGMA busy_timeout=5000")
        self._connection = connection
        self._write_lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        if str(self.path) != ":memory:":
            # The chmod runs on a worker thread; it happens once per process.
            await asyncio.to_thread(ensure_private_file, self.path)

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise StorageError("database is not open")
        return self._connection

    async def initialize(self, migrations: Iterable[Migration]) -> list[Migration]:
        """Apply pending migrations in order. Returns the migrations applied."""
        ordered = sorted(migrations, key=lambda m: m.version)
        seen: dict[int, str] = {}
        for migration in ordered:
            existing = seen.get(migration.version)
            if existing is not None and existing != migration.name:
                raise StorageError(
                    f"conflicting migrations for version {migration.version}: "
                    f"{existing!r} vs {migration.name!r}"
                )
            seen[migration.version] = migration.name
        await self.execute(
            f"CREATE TABLE IF NOT EXISTS {_MIGRATION_TABLE} ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, owner TEXT NOT NULL, "
            "applied_at TEXT NOT NULL)"
        )
        row = await self.fetchone(f"SELECT MAX(version) AS v FROM {_MIGRATION_TABLE}")
        current = int(row["v"]) if row and row["v"] is not None else 0
        applied: list[Migration] = []
        for migration in ordered:
            if migration.version <= current:
                continue
            async with self.transaction() as cursor:
                for statement in migration.statements:
                    await cursor.execute(statement)
                await cursor.execute(
                    f"INSERT OR REPLACE INTO {_MIGRATION_TABLE}(version, name, owner, applied_at) "
                    "VALUES (?, ?, ?, datetime('now'))",
                    (migration.version, migration.name, migration.owner),
                )
            applied.append(migration)
        return applied

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Cursor]:
        """Exclusive write scope. Commits on success, rolls back on any exception."""
        assert self._write_lock is not None
        async with self._write_lock:
            connection = self.connection
            cursor = await connection.cursor()
            try:
                await connection.execute("BEGIN IMMEDIATE")
                yield cursor
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()
            finally:
                await cursor.close()

    async def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> None:
        async with self.transaction() as cursor:
            await cursor.execute(sql, params)

    async def fetchall(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[aiosqlite.Row]:
        assert self._read_lock is not None
        async with self._read_lock:
            cursor = await self.connection.execute(sql, params)
            try:
                return list(await cursor.fetchall())
            finally:
                await cursor.close()

    async def fetchone(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> aiosqlite.Row | None:
        rows = await self.fetchall(sql, params)
        return rows[0] if rows else None

    async def scalars(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[Any]:
        return [row[0] for row in await self.fetchall(sql, params)]

    # ---------------------------------------------------------------- envelopes

    async def save_envelope(
        self,
        envelope: SourceEnvelope,
        *,
        updated_at: str,
        retention_seconds: int = 604800,
    ) -> None:
        payload_json = (
            json.dumps(to_jsonable(envelope.payload), separators=(",", ":"))
            if envelope.payload is not None
            else None
        )
        await self.execute(
            """
            INSERT INTO source_state (
                source_id, payload_schema, payload_schema_version, generation, payload_json,
                connectivity, freshness, last_attempt_at, last_success_at, fetched_at, as_of,
                precision, valid_until, stale_after_seconds, content_hash, validator_etag,
                validator_last_modified, provenance, coverage_json, error_json,
                retention_seconds, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                payload_schema=excluded.payload_schema,
                payload_schema_version=excluded.payload_schema_version,
                generation=excluded.generation,
                payload_json=excluded.payload_json,
                connectivity=excluded.connectivity,
                freshness=excluded.freshness,
                last_attempt_at=excluded.last_attempt_at,
                last_success_at=excluded.last_success_at,
                fetched_at=excluded.fetched_at,
                as_of=excluded.as_of,
                precision=excluded.precision,
                valid_until=excluded.valid_until,
                stale_after_seconds=excluded.stale_after_seconds,
                content_hash=excluded.content_hash,
                validator_etag=excluded.validator_etag,
                validator_last_modified=excluded.validator_last_modified,
                provenance=excluded.provenance,
                coverage_json=excluded.coverage_json,
                error_json=excluded.error_json,
                retention_seconds=excluded.retention_seconds,
                updated_at=excluded.updated_at
            """,
            (
                envelope.source_id,
                envelope.payload_schema,
                envelope.payload_schema_version,
                envelope.generation,
                payload_json,
                str(envelope.connectivity),
                str(envelope.freshness),
                _iso(envelope.last_attempt_at),
                _iso(envelope.last_success_at),
                _iso(envelope.fetched_at),
                _iso(envelope.as_of),
                str(envelope.precision),
                _iso(envelope.valid_until),
                envelope.stale_after_seconds,
                envelope.content_hash,
                envelope.validator_etag,
                envelope.validator_last_modified,
                envelope.provenance,
                json.dumps(_coverage_dict(envelope.coverage)),
                json.dumps(envelope.error.to_json()) if envelope.error else None,
                retention_seconds,
                updated_at,
            ),
        )

    async def delete_envelope(self, source_id: str) -> None:
        """Provider-specific purge: removes cached data without touching saved items."""
        await self.execute("DELETE FROM source_state WHERE source_id = ?", (source_id,))

    async def load_envelopes(
        self,
        models: Mapping[str, type[SourcePayload] | None],
        *,
        now: datetime,
        retention: Mapping[str, int] | None = None,
    ) -> dict[str, SourceEnvelope]:
        """Load cached envelopes for the configured source instances only."""
        if not models:
            return {}
        out: dict[str, SourceEnvelope] = {}
        for source_id, payload_model in models.items():
            row = await self.fetchone(
                "SELECT * FROM source_state WHERE source_id = ?", (source_id,)
            )
            if row is None:
                continue
            limit = (retention or {}).get(source_id)
            if limit is not None:
                fetched = _parse_dt(row["fetched_at"])
                if fetched is not None and (now - fetched).total_seconds() > limit:
                    continue
            envelope, schema_ok = row_to_envelope(row, payload_model=payload_model, now=now)
            if not schema_ok and envelope.has_payload:
                envelope = replace(envelope, payload=None, error=None)
            out[source_id] = envelope
        return out

    async def purge_source(self, source_id: str) -> None:
        """Purge cached payload, unsaved items and metric history for one source."""
        async with self.transaction() as cursor:
            await cursor.execute("DELETE FROM source_state WHERE source_id = ?", (source_id,))
            await cursor.execute("DELETE FROM metric_samples WHERE source_id = ?", (source_id,))
            await cursor.execute(
                "DELETE FROM items WHERE source_id = ? AND item_id NOT IN "
                "(SELECT item_id FROM item_state WHERE saved = 1)",
                (source_id,),
            )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._connection is not None:
            with contextlib.suppress(sqlite3.Error):
                await self._connection.commit()
            await self._connection.close()
            self._connection = None


def connection_factory(path: Path) -> Callable[[], Any]:  # pragma: no cover - compat shim
    """Return a no-arg callable creating a blocking connection (used by tooling)."""

    def _open() -> sqlite3.Connection:
        return sqlite3.connect(str(path))

    return _open


def _coverage_dict(coverage: Any) -> dict[str, Any]:
    if hasattr(coverage, "model_dump"):
        return coverage.model_dump(mode="json")  # type: ignore[no-any-return]
    return {}


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_coverage(raw: Any) -> Coverage:
    if not raw:
        return Coverage()
    try:
        return Coverage.model_validate_json(raw)
    except Exception:
        return Coverage()


def row_to_envelope(
    row: Any,
    *,
    payload_model: type[Any] | None,
    now: datetime,
) -> tuple[SourceEnvelope, bool]:
    """Rebuild an envelope from a stored row, recomputing freshness.

    Returns ``(envelope, schema_ok)``. A payload whose stored schema id/version does
    not match the registered spec is dropped rather than interpreted with the wrong
    model: "unknown cached payload versions must not be interpreted with the wrong
    model".
    """
    payload: SourcePayload | None = None
    schema_ok = True
    raw_payload = row["payload_json"]
    if raw_payload:
        if payload_model is None:
            schema_ok = False
        else:
            try:
                payload = payload_model.model_validate_json(raw_payload)
            except Exception:
                schema_ok = False
                payload = None

    envelope = SourceEnvelope(
        source_id=row["source_id"],
        payload_schema=row["payload_schema"],
        payload_schema_version=int(row["payload_schema_version"]),
        generation=int(row["generation"]),
        payload=payload,
        connectivity=Connectivity(row["connectivity"]),
        last_attempt_at=_parse_dt(row["last_attempt_at"]),
        last_success_at=_parse_dt(row["last_success_at"]),
        fetched_at=_parse_dt(row["fetched_at"]),
        as_of=_parse_dt(row["as_of"]),
        precision=Precision(row["precision"]),
        valid_until=_parse_dt(row["valid_until"]),
        stale_after_seconds=row["stale_after_seconds"],
        content_hash=row["content_hash"],
        validator_etag=row["validator_etag"],
        validator_last_modified=row["validator_last_modified"],
        provenance=row["provenance"] or "",
        coverage=_parse_coverage(row["coverage_json"]),
        error=_parse_error(row["error_json"]),
    )
    envelope = replace(envelope, freshness=freshness_for(envelope, now))
    return envelope, schema_ok


def _parse_error(raw: Any) -> SourceErrorInfo | None:
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except TypeError, ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    return SourceErrorInfo(
        category=ErrorCategory(str(decoded.get("category", "internal"))),
        message=str(decoded.get("message", ""))[:300],
        retry_after_seconds=decoded.get("retry_after_seconds"),
        http_status=decoded.get("http_status"),
        at=decoded.get("at"),
        detail=dict(decoded.get("detail") or {}),
    )


def _iso(moment: Any) -> str | None:
    if isinstance(moment, datetime):
        return moment.isoformat()
    if moment is None:
        return None
    return str(moment)
