"""Provider tests for the generic local snapshot feature.

Fixtures are synthetic. The oversized case is generated in ``tmp_path`` instead of
committing a megabyte file, and the provider always reads through the host's bounded
``FileReader`` (allowlist plus size cap), never with ``open``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from sideboard.core.capabilities import FileReader
from sideboard.core.clock import FakeClock
from sideboard.core.contracts import (
    Cadence,
    Capability,
    Connectivity,
    Freshness,
    ProviderBuild,
    SourceEnvelope,
    freshness_for,
)
from sideboard.core.errors import CapabilityError, ParseError, UnavailableError
from sideboard.features.local_snapshot.config import LocalSnapshotSettings
from sideboard.features.local_snapshot.feature import (
    FEATURE,
    FILE_SOURCE,
    ITEMS_WIDGET,
    METRICS_WIDGET,
)
from sideboard.features.local_snapshot.models import SnapshotPayload
from sideboard.features.local_snapshot.provider import (
    MAX_SNAPSHOT_BYTES,
    PROVENANCE,
    LocalSnapshotProvider,
    demo_payload,
)

FIXTURES = Path(__file__).parent / "fixtures"

CLOCK_START = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def settings_for(path: Path | str, **overrides: object) -> LocalSnapshotSettings:
    return LocalSnapshotSettings(path=str(path), **overrides)  # type: ignore[arg-type]


def fixture_settings(name: str, **overrides: object) -> LocalSnapshotSettings:
    return settings_for(FIXTURES / name, **overrides)


def files_for(root: Path) -> FileReader:
    return FileReader(allowed_roots=(str(root),))


def build_context(
    settings: LocalSnapshotSettings,
    *,
    files: FileReader | None,
    clock: FakeClock | None = None,
    offline: bool = False,
) -> ProviderBuild:
    return ProviderBuild(
        source_id="backup_snapshot",
        settings=settings,
        clock=clock or FakeClock(start=CLOCK_START),
        files=files,
        offline=offline,
    )


def provider_for(settings: LocalSnapshotSettings) -> LocalSnapshotProvider:
    built = FILE_SOURCE.factory(
        ProviderBuild(
            source_id="backup_snapshot",
            settings=settings,
            clock=FakeClock(start=CLOCK_START),
        )
    )
    assert isinstance(built, LocalSnapshotProvider)
    return built


async def collect(
    settings: LocalSnapshotSettings, *, files: FileReader | None, offline: bool = False
) -> SnapshotPayload:
    provider = provider_for(settings)
    payload = await provider.fetch(build_context(settings, files=files, offline=offline))
    assert isinstance(payload, SnapshotPayload)
    return payload


def write_snapshot(tmp_path: Path, document: object, name: str = "snapshot.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def valid_document() -> dict[str, object]:
    return json.loads((FIXTURES / "valid_snapshot.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


async def test_valid_snapshot_is_read_and_validated() -> None:
    payload = await collect(fixture_settings("valid_snapshot.json"), files=files_for(FIXTURES))
    document = payload.document
    assert document.schema_version == 1
    assert document.source_id == "example-backup"
    assert document.title == "Example backup status"
    assert [metric.id for metric in payload.metrics] == [
        "last_success",
        "duration_seconds",
        "verified",
        "note",
    ]
    assert [entry.id for entry in payload.items] == ["run-001", "run-000"]
    assert payload.provenance == PROVENANCE
    assert payload.as_of == datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    assert payload.valid_until == datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
    assert payload.coverage.notes is None
    assert payload.schema_id() == "local_snapshot.file"


async def test_history_is_preserved_including_gaps() -> None:
    payload = await collect(fixture_settings("valid_snapshot.json"), files=files_for(FIXTURES))
    duration = next(metric for metric in payload.metrics if metric.id == "duration_seconds")
    assert duration.history == (38.0, 40.5, None, 44.0, 42.5)


async def test_history_is_truncated_to_max_history_points() -> None:
    payload = await collect(
        fixture_settings("valid_snapshot.json", max_history_points=3),
        files=files_for(FIXTURES),
    )
    duration = next(metric for metric in payload.metrics if metric.id == "duration_seconds")
    assert duration.history == (None, 44.0, 42.5)
    assert payload.coverage.notes is not None and "truncated" in payload.coverage.notes


async def test_max_history_points_zero_drops_histories() -> None:
    payload = await collect(
        fixture_settings("valid_snapshot.json", max_history_points=0),
        files=files_for(FIXTURES),
    )
    assert all(metric.history == () for metric in payload.metrics)
    assert payload.coverage.notes is not None


async def test_offline_mode_still_reads_an_approved_local_file() -> None:
    payload = await collect(
        fixture_settings("valid_snapshot.json"), files=files_for(FIXTURES), offline=True
    )
    assert payload.document.source_id == "example-backup"


async def test_expired_snapshot_is_returned_for_honest_staleness(tmp_path: Path) -> None:
    document = valid_document()
    document["generated_at"] = "2026-10-13T08:00:00Z"
    document["expires_at"] = "2026-10-13T09:00:00Z"
    path = write_snapshot(tmp_path, document)
    payload = await collect(settings_for(path), files=files_for(tmp_path))

    assert payload.valid_until == datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
    assert payload.metrics  # the data is still shown, only marked expired
    envelope = SourceEnvelope(
        source_id="backup_snapshot",
        payload_schema="local_snapshot.file",
        payload_schema_version=1,
        payload=payload,
        connectivity=Connectivity.OK,
        last_success_at=CLOCK_START,
        valid_until=payload.valid_until,
    )
    assert freshness_for(envelope, CLOCK_START) is Freshness.EXPIRED


async def test_aclose_is_safe_twice() -> None:
    provider = provider_for(fixture_settings("valid_snapshot.json"))
    await provider.aclose()
    await provider.aclose()


# --------------------------------------------------------------------------- #
# Failures are typed, never swallowed and never faked
# --------------------------------------------------------------------------- #


async def test_missing_path_raises_unavailable() -> None:
    provider = provider_for(LocalSnapshotSettings())
    with pytest.raises(UnavailableError):
        await provider.fetch(build_context(LocalSnapshotSettings(), files=files_for(FIXTURES)))


async def test_missing_file_raises_unavailable() -> None:
    settings = settings_for(FIXTURES / "does-not-exist.json")
    with pytest.raises(UnavailableError):
        await collect(settings, files=files_for(FIXTURES))


async def test_path_outside_the_allowlist_raises_capability_error(tmp_path: Path) -> None:
    settings = fixture_settings("valid_snapshot.json")
    with pytest.raises(CapabilityError):
        await collect(settings, files=files_for(tmp_path))


async def test_no_file_reader_raises_capability_error() -> None:
    with pytest.raises(CapabilityError):
        await collect(fixture_settings("valid_snapshot.json"), files=None)


async def test_partial_write_in_progress_is_a_parse_error() -> None:
    with pytest.raises(ParseError) as excinfo:
        await collect(fixture_settings("partial_snapshot.json"), files=files_for(FIXTURES))
    assert "not valid JSON" in excinfo.value.message


async def test_invalid_snapshot_is_a_parse_error_with_a_readable_message() -> None:
    with pytest.raises(ParseError) as excinfo:
        await collect(fixture_settings("invalid_snapshot.json"), files=files_for(FIXTURES))
    message = excinfo.value.message
    assert "generated_at" in message
    assert "url" in message


async def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    document = valid_document()
    document["future_field"] = "value"
    path = write_snapshot(tmp_path, document)
    with pytest.raises(ParseError) as excinfo:
        await collect(settings_for(path), files=files_for(tmp_path))
    assert "future_field" in excinfo.value.message


async def test_control_characters_in_a_metric_value_are_rejected(tmp_path: Path) -> None:
    document = valid_document()
    document["metrics"] = [{"id": "note", "label": "Note", "value": "first\nsecond"}]
    path = write_snapshot(tmp_path, document)
    with pytest.raises(ParseError) as excinfo:
        await collect(settings_for(path), files=files_for(tmp_path))
    assert "control" in excinfo.value.message


async def test_non_http_item_url_is_rejected(tmp_path: Path) -> None:
    document = valid_document()
    document["items"] = [{"id": "bad", "title": "Bad", "url": "file:///etc/passwd"}]
    path = write_snapshot(tmp_path, document)
    with pytest.raises(ParseError) as excinfo:
        await collect(settings_for(path), files=files_for(tmp_path))
    assert "http(s)" in excinfo.value.message


async def test_naive_timestamp_is_rejected(tmp_path: Path) -> None:
    document = valid_document()
    document["generated_at"] = "2026-10-13T10:00:00"
    path = write_snapshot(tmp_path, document)
    with pytest.raises(ParseError) as excinfo:
        await collect(settings_for(path), files=files_for(tmp_path))
    assert "timezone" in excinfo.value.message


async def test_oversized_file_is_a_parse_error(tmp_path: Path) -> None:
    document = {
        "schema_version": 1,
        "source_id": "too-big",
        "generated_at": "2026-10-13T10:00:00Z",
        "title": "x" * (MAX_SNAPSHOT_BYTES + 1024),
    }
    path = write_snapshot(tmp_path, document, name="oversized_snapshot.json")
    assert path.stat().st_size > MAX_SNAPSHOT_BYTES
    with pytest.raises(ParseError) as excinfo:
        await collect(settings_for(path), files=files_for(tmp_path))
    assert "exceeds" in excinfo.value.message


# --------------------------------------------------------------------------- #
# Settings, demo data and registration
# --------------------------------------------------------------------------- #


def test_settings_require_an_absolute_or_home_relative_path() -> None:
    assert LocalSnapshotSettings(path="~/state.json").path == "~/state.json"
    with pytest.raises(ValidationError):
        LocalSnapshotSettings(path="relative/state.json")
    with pytest.raises(ValidationError):
        LocalSnapshotSettings(path="/tmp/bad\x00.json")
    with pytest.raises(ValidationError):
        LocalSnapshotSettings.model_validate({"path": "/tmp/ok.json", "unknown": 1})


def test_refresh_seconds_alias_fills_missing_cadence_fields() -> None:
    settings = LocalSnapshotSettings(path="/tmp/ok.json", refresh_seconds=45.0)
    assert (settings.active_seconds, settings.ambient_seconds, settings.background_seconds) == (
        45.0,
        45.0,
        45.0,
    )
    explicit = LocalSnapshotSettings(path="/tmp/ok.json", refresh_seconds=45.0, active_seconds=10.0)
    assert explicit.active_seconds == 10.0


def test_demo_payload_is_private_and_synthetic() -> None:
    payload = demo_payload()
    assert isinstance(payload, SnapshotPayload)
    assert payload.document.source_id == "demo-backup"
    assert payload.privacy.value == "private"
    assert payload.metrics and payload.items
    as_of = payload.as_of
    valid_until = payload.valid_until
    assert as_of is not None
    assert valid_until is not None
    assert valid_until > as_of
    assert "synthetic" in (payload.document.title or "").lower()


def test_registration_matches_the_contract() -> None:
    assert FEATURE.id == "local_snapshot"
    assert FEATURE.sources == (FILE_SOURCE,)
    assert FEATURE.widgets == (METRICS_WIDGET, ITEMS_WIDGET)
    assert FILE_SOURCE.type_id == "local_snapshot.file"
    assert FILE_SOURCE.payload_schema == "local_snapshot.file"
    assert FILE_SOURCE.payload_schema_version == 1
    assert FILE_SOURCE.capabilities == frozenset({Capability.FILE})
    assert FILE_SOURCE.cadence == Cadence(
        active_seconds=30.0, ambient_seconds=60.0, background_seconds=120.0
    )
    assert FILE_SOURCE.requires_setup is True
    assert FILE_SOURCE.setup_hint == (
        "Point this source at an explicitly selected JSON snapshot file."
    )
    assert FILE_SOURCE.demo_builder is demo_payload
    assert METRICS_WIDGET.type_id == "local_snapshot.metrics"
    assert ITEMS_WIDGET.type_id == "local_snapshot.items"
    for widget in FEATURE.widgets:
        binding = widget.binding("primary")
        assert binding is not None
        assert binding.accepts == frozenset({"local_snapshot.file"})
