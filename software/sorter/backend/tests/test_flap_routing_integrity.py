"""Routing ownership and release gates using the production PCA/state/API paths."""

import queue
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coordinator import Coordinator
from defs.known_object import KnownObject
from defs.sorter_controller import SorterLifecycle
from piece_transport import ClassificationChannelTransport
from runtime_stats import RuntimeStatsCollector
from server import shared_state
from server.routers import hardware, system
from subsystems.distribution.chute import BinAddress
from subsystems.distribution.state_machine import DistributionStateMachine
from subsystems.distribution.states import DistributionState
from subsystems.shared_variables import SharedVariables
from test_distribution_sending import _Profiler
from test_flap_control import rig, select
from test_indexed_pocket_pipeline import _pipeline


@pytest.fixture
def route(rig, monkeypatch):
    select(0)  # Known initial state: the upstream flap really intercepts.
    rig.gc.runtime_stats = RuntimeStatsCollector()
    rig.gc.profiler = _Profiler()
    rig.chute.stepper.degrees_for_microsteps = lambda n: n * 0.225
    shared = SharedVariables()
    shared.transport = ClassificationChannelTransport()
    piece = KnownObject(part_id="3001", color_id="1")
    shared.transport.placePieceForDistribution(piece)
    profile = Mock()
    profile.getCategoryIdForPart.return_value = "test-category"
    profile.highValueCategoryId.return_value = None
    machine = DistributionStateMachine(
        rig.controller.irl, rig.gc, shared, profile,
        NS(layers=[NS(enabled=True)] * 3), queue.Queue(),
    )
    positioning = machine.states_map[DistributionState.POSITIONING]
    # Assignment/provider work is outside this slice; all flap and lifecycle
    # transitions below are production methods, with inert MCU feedback.
    positioning._reserveHarvestRoute = Mock(return_value=None)
    positioning._findOrAssignBinForCategory = Mock(return_value=(BinAddress(2, 0, 0), False))
    coordinator = object.__new__(Coordinator)
    coordinator.distribution = machine
    coordinator.shared = shared
    coordinator.classification = NS(supportsStatefulPause=lambda: True, pause=Mock(), resume=Mock())
    coordinator.feeder = NS(hold_motion=Mock())
    rig.controller.coordinator = coordinator
    rig.controller.gc = rig.gc
    rig.shared, rig.piece, rig.machine, rig.positioning = shared, piece, machine, positioning
    monkeypatch.setattr(shared_state, "hardware_error", None)
    monkeypatch.setattr(shared_state, "command_queue", queue.Queue())
    return rig


def prepare(route, mode="normal"):
    if mode == "sample":
        route.shared.sample_collection_mode = True
    elif mode == "oversize":
        route.piece.too_big = True
    elif mode == "no_bin":
        route.machine.sorting_profile.getCategoryIdForPart.return_value = "misc"
        route.positioning._findOrAssignBinForCategory.return_value = (None, False)
    elif mode == "layer_oversize":
        route.piece.max_dimension_mm = 100
        route.machine.layout.layers[2].max_dimension_mm = 50
    route.machine.step()  # IDLE -> POSITIONING closes the gate.
    route.machine.step()  # Request the complete route.


def due_can_release(route):
    pipeline = _pipeline()
    pipeline.shared = route.shared
    pipeline.transport = route.shared.transport
    worker = NS(ctx=NS(known_object=route.piece, classification_applied=True))
    load = pipeline._ledger.admit(worker, admitted_at_mono=1.0)
    load.distribution_placed = True
    return pipeline._prepareDueLoad(load)


@pytest.mark.parametrize("phase", [DistributionState.POSITIONING, DistributionState.READY])
@pytest.mark.parametrize("manual", ["bin", "servo", "section", "sample"])
def test_retained_transaction_blocks_manual_commands_and_resumes_original_route(route, phase, manual):
    prepare(route)
    route.positioning._moving_started_at -= 1
    if phase == DistributionState.READY:
        route.machine.step()
        route.machine.step()
        assert due_can_release(route)
    assert route.machine.current_state == phase
    route.controller.coordinator.pause()
    before = list(route.firmware.moves)
    calls = {
        "bin": lambda: select(0),
        "servo": lambda: hardware.move_to_layer_servo(2, hardware.ServoLayerMovePayload(angle=56)),
        "section": lambda: hardware.move_to_section(hardware.MoveToSectionPayload(section_index=1)),
        "sample": lambda: system.set_sample_collection_mode({"enabled": True}),
    }
    with pytest.raises(hardware.HTTPException) as error:
        calls[manual]()
    assert error.value.status_code == 409 and "retained" in error.value.detail
    assert route.firmware.moves == before
    assert route.piece.destination_bin == (2, 0, 0)
    route.controller.coordinator.resume()
    route.machine.step()
    route.machine.step()
    assert route.machine.current_state == DistributionState.READY
    assert due_can_release(route)
    assert [s.isClosed() for s in route.servos] == [False, False, True]
    assert route.firmware.moves == before  # No same-route cycle on resume.


@pytest.mark.parametrize("mode", ["sample", "oversize", "no_bin", "layer_oversize"])
def test_passthrough_rejection_holds_release_and_surfaces_existing_incident(route, mode):
    route.firmware.reject = 0
    prepare(route, mode)
    assert not due_can_release(route)
    assert route.machine.current_state == DistributionState.POSITIONING
    assert route.gc.runtime_stats.activeIncident()["kind"] == "distribution_chute_jam"
    assert shared_state.command_queue.get_nowait().tag == "pause"
    assert route.firmware.positions[0] == 1050
    assert route.servos[0].feedback()["state"] == "failed"


@pytest.mark.parametrize("mode", ["sample", "oversize", "no_bin", "layer_oversize"])
def test_passthrough_waits_for_all_flaps_before_c4_release(route, mode):
    route.firmware.hold = True
    prepare(route, mode)
    original_moves = list(route.firmware.moves)
    for _ in range(3):
        route.machine.step()
        assert route.machine.current_state == DistributionState.POSITIONING
        assert not due_can_release(route)
        assert route.piece.distribution_positioned_at is None
    assert route.firmware.moves == original_moves
    route.firmware.hold = False
    route.machine.step()
    assert route.machine.current_state == DistributionState.READY
    route.machine.step()
    assert due_can_release(route)
    assert all(s.isOpen() for s in route.servos)
    assert route.piece.destination_bin is None
    assert route.piece.distribution_positioned_at is not None
    assert route.firmware.moves == original_moves


@pytest.mark.parametrize("failure", ["uncalibrated", "offline", "rejected", "short", "unknown"])
def test_normal_route_cannot_skip_or_accept_failed_upstream_passage(route, failure):
    if failure == "uncalibrated":
        route.servos[0].set_preset_angles(None, None)
    elif failure == "offline":
        route.servos[0] = NS(available=False)
    elif failure == "rejected":
        route.firmware.reject = 0
    elif failure == "short":
        route.firmware.short = True
    prepare(route)
    if failure == "unknown":
        # Firmware profile drift makes the previously accepted route unknown.
        assert route.servos[0].isOpen()
        route.firmware.positions[0] = 700
    route.positioning._moving_started_at -= 1
    route.machine.step()
    assert not due_can_release(route)
    assert route.gc.runtime_stats.activeIncident()["kind"] == "distribution_chute_jam"
    assert route.shared.transport.getPieceForDistributionPositioning() is route.piece


@pytest.mark.parametrize("failure", ["pending", "wrong", "unknown"])
def test_ready_rechecks_full_path_after_pause_or_feedback_change(route, failure):
    prepare(route)
    route.positioning._moving_started_at -= 1
    route.machine.step()
    route.machine.step()
    assert due_can_release(route)
    if failure == "pending":
        route.firmware.hold = True
        route.servos[0].close()
    elif failure == "wrong":
        route.servos[0].close()
        assert route.servos[0].isClosed()
    else:
        route.firmware.positions[0] = 700
    route.machine.step()
    assert not due_can_release(route)
    assert route.shared.transport.getPieceForDistributionPositioning() is route.piece


@pytest.mark.parametrize("path,body", [
    ("/api/hardware-config/servo/layers/0/move-to", {"angle": 39}),
    ("/api/hardware-config/servo/layers/0/nudge", {"degrees": 2}),
    ("/api/hardware-config/servo/layers/0/toggle", {}),
    ("/api/hardware-config/servo/layers/0/preview", {"is_open": True, "invert": False}),
    ("/api/hardware-config/servo/layers/0/calibrate", {}),
    ("/api/hardware-config/waveshare/servos/1/move", {"position": "open"}),
    ("/api/hardware-config/waveshare/servos/1/nudge", {"degrees": 2}),
    ("/api/hardware-config/waveshare/servos/1/calibrate", {}),
])
def test_direct_http_controls_reject_running_sorter_without_commands(rig, path, body):
    rig.controller.state = SorterLifecycle.RUNNING
    app = FastAPI()
    app.include_router(hardware.router)
    with TestClient(app) as client:
        response = client.post(path, json=body)
    assert response.status_code == 409, response.text
    assert not rig.firmware.moves


def test_direct_control_cannot_change_flaps_under_successful_bin_response(rig):
    entered, release = threading.Event(), threading.Event()
    result = {}
    def chute_move(address):
        entered.set()
        assert release.wait(3)
        return 250
    rig.chute.moveToBin.side_effect = chute_move
    def bin_request():
        result["bin"] = select(0)
    thread = threading.Thread(target=bin_request)
    thread.start()
    try:
        assert entered.wait(3)
        before = list(rig.firmware.moves)
        with pytest.raises(hardware.HTTPException) as error:
            hardware.move_to_layer_servo(0, hardware.ServoLayerMovePayload(angle=39))
        assert error.value.status_code == 409
        assert rig.firmware.moves == before
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert result["bin"]["flap_state"] == "settled"
    assert [s.isClosed() for s in rig.servos] == [True, False, False]


def test_idle_with_transport_owned_piece_still_rejects_manual_control(route):
    assert route.machine.current_state == DistributionState.IDLE
    with pytest.raises(hardware.HTTPException, match="retained"):
        select(1)


@pytest.mark.parametrize("mode", ["normal", "sample"])
@pytest.mark.parametrize("last_layer", [0, 1, 2])
def test_release_waits_for_each_individual_required_flap(route, mode, last_layer):
    for index, servo in enumerate(route.servos):
        (servo.open if mode == "normal" and index == 2 else servo.close)()
        assert servo.stopped
    route.firmware.hold = True
    prepare(route, mode)
    route.positioning._moving_started_at -= 1
    assert all(target is not None for target in route.firmware.targets)
    for index in range(3):
        if index != last_layer:
            route.firmware.positions[index] = route.firmware.targets[index]
            route.firmware.targets[index] = None
    route.machine.step()
    assert not due_can_release(route)
    assert route.machine.current_state == DistributionState.POSITIONING
    route.firmware.hold = False
    route.machine.step()
    route.machine.step()
    assert due_can_release(route)


@pytest.mark.parametrize("mode", ["normal", "sample"])
def test_explicit_resume_retries_failed_completion_for_same_retained_transaction(route, mode):
    route.firmware.short = True
    prepare(route, mode)
    route.positioning._moving_started_at -= 1
    route.machine.step()
    assert not due_can_release(route)
    assert route.gc.runtime_stats.activeIncident()["kind"] == "distribution_chute_jam"
    before = list(route.firmware.moves)
    route.controller.coordinator.pause()
    # Existing operator incident-clear + Resume, with the simulated fault removed.
    route.firmware.short = False
    route.gc.runtime_stats.clearActiveIncident()
    shared_state.hardware_error = None
    route.controller.coordinator.resume()
    route.machine.step()
    route.machine.step()
    assert due_can_release(route)
    assert route.shared.transport.getPieceForDistributionPositioning() is route.piece
    assert route.piece.destination_bin == ((2, 0, 0) if mode == "normal" else None)
    assert len(route.firmware.moves) > len(before)
    if mode == "normal":
        route.positioning._reserveHarvestRoute.assert_called_once()
