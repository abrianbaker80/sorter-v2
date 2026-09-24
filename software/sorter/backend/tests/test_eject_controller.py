"""EjectController state-machine transitions (closed-loop eject + fall recovery).

Pure-logic tests: the controller is driven with synthetic ChannelStates, a fake
stepper, and recorded callbacks — no hardware, no perception workers. The
harness models a piece whose ACTUAL forward gap to the exit zone shrinks by only
a FRACTION of each commanded move (simulated slippage), so the closed-loop
re-measurement is exercised the way it is on the machine.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace
import pytest

from unittest.mock import Mock

from perception.cascade import Action
from perception.state import ChannelState, PieceObservation
from subsystems.feeder.go_to_angle.config import GoToAngleConfig
from subsystems.feeder.go_to_angle.eject import EjectController, EjectPhase
from subsystems.feeder.go_to_angle.flow import GoToAngleFeeding
from runtime_stats import RuntimeStatsCollector


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


def test_unmeasured_c3_motion_holds_while_classification_gate_is_closed() -> None:
    feeding = object.__new__(GoToAngleFeeding)
    feeding._motion_tick = 0
    feeding._move_targets = {}
    feeding._busy = Mock(return_value=False)
    feeding._fast_eject_enabled = Mock(return_value=False)
    feeding._apply_action = Mock()

    feeding._drive_channel(
        "ch3",
        3,
        Action.ADVANCE,
        SimpleNamespace(advance_clearance_deg=None),
        SimpleNamespace(),
        False,
        SimpleNamespace(),
        _cfg(ch3_fast_eject_enabled=False),
        None,
        100.0,
    )

    feeding._apply_action.assert_not_called()


@pytest.mark.parametrize("fast_eject", [False, True])
@pytest.mark.parametrize("clearance", [0.75, 20.0, 45.0])
def test_c3_stages_to_existing_exit_limit_while_c4_is_busy(monkeypatch, clearance, fast_eject):
    feeding, now = _owned_feeder(monkeypatch)
    stepper = feeding.irl.c_channel_3_rotor_stepper
    cfg = _cfg(ch3_fast_eject_enabled=fast_eject)
    perception = SimpleNamespace(secondary_zone_occupied=lambda *a, **k: False)
    state = ChannelState(ts=now[0], in_drop=True, in_exit=False, n_pieces=1,
                         advance_clearance_deg=clearance)
    feeding._on_ch3_release_attempt = Mock()
    feeding._drive_channel("ch3", 3, Action.ADVANCE, state, None, False,
                           stepper, cfg, perception, now[0])
    from subsystems.feeder.go_to_angle.flow import CHANNEL_OUTPUT_GEAR_RATIO
    assert stepper.moves == [pytest.approx(min(clearance, cfg.advance_output_deg)
                                          * CHANNEL_OUTPUT_GEAR_RATIO)]
    feeding._on_ch3_release_attempt.assert_not_called()
    assert feeding.shared.c3_motion_pending


@pytest.mark.parametrize("phase", [EjectPhase.ADVANCING, EjectPhase.AWAITING_FALL,
                                  EjectPhase.RECOVERING])
def test_closed_gate_staging_cannot_resume_active_eject_recovery(monkeypatch, phase):
    feeding, now = _owned_feeder(monkeypatch)
    h = _make(start_gap=0)
    cfg = _cfg(ch3_fast_eject_enabled=True, ch3_fast_eject_fall_timeout_ms=100)
    h.tick(cfg=cfg)
    h.ctrl._phase = phase
    feeding._eject_controllers[3] = h.ctrl
    h.advance(10.0)
    state = ChannelState(ts=now[0], in_drop=True, in_exit=False, n_pieces=1,
                         advance_clearance_deg=20.0)
    downstream = ChannelState(ts=now[0], in_drop=False, in_exit=False, n_pieces=0)
    perception = SimpleNamespace(secondary_zone_occupied=lambda *a, **k: False)
    stepper = feeding.irl.c_channel_3_rotor_stepper
    feeding._drive_channel("ch3", 3, Action.ADVANCE, state, downstream, False,
                           stepper, cfg, perception, h.now[0])
    assert h.stepper.jitter_calls == 0
    assert stepper.moves == []
    assert h.ctrl.phase == phase


@pytest.mark.parametrize("hold", ["exit", "unconfirmed", "recovery", "foreign_exit"])
def test_c3_staging_preserves_closed_gate_release_and_transfer_holds(monkeypatch, hold):
    feeding, now = _owned_feeder(monkeypatch)
    stepper = feeding.irl.c_channel_3_rotor_stepper
    cfg = _cfg(ch3_fast_eject_enabled=False)
    if hold in {"unconfirmed", "recovery"}:
        from subsystems.classification_channel.transfer_episode import TransferEpisode
        feeding.shared.c3_transfer_episode = TransferEpisode(0, 0, now[0], 1000.0,
            state="waiting" if hold == "unconfirmed" else "recovering")
    perception = SimpleNamespace(secondary_zone_occupied=lambda *a, **k: hold == "foreign_exit")
    state = ChannelState(ts=now[0], in_drop=True, in_exit=hold == "exit", n_pieces=1,
                         advance_clearance_deg=20.0)
    feeding._drive_channel("ch3", 3, Action.PRECISE if hold == "exit" else Action.ADVANCE,
                           state, None, False, stepper, cfg, perception, now[0])
    assert stepper.moves == []


def test_c3_open_gate_uses_measured_boundary_release_and_publishes_attempt_once() -> None:
    feeding = object.__new__(GoToAngleFeeding)
    feeding.shared = SimpleNamespace(c3_motion_pending=False)
    feeding._motion_tick = 0
    feeding._move_targets = {}
    feeding._busy = Mock(return_value=False)
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
    feeding._motion_tick = 0
    feeding._move_targets = {}
    feeding._busy = Mock(return_value=False)
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
    feeding._motion_tick = 0
    feeding._move_targets = {}
    feeding._busy = Mock(return_value=False)
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
    feeding.shared = SimpleNamespace(c3_motion_pending=False)
    feeding._motion_tick = 0
    feeding._move_targets = {}
    feeding._busy = Mock(return_value=False)
    feeding.gc = SimpleNamespace(
        runtime_stats=Mock()
    )
    feeding.irl = SimpleNamespace(c_channel_3_rotor_stepper=SimpleNamespace(_name="c3"))
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
    feeding.shared = SimpleNamespace(c3_motion_pending=False)
    feeding._motion_tick = 0
    feeding._move_targets = {}
    feeding._busy = Mock(return_value=False)
    feeding.gc = SimpleNamespace(
        runtime_stats=Mock()
    )
    feeding.irl = SimpleNamespace(
        c_channel_1_rotor_stepper=SimpleNamespace(_name="c1"),
        c_channel_2_rotor_stepper=SimpleNamespace(_name="c2"),
        c_channel_3_rotor_stepper=SimpleNamespace(_name="c3"),
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


class _OwnedStepper:
    def __init__(self, name):
        self._name = name
        self.position = 100
        self.stopped = True
        self.target = None
        self.moves = []
        self.speeds = []
        self.accept = True
        self.software_disabled = False
        self.jittering = False
        self.jitters = []

    def jitter_degrees(self, amplitude, cycles, speed, accel):
        self.jitters.append((amplitude, cycles, speed, accel))
        if self.accept:
            self.target = self.position
            self.jittering = True
        return self.accept

    def is_jittering(self):
        return self.jittering

    def microsteps_for_degrees(self, deg):
        return round(deg * 10)

    def estimateMoveDegreesMs(self, *args, **kwargs):
        return 100

    def set_speed_limits(self, *args):
        self.speeds.append(args)

    def move_degrees(self, deg):
        self.moves.append(deg)
        if self.accept:
            self.target = self.position + self.microsteps_for_degrees(deg)
        return self.accept


def _owned_feeder(monkeypatch):
    now = [10.0]
    monkeypatch.setattr("subsystems.feeder.go_to_angle.flow.time.monotonic", lambda: now[0])
    irl = SimpleNamespace(**{f"c_channel_{i}_rotor_stepper": _OwnedStepper(f"c{i}") for i in (1, 2, 3)})
    stats = RuntimeStatsCollector()
    stats.setLifecycleState("running", now_wall=1000.0, now_monotonic=now[0])
    gc = SimpleNamespace(logger=_SilentLogger(), runtime_stats=stats,
                         rotary_channel_steppers_can_operate_in_parallel=True)
    feeder = GoToAngleFeeding(irl, SimpleNamespace(), gc, SimpleNamespace(c3_motion_pending=False), None)
    return feeder, now


def test_owned_move_requires_post_acceptance_target_and_fresh_completion(monkeypatch):
    f, now = _owned_feeder(monkeypatch)
    s, cfg = f.irl.c_channel_3_rotor_stepper, _cfg()
    assert f._move("ch3_precise", s, 2.0, 50, cfg)
    assert f.shared.c3_motion_pending
    assert f._busy(s)
    now[0] += 0.2
    f._motion_tick += 1
    assert f._busy(s)  # Idle at the pre-move position is not completion.
    assert f._recovery_move(s, 3, cfg) is None
    assert len(s.moves) == 1
    s.position = s.target
    s.stopped = False
    f._motion_tick += 1
    assert f._busy(s)  # Target alone also does not prove stopped.
    s.stopped = True
    f._motion_tick += 1
    assert not f._busy(s)
    assert not f.shared.c3_motion_pending
    assert f._recovery_move(s, 3, cfg)
    assert not f._move("ch3_precise", s, 2.0, 0, cfg)
    assert len(s.moves) == 2


def test_recovery_owner_blocks_normal_same_tick_and_not_other_stepper(monkeypatch):
    f, now = _owned_feeder(monkeypatch)
    cfg = _cfg(enable_ch1=False, enable_ch2=True, enable_ch3=True)
    s = f.irl.c_channel_2_rotor_stepper
    # Invoke recovery at the actual watchdog boundary in _step_perception.
    f._stuck_watchdog.observe = lambda **kw: f._recovery_move(s, 2, cfg) if kw["channel_id"] == 3 else None
    f._classification_ready = lambda *_: True
    piece = ChannelState(ts=10.0, in_drop=False, in_exit=False, n_pieces=1, advance_clearance_deg=20.0)
    empty = ChannelState(ts=10.0, in_drop=False, in_exit=False, n_pieces=0)
    perception = SimpleNamespace(read_states=lambda: {2: piece, 3: piece, 4: empty}, secondary_zone_occupied=lambda *a, **k: False)
    f._step_perception(cfg, perception)
    assert len(s.moves) == 1
    assert len(s.speeds) == 1
    assert len(f.irl.c_channel_3_rotor_stepper.moves) == 1


def test_rejected_move_has_no_owner_and_feedback_failure_holds(monkeypatch):
    f, now = _owned_feeder(monkeypatch)
    s, cfg = f.irl.c_channel_2_rotor_stepper, _cfg()
    s.accept = False
    assert not f._move("ch2", s, 2, 0, cfg)
    assert not f._move_targets
    now[0] += 0.6
    s.accept = True
    assert f._move("ch2", s, 2, 0, cfg)
    now[0] += 0.2
    f._motion_tick += 1
    s.position = None
    assert f._busy(s)
    assert s._name in f._move_targets


def test_lost_move_acknowledgement_keeps_ownership(monkeypatch):
    f, now = _owned_feeder(monkeypatch)
    s, cfg = f.irl.c_channel_3_rotor_stepper, _cfg()
    s.move_degrees = Mock(side_effect=RuntimeError("lost acknowledgement"))
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        f._move("ch3", s, 2, 0, cfg)
    now[0] += 1
    f._motion_tick += 1
    assert f._busy(s)
    assert f.shared.c3_motion_pending
    assert f._recovery_move(s, 3, cfg) is None


def test_suppressed_move_does_not_claim_acceptance(monkeypatch):
    f, now = _owned_feeder(monkeypatch)
    s = f.irl.c_channel_3_rotor_stepper
    s.software_disabled = True
    assert not f._move("ch3", s, 2, 0, _cfg())
    assert not f.shared.c3_motion_pending
    assert not s.moves and not s.speeds


@pytest.mark.parametrize('outcome',['rejected','lost_ack'])
def test_staging_purpose_follows_exact_motor_owner(monkeypatch,outcome):
    f,now=_owned_feeder(monkeypatch)
    s=f.irl.c_channel_3_rotor_stepper
    if outcome=='rejected':
        s.accept=False
        assert not f._move('ch3_advance',s,1,0,_cfg(),c3_safe_staging=True)
        assert not f.shared.c3_motion_pending and not f.shared.c3_safe_staging_pending
    else:
        s.move_degrees=Mock(side_effect=RuntimeError('lost ACK'))
        with pytest.raises(RuntimeError,match='lost ACK'):
            f._move('ch3_advance',s,1,0,_cfg(),c3_safe_staging=True)
        assert f.shared.c3_motion_pending and f.shared.c3_safe_staging_pending
        now[0]+=1;f._motion_tick+=1
        assert not f._move('ch3_precise',s,1,0,_cfg())
        assert f.shared.c3_safe_staging_pending  # A refused call cannot relabel an owner.


def _first_tick_feeder(monkeypatch):
    f, now = _owned_feeder(monkeypatch)
    f._cfg = lambda: _cfg(enable_ch1=True, enable_ch2=False, enable_ch3=False)
    f._classification_ready = lambda *_: True
    empty = ChannelState(ts=1000.0, in_drop=False, in_exit=False, n_pieces=0)
    f.gc.perception_service = SimpleNamespace(read_states=lambda: {2: empty, 3: empty, 4: empty})
    return f, now


@pytest.mark.parametrize("gap,expected,cap,released", [
    (8.0, 14.0, 120.0, True), (None, 6.0, 120.0, True),
    (-2.0, 6.0, 120.0, True), (8.0, 10.0, 10.0, False),
])
def test_c3_release_margin_does_not_change_c2_pulse(monkeypatch, gap, expected, cap, released):
    from subsystems.feeder.go_to_angle.config import configFromDict

    f, _ = _owned_feeder(monkeypatch)
    cfg = configFromDict({"precise_pulse_output_deg": 3.0,
                          "ch3_release_margin_output_deg": 6.0,
                          "max_move_output_deg": cap,
                          "ch2_fast_eject_enabled": False,
                          "ch3_fast_eject_enabled": False})
    state = ChannelState(ts=1000, in_drop=False, in_exit=True, n_pieces=1,
                         exit_com_forward_deg=gap)
    empty = ChannelState(ts=1000, in_drop=False, in_exit=False, n_pieces=0)
    f._on_ch3_release_attempt = Mock()
    for ch in (2, 3):
        f._drive_channel(f"ch{ch}", ch, Action.PRECISE, state, empty, True,
                         getattr(f.irl, f"c_channel_{ch}_rotor_stepper"), cfg, None, 10)
    ratio = 130.0 / 12.0
    assert f.irl.c_channel_2_rotor_stepper.moves == pytest.approx([3 * ratio])
    assert f.irl.c_channel_3_rotor_stepper.moves == pytest.approx([expected * ratio])
    assert f._on_ch3_release_attempt.call_count == int(released)


@pytest.mark.parametrize("channel", [2, 3])
@pytest.mark.parametrize("sequence", ["successor", "alternating"])
def test_moving_successor_does_not_inherit_departed_leader_position(monkeypatch, channel, sequence):
    f, now = _owned_feeder(monkeypatch)
    cfg = _cfg(enable_ch1=(channel == 2), stuck_no_progress_ms=5000)
    f._cfg = lambda: cfg
    f._classification_ready = lambda *_: True
    f._drive_channel = Mock()  # Recorded positions drive the real watchdog path.
    empty = ChannelState(ts=1000, in_drop=False, in_exit=False, n_pieces=0)
    states = {2: empty, 3: empty, 4: empty}
    f.gc.perception_service = SimpleNamespace(
        read_states=lambda: states,
        secondary_zone_occupied=lambda *a, **k: False,
    )
    def observe(at, track_id, pos):
        now[0] = at
        states[channel] = ChannelState(
            ts=1000 + at, in_drop=(channel == 2), in_exit=(pos <= 0), n_pieces=1,
            pieces=(PieceObservation(pos, 0, 0, sv_bt_track_id=track_id),),
        )
        f.step()

    if sequence == "successor":
        observe(10, 11, -1)  # Departing leader at the lip.
        observe(10.2, 13, 160)  # Its successor is further upstream.
        for tick, pos in enumerate([140, 120, 100, 80, 60, 40], start=1):
            observe(10.2 + tick, 13, pos)
    else:
        for tick in range(31):
            observe(10 + tick * 0.2, 1 + tick % 2, 60 - tick)
    upstream = getattr(f.irl, f"c_channel_{channel - 1}_rotor_stepper")
    assert upstream.moves == []
    assert f._stuck_watchdog._trackers[channel].nudge_attempts == 0


@pytest.mark.parametrize("hold_kind", ["coordinator", "chute"])
def test_stateful_feeder_hold_excludes_pause_without_releasing_motor_owner(monkeypatch, hold_kind):
    from subsystems.feeder.state_machine import FeederStateMachine
    from subsystems.feeder.states import FeederState

    f, now = _owned_feeder(monkeypatch)
    cfg = _cfg(enable_ch1=False, stuck_no_progress_ms=1000)
    f._cfg = lambda: cfg
    f._classification_ready = lambda *_: True
    f._drive_channel = Mock()  # Isolate the real watchdog from new normal moves.
    empty = ChannelState(ts=1000, in_drop=False, in_exit=False, n_pieces=0)
    piece = ChannelState(ts=1000, in_drop=False, in_exit=False, n_pieces=1,
                         pieces=(PieceObservation(40, 0, 0),))
    f.gc.perception_service = SimpleNamespace(
        read_states=lambda: {2: empty, 3: piece, 4: empty},
        secondary_zone_occupied=lambda *a, **k: False,
    )
    c2, c3 = f.irl.c_channel_2_rotor_stepper, f.irl.c_channel_3_rotor_stepper
    assert f._move("ch3", c3, 2, 0, cfg)
    f.step()
    machine = object.__new__(FeederStateMachine)
    machine.current_state = FeederState.FEEDING
    machine.states_map = {FeederState.FEEDING: f}
    def hold():
        if hold_kind == "coordinator":
            machine.hold_motion()
        else:
            f.gc.rotary_channel_steppers_can_operate_in_parallel = False
            f.shared.chute_move_in_progress = True
            f.step()

    now[0] = 10.5
    hold()
    now[0] = 100.5
    hold()  # Repeated incident/manual/chute holds must not restart the pause.
    now[0] = 210.5
    f.shared.chute_move_in_progress = False
    f.step()
    assert c2.moves == []  # Only 0.5 s of active no-progress time has elapsed.
    assert c3._name in f._move_targets and f.shared.c3_motion_pending
    assert len(c3.moves) == 1  # Old stopped feedback is still not completion.
    now[0] = 211.01
    f.step()
    assert len(c2.moves) == 1  # Real active stall still triggers recovery on time.


def test_real_collector_first_feeder_tick_and_completion_sequence(monkeypatch):
    # The live path: step -> perception cascade -> C1 _move -> real collector.
    f, now = _first_tick_feeder(monkeypatch)
    s, stats = f.irl.c_channel_1_rotor_stepper, f.gc.runtime_stats
    stats.observeFeederState(now[0], False, False, True, True, "idle", "idle")
    now[0] += 0.25
    move = s.move_degrees

    def reply_after_50ms(deg):
        accepted = move(deg)
        now[0] += 0.05
        return accepted

    s.move_degrees = reply_after_50ms
    f.step()
    counts = stats._pulse_counts["ch1"]
    assert (counts.attempts, counts.sent, counts.busy_skip, counts.failed) == (1, 1, 0, 0)
    # Event uses monotonic seconds at the outcome, not wall time or tick start.
    assert stats._ch2_clear_to_ch1_pulse_s == pytest.approx([0.30])
    assert s._name in f._move_targets and len(s.moves) == 1
    f.step()  # Normal busy gate does not submit or count another attempt.
    assert len(s.moves) == 1 and counts.attempts == 1
    now[0] += 1
    f.step()  # Time elapsed, but stopped at old position is insufficient.
    assert len(s.moves) == 1 and s._name in f._move_targets
    s.position = s.target
    s.stopped = False
    f.step()  # Target alone is also insufficient.
    assert len(s.moves) == 1
    s.stopped = True
    f.step()  # Valid completion releases old owner and permits next move.
    assert len(s.moves) == 2 and counts.sent == counts.attempts == 2
    assert f._move_targets[s._name] == s.target
    assert s.target > s.position  # New owner retained; acceptance != completion.


@pytest.mark.parametrize("outcome", ["rejected", "missing_ack", "position_unavailable"])
def test_real_collector_first_tick_failure_outcomes(monkeypatch, outcome):
    f, _ = _first_tick_feeder(monkeypatch)
    s, stats = f.irl.c_channel_1_rotor_stepper, f.gc.runtime_stats
    if outcome == "rejected":
        s.accept = False
    elif outcome == "missing_ack":
        s.move_degrees = Mock(side_effect=RuntimeError("missing move ACK"))
    else:
        s.position = None
    if outcome == "missing_ack":
        with pytest.raises(RuntimeError, match="missing move ACK"):
            f.step()  # Preserve original exception, not a metrics TypeError.
    else:
        f.step()
    counts = stats._pulse_counts["ch1"]
    assert (counts.attempts, counts.sent, counts.busy_skip, counts.failed) == (1, 0, 0, 1)
    assert (s._name in f._move_targets) == (outcome == "missing_ack")
    assert not stats._ch2_clear_to_ch1_pulse_s


def test_real_collector_watchdog_callback_and_busy_deferrals(monkeypatch):
    f, now = _owned_feeder(monkeypatch)
    monkeypatch.setattr("subsystems.feeder.pulse_perception.stuck_watchdog._handling_off", lambda: False)
    monkeypatch.setattr("subsystems.feeder.pulse_perception.stuck_watchdog._handling_automatic", lambda: True)
    s, stats, wd = f.irl.c_channel_2_rotor_stepper, f.gc.runtime_stats, f._stuck_watchdog
    cfg = _cfg(stuck_watchdog_enabled=True, stuck_no_progress_ms=1000, stuck_max_nudge_attempts=3)
    args = dict(channel_id=3, channel_label="C3", upstream_label="C2", upstream_channel_id=2,
                upstream_stepper=s, upstream_enabled=True, leading_pos_deg=40.0,
                wants_advance=True, cfg=cfg)
    wd.observe(**args, now=now[0])
    now[0] += 2
    wd.observe(**args, now=now[0])  # Real observe -> callback -> shared _move.
    recovery = stats._pulse_counts["ch2_recovery"]
    assert len(s.moves) == 1 and s._name in f._move_targets
    assert (recovery.attempts, recovery.sent, recovery.busy_skip) == (1, 1, 0)
    assert not f._move("ch2_normal", s, 2, 0, cfg)
    normal = stats._pulse_counts["ch2_normal"]
    assert (normal.attempts, normal.sent, normal.busy_skip) == (1, 0, 1)
    now[0] += 2
    f._motion_tick += 1
    wd.observe(**args, now=now[0])  # No target confirmation: recovery deferred.
    assert len(s.moves) == 1 and s._name in f._move_targets
    assert (recovery.attempts, recovery.sent, recovery.busy_skip) == (2, 1, 1)
    assert wd._trackers[3].nudge_attempts == 1  # Deferral spends no recovery budget.
