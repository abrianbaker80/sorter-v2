"""Native runtime boundaries with actual SQLite, fake actuators and controlled state."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import ast
import queue
import sqlite3
import time

import pytest
from fastapi import HTTPException

import smart_bins_native_custody as native
import smart_bins_native_completion as completion
from subsystems.classification_channel.smart_bins_native_adapter import (
    NativeCustodyAdapter,
)
from subsystems.distribution.positioning import Positioning
from subsystems.distribution.ready import Ready
from subsystems.distribution.states import DistributionState
from subsystems.classification_channel.two_piece import spoke_home
from subsystems.classification_channel.two_piece.channel_clear import (
    ChannelClearResult,
    clearChannelByAdvancing,
)
from subsystems.classification_channel.two_piece.base import C4FiveSectorPlatter
from subsystems.classification_channel.two_piece.flow import _Phase
from subsystems.feeder.pulse_perception.flow import PulsePerceptionFeeding
from hardware.sorter_interface import StepperMotor, FiniteMoveReceipt
from defs.known_object import KnownObject
from irl.bin_layout import DistributionLayout, Layer, BinSection, Bin, BinSize
from piece_transport import ClassificationChannelTransport
from profiler import Profiler
from subsystems.shared_variables import SharedVariables
from test_smart_bins_reservations import prepared as prepared, connect
from test_native_motion_truth import pca, Device
from test_c4_distribution_handoff import _mkGc, _mkChannel, _addPiece


@pytest.fixture(autouse=True)
def reset_fence():
    native.install(None)
    yield
    native.install(None)


@pytest.fixture
def world(prepared):
    native.initialize_native_schema("m", "p", "sorter", 0)
    completion.initialize_schema()
    gc = _mkGc()
    gc.machine_id = "m"
    gc.disable_chute = False
    adapter = NativeCustodyAdapter("m")
    native.install(adapter)
    gc.smart_bins_native_adapter = adapter
    shared = SharedVariables()
    shared.native_custody = adapter
    transport = ClassificationChannelTransport()
    transport.native_adapter = adapter
    shared.transport = transport
    shared.set_distribution_gate(False)
    piece = KnownObject(part_id="3001", color_id="1")
    transport.placePieceForDistribution(piece)
    layout = DistributionLayout(
        [
            Layer(
                [BinSection([Bin(BinSize.MEDIUM), Bin(BinSize.MEDIUM)])],
                max_pieces_per_bin=2,
            )
        ]
    )
    servo, servo_device = pca()
    servo_device.position = 0
    trace = []

    def move_bin(address):
        with connect(prepared) as conn:
            row = conn.execute("SELECT state FROM smart_bin_reservations").fetchone()
            assert row is not None and row[0] == "RESERVED"
        trace.append("route_after_reservation")
        chute.stepper.last_finite_receipt = FiniteMoveReceipt(1, 100, "ACCEPTED")
        return 1

    chute = SimpleNamespace(
        isBinReachable=lambda a: True,
        moveToBin=move_bin,
        homed=True,
        current_angle=0.0,
        getAngleForBin=lambda a: 0.0,
        stepper=SimpleNamespace(stopped=True, last_finite_receipt=None),
    )
    irl = SimpleNamespace(
        servos=[servo],
        distribution_layout=layout,
        chute=chute,
        carousel_stepper=SimpleNamespace(stopped=True),
        c_channel_3_rotor_stepper=SimpleNamespace(stopped=True),
    )
    profile = SimpleNamespace(
        artifact_hash="artifact",
        getCategoryIdForPart=lambda *a: "A",
        highValueCategoryId=lambda p: None,
    )
    positioning = Positioning(irl, gc, shared, chute, layout, profile, queue.Queue())
    ready = Ready(irl, gc, shared)
    return SimpleNamespace(
        path=prepared,
        gc=gc,
        adapter=adapter,
        shared=shared,
        transport=transport,
        piece=piece,
        positioning=positioning,
        ready=ready,
        irl=irl,
        trace=trace,
        servo_device=servo_device,
    )


def positioned(world):
    assert world.positioning.step() is None
    assert world.trace == ["route_after_reservation"]
    assert world.positioning.step() == DistributionState.READY
    assert not world.shared.distribution_ready


def armed(world):
    positioned(world)
    assert world.ready.step() is None
    assert world.shared.distribution_ready


def channel(world, monkeypatch):
    ch = _mkChannel(world.transport, world.shared)
    ch.gc = world.gc
    device = Device()
    stepper = StepperMotor(device, 0, world.gc)
    stepper.set_name("carousel")
    ch.irl = SimpleNamespace(carousel_stepper=stepper)
    ch.ctx.config = SimpleNamespace(
        discharge_center_tolerance_deg=1,
        discharge_max_move_output_deg=10,
        discharge_speed_usteps_per_s=1000,
        precise_converge_speed_usteps_per_s=1000,
        precise_center_tolerance_deg=1,
    )
    ch._eject_target = _addPiece(ch, 7, gap_to_exit=2, obj=world.piece, placed=True)
    ch._phase = _Phase.EJECTING
    monkeypatch.setattr(
        C4FiveSectorPlatter,
        "from_irl_config",
        lambda _: SimpleNamespace(output_degrees_to_motor_microsteps=lambda deg: 100),
    )
    return ch, device


def test_reservation_precedes_route_and_intent_precedes_ready(world, monkeypatch):
    positioned(world)
    original = world.shared.set_distribution_gate

    def gate(value, **kwargs):
        if value:
            with connect(world.path) as conn:
                assert (
                    conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[
                        0
                    ]
                    == "RELEASE_INTENT"
                )
        original(value, **kwargs)

    monkeypatch.setattr(world.shared, "set_distribution_gate", gate)
    world.ready.step()
    assert world.shared.distribution_ready
    assert world.shared.distribution_positioned_uuid == world.piece.uuid


@pytest.mark.parametrize("enabled", [False, True])
def test_native_timing_uses_bounded_existing_profiler(world, monkeypatch, enabled):
    profiler = Profiler(enabled)
    world.adapter.profiler = profiler
    armed(world)
    ch, dev = channel(world, monkeypatch)
    dev.outcome = b"\x01"
    assert ch.startOutputMove(10, 1000)
    names = {row["metric_name"] for row in profiler.snapshotRows()}
    expected = {
        "smart_bins.reserve_ms",
        "smart_bins.release_intent_ms",
        "smart_bins.dispatch_consume_ms",
        "smart_bins.current_authorization_ms",
    }
    assert names == (expected if enabled else set())


def test_commit_failure_keeps_ready_closed(world, monkeypatch):
    positioned(world)
    real = native.critical_transaction

    class Fail:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, key):
            return getattr(self.conn, key)

        def commit(self):
            raise sqlite3.OperationalError("commit failed")

    @contextmanager
    def failing():
        with real() as conn:
            yield Fail(conn)

    monkeypatch.setattr(native, "critical_transaction", failing)
    world.ready.step()
    assert not world.shared.distribution_ready
    assert world.adapter.permit is None


@pytest.mark.parametrize("outcome", [b"\x01", b"\x00", "ambiguous"])
def test_exactly_one_native_dispatch(world, monkeypatch, outcome):
    armed(world)
    ch, dev = channel(world, monkeypatch)
    if outcome == "ambiguous":
        dev.error = OSError("write completed; response lost")
    else:
        dev.outcome = outcome
    expected = outcome == b"\x01"
    assert ch.startOutputMove(10, 1000) is expected
    assert len(dev.moves) == 1
    assert not ch.startOutputMove(10, 1000)
    assert len(dev.moves) == 1
    with connect(world.path) as conn:
        kinds = [
            r[0] for r in conn.execute("SELECT kind FROM smart_bin_native_evidence")
        ]
        assert (
            "MOTOR_ACCEPTED"
            if expected
            else "MOTOR_REJECTED"
            if outcome == b"\x00"
            else "MOTOR_AMBIGUOUS"
        ) in kinds
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 0
        )


def test_fresh_absence_is_inferred_and_preserves_drop_identity(world, monkeypatch):
    armed(world)
    ch, dev = channel(world, monkeypatch)
    assert ch.startOutputMove(10, 1000)
    ch._native_release_observed_after = time.time() - 1
    ch._eject_target.last_seen = time.monotonic() - 1
    ch._ejecting(SimpleNamespace(ts=time.time(), pieces=()), True, time.monotonic())
    assert world.transport.getPieceForDistributionDrop() is world.piece
    with connect(world.path) as conn:
        kinds = [
            r[0] for r in conn.execute("SELECT kind FROM smart_bin_native_evidence")
        ]
        assert "INFERRED_EXIT" in kinds and "SOFTWARE_SLOT_PROMOTION" in kinds
        assert (
            conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[0]
            == "RELEASE_INTENT"
        )
    assert len(dev.moves) == 1


def test_timeout_visible_retains_uncertainty_and_stops_staging(world, monkeypatch):
    armed(world)
    ch, dev = channel(world, monkeypatch)
    assert ch.startOutputMove(10, 1000)
    now = time.monotonic()
    ch._phase_started_at = now - 16
    ch._eject_target.last_seen = now
    ch._ejecting(
        SimpleNamespace(ts=time.time(), pieces=(SimpleNamespace(sv_bt_track_id=7),)),
        True,
        now,
    )
    assert not ch.startOutputMove(10, 1000)
    assert len(dev.moves) == 1
    with connect(world.path) as conn:
        assert (
            conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[0]
            == "UNCERTAIN"
        )
        assert conn.execute(
            "SELECT 1 FROM smart_bin_native_evidence WHERE kind='EJECT_TIMEOUT_VISIBLE'"
        ).fetchone()


def test_lost_placed_piece_withdraws_without_cancellation(world):
    positioned(world)
    assert world.transport.clearPieceForDistribution(world.piece)
    assert world.transport.getPieceForDistributionPositioning() is None
    with connect(world.path) as conn:
        assert (
            conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[0]
            == "UNCERTAIN"
        )


def test_stall_snapshot_failure_prevents_promotion_and_motion(world, monkeypatch):
    armed(world)
    ch, dev = channel(world, monkeypatch)
    monkeypatch.setattr(
        native, "hold", Mock(side_effect=sqlite3.OperationalError("disk full"))
    )
    result = ch.attemptStallAutoClear(max_output_deg=720)
    assert not result.cleared
    assert world.transport.getPieceForDistributionPositioning() is world.piece
    assert not dev.moves


def test_initial_staging_unqualified_material_is_held(world, monkeypatch):
    ch, dev = channel(world, monkeypatch)
    ch._eject_target = None
    assert not ch.startOutputMove(10, 1000)
    assert not dev.moves
    assert world.adapter.blocked


def test_multidrop_hold_preserved_no_quantity_invention(world):
    world.shared.bucket_passthrough_hold = True
    assert world.positioning.step() is None
    assert world.shared.bucket_passthrough_hold
    assert not world.trace
    with connect(world.path) as conn:
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_reservations").fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_native_holds").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("cleared", [False, None])
def test_failed_or_unknown_purge_prevents_alignment(monkeypatch, cleared):
    result = ChannelClearResult(False, True, 0, "failed") if cleared is False else None
    monkeypatch.setattr(spoke_home, "clearPiecesFromChannel", lambda *a: result)
    vision = Mock()
    stepper = Mock()
    assert not spoke_home.maybeRunSpokeHome(
        SimpleNamespace(logger=Mock()),
        SimpleNamespace(carousel_stepper=stepper),
        None,
        vision,
    )
    vision.getCaptureThreadForRole.assert_not_called()
    stepper.move_steps.assert_not_called()


def test_startup_held_claim_prevents_doors_and_sweep(world):
    positioned(world)
    servos = [Mock()]
    stepper = Mock()
    world.gc.perception_service = SimpleNamespace(
        read_state=lambda _: SimpleNamespace(n_pieces=1)
    )
    result = clearChannelByAdvancing(
        world.gc, SimpleNamespace(servos=servos, carousel_stepper=stepper), None
    )
    assert not result.cleared
    servos[0].open.assert_not_called()
    stepper.move_steps.assert_not_called()


def test_main_checks_before_discovery_and_server_start():
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    main = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    for name in ("_home_hardware", "_initialize_hardware"):
        fn = next(
            n for n in main.body if isinstance(n, ast.FunctionDef) and n.name == name
        )
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        inspect = next(
            n
            for n in calls
            if isinstance(n.func, ast.Attribute) and n.func.attr == "require_startup"
        )
        discovery = next(
            n
            for n in calls
            if isinstance(n.func, ast.Name) and n.func.id == "mkIRLInterface"
        )
        assert inspect.lineno < discovery.lineno


def test_manual_refusal_is_409_before_driver(world):
    positioned(world)
    from server.routers import steppers, hardware

    for check in (steppers._ensure_manual_motion_allowed, hardware._ensure_not_homing):
        with pytest.raises(HTTPException) as raised:
            check("move")
        assert raised.value.status_code == 409
    with native.motion_entry("finite", "c_channel_1_rotor"):
        pass
    assert not world.adapter.admission_allowed()


def test_c3_admission_refused_before_speed_or_move(world):
    positioned(world)
    feeder = object.__new__(PulsePerceptionFeeding)
    feeder.shared = world.shared
    feeder._busy = lambda _: False
    motor = Mock()
    assert not feeder._move("C3", 3, motor, 10, 0, None)
    motor.set_speed_limits.assert_not_called()
    motor.move_degrees.assert_not_called()


def test_route_configuration_is_frozen_before_endpoint_side_effects(world):
    positioned(world)
    from server.routers import hardware, steppers

    for endpoint in (
        hardware.save_servo_hardware_config,
        hardware.save_chute_aiming_config,
        hardware.save_storage_layer_hardware_config,
        hardware.set_section_enabled,
    ):
        with pytest.raises(HTTPException) as raised:
            endpoint(None)
        assert raised.value.status_code == 409
    with pytest.raises(HTTPException) as raised:
        steppers.set_tmc_settings("carousel", None)
    assert raised.value.status_code == 409


def test_native_ambiguity_is_one_wire_write_through_rb01(world, monkeypatch):
    from test_hardware_bus import _Serial, _bus
    from hardware.sorter_interface import InterfaceCommandCode

    armed(world)
    ch, device = channel(world, monkeypatch)
    port = _Serial()
    bus = _bus(port)
    original = device.send_command

    def send(command, channel, payload):
        if command == InterfaceCommandCode.STEPPER_MOVE_STEPS:
            return bus.send_command(0, command, channel, payload, retries=99)
        return original(command, channel, payload)

    device.send_command = send
    assert not ch.startOutputMove(10, 1000)
    assert len(port.writes) == 1
    assert not ch.startOutputMove(10, 1000)
    assert len(port.writes) == 1
    assert bus._unresolved_addresses == {0}
    with connect(world.path) as conn:
        assert (
            conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[0]
            == "UNCERTAIN"
        )


def test_manual_move_already_running_prevents_reservation(world):
    world.irl.carousel_stepper.stopped = False
    world.positioning.step()
    assert not world.trace
    with connect(world.path) as conn:
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_reservations").fetchone()[0]
            == 0
        )


def test_observed_channel_clear_is_not_receiving_evidence(world):
    world.gc.perception_service = SimpleNamespace(
        read_state=lambda _: SimpleNamespace(n_pieces=0, ts=time.time())
    )
    result = clearChannelByAdvancing(world.gc, world.irl, None)
    assert result.cleared and result.output_deg_moved == 0
    with connect(world.path) as conn:
        row = conn.execute(
            "SELECT details_json FROM smart_bin_native_evidence WHERE kind='CHANNEL_CLEAR_OBSERVED'"
        ).fetchone()
        assert row and '"actual_destination": null' in row[0]
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 0
        )


def test_stale_channel_empty_does_not_authorize_startup_purge(world):
    world.gc.perception_service = SimpleNamespace(
        read_state=lambda _: SimpleNamespace(n_pieces=0, ts=time.time() - 10)
    )
    result = clearChannelByAdvancing(world.gc, world.irl, None)
    assert not result.cleared and result.output_deg_moved == 0
