"""Pure bin-content compatibility rules shared by legacy routing and the inactive ledger."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class BinEligibility:
    kind: str
    assignments: tuple[str, ...] = ()


def classify_bin(
    incoming: str,
    labels: tuple[str, ...],
    recorded_count: int,
    recorded: Mapping[str, int],
    *,
    allow_sharing: bool,
    misc: str = "misc",
) -> BinEligibility:
    """Classify one reachable, enabled, fitting, non-full bin.

    A missing label never erases occupancy; recorded MISC never becomes a
    physical assignment. Callers independently enforce count limits and fit.
    """
    if (
        type(recorded_count) is not int or recorded_count < 0
        or any(type(qty) is not int or qty <= 0 or not isinstance(key, str) or not key
               for key, qty in recorded.items())
        or sum(recorded.values()) != recorded_count
    ):
        return BinEligibility("uncertain_contents")
    known = set(recorded)
    assigned = set(labels)
    if misc in known or misc in assigned:
        return BinEligibility("recorded_misc")
    if assigned and not known.issubset(assigned):
        return BinEligibility("incompatible_contents")
    if incoming == misc:
        return BinEligibility("virtual_reject")
    if incoming in assigned:
        return BinEligibility("assigned_match", tuple(labels))
    if assigned:
        return BinEligibility("shared", tuple((*labels, incoming))) if allow_sharing else BinEligibility("no_share")
    if known:
        categories = tuple(sorted(known))
        if incoming in known:
            return (BinEligibility("recorded_match", categories)
                    if len(known) == 1 or allow_sharing else BinEligibility("no_share"))
        return (BinEligibility("shared", (*categories, incoming))
                if allow_sharing else BinEligibility("no_share"))
    return BinEligibility("empty", (incoming,))


def fits_dimension(piece_max_mm: float | None, layer_max_mm: float | None) -> bool:
    """Unknown dimensions and absent limits preserve the legacy permissive fit."""
    return not (
        isinstance(piece_max_mm, (int, float)) and not isinstance(piece_max_mm, bool)
        and isinstance(layer_max_mm, (int, float)) and not isinstance(layer_max_mm, bool)
        and layer_max_mm > 0 and piece_max_mm > layer_max_mm
    )
