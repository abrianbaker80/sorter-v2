"""Bounded C4 discharge recovery through the normal bottom-reject route."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from defs.known_object import ClassificationStatus, PieceStage
from subsystems.classification_channel.incidents import (
    record_classification_track_lost_auto_resolved,
)
from subsystems.classification_channel.states import ClassificationChannelState

from .channel_clear import clearChannelByAdvancing
from .constants import LOG_TAG

if TYPE_CHECKING:
    from .discharging import Discharging


_DISTRIBUTION_TIMEOUT_S = 8.0
_MAX_CLEAR_OUTPUT_DEG = 360.0


class C4AutoReject:
    """Retarget one unsafe C4 cycle, clear C4, and resume without credit."""

    def __init__(self, owner: "Discharging") -> None:
        self.owner = owner
        self.reset()

    @property
    def active(self) -> bool:
        return self.phase is not None

    def reset(self) -> None:
        self.phase: Optional[str] = None
        self.reason = ""
        self.multi_piece = False
        self.phase_started_at = 0.0

    def start(self, reason: str, *, multi_piece: bool) -> None:
        owner = self.owner
        if self.active or owner._faulted:
            return
        obj = owner.ctx.known_object
        if obj is None:
            owner._holdForEvidenceFailure(
                f"{reason}; automatic reject had no tracked object"
            )
            return

        owner.stopStepper()
        if owner._seq is not None:
            reset = getattr(owner._seq, "reset", None)
            if callable(reset):
                reset()

        self.reason = str(reason)
        self.multi_piece = bool(multi_piece)
        if not owner.ctx.distribution_placed:
            self._prepareObject(obj)
            owner.transport.placePieceForDistribution(obj)
            owner.ctx.distribution_placed = True
            owner.emitKnownObject()
            self._setPhase("wait_reject_ready")
            owner.logger.info(
                f"{LOG_TAG} piece {obj.uuid[:8]} queued directly for bottom reject"
            )
            return
        cancel = getattr(owner.transport, "cancelPieceForDistribution", None)
        canceled = cancel(obj.uuid) if callable(cancel) else None
        if canceled is None:
            owner._holdForEvidenceFailure(
                f"{reason}; automatic reject could not cancel the normal-bin route"
            )
            return

        owner.ctx.distribution_placed = False
        self._setPhase("wait_cancel")
        owner.logger.warning(
            f"{LOG_TAG} DISCHARGING: {reason}; rerouting to bottom reject"
        )

    def step(self, now: float) -> Optional[ClassificationChannelState]:
        owner = self.owner
        obj = owner.ctx.known_object
        if obj is None:
            self._fail("tracked object disappeared during recovery")
            return None

        if self.phase == "wait_cancel":
            checker = getattr(
                owner.transport,
                "isCanceledPieceForDistribution",
                None,
            )
            if not callable(checker):
                self._fail("distribution transport cannot confirm cancellation")
                return None
            if checker(obj.uuid):
                if self._expired(now):
                    self._fail(
                        "distribution did not acknowledge the canceled normal-bin route"
                    )
                return None
            if owner.transport.getPieceForDistributionPositioning() is not None:
                self._fail(
                    "distribution still owns a positioning slot after cancellation"
                )
                return None

            self._prepareObject(obj)
            owner.transport.placePieceForDistribution(obj)
            owner.ctx.distribution_placed = True
            owner.emitKnownObject()
            self._setPhase("wait_reject_ready")
            owner.logger.info(
                f"{LOG_TAG} DISCHARGING: piece {obj.uuid[:8]} requeued for "
                "bottom reject"
            )
            return None

        if self.phase == "wait_reject_ready":
            positioned = owner.transport.getPieceForDistributionPositioning()
            owns_slot = positioned is not None and positioned.uuid == obj.uuid
            reject_selected = (
                obj.stage == PieceStage.distributing
                and obj.destination_bin is None
                and obj.part_id is None
            )
            ready = bool(owner.shared.distribution_ready)
            doors_ready = self._doorsReady()
            if owns_slot and reject_selected and ready and doors_ready:
                self._setPhase("clear_c4")
                return None
            if self._expired(now):
                self._fail(
                    "bottom-reject path was not ready before its timeout "
                    f"(owns_slot={owns_slot}, selected={reject_selected}, "
                    f"ready={ready}, doors_ready={doors_ready})"
                )
            return None

        if self.phase == "clear_c4":
            try:
                result = clearChannelByAdvancing(
                    owner.gc,
                    owner.irl,
                    owner.irl_config,
                    vision=getattr(owner.cv, "_vision", None),
                    max_output_deg=_MAX_CLEAR_OUTPUT_DEG,
                    label=f"{LOG_TAG} auto reject",
                )
            except Exception as exc:
                self._fail(f"C4 automatic clearing failed: {exc}")
                return None
            if not result.cleared:
                self._fail(
                    "C4 automatic clearing could not prove the channel empty "
                    f"after {result.output_deg_moved:.0f} degrees ({result.reason})"
                )
                return None

            owner._releaseOnce()
            try:
                record_classification_track_lost_auto_resolved(
                    owner.gc,
                    piece=obj,
                    reason=self.reason,
                    moved_deg=float(result.output_deg_moved),
                    multi_piece=self.multi_piece,
                )
            except Exception as exc:
                owner.logger.warning(
                    f"{LOG_TAG} could not record automatic C4 reject: {exc}"
                )
            owner.logger.info(
                f"{LOG_TAG} DISCHARGING -> IDLE (auto-rejected {obj.uuid[:8]}, "
                f"C4 clear after {result.output_deg_moved:.0f} degrees)"
            )
            self.phase = None
            return ClassificationChannelState.IDLE

        self._fail(f"unknown automatic-reject phase {self.phase!r}")
        return None

    def _prepareObject(self, obj) -> None:
        obj.stage = PieceStage.created
        obj.classification_status = (
            ClassificationStatus.multi_drop_fail
            if self.multi_piece
            else ClassificationStatus.unknown
        )
        obj.part_id = None
        obj.part_name = None
        obj.part_category = None
        obj.color_id = "any_color"
        obj.color_name = "Any Color"
        obj.category_id = None
        obj.destination_bin = None
        obj.confidence = None
        obj.color_confidence = None
        obj.color_provider = None
        obj.mold_provider = None
        obj.max_dimension_mm = None
        obj.moving_avg_price = None
        obj.piece_metadata = None
        obj.high_value_routed = False
        obj.not_in_inventory = None
        obj.too_big = False
        obj.too_big_for_layer = False
        obj.intended_layer_index = None
        obj.distributing_at = None
        obj.distribution_target_selected_at = None
        obj.distribution_motion_started_at = None
        obj.distribution_positioned_at = None
        obj.distributed_at = None
        obj.aborted = False
        obj.dead = False
        obj.classified_at = time.time()
        obj.updated_at = time.time()

    def _doorsReady(self) -> bool:
        owner = self.owner
        if bool(getattr(owner.gc, "disable_servos", False)):
            return True
        servos = list(getattr(owner.irl, "servos", []) or [])
        if not servos:
            return False
        for servo in servos:
            try:
                if not bool(getattr(servo, "available", True)):
                    return False
                if not bool(getattr(servo, "is_calibrated", True)):
                    return False
                if not bool(servo.stopped):
                    return False
                is_closed = getattr(servo, "isClosed", None)
                if callable(is_closed) and bool(is_closed()):
                    return False
            except Exception:
                return False
        return True

    def _setPhase(self, phase: str) -> None:
        self.phase = phase
        self.phase_started_at = time.monotonic()

    def _expired(self, now: float) -> bool:
        return now - self.phase_started_at >= _DISTRIBUTION_TIMEOUT_S

    def _fail(self, detail: str) -> None:
        reason = f"{self.reason}; automatic reject failed: {detail}"
        self.phase = None
        self.owner._holdForEvidenceFailure(reason)
