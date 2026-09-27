import time
from dataclasses import replace
from typing import Optional, TYPE_CHECKING

from states.base_state import BaseState
from subsystems.shared_variables import SharedVariables
from subsystems.bus import StationId
from irl.config import IRLInterface, IRLConfig
from global_config import GlobalConfig
from vision import VisionManager

from ..states import FeederState
from ..go_to_angle.config import GoToAngleConfig
from .config import (
    PulsePerceptionConfig,
    channelMaxMoveOutputDeg,
    channelMoveSpeed,
)
from .stuck_watchdog import FeederStuckWatchdog
from subsystems.feeder.incidents import feeder_jam_incident_active

# A deliberately simple pulsing state machine on the new perception stack.
#
# It reads ChannelState booleans from the perception service (exactly like the
# go-to-angle flow's perception path) and, per channel, does one of three
# things each tick:
#   - piece in the EXIT zone + downstream ready   -> pulse exit_pulse_output_deg,
#                                                     pause exit_pulse_pause_ms
#   - piece in the EXIT zone + downstream NOT ready -> hold still (never pulse a
#                                                     piece off the edge into a
#                                                     busy downstream channel)
#   - piece in the DROP zone only                 -> pulse drop_pulse_output_deg,
#                                                     pause drop_pulse_pause_ms
#   - empty channel                               -> idle
#
# No fast-eject, no COM closed loop, no jitter recovery — that all lives in the
# go-to-angle flow. "The other stack will be removed in time"; this is the
# minimal perception-native feeder.

if TYPE_CHECKING:
    from hardware.sorter_interface import StepperMotor

# Motor-shaft to channel-output gear ratio. One output (LEGO wheel) degree
# requires this many motor degrees. Matches the go-to-angle flow's constant.
CHANNEL_OUTPUT_GEAR_RATIO = 130.0 / 12.0

# Minimum stepper speed floor for pulse moves. MUST stay > 0: a min_speed of 0
# wedges the firmware on a distance move — braking clamps _current_speed to 0
# before the step target is reached, the move never transitions back to STOPPED,
# and every subsequent move_steps is rejected (motor frozen until a manual UI
# move re-stops it). 16 matches the firmware default and the exit-pulse path in
# detection.py.
MIN_MOVE_SPEED_USTEPS_PER_S = 16

# Re-read the tuning config from disk at most this often so the tuning page
# takes effect live without a restart, without hammering the filesystem.
_CONFIG_TTL_S = 1.0

# After a C3 exit dispense, keep C3 blocked this long so the in-flight piece
# can register downstream before we consider another move.
CLASSIFICATION_PENDING_ADMISSION_MS = 1500


def _leading_com(state) -> Optional[float]:
    # Leading (most-forward) on-channel piece's travel position toward the exit.
    # None when the channel reports no piece this frame. The jam watchdog treats
    # this as the channel's progress signal.
    pieces = getattr(state, "pieces", ())
    if pieces:
        return float(pieces[0].com_forward_to_exit_deg)
    return None


def _wants_advance(action) -> bool:
    # The channel is actively trying to move THIS piece (ADVANCE/PRECISE), vs.
    # intentionally holding for a busy downstream (FREEZE) or empty (IDLE).
    from perception.cascade import Action

    return action in (Action.ADVANCE, Action.PRECISE)


class PulsePerceptionFeeding(BaseState):
    def __init__(
        self,
        irl: IRLInterface,
        irl_config: IRLConfig,
        gc: GlobalConfig,
        shared: SharedVariables,
        vision: VisionManager,
    ):
        super().__init__(irl, gc)
        self.irl_config = irl_config
        self.shared = shared
        self.vision = vision
        self._busy_until: dict[str, float] = {}
        self._move_targets: dict[str, int] = {}
        self._jitter_owned: set[str] = set()
        self._completion_checked_tick: dict[str, int] = {}
        self._motion_tick = 0
        self._last_perception_tick = 0.0
        self._stuck_watchdog = FeederStuckWatchdog(gc)
        self._config: PulsePerceptionConfig = PulsePerceptionConfig()
        self._config_loaded_at: float = 0.0
        self._recovery_config: GoToAngleConfig = GoToAngleConfig()
        self._recovery_config_loaded_at: float = float("-inf")
        self._classification_pending_until: float = 0.0
        self._ch3_was_at_exit: bool = False
        # Per-channel monotonic timestamp of the last frame that reported a piece
        # in the drop zone. Drives the C2/C3 drop-zone occupancy latch.
        self._drop_seen_at: dict[int, float] = {}
        machine_setup = getattr(irl_config, "machine_setup", None)
        self._classification_setup = bool(
            machine_setup is not None
            and getattr(machine_setup, "uses_classification_channel", False)
        )
        if getattr(shared, "c4_runtime_owner", None) is not None:
            shared.request_c3_recovery = self._recover_transfer

    def _cfg(self) -> PulsePerceptionConfig:
        now = time.monotonic()
        if now - self._config_loaded_at >= _CONFIG_TTL_S:
            try:
                from toml_config import getPulsePerceptionConfig
                from .config import configFromDict
                self._config = configFromDict(getPulsePerceptionConfig())
            except Exception as exc:
                self.gc.logger.warning(f"PulsePerception: config load failed: {exc}")
            self._config_loaded_at = now
        return self._config

    def _busy(self, stepper: "StepperMotor") -> bool:
        name = stepper._name
        if time.monotonic() < self._busy_until.get(name, 0.0):
            return True
        target = self._move_targets.get(name)
        if target is None:
            return False
        if getattr(stepper, "software_disabled", False):
            return True  # A suppressed motor's synthetic stopped state is not proof.
        if self._completion_checked_tick.get(name) == self._motion_tick:
            return True
        self._completion_checked_tick[name] = self._motion_tick
        try:
            if name in self._jitter_owned and stepper.is_jittering():
                return True
            if not stepper.stopped or int(stepper.position) != target:
                return True
        except Exception as exc:
            self.gc.logger.warning(f"PulsePerception: {name} completion unavailable: {exc}")
            return True
        del self._move_targets[name]
        self._jitter_owned.discard(name)
        if stepper is getattr(self.irl, "c_channel_3_rotor_stepper", None):
            self.shared.c3_motion_pending = False
            self.shared.c3_safe_staging_pending = False
        return False

    def _move(
        self,
        label: str,
        channel: int,
        stepper: "StepperMotor",
        output_deg: float,
        pause_ms: int,
        cfg: PulsePerceptionConfig,
        enforce_min: bool = True,
        c3_safe_staging: bool = False,
    ) -> bool:
        if self._busy(stepper):
            return False
        is_c3 = (
            stepper is getattr(self.irl, "c_channel_3_rotor_stepper", None)
            and getattr(self.shared, "c4_runtime_owner", None) is not None
        )
        if is_c3 and getattr(stepper, "software_disabled", False):
            return False
        speed = channelMoveSpeed(cfg, channel)
        output_deg = abs(output_deg)
        if enforce_min:
            output_deg = max(cfg.min_move_output_deg, output_deg)
        output_deg = min(channelMaxMoveOutputDeg(cfg, channel), output_deg)
        sign = 1 if cfg.forward_direction_sign >= 0 else -1
        motor_deg = sign * output_deg * CHANNEL_OUTPUT_GEAR_RATIO
        if is_c3:
            try:
                target = int(stepper.position) + stepper.microsteps_for_degrees(motor_deg)
            except Exception as exc:
                self.gc.logger.warning(f"PulsePerception: C3 position unavailable: {exc}")
                return False
        # Set the move speed and tell the motor to move to the angle — that's it.
        # We NEVER set acceleration here; the motor keeps whatever acceleration it
        # already has.
        try:
            stepper.set_speed_limits(MIN_MOVE_SPEED_USTEPS_PER_S, speed)
        except Exception as exc:
            self.gc.logger.warning(f"PulsePerception: {label} speed set failed: {exc}")
        exec_ms = stepper.estimateMoveDegreesMs(abs(motor_deg), max_speed=speed or 5000)
        if is_c3:
            # An uncertain acknowledgement retains this owner until fresh stopped
            # and target-position evidence resolves the accepted finite command.
            self._move_targets[stepper._name] = target
            self._completion_checked_tick.pop(stepper._name, None)
            self.shared.c3_motion_pending = True
            self.shared.c3_safe_staging_pending = bool(
                c3_safe_staging and not self.shared.classification_ready
            )
        success = stepper.move_degrees(motor_deg)
        cooldown_ms = (max(0, exec_ms) + max(0, pause_ms)) if success else 500
        if is_c3 and not success:
            self._move_targets.pop(stepper._name, None)
            self.shared.c3_motion_pending = False
            self.shared.c3_safe_staging_pending = False
        self._busy_until[stepper._name] = time.monotonic() + cooldown_ms / 1000.0
        self.gc.logger.info(
            f"PulsePerception: {label} pulse ch={channel} speed={speed} "
            f"output={output_deg:.1f}° motor={motor_deg:.1f}° "
            f"success={success} exec_ms={exec_ms} pause_ms={pause_ms}"
        )
        return success

    def _on_ch3_dispense(self) -> None:
        if getattr(self.shared, "c4_runtime_owner", None) is not None:
            # PhysicalHandoff alone proves C4 arrival and publishes delivery.
            return
        try:
            from .autotune import noteDispense
            noteDispense()
        except Exception:
            pass
        if hasattr(self.shared, "publish_piece_delivered"):
            try:
                self.shared.publish_piece_delivered(
                    source=StationId.C3,
                    target=StationId.CLASSIFICATION,
                    delivered_at_mono=time.monotonic(),
                )
            except Exception:
                pass
        self._classification_pending_until = (
            time.monotonic() + CLASSIFICATION_PENDING_ADMISSION_MS / 1000.0
        )

    def _classification_ready(self, cfg: PulsePerceptionConfig) -> bool:
        if not cfg.gate_ch3_on_classification_ready or not self._classification_setup:
            return True
        if time.monotonic() < self._classification_pending_until:
            return False
        return bool(self.shared.classification_ready)

    def _latch_drop(self, ch: int, state, now: float, cfg: PulsePerceptionConfig):
        """Persist drop-zone occupancy for one feeder channel.

        Once a piece is seen in the drop zone we consider the zone occupied for
        ``drop_zone_persistence_ms`` after the last positive frame — a one/two
        frame detection dropout no longer reads as 'empty'. Only ``in_drop`` is
        latched; the exit fields pass through untouched so exit handling still
        sees the live state. 0 disables the latch."""
        window_ms = cfg.drop_zone_persistence_ms
        if window_ms <= 0:
            return state
        if state.in_drop:
            self._drop_seen_at[ch] = now
            return state
        last = self._drop_seen_at.get(ch)
        if last is not None and (now - last) * 1000.0 <= window_ms:
            return replace(state, in_drop=True)
        return state

    def step(self) -> Optional[FeederState]:
        self._motion_tick += 1
        if getattr(self.shared, "c4_runtime_owner", None) is not None:
            self.shared.request_c3_recovery = self._recover_transfer
        c3_stepper = getattr(self.irl, "c_channel_3_rotor_stepper", None)
        if c3_stepper is not None:
            self._busy(c3_stepper)
            self.shared.c3_motion_pending = c3_stepper._name in self._move_targets
        cfg = self._cfg()

        can_run = self.gc.rotary_channel_steppers_can_operate_in_parallel or (
            not self.shared.chute_move_in_progress
        )
        if not can_run:
            return FeederState.FEEDING

        perception_service = getattr(self.gc, "perception_service", None)
        if perception_service is None:
            return FeederState.FEEDING

        from perception.cascade import Action, feederChannelAction, c1Action
        from perception.state import EMPTY_STATE

        states = perception_service.read_states()
        c2 = states.get(2, EMPTY_STATE)
        c3 = states.get(3, EMPTY_STATE)
        c4 = states.get(4, EMPTY_STATE)

        now_mono = time.monotonic()
        self._last_perception_tick = now_mono
        # Hold C2/C3 drop-zone occupancy across brief detector dropouts so the
        # per-channel action (and the ``not c3.in_drop`` upstream gate below) see
        # a stable "occupied" instead of flickering empty for a frame.
        c2 = self._latch_drop(2, c2, now_mono, cfg)
        c3 = self._latch_drop(3, c3, now_mono, cfg)

        if cfg.enable_ch3:
            # C3's downstream is the classification channel (C4). The feeder does
            # NOT define "ready" itself — that determination is owned and exposed
            # by the classification channel (shared.classification_ready, set per
            # its active mode: single-piece = whole channel empty, two-piece = drop
            # zone clear). The feeder just asks. The only feeder-side gate is the
            # post-dispense admission window (let an in-flight piece register first).
            c3_downstream_ready = (
                now_mono >= self._classification_pending_until
                and self._classification_ready(cfg)
            )
            action = feederChannelAction(
                c3, downstream_clear=c3_downstream_ready, greedy=cfg.ch3_greedy_enabled
            )
            episode = getattr(self.shared, "c3_transfer_episode", None)
            if (getattr(self.shared, "c4_runtime_owner", None) is not None
                    and episode is not None and episode.unresolved):
                action = Action.IDLE  # The existing reservation owns C3 until resolved.
            # C3 hung at the C2->C3 hand-off: keep C3 from hammering a piece it
            # can't move; nudge C2 (its upstream) to free it, escalate on failure.
            self._stuck_watchdog.observe(
                channel_id=3,
                channel_label="C3",
                upstream_label="C2",
                upstream_channel_id=2,
                upstream_stepper=self.irl.c_channel_2_rotor_stepper,
                upstream_enabled=bool(cfg.enable_ch2),
                leading_pos_deg=_leading_com(c3),
                wants_advance=_wants_advance(action),
                cfg=cfg,
                now=now_mono,
            )
            if not feeder_jam_incident_active(self.gc, channel_label="C3"):
                self._apply_action(
                    "ch3", 3, action, self.irl.c_channel_3_rotor_stepper, c3, cfg
                )
            # Legacy mode retains its falling-edge delivery notification. With
            # physical C4, PhysicalHandoff owns delivery after intake evidence.
            ch3_at_exit_now = c3.in_exit
            if self._ch3_was_at_exit and not ch3_at_exit_now:
                self._on_ch3_dispense()
            self._ch3_was_at_exit = ch3_at_exit_now

        if cfg.enable_ch2:
            episode = getattr(self.shared, "c3_transfer_episode", None)
            transfer_hold = bool(
                getattr(self.shared, "c4_runtime_owner", None) is not None
                and episode is not None and episode.unresolved
                and episode.forced_reject_reason
            )
            # C2's downstream is C3. "Clear" = C3's drop zone is not occupied,
            # so we never pulse a C2 piece off the edge into a busy C3.
            action = (Action.IDLE if transfer_hold else feederChannelAction(
                c2, downstream_clear=not c3.in_drop, greedy=cfg.ch2_greedy_enabled
            ))
            # C2 hung at the C1->C2 hand-off: nudge C1 (its upstream) to free the
            # piece, escalate to the operator jam incident if that keeps failing.
            self._stuck_watchdog.observe(
                channel_id=2,
                channel_label="C2",
                upstream_label="C1",
                upstream_channel_id=1,
                upstream_stepper=self.irl.c_channel_1_rotor_stepper,
                upstream_enabled=bool(cfg.enable_ch1),
                leading_pos_deg=_leading_com(c2),
                wants_advance=_wants_advance(action),
                cfg=cfg,
                now=now_mono,
            )
            if not feeder_jam_incident_active(self.gc, channel_label="C2"):
                self._apply_action(
                    "ch2", 2, action, self.irl.c_channel_2_rotor_stepper, c2, cfg
                )

        if cfg.enable_ch1:
            # C1 has no exit zone of its own; it just advances unless C2's drop
            # zone is occupied.
            stepper = self.irl.c_channel_1_rotor_stepper
            if c1Action(c2) == Action.ADVANCE and not self._busy(stepper):
                self._move(
                    "ch1",
                    1,
                    stepper,
                    cfg.ch1_pulse_output_deg,
                    cfg.ch1_pulse_pause_ms,
                    cfg,
                )

        return FeederState.FEEDING

    def _apply_action(
        self,
        label: str,
        channel: int,
        action,
        stepper: "StepperMotor",
        state,
        cfg: PulsePerceptionConfig,
    ) -> None:
        from perception.cascade import Action

        if self._busy(stepper):
            return
        if action == Action.ADVANCE:
            # Free advance pulse, but never push the most-forward piece off the
            # edge into the exit zone: cap the move to its forward clearance to
            # the exit edge. Once a piece reaches the exit, the PRECISE/FREEZE
            # branch (gated on downstream readiness) meters it out instead.
            # A piece still in the drop zone uses the drop-zone params; a greedy
            # advance of a piece that has already left the drop zone uses the
            # greedy params (only reachable when greedy mode is on for this
            # channel — the cascade returns IDLE here otherwise).
            if state.in_drop:
                output_deg = cfg.drop_pulse_output_deg
                pause_ms = cfg.drop_pulse_pause_ms
                move_label = f"{label}_drop"
            else:
                output_deg = cfg.greedy_pulse_output_deg
                pause_ms = cfg.greedy_pulse_pause_ms
                move_label = f"{label}_greedy"
            enforce_min = True
            clearance = getattr(state, "advance_clearance_deg", None)
            if channel == 3 and getattr(self.shared, "c4_runtime_owner", None) is not None:
                if clearance is None or clearance <= 0:
                    return  # No measured safe staging travel before C4 reservation.
                output_deg = min(output_deg, clearance)
                enforce_min = False  # A tuned minimum cannot overrun clearance.
            elif clearance is not None and clearance < output_deg:
                output_deg = clearance
                enforce_min = False
            self._move(
                move_label,
                channel,
                stepper,
                output_deg,
                pause_ms,
                cfg,
                enforce_min=enforce_min,
                c3_safe_staging=(
                    channel == 3
                    and getattr(self.shared, "c4_runtime_owner", None) is not None
                    and not self.shared.classification_ready
                ),
            )
        elif action == Action.PRECISE:
            physical = getattr(self.shared, "c4_runtime_owner", None) is not None
            if channel == 3 and physical:
                episode = getattr(self.shared, "c3_transfer_episode", None)
                if episode is not None and episode.unresolved:
                    return  # One unresolved reservation owns this release.
                reserve = getattr(self.shared, "reserve_c4_transfer", None)
                if not callable(reserve):
                    return  # Physical mode fails closed without a custody owner.
                evidence, view = self._capture_c3_release(state)
                if not reserve(state, evidence):
                    return
            moved = self._move(
                f"{label}_exit",
                channel,
                stepper,
                cfg.exit_pulse_output_deg,
                cfg.exit_pulse_pause_ms,
                cfg,
                enforce_min=False,
                c3_safe_staging=False,
            )
            if channel == 3 and physical and moved:
                self.shared.c3_release_evidence = evidence
                self.shared.c3_release_view = view
                leader = next(iter(getattr(state, "pieces", ())), None)
                self.shared.c3_release_leader_id = getattr(leader, "sv_bt_track_id", None)
                self._on_ch3_release_attempt()
        # IDLE / FREEZE: no move.

    def _capture_c3_release(self, state):
        from ..go_to_angle.recovery import capture_support

        evidence = {}
        try:
            evidence = capture_support(self.gc.perception_service)
            leader = next(iter(getattr(state, "pieces", ())), None)
            bbox = getattr(leader, "bbox", None)
            matches = [p for p in evidence.get("material", []) if bbox and
                max(bbox[0], p['bbox'][0]) < min(bbox[2], p['bbox'][2]) and
                max(bbox[1], p['bbox'][1]) < min(bbox[3], p['bbox'][3])]
            if matches:
                evidence['anchor'] = {
                    'low': min(p['low'] for p in matches),
                    'high': max(p['high'] for p in matches),
                    'radius_low': min(p['radius_low'] for p in matches),
                    'radius_high': max(p['radius_high'] for p in matches),
                }
                evidence['group_size_unknown'] = len(matches) > 1
                evidence['followers'] = [p for p in evidence['material'] if p not in matches]
                evidence['motion_deg'] = self._cfg().exit_pulse_output_deg
                evidence['leader_com'] = min(p['com'] for p in matches)
        except Exception as exc:
            self.gc.logger.warning(f"PulsePerception: C3 release evidence unavailable: {exc}")
        view = None
        try:
            from recognition_views import capture_release_view

            leader = next(iter(getattr(state, "pieces", ())), None)
            view = capture_release_view(
                self.gc.perception_service, evidence,
                getattr(leader, "sv_bt_track_id", None),
            )
        except Exception:
            pass  # Optional image capture cannot block the physical release.
        return evidence, view

    def _on_ch3_release_attempt(self) -> None:
        notifier = getattr(self.shared, "publish_piece_release_attempt", None)
        if callable(notifier):
            try:
                notifier(
                    source=StationId.C3,
                    target=StationId.CLASSIFICATION,
                    started_at_mono=time.monotonic(),
                )
            except Exception as exc:
                self.gc.logger.warning(
                    f"PulsePerception: C3 release attempt notification failed: {exc}"
                )
        self._classification_pending_until = (
            time.monotonic() + CLASSIFICATION_PENDING_ADMISSION_MS / 1000.0
        )

    def _recovery_cfg(self):
        # Keep the accepted recovery-only policy, but use the active Pulse owner
        # for C3 direction, speed, move clamp and pulse pause.
        from toml_config import getGoToAngleConfig
        from ..go_to_angle.config import configFromDict

        now = time.monotonic()
        if (not hasattr(self, "_recovery_config")
                or now - self._recovery_config_loaded_at >= _CONFIG_TTL_S):
            try:
                self._recovery_config = configFromDict(getGoToAngleConfig())
            except Exception as exc:
                self.gc.logger.warning(
                    f"PulsePerception: C3 recovery config load failed: {exc}"
                )
            self._recovery_config_loaded_at = now
        pulse = self._cfg()
        return replace(
            self._recovery_config,
            forward_direction_sign=pulse.forward_direction_sign,
            move_speed_usteps_per_s=pulse.ch3_move_speed_usteps_per_s,
            max_move_output_deg=pulse.ch3_max_move_output_deg,
            precise_pulse_pause_ms=pulse.exit_pulse_pause_ms,
        )

    def _owned_recovery_move(
        self,
        label: str,
        stepper: "StepperMotor",
        output_deg: float,
        settle_ms: int,
        cfg: GoToAngleConfig,
        enforce_min: bool = True,
        recovery: bool = False,
        c3_recovery_direction: int | None = None,
        c3_recovery_deadline: float | None = None,
        c3_safe_staging: bool = False,
        c3_recovery_audit: dict | None = None,
    ) -> bool:
        if c3_recovery_direction is not None and (
                c3_recovery_direction not in (-1, 1)
                or stepper is not getattr(self.irl, "c_channel_3_rotor_stepper", None)):
            raise ValueError("explicit recovery direction is restricted to C3")
        if self._busy(stepper):
            self.gc.runtime_stats.observePulse(label, "busy", time.monotonic())
            return False
        if getattr(stepper, "software_disabled", False):
            return False  # Suppressed commands cannot own or announce a move.
        speed = int(cfg.move_speed_usteps_per_s)
        output_deg = abs(output_deg)
        if enforce_min:
            output_deg = max(cfg.min_move_output_deg, output_deg)
        if not recovery:
            output_deg = min(cfg.max_move_output_deg, output_deg)
        sign = 1 if cfg.forward_direction_sign >= 0 else -1
        if c3_recovery_direction is not None:
            sign *= c3_recovery_direction
        motor_deg = sign * output_deg * CHANNEL_OUTPUT_GEAR_RATIO
        # Set the move speed and tell the motor to move to the angle — that's it.
        # We NEVER set acceleration here; the motor keeps whatever acceleration it
        # already has.
        try:
            start_position = int(stepper.position)
        except Exception as exc:
            self.gc.logger.warning(f"GoToAngle: {label} position unavailable: {exc}")
            self.gc.runtime_stats.observePulse(label, "failed", time.monotonic())
            return False
        target = start_position + stepper.microsteps_for_degrees(motor_deg)
        try:
            if recovery:
                stepper.enabled = True
            stepper.set_speed_limits(16 if recovery else 0, max(16, speed) if recovery else speed)
        except Exception as exc:
            self.gc.logger.warning(f"GoToAngle: {label} speed set failed: {exc}")
        exec_ms = stepper.estimateMoveDegreesMs(
            abs(motor_deg), max_speed=speed or 5000
        )
        if c3_recovery_deadline is not None and (
                time.monotonic() + (max(0, exec_ms) + max(0, settle_ms))/1000.0
                > c3_recovery_deadline):
            if c3_recovery_audit is not None:
                c3_recovery_audit['dispatch_refused'] = 'leg_within_time_budget'
            return False  # Hardware preparation cannot extend the recovery budget.
        # Retain ownership if the acknowledgement is lost after acceptance.
        # Only an explicit rejection can discard it without completion proof.
        self._move_targets[stepper._name] = target
        self._completion_checked_tick.pop(stepper._name, None)
        self._busy_until[stepper._name] = time.monotonic() + (max(0, exec_ms) + max(0, settle_ms)) / 1000.0
        is_c3 = stepper is getattr(self.irl, "c_channel_3_rotor_stepper", None)
        if is_c3:
            self.shared.c3_motion_pending = True
            self.shared.c3_safe_staging_pending = c3_safe_staging
        try:
            success = stepper.move_degrees(motor_deg)
        except Exception:
            self.gc.runtime_stats.observePulse(label, "failed", time.monotonic())
            raise
        cooldown_ms = (max(0, exec_ms) + max(0, settle_ms)) if success else 500
        now_mono = time.monotonic()
        self._busy_until[stepper._name] = now_mono + cooldown_ms / 1000.0
        if not success:
            self._move_targets.pop(stepper._name, None)
            if is_c3:
                self.shared.c3_motion_pending = False
                self.shared.c3_safe_staging_pending = False
        self.gc.runtime_stats.observePulse(label, "sent" if success else "failed", now_mono)
        self.gc.logger.info(
            f"GoToAngle: {label} move output={output_deg:.1f}° motor={motor_deg:.1f}° "
            f"success={success} exec_ms={exec_ms} settle_ms={settle_ms}"
        )
        return success

    def _recover_transfer(self, episode, boundary, *, action=None):
        """Evaluate all gates, then submit at most one C3 leg through its owner."""
        from ..go_to_angle.recovery import (capture_support, assess_support, followthrough_to_exit_end,
                               observed_forward_progress, retained_path, UNCERTAINTY_DEG)
        now, wall = time.monotonic(), time.time()
        cfg = self._recovery_cfg()
        evidence = {}
        evidence_error = None
        try:
            evidence = capture_support(self.gc.perception_service)
        except Exception as exc:
            evidence_error = str(exc)
        motors = {}
        probe_motors = episode.recovery_started_mono is not None or action is not None
        for channel in (2, 3):
            stepper = getattr(self.irl, f"c_channel_{channel}_rotor_stepper", None)
            try:
                if not probe_motors and stepper is not None:
                    # Normal arrival only tracks spatial continuity. Completion
                    # stays with the existing feeder tick; add no normal-path I/O.
                    held = (stepper._name in self._move_targets or
                            now < self._busy_until.get(stepper._name, 0.0))
                    motors[str(channel)] = {'busy': held, 'stopped': None, 'position': None,
                        'target': self._move_targets.get(stepper._name),
                        'suppressed': bool(getattr(stepper, 'software_disabled', False)),
                        'source': 'existing_owner_bookkeeping'}
                    continue
                busy = self._busy(stepper) if stepper is not None else True
                motors[str(channel)] = {'busy': busy, 'stopped': bool(stepper.stopped),
                    'position': int(stepper.position), 'target': self._move_targets.get(stepper._name),
                    'suppressed': bool(getattr(stepper, 'software_disabled', False))}
            except Exception as exc:
                motors[str(channel)] = {'busy': True, 'stopped': False, 'error': str(exc)}
        now, wall = time.monotonic(), time.time()
        fresh = bool(evidence) and 0 <= wall-evidence['ts'] <= 1.5
        if (not motors['3']['busy'] and (motors['3']['stopped'] or not probe_motors) and
                not getattr(self.shared, 'c3_motion_pending', False)):
            episode.release_evidence.setdefault('completion_observed_wall', wall)
        requested = action.get('degrees') if action else None
        support = assess_support(episode, evidence, fresh=fresh,
                                 moving=motors['3']['busy'], proposed=requested)
        retained_plan = retained_path(episode, evidence, fresh=fresh, cfg=cfg,
                                      gear_ratio=CHANNEL_OUTPUT_GEAR_RATIO)
        if retained_plan and retained_plan['forward_degrees'] is not None:
            # Preserve strict follower inequality at actual motor resolution.
            import math
            try:
                resolution = abs(self.irl.c_channel_3_rotor_stepper.microsteps_for_degrees(360))/360*CHANNEL_OUTPUT_GEAR_RATIO
                clearance = retained_plan['forward_clearance_deg']
                steps = math.ceil(clearance*resolution)-1 if clearance is not None else 1
                if resolution > 0 and steps < 1:
                    retained_plan['forward_degrees'] = None
                    retained_plan['jitter'] = retained_plan['jitter_available']
                    if not retained_plan['jitter']:
                        retained_plan['reason'] = 'follower clearance permits no positive motor step or bounded jitter'
            except Exception:
                pass  # Existing dispatch motion_estimate_available veto remains.
        retained = bool(action and action.get('retained'))
        if retained and retained_plan:
            support = {**support, 'material':[retained_plan['piece']],
                       'followers':retained_plan['followers'],
                       'forward_clearance_deg':retained_plan['forward_clearance_deg']}
        endpoint_available = True
        clip_error = None
        observed_com, progress = observed_forward_progress(episode, support['material'])
        jitter = bool(action and action.get('kind') == 'jitter')
        if jitter:
            requested = float(cfg.jitter_amplitude_motor_deg)/CHANNEL_OUTPUT_GEAR_RATIO
            action = {**action, 'degrees': requested}
            support = assess_support(episode, evidence, fresh=fresh,
                                     moving=motors['3']['busy'], proposed=requested)
        elif action:
            requested = retained_plan['forward_degrees'] if retained and retained_plan else followthrough_to_exit_end(
                evidence, support, margin=cfg.ch3_release_margin_output_deg,
                maximum=abs(cfg.max_move_output_deg))
            endpoint_available = requested is not None
            # Preserve the exact sweep predicate, including strict inequality,
            # after motor rounding. Partial safe travel is useful; it is not an
            # assertion that the leader will move by the commanded distance.
            clearance = support['forward_clearance_deg']
            if requested is not None and clearance is not None:
                import math
                stepper = self.irl.c_channel_3_rotor_stepper
                try:
                    per_degree = abs(stepper.microsteps_for_degrees(360))/360*CHANNEL_OUTPUT_GEAR_RATIO
                    if not math.isfinite(per_degree) or per_degree <= 0:
                        raise ValueError('invalid C3 motor conversion')
                    safe_steps = max(0, math.ceil(clearance*per_degree)-1)
                    requested = min(requested, safe_steps/per_degree)
                except Exception as exc:
                    clip_error = str(exc)
                    requested = None
                    endpoint_available = False
            action = {**action, 'degrees': requested or 0.0}
            requested = action['degrees']
            support = assess_support(episode, evidence, fresh=fresh,
                                     moving=motors['3']['busy'], proposed=requested)
        if retained and retained_plan:
            # Reacquire only the exact original ID. Every other detection stays
            # in the sweep calculation, even if boxes touch or overlap.
            support = {**support, 'same_piece_retained':True,
                'material':[retained_plan['piece']], 'followers':retained_plan['followers'],
                'forward_clearance_deg':retained_plan['forward_clearance_deg'],
                'predicates':{**support['predicates'], 'spatial_association':True,
                    'c3_supported_region':True,
                    'forward_sweep_clear':all(p['low'] > abs(requested or 0)
                                              for p in retained_plan['followers']),
                    'reverse_sweep_clear':retained_plan['reverse_sweep_clear']}}
            observed_com = retained_plan['observed_com']
            progress = retained_plan['observed_progress_deg']
        predicates = {**boundary['predicates'], **support['predicates'],
            'active_episode': episode is getattr(self.shared, 'c3_transfer_episode', None),
            'episode_open': episode.recovery_open,
            'c3_enabled': bool(self._cfg().enable_ch3),
            'c3_frame_fresh': fresh,
            'feeder_tick_fresh': 0 <= now-getattr(self, '_last_perception_tick', 0) <= 1.0,
            'motor_c2_resolved': not motors['2']['busy'] and motors['2']['stopped'],
            'motor_c3_resolved': not motors['3']['busy'] and motors['3']['stopped'],
            'motors_unsuppressed': not any(m.get('suppressed', False) for m in motors.values()),
            'c3_owner_consistent': bool(getattr(self.shared, 'c3_motion_pending', False)) == motors['3']['busy'],
            'recovery_budget': episode.recovery_deadline_mono is None or now < episode.recovery_deadline_mono}
        command_motor_degrees = None
        command_microsteps = None
        command_error = clip_error
        if action:
            predicates['followthrough_endpoint_available'] = endpoint_available
            number = len(episode.current_recovery_legs)+1
            expected_key = ('terminal.' if episode.terminal_recovery_active else '') + f'{number}.forward'
            predicates['continuation_sequence'] = action['stage'] == 1 and action['key'] == expected_key
            predicates['leg_unspent'] = not any(l['key'] == action['key'] for l in episode.recovery_legs)
            predicates['observed_forward_progress'] = (
                episode.terminal_recovery_active and not episode.current_recovery_legs or
                progress is not None and progress > 2*UNCERTAINTY_DEG)
            predicates['positive_safe_travel'] = requested > 0
            stepper = self.irl.c_channel_3_rotor_stepper
            command_motor_degrees = requested*CHANNEL_OUTPUT_GEAR_RATIO*(1 if cfg.forward_direction_sign >= 0 else -1)
            try:
                command_microsteps = stepper.microsteps_for_degrees(command_motor_degrees)
                predicates['positive_safe_travel'] = requested > 0 and abs(command_microsteps) > 0
                required_seconds = (stepper.estimateMoveDegreesMs(abs(command_motor_degrees),
                    max_speed=cfg.move_speed_usteps_per_s or 5000)+cfg.precise_pulse_pause_ms)/1000.0
            except Exception as exc:
                command_error = str(exc)
                required_seconds = float('inf')
            predicates['motion_estimate_available'] = command_error is None
            predicates['leg_within_time_budget'] = (episode.recovery_deadline_mono is not None and
                                                  now+required_seconds <= episode.recovery_deadline_mono)
            completion_wall = (episode.current_recovery_legs[-1].get('completed_at_wall')
                               if episode.current_recovery_legs else episode.release_evidence.get('completion_observed_wall'))
            predicates['post_motion_frames'] = (completion_wall is not None and
                evidence.get('ts', 0) > completion_wall and boundary['frame_ts'] > completion_wall)
        if jitter:
            count = sum(l.get('kind') == 'jitter' for l in episode.recovery_legs)
            predicates.update({
                'same_piece_retained': support.get('same_piece_retained', False),
                'c3_supported_region': support.get('same_piece_retained', False),
                'followthrough_endpoint_available': True,
                'observed_forward_progress': True,
                'continuation_sequence': action['stage'] == 2 and action['key'] == f'{count+1}.jitter',
                'jitter_allowance': count < min(3, int(cfg.fall_recovery_max_jitter_attempts)),
                # Reverse clearance is calculated for 1.5 output degrees.
                'jitter_profile_safe': 0 < requested <= min(1.5, abs(cfg.max_move_output_deg))
                    and cfg.jitter_speed_usteps_per_s > 0 and cfg.jitter_accel_usteps_per_s2 > 0,
            })
            try:
                import math
                amplitude = abs(command_microsteps)
                speed = int(cfg.jitter_speed_usteps_per_s)
                accel = int(cfg.jitter_accel_usteps_per_s2)
                # Four amplitude lengths, including each acceleration/deceleration.
                required_seconds = 4*(amplitude/speed + 2*math.sqrt(amplitude/accel)) + cfg.jitter_pause_ms/1000
                predicates['leg_within_time_budget'] = (episode.recovery_deadline_mono is not None
                    and now+required_seconds <= episode.recovery_deadline_mono)
            except (TypeError, ValueError, ZeroDivisionError):
                predicates['motion_estimate_available'] = False
        if retained:
            # A positively identified finite path is bounded by existing command
            # limits and fresh geometry, not the expired uncertain-arrival clock.
            permitted = bool(retained_plan and not retained_plan['reason'] and
                             jitter == retained_plan['jitter'])
            predicates['retained_path_available'] = permitted
            predicates['recovery_budget'] = permitted
            predicates['leg_within_time_budget'] = permitted and predicates.get('motion_estimate_available',False)
        decision = {**support, 'episode_id': episode.episode_id, 'at_wall': wall, 'at_mono': now,
            'stage': action['stage'] if action else episode.recovery_stage,
            'leg': action['key'] if action else None, 'branch': episode.recovery_branch,
            'signed_output_degrees': requested, 'frame_ts': evidence.get('ts'),
            'observed_com': observed_com, 'observed_progress_deg': progress,
            'frame_age_s': wall-evidence['ts'] if evidence else None,
            'feeder_tick_age_s': now-getattr(self, '_last_perception_tick', 0),
            'evidence_error': evidence_error or (None if evidence else 'paired C3 frame/geometry unavailable'),
            'command_error': command_error,
            'boundary': boundary, 'motors': motors,
            'predicates': predicates, 'failed_predicates': [k for k,v in predicates.items() if not v],
            'result': 'observed', 'retained_plan':retained_plan}
        episode.recovery_decision = decision
        if action is None:
            return decision
        # Reverse clearance is a branch selector, not a forward-motion veto.
        required = set(predicates) if jitter else set(predicates)-{'reverse_sweep_clear'}
        if retained:
            required.discard('observed_forward_progress')
        if requested < 0:
            required.add('reverse_sweep_clear')
            required.discard('forward_sweep_clear')
        failed = [k for k in required if not predicates[k]]
        if failed:
            waitable = {'c3_frame_fresh', 'feeder_tick_fresh', 'motor_c2_resolved',
                        'motor_c3_resolved', 'c4_empty', 'c4_frame_fresh', 'post_motion_frames'}
            # Stale samples may not establish support; wait within the same budget.
            if not fresh:
                waitable |= {'spatial_association', 'c3_supported_region', 'forward_sweep_clear',
                             'reverse_sweep_clear', 'followthrough_endpoint_available',
                             'observed_forward_progress', 'positive_safe_travel', 'same_piece_retained'}
            if not predicates.get('post_motion_frames', True):
                waitable.add('observed_forward_progress')
            decision['result'] = 'wait' if set(failed) <= waitable else 'unsafe'
            decision['blocking_predicates'] = sorted(failed)
            return decision
        leg = {**action, 'issued_at_mono': now, 'issued_at_wall': wall,
               'accepted': None, 'completed_at_mono': None,
               'observed_com': observed_com, 'observation_ts': evidence.get('ts'),
               'observed_progress_deg': progress,
               'motor_degrees': command_motor_degrees, 'microsteps': command_microsteps}
        # Spend the leg before I/O. A lost acknowledgement must never replay it.
        episode.recovery_legs.append(leg)
        episode.recovery_stage = action['stage']
        episode.support_motion_deg = requested
        if action['stage'] == 1:
            episode.followthrough_issued = True
        stepper = self.irl.c_channel_3_rotor_stepper
        try:
            motion_deadline = float('inf') if retained else episode.recovery_deadline_mono
            accepted = self._owned_jitter(stepper, cfg, required_seconds, motion_deadline) if jitter else self._owned_recovery_move('ch3_followthrough' if action['stage'] == 1 else 'ch3_transport_recovery',
                stepper, abs(requested), cfg.precise_pulse_pause_ms, cfg,
                enforce_min=False, c3_recovery_direction=-1 if requested < 0 else 1,
                c3_recovery_deadline=motion_deadline, c3_recovery_audit=leg)
            leg['accepted'] = accepted
            leg['target'] = self._move_targets.get(stepper._name)
            if retained and accepted:
                leg['completion_due_mono'] = self._busy_until[stepper._name]
            decision['result'] = 'accepted' if accepted else 'rejected'
            if not accepted and time.monotonic()+required_seconds > motion_deadline:
                decision['result'] = 'unsafe'
                decision['predicates']['leg_within_time_budget'] = False
                decision['failed_predicates'].append('leg_within_time_budget')
                decision['blocking_predicates'] = ['leg_within_time_budget']
            if accepted and action['stage'] == 1:
                episode.followthrough_count += 1
                episode.followthrough_at_mono = now
        except Exception as exc:
            leg['error'] = str(exc)
            leg['target'] = self._move_targets.get(stepper._name)
            decision['result'] = 'acceptance_unknown'
        decision['command'] = dict(leg)
        return decision

    def _owned_jitter(self, stepper, cfg, duration, deadline):
        """One original firmware cycle; the normal feeder owner holds its origin."""
        if (self._busy(stepper) or stepper.software_disabled
                or time.monotonic()+duration > deadline):
            return False
        origin = int(stepper.position)
        name = stepper._name
        self._move_targets[name] = origin
        self._jitter_owned.add(name)
        self._completion_checked_tick.pop(name, None)
        self._busy_until[name] = time.monotonic()+duration
        self.shared.c3_motion_pending = True
        self.shared.c3_safe_staging_pending = False
        # No force: software suppression remains effective. Unknown acceptance
        # retains this owner exactly as an uncertain normal move does.
        accepted = stepper.jitter_degrees(cfg.jitter_amplitude_motor_deg, 1,
            int(cfg.jitter_speed_usteps_per_s), int(cfg.jitter_accel_usteps_per_s2))
        if not accepted:
            self._move_targets.pop(name, None)
            self._jitter_owned.discard(name)
            self.shared.c3_motion_pending = False
        return accepted

    def cleanup(self) -> None:
        super().cleanup()
