"""Fixed Docker operation builders and the child-process environment policy.

Every operation is read-only, uses a fixed argument array, and is scoped to an
explicitly pinned context. Nothing in this module accepts a caller-supplied argument
list, a shell string, a label key or a format template from configuration.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from ...core.errors import ParseError, UnavailableError
from .config import REMOTE_ENDPOINT_PREFIXES

__all__ = [
    "CONTEXT_INSPECT_TEMPLATE",
    "ENGINE_INFO_TEMPLATE",
    "HEALTH_TEMPLATE",
    "INVENTORY_TEMPLATE",
    "SAFE_ENV_KEYS",
    "ContainerInspect",
    "ContextEndpoint",
    "build_argv",
    "deny_remote_endpoint",
    "docker_env",
    "parse_context_endpoint",
    "parse_engine_info",
    "parse_health_line",
    "parse_inventory_line",
]

# Allowlisted Compose labels only. Nothing else from the container labels map is read.
# Args are passed to the CLI directly (no shell), so the template carries real double
# quotes. A backslash escape would be passed through literally and Go's template parser
# rejects it ("unexpected \\ in operand").
INVENTORY_TEMPLATE = (
    '{"id":"{{.ID}}","name":"{{.Names}}","image":"{{.Image}}",'
    '"state":"{{.State}}","status":"{{.Status}}",'
    '"project":"{{.Label "com.docker.compose.project"}}",'
    '"service":"{{.Label "com.docker.compose.service"}}"}'
)

# Narrow inspect template: restart count, exit code, status and health status only.
# Deliberately not `{{json .}}`, which would include config, env and mounts.
# ``.State.Health`` only exists when a healthcheck is defined, and Docker runs the
# template engine with ``missingkey=error``: field access on an absent key aborts the
# whole call. ``index`` on the state map returns the zero value instead, so the same
# template works for containers with and without a healthcheck.
HEALTH_TEMPLATE = (
    '{"id":"{{.Id}}","restarts":{{.RestartCount}},'
    '"status":"{{.State.Status}}","exit":{{.State.ExitCode}},'
    '"started":"{{.State.StartedAt}}",'
    '"health":"{{if index .State "Health"}}{{index (index .State "Health") "Status"}}{{end}}"}'
)

CONTEXT_INSPECT_TEMPLATE = '{"name":"{{.Name}}","endpoints":{{json .Endpoints}}}'

ENGINE_INFO_TEMPLATE = (
    '{"name":"{{.Name}}","version":"{{.ServerVersion}}","ncpu":{{.NCPU}},'
    '"os":"{{.OperatingSystem}}"}'
)


class ContainerLike(Protocol):
    """Structural type for the inventory rows this helper selects from."""

    id: str
    running: bool


SAFE_ENV_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "DOCKER_CONFIG")

# Variables that could redirect collection away from the approved context.
REDIRECT_ENV_KEYS = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "DOCKER_API_VERSION",
)

_DAEMON_PATTERNS = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "error during connect",
    "connection refused",
    "no such file or directory",
)
_PERMISSION_PATTERNS = ("permission denied", "operation not permitted", "access is denied")
_CONTEXT_PATTERNS = ("context", "does not exist", "not found")


def docker_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a minimal child environment.

    ``DOCKER_HOST`` and friends are dropped so an inherited or subsequently changed
    global configuration cannot redirect collection. ``--context`` is passed on every
    call, and the endpoint is re-inspected before the first daemon call.
    """
    source = dict(os.environ if environ is None else environ)
    env = {key: source[key] for key in SAFE_ENV_KEYS if key in source}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin")
    for key in REDIRECT_ENV_KEYS:
        env.pop(key, None)
    return env


def build_argv(context: str, *args: str) -> list[str]:
    """Fixed argv array for one read-only operation against the pinned context."""
    return ["docker", "--context", context, *args]


@dataclass(frozen=True, slots=True)
class ContextEndpoint:
    name: str
    endpoint: str
    is_local: bool
    note: str | None = None


def parse_context_endpoint(payload: str) -> ContextEndpoint:
    """Parse ``docker context inspect`` output and classify the endpoint."""
    try:
        decoded = json.loads(payload.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise ParseError("docker context inspect returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ParseError("unexpected docker context inspect shape")
    name = str(decoded.get("name", ""))
    endpoints = decoded.get("endpoints")
    endpoint = ""
    if isinstance(endpoints, dict):
        ordered = [key for key in ("docker", "Docker", "docker-host") if key in endpoints]
        ordered.extend(key for key in endpoints if key not in ordered)
        for key in ordered:
            endpoint = _endpoint_of(endpoints[key])
            if endpoint:
                break
    return ContextEndpoint(name=name, endpoint=endpoint, is_local=_endpoint_is_local(endpoint))


def _endpoint_of(value: object) -> str:
    """Extract a host string from either context endpoint shape.

    ``docker context inspect`` renders the endpoint as an object
    (``{"docker": {"Host": "unix:///…"}}``) while ``docker context ls --format`` renders
    the same information as a plain string. Both are accepted.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("Host", "host", "EndPoint", "endpoint"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


def _endpoint_is_local(endpoint: str) -> bool:
    lowered = endpoint.lower()
    if not lowered:
        return False
    if lowered.startswith(REMOTE_ENDPOINT_PREFIXES):
        return False
    if lowered.startswith("unix://"):
        return True
    # A bare absolute path such as /var/run/docker.sock is still a local socket.
    return lowered.startswith("/")


def deny_remote_endpoint(endpoint: ContextEndpoint) -> None:
    """Refuse a non-local endpoint rather than collecting from it."""
    if endpoint.is_local:
        return
    raise UnavailableError(
        "the selected Docker context points at a remote engine; only an approved local "
        "socket is supported",
        context=endpoint.name,
        endpoint_scheme=endpoint.endpoint.split("://", 1)[0] or "unknown",
    )


@dataclass(frozen=True, slots=True)
class ContainerInspect:
    id: str
    restarts: int | None
    status: str | None
    exit_code: int | None
    started_at: str | None
    health: str | None


def parse_inventory_line(line: str) -> dict[str, str] | None:
    """Parse one JSONL inventory row. Malformed rows are skipped by the caller."""
    try:
        decoded = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return {str(key): str(value or "") for key, value in decoded.items()}


def parse_health_line(line: str) -> ContainerInspect | None:
    try:
        decoded = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return ContainerInspect(
        id=str(decoded.get("id", "")),
        restarts=_as_int(decoded.get("restarts")),
        status=_as_str(decoded.get("status")),
        exit_code=_as_int(decoded.get("exit")),
        started_at=_as_str(decoded.get("started")),
        health=_as_str(decoded.get("health")) or None,
    )


def parse_engine_info(name: str, payload: str) -> dict[str, str | int | None]:
    try:
        decoded = json.loads(payload.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise ParseError("docker info returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ParseError("unexpected docker info shape")
    return {
        "context": name,
        "name": _as_str(decoded.get("name")),
        "server_version": _as_str(decoded.get("version")),
        "operating_system": _as_str(decoded.get("os")),
        "cpu_count": _as_int(decoded.get("ncpu")),
    }


def _as_str(value: object) -> str:
    text = str(value if value is not None else "").strip()
    return text[:120]


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d+", text):
            return int(text)
    return None


def classify_cli_failure(stderr: str, returncode: int, *, command: str = "") -> UnavailableError:
    """Map a failed Docker CLI call to one of the distinct local states.

    The fallback names the command and quotes a bounded, control-free excerpt of what the
    CLI said. "docker CLI failed with exit code 1" is not a diagnosis: it was logged four
    times on a real machine with nothing to act on.
    """
    lowered = stderr.lower()
    if any(pattern in lowered for pattern in _PERMISSION_PATTERNS):
        return UnavailableError(
            "the Docker socket denied access; the current user is not permitted",
            state="access_denied",
        )
    if any(pattern in lowered for pattern in _DAEMON_PATTERNS):
        return UnavailableError(
            "the Docker engine is not reachable (stopped or missing socket)",
            state="engine_stopped",
        )
    if "context" in lowered and any(pattern in lowered for pattern in _CONTEXT_PATTERNS):
        return UnavailableError("the pinned Docker context does not exist", state="context_missing")
    detail = " ".join(_control_free(stderr).split())[:120]
    where = f"docker {command}".strip()
    message = f"{where} failed with exit code {returncode}"
    if detail:
        message = f"{message}: {detail}"
    return UnavailableError(message, state="cli_error")


def _control_free(value: str) -> str:
    """Strip control characters from CLI output before it reaches a message or a card."""
    return "".join(char for char in value if char.isprintable() or char == " ")


def selected_ids(containers: Sequence[ContainerLike], limit: int) -> tuple[tuple[str, ...], bool]:
    """Select running container IDs for one batched stats call, disclosing truncation."""
    ids: list[str] = []
    for container in containers:
        if getattr(container, "running", False) and getattr(container, "id", ""):
            ids.append(str(container.id))
            if len(ids) >= limit:
                return tuple(ids), True
    return tuple(ids), False
