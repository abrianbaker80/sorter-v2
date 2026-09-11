from types import SimpleNamespace

import numpy as np

from defs.known_object import ClassificationStatus, KnownObject, PieceStage
from piece_transport import ClassificationChannelTransport
from subsystems.classification_channel.simple_state_machine_rev01.awaiting_distribution import (
    AwaitingDistribution,
)
from subsystems.classification_channel.simple_state_machine_rev01.context import (
    SimpleStateMachineRev01Context,
)
from subsystems.classification_channel.simple_state_machine_rev01.constants import (
    C4_TRAVEL_SIGN,
)
from subsystems.classification_channel.five_sector_platter import C4FiveSectorPlatter
from subsystems.classification_channel.simple_state_machine_rev01.discharging import (
    Discharging,
)
from subsystems.classification_channel.simple_state_machine_rev01.moving_to_precise import (
    MovingToPrecise,
)
from subsystems.classification_channel.states import ClassificationChannelState
from perception.state import ChannelState, PieceObservation

from subsystems.classification_channel.simple_state_machine_rev01.capturing import (
    Capturing,
)
from subsystems.classification_channel.simple_state_machine_rev01.idle import Idle
from subsystems.classification_channel.simple_state_machine_rev01.rev01_config import (
    Rev01Config,
    configFromDict,
)
from subsystems.common.jitter_recovery import JitterPhase
from subsystems.classification_channel.simple_state_machine_rev01.vision import (
    Rev01Vision,
)

def test_rev01_crop_bbox_uses_exact_bounds() -> None:
    frame = np.arange(6 * 8 * 3, dtype=np.uint8).reshape((6, 8, 3))

    crop = Rev01Vision.cropBbox(frame, (2, 1, 6, 5))

    assert crop is not None
    assert crop.shape == (4, 4, 3)
    assert np.array_equal(crop, frame[1:5, 2:6])


def test_rev01_select_recognition_crops_keeps_even_spread() -> None:
    crops = [np.full((2, 2, 3), idx, dtype=np.uint8) for idx in range(12)]

    capturing = Capturing.__new__(Capturing)
    capturing.ctx = type("Ctx", (), {"config": Rev01Config(max_captures=8)})()
    selected = capturing.selectRecognitionCrops(crops)

    assert len(selected) == 8
    selected_ids = [int(crop[0, 0, 0]) for crop in selected]
    assert selected_ids == [0, 2, 3, 5, 6, 8, 9, 11]


def test_rev01_select_recognition_crops_caps_at_brickognize_limit() -> None:
    # max_captures above the 8-image Brickognize cap must still yield 8.
    crops = [np.full((2, 2, 3), idx, dtype=np.uint8) for idx in range(12)]

    capturing = Capturing.__new__(Capturing)
    capturing.ctx = type("Ctx", (), {"config": Rev01Config(max_captures=12)})()

    assert len(capturing.selectRecognitionCrops(crops)) == 8


def test_rev01_config_parses_capture_sweep_output_deg() -> None:
    cfg = configFromDict({"capture_sweep_output_deg": 135.5})

    assert cfg.capture_sweep_output_deg == 135.5


def test_rev01_config_parses_kick_off_output_deg() -> None:
    cfg = configFromDict({"kick_off_output_deg": 180.0})

    assert cfg.kick_off_output_deg == 180.0


def test_rev01_config_parses_verify_discharge_fields() -> None:
    cfg = configFromDict(
        {
            "verify_discharge_wait_ms": 750,
            "verify_discharge_max_jitter_attempts": 5,
        }
    )

    assert cfg.verify_discharge_wait_ms == 750
    assert cfg.verify_discharge_max_jitter_attempts == 5


class _Logger:
    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass


class _Stepper:
    def __init__(self) -> None:
        self.stopped = True
        self.position = 0
        self.moves: list[int] = []
        self.speed_limits: list[tuple[int, int]] = []

    def set_speed_limits(self, min_speed: int, max_speed: int) -> None:
        self.speed_limits.append((int(min_speed), int(max_speed)))

    def move_steps(self, steps: int) -> bool:
        self.moves.append(int(steps))
        self.position += int(steps)
        self.stopped = True
        return True

    def move_at_speed(self, speed: int) -> bool:
        self.stopped = int(speed) == 0
        return True


class _Vision:
    def getClassificationChannelDetectionCandidates(self):
        return []

    def getCarouselPolygon(self):
        return [[0, 0], [1, 0], [1, 1], [0, 1]]


class _Perception:
    def __init__(self, state: ChannelState) -> None:
        self.state = state

    def read_state(self, channel: int) -> ChannelState:
        assert channel == 4
        return self.state


class _Shared:
    def __init__(self) -> None:
        self.classification_ready = True
        self.distribution_ready = False

    def get_classification_ready(self) -> bool:
        return self.classification_ready

    def set_classification_gate(self, ready: bool, **kwargs) -> None:
        self.classification_ready = bool(ready)


class _RuntimeStats:
    def __init__(self) -> None:
        self.incident = None
        self.auto_resolved: list[dict] = []

    def activeIncident(self):
        return self.incident

    def setActiveIncident(self, incident) -> None:
        self.incident = incident

    def recordAutoResolvedIncident(self, incident, *, resolved_by="auto") -> None:
        self.auto_resolved.append({**incident, "resolved_by": resolved_by})

    def observePerfMs(self, *args, **kwargs) -> None:
        pass


class _Transport:
    def __init__(self) -> None:
        self.placed: list[KnownObject] = []
        self.advance_count = 0

    def placePieceForDistribution(self, piece: KnownObject) -> None:
        self.placed.append(piece)

    def advanceTransport(self) -> None:
        self.advance_count += 1


class _Clock:
    def __init__(self, wall: float, monotonic: float) -> None:
        self.wall = wall
        self.monotonic_value = monotonic

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_value

    def perf_counter(self) -> float:
        return self.monotonic_value


def _c4_state(
    ts: float,
    gap: float | None,
    *,
    in_precise: bool = False,
    n_pieces: int = 1,
    in_falloff: bool = False,
    discharge_gap: float | None = None,
    in_drop: bool = False,
    confirmed_track_id: int | None = None,
    tracker_available: bool = False,
) -> ChannelState:
    zone_code = 3 if in_precise else 2 if in_falloff else 1 if in_drop else 0
    pieces = (
        PieceObservation(
            com_forward_to_exit_deg=float(gap or 0.0),
            com_section=0,
            zone_code=zone_code,
            bbox=(10, 10, 20, 20),
            sv_bt_track_id=confirmed_track_id,
        ),
    ) if n_pieces > 0 else ()
    return ChannelState(
        ts=ts,
        in_drop=in_drop,
        in_exit=in_precise or in_falloff,
        n_pieces=n_pieces,
        n_confirmed_pieces=(
            (1 if confirmed_track_id is not None else 0)
            if tracker_available
            else None
        ),
        in_precise=in_precise,
        in_exit_majority=in_falloff,
        exit_com_forward_deg=discharge_gap,
        exit_com_forward_to_center_deg=discharge_gap,
        exit_com_forward_to_precise_deg=gap,
        exit_com_in_precise=in_precise,
        pieces=pieces,
    )


def _irl_config() -> SimpleNamespace:
    return SimpleNamespace(
        classification_channel_config=SimpleNamespace(
            c4_sector_count=10,
            drop_angle_deg=30.0,
            drop_tolerance_deg=14.0,
        ),
        c_channel_4_rotor_stepper=SimpleNamespace(microsteps=8),
    )


def _context(config: Rev01Config | None = None) -> SimpleStateMachineRev01Context:
    ctx = SimpleStateMachineRev01Context()
    ctx.config = config or Rev01Config()
    ctx.known_object = KnownObject()
    return ctx


def _arm_indexed_route(
    ctx: SimpleStateMachineRev01Context,
    stepper: _Stepper,
    *,
    start_sector: int = 0,
) -> None:
    platter = C4FiveSectorPlatter.from_irl_config(_irl_config())
    ctx.c4_cycle_start_sector = start_sector
    ctx.c4_safe_staging_target_steps = platter.sector_position_microsteps(
        start_sector + 5
    )
    ctx.c4_exit_target_steps = platter.sector_position_microsteps(start_sector + 7)
    ctx.precise_staged = True
    ctx.precise_staged_frame_ts = 999.9
    ctx.discharge_armed_frame_ts = 999.9
    stepper.position = ctx.c4_safe_staging_target_steps


def _gc(perception: _Perception) -> tuple[SimpleNamespace, _RuntimeStats]:
    stats = _RuntimeStats()
    return (
        SimpleNamespace(
            logger=_Logger(),
            perception_service=perception,
            runtime_stats=stats,
            profiler=SimpleNamespace(observeValue=lambda *args, **kwargs: None),
        ),
        stats,
    )


def _idle(
    perception: _Perception,
    config: Rev01Config | None = None,
    stepper: _Stepper | None = None,
) -> Idle:
    gc, _stats = _gc(perception)
    return Idle(
        irl=SimpleNamespace(carousel_stepper=stepper or _Stepper()),
        irl_config=_irl_config(),
        gc=gc,
        shared=_Shared(),
        transport=_Transport(),
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=_context(config),
    )


def test_rev01_idle_counts_distinct_confirmed_frames_not_coordinator_ticks() -> None:
    perception = _Perception(
        _c4_state(
            1000.0,
            12.0,
            in_drop=True,
            confirmed_track_id=7,
            tracker_available=True,
        )
    )
    state = _idle(perception, Rev01Config(presence_streak_to_start=2))

    for _ in range(10):
        assert state.step() is None

    perception.state = _c4_state(
        1000.1,
        12.0,
        in_drop=True,
        confirmed_track_id=7,
        tracker_available=True,
    )
    assert state.step() == ClassificationChannelState.REV01_CAPTURING


def test_rev01_idle_tentative_hardware_detection_cannot_start_cycle() -> None:
    perception = _Perception(
        _c4_state(
            1000.0,
            12.0,
            in_drop=True,
            tracker_available=True,
        )
    )
    state = _idle(perception, Rev01Config(presence_streak_to_start=2))

    for index in range(5):
        perception.state = _c4_state(
            1000.0 + index / 10.0,
            12.0,
            in_drop=True,
            tracker_available=True,
        )
        assert state.step() is None

    perception.state = _c4_state(
        1000.5,
        12.0,
        in_drop=True,
        confirmed_track_id=9,
        tracker_available=True,
    )
    assert state.step() == ClassificationChannelState.REV01_CAPTURING


def test_rev01_idle_confirmed_mid_channel_detection_cannot_start_cycle() -> None:
    perception = _Perception(
        _c4_state(
            1000.0,
            12.0,
            confirmed_track_id=11,
            tracker_available=True,
        )
    )
    state = _idle(perception, Rev01Config(presence_streak_to_start=2))

    for index in range(5):
        perception.state = _c4_state(
            1000.0 + index / 10.0,
            12.0,
            confirmed_track_id=11,
            tracker_available=True,
        )
        assert state.step() is None
        assert state.shared.classification_ready is False


def test_rev01_idle_clear_confirmation_counts_distinct_frames() -> None:
    perception = _Perception(_c4_state(1000.0, None, n_pieces=0))
    state = _idle(perception, Rev01Config(idle_clear_confirm_reads=3))

    for _ in range(10):
        assert state.step() is None
        assert state.shared.classification_ready is False

    perception.state = _c4_state(1000.1, None, n_pieces=0)
    assert state.step() is None
    assert state.shared.classification_ready is False
    perception.state = _c4_state(1000.2, None, n_pieces=0)
    assert state.step() is None
    assert state.shared.classification_ready is True


def test_rev01_idle_returns_c4_to_absolute_pocket_before_opening_c3(
    monkeypatch,
) -> None:
    import subsystems.classification_channel.simple_state_machine_rev01.idle as idle_module

    clock = _Clock(wall=1000.0, monotonic=10.0)
    monkeypatch.setattr(idle_module, "time", clock)
    stepper = _Stepper()
    stepper.position = 500
    perception = _Perception(_c4_state(1000.0, None, n_pieces=0))
    state = _idle(
        perception,
        Rev01Config(idle_clear_confirm_reads=3),
        stepper,
    )

    assert state.step() is None
    assert stepper.moves == [-500]
    assert state.shared.classification_ready is False

    # The command acknowledgement is not completion evidence.  Even though the
    # fake motor stops immediately, the same perception frame cannot reopen C3.
    assert state.step() is None
    assert stepper.moves == [-500]
    assert state.shared.classification_ready is False

    for index in range(1, 4):
        clock.wall = 1000.0 + index / 10.0
        clock.monotonic_value = 10.0 + index / 10.0
        perception.state = _c4_state(clock.wall, None, n_pieces=0)
        assert state.step() is None

    assert stepper.position == 0
    assert stepper.moves == [-500]
    assert state.shared.classification_ready is True


def test_rev01_moving_to_precise_uses_one_absolute_five_pocket_move(monkeypatch) -> None:
    import subsystems.classification_channel.simple_state_machine_rev01.moving_to_precise as moving_module

    clock = _Clock(wall=1000.0, monotonic=10.0)
    monkeypatch.setattr(moving_module, "time", clock)
    stepper = _Stepper()
    perception = _Perception(_c4_state(1000.0, 200.0))
    ctx = _context()
    irl_config = _irl_config()
    state = MovingToPrecise(
        irl=SimpleNamespace(carousel_stepper=stepper),
        irl_config=irl_config,
        gc=_gc(perception)[0],
        shared=_Shared(),
        transport=_Transport(),
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() is None
    platter = C4FiveSectorPlatter.from_irl_config(irl_config)
    assert stepper.moves == [platter.sector_position_microsteps(5)]
    assert ctx.c4_safe_staging_target_steps == platter.sector_position_microsteps(5)
    assert ctx.c4_exit_target_steps == platter.sector_position_microsteps(7)

    # A frame captured while moving cannot prove staging.
    stepper.stopped = False
    clock.wall = 1000.05
    clock.monotonic_value = 10.05
    perception.state = _c4_state(1000.04, 0.0)
    assert state.step() is None

    # Stopping establishes the post-move frame barrier; the same frame remains
    # ineligible and no visual correction move is ever issued.
    stepper.stopped = True
    clock.wall = 1000.10
    clock.monotonic_value = 10.10
    assert state.step() is None
    assert state.step() is None
    perception.state = _c4_state(1000.11, None, n_pieces=1)
    assert state.step() == ClassificationChannelState.REV01_AWAITING_DISTRIBUTION
    assert stepper.moves == [platter.sector_position_microsteps(5)]
    assert ctx.precise_staged


def test_rev01_moving_to_precise_starts_chute_while_c4_is_moving(monkeypatch) -> None:
    import subsystems.classification_channel.simple_state_machine_rev01.moving_to_precise as moving_module

    clock = _Clock(wall=1000.0, monotonic=10.0)
    monkeypatch.setattr(moving_module, "time", clock)
    stepper = _Stepper()
    perception = _Perception(_c4_state(1000.0, 200.0))
    ctx = _context()
    transport = _Transport()
    state = MovingToPrecise(
        irl=SimpleNamespace(carousel_stepper=stepper),
        irl_config=_irl_config(),
        gc=_gc(perception)[0],
        shared=_Shared(),
        transport=transport,
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() is None
    stepper.stopped = False
    ctx.classify_started_at = 9.0
    ctx.classification_result = {"items": [], "colors": []}
    clock.monotonic_value = 10.2

    assert state.step() is None
    assert transport.placed == [ctx.known_object]
    assert ctx.classification_applied
    assert ctx.distribution_placed
    assert not stepper.stopped

    # Coordinator ticks cannot apply or enqueue the same piece twice.
    assert state.step() is None
    assert transport.placed == [ctx.known_object]


def test_rev01_moving_to_precise_rejects_unaligned_cycle_origin() -> None:
    stepper = _Stepper()
    stepper.position = 100
    perception = _Perception(_c4_state(1000.0, 200.0))
    gc, stats = _gc(perception)
    ctx = _context()
    state = MovingToPrecise(
        irl=SimpleNamespace(carousel_stepper=stepper),
        irl_config=_irl_config(),
        gc=gc,
        shared=_Shared(),
        transport=_Transport(),
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() is None
    assert stepper.moves == []
    assert stats.incident["kind"] == "classification_track_lost"
    assert "absolute pocket boundary" in stats.incident["reason"]


def test_rev01_moving_to_precise_timeout_routes_to_auto_reject(monkeypatch) -> None:
    import subsystems.classification_channel.simple_state_machine_rev01.moving_to_precise as moving_module

    clock = _Clock(wall=1000.0, monotonic=10.0)
    monkeypatch.setattr(moving_module, "time", clock)
    stepper = _Stepper()
    perception = _Perception(_c4_state(1000.0, 12.0))
    gc, stats = _gc(perception)
    ctx = _context(Rev01Config(rotate_timeout_s=1.0))
    state = MovingToPrecise(
        irl=SimpleNamespace(carousel_stepper=stepper),
        irl_config=_irl_config(),
        gc=gc,
        shared=_Shared(),
        transport=_Transport(),
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() is None
    stepper.stopped = False
    clock.monotonic_value = 11.1
    assert state.step() == ClassificationChannelState.REV01_DISCHARGING

    assert stepper.stopped
    assert not ctx.precise_staged
    assert stats.incident is None
    assert "indexed safe staging point" in str(ctx.auto_reject_reason)


def test_rev01_discharging_consumes_staging_auto_reject_without_normal_route(
    monkeypatch,
) -> None:
    import subsystems.classification_channel.simple_state_machine_rev01.auto_reject as auto_reject_module

    monkeypatch.setattr(
        auto_reject_module,
        "clearChannelByAdvancing",
        lambda *args, **kwargs: SimpleNamespace(
            cleared=True,
            occupied_at_start=True,
            output_deg_moved=72.0,
            reason="cleared",
        ),
    )
    perception = _Perception(_c4_state(1000.0, None, n_pieces=1))
    gc, stats = _gc(perception)
    gc.disable_servos = True
    ctx = _context()
    ctx.auto_reject_reason = "C4 staging move timed out"
    obj = ctx.known_object
    assert obj is not None
    obj.part_id = "3623"
    obj.classification_status = ClassificationStatus.classified
    transport = ClassificationChannelTransport()
    shared = _Shared()
    state = Discharging(
        irl=SimpleNamespace(carousel_stepper=_Stepper(), servos=[]),
        irl_config=_irl_config(),
        gc=gc,
        shared=shared,
        transport=transport,
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() is None
    assert ctx.auto_reject_reason is None
    assert transport.getPieceForDistributionPositioning() is obj
    assert obj.part_id is None
    assert obj.classification_status == ClassificationStatus.unknown

    obj.stage = PieceStage.distributing
    obj.destination_bin = None
    shared.distribution_ready = True
    assert state.step() is None
    assert state.step() == ClassificationChannelState.IDLE

    assert transport.getPieceForDistributionDrop() is obj
    assert stats.incident is None
    assert len(stats.auto_resolved) == 1
    assert stats.auto_resolved[0]["resolution"] == (
        "auto_rejected_to_bottom_bin_after_c4_clear"
    )


def test_rev01_awaiting_distribution_requires_staging_proof() -> None:
    perception = _Perception(_c4_state(1000.0, 0.0, in_precise=True))
    gc, stats = _gc(perception)
    ctx = _context()
    transport = _Transport()
    state = AwaitingDistribution(
        irl=SimpleNamespace(carousel_stepper=_Stepper()),
        irl_config=_irl_config(),
        gc=gc,
        shared=_Shared(),
        transport=transport,
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() is None
    assert transport.placed == []
    assert stats.incident["kind"] == "classification_track_lost"
    assert "without indexed staging proof" in stats.incident["reason"]


def test_rev01_awaiting_distribution_finishes_slow_classification_once(
    monkeypatch,
) -> None:
    import subsystems.classification_channel.simple_state_machine_rev01.awaiting_distribution as awaiting_module

    clock = _Clock(wall=1000.0, monotonic=10.0)
    monkeypatch.setattr(awaiting_module, "time", clock)
    perception = _Perception(_c4_state(999.9, None, n_pieces=1))
    gc, _stats = _gc(perception)
    ctx = _context()
    ctx.precise_staged = True
    ctx.precise_staged_frame_ts = 999.9
    transport = _Transport()
    state = AwaitingDistribution(
        irl=SimpleNamespace(carousel_stepper=_Stepper()),
        irl_config=_irl_config(),
        gc=gc,
        shared=_Shared(),
        transport=transport,
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )
    assert state.step() is None
    assert transport.placed == []

    ctx.classify_started_at = 9.0
    ctx.classification_result = {"items": [], "colors": []}
    clock.monotonic_value = 10.1
    assert state.step() is None
    assert transport.placed == [ctx.known_object]
    assert ctx.classification_applied
    assert ctx.distribution_placed

    assert state.step() is None
    assert transport.placed == [ctx.known_object]


def test_rev01_awaiting_distribution_continues_immediately_when_chute_ready(
) -> None:
    perception = _Perception(_c4_state(999.9, None, n_pieces=1))
    gc, _stats = _gc(perception)
    ctx = _context()
    ctx.precise_staged = True
    ctx.precise_staged_frame_ts = 999.9
    ctx.classification_applied = True
    ctx.distribution_placed = True
    ctx.known_object.stage = PieceStage.distributing
    shared = _Shared()
    shared.distribution_ready = True
    state = AwaitingDistribution(
        irl=SimpleNamespace(carousel_stepper=_Stepper()),
        irl_config=_irl_config(),
        gc=gc,
        shared=shared,
        transport=_Transport(),
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() == ClassificationChannelState.REV01_DISCHARGING
    assert ctx.discharge_armed_frame_ts == 999.9


def test_rev01_awaiting_distribution_waits_only_for_chute() -> None:
    perception = _Perception(_c4_state(999.9, None, n_pieces=1))
    ctx = _context()
    ctx.precise_staged = True
    ctx.precise_staged_frame_ts = 999.9
    ctx.classification_applied = True
    ctx.distribution_placed = True
    ctx.known_object.stage = PieceStage.distributing
    shared = _Shared()
    state = AwaitingDistribution(
        irl=SimpleNamespace(carousel_stepper=_Stepper()),
        irl_config=_irl_config(),
        gc=_gc(perception)[0],
        shared=shared,
        transport=_Transport(),
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() is None
    shared.distribution_ready = True
    assert state.step() == ClassificationChannelState.REV01_DISCHARGING


def test_rev01_discharging_requires_motion_then_distinct_empty_frames(
    monkeypatch,
) -> None:
    import subsystems.classification_channel.simple_state_machine_rev01.discharging as discharging_module

    clock = _Clock(wall=1000.0, monotonic=10.0)
    monkeypatch.setattr(discharging_module, "time", clock)
    stepper = _Stepper()
    perception = _Perception(_c4_state(1000.0, None, n_pieces=1))
    gc, _stats = _gc(perception)
    ctx = _context(
        Rev01Config(
            capture_at_rest_ms=0.0,
            discharge_clear_confirm_ms=500,
            discharge_jitter_dwell_ms=5000,
        )
    )
    _arm_indexed_route(ctx, stepper)
    transport = _Transport()
    state = Discharging(
        irl=SimpleNamespace(carousel_stepper=stepper),
        irl_config=_irl_config(),
        gc=gc,
        shared=_Shared(),
        transport=transport,
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() is None
    assert stepper.moves == [
        ctx.c4_exit_target_steps - ctx.c4_safe_staging_target_steps
    ]
    assert transport.advance_count == 0
    assert state.step() is None  # establishes the post-motion settle barrier
    assert len(stepper.moves) == 1  # the same frame cannot repeat the command

    clock.wall = 1000.1
    clock.monotonic_value = 10.1
    perception.state = _c4_state(1000.1, None, n_pieces=0)
    assert state.step() is None
    assert transport.advance_count == 0

    # Re-reading one empty slot while coordinator ticks cannot manufacture the
    # 500 ms proof window.
    clock.wall = 1001.0
    clock.monotonic_value = 11.0
    assert state.step() is None
    assert transport.advance_count == 0

    perception.state = _c4_state(1000.7, None, n_pieces=0)
    assert state.step() == ClassificationChannelState.IDLE
    assert transport.advance_count == 1


def test_rev01_discharging_requires_an_absolute_indexed_route() -> None:
    perception = _Perception(_c4_state(1000.0, None, n_pieces=0))
    gc, stats = _gc(perception)
    ctx = _context(Rev01Config(empty_streak_to_abort=3))
    ctx.discharge_armed_frame_ts = 999.0
    transport = _Transport()
    state = Discharging(
        irl=SimpleNamespace(carousel_stepper=_Stepper()),
        irl_config=_irl_config(),
        gc=gc,
        shared=_Shared(),
        transport=transport,
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() is None

    assert transport.advance_count == 0
    assert stats.incident["kind"] == "classification_track_lost"
    assert "without an absolute indexed route" in stats.incident["reason"]


def test_rev01_discharging_never_credits_while_any_c4_detection_remains(
    monkeypatch,
) -> None:
    import subsystems.classification_channel.simple_state_machine_rev01.discharging as discharging_module

    clock = _Clock(wall=1000.0, monotonic=10.0)
    monkeypatch.setattr(discharging_module, "time", clock)
    stepper = _Stepper()
    perception = _Perception(
        _c4_state(
            1000.0,
            0.0,
            in_falloff=True,
            discharge_gap=8.0,
        )
    )
    ctx = _context(
        Rev01Config(
            capture_at_rest_ms=0.0,
            discharge_clear_confirm_ms=500,
            discharge_jitter_dwell_ms=5000,
        )
    )
    _arm_indexed_route(ctx, stepper)
    transport = _Transport()
    state = Discharging(
        irl=SimpleNamespace(carousel_stepper=stepper),
        irl_config=_irl_config(),
        gc=_gc(perception)[0],
        shared=_Shared(),
        transport=transport,
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() is None
    assert len(stepper.moves) == 1
    assert state.step() is None  # establish the zero-ms post-move barrier

    # A persistent detection elsewhere on C4 is not a successful one-piece drop.
    clock.wall = 1000.1
    clock.monotonic_value = 10.1
    perception.state = _c4_state(1000.1, None, n_pieces=1, in_drop=True)
    assert state.step() is None
    clock.wall = 1000.7
    clock.monotonic_value = 10.7
    perception.state = _c4_state(1000.7, None, n_pieces=1, in_drop=True)
    assert state.step() is None
    assert transport.advance_count == 0


def test_rev01_discharging_uses_only_indexed_move_then_times_out_if_occupied(
    monkeypatch,
) -> None:
    import subsystems.classification_channel.simple_state_machine_rev01.discharging as discharging_module

    clock = _Clock(wall=1000.0, monotonic=10.0)
    monkeypatch.setattr(discharging_module, "time", clock)
    stepper = _Stepper()
    perception = _Perception(_c4_state(1000.0, None, n_pieces=1))
    gc, stats = _gc(perception)
    ctx = _context(Rev01Config(discharge_total_timeout_ms=1000))
    _arm_indexed_route(ctx, stepper)
    transport = _Transport()
    state = Discharging(
        irl=SimpleNamespace(carousel_stepper=stepper),
        irl_config=_irl_config(),
        gc=gc,
        shared=_Shared(),
        transport=transport,
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() is None
    assert len(stepper.moves) == 1
    clock.monotonic_value = 11.1
    assert state.step() is None

    assert len(stepper.moves) == 1
    assert transport.advance_count == 0
    assert stats.incident is None
    assert state._auto_reject.active
    assert transport.placed == [ctx.known_object]


def test_rev01_discharging_track_loss_cancels_positioning_without_credit() -> None:
    perception = _Perception(_c4_state(1000.0, None, n_pieces=0))
    ctx = _context()
    ctx.distribution_placed = True
    transport = ClassificationChannelTransport()
    transport.placePieceForDistribution(ctx.known_object)
    state = Discharging(
        irl=SimpleNamespace(carousel_stepper=_Stepper()),
        irl_config=_irl_config(),
        gc=_gc(perception)[0],
        shared=_Shared(),
        transport=transport,
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )
    state._faulted = True

    assert state.canRecoverLostTrack(ctx.known_object.uuid)
    assert state.abandonLostTrackObject("test track loss")
    assert ctx.known_object.aborted
    assert transport.getPieceForDistributionPositioning() is None
    assert transport.getPieceForDistributionDrop() is None


def test_rev01_discharging_multi_feed_auto_rejects_then_resumes(
    monkeypatch,
) -> None:
    import subsystems.classification_channel.simple_state_machine_rev01.discharging as discharging_module
    import subsystems.classification_channel.simple_state_machine_rev01.auto_reject as auto_reject_module

    clock = _Clock(wall=1000.0, monotonic=10.0)
    monkeypatch.setattr(discharging_module, "time", clock)
    clear_calls: list[float] = []

    def _clear(*args, **kwargs):
        clear_calls.append(float(kwargs["max_output_deg"]))
        return SimpleNamespace(
            cleared=True,
            occupied_at_start=True,
            output_deg_moved=36.0,
            reason="cleared",
        )

    monkeypatch.setattr(auto_reject_module, "clearChannelByAdvancing", _clear)

    stepper = _Stepper()
    perception = _Perception(_c4_state(1000.0, None, n_pieces=1))
    gc, stats = _gc(perception)
    gc.disable_servos = True
    ctx = _context(
        Rev01Config(
            capture_at_rest_ms=0.0,
            discharge_clear_confirm_ms=0,
            discharge_jitter_dwell_ms=5000,
            multi_feed_confirm_reads=3,
        )
    )
    _arm_indexed_route(ctx, stepper)
    obj = ctx.known_object
    assert obj is not None
    obj.part_id = "15712"
    obj.part_name = "Tile"
    obj.color_id = "5"
    obj.color_name = "Red"
    obj.classification_status = ClassificationStatus.classified
    obj.stage = PieceStage.distributing
    obj.destination_bin = (1, 3, 2)
    transport = ClassificationChannelTransport()
    transport.placePieceForDistribution(obj)
    ctx.distribution_placed = True
    shared = _Shared()
    shared.distribution_ready = True
    state = Discharging(
        irl=SimpleNamespace(carousel_stepper=stepper, servos=[]),
        irl_config=_irl_config(),
        gc=gc,
        shared=shared,
        transport=transport,
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() is None  # indexed exit move
    assert state.step() is None  # post-motion frame barrier
    for offset in (0.1, 0.2, 0.3):
        clock.wall = 1000.0 + offset
        clock.monotonic_value = 10.0 + offset
        perception.state = _c4_state(1000.0 + offset, None, n_pieces=2)
        assert state.step() is None

    assert stats.incident is None
    assert transport.getPieceForDistributionPositioning() is None
    assert transport.isCanceledPieceForDistribution(obj.uuid)
    assert transport.getPieceForDistributionDrop() is None

    # The distribution READY state acknowledges cancellation on the next
    # coordinator tick; only then may this same object start a reject route.
    assert transport.consumeCanceledPieceForDistribution(obj.uuid)
    clock.wall = 1000.31
    clock.monotonic_value = 10.31
    assert state.step() is None
    assert transport.getPieceForDistributionPositioning() is obj
    assert obj.classification_status == ClassificationStatus.multi_drop_fail
    assert obj.stage == PieceStage.created
    assert obj.part_id is None

    # Simulate normal distribution selecting its existing all-doors-open
    # passthrough route and reporting it ready.
    obj.stage = PieceStage.distributing
    obj.destination_bin = None
    shared.distribution_ready = True
    clock.wall = 1000.32
    clock.monotonic_value = 10.32
    assert state.step() is None
    clock.wall = 1000.33
    clock.monotonic_value = 10.33
    assert state.step() == ClassificationChannelState.IDLE
    assert clear_calls == [360.0]
    assert transport.getPieceForDistributionDrop() is obj
    assert stats.incident is None
    assert len(stats.auto_resolved) == 1
    assert stats.auto_resolved[0]["resolution"] == (
        "auto_rejected_to_bottom_bin_after_c4_clear"
    )


def test_rev01_discharging_auto_reject_clear_failure_holds_without_credit(
    monkeypatch,
) -> None:
    import subsystems.classification_channel.simple_state_machine_rev01.discharging as discharging_module
    import subsystems.classification_channel.simple_state_machine_rev01.auto_reject as auto_reject_module

    clock = _Clock(wall=1000.0, monotonic=10.0)
    monkeypatch.setattr(discharging_module, "time", clock)
    monkeypatch.setattr(
        auto_reject_module,
        "clearChannelByAdvancing",
        lambda *args, **kwargs: SimpleNamespace(
            cleared=False,
            occupied_at_start=True,
            output_deg_moved=360.0,
            reason="budget_exhausted",
        ),
    )
    perception = _Perception(_c4_state(1000.0, None, n_pieces=2))
    gc, stats = _gc(perception)
    gc.disable_servos = True
    ctx = _context()
    obj = ctx.known_object
    assert obj is not None
    obj.part_id = "15712"
    obj.classification_status = ClassificationStatus.classified
    obj.stage = PieceStage.distributing
    obj.destination_bin = (1, 3, 2)
    transport = ClassificationChannelTransport()
    transport.placePieceForDistribution(obj)
    ctx.distribution_placed = True
    shared = _Shared()
    shared.distribution_ready = True
    state = Discharging(
        irl=SimpleNamespace(carousel_stepper=_Stepper(), servos=[]),
        irl_config=_irl_config(),
        gc=gc,
        shared=shared,
        transport=transport,
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    state._beginAutoReject("test discharge evidence loss", multi_piece=True)
    assert transport.consumeCanceledPieceForDistribution(obj.uuid)
    assert state.step() is None  # requeue as reject
    obj.stage = PieceStage.distributing
    obj.destination_bin = None
    assert state.step() is None  # reject route ready
    assert state.step() is None  # failed full-turn clear -> incident

    assert transport.getPieceForDistributionDrop() is None
    assert stats.incident["kind"] == "classification_track_lost"
    assert "automatic reject failed" in stats.incident["reason"]
    assert stats.auto_resolved == []


def test_rev01_discharging_timeout_preempts_an_active_jitter(monkeypatch) -> None:
    import subsystems.classification_channel.simple_state_machine_rev01.discharging as discharging_module

    class _ActiveJitter:
        is_active = True
        attempts_made = 1

        def tick(self, **kwargs):
            raise AssertionError("expired jitter must not be ticked")

    clock = _Clock(wall=1000.0, monotonic=10.0)
    monkeypatch.setattr(discharging_module, "time", clock)
    perception = _Perception(_c4_state(1000.0, None, n_pieces=1))
    gc, stats = _gc(perception)
    ctx = _context(Rev01Config(discharge_total_timeout_ms=1000))
    stepper = _Stepper()
    _arm_indexed_route(ctx, stepper)
    transport = _Transport()
    state = Discharging(
        irl=SimpleNamespace(carousel_stepper=stepper),
        irl_config=_irl_config(),
        gc=gc,
        shared=_Shared(),
        transport=transport,
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )

    assert state.step() is None
    state._seq = _ActiveJitter()
    clock.monotonic_value = 11.1
    assert state.step() is None

    assert transport.advance_count == 0
    assert stats.incident is None
    assert state._auto_reject.active
    assert transport.placed == [ctx.known_object]


def test_rev01_discharging_jitter_retry_requires_a_distinct_settled_frame(
    monkeypatch,
) -> None:
    import subsystems.classification_channel.simple_state_machine_rev01.discharging as discharging_module

    class _PausedJitter:
        is_active = True
        phase = JitterPhase.PAUSE
        attempts_made = 1
        ticks = 0

        def tick(self, **kwargs):
            self.ticks += 1
            return JitterPhase.PAUSE

    clock = _Clock(wall=1000.0, monotonic=10.0)
    monkeypatch.setattr(discharging_module, "time", clock)
    perception = _Perception(_c4_state(1000.0, None, n_pieces=1))
    ctx = _context(Rev01Config(capture_at_rest_ms=0.0))
    ctx.discharge_armed_frame_ts = 999.0
    state = Discharging(
        irl=SimpleNamespace(carousel_stepper=_Stepper()),
        irl_config=_irl_config(),
        gc=_gc(perception)[0],
        shared=_Shared(),
        transport=_Transport(),
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=ctx,
    )
    jitter = _PausedJitter()
    state._seq = jitter
    state._motion_issued = True
    state._awaiting_post_move_frame = True

    assert state.step() is None  # establish the settle barrier
    perception.state = _c4_state(1000.1, None, n_pieces=1)
    clock.wall = 1000.1
    clock.monotonic_value = 10.1
    assert state.step() is None
    assert jitter.ticks == 1

    clock.wall = 1000.5
    clock.monotonic_value = 10.5
    assert state.step() is None
    assert jitter.ticks == 1  # repeated frame cannot authorize another retry


def test_rev01_discharging_legacy_fallback_fixed_kick_then_idle() -> None:
    # No perception_service on gc → legacy non-perception fallback: a single
    # fixed kick-off move, then (with zero settle pause) straight back to IDLE.
    stepper = _Stepper()
    state = Discharging(
        irl=SimpleNamespace(carousel_stepper=stepper),
        irl_config=SimpleNamespace(
            classification_channel_config=SimpleNamespace(
                drop_angle_deg=30.0,
                drop_tolerance_deg=14.0,
            ),
        ),
        gc=SimpleNamespace(logger=_Logger()),
        shared=SimpleNamespace(
            set_distribution_gate=lambda *args, **kwargs: None,
        ),
        transport=SimpleNamespace(advanceTransport=lambda *args, **kwargs: None),
        vision=_Vision(),
        event_queue=SimpleNamespace(put=lambda *args, **kwargs: None),
        context=SimpleNamespace(
            config=Rev01Config(
                kick_off_output_deg=180.0,
                discharge_speed_usteps_per_s=3000,
                post_discharge_pause_ms=0.0,
            ),
            discharging_started_at=0.0,
            known_object=KnownObject(),
        ),
    )

    next_state = state.step()

    # The configured C4 travel sign is preserved all the way to firmware. With
    # the default platter
    # (200 steps/rev * 8 microsteps * 130/12 gear ratio = 17333.33 µsteps/output_rev),
    # -180°/360 * 17333.33 ≈ -8667 microsteps.
    assert next_state == ClassificationChannelState.IDLE
    assert stepper.moves == [int(C4_TRAVEL_SIGN * 8667)]
