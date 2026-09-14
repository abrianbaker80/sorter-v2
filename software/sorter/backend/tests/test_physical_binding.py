from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace
import ast
import struct
from pathlib import Path

import numpy as np
import pytest

from hardware.sorter_interface import StepperMotor, ServoMotor
from hardware.waveshare_servo import WaveshareServoMotor
from subsystems.distribution.chute import Chute, BinAddress, GEAR_RATIO
from subsystems.classification_channel import intake_routing
from subsystems.classification_channel.physical_binding import C4Calibration, PhysicalC4Binding
from subsystems.classification_channel.fail_forward_runtime import Lifecycle
from subsystems.classification_channel.physical_fifo import PocketState
from subsystems.classification_channel.demand_planner import AdvanceKind, ChuteMove
from perception.capture import PerceptionFrame
from perception.state import PieceObservation


class Motor:
    microsteps_for_degrees = StepperMotor.microsteps_for_degrees
    degrees_for_microsteps = StepperMotor.degrees_for_microsteps
    estimateMoveDegreesMs = StepperMotor.estimateMoveDegreesMs
    estimateMoveStepsMs = StepperMotor.estimateMoveStepsMs
    move_degrees = StepperMotor.move_degrees
    position_degrees = property(lambda self: self.degrees_for_microsteps(self.position))

    def __init__(self, position=0):
        self.position = position
        self.stopped = True
        self.software_disabled = self.stalled = False
        self._steps_per_revolution = 200
        self._microsteps = 16
        self._applied_acceleration = self._default_acceleration = 10000
        self.commands = []
        self.accept = True
        self.target = position

    def move_steps(self, steps, **kwargs):
        self.commands.append(steps)
        if isinstance(self.accept, Exception):
            raise self.accept
        if not self.accept:
            return False
        self.target = self.position + steps
        self.stopped = False
        return True

    def finish(self):
        self.position = self.target
        self.stopped = True


class Door:
    available = True
    def __init__(self):
        self.commands = []
        self.opened = None
        self.ready = True
        self.accept = True
    def command_door(self, opened):
        self.commands.append(opened)
        if not self.accept:
            raise RuntimeError('door command not acknowledged')
        self.opened = opened
    def door_at_target(self, opened):
        return self.ready and self.opened == opened


def calibration(**kwargs):
    return replace(C4Calibration(Fraction(32000, 3), 37, -1, Fraction(2, 3),
                                 1.0, .1, 5, 8, .3), **kwargs)


@pytest.fixture
def rig(monkeypatch):
    workers = []
    class Worker:
        def __init__(self, *, target, **kwargs):
            self.run = target
        def start(self):
            workers.append(self)
    monkeypatch.setattr(intake_routing, 'Thread', Worker)
    logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None)
    gc = SimpleNamespace(logger=logger, disable_chute=False, disable_servos=False)
    layout = SimpleNamespace(layers=[SimpleNamespace(sections=[SimpleNamespace(bins=[0, 1])])])
    chute = Chute(gc, Motor(), SimpleNamespace(), layout,
                  section_width_deg=40, first_section_offset_deg=7)
    chute._homed = True
    c4, doors = Motor(37), {0: Door()}
    def build(recognize=lambda crop: 'A', config=None, **kwargs):
        return PhysicalC4Binding(mode='fail-forward-physical-experimental', c4=c4,
            chute=chute, doors=doors, destinations={'A': BinAddress(0, 0, 1)},
            discard_destination='discard', calibration=config or calibration(),
            recognize=recognize, routing_timeout_s=.5, **kwargs)
    return build, c4, chute, doors, workers


def deposit(binding, now, sample=None):
    return binding.runtime.confirmed_deposit(boundary=binding.runtime.fifo.boundary,
                                              now=now, sample=sample)


def sample(count=1):
    return ([PieceObservation(100, 30, 1, (2, 2, 8, 8)) for _ in range(count)],
            PerceptionFrame('carousel', 100, np.zeros((16, 16, 3), dtype=np.uint8)))


def test_all_ten_targets_wrap_and_duplicate_ack(rig):
    build, motor, chute, _, _ = rig
    b = build(); r = b.runtime
    targets = []
    old = None
    for index in range(21):
        now = index * 10.
        deposit(b, now)
        b.tick(now)
        command = r.active_index
        assert command is not None
        assert r.fifo.boundary == index
        assert not r.acknowledge_index(command, stopped=False, position=motor.target, now=now)
        if old:
            assert not r.acknowledge_index(old, stopped=True, position=old.target.absolute_microsteps, now=now)
        motor.finish(); chute.stepper.finish()
        b.tick(now + .5)
        assert r.fifo.boundary == index + 1
        assert not r.acknowledge_index(command, stopped=True, position=motor.position, now=now+.5)
        targets.append(motor.position)
        old = command
    expected = [37 + (-(i * 3200) * 2 + 3) // 6 for i in range(1, 22)]
    assert targets == expected
    assert len(motor.commands) == 21
    assert r.fifo.intake_pocket_id == 1
    assert len(r.take_discharge_events()) == 15


@pytest.mark.parametrize('accept', [False, TimeoutError('missing ACK')])
def test_command_rejection_never_advances_or_retries(rig, accept):
    build, motor, _, _, _ = rig
    b = build(); deposit(b, 0); motor.accept = accept
    with pytest.raises((RuntimeError, TimeoutError)):
        b.tick(0)
    for now in (1, 10, 30):
        assert b.tick(now).kind is AdvanceKind.HOLD
    assert b.runtime.lifecycle is Lifecycle.FAULTED
    assert b.runtime.fifo.boundary == 0
    assert len(motor.commands) == 1


def test_stopped_short_times_out_without_advancing(rig):
    build, motor, _, _, _ = rig
    b = build(); deposit(b, 0); b.tick(0)
    motor.stopped = True
    b.tick(1)
    assert b.runtime.fifo.boundary == 0
    with pytest.raises(RuntimeError, match='timed out'):
        b.tick(5)
    assert b.tick(10).kind is AdvanceKind.HOLD
    assert len(motor.commands) == 1


def test_existing_bin_calibration_estimator_and_target_feedback(rig):
    build, _, chute, doors, _ = rig
    b = build()
    observation = b.chute.observe(0)
    angle = chute.getAngleForBin(BinAddress(0, 0, 1))
    assert angle == 37
    expected = max(chute.stepper.estimateMoveDegreesMs(37 * GEAR_RATIO,
                        max_speed=chute.operating_speed_microsteps_per_second)/1000, .3)
    assert observation.travel_seconds['A'] == expected
    move = ChuteMove('A', 0, expected)
    b.chute.move(move); b.chute.move(move)
    assert len(chute.stepper.commands) == 1
    assert doors[0].commands == [False]
    assert b.chute.observe(.01).aligned_destination is None
    chute.stepper.finish()
    doors[0].ready = False
    assert b.chute.observe(.1).aligned_destination is None
    doors[0].ready = True
    assert b.chute.observe(.2).aligned_destination == 'A'


def test_eta_controls_slice2_release_readiness(rig):
    build, _, _, _, _ = rig
    b = build(); fifo = b.runtime.fifo
    p = fifo.deposit(); fifo.resolve(p.pocket_id, p.generation,
                                    PocketState.ROUTED,
                                    destination='A')
    # Set up P0 using the accepted FIFO completion contract.
    for _ in range(6):
        target = fifo.prepare_index(); fifo.complete_index(target, confirmed_microsteps=target.absolute_microsteps)
    obs = b.chute.observe(0)
    eta = obs.travel_seconds['A']
    b.runtime.planner.timing = replace(b.runtime.planner.timing, release_after_start_s=eta+.1)
    decision = b.runtime.planner.plan(0, obs, drain=True)
    assert decision.kind is AdvanceKind.DRAIN
    assert decision.chute_move.arrive_at == eta


def test_release_and_fall_clear_configuration(rig):
    build, motor, chute, _, _ = rig
    config = calibration(release_fraction=Fraction(1, 4), release_after_start_s=.4, fall_clear_s=.7)
    b = build(config=config); r = b.runtime
    start = r.fifo.target_for(0).absolute_microsteps
    end = r.fifo.target_for(1).absolute_microsteps
    assert config.release_microsteps(r.fifo) == start + Fraction(end-start, 4)
    assert calibration().fall_clear_s == 1.5
    deposit(b, 0); r.start_drain(0)
    for i in range(7):
        b.tick(i*2)
        command = r.active_index
        motor.finish(); chute.stepper.finish()
        assert command
        assert r.acknowledge_index(command, stopped=True, position=motor.position,
                 now=i*2+1, physical_release_at=i*2+.6)
        # Consume wrapper's completion state before next direct callback-driven index.
        assert b.motor.stopped
        if i < 6:
            assert not r.take_discharge_events()
    assert len(r.take_discharge_events()) == 1
    assert r.planner.fall_clear_at == pytest.approx(13.3)


@pytest.mark.parametrize('problem', ['timeout', 'provider', 'ambiguous', 'harvest', 'unknown', 'multidrop', 'stale'])
def test_nonmechanical_failures_drain_to_discard(rig, problem):
    build, motor, chute, _, workers = rig
    def recognize(crop):
        if problem in ('provider', 'harvest'):
            raise RuntimeError(problem)
        return 'unknown' if problem == 'unknown' else None
    b = build(recognize); r = b.runtime
    deposit(b, 0, sample(2 if problem == 'multidrop' else 1))
    if problem != 'timeout':
        for worker in workers:
            worker.run()
    if problem == 'stale':
        p = r.fifo.pockets[0]
        key = replace(r.bridge.key_for(p), generation=999)
        assert not r.bridge.post_result(key, 'A')
    r.start_drain(0)
    for i in range(7):
        b.tick(i*2)
        motor.finish(); chute.stepper.finish()
        # tick may submit next index immediately while draining; use callback instead.
        command = r.active_index
        assert command
        assert r.acknowledge_index(command, stopped=True, position=motor.position, now=i*2+1)
        assert b.motor.stopped
    events = r.take_discharge_events()
    assert len(events) == 1 and events[0].destination == 'discard'
    assert r.fault is None


@pytest.mark.parametrize('failure', ['chute', 'door', 'door_timeout', 'disabled', 'stall'])
def test_hardware_failure_latches_no_more_c4_commands(rig, failure):
    build, motor, chute, doors, workers = rig
    b = build(); deposit(b, 0, sample()); workers[0].run()
    if failure == 'chute': chute.stepper.accept = False
    if failure == 'door': doors[0].accept = False
    if failure == 'disabled': motor.software_disabled = True
    if failure == 'stall': motor.stalled = True
    if failure == 'door_timeout':
        doors[0].ready = False
        b.tick(0); motor.finish(); chute.stepper.finish()
    with pytest.raises(RuntimeError):
        b.tick(8 if failure == 'door_timeout' else 0)
    count = len(motor.commands)
    b.tick(20)
    assert len(motor.commands) == count
    assert b.runtime.lifecycle is Lifecycle.FAULTED


def test_no_legacy_dependencies_and_no_default_selector():
    source = Path('subsystems/classification_channel/physical_binding.py').read_text()
    imports = [node for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.Import, ast.ImportFrom))]
    text = '\n'.join(ast.unparse(node) for node in imports).lower()
    for forbidden in ('piece_transport', 'reservation', 'ownership', 'harvest', 'known_object'):
        assert forbidden not in text
    assert 'physical_binding' not in Path('coordinator.py').read_text()


def test_servo_checked_command_and_fresh_feedback():
    servo = ServoMotor.__new__(ServoMotor)
    servo._open_angle, servo._closed_angle = 20, 90
    servo.apply_open_speed = servo.apply_close_speed = lambda: None
    servo.move_to_and_release = lambda target: False
    with pytest.raises(RuntimeError, match='not acknowledged'):
        servo.command_door(True)
    servo._channel = 0
    responses = iter([b'\x01', struct.pack('<H', 200)])
    servo._dev = SimpleNamespace(send_command=lambda *a: SimpleNamespace(payload=next(responses)))
    assert servo.door_at_target(True)


def test_waveshare_missing_feedback_never_uses_shadow():
    servo = WaveshareServoMotor.__new__(WaveshareServoMotor)
    servo._open_position, servo._closed_position = 10, 90
    servo._servo_id = 1
    servo._current_position = 10
    servo._bus = SimpleNamespace(read_position=lambda *a: None)
    with pytest.raises(RuntimeError, match='missing door'):
        servo.door_at_target(True)

@pytest.mark.parametrize('phase', ['enable', 'release'])
def test_waveshare_torque_rejection_is_command_failure(phase):
    servo = WaveshareServoMotor.__new__(WaveshareServoMotor)
    servo._enabled = False
    servo._servo_id = 1
    servo._open_position, servo._closed_position = 10, 90
    servo._move_started_at, servo._move_duration = -1, .3
    servo._bus = SimpleNamespace(set_torque=lambda *a: False, read_position=lambda *a: 10)
    with pytest.raises(RuntimeError, match='torque '+phase+' not acknowledged'):
        if phase == 'enable':
            servo.command_door(True)
        else:
            servo.door_at_target(True)


@pytest.mark.parametrize('kwargs', [dict(release_fraction=Fraction(11, 10)),
    dict(fall_clear_s=-1), dict(chute_eta_scale=.5), dict(index_timeout_s=0),
    dict(door_travel_s=float('nan')), dict(release_after_start_s=6)])
def test_invalid_calibration_rejected(kwargs):
    with pytest.raises(ValueError):
        calibration(**kwargs)


def test_late_eta_holds_release_and_adjustments_apply(rig):
    build, _, _, _, _ = rig
    b = build(config=calibration(chute_eta_scale=2, chute_eta_allowance_s=.4))
    base = build().chute.observe(0).travel_seconds['A']
    obs = b.chute.observe(0)
    assert obs.travel_seconds['A'] == base*2+.4
    fifo = b.runtime.fifo
    p = fifo.deposit(); fifo.resolve(p.pocket_id, p.generation, PocketState.ROUTED, destination='A')
    for _ in range(6):
        target = fifo.prepare_index(); fifo.complete_index(target, confirmed_microsteps=target.absolute_microsteps)
    b.runtime.planner.timing = replace(b.runtime.planner.timing,
                                      release_after_start_s=obs.travel_seconds['A']+.099)
    assert b.runtime.planner.plan(0, obs, drain=True).kind is AdvanceKind.HOLD


def test_real_stepper_ack_and_position_protocol(rig):
    from hardware.sorter_interface import InterfaceCommandCode
    build, motor, _, _, _ = rig
    b = build()
    real = StepperMotor.__new__(StepperMotor)
    real.software_disabled = real.stalled = False
    real._direction_inverted = True
    real._default_acceleration = real._applied_acceleration = None
    real._name = real._hardware_name = 'c4'
    real._channel = 4
    real._current_position_steps = 37
    real._steps_per_revolution = 200
    real._microsteps = 16
    real._gc = SimpleNamespace(logger=SimpleNamespace(info=lambda *a: None, error=lambda *a: None))
    position, target, stopped = -37, -37, True
    commands = []
    def send(code, channel, payload):
        nonlocal target, stopped
        if code == InterfaceCommandCode.STEPPER_MOVE_STEPS:
            commands.append(struct.unpack('<i', payload)[0])
            target = position + commands[-1]
            stopped = False
            return SimpleNamespace(payload=b'\x01')
        if code == InterfaceCommandCode.STEPPER_IS_STOPPED:
            return SimpleNamespace(payload=bytes([stopped]))
        if code == InterfaceCommandCode.STEPPER_GET_POSITION:
            return SimpleNamespace(payload=struct.pack('<i', position))
        raise AssertionError(code)
    real._dev = SimpleNamespace(send_command=send)
    b.motor.stepper = real
    deposit(b, 0); b.tick(0)
    assert b.runtime.fifo.boundary == 0
    b.tick(.1)
    assert b.runtime.fifo.boundary == 0 and len(commands) == 1
    position, stopped = target, True
    b.tick(.2)
    assert b.runtime.fifo.boundary == 1
    assert commands == [1067]  # logical clockwise negative -> physical positive


def test_construction_is_inert_and_mode_is_required(rig):
    build, motor, chute, doors, _ = rig
    b = build()
    assert motor.commands == chute.stepper.commands == doors[0].commands == []
    with pytest.raises(ValueError, match='explicit experimental'):
        PhysicalC4Binding(mode='production', c4=motor, chute=chute, doors=doors,
            destinations={}, discard_destination='discard', calibration=calibration(),
            recognize=lambda crop: None, routing_timeout_s=1)


def test_missing_feedback_fault_prevents_later_commands(rig):
    build, motor, _, _, _ = rig
    b = build(); deposit(b, 0); b.tick(0)
    class MissingPosition(Motor):
        @property
        def position(self):
            raise TimeoutError('missing position feedback')
    motor.stopped = True
    motor.__class__ = MissingPosition
    with pytest.raises(TimeoutError):
        b.tick(1)
    assert b.runtime.fifo.boundary == 0
    b.tick(2)
    assert len(motor.commands) == 1
