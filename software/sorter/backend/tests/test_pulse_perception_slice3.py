"""PulsePerception C3 motor owner against the physical Slice 3 handoff contract."""

import time
from types import SimpleNamespace as NS

import pytest

from perception.cascade import Action
from perception.state import ChannelState, PieceObservation
from subsystems.bus import StationId
from subsystems.classification_channel.physical_controller import PhysicalC4Controller
from subsystems.classification_channel.physical_handoff import PhysicalHandoff
from subsystems.classification_channel.transfer_episode import TransferEpisode
from subsystems.feeder.go_to_angle.config import GoToAngleConfig
from subsystems.feeder.pulse_perception.config import PulsePerceptionConfig
from subsystems.feeder.pulse_perception.flow import (
    CHANNEL_OUTPUT_GEAR_RATIO,
    PulsePerceptionFeeding,
)
from test_bounded_transfer import Evidence


class Motor:
    def __init__(self, name):
        self._name = name
        self.position = 100
        self.target = 100
        self.stopped = True
        self.software_disabled = False
        self.moves = []
        self.speed_limits = []
        self.accept = True
        self.raise_after_accept = False

    def microsteps_for_degrees(self, degrees):
        return round(degrees * 10)

    def estimateMoveDegreesMs(self, *_args, **_kwargs):
        return 100

    def set_speed_limits(self, *_args):
        self.speed_limits.append(_args)

    def move_degrees(self, degrees):
        self.moves.append(degrees)
        if not self.accept:
            return False
        self.target = self.position + self.microsteps_for_degrees(degrees)
        self.stopped = False
        if self.raise_after_accept:
            raise OSError("acknowledgement lost after dispatch")
        return True

    def is_jittering(self):
        return False


class Logger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


def state(*, exit=False, drop=False, track=42, clearance=10.0):
    piece = PieceObservation(0.0, 0, 2 if exit else 1, (10, 20, 30, 40), track)
    return ChannelState(
        ts=time.time(), in_drop=drop, in_exit=exit, n_pieces=1,
        advance_clearance_deg=clearance, pieces=(piece,),
    )


def harness(*, physical=True, perception=None):
    c1, c2, c3 = (Motor(f"c{i}") for i in (1, 2, 3))
    irl = NS(c_channel_1_rotor_stepper=c1, c_channel_2_rotor_stepper=c2,
             c_channel_3_rotor_stepper=c3)
    owner = NS(handoff=None) if physical else None
    attempts, deliveries, calls = [], [], []
    shared = NS(
        c4_runtime_owner=owner,
        c3_transfer_episode=None,
        classification_ready=True,
        c3_motion_pending=False,
        c3_safe_staging_pending=False,
        chute_move_in_progress=False,
        publish_piece_release_attempt=lambda **kw: attempts.append(kw),
        publish_piece_delivered=lambda **kw: deliveries.append(kw),
    )
    gc = NS(logger=Logger(), perception_service=perception,
            rotary_channel_steppers_can_operate_in_parallel=True,
            runtime_stats=NS(observePulse=lambda *_: None))
    config = NS(machine_setup=NS(uses_classification_channel=True))
    flow = PulsePerceptionFeeding(irl, config, gc, shared, NS())
    flow._cfg = lambda: PulsePerceptionConfig(enable_ch1=False, enable_ch2=False)
    flow._recovery_cfg = lambda: GoToAngleConfig(ch3_fast_eject_enabled=False)

    def reserve(current, evidence):
        calls.append(("reserve", current, evidence))
        if owner is None or owner.handoff is not None:
            return False
        episode = TransferEpisode(0, 0, time.monotonic(), time.time(), leader_id=42)
        episode.release_evidence = evidence
        shared.c3_transfer_episode = episode
        owner.handoff = episode
        shared.classification_ready = False
        return True

    shared.reserve_c4_transfer = reserve
    return NS(flow=flow, shared=shared, owner=owner, irl=irl,
              attempts=attempts, deliveries=deliveries, calls=calls)


def release(h):
    h.flow._capture_c3_release = lambda _state: ({"anchor": {"low": 1}}, "view")
    h.flow._apply_action("ch3", 3, Action.PRECISE,
                         h.irl.c_channel_3_rotor_stepper, state(exit=True),
                         h.flow._cfg())


def test_reservation_precedes_c3_command_and_attempt_keeps_episode():
    h = harness()
    motor = h.irl.c_channel_3_rotor_stepper
    original_move = motor.move_degrees
    motor.move_degrees = lambda degrees: (
        h.calls.append(("move", degrees)), original_move(degrees)
    )[1]
    release(h)
    assert [row[0] for row in h.calls] == ["reserve", "move"]
    assert len(motor.moves) == len(h.attempts) == 1
    assert motor.moves == [pytest.approx(2.0 * CHANNEL_OUTPUT_GEAR_RATIO)]
    assert h.shared.c3_transfer_episode is h.owner.handoff
    assert h.shared.c3_release_evidence == {"anchor": {"low": 1}}
    assert h.shared.c3_release_view == "view"
    assert h.shared.c3_release_leader_id == 42
    assert h.attempts[0]["source"] is StationId.C3
    assert h.shared.c3_motion_pending and not h.shared.c3_safe_staging_pending
    assert not h.deliveries  # Command acceptance is never C4 arrival proof.


@pytest.mark.parametrize("failure", ["denied", "missing"])
def test_physical_release_fails_closed_before_motor(failure):
    h = harness()
    if failure == "denied":
        h.shared.reserve_c4_transfer = lambda *_: False
    else:
        h.shared.reserve_c4_transfer = None
    release(h)
    assert not h.irl.c_channel_3_rotor_stepper.moves
    assert not h.attempts and not h.deliveries


def test_rejected_c3_command_does_not_claim_a_release_attempt():
    h = harness()
    h.irl.c_channel_3_rotor_stepper.accept = False
    release(h)
    assert len(h.calls) == 1  # Reservation precedes the rejected command.
    assert len(h.irl.c_channel_3_rotor_stepper.moves) == 1
    assert h.shared.c3_transfer_episode is h.owner.handoff
    assert not h.attempts and not h.deliveries
    assert not h.shared.c3_motion_pending


def test_duplicate_ticks_cannot_reserve_or_pulse_same_handoff():
    h = harness()
    release(h)
    h.flow._busy_until["c3"] = 0
    h.flow._motion_tick += 1
    motor = h.irl.c_channel_3_rotor_stepper
    motor.position, motor.stopped = motor.target, True
    assert not h.flow._busy(motor)
    release(h)
    assert len(h.calls) == len(motor.moves) == len(h.attempts) == 1


def test_falling_edge_does_not_publish_delivery_for_physical_c4():
    h = harness()
    release(h)
    h.flow._on_ch3_dispense()
    assert not h.deliveries
    legacy = harness(physical=False)
    legacy.flow._on_ch3_dispense()
    assert len(legacy.deliveries) == 1


def test_legacy_c3_exit_pulse_does_not_require_physical_reservation():
    h = harness(physical=False)
    h.shared.reserve_c4_transfer = None
    release(h)
    assert h.irl.c_channel_3_rotor_stepper.moves == [
        pytest.approx(2.0 * CHANNEL_OUTPUT_GEAR_RATIO)]
    assert not h.attempts


def test_c3_completion_requires_fresh_stopped_and_exact_target():
    h = harness()
    release(h)
    motor = h.irl.c_channel_3_rotor_stepper
    h.flow._busy_until["c3"] = 0  # Estimated cooldown elapsed.
    h.flow._motion_tick += 1
    assert h.flow._busy(motor) and h.shared.c3_motion_pending
    h.flow._motion_tick += 1
    motor.stopped = True
    motor.position = motor.target - 1
    assert h.flow._busy(motor) and h.shared.c3_motion_pending
    h.flow._motion_tick += 1
    motor.position = motor.target
    assert not h.flow._busy(motor)
    assert not h.shared.c3_motion_pending
    assert not h.shared.c3_safe_staging_pending


def test_lost_acknowledgement_keeps_one_motor_owner():
    h = harness()
    motor = h.irl.c_channel_3_rotor_stepper
    motor.raise_after_accept = True
    with pytest.raises(OSError, match="acknowledgement lost"):
        release(h)
    assert len(motor.moves) == 1
    assert h.shared.c3_motion_pending
    assert h.flow._move_targets["c3"] == motor.target
    release(h)  # Existing owner cannot issue a duplicate dispatch.
    assert len(motor.moves) == 1


def test_physical_c3_advance_needs_measured_clearance_and_legacy_keeps_pulse():
    h = harness()
    h.flow._apply_action("ch3", 3, Action.ADVANCE,
                         h.irl.c_channel_3_rotor_stepper,
                         state(drop=True, clearance=None), h.flow._cfg())
    assert not h.irl.c_channel_3_rotor_stepper.moves
    legacy = harness(physical=False)
    legacy.flow._apply_action("ch3", 3, Action.ADVANCE,
                              legacy.irl.c_channel_3_rotor_stepper,
                              state(drop=True, clearance=None), legacy.flow._cfg())
    assert legacy.irl.c_channel_3_rotor_stepper.moves == [
        pytest.approx(30 * CHANNEL_OUTPUT_GEAR_RATIO)]


def test_physical_c3_advance_never_exceeds_clearance_after_minimum_pulse():
    h = harness()
    cfg = PulsePerceptionConfig(min_move_output_deg=10.0, drop_pulse_output_deg=2.0)
    h.flow._apply_action("ch3", 3, Action.ADVANCE,
                         h.irl.c_channel_3_rotor_stepper,
                         state(drop=True, clearance=3.0), cfg)
    assert h.irl.c_channel_3_rotor_stepper.moves == [
        pytest.approx(2.0 * CHANNEL_OUTPUT_GEAR_RATIO)]
    assert not h.calls and not h.attempts


def c4_motion_allowed(h):
    ticks = []
    runtime = NS(
        _verify=None, recovering=False, can_admit=False, bindings={},
        tick=lambda _now, **kwargs: ticks.append(kwargs),
    )
    controller = PhysicalC4Controller.__new__(PhysicalC4Controller)
    controller._paused = False
    controller._reestablishing = False
    controller.runtime = runtime
    controller.handoff = None
    controller._last_admission = 0.0
    controller.shared = h.shared
    controller.gc = NS(runtime_stats=NS(
        setOwnedPieceUuids=lambda *_: None,
        observeState=lambda *_: None,
    ))
    controller._capture = lambda _now: None
    controller.phaseName = lambda: "test"
    h.shared.set_classification_gate = lambda ready, **_kw: setattr(
        h.shared, "classification_ready", ready
    )
    controller._step()
    return ticks[-1]["allow_motion"]


@pytest.mark.parametrize(
    ("gate_open", "action", "safe_staging", "overlap"),
    [
        (False, Action.ADVANCE, True, True),
        (True, Action.ADVANCE, False, False),
        (True, Action.PRECISE, False, False),
    ],
)
def test_only_closed_boundary_staging_can_overlap_c4_index(
    gate_open, action, safe_staging, overlap
):
    h = harness()
    h.shared.classification_ready = gate_open
    if action is Action.PRECISE:
        release(h)
    else:
        h.flow._apply_action(
            "ch3", 3, action, h.irl.c_channel_3_rotor_stepper,
            state(drop=True, clearance=3.0), h.flow._cfg(),
        )
        assert h.irl.c_channel_3_rotor_stepper.moves == [
            pytest.approx(3.0 * CHANNEL_OUTPUT_GEAR_RATIO)
        ]
    assert h.shared.c3_motion_pending
    assert h.shared.c3_safe_staging_pending is safe_staging
    assert c4_motion_allowed(h) is overlap


def test_gate_opening_during_staging_preparation_blocks_c4_overlap():
    h = harness()
    h.shared.classification_ready = False
    motor = h.irl.c_channel_3_rotor_stepper
    motor.set_speed_limits = lambda *_args: setattr(
        h.shared, "classification_ready", True
    )
    h.flow._apply_action(
        "ch3", 3, Action.ADVANCE, motor,
        state(drop=True, clearance=3.0), h.flow._cfg(),
    )
    assert motor.moves == [pytest.approx(3.0 * CHANNEL_OUTPUT_GEAR_RATIO)]
    assert h.shared.c3_motion_pending
    assert not h.shared.c3_safe_staging_pending
    assert not c4_motion_allowed(h)


def test_pulse_step_registers_recovery_owner_and_keeps_c1_c2_pulses(monkeypatch):
    import subsystems.feeder.pulse_perception.flow as module

    h = harness(perception=NS(read_states=lambda: {
        2: state(drop=True), 3: ChannelState(time.time(), False, False, 0),
    }))
    h.flow._cfg = lambda: PulsePerceptionConfig()
    h.flow._stuck_watchdog.observe = lambda **_: None
    monkeypatch.setattr(module, "feeder_jam_incident_active", lambda *_a, **_k: False)
    h.flow.step()
    assert h.shared.request_c3_recovery.__self__ is h.flow
    assert h.shared.request_c3_recovery.__func__ is h.flow._recover_transfer.__func__
    assert h.irl.c_channel_1_rotor_stepper.moves == []  # C2 drop zone blocks C1.
    assert h.irl.c_channel_2_rotor_stepper.moves == [
        pytest.approx(10 * CHANNEL_OUTPUT_GEAR_RATIO)]
    assert h.irl.c_channel_3_rotor_stepper.moves == []


def test_empty_c2_preserves_normal_c1_pulse(monkeypatch):
    import subsystems.feeder.pulse_perception.flow as module

    h = harness(perception=NS(read_states=lambda: {}))
    h.flow._cfg = lambda: PulsePerceptionConfig()
    h.flow._stuck_watchdog.observe = lambda **_: None
    monkeypatch.setattr(module, "feeder_jam_incident_active", lambda *_a, **_k: False)
    h.flow.step()
    assert h.irl.c_channel_1_rotor_stepper.moves == [
        pytest.approx(1.0 * CHANNEL_OUTPUT_GEAR_RATIO)]
    assert not h.irl.c_channel_2_rotor_stepper.moves
    assert not h.irl.c_channel_3_rotor_stepper.moves


def recovery_handoff(monkeypatch, *, arrived=False, retained=False):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "time", lambda: 1000.0 + clock[0])
    evidence = Evidence(clock)
    evidence.gap = 0.0
    initial = __import__(
        "subsystems.feeder.go_to_angle.recovery", fromlist=["capture_support"]
    ).capture_support(evidence)
    initial["anchor"] = dict(initial["material"][0])
    initial["followers"] = []
    initial["motion_deg"] = 2.0
    h = harness(perception=evidence)
    ep = TransferEpisode(0, 0, 100.0, 1100.0, leader_id=42)
    ep.release_evidence = initial
    h.shared.c3_transfer_episode = ep
    h.owner.handoff = ep
    h.shared.request_c3_recovery = h.flow._recover_transfer
    h.flow._last_perception_tick = clock[0]
    evidence.arrived = arrived
    evidence.missing = not retained
    finishes = []
    binding = NS(episode=ep)
    controller = NS(
        shared=h.shared, gc=h.flow.gc, irl=h.irl, perception=evidence,
        config=NS(presence_streak_to_start=2),
        finish_handoff=lambda _binding, now, *, arrived: finishes.append((now, arrived)),
        recovery_boundary=lambda _binding, stamp: {
            "frame_ts": stamp,
            "predicates": {"reserved_boundary": True, "c4_available": True,
                           "c4_stopped_aligned": True, "c4_frame_fresh": True,
                           "c4_empty": True},
        },
        noteProgress=lambda: None,
    )
    h.flow.gc.runtime_stats.observeTransferEpisode = lambda *_: None
    return h, PhysicalHandoff(controller, binding), evidence, clock, finishes


def test_physical_handoff_confirms_arrival_with_pulse_recovery_owner(monkeypatch):
    h, handoff, _evidence, clock, finishes = recovery_handoff(
        monkeypatch, arrived=True, retained=False
    )
    for at in (100.1, 100.2, 103.1, 103.2):
        clock[0] = at
        h.flow._last_perception_tick = at
        handoff.tick(at)
    assert finishes == [(103.2, True)]
    assert not h.irl.c_channel_3_rotor_stepper.moves
    assert h.owner.handoff is handoff._episode  # No replacement reservation.


def test_physical_handoff_retained_original_not_misadmitted(monkeypatch):
    h, handoff, _evidence, clock, finishes = recovery_handoff(
        monkeypatch, arrived=True, retained=True
    )
    for at in (100.1, 100.2, 103.1, 103.2):
        clock[0] = at
        h.flow._last_perception_tick = at
        handoff.tick(at)
    assert finishes == []
    assert handoff._episode.recovery_arrival is False
    assert h.shared.c3_transfer_episode is handoff._episode


def test_physical_handoff_uncertain_departure_discard_binds_same_episode(monkeypatch):
    h, handoff, _evidence, clock, finishes = recovery_handoff(
        monkeypatch, arrived=False, retained=False
    )
    for at in (100.1, 103.2, 115.3):
        clock[0] = at
        h.flow._last_perception_tick = at
        handoff.tick(at)
    assert finishes == [(115.3, False)]
    assert handoff._episode.forced_reject_reason
    assert h.shared.c3_transfer_episode is handoff._episode
    assert not h.irl.c_channel_3_rotor_stepper.moves


@pytest.mark.parametrize("identity", ["radial_jump", "duplicate_id", "missing_id"])
def test_physical_handoff_unsupported_identity_expires_to_reject(monkeypatch, identity):
    h, handoff, evidence, clock, finishes = recovery_handoff(
        monkeypatch, arrived=False, retained=True
    )
    ep = handoff._episode
    original = ep.episode_id, ep.boundary_index, ep.pocket_id
    if identity == "radial_jump":
        evidence.override = [PieceObservation(0, 0, 2, (600, 496, 608, 504), 42)]
    elif identity == "duplicate_id":
        evidence.override = [evidence.piece(0, 42), evidence.piece(40, 42)]
    else:
        evidence.leader_id = None
    ep.recovery_started_mono, ep.recovery_deadline_mono = 103.0, 115.0
    ep.state = "recovering"
    clock[0] = h.flow._last_perception_tick = 115.3
    handoff.tick(clock[0])
    assert finishes == [(115.3, False)]
    assert ep.recovery_decision["final_outcome"] == "unverified_discard_bound"
    assert ep.forced_reject_reason
    assert (ep.episode_id, ep.boundary_index, ep.pocket_id) == original
    assert h.shared.c3_transfer_episode is ep
    assert not h.irl.c_channel_3_rotor_stepper.moves
    assert not h.deliveries


def test_physical_handoff_blocked_supported_original_keeps_intervention(monkeypatch):
    h, handoff, evidence, clock, finishes = recovery_handoff(
        monkeypatch, arrived=False, retained=True
    )
    ep = handoff._episode
    evidence.trailing_gap = 0.0
    ep.recovery_started_mono, ep.recovery_deadline_mono = 103.0, 115.0
    ep.state = "recovering"
    clock[0] = h.flow._last_perception_tick = 115.3
    with pytest.raises(RuntimeError, match="follower blocks"):
        handoff.tick(clock[0])
    assert ep.state == "unresolved" and not ep.forced_reject_reason
    assert finishes == [] and h.shared.c3_transfer_episode is ep
    assert not h.irl.c_channel_3_rotor_stepper.moves


def test_physical_handoff_unsupported_identity_cannot_discard_owned_motion(monkeypatch):
    h, handoff, evidence, clock, finishes = recovery_handoff(
        monkeypatch, arrived=False, retained=True
    )
    ep = handoff._episode
    evidence.override = [PieceObservation(0, 0, 2, (600, 496, 608, 504), 42)]
    ep.recovery_started_mono, ep.recovery_deadline_mono = 103.0, 115.0
    ep.state = "recovering"
    motor = h.irl.c_channel_3_rotor_stepper
    motor.target, motor.stopped = 200, False
    h.flow._move_targets["c3"] = motor.target
    h.shared.c3_motion_pending = True
    clock[0] = h.flow._last_perception_tick = 115.3
    with pytest.raises(RuntimeError, match="C3 motor has not completed"):
        handoff.tick(clock[0])
    assert ep.state == "unresolved" and finishes == []
    assert h.shared.c3_motion_pending and h.flow._move_targets["c3"] == 200
    assert not motor.moves


def recovery_action_fixture(monkeypatch):
    h, handoff, _evidence, clock, _finishes = recovery_handoff(
        monkeypatch, arrived=False, retained=True
    )
    ep = handoff._episode
    clock[0] = 104.0
    h.flow._last_perception_tick = 104.0
    ep.recovery_started_mono = 103.0
    ep.recovery_deadline_mono = 115.0
    ep.state = "recovering"
    ep.release_evidence["leader_com"] = 5.0
    ep.release_evidence["completion_observed_wall"] = 1100.0
    boundary = handoff.controller.recovery_boundary(handoff.binding, 1104.0)
    return h, ep, boundary, clock


def test_bounded_recovery_action_uses_same_episode_and_fresh_motor_owner(monkeypatch):
    h, ep, boundary, clock = recovery_action_fixture(monkeypatch)
    owner = h.shared.request_c3_recovery
    observation = owner(ep, boundary)
    assert observation["result"] == "observed"
    assert observation["same_piece_retained"]
    assert not h.irl.c_channel_3_rotor_stepper.moves
    action = {"stage": 1, "key": "1.forward"}
    accepted = owner(ep, boundary, action=action)
    assert accepted["result"] == "accepted"
    assert len(ep.recovery_legs) == len(h.irl.c_channel_3_rotor_stepper.moves) == 1
    assert ep.recovery_legs[0]["target"] == h.flow._move_targets["c3"]
    assert h.shared.c3_motion_pending and not h.shared.c3_safe_staging_pending
    assert h.shared.c3_transfer_episode is ep
    owner(ep, boundary, action=action)
    assert len(h.irl.c_channel_3_rotor_stepper.moves) == 1
    motor = h.irl.c_channel_3_rotor_stepper
    h.flow._busy_until["c3"] = 0
    h.flow._motion_tick += 1
    assert h.flow._busy(motor)  # Cooldown alone cannot clear the accepted leg.
    motor.stopped, motor.position = True, motor.target
    h.flow._motion_tick += 1
    assert not h.flow._busy(motor)
    assert not h.shared.c3_motion_pending
    clock[0] = 104.5
    owner(ep, boundary, action=action)
    assert len(motor.moves) == 1  # Spent key cannot replay after completion.


def test_recovery_uses_pulse_motor_envelope_and_gotoangle_only_policy(monkeypatch):
    import toml_config

    h, ep, boundary, _clock = recovery_action_fixture(monkeypatch)
    pulse = PulsePerceptionConfig(
        forward_direction_sign=-1,
        ch3_move_speed_usteps_per_s=3456,
        ch3_max_move_output_deg=7.0,
        exit_pulse_pause_ms=444,
    )
    h.flow._cfg = lambda: pulse
    del h.flow._recovery_cfg  # Use the production envelope constructor.
    monkeypatch.setattr(toml_config, "getGoToAngleConfig", lambda: {
        "forward_direction_sign": 1,
        "move_speed_usteps_per_s": 1111,
        "max_move_output_deg": 70.0,
        "precise_pulse_pause_ms": 100,
        "ch3_release_margin_output_deg": 1.75,
        "fall_recovery_max_jitter_attempts": 2,
        "jitter_amplitude_motor_deg": 5.5,
        "jitter_pause_ms": 610,
    })
    cfg = h.flow._recovery_cfg()
    assert cfg.forward_direction_sign == -1
    assert cfg.move_speed_usteps_per_s == 3456
    assert cfg.max_move_output_deg == 7.0
    assert cfg.precise_pulse_pause_ms == 444
    assert cfg.ch3_release_margin_output_deg == 1.75
    assert cfg.fall_recovery_max_jitter_attempts == 2
    assert cfg.jitter_amplitude_motor_deg == 5.5
    assert cfg.jitter_pause_ms == 610

    decision = h.shared.request_c3_recovery(
        ep, boundary, action={"stage": 1, "key": "1.forward"}
    )
    motor = h.irl.c_channel_3_rotor_stepper
    assert decision["result"] == "accepted"
    assert 0 < decision["signed_output_degrees"] <= 7.0
    assert motor.moves == [pytest.approx(
        -decision["signed_output_degrees"] * CHANNEL_OUTPUT_GEAR_RATIO
    )]
    assert motor.speed_limits[-1][1] == 3456
    assert h.shared.c3_motion_pending and not h.shared.c3_safe_staging_pending


def test_positive_retained_c3_path_keeps_original_episode(monkeypatch):
    h, ep, boundary, _clock = recovery_action_fixture(monkeypatch)
    decision = h.shared.request_c3_recovery(
        ep, boundary, action={"stage": 1, "key": "1.forward", "retained": True}
    )
    assert decision["result"] == "accepted"
    assert decision["same_piece_retained"]
    assert h.shared.c3_transfer_episode is ep
    assert len(ep.recovery_legs) == len(h.irl.c_channel_3_rotor_stepper.moves) == 1
    assert h.shared.c3_motion_pending and not h.shared.c3_safe_staging_pending


def test_recovery_rejects_stale_evidence_and_expired_budget(monkeypatch):
    h, ep, boundary, clock = recovery_action_fixture(monkeypatch)
    action = {"stage": 1, "key": "1.forward"}
    clock[0] = 116.0
    h.flow._last_perception_tick = 116.0
    boundary["frame_ts"] = 1116.0
    expired = h.shared.request_c3_recovery(ep, boundary, action=action)
    assert expired["result"] in {"unsafe", "wait"}
    assert not h.irl.c_channel_3_rotor_stepper.moves
    clock[0] = 104.0
    ep.recovery_deadline_mono = 115.0
    h.flow._last_perception_tick = 104.0
    boundary["frame_ts"] = 1104.0
    h.flow.gc.perception_service.c3_stale = True
    stale = h.shared.request_c3_recovery(ep, boundary, action=action)
    assert stale["result"] in {"unsafe", "wait"}
    assert not h.irl.c_channel_3_rotor_stepper.moves


def test_recovery_unknown_ack_spends_leg_and_retains_motor_owner(monkeypatch):
    h, ep, boundary, _clock = recovery_action_fixture(monkeypatch)
    motor = h.irl.c_channel_3_rotor_stepper
    motor.raise_after_accept = True
    action = {"stage": 1, "key": "1.forward"}
    result = h.shared.request_c3_recovery(ep, boundary, action=action)
    assert result["result"] == "acceptance_unknown"
    assert len(ep.recovery_legs) == len(motor.moves) == 1
    assert h.shared.c3_motion_pending
    assert h.flow._move_targets["c3"] == motor.target
    h.shared.request_c3_recovery(ep, boundary, action=action)
    assert len(motor.moves) == 1
