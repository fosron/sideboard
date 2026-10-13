"""Rotation state machine.

Holds are tracked separately per reason (``hover``, ``interaction``, ``modal``,
``manual``) so releasing one can never accidentally clear another. Only the hovered
card stops; fetching and caching continue while a card is held.

Releasing a hold resumes the *remaining* dwell after a grace period, so a card does
not jump the instant the pointer moves away.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["Candidate", "HoldReason", "RotationController", "SlotState"]


class HoldReason(StrEnum):
    HOVER = "hover"
    INTERACTION = "interaction"
    MODAL = "modal"
    MANUAL = "manual"


class Candidate(StrEnum):
    """How useful a widget is as a rotating card right now."""

    READY = "ready"
    SETUP = "setup"
    ERROR = "error"
    EMPTY = "empty"


@dataclass(slots=True)
class SlotState:
    """One on-screen rotation slot."""

    slot_id: str
    queue: tuple[str, ...]
    pinned: bool = False
    dwell: float = 40.0
    current: str | None = None
    shown_at: float = 0.0
    label: str | None = None
    holds: set[HoldReason] = field(default_factory=set)
    pointer_inside: bool = False
    resume_after: float | None = None
    remaining: float | None = None
    current_kind: Candidate = Candidate.EMPTY

    @property
    def held(self) -> bool:
        return bool(self.holds)


class RotationController:
    """Decides which widget each slot shows and when to advance.

    Pure state machine: no widgets, no timers of its own. The host calls
    :meth:`tick` from a single UI timer.
    """

    def __init__(
        self,
        *,
        slots: Mapping[str, Sequence[str]],
        pinned: Mapping[str, bool] | None = None,
        dwell: Mapping[str, float] | None = None,
        labels: Mapping[str, str] | None = None,
        clock_now: Callable[[], float],
        classify: Callable[[str], Candidate],
        grace_seconds: float = 2.0,
        keyboard_hold_seconds: float = 15.0,
    ) -> None:
        self._now = clock_now
        self._classify = classify
        self.grace_seconds = grace_seconds
        self.keyboard_hold_seconds = keyboard_hold_seconds
        self.slots: dict[str, SlotState] = {
            slot_id: SlotState(
                slot_id=slot_id,
                queue=tuple(queue),
                pinned=bool((pinned or {}).get(slot_id, False)),
                dwell=float((dwell or {}).get(slot_id, 40.0)),
                label=(labels or {}).get(slot_id),
            )
            for slot_id, queue in slots.items()
        }
        self.global_holds: set[HoldReason] = set()

    # ------------------------------------------------------------------ queries

    @property
    def held_globally(self) -> bool:
        return bool(self.global_holds)

    def held(self, slot_id: str) -> bool:
        """True only for an active hold. The grace window is not a hold."""
        if self.global_holds:
            return True
        slot = self.slots.get(slot_id)
        return bool(slot and slot.holds)

    def in_grace(self, slot_id: str) -> bool:
        """True while the remaining dwell is suspended after a released hold."""
        slot = self.slots.get(slot_id)
        if slot is None or slot.resume_after is None:
            return False
        return self._now() < slot.resume_after

    def hold_reasons(self, slot_id: str) -> tuple[str, ...]:
        reasons = {str(reason) for reason in self.global_holds}
        slot = self.slots.get(slot_id)
        if slot is not None:
            reasons |= {str(reason) for reason in slot.holds}
        return tuple(sorted(reasons))

    def current(self, slot_id: str) -> str | None:
        slot = self.slots.get(slot_id)
        return slot.current if slot is not None else None

    def current_kind(self, slot_id: str) -> Candidate:
        slot = self.slots.get(slot_id)
        return slot.current_kind if slot is not None else Candidate.EMPTY

    def visible_widgets(self) -> frozenset[str]:
        return frozenset(slot.current for slot in self.slots.values() if slot.current is not None)

    def snapshot(self) -> dict[str, str]:
        return {
            slot_id: slot.current
            for slot_id, slot in self.slots.items()
            if slot.current is not None
        }

    # ------------------------------------------------------------------ holds

    def set_hold(self, slot_id: str, reason: HoldReason, active: bool) -> bool:
        """Apply or release a hold. Returns True when the slot's hold state changed."""
        slot = self.slots.get(slot_id)
        if slot is None:
            return False
        before = self.held(slot_id)
        if active:
            if reason not in slot.holds:
                elapsed = self._now() - slot.shown_at
                slot.remaining = max(0.0, slot.dwell - elapsed)
            slot.holds.add(reason)
            slot.resume_after = None
        else:
            was_held = reason in slot.holds
            slot.holds.discard(reason)
            if was_held and not slot.holds and not self.global_holds:
                remaining = slot.remaining if slot.remaining is not None else slot.dwell
                slot.resume_after = self._now() + self.grace_seconds
                slot.shown_at = slot.resume_after + remaining - slot.dwell
        after = self.held(slot_id)
        return before != after

    def set_global_hold(self, reason: HoldReason, active: bool) -> None:
        if active:
            self.global_holds.add(reason)
        else:
            self.global_holds.discard(reason)

    def clear_pointer_holds(self) -> None:
        """Terminal blur: drop stale pointer state and require fresh pointer events."""
        for slot in self.slots.values():
            slot.pointer_inside = False
            slot.holds.discard(HoldReason.HOVER)
            slot.resume_after = None

    def reset_holds(self) -> None:
        """Visible reset-holds control. Keeps an explicit manual pause intact."""
        for slot in self.slots.values():
            slot.holds.clear()
            slot.resume_after = None
        self.global_holds.discard(HoldReason.MODAL)
        self.global_holds.discard(HoldReason.HOVER)

    # ------------------------------------------------------------------ selection

    def classify(self, widget_id: str) -> Candidate:
        try:
            return self._classify(widget_id)
        except Exception:  # pragma: no cover - classifier must not break rotation
            return Candidate.ERROR

    def _setup_taken(self, exclude_slot: str) -> bool:
        return any(
            slot.current_kind is Candidate.SETUP
            for slot_id, slot in self.slots.items()
            if slot_id != exclude_slot
        )

    def choose(self, slot_id: str, *, after: str | None = None) -> str | None:
        """Pick the best candidate for a slot, preferring ready content.

        A setup card appears at most once per screen, so the Overview shows one setup
        card instead of repeating it in every slot.
        """
        slot = self.slots.get(slot_id)
        if slot is None or not slot.queue:
            return None
        order = _rotate(slot.queue, after)
        top = self._setup_taken(slot_id)
        ready: str | None = None
        setup: str | None = None
        failed: str | None = None
        visible = self.visible_widgets()
        for widget_id in order:
            kind = self.classify(widget_id)
            if kind is Candidate.EMPTY:
                continue
            if kind is Candidate.READY:
                if widget_id in visible and len(slot.queue) > 1 and widget_id != slot.current:
                    continue
                ready = widget_id
                break
            if kind is Candidate.SETUP and setup is None and not top and widget_id not in visible:
                setup = widget_id
            elif kind is Candidate.ERROR and failed is None and widget_id not in visible:
                failed = widget_id
        return ready or setup or failed

    def assign(self, slot_id: str, widget_id: str | None, *, now: float | None = None) -> None:
        slot = self.slots.get(slot_id)
        if slot is None:
            return
        slot.current = widget_id
        slot.shown_at = self._now() if now is None else now
        slot.remaining = None
        slot.current_kind = Candidate.EMPTY if widget_id is None else self.classify(widget_id)

    def initialize(self) -> list[str]:
        """Fill every slot for the first time. Returns the slot ids that changed."""
        changed: list[str] = []
        for slot_id, slot in self.slots.items():
            widget_id = self.choose(slot_id)
            if widget_id is None:
                continue
            self.assign(slot_id, widget_id)
            if slot.pinned:
                slot.shown_at = float("-inf")
            changed.append(slot_id)
        return changed

    def advance(self, slot_id: str, *, manual: bool = False) -> str | None:
        """Move a slot to its next candidate. Pinned slots rotate only on request."""
        slot = self.slots.get(slot_id)
        if slot is None:
            return None
        if slot.pinned and not manual:
            return slot.current
        if not slot.queue:
            return slot.current
        previous = slot.current
        widget_id = self.choose(slot_id, after=previous)
        if widget_id is None:
            widget_id = previous
        self.assign(slot_id, widget_id)
        return slot.current

    def tick(self, *, now: float | None = None) -> list[str]:
        """Advance any slot whose dwell expired. Returns the slot ids that changed."""
        moment = self._now() if now is None else now
        changed: list[str] = []
        for slot_id, slot in self.slots.items():
            if slot.resume_after is not None and moment >= slot.resume_after:
                # The release already placed shown_at so that the remaining dwell expires
                # at resume_after + remaining. Observing the end of the grace only clears
                # the marker; it must not restart the dwell.
                slot.resume_after = None
                slot.remaining = None
            if (
                self.held(slot_id)
                or slot.pinned
                or slot.current is None
                or slot.resume_after is not None
            ):
                continue
            if moment - slot.shown_at >= slot.dwell:
                self.advance(slot_id)
                changed.append(slot_id)
        return changed

    def reconfigure(
        self,
        *,
        slots: Mapping[str, Sequence[str]],
        pinned: Mapping[str, bool] | None = None,
        dwell: Mapping[str, float] | None = None,
        labels: Mapping[str, str] | None = None,
        keyboard_hold_seconds: float | None = None,
        grace_seconds: float | None = None,
    ) -> list[str]:
        """Apply a validated change, preserving selection by stable ID where it still exists."""
        if keyboard_hold_seconds is not None:
            self.keyboard_hold_seconds = keyboard_hold_seconds
        if grace_seconds is not None:
            self.grace_seconds = grace_seconds
        kept: dict[str, SlotState] = {}
        for slot_id, queue in slots.items():
            previous = self.slots.get(slot_id)
            state = SlotState(
                slot_id=slot_id,
                queue=tuple(queue),
                pinned=bool((pinned or {}).get(slot_id, False)),
                dwell=float((dwell or {}).get(slot_id, 40.0)),
                label=(labels or {}).get(slot_id),
            )
            if previous is not None:
                state.holds = set(previous.holds)
                state.pointer_inside = previous.pointer_inside
                state.resume_after = previous.resume_after
                state.remaining = previous.remaining
                if previous.current is not None and previous.current in state.queue:
                    state.current = previous.current
                    state.current_kind = previous.current_kind
                    state.shown_at = previous.shown_at
            kept[slot_id] = state
        self.slots = kept
        changed: list[str] = []
        for slot_id, slot in self.slots.items():
            if slot.current is None:
                widget_id = self.choose(slot_id)
                if widget_id is not None:
                    self.assign(slot_id, widget_id)
                    if slot.pinned:
                        slot.shown_at = float("-inf")
                    changed.append(slot_id)
        return changed


def _rotate(queue: tuple[str, ...], after: str | None) -> tuple[str, ...]:
    if after is None or after not in queue:
        return queue
    index = queue.index(after)
    return queue[index + 1 :] + queue[: index + 1]
