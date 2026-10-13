"""Synthetic collectors for ``--demo``.

Demo mode uses a separate database and **synthetic collectors only**. A feature
opts in by declaring ``demo_builder`` on its :class:`SourceSpec`. A source with no
synthetic builder is reported as unavailable in demo mode rather than being allowed
to perform real I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .contracts import ProviderBuild, SourcePayload, SourceSpec

__all__ = ["SyntheticProvider", "synthetic_provider"]


@dataclass(slots=True)
class SyntheticProvider:
    """Returns a freshly built synthetic payload on every fetch. No I/O, no network."""

    builder: Callable[[], SourcePayload]
    payload_model: type[SourcePayload]
    calls: int = field(default=0, init=False)

    async def fetch(self, context: ProviderBuild) -> SourcePayload:
        self.calls += 1
        payload = self.builder()
        if not isinstance(payload, self.payload_model):
            raise TypeError(
                f"demo builder returned {type(payload).__name__}, "
                f"expected {self.payload_model.__name__}"
            )
        _ = context
        return payload

    async def aclose(self) -> None:
        return None


def synthetic_provider(spec: SourceSpec, settings: object) -> SyntheticProvider | None:
    """Return a synthetic provider for a spec, or None when the feature has no demo data."""
    _ = settings
    builder = spec.demo_builder
    if builder is None:
        return None
    return SyntheticProvider(builder=builder, payload_model=spec.payload_model)
