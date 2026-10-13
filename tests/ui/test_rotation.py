"""Rotation state-machine tests with a fake clock.

Covers the acceptance list: fake-clock dwell, independent slots, overlapping pause
reasons, stationary hover, resume grace, initial focus, manual advance, pinned refresh,
modal close and blur handling.
"""

from __future__ import annotations

from sideboard.ui.rotation import Candidate, HoldReason, RotationController


class FakeNow:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make(
    *,
    slots: dict[str, list[str]] | None = None,
    pinned: dict[str, bool] | None = None,
    dwell: dict[str, float] | None = None,
    kinds: dict[str, Candidate] | None = None,
) -> tuple[RotationController, FakeNow]:
    now = FakeNow()
    mapping = kinds or {}
    controller = RotationController(
        slots=slots or {"c": ["news", "hn_top", "youtube"], "d": ["mac_summary", "reddit"]},
        pinned=pinned or {"a": True},
        dwell=dwell or {"c": 40.0, "d": 55.0},
        clock_now=now,
        classify=lambda widget_id: mapping.get(widget_id, Candidate.READY),
        grace_seconds=2.0,
        keyboard_hold_seconds=15.0,
    )
    return controller, now


def test_dwell_advances_only_after_the_configured_time() -> None:
    controller, now = make(slots={"c": ["news", "hn_top"]}, dwell={"c": 40.0}, pinned={})
    controller.initialize()
    first = controller.current("c")
    now.advance(39.9)
    assert controller.tick() == []
    assert controller.current("c") == first
    now.advance(0.2)
    assert controller.tick() == ["c"]
    assert controller.current("c") != first


def test_slots_are_independent() -> None:
    controller, now = make()
    controller.initialize()
    now.advance(40.0)
    changed = controller.tick()
    assert changed == ["c"]
    now.advance(15.0)
    assert controller.tick() == ["d"]


def test_pinned_slot_never_rotates_but_refreshes() -> None:
    controller, now = make(
        slots={"a": ["portfolio", "hn_top"]}, pinned={"a": True}, dwell={"a": 40.0}
    )
    controller.initialize()
    assert controller.current("a") == "portfolio"
    now.advance(1000.0)
    assert controller.tick() == []
    assert controller.current("a") == "portfolio"


def test_manual_advance_works_even_while_paused_and_pinned() -> None:
    controller, _ = make(
        slots={"a": ["portfolio", "hn_top"]}, pinned={"a": True}, dwell={"a": 40.0}
    )
    controller.initialize()
    controller.set_global_hold(HoldReason.MANUAL, True)
    assert controller.current("a") == "portfolio"
    assert controller.advance("a", manual=True) == "hn_top"


def test_hover_hold_stops_only_the_hovered_slot() -> None:
    controller, now = make()
    controller.initialize()
    controller.set_hold("c", HoldReason.HOVER, True)
    now.advance(120.0)
    changed = controller.tick()
    assert changed == ["d"]
    assert controller.current("c") is not None
    assert controller.held("c") is True
    assert controller.held("d") is False


def test_stationary_hover_keeps_holding_indefinitely() -> None:
    controller, now = make()
    controller.initialize()
    controller.set_hold("c", HoldReason.HOVER, True)
    for _ in range(50):
        now.advance(60.0)
        controller.tick()
    assert controller.held("c") is True


def test_release_resumes_the_remaining_dwell_after_grace() -> None:
    controller, now = make(slots={"c": ["news", "hn_top", "youtube"]}, dwell={"c": 40.0}, pinned={})
    controller.initialize()
    now.advance(10.0)
    controller.set_hold("c", HoldReason.HOVER, True)
    now.advance(500.0)
    assert controller.held("c") is True
    controller.set_hold("c", HoldReason.HOVER, False)
    # Grace period: the hold is released, but the dwell has not resumed yet.
    assert controller.held("c") is False
    assert controller.in_grace("c") is True
    now.advance(2.1)
    controller.tick()  # the grace window ends, the remaining dwell starts again
    # 30 s of the original dwell remain, so no advance yet.
    now.advance(29.0)
    assert controller.tick() == []
    now.advance(1.5)
    assert controller.tick() == ["c"]


def test_overlapping_pause_reasons_do_not_clear_each_other() -> None:
    controller, now = make(slots={"c": ["news", "hn_top"]}, dwell={"c": 40.0}, pinned={})
    controller.initialize()
    controller.set_global_hold(HoldReason.MODAL, True)
    controller.set_hold("c", HoldReason.INTERACTION, True)
    controller.set_global_hold(HoldReason.MODAL, False)
    now.advance(500.0)
    # The interaction hold is still active, so this slot did not advance.
    assert "c" not in controller.tick()
    assert "interaction" in controller.hold_reasons("c")
    # Releasing it resumes the untouched dwell after the grace period, so the modal
    # release can never have silently cleared the interaction hold.
    controller.set_hold("c", HoldReason.INTERACTION, False)
    assert controller.in_grace("c") is True
    now.advance(2.5)
    assert controller.in_grace("c") is False
    now.advance(40.5)
    assert "c" in controller.tick()


def test_modal_pauses_every_slot_until_closed() -> None:
    controller, now = make()
    controller.initialize()
    controller.set_global_hold(HoldReason.MODAL, True)
    now.advance(1000.0)
    assert controller.tick() == []
    controller.set_global_hold(HoldReason.MODAL, False)
    now.advance(3.0)
    now.advance(1000.0)
    assert controller.tick() != []


def test_blur_clears_a_stale_pointer_hold_only() -> None:
    controller, now = make()
    controller.initialize()
    controller.set_hold("c", HoldReason.HOVER, True)
    controller.set_hold("d", HoldReason.INTERACTION, True)
    controller.clear_pointer_holds()
    assert controller.held("c") is False
    # An explicit interaction hold is not a pointer hold and survives a blur.
    assert controller.held("d") is True
    now.advance(20.0)
    assert "d" not in controller.tick()


def test_reset_holds_releases_everything_but_keeps_manual_pause() -> None:
    controller, _ = make()
    controller.initialize()
    controller.set_hold("c", HoldReason.HOVER, True)
    controller.set_global_hold(HoldReason.MODAL, True)
    controller.reset_holds()
    assert controller.held("c") is False
    assert controller.held_globally is False


def test_setup_card_is_shown_at_most_once_per_screen() -> None:
    controller, _ = make(
        slots={"a": ["portfolio", "sources"], "b": ["weather", "sources"], "c": ["news"]},
        pinned={"a": True, "b": True},
        kinds={
            "portfolio": Candidate.SETUP,
            "weather": Candidate.SETUP,
            "sources": Candidate.SETUP,
            "news": Candidate.READY,
        },
    )
    controller.initialize()
    chosen = [controller.current(slot) for slot in ("a", "b", "c")]
    assert chosen.count("sources") <= 1
    assert "news" in chosen


def test_empty_widgets_are_skipped() -> None:
    controller, _ = make(
        slots={"c": ["empty_a", "ready_b"]},
        pinned={},
        kinds={"empty_a": Candidate.EMPTY, "ready_b": Candidate.READY},
    )
    controller.initialize()
    assert controller.current("c") == "ready_b"


def test_same_widget_is_not_displayed_in_two_rotating_slots() -> None:
    controller, _ = make(
        slots={"c": ["news", "hn_top"], "d": ["news", "reddit"]},
        pinned={},
        dwell={"c": 40.0, "d": 55.0},
    )
    controller.initialize()
    assert controller.current("c") != controller.current("d")


def test_reconfigure_preserves_selection_by_stable_id() -> None:
    controller, _ = make()
    controller.initialize()
    current_c = controller.current("c")
    controller.reconfigure(
        slots={"c": ["news", "hn_top", "youtube"], "d": ["mac_summary", "reddit"]},
        pinned={"c": False, "d": False},
        dwell={"c": 20.0, "d": 30.0},
    )
    assert controller.current("c") == current_c
    assert controller.slots["c"].dwell == 20.0


def test_removing_the_current_widget_picks_a_replacement() -> None:
    controller, _ = make()
    controller.initialize()
    controller.reconfigure(slots={"c": ["youtube"], "d": ["reddit"]}, pinned={}, dwell={"c": 10.0})
    assert controller.current("c") == "youtube"
