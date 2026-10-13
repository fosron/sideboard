"""Bounded I/O boundary tests: HTTP policy, caps, status mapping, files, commands."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from sideboard.core.capabilities import (
    CommandRunner,
    FileReader,
    HttpReader,
    HttpScope,
    validate_public_url,
)
from sideboard.core.errors import (
    BlockedError,
    CapabilityError,
    ParseError,
    RateLimitedError,
    TransientNetworkError,
    UnauthorizedError,
    UnavailableError,
)

BASE = "https://example.com/feed"


def reader(**kwargs: object) -> HttpReader:
    client = httpx.AsyncClient()
    return HttpReader(client=client, scope=HttpScope.PUBLIC, **kwargs)  # type: ignore[arg-type]


def test_private_and_local_hosts_are_refused() -> None:
    for url in (
        "http://127.0.0.1/x",
        "http://localhost/x",
        "http://10.0.0.5/x",
        "http://192.168.1.10/x",
        "http://169.254.1.1/x",
        "http://[::1]/x",
        "http://printer.local/x",
        "file:///etc/passwd",
        "gopher://example.com/x",
    ):
        with pytest.raises(BlockedError):
            validate_public_url(url)


def test_public_host_is_allowed() -> None:
    assert validate_public_url(BASE) == BASE


def test_allowlisted_local_scope_requires_a_decision() -> None:
    http = HttpReader(
        client=httpx.AsyncClient(), scope=HttpScope.LOCAL, allow_hosts=frozenset({"localhost"})
    )
    assert http._validate("http://localhost:8080/health") == "http://localhost:8080/health"
    with pytest.raises(BlockedError):
        http._validate("http://other.internal/x")


@respx.mock
async def test_redirect_to_a_private_host_is_refused() -> None:
    respx.get(BASE).mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
    )
    http = reader()
    with pytest.raises(BlockedError):
        await http.get(BASE)
    await http.aclose()


@respx.mock
async def test_redirect_within_public_hosts_is_followed() -> None:
    respx.get(BASE).mock(
        return_value=httpx.Response(301, headers={"location": "https://example.com/real"})
    )
    respx.get("https://example.com/real").mock(
        return_value=httpx.Response(200, text="ok", headers={"content-type": "text/plain"})
    )
    http = reader()
    response = await http.get(BASE)
    assert response.status == 200
    assert response.text() == "ok"
    await http.aclose()


@respx.mock
async def test_decompressed_size_is_capped() -> None:
    respx.get(BASE).mock(return_value=httpx.Response(200, content=b"x" * 5000))
    http = reader(max_bytes=1024)
    response = await http.get(BASE)
    assert response.truncated is True
    assert len(response.content) == 1024
    await http.aclose()


@respx.mock
async def test_status_mapping() -> None:
    http = reader()
    respx.get(BASE).mock(return_value=httpx.Response(429, headers={"retry-after": "90"}))
    with pytest.raises(RateLimitedError) as info:
        await http.get(BASE)
    assert info.value.retry_after_seconds == 90.0

    respx.get(BASE).mock(return_value=httpx.Response(403))
    with pytest.raises(UnauthorizedError):
        await http.get(BASE)

    respx.get(BASE).mock(return_value=httpx.Response(410))
    with pytest.raises(UnavailableError):
        await http.get(BASE)

    respx.get(BASE).mock(return_value=httpx.Response(503))
    with pytest.raises(TransientNetworkError):
        await http.get(BASE)
    await http.aclose()


@respx.mock
async def test_conditional_request_returns_not_modified() -> None:
    route = respx.get(BASE).mock(return_value=httpx.Response(304))
    http = reader()
    response = await http.get(BASE, etag='"abc"', last_modified="Wed, 21 Oct 2026 07:28:00 GMT")
    assert response.not_modified is True
    request = route.calls[0].request
    assert request.headers["if-none-match"] == '"abc"'
    assert request.headers["if-modified-since"].startswith("Wed, 21 Oct 2026")
    await http.aclose()


@respx.mock
async def test_invalid_json_raises_parse_error() -> None:
    respx.get(BASE).mock(return_value=httpx.Response(200, text="not json"))
    http = reader()
    with pytest.raises(ParseError):
        (await http.get_json(BASE)).json()
    await http.aclose()


@respx.mock
async def test_connection_error_is_transient() -> None:
    respx.get(BASE).mock(side_effect=httpx.ConnectError("boom"))
    http = reader()
    with pytest.raises(TransientNetworkError):
        await http.get(BASE)
    await http.aclose()


async def test_file_reader_stays_inside_the_allowlist(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    inside = allowed / "snapshot.json"
    inside.write_text('{"ok": true}', encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    reader_ = FileReader(allowed_roots=(str(allowed),))
    assert reader_.read_text(str(inside)) == '{"ok": true}'
    with pytest.raises(CapabilityError):
        reader_.read_text(str(outside))
    with pytest.raises(UnavailableError):
        reader_.read_text(str(allowed / "missing.json"))
    with pytest.raises(UnavailableError):
        reader_.read_text(str(allowed))
    with pytest.raises(ParseError):
        FileReader(allowed_roots=(str(allowed),), max_bytes=2).read_text(str(inside))


async def test_command_runner_reports_missing_binary() -> None:
    runner = CommandRunner(concurrency=1)
    with pytest.raises(UnavailableError):
        await runner.run(["definitely-not-a-real-binary-sideboard"])
    await runner.aclose()


async def test_command_runner_times_out_and_reaps() -> None:
    runner = CommandRunner(concurrency=1)
    with pytest.raises(UnavailableError):
        await runner.run(["sleep", "30"], timeout=0.3)
    # The semaphore is released again, so a subsequent call still works.
    result = await runner.run(["true"], timeout=5)
    assert result.ok
    await runner.aclose()


async def test_command_runner_caps_output() -> None:
    runner = CommandRunner(concurrency=1, max_output_bytes=64)
    result = await runner.run(["printf", "%s", "x" * 500], timeout=5)
    assert result.truncated is True
    assert len(result.stdout) <= 64
    await runner.aclose()


async def test_command_runner_never_uses_a_shell() -> None:
    runner = CommandRunner(concurrency=1)
    # A shell metacharacter is passed as a literal argument, not interpreted.
    result = await runner.run(["printf", "%s", "a; rm -rf /"], timeout=5)
    assert result.stdout == "a; rm -rf /"
    await runner.aclose()
