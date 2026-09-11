from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class PocketRoute(str, Enum):
    REJECT = "reject"
    NORMAL = "normal"


@dataclass(slots=True)
class PocketLoad:
    pocket_id: int
    payload: Any
    admitted_at_mono: float
    indexes_traveled: int = 0
    route: PocketRoute = PocketRoute.REJECT
    reject_reason: str | None = "unresolved"
    route_locked: bool = False
    distribution_placed: bool = False

    def lock_route(self, route: PocketRoute, reason: str | None = None) -> None:
        if self.route_locked:
            return
        self.route = route
        self.reject_reason = reason
        self.route_locked = True


class PocketLedger:
    """Pure logical ownership for an absolute, one-pocket C4 route."""

    def __init__(
        self,
        *,
        sector_count: int,
        exit_advance: int,
        travel_sign: int,
        boundary_index: int,
    ) -> None:
        if not 1 <= exit_advance < sector_count:
            raise ValueError("exit_advance must be inside the rotor")
        if travel_sign not in (-1, 1):
            raise ValueError("travel_sign must be -1 or 1")
        self.sector_count = int(sector_count)
        self.exit_advance = int(exit_advance)
        self.travel_sign = int(travel_sign)
        self.boundary_index = int(boundary_index)
        self._loads: list[PocketLoad] = []

    @property
    def loads(self) -> tuple[PocketLoad, ...]:
        return tuple(self._loads)

    @property
    def intake_pocket_id(self) -> int:
        return self.boundary_index % self.sector_count

    @property
    def can_admit(self) -> bool:
        return (
            len(self._loads) < self.exit_advance
            and all(load.pocket_id != self.intake_pocket_id for load in self._loads)
        )

    def admit(self, payload: Any, *, admitted_at_mono: float) -> PocketLoad:
        if not self.can_admit:
            raise RuntimeError("intake pocket is not available")
        load = PocketLoad(
            pocket_id=self.intake_pocket_id,
            payload=payload,
            admitted_at_mono=float(admitted_at_mono),
        )
        self._loads.append(load)
        return load

    def due_to_exit_on_next_index(self) -> PocketLoad | None:
        due = [
            load
            for load in self._loads
            if load.indexes_traveled == self.exit_advance - 1
        ]
        if len(due) > 1:
            raise RuntimeError("multiple loads claim the exit pocket")
        return due[0] if due else None

    def advance_to(self, boundary_index: int) -> tuple[PocketLoad, ...]:
        expected = self.boundary_index + self.travel_sign
        if int(boundary_index) != expected:
            raise RuntimeError(
                f"non-sequential pocket index: expected {expected}, got {boundary_index}"
            )
        self.boundary_index = expected
        for load in self._loads:
            load.indexes_traveled += 1
        exited = tuple(
            load for load in self._loads if load.indexes_traveled >= self.exit_advance
        )
        self._loads = [load for load in self._loads if load not in exited]
        return exited

    def clear(self) -> tuple[PocketLoad, ...]:
        loads = tuple(self._loads)
        self._loads = []
        return loads
