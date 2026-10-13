"""Presenter tests for the Reddit feature: pure, honest, no I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sideboard.core.contracts import (
    Connectivity,
    Coverage,
    Freshness,
    Precision,
    PresentInput,
    Privacy,
    Severity,
    SourceEnvelope,
    ViewContext,
    ViewMode,
    ViewModel,
)
from sideboard.core.errors import ErrorCategory, SourceErrorInfo
from sideboard.features.reddit.config import RedditListSettings, RedditSettings
from sideboard.features.reddit.models import CommunityReport, RedditPayload, Thread
from sideboard.features.reddit.presenter import (
    RankWeights,
    present,
    rank_threads,
    thread_rank,
)
from sideboard.features.reddit.provider import PROVENANCE_RSS, demo_payload

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def thread(
    thread_id: str,
    *,
    community: str = "selfhosted",
    hours_old: float | None = 1.0,
    score: int | None = None,
    comments: int | None = None,
) -> Thread:
    return Thread(
        id=thread_id,
        community=community,
        title=f"Synthetic {thread_id}",
        url=f"https://www.reddit.com/r/{community}/comments/{thread_id}/synthetic/",
        permalink=f"/r/{community}/comments/{thread_id}/synthetic/",
        author="/u/example_author",
        published=None if hours_old is None else NOW - timedelta(hours=hours_old),
        score=score,
        comment_count=comments,
        summary="Synthetic excerpt.",
    )


def rss_payload(*, with_scores: bool = False) -> RedditPayload:
    threads = (
        thread("t3_a", hours_old=1.0, score=120 if with_scores else None),
        thread("t3_b", community="homelab", hours_old=30.0, comments=8 if with_scores else None),
        thread("t3_c", community="PHP", hours_old=3.0),
        thread("t3_d", hours_old=None),
    )
    return RedditPayload(
        threads=threads,
        community_reports=(
            CommunityReport(
                community="selfhosted", status="ok", note="3 thread(s) in this refresh"
            ),
            CommunityReport(community="homelab", status="ok"),
            CommunityReport(community="PHP", status="ok"),
        ),
        as_of=NOW - timedelta(hours=1),
        precision=Precision.INSTANT,
        valid_until=NOW + timedelta(hours=47),
        provenance=PROVENANCE_RSS,
        coverage=Coverage(expected=3, observed=3),
    )


def envelope(
    payload: RedditPayload | None,
    *,
    connectivity: Connectivity = Connectivity.OK,
    error: SourceErrorInfo | None = None,
    valid_until: datetime | None = None,
) -> SourceEnvelope:
    return SourceEnvelope(
        source_id="reddit_home",
        payload_schema="reddit.threads",
        payload_schema_version=1,
        payload=payload,
        connectivity=connectivity,
        freshness=Freshness.FRESH if payload is not None else Freshness.NONE,
        last_success_at=NOW if payload is not None else None,
        as_of=payload.as_of if payload is not None else None,
        valid_until=valid_until,
        error=error,
    )


def blocked(mode: str) -> SourceErrorInfo:
    return SourceErrorInfo(
        category=ErrorCategory.BLOCKED,
        message="Reddit feed is in link-only mode",
        detail={"mode": mode},
    )


def view_for(
    bound: SourceEnvelope | None,
    *,
    settings: RedditListSettings | None = None,
    mode: ViewMode = ViewMode.COMPACT,
    inputs: dict[str, SourceEnvelope | None] | None = None,
) -> ViewModel:
    return present(
        PresentInput(
            settings=settings or RedditListSettings(),
            inputs=inputs if inputs is not None else {"reddit_home": bound},
            context=ViewContext(mode=mode, now=NOW),
        )
    )


# --------------------------------------------------------------------------- #
# Link-only and disabled modes
# --------------------------------------------------------------------------- #


def test_links_mode_renders_deliberate_community_links() -> None:
    view = view_for(envelope(None, connectivity=Connectivity.BLOCKED, error=blocked("links")))
    assert view.status is not None
    assert view.status.severity is Severity.INFO
    assert view.status.headline == "Link-only mode"
    assert "conditional" in (view.status.detail or "")
    assert any('mode = "rss"' in hint for hint in view.status.hints)
    assert [item.id for item in view.items] == [
        "reddit:link:selfhosted",
        "reddit:link:homelab",
        "reddit:link:PHP",
    ]
    assert view.items[0].url == "https://www.reddit.com/r/selfhosted/"
    assert all(
        item.url is not None and item.url.startswith("https://www.reddit.com/r/")
        for item in view.items
    )
    assert view.footnote == "link-only · no Reddit feed is fetched"
    assert view.privacy is Privacy.PUBLIC


def test_links_mode_without_an_envelope_is_still_deliberate() -> None:
    view = view_for(None)
    assert view.status is not None
    assert view.status.headline == "Source not configured"
    assert [item.id for item in view.items] == [
        "reddit:link:selfhosted",
        "reddit:link:homelab",
        "reddit:link:PHP",
    ]
    assert view.footnote is not None and "link fallback" in view.footnote


def test_links_mode_reads_binding_name_keyed_inputs() -> None:
    bound = envelope(None, connectivity=Connectivity.BLOCKED, error=blocked("links"))
    view = view_for(None, inputs={"primary": bound})
    assert view.status is not None and view.status.headline == "Link-only mode"


def test_disabled_mode_explains_itself_and_shows_no_links() -> None:
    view = view_for(envelope(None, connectivity=Connectivity.BLOCKED, error=blocked("disabled")))
    assert view.status is not None
    assert view.status.headline == "Reddit is disabled"
    assert view.items == ()
    assert view.footnote == "disabled · no request made"


def test_widget_communities_limit_the_link_list() -> None:
    settings = RedditListSettings(communities=["selfhosted", "homelab", "PHP"], max_communities=2)
    view = view_for(None, settings=settings)
    assert [item.id for item in view.items] == ["reddit:link:selfhosted", "reddit:link:homelab"]
    assert view.footnote is not None and "showing 2 communities" in view.footnote


# --------------------------------------------------------------------------- #
# Collected threads
# --------------------------------------------------------------------------- #


def test_compact_view_omits_absent_score_metadata() -> None:
    payload = rss_payload()
    view = view_for(envelope(payload))
    ids = [item.id for item in view.items]
    assert ids == ["reddit:t3_a", "reddit:t3_c", "reddit:t3_b", "reddit:t3_d"]
    assert all(item.meta == () for item in view.items)
    assert all(entry.score is None and entry.comment_count is None for entry in payload.threads)
    assert view.footnote is not None
    assert "public RSS · no scores available" in view.footnote
    assert "4 of 4" in view.footnote
    assert view.items[0].subtitle == "r/selfhosted · u/example_author"
    assert view.items[0].url is not None


def test_meta_shows_scores_only_when_the_source_reports_them() -> None:
    view = view_for(envelope(rss_payload(with_scores=True)))
    by_id = {item.id: item for item in view.items}
    assert by_id["reddit:t3_a"].meta == (("score", "120"),)
    assert by_id["reddit:t3_b"].meta == (("comments", "8"),)
    assert by_id["reddit:t3_c"].meta == ()
    assert view.footnote is not None
    assert "public RSS · no scores available" not in view.footnote


def test_compact_view_honours_max_items() -> None:
    view = view_for(envelope(rss_payload()), settings=RedditListSettings(max_items=2))
    assert [item.id for item in view.items] == ["reddit:t3_a", "reddit:t3_c"]
    assert view.footnote is not None and "2 of 4" in view.footnote


def test_partial_coverage_is_reported_not_hidden() -> None:
    payload = rss_payload().model_copy(
        update={
            "coverage": Coverage(
                expected=2, observed=1, missing=("homelab",), notes="r/homelab: blocked"
            ),
            "community_reports": (
                CommunityReport(community="selfhosted", status="ok"),
                CommunityReport(
                    community="homelab", status="blocked", note="unauthorized: refused"
                ),
            ),
        }
    )
    view = view_for(envelope(payload))
    assert view.status is not None
    assert view.status.severity is Severity.WARNING
    assert view.status.headline == "Partial Reddit coverage"
    assert "homelab" in (view.status.detail or "")
    assert view.footnote is not None and "unavailable: r/homelab" in view.footnote


def test_unavailable_rss_feed_shows_status_and_community_links() -> None:
    error = SourceErrorInfo(
        category=ErrorCategory.UNAUTHORIZED, message="provider refused the request", http_status=403
    )
    view = view_for(envelope(None, connectivity=Connectivity.BLOCKED, error=error))
    assert view.status is not None
    assert view.status.headline == "Reddit feed unavailable"
    assert view.items and all(item.id.startswith("reddit:link:") for item in view.items)
    assert view.footnote is not None and "link fallback" in view.footnote


def test_offline_status_is_used_when_the_host_is_offline() -> None:
    bound = envelope(None, connectivity=Connectivity.OFFLINE)
    view = present(
        PresentInput(
            settings=RedditListSettings(),
            inputs={"reddit_home": bound},
            context=ViewContext(mode=ViewMode.COMPACT, now=NOW, offline=True),
        )
    )
    assert view.status is not None
    assert view.status.headline == "Offline"
    assert view.items


def test_wrong_payload_type_degrades_honestly() -> None:
    bound = envelope(None)
    view = present(
        PresentInput(
            settings=RedditListSettings(),
            inputs={"reddit_home": bound},
            context=ViewContext(now=NOW),
        )
    )
    assert view.status is not None
    assert view.items and all(item.id.startswith("reddit:link:") for item in view.items)


def test_demo_payload_renders_without_scores_claims() -> None:
    view = view_for(envelope(demo_payload()))
    assert view.items
    assert all(item.meta == () for item in view.items)
    assert view.footnote is not None
    assert "no scores from this source" in view.footnote


# --------------------------------------------------------------------------- #
# Detail mode
# --------------------------------------------------------------------------- #


def test_detail_view_groups_threads_and_reports_feeds() -> None:
    view = view_for(envelope(rss_payload()), mode=ViewMode.DETAIL)
    assert view.table is not None
    table = view.table
    assert [column.id for column in table.columns] == ["community", "thread", "published"]
    group_names = [name for name, _ in table.groups]
    assert group_names == ["r/selfhosted", "r/homelab", "r/PHP", "Feeds"]
    feed_rows = dict(table.groups)["Feeds"]
    assert "reddit:feed:selfhosted" in feed_rows
    assert table.footnote is not None
    assert "public RSS · no scores available" in table.footnote
    assert "feeds: r/selfhosted ok" in table.footnote
    assert table.truncated is False


def test_detail_view_adds_score_columns_only_when_present() -> None:
    view = view_for(envelope(rss_payload(with_scores=True)), mode=ViewMode.DETAIL)
    assert view.table is not None
    assert [column.id for column in view.table.columns] == [
        "community",
        "thread",
        "published",
        "score",
        "comments",
    ]
    rows = {row.id: row for row in view.table.rows}
    assert rows["reddit:t3_a"].cells["score"] == "120"
    assert rows["reddit:t3_c"].cells["score"] == "—"


def test_detail_view_is_bounded_and_marks_truncation() -> None:
    payload = rss_payload().model_copy(
        update={"threads": tuple(thread(f"t3_{index}") for index in range(80))}
    )
    view = view_for(envelope(payload), mode=ViewMode.DETAIL)
    assert view.table is not None
    assert view.table.truncated is True
    thread_rows = [rows for name, rows in view.table.groups if name != "Feeds"]
    assert sum(len(rows) for rows in thread_rows) == 60
    assert view.table.footnote is not None and "60 of 80 threads" in view.table.footnote


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


def test_rank_threads_prefers_fresher_threads() -> None:
    older = thread("old", hours_old=20)
    newer = thread("new", hours_old=1)
    ranked = rank_threads((older, newer), now=NOW, weights=RankWeights())
    assert [entry.id for entry in ranked] == ["new", "old"]


def test_rank_threads_honours_configured_community_weights() -> None:
    older = thread("older_homelab", community="homelab", hours_old=20)
    newer = thread("newer_selfhosted", community="selfhosted", hours_old=1)
    weights = RankWeights(community_weights={"homelab": 5.0})
    ranked = rank_threads((older, newer), now=NOW, weights=weights)
    assert [entry.id for entry in ranked] == ["older_homelab", "newer_selfhosted"]


def test_rank_threads_treats_missing_metadata_honestly() -> None:
    undated = thread("undated", hours_old=None)
    dated = thread("dated", hours_old=48)
    ranked = rank_threads((undated, dated), now=NOW, weights=RankWeights())
    assert [entry.id for entry in ranked] == ["dated", "undated"]

    base = thread("scored", hours_old=1)
    weights = RankWeights()
    # A reported score of 0 and an absent score must rank identically: absent is not
    # a strong signal, but it is not a fabricated weak one either.
    assert thread_rank(base, now=NOW, weights=weights) == thread_rank(
        base.model_copy(update={"score": 0}), now=NOW, weights=weights
    )
    scored = base.model_copy(update={"score": 10_000})
    assert thread_rank(scored, now=NOW, weights=weights) > thread_rank(
        base, now=NOW, weights=weights
    )


def test_rank_threads_is_stable_for_equal_ranks() -> None:
    first = thread("first", hours_old=4)
    second = thread("second", hours_old=4)
    ranked = rank_threads((first, second), now=NOW, weights=RankWeights())
    assert [entry.id for entry in ranked] == ["first", "second"]


def test_unknown_thread_communities_are_listed_in_detail() -> None:
    payload = rss_payload().model_copy(
        update={
            "community_reports": (),
            "threads": (thread("t3_only", community="eupersonalfinance"),),
        }
    )
    view = view_for(envelope(payload), mode=ViewMode.DETAIL)
    assert view.table is not None
    assert [name for name, _ in view.table.groups] == ["r/eupersonalfinance"]


def test_source_and_widget_settings_stay_separate() -> None:
    # The source decides what may be fetched; the widget only decides how it looks.
    assert RedditSettings().communities == RedditListSettings().communities
