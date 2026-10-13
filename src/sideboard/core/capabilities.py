"""Approved access boundaries.

Four narrow, typed services that providers may declare and receive:

* :class:`HttpReader` – bounded HTTP with an allowlist, redirect validation and size caps
* :class:`FileReader` – reads only explicitly configured paths
* :class:`CommandRunner` – fixed argv arrays, timeouts, output caps, correct reaping
* (``database`` is handed out directly by the host only for ``Capability.REPOSITORY``)

Configuration can narrow these scopes; it can never widen them.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import signal
import stat as stat_module
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .errors import (
    BlockedError,
    CapabilityError,
    ParseError,
    RateLimitedError,
    TransientNetworkError,
    UnauthorizedError,
    UnavailableError,
)

__all__ = [
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "CommandResult",
    "CommandRunner",
    "FileReader",
    "HttpLimits",
    "HttpReader",
    "HttpResponse",
    "HttpScope",
    "validate_public_url",
]

DEFAULT_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3


class HttpScope(StrEnum):
    """Public feeds must not reach private networks; local services use their own scope."""

    PUBLIC = "public"
    LOCAL = "local"


_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


def _host_is_private(host: str) -> bool:
    lowered = host.lower().strip("[]")
    if lowered in _BLOCKED_HOSTNAMES or lowered.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def validate_public_url(url: str, *, allow_hosts: frozenset[str] = frozenset()) -> str:
    """Reject schemes and hosts a public feed must never reach.

    Redirect hops are validated individually by the reader, so this is called per hop.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise BlockedError(f"unsupported URL scheme: {parts.scheme or '(none)'}")
    host = parts.hostname or ""
    if not host:
        raise BlockedError("URL has no host")
    if host.lower() in {h.lower() for h in allow_hosts}:
        return url
    if _host_is_private(host):
        raise BlockedError("refusing to fetch a private or loopback host from a public source")
    return url


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Bounded response. ``truncated`` means the payload cap stopped the read."""

    url: str
    status: int
    headers: Mapping[str, str]
    content: bytes
    not_modified: bool = False
    truncated: bool = False

    def text(self) -> str:
        charset = "utf-8"
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> object:
        try:
            return json.loads(self.text())
        except json.JSONDecodeError as exc:
            raise ParseError("response was not valid JSON") from exc

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass(slots=True)
class HttpLimits:
    """Shared global and per-host concurrency limits.

    One instance per application, so HN's bounded item fetches share the global budget.
    """

    global_limit: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(6))
    per_host_limit: int = 2
    _host_limits: dict[str, asyncio.Semaphore] = field(default_factory=dict, repr=False)

    def host(self, name: str) -> asyncio.Semaphore:
        semaphore = self._host_limits.get(name)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.per_host_limit)
            self._host_limits[name] = semaphore
        return semaphore


@dataclass(slots=True)
class HttpReader:
    """Bounded HTTP access bound to one scope and one host allowlist.

    * total deadline per request (default 20 s), connect 5 s, read 10 s
    * manual redirect following, each hop re-validated
    * response cap counted on decompressed bytes
    * honors ``Retry-After``, ``ETag`` and ``Last-Modified``
    """

    client: httpx.AsyncClient
    scope: HttpScope = HttpScope.PUBLIC
    allow_hosts: frozenset[str] = frozenset()
    max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    total_timeout: float = 20.0
    limits: HttpLimits = field(default_factory=HttpLimits)
    _closed: bool = False

    def _validate(self, url: str) -> str:
        if self.scope is HttpScope.PUBLIC:
            return validate_public_url(url, allow_hosts=self.allow_hosts)
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise BlockedError(f"unsupported URL scheme: {parts.scheme or '(none)'}")
        host = (parts.hostname or "").lower()
        if not host:
            raise BlockedError("URL has no host")
        allowed = {h.lower() for h in self.allow_hosts}
        if allowed and not any(host == entry or host.endswith(f".{entry}") for entry in allowed):
            raise BlockedError(f"host {host!r} is not in the approved local allowlist")
        return url

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str | int | float] | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        request_headers: dict[str, str] = {
            "User-Agent": "Sideboard/0.1 (+local dashboard)",
            "Accept-Encoding": "gzip, deflate",
        }
        if accept:
            request_headers["Accept"] = accept
        if etag:
            request_headers["If-None-Match"] = etag
        if last_modified:
            request_headers["If-Modified-Since"] = last_modified
        if headers:
            request_headers.update(dict(headers.items()))

        current = self._validate(str(url))
        deadline = timeout if timeout is not None else self.total_timeout
        hop_params: Mapping[str, str | int | float] | None = params
        for _ in range(MAX_REDIRECTS + 1):
            response = await self._one(current, request_headers, hop_params, deadline)
            if response.status in {301, 302, 303, 307, 308}:
                location = response.header("location")
                if not location:
                    return response
                current = self._validate(str(httpx.URL(current).join(location)))
                hop_params = None
                continue
            return response
        raise ParseError("too many redirects")

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int | float] | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        return await self.get(
            url,
            params=params,
            etag=etag,
            last_modified=last_modified,
            accept="application/json",
            timeout=timeout,
        )

    async def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, str | int | float] | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        """Convenience for feed/XML endpoints; the body is capped the same way."""
        return await self.get(
            url,
            params=params,
            etag=etag,
            last_modified=last_modified,
            accept=accept,
            timeout=timeout,
        )

    async def _one(
        self,
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, str | int | float] | None,
        timeout: float,
    ) -> HttpResponse:
        host = (urlsplit(url).hostname or "").lower()
        try:
            async with asyncio.timeout(timeout):
                async with self.limits.global_limit, self.limits.host(host):
                    return await self._stream(url, headers, params)
        except TimeoutError as exc:
            raise TransientNetworkError("request deadline exceeded", host=host) from exc
        except httpx.TooManyRedirects as exc:
            raise ParseError("too many redirects") from exc
        except httpx.HTTPError as exc:
            raise TransientNetworkError(_redact_httpx_error(exc), host=host) from exc

    async def _stream(
        self,
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, str | int | float] | None,
    ) -> HttpResponse:
        request = self.client.build_request(
            "GET",
            url,
            headers=headers,
            params=params,
            timeout=httpx.Timeout(
                connect=self.connect_timeout,
                read=self.read_timeout,
                write=self.read_timeout,
                pool=self.connect_timeout,
            ),
        )
        response = await self.client.send(request, stream=True, follow_redirects=False)
        try:
            status = response.status_code
            collected: list[bytes] = []
            total = 0
            truncated = False
            if status != 304:
                async for chunk in response.aiter_bytes():
                    if total + len(chunk) > self.max_bytes:
                        collected.append(chunk[: self.max_bytes - total])
                        truncated = True
                        break
                    collected.append(chunk)
                    total += len(chunk)
            result = HttpResponse(
                url=str(response.url),
                status=status,
                headers={k.lower(): v for k, v in response.headers.items()},
                content=b"".join(collected),
                not_modified=status == 304,
                truncated=truncated,
            )
        finally:
            await response.aclose()
        self._raise_for_status(result)
        return result

    @staticmethod
    def _raise_for_status(response: HttpResponse) -> None:
        status = response.status
        if status < 400:
            return
        retry_after = _parse_retry_after(response.headers.get("retry-after"))
        if status == 429:
            raise RateLimitedError(
                "provider rate limited the request", retry_after, http_status=429
            )
        if status in {401, 403}:
            raise UnauthorizedError("provider refused the request", http_status=status)
        if status in {404, 410}:
            raise UnavailableError("resource is no longer available", http_status=status)
        if status in {400, 451}:
            raise BlockedError("provider declined the request", http_status=status)
        raise TransientNetworkError(f"unexpected HTTP status {status}", http_status=status)

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self.client.aclose()


def _redact_httpx_error(exc: httpx.HTTPError) -> str:
    message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message[:200]}"


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


@dataclass(slots=True)
class FileReader:
    """Reads only explicitly configured regular files.

    ``allowed_roots`` are the exact paths the user configured (files or directories).
    A path outside every root is refused, as is a non-regular file.
    """

    allowed_roots: tuple[str, ...] = ()
    max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES

    def _resolve(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise UnavailableError("path could not be resolved") from exc
        for root in self.allowed_roots:
            root_path = Path(root).expanduser().resolve(strict=False)
            if resolved == root_path or root_path in resolved.parents:
                return resolved
        raise CapabilityError("path is not in the configured allowlist")

    def read_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes:
        resolved = self._resolve(path)
        try:
            info = resolved.stat()
        except FileNotFoundError as exc:
            raise UnavailableError("file does not exist") from exc
        except PermissionError as exc:
            raise UnavailableError("file is not readable") from exc
        if not stat_module.S_ISREG(info.st_mode):
            raise UnavailableError("path is not a regular file")
        cap = max_bytes if max_bytes is not None else self.max_bytes
        if info.st_size > cap:
            raise ParseError(f"file exceeds the {cap} byte limit")
        try:
            with resolved.open("rb") as handle:
                return handle.read(cap)
        except PermissionError as exc:
            raise UnavailableError("file is not readable") from exc
        except OSError as exc:
            raise UnavailableError("file could not be read") from exc

    def read_text(self, path: str, *, max_bytes: int | None = None) -> str:
        return self.read_bytes(path, max_bytes=max_bytes).decode("utf-8", errors="replace")

    def stat_path(self, path: str) -> os.stat_result | None:
        try:
            resolved = self._resolve(path)
        except CapabilityError, UnavailableError:
            return None
        try:
            return resolved.stat()
        except OSError:
            return None


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(slots=True)
class CommandRunner:
    """Runs fixed argv arrays. No shell, no interpolation, bounded output, reaped children."""

    concurrency: int = 1
    max_output_bytes: int = 512 * 1024
    default_timeout: float = 10.0
    _semaphore: asyncio.Semaphore | None = None
    _live: set[asyncio.subprocess.Process] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(max(1, self.concurrency))

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        max_output_bytes: int | None = None,
    ) -> CommandResult:
        if not argv:
            raise CapabilityError("empty command")
        args = [str(a) for a in argv]
        cap = max_output_bytes if max_output_bytes is not None else self.max_output_bytes
        deadline = timeout if timeout is not None else self.default_timeout
        assert self._semaphore is not None
        started = time.monotonic()

        async with self._semaphore:
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    env=dict(env) if env is not None else None,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise UnavailableError(f"command not found: {args[0]}") from exc
            except PermissionError as exc:
                raise UnavailableError(f"command not executable: {args[0]}") from exc
            self._live.add(process)
            try:
                stdout, stderr, truncated = await asyncio.wait_for(
                    self._drain(process, cap), timeout=deadline
                )
            except TimeoutError:
                await self._terminate(process)
                raise UnavailableError(
                    f"command timed out after {deadline:g}s: {args[0]}"
                ) from None
            except asyncio.CancelledError:
                await self._terminate(process)
                raise
            finally:
                self._live.discard(process)
            duration = time.monotonic() - started
        return CommandResult(
            argv=tuple(args),
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            truncated=truncated,
        )

    @staticmethod
    async def _drain(process: asyncio.subprocess.Process, cap: int) -> tuple[str, str, bool]:
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        sizes = {"out": 0, "err": 0}
        truncated = False

        async def pump(stream: asyncio.StreamReader, sink: list[bytes], key: str) -> None:
            nonlocal truncated
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return
                remaining = cap - sizes[key]
                if remaining <= 0:
                    truncated = True
                    continue
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                    truncated = True
                sizes[key] += len(chunk)
                sink.append(chunk)

        await asyncio.gather(
            pump(process.stdout, stdout_chunks, "out"),
            pump(process.stderr, stderr_chunks, "err"),
        )
        await process.wait()
        return (
            b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            b"".join(stderr_chunks).decode("utf-8", errors="replace"),
            truncated,
        )

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(process.pid), sig)
            except ProcessLookupError, PermissionError, OSError:
                try:
                    process.send_signal(sig)
                except ProcessLookupError, AttributeError:
                    return
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
                return
            except TimeoutError:
                continue
        with contextlib.suppress(TimeoutError):  # pragma: no cover - defensive
            await asyncio.wait_for(process.wait(), timeout=3.0)

    async def aclose(self) -> None:
        for process in list(self._live):
            await self._terminate(process)
        self._live.clear()
