import time
from typing import Optional

from subsystems.classification_channel.five_sector_platter import C4FiveSectorPlatter
from subsystems.classification_channel.states import ClassificationChannelState

from .base import Rev01BaseState
from .constants import LOG_TAG


_C4_ALIGNMENT_POSITION_TOLERANCE_STEPS = 1
_C4_ALIGNMENT_MAX_ATTEMPTS = 2


class Idle(Rev01BaseState):
    """Part of the SIMPLE_STATE_MACHINE_REV01 path — the classification half of the
    GO_TO_ANGLE_REV01 + SIMPLE_STATE_MACHINE_REV01 Rev04 pair.

    The other (legacy) classification paths do not use this file or package.
    When adding C4 exit jitter unstick later, this is the file + the rev01_config
    jitter_* fields to focus on (symmetric to GoToAngleFeeding).
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._presence_streak = 0
        self._clear_streak = 0
        self._last_frame_ts = 0.0
        self._alignment_move_pending = False
        self._alignment_post_move_frame_not_before = 0.0
        self._alignment_attempts = 0
        self._alignment_confirmed = False
        self._last_alignment_error: str | None = None
        self.logger.info(f"{LOG_TAG} IDLE state constructed")

    def step(self) -> Optional[ClassificationChannelState]:
        perception_service = getattr(self.gc, "perception_service", None)
        if perception_service is not None:
            return self._step_perception(perception_service)
        return self._step_legacy()

    # ---- Rev04 perception path ----

    def _step_perception(self, perception_service) -> Optional[ClassificationChannelState]:
        """Read the C4 perception slot and drive the simple state machine.

        Cascade rule for C4 (from the rev04 doc):
        - Channel empty (``n_pieces == 0``)        → classification_ready=True, stay Idle.
        - Anything on the channel                  → classification_ready=False.
        - After ``presence_streak_to_start`` consecutive frames with a
          piece in the C3 ingress/drop zone, transition to CAPTURING.

        No stuck-piece timeout. No exit-zone-only branch escalation —
        if a piece is parked in the exit zone, perception reports
        ``in_exit=True, in_drop=False, n_pieces≥1`` and the SSM holds
        Idle reporting "not ready."
        """
        t0 = time.perf_counter()
        state = perception_service.read_state(4)
        self.gc.runtime_stats.observePerfMs(
            "classification.rev01.idle.perception_read_ms",
            (time.perf_counter() - t0) * 1000.0,
        )
        # How stale is the perception result at the instant we gate a decision
        # on it: wall-clock now minus the frame's capture timestamp (state.ts).
        # This is the dashboard's "age of inference data we decide on" metric.
        if state.ts:
            self.gc.runtime_stats.observePerfMs(
                "classification.decision_frame_age_ms",
                max(0.0, (time.time() - state.ts) * 1000.0),
            )
        self.gc.profiler.observeValue(
            "classification.rev01.idle.n_pieces", float(state.n_pieces)
        )

        frame_ts = float(state.ts)
        if frame_ts <= 0.0:
            self._presence_streak = 0
            self._clear_streak = 0
            self.setClassificationReady(False, "waiting for C4 perception")
            return None
        new_frame = frame_ts > self._last_frame_ts
        if new_frame:
            self._last_frame_ts = frame_ts

        # A position correction is allowed only while the C3 gate is closed.
        # Do not interpret detections made during that motion; first require the
        # motor to stop and perception to publish a genuinely post-motion frame.
        if self._alignment_move_pending and not self._alignmentMoveSettled(
            frame_ts, new_frame
        ):
            self._presence_streak = 0
            self._clear_streak = 0
            self.setClassificationReady(False, "aligning C4 pocket")
            return None

        if state.n_pieces == 0:
            self._presence_streak = 0
            aligned, alignment_reason = self._ensurePocketAligned()
            if not aligned:
                self._clear_streak = 0
                self.setClassificationReady(False, alignment_reason)
                return None
            if new_frame:
                self._clear_streak += 1
            # Asymmetry guard: we require presence_streak_to_start confirmed
            # reads to BELIEVE a piece arrived, but a single zero-read used to
            # flip ready=True instantly. The detector blinks to 0 for a frame or
            # two with a piece still on the channel; a premature ready=True lets
            # C3 push a second piece in while the first is merely undetected →
            # double feed. Require a confirmed clear streak too.
            if self._clear_streak >= self.ctx.config.idle_clear_confirm_reads:
                self.setClassificationReady(True, "channel clear")
            else:
                self.setClassificationReady(
                    False,
                    f"confirming clear ({self._clear_streak}/"
                    f"{self.ctx.config.idle_clear_confirm_reads})",
                )
            return None

        self._clear_streak = 0
        # Channel is occupied. Always not-ready.
        self.setClassificationReady(False, f"{state.n_pieces} piece(s) on channel")

        # A new C4 object has exactly one physical ingress: C3 deposits it in
        # the calibrated drop zone. Detections elsewhere still close the C3
        # gate above, but cannot establish provenance for a new cycle. This is
        # especially important while the rotor settles after discharge, when
        # moving hardware can otherwise appear as a mid-channel detection.
        raw_actionable = bool(state.in_drop)
        confirmed_actionable = raw_actionable
        if state.n_confirmed_pieces is not None:
            # Raw/tentative detections close the C3 gate immediately, but the C4
            # tracker deliberately withholds an id until a bbox survives min_hits
            # distinct frames. Only that confirmed object may start a cycle.
            confirmed_actionable = any(
                piece.sv_bt_track_id is not None and int(piece.zone_code) == 1
                for piece in state.pieces
            )

        if raw_actionable:
            if new_frame:
                self._presence_streak += 1
        else:
            self._presence_streak = 0

        self.gc.profiler.observeValue(
            "classification.rev01.idle.presence_streak",
            float(self._presence_streak),
        )
        if (
            confirmed_actionable
            and self._presence_streak >= self.ctx.config.presence_streak_to_start
        ):
            self._presence_streak = 0
            self.abandonInFlightObject("new cycle starting")
            self.ctx.reset()
            self.logger.info(
                f"{LOG_TAG} IDLE -> ROTATING_AND_CAPTURING "
                f"(perception confirmed C3 ingress, n_pieces={state.n_pieces})"
            )
            return ClassificationChannelState.REV01_CAPTURING
        return None

    def _classificationStepper(self):
        return getattr(self.irl, "carousel_stepper", None) or getattr(
            self.irl,
            "classification_channel_rotor_stepper",
            None,
        ) or getattr(self.irl, "c_channel_4_rotor_stepper", None)

    def _alignmentMoveSettled(self, frame_ts: float, new_frame: bool) -> bool:
        stepper = self._classificationStepper()
        if stepper is None:
            self._reportAlignmentError("C4 classification stepper unavailable")
            return False
        try:
            stopped = bool(stepper.stopped)
        except Exception as exc:
            self._reportAlignmentError(f"C4 position status unavailable: {exc}")
            return False
        if not stopped:
            return False
        if self._alignment_post_move_frame_not_before == 0.0:
            self._alignment_post_move_frame_not_before = time.time()
            return False
        if (
            not new_frame
            or frame_ts < self._alignment_post_move_frame_not_before
        ):
            return False
        self._alignment_move_pending = False
        self._alignment_post_move_frame_not_before = 0.0
        return True

    def _ensurePocketAligned(self) -> tuple[bool, str]:
        if self._alignment_confirmed:
            return True, "C4 pocket aligned"
        stepper = self._classificationStepper()
        if stepper is None:
            reason = "C4 classification stepper unavailable"
            self._reportAlignmentError(reason)
            return False, reason
        try:
            if not bool(stepper.stopped):
                return False, "waiting for C4 motion to stop"
            position = int(stepper.position)
        except Exception as exc:
            reason = f"C4 absolute position unavailable: {exc}"
            self._reportAlignmentError(reason)
            return False, reason

        platter = C4FiveSectorPlatter.from_irl_config(self.irl_config)
        correction_steps = platter.nearest_sector_delta_microsteps(position)
        if abs(correction_steps) <= _C4_ALIGNMENT_POSITION_TOLERANCE_STEPS:
            self._alignment_attempts = 0
            self._alignment_confirmed = True
            self._last_alignment_error = None
            return True, "C4 pocket aligned"

        half_pocket_steps = abs(
            platter.output_degrees_to_motor_microsteps(
                platter.sector_size_deg / 2.0
            )
        ) + 1
        if abs(correction_steps) > half_pocket_steps:
            reason = (
                "C4 absolute-position correction exceeded half a pocket "
                f"({correction_steps} microsteps)"
            )
            self._reportAlignmentError(reason)
            return False, reason
        if self._alignment_attempts >= _C4_ALIGNMENT_MAX_ATTEMPTS:
            reason = (
                "C4 did not reach its absolute pocket boundary after "
                f"{_C4_ALIGNMENT_MAX_ATTEMPTS} attempts"
            )
            self._reportAlignmentError(reason)
            return False, reason

        try:
            stepper.set_speed_limits(
                16,
                max(16, int(self.ctx.config.precise_converge_speed_usteps_per_s)),
            )
            acknowledged = bool(stepper.move_steps(int(correction_steps)))
        except Exception as exc:
            reason = f"C4 pocket-alignment command failed: {exc}"
            self._reportAlignmentError(reason)
            return False, reason
        if not acknowledged:
            reason = "C4 pocket-alignment command was not acknowledged"
            self._reportAlignmentError(reason)
            return False, reason

        self._alignment_attempts += 1
        self._alignment_move_pending = True
        self._alignment_post_move_frame_not_before = 0.0
        correction_output_deg = platter.motor_microsteps_to_output_degrees(
            correction_steps
        )
        self.logger.info(
            f"{LOG_TAG} IDLE: correcting C4 from absolute position {position} "
            f"by {correction_steps} microsteps ({correction_output_deg:.2f} output deg) "
            "before reopening C3"
        )
        return False, "aligning C4 pocket"

    def _reportAlignmentError(self, reason: str) -> None:
        if self._last_alignment_error == reason:
            return
        self._last_alignment_error = reason
        self.logger.error(f"{LOG_TAG} IDLE: {reason}; C3 gate remains closed")

    # ---- Legacy (non-perception) path, unchanged ----

    def _step_legacy(self) -> Optional[ClassificationChannelState]:
        bboxes_started = time.perf_counter()
        bboxes = self.cv.bboxesOnChannel()
        self.gc.runtime_stats.observePerfMs(
            "classification.rev01.idle.bboxes_on_channel_ms",
            (time.perf_counter() - bboxes_started) * 1000.0,
        )
        self.gc.profiler.observeValue(
            "classification.rev01.idle.bbox_count",
            float(len(bboxes)),
        )
        if not bboxes:
            self._presence_streak = 0
            self._clear_streak += 1
            if self._clear_streak >= self.ctx.config.idle_clear_confirm_reads:
                self.setClassificationReady(True, "channel clear")
            else:
                self.setClassificationReady(
                    False,
                    f"confirming clear ({self._clear_streak}/"
                    f"{self.ctx.config.idle_clear_confirm_reads})",
                )
            return None
        self._clear_streak = 0

        actionable_started = time.perf_counter()
        actionable = self.bboxesOutsideExitZone(bboxes)
        self.gc.runtime_stats.observePerfMs(
            "classification.rev01.idle.bboxes_outside_exit_ms",
            (time.perf_counter() - actionable_started) * 1000.0,
        )
        self.gc.profiler.observeValue(
            "classification.rev01.idle.actionable_bbox_count",
            float(len(actionable)),
        )
        if not actionable:
            self._presence_streak = 0
            self.setClassificationReady(False, f"{len(bboxes)} piece(s) in exit zone")
            # TODO: wire jitter unstick using self.ctx.config.jitter_* when
            # exit-only dwell exceeds threshold (see GoToAngleFeeding for pattern).
            return None

        self._presence_streak += 1
        self.gc.profiler.observeValue(
            "classification.rev01.idle.presence_streak",
            float(self._presence_streak),
        )
        self.setClassificationReady(False, f"{len(actionable)} bbox(es) on channel")
        if self._presence_streak >= self.ctx.config.presence_streak_to_start:
            self._presence_streak = 0
            self.abandonInFlightObject("new cycle starting")
            self.ctx.reset()
            self.logger.info(
                f"{LOG_TAG} IDLE -> ROTATING_AND_CAPTURING "
                f"(piece confirmed on channel, count={len(actionable)})"
            )
            return ClassificationChannelState.REV01_CAPTURING
        return None

    def cleanup(self) -> None:
        super().cleanup()
        self._presence_streak = 0
        self._clear_streak = 0
        self._last_frame_ts = 0.0
        self._alignment_move_pending = False
        self._alignment_post_move_frame_not_before = 0.0
        self._alignment_attempts = 0
        self._alignment_confirmed = False
        self._last_alignment_error = None
