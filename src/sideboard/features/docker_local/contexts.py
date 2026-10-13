"""Local Docker context discovery for explicit setup.

Listing contexts is a read-only convenience so the user can pin an approved local
socket. It never selects a context, never switches the global context and never starts
an engine.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass

from ...core.capabilities import CommandRunner
from ...core.errors import UnavailableError
from .commands import docker_env, parse_context_endpoint

__all__ = ["ContextSummary", "list_docker_contexts", "resolve_docker_binary"]

CONTEXT_LIST_TEMPLATE = (
    '{"name":"{{.Name}}","endpoint":"{{.DockerEndpoint}}","current":{{.Current}}}'
)


@dataclass(frozen=True, slots=True)
class ContextSummary:
    name: str
    endpoint: str
    is_local: bool
    current: bool = False


def resolve_docker_binary() -> str | None:
    """Locate the Docker CLI without executing anything."""
    return shutil.which("docker")


async def list_docker_contexts(
    runner: CommandRunner, *, timeout: float = 8.0
) -> tuple[tuple[ContextSummary, ...], str | None]:
    """Return the available contexts and an optional note. Never mutates anything."""
    try:
        result = await runner.run(
            ["docker", "context", "ls", "--format", CONTEXT_LIST_TEMPLATE],
            timeout=timeout,
            env=docker_env(),
        )
    except UnavailableError as error:
        return (), error.message
    if not result.ok:
        return (), f"docker context ls failed with exit code {result.returncode}"
    summaries: list[ContextSummary] = []
    for line in result.stdout.splitlines():
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict):
            continue
        endpoint = str(decoded.get("endpoint", ""))
        endpoint_info = parse_context_endpoint(
            json.dumps({"name": decoded.get("name"), "endpoints": {"docker": endpoint}})
        )
        summaries.append(
            ContextSummary(
                name=str(decoded.get("name") or "default"),
                endpoint=endpoint or "(endpoint reported by the current environment)",
                is_local=endpoint_info.is_local,
                current=bool(decoded.get("current", False)),
            )
        )
    return tuple(summaries), None
