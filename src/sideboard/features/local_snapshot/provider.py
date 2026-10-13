"""Read-only consumption of one produced local snapshot file.

Plan section 6.7 contract, implemented literally:

* only the explicitly configured regular-file path is read, through the host's
  :class:`~sideboard.core.capabilities.FileReader` (allowlist, 1 MiB cap)
* reader errors are **not** swallowed: a ``ParseError`` (oversized or unreadable file),
  ``UnavailableError`` (missing file) or ``CapabilityError`` (path not allowlisted) is
  surfaced to the host, which keeps the last good snapshot and shows a warning
* producers are expected to write a temporary file and rename it atomically. A partial
  or invalid document is rejected here, so an update in progress never replaces good
  data with a half-written file.
* an ``expires_at`` in the past is **not** an error. The payload is returned with
  ``valid_until = expires_at`` so the host marks it stale/expired honestly and the card
  can still show the data the producer last wrote.
* offline mode does not block this source: it reads an approved local file, not a
  public network resource.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ...core.contracts import Coverage, Privacy, ProviderBuild, SourcePayload
from ...core.errors import CapabilityError, ParseError, UnavailableError
from .config import LocalSnapshotSettings
from .models import SnapshotDocument, SnapshotItem, SnapshotMetric, SnapshotPayload
from .validate import validate_snapshot

__all__ = [
    "MAX_SNAPSHOT_BYTES",
    "PROVENANCE",
    "LocalSnapshotProvider",
    "build_provider",
    "demo_payload",
]

MAX_SNAPSHOT_BYTES = 1024 * 1024
"""Hard 1 MiB limit, enforced by the file reader before any parsing."""

PROVENANCE = "local-snapshot-file"


@dataclass(slots=True)
class LocalSnapshotProvider:
    """One bounded read per refresh. Owns no polling loop and no file handle."""

    settings: LocalSnapshotSettings
    _closed: bool = False

    async def fetch(self, context: ProviderBuild) -> SourcePayload:
        """Read, decode and validate the configured snapshot file."""
        files = context.files
        if files is None:
            raise CapabilityError("the local snapshot source has no approved file reader")
        path = self.settings.path.strip()
        if not path:
            raise UnavailableError("no local snapshot path is configured")
        # The reader enforces the allowlist and the size cap; its typed errors surface.
        raw = files.read_text(path, max_bytes=MAX_SNAPSHOT_BYTES)
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"snapshot is not valid JSON (line {exc.lineno}, column {exc.colno})"
            ) from exc
        payload = validate_snapshot(decoded)
        bounded, trimmed = _bound_history(payload.document, self.settings.max_history_points)
        coverage = Coverage(notes=_trim_note(self.settings.max_history_points) if trimmed else None)
        return SnapshotPayload(document=bounded, provenance=PROVENANCE, coverage=coverage)

    async def aclose(self) -> None:
        """Release owned resources (none). Safe to call more than once."""
        self._closed = True


def _bound_history(document: SnapshotDocument, limit: int) -> tuple[SnapshotDocument, bool]:
    """Keep the newest ``limit`` history points per metric (history is oldest-first)."""
    trimmed = False
    metrics: list[SnapshotMetric] = []
    for metric in document.metrics:
        if len(metric.history) > limit:
            trimmed = True
            metrics.append(
                metric.model_copy(update={"history": metric.history[-limit:] if limit > 0 else ()})
            )
        else:
            metrics.append(metric)
    if not trimmed:
        return document, False
    return document.model_copy(update={"metrics": tuple(metrics)}), True


def _trim_note(limit: int) -> str:
    if limit <= 0:
        return "producer metric histories were dropped (max_history_points = 0)"
    return f"producer metric histories were truncated to the newest {limit} point(s)"


def build_provider(context: ProviderBuild) -> LocalSnapshotProvider:
    """Provider factory for ``local_snapshot.file``."""
    settings = context.settings
    if not isinstance(settings, LocalSnapshotSettings):
        raise CapabilityError("the local snapshot source received unexpected settings")
    return LocalSnapshotProvider(settings=settings)


def demo_payload() -> SnapshotPayload:
    """Synthetic backup-job snapshot for ``--demo``. Clearly synthetic, no file I/O."""
    now = datetime.now(UTC).replace(microsecond=0)
    document = SnapshotDocument(
        schema_version=1,
        source_id="demo-backup",
        generated_at=now,
        expires_at=now + timedelta(hours=2),
        privacy=Privacy.PRIVATE,
        title="Example backup status (synthetic)",
        metrics=(
            SnapshotMetric(
                id="last_success",
                label="Last successful backup",
                value=(now - timedelta(minutes=15)).isoformat(),
                unit="timestamp",
                basis="synthetic",
            ),
            SnapshotMetric(
                id="duration_seconds",
                label="Duration",
                value=42.5,
                unit="s",
                basis="synthetic",
                history=(38.0, 40.5, None, 44.0, 42.5),
            ),
            SnapshotMetric(
                id="bytes_written",
                label="Bytes written",
                value=12_345_678_901,
                unit="B",
                basis="synthetic",
                history=(9.1e9, 1.01e10, 1.15e10, 1.2e10, 1.2345e10),
            ),
            SnapshotMetric(
                id="verified",
                label="Restore verified",
                value=True,
                basis="synthetic",
            ),
        ),
        items=(
            SnapshotItem(
                id="report-demo",
                title="Synthetic backup report",
                url="https://example.invalid/backup/report-demo",
                subtitle="example.invalid · synthetic",
                published_at=now,
            ),
        ),
    )
    return SnapshotPayload(
        document=document,
        provenance=PROVENANCE,
        coverage=Coverage(notes="synthetic demo data"),
    )
