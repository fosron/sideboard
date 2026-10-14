"""GitHub Actions status through the existing ``gh`` CLI, read-only.

``gh`` keeps its own credential in the system keyring, so this source needs no new
secret: it inherits whatever ``gh auth login`` already established. Every call is a
fixed argument array, no call mutates anything (no run cancel, rerun, delete or
dispatch), and the response is a bounded JSON document.

The one command is a GraphQL query with an alias per configured repository, so six
repositories cost one child process rather than twelve.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from ...core.contracts import (
    Cadence,
    Capability,
    Coverage,
    MetricReading,
    Precision,
    ProviderBuild,
    SourcePayload,
    SourceSpec,
)
from ...core.errors import ParseError, RateLimitedError, SideboardError, UnavailableError
from .config import GithubActionsSettings
from .models import GithubActionsPayload, RunReading

log = logging.getLogger("sideboard.github")


def runs_path(repo: str, limit: int) -> str:
    """The REST path for one repository's newest runs.

    REST, not GraphQL: the GraphQL ``Repository`` type has no Actions field, so a
    ``workflowRuns`` selection fails with "Field 'workflowRuns' doesn't exist on type
    'Repository'". One call per repository, newest first, bounded by ``per_page``.
    """
    return f"/repos/{repo}/actions/runs?per_page={limit}"


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse_repo_runs(payload: object, repo: str, limit: int) -> tuple[RunReading, ...]:
    """Read one repository's run list. A malformed entry is skipped, not fatal."""
    if not isinstance(payload, dict):
        raise ParseError("GitHub returned no object for a repository")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise ParseError("GitHub response had no workflow_runs list")
    out: list[RunReading] = []
    for raw in runs[:limit]:
        if not isinstance(raw, dict):
            continue
        out.append(
            RunReading(
                repo=repo,
                run_id=int(raw.get("id") or 0),
                workflow=str(raw.get("name") or "unnamed workflow"),
                status=str(raw.get("status") or "unknown").lower(),
                conclusion=(str(raw["conclusion"]).lower() if raw.get("conclusion") else None),
                branch=(str(raw["head_branch"]) if raw.get("head_branch") else None),
                event=(str(raw["event"]) if raw.get("event") else None),
                created_at=parse_timestamp(raw.get("created_at")),
                updated_at=parse_timestamp(raw.get("updated_at")),
                url=(str(raw["html_url"]) if raw.get("html_url") else None),
            )
        )
    return tuple(out)


def classify_failure(stderr: str, returncode: int) -> SideboardError:
    """Map a failed ``gh`` call to something the user can act on."""
    lowered = stderr.lower()
    if "gh auth login" in lowered or "not logged in" in lowered or "authentication" in lowered:
        return UnavailableError(
            "the GitHub CLI is not authenticated: run `gh auth login`", state="unauthenticated"
        )
    if "rate limit" in lowered:
        return RateLimitedError("GitHub rate limit reached; the card keeps the last good data")
    if "could not resolve to a repository" in lowered or "http 404" in lowered:
        return UnavailableError("a configured repository was not found", state="repo_missing")
    detail = " ".join(_control_free(stderr).split())[:120]
    message = f"gh api graphql failed with exit code {returncode}"
    if detail:
        message = f"{message}: {detail}"
    return UnavailableError(message, state="cli_error")


def _control_free(value: str) -> str:
    return "".join(char for char in value if char.isprintable() or char == " ")


class GithubActionsProvider:
    """Reads workflow runs for a fixed list of repositories."""

    def __init__(self, settings: GithubActionsSettings) -> None:
        self.settings = settings

    async def aclose(self) -> None:
        """No owned resources; safe to call more than once."""
        return

    async def fetch(self, context: ProviderBuild) -> GithubActionsPayload:
        commands = context.commands
        if commands is None:
            raise UnavailableError("this collector requires the approved CLI capability")
        settings = context.settings
        assert isinstance(settings, GithubActionsSettings)
        repos = tuple(settings.repos)
        if not repos:
            raise UnavailableError(
                "no repositories configured: set sources.github.settings.repos",
                state="unconfigured",
            )
        now = context.clock.now()
        runs: list[RunReading] = []
        missing: list[str] = []
        notes: list[str] = []
        for repo in repos:
            result = await commands.run(
                ("gh", "api", runs_path(repo, settings.run_limit)),
                timeout=settings.timeout_seconds,
            )
            if not result.ok:
                failure = classify_failure(result.stderr, result.returncode)
                if isinstance(failure, (UnavailableError, RateLimitedError)) and failure.detail.get(
                    "state"
                ) in {"unauthenticated", "cli_error"}:
                    # Auth and CLI problems affect every repository, so they stop the
                    # refresh instead of being reported six times.
                    raise failure
                missing.append(repo)
                notes.append(f"{repo}: {failure.message[:120]}")
                continue
            if result.truncated:
                missing.append(repo)
                notes.append(f"{repo}: output exceeded the configured cap")
                continue
            try:
                parsed = json.loads(result.stdout)
            except ValueError as error:
                missing.append(repo)
                notes.append(f"{repo}: gh did not return JSON ({error})")
                continue
            runs.extend(parse_repo_runs(parsed, repo, settings.run_limit))
        if missing and len(missing) == len(repos):
            raise UnavailableError("; ".join(notes)[:200] or "no repository returned data")
        window = timedelta(hours=settings.failure_hours)
        failures = tuple(
            run
            for run in runs
            if run.failed and run.created_at is not None and now - run.created_at <= window
        )
        running = tuple(run for run in runs if run.running)
        if missing:
            notes.insert(0, "no data for: " + ", ".join(missing))
        return GithubActionsPayload(
            as_of=now,
            precision=Precision.INSTANT,
            valid_until=now + timedelta(seconds=max(60.0, settings.valid_seconds)),
            provenance="gh-cli:api.rest",
            coverage=Coverage(
                expected=len(repos),
                observed=len(repos) - len(missing),
                missing=tuple(missing),
                notes=f"{len(running)} running, {len(failures)} failed recently",
            ),
            repos=repos,
            runs=tuple(runs),
            running=running,
            failures=failures,
            sampled_at=now,
            notes=tuple(notes),
        )


def metrics_of(payload: SourcePayload) -> tuple[MetricReading, ...]:
    if not isinstance(payload, GithubActionsPayload):
        return ()
    return (
        MetricReading("github_running", float(len(payload.running)), "", "running"),
        MetricReading("github_failed", float(len(payload.failures)), "", "failed"),
    )


def make_spec() -> SourceSpec:
    return SourceSpec(
        type_id="github.actions",
        payload_model=GithubActionsPayload,
        settings_model=GithubActionsSettings,
        payload_schema="github.actions",
        payload_schema_version=1,
        factory=lambda build: GithubActionsProvider(_settings_of(build)),
        capabilities=frozenset({Capability.CLI}),
        cadence=Cadence(active_seconds=60, ambient_seconds=90, background_seconds=180),
        retention_seconds=24 * 3600,
        description="GitHub Actions workflow runs for a fixed list of repositories, via gh.",
        requires_setup=True,
        setup_hint=(
            "Authenticate the GitHub CLI (`gh auth login`) and list repositories in "
            "sources.github.settings.repos as owner/name. The token stays in gh's own "
            "keyring; nothing is stored in this configuration."
        ),
        metrics_extractor=metrics_of,
        demo_builder=demo_payload,
        setup_check=lambda settings: bool(settings.repos),
    )


def _settings_of(build: ProviderBuild) -> GithubActionsSettings:
    settings = build.settings
    assert isinstance(settings, GithubActionsSettings)
    return settings


def demo_payload() -> SourcePayload:
    """Synthetic runs for ``--demo``. No CLI is executed."""
    now = datetime.now(UTC)
    runs = (
        RunReading(
            repo="example-org/demo-app",
            run_id=1,
            workflow="Pest",
            status="in_progress",
            conclusion=None,
            branch="feature/wbs-1129-location-settlement-population",
            event="pull_request",
            created_at=now - timedelta(minutes=6),
            updated_at=now - timedelta(minutes=1),
            url="https://github.com/example-org/demo-app/actions/runs/1",
        ),
        RunReading(
            repo="example-org/demo-app",
            run_id=2,
            workflow="Browser Tests",
            status="in_progress",
            conclusion=None,
            branch="feature/wbs-1129-location-settlement-population",
            event="pull_request",
            created_at=now - timedelta(minutes=5),
            updated_at=now,
            url="https://github.com/example-org/demo-app/actions/runs/2",
        ),
        RunReading(
            repo="example-org/demo-services",
            run_id=3,
            workflow="Deploy",
            status="completed",
            conclusion="failure",
            branch="main",
            event="push",
            created_at=now - timedelta(hours=3),
            updated_at=now - timedelta(hours=2),
            url="https://github.com/example-org/demo-services/actions/runs/3",
        ),
        RunReading(
            repo="example-org/demo-infra",
            run_id=4,
            workflow="Terraform plan",
            status="completed",
            conclusion="success",
            branch="main",
            event="push",
            created_at=now - timedelta(hours=8),
            updated_at=now - timedelta(hours=7),
            url="https://github.com/example-org/demo-infra/actions/runs/4",
        ),
    )
    return GithubActionsPayload(
        as_of=now,
        valid_until=now + timedelta(minutes=5),
        provenance="demo",
        coverage=Coverage(expected=4, observed=4, notes="4 runs"),
        repos=(
            "example-org/demo-app",
            "example-org/demo-services",
            "example-org/demo-infra",
            "example-org/demo-gitops",
        ),
        runs=runs,
        running=tuple(run for run in runs if run.running),
        failures=tuple(run for run in runs if run.failed),
        sampled_at=now,
        notes=("demo data: synthetic workflow runs, no gh CLI was executed",),
    )
