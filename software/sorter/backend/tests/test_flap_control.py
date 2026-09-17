"""Real PCA driver + manual routing with an inert firmware transport."""
import queue
import struct
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from defs.sorter_controller import SorterLifecycle
from hardware.sorter_interface import InterfaceCommandCode as C, ServoMotor
from irl.bin_layout import BinLayoutConfig, LayerConfig
from server import shared_state
from server.routers import hardware
from sorter_controller import SorterController
from subsystems.distribution.chute import BinAddress
from subsystems.distribution.positioning import Positioning


class Firmware:
    def __init__(self):
        self.positions = [390, 100, 560]
        self.targets = [None] * 3
        self.moves = []
        self.reject = None
        self.hold = False
        self.short = False
        self.io_error = False

    def send_command(self, command, channel, payload):
        if self.io_error:
            raise OSError('transport unavailable')
        if command == C.SERVO_IS_STOPPED:
            if self.targets[channel] is not None:
                if self.hold:
                    return NS(payload=b'\x00')
                if not self.short:
                    self.positions[channel] = self.targets[channel]
                self.targets[channel] = None
            return NS(payload=b'\x01')
        if command == C.SERVO_GET_POSITION:
            return NS(payload=struct.pack('<H', self.positions[channel]))
        if command in (C.SERVO_MOVE_TO, C.SERVO_MOVE_TO_AND_RELEASE):
            angle = struct.unpack('<H', payload[:2])[0]
            self.moves.append((channel, angle, command, payload))
            if channel == self.reject or self.targets[channel] is not None:
                return NS(payload=b'\x00')
            self.targets[channel] = angle
            return NS(payload=b'\x01')
        if command == C.SERVO_STOP:
            self.targets[channel] = None
        return NS(payload=b'\x01')


@pytest.fixture
def rig(monkeypatch):
    firmware = Firmware()
    gc = NS(logger=Mock(), disable_servos=False, disable_chute=False, runtime_stats=Mock())
    servos = [ServoMotor(firmware, i, gc) for i in range(3)]
    for servo, opened, closed in zip(servos, [39, 10, 56], [105, 110, 120]):
        servo.set_preset_angles(opened, closed)
    layout = NS(layers=[NS(sections=[NS(bins=[None] * 3)] * 6) for _ in range(3)])
    chute = NS(homed=True, stepper=NS(stopped=True), layout=layout,
               current_angle=60.0, getAngleForBin=Mock(return_value=60.0),
               moveToBin=Mock(return_value=250))
    controller = NS(state=SorterLifecycle.PAUSED, irl=NS(servos=servos, chute=chute))
    monkeypatch.setattr(shared_state, 'controller_ref', controller)
    monkeypatch.setattr(shared_state, 'hardware_worker_thread', None)
    monkeypatch.setattr(shared_state, 'hardware_state', 'ready')
    return NS(firmware=firmware, servos=servos, gc=gc, chute=chute, controller=controller)


def select(layer, section=0, bin_=0):
    return hardware.move_to_bin(hardware.MoveToBinPayload(
        layer_index=layer, section_index=section, bin_index=bin_))


@pytest.mark.parametrize('layer', [0, 1, 2])
def test_selects_only_requested_intercepting_layer(rig, layer):
    result = select(layer)
    assert result['ok'] and result['flap_state'] == 'settled'
    assert result['chute_state'] == 'requested'
    assert [s.isClosed() for s in rig.servos] == [i == layer for i in range(3)]
    assert [s.isOpen() for s in rig.servos] == [i != layer for i in range(3)]
    assert rig.firmware.moves[0][0] == layer
    assert len(rig.firmware.moves) == 3


@pytest.mark.parametrize('layer', [0, 1, 2])
def test_same_layer_all_bins_never_cycle_flaps(rig, layer):
    select(layer)
    original = list(rig.firmware.moves)
    for section in range(6):
        for bin_ in range(3):
            select(layer, section, bin_)
    assert rig.firmware.moves == original
    assert rig.chute.moveToBin.call_count == 19


@pytest.mark.parametrize('first,second', [(a,b) for a in range(3) for b in range(3) if a != b])
def test_layer_transition_moves_only_previous_and_new_destination(rig, first, second):
    select(first)
    rig.firmware.moves.clear()
    select(second)
    assert [(i,a) for i,a,_,_ in rig.firmware.moves] == [
        (second, rig.servos[second].closed_angle * 10),
        (first, rig.servos[first].open_angle * 10),
    ]


def test_pending_same_target_coalesces_opposing_target_is_refused(rig):
    servo = rig.servos[0]
    rig.firmware.hold = True
    servo.close()
    servo.close()
    assert not servo.isClosed() and not servo.isOpen()
    assert servo.feedback()['state'] == 'moving'
    with pytest.raises(RuntimeError, match='busy'):
        servo.open()
    assert len(rig.firmware.moves) == 1
    rig.firmware.hold = False
    assert servo.isClosed()
    servo.open()
    assert servo.isOpen()


@pytest.mark.parametrize('method', ['move_to', 'move_to_and_release'])
def test_rejected_command_is_not_recorded_as_reached(rig, method):
    servo = rig.servos[0]
    servo.open()
    assert servo.isOpen()
    rig.firmware.reject = 0
    assert getattr(servo, method)(105) is False
    assert servo._current_angle == 39
    assert servo.angle is None
    assert servo.feedback()['state'] == 'failed'
    assert servo.feedback()['target_angle'] is None
    assert not servo.isClosed()
    with pytest.raises(RuntimeError, match='rejected'):
        servo.close()


def test_failed_target_returns_http_error_without_chute_motion_and_allows_explicit_retry(rig):
    app = FastAPI()
    app.include_router(hardware.router)
    rig.firmware.reject = 1
    with TestClient(app) as client:
        response = client.post('/api/bins/move-to', json={'layer_index': 1, 'section_index': 0, 'bin_index': 0})
        assert response.status_code == 409
        assert 'rejected' in response.json()['detail']
    rig.chute.moveToBin.assert_not_called()
    assert not rig.servos[1].isClosed()
    rig.firmware.reject = None
    assert select(1)['ok']


def test_failed_passage_flap_does_not_claim_success(rig):
    rig.firmware.reject = 0
    with pytest.raises(hardware.HTTPException) as error:
        select(2)
    assert error.value.status_code == 409
    assert 'rejected' in error.value.detail
    rig.chute.moveToBin.assert_not_called()


def test_existing_busy_flap_rejects_before_any_new_command(rig):
    rig.firmware.hold = True
    rig.servos[2].close()
    before = list(rig.firmware.moves)
    with pytest.raises(hardware.HTTPException) as error:
        select(0)
    assert 'still moving' in error.value.detail
    assert rig.firmware.moves == before
    rig.chute.moveToBin.assert_not_called()


def test_stopped_short_is_failed_not_reached(rig):
    rig.firmware.short = True
    rig.servos[0].close()
    with pytest.raises(RuntimeError, match='stopped before reaching'):
        _ = rig.servos[0].stopped
    with pytest.raises(RuntimeError):
        _ = rig.servos[0].stopped
    assert rig.servos[0].angle is None
    assert not rig.servos[0].isClosed()


def test_timeout_never_dispatches_chute(rig, monkeypatch):
    rig.firmware.hold = True
    clock = [0.0]
    monkeypatch.setattr(hardware.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(hardware.time, 'sleep', lambda _: clock.__setitem__(0, clock[0] + 1))
    with pytest.raises(hardware.HTTPException) as error:
        select(0)
    assert 'Timed out' in error.value.detail
    rig.chute.moveToBin.assert_not_called()


@pytest.mark.parametrize('failure', ['io', 'uncalibrated', 'running', 'homing', 'chute_busy'])
def test_unavailable_or_owned_hardware_does_not_receive_commands(rig, failure):
    if failure == 'io': rig.firmware.io_error = True
    if failure == 'uncalibrated': rig.servos[1].set_preset_angles(None, None)
    if failure == 'running': rig.controller.state = SorterLifecycle.RUNNING
    if failure == 'homing': shared_state.hardware_state = 'homing'
    if failure == 'chute_busy': rig.chute.stepper.stopped = False
    with pytest.raises(hardware.HTTPException):
        select(0)
    assert not rig.firmware.moves
    rig.chute.moveToBin.assert_not_called()


def test_unknown_start_and_stop_do_not_claim_a_position(rig):
    servo = rig.servos[0]
    assert servo.angle is None
    assert servo.isOpen() is None and servo.isClosed() is None
    assert servo.feedback()['state'] == 'unknown'
    servo.close()
    servo.stop()
    assert servo.angle is None
    assert servo.feedback()['state'] == 'failed'


def test_pending_flap_is_not_persisted_as_closed(rig, monkeypatch):
    import irl.config as config
    saved = Mock()
    monkeypatch.setattr(config, 'set_servo_states', saved)
    rig.firmware.hold = True
    rig.servos[0].open()
    config.save_servo_states(rig.servos, rig.gc)
    saved.assert_called_once_with({})


def test_direct_servo_move_also_propagates_rejection(rig, monkeypatch):
    monkeypatch.setattr(hardware, '_read_machine_params_config', lambda: ('unused', {}))
    rig.firmware.reject = 0
    with pytest.raises(hardware.HTTPException) as error:
        hardware.move_to_layer_servo(0, hardware.ServoLayerMovePayload(angle=105))
    assert error.value.status_code == 500
    assert 'rejected or busy' in error.value.detail


def test_release_guarantee_survives_same_target_after_a_jog(rig):
    servo = rig.servos[0]
    rig.firmware.hold = True
    assert servo.move_to(105)
    with pytest.raises(RuntimeError, match='busy'):
        servo.close()
    assert len(rig.firmware.moves) == 1
    rig.firmware.hold = False
    assert servo.isClosed()
    servo.close()
    assert not servo.enabled
    assert len(rig.firmware.moves) == 1


def test_new_release_command_preserves_hard_firmware_deadline(rig):
    rig.servos[0].close()
    _, _, command, payload = rig.firmware.moves[-1]
    assert command == C.SERVO_MOVE_TO_AND_RELEASE
    assert struct.unpack('<HH', payload) == (1050, 3500)


def test_manual_same_layer_releases_prior_jog_without_cycling(rig):
    select(0)
    assert rig.servos[0].move_to(105)
    assert rig.servos[0].isClosed()
    before = len(rig.firmware.moves)
    select(0)
    assert not rig.servos[0].enabled
    assert len(rig.firmware.moves) == before


def test_stale_firmware_position_invalidates_settled_estimate(rig):
    select(0)
    rig.firmware.positions[0] = 700
    assert rig.servos[0].angle is None
    assert not rig.servos[0].isClosed()
    before = len(rig.firmware.moves)
    select(0)
    assert len(rig.firmware.moves) == before + 1


@pytest.mark.parametrize('layer,section,bin_', [(3,0,0), (-1,0,0), (0,-1,0), (0,6,0), (0,0,-1), (0,0,3)])
def test_invalid_coordinates_never_move_flaps(rig, layer, section, bin_):
    with pytest.raises(hardware.HTTPException) as error:
        select(layer, section, bin_)
    assert error.value.status_code == 400
    assert not rig.firmware.moves


def test_concurrent_request_is_rejected_without_dispatch(rig):
    entered, release = threading.Event(), threading.Event()
    def owner():
        with shared_state.hardware_lifecycle_lock:
            entered.set()
            assert release.wait(3)
    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert entered.wait(3)
        with pytest.raises(hardware.HTTPException) as error:
            select(0)
        assert error.value.status_code == 409
        assert not rig.firmware.moves
    finally:
        release.set()
        thread.join(3)


def automatic(rig, layer):
    state = Positioning(rig.controller.irl, rig.gc, NS(set_chute_motion=Mock()),
                        rig.chute, NS(layers=[]), Mock(), queue.Queue())
    state._target_address = BinAddress(layer, 0, 0)
    state._raiseChuteJamAlert = Mock()
    return state


@pytest.mark.parametrize('layer', [0, 1, 2])
def test_automatic_uses_same_driver_and_deduplicates_settled_moves(rig, layer):
    state = automatic(rig, layer)
    assert state._startDoorAndChutePositioning(layer)
    assert not state._movingDoorServoIndices()
    assert [s.isClosed() for s in rig.servos] == [i == layer for i in range(3)]
    before = list(rig.firmware.moves)
    assert state._startDoorAndChutePositioning(layer)
    assert rig.firmware.moves == before


def test_automatic_passage_failure_is_not_success(rig):
    rig.firmware.reject = 0
    state = automatic(rig, 1)
    assert not state._startDoorAndChutePositioning(1)
    assert state._raiseChuteJamAlert.called


def test_automatic_failed_completion_remains_blocked(rig):
    state = automatic(rig, 0)
    rig.firmware.short = True
    assert state._startDoorAndChutePositioning(0)
    assert 0 in state._movingDoorServoIndices()
    assert 0 in state._movingDoorServoIndices()
    assert state._raiseChuteJamAlert.called


def test_active_layer_reports_intercepting_flap_and_no_layer_for_failure(rig, monkeypatch):
    layout = BinLayoutConfig(layers=[LayerConfig(sections=[['medium']*3]*6) for _ in range(3)])
    monkeypatch.setattr(hardware, 'getBinLayout', lambda: layout)
    monkeypatch.setattr(hardware, '_read_machine_params_config', lambda: ('unused', {}))
    monkeypatch.setattr(hardware, 'getBinCategories', lambda: None)
    monkeypatch.setattr(hardware, '_current_not_in_inventory', lambda: [[[False]*3]*6]*3)
    for layer in range(3):
        select(layer)
        assert hardware.get_bins_layout()['active_layer'] == layer
    rig.servos[0].stop()
    assert hardware.get_bins_layout()['active_layer'] is None


def test_resume_waits_for_manual_ownership(rig):
    controller = object.__new__(SorterController)
    controller.irl = NS(enableSteppers=Mock())
    controller.coordinator = NS(resume=Mock())
    controller.gc = NS(runtime_stats=Mock(), run_recorder=Mock(), lifetime_stats=Mock())
    controller.vision = NS()
    started = threading.Event()
    def resume():
        started.set()
        controller.resume()
    with shared_state.hardware_lifecycle_lock:
        thread = threading.Thread(target=resume)
        thread.start()
        assert started.wait(3)
        controller.irl.enableSteppers.assert_not_called()
    thread.join(3)
    assert not thread.is_alive()
    controller.irl.enableSteppers.assert_called_once()
