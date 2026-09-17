import queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from coordinator import Coordinator
from defs.known_object import KnownObject
from subsystems.distribution.chute import BinAddress, GEAR_RATIO
from subsystems.distribution.positioning import Positioning
from subsystems.distribution.state_machine import DistributionStateMachine
from subsystems.distribution.states import DistributionState


@pytest.fixture
def interrupted(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("subsystems.distribution.positioning.time.monotonic", lambda: clock[0])
    chute = SimpleNamespace(
        homed=True, current_angle=64.921875,
        stepper=SimpleNamespace(stopped=True, degrees_for_microsteps=lambda n: n * 0.225),
        getAngleForBin=Mock(return_value=322.55), moveToBin=Mock(return_value=1300),
    )
    gc = SimpleNamespace(logger=Mock(), runtime_stats=Mock(), disable_servos=False, disable_chute=False)
    transport = object()
    shared = SimpleNamespace(set_chute_motion=Mock(), set_distribution_gate=Mock(), transport=transport, distribution_ready=False)
    positioner = Positioning(SimpleNamespace(), gc, shared, chute, SimpleNamespace(), Mock(), queue.Queue())
    positioner._phase = "moving"
    positioner._target_address = BinAddress(0, 4, 1)
    positioner._piece = KnownObject()
    positioner._piece.harvest_allocation_id = "preserved-allocation"
    positioner._moving_started_at = 90.0
    positioner._state_entered_at = 89.0
    positioner._movingDoorServoIndices = Mock(return_value=[])
    positioner._raiseChuteJamAlert = Mock()
    machine = object.__new__(DistributionStateMachine)
    machine.current_state = DistributionState.POSITIONING
    machine.states_map = {DistributionState.POSITIONING: positioner}
    coordinator = object.__new__(Coordinator)
    coordinator.distribution = machine
    coordinator.classification = SimpleNamespace(supportsStatefulPause=lambda: True, resume=Mock())
    return positioner, coordinator, clock


def test_rehome_reissues_retained_route_and_waits_for_verified_completion(interrupted):
    state, coordinator, clock = interrupted
    piece, address, transport = state._piece, state._target_address, state.shared.transport
    coordinator.resume()
    assert state.step() is None
    state.chute.moveToBin.assert_called_once_with(address)
    assert state._piece is piece and state._target_address is address
    assert state.shared.transport is transport
    assert piece.harvest_allocation_id == "preserved-allocation"
    assert piece.distribution_positioned_at is None
    state.chute.stepper.stopped = False
    assert state.step() is None
    state.chute.stepper.stopped = True
    # An old stopped observation cannot complete a newly accepted move.
    assert state.step() is None
    clock[0] += 2
    state.chute.current_angle = 322.55
    assert state.step() == DistributionState.READY
    assert piece.distribution_positioned_at is not None
    state.chute.moveToBin.assert_called_once_with(address)


def test_normal_pause_at_target_does_not_duplicate_command(interrupted):
    state, coordinator, _ = interrupted
    state.chute.current_angle = 322.55
    coordinator.resume()
    assert state.step() == DistributionState.READY
    state.chute.moveToBin.assert_not_called()


def test_resume_waits_for_existing_motion_and_valid_home(interrupted):
    state, coordinator, _ = interrupted
    coordinator.resume()
    state.chute.stepper.stopped = False
    assert state.step() is None
    state.chute.moveToBin.assert_not_called()
    state.chute.stepper.stopped = True
    state.chute.homed = False
    assert state.step() is None
    state.chute.moveToBin.assert_not_called()
    state.chute.homed = True
    assert state.step() is None
    state.chute.moveToBin.assert_called_once_with(state._target_address)


def test_failed_reposition_does_not_retry_or_mark_ready(interrupted):
    state, coordinator, clock = interrupted
    coordinator.resume()
    assert state.step() is None
    clock[0] += 2
    for _ in range(2):
        assert state.step() is None
    state.chute.moveToBin.assert_called_once_with(state._target_address)
    assert state._piece.distribution_positioned_at is None
    assert state._raiseChuteJamAlert.called


def test_normal_move_stopped_at_wrong_bin_fails_closed(interrupted):
    state, _, _ = interrupted
    assert state.step() is None
    state.chute.moveToBin.assert_not_called()
    assert state._raiseChuteJamAlert.called


@pytest.mark.parametrize("angle", [float("nan"), float("inf")])
def test_invalid_position_feedback_cannot_release(interrupted, angle):
    state, coordinator, _ = interrupted
    state.chute.current_angle = angle
    coordinator.resume()
    assert state.step() is None
    assert state._raiseChuteJamAlert.called
    state.chute.moveToBin.assert_not_called()


def test_unreachable_reserved_target_is_not_reallocated(interrupted):
    state, coordinator, _ = interrupted
    state.chute.getAngleForBin.return_value = None
    coordinator.resume()
    assert state.step() is None
    state.chute.moveToBin.assert_not_called()
    assert state._raiseChuteJamAlert.called


def test_position_tolerance_is_one_motor_microstep(interrupted):
    state, _, _ = interrupted
    state.chute.current_angle = 322.55 + 0.225 / GEAR_RATIO * 0.9
    assert state.step() == DistributionState.READY


def test_explicit_disabled_chute_mode_keeps_simulation_behavior(interrupted):
    state, _, _ = interrupted
    state.gc.disable_chute = True
    state.chute.homed = False
    assert state.step() == DistributionState.READY


def test_reposition_command_error_preserves_route_and_does_not_retry(interrupted):
    state, coordinator, clock = interrupted
    piece, address = state._piece, state._target_address
    state.chute.moveToBin.side_effect = RuntimeError("ambiguous motor acknowledgement")
    coordinator.resume()
    assert state.step() is None
    clock[0] += 2
    assert state.step() is None
    state.chute.moveToBin.assert_called_once_with(address)
    assert state._piece is piece and state._target_address is address
    assert piece.distribution_positioned_at is None
    assert state._raiseChuteJamAlert.called


def test_feedback_exception_cannot_release(interrupted):
    state, coordinator, _ = interrupted
    state.chute.getAngleForBin.side_effect = RuntimeError("position unavailable")
    coordinator.resume()
    assert state.step() is None
    state.chute.moveToBin.assert_not_called()
    assert state._raiseChuteJamAlert.called
