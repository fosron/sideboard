"""Docker read-only adapter tests: argv policy, unit parsing, failure states, no mutation.

Everything runs against a fake command runner, so no Docker socket, daemon or CLI is
needed and no container can be touched by the test suite.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from sideboard.core.capabilities import CommandResult
from sideboard.core.clock import FakeClock
from sideboard.core.contracts import ProviderBuild
from sideboard.core.errors import ParseError, UnavailableError
from sideboard.features.docker_local.commands import (
    HEALTH_TEMPLATE,
    INVENTORY_TEMPLATE,
    SAFE_ENV_KEYS,
    build_argv,
    classify_cli_failure,
    deny_remote_endpoint,
    docker_env,
    parse_context_endpoint,
    parse_health_line,
    parse_inventory_line,
)
from sideboard.features.docker_local.config import DockerSettings
from sideboard.features.docker_local.models import DockerPayload
from sideboard.features.docker_local.presenter import health_label, present_summary, present_table
from sideboard.features.docker_local.provider import DockerProvider
from sideboard.features.docker_local.units import (
    parse_cpu_percent,
    parse_io_pair,
    parse_size,
    parse_size_pair,
)


class FakeRunner:
    """Returns canned output per subcommand and records every argv."""

    def __init__(self, responses: dict[str, str], *, error: dict[str, str] | None = None) -> None:
        self.responses = responses
        self.error = error or {}
        self.calls: list[tuple[str, ...]] = []

    async def run(self, argv, *, timeout=None, env=None, max_output_bytes=None):
        _ = (timeout, max_output_bytes, env)
        self.calls.append(tuple(argv))
        key = " ".join(argv[1:4])
        for match, message in self.error.items():
            if match in " ".join(argv):
                return CommandResult(
                    argv=tuple(argv),
                    returncode=1,
                    stdout="",
                    stderr=message,
                    duration_seconds=0.01,
                )
        for match, payload in self.responses.items():
            if match in " ".join(argv):
                return CommandResult(
                    argv=tuple(argv),
                    returncode=0,
                    stdout=payload,
                    stderr="",
                    duration_seconds=0.01,
                )
        return CommandResult(
            argv=tuple(argv),
            returncode=1,
            stdout="",
            stderr=f"no fixture for {key}",
            duration_seconds=0.0,
        )

    async def aclose(self) -> None:
        return None


def endpoint_payload(name: str = "orbstack", endpoint: str = "unix:///tmp/docker.sock") -> str:
    return json.dumps({"name": name, "endpoints": {"docker": endpoint}})


def inventory_line(**overrides: str) -> str:
    row = {
        "id": "abc123",
        "name": "sideboard-db-1",
        "image": "postgres:17",
        "state": "running",
        "status": "Up 3 hours (healthy)",
        "project": "sideboard",
        "service": "db",
    }
    row.update(overrides)
    return json.dumps(row)


def stats_line(**overrides: str) -> str:
    row = {
        "Container": "abc123",
        "Name": "sideboard-db-1",
        "CPUPerc": "124.50%",
        "MemUsage": "220MiB / 1GiB",
        "MemPerc": "21.48%",
        "NetIO": "64MB / 40MB",
        "BlockIO": "10MB / 2MB",
        "PIDs": "9",
    }
    row.update(overrides)
    return json.dumps(row)


def health_line(**overrides: object) -> str:
    row = {
        "id": "abc123",
        "restarts": 2,
        "status": "running",
        "exit": 0,
        "started": "2026-10-13T09:00:00Z",
        "health": "",
    }
    row.update(overrides)
    return json.dumps(row)


# --------------------------------------------------------------------------- argv/env


def test_argv_always_pins_the_context() -> None:
    assert build_argv("orbstack", "container", "ls") == [
        "docker",
        "--context",
        "orbstack",
        "container",
        "ls",
    ]


def test_child_environment_drops_redirects() -> None:
    env = docker_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/Users/example",
            "DOCKER_HOST": "tcp://remote:2375",
            "DOCKER_CONTEXT": "other",
            "DOCKER_TLS_VERIFY": "1",
            "DOCKER_CERT_PATH": "/tmp/certs",
        }
    )
    assert "DOCKER_HOST" not in env
    assert "DOCKER_CONTEXT" not in env
    assert "DOCKER_TLS_VERIFY" not in env
    assert env["PATH"] == "/usr/bin"
    assert set(env) <= set(SAFE_ENV_KEYS)


def test_remote_endpoint_is_refused() -> None:
    remote = parse_context_endpoint(
        json.dumps({"name": "prod", "endpoints": {"docker": "ssh://user@host"}})
    )
    assert remote.is_local is False
    with pytest.raises(UnavailableError):
        deny_remote_endpoint(remote)
    tcp = parse_context_endpoint(
        json.dumps({"name": "tcp", "endpoints": {"docker": "tcp://1.2.3.4:2375"}})
    )
    with pytest.raises(UnavailableError):
        deny_remote_endpoint(tcp)


def test_local_unix_endpoint_is_accepted() -> None:
    local = parse_context_endpoint(endpoint_payload())
    assert local.is_local is True
    deny_remote_endpoint(local)


def test_context_name_cannot_be_an_endpoint() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DockerSettings(context="ssh://user@host")
    with pytest.raises(ValidationError):
        DockerSettings(context="unix:///var/run/docker.sock")


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Cannot connect to the Docker daemon at unix:///x", "engine_stopped"),
        ("permission denied while trying to connect", "access_denied"),
        ('context "nope" does not exist', "context_missing"),
    ],
)
def test_cli_failures_map_to_distinct_states(stderr: str, expected: str) -> None:
    error = classify_cli_failure(stderr, 1)
    assert error.detail.get("state") == expected


# --------------------------------------------------------------------------- units


def test_size_units_are_distinct() -> None:
    assert parse_size("1kB") == 1000
    assert parse_size("1KiB") == 1024
    assert parse_size("1GiB") == 1024**3
    assert parse_size("0B") == 0
    assert parse_size("1.5GiB") == int(1.5 * 1024**3)
    assert parse_size("") is None
    assert parse_size("N/A") is None
    with pytest.raises(ParseError):
        parse_size("1 quatloos")


def test_size_pair_and_io_pair() -> None:
    assert parse_size_pair("220MiB / 1GiB") == (220 * 1024**2, 1024**3)
    assert parse_io_pair("1.2kB / 3.4kB") == (1200, 3400)
    assert parse_size_pair(None) == (None, None)


def test_cpu_percent_preserves_multicore_scale() -> None:
    assert parse_cpu_percent("124.50%") == 124.5
    assert parse_cpu_percent("0.00%") == 0.0
    assert parse_cpu_percent("") is None
    with pytest.raises(ParseError):
        parse_cpu_percent("busy")


def test_inventory_and_health_lines_skip_malformed_rows() -> None:
    assert parse_inventory_line("not json") is None
    assert parse_inventory_line("[1,2]") is None
    row = parse_inventory_line(inventory_line())
    assert row is not None and row["project"] == "sideboard"
    parsed = parse_health_line(health_line())
    assert parsed is not None
    assert parsed.restarts == 2
    assert parse_health_line("{oops") is None


# --------------------------------------------------------------------------- provider


def build_provider(
    *,
    responses: dict[str, str] | None = None,
    error: dict[str, str] | None = None,
    settings: DockerSettings | None = None,
    clock: FakeClock | None = None,
) -> tuple[DockerProvider, FakeRunner, ProviderBuild]:
    settings = settings or DockerSettings(context="orbstack", expected_running=["sideboard-db-1"])
    runner = FakeRunner(
        responses={
            "context inspect": endpoint_payload(),
            "info --format": json.dumps(
                {"name": "orbstack", "version": "29.8.2", "ncpu": 8, "os": "OrbStack"}
            ),
            **(responses or {}),
        },
        error=error,
    )
    provider = DockerProvider(settings=settings)
    clock = clock or FakeClock(datetime(2026, 10, 13, 12, 0, tzinfo=UTC))
    build = ProviderBuild(source_id="docker", settings=settings, clock=clock, commands=runner)  # type: ignore[arg-type]
    return provider, runner, build


async def test_missing_context_is_a_named_state() -> None:
    provider, _runner, build = build_provider(settings=DockerSettings(context=""))
    with pytest.raises(UnavailableError) as info:
        await provider.fetch(build)
    assert info.value.detail.get("state") == "no_context"


async def test_no_engine_is_reported_not_faked() -> None:
    provider, _runner, build = build_provider(
        error={"context inspect": "Cannot connect to the Docker daemon"}
    )
    with pytest.raises(UnavailableError) as info:
        await provider.fetch(build)
    assert info.value.detail.get("state") == "engine_stopped"


async def test_empty_inventory_is_a_valid_state() -> None:
    provider, _runner, build = build_provider(
        responses={"container ls": "", "container stats": "", "container inspect": ""}
    )
    payload = await provider.fetch(build)
    assert isinstance(payload, DockerPayload)
    assert payload.containers == ()
    assert payload.engine.is_local is True
    assert payload.engine.cpu_count == 8


async def test_stats_and_metadata_are_folded_in_without_clipping_cpu() -> None:
    clock = FakeClock(datetime(2026, 10, 13, 12, 0, tzinfo=UTC))
    provider, runner, build = build_provider(
        responses={
            "container ls": inventory_line(),
            "container stats": stats_line(),
            "container inspect": health_line(),
        },
        clock=clock,
    )
    first = await provider.fetch(build)
    container = first.containers[0]
    assert container.cpu_percent == 124.5
    assert container.cpu_percent_of_engine == pytest.approx(15.56, abs=0.01)
    assert container.memory_used_bytes == 220 * 1024**2
    assert container.memory_limit_bytes == 1024**3
    assert container.health is None
    assert container.health_defined is False
    assert container.restarts == 2
    assert container.rate_gap is True  # first sample: no baseline yet

    clock.advance(15)
    runner.responses["container stats"] = stats_line(**{"NetIO": "74MB / 49MB"})
    second = await provider.fetch(build)
    container = second.containers[0]
    assert container.net_rx_bps == pytest.approx((74 * 1000**2 - 64 * 1000**2) / 15)

    # Exactly one batched stats call per refresh, never one per container.
    stats_calls = [call for call in runner.calls if "stats" in call]
    assert len(stats_calls) == 2
    assert stats_calls[0].count("abc123") == 1


async def test_running_container_without_a_healthcheck_is_health_unknown() -> None:
    provider, _runner, build = build_provider(
        responses={
            "container ls": inventory_line(),
            "container stats": stats_line(),
            "container inspect": health_line(),
        }
    )
    payload = await provider.fetch(build)
    container = payload.containers[0]
    assert container.running is True
    assert container.health is None
    assert health_label(container) == "unknown"


async def test_health_status_is_read_when_defined() -> None:
    provider, _runner, build = build_provider(
        responses={
            "container ls": inventory_line(),
            "container stats": stats_line(),
            "container inspect": health_line(health="unhealthy"),
        }
    )
    payload = await provider.fetch(build)
    assert payload.containers[0].health == "unhealthy"
    assert payload.containers[0].health_defined is True


async def test_deliberate_stop_is_not_an_incident_unless_expected() -> None:
    stopped = inventory_line(
        id="zzz", name="demo-batch-1", state="exited", status="Exited (0) 2 hours ago"
    )
    provider, _runner, build = build_provider(
        responses={
            "container ls": stopped,
            "container stats": "",
            "container inspect": health_line(id="zzz", status="exited", exit=0),
        }
    )
    payload = await provider.fetch(build)
    container = payload.containers[0]
    assert container.is_unexpected_exit is True
    # The container name is not in expected_running, so no attention note is raised.
    assert not any("expected running" in note for note in payload.notes)


async def test_expected_running_service_stop_is_reported() -> None:
    stopped = inventory_line(name="sideboard-db-1", state="exited", status="Exited (1) 5 min ago")
    provider, _runner, build = build_provider(
        responses={
            "container ls": stopped,
            "container stats": "",
            "container inspect": health_line(status="exited", exit=1),
        }
    )
    payload = await provider.fetch(build)
    assert any("expected running but stopped" in note for note in payload.notes)


async def test_recreated_container_resets_the_rate_baseline() -> None:
    clock = FakeClock(datetime(2026, 10, 13, 12, 0, tzinfo=UTC))
    provider, runner, build = build_provider(
        responses={
            "container ls": inventory_line(id="new1"),
            "container stats": stats_line(Container="new1", NetIO="1MB / 1MB"),
            "container inspect": health_line(id="new1"),
        },
        clock=clock,
    )
    first = await provider.fetch(build)
    assert first.containers[0].rate_gap is True
    clock.advance(15)
    runner.responses["container stats"] = stats_line(Container="new1", NetIO="3MB / 2MB")
    second = await provider.fetch(build)
    assert second.containers[0].rate_gap is False
    assert second.containers[0].net_rx_bps == pytest.approx((3 * 1000**2 - 1 * 1000**2) / 15)


async def test_inventory_truncation_is_disclosed() -> None:
    lines = "\n".join(inventory_line(id=f"id{index}", name=f"c{index}") for index in range(5))
    provider, _runner, build = build_provider(
        responses={"container ls": lines, "container stats": "", "container inspect": ""},
        settings=DockerSettings(context="orbstack", inventory_limit=2, expected_running=[]),
    )
    payload = await provider.fetch(build)
    assert len(payload.containers) == 2
    assert payload.truncated is True
    assert any("truncated" in note for note in payload.notes)


def test_no_mutating_operation_is_ever_built() -> None:
    """The adapter may only build read-only argv arrays."""
    import pathlib

    source = pathlib.Path("src/sideboard/features/docker_local/provider.py").read_text(
        encoding="utf-8"
    )
    forbidden = (
        "container start",
        "container stop",
        "container restart",
        "container kill",
        "container rm",
        "container exec",
        "container logs",
        "container run",
        "image pull",
        "image rm",
        "system prune",
        "volume rm",
        "context use",
        "context rm",
        "compose up",
        "compose down",
    )
    for command in forbidden:
        assert command not in source, f"mutating command {command!r} appears in the adapter"


def test_only_read_only_subcommands_are_used_at_runtime() -> None:
    import asyncio

    provider, runner, build = build_provider(
        responses={
            "container ls": inventory_line(),
            "container stats": stats_line(),
            "container inspect": health_line(),
        }
    )
    asyncio.run(provider.fetch(build))
    allowed = {
        ("context", "inspect"),
        ("container", "ls"),
        ("container", "stats"),
        ("container", "inspect"),
        ("info", "--format"),
        ("system", "df"),
    }
    for call in runner.calls:
        tail = call[3:]
        assert any(tail[:2] == prefix for prefix in allowed), f"unexpected operation: {call}"


# --------------------------------------------------------------------------- presenter


def summary_settings():
    from sideboard.features.docker_local.config import DockerSummarySettings

    return DockerSummarySettings()


def table_settings():
    from sideboard.features.docker_local.config import DockerTableSettings

    return DockerTableSettings()


def view_context(runtime=None):
    from sideboard.core.contracts import ViewContext

    return ViewContext(now=datetime(2026, 10, 13, 12, 0, tzinfo=UTC), runtime=runtime)


def envelope_for(payload):
    from sideboard.core.contracts import Connectivity, Freshness, SourceEnvelope

    return SourceEnvelope(
        source_id="docker",
        payload_schema="docker.containers",
        payload_schema_version=1,
        payload=payload,
        connectivity=Connectivity.OK,
        freshness=Freshness.FRESH,
        fetched_at=datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
        as_of=datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
    )


def test_summary_states_engine_scale_not_host_cpu() -> None:
    from sideboard.core.contracts import PresentInput
    from sideboard.features.docker_local.provider import demo_payload

    payload = demo_payload()
    view = present_summary(
        PresentInput(
            settings=summary_settings(),
            inputs={"primary": envelope_for(payload)},
            context=view_context(),
        )
    )
    cpu = view.metric("engine_cpu")
    assert cpu is not None
    assert "not Mac host CPU" in cpu.basis
    memory = view.metric("engine_memory")
    assert memory is not None and "not added to Mac RAM" in memory.basis
    unhealthy = view.metric("unhealthy")
    assert unhealthy is not None and "without a healthcheck" in (unhealthy.note or "")


def test_table_groups_by_compose_project_and_masks_names() -> None:
    from sideboard.core.contracts import PresentInput
    from sideboard.features.docker_local.provider import demo_payload

    payload = demo_payload()
    view = present_table(
        PresentInput(
            settings=table_settings(),
            inputs={"primary": envelope_for(payload)},
            context=view_context(),
        )
    )
    assert view.table is not None
    assert view.table.groups
    assert all(row.privacy.value == "private" for row in view.table.rows)


def test_missing_payload_renders_an_honest_status() -> None:
    from sideboard.core.contracts import PresentInput

    view = present_summary(
        PresentInput(settings=summary_settings(), inputs={}, context=view_context())
    )
    assert view.status is not None


# --------------------------------------------------------------------------- regressions
# These three cases were found by pointing the adapter at a real Docker engine after the
# fixture-based tests passed. Each one aborted the whole collection.


def test_context_endpoint_accepts_the_nested_inspect_shape() -> None:
    """`docker context inspect` nests the host under a named endpoint object."""
    nested = json.dumps(
        {
            "name": "orbstack",
            "endpoints": {"docker": {"Host": "unix:///Users/x/.orbstack/run/docker.sock"}},
        }
    )
    parsed = parse_context_endpoint(nested)
    assert parsed.is_local is True
    assert parsed.endpoint == "unix:///Users/x/.orbstack/run/docker.sock"

    nested_remote = json.dumps(
        {"name": "prod", "endpoints": {"docker": {"Host": "ssh://user@host"}}}
    )
    assert parse_context_endpoint(nested_remote).is_local is False

    flat = json.dumps({"name": "local", "endpoints": {"docker": "unix:///var/run/docker.sock"}})
    assert parse_context_endpoint(flat).is_local is True


def test_inventory_template_passes_real_quotes_to_the_cli() -> None:
    """A backslash escape would be passed literally and rejected by the template parser."""
    assert '\\"' not in INVENTORY_TEMPLATE
    assert '{{.Label "com.docker.compose.project"}}' in INVENTORY_TEMPLATE
    assert '{{.Label "com.docker.compose.service"}}' in INVENTORY_TEMPLATE


def test_health_template_survives_a_container_without_a_healthcheck() -> None:
    """Docker runs templates with missingkey=error, so `.State.Health` aborts the call."""
    assert ".State.Health}}" not in HEALTH_TEMPLATE
    assert 'index .State "Health"' in HEALTH_TEMPLATE


def test_metadata_matches_the_short_inventory_id() -> None:
    """The inventory prints 12-character ids; inspect returns 64-character ones."""
    short = "2394b231862c"
    full = "2394b231862caaf98f1fc705a87a7732300a602673abd231f16a58da8a8d67a7"
    provider, _runner, build = build_provider(
        responses={
            "container ls": inventory_line(id=short),
            "container stats": stats_line(Container=short),
            "container inspect": health_line(id=full, restarts=3, health="healthy"),
        }
    )
    payload = asyncio.run(provider.fetch(build))
    container = payload.containers[0]
    assert container.id == short
    assert container.restarts == 3
    assert container.health == "healthy"
    assert container.health_defined is True
