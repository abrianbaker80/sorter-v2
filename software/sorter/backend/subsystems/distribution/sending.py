import time
import queue
from typing import Optional
from states.base_state import BaseState
from subsystems.shared_variables import SharedVariables
from .states import DistributionState
from irl.config import IRLInterface
from global_config import GlobalConfig
from utils.event import knownObjectToEvent
from defs.known_object import PieceStage


CHUTE_SETTLE_MS = 1500
SAMPLE_COLLECTION_CHUTE_SETTLE_MS = 400
MISSING_DROP_PIECE_GRACE_MS = 1500


class Sending(BaseState):
    def __init__(
        self,
        irl: IRLInterface,
        gc: GlobalConfig,
        shared: SharedVariables,
        event_queue: queue.Queue,
        *,
        post_distribute_cooldown_s: float = 0.0,
    ):
        super().__init__(irl, gc)
        self.shared = shared
        self.event_queue = event_queue
        self._cooldown_s = max(0.0, float(post_distribute_cooldown_s))
        self.piece = None
        self.start_time: float = 0.0
        self._occupancy_state: str | None = None
        self._committed: bool = False
        self._native_pending_event_sent = False
        self._native_next_check_at = 0.0
        self._native_last_error: str | None = None

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
        if self.piece is None and not self._committed:
            if self.start_time <= 0.0:
                self.start_time = now
            transport = self.shared.transport
            self.piece = (
                transport.getPieceForDistributionDrop()
                if transport is not None
                else None
            )
            if self.piece is None:
                elapsed_ms = (now - self.start_time) * 1000
                self._setOccupancyState("sending.wait_drop_piece")
                if elapsed_ms >= MISSING_DROP_PIECE_GRACE_MS:
                    adapter = getattr(self.gc, "smart_bins_native_adapter", None)
                    if (adapter is not None and adapter.active and
                        adapter.current is not None):
                        # A missing drop object cannot erase durable custody.
                        return None
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

        elapsed_ms = (now - self.start_time) * 1000
        settle_ms = self._settleMs()
        self._setOccupancyState("sending.wait_chute_settle")
        if elapsed_ms < settle_ms:
            return None

        if self.piece and self.piece.native_reservation_id:
            return self._stepNative()

        # Commit the piece once (stats, event, recorder) — must not repeat
        # even if we decide to hold the gate for additional cooldown below.
        if not self._committed:
            self.logger.info(f"Sending: settle complete ({elapsed_ms:.0f}ms)")
            self._setOccupancyState("sending.commit_piece")
            if self.piece and self._alreadyCommitted(self.piece):
                # The drop slot still holds the piece committed on an earlier
                # cycle: READY released without a new drop (the gate fallback).
                # Recording it again would double-count it in the run history
                # and set progress.
                self.logger.warning(
                    f"Sending: piece {self.piece.uuid[:8]} was already committed; not recording it again"
                )
            elif self.piece:
                self.piece.stage = PieceStage.distributed
                self.piece.distributed_at = time.time()
                self.piece.updated_at = time.time()
                self.event_queue.put(knownObjectToEvent(self.piece))
                self.gc.run_recorder.recordPiece(self.piece)
                tracker = getattr(self.gc, 'set_progress_tracker', None)
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
            self._committed = True

        if not self._shouldReopenGate():
            self._setOccupancyState("sending.wait_piece_exit")
            return None

        self.shared.set_distribution_gate(True, reason=None)
        return DistributionState.IDLE

    def _nativeEffect(self, completion, effect: str, callback) -> bool:
        piece = self.piece
        identity = (piece.native_machine_id, piece.native_reservation_id,
                    piece.uuid, piece.native_delivery_id)
        phase = completion.effect_phase(*identity, effect)
        if phase == "SUCCEEDED":
            return True
        if phase == "ATTEMPTED":
            raise completion.NativeCompletionRefused(
                "Ambiguous callback attempt requires reconciliation"
            )
        attempt_id = completion.begin_effect(*identity, effect)
        if attempt_id is None:
            raise completion.NativeCompletionRefused(
                "Publication effect changed before callback"
            )
        try:
            callback()
        except Exception as exc:
            failure_phase = (
                "GATE_OPEN" if effect == "GATE_OPEN" else
                "ADMISSION_RELEASE" if effect == "ADMISSION_RELEASE" else
                "PUBLICATION_CALLBACKS"
            )
            completion.record_effect_failure(
                *identity, effect, attempt_id, failure_phase, str(exc)
            )
            raise
        completion.finish_effect(*identity, effect, attempt_id)
        return True

    def _stepNative(self) -> Optional[DistributionState]:
        import smart_bins_native_completion as completion

        if time.time() < self._native_next_check_at:
            return None
        piece = self.piece
        machine_id = piece.native_machine_id
        reservation_id = piece.native_reservation_id
        if not machine_id or not reservation_id or machine_id != self.gc.machine_id:
            self.logger.warning("Sending: native provenance is incomplete or foreign")
            return None
        try:
            delivery_id = completion.complete_native(piece, machine_id,
                                                     reservation_id, self.gc.run_id)
            if delivery_id is None:
                # Route and settle are not receiving proof. No history, count or
                # progress is published for this held claim.
                if not self._native_pending_event_sent:
                    piece.destination_bin = None
                    self.event_queue.put(knownObjectToEvent(piece))
                    self._native_pending_event_sent = True
                self._setOccupancyState("sending.wait_piece_exit")
                self._native_next_check_at = time.time() + 1.0
                return None
            piece.native_delivery_id = delivery_id
            details = completion.delivery_details(machine_id, reservation_id,
                                                  piece.uuid, delivery_id)
            piece.destination_bin = details["destination_bin"]
            piece.distributed_at = details["distributed_at"]
            piece.updated_at = time.time()
            piece.stage = PieceStage.distributed

            if not self._nativeEffect(
                completion, "EVENT_ENQUEUED",
                lambda: self.event_queue.put(knownObjectToEvent(piece))
            ):
                return None
            if not self._nativeEffect(
                completion, "RUN_RECORDER_ADOPTED",
                lambda: self.gc.run_recorder.recordCommittedNativePiece(
                    piece, reservation_id, delivery_id)
            ):
                return None
            tracker = getattr(self.gc, "set_progress_tracker", None)
            if not self._nativeEffect(
                completion, "PROGRESS_RECORDED",
                lambda: tracker.record(piece.part_id, piece.color_id,
                                       piece.category_id) if tracker else None
            ):
                return None
            if not self._nativeEffect(
                completion, "PROGRESS_SYNC_NOTIFIED",
                lambda: __import__("server.set_progress_sync", fromlist=[
                    "getSetProgressSyncWorker"]).getSetProgressSyncWorker().notify()
                if tracker else None
            ):
                return None
            if completion.effect_phase(machine_id, reservation_id, piece.uuid,
                                       delivery_id, "CLOSE") != "SUCCEEDED":
                completion.close_publication(machine_id, reservation_id,
                                             piece.uuid, delivery_id)
            if not self._shouldReopenGate():
                return None
            if completion.current_blocker(
                machine_id, except_reservation=reservation_id
            ):
                return None
            if not self._nativeEffect(
                completion, "GATE_OPEN",
                lambda: self.shared.set_distribution_gate(True, reason=None)
            ):
                return None
            adapter = getattr(self.gc, "smart_bins_native_adapter", None)
            if adapter is None:
                return None
            if not self._nativeEffect(
                completion, "ADMISSION_RELEASE",
                lambda: adapter.release_completed(piece.uuid, reservation_id,
                                                  delivery_id)
            ):
                return None
            return DistributionState.IDLE
        except Exception as exc:
            message = str(exc)
            if message != self._native_last_error:
                self.logger.warning(
                    f"Sending: native completion remains held: {message}"
                )
                self._native_last_error = message
            self._native_next_check_at = time.time() + 1.0
            return None

    @staticmethod
    def _alreadyCommitted(piece) -> bool:
        return piece.stage == PieceStage.distributed or piece.distributed_at is not None

    def _shouldReopenGate(self) -> bool:
        if bool(getattr(self.shared, "sample_collection_mode", False)):
            return True

        elapsed_since_drop = time.time() - self.start_time
        required_s = (self._settleMs() / 1000.0) + self._cooldown_s
        if elapsed_since_drop < required_s:
            return False
        return True

    def _settleMs(self) -> int:
        if bool(getattr(self.shared, "sample_collection_mode", False)):
            return SAMPLE_COLLECTION_CHUTE_SETTLE_MS
        return CHUTE_SETTLE_MS

    def cleanup(self) -> None:
        super().cleanup()
        self.piece = None
        self.start_time = 0.0
        self._committed = False
        self._native_pending_event_sent = False
        self._native_next_check_at = 0.0
        self._native_last_error = None
