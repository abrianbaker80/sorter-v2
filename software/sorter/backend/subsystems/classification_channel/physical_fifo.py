"""Pure ten-pocket clockwise C4 model; no hardware or runtime integration.

Physical IDs are fixed: at boundary zero pocket 0 is at P6, pocket 1 at
P7, etc. Each completed clockwise index decrements every station modulo 10.
P0 -> P9 is the EXIT sweep: the load leaves, while its physical pocket stays.
The caller alone establishes physical completion. Submission, acknowledgment,
interruption and retry have no completion semantics here.

Single-writer API. Immutable snapshots may be passed to asynchronous workers;
results must return through the writer with the pocket generation. Generations
are unbounded Python integers, monotonic within this model's lifetime. A later
runtime must segregate callbacks across model replacement (e.g. a run epoch).
"""

from dataclasses import dataclass, replace
from enum import Enum
from fractions import Fraction
from typing import Mapping


POCKET_COUNT = 10
INTAKE_STATION = 6
DISCHARGE_ADVANCES = 7


def _integer(value: int, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value


class PocketState(str, Enum):
    EMPTY = "EMPTY"
    PENDING = "PENDING"
    ROUTED = "ROUTED"
    DISCARD = "DISCARD"


@dataclass(frozen=True, slots=True)
class Pocket:
    pocket_id: int
    generation: int = 0
    state: PocketState = PocketState.EMPTY
    destination: str | None = None
    deposited_boundary: int | None = None
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class IndexTarget:
    boundary: int
    absolute_microsteps: int


@dataclass(frozen=True, slots=True)
class Discharge:
    pocket: Pocket
    boundary: int


class PhysicalC4FIFO:
    """Ten fixed slots and one confirmed rotor boundary.

    ``microsteps_per_revolution`` describes the rotor output, including gearing,
    and must be an exact integer or Fraction. ``clockwise_sign`` maps clockwise
    to the configured motor coordinate; it does not change the station sequence.
    Absolute targets round once, to nearest microstep, ties toward +infinity.
    No persisted state restoration or physical-position inference is provided.
    """

    def __init__(
        self,
        *,
        microsteps_per_revolution: int | Fraction,
        origin_microsteps: int = 0,
        clockwise_sign: int = 1,
        initial_boundary: int = 0,
    ) -> None:
        if type(microsteps_per_revolution) not in (int, Fraction):
            raise ValueError("microsteps_per_revolution must be exact")
        if microsteps_per_revolution < POCKET_COUNT:
            raise ValueError("each index must span at least one microstep")
        self._origin = _integer(origin_microsteps, "origin_microsteps")
        self._sign = _integer(clockwise_sign, "clockwise_sign")
        if self._sign not in (-1, 1):
            raise ValueError("clockwise_sign must be -1 or 1")
        self._steps_per_index = Fraction(microsteps_per_revolution, POCKET_COUNT)
        self._boundary = _integer(initial_boundary, "initial_boundary")
        if initial_boundary < 0:
            raise ValueError("initial boundary must be nonnegative")
        self._pending: IndexTarget | None = None
        self._pockets = [Pocket(i) for i in range(POCKET_COUNT)]

    @property
    def boundary(self) -> int:
        return self._boundary

    @property
    def pockets(self) -> tuple[Pocket, ...]:
        return tuple(self._pockets)

    @property
    def pending_index(self) -> IndexTarget | None:
        return self._pending

    def _pocket(self, pocket_id: int) -> Pocket:
        _integer(pocket_id, "pocket_id")
        if not 0 <= pocket_id < POCKET_COUNT:
            raise ValueError("pocket_id must be in [0, 9]")
        return self._pockets[pocket_id]

    def station_of(self, pocket_id: int) -> int:
        self._pocket(pocket_id)
        return (INTAKE_STATION + pocket_id - self._boundary) % POCKET_COUNT

    @property
    def intake_pocket_id(self) -> int:
        return self._boundary % POCKET_COUNT

    def deposit(self, metadata: Mapping[str, str] | None = None) -> Pocket:
        """Deposit at stationary P6; return the new generation's snapshot.

        Once an index is prepared, deposit is forbidden until completion. This
        prevents assigning material to an ambiguous in-motion station mapping.
        Metadata is optional string pairs, copied into an immutable snapshot.
        """
        if self._pending is not None:
            raise RuntimeError("cannot deposit during an outstanding index")
        old = self._pocket(self.intake_pocket_id)
        if old.state is not PocketState.EMPTY:
            raise RuntimeError("P6 is occupied")
        pairs = tuple((metadata or {}).items())
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in pairs):
            raise ValueError("metadata keys and values must be strings")
        load = Pocket(old.pocket_id, old.generation + 1, PocketState.PENDING,
                      deposited_boundary=self._boundary, metadata=tuple(sorted(pairs)))
        self._pockets[old.pocket_id] = load
        return load

    def resolve(
        self, pocket_id: int, generation: int, state: PocketState,
        *, destination: str | None = None,
    ) -> bool:
        """Resolve PENDING once; stale, empty or already resolved returns False.

        Route commitment/overrides belong to the later scheduler, not this core.
        No route is required to complete an index or emit a discharge.
        """
        pocket = self._pocket(pocket_id)
        _integer(generation, "generation")
        if state not in (PocketState.ROUTED, PocketState.DISCARD) or not isinstance(state, PocketState):
            raise ValueError("resolution must be ROUTED or DISCARD")
        if state is PocketState.ROUTED:
            if not isinstance(destination, str) or not destination.strip():
                raise ValueError("ROUTED requires a nonempty destination")
        elif destination is not None:
            raise ValueError("DISCARD cannot have a routed destination")
        if pocket.generation != generation or pocket.state is not PocketState.PENDING:
            return False
        self._pockets[pocket_id] = replace(pocket, state=state, destination=destination)
        return True

    def discard_all(self, *, include_unknown: bool = False) -> None:
        """Recovery only: retain generations/routes as reject custody until EXIT.

        Unknown empty positions receive a generation too. No occupancy is erased
        by this operation, and no ordinary recognition result can undo it.
        """
        if self._pending is not None:
            raise RuntimeError("finish the owned index before starting recovery")
        for pocket in self._pockets:
            if pocket.state is not PocketState.EMPTY:
                self._pockets[pocket.pocket_id] = replace(
                    pocket, state=PocketState.DISCARD, destination=None)
            elif include_unknown:
                self._pockets[pocket.pocket_id] = Pocket(
                    pocket.pocket_id, pocket.generation + 1, PocketState.DISCARD,
                    deposited_boundary=self._boundary,
                    metadata=(("recovery", "unknown"),))

    def reestablish_for_recovery(self, boundary: int) -> None:
        """Exclusive recovery after stopped motor + a fresh ConfirmedIndex.

        This records observed geometry, never a normal discharge. Interrupted
        custody is conservatively swept again. Generations remain unchanged.
        """
        _integer(boundary, "boundary")
        if boundary < self._boundary:
            raise ValueError("recovery boundary must be monotonic")
        self._boundary = boundary
        self._pending = None
        self.discard_all(include_unknown=True)

    def target_for(self, boundary: int) -> IndexTarget:
        _integer(boundary, "boundary")
        if boundary < 0:
            raise ValueError("boundary must be nonnegative")
        exact = self._origin + self._sign * boundary * self._steps_per_index
        rounded = (2 * exact.numerator + exact.denominator) // (2 * exact.denominator)
        return IndexTarget(boundary, rounded)

    def prepare_index(self) -> IndexTarget:
        """Return the same absolute target for submission, retry or resume.

        Preparing never changes the confirmed boundary or pocket state.
        """
        if self._pending is None:
            self._pending = self.target_for(self._boundary + 1)
        return self._pending

    def complete_index(
        self, target: IndexTarget, *, confirmed_microsteps: int,
    ) -> tuple[Discharge, ...]:
        """Commit only after the caller confirms motion stopped at this target.

        Position feedback alone is not a stop signal: the adapter must establish
        both. Bad/out-of-order completion leaves all state intact. An already
        completed target is a no-op, including while a later index is pending.
        Discharge events are returned only once; delivery persistence is outside
        this model.
        """
        if not isinstance(target, IndexTarget):
            raise ValueError("expected IndexTarget")
        _integer(target.absolute_microsteps, "absolute_microsteps")
        if target != self.target_for(target.boundary) or target.boundary == 0:
            raise ValueError("invalid absolute target")
        if _integer(confirmed_microsteps, "confirmed_microsteps") != target.absolute_microsteps:
            raise ValueError("completion position does not match target")
        if target.boundary <= self._boundary:
            return ()
        if target != self._pending:
            raise ValueError("completion is not the outstanding index")
        self._boundary = target.boundary
        self._pending = None
        exited = []
        for pocket in self._pockets:
            if (pocket.state is not PocketState.EMPTY
                    and self.station_of(pocket.pocket_id) == 9):
                exited.append(Discharge(pocket, self._boundary))
                self._pockets[pocket.pocket_id] = Pocket(pocket.pocket_id, pocket.generation)
        return tuple(exited)
