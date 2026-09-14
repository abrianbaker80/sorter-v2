"""Explicitly constructed, single-writer C4 integration; no production selector.

Commands use the existing StepperMotor surface. ChuteIO binds calibrated chute
and door operations externally; observations must be fresh physical feedback.
No device is constructed, configured, homed or enabled by this module.
"""

from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
from math import isfinite
from threading import get_ident
from typing import Protocol, Sequence

from perception.capture import PerceptionFrame
from perception.state import PieceObservation
from .demand_planner import (
    AdvanceKind, C4DemandPlanner, ChuteMove, ChuteObservation, Decision,
)
from .intake_routing import C4IntakeRoutingBridge
from .physical_fifo import IndexTarget, Pocket, PocketState


class IndexMotor(Protocol):
    # Structural subset of hardware.sorter_interface.StepperMotor.
    @property
    def stopped(self) -> bool: ...

    @property
    def position(self) -> int: ...

    def move_steps(self, steps: int) -> bool: ...


class ChuteIO(Protocol):
    """Bind existing Chute geometry/motion and servo readiness, or a simulator.

    move must submit immediately, without waiting for arrival, and raise on
    rejection/uncertain submission. observe includes doors in aligned feedback
    and estimates. A future physical binding must verify calibration and reuse
    Chute.getAngleForBin/moveToBin and stepper.estimateMoveDegreesMs.
    """

    def observe(self, now: float) -> ChuteObservation: ...

    def move(self, move: ChuteMove) -> None: ...


class Lifecycle(str, Enum):
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    DRAINED = "DRAINED"
    FAULTED = "FAULTED"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class IndexCommand:
    epoch: object
    target: IndexTarget


@dataclass(frozen=True)
class PhysicalDischargeEvent:
    # Process-local idempotency key: (epoch, pocket_id, generation).
    epoch: object
    pocket_id: int
    generation: int
    boundary: int
    state: PocketState
    destination: str
    metadata: tuple[tuple[str, str], ...]


class C4RuntimeCoordinator:
    def __init__(
        self, bridge: C4IntakeRoutingBridge, planner: C4DemandPlanner,
        motor: IndexMotor, chute: ChuteIO,
    ) -> None:
        if bridge.fifo is not planner.fifo or planner.fifo.pending_index is not None:
            raise ValueError("attach to the same idle FIFO")
        self.bridge = bridge
        self.planner = planner
        self.fifo = planner.fifo
        self.motor = motor
        self.chute = chute
        self.lifecycle = Lifecycle.RUNNING
        self.fault: str | None = None
        self._owner = get_ident()
        self._epoch = object()
        self._now = 0.0
        self._active: IndexCommand | None = None
        self._chute_move: ChuteMove | None = None
        self._events: deque[PhysicalDischargeEvent] = deque()

    @property
    def active_index(self) -> IndexCommand | None:
        return self._active

    def _writer(self, now: float) -> None:
        if get_ident() != self._owner:
            raise RuntimeError("coordinator requires its single owner thread")
        if not isfinite(now) or now < self._now:
            raise ValueError("now must be finite, nonnegative and monotonic")
        self._now = now

    def confirmed_deposit(
        self, *, boundary: int, now: float,
        sample: tuple[Sequence[PieceObservation], PerceptionFrame] | None,
    ) -> Pocket | None:
        self._writer(now)
        if self.lifecycle is not Lifecycle.RUNNING:
            raise RuntimeError("intake is closed")
        return self.bridge.confirmed_deposit(boundary=boundary, now=now, sample=sample)

    def start_drain(self, now: float) -> None:
        self._writer(now)
        if self.lifecycle is Lifecycle.RUNNING:
            self.lifecycle = Lifecycle.DRAINING
        elif self.lifecycle not in (Lifecycle.DRAINING, Lifecycle.DRAINED):
            raise RuntimeError("cannot drain this lifecycle")

    def acknowledge_index(
        self, command: IndexCommand, *, stopped: bool, position: int,
        now: float, physical_release_at: float | None = None,
    ) -> bool:
        """Consume fresh stopped-at-target evidence, never a transport ACK.

        Callback owners retain the issued command, including its runtime epoch.
        Replays (even while a newer index is active) cannot mutate the FIFO.
        This remains usable after an uncertain command fault to record actual
        completion, but never authorizes further motion or automatic retry.
        """
        self._writer(now)
        if command.epoch is not self._epoch or command != self._active:
            return False
        if not stopped or position != command.target.absolute_microsteps:
            return False
        events = self.planner.complete_index(
            command.target, confirmed_microsteps=position, now=now,
            physical_release_at=physical_release_at,
        )
        self._active = None
        for event in events:
            pocket = event.pocket
            destination = (self.planner.discard_destination
                           if pocket.state is PocketState.DISCARD else pocket.destination)
            assert destination is not None
            self._events.append(PhysicalDischargeEvent(
                self._epoch, pocket.pocket_id, pocket.generation, event.boundary,
                pocket.state, destination, pocket.metadata,
            ))
        return True

    def take_discharge_events(self) -> tuple[PhysicalDischargeEvent, ...]:
        """Drain the in-memory outbox; consumers deduplicate by event key.

        No persistence or consumer callback can block physical transport.
        Crash-durable delivery belongs to a later accounting integration.
        """
        self._writer(self._now)
        events = tuple(self._events)
        self._events.clear()
        return events

    def _observe_chute(self, now: float) -> ChuteObservation:
        observation = self.chute.observe(now)
        move = self._chute_move
        if move is not None:
            if (observation.moving_destination is None
                    and observation.aligned_destination == move.destination):
                self._chute_move = None
            elif observation.moving_destination is None:
                # Submission is not arrival. Retain the original ETA; never
                # reissue a command or refresh an expired prediction on a tick.
                observation = replace(observation, moving_destination=move.destination,
                                      arrival_at=move.arrive_at)
        return observation

    def tick(self, now: float) -> Decision:
        """Poll completion/results, plan, and submit at most one pocket move.

        Exceptions/rejections latch FAULTED with the absolute target retained.
        Interrupted-motion resume needs remaining-release geometry and is not
        authorized here. A repeated tick cannot replay a relative motor command.
        """
        self._writer(now)
        if self.lifecycle in (Lifecycle.CLOSED, Lifecycle.DRAINED, Lifecycle.FAULTED):
            return Decision(AdvanceKind.HOLD, self.lifecycle.value)
        try:
            if self._active is not None:
                stopped = self.motor.stopped
                if stopped:
                    self.acknowledge_index(self._active, stopped=True,
                                           position=self.motor.position, now=now)
            deadlines = self.bridge.poll(now)
            if (self.lifecycle is Lifecycle.DRAINING and self._active is None
                    and all(p.state is PocketState.EMPTY for p in self.fifo.pockets)
                    and now >= self.planner.fall_clear_at):
                self.lifecycle = Lifecycle.DRAINED
                return Decision(AdvanceKind.HOLD, "drain complete")
            if self._active is None:
                if not self.motor.stopped:
                    raise RuntimeError("uncommanded C4 motion")
                if self.motor.position != self.fifo.target_for(self.fifo.boundary).absolute_microsteps:
                    raise RuntimeError("C4 position differs from confirmed boundary")
            decision = self.planner.plan(
                now, self._observe_chute(now),
                drain=self.lifecycle is Lifecycle.DRAINING, deadlines=deadlines,
            )
            if decision.chute_move is not None:
                self._chute_move = decision.chute_move
                self.chute.move(decision.chute_move)
            if decision.kind is not AdvanceKind.HOLD:
                assert decision.index is not None
                self._active = IndexCommand(self._epoch, decision.index)
                origin = self.fifo.target_for(self.fifo.boundary).absolute_microsteps
                if not self.motor.move_steps(decision.index.absolute_microsteps - origin):
                    raise RuntimeError("C4 index command rejected")
            return decision
        except Exception as exc:
            self.lifecycle = Lifecycle.FAULTED
            self.fault = str(exc)
            raise

    def close(self, now: float) -> None:
        self._writer(now)
        if self.lifecycle is Lifecycle.CLOSED:
            return
        if self.lifecycle is not Lifecycle.DRAINED:
            raise RuntimeError("drain before closing coordinator")
        self.bridge.close(now)
        self.lifecycle = Lifecycle.CLOSED
