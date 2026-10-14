"""Local git repositories read with the existing ``git`` CLI, read-only.

Two fixed calls per repository: ``git status --porcelain=v2 --branch`` and one
``git log -1``. Nothing is fetched, staged, committed, checked out or cleaned; the
index is not even refreshed (``--no-optional-locks``). A repository that is missing,
renamed or not a checkout is reported as such and does not fail the others.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
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
from ...core.errors import UnavailableError
from .config import GitSettings
from .models import GitPayload, RepoStatus

log = logging.getLogger("sideboard.git")

_FIELD_SEPARATOR = "\x1f"
_STATUS_ARGS = ("--porcelain=v2", "--branch", "--untracked-files=normal")
_LOG_FORMAT = f"%cI{_FIELD_SEPARATOR}%h{_FIELD_SEPARATOR}%s"


@dataclass(frozen=True, slots=True)
class StatusFields:
    """The parts of porcelain v2 this feature keeps."""

    branch: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    changed: int = 0
    untracked: int = 0
    unmerged: int = 0


def parse_status(stdout: str) -> StatusFields:
    """Read porcelain v2 headers and count entries.

    Only counts and the branch state are kept: which file changed is a debugging detail
    that grows with the repository, and a dashboard row cannot show it anyway.
    """
    branch: str | None = None
    upstream: str | None = None
    ahead = 0
    behind = 0
    changed = 0
    untracked = 0
    unmerged = 0
    for line in stdout.splitlines():
        if line.startswith("# branch.head "):
            name = line[len("# branch.head ") :].strip()
            branch = None if name in {"(detached)", ""} else name
        elif line.startswith("# branch.upstream "):
            upstream = line[len("# branch.upstream ") :].strip() or None
        elif line.startswith("# branch.ab "):
            parts = line[len("# branch.ab ") :].split()
            for part in parts:
                if part.startswith("+") and part[1:].isdigit():
                    ahead = int(part[1:])
                elif part.startswith("-") and part[1:].isdigit():
                    behind = int(part[1:])
        elif line.startswith(("1 ", "2 ")):
            changed += 1
        elif line.startswith("? "):
            untracked += 1
        elif line.startswith("u "):
            unmerged += 1
    return StatusFields(
        branch=branch,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        changed=changed,
        untracked=untracked,
        unmerged=unmerged,
    )


def parse_log(stdout: str) -> tuple[datetime | None, str | None, str | None]:
    """``commit ISO date <US> short sha <US> subject`` from one ``git log -1``."""
    line = stdout.strip().splitlines()[0] if stdout.strip() else ""
    if not line:
        return None, None, None
    parts = line.split(_FIELD_SEPARATOR)
    when: datetime | None = None
    if parts and parts[0]:
        try:
            when = datetime.fromisoformat(parts[0])
        except ValueError:
            when = None
    sha = parts[1] if len(parts) > 1 else None
    subject = parts[2] if len(parts) > 2 else None
    return when, sha, subject


def _validate_path(raw: str) -> str:
    """Refuse anything that is not a plain directory path.

    ``git -C <value>`` puts the value where git expects a path, so a value starting with
    a dash would be read as an option; a value that is not a directory is reported to the
    user instead of being passed on.
    """
    path = raw.strip()
    if not path:
        raise ValueError("empty repository path")
    if path.startswith("-"):
        raise ValueError(f"repository path may not start with a dash: {raw!r}")
    return path


class GitProvider:
    """Reads branch state and the last commit for each configured repository."""

    def __init__(self, settings: GitSettings) -> None:
        self.settings = settings

    async def aclose(self) -> None:
        """No owned resources; safe to call more than once."""
        return

    async def fetch(self, context: ProviderBuild) -> GitPayload:
        commands = context.commands
        if commands is None:
            raise UnavailableError("this collector requires the approved CLI capability")
        settings = context.settings
        assert isinstance(settings, GitSettings)
        if not settings.repos:
            raise UnavailableError(
                "no repositories configured: set sources.git.settings.repos",
                state="unconfigured",
            )
        now = context.clock.now()
        statuses: list[RepoStatus] = []
        missing: list[str] = []
        for raw in settings.repos:
            path = _validate_path(raw)

            name = os.path.basename(path) or path  # noqa: PTH119 - no pathlib in async
            # git itself answers "is this a checkout": asking the filesystem would be a
            # blocking call in an async path and would miss worktrees and submodules,
            # where .git is a file rather than a directory.
            status = await commands.run(
                (
                    "git",
                    "--no-optional-locks",
                    "-C",
                    path,
                    "status",
                    *_STATUS_ARGS,
                ),
                timeout=settings.timeout_seconds,
            )
            if not status.ok:
                missing.append(path)
                lowered = status.stderr.lower()
                note = (
                    "not a git checkout"
                    if "not a git repository" in lowered
                    else f"git status failed: {_first_line(status.stderr)}"
                )
                statuses.append(RepoStatus(path=path, name=name, note=note))
                continue
            log_result = await commands.run(
                (
                    "git",
                    "--no-optional-locks",
                    "-C",
                    path,
                    "log",
                    "-1",
                    f"--format={_LOG_FORMAT}",
                ),
                timeout=settings.timeout_seconds,
            )
            fields = parse_status(status.stdout)
            committed_at, sha, subject = (
                parse_log(log_result.stdout) if log_result.ok else (None, None, None)
            )
            statuses.append(
                RepoStatus(
                    path=path,
                    name=name,
                    branch=fields.branch,
                    upstream=fields.upstream,
                    ahead=fields.ahead,
                    behind=fields.behind,
                    changed=fields.changed,
                    untracked=fields.untracked,
                    unmerged=fields.unmerged,
                    last_commit_at=committed_at,
                    last_commit_sha=sha,
                    last_commit_subject=subject,
                )
            )
        notes: list[str] = []
        if missing:
            notes.append("no data for: " + ", ".join(missing))
        return GitPayload(
            as_of=now,
            precision=Precision.INSTANT,
            valid_until=now + timedelta(seconds=max(60.0, settings.valid_seconds)),
            provenance="git-cli:status,log",
            coverage=Coverage(
                expected=len(settings.repos),
                observed=len(settings.repos) - len(missing),
                missing=tuple(missing),
                notes=f"{sum(1 for s in statuses if s.dirty)} with local changes",
            ),
            repos=tuple(statuses),
            sampled_at=now,
            notes=tuple(notes),
        )


def _first_line(value: str) -> str:
    text = " ".join(value.split())
    return text[:120]


def metrics_of(payload: SourcePayload) -> tuple[MetricReading, ...]:
    if not isinstance(payload, GitPayload):
        return ()
    dirty = sum(1 for repo in payload.repos if repo.dirty)
    unpushed = sum(repo.ahead for repo in payload.repos)
    return (
        MetricReading("git_dirty", float(dirty), "", "dirty repositories"),
        MetricReading("git_unpushed", float(unpushed), "", "unpushed commits"),
    )


def make_spec() -> SourceSpec:
    return SourceSpec(
        type_id="git.repositories",
        payload_model=GitPayload,
        settings_model=GitSettings,
        payload_schema="git.repositories",
        payload_schema_version=1,
        factory=lambda build: GitProvider(_settings_of(build)),
        capabilities=frozenset({Capability.CLI}),
        cadence=Cadence(active_seconds=120, ambient_seconds=300, background_seconds=900),
        retention_seconds=24 * 3600,
        description="Branch state and last commit for local git checkouts, via the git CLI.",
        requires_setup=True,
        setup_hint=(
            "List local checkout paths in sources.git.settings.repos. Read-only: status "
            "and log only, never fetch, pull, commit or clean."
        ),
        metrics_extractor=metrics_of,
        demo_builder=demo_payload,
        setup_check=lambda settings: bool(settings.repos),
    )


def _settings_of(build: ProviderBuild) -> GitSettings:
    settings = build.settings
    assert isinstance(settings, GitSettings)
    return settings


def demo_payload() -> SourcePayload:
    """Synthetic checkouts for ``--demo``. No CLI is executed."""
    now = datetime.now(UTC)
    repos = (
        RepoStatus(
            path="/Users/demo/Documents/repos/demo-app",
            name="demo-app",
            branch="feature/wbs-1129-location-settlement-population",
            upstream="origin/feature/wbs-1129-location-settlement-population",
            ahead=2,
            behind=0,
            changed=3,
            untracked=1,
            last_commit_at=now - timedelta(minutes=40),
            last_commit_sha="a1b2c3d",
            last_commit_subject="WBS-1129: populate settlement locations",
        ),
        RepoStatus(
            path="/Users/demo/Documents/repos/demo-services",
            name="demo-services",
            branch="main",
            upstream="origin/main",
            behind=4,
            last_commit_at=now - timedelta(hours=9),
            last_commit_sha="d4e5f60",
            last_commit_subject="Bump demo-app image tag",
        ),
        RepoStatus(
            path="/Users/demo/Documents/repos/demo-infra",
            name="demo-infra",
            branch="main",
            upstream="origin/main",
            last_commit_at=now - timedelta(days=2),
            last_commit_sha="1122334",
            last_commit_subject="Pin provider versions",
        ),
    )
    return GitPayload(
        as_of=now,
        valid_until=now + timedelta(minutes=10),
        provenance="demo",
        coverage=Coverage(expected=3, observed=3, notes="3 checkouts"),
        repos=repos,
        sampled_at=now,
        notes=("demo data: synthetic repositories, no git CLI was executed",),
    )
