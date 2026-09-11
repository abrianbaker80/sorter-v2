import time
from typing import Optional

from subsystems.classification_channel.incidents import (
    publish_classification_track_lost_incident,
)
from subsystems.classification_channel.five_sector_platter import C4FiveSectorPlatter
from subsystems.classification_channel.states import ClassificationChannelState
from subsystems.common.jitter_recovery import JitterParams, JitterPhase, JitterSequence

from .auto_reject import C4AutoReject
from .base import Rev01BaseState
from .constants import C4_TRAVEL_SIGN, LOG_TAG


class Discharging(Rev01BaseState):
    """Move one staged piece into the chute and prove that it left C4.

    A distribution record is committed only after the absolute exit move and
    distinct post-motion frames show the whole one-piece C4 channel continuously
    clear. Any remaining detection blocks credit. A structural motion failure
    holds the machine with an incident. A recoverable
    loss of discharge evidence is rerouted through the normal bottom-reject
    path, then C4 is cleared and verified before the flow resumes.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._discharge_started_at: Optional[float] = None
        self._released = False
        self._faulted = False
        self._last_frame_ts = 0.0
        self._motion_issued = False
        self._awaiting_post_move_frame = False
        self._post_move_frame_not_before = 0.0
        self._clear_first_frame_ts: Optional[float] = None
        self._in_falloff_since_ts: Optional[float] = None
        self._seq: Optional[JitterSequence] = None
        self._jitter_attempts_seen = 0
        self._auto_reject = C4AutoReject(self)
        # legacy fallback only
        self._kick_started = False
        self._kick_done_at: Optional[float] = None

    def step(self) -> Optional[ClassificationChannelState]:
        self.setClassificationReady(False, "discharging")
        perception_service = getattr(self.gc, "perception_service", None)
        if perception_service is None:
            return self._step_legacy_fallback()
        return self._step_perception(perception_service)

    # ---- active perception path ----

    def _step_perception(
        self, perception_service
    ) -> Optional[ClassificationChannelState]:
        now = time.monotonic()
        cfg = self.ctx.config
        if self._discharge_started_at is None:
            self._discharge_started_at = now

        pending_reject = self.ctx.auto_reject_reason
        if pending_reject is not None and not self._auto_reject.active:
            multi_piece = bool(self.ctx.auto_reject_multi_piece)
            self.ctx.auto_reject_reason = None
            self.ctx.auto_reject_multi_piece = False
            self._beginAutoReject(pending_reject, multi_piece=multi_piece)
            return None

        if self._auto_reject.active:
            return self._auto_reject.step(now)

        if float(self.ctx.discharge_armed_frame_ts) <= 0.0:
            self._holdForEvidenceFailure(
                "C4 entered discharge without a fresh post-chute staging frame"
            )
            return None
        if self._faulted:
            return None

        stepper = getattr(self.irl, "carousel_stepper", None)
        if not self._motion_issued:
            try:
                if stepper is not None and not bool(stepper.stopped):
                    return None
            except Exception as exc:
                self._holdForEvidenceFailure(
                    f"C4 motion status unavailable before discharge: {exc}"
                )
                return None
            self._startIndexedExitMove(cfg, stepper)
            return None

        state = perception_service.read_state(4)
        n = int(state.n_pieces)
        exit_occupied = bool(state.in_exit)
        frame_ts = float(state.ts)
        new_frame = frame_ts > self._last_frame_ts
        if new_frame:
            self._last_frame_ts = frame_ts

        moving = stepper is not None and not bool(stepper.stopped)

        total_ms = (now - self._discharge_started_at) * 1000.0
        if total_ms >= float(cfg.discharge_total_timeout_ms):
            self._beginAutoReject(
                f"C4 could not confirm discharge within "
                f"{cfg.discharge_total_timeout_ms}ms "
                f"(n={n}, exit_occupied={exit_occupied})",
                multi_piece=bool(self.ctx.multi_feed_detected or n > 1),
            )
            return None

        # Let an in-flight jitter run to resolution, but never beyond the same
        # total discharge deadline used by ordinary moves.
        seq = self._seq
        if seq is not None and seq.is_active:
            if seq.phase == JitterPhase.PAUSE:
                # A retry may only be decided from a distinct frame captured
                # after the previous jitter stopped and C4 settled.
                if not new_frame or not self._postMotionFrameReady(
                    frame_ts, moving, cfg
                ):
                    return None
            phase = seq.tick(still_stuck=exit_occupied, now=now)
            if seq.attempts_made > self._jitter_attempts_seen:
                self._jitter_attempts_seen = seq.attempts_made
                self._noteMotion()
            if phase == JitterPhase.CLEARED:
                self.logger.info(
                    f"{LOG_TAG} DISCHARGING: jitter ended with an empty reading; "
                    "awaiting post-motion clear proof"
                )
                self._in_falloff_since_ts = None
            if phase == JitterPhase.EXHAUSTED:
                self._beginAutoReject(
                    "C4 jitter exhausted without confirmed post-motion clear",
                    multi_piece=bool(self.ctx.multi_feed_detected or n > 1),
                )
                return None
            if seq.is_active:
                return None  # JITTERING / PAUSE — let it finish

        # A move may finish before perception publishes the resulting frame.
        # Wait for motor stop, a settle interval, and a frame newer than that
        # interval before accepting clear evidence or issuing another move.
        if not self._postMotionFrameReady(frame_ts, moving, cfg):
            return None

        if not new_frame or frame_ts < float(self.ctx.discharge_armed_frame_ts):
            return None

        if self.ctx.observeMultiFeed(n, frame_ts, cfg.multi_feed_confirm_reads):
            self.logger.info(
                f"{LOG_TAG} DISCHARGING: multi-feed confirmed "
                f"({n} pieces over {cfg.multi_feed_confirm_reads} frames)"
            )
            self._beginAutoReject(
                f"C4 confirmed {n} pieces during a one-piece discharge",
                multi_piece=True,
            )
            return None

        # In the conservative one-piece flow the entire C4 channel must clear.
        # A detection elsewhere cannot be credited as a successful drop merely
        # because the calibrated exit arc blinked empty.
        if n == 0:
            if self._clear_first_frame_ts is None:
                self._clear_first_frame_ts = frame_ts
            clear_ms = (frame_ts - self._clear_first_frame_ts) * 1000.0
            if clear_ms >= float(cfg.discharge_clear_confirm_ms):
                self.stopStepper()
                self._releaseOnce()
                self.logger.info(
                    f"{LOG_TAG} DISCHARGING -> IDLE (distinct post-motion frames "
                    f"showed C4 clear for {clear_ms:.0f}ms)"
                )
                return ClassificationChannelState.IDLE
            return None

        self._clear_first_frame_ts = None

        in_falloff = bool(state.in_exit_majority)
        if in_falloff:
            if self._in_falloff_since_ts is None:
                self._in_falloff_since_ts = frame_ts
        else:
            self._in_falloff_since_ts = None

        if moving:
            return None

        falloff_ms = (
            0.0
            if self._in_falloff_since_ts is None
            else (frame_ts - self._in_falloff_since_ts) * 1000.0
        )
        if self._in_falloff_since_ts is not None and falloff_ms >= float(
            cfg.discharge_jitter_dwell_ms
        ):
            if not self._startJitter(cfg, falloff_ms):
                self._holdForEvidenceFailure(
                    "C4 piece was stuck in the fall-off zone and jitter was unavailable"
                )
            return None

        # The single indexed move already placed the pocket at the exit. Vision
        # now verifies the drop or identifies a jam; it never steers another
        # ordinary move. A persistent piece reaches the existing jitter/timeout
        # recovery above and is never silently credited.
        return None

    def _startIndexedExitMove(self, cfg, stepper) -> bool:
        if stepper is None:
            self._holdForEvidenceFailure("C4 carousel stepper unavailable")
            return False
        staging_target = self.ctx.c4_safe_staging_target_steps
        exit_target = self.ctx.c4_exit_target_steps
        if staging_target is None or exit_target is None:
            self._holdForEvidenceFailure(
                "C4 discharge entered without an absolute indexed route"
            )
            return False
        try:
            current_position = int(stepper.position)
        except Exception as exc:
            self._holdForEvidenceFailure(f"C4 absolute position unavailable: {exc}")
            return False
        if abs(current_position - int(staging_target)) > 1:
            self._holdForEvidenceFailure(
                "C4 left its indexed safe staging point before discharge "
                f"(position={current_position}, expected={staging_target})"
            )
            return False

        move_steps = int(exit_target) - current_position
        platter = C4FiveSectorPlatter.from_irl_config(self.irl_config)
        max_expected_steps = abs(
            platter.output_degrees_to_motor_microsteps(
                3.0 * platter.sector_size_deg
            )
        )
        if move_steps == 0 or abs(move_steps) > max_expected_steps:
            self._holdForEvidenceFailure(
                f"C4 indexed exit delta is invalid ({move_steps} microsteps)"
            )
            return False
        try:
            stepper.set_speed_limits(
                16,
                max(16, int(cfg.discharge_speed_usteps_per_s)),
            )
            acknowledged = bool(stepper.move_steps(move_steps))
        except Exception as exc:
            self._holdForEvidenceFailure(f"C4 indexed exit command failed: {exc}")
            return False
        if not acknowledged:
            self._holdForEvidenceFailure(
                f"C4 indexed exit move was not acknowledged "
                f"(requested={move_steps} microsteps)"
            )
            return False
        self._noteMotion()
        self.logger.info(
            f"{LOG_TAG} DISCHARGING: started absolute indexed exit move "
            f"(target={exit_target}, steps={move_steps})"
        )
        return True

    def _noteMotion(self) -> None:
        self._motion_issued = True
        self._awaiting_post_move_frame = True
        self._post_move_frame_not_before = 0.0
        self._clear_first_frame_ts = None

    def _postMotionFrameReady(self, frame_ts: float, moving: bool, cfg) -> bool:
        if not self._awaiting_post_move_frame:
            return True
        if moving:
            return False
        if self._post_move_frame_not_before == 0.0:
            self._post_move_frame_not_before = time.time()
            return False
        if (
            time.time() < self._post_move_frame_not_before
            or frame_ts < self._post_move_frame_not_before
        ):
            return False
        self._awaiting_post_move_frame = False
        self._post_move_frame_not_before = 0.0
        return True

    def _startJitter(self, cfg, falloff_ms: float) -> bool:
        seq = self._getOrBuildSeq(cfg)
        if seq is None:
            return False
        if not seq.is_active:
            self.logger.info(
                f"{LOG_TAG} DISCHARGING: piece stuck in fall-off region for "
                f"{falloff_ms:.0f}ms — jitter unstick"
            )
            if not seq.start():
                return False
            self._jitter_attempts_seen = seq.attempts_made
            self._noteMotion()
        return True

    def _beginAutoReject(self, reason: str, *, multi_piece: bool) -> None:
        self._auto_reject.start(reason, multi_piece=multi_piece)

    def _holdForEvidenceFailure(self, reason: str) -> None:
        self.stopStepper()
        if self._faulted:
            return
        self._faulted = True
        self.logger.error(f"{LOG_TAG} DISCHARGING: {reason}; holding without credit")
        try:
            publish_classification_track_lost_incident(
                self.gc,
                piece=self.ctx.known_object,
                reason=reason,
            )
        except Exception as exc:
            self.logger.warning(
                f"{LOG_TAG} could not publish C4 discharge incident: {exc}"
            )

    def ownsLostTrackFault(self, piece_uuid: str | None) -> bool:
        obj = self.ctx.known_object
        return bool(
            self._faulted
            and obj is not None
            and isinstance(piece_uuid, str)
            and obj.uuid == piece_uuid
        )

    def canRecoverLostTrack(self, piece_uuid: str | None) -> bool:
        """Whether this faulted discharge can be abandoned without credit."""
        return bool(
            self.ownsLostTrackFault(piece_uuid)
            and self.canAbandonLostTrackObject(piece_uuid)
        )

    def _releaseOnce(self) -> None:
        if self._released:
            return
        obj = self.ctx.known_object
        # advanceTransport (non-dynamic) shifts wait -> exit so the piece
        # distribution positioned now occupies the drop slot it reads from.
        # Distribution watches that slot transition itself (Ready -> Sending) to
        # know the piece was flung. Classification does NOT touch
        # ``distribution_ready`` — distribution is the sole owner of that gate.
        # The old cross-subsystem False edge here could be lost in a race and
        # wedge distribution in READY forever; the durable slot signal can't be.
        self.transport.advanceTransport()
        self._released = True
        if obj is not None:
            self.logger.info(
                f"{LOG_TAG} DISCHARGING: released piece {obj.uuid[:8]} to distribution"
            )

    def _getOrBuildSeq(self, cfg) -> Optional[JitterSequence]:
        if self._seq is not None:
            return self._seq
        stepper = getattr(self.irl, "carousel_stepper", None)
        if stepper is None:
            return None
        self._seq = JitterSequence(
            stepper,
            JitterParams(
                amplitude_motor_deg=cfg.jitter_amplitude_motor_deg,
                cycles=int(cfg.jitter_cycles),
                speed_usteps_per_s=int(cfg.jitter_speed_usteps_per_s),
                accel_usteps_per_s2=int(cfg.jitter_accel_usteps_per_s2),
                pause_ms=int(cfg.jitter_pause_ms),
                max_attempts=int(cfg.verify_discharge_max_jitter_attempts),
            ),
            label=f"{LOG_TAG} discharge",
            logger=self.logger,
        )
        return self._seq

    # ---- legacy (non-perception) fallback ----

    def _step_legacy_fallback(self) -> Optional[ClassificationChannelState]:
        cfg = self.ctx.config
        if not self._kick_started:
            self.ctx.discharging_started_at = time.monotonic()
            output_deg = C4_TRAVEL_SIGN * float(cfg.kick_off_output_deg)
            if not self.startOutputMove(output_deg, cfg.discharge_speed_usteps_per_s):
                self._holdForEvidenceFailure(
                    "C4 legacy discharge move was not acknowledged"
                )
                return None
            self._kick_started = True
            self.logger.info(
                f"{LOG_TAG} DISCHARGING (legacy) fixed kick (output={output_deg:.1f}°)"
            )

        stepper = getattr(self.irl, "carousel_stepper", None)
        if stepper is not None and not bool(stepper.stopped):
            return None

        if self._kick_done_at is None:
            self._kick_done_at = time.monotonic()
            self._releaseOnce()

        if time.monotonic() - self._kick_done_at < cfg.post_discharge_pause_ms / 1000.0:
            return None
        return ClassificationChannelState.IDLE

    def cleanup(self) -> None:
        super().cleanup()
        self.stopStepper()
        if self._seq is not None:
            self._seq.reset()
        self._discharge_started_at = None
        self._released = False
        self._faulted = False
        self._last_frame_ts = 0.0
        self._motion_issued = False
        self._awaiting_post_move_frame = False
        self._post_move_frame_not_before = 0.0
        self._clear_first_frame_ts = None
        self._in_falloff_since_ts = None
        self._jitter_attempts_seen = 0
        self._auto_reject.reset()
        self._kick_started = False
        self._kick_done_at = None
