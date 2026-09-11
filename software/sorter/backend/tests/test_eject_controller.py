"""EjectController state-machine transitions (closed-loop eject + fall recovery).

Pure-logic tests: the controller is driven with synthetic ChannelStates, a fake
stepper, and recorded callbacks — no hardware, no perception workers. The
harness models a piece whose ACTUAL forward gap to the exit zone shrinks by only
a FRACTION of each commanded move (simulated slippage), so the closed-loop
re-measurement is exercised the way it is on the machine.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import Mock

from perception.cascade import Action
from perception.state import ChannelState, PieceObservation
from subsystems.feeder.go_to_angle.config import GoToAngleConfig
from subsystems.feeder.go_to_angle.eject import EjectController, EjectPhase
from subsystems.feeder.go_to_angle.flow import GoToAngleFeeding


class _SilentLogger:
    def info(self, *a, **k) -> None: ...
    def warning(self, *a, **k) -> None: ...
    def error(self, *a, **k) -> None: ...


class _FakeStepper:
    def __init__(self) -> None:
        self.jitter_calls = 0
        self._jittering = False

    def jitter_degrees(self, *args, **kwargs) -> bool:
        self.jitter_calls += 1
        self._jittering = True
        return True

    def is_jittering(self) -> bool:
        return self._jittering

    def finish_jitter(self) -> None:
        self._jittering = False


_MOVE_DURATION_S = 0.3


@dataclass
class _Harness:
    ctrl: EjectController
    stepper: _FakeStepper
    moves: list = field(default_factory=list)      # commanded step sizes (deg)
    successes: list = field(default_factory=list)
    now: list = field(default_factory=lambda: [100.0])
    gap: list = field(default_factory=lambda: [20.0])   # true forward gap (deg)
    move_done_at: list = field(default_factory=lambda: [0.0])
    slip: list = field(default_factory=lambda: [0.5])   # fraction of cmd actually moved
    accept_move: list = field(default_factory=lambda: [True])
    present: list = field(default_factory=lambda: [True])
    in_precise: list = field(default_factory=lambda: [True])  # COM in precise zone

    def advance(self, dt: float) -> None:
        self.now[0] += dt

    def state(self) -> ChannelState:
        g = self.gap[0]
        present = self.present[0]
        return ChannelState(
            ts=1.0,
            in_drop=False,
            in_exit=present and g <= 0.0,
            n_pieces=1 if present else 0,
            exit_com_forward_deg=(g if present else None),
            exit_com_in_precise=present and self.in_precise[0],
        )

    def tick(
        self,
        *,
        down: int = 0,
        ready: bool = True,
        cfg: GoToAngleConfig,
        state: ChannelState | None = None,
    ) -> bool:
        downstream = ChannelState(ts=1.0, in_drop=False, in_exit=False, n_pieces=down)
        return self.ctrl.tick(
            state=self.state() if state is None else state,
            downstream=downstream,
            downstream_ready=ready,
            cfg=cfg,
            now=self.now[0],
        )


def _make(start_gap: float = 20.0, slip: float = 0.5, in_precise: bool = True) -> _Harness:
    h = _Harness(ctrl=None, stepper=_FakeStepper())  # type: ignore[arg-type]
    h.gap[0] = start_gap
    h.slip[0] = slip
    h.in_precise[0] = in_precise

    def advance_move(step: float) -> bool:
        h.moves.append(step)
        if not h.accept_move[0]:
            return False
        h.gap[0] -= h.slip[0] * step
        h.move_done_at[0] = h.now[0] + _MOVE_DURATION_S
        return True

    h.ctrl = EjectController(
        channel_id=3,
        stepper=h.stepper,
        is_stopped=lambda: h.now[0] >= h.move_done_at[0],
        advance_move=advance_move,
        on_success=lambda: h.successes.append(h.now[0]),
        logger=_SilentLogger(),
    )
    return h


def _cfg(**over) -> GoToAngleConfig:
    cfg = GoToAngleConfig()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _tracked_state(track_id: int, gap: float) -> ChannelState:
    return ChannelState(
        ts=1.0,
        in_drop=False,
        in_exit=gap <= 0.0,
        n_pieces=1,
        exit_com_forward_deg=gap,
        exit_com_in_precise=True,
        pieces=(
            PieceObservation(
                com_forward_to_exit_deg=gap,
                com_section=0,
                zone_code=3 if gap > 0.0 else 2,
                sv_bt_track_id=track_id,
            ),
        ),
        n_confirmed_pieces=1,
    )


def _run_advance(h: _Harness, cfg: GoToAngleConfig, max_ticks: int = 60) -> None:
    """Tick until the controller leaves ADVANCING (or runs out of ticks),
    advancing time between ticks so each queued move 'completes'."""
    for _ in range(max_ticks):
        h.tick(cfg=cfg)
        if h.ctrl.phase != EjectPhase.ADVANCING:
            return
        h.advance(_MOVE_DURATION_S)


# --- IDLE -------------------------------------------------------------------


def test_idle_passes_when_piece_not_yet_in_precise_zone() -> None:
    # COM short of the precise zone (gap>0, not in precise) → controller must NOT
    # take the tick; the normal advance carries the piece in.
    h = _make(start_gap=40.0, in_precise=False)
    consumed = h.tick(cfg=_cfg())
    assert consumed is False
    assert h.ctrl.phase == EjectPhase.IDLE
    assert h.moves == []


def test_idle_triggers_when_com_in_precise_zone() -> None:
    # Same gap, but the COM is now in the precise zone → eject starts.
    h = _make(start_gap=40.0, in_precise=True)
    h.tick(cfg=_cfg())
    assert h.ctrl.phase == EjectPhase.ADVANCING


def test_idle_passes_when_no_piece() -> None:
    h = _make()
    h.present[0] = False
    assert h.tick(cfg=_cfg()) is False


def test_within_trigger_but_downstream_busy_holds() -> None:
    h = _make(start_gap=5.0)
    consumed = h.tick(down=0, ready=False, cfg=_cfg())
    assert consumed is True
    assert h.ctrl.phase == EjectPhase.IDLE  # holding, no move issued
    assert h.moves == []


# --- ADVANCING (closed loop + slippage) ------------------------------------


def test_advances_proportionally_and_reaches_exit() -> None:
    h = _make(start_gap=20.0, slip=0.5)
    cfg = _cfg(fast_eject_min_step_deg=2.0)
    _run_advance(h, cfg)
    assert h.ctrl.phase == EjectPhase.AWAITING_FALL
    assert h.gap[0] <= 0.0
    # Multiple moves were needed (slippage), and commanded steps shrank as the
    # measured gap closed — proving re-measurement each iteration.
    assert len(h.moves) >= 3
    assert h.moves[0] > h.moves[-1]


def test_advance_freezes_when_downstream_goes_busy() -> None:
    h = _make(start_gap=20.0)
    cfg = _cfg()
    h.tick(cfg=cfg)  # IDLE → ADVANCING, first move
    assert h.ctrl.phase == EjectPhase.ADVANCING
    n_before = len(h.moves)
    h.advance(_MOVE_DURATION_S)
    consumed = h.tick(ready=False, cfg=cfg)  # downstream busy mid-approach
    assert consumed is True
    assert len(h.moves) == n_before  # no new move while frozen


def test_downstream_arrival_during_advance_completes_without_another_move() -> None:
    h = _make(start_gap=20.0, slip=0.0)
    cfg = _cfg()
    h.tick(down=0, cfg=cfg)  # commit handoff and issue its first move
    assert h.ctrl.phase == EjectPhase.ADVANCING
    n_before = len(h.moves)

    h.advance(_MOVE_DURATION_S)
    # C4 sees the transferred piece while C3 now sees the following piece at a
    # positive gap.  The arrival must win; that following piece is not re-driven.
    h.gap[0] = 70.0
    consumed = h.tick(down=1, cfg=cfg)

    assert consumed is True
    assert h.ctrl.phase == EjectPhase.IDLE
    assert len(h.moves) == n_before
    assert len(h.successes) == 1


def test_tracked_handoff_does_not_adopt_following_piece() -> None:
    h = _make(start_gap=20.0, slip=0.0)
    cfg = _cfg(fall_confirm_timeout_ms=100)
    h.tick(down=0, cfg=cfg, state=_tracked_state(101, 20.0))
    assert h.ctrl.phase == EjectPhase.ADVANCING
    n_before = len(h.moves)

    # Track 101 has left C3; track 202 is now the leading local detection.
    h.advance(0.2)
    consumed = h.tick(down=0, cfg=cfg, state=_tracked_state(202, 70.0))

    assert consumed is True
    assert h.ctrl.phase == EjectPhase.AWAITING_FALL
    assert len(h.moves) == n_before
    assert h.stepper.jitter_calls == 0

    # Even beyond the old recovery timeout, the following piece remains still.
    h.advance(0.2)
    h.tick(down=0, cfg=cfg, state=_tracked_state(202, 70.0))
    assert h.ctrl.phase == EjectPhase.AWAITING_FALL
    assert len(h.moves) == n_before
    assert h.stepper.jitter_calls == 0

    h.tick(down=1, cfg=cfg, state=_tracked_state(202, 70.0))
    assert h.ctrl.phase == EjectPhase.IDLE
    assert len(h.successes) == 1


def test_advance_safety_cap_kicks_to_recovery() -> None:
    # Total slip (piece never moves) → gap never closes → safety cap fires.
    h = _make(start_gap=20.0, slip=0.0)
    cfg = _cfg(fast_eject_max_advance_iterations=3)
    _run_advance(h, cfg)
    assert h.ctrl.phase == EjectPhase.RECOVERING
    assert h.stepper.jitter_calls == 1


def test_rejected_advances_do_not_consume_stuck_budget() -> None:
    h = _make(start_gap=20.0, slip=0.0)
    h.accept_move[0] = False
    cfg = _cfg(fast_eject_max_advance_iterations=3)

    for _ in range(10):
        h.tick(cfg=cfg)

    assert h.ctrl.phase == EjectPhase.ADVANCING
    assert h.stepper.jitter_calls == 0

    h.accept_move[0] = True
    _run_advance(h, cfg)
    assert h.ctrl.phase == EjectPhase.RECOVERING
    assert h.stepper.jitter_calls == 1


# --- AWAITING_FALL ----------------------------------------------------------


def test_success_only_on_downstream_appearance() -> None:
    h = _make(start_gap=20.0)
    cfg = _cfg(fall_confirm_timeout_ms=700)
    _run_advance(h, cfg)
    assert h.ctrl.phase == EjectPhase.AWAITING_FALL
    # Piece still sits in our exit (gap<=0) but nothing downstream → NOT success.
    h.advance(0.1)
    h.tick(down=0, cfg=cfg)
    assert h.ctrl.phase == EjectPhase.AWAITING_FALL
    assert h.successes == []
    # Downstream count rises → success.
    consumed = h.tick(down=1, cfg=cfg)
    assert consumed is True
    assert h.ctrl.phase == EjectPhase.IDLE
    assert len(h.successes) == 1


def test_timeout_starts_jitter_recovery() -> None:
    h = _make(start_gap=20.0)
    cfg = _cfg(fall_confirm_timeout_ms=200, fall_recovery_max_jitter_attempts=2)
    _run_advance(h, cfg)
    assert h.ctrl.phase == EjectPhase.AWAITING_FALL
    h.advance(0.3)  # > timeout
    consumed = h.tick(down=0, cfg=cfg)
    assert consumed is True
    assert h.ctrl.phase == EjectPhase.RECOVERING
    assert h.stepper.jitter_calls == 1


# --- RECOVERING -------------------------------------------------------------


def test_recovery_success_on_downstream() -> None:
    h = _make(start_gap=20.0)
    cfg = _cfg(fall_confirm_timeout_ms=100, fall_recovery_max_jitter_attempts=3)
    _run_advance(h, cfg)
    h.advance(0.2)
    h.tick(down=0, cfg=cfg)  # → RECOVERING
    assert h.ctrl.phase == EjectPhase.RECOVERING
    consumed = h.tick(down=1, cfg=cfg)  # downstream appears mid-recovery
    assert consumed is True
    assert h.ctrl.phase == EjectPhase.IDLE
    assert len(h.successes) == 1


def test_recovery_reapproaches_if_piece_knocked_out_of_exit() -> None:
    h = _make(start_gap=20.0)
    cfg = _cfg(fall_confirm_timeout_ms=100)
    _run_advance(h, cfg)
    h.advance(0.2)
    h.tick(down=0, cfg=cfg)  # → RECOVERING
    assert h.ctrl.phase == EjectPhase.RECOVERING
    # Jitter knocked the piece back out of the exit zone (gap positive again).
    h.gap[0] = 6.0
    h.tick(down=0, cfg=cfg)
    assert h.ctrl.phase == EjectPhase.ADVANCING


def test_downstream_arrival_preempts_recovery_reapproach() -> None:
    h = _make(start_gap=20.0)
    cfg = _cfg(fall_confirm_timeout_ms=100)
    _run_advance(h, cfg)
    h.advance(0.2)
    h.tick(down=0, cfg=cfg)  # → RECOVERING
    assert h.ctrl.phase == EjectPhase.RECOVERING

    # The next C3 piece is now visible away from the exit in the same frame in
    # which C4 confirms the original transfer.  Do not re-approach that piece.
    h.gap[0] = 70.0
    consumed = h.tick(down=1, cfg=cfg)

    assert consumed is True
    assert h.ctrl.phase == EjectPhase.IDLE
    assert len(h.successes) == 1


def test_recovery_exhausts_and_assumes_glitch() -> None:
    h = _make(start_gap=20.0)
    cfg = _cfg(
        fall_confirm_timeout_ms=100,
        fall_recovery_max_jitter_attempts=2,
        jitter_pause_ms=50,
    )
    _run_advance(h, cfg)
    h.gap[0] = -1.0  # piece sits in exit, never falls, never appears downstream
    h.advance(0.2)
    h.tick(down=0, cfg=cfg)  # → RECOVERING, jitter #1
    assert h.stepper.jitter_calls == 1
    for _ in range(40):
        if h.ctrl.phase != EjectPhase.RECOVERING:
            break
        h.stepper.finish_jitter()
        h.advance(0.1)  # > jitter_pause_ms
        h.tick(down=0, cfg=cfg)
    assert h.ctrl.phase == EjectPhase.IDLE
    assert h.stepper.jitter_calls == 2  # max_attempts, then give up
    assert h.successes == []


# --- precise-pulse downstream gate -----------------------------------------


def test_all_c3_motion_holds_while_classification_gate_is_closed() -> None:
    feeding = object.__new__(GoToAngleFeeding)
    feeding._fast_eject_enabled = Mock(return_value=False)
    feeding._apply_action = Mock()

    feeding._drive_channel(
        "ch3",
        3,
        Action.ADVANCE,
        SimpleNamespace(advance_clearance_deg=20.0),
        SimpleNamespace(),
        False,
        SimpleNamespace(),
        _cfg(ch3_fast_eject_enabled=False),
        None,
        100.0,
    )

    feeding._apply_action.assert_not_called()


def test_c3_open_gate_uses_measured_boundary_release_and_publishes_attempt_once() -> None:
    feeding = object.__new__(GoToAngleFeeding)
    feeding._fast_eject_enabled = Mock(return_value=False)
    feeding._apply_action = Mock(return_value=True)
    feeding._on_ch3_release_attempt = Mock()
    state = SimpleNamespace(
        advance_clearance_deg=None,
        exit_com_forward_deg=8.0,
    )
    stepper = SimpleNamespace()
    cfg = _cfg(ch3_fast_eject_enabled=False)

    feeding._drive_channel(
        "ch3",
        3,
        Action.PRECISE,
        state,
        SimpleNamespace(),
        True,
        stepper,
        cfg,
        None,
        100.0,
    )

    feeding._apply_action.assert_called_once_with(
        "ch3",
        Action.PRECISE,
        stepper,
        cfg,
        advance_clearance_deg=None,
        precise_output_deg=11.0,
    )
    feeding._on_ch3_release_attempt.assert_called_once_with()


def test_c3_capped_approach_does_not_publish_before_boundary_crossing() -> None:
    feeding = object.__new__(GoToAngleFeeding)
    feeding._fast_eject_enabled = Mock(return_value=False)
    feeding._apply_action = Mock(return_value=True)
    feeding._on_ch3_release_attempt = Mock()
    cfg = _cfg(
        ch3_fast_eject_enabled=False,
        precise_pulse_output_deg=3.0,
        max_move_output_deg=10.0,
    )
    stepper = SimpleNamespace()

    feeding._drive_channel(
        "ch3",
        3,
        Action.PRECISE,
        SimpleNamespace(
            advance_clearance_deg=None,
            exit_com_forward_deg=20.0,
        ),
        SimpleNamespace(),
        True,
        stepper,
        cfg,
        None,
        100.0,
    )

    feeding._apply_action.assert_called_once_with(
        "ch3",
        Action.PRECISE,
        stepper,
        cfg,
        advance_clearance_deg=None,
        precise_output_deg=10.0,
    )
    feeding._on_ch3_release_attempt.assert_not_called()


def test_rejected_boundary_release_does_not_publish_attempt() -> None:
    feeding = object.__new__(GoToAngleFeeding)
    feeding._fast_eject_enabled = Mock(return_value=False)
    feeding._apply_action = Mock(return_value=False)
    feeding._on_ch3_release_attempt = Mock()

    feeding._drive_channel(
        "ch3",
        3,
        Action.PRECISE,
        SimpleNamespace(
            advance_clearance_deg=None,
            exit_com_forward_deg=1.0,
        ),
        SimpleNamespace(),
        True,
        SimpleNamespace(),
        _cfg(ch3_fast_eject_enabled=False),
        None,
        100.0,
    )

    feeding._on_ch3_release_attempt.assert_not_called()


def test_perception_exit_fall_without_release_command_does_not_publish() -> None:
    feeding = object.__new__(GoToAngleFeeding)
    feeding.gc = SimpleNamespace(
        runtime_stats=SimpleNamespace(observePerfMs=lambda *_args: None)
    )
    feeding.irl = SimpleNamespace(c_channel_3_rotor_stepper=SimpleNamespace())
    feeding._classification_pending_until = 0.0
    feeding._ch3_was_at_exit = False
    feeding._drop_seen_at = {}
    feeding._classification_ready = Mock(return_value=True)
    feeding._fast_eject_enabled = Mock(return_value=False)
    feeding._drive_channel = Mock()
    feeding._stuck_watchdog = Mock()
    feeding._on_ch3_dispense = Mock()

    c3_exit = ChannelState(
        ts=1.0,
        in_drop=False,
        in_exit=True,
        n_pieces=1,
        exit_com_forward_deg=0.0,
        exit_com_in_precise=True,
    )
    c4_false_positive = ChannelState(
        ts=1.0, in_drop=True, in_exit=False, n_pieces=1
    )
    empty = ChannelState(ts=1.0, in_drop=False, in_exit=False, n_pieces=0)
    states = {2: empty, 3: c3_exit, 4: c4_false_positive}
    perception = SimpleNamespace(
        read_states=lambda: states,
        secondary_zone_occupied=lambda *_args, **_kwargs: False,
    )
    cfg = _cfg(
        enable_ch1=False,
        enable_ch2=False,
        enable_ch3=True,
        ch3_fast_eject_enabled=False,
    )

    feeding._step_perception(cfg, perception)
    assert feeding._drive_channel.call_args.args[2] == Action.PRECISE

    states[3] = empty
    feeding._step_perception(cfg, perception)
    feeding._on_ch3_dispense.assert_not_called()


def test_go_to_angle_advances_pieces_between_drop_and_exit_zones() -> None:
    feeding = object.__new__(GoToAngleFeeding)
    feeding.gc = SimpleNamespace(
        runtime_stats=SimpleNamespace(observePerfMs=lambda *_args: None)
    )
    feeding.irl = SimpleNamespace(
        c_channel_1_rotor_stepper=SimpleNamespace(),
        c_channel_2_rotor_stepper=SimpleNamespace(),
        c_channel_3_rotor_stepper=SimpleNamespace(),
    )
    feeding._classification_pending_until = 0.0
    feeding._drop_seen_at = {}
    feeding._classification_ready = Mock(return_value=True)
    feeding._drive_channel = Mock()
    feeding._stuck_watchdog = Mock()

    neutral_piece = ChannelState(
        ts=1.0,
        in_drop=False,
        in_exit=False,
        n_pieces=1,
        advance_clearance_deg=40.0,
    )
    empty = ChannelState(ts=1.0, in_drop=False, in_exit=False, n_pieces=0)
    perception = SimpleNamespace(
        read_states=lambda: {2: neutral_piece, 3: neutral_piece, 4: empty},
        secondary_zone_occupied=lambda *_args, **_kwargs: False,
    )
    cfg = _cfg(enable_ch1=False, enable_ch2=True, enable_ch3=True)

    feeding._step_perception(cfg, perception)

    actions_by_channel = {
        call.args[0]: call.args[2] for call in feeding._drive_channel.call_args_list
    }
    assert actions_by_channel == {
        "ch3": Action.ADVANCE,
        "ch2": Action.ADVANCE,
    }
