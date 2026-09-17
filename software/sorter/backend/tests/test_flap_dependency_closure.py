"""Resume through the production command/controller path, including on HEAD + payload."""
import os
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from message_queue.handler import handleServerToMainEvent
from server import shared_state
from server.routers import steppers
from server.routers import hardware
from sorter_controller import SorterController
from subsystems.bus import TickBus
from subsystems.distribution.states import DistributionState
from test_flap_control import rig
from test_flap_routing_integrity import route, prepare, due_can_release
from test_indexed_pocket_pipeline import _pipeline


@pytest.fixture
def production(route, monkeypatch):
    coordinator = route.controller.coordinator
    coordinator.gc = route.gc
    coordinator.logger = route.gc.logger
    coordinator.bus = TickBus()
    coordinator.manual_feed_mode = True
    pipeline = _pipeline()
    pipeline.shared = route.shared
    pipeline.transport = route.shared.transport
    coordinator.classification.pause = pipeline.pause
    coordinator.classification.resume = Mock(wraps=pipeline.resume)
    coordinator.classification.step = Mock()
    route.gc.run_recorder = Mock()
    route.gc.lifetime_stats = Mock()
    controller = object.__new__(SorterController)
    controller.irl = route.controller.irl
    controller.irl.enableSteppers = Mock()
    controller.gc = route.gc
    controller.coordinator = coordinator
    controller.vision = NS()
    controller.state = route.controller.state
    monkeypatch.setattr(shared_state, 'controller_ref', controller)
    monkeypatch.setattr(shared_state, 'gc_ref', route.gc)
    route.controller = controller
    route.pipeline = pipeline
    return route


def resume_via_api(r):
    # Endpoint queues only; the real main-thread handler dispatches the command.
    assert steppers.resume().success
    event = shared_state.command_queue.get_nowait()
    assert event.tag == 'resume'
    handleServerToMainEvent(r.gc, r.controller, event)


def tick(r, count=1):
    for _ in range(count):
        r.controller.step()


def pause_at(r, phase, mode):
    prepare(r, mode)
    r.positioning._moving_started_at -= 1
    if phase == 'ready':
        r.machine.step()
        r.machine.step()
        assert due_can_release(r)
    r.controller.pause()
    assert r.pipeline._paused_at_mono is not None


@pytest.mark.parametrize('phase', ['ready', 'positioning'])
@pytest.mark.parametrize('mode', ['normal', 'sample'])
def test_isolated_payload_production_resume_waits_for_complete_original_route(production, phase, mode):
    r = production
    pause_at(r, phase, mode)
    destination = r.piece.destination_bin
    # Simulate lost profile proof while paused. Only Resume may restore it.
    r.firmware.positions = [700, 700, 700]
    r.firmware.hold = True
    resume_via_api(r)
    assert r.machine.current_state == DistributionState.POSITIONING
    tick(r, 3)
    assert not due_can_release(r)
    assert r.pipeline._paused_at_mono is not None
    r.controller.coordinator.classification.resume.assert_not_called()
    r.controller.coordinator.classification.step.assert_not_called()
    # Completing just two flaps must not release the retained load.
    for index in (0, 2):
        r.firmware.positions[index] = r.firmware.targets[index]
        r.firmware.targets[index] = None
    tick(r)
    assert not due_can_release(r)
    assert r.pipeline._paused_at_mono is not None
    r.firmware.hold = False
    tick(r, 2)
    assert due_can_release(r)
    assert r.pipeline._paused_at_mono is None
    r.controller.coordinator.classification.resume.assert_called_once()
    assert r.piece.destination_bin == destination
    assert r.shared.transport.getPieceForDistributionPositioning() is r.piece
    if mode == 'normal':
        r.positioning._reserveHarvestRoute.assert_called_once()
        r.positioning._findOrAssignBinForCategory.assert_called_once()


@pytest.mark.parametrize('phase', ['ready', 'positioning'])
def test_partial_resume_failure_holds_classification_until_explicit_successful_retry(production, phase):
    r = production
    pause_at(r, phase, 'normal')
    r.firmware.positions = [700, 700, 700]
    r.firmware.reject = 1
    resume_via_api(r)
    tick(r, 3)
    assert not due_can_release(r)
    assert r.gc.runtime_stats.activeIncident()['kind'] == 'distribution_chute_jam'
    assert r.pipeline._paused_at_mono is not None
    r.controller.coordinator.classification.resume.assert_not_called()
    r.controller.coordinator.classification.step.assert_not_called()
    r.controller.pause()
    while not shared_state.command_queue.empty():
        shared_state.command_queue.get_nowait()
    r.gc.runtime_stats.clearActiveIncident()
    shared_state.hardware_error = None
    r.firmware.reject = None
    resume_via_api(r)
    tick(r, 2)
    assert due_can_release(r)
    assert r.piece.destination_bin == (2, 0, 0)
    assert r.pipeline._paused_at_mono is None
    r.controller.coordinator.classification.resume.assert_called_once()


def test_ready_resume_restores_chute_to_original_bin_before_classification(production):
    r = production
    pause_at(r, 'ready', 'normal')
    r.chute.current_angle = 0
    r.chute.moveToBin.reset_mock()
    resume_via_api(r)
    tick(r, 2)
    assert not due_can_release(r)
    assert r.pipeline._paused_at_mono is not None
    r.chute.moveToBin.assert_called_once_with(r.positioning._target_address)
    r.chute.current_angle = 60
    r.positioning._moving_started_at -= 1
    tick(r, 2)
    assert due_can_release(r)
    assert r.piece.destination_bin == (2, 0, 0)
    r.controller.coordinator.classification.resume.assert_called_once()


@pytest.mark.parametrize('phase', [DistributionState.READY, DistributionState.POSITIONING,
                                  DistributionState.SENDING, DistributionState.IDLE])
@pytest.mark.parametrize('control', ['bin', 'direct'])
def test_retained_ownership_rejects_controls_in_isolated_payload(route, phase, control):
    route.machine.current_state = phase
    before = list(route.firmware.moves)
    with pytest.raises(hardware.HTTPException) as error:
        if control == 'bin':
            hardware.move_to_bin(hardware.MoveToBinPayload(layer_index=0, section_index=0, bin_index=0))
        else:
            hardware.move_to_layer_servo(0, hardware.ServoLayerMovePayload(angle=39))
    assert error.value.status_code == 409
    assert route.firmware.moves == before
    assert route.shared.transport.getPieceForDistributionPositioning() is route.piece


def test_no_ambient_source_imports_when_running_isolated_payload():
    # The offline closure runner sets this to the fresh HEAD export, whose
    # omitted files are checked byte-for-byte against Git before pytest runs.
    isolated = os.environ.get('FLAP_ISOLATED_ROOT')
    if isolated:
        import coordinator
        import hardware.sorter_interface
        import subsystems.classification_channel.indexed_pocket_pipeline
        import subsystems.shared_variables
        import runtime_stats
        for module in (coordinator, hardware.sorter_interface,
                       subsystems.classification_channel.indexed_pocket_pipeline,
                       subsystems.shared_variables, runtime_stats):
            assert Path(module.__file__).resolve().is_relative_to(Path(isolated).resolve())
