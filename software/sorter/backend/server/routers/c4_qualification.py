"""Bounded, explicitly claimed physical qualification; never a production selector.

The normal controller must be absent. One dedicated thread owns the accepted
binding. An HTTP admission guard excludes other mutating controls while claimed;
emergency stop remains available. No providers or operational stores are called.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import time
from uuid import uuid4
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from server import shared_state
from subsystems.classification_channel.physical_binding import C4Calibration, PhysicalC4Binding
from subsystems.classification_channel.fail_forward_runtime import Lifecycle
from subsystems.classification_channel.physical_fifo import PocketState
from subsystems.classification_channel.demand_planner import ChuteMove
from subsystems.classification_channel.fall_clear import FallClearModel
from subsystems.distribution.chute import BinAddress

PREFIX = "/api/c4-qualification"
MODE = "fail-forward-physical-experimental"
router = APIRouter(prefix=PREFIX)


class Claim(BaseModel):
    mode: Literal["fail-forward-physical-experimental"]


class OpenBinding(BaseModel):
    revolution_numerator: int = Field(gt=0)
    revolution_denominator: int = Field(gt=0)
    clockwise_sign: Literal[-1, 1]
    release_numerator: int = Field(ge=0)
    release_denominator: int = Field(gt=0)
    release_after_start_s: float = Field(ge=0, allow_inf_nan=False)
    arrival_margin_s: float = Field(ge=0, allow_inf_nan=False)
    fall_clear_model: FallClearModel = Field(default_factory=FallClearModel)
    door_travel_s: float = Field(ge=0, le=10, allow_inf_nan=False)
    chute_eta_scale: float = Field(default=1, ge=1, le=10, allow_inf_nan=False)
    chute_eta_allowance_s: float = Field(default=0, ge=0, le=10, allow_inf_nan=False)
    destinations: dict[str, tuple[int, int, int]] = Field(default_factory=dict)


class Deposit(BaseModel):
    # Operator confirms an individual physical P6 deposit. A fixed destination
    # is test routing, explicitly recorded, not a claimed provider result.
    confirmed: Literal[True]
    destination: str | None = None


class Advance(BaseModel):
    binding_id: str
    expected_boundary: int = Field(ge=0)
    empty_intake: bool = False


class Destination(BaseModel):
    destination: str


class Session:
    def __init__(self):
        self.claimed = False
        self.busy = False
        self.aborted = False
        self.binding = None
        self.binding_id = None
        self.index_receipts = {}
        self.irl = None
        self.origin_verified = False
        self.origin_position = None
        self.records = []
        self.active_mutations = 0
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="c4-qualification")

    def _record(self, kind, **data):
        record = dict(kind=kind, monotonic=time.monotonic(), wall=time.time(), **data)
        self.records.append(record)
        return record

    def _devices(self):
        if not self.claimed or self.aborted:
            raise RuntimeError("qualification is not active or was stopped")
        if shared_state.controller_ref is not None:
            raise RuntimeError("normal controller must remain absent")
        if shared_state.getActiveIRL() is not self.irl:
            raise RuntimeError("hardware instance changed")
        if shared_state.hardware_state != "initialized":
            raise RuntimeError("qualification requires initialized hardware without normal controller")
        return self.irl.c_channel_4_rotor_stepper, self.irl.chute

    def claim(self):
        if self.claimed:
            raise RuntimeError("qualification already claimed")
        if shared_state.controller_ref is not None or shared_state.hardware_state != "initialized":
            raise RuntimeError("initialize hardware without creating a normal controller first")
        worker = shared_state.hardware_worker_thread
        if worker is not None and worker.is_alive():
            raise RuntimeError("initialization is still active")
        irl = shared_state.getActiveIRL()
        if irl is None:
            raise RuntimeError("hardware unavailable")
        for name in ("c_channel_1_rotor_stepper", "c_channel_2_rotor_stepper",
                     "c_channel_3_rotor_stepper", "c_channel_4_rotor_stepper", "chute_stepper"):
            motor = getattr(irl, name, None)
            if motor is not None and (not motor.stopped or motor.stalled):
                raise RuntimeError(f"{name} is moving or stalled")
        self.irl = irl
        self.claimed = True
        return self.inspect()

    def inspect(self):
        c4, chute = self._devices()
        return dict(c4_position=c4.position, c4_stopped=c4.stopped,
                    c4_disabled=c4.software_disabled, c4_stalled=c4.stalled,
                    microsteps=c4._microsteps, steps_per_revolution=c4._steps_per_revolution,
                    direction_inverted=c4._direction_inverted,
                    default_acceleration=c4._default_acceleration,
                    chute_position=chute.stepper.position, chute_stopped=chute.stepper.stopped,
                    chute_homed=chute.homed, layers=len(chute.layout.layers),
                    origin_verified=self.origin_verified)

    def verify_origin(self):
        c4, _ = self._devices()
        if self.binding is not None or not c4.stopped:
            raise RuntimeError("origin verification requires stationary unbound hardware")
        from dataclasses import replace
        from subsystems.classification_channel.simple_state_machine_rev01 import spoke_home as h
        from subsystems.classification_channel.simple_state_machine_rev01.rev01_config import configFromDict
        from toml_config import getClassificationChannelRev01Config
        capture = shared_state.vision_manager.getCaptureThreadForRole("classification_channel")
        frame = capture.latest_frame
        if frame is None or time.time() - frame.timestamp > 1:
            raise RuntimeError("fresh C4 image required")
        geometry = h.loadSpokeHomeGeometry(frame.raw.shape[:2])
        if geometry is None:
            raise RuntimeError("C4 optical home geometry unavailable")
        annulus, zero = geometry
        initial = h.detectSpokeAngle(frame.raw, annulus, h.DETECTOR_PARAMS, spoke_count=10)
        if not initial.success:
            raise RuntimeError("C4 spoke detection failed")
        result = h._waitForStableSpokeResult(capture, frame.timestamp, initial.annulus_used,
            replace(h.DETECTOR_PARAMS, center_refine_radius_frac=0), spoke_count=10,
            initial_result=initial)
        if result is None:
            raise RuntimeError("C4 optical phase is not stable")
        reference = h.angleForPoint(*zero, result.annulus_used.center_x, result.annulus_used.center_y)
        offset = configFromDict(getClassificationChannelRev01Config()).home_offset_output_deg
        residual = h.computeSignedAlignmentErrorDeg(result.angle_deg, reference + offset, spoke_count=10)
        record = self._record("optical_origin", residual_deg=residual, spoke_deg=result.angle_deg,
            reference_deg=reference, configured_offset_deg=offset, position=c4.position)
        if abs(residual) > h.SPOKE_HOME_MAX_RESIDUAL_DEG:
            raise RuntimeError(f"incorrect C4 origin geometry: residual {residual:.3f} degrees")
        self.origin_verified = True
        self.origin_position = c4.position
        return record

    def home_chute(self):
        c4, chute = self._devices()
        if self.binding is not None or not c4.stopped or not chute.stepper.stopped:
            raise RuntimeError("stationary unbound hardware required")
        if not chute.home():
            raise RuntimeError("chute home failed")
        return self._record("chute_home", position=chute.stepper.position)

    def open(self, params):
        c4, chute = self._devices()
        if self.binding is not None or not self.origin_verified or c4.position != self.origin_position:
            raise RuntimeError("binding needs a verified unchanged origin")
        if not c4.stopped or not chute.homed or not chute.stepper.stopped:
            raise RuntimeError("stationary C4 and homed chute required")
        calibration = C4Calibration(
            Fraction(params.revolution_numerator, params.revolution_denominator), c4.position,
            params.clockwise_sign, Fraction(params.release_numerator, params.release_denominator),
            params.release_after_start_s, params.arrival_margin_s, 10, 15, params.door_travel_s,
            params.chute_eta_scale, params.chute_eta_allowance_s,
            fall_clear_model=params.fall_clear_model)
        self.binding = PhysicalC4Binding(mode=MODE, c4=c4, chute=chute,
            doors=dict(enumerate(self.irl.servos)),
            destinations={k: BinAddress(*v) for k, v in params.destinations.items()},
            discard_destination="DISCARD", calibration=calibration,
            recognize=lambda crop: None, routing_timeout_s=1)
        self.binding_id = uuid4().hex
        self.index_receipts = {}
        return self._record("binding_open", binding_id=self.binding_id,
                            calibration=params.model_dump(), origin=c4.position,
                            fall_clear_by_destination=dict(self.binding.runtime.planner.timing.fall_clear_by_destination))

    def _runtime(self):
        self._devices()
        if self.binding is None:
            raise RuntimeError("open a binding first")
        if self.binding.runtime.lifecycle is Lifecycle.FAULTED:
            raise RuntimeError(self.binding.runtime.fault)
        return self.binding.runtime

    def _motion_fault(self, exc):
        runtime = self.binding.runtime
        runtime.lifecycle = Lifecycle.FAULTED
        runtime.fault = str(exc)
        from server.routers.steppers import _halt_stepper
        results = {}
        for name, motor in (("C4", self.binding.motor.stepper),
                            ("chute", self.binding.chute.chute.stepper)):
            try:
                _halt_stepper(motor, force=True)
                results[name] = "stop commanded"
            except Exception as stop_exc:
                results[name] = str(stop_exc)
        self._record("motion_fault_stop", error=str(exc), stop_results=results)

    def deposit(self, params):
        runtime = self._runtime()
        if not self.binding.motor.stopped or runtime.active_index is not None:
            raise RuntimeError("deposit requires stationary confirmed boundary")
        if params.destination is not None and not self.binding.chute.reachable(params.destination):
            raise ValueError("test destination is unreachable")
        if runtime.lifecycle is not Lifecycle.RUNNING:
            raise RuntimeError("intake is closed")
        # Direct pure-FIFO route is an explicit controlled routing fixture. The
        # recognition bridge is exercised separately by missing-image DISCARD.
        if params.destination is None:
            pocket = runtime.confirmed_deposit(boundary=runtime.fifo.boundary,
                now=time.monotonic(), sample=None)
        else:
            pocket = runtime.fifo.deposit(metadata={"source": "operator-test-route"})
            runtime.fifo.resolve(pocket.pocket_id, pocket.generation, PocketState.ROUTED,
                                 destination=params.destination)
        return self._record("deposit", pocket_id=pocket.pocket_id,
            generation=pocket.generation, boundary=runtime.fifo.boundary,
            destination=params.destination or "DISCARD")

    def advance(self, params):
        if params.binding_id != self.binding_id:
            raise RuntimeError("binding identity does not match current qualification")
        if params.expected_boundary in self.index_receipts:
            return self.index_receipts[params.expected_boundary]
        runtime = self._runtime()
        if params.expected_boundary != runtime.fifo.boundary:
            raise RuntimeError("expected boundary does not match confirmed boundary")
        if runtime.active_index is not None:
            raise RuntimeError("index already outstanding")
        intake_empty = runtime.fifo.pockets[runtime.fifo.intake_pocket_id].state is PocketState.EMPTY
        if params.empty_intake != intake_empty:
            raise RuntimeError("declared intake occupancy differs from confirmed deposit")
        if params.empty_intake:
            runtime.start_drain(time.monotonic())
        started = time.monotonic()
        samples = []
        command = None
        try:
            while time.monotonic() - started < 20:
                self._devices()
                now = time.monotonic()
                self.binding.motor.now = now
                if command is None:
                    decision = self.binding.tick(now)
                    command = runtime.active_index
                    if command is not None:
                        self._record("index_submitted", boundary=command.target.boundary,
                                     target=command.target.absolute_microsteps, tick_started_at=now)
                    elif runtime.lifecycle is Lifecycle.DRAINED:
                        return self._record("drained", boundary=runtime.fifo.boundary)
                    elif not params.empty_intake and decision.reason == "no demand":
                        raise RuntimeError("no confirmed intake demand")
                else:
                    self.binding.chute.observe(now)
                    feedback_started_at = time.monotonic()
                    stopped = self.binding.motor.stopped
                    position = self.binding.motor.position
                    feedback_completed_at = time.monotonic()
                    samples.append(dict(monotonic=feedback_completed_at, wall=time.time(),
                        feedback_started_at=feedback_started_at, position=position, stopped=stopped))
                    # Do not tick again here: polling completion must not submit
                    # a second index. Explicit matching feedback advances once.
                    if stopped and position == command.target.absolute_microsteps:
                        runtime.acknowledge_index(command, stopped=True, position=position,
                                                  now=feedback_completed_at)
                        events = [dict(pocket_id=e.pocket_id, generation=e.generation,
                            boundary=e.boundary, destination=e.destination, state=e.state.value)
                            for e in runtime.take_discharge_events()]
                        receipt = self._record("index_complete", binding_id=self.binding_id,
                            boundary=runtime.fifo.boundary,
                            samples=samples, discharges=events, fall_clear_at=runtime.planner.fall_clear_at)
                        self.index_receipts[params.expected_boundary] = receipt
                        return receipt
                time.sleep(.02)
            raise RuntimeError("bounded index operation timed out")
        except Exception as exc:
            self._motion_fault(exc)
            self._record("fault", error=str(exc), samples=samples)
            raise

    def chute_move(self, params):
        runtime = self._runtime()
        if runtime.active_index is not None or any(p.state is not PocketState.EMPTY for p in runtime.fifo.pockets):
            raise RuntimeError("chute timing calibration requires empty C4")
        now = time.monotonic()
        if now < runtime.planner.fall_clear_at:
            raise RuntimeError("fall-clear is still active")
        chute = self.binding.chute
        observation = chute.observe(now)
        eta = observation.travel_seconds.get(params.destination)
        if eta is None:
            raise ValueError("destination is unreachable")
        try:
            return self._measure_chute(params, runtime, chute, now, eta)
        except Exception as exc:
            self._motion_fault(exc)
            self._record("fault", error=str(exc), operation="chute_move")
            raise

    def _measure_chute(self, params, runtime, chute, now, eta):
        chute.move(ChuteMove(params.destination, now, now + eta))
        samples = []
        while time.monotonic() - now < self.binding.calibration.chute_timeout_s:
            self._devices()
            at = time.monotonic()
            observation = chute.observe(at)
            position = chute.chute.stepper.position
            feedback_completed_at = time.monotonic()
            samples.append(dict(monotonic=feedback_completed_at, feedback_started_at=at, position=position))
            if observation.aligned_destination == params.destination:
                return self._record("chute_arrived", destination=params.destination,
                    estimated_s=eta, measured_s=feedback_completed_at-now, samples=samples)
            time.sleep(.02)
        raise RuntimeError("chute did not reach its commanded destination")

    def close(self):
        if self.binding is None:
            c4, chute = self._devices()
            if any(not motor.stopped or motor.stalled for motor in (c4, chute.stepper)):
                raise RuntimeError("unbound qualification requires stopped unstalled motors before closing")
            self.origin_verified = False
            self.claimed = False
            return self._record("closed", unbound=True)
        runtime = self._runtime()
        if runtime.active_index is not None or any(p.state is not PocketState.EMPTY for p in runtime.fifo.pockets):
            raise RuntimeError("drain physical loads before closing")
        if time.monotonic() < runtime.planner.fall_clear_at:
            raise RuntimeError("wait for final fall-clear")
        runtime.start_drain(time.monotonic())
        self.binding.tick(time.monotonic())
        runtime.close(time.monotonic())
        self.binding = None
        self.origin_verified = False
        self.claimed = False
        return self._record("closed")


session = Session()


async def _run(operation, *args):
    # Admission is on the API event loop; never queue multiple motion requests.
    if session.busy:
        raise HTTPException(409, "qualification operation already in progress; inspect its receipt")
    session.busy = True
    future = asyncio.get_running_loop().run_in_executor(session.executor, operation, *args)
    # A disconnected client must not release admission while its command runs.
    future.add_done_callback(lambda _: setattr(session, "busy", False))
    try:
        return await asyncio.shield(future)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/status")
async def status():
    return dict(claimed=session.claimed, busy=session.busy, aborted=session.aborted,
                records=list(session.records))


@router.post("/claim")
async def claim(params: Claim):
    return await _run(session.claim)


@router.post("/inspect")
async def inspect():
    return await _run(session.inspect)


@router.post("/verify-origin")
async def verify_origin():
    return await _run(session.verify_origin)


@router.post("/home-chute")
async def home_chute():
    return await _run(session.home_chute)


@router.post("/open")
async def open_binding(params: OpenBinding):
    return await _run(session.open, params)


@router.post("/deposit")
async def deposit(params: Deposit):
    return await _run(session.deposit, params)


@router.post("/advance")
async def advance(params: Advance):
    return await _run(session.advance, params)


@router.post("/chute-move")
async def chute_move(params: Destination):
    return await _run(session.chute_move, params)


@router.post("/close")
async def close():
    return await _run(session.close)


def install(app):
    app.include_router(router)

    @app.middleware("http")
    async def exclusive_qualification(request, call_next):
        mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
        if mutation and request.url.path == PREFIX + "/claim" and session.active_mutations:
            return JSONResponse({"detail": "another control request is in progress"}, status_code=409)
        if (session.claimed or session.busy) and mutation:
            path = request.url.path
            if path in {"/stepper/stop-all", "/stepper/stop", "/pause"}:
                session.aborted = True
            elif not path.startswith(PREFIX + "/"):
                return JSONResponse({"detail": "experimental C4 qualification owns hardware"}, status_code=409)
        if mutation:
            session.active_mutations += 1
        try:
            return await call_next(request)
        finally:
            if mutation:
                session.active_mutations -= 1
