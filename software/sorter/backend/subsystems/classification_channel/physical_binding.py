"""Explicit opt-in binding to already initialized sorterOS devices.

No construction/homing/enabling of C4, production selection, or automatic retry.
One owner supplies monotonic time. Device feedback and submission exceptions are
mechanical faults; routing answers are sanitized before entering Slice 3.
"""
from dataclasses import dataclass
from fractions import Fraction
from math import isfinite
from typing import Callable, Mapping, Protocol, TYPE_CHECKING

import numpy as np

from subsystems.distribution.chute import BinAddress, Chute, GEAR_RATIO
from .demand_planner import C4DemandPlanner, ChuteMove, ChuteObservation, Timing
from .fail_forward_runtime import C4RuntimeCoordinator
from .intake_routing import C4IntakeRoutingBridge
from .physical_fifo import PhysicalC4FIFO
from .fall_clear import FallClearModel

if TYPE_CHECKING:
    from hardware.sorter_interface import StepperMotor


@dataclass(frozen=True)
class C4Calibration:
    # Exact output gearing, mapped into the existing logical motor coordinate.
    microsteps_per_revolution: int | Fraction
    origin_microsteps: int
    clockwise_sign: int
    # P0 release within the seventh one-pocket sweep; NOT another station.
    release_fraction: Fraction
    release_after_start_s: float
    arrival_margin_s: float
    index_timeout_s: float
    chute_timeout_s: float
    door_travel_s: float
    chute_eta_scale: float = 1.0
    chute_eta_allowance_s: float = 0.0
    fall_clear_s: float = 1.5  # Compatibility only, not physically measured.
    fall_clear_model: FallClearModel | None = None

    def __post_init__(self) -> None:
        self.make_fifo()  # Reuse Slice 1's exact geometry validation.
        if not isinstance(self.release_fraction, Fraction) or not 0 <= self.release_fraction <= 1:
            raise ValueError("release_fraction must be an exact fraction within one index")
        self.timing()
        for name in ("index_timeout_s", "chute_timeout_s", "door_travel_s",
                     "chute_eta_scale", "chute_eta_allowance_s"):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"invalid {name}")
        if self.index_timeout_s <= 0 or self.chute_timeout_s <= 0 or self.chute_eta_scale < 1:
            raise ValueError("positive timeouts and ETA scale >= 1 required")
        if self.release_after_start_s > self.index_timeout_s:
            raise ValueError("release prediction exceeds index timeout")

    def make_fifo(self) -> PhysicalC4FIFO:
        return PhysicalC4FIFO(microsteps_per_revolution=self.microsteps_per_revolution,
                              origin_microsteps=self.origin_microsteps,
                              clockwise_sign=self.clockwise_sign)

    def timing(self) -> Timing:
        return Timing(self.release_after_start_s, self.arrival_margin_s, self.fall_clear_s)

    def release_microsteps(self, fifo: PhysicalC4FIFO) -> Fraction:
        """Physical release coordinate for the next sweep (no completion inference)."""
        start = fifo.target_for(fifo.boundary).absolute_microsteps
        end = fifo.target_for(fifo.boundary + 1).absolute_microsteps
        return start + self.release_fraction * (end - start)


class Door(Protocol):
    # Implemented by existing MCU and Waveshare servo abstractions.
    @property
    def available(self) -> bool: ...
    def command_door(self, opened: bool) -> None: ...
    def door_at_target(self, opened: bool) -> bool: ...


class PhysicalIndexMotor:
    """Checked StepperMotor subset; retains one submission, never retries it."""
    def __init__(self, stepper: "StepperMotor", timeout_s: float):
        self.stepper = stepper
        self.timeout_s = timeout_s
        self.now = 0.0
        self.deadline: float | None = None
        self.target: int | None = None

    def _enabled(self) -> None:
        if self.stepper.software_disabled or self.stepper.stalled:
            raise RuntimeError("C4 disabled or stalled")

    @property
    def position(self) -> int:
        self._enabled()
        return self.stepper.position

    @property
    def stopped(self) -> bool:
        self._enabled()
        stopped = self.stepper.stopped
        if self.target is not None:
            if stopped and self.stepper.position == self.target:
                self.target = None
                self.deadline = None
            elif self.deadline is not None and self.now >= self.deadline:
                raise RuntimeError("C4 completion acknowledgment timed out")
        return stopped

    def move_steps(self, steps: int) -> bool:
        self._enabled()
        if self.target is not None:
            raise RuntimeError("C4 command already outstanding")
        self.target = self.position + steps
        self.deadline = self.now + self.timeout_s
        return self.stepper.move_steps(steps)


class CalibratedChute:
    def __init__(self, chute: Chute, doors: Mapping[int, Door],
                 destinations: Mapping[str, BinAddress], discard_destination: str,
                 calibration: C4Calibration):
        self.chute = chute
        self.doors = dict(doors)
        # Immutable snapshot of addresses, retaining the existing live aiming model.
        self.destinations = {key: BinAddress(v.layer_index, v.section_index, v.bin_index)
                             for key, v in destinations.items()}
        self.discard_destination = discard_destination
        self.calibration = calibration
        if not discard_destination.strip() or discard_destination in destinations:
            raise ValueError("discard must be a separate passthrough destination")
        if not self.doors or set(self.doors) != set(range(len(chute.layout.layers))):
            raise ValueError("bind every physical layer door")
        for key, address in self.destinations.items():
            if not key.strip() or min(address.layer_index, address.section_index, address.bin_index) < 0:
                raise ValueError("invalid destination address")
            try:
                chute.layout.layers[address.layer_index].sections[address.section_index].bins[address.bin_index]
            except IndexError as exc:
                raise ValueError("destination absent from existing layout") from exc
        self._active: ChuteMove | None = None
        self._selected: str | None = None
        self._target_steps: int | None = None

    def reachable(self, destination: str) -> bool:
        if destination == self.discard_destination:
            return True
        address = self.destinations.get(destination)
        return address is not None and self.chute.getAngleForBin(address) is not None

    def _opened(self, destination: str, layer: int) -> bool:
        address = self.destinations.get(destination)
        return address is None or layer != address.layer_index

    def _ready(self, destination: str) -> bool:
        stopped = self.chute.stepper.stopped
        position = self.chute.stepper.position
        doors = all(door.door_at_target(self._opened(destination, layer))
                    for layer, door in self.doors.items())
        return stopped and position == self._target_steps and doors

    def _check(self) -> None:
        if (not self.chute.homed or self.chute.gc.disable_chute
                or self.chute.gc.disable_servos or self.chute.stepper.software_disabled
                or self.chute.stepper.stalled):
            raise RuntimeError("chute must be homed, enabled and unstalled")
        if any(not door.available for door in self.doors.values()):
            raise RuntimeError("layer door unavailable")

    def observe(self, now: float) -> ChuteObservation:
        self._check()
        aligned = None
        if self._selected is not None and self._ready(self._selected):
            aligned = self._selected
            self._active = None
        if self._active is not None:
            if now >= self._active.depart_at + self.calibration.chute_timeout_s:
                raise RuntimeError("chute/door completion acknowledgment timed out")
            return ChuteObservation(None, {}, self._active.destination, self._active.arrive_at)
        if self._selected is not None and aligned is None:
            raise RuntimeError("chute/door target lost after verified arrival")
        # No prediction from an unowned moving configuration.
        if not self.chute.stepper.stopped:
            raise RuntimeError("uncommanded chute motion")
        current = self.chute.current_angle
        travel = {}
        for key in (*self.destinations, self.discard_destination):
            if not self.reachable(key):
                continue
            address = self.destinations.get(key)
            angle = self.chute.getAngleForBin(address) if address is not None else current
            assert angle is not None
            motor_s = self.chute.stepper.estimateMoveDegreesMs(
                (angle - current) * GEAR_RATIO,
                max_speed=self.chute.operating_speed_microsteps_per_second) / 1000
            travel[key] = (max(motor_s, self.calibration.door_travel_s)
                           * self.calibration.chute_eta_scale
                           + self.calibration.chute_eta_allowance_s)
        return ChuteObservation(aligned, travel)

    def move(self, move: ChuteMove) -> None:
        self._check()
        if self._active is not None:
            if self._active == move:
                return
            raise RuntimeError("chute command already outstanding")
        address = self.destinations.get(move.destination)
        if not self.reachable(move.destination):
            raise ValueError("unreachable route must be normalized before planning")
        self._active = move
        self._selected = move.destination
        # Close target first, then open all other calibrated layer doors.
        layers = sorted(self.doors, key=lambda layer: self._opened(move.destination, layer))
        for layer in layers:
            self.doors[layer].command_door(self._opened(move.destination, layer))
        origin = self.chute.stepper.position
        if address is None:
            self._target_steps = origin  # discard: open all, retain chute azimuth
        else:
            angle = self.chute.getAngleForBin(address)
            assert angle is not None
            delta = (angle - self.chute.current_angle) * GEAR_RATIO
            self._target_steps = origin + self.chute.stepper.microsteps_for_degrees(delta)
            self.chute.moveToBin(address, require_ack=True)


class PhysicalC4Binding:
    """Non-default adapter seam. Construction sends no commands.

    Caller has exclusive device ownership and an established empty P6 origin.
    No resume/reset API: faults require separate physical reconciliation.
    """
    def __init__(self, *, mode: str, c4: "StepperMotor", chute: Chute,
                 doors: Mapping[int, Door], destinations: Mapping[str, BinAddress],
                 discard_destination: str, calibration: C4Calibration,
                 recognize: Callable[[np.ndarray], str | None], routing_timeout_s: float):
        if mode != "fail-forward-physical-experimental":
            raise ValueError("explicit experimental physical mode required")
        self.calibration = calibration
        self.motor = PhysicalIndexMotor(c4, calibration.index_timeout_s)
        self.chute = CalibratedChute(chute, doors, destinations, discard_destination, calibration)
        # Geometry-only route snapshot: worker never polls hardware or calls Harvest.
        reachable = frozenset(key for key in destinations if self.chute.reachable(key))

        def safe_recognize(crop: np.ndarray) -> str | None:
            try:
                result = recognize(crop)
                return result if isinstance(result, str) and result in reachable else None
            except Exception:
                return None

        fifo = calibration.make_fifo()
        bridge = C4IntakeRoutingBridge(fifo, recognize=safe_recognize,
                                      routing_timeout_s=routing_timeout_s)
        timing = calibration.timing()
        if calibration.fall_clear_model is not None:
            model = calibration.fall_clear_model
            durations = {key: model.seconds(address.layer_index + 1)
                         for key, address in self.chute.destinations.items()}
            # DISCARD opens every flap. Conservatively bound passthrough by the
            # deepest installed layer including its full contact-path allowance.
            durations[discard_destination] = model.seconds(len(chute.layout.layers))
            timing = Timing(timing.release_after_start_s, timing.arrival_margin_s,
                            durations[discard_destination], durations)
        self.runtime = C4RuntimeCoordinator(bridge, C4DemandPlanner(
            fifo, timing, discard_destination=discard_destination),
            self.motor, self.chute)

    def tick(self, now: float):
        # Runtime validates owner/time before observing devices or issuing motion.
        self.motor.now = now
        return self.runtime.tick(now)
