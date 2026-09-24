import time
from dataclasses import replace
from typing import Optional, TYPE_CHECKING

from states.base_state import BaseState
from subsystems.shared_variables import SharedVariables
from subsystems.bus import StationId
from defs.channel import ChannelDetection
from irl.config import IRLInterface, IRLConfig
from global_config import GlobalConfig
from vision import VisionManager

from ..states import FeederState
from ..analysis import analyzeFeederChannels
from .config import GoToAngleConfig
from .eject import EjectController
from . import geometry

# Exit handling is a per-channel strategy. A channel runs in either:
#  - precise-pulse mode (default): meter the piece into the exit one small pulse
#    at a time, gated on downstream readiness (``_apply_action`` PRECISE), or
#  - fast-eject mode (C3 by default): one quick move that balances the piece's
#    bbox COM on the exit's fall-off edge, then an explicit watch-for-fall +
#    jitter recovery (``EjectController`` in eject.py).
# Jitter now fires ONLY inside the fast-eject fall-recovery procedure — the old
# exit-dwell jitter has been removed. The perception sub-path (``_step_perception``)
# is where fast-eject lives; the legacy-vision sub-path keeps precise pulsing only.

if TYPE_CHECKING:
    from hardware.sorter_interface import StepperMotor

# Motor-shaft to channel-output gear ratio. One output (LEGO wheel) degree
# requires this many motor degrees. Matches the reactive flow's constant.
CHANNEL_OUTPUT_GEAR_RATIO = 130.0 / 12.0

# Re-read the tuning config from disk at most this often so the tuning page
# takes effect live without a restart, without hammering the filesystem.
_CONFIG_TTL_S = 1.0

# After a C3 exit dispense, keep C3 blocked this long so the in-flight piece
# can register downstream before we consider another move.
CLASSIFICATION_PENDING_ADMISSION_MS = 1500


def _leading_track_id(state) -> Optional[int]:
    pieces = getattr(state, "pieces", ())
    return getattr(pieces[0], "sv_bt_track_id", None) if pieces else None


class GoToAngleFeeding(BaseState):
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
        self._config: GoToAngleConfig = GoToAngleConfig()
        self._config_loaded_at: float = 0.0
        self._classification_pending_until: float = 0.0
        self._ch3_was_at_exit: bool = False
        # Per-channel fast-eject controllers, lazily built on first use (steppers
        # may not be ready at __init__ in test contexts). Only channels running
        # in fast-eject mode get one.
        self._eject_controllers: dict[int, EjectController] = {}
        # Per-channel monotonic timestamp of the last frame that reported a piece
        # in the drop zone. Drives the C2/C3 drop-zone occupancy latch.
        self._drop_seen_at: dict[int, float] = {}
        machine_setup = getattr(irl_config, "machine_setup", None)
        self._classification_setup = bool(
            machine_setup is not None
            and getattr(machine_setup, "uses_classification_channel", False)
        )

    def _cfg(self) -> GoToAngleConfig:
        now = time.monotonic()
        if now - self._config_loaded_at >= _CONFIG_TTL_S:
            try:
                from toml_config import getGoToAngleConfig
                from .config import configFromDict
                self._config = configFromDict(getGoToAngleConfig())
            except Exception as exc:
                self.gc.logger.warning(f"GoToAngle: config load failed: {exc}")
            self._config_loaded_at = now
        return self._config

    def _busy(self, stepper: "StepperMotor") -> bool:
        # C1/C2 keep their installed scheduling/completion behavior.
        if stepper is not getattr(self.irl, "c_channel_3_rotor_stepper", None):
            return time.monotonic() < self._busy_until.get(stepper._name, 0.0)
        name = stepper._name
        if time.monotonic() < self._busy_until.get(name, 0.0):
            return True
        target = self._move_targets.get(name)
        if target is None:
            return False
        if getattr(stepper, "software_disabled", False):
            # stopped is synthetic True while suppressed, not firmware proof.
            return True
        if self._completion_checked_tick.get(name) == self._motion_tick:
            return True
        self._completion_checked_tick[name] = self._motion_tick
        try:
            # Fresh synchronous queries AFTER the accepted move and its
            # estimated/settle interval. Idle alone is not proof of completion.
            if name in self._jitter_owned and stepper.is_jittering():
                return True
            if not stepper.stopped or int(stepper.position) != target:
                return True
        except Exception as exc:
            self.gc.logger.warning(f"GoToAngle: {name} completion unavailable: {exc}")
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
        stepper: "StepperMotor",
        output_deg: float,
        settle_ms: int,
        cfg: GoToAngleConfig,
        enforce_min: bool = True,
        c3_recovery_direction: int | None = None,
        c3_recovery_deadline: float | None = None,
        c3_safe_staging: bool = False,
        c3_recovery_audit: dict | None = None,
    ) -> bool:
        if c3_recovery_direction is not None and (
                c3_recovery_direction not in (-1, 1)
                or stepper is not getattr(self.irl, "c_channel_3_rotor_stepper", None)):
            raise ValueError("explicit recovery direction is restricted to C3")
        if stepper is not getattr(self.irl, "c_channel_3_rotor_stepper", None):
            return self._move_untracked(label, stepper, output_deg, settle_ms, cfg, enforce_min)
        if self._busy(stepper):
            self.gc.runtime_stats.observePulse(label, "busy", time.monotonic())
            return False
        if getattr(stepper, "software_disabled", False):
            return False  # Suppressed commands cannot own or announce a move.
        speed = int(cfg.move_speed_usteps_per_s)
        output_deg = abs(output_deg)
        if enforce_min:
            output_deg = max(cfg.min_move_output_deg, output_deg)
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
            stepper.set_speed_limits(0, speed)
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


    def _move_untracked(
        self,
        label: str,
        stepper: "StepperMotor",
        output_deg: float,
        settle_ms: int,
        cfg: GoToAngleConfig,
        enforce_min: bool = True,
    ) -> bool:
        if self._busy(stepper):
            return False
        speed = int(cfg.move_speed_usteps_per_s)
        output_deg = abs(output_deg)
        if enforce_min:
            output_deg = max(cfg.min_move_output_deg, output_deg)
        output_deg = min(cfg.max_move_output_deg, output_deg)
        sign = 1 if cfg.forward_direction_sign >= 0 else -1
        motor_deg = sign * output_deg * CHANNEL_OUTPUT_GEAR_RATIO
        # Set the move speed and tell the motor to move to the angle — that's it.
        # We NEVER set acceleration here; the motor keeps whatever acceleration it
        # already has.
        try:
            stepper.set_speed_limits(0, speed)
        except Exception as exc:
            self.gc.logger.warning(f"GoToAngle: {label} speed set failed: {exc}")
        success = stepper.move_degrees(motor_deg)
        exec_ms = stepper.estimateMoveDegreesMs(
            abs(motor_deg), max_speed=speed or 5000
        )
        cooldown_ms = (max(0, exec_ms) + max(0, settle_ms)) if success else 500
        self._busy_until[stepper._name] = time.monotonic() + cooldown_ms / 1000.0
        self.gc.logger.info(
            f"GoToAngle: {label} move output={output_deg:.1f}° motor={motor_deg:.1f}° "
            f"success={success} exec_ms={exec_ms} settle_ms={settle_ms}"
        )
        return success

    def _pieces_for_channel(
        self, detections: list[ChannelDetection], channel_id: int
    ) -> list[tuple[float, ChannelDetection]]:
        out: list[tuple[float, ChannelDetection]] = []
        for det in detections:
            if det.channel_id != channel_id:
                continue
            rel = geometry.pieceRelativeAngle(det.bbox, det.channel)
            out.append((rel, det))
        return out

    def _piece_at_exit(
        self, channel_id: int, detections: list[ChannelDetection]
    ) -> bool:
        pieces = self._pieces_for_channel(detections, channel_id)
        for rel, det in pieces:
            if geometry.sectionForRelativeAngle(rel) in det.channel.exit_sections:
                return True
        return False

    def _service_channel(
        self,
        label: str,
        channel_id: int,
        stepper: "StepperMotor",
        detections: list[ChannelDetection],
        downstream_ready: bool,
        cfg: GoToAngleConfig,
    ) -> bool:
        if self._busy(stepper):
            return False
        pieces = self._pieces_for_channel(detections, channel_id)
        if not pieces:
            return False
        channel = pieces[0][1].channel
        exit_sections = channel.exit_sections

        at_exit = any(
            geometry.sectionForRelativeAngle(rel) in exit_sections
            for (rel, _) in pieces
        )
        if at_exit:
            # The ONLY condition under which a channel holds still: it has a
            # piece at its exit and the downstream channel can't accept it yet.
            # Precise mode otherwise — nudge one small fixed angle at a time,
            # pausing between pulses so the downstream channel can register the
            # piece before we push again. Each tick re-reads vision, so we stop
            # as soon as the piece clears the exit instead of dumping the train.
            if not downstream_ready:
                return False
            return self._move(
                f"{label}_precise",
                stepper,
                cfg.precise_pulse_output_deg,
                cfg.precise_pulse_pause_ms,
                cfg,
                enforce_min=False,
            )

        # No piece at the exit: advance freely to carry pieces forward and clear
        # this channel's own drop zone. Downstream readiness is irrelevant here —
        # channels run in parallel and only the exit push waits on downstream.
        return self._move(
            f"{label}_advance", stepper, cfg.advance_output_deg, cfg.settle_after_move_ms, cfg
        )

    def _on_ch3_dispense(self) -> None:
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

    def _recover_transfer(self, episode, boundary, *, action=None):
        """Evaluate all gates, then submit at most one C3 leg through its owner."""
        from .recovery import (capture_support, assess_support, followthrough_to_exit_end,
                               observed_forward_progress, retained_path, UNCERTAINTY_DEG)
        now, wall = time.monotonic(), time.time()
        cfg = self._cfg()
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
            'c3_enabled': bool(cfg.enable_ch3 and not self._fast_eject_enabled(3, cfg)),
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
            accepted = self._owned_jitter(stepper, cfg, required_seconds, motion_deadline) if jitter else self._move('ch3_followthrough' if action['stage'] == 1 else 'ch3_transport_recovery',
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


    def _classification_ready(self, cfg: GoToAngleConfig) -> bool:
        if not cfg.gate_ch3_on_classification_ready or not self._classification_setup:
            return True
        if time.monotonic() < self._classification_pending_until:
            return False
        return bool(self.shared.classification_ready)

    # ---------------------------------------------------------------------
    # Fast-eject controllers (perception path only).
    #
    # A channel running in fast-eject mode hands its exit handling to a
    # per-channel ``EjectController`` (see eject.py) instead of precise pulsing.
    # Controllers are built lazily on first use because the steppers may not be
    # ready at __init__ (e.g. in test contexts). Jitter recovery now lives
    # entirely inside the controller — it is the only place the feeder jitters.
    # ---------------------------------------------------------------------

    def _channel_stepper(self, ch: int):
        if ch == 2:
            return self.irl.c_channel_2_rotor_stepper
        if ch == 3:
            return self.irl.c_channel_3_rotor_stepper
        return None

    def _fast_eject_enabled(self, ch: int, cfg: GoToAngleConfig) -> bool:
        if ch == 3 and getattr(self.shared, "c4_runtime_owner", None) is not None:
            return False
        if ch == 2:
            return bool(cfg.ch2_fast_eject_enabled)
        if ch == 3:
            return bool(cfg.ch3_fast_eject_enabled)
        return False

    def _get_eject_controller(
        self, ch: int, cfg: GoToAngleConfig, perception_service
    ) -> Optional[EjectController]:
        ctrl = self._eject_controllers.get(ch)
        if ctrl is not None:
            return ctrl
        stepper = self._channel_stepper(ch)
        if stepper is None:
            return None

        def _advance_move(output_deg: float, _stepper=stepper) -> bool:
            # One closed-loop advance step: just tell the motor to move that many
            # channel-degrees at the normal move speed. Like every move here, it
            # never touches acceleration. Completion is detected by polling the
            # stepper's stopped state (see _is_stopped), not a time estimate.
            return self._move(
                f"ch{ch}_eject",
                _stepper,
                output_deg,
                cfg.settle_after_move_ms,
                cfg,
                enforce_min=False,
            )

        def _is_stopped(_stepper=stepper) -> bool:
            # One cheap firmware round-trip. On any query error, report stopped so
            # the controller keeps progressing rather than hanging mid-advance.
            try:
                if ch == 3 and self._busy(_stepper):
                    return False
                return bool(_stepper.stopped)
            except Exception:
                return ch != 3

        on_success = self._on_ch3_dispense if ch == 3 else (lambda: None)
        ctrl = EjectController(
            channel_id=ch,
            stepper=stepper,
            is_stopped=_is_stopped,
            advance_move=_advance_move,
            on_success=on_success,
            logger=self.gc.logger,
        )
        self._eject_controllers[ch] = ctrl
        return ctrl

    def step(self) -> Optional[FeederState]:
        self._motion_tick += 1
        stepper = getattr(self.irl, "c_channel_3_rotor_stepper", None)
        if stepper is not None:
            self._busy(stepper)
        cfg = self._cfg()
        runtime_stats = self.gc.runtime_stats

        can_run_started = time.perf_counter()
        can_run = self.gc.rotary_channel_steppers_can_operate_in_parallel or (
            not self.shared.chute_move_in_progress
        )
        runtime_stats.observePerfMs(
            "feeder.go_to_angle.can_run_ms",
            (time.perf_counter() - can_run_started) * 1000.0,
        )
        if not can_run:
            return FeederState.FEEDING

        perception_service = getattr(self.gc, "perception_service", None)
        if perception_service is not None:
            return self._step_perception(cfg, perception_service)

        detections_started = time.perf_counter()
        detections = self.vision.getFeederHeatmapDetections()
        runtime_stats.observePerfMs(
            "feeder.go_to_angle.get_feeder_detections_ms",
            (time.perf_counter() - detections_started) * 1000.0,
        )
        detection_available_started = time.perf_counter()
        detection_available, _reason = self.vision.getFeederDetectionAvailability()
        runtime_stats.observePerfMs(
            "feeder.go_to_angle.detection_availability_ms",
            (time.perf_counter() - detection_available_started) * 1000.0,
        )
        if not detection_available:
            return FeederState.FEEDING

        analyze_started = time.perf_counter()
        analysis = analyzeFeederChannels(detections)
        runtime_stats.observePerfMs(
            "feeder.go_to_angle.analyze_state_ms",
            (time.perf_counter() - analyze_started) * 1000.0,
        )

        # Channels run in parallel and independently. Each one advances freely
        # to clear its own drop zone; only its exit push waits on the downstream
        # channel being ready to accept (C3 -> C4 classification, C2 -> C3,
        # C1 -> C2).
        if cfg.enable_ch3:
            classification_ready_started = time.perf_counter()
            classification_ready = self._classification_ready(cfg)
            runtime_stats.observePerfMs(
                "feeder.go_to_angle.classification_ready_ms",
                (time.perf_counter() - classification_ready_started) * 1000.0,
            )
            ch3_step_started = time.perf_counter()
            self._service_channel(
                "ch3",
                3,
                self.irl.c_channel_3_rotor_stepper,
                detections,
                downstream_ready=classification_ready,
                cfg=cfg,
            )
            runtime_stats.observePerfMs(
                "feeder.go_to_angle.ch3_step_ms",
                (time.perf_counter() - ch3_step_started) * 1000.0,
            )
            # A piece counts as delivered the moment it clears C3's exit zone
            # (the precise pulses stop on their own once vision no longer sees
            # it there). Fire the downstream notification + admission window
            # once on that falling edge, not on every micro-pulse.
            ch3_exit_check_started = time.perf_counter()
            ch3_at_exit_now = self._piece_at_exit(3, detections)
            runtime_stats.observePerfMs(
                "feeder.go_to_angle.ch3_exit_check_ms",
                (time.perf_counter() - ch3_exit_check_started) * 1000.0,
            )
            if self._ch3_was_at_exit and not ch3_at_exit_now:
                self._on_ch3_dispense()
            self._ch3_was_at_exit = ch3_at_exit_now
        if cfg.enable_ch2:
            ch2_step_started = time.perf_counter()
            self._service_channel(
                "ch2",
                2,
                self.irl.c_channel_2_rotor_stepper,
                detections,
                downstream_ready=not analysis.ch3_dropzone_occupied,
                cfg=cfg,
            )
            runtime_stats.observePerfMs(
                "feeder.go_to_angle.ch2_step_ms",
                (time.perf_counter() - ch2_step_started) * 1000.0,
            )
        if cfg.enable_ch1:
            stepper = self.irl.c_channel_1_rotor_stepper
            ch1_gate_started = time.perf_counter()
            ch1_can_move = not analysis.ch2_dropzone_occupied and not self._busy(stepper)
            runtime_stats.observePerfMs(
                "feeder.go_to_angle.ch1_gate_ms",
                (time.perf_counter() - ch1_gate_started) * 1000.0,
            )
            if ch1_can_move:
                ch1_step_started = time.perf_counter()
                self._move(
                    "ch1", stepper, cfg.ch1_advance_output_deg, cfg.ch1_settle_after_move_ms, cfg
                )
                runtime_stats.observePerfMs(
                    "feeder.go_to_angle.ch1_step_ms",
                    (time.perf_counter() - ch1_step_started) * 1000.0,
                )

        return FeederState.FEEDING

    # ---------------------------------------------------------------------
    # Rev04 perception path
    # ---------------------------------------------------------------------

    def _latch_drop(self, ch: int, state, now: float, cfg: GoToAngleConfig):
        """Persist drop-zone occupancy for one feeder channel.

        Once a piece is seen in the drop zone we consider the zone occupied for
        ``drop_zone_persistence_ms`` after the last positive frame — a one/two
        frame detection dropout no longer reads as 'empty'. Only ``in_drop`` is
        latched; exit/precise/COM fields pass through untouched (the eject path
        must still see the live exit state). 0 disables the latch."""
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

    def _step_perception(self, cfg: GoToAngleConfig, perception_service) -> Optional[FeederState]:
        """The new mode-pair flow: read perception state, apply cascade, move.

        No detection list, no analyzeFeederChannels, no per-channel filter,
        no in-flow tracker/handoff. The cascade is a pure function over
        ``ChannelState`` booleans; this method just dispatches its output
        to the existing ``_move`` machinery.
        """
        from perception.cascade import Action, cascade
        from perception.state import EMPTY_STATE

        runtime_stats = self.gc.runtime_stats
        t0 = time.perf_counter()
        states = perception_service.read_states()
        runtime_stats.observePerfMs(
            "feeder.go_to_angle.read_states_ms",
            (time.perf_counter() - t0) * 1000.0,
        )
        c2 = states.get(2, EMPTY_STATE)
        c3 = states.get(3, EMPTY_STATE)
        c4 = states.get(4, EMPTY_STATE)

        now_mono = time.monotonic()
        self._motion_tick += 1
        self._last_perception_tick = now_mono
        owner = getattr(self.shared, "request_c3_recovery", None)
        if owner is not None and owner != self._recover_transfer:
            raise RuntimeError("C3 recovery already has a feeder owner")
        self.shared.request_c3_recovery = self._recover_transfer
        stepper = getattr(self.irl, "c_channel_3_rotor_stepper", None)
        if stepper is not None:
            self._busy(stepper)
        # Hold C2/C3 drop-zone occupancy across brief detector dropouts so the
        # cascade (and the ``not c3.in_drop`` upstream gate below) see a stable
        # "occupied" instead of flickering empty for a frame. Applied before the
        # cascade so every consumer reads the same latched value.
        c2 = self._latch_drop(2, c2, now_mono, cfg)
        c3 = self._latch_drop(3, c3, now_mono, cfg)

        actions = cascade(c2, c3, c4)

        if cfg.enable_ch3:
            # C3's downstream is the classification channel (C4). The feeder does
            # NOT define "ready" itself — the classification channel owns and
            # exposes it (shared.classification_ready, set per its active mode:
            # single-piece = whole channel empty, two-piece = drop zone clear).
            # The feeder just asks. The only feeder-side gate is the post-dispense
            # admission window (let an in-flight piece register first).
            c3_downstream_ready = (
                now_mono >= self._classification_pending_until
                and self._classification_ready(cfg)
            )
            # Extra hold: if the classification camera sees a piece sitting in
            # C3's annotated exit zone (a secondary/foreign zone on channel 4)
            # while classification is not ready, freeze C3 so we don't shove it
            # forward into a busy C4. No-op until such a zone is drawn — the
            # accessor returns False when no C3 secondary zone exists.
            if (
                not self._classification_ready(cfg)
                and perception_service.secondary_zone_occupied(4, source_channel=3)
            ):
                c3_downstream_ready = False
            c3_action = actions.c3
            if getattr(self.shared, "c4_runtime_owner", None) is not None:
                from perception.cascade import feederChannelAction
                c3_action = feederChannelAction(c3, downstream_clear=c3_downstream_ready, greedy=True)
            self._drive_channel(
                "ch3", 3, c3_action, c3, c4, c3_downstream_ready,
                self.irl.c_channel_3_rotor_stepper, cfg, perception_service, now_mono,
            )
        if cfg.enable_ch2:
            self._drive_channel(
                "ch2", 2, actions.c2, c2, c3, not c3.in_drop,
                self.irl.c_channel_2_rotor_stepper, cfg, perception_service, now_mono,
            )
        # C1 has no exit zone of its own — no fast-eject / recovery applies.
        if cfg.enable_ch1:
            stepper = self.irl.c_channel_1_rotor_stepper
            if actions.c1 == Action.ADVANCE and not self._busy(stepper):
                self._move(
                    "ch1",
                    stepper,
                    cfg.ch1_advance_output_deg,
                    cfg.ch1_settle_after_move_ms,
                    cfg,
                )

        return FeederState.FEEDING

    def _drive_channel(
        self,
        label: str,
        ch: int,
        action,
        state,
        downstream,
        downstream_ready: bool,
        stepper: "StepperMotor",
        cfg: GoToAngleConfig,
        perception_service,
        now: float,
    ) -> None:
        """Drive one feeder channel for a perception tick. Fast-eject channels
        hand their exit handling to the per-channel EjectController; when the
        controller does not take the tick (piece not near the exit), or for
        precise-mode channels, fall back to the normal cascade action."""
        # An open transfer is serviced only by request_c3_recovery. Normal
        # feeding must not become a second command writer during that episode.
        episode = getattr(self.shared, "c3_transfer_episode", None)
        if ch == 3 and episode is not None and episode.unresolved:
            return
        if ch == 3 and getattr(self.shared, "c4_runtime_owner", None) is not None:
            return self._drive_physical_c3(label=label, ch=ch, action=action, state=state,
                downstream=downstream, downstream_ready=downstream_ready,
                stepper=stepper, cfg=cfg, perception_service=perception_service, now=now)
        if self._fast_eject_enabled(ch, cfg):
            ctrl = self._get_eject_controller(ch, cfg, perception_service)
            if ctrl is not None:
                consumed = ctrl.tick(
                    state=state,
                    downstream=downstream,
                    downstream_ready=downstream_ready,
                    cfg=cfg,
                    now=now,
                )
                if consumed:
                    return
                # Not consumed ⇒ the controller is idle and the piece isn't near
                # the exit. Run the normal drop-zone advance/idle. The controller
                # owns every in-exit case, so ``action`` here is ADVANCE/IDLE,
                # never PRECISE.
        self._apply_action(
            label, action, stepper, cfg, advance_clearance_deg=state.advance_clearance_deg
        )

    def _apply_action(
        self,
        label: str,
        action,
        stepper: "StepperMotor",
        cfg: GoToAngleConfig,
        advance_clearance_deg: float | None = None,
    ) -> None:
        from perception.cascade import Action

        if self._busy(stepper):
            return
        if action == Action.ADVANCE:
            # Free advance to clear the drop zone, but never push the
            # most-forward piece into the exit zone: cap the move to its
            # forward distance to the exit edge. Once the piece reaches the
            # exit, the PRECISE/FREEZE branch (gated on downstream readiness)
            # meters it out instead of this ungated advance dumping it through.
            output_deg = cfg.advance_output_deg
            enforce_min = True
            if (
                advance_clearance_deg is not None
                and advance_clearance_deg < output_deg
            ):
                output_deg = advance_clearance_deg
                enforce_min = False
            self._move(
                f"{label}_advance",
                stepper,
                output_deg,
                cfg.settle_after_move_ms,
                cfg,
                enforce_min=enforce_min,
            )
        elif action == Action.PRECISE:
            self._move(
                f"{label}_precise",
                stepper,
                cfg.precise_pulse_output_deg,
                cfg.precise_pulse_pause_ms,
                cfg,
                enforce_min=False,
            )
        # IDLE / FREEZE: no move.

    def reconcile_verified_c3_pause(self, expected_position: int) -> None:
        """Cancel interrupted C3 work only after the installed pause barrier.

        Cancellation is not target completion and cannot retire a transfer.
        The coordinator has already verified fresh safe C3/C4 observations.
        """
        episode = getattr(self.shared, "c3_transfer_episode", None)
        if episode is not None and episode.unresolved:
            raise RuntimeError("Cannot cancel an unresolved C3 transfer")
        stepper = self.irl.c_channel_3_rotor_stepper
        if not stepper.stationary_verified():
            raise RuntimeError("C3 pause cancellation requires verified stop")
        position = int(stepper.position)
        if position != expected_position:
            raise RuntimeError("C3 moved after the verified pause boundary")
        name = stepper._name
        target = self._move_targets.pop(name, None)
        if target is not None:
            self._last_c3_cancelled_motion = {
                "target": target, "position": position,
                "reason": "verified_retained_pause", "at_wall": time.time(),
            }
        self._busy_until.pop(name, None)
        self._jitter_owned.discard(name)
        self._completion_checked_tick.pop(name, None)
        self.shared.c3_motion_pending = False
        self.shared.c3_safe_staging_pending = False

    def cleanup(self) -> None:
        super().cleanup()
        for ctrl in self._eject_controllers.values():
            ctrl.reset()

    def _drive_physical_c3(
        self,
        label: str,
        ch: int,
        action,
        state,
        downstream,
        downstream_ready: bool,
        stepper: "StepperMotor",
        cfg: GoToAngleConfig,
        perception_service,
        now: float,
    ) -> None:
        """Drive one feeder channel for a perception tick. Fast-eject channels
        hand their exit handling to the per-channel EjectController; when the
        controller does not take the tick (piece not near the exit), or for
        precise-mode channels, fall back to the normal cascade action."""
        from perception.cascade import Action

        # C4 owns release permission, not upstream staging. Reuse the existing
        # ADVANCE distance cap to stage before the exit while C4 is occupied.
        # Preserve the closed-gate hold for uncertain/in-flight transfers and
        # material already visible in the C3 throat from the C4 camera.
        if ch == 3 and not downstream_ready:
            clearance = state.advance_clearance_deg
            if action != Action.ADVANCE or clearance is None or clearance <= 0:
                return
            episode = getattr(self.shared, "c3_transfer_episode", None)
            if episode is not None and episode.unresolved:
                return
            if perception_service.secondary_zone_occupied(4, source_channel=3):
                return
            ctrl = self._eject_controllers.get(ch)
            if ctrl is not None and ctrl.phase != EjectPhase.IDLE:
                return
            self._apply_physical_action(label, action, stepper, cfg,
                               advance_clearance_deg=clearance, c3_safe_staging=True)
            return

        if self._fast_eject_enabled(ch, cfg):
            ctrl = self._get_eject_controller(ch, cfg, perception_service)
            if ctrl is not None:
                consumed = ctrl.tick(
                    state=state,
                    downstream=downstream,
                    downstream_ready=downstream_ready,
                    cfg=cfg,
                    now=now,
                )
                if consumed:
                    return
                # Not consumed ⇒ the controller is idle and the piece isn't near
                # the exit. Run the normal drop-zone advance/idle. The controller
                # owns every in-exit case, so ``action`` here is ADVANCE/IDLE,
                # never PRECISE.
        # ``action`` is derived from the latest camera frame, where C4 can look
        # momentarily empty while its rotor carries an admitted piece between
        # zones.  The classification state machine's readiness flag is the
        # authoritative hand-off gate across that motion.  Never let a transient
        # empty frame turn PRECISE back on while C4 is still processing a piece.
        precise_output_deg = None
        release_command = False
        if ch == 3 and action == Action.PRECISE:
            # Use observed COM distance as the initial rotor command, not as
            # predicted piece travel: frictional slip can leave it upstream.
            # Subsequent fresh observations and C4 arrival own completion. If the
            # measurement is temporarily unavailable, preserve the configured
            # release margin as the single authorized release attempt.
            release_margin = cfg.ch3_release_margin_output_deg
            gap = getattr(state, "exit_com_forward_deg", None)
            requested = (
                release_margin
                if gap is None
                else max(0.0, float(gap)) + release_margin
            )
            precise_output_deg = min(abs(float(cfg.max_move_output_deg)), requested)
            release_command = gap is None or precise_output_deg >= requested

        release_evidence = {}
        if ch == 3 and release_command:
            from .recovery import capture_support
            try:
                release_evidence = capture_support(perception_service)
                source = getattr(state, 'pieces', ())
                bbox = source[0].bbox if source else None
                matches = [p for p in release_evidence.get('material', []) if bbox and
                    max(bbox[0], p['bbox'][0]) < min(bbox[2], p['bbox'][2]) and
                    max(bbox[1], p['bbox'][1]) < min(bbox[3], p['bbox'][3])]
                if matches:
                    release_evidence['anchor'] = {
                        'low': min(p['low'] for p in matches), 'high': max(p['high'] for p in matches),
                        'radius_low': min(p['radius_low'] for p in matches),
                        'radius_high': max(p['radius_high'] for p in matches)}
                    release_evidence['group_size_unknown'] = len(matches) > 1
                    release_evidence["followers"] = [p for p in release_evidence["material"] if p not in matches]
                    release_evidence["motion_deg"] = precise_output_deg
                    release_evidence['leader_com'] = min(p['com'] for p in matches)
            except Exception as exc:
                self.gc.logger.warning(f"C3 release evidence unavailable: {exc}")
        release_view = None
        if ch == 3 and release_command:
            try:
                from recognition_views import capture_release_view
                release_view = capture_release_view(
                    perception_service, release_evidence, _leading_track_id(state))
            except Exception:
                pass  # Optional image failure never changes physical release.
        if ch == 3 and release_command:
            reserve = getattr(self.shared, "reserve_c4_transfer", None)
            if callable(reserve) and not reserve(state, release_evidence):
                return
        moved = self._apply_physical_action(
            label,
            action,
            stepper,
            cfg,
            advance_clearance_deg=state.advance_clearance_deg,
            precise_output_deg=precise_output_deg,
        )
        if (
            ch == 3
            and action == Action.PRECISE
            and moved
            and release_command
        ):
            # A motor command is only a release attempt. C4 owns the physical
            # arrival proof and publishes PieceDelivered after fresh intake
            # evidence; treating this command as delivery created empty logical
            # pockets whenever a piece slipped instead of falling.
            self.shared.c3_release_evidence = release_evidence
            self.shared.c3_release_view = release_view
            self.shared.c3_release_leader_id = _leading_track_id(state)
            self._on_ch3_release_attempt()

    def _apply_physical_action(
        self,
        label: str,
        action,
        stepper: "StepperMotor",
        cfg: GoToAngleConfig,
        advance_clearance_deg: float | None = None,
        precise_output_deg: float | None = None,
        c3_safe_staging: bool = False,
    ) -> bool:
        from perception.cascade import Action

        if self._busy(stepper):
            return False
        if action == Action.ADVANCE:
            # Free advance to clear the drop zone, but never push the
            # most-forward piece into the exit zone: cap the move to its
            # forward distance to the exit edge. Once the piece reaches the
            # exit, the PRECISE/FREEZE branch (gated on downstream readiness)
            # meters it out instead of this ungated advance dumping it through.
            output_deg = cfg.advance_output_deg
            enforce_min = True
            if (
                advance_clearance_deg is not None
                and advance_clearance_deg < output_deg
            ):
                output_deg = advance_clearance_deg
                enforce_min = False
            return self._move(
                f"{label}_advance",
                stepper,
                output_deg,
                cfg.settle_after_move_ms,
                cfg,
                enforce_min=enforce_min,
                c3_safe_staging=c3_safe_staging,
            )
        elif action == Action.PRECISE:
            return self._move(
                f"{label}_precise",
                stepper,
                (
                    cfg.precise_pulse_output_deg
                    if precise_output_deg is None
                    else precise_output_deg
                ),
                cfg.precise_pulse_pause_ms,
                cfg,
                enforce_min=False,
            )
        # IDLE / FREEZE: no move.
        return False

    def _on_ch3_release_attempt(self) -> None:
        """Arm C4 after a release command without claiming it delivered."""
        if hasattr(self.shared, "publish_piece_release_attempt"):
            try:
                self.shared.publish_piece_release_attempt(
                    source=StationId.C3,
                    target=StationId.CLASSIFICATION,
                    started_at_mono=time.monotonic(),
                )
            except Exception:
                pass
        self._classification_pending_until = (
            time.monotonic() + CLASSIFICATION_PENDING_ADMISSION_MS / 1000.0
        )


    def hold_motion(self) -> None:
        self._motion_tick += 1
        stepper = getattr(self.irl, "c_channel_3_rotor_stepper", None)
        if stepper is not None:
            self._busy(stepper)
