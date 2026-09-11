import time
from typing import Optional

from defs.known_object import PieceStage
from subsystems.classification_channel.incidents import (
    publish_classification_track_lost_incident,
)
from subsystems.classification_channel.states import ClassificationChannelState

from .base import Rev01BaseState
from .constants import LOG_TAG


class AwaitingDistribution(Rev01BaseState):
    """Hold at indexed safe staging only until the chute is ready.

    Classification and transport placement normally completed during the C4
    staging move. If recognition was slower, the same idempotent helper finishes
    that work here. Once distribution owns the piece and reports the chute aimed,
    discharge begins immediately; chute motion cannot invalidate the stationary
    post-staging occupancy frame already recorded by MOVING_TO_PRECISE.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._entered_at = 0.0
        self._last_wait_log_ms = 0.0
        self._failure_reported = False

    def step(self) -> Optional[ClassificationChannelState]:
        self.setClassificationReady(False, "awaiting_distribution")
        obj = self.ctx.known_object
        now = time.monotonic()

        if obj is None:
            self._holdForEvidenceFailure(
                "C4 reached awaiting-distribution without a tracked piece"
            )
            return None

        if not bool(self.ctx.precise_staged):
            self._holdForEvidenceFailure(
                "C4 reached awaiting-distribution without indexed staging proof"
            )
            return None

        if self._entered_at == 0.0:
            self._entered_at = now

        if not self.preparePieceForDistribution(now):
            waited_ms = (now - self._entered_at) * 1000.0
            if waited_ms - self._last_wait_log_ms >= 1000.0:
                self._last_wait_log_ms = waited_ms
                self.logger.info(
                    f"{LOG_TAG} AWAITING_DISTRIBUTION: waiting on classification "
                    f"({waited_ms:.0f}ms)"
                )
            return None

        # Wait only for distribution to take ownership and aim the chute.
        distribution_ready = bool(self.shared.distribution_ready)
        taken = obj.stage == PieceStage.distributing
        if taken and distribution_ready:
            self.ctx.discharge_armed_frame_ts = float(
                self.ctx.precise_staged_frame_ts
            )
            waited_ms = (now - self._entered_at) * 1000.0
            self.logger.info(
                f"{LOG_TAG} AWAITING_DISTRIBUTION -> DISCHARGING (chute aimed at "
                f"bin={obj.destination_bin}, staged frame="
                f"{self.ctx.discharge_armed_frame_ts:.6f}, "
                f"after {waited_ms:.0f}ms)"
            )
            return ClassificationChannelState.REV01_DISCHARGING

        waited_ms = (now - self._entered_at) * 1000.0
        if waited_ms - self._last_wait_log_ms >= 1000.0:
            self._last_wait_log_ms = waited_ms
            self.logger.info(
                f"{LOG_TAG} AWAITING_DISTRIBUTION: waiting for distribution "
                f"(taken={taken}, ready={distribution_ready}, {waited_ms:.0f}ms)"
            )
        return None

    def _holdForEvidenceFailure(self, reason: str) -> None:
        if self._failure_reported:
            return
        self._failure_reported = True
        self.logger.error(f"{LOG_TAG} AWAITING_DISTRIBUTION: {reason}; holding")
        try:
            publish_classification_track_lost_incident(
                self.gc,
                piece=self.ctx.known_object,
                reason=reason,
            )
        except Exception as exc:
            self.logger.warning(
                f"{LOG_TAG} could not publish C4 handoff incident: {exc}"
            )

    def ownsLostTrackFault(self, piece_uuid: str | None) -> bool:
        if not self._failure_reported:
            return False
        obj = self.ctx.known_object
        if obj is None:
            return piece_uuid in (None, "")
        return isinstance(piece_uuid, str) and obj.uuid == piece_uuid

    def canRecoverLostTrack(self, piece_uuid: str | None) -> bool:
        if not self.ownsLostTrackFault(piece_uuid):
            return False
        obj = self.ctx.known_object
        return obj is None or self.canAbandonLostTrackObject(piece_uuid)

    def abandonLostTrackObject(self, reason: str) -> bool:
        if self.ctx.known_object is None:
            return True
        return super().abandonLostTrackObject(reason)

    def cleanup(self) -> None:
        super().cleanup()
        self._entered_at = 0.0
        self._last_wait_log_ms = 0.0
        self._failure_reported = False
