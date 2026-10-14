"""Spatial card navigation: which card is one step away in a direction.

Pure geometry, so the rule can be tested without a terminal. The application asks for the
neighbour of the focused card; this module answers with a key, or ``None`` when there is
nothing that way.

Left and right wrap within the visible set, so walking a row of cards with the arrow keys
does not stop at the end. Up and down do not wrap: a card above the top row is not where
the user expects to land.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["Box", "neighbour"]

DIRECTIONS = ("left", "right", "up", "down")


@dataclass(frozen=True, slots=True)
class Box:
    """A card's place on screen, in cells."""

    key: str
    x: int
    y: int
    width: int
    height: int

    @property
    def centre_x(self) -> float:
        return self.x + self.width / 2

    @property
    def centre_y(self) -> float:
        return self.y + self.height / 2


def neighbour(boxes: Sequence[Box], current: str, direction: str) -> str | None:
    """The nearest card in ``direction`` from ``current``, or ``None``.

    "Nearest" is the smallest step along the axis of travel, with the distance along the
    other axis as the tie-break: moving right from the left column lands on the card that
    is beside it, not on the one diagonally far away.
    """
    if direction not in DIRECTIONS:
        return None
    here = next((box for box in boxes if box.key == current), None)
    if here is None:
        return None
    others = [box for box in boxes if box.key != current]
    if not others:
        return None

    candidates: list[tuple[float, float, str]] = []
    for box in others:
        if direction == "right" and box.x > here.x:
            candidates.append((box.x - here.x, abs(box.centre_y - here.centre_y), box.key))
        elif direction == "left" and box.x < here.x:
            candidates.append((here.x - box.x, abs(box.centre_y - here.centre_y), box.key))
        elif direction == "down" and box.y > here.y:
            candidates.append((box.y - here.y, abs(box.centre_x - here.centre_x), box.key))
        elif direction == "up" and box.y < here.y:
            candidates.append((here.y - box.y, abs(box.centre_x - here.centre_x), box.key))
    if candidates:
        candidates.sort()
        return candidates[0][2]

    if direction == "right":
        # Wrap to the leftmost card of the row, then walk again from there.
        return min(others, key=lambda box: (box.x, box.y)).key
    if direction == "left":
        # …and to the last card in reading order, so wrapping backwards from the first
        # card lands where "backwards" ends.
        return max(others, key=lambda box: (box.x, box.y)).key
    return None
