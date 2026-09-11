import logging
import queue
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from piece_transport import ClassificationChannelTransport
from subsystems.classification_channel.indexed_pocket_pipeline import (
    IndexedPocketPipeline,
    _Phase,
)
from subsystems.classification_channel.pocket_ledger import (
    PocketLedger,
    PocketRoute,
)


def test_ledger_advances_fifo_without_reusing_an_occupied_pocket() -> None:
    ledger = PocketLedger(
        sector_count=10, exit_advance=7, travel_sign=-1, boundary_index=0
    )
    first = ledger.admit("first", admitted_at_mono=1.0)
    assert first.route == PocketRoute.REJECT

    for index in range(1, 7):
        assert ledger.advance_to(-index) == ()
        ledger.admit(f"piece-{index}", admitted_at_mono=float(index + 1))

    assert not ledger.can_admit
    assert ledger.due_to_exit_on_next_index() is first
    assert ledger.advance_to(-7) == (first,)
    assert ledger.can_admit


def test_ledger_rejects_non_sequential_absolute_index() -> None:
    ledger = PocketLedger(
        sector_count=10, exit_advance=7, travel_sign=1, boundary_index=4
    )
    with pytest.raises(RuntimeError, match="non-sequential"):
        ledger.advance_to(6)


class _Stepper:
    def __init__(self) -> None:
        self.position = 0
        self.stopped = True
        self.moves: list[int] = []

    def set_speed_limits(self, _minimum: int, _maximum: int) -> None:
        pass

    def move_steps(self, steps: int) -> bool:
        self.moves.append(int(steps))
        self.position += int(steps)
        return True

    def move_at_speed(self, _speed: int) -> bool:
        return True


class _Shared:
    def __init__(self) -> None:
        self.distribution_ready = True
        self.classification_ready = False
        self.release_attempt_mono = None
        self.deliveries: list[dict] = []

    def latest_piece_release_attempt_mono(self, **_kwargs):
        return self.release_attempt_mono

    def publish_piece_delivered(self, **kwargs) -> None:
        self.deliveries.append(dict(kwargs))

    def set_classification_gate(self, ready: bool, *, reason=None) -> None:
        self.classification_ready = bool(ready)


class _ImmediateRejectTransport(ClassificationChannelTransport):
    def placePieceForDistribution(self, obj) -> None:
        super().placePieceForDistribution(obj)
        obj.stage = obj.stage.distributing


def _pipeline(*, perception_service=None) -> IndexedPocketPipeline:
    stepper = _Stepper()
    irl = SimpleNamespace(carousel_stepper=stepper)
    irl_config = SimpleNamespace(
        classification_channel_config=SimpleNamespace(
            c4_sector_count=10,
            c4_gear_ratio=130.0 / 12.0,
        ),
        c_channel_4_rotor_stepper=SimpleNamespace(
            microsteps=8,
            default_steps_per_second=5000,
        ),
        feeder_config=SimpleNamespace(classification_channel_eject=None),
    )
    gc = SimpleNamespace(
        logger=logging.getLogger("test-indexed-pocket"),
        perception_service=perception_service,
        classification_burst_dump_root=None,
    )
    return IndexedPocketPipeline(
        irl,
        irl_config,
        gc,
        _Shared(),
        _ImmediateRejectTransport(),
        None,
        queue.Queue(),
    )


class _PerceptionSequence:
    def __init__(self, samples) -> None:
        self.samples = list(samples)

    def read_pieces_and_frame(self, channel_id: int):
        assert channel_id == 4
        if not self.samples:
            return None
        return self.samples.pop(0)


def _drop_sample(timestamp: float, *, present: bool):
    piece = SimpleNamespace(zone_code=1)
    frame = SimpleNamespace(timestamp=timestamp, bgr=None)
    return ([piece] if present else [], frame)


def test_release_attempt_does_not_create_a_logical_pocket_without_arrival() -> None:
    pipeline = _pipeline()
    shared = pipeline.shared
    shared.release_attempt_mono = 10.0

    pipeline._waitIntake(20.0)

    assert pipeline._phase == _Phase.WAIT_ARRIVAL
    assert pipeline._ledger.loads == ()
    assert pipeline._tail is None
    assert not shared.classification_ready

    pipeline._waitArrival(23.1)

    assert pipeline._phase == _Phase.WAIT_INTAKE
    assert pipeline._ledger.loads == ()
    assert pipeline._tail is None
    assert shared.classification_ready
    assert shared.deliveries == []
    assert pipeline._stepper.moves == []


def test_arrival_requires_two_fresh_physical_c4_frames() -> None:
    now_wall = time.time()
    perception = _PerceptionSequence(
        [
            _drop_sample(now_wall - 1.0, present=True),
            _drop_sample(now_wall + 1.0, present=True),
            _drop_sample(now_wall + 2.0, present=True),
        ]
    )
    pipeline = _pipeline(perception_service=perception)
    shared = pipeline.shared
    shared.release_attempt_mono = 10.0
    pipeline._waitIntake(20.0)
    pipeline._arrival_armed_at_wall = now_wall
    pipeline._admit = Mock()

    pipeline._waitArrival(20.1)
    pipeline._admit.assert_not_called()
    pipeline._waitArrival(20.2)
    pipeline._admit.assert_not_called()
    pipeline._waitArrival(20.3)

    pipeline._admit.assert_called_once()
    assert pipeline._admit.call_args.args == (20.3,)
    assert len(pipeline._admit.call_args.kwargs["initial_samples"]) == 2
    assert len(shared.deliveries) == 1
    assert shared.deliveries[0]["delivered_at_mono"] == 20.3


def test_empty_fresh_frame_resets_arrival_confirmation_streak() -> None:
    now_wall = time.time()
    perception = _PerceptionSequence(
        [
            _drop_sample(now_wall + 1.0, present=True),
            _drop_sample(now_wall + 2.0, present=False),
            _drop_sample(now_wall + 3.0, present=True),
            _drop_sample(now_wall + 4.0, present=True),
        ]
    )
    pipeline = _pipeline(perception_service=perception)
    pipeline.shared.release_attempt_mono = 10.0
    pipeline._waitIntake(20.0)
    pipeline._arrival_armed_at_wall = now_wall
    pipeline._admit = Mock()

    for timestamp in (20.1, 20.2, 20.3):
        pipeline._waitArrival(timestamp)
        pipeline._admit.assert_not_called()

    pipeline._waitArrival(20.4)
    pipeline._admit.assert_called_once()


def test_confirming_frames_seed_the_real_capture_burst() -> None:
    pipeline = _pipeline()
    image = np.random.default_rng(7).integers(0, 255, (80, 80, 3), dtype=np.uint8)
    piece = SimpleNamespace(zone_code=1, bbox=(20, 20, 60, 60))
    frames = [
        SimpleNamespace(timestamp=time.time() + offset, bgr=image)
        for offset in (1.0, 2.0)
    ]

    pipeline._admit(
        10.0,
        initial_samples=[([piece], frames[0]), ([piece], frames[1])],
    )

    load = pipeline._tail
    assert load is not None
    assert pipeline._phase == _Phase.CAPTURE_TAIL
    assert len(load.payload.ctx.captured_crops) == 2
    assert len(load.payload.ctx.known_object.recognition_image_set) == 2


def test_missing_capture_is_rejected_and_exits_after_seven_absolute_indexes() -> None:
    pipeline = _pipeline()
    pipeline._admit(1.0)
    load = pipeline._tail
    assert load is not None

    pipeline._captureTail(load.admitted_at_mono + 2.0)
    assert load.route_locked
    assert load.route == PocketRoute.REJECT
    assert pipeline._phase == _Phase.INDEX_ONE_POCKET

    for _ in range(7):
        pipeline._phase = _Phase.INDEX_ONE_POCKET
        pipeline._indexOnePocket(10.0)
        pipeline._indexOnePocket(10.1)

    assert pipeline._ledger.loads == ()
    assert pipeline.transport.getPieceForDistributionDrop() is not None
    assert len(pipeline._stepper.moves) == 7


def test_two_piece_pocket_locks_bottom_reject_without_waiting_for_recognition() -> None:
    pipeline = _pipeline()
    pipeline._admit(1.0)
    load = pipeline._tail
    assert load is not None

    pipeline._forceReject(load, "multiple pieces", multi_piece=True)
    obj = load.payload.ctx.known_object
    assert load.route == PocketRoute.REJECT
    assert load.route_locked
    assert obj.part_id is None
    assert obj.classification_status.value == "multi_drop_fail"


def test_idle_drain_guards_once_then_indexes_all_remaining_loads() -> None:
    pipeline = _pipeline()
    pipeline._admit(1.0)
    load = pipeline._tail
    assert load is not None
    pipeline._forceReject(load, "test reject")

    # Finish the mandatory first index after capture/reject, leaving the load
    # six pockets from its exit and returning to intake wait.
    pipeline._phase = _Phase.INDEX_ONE_POCKET
    pipeline._indexOnePocket(2.0)
    pipeline._indexOnePocket(2.1)
    assert pipeline._phase == _Phase.WAIT_INTAKE
    assert len(pipeline._ledger.loads) == 1

    pipeline._last_intake_activity = 0.0
    pipeline._waitIntake(10.0)
    assert pipeline._drain_armed_at == 10.0
    assert not pipeline._drain_active
    pipeline._waitIntake(11.6)
    assert pipeline._drain_active
    assert pipeline._phase == _Phase.INDEX_ONE_POCKET

    # Each absolute index takes a start/settle pair in this synchronous fake.
    # The phase must remain INDEX_ONE_POCKET between indexes instead of routing
    # through WAIT_INTAKE and paying another drain guard.
    for index in range(5):
        pipeline._indexOnePocket(12.0 + index)
        pipeline._indexOnePocket(12.1 + index)
        assert pipeline._drain_active
        assert pipeline._phase == _Phase.INDEX_ONE_POCKET

    pipeline._indexOnePocket(18.0)
    pipeline._indexOnePocket(18.1)
    assert pipeline._ledger.loads == ()
    assert not pipeline._drain_active
    assert pipeline._drain_armed_at == 0.0
    assert pipeline._phase == _Phase.WAIT_INTAKE
    assert len(pipeline._stepper.moves) == 7


def test_pause_resume_preserves_owned_pockets_and_freezes_deadlines() -> None:
    pipeline = _pipeline()
    pipeline._admit(time.monotonic())
    load = pipeline._tail
    assert load is not None
    pipeline._forceReject(load, "test reject")
    original_admitted_at = load.admitted_at_mono

    pipeline.pause()
    assert pipeline._ledger.loads == (load,)
    assert pipeline._phase == _Phase.CAPTURE_TAIL
    assert not pipeline.shared.classification_ready

    # Model a long operator pause without sleeping in the test.  Resume must
    # shift the capture deadline and retain the exact same pocket payload.
    pipeline._paused_at_mono -= 60.0
    pipeline._paused_at_wall -= 60.0
    pipeline.resume()

    assert pipeline._ledger.loads == (load,)
    assert pipeline._tail is load
    assert pipeline._phase == _Phase.CAPTURE_TAIL
    assert load.admitted_at_mono >= original_admitted_at + 59.0


def test_full_cleanup_still_discards_indexed_state_for_rehome() -> None:
    pipeline = _pipeline()
    pipeline._admit(time.monotonic())

    pipeline.cleanup()

    assert pipeline._ledger.loads == ()
    assert pipeline._tail is None
    assert pipeline._phase == _Phase.WAIT_INTAKE
