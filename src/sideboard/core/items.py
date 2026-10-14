"""Reading items: bounded metadata, local read/saved state, retention and purge.

Only minimal metadata is stored. Expired provider content is removed **even while
offline**; a locally saved bookmark keeps the user's link and title in
``saved_links`` rather than retaining an indefinite cached copy of provider content.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .contracts import Item, ItemState, Privacy
from .storage import Database

__all__ = ["ItemRecord", "ItemStore", "privacy_for"]

DEFAULT_RETENTION_SECONDS = 48 * 3600


@dataclass(frozen=True, slots=True)
class ItemRecord:
    """Minimal stored metadata for one item."""

    item_id: str
    source_id: str
    kind: str
    title: str
    subtitle: str | None = None
    url: str | None = None
    category: str | None = None
    published_at: datetime | None = None
    meta: tuple[tuple[str, str], ...] = ()
    privacy: Privacy = Privacy.PUBLIC

    def to_item(self, state: ItemState | None = None) -> Item:
        return Item(
            id=self.item_id,
            title=self.title,
            subtitle=self.subtitle,
            url=self.url,
            category=self.category,
            published_at=self.published_at,
            meta=self.meta,
            privacy=self.privacy,
            source_id=self.source_id,
            saved=bool(state and state.saved),
            read=bool(state and state.read),
        )


def privacy_for(value: bool) -> Privacy:
    return Privacy.PRIVATE if value else Privacy.PUBLIC


class ItemStore:
    """Namespaced repository for reading items and their local state."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert(
        self,
        source_id: str,
        kind: str,
        records: Sequence[ItemRecord],
        *,
        now: datetime,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
    ) -> int:
        """Insert or refresh items. Never removes local read/saved state."""
        import json

        written = 0
        async with self.database.transaction() as cursor:
            for record in records:
                published = record.published_at or now
                retention_until = (published + timedelta(seconds=retention_seconds)).isoformat()
                await cursor.execute(
                    """
                    INSERT INTO items (item_id, source_id, kind, title, subtitle, url, category,
                                       published_at, meta_json, privacy, fetched_at, retention_until)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_id) DO UPDATE SET
                        title=excluded.title,
                        subtitle=excluded.subtitle,
                        url=excluded.url,
                        category=excluded.category,
                        published_at=excluded.published_at,
                        meta_json=excluded.meta_json,
                        privacy=excluded.privacy,
                        fetched_at=excluded.fetched_at,
                        retention_until=excluded.retention_until
                    """,
                    (
                        record.item_id,
                        record.source_id,
                        kind,
                        record.title,
                        record.subtitle,
                        record.url,
                        record.category,
                        published.isoformat() if record.published_at else None,
                        json.dumps(dict(record.meta)) if record.meta else None,
                        str(record.privacy),
                        now.isoformat(),
                        retention_until,
                    ),
                )
                written += 1
        return written

    async def states(self, item_ids: Iterable[str]) -> Mapping[str, ItemState]:
        ids = list(dict.fromkeys(item_ids))
        if not ids:
            return {}
        out: dict[str, ItemState] = {}
        chunk = 400
        for start in range(0, len(ids), chunk):
            window = ids[start : start + chunk]
            placeholders = ",".join("?" for _ in window)
            rows = await self.database.fetchall(
                f"SELECT item_id, saved, read, dismissed FROM item_state "
                f"WHERE item_id IN ({placeholders})",
                window,
            )
            for row in rows:
                out[str(row["item_id"])] = ItemState(
                    saved=bool(row["saved"]),
                    read=bool(row["read"]),
                    dismissed=bool(row["dismissed"]),
                )
        return out

    async def ids_with_state(self, flag: str) -> frozenset[str]:
        if flag not in {"saved", "read", "dismissed"}:
            raise ValueError("unsupported state flag")
        rows = await self.database.fetchall(f"SELECT item_id FROM item_state WHERE {flag} = 1")
        return frozenset(str(row["item_id"]) for row in rows)

    async def set_state(
        self,
        item_id: str,
        *,
        saved: bool | None = None,
        read: bool | None = None,
        dismissed: bool | None = None,
        now: datetime,
    ) -> ItemState:
        current = (await self.states([item_id])).get(item_id, ItemState())
        updated = ItemState(
            saved=current.saved if saved is None else saved,
            read=current.read if read is None else read,
            dismissed=current.dismissed if dismissed is None else dismissed,
        )
        await self.database.execute(
            """
            INSERT INTO item_state (item_id, saved, read, dismissed, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                saved=excluded.saved, read=excluded.read,
                dismissed=excluded.dismissed, updated_at=excluded.updated_at
            """,
            (
                item_id,
                int(updated.saved),
                int(updated.read),
                int(updated.dismissed),
                now.isoformat(),
            ),
        )
        return updated

    async def saved_items(self, *, limit: int = 100) -> tuple[ItemRecord, ...]:
        """Every saved link, from both stores.

        Provider items carry their metadata until retention removes them; a bookmark keeps
        the link and title afterwards. Merging the two is the only way the list is
        complete: returning the items rows *instead of* the bookmarks hid five older
        bookmarks as soon as one recent saved item existed in the items table.
        """
        rows = await self.database.fetchall(
            """
            SELECT i.item_id, i.source_id, i.kind, i.title, i.subtitle, i.url, i.category,
                   i.published_at, i.meta_json, i.privacy
            FROM items i JOIN item_state s ON s.item_id = i.item_id
            WHERE s.saved = 1
            ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
            LIMIT ?
            """,
            (limit,),
        )
        merged: dict[str, ItemRecord] = {str(row["item_id"]): _row_to_record(row) for row in rows}
        bookmark_rows = await self.database.fetchall(
            "SELECT item_id, source_id, title, url, saved_at FROM saved_links "
            "ORDER BY saved_at DESC LIMIT ?",
            (limit,),
        )
        for row in bookmark_rows:
            # A live item wins: it has the subtitle, category and publication date. The
            # bookmark is the fallback for content retention has already removed.
            merged.setdefault(
                str(row["item_id"]),
                ItemRecord(
                    item_id=str(row["item_id"]),
                    source_id=str(row["source_id"]),
                    kind="bookmark",
                    title=str(row["title"]),
                    url=row["url"],
                    published_at=_parse(str(row["saved_at"])),
                ),
            )
        ordered = sorted(
            merged.values(),
            key=lambda record: (
                -(record.published_at.timestamp() if record.published_at is not None else 0.0)
            ),
        )
        return tuple(ordered[:limit])

    async def purge_expired(self, now: datetime) -> int:
        """Delete provider content past retention, preserving saved links.

        Runs offline as well: retention is a local contract, not a network one.
        """
        stamp = now.isoformat()
        async with self.database.transaction() as cursor:
            await cursor.execute(
                """
                INSERT OR REPLACE INTO saved_links (item_id, source_id, title, url, saved_at)
                SELECT i.item_id, i.source_id, i.title, i.url, ?
                FROM items i JOIN item_state s ON s.item_id = i.item_id
                WHERE s.saved = 1 AND i.retention_until IS NOT NULL AND i.retention_until <= ?
                """,
                (stamp, stamp),
            )
            await cursor.execute(
                """
                DELETE FROM items
                WHERE retention_until IS NOT NULL AND retention_until <= ?
                  AND item_id NOT IN (SELECT item_id FROM item_state WHERE saved = 1)
                """,
                (stamp,),
            )
            return cursor.rowcount or 0

    async def purge_source(self, source_id: str) -> int:
        """Provider-specific purge: removes cached items, keeps unrelated saved items."""
        async with self.database.transaction() as cursor:
            await cursor.execute(
                "INSERT OR REPLACE INTO saved_links (item_id, source_id, title, url, saved_at) "
                "SELECT i.item_id, i.source_id, i.title, i.url, COALESCE(i.published_at, i.fetched_at) "
                "FROM items i JOIN item_state s ON s.item_id = i.item_id "
                "WHERE i.source_id = ? AND s.saved = 1",
                (source_id,),
            )
            await cursor.execute(
                "DELETE FROM items WHERE source_id = ? AND item_id NOT IN "
                "(SELECT item_id FROM item_state WHERE saved = 1)",
                (source_id,),
            )
            return cursor.rowcount or 0

    async def item_metadata(self, item_id: str) -> ItemRecord | None:
        row = await self.database.fetchone("SELECT * FROM items WHERE item_id = ?", (item_id,))
        return _row_to_record(row) if row is not None else None


def _row_to_record(row: object) -> ItemRecord:
    import json

    meta: tuple[tuple[str, str], ...] = ()
    raw_meta = row["meta_json"]  # type: ignore[index]
    if raw_meta:
        try:
            decoded = json.loads(str(raw_meta))
            if isinstance(decoded, dict):
                meta = tuple((str(k), str(v)) for k, v in decoded.items())
        except ValueError:
            meta = ()
    return ItemRecord(
        item_id=str(row["item_id"]),  # type: ignore[index]
        source_id=str(row["source_id"]),  # type: ignore[index]
        kind=str(row["kind"]),  # type: ignore[index]
        title=str(row["title"]),  # type: ignore[index]
        subtitle=row["subtitle"],  # type: ignore[index]
        url=row["url"],  # type: ignore[index]
        category=row["category"],  # type: ignore[index]
        published_at=_parse(row["published_at"]),  # type: ignore[index]
        meta=meta,
        privacy=Privacy(str(row["privacy"])),  # type: ignore[index]
    )


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
