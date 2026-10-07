import time
import queue
from typing import Optional
import server.shared_state as shared_state
from states.base_state import BaseState
from subsystems.shared_variables import SharedVariables
from .states import DistributionState
from irl.config import IRLInterface
from global_config import GlobalConfig
from utils.event import knownObjectToEvent
from defs.known_object import (
    HARVEST_CONFIRMATION_UNCREDITED, PieceStage, UNVERIFIED_C4_HANDOFF,
)
from defs.events import PauseCommandData, PauseCommandEvent
from subsystems.classification_channel.incidents import (
    CLASSIFICATION_TRACK_LOST_INCIDENT_KIND,
    publish_classification_track_lost_incident,
)
from smart_bins_native_completion import CurrentFollowupHeld

CHUTE_SETTLE_MS = 1500
SAMPLE_COLLECTION_CHUTE_SETTLE_MS = 400
MISSING_DROP_PIECE_GRACE_MS = 1500
PIECE_EXIT_INCIDENT_MS = 8000


class Sending(BaseState):
    def __init__(
        self,
        irl: IRLInterface,
        gc: GlobalConfig,
        shared: SharedVariables,
        event_queue: queue.Queue,
        *,
        vision=None,
        post_distribute_cooldown_s: float = 0.0,
        native_completion=None,
    ):
        super().__init__(irl, gc)
        self.shared = shared
        self.event_queue = event_queue
        self.vision = vision
        self.native_completion = native_completion
        self._cooldown_s = max(0.0, float(post_distribute_cooldown_s))
        self.piece = None
        self.start_time: float = 0.0
        self._occupancy_state: str | None = None
        self._committed: bool = False
        self._exit_wait_incident_piece_uuid: str | None = None
        self._harvest_pause_enqueued: bool = False
        self._native_incident: dict | None = None

    def _setOccupancyState(self, state_name: str) -> None:
        if self._occupancy_state == state_name:
            return
        prev_state = self._occupancy_state
        self._occupancy_state = state_name
        self.gc.runtime_stats.observeStateTransition(
            "distribution.occupancy",
            prev_state,
            state_name,
        )

    def step(self) -> Optional[DistributionState]:
        now = time.time()
        retained_guard = getattr(self.shared, "native_completion_guard", None)
        ownership_guard = self.native_completion
        if (retained_guard is not None
            and (self.native_completion is not None or self._nativeCompletionPending())):
            ownership_guard = retained_guard
        handoff = None
        if ownership_guard is not None:
            try:
                handoff = ownership_guard.check_drop_ownership(self.shared.transport)
            except Exception as exc:
                self._holdNative(str(exc))
                return None
            if (self.native_completion is not None and retained_guard is not None
                and self.native_completion is not retained_guard):
                self._holdNative("guarded completion adapter differs from retained owner")
                return None
        if self.piece is None and not self._committed:
            if self.start_time <= 0.0:
                self.start_time = now
            transport = self.shared.transport
            self.piece = (handoff.piece if handoff is not None else
                          transport.getPieceForDistributionDrop() if transport is not None
                          else None)
            if self.piece is None:
                elapsed_ms = (now - self.start_time) * 1000
                self._setOccupancyState("sending.wait_drop_piece")
                if self.native_completion is not None or self._nativeCompletionPending():
                    self.shared.set_distribution_gate(False, reason="native drop identity pending")
                    if elapsed_ms >= MISSING_DROP_PIECE_GRACE_MS:
                        self._holdNative("guarded distribution-drop piece is missing")
                    return None
                if elapsed_ms >= MISSING_DROP_PIECE_GRACE_MS:
                    self.logger.warning(
                        "Sending: no distribution-drop piece available after "
                        f"{elapsed_ms:.0f}ms; reopening distribution gate"
                    )
                    self.gc.runtime_stats.observeBlockedReason(
                        "distribution",
                        "sending_missing_drop_piece",
                    )
                    self.shared.set_distribution_gate(True, reason=None)
                    return DistributionState.IDLE
                return None

        if self.native_completion is not None:
            return self._stepNative(now)
        if self._nativeCompletionPending() or (
            self.piece is not None and any(
                getattr(self.piece, name, None) is not None
                for name in ("native_machine_id", "native_reservation_id", "native_delivery_id")
            )
        ):
            self._holdNative("guarded distribution drop has no completion adapter")
            return None

        # An orphaned positioning slot can enter SENDING without a new physical
        # index. A previously completed drop must never be credited twice.
        if self.piece is not None and (
            self.piece.stage == PieceStage.distributed
            or self._hasUncreditedHarvestDrop()
        ):
            self._committed = True
        elapsed_ms = (now - self.start_time) * 1000
        settle_ms = self._settleMs()
        if elapsed_ms < settle_ms:
            self._setOccupancyState("sending.wait_chute_settle")
            return None

        # Commit the piece once (stats, event, recorder) — must not repeat
        # even if we decide to hold the gate for additional cooldown below.
        if not self._committed:
            self.logger.info(f"Sending: settle complete ({elapsed_ms:.0f}ms)")
            self._setOccupancyState("sending.commit_piece")
            if self.piece:
                try:
                    from project_harvest_runtime import confirm_piece_drop

                    unverified = self.piece.transport_failure_reason == UNVERIFIED_C4_HANDOFF
                    harvest_result = None if unverified else confirm_piece_drop(self.gc, self.piece)
                except Exception as exc:
                    self.logger.exception("Sending: Harvest physical confirmation failed")
                    try:
                        recovered = self._recoverHarvestConfirmationFailure(exc)
                    except Exception as recovery_exc:
                        self.logger.exception("Sending: Harvest uncertainty retirement failed")
                        self._pauseForHarvestFailure(str(recovery_exc))
                        self._setOccupancyState("sending.harvest_confirmation_failed")
                        return None
                    if not recovered:
                        self._pauseForHarvestFailure(str(exc))
                        self._setOccupancyState("sending.harvest_confirmation_failed")
                        return None
                if not self._committed:
                    self.piece.stage = PieceStage.distributed
                    self.piece.distributed_at = time.time()
                    self.piece.updated_at = time.time()
                    self.event_queue.put(knownObjectToEvent(self.piece))
                    if not unverified:
                        self.gc.run_recorder.recordPiece(self.piece)
                    tracker = None if unverified else getattr(self.gc, 'set_progress_tracker', None)
                    if tracker is not None:
                        tracker.record(
                            self.piece.part_id,
                            self.piece.color_id,
                            self.piece.category_id,
                        )
                        try:
                            from server.set_progress_sync import getSetProgressSyncWorker

                            getSetProgressSyncWorker().notify()
                        except Exception:
                            pass
                    if isinstance(harvest_result, dict):
                        self.gc.runtime_stats.observeHarvestConfirmation(harvest_result)
                        if harvest_result.get("project_completed"):
                            self.logger.info(
                                "Sending: Harvest project quantities are complete; pausing sorter"
                            )
                            self._enqueuePause()
                        elif harvest_result.get("pause_required"):
                            self.logger.info(
                                "Sending: Harvest controlled test reached its piece limit; pausing sorter"
                            )
                            self._enqueuePause()
            self._committed = True

        # Chute-settle timer elapsed and the piece has been committed. Now
        # gate the downstream reopen on either:
        #   (a) the carousel tracker no longer showing the dropped piece's
        #       global_id (physical exit confirmed by vision), or
        #   (b) a minimum cooldown after drop commit, used as a fallback
        #       when the tracker signal is unavailable.
        # Root cause of ~63% multi_drop_fail rate was a fixed 1500ms
        # wall-clock reopen that didn't wait for the piece to physically
        # leave the chute.
        if self._hasUncreditedHarvestDrop() and self.shared.chute_move_in_progress:
            self._setOccupancyState("sending.wait_chute_motion_after_harvest_uncertainty")
            return None
        if not self._shouldReopenGate():
            self._setOccupancyState("sending.wait_piece_exit")
            return None

        self.shared.set_distribution_gate(True, reason=None)
        return DistributionState.IDLE

    def _stepNative(self, now: float) -> Optional[DistributionState]:
        """Settle once, commit the ledger, then publish from its verified receipt."""
        adapter = self.native_completion
        piece = self.piece
        try:
            handoff = adapter.require_handoff(piece)
            self._requireCurrentNativeDrop(handoff)
            if piece.stage is PieceStage.distributed and handoff.receipt is None:
                raise RuntimeError("distributed flag lacks durable native receipt")
        except Exception as exc:
            self._holdNative(str(exc))
            return None

        elapsed_ms = (now - self.start_time) * 1000
        if elapsed_ms < self._settleMs():
            self._setOccupancyState("sending.wait_chute_settle")
            return None
        if not self._committed:
            self._setOccupancyState("sending.commit_piece")
            try:
                runtime_run_id = self.gc.run_recorder.run_id
                handoff, receipt = adapter.complete(
                    piece, runtime_run_id=runtime_run_id, completed_at=now)
            except Exception as exc:
                handoff.completion_error = exc
                self._holdNative(f"native completion unresolved: {exc}")
                return None
            # complete() reads back the delivery and matching piece history.
            # No public completion field is written before this point.
            piece.native_delivery_id = receipt["delivery_id"]
            piece.stage = PieceStage.distributed
            piece.distributed_at = handoff.evidence.completed_at
            piece.updated_at = now
            self._committed = True
            try:
                self._requireCurrentNativeDrop(handoff)
                self._clearNativeIncident()
            except Exception as exc:
                self._holdNative(f"native completion post-commit hold: {exc}")
                return None

        try:
            self._requireCurrentNativeDrop(handoff)
        except Exception as exc:
            self._holdNative(str(exc))
            return None
        if not self._publishNativeAfterCommit(handoff):
            self._holdNative(handoff.publication_error or "native notification failed")
            return None
        self._clearNativeIncident()
        if self._activeIncident() is not None:
            self._setOccupancyState("sending.wait_native_incident")
            return None
        if not self._shouldReopenGate():
            self._setOccupancyState("sending.wait_piece_exit")
            return None
        try:
            self._requireCurrentNativeDrop(handoff)
            adapter.close(handoff)
            adapter.require_current_followup(handoff, states=("CLOSED",))
        except Exception as exc:
            # No external gate action has run. A failed or lost CLOSE write
            # remains retryable with its original key and revision.
            self._holdNative(f"native closure held: {exc}")
            return None
        failure_phase = "GATE_OPEN"
        try:
            self.shared.set_distribution_gate(True, reason=None)
            failure_phase = "ADMISSION_RELEASE"
            adapter.release_admission(piece)
        except Exception as exc:
            self._holdNative(f"native gate release held: {exc}",
                             persist_failure=not isinstance(exc, CurrentFollowupHeld),
                             failure_phase=failure_phase)
            return None
        return DistributionState.IDLE

    def _requireCurrentNativeDrop(self, handoff) -> None:
        if self.native_completion.check_drop_ownership(self.shared.transport) is not handoff:
            raise RuntimeError("guarded completion handoff changed")

    def _nativeCompletionPending(self) -> bool:
        guard = getattr(self.shared, "native_completion_guard", None)
        if guard is None:
            return False
        try:
            return bool(guard.blocks_admission())
        except Exception:
            return True

    def _publishNativeAfterCommit(self, handoff) -> bool:
        if handoff.publication_state == "complete":
            if handoff.publication_durable:
                return True
            try:
                self.native_completion.record_published(handoff)
            except Exception as exc:
                handoff.publication_error = str(exc)
                return False
            return True
        if handoff.publication_state != "pending":
            return False
        # An incrementing callback can succeed and then lose its acknowledgement.
        # Mark the whole bounded publication attempt before entering it; failure
        # holds for reconciliation instead of blindly invoking it again.
        try:
            self.native_completion.begin_publication(handoff)
        except Exception as exc:
            handoff.publication_exception = exc
            handoff.publication_error = str(exc)
            return False
        handoff.publication_state = "attempted"
        try:
            self.event_queue.put(knownObjectToEvent(self.piece))
            self.gc.run_recorder.recordCommittedNativePiece(
                self.piece, handoff.reservation_id, handoff.receipt["delivery_id"])
            tracker = getattr(self.gc, "set_progress_tracker", None)
            if tracker is not None:
                tracker.record(self.piece.part_id, self.piece.color_id,
                               self.piece.category_id)
                from server.set_progress_sync import getSetProgressSyncWorker
                getSetProgressSyncWorker().notify()
        except Exception as exc:
            handoff.publication_exception = exc
            handoff.publication_error = str(exc)
            handoff.publication_state = "failed"
            return False
        handoff.publication_state = "complete"
        try:
            self.native_completion.record_published(handoff)
        except Exception as exc:
            handoff.publication_error = str(exc)
            return False
        return True

    def _holdNative(self, detail: str, *, persist_failure: bool = False,
                    failure_phase: str | None = None) -> None:
        if self.native_completion is not None:
            self.native_completion.hold_admission(self.piece)
        self.shared.set_distribution_gate(False, reason="native completion held")
        self._setOccupancyState("sending.native_completion_held")
        stats = self.gc.runtime_stats
        stats.observeBlockedReason("distribution", "native_completion_held")
        if self._native_incident is None:
            try:
                active = stats.activeIncident()
                if active is None or (
                    active.get("kind") == "smart_bin_completion_held"
                    and active.get("piece_uuid") == getattr(self.piece, "uuid", None)
                ):
                    stats.setActiveIncident({
                        "kind": "smart_bin_completion_held",
                        "piece_uuid": getattr(self.piece, "uuid", None),
                        "channel": "distribution", "reason": detail,
                    })
                    self._native_incident = stats.activeIncident()
            except Exception:
                self.logger.exception("Sending: could not publish native completion incident")
        self._enqueuePause()
        # Gate and pause first: unavailable SQLite cannot delay the local stop.
        if self.native_completion is not None:
            try:
                self.native_completion.persist_ownership_loss()
                handoff = self.native_completion._handoff
                if handoff is not None and (
                    handoff.publication_state == "failed" or persist_failure
                ):
                    self.native_completion.persist_side_effect_failure(
                        handoff.publication_error or detail,
                        failure_phase=("PUBLICATION_CALLBACKS" if handoff.publication_state == "failed"
                                       else failure_phase))
            except Exception:
                self.logger.exception("Sending: native follow-up hold could not be persisted")

    def _clearNativeIncident(self) -> None:
        if self._native_incident is not None:
            self.gc.runtime_stats.clearActiveIncidentIfMatches(
                self._native_incident, resolved_by="native_completion_receipt")
            self._native_incident = None

    def _enqueuePause(self) -> None:
        if self._harvest_pause_enqueued:
            return
        command_queue = shared_state.command_queue
        if command_queue is None:
            self.logger.error(
                "Sending: cannot enqueue safety pause because the controller "
                "command queue is unavailable"
            )
            return
        try:
            command_queue.put_nowait(
                PauseCommandEvent(tag="pause", data=PauseCommandData())
            )
            self._harvest_pause_enqueued = True
        except Exception:
            self.logger.exception("Sending: failed to enqueue safety pause")

    def _hasUncreditedHarvestDrop(self) -> bool:
        piece = self.piece
        return bool(
            piece is not None and piece.aborted
            and piece.stage is PieceStage.distributing and piece.distributed_at is None
            and piece.transport_failure_reason == HARVEST_CONFIRMATION_UNCREDITED
            and piece.c4_marker_exit_boundary is not None
            and not bool(getattr(self.shared.transport, "dynamic_mode", False))
        )

    def _recoverHarvestConfirmationFailure(self, exc: Exception) -> bool:
        from project_harvest_runtime import (
            is_confirmation_evidence_failure, retire_unconfirmed_piece_drop,
        )

        piece = self.piece
        # SENDING sees this marker-owned piece only after confirmed discharge.
        # It cannot reroute that physical drop or manufacture a reject receipt.
        if (piece is None or piece.c4_marker_exit_boundary is None
            or bool(getattr(self.shared.transport, "dynamic_mode", False))
            or not is_confirmation_evidence_failure(exc)):
            return False
        retired = retire_unconfirmed_piece_drop(
            self.gc, piece, reason=f"Harvest confirmation uncredited ({exc.code}): {exc}",
        )
        piece.aborted = True
        piece.transport_failure_reason = HARVEST_CONFIRMATION_UNCREDITED
        piece.updated_at = time.time()
        self.event_queue.put(knownObjectToEvent(piece))
        self._committed = True
        self._setOccupancyState("sending.harvest_confirmation_uncredited")
        self.logger.warning(
            "Sending: piece %s discharged but Harvest confirmation is uncredited (%s); "
            "retired %s original planned allocation(s), continuing without delivery credit",
            piece.uuid, exc.code, retired,
        )
        return True

    def _pauseForHarvestFailure(self, detail: str) -> None:
        try:
            with shared_state.hardware_lifecycle_lock:
                shared_state.setHardwareStatus(
                    error=f"Harvest live sorting paused: {detail}"
                )
        except Exception:
            pass
        self._enqueuePause()

    def _shouldReopenGate(self) -> bool:
        if self.piece is not None and self.piece.c4_marker_exit_boundary is not None:
            return time.time() - self.start_time >= self._settleMs() / 1000.0
        if bool(getattr(self.shared, "sample_collection_mode", False)):
            return True

        piece = self.piece
        # When layer servos are disabled (simulated distributor) the chute
        # door never opens, so a piece that physically reaches the chute
        # stays parked there inside the carousel camera view. The tracker
        # then keeps reporting its global_id alive and the gate would never
        # reopen — pipeline deadlock after 1-2 pieces. Fall back to the
        # cooldown-only gate in that mode so distribution still ticks.
        track_id = getattr(piece, "tracked_global_id", None) if piece is not None else None
        if isinstance(track_id, int) and not self.gc.disable_servos:
            vision = self.vision
            if vision is not None and hasattr(vision, "getFeederTrackerLiveGlobalIds"):
                try:
                    live = vision.getFeederTrackerLiveGlobalIds("carousel")
                except Exception:
                    live = None
                if isinstance(live, (set, frozenset)) and int(track_id) in live:
                    # Piece still visible on the carousel tracker — hold
                    # the gate closed regardless of cooldown.
                    if self._pieceExitWaitTimedOut():
                        return self._handlePieceExitTimeout(int(track_id))
                    return False

        elapsed_since_drop = time.time() - self.start_time
        required_s = (self._settleMs() / 1000.0) + self._cooldown_s
        if elapsed_since_drop < required_s:
            return False
        return True

    def _pieceExitWaitTimedOut(self) -> bool:
        elapsed_ms = (time.time() - self.start_time) * 1000
        return elapsed_ms >= max(
            PIECE_EXIT_INCIDENT_MS,
            self._settleMs() + int(self._cooldown_s * 1000.0),
        )

    def _handlePieceExitTimeout(self, track_id: int) -> bool:
        piece = self.piece
        piece_uuid = str(getattr(piece, "uuid", "") or "")
        active = self._activeIncident()
        if self._exit_wait_incident_piece_uuid == piece_uuid:
            if (
                isinstance(active, dict)
                and active.get("kind") == CLASSIFICATION_TRACK_LOST_INCIDENT_KIND
                and active.get("piece_uuid") == piece_uuid
            ):
                self._setOccupancyState("sending.wait_piece_exit_incident")
                self.gc.runtime_stats.observeBlockedReason(
                    "distribution",
                    "sending_piece_exit_incident",
                )
                return False

            self.logger.warning(
                "Sending: C4 exit-wait incident for piece %s was cleared; "
                "reopening distribution gate"
                % (piece_uuid[:8] or "unknown")
            )
            self._forceKillLiveTrack(track_id)
            return True

        elapsed_ms = (time.time() - self.start_time) * 1000
        reason = f"distribution_drop_track_still_live_after_{int(elapsed_ms)}ms"
        published = False
        if piece is not None:
            published = publish_classification_track_lost_incident(
                self.gc,
                piece=piece,
                reason=reason,
            )
        if published:
            self._exit_wait_incident_piece_uuid = piece_uuid
            self._setOccupancyState("sending.wait_piece_exit_incident")
            self.logger.warning(
                "Sending: piece %s still visible on C4 tracker after %.0fms; "
                "waiting for operator incident"
                % (piece_uuid[:8] or "unknown", elapsed_ms)
            )
            return False

        self.logger.warning(
            "Sending: piece %s still visible on C4 tracker after %.0fms, "
            "but track-lost incidents are disabled/unavailable; reopening gate"
            % (piece_uuid[:8] or "unknown", elapsed_ms)
        )
        self.gc.runtime_stats.observeBlockedReason(
            "distribution",
            "sending_piece_exit_timeout_reopened",
        )
        self._forceKillLiveTrack(track_id)
        return True

    def _activeIncident(self) -> dict | None:
        runtime_stats = getattr(self.gc, "runtime_stats", None)
        if runtime_stats is None or not hasattr(runtime_stats, "activeIncident"):
            return None
        try:
            active = runtime_stats.activeIncident()
        except Exception:
            return None
        return active if isinstance(active, dict) else None

    def _forceKillLiveTrack(self, track_id: int) -> None:
        vision = self.vision
        if vision is None or not hasattr(vision, "forceKillCarouselTrack"):
            return
        try:
            vision.forceKillCarouselTrack(int(track_id))
        except Exception:
            pass

    def _settleMs(self) -> int:
        if bool(getattr(self.shared, "sample_collection_mode", False)):
            return SAMPLE_COLLECTION_CHUTE_SETTLE_MS
        return CHUTE_SETTLE_MS

    def cleanup(self) -> None:
        super().cleanup()
        self._occupancy_state = None
        self.gc.runtime_stats.endState("distribution.occupancy")
        self.piece = None
        self.start_time = 0.0
        self._committed = False
        self._exit_wait_incident_piece_uuid = None
        self._harvest_pause_enqueued = False
        self._native_incident = None
