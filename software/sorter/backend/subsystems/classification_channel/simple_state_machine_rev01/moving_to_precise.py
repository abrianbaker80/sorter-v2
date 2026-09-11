import time
from typing import Optional

from subsystems.classification_channel.five_sector_platter import C4FiveSectorPlatter
from subsystems.classification_channel.incidents import (
    publish_classification_track_lost_incident,
)
from subsystems.classification_channel.states import ClassificationChannelState

from .base import Rev01BaseState
from .constants import (
    C4_EXIT_SECTOR_ADVANCE,
    C4_INDEXED_ROUTE_SECTOR_COUNT,
    C4_SAFE_STAGING_SECTOR_ADVANCE,
    C4_TRAVEL_SIGN,
    LOG_TAG,
)


_C4_START_ALIGNMENT_TOLERANCE_STEPS = 1


class MovingToPrecise(Rev01BaseState):
    """Move the captured piece to C4's fixed, indexed safe staging point.

    IDLE admits C3 only while C4 is on an absolute pocket boundary. From that
    known origin this state advances exactly five pockets (180 degrees on the
    installed rotor), while Brickognize and chute positioning run concurrently.
    Camera evidence confirms that the piece remains on C4 after the move; camera
    COM estimates never steer the motor.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._started_at = 0.0
        self._move_started = False
        self._post_move_frame_not_before = 0.0
        self._faulted = False

    def step(self) -> Optional[ClassificationChannelState]:
        now = time.monotonic()
        self.setClassificationReady(False, "moving_to_safe_staging")
        if self._faulted:
            return None

        perception_service = getattr(self.gc, "perception_service", None)
        if perception_service is None:
            return self._step_legacy()

        if self._started_at == 0.0:
            self._started_at = now

        cfg = self.ctx.config
        stepper = getattr(self.irl, "carousel_stepper", None)

        if not self._move_started:
            if not self._startIndexedStagingMove(stepper, cfg):
                return None
            return None

        # Recognition normally finishes while the acknowledged indexed move is
        # in flight. Handing the result to distribution here lets the chute aim
        # in parallel, but never publishes a route after a C4 motion fault.
        self.preparePieceForDistribution(now)

        if now - self._started_at > cfg.rotate_timeout_s:
            reason = (
                "C4 could not reach and verify its indexed safe staging point "
                f"within {cfg.rotate_timeout_s}s"
            )
            self.stopStepper()
            self.ctx.auto_reject_reason = reason
            self.ctx.auto_reject_multi_piece = bool(
                self.ctx.multi_feed_detected
            )
            self.logger.warning(
                f"{LOG_TAG} MOVING_TO_PRECISE: {reason}; rerouting to "
                "bottom reject"
            )
            return ClassificationChannelState.REV01_DISCHARGING

        try:
            if stepper is None or not bool(stepper.stopped):
                return None
        except Exception as exc:
            self._holdForEvidenceFailure(f"C4 motion status unavailable: {exc}")
            return None

        # Never accept a frame captured during the move. One fresh post-stop
        # frame is enough: position comes from the indexed stepper route, while
        # vision's job here is only to prove the piece is still present.
        if self._post_move_frame_not_before == 0.0:
            self._post_move_frame_not_before = time.time()
            return None
        state = perception_service.read_state(4)
        if float(state.ts) <= self._post_move_frame_not_before:
            return None

        n_pieces = int(state.n_pieces)
        if self.ctx.observeMultiFeed(
            n_pieces,
            float(state.ts),
            cfg.multi_feed_confirm_reads,
        ):
            self.logger.info(
                f"{LOG_TAG} SAFE_STAGING: multi-feed confirmed "
                f"({n_pieces} pieces over {cfg.multi_feed_confirm_reads} frames)"
            )
        if n_pieces <= 0:
            return None

        self.ctx.precise_staged = True
        self.ctx.precise_staged_frame_ts = float(state.ts)
        self.logger.info(
            f"{LOG_TAG} SAFE_STAGING -> AWAITING_DISTRIBUTION "
            f"(absolute target={self.ctx.c4_safe_staging_target_steps}, "
            f"piece_count={n_pieces}, distribution_placed="
            f"{self.ctx.distribution_placed}, chute_ready="
            f"{bool(self.shared.distribution_ready)})"
        )
        return ClassificationChannelState.REV01_AWAITING_DISTRIBUTION

    def _startIndexedStagingMove(self, stepper, cfg) -> bool:
        if stepper is None:
            self._holdForEvidenceFailure("C4 carousel stepper unavailable")
            return False
        platter = C4FiveSectorPlatter.from_irl_config(self.irl_config)
        if platter.sector_count != C4_INDEXED_ROUTE_SECTOR_COUNT:
            self._holdForEvidenceFailure(
                "C4 indexed route requires the configured ten-pocket rotor "
                f"(configured={platter.sector_count})"
            )
            return False
        try:
            current_position = int(stepper.position)
        except Exception as exc:
            self._holdForEvidenceFailure(f"C4 absolute position unavailable: {exc}")
            return False

        start_sector = platter.nearest_sector_index(current_position)
        aligned_position = platter.sector_position_microsteps(start_sector)
        alignment_error = current_position - aligned_position
        if abs(alignment_error) > _C4_START_ALIGNMENT_TOLERANCE_STEPS:
            self._holdForEvidenceFailure(
                "C4 cycle did not start on an absolute pocket boundary "
                f"(position={current_position}, error={alignment_error} microsteps)"
            )
            return False

        travel_sign = 1 if C4_TRAVEL_SIGN >= 0.0 else -1
        staging_sector = (
            start_sector + travel_sign * C4_SAFE_STAGING_SECTOR_ADVANCE
        )
        exit_sector = start_sector + travel_sign * C4_EXIT_SECTOR_ADVANCE
        staging_target = platter.sector_position_microsteps(staging_sector)
        exit_target = platter.sector_position_microsteps(exit_sector)
        move_steps = staging_target - current_position
        try:
            stepper.set_speed_limits(
                16,
                max(16, int(cfg.precise_converge_speed_usteps_per_s)),
            )
            acknowledged = bool(stepper.move_steps(int(move_steps)))
        except Exception as exc:
            self._holdForEvidenceFailure(f"C4 indexed staging command failed: {exc}")
            return False
        if not acknowledged:
            self._holdForEvidenceFailure(
                f"C4 indexed staging move was not acknowledged "
                f"(requested={move_steps} microsteps)"
            )
            return False

        self.ctx.c4_cycle_start_sector = start_sector
        self.ctx.c4_safe_staging_target_steps = staging_target
        self.ctx.c4_exit_target_steps = exit_target
        self._move_started = True
        self.logger.info(
            f"{LOG_TAG} SAFE_STAGING started absolute indexed move "
            f"(sector={start_sector}->{staging_sector}, target={staging_target}, "
            f"steps={move_steps}, exit_target={exit_target})"
        )
        return True

    def _holdForEvidenceFailure(self, reason: str) -> None:
        self.stopStepper()
        if self._faulted:
            return
        self._faulted = True
        self.logger.error(f"{LOG_TAG} MOVING_TO_PRECISE: {reason}; holding")
        try:
            publish_classification_track_lost_incident(
                self.gc,
                piece=self.ctx.known_object,
                reason=reason,
            )
        except Exception as exc:
            # Incident reporting must not turn an evidence failure into a state
            # transition. The state remains latched even if telemetry fails.
            self.logger.warning(
                f"{LOG_TAG} could not publish C4 staging incident: {exc}"
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
        """Whether an operator-cleared track-loss belongs to this fault latch.

        MOVING_TO_PRECISE has not handed the piece to distribution yet, so once
        the operator acknowledges that its C4 track is gone, abandoning this
        one cycle and returning to IDLE is the only ownership-safe recovery.
        """
        return bool(
            self.ownsLostTrackFault(piece_uuid)
            and self.canAbandonLostTrackObject(piece_uuid)
        )

    # ---- legacy (non-perception) fallback: single fixed reverse move ----

    def _step_legacy(self) -> Optional[ClassificationChannelState]:
        cfg = self.ctx.config
        if self._started_at == 0.0:
            self._started_at = time.monotonic()
            self.startOutputMove(
                C4_TRAVEL_SIGN * float(cfg.capture_sweep_output_deg),
                cfg.precise_converge_speed_usteps_per_s,
            )
            self.logger.info(f"{LOG_TAG} MOVING_TO_PRECISE (legacy) fixed reverse move")
        stepper = getattr(self.irl, "carousel_stepper", None)
        if stepper is not None and not bool(stepper.stopped):
            return None
        self.ctx.precise_staged = True
        self.ctx.precise_staged_frame_ts = 0.0
        return ClassificationChannelState.REV01_AWAITING_DISTRIBUTION

    def cleanup(self) -> None:
        super().cleanup()
        self.stopStepper()
        self._started_at = 0.0
        self._move_started = False
        self._post_move_frame_not_before = 0.0
        self._faulted = False
