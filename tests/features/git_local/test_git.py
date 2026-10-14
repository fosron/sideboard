"""Local git provider and presenter contracts.

The CLI is never executed: canned porcelain v2 and ``git log`` output drives the parsing,
so counting, branch state and the per-repository failure notes are deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sideboard.core.capabilities import CommandResult
from sideboard.core.clock import FakeClock
from sideboard.core.contracts import PresentInput, ProviderBuild, ViewContext, ViewMode
from sideboard.features.git_local.config import GitSettings, GitStatusWidgetSettings
from sideboard.features.git_local.presenter import present_status
from sideboard.features.git_local.provider import GitProvider, demo_payload, parse_log, parse_status

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
SEP = "\x1f"

STATUS_OUTPUT = """# branch.oid a1b2c3d
# branch.head feature/wbs-1129
# branch.upstream origin/feature/wbs-1129
# branch.ab +2 -1
1 M. N... 100644 100644 100644 aaa bbb src/one.php
1 .M N... 100644 100644 100644 ccc ddd src/two.php
2 R. N... 100644 100644 100644 eee fff R100 src/three.php\tsrc/four.php
? notes.txt
? scratch/
u UU N... 100644 100644 100644 100644 ggg hhh iii src/conflict.php
"""

LOG_OUTPUT = f"2026-10-14T11:20:00+00:00{SEP}a1b2c3d{SEP}WBS-1129: populate settlement locations\n"


class FakeRunner:
    """Serves canned output per git subcommand and records every argv."""

    def __init__(self, status: str = STATUS_OUTPUT, log: str = LOG_OUTPUT) -> None:
        self.status = status
        self.log = log
        self.calls: list[tuple[str, ...]] = []

    async def run(self, argv, *, timeout=None, env=None, max_output_bytes=None):
        _ = (timeout, env, max_output_bytes)
        self.calls.append(tuple(argv))
        if "status" in argv:
            return CommandResult(tuple(argv), 0, self.status, "", 0.01)
        if "log" in argv:
            return CommandResult(tuple(argv), 0, self.log, "", 0.01)
        return CommandResult(tuple(argv), 1, "", "unexpected command", 0.0)

    async def aclose(self) -> None:
        return None


class FailingRunner(FakeRunner):
    def __init__(self, stderr: str) -> None:
        super().__init__()
        self.stderr = stderr

    async def run(self, argv, *, timeout=None, env=None, max_output_bytes=None):
        _ = (timeout, env, max_output_bytes)
        self.calls.append(tuple(argv))
        return CommandResult(tuple(argv), 128, "", self.stderr, 0.01)


def test_porcelain_v2_headers_and_counts() -> None:
    fields = parse_status(STATUS_OUTPUT)
    assert fields.branch == "feature/wbs-1129"
    assert fields.upstream == "origin/feature/wbs-1129"
    assert fields.ahead == 2 and fields.behind == 1
    assert fields.changed == 3, "1 and 2 records, including the rename"
    assert fields.untracked == 2
    assert fields.unmerged == 1


def test_detached_head_has_no_branch_and_no_upstream() -> None:
    fields = parse_status("# branch.oid a1b2c3d\n# branch.head (detached)\n")
    assert fields.branch is None
    assert fields.upstream is None
    assert fields.ahead == 0 and fields.behind == 0


def test_log_line_is_split_on_the_unit_separator() -> None:
    when, sha, subject = parse_log(LOG_OUTPUT)
    assert when == datetime(2026, 10, 14, 11, 20, tzinfo=UTC)
    assert sha == "a1b2c3d"
    assert subject == "WBS-1129: populate settlement locations"
    assert parse_log("") == (None, None, None)


def build_provider(runner: FakeRunner, paths: tuple[str, ...]) -> tuple[GitProvider, ProviderBuild]:
    settings = GitSettings(repos=list(paths))
    build = ProviderBuild(
        source_id="git",
        settings=settings,
        clock=FakeClock(NOW),
        commands=runner,  # type: ignore[arg-type]
    )
    return GitProvider(settings=settings), build


async def test_each_repository_costs_two_read_only_calls() -> None:
    runner = FakeRunner()
    provider, build = build_provider(runner, ("/tmp/demo-app",))
    payload = await provider.fetch(build)
    repo = payload.repos[0]
    assert repo.branch == "feature/wbs-1129"
    assert repo.dirty is True
    assert repo.last_commit_subject == "WBS-1129: populate settlement locations"
    assert len(runner.calls) == 2
    for call in runner.calls:
        assert call[:3] == ("git", "--no-optional-locks", "-C")
        assert call[3] == "/tmp/demo-app"
    assert "status" in runner.calls[0] and "log" in runner.calls[1]
    assert payload.coverage.notes == "1 with local changes"


async def test_a_path_that_is_not_a_checkout_is_reported_not_fatal() -> None:
    runner = FailingRunner("fatal: not a git repository (or any of the parent directories): .git\n")
    provider, build = build_provider(runner, ("/tmp/not-a-repo",))
    payload = await provider.fetch(build)
    assert payload.repos[0].note == "not a git checkout"
    assert payload.coverage.missing == ("/tmp/not-a-repo",)
    assert payload.repos[0].dirty is False


async def test_no_paths_configured_is_a_named_state() -> None:
    from sideboard.core.errors import UnavailableError

    runner = FakeRunner()
    provider, build = build_provider(runner, ())
    with pytest.raises(UnavailableError) as info:
        await provider.fetch(build)
    assert info.value.detail.get("state") == "unconfigured"


def test_a_path_may_not_start_with_a_dash() -> None:
    with pytest.raises(ValueError):
        GitSettings(repos=["--upload-pack=touch /tmp/pwned"])
    assert GitSettings(repos=["~/Documents/repos/demo-app"]).repos == [
        f"{__import__('os').path.expanduser('~')}/Documents/repos/demo-app"
    ]


def view_for(payload, mode: ViewMode = ViewMode.COMPACT):
    from sideboard.core.contracts import Connectivity, Freshness, SourceEnvelope

    return present_status(
        PresentInput(
            settings=GitStatusWidgetSettings(),
            inputs={
                "primary": SourceEnvelope(
                    source_id="git",
                    payload_schema="git.repositories",
                    payload_schema_version=1,
                    payload=payload,
                    connectivity=Connectivity.OK,
                    freshness=Freshness.FRESH,
                    fetched_at=NOW,
                )
            },
            context=ViewContext(now=NOW, width=76, height=14, mode=mode),
        )
    )


def test_the_card_puts_uncommitted_work_first() -> None:
    view = view_for(demo_payload())
    checkouts = view.metric("repos")
    dirty = view.metric("dirty")
    behind = view.metric("behind")
    assert checkouts is not None and checkouts.value_text == "3"
    assert dirty is not None and dirty.value_text == "1"
    assert behind is not None and behind.value_text == "1"
    first = view.items[0]
    assert first.badge == "feature/wbs-1129-location-settlement-population"
    assert ("changed", "4") in first.meta, "3 modified + 1 untracked"
    assert ("ahead", "2") in first.meta


def test_hiding_clean_checkouts_keeps_the_counts() -> None:
    from sideboard.core.contracts import Connectivity, Freshness, SourceEnvelope

    view = present_status(
        PresentInput(
            settings=GitStatusWidgetSettings(hide_clean=True),
            inputs={
                "primary": SourceEnvelope(
                    source_id="git",
                    payload_schema="git.repositories",
                    payload_schema_version=1,
                    payload=demo_payload(),
                    connectivity=Connectivity.OK,
                    freshness=Freshness.FRESH,
                    fetched_at=NOW,
                )
            },
            context=ViewContext(now=NOW, width=76, height=14),
        )
    )
    assert len(view.items) == 1, "only the dirty checkout is listed"
    checkouts = view.metric("repos")
    assert checkouts is not None and checkouts.value_text == "3", "the metric counts all of them"


def test_the_detail_view_is_a_table_of_checkouts() -> None:
    view = view_for(demo_payload(), ViewMode.DETAIL)
    assert view.table is not None
    assert [column.id for column in view.table.columns] == [
        "repo",
        "branch",
        "changed",
        "ahead",
        "behind",
        "last",
        "subject",
    ]
    assert "nothing is fetched or staged" in (view.table.footnote or "")
