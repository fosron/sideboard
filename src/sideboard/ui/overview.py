"""Overview pane: the responsive rotating grid.

Measured in terminal columns and rows, never iPad pixels.

* ``grid4``  — ≥110 columns and ≥32 rows: every configured slot, two per row, in order
* ``stack2`` — ≥70 columns: the pinned slot on top, merged rotating queue below
* ``single`` — ≥60×20: one card normally, pins preserved
* ``compact`` — below 60×20: a useful one-line status list

Narrowing merges rotating queues; it never drops a widget from the queue, and pins
survive every narrowing. Slot contents are decided by :class:`RotationController`, so
a rebuild after a resize preserves the current card, holds and pins by stable ID.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from textual.containers import Horizontal, Vertical
from textual.widget import Widget

from .card import CardShell

__all__ = ["LayoutMode", "OverviewPane", "SlotSpec", "layout_mode", "slot_specs"]

GRID_COLUMNS = 110
GRID_ROWS = 32
STACK_COLUMNS = 70
SINGLE_COLUMNS = 60
SINGLE_ROWS = 20

_ID_SAFE = str.maketrans({"+": "-", ":": "-", ".": "-", " ": "-"})


def _widget_id(raw: str) -> str:
    """Textual widget ids allow letters, digits, underscores and hyphens only."""
    return raw.translate(_ID_SAFE)


@dataclass(frozen=True, slots=True)
class SlotSpec:
    """One on-screen slot: which configured queues it draws from."""

    slot_id: str
    queue: tuple[str, ...]
    pinned: bool
    dwell: float
    label: str
    displays: tuple[str, ...] = ()


def layout_mode(width: int, height: int) -> LayoutMode:
    if width < SINGLE_COLUMNS or height < SINGLE_ROWS:
        return "compact"
    if width >= GRID_COLUMNS and height >= GRID_ROWS:
        return "grid4"
    if width >= STACK_COLUMNS:
        return "stack2"
    return "single"


LayoutMode = str


def slot_specs(
    slots: Mapping[str, Sequence[str]],
    dwell: Mapping[str, float],
    pinned: Mapping[str, bool],
    *,
    mode: LayoutMode,
) -> tuple[SlotSpec, ...]:
    """Translate configured slots into on-screen slots for one layout mode."""
    ordered = sorted(slots.items())
    pinned_slots = [(sid, queue) for sid, queue in ordered if pinned.get(sid, False)]
    rotating = [(sid, queue) for sid, queue in ordered if not pinned.get(sid, False)]
    primary = pinned_slots[0] if pinned_slots else (rotating[0] if rotating else None)
    rest = [item for item in rotating if primary is None or item[0] != primary[0]]

    if mode == "grid4":
        return tuple(
            SlotSpec(
                slot_id=slot_id,
                queue=tuple(queue),
                pinned=bool(pinned.get(slot_id, False)),
                dwell=float(dwell.get(slot_id, 40.0)),
                label=slot_id.upper(),
                displays=(slot_id,),
            )
            for slot_id, queue in ordered
        )
    if mode == "stack2":
        specs: list[SlotSpec] = []
        if primary is not None:
            specs.append(
                SlotSpec(
                    slot_id=primary[0],
                    queue=tuple(primary[1]),
                    pinned=True,
                    dwell=float(dwell.get(primary[0], 40.0)),
                    label=primary[0].upper(),
                    displays=(primary[0],),
                )
            )
        if rest:
            specs.append(_merged("c+d", rest, dwell, label="ROTATING"))
        return tuple(specs)

    combined = ([primary] if primary is not None else []) + rest
    if not combined:
        return ()
    return (_merged("a+c+d" if mode == "single" else "compact", combined, dwell, label="ALL"),)


def _merged(
    slot_id: str,
    parts: Sequence[tuple[str, Sequence[str]]],
    dwell: Mapping[str, float],
    *,
    label: str,
) -> SlotSpec:
    queue: list[str] = []
    for _, widgets in parts:
        for widget_id in widgets:
            if widget_id not in queue:
                queue.append(widget_id)
    minimum = min((float(dwell.get(part_id, 40.0)) for part_id, _ in parts), default=40.0)
    return SlotSpec(
        slot_id=slot_id,
        queue=tuple(queue),
        pinned=False,
        dwell=minimum,
        label=label,
        displays=tuple(part_id for part_id, _ in parts),
    )


class OverviewPane(Widget):
    """Container of :class:`CardShell` slots, rebuilt only when the layout changes."""

    def __init__(self, *, id: str | None = "sb-overview-grid", classes: str = "") -> None:
        super().__init__(id=id, classes=classes)
        self.mode: LayoutMode | None = None
        self.specs: tuple[SlotSpec, ...] = ()
        self._cards: dict[str, CardShell] = {}

    @property
    def cards(self) -> Mapping[str, CardShell]:
        return self._cards

    def card(self, slot_id: str) -> CardShell | None:
        return self._cards.get(slot_id)

    def compose(self) -> object:
        yield Vertical(id="sb-overview-rows")

    async def rebuild(self, mode: LayoutMode, specs: Sequence[SlotSpec]) -> None:
        """Mount one card per slot. Called on resize and on config reload."""
        self.mode = mode
        self.specs = tuple(specs)
        rows = self.query_one("#sb-overview-rows", Vertical)
        await rows.remove_children()
        self._cards.clear()
        # Two slots per row, for as many slots as the configuration defines: a sixth slot
        # is a third row, not a card that silently never appears.
        chunks = (
            [self.specs[index : index + 2] for index in range(0, len(self.specs), 2)]
            if mode == "grid4"
            else [self.specs]
        )
        for index, chunk in enumerate(chunks):
            if not chunk:
                continue
            row = Horizontal(classes="sb-overview-row", id=f"sb-overview-row-{index}")
            await rows.mount(row)
            for spec in chunk:
                shell = CardShell(
                    card_key=spec.slot_id,
                    slot_label=spec.label,
                    id=_widget_id(f"sb-card-{spec.slot_id}"),
                )
                self._cards[spec.slot_id] = shell
                await row.mount(shell)

    def set_card(self, slot_id: str, **kwargs: object) -> None:
        shell = self._cards.get(slot_id)
        if shell is not None:
            shell.set_card(**kwargs)  # type: ignore[arg-type]
