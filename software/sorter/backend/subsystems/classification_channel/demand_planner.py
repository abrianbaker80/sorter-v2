"""Offline C4 demand/chute coordinator. No production runtime imports.

One writer owns the FIFO and this planner. Call plan immediately before issuing
its directives. Once it returns an index, repeated calls HOLD; an interrupted
adapter retains fifo.pending_index, never creates a new index. Retaining that
target does not authorize resumed motion: the later adapter must revalidate
chute readiness against the remaining physical distance to release. This
offline planner does not infer partial-index geometry or authorize resume.
Physical completion is reported separately. All times are caller-supplied on
one monotonic clock. This module neither polls hardware nor reads a clock.
"""

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Mapping
from types import MappingProxyType

from .physical_fifo import Discharge, IndexTarget, PhysicalC4FIFO, Pocket, PocketState


# Compatibility source: distribution/sending.py CHUTE_SETTLE_MS, normal
# Sending._settleMs(). NOT the 400 ms sample mode, 0.8 s extra cooldown, tracker
# disappearance gate or 8 s incident timer. No independent gravity calibration
# exists in this candidate; a future adapter must supply a measured override.
NORMAL_FALL_CLEAR_S = 1.5


def _seconds(value: float, name: str) -> None:
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")


class AdvanceKind(str, Enum):
    FEED = "FEED"
    DRAIN = "DRAIN"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Timing:
    # From start of this index to the physical P0 release point, not to motor
    # stop. Required: do not infer from the legacy post-drop waiting interval.
    release_after_start_s: float
    arrival_margin_s: float
    fall_clear_s: float = NORMAL_FALL_CLEAR_S
    fall_clear_by_destination: Mapping[str, float] | None = None

    def __post_init__(self) -> None:
        for name in ("release_after_start_s", "arrival_margin_s", "fall_clear_s"):
            _seconds(getattr(self, name), name)
        if self.fall_clear_by_destination is not None:
            values = dict(self.fall_clear_by_destination)
            for destination, seconds in values.items():
                if not destination.strip():
                    raise ValueError("empty fall-clear destination")
                _seconds(seconds, "destination fall-clear")
            object.__setattr__(self, "fall_clear_by_destination", MappingProxyType(values))

    def fall_clear_for(self, destination: str) -> float:
        if self.fall_clear_by_destination is None:
            return self.fall_clear_s
        return self.fall_clear_by_destination[destination]


@dataclass(frozen=True)
class RouteDeadline:
    pocket_id: int
    generation: int
    at: float


@dataclass(frozen=True)
class ChuteObservation:
    """Fresh physical feedback and calibrated estimates, provided by adapter.

    aligned_destination means stopped AND doors/position verified. A non-None
    moving_destination overrides it. arrival_at is the prediction for that
    in-flight move including door readiness. travel_seconds contains estimates
    from the present stopped configuration, including doors, only for reachable
    destinations. Missing estimates never authorize a release.
    """
    aligned_destination: str | None
    travel_seconds: Mapping[str, float]
    moving_destination: str | None = None
    arrival_at: float | None = None

    def __post_init__(self) -> None:
        for value in self.travel_seconds.values():
            _seconds(value, "chute travel")
        if self.arrival_at is not None:
            _seconds(self.arrival_at, "chute arrival")
        if (self.moving_destination is None) != (self.arrival_at is None):
            raise ValueError("moving destination and arrival prediction must be paired")


@dataclass(frozen=True)
class ChuteMove:
    destination: str
    depart_at: float
    arrive_at: float


@dataclass(frozen=True)
class Decision:
    kind: AdvanceKind
    reason: str
    index: IndexTarget | None = None
    chute_move: ChuteMove | None = None
    release_at: float | None = None


class C4DemandPlanner:
    def __init__(self, fifo: PhysicalC4FIFO, timing: Timing, *, discard_destination: str):
        if not discard_destination.strip():
            raise ValueError("discard destination is required")
        if fifo.pending_index is not None:
            raise ValueError("attach planner before preparing an index")
        self.fifo = fifo
        self.timing = timing
        self.discard_destination = discard_destination
        self._active: Decision | None = None
        self._started_at = 0.0
        self._last_now = 0.0
        self._fall_clear_at = 0.0

    @property
    def fall_clear_at(self) -> float:
        return self._fall_clear_at

    def _time(self, now: float) -> None:
        _seconds(now, "now")
        if now < self._last_now:
            raise ValueError("clock moved backwards")
        self._last_now = now

    def _loads(self) -> list[Pocket]:
        return sorted(
            (p for p in self.fifo.pockets if p.state is not PocketState.EMPTY),
            key=lambda p: p.deposited_boundary if p.deposited_boundary is not None else -1,
        )

    def _destination(self, pocket: Pocket) -> str | None:
        if pocket.state is PocketState.DISCARD:
            return self.discard_destination
        return pocket.destination if pocket.state is PocketState.ROUTED else None

    def _position(
        self, destination: str, now: float, chute: ChuteObservation,
    ) -> tuple[float | None, ChuteMove | None]:
        if chute.moving_destination is not None:
            # An overdue prediction is not proof of arrival. Require fresh
            # stopped/aligned feedback instead of trusting an expired ETA.
            if chute.moving_destination == destination and chute.arrival_at is not None and chute.arrival_at > now:
                return chute.arrival_at, None
            return None, None
        if chute.aligned_destination == destination:
            return now, None
        travel = chute.travel_seconds.get(destination)
        if travel is None:
            return None, None
        _seconds(travel, "chute travel")
        # Never issue a future-dated movement that could survive a later route
        # change. Replan at fall_clear_at and issue the directive then.
        if now < self._fall_clear_at:
            return None, None
        return now + travel, ChuteMove(destination, now, now + travel)

    def plan(
        self, now: float, chute: ChuteObservation, *, drain: bool = False,
        deadlines: tuple[RouteDeadline, ...] = (),
    ) -> Decision:
        self._time(now)
        if self._active is not None:
            return Decision(AdvanceKind.HOLD, "index outstanding", index=self._active.index)
        for deadline in deadlines:
            _seconds(deadline.at, "routing deadline")
            if now >= deadline.at:
                self.fifo.resolve(deadline.pocket_id, deadline.generation, PocketState.DISCARD)
        loads = self._loads()
        due = next((p for p in loads if self.fifo.station_of(p.pocket_id) == 0), None)
        # P0 is the final routing deadline. A request cannot extend transport
        # residence: unresolved means discard before the exit-producing sweep.
        if due is not None and due.state is PocketState.PENDING:
            self.fifo.resolve(due.pocket_id, due.generation, PocketState.DISCARD)
            loads = self._loads()
            due = self.fifo.pockets[due.pocket_id]
        future = next((p for p in loads if self._destination(p) is not None), None)
        destination = self._destination(future) if future is not None else None
        arrival, move = self._position(destination, now, chute) if destination is not None else (None, None)
        intake = self.fifo.pockets[self.fifo.intake_pocket_id]
        if intake.state is not PocketState.EMPTY:
            kind = AdvanceKind.FEED
        elif drain and loads:
            kind = AdvanceKind.DRAIN
        else:
            return Decision(AdvanceKind.HOLD, "no demand", chute_move=move)
        release = now + self.timing.release_after_start_s if due is not None else None
        if release is not None:
            assert due is not None
            aligned = chute.moving_destination is None and chute.aligned_destination == self._destination(due)
            timely = arrival is not None and arrival + self.timing.arrival_margin_s <= release
            if release < self._fall_clear_at or not (aligned or timely):
                return Decision(AdvanceKind.HOLD, "chute not ready for release", chute_move=move)
        target = self.fifo.prepare_index()
        self._active = Decision(kind, "advance", target, move, release)
        self._started_at = now
        return self._active

    def complete_index(
        self, target: IndexTarget, *, confirmed_microsteps: int,
        now: float, physical_release_at: float | None = None,
    ) -> tuple[Discharge, ...]:
        """Report stopped-at-target completion, not submission/ACK.

        Supply actual physical release time when known. Otherwise completion
        time starts fall-clear conservatively; the predicted release is never
        treated as physical evidence. Duplicate completion cannot extend the
        fall timer or clear a newer outstanding target.
        """
        self._time(now)
        release = now if physical_release_at is None else physical_release_at
        _seconds(release, "physical release")
        if target.boundary > self.fifo.boundary:
            if self._active is None or target != self._active.index:
                raise ValueError("index was not planned")
            if not self._started_at <= release <= now:
                raise ValueError("release outside this physical index interval")
        old_boundary = self.fifo.boundary
        # Validate before consuming the FIFO target; a missing adapter route
        # must never lose custody and then fail while calculating its timer.
        due = next((p for p in self._loads() if self.fifo.station_of(p.pocket_id) == 0), None)
        duration = (self.timing.fall_clear_for(self._destination(due))
                    if target.boundary > old_boundary and due is not None else 0.0)
        events = self.fifo.complete_index(target, confirmed_microsteps=confirmed_microsteps)
        if self.fifo.boundary != old_boundary:
            self._active = None
        if events:
            self._fall_clear_at = max(self._fall_clear_at, release + duration)
        return events
