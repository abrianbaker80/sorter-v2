"""Retained READY transaction qualification; all hardware is local fixture feedback."""

from types import SimpleNamespace
from copy import deepcopy
from unittest.mock import Mock

import pytest

from coordinator import Coordinator
from defs.known_object import KnownObject, PieceStage
from piece_transport import ClassificationChannelTransport
from subsystems.bus import TickBus
from subsystems.classification_channel.pocket_ledger import PocketRoute
from subsystems.distribution.ready import Ready
from subsystems.distribution.states import DistributionState
from subsystems.shared_variables import SharedVariables
from test_distribution_sending import _GlobalConfig
from test_indexed_pocket_pipeline import _pipeline


@pytest.fixture
def retained(monkeypatch):
    monkeypatch.setattr("server.shared_state.hardware_state", "ready")
    monkeypatch.setattr("server.shared_state.hardware_error", None)
    gc = _GlobalConfig()
    gc.disable_chute = False
    shared = SharedVariables(gc=gc, bus=TickBus())
    transport = shared.transport = ClassificationChannelTransport()
    piece = KnownObject()
    piece.stage = PieceStage.distributing
    piece.destination_bin = (0, 0, 0)
    piece.distribution_positioned_at = 123.0
    piece.harvest_allocation_id = "retained-allocation"
    transport.placePieceForDistribution(piece)
    chute = SimpleNamespace(
        homed=True, current_angle=30.0,
        getAngleForBin=Mock(return_value=30.0),
        stepper=SimpleNamespace(stopped=True, degrees_for_microsteps=lambda _: 0.9),
    )
    irl = SimpleNamespace(chute=chute, servos=[
        SimpleNamespace(available=True, is_calibrated=True, stopped=True, opened=False),
    ])
    irl.servos[0].isOpen = lambda: irl.servos[0].opened
    irl.servos[0].isClosed = lambda: not irl.servos[0].opened
    ready = Ready(irl, gc, shared)
    shared.set_distribution_gate(False, reason="positioning")
    assert ready.step() is None
    assert shared.distribution_ready
    coordinator = object.__new__(Coordinator)
    coordinator.gc = gc
    coordinator.shared = shared
    return SimpleNamespace(ready=ready, shared=shared, transport=transport,
                           piece=piece, gc=gc, irl=irl, coordinator=coordinator)


def hold(retained):
    incident = {"kind": "test_hold"}
    retained.gc.runtime_stats.setActiveIncident(incident)
    retained.coordinator._hold_process_for_incident(incident)
    assert not retained.shared.distribution_ready


def clear(retained):
    retained.gc.runtime_stats.clearActiveIncident()


def test_incident_clear_restores_gate_and_preserves_owned_c4_load(retained):
    r = retained
    pipeline = _pipeline()
    pipeline.shared = r.shared
    pipeline.transport = r.transport
    worker = SimpleNamespace(ctx=SimpleNamespace(
        known_object=r.piece, classification_applied=True,
    ))
    load = pipeline._ledger.admit(worker, admitted_at_mono=1.0)
    load.lock_route(PocketRoute.NORMAL)
    load.distribution_placed = True
    original_piece = deepcopy(vars(r.piece))
    signaled_at = r.ready._signaled_at
    gate_write = Mock(wraps=r.shared.set_distribution_gate)
    r.shared.set_distribution_gate = gate_write

    hold(r)
    for _ in range(3):
        assert r.ready.step() is None
        assert not pipeline._prepareDueLoad(load)
        assert not r.shared.distribution_ready
    clear(r)
    for _ in range(3):
        clear(r)
        assert r.ready.step() is None
        assert pipeline._prepareDueLoad(load)
    assert sum(call.args[0] is True for call in gate_write.call_args_list) == 1
    assert r.ready._positioned_uuid == r.piece.uuid
    assert r.ready._signaled_at == signaled_at
    assert r.transport.getPieceForDistributionPositioning() is r.piece
    assert r.transport.getPieceForDistributionDrop() is None
    assert vars(r.piece) == original_piece
    assert pipeline._ledger.loads == (load,)
    assert load.payload is worker and load.distribution_placed and load.route_locked
    assert pipeline._stepper.moves == []
    assert r.gc.run_recorder.pieces == []


@pytest.mark.parametrize("invalid", [
    "standby", "homing", "error", "hardware_error", "stage", "destination",
    "chute_motion", "unhomed", "moving", "off_target", "bad_target", "bad_feedback",
    "bad_tolerance", "not_positioned", "servo_offline", "servo_uncalibrated",
    "servo_moving", "feedback_error", "missing_chute",
])
def test_invalid_readiness_cannot_reopen_gate(retained, monkeypatch, invalid):
    r = retained
    hold(r)
    clear(r)
    if invalid in {"standby", "homing", "error"}:
        monkeypatch.setattr("server.shared_state.hardware_state", invalid)
    elif invalid == "hardware_error":
        monkeypatch.setattr("server.shared_state.hardware_error", "fault")
    elif invalid == "stage":
        r.piece.stage = PieceStage.created
    elif invalid == "destination":
        r.piece.destination_bin = (0, 0, 1)
    elif invalid == "chute_motion":
        r.shared.set_chute_motion(True, target_bin=None)
    elif invalid == "unhomed":
        r.irl.chute.homed = False
    elif invalid == "moving":
        r.irl.chute.stepper.stopped = False
    elif invalid == "off_target":
        r.irl.chute.current_angle = 40.0
    elif invalid == "bad_target":
        r.irl.chute.getAngleForBin.return_value = None
    elif invalid == "bad_feedback":
        r.irl.chute.current_angle = float("nan")
    elif invalid == "bad_tolerance":
        r.irl.chute.stepper.degrees_for_microsteps = lambda _: 0.0
    elif invalid == "not_positioned":
        r.piece.distribution_positioned_at = None
    elif invalid == "servo_offline":
        r.irl.servos[0].available = False
    elif invalid == "servo_uncalibrated":
        r.irl.servos[0].is_calibrated = False
    elif invalid == "servo_moving":
        r.irl.servos[0].stopped = False
    elif invalid == "feedback_error":
        r.irl.chute.getAngleForBin.side_effect = RuntimeError("no feedback")
    elif invalid == "missing_chute":
        del r.irl.chute
    for _ in range(3):
        assert r.ready.step() is None
        assert not r.shared.distribution_ready
        assert r.ready._positioned_uuid == r.piece.uuid
        assert r.transport.getPieceForDistributionPositioning() is r.piece
        assert r.transport.getPieceForDistributionDrop() is None


def test_incident_before_first_ready_tick_does_not_signal_or_consume_cancel(retained):
    r = retained
    r.ready.cleanup()
    hold(r)
    r.transport.cancelPieceForDistribution(r.piece.uuid)
    assert r.ready.step() is None
    assert not r.ready.signaled
    assert not r.shared.distribution_ready
    assert r.transport.isCanceledPieceForDistribution(r.piece.uuid)
    clear(r)
    assert r.ready.step() == DistributionState.IDLE
    assert not r.transport.isCanceledPieceForDistribution(r.piece.uuid)


@pytest.mark.parametrize("action", ["advance", "replace", "cancel", "no_transport"])
def test_changed_transaction_never_restores_old_ready_gate(retained, action):
    r = retained
    hold(r)
    clear(r)
    if action == "advance":
        r.transport.advanceTransport()
    elif action == "replace":
        r.transport.placePieceForDistribution(KnownObject())
    elif action == "cancel":
        r.transport.cancelPieceForDistribution(r.piece.uuid)
    else:
        r.shared.transport = None
    next_state = r.ready.step()
    if action == "cancel":
        assert next_state == DistributionState.IDLE  # Existing cancellation handshake.
        assert r.transport.getPieceForDistributionDrop() is None
    else:
        assert not r.shared.distribution_ready
        assert next_state == (None if action == "no_transport" else DistributionState.SENDING)
    assert r.ready._positioned_uuid == r.piece.uuid
    assert r.gc.run_recorder.pieces == []


def test_normal_ready_signals_once_then_detects_durable_drop(retained):
    r = retained
    r.shared.set_distribution_gate = Mock(wraps=r.shared.set_distribution_gate)
    for _ in range(3):
        assert r.ready.step() is None
    r.shared.set_distribution_gate.assert_not_called()
    r.transport.advanceTransport()
    assert r.ready.step() == DistributionState.SENDING
    r.shared.set_distribution_gate.assert_not_called()
    assert r.transport.getPieceForDistributionDrop() is r.piece
    r.ready.cleanup()
    assert not r.ready.signaled and r.ready._positioned_uuid is None


def test_coordinator_clear_tick_restores_before_classification_steps(retained):
    r = retained
    c = r.coordinator
    c.bus = TickBus()
    c._maybe_start_auto_incident_resolution = Mock()
    c.distribution = SimpleNamespace(step=Mock(side_effect=r.ready.step))
    observed_gates = []
    c.classification = SimpleNamespace(step=lambda: observed_gates.append(r.shared.distribution_ready))
    c.feeder = SimpleNamespace(hold_motion=Mock(), step=Mock())
    c.manual_feed_mode = False
    hold(r)
    for _ in range(3):
        c.step()
        assert not r.shared.distribution_ready
    c.distribution.step.assert_not_called()
    assert observed_gates == []
    c.feeder.step.assert_not_called()
    clear(r)
    c.step()
    assert observed_gates == [True]
    assert r.transport.getPieceForDistributionPositioning() is r.piece


def test_incident_appearing_during_feedback_blocks_restoration(retained):
    r = retained
    hold(r)
    clear(r)
    def target(_address):
        r.gc.runtime_stats.setActiveIncident({"kind": "new_fault"})
        return 30.0
    r.irl.chute.getAngleForBin.side_effect = target
    assert r.ready.step() is None
    assert not r.shared.distribution_ready


def test_passthrough_restoration_does_not_require_a_bin_target(retained):
    r = retained
    r.ready.cleanup()
    r.piece.destination_bin = None
    r.irl.servos[0].opened = True
    r.piece.distribution_positioned_at = None
    assert r.ready.step() is None
    hold(r)
    clear(r)
    assert r.ready.step() is None
    assert r.shared.distribution_ready
    r.irl.chute.getAngleForBin.assert_not_called()
