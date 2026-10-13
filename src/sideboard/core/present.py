"""Presenter invocation with contained failures.

Kept out of the UI layer so it can be unit-tested without Textual.

* a missing binding produces a standard incomplete/unconfigured view
* a presenter exception produces a contained error card, never a crash
* presentation never fetches data, queries a database or starts a process
"""

from __future__ import annotations

from collections.abc import Mapping

from .config import WidgetInstance
from .contracts import (
    Connectivity,
    PresentInput,
    Severity,
    SourceEnvelope,
    StatusView,
    ViewContext,
    ViewModel,
)
from .errors import SideboardError

__all__ = [
    "classify_view",
    "combine_envelopes",
    "error_view",
    "missing_binding_view",
    "present_widget",
    "status_from_envelope",
]

_FRESHNESS_RANK = {"none": 3, "expired": 2, "stale": 1, "fresh": 0}
_CONNECTIVITY_RANK = {
    "ok": 0,
    "unconfigured": 1,
    "unavailable": 2,
    "offline": 3,
    "error": 4,
    "blocked": 5,
}


def combine_envelopes(
    envelopes: Mapping[str, SourceEnvelope | None],
) -> SourceEnvelope | None:
    """Pick the least flattering bound envelope, so a composite card cannot look healthy.

    A card bound to holdings + quotes + FX is only as trustworthy as its weakest input.
    """
    candidates = [envelope for envelope in envelopes.values() if envelope is not None]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda env: (
            _FRESHNESS_RANK.get(str(env.freshness), 1),
            _CONNECTIVITY_RANK.get(str(env.connectivity), 2),
            0 if env.has_payload else 1,
        ),
    )


def classify_view(view: ViewModel) -> str:
    """Rotation/diagnostics classification: ready, setup, error or empty."""
    if view.status is None:
        return "empty" if view.is_empty else "ready"
    if view.status.severity is Severity.ERROR:
        return "error"
    if not view.is_empty:
        return "ready"
    headline = view.status.headline.lower()
    if "not configured" in headline or "unavailable" in headline:
        return "setup"
    if view.status.severity is Severity.WARNING:
        return "error"
    return "empty"


def present_widget(
    instance: WidgetInstance,
    envelopes: Mapping[str, SourceEnvelope | None],
    context: ViewContext,
) -> ViewModel:
    """Run one presenter, converting any failure into a contained view."""
    missing = _missing_bindings(instance, envelopes)
    if missing:
        return missing_binding_view(instance, missing, context)
    return _call(instance, envelopes, context)


def _call(
    instance: WidgetInstance,
    envelopes: Mapping[str, SourceEnvelope | None],
    context: ViewContext,
) -> ViewModel:
    try:
        view = instance.spec.present(
            PresentInput(
                settings=instance.settings,
                inputs=envelopes,
                context=context,
            )
        )
    except Exception as exc:  # contained: one bad presenter must not crash the dashboard
        return error_view(instance.widget_id, exc)
    if not isinstance(view, ViewModel):  # pragma: no cover - contract guard
        return error_view(instance.widget_id, TypeError("presenter did not return a ViewModel"))
    if instance.title and view.title is None:
        return _retitle(view, instance.title)
    return view


def _retitle(view: ViewModel, title: str) -> ViewModel:
    from dataclasses import replace

    return replace(view, title=title)


def _missing_bindings(
    instance: WidgetInstance, envelopes: Mapping[str, SourceEnvelope | None]
) -> tuple[str, ...]:
    """Report only *unconfigured* bindings.

    A bound source that has not produced a payload yet is not a configuration problem:
    the presenter receives the empty envelope and must say something honest about it.
    That is how a blocked, offline or import-driven source is rendered, and it keeps the
    "not configured" message meaningful.
    """
    missing: list[str] = []
    for binding in instance.spec.inputs:
        if not binding.required:
            continue
        if instance.bindings.get(binding.name) is None:
            missing.append(binding.name)
    return tuple(missing)


def missing_binding_view(
    instance: WidgetInstance, missing: tuple[str, ...], context: ViewContext
) -> ViewModel:
    detail = ", ".join(sorted(missing))
    return ViewModel(
        title=instance.title,
        status=StatusView(
            severity=Severity.INFO,
            headline="Not configured",
            detail=f"Waiting for source binding: {detail}",
            hints=(f"Configure the {instance.spec.type_id} widget's sources in config.toml",),
        ),
        footnote="unconfigured",
    )


def error_view(widget_id: str, error: BaseException) -> ViewModel:
    if isinstance(error, SideboardError):
        detail = f"{error.category}: {error.message}"
    else:
        detail = type(error).__name__
    return ViewModel(
        title=widget_id,
        status=StatusView(
            severity=Severity.ERROR,
            headline="Widget error",
            detail=detail[:200],
        ),
        footnote="error",
    )


def status_from_envelope(
    envelope: SourceEnvelope | None,
    *,
    offline: bool = False,
    demo: bool = False,
    privacy: bool = False,
) -> StatusView | None:
    """Honest banner for a source that has no usable payload, or is blocked/offline."""
    if envelope is None:
        return StatusView(
            severity=Severity.WARNING,
            headline="Source not configured",
            detail="No source instance is bound to this card.",
        )
    if offline and envelope.connectivity is Connectivity.OFFLINE:
        return StatusView(
            severity=Severity.INFO,
            headline="Offline",
            detail="Public network access is suppressed in offline mode.",
            hints=("Launch without --offline to fetch public sources.",),
        )
    if envelope.payload is not None:
        if envelope.error is not None:
            return StatusView(
                severity=Severity.WARNING,
                headline="Last refresh failed",
                detail=f"{envelope.error.category}: {envelope.error.message}"[:200],
                hints=("Showing the last good data.",),
            )
        return None
    connectivity = envelope.connectivity
    if connectivity is Connectivity.BLOCKED:
        return StatusView(
            severity=Severity.WARNING,
            headline="Access blocked",
            detail=(envelope.error.message if envelope.error else "The provider denied access.")[
                :200
            ],
            hints=("No access evasion is attempted. Check the provider's terms.",),
        )
    if connectivity is Connectivity.UNAVAILABLE:
        return StatusView(
            severity=Severity.INFO,
            headline="Unavailable",
            detail=(envelope.error.message if envelope.error else "The resource is not available.")[
                :200
            ],
        )
    if connectivity is Connectivity.UNCONFIGURED:
        return StatusView(
            severity=Severity.INFO,
            headline="Not configured",
            detail="This source needs setup before it can collect.",
        )
    if connectivity is Connectivity.OFFLINE:
        return StatusView(
            severity=Severity.INFO,
            headline="Offline",
            detail="Network access is suppressed.",
        )
    return StatusView(
        severity=Severity.WARNING,
        headline="No data yet",
        detail=(envelope.error.message if envelope.error else None),
        hints=("Waiting for the first successful refresh.",),
    )
