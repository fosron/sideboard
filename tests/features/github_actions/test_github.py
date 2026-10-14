"""GitHub Actions provider and presenter contracts.

The CLI is never executed: a fake runner returns canned GraphQL JSON, so the parsing,
the coverage reporting and the failure classification are all deterministic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sideboard.core.capabilities import CommandResult
from sideboard.core.clock import FakeClock
from sideboard.core.contracts import PresentInput, ProviderBuild, ViewContext
from sideboard.core.errors import RateLimitedError, UnavailableError
from sideboard.features.github_actions.config import (
    GithubActionsSettings,
    GithubActionsWidgetSettings,
)
from sideboard.features.github_actions.presenter import present_actions
from sideboard.features.github_actions.provider import (
    GithubActionsProvider,
    classify_failure,
    demo_payload,
    parse_repo_runs,
    runs_path,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
REPOS = ("example-org/demo-app", "example-org/demo-services")


class FakeRunner:
    """Returns canned output per repository and records every argv.

    ``per_repo`` matches a repository name inside the request path, so one repository can
    fail while another succeeds — which is the case the provider has to get right.
    """

    def __init__(
        self,
        stdout: str = "",
        *,
        returncode: int = 0,
        stderr: str = "",
        per_repo: dict[str, str] | None = None,
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.per_repo = per_repo or {}
        self.calls: list[tuple[str, ...]] = []

    async def run(self, argv, *, timeout=None, env=None, max_output_bytes=None):
        _ = (timeout, env, max_output_bytes)
        self.calls.append(tuple(argv))
        joined = " ".join(argv)
        for match, payload in self.per_repo.items():
            if match in joined:
                return CommandResult(tuple(argv), 0, payload, "", 0.01)
        return CommandResult(
            argv=tuple(argv),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            duration_seconds=0.01,
        )

    async def aclose(self) -> None:
        return None


def run_node(
    *,
    status: str = "in_progress",
    conclusion: str | None = None,
    workflow: str = "Pest",
    created: str = "2026-10-14T11:30:00Z",
    updated: str = "2026-10-14T11:55:00Z",
    branch: str = "main",
    run_id: int = 1,
) -> dict[str, object]:
    return {
        "id": run_id,
        "name": workflow,
        "status": status,
        "conclusion": conclusion,
        "head_branch": branch,
        "event": "pull_request",
        "created_at": created,
        "updated_at": updated,
        "html_url": f"https://github.com/example/actions/runs/{run_id}",
    }


def response(runs: list[dict[str, object]]) -> str:
    """The REST shape: one page of workflow runs for one repository."""
    return json.dumps({"total_count": len(runs), "workflow_runs": runs})


def build_provider(
    *,
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
    per_repo: dict[str, str] | None = None,
) -> tuple[GithubActionsProvider, FakeRunner, ProviderBuild]:
    settings = GithubActionsSettings(repos=list(REPOS))
    runner = FakeRunner(stdout, returncode=returncode, stderr=stderr, per_repo=per_repo)
    build = ProviderBuild(
        source_id="github",
        settings=settings,
        clock=FakeClock(NOW),
        commands=runner,  # type: ignore[arg-type]
    )
    return GithubActionsProvider(settings=settings), runner, build


def test_the_request_is_one_rest_page_per_repository() -> None:
    """REST, not GraphQL: the GraphQL Repository type has no Actions field.

    A `workflowRuns` selection fails with "Field 'workflowRuns' doesn't exist on type
    'Repository'" — which is how this was found, on the live API.
    """
    assert runs_path(REPOS[0], 12) == "/repos/example-org/demo-app/actions/runs?per_page=12"


def test_running_and_failed_are_derived_from_status_and_conclusion() -> None:
    runs = parse_repo_runs(
        json.loads(
            response(
                [
                    run_node(status="QUEUED", run_id=1),
                    run_node(status="IN_PROGRESS", run_id=2),
                    run_node(status="COMPLETED", conclusion="FAILURE", run_id=3),
                    run_node(status="COMPLETED", conclusion="SUCCESS", run_id=4),
                ]
            )
        ),
        REPOS[0],
        10,
    )
    assert [run.run_id for run in runs if run.running] == [1, 2]
    assert [run.run_id for run in runs if run.failed] == [3]
    assert runs[0].workflow == "Pest"
    assert runs[0].short_repo == "demo-app"


async def test_one_call_per_repository_and_a_failure_is_coverage() -> None:
    """A repository that fails is reported, not fatal: the others still refresh."""
    provider, runner, build = build_provider(stdout=response([run_node()]))
    payload = await provider.fetch(build)
    assert len(runner.calls) == len(REPOS), "one bounded call per repository"
    assert runner.calls[0][:2] == ("gh", "api")
    assert runner.calls[0][2].startswith("/repos/example-org/demo-app/actions/runs")
    assert payload.coverage.observed == 2
    assert payload.coverage.missing == ()
    assert payload.running and payload.running[0].workflow == "Pest"


async def test_a_recent_failure_is_reported_and_an_old_one_is_not() -> None:
    provider, _runner, build = build_provider(
        per_repo={
            REPOS[0]: response(
                [
                    run_node(status="COMPLETED", conclusion="FAILURE", run_id=1),
                    run_node(
                        status="COMPLETED",
                        conclusion="FAILURE",
                        created="2026-10-10T09:00:00Z",
                        run_id=2,
                    ),
                ]
            ),
            REPOS[1]: response([run_node(status="COMPLETED", conclusion="SUCCESS", run_id=9)]),
        }
    )
    payload = await provider.fetch(build)
    assert [run.run_id for run in payload.failures] == [1], "the old failure is history"
    assert len(payload.runs) == 3, "both repositories' runs are kept"


def test_failure_classification_names_what_to_do() -> None:
    assert "gh auth login" in classify_failure("gh: not logged in", 1).message
    assert isinstance(classify_failure("API rate limit exceeded", 1), RateLimitedError)
    missing = classify_failure("Could not resolve to a Repository with the name 'x'.", 1)
    assert isinstance(missing, UnavailableError)
    assert missing.detail.get("state") == "repo_missing"
    generic = classify_failure("boom\nsecond line", 3)
    assert "exit code 3" in generic.message and "boom" in generic.message


def test_settings_refuse_a_repo_that_is_not_owner_slash_name() -> None:
    with pytest.raises(ValueError):
        GithubActionsSettings(repos=["demo-app"])
    with pytest.raises(ValueError):
        GithubActionsSettings(repos=["example-org/demo-app; rm -rf /"])


def view_context(mode: str = "compact") -> ViewContext:
    from sideboard.core.contracts import ViewMode

    return ViewContext(now=NOW, width=76, height=14, mode=ViewMode(mode))


def view_for(payload, mode: str = "compact"):
    from sideboard.core.contracts import Connectivity, Freshness, SourceEnvelope

    return present_actions(
        PresentInput(
            settings=GithubActionsWidgetSettings(),
            inputs={
                "primary": SourceEnvelope(
                    source_id="github",
                    payload_schema="github.actions",
                    payload_schema_version=1,
                    payload=payload,
                    connectivity=Connectivity.OK,
                    freshness=Freshness.FRESH,
                    fetched_at=NOW,
                )
            },
            context=view_context(mode),
        )
    )


def test_the_card_lists_running_runs_before_failures() -> None:
    payload = demo_payload()
    view = view_for(payload)
    running = view.metric("running")
    failed = view.metric("failed")
    assert running is not None and running.value_text == "2"
    assert failed is not None and failed.value_text == "1"
    titles = [item.title for item in view.items]
    assert titles[0].startswith("Pest")
    assert titles[1].startswith("Browser Tests")
    assert "✗" in (view.items[2].badge or ""), "a failure is marked in its badge"


def test_nothing_running_says_so_instead_of_an_empty_list() -> None:
    payload = demo_payload().model_copy(update={"running": (), "failures": (), "runs": ()})
    view = view_for(payload)
    assert view.items == ()
    assert view.status is not None and view.status.headline == "Nothing running"


def test_the_detail_view_is_a_table_of_runs() -> None:
    view = view_for(demo_payload(), mode="detail")
    assert view.table is not None
    assert [column.id for column in view.table.columns] == [
        "workflow",
        "repo",
        "status",
        "branch",
        "event",
        "age",
    ]
    assert "can cancel or retry" in (view.table.footnote or "")
