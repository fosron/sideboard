"""Reading-item state, retention/purge and bounded metric history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sideboard.core.history import BoundedHistory, downsample
from sideboard.core.items import ItemRecord, ItemStore
from sideboard.core.storage import CORE_MIGRATIONS, Database

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


async def _store(tmp_path: Path) -> tuple[ItemStore, Database]:
    database = await Database.open(tmp_path / "db.sqlite3")
    await database.initialize(CORE_MIGRATIONS)
    return ItemStore(database), database


def record(
    item_id: str = "news:1", *, source_id: str = "news", published: datetime | None = NOW
) -> ItemRecord:
    return ItemRecord(
        item_id=item_id,
        source_id=source_id,
        kind="article",
        title="A headline",
        url="https://example.com/a",
        category="world",
        published_at=published,
    )


async def test_read_and_saved_state_is_local_and_stable(tmp_path: Path) -> None:
    store, database = await _store(tmp_path)
    try:
        await store.upsert("news", "article", [record()], now=NOW)
        state = await store.set_state("news:1", saved=True, now=NOW)
        assert state.saved is True and state.read is False
        state = await store.set_state("news:1", read=True, now=NOW)
        assert state.saved is True and state.read is True
        assert await store.ids_with_state("saved") == frozenset({"news:1"})
        # Re-listing the same item must not clear the user's state.
        await store.upsert("news", "article", [record()], now=NOW + timedelta(hours=1))
        assert (await store.states(["news:1"]))["news:1"].saved is True
    finally:
        await database.aclose()


async def test_expired_provider_content_is_purged_but_a_saved_link_survives(
    tmp_path: Path,
) -> None:
    store, database = await _store(tmp_path)
    try:
        await store.upsert(
            "reddit", "thread", [record("reddit:1", published=NOW)], now=NOW, retention_seconds=3600
        )
        await store.upsert(
            "reddit", "thread", [record("reddit:2", published=NOW)], now=NOW, retention_seconds=3600
        )
        await store.set_state("reddit:1", saved=True, now=NOW)
        removed = await store.purge_expired(NOW + timedelta(hours=2))
        assert removed == 1
        ids = [row.item_id for row in await store.saved_items()]
        # The user's link is retained, without keeping an indefinite cached copy.
        assert "reddit:1" in ids
        assert await store.item_metadata("reddit:2") is None
    finally:
        await database.aclose()


async def test_provider_purge_leaves_saved_items_alone(tmp_path: Path) -> None:
    store, database = await _store(tmp_path)
    try:
        await store.upsert("news", "article", [record("news:1")], now=NOW)
        await store.upsert("youtube", "upload", [record("yt:1", source_id="youtube")], now=NOW)
        await store.set_state("news:1", saved=True, now=NOW)
        await store.purge_source("youtube")
        assert await store.item_metadata("yt:1") is None
        assert await store.item_metadata("news:1") is not None
    finally:
        await database.aclose()


def test_downsample_preserves_gaps() -> None:
    from sideboard.core.contracts import SeriesPoint

    points = [SeriesPoint(t=float(index), value=float(index)) for index in range(100)]
    points[40] = SeriesPoint(t=40.0, value=None)
    reduced = downsample(points, 10)
    assert len(reduced) <= 11
    assert any(point.value is None for point in reduced)


async def test_history_is_bounded_and_marks_gaps(tmp_path: Path) -> None:
    database = await Database.open(tmp_path / "db.sqlite3")
    await database.initialize(CORE_MIGRATIONS)
    try:
        history = BoundedHistory(database=database, in_memory_seconds=10.0)
        history.record("mac", "cpu_percent", 1000.0, 10.0, unit="%")
        history.record("mac", "cpu_percent", 1005.0, 20.0, unit="%")
        # A long gap must be recorded as a gap, not as zero load.
        history.record("mac", "cpu_percent", 2000.0, 30.0, unit="%")
        series = history.ring("mac", "cpu_percent").to_series(max_points=10)
        assert any(point.value is None for point in series.points)
        assert len(history.ring("mac", "cpu_percent").points) <= 10 + 1
        # One aggregate per minute: the middle sample is throttled away.
        flushed = await history.flush()
        assert flushed == 2
        assert history.pending == 0
    finally:
        await database.aclose()


async def test_history_aggregate_is_throttled_to_one_per_minute(tmp_path: Path) -> None:
    database = await Database.open(tmp_path / "db.sqlite3")
    await database.initialize(CORE_MIGRATIONS)
    try:
        history = BoundedHistory(database=database, aggregate_seconds=60)
        for index in range(10):
            history.record("mac", "cpu_percent", 1000.0 + index * 5, 1.0)
        assert history.pending == 1
        await history.flush()
        rows = await database.fetchall("SELECT COUNT(*) AS n FROM metric_samples")
        assert rows[0]["n"] == 1
    finally:
        await database.aclose()


async def test_saved_items_merge_live_items_and_bookmarks(tmp_path: Path) -> None:
    """Both stores contribute: a recent save must not hide older bookmarks.

    A bookmark row is written for every saved item that passes retention, precisely so the
    link survives if the item row ever goes away. ``saved_items()`` returned the items rows
    *instead of* the bookmarks whenever any live saved item existed, so those links were
    invisible from the moment one new item was saved.
    """
    store, database = await _store(tmp_path)
    try:
        await store.upsert(
            "reddit",
            "thread",
            [record("reddit:old", source_id="reddit", published=NOW)],
            now=NOW,
            retention_seconds=3600,
        )
        await store.set_state("reddit:old", saved=True, now=NOW)
        # Passing retention writes the bookmark and keeps the row, as the purge does.
        removed = await store.purge_expired(NOW + timedelta(hours=2))
        assert removed == 0
        assert [row.item_id for row in await store.saved_items()] == ["reddit:old"]

        later = NOW + timedelta(hours=3)
        await store.upsert("news", "article", [record("news:new", published=later)], now=later)
        await store.set_state("news:new", saved=True, now=later)
        # The older item's row goes away (any cleanup path may do this); its bookmark stays.
        await database.execute("DELETE FROM items WHERE item_id = ?", ("reddit:old",))

        saved = await store.saved_items()
        ids = [row.item_id for row in saved]
        assert set(ids) == {"reddit:old", "news:new"}, ids
        assert ids[0] == "news:new", "newest first"
        live = next(row for row in saved if row.item_id == "news:new")
        bookmark = next(row for row in saved if row.item_id == "reddit:old")
        assert live.category == "world"
        assert bookmark.kind == "bookmark" and bookmark.url == "https://example.com/a"
    finally:
        await database.aclose()
