"""Runtime replay with real FIFO/planner/journeys/marker feedback; no hardware."""

from dataclasses import replace
from fractions import Fraction
import logging
import queue
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from defs.known_object import KnownObject
from recognition_journey import RecognitionResult, Stage, Detection
from subsystems.classification_channel.physical_fifo import PhysicalC4FIFO, PocketState
from subsystems.classification_channel.physical_runtime import PhysicalC4Runtime
from subsystems.classification_channel.demand_planner import ChuteObservation
from subsystems.classification_channel.transfer_episode import TransferEpisode
from subsystems.classification_channel.marker_positioner import PositionError
from test_marker_positioner import Rig
from test_recognition_journey import scene, A, B, BOX, REGION


class Chute:
    def __init__(self):
        self.exits = []
        self.recovery = False
        self.ready = True

    def reject(self, piece, reason):
        piece.part_id = None
        piece.forced_reject_reason = reason

    def observe(self, runtime, now):
        loads = sorted(
            (p for p in runtime.fifo.pockets if p.state is not PocketState.EMPTY),
            key=lambda p: runtime.fifo.station_of(p.pocket_id),
        )
        for p in loads:
            b = runtime.bindings.get((p.pocket_id, p.generation))
            if (
                b
                and b.result_applied
                and b.piece.part_id
                and p.state is PocketState.PENDING
            ):
                runtime.fifo.resolve(
                    *b.key, PocketState.ROUTED, destination=b.piece.part_id
                )
        loads = [runtime.fifo.pockets[p.pocket_id] for p in loads]
        dest = next(
            (
                p.destination if p.state is PocketState.ROUTED else "reject"
                for p in loads
                if p.state in (PocketState.ROUTED, PocketState.DISCARD)
            ),
            "reject",
        )
        return ChuteObservation(dest if self.ready else None, {})

    def clear(self):
        return self.ready

    def begin_recovery(self):
        self.recovery = True

    def reset(self):
        self.recovery = False

    def discharge(self, event, binding):
        self.exits.append((event, binding))


class Bench:
    def __init__(self, gains=()):
        self.rig = Rig(gains=gains)
        self.rig.bind()
        self.chute = Chute()
        self.runtime = PhysicalC4Runtime(
            PhysicalC4FIFO(microsteps_per_revolution=Fraction(52000, 3)),
            self.rig.p,
            speed=500,
            distribution=self.chute,
            apply_result=lambda b, r: setattr(b.piece, "part_id", r.response),
        )

    def reserve(self):
        r = self.runtime
        ep = TransferEpisode(
            r.fifo.boundary,
            r.fifo.intake_pocket_id,
            self.rig.clock(),
            100.0,
            leader_id=42,
        )
        return r.reserve(KnownObject(), ep)

    def admit(self, route=None, arrived=True):
        b = self.reserve()
        self.runtime.finish_handoff(b, arrived=arrived)
        if route:
            self.runtime.accept_result(
                b,
                RecognitionResult(
                    b.piece.uuid, b.episode.episode_id, b.generation, route
                ),
            )
        return b

    def tick(self, **kwargs):
        self.rig.clock.advance()
        self.runtime.tick(self.rig.clock(), **kwargs)

    def advance(self):
        boundary = self.runtime.fifo.boundary
        for _ in range(200):
            self.tick()
            if self.runtime.fifo.boundary > boundary:
                return
        pytest.fail("no marker-confirmed advance")


def test_reservation_precedes_release_and_same_generation_is_finalized():
    b = Bench()
    r = b.runtime
    binding = b.reserve()
    assert binding.key == (0, 1)
    assert dict(r.fifo.pockets[0].metadata)["journey_uuid"] == binding.piece.uuid
    for _ in range(10):
        b.tick()
    assert not b.rig.commands and r.fifo.boundary == 0
    r.finish_handoff(binding, arrived=True)
    assert r.fifo.pockets[0].generation == 1 and binding.admitted
    b.advance()
    assert r.fifo.boundary == 1


def test_retained_c3_piece_keeps_handoff_and_forbids_index():
    b = Bench()
    x = b.reserve()
    assert not b.runtime.finish_handoff(x, arrived=False, retained_on_c3=True)
    for _ in range(20):
        b.tick()
    assert b.runtime.handoff is x and not b.rig.commands


def test_uncertain_arrival_is_discard_and_does_not_wait_for_images():
    b = Bench()
    x = b.admit(arrived=False)
    assert b.runtime.fifo.pockets[x.pocket_id].state is PocketState.DISCARD
    for _ in range(7):
        b.advance()
    assert b.chute.exits[0][1] is x


@pytest.mark.parametrize("gains", [(1,), (0.95, 1), (1.01, 1)])
def test_only_marker_confirmation_advances_fifo_and_corrections_keep_identity(gains):
    b = Bench(gains)
    x = b.admit("bin-a")
    b.tick()
    target = b.runtime.fifo.pending_index
    assert target is not None and b.runtime.fifo.boundary == 0
    while not b.rig.commands:
        b.tick()
    assert b.runtime.fifo.boundary == 0  # motor receipt alone is insufficient
    b.advance()
    assert b.runtime.fifo.pockets[0].generation == x.generation
    assert b.runtime.fifo.pockets[0].destination == "bin-a"
    assert b.runtime.fifo.boundary == 1


@pytest.mark.parametrize("failure", ["slip", "missing", "receipt"])
def test_marker_or_motor_failure_never_advances_fifo(failure):
    b = Bench((0,) if failure == "slip" else ())
    x = b.admit("a")
    b.tick()
    if failure == "missing":
        b.rig.marker_missing = True
    if failure == "receipt":
        b.rig.token += 1
    with pytest.raises(PositionError):
        for _ in range(180):
            b.tick()
    assert b.runtime.fifo.boundary == 0 and b.runtime.current(x)


def test_fifo_wrap_preserves_routes_around_discards():
    b = Bench()
    expected = []
    for i in range(24):
        x = b.admit(f"bin-{i}" if i % 3 else None, arrived=bool(i % 3))
        expected.append(x)
        assert x.key == (i % 10, i // 10 + 1)
        b.advance()
    for _ in range(6):
        b.advance()
    assert [x for _, x in b.chute.exits] == expected
    for event, binding in b.chute.exits:
        i = expected.index(binding)
        assert event.pocket.destination == (f"bin-{i}" if i % 3 else None)
    assert all(p.state is PocketState.EMPTY for p in b.runtime.fifo.pockets)


@pytest.mark.parametrize(
    "error,response", [(None, None), ("provider failure", None), (None, "valid")]
)
def test_unknown_error_and_late_results_become_discard(error, response):
    b = Bench()
    x = b.admit()
    for _ in range(6):
        b.advance()
    b.tick()  # P0 deadline
    assert b.runtime.fifo.pockets[0].state is PocketState.DISCARD
    assert not b.runtime.accept_result(
        x,
        RecognitionResult(
            x.piece.uuid, x.episode.episode_id, x.generation, response, error
        ),
    )


def test_wrong_generation_and_retired_callbacks_cannot_mutate_reused_pocket():
    b = Bench()
    old = b.admit("a")
    for _ in range(10):
        if b.runtime.fifo.boundary >= 7:
            # Idle empty motion isn't demand. Recovery may explicitly sweep it.
            b.runtime.recover(unknown=False)
        b.advance()
    # Finish the full sweep rather than discarding its lifecycle state.
    while b.runtime.recovering:
        b.tick()
    new = b.admit()
    assert new.pocket_id == old.pocket_id and new.generation > old.generation
    assert not b.runtime.accept_result(
        old,
        RecognitionResult(
            old.piece.uuid, old.episode.episode_id, old.generation, "wrong"
        ),
    )
    assert b.runtime.fifo.pockets[new.pocket_id].state is PocketState.PENDING
    assert not b.runtime.accept_result(
        new,
        RecognitionResult(
            new.piece.uuid, new.episode.episode_id, old.generation, "wrong"
        ),
    )
    assert b.runtime.fifo.pockets[new.pocket_id].state is PocketState.DISCARD


def test_pause_preserves_journeys_and_resume_verifies_marker_boundary():
    b = Bench()
    x = b.admit()
    x.journey = b.runtime.journeys.start_reserved(
        x.piece, episode_id=x.episode.episode_id, generation=x.generation
    )
    snapshot = b.runtime.fifo.pockets
    b.runtime.pause()
    for _ in range(20):
        b.tick()
    assert b.runtime.fifo.pockets == snapshot and not x.journey.closed
    b.runtime.resume(b.rig.clock())
    b.tick()
    assert not b.rig.commands
    b.advance()
    assert b.runtime.fifo.boundary == 1


def test_resume_verifies_sample_completed_after_tick_entry():
    b = Bench()
    b.runtime.pause()
    b.runtime.resume(b.rig.clock())
    boundary = b.runtime.fifo.boundary
    original_sample = b.rig.sample
    receipt_pairs = []

    def sample_after_processing():
        b.rig.clock.advance(0.02)
        sample = original_sample()
        receipt_pairs.append(sample.received_mono)
        return sample

    b.rig.sample = sample_after_processing
    for _ in range(8):
        b.rig.clock.advance()
        tick_entry = b.rig.clock()
        b.runtime.tick(tick_entry, allow_motion=False)
        assert receipt_pairs[-1] > tick_entry
        assert not b.rig.commands
        assert b.runtime.fifo.boundary == boundary
        if b.runtime._verify is None:
            break

    assert b.runtime._verify is None


def test_resume_displaced_marker_does_not_guess():
    b = Bench()
    b.admit("a")
    b.runtime.pause()
    b.rig.phase = (b.rig.phase + 36) % 360
    b.runtime.resume(b.rig.clock())
    with pytest.raises(PositionError, match="retained C4 boundary"):
        for _ in range(20):
            b.tick()
    assert b.runtime.fifo.boundary == 0 and not b.rig.commands


@pytest.mark.parametrize("start", [0, 1, 6, 9])
def test_complete_recovery_sweeps_unknown_and_returns_empty_at_p0(start):
    b = Bench()
    for _ in range(start):
        b.admit("normal")
        b.advance()
    old = tuple(b.runtime.fifo.pockets)
    b.runtime.recover()
    assert all(p.state is PocketState.DISCARD for p in b.runtime.fifo.pockets)
    for _ in range(3000):
        b.tick()
        if not b.runtime.recovering:
            break
    assert not b.runtime.recovering and b.runtime.fifo.boundary % 10 == 0
    assert b.runtime.fifo.boundary - start >= 10
    assert all(p.state is PocketState.EMPTY for p in b.runtime.fifo.pockets)
    assert all(
        p.generation >= prior.generation
        for p, prior in zip(b.runtime.fifo.pockets, old)
    )
    assert not b.runtime.bindings and b.runtime.handoff is None


def test_active_index_and_handoff_are_mutually_exclusive():
    b = Bench()
    b.admit()
    b.tick()
    with pytest.raises(RuntimeError, match="conflicts"):
        b.reserve()
    assert b.runtime.fifo.boundary == 0


def test_loaded_journey_keeps_c3_fall_landing_and_changed_alias():
    b = Bench()
    x = b.reserve()
    js = b.runtime.journeys
    js.set_epoch(3, A.epoch)
    js.set_epoch(4, B.epoch)
    x.journey = j = js.start_reserved(
        x.piece,
        episode_id=x.episode.episode_id,
        generation=x.generation,
        scene=scene(),
        alias=A,
    )
    resident = Detection(replace(B, track_id=99), (125, 10, 150, 40))
    assert js.begin_crossing(
        j,
        release=scene(seq=2, ts=100.2),
        empty_c4=scene(B, seq=10, ts=100.1, detections=[resident]),
        c3_region=REGION,
        c4_region=REGION,
        reservation=(x.episode.episode_id, *x.key),
    )
    fall_alias = replace(B, track_id=77)
    assert js.burst_frame(scene(fall_alias, seq=11, ts=100.3))
    assert js.burst_frame(
        scene(
            B,
            seq=12,
            ts=100.4,
            detections=[Detection(fall_alias, BOX), Detection(B, BOX), resident],
        )
    )
    assert js.land(
        j,
        scene=scene(
            B, seq=13, ts=100.6, seed=8, detections=[Detection(B, BOX), resident]
        ),
        c3_after=scene(seq=3, ts=100.7, detections=[]),
    )
    assert js.observe(j, scene(B, seq=14, ts=100.8, seed=5), B, Stage.SETTLED)
    b.runtime.finish_handoff(x, arrived=True)
    assert j.uuid == x.piece.uuid
    assert {
        Stage.C3_READY,
        Stage.C3_EXIT,
        Stage.FALL,
        Stage.LANDING,
        Stage.SETTLED,
    } <= {o.stage for o in j.observations}
    calls = []
    assert js.submit(
        j,
        lambda request: calls.append(request) or "bin-good",
        launch=lambda work: work(),
    )
    b.tick()
    assert len(calls) == 1 and 1 <= len(calls[0].images) <= 4
    assert b.runtime.fifo.pockets[0].destination == "bin-good"


def test_scene_publisher_changes_epoch_on_tracker_replacement_and_generation_on_reuse():
    from perception.journey_scenes import JourneyScenes

    journal = JourneyScenes(4)
    camera, tracker = object(), object()
    frame = NS(timestamp=100, bgr=scene(B).bgr)
    first = journal.publish(frame, [BOX], {BOX: 7}, camera=camera, tracker=tracker)
    journal.publish(frame, [], {}, camera=camera, tracker=tracker)
    reused = journal.publish(frame, [BOX], {BOX: 7}, camera=camera, tracker=tracker)
    assert reused.detections[0].alias.generation > first.detections[0].alias.generation
    replaced = journal.publish(frame, [BOX], {BOX: 7}, camera=camera, tracker=object())
    assert replaced.epoch != first.epoch


def test_active_controller_selection_has_no_legacy_delegate_or_watchdog(monkeypatch):
    from irl.config import ClassificationChannelMode
    from subsystems.classification_channel.state_machine import (
        ClassificationChannelStateMachine,
    )
    import subsystems.classification_channel.physical_controller as module

    owner = NS(physical_c4_authority=True, step=Mock())
    monkeypatch.setattr(module, "PhysicalC4Controller", lambda *args: owner)
    for mode in (
        ClassificationChannelMode.TWO_PIECE_STATE_MACHINE_REV01,
        ClassificationChannelMode.INDEXED_POCKET_PIPELINE_REV01,
    ):
        sm = ClassificationChannelStateMachine(
            irl=NS(),
            irl_config=NS(classification_channel_config=NS(mode=mode)),
            gc=NS(
                logger=logging.getLogger("test"), profiler=Mock(), runtime_stats=Mock()
            ),
            shared=NS(),
            vision=None,
            event_queue=queue.Queue(),
            transport=NS(),
        )
        sm._checkStall = Mock(side_effect=AssertionError("legacy watchdog"))
        sm.step()
        assert (
            sm._delegate is owner
            and sm._two_piece is None
            and sm.supportsStatefulPause()
        )


def controller_bench(monkeypatch):
    from subsystems.classification_channel.physical_controller import (
        PhysicalC4Controller,
    )
    from subsystems.shared_variables import SharedVariables
    from runtime_stats import RuntimeStatsCollector
    from piece_transport import ClassificationChannelTransport
    from server import shared_state

    monkeypatch.setattr(shared_state, "command_queue", queue.Queue())
    monkeypatch.setattr(shared_state, "hardware_state", "ready")
    monkeypatch.setattr(shared_state, "setHardwareStatus", Mock())

    bench = Bench()
    monkeypatch.setattr("time.monotonic", bench.rig.clock)
    monkeypatch.setattr("time.time", bench.rig.clock)
    config = NS(
        classification_channel_config=NS(c4_sector_count=10, c4_gear_ratio=130 / 12),
        c_channel_4_rotor_stepper=NS(microsteps=8, default_steps_per_second=500),
        feeder_config=NS(classification_channel_eject=None),
    )
    records = {3: (), 4: ()}
    samples = {3: None, 4: None}
    perception = NS(
        read_journey_scenes=lambda ch: records[ch],
        read_pieces_and_frame=lambda ch: samples[ch],
    )
    gc = NS(
        logger=logging.getLogger("controller-test"),
        runtime_stats=RuntimeStatsCollector(),
        profiler=Mock(),
        perception_service=perception,
        classification_burst_dump_root=None,
    )
    shared = SharedVariables(gc)
    shared.transport = transport = ClassificationChannelTransport()
    shared.c4_reset_distribution = lambda: transport.resetC4Distribution()
    irl = NS(
        carousel_stepper=NS(
            stopped=True, stationary_verified=lambda: True, enabled=True
        ),
        servos=[],
        **{f"c_channel_{n}_rotor_stepper": NS(stopped=True) for n in (1, 2, 3)},
    )
    controller = PhysicalC4Controller(
        irl, config, gc, shared, transport, None, queue.Queue(), positioner=bench.rig.p
    )
    controller.runtime = bench.runtime
    controller._region = lambda ch: REGION
    controller._fault = lambda reason: (
        None
    )  # actuator stop is exercised separately below
    shared.request_c3_recovery = lambda ep, boundary: {
        "predicates": {
            **boundary["predicates"],
            "motor_c3_resolved": True,
            "motor_c2_resolved": True,
        },
        "material": [],
        "followers": [],
    }
    return bench, controller, records, samples


def reserve_controller(c, b, records):
    now = b.rig.clock()
    records[3] = (scene(ts=now),)
    records[4] = (scene(B, ts=now - 0.1, detections=[]),)
    assert c.reserve_transfer(NS(ts=now, pieces=[NS(sv_bt_track_id=42, bbox=BOX)]), {})
    return c.runtime.handoff


def test_controller_unlocated_handoff_expires_to_same_discard_generation(monkeypatch):
    b, c, records, _ = controller_bench(monkeypatch)
    binding = reserve_controller(c, b, records)
    c.shared.request_c3_recovery = lambda ep, boundary: {
        "predicates": {
            **boundary["predicates"],
            **dict.fromkeys(
                (
                    "active_episode",
                    "c3_enabled",
                    "motors_unsuppressed",
                    "c3_owner_consistent",
                    "motor_c2_resolved",
                    "motor_c3_resolved",
                ),
                True,
            ),
        },
        "material": [],
        "followers": [],
    }
    for _ in range(17):
        b.rig.clock.advance(1)
        c.step()
        if binding.admitted:
            break
    assert c.fault is None and binding.admitted and c.handoff is None
    assert c.runtime.fifo.pockets[binding.pocket_id].state is PocketState.DISCARD
    assert c.runtime.current(binding) and binding.episode.state == "discard_bound"


def test_controller_retained_original_and_pause_keep_reservation_until_marker_verify(
    monkeypatch,
):
    b, c, records, samples = controller_bench(monkeypatch)
    binding = reserve_controller(c, b, records)
    observe = Mock(
        side_effect=lambda ep, boundary: {
            "predicates": {**boundary["predicates"], "motor_c3_resolved": True},
            "material": [{"id": 42}],
            "followers": [],
            "same_piece_retained": True,
        }
    )
    c.shared.request_c3_recovery = observe
    for i in range(3):
        b.rig.clock.advance(0.1)
        samples[4] = ([NS(zone_code=1)], NS(timestamp=b.rig.clock()))
        c.step()
    assert c.runtime.handoff is binding and not b.rig.commands
    armed = c.handoff._arrival_armed_at_mono
    c.pause()
    b.rig.clock.advance(25)
    c.resume()
    assert c.handoff._arrival_armed_at_mono == pytest.approx(armed + 25)
    observe.reset_mock()
    b.rig.clock.advance()
    c.step()
    assert c.runtime._verify is not None
    observe.assert_not_called()
    for _ in range(8):
        b.rig.clock.advance()
        c.step()
        if c.runtime._verify is None:
            break
    assert c.runtime._verify is None and c.runtime.handoff is binding
    assert not binding.journey.closed and not b.rig.commands


def test_controller_unsupported_identity_rejects_and_reopens_admission(monkeypatch):
    from server import shared_state

    b, c, records, _ = controller_bench(monkeypatch)
    binding = reserve_controller(c, b, records)
    original = binding.key, binding.episode.episode_id
    c.shared.request_c3_recovery = lambda ep, boundary: {
        "predicates": {
            **boundary["predicates"],
            **dict.fromkeys(("active_episode", "c3_enabled", "motors_unsuppressed",
                            "c3_owner_consistent", "motor_c2_resolved",
                            "motor_c3_resolved", "feeder_tick_fresh", "c3_frame_fresh"), True),
            "spatial_association": False,
            "c3_supported_region": False,
        },
        "material": [],
        "followers": [{"id": 42}],
        "same_piece_retained": False,
        "retained_plan": None,
    }
    for _ in range(17):
        b.rig.clock.advance(1)
        c.step()
        if binding.admitted:
            break
    assert binding.admitted and binding.episode.state == "discard_bound"
    assert (binding.key, binding.episode.episode_id) == original
    assert c.runtime.fifo.pockets[binding.pocket_id].state is PocketState.DISCARD
    assert binding.piece.part_id is None and binding.piece.destination_bin is None
    assert binding.piece.forced_reject_reason and c.handoff is None
    for _ in range(160):
        b.rig.clock.advance()
        c.step()
        if c.shared.classification_ready:
            break
    assert c.runtime.fifo.boundary == 1 and c.runtime.can_admit
    assert c.shared.classification_ready and c.fault is None
    assert c.gc.runtime_stats.activeIncident() is None
    assert shared_state.command_queue.empty()
    shared_state.setHardwareStatus.assert_not_called()


def test_delayed_c3_frame_finishes_historical_landing_after_index_requested(
    monkeypatch,
):
    b, c, records, _ = controller_bench(monkeypatch)
    binding = reserve_controller(c, b, records)
    b.rig.clock.advance(0.1)
    records[4] += (scene(B, seq=2, ts=b.rig.clock()),)
    c.finish_handoff(binding, b.rig.clock(), arrived=True)
    assert not binding.journey.crossing_confirmed
    c.runtime.tick(b.rig.clock())
    assert c.runtime._pending is not None
    b.rig.clock.advance(0.1)
    records[3] += (scene(seq=2, ts=b.rig.clock(), detections=[]),)
    c.step()
    assert binding.journey.crossing_confirmed
    # Ready and release use the same frame here; Slice 2 deduplicates that
    # exact frame while retaining its upstream association after landing.
    assert Stage.C3_READY in {o.stage for o in binding.journey.observations}


@pytest.mark.parametrize("latched", [False, True])
def test_direct_drain_establishes_current_phase_and_sweeps(monkeypatch, latched):
    from subsystems.classification_channel.complete_drain import drain_controller

    b, c, _, _ = controller_bench(monkeypatch)
    binding = b.admit("a")
    c.pause()
    b.rig.phase = (b.rig.phase + 72) % 360
    if latched:
        c.fault = "displaced"
    monkeypatch.setattr(c, "reject_path_ready", lambda: True)
    controller = NS(
        coordinator=NS(
            classification=NS(_delegate=c), feeder=Mock(), distribution=Mock()
        ),
        gc=c.gc,
    )
    result = drain_controller(controller, clock=b.rig.clock, sleep=b.rig.clock.advance)
    assert c.fault is None and result["pockets_swept"] >= 10
    assert result["boundary"] % 10 == 0 and c._paused
    assert c.runtime.fifo.pockets[binding.pocket_id].generation >= binding.generation
    assert all(p.state is PocketState.EMPTY for p in c.runtime.fifo.pockets)


def test_reject_recovery_waits_for_existing_opposing_flap_command(monkeypatch):
    b, c, _, _ = controller_bench(monkeypatch)
    door = NS(
        available=True,
        is_calibrated=True,
        stopped=False,
        open=Mock(),
        isOpen=lambda: True,
    )
    c.irl.servos = [door]
    assert not c.reject_path_ready()
    door.open.assert_not_called()
    door.stopped = True
    assert c.reject_path_ready()
    door.open.assert_called_once_with()


def test_physical_fault_stops_same_coordinator_tick_and_reaches_pause_queue(
    monkeypatch,
):
    from unittest.mock import MagicMock
    from coordinator import Coordinator
    from sorter_controller import SorterController
    from defs.sorter_controller import SorterLifecycle
    from subsystems.bus import TickBus
    from server import shared_state
    import threading

    b, c, _, _ = controller_bench(monkeypatch)
    x = b.admit("a")
    monkeypatch.setattr(b.runtime, "tick", Mock(side_effect=OSError("source failed")))
    co = object.__new__(Coordinator)
    co.gc, co.shared, co.bus = c.gc, c.shared, TickBus()
    co.gc.profiler = MagicMock()
    co.classification = NS(
        _delegate=c, step=c.step, pause=c.pause, supportsStatefulPause=lambda: True
    )
    co.distribution, co.feeder = Mock(), Mock()
    co.manual_feed_mode = False
    controller = object.__new__(SorterController)
    controller._operation_lock = threading.RLock()
    controller.state, controller.coordinator, controller.gc = (
        SorterLifecycle.RUNNING,
        co,
        c.gc,
    )
    controller.gc.run_recorder = controller.gc.lifetime_stats = Mock()
    controller.vision = Mock()
    controller.step()
    co.feeder.step.assert_not_called()
    co.feeder.hold_motion.assert_called_once_with()
    assert shared_state.command_queue.get_nowait().tag == "pause"
    controller.pause()
    assert controller.state is SorterLifecycle.PAUSED and c.runtime.current(x)


def test_missing_marker_setup_latches_fault_before_positioner_exists(monkeypatch):
    import subsystems.classification_channel.physical_controller as module

    b, c, _, _ = controller_bench(monkeypatch)
    c.runtime = c.positioner = None
    monkeypatch.setattr(c, "reject_path_ready", lambda: True)
    monkeypatch.setattr(
        module,
        "create_positioner",
        Mock(side_effect=FileNotFoundError("mapping missing")),
    )
    c.step()
    assert c.fault == "mapping missing" and c.shared.c4_runtime_owner is c
    fresh = Rig()
    monkeypatch.setattr(module, "create_positioner", lambda *args: fresh.p)
    c.begin_recovery()
    assert c.positioner is fresh.p and c.fault is None


def test_restart_unknown_runtime_drains_before_accepting_first_piece(monkeypatch):
    from subsystems.classification_channel.complete_drain import drain_controller
    from subsystems.classification_channel.marker_positioner import MarkerPositioner

    b, c, _, _ = controller_bench(monkeypatch)
    c.runtime = None
    b.rig.phase = (b.rig.phase + 108) % 360
    c.positioner = MarkerPositioner(
        b.rig, b.rig, b.rig.p.mapping, c._fault, clock=b.rig.clock
    )
    monkeypatch.setattr(c, "reject_path_ready", lambda: True)
    c.shared.c4_reject_path_ready = lambda: True
    c.shared.set_distribution_gate(True)
    assert not c.reserve_transfer(NS(pieces=[]), {})
    controller = NS(
        coordinator=NS(
            classification=NS(_delegate=c), feeder=Mock(), distribution=Mock()
        ),
        gc=c.gc,
    )
    result = drain_controller(controller, clock=b.rig.clock, sleep=b.rig.clock.advance)
    assert result["pockets_swept"] >= 10 and result["boundary"] % 10 == 0
    assert not c.runtime.can_admit  # complete recovery returns paused/READY
    assert all(
        p.state is PocketState.EMPTY and p.generation == 1
        for p in c.runtime.fifo.pockets
    )
    assert not c.runtime.bindings and c.shared.c3_transfer_episode is None


def test_failed_pending_marker_request_is_quarantined_by_complete_recovery(monkeypatch):
    from subsystems.classification_channel.complete_drain import drain_controller

    b, c, _, _ = controller_bench(monkeypatch)
    binding = b.admit("a")
    b.tick()
    pending = c.runtime._pending
    b.rig.token += 1
    c.step()
    assert c.fault and c.runtime._pending is pending
    assert c.runtime.fifo.boundary == 0 and c.runtime.current(binding)
    monkeypatch.setattr(c, "reject_path_ready", lambda: True)
    controller = NS(
        coordinator=NS(
            classification=NS(_delegate=c), feeder=Mock(), distribution=Mock()
        ),
        gc=c.gc,
    )
    result = drain_controller(controller, clock=b.rig.clock, sleep=b.rig.clock.advance)
    assert result["boundary"] % 10 == 0 and c.fault is None
    assert all(p.state is PocketState.EMPTY for p in c.runtime.fifo.pockets)


def test_planned_harvest_retirement_preserves_confirmed_and_other_runtime(tmp_path):
    import json
    from test_project_harvest_projects import _store_with_draft, _bom

    store, draft = _store_with_draft(tmp_path, repeated_part=True)
    project = store.create_project(draft_id=draft["draft_id"])
    project_id = project["project_id"]
    store.save_bom(
        project_id,
        content=_bom({"part_id": "3001", "color_id": "2", "quantity": 3}),
        filename="bom.json",
        provider="private_moc",
    )
    allocations = [
        store.propose_allocation(
            project_id, piece_id=f"piece-{n}", part_id="3001", color_id="2"
        )
        for n in range(3)
    ]
    store.confirm_allocation(
        project_id, allocations[0]["allocation_id"], evidence={"simulation": True}
    )
    with store._connection() as conn:
        conn.execute(
            "UPDATE harvest_allocations SET mode = 'live', runtime_id = 'current'"
        )
        conn.execute(
            "UPDATE harvest_allocations SET runtime_id = 'older' WHERE piece_id = 'piece-1'"
        )
        conn.execute(
            "INSERT INTO harvest_runtime_state VALUES(1, ?, 'current', ?, 'test')",
            (project_id, json.dumps({"activation_id": "current"})),
        )
        conn.commit()
        before = dict(
            conn.execute("SELECT piece_id,status FROM harvest_allocations").fetchall()
        )
    assert store.retire_c4_planned_allocations(piece_ids=["piece-0", "piece-1"]) == 0
    assert store.retire_c4_planned_allocations() == 1
    assert store.retire_c4_planned_allocations() == 0
    with store._connection() as conn:
        after = dict(
            conn.execute("SELECT piece_id,status FROM harvest_allocations").fetchall()
        )
    assert after == {**before, "piece-2": "undone"}
    assert after["piece-0"] == "confirmed" and after["piece-1"] == "planned"


def test_original_carousel_capture_produces_exact_scene_history_and_freefall(
    monkeypatch,
):
    import numpy as np
    from perception.journey_scenes import JourneyScenes
    from recognition_journey_capture import retain_rolling_fall, retain_track_history
    import vision.tracking.drop_zone_burst as burst_module

    monkeypatch.setattr(burst_module, "BURST_POST_FRAMES", 0)
    monkeypatch.setattr(
        "classification.auto_recognize.run_async",
        Mock(side_effect=AssertionError("second provider")),
    )
    journal = JourneyScenes(4)
    yy, xx = np.indices((140, 160))
    radius = np.hypot(xx - 80, yy - 70)
    channel = NS(center=(80, 70), mask=(radius > 10) & (radius < 65))
    camera = object()

    def publish(stamp, seed, boxes):
        frame = NS(timestamp=stamp, bgr=scene(B, seed=seed).bgr)
        result = journal.publish_c4(
            frame, boxes, [1.0] * len(boxes), camera=camera, channel=channel
        )
        for thread in tuple(journal.burst._active.values()):
            thread.join(3)
            assert not thread.is_alive()
        return result

    empty = publish(100.1, 1, [])
    b = Bench()
    binding = b.reserve()
    js = b.runtime.journeys
    js.set_epoch(3, A.epoch)
    js.set_epoch(4, empty.epoch)
    j = js.start_reserved(
        binding.piece,
        episode_id=binding.episode.episode_id,
        generation=binding.generation,
        scene=scene(ts=100.2),
        alias=A,
    )
    binding.journey = j
    assert js.begin_crossing(
        j,
        release=scene(seq=2, ts=100.3),
        empty_c4=empty,
        c3_region=REGION,
        c4_region=REGION,
        reservation=(binding.episode.episode_id, *binding.key),
    )
    fall = publish(100.4, 2, [BOX])
    publish(100.5, 3, [BOX])
    landing = publish(100.6, 4, [BOX])
    alias = landing.detections[0].alias
    assert alias == fall.detections[0].alias
    assert (
        retain_rolling_fall(
            js,
            journal.rolling_buffer,
            lambda frame: next(
                (s for s in journal.records if s.timestamp == frame.timestamp), None
            ),
        )
        >= 1
    )
    assert js.land(j, scene=landing, c3_after=scene(seq=3, ts=100.7, detections=[]))
    publish(100.8, 5, [BOX])
    reference, detail = journal.details[alias]
    assert reference.alias == alias and detail["segments"][0]["sector_snapshots"]
    assert detail["drop_zone_burst"]
    assert (
        retain_track_history(
            js,
            j,
            detail,
            reference,
            lambda record: next(
                (
                    s
                    for s in journal.records
                    if s.timestamp == record.get("captured_ts", record.get("timestamp"))
                ),
                None,
            ),
        )
        >= 1
    )
    assert {
        Stage.C3_READY,
        Stage.C3_EXIT,
        Stage.FALL,
        Stage.LANDING,
        Stage.SETTLED,
    } <= {o.stage for o in j.observations}
    requests = []
    assert js.submit(j, lambda req: requests.append(req), launch=lambda work: work())
    assert len(requests) == 1 and 1 <= len(requests[0].images) <= 4
    from classification.auto_recognize import run_async

    run_async.assert_not_called()
    # Complete raw detections remain present even when observation tracking
    # cannot pair one of them; it never creates physical ownership.
    foreign = (125, 10, 150, 40)
    full = publish(100.9, 6, [BOX, foreign])
    assert {d.bbox for d in full.detections} == {BOX, foreign}


def test_real_inference_worker_publishes_complete_original_scene_and_history(
    monkeypatch,
):
    import time
    from perception.tests.test_no_camera_crossover import _build_worker_set
    from perception.service import PerceptionService
    import vision.tracking.drop_zone_burst as burst_module

    monkeypatch.setattr(burst_module, "BURST_POST_FRAMES", 0)
    captures, _, runtimes, _, slots, workers = _build_worker_set({2: (), 3: (), 4: ()})
    worker = workers[4]
    target = (42, 72, 58, 88)
    x, y, _, _ = worker._crop_rect
    # Runtime output coordinates are relative to its actual inference crop.
    runtimes[4].set_bboxes(
        [(target[0] - x, target[1] - y, target[2] - x, target[3] - y)]
    )
    service = object.__new__(PerceptionService)
    service._workers = {4: worker}
    worker.start()
    try:
        deadline = time.monotonic() + 3
        count = 0
        while time.monotonic() < deadline:
            count += 1
            captures["carousel"].push(timestamp=10 + count * 0.05, fill=100)
            time.sleep(0.02)
            if len(service.read_journey_scenes(4)) >= 3:
                break
        scenes = service.read_journey_scenes(4)
        assert len(scenes) >= 3
        latest = scenes[-1]
        assert latest.channel == 4 and latest.detections[0].bbox == target
        reference, detail = service.read_journey_history(latest.detections[0].alias)
        assert reference.alias in {d.alias for d in latest.detections}
        assert detail["segments"][0]["sector_snapshots"]
        assert slots[4].read().pieces[0].zone_code == 1
        assert worker.source_id_assertions == 0
    finally:
        worker.stop()
        if hasattr(worker.journey_scenes, "burst"):
            for thread in tuple(worker.journey_scenes.burst._active.values()):
                thread.join(3)


def test_controller_reserves_before_release_and_captures_same_uuid_on_arrival(
    monkeypatch,
):
    b, c, records, samples = controller_bench(monkeypatch)
    now = b.rig.clock()
    records[3] = (scene(ts=now),)
    records[4] = (scene(B, ts=now - 0.1, detections=[]),)
    state = NS(ts=now, pieces=[NS(sv_bt_track_id=A.track_id, bbox=BOX)])
    assert c.reserve_transfer(state, {})
    binding = c.runtime.handoff
    assert binding.journey.uuid == binding.piece.uuid and not binding.admitted
    assert not c.shared.classification_ready and not b.rig.commands
    for i in range(1, 4):
        b.rig.clock.advance(0.1)
        stamp = b.rig.clock()
        records[4] += (scene(B, seq=10 + i, ts=stamp, seed=10 + i),)
        records[3] += (scene(seq=i + 1, ts=stamp, detections=[]),)
        samples[4] = ([NS(zone_code=1)], NS(timestamp=stamp))
        c.step()
        if c.handoff is None:
            break
    assert c.fault is None and c.handoff is None
    assert binding.admitted and binding.journey.uuid == binding.piece.uuid
    assert any(
        o.stage in (Stage.LANDING, Stage.SETTLED) for o in binding.journey.observations
    )
    assert c.runtime.fifo.pockets[0].generation == binding.generation


def test_existing_unknown_intake_is_rejected_before_original_c3_release(monkeypatch):
    b, c, records, samples = controller_bench(monkeypatch)
    now = b.rig.clock()
    records[4] = (scene(B, ts=now),)
    samples[4] = ([NS(zone_code=1)], NS(timestamp=now))
    assert not c.reserve_transfer(NS(ts=now, pieces=[NS(sv_bt_track_id=42)]), {})
    assert c.runtime.handoff is None and c.handoff is None
    assert c.runtime.fifo.pockets[0].state is PocketState.DISCARD
    assert not b.rig.commands


def test_neighbor_inside_capture_rectangle_does_not_create_phantom_intake(monkeypatch):
    import numpy as np
    from perception.journey_scenes import zone_region
    from perception.arcs import orderedPieceObservations

    b, c, records, samples = controller_bench(monkeypatch)
    yy, xx = np.indices((210, 210))
    radius = np.hypot(xx - 100, yy - 100)
    channel = NS(
        channel_id=4,
        center=(100, 100),
        mask=(radius >= 30) & (radius <= 90),
        radius1_angle_image=0.0,
        drop_sections=frozenset(range(30)),
        exit_sections=frozenset(range(200, 230)),
        precise_sections=frozenset(),
        reverse=False,
    )
    box = (124, 136, 132, 144)
    region = zone_region(channel, channel.drop_sections)
    from recognition_journey import _overlap

    assert _overlap(box, region)
    labels = orderedPieceObservations([box], channel)
    assert labels and labels[0][2] != 1
    now = b.rig.clock()
    records[4] = (scene(B, ts=now, detections=[Detection(B, box)]),)
    samples[4] = ([NS(zone_code=labels[0][2])], NS(timestamp=now))
    c._region = lambda ch: region
    assert c.reserve_transfer(NS(ts=now, pieces=[NS(sv_bt_track_id=42)]), {})
    assert c.runtime.handoff is not None
    assert c.runtime.fifo.pockets[0].state is PocketState.PENDING


def test_real_distribution_resolves_selected_destination_before_chute_alignment(
    monkeypatch,
):
    from subsystems.classification_channel.physical_distribution import (
        PhysicalDistribution,
    )

    b, c, _, _ = controller_bench(monkeypatch)
    r = b.runtime
    r.distribution = PhysicalDistribution(c.shared, c.events, c.gc)
    x = b.admit("3001")
    r.distribution.observe(r, b.rig.clock())
    assert c.transport.getPieceForDistributionPositioning() is x.piece
    x.piece.destination_bin = (0, 1, 2)
    x.piece.distribution_target_selected_at = b.rig.clock()
    c.shared.distribution_ready = False
    observation = r.distribution.observe(r, b.rig.clock())
    assert observation.aligned_destination is None
    assert r.fifo.pockets[0].destination == "bin:0:1:2"
    assert r.fifo.pockets[0].state is PocketState.ROUTED


def test_controller_fault_preserves_owner_and_backend_lifecycle(monkeypatch):
    from server import shared_state

    b, c, _, _ = controller_bench(monkeypatch)
    x = b.admit("a")

    def fail(*args, **kwargs):
        raise PositionError("marker disconnected")

    monkeypatch.setattr(b.runtime, "tick", fail)
    monkeypatch.setattr(shared_state, "setHardwareStatus", Mock())
    c.step()
    assert c.fault == "marker disconnected" and c.runtime.current(x)
    assert c.shared.c4_runtime_owner is c and c._paused
    shared_state.setHardwareStatus.assert_called_with(
        state="error", error="marker disconnected"
    )


def _harvest_terminal_distribution():
    from defs.known_object import HARVEST_CONFIRMATION_UNCREDITED, PieceStage
    from piece_transport import ClassificationChannelTransport
    from subsystems.classification_channel.physical_distribution import PhysicalDistribution

    transport = ClassificationChannelTransport()
    piece = KnownObject(
        stage=PieceStage.distributing, aborted=True,
        transport_failure_reason=HARVEST_CONFIRMATION_UNCREDITED,
        c4_marker_exit_boundary=10,
    )
    transport._exit_piece = piece
    shared = NS(
        transport=transport, distribution_ready=False,
        chute_move_in_progress=False, native_completion_guard=None,
    )
    return PhysicalDistribution(shared, queue.Queue()), piece


def test_harvest_terminal_failure_clear_waits_for_distribution_ownership():
    from defs.known_object import PieceStage

    distribution, piece = _harvest_terminal_distribution()
    assert not distribution.clear()
    distribution.shared.distribution_ready = True
    assert distribution.clear()
    assert piece.stage is PieceStage.distributing
    assert piece.distributed_at is None and piece.aborted


@pytest.mark.parametrize("unresolved", [
    "not_aborted", "other_failure", "no_exit", "nonterminal_stage",
    "credited", "positioning_owned", "chute_motion", "native_owned", "native_guard",
])
def test_harvest_terminal_clear_does_not_generalize_to_unresolved_states(unresolved):
    from defs.known_object import PieceStage

    distribution, piece = _harvest_terminal_distribution()
    distribution.shared.distribution_ready = True
    if unresolved == "not_aborted":
        piece.aborted = False
    elif unresolved == "other_failure":
        piece.transport_failure_reason = "unconfirmed distribution"
    elif unresolved == "no_exit":
        piece.c4_marker_exit_boundary = None
    elif unresolved == "nonterminal_stage":
        piece.stage = PieceStage.created
    elif unresolved == "credited":
        piece.distributed_at = 1.0
    elif unresolved == "positioning_owned":
        distribution.transport.placePieceForDistribution(piece)
    elif unresolved == "chute_motion":
        distribution.shared.chute_move_in_progress = True
    elif unresolved == "native_owned":
        piece.native_reservation_id = "retained-reservation"
    else:
        distribution.shared.native_completion_guard = NS(blocks_admission=lambda: True)
    assert not distribution.clear()


def test_clear_preserves_ordinary_success_and_native_ownership_guard():
    from defs.known_object import PieceStage

    distribution, piece = _harvest_terminal_distribution()
    distribution.shared.distribution_ready = True
    piece.stage = PieceStage.distributed
    piece.aborted = False
    piece.transport_failure_reason = None
    assert distribution.clear()
    distribution.native_completion = NS(blocks_admission=lambda: True)
    assert not distribution.clear()


def test_complete_recovery_reestablishes_mismatched_boundary_without_erasing_generations(
    monkeypatch,
):
    b, c, _, _ = controller_bench(monkeypatch)
    x = b.admit("a")
    c.fault = "retained marker mismatch"
    b.rig.phase = (b.rig.phase + 36) % 360
    monkeypatch.setattr(c, "reject_path_ready", lambda: True)
    c.begin_recovery()
    assert c.runtime.current(x) and c._reestablishing
    for _ in range(4000):
        b.rig.clock.advance()
        c.step()
        if not c._reestablishing and not c.runtime.recovering:
            break
    assert c.fault is None
    assert c.runtime.fifo.boundary % 10 == 0
    assert c.runtime.fifo.pockets[x.pocket_id].generation >= x.generation
    assert all(p.state is PocketState.EMPTY for p in c.runtime.fifo.pockets)


def test_provider_and_metadata_work_are_off_control_tick_and_cannot_write_piece(
    monkeypatch,
):
    import threading
    from subsystems.classification_channel.simple_state_machine_rev01.base import (
        Rev01BaseState,
    )
    import subsystems.classification_channel.physical_controller as module

    b, c, _, _ = controller_bench(monkeypatch)
    x = b.admit()
    js = b.runtime.journeys
    js.set_epoch(4, B.epoch)
    x.journey = js.c4_only(
        x.piece,
        episode_id=x.episode.episode_id,
        generation=x.generation,
        scene=scene(B),
        alias=B,
    )
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(
        module,
        "brickognize_provider",
        lambda gc: lambda req: {"items": [{"id": "3001"}]},
    )

    def metadata(worker, obj):
        entered.set()
        assert release.wait(5)
        done.set()

    monkeypatch.setattr(Rev01BaseState, "_applyHivePieceMetadata", metadata)
    try:
        assert js.submit(x.journey, c._provider_for(x))
        assert entered.wait(5)
        for _ in range(5):
            b.tick()
        assert x.piece.part_id is None and b.runtime.fifo.pending_index is not None
    finally:
        release.set()
    assert done.wait(5)


def test_real_tracked_stepper_reads_do_not_invalidate_receipt_and_replacement_does():
    import struct
    from hardware.sorter_interface import StepperMotor, InterfaceCommandCode

    class Device:
        position = 0

        def send_command(self, command, channel, payload):
            if command == InterfaceCommandCode.STEPPER_GET_POSITION:
                return NS(payload=struct.pack("<i", self.position))
            if command == InterfaceCommandCode.STEPPER_IS_STOPPED:
                return NS(payload=b"\x01")
            if command == InterfaceCommandCode.STEPPER_MOVE_STEPS:
                self.position += struct.unpack("<i", payload)[0]
            return NS(payload=b"\x01")

    motor = StepperMotor(Device(), 0, NS(logger=Mock()))
    receipt = motor.start_tracked_move(100, 500)
    generation = motor._motion_generation
    assert motor.enabled and motor.position == 100
    assert motor._motion_generation == generation
    assert motor.tracked_move_complete(receipt)
    motor.enabled = False
    with pytest.raises(RuntimeError, match="interrupted"):
        motor.tracked_move_complete(receipt)
