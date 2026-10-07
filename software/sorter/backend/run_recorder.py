import time
from typing import Optional
from blob_manager import BLOB_DIR
from defs.known_object import KnownObject
from global_config import GlobalConfig
import piece_records
import runtime_stat_records

# Legacy location of the old per-run JSON dumps. Nothing writes here anymore —
# both piece history and runtime-stats history now live in the local_state
# SQLite DB. Kept only so the one-off migration script can still find old files.
RECORDS_DIR = BLOB_DIR / "records"


def _serializePiece(p: KnownObject) -> dict:
    return {
        "uuid": p.uuid,
        "transfer_episode_id": p.transfer_episode_id,
        "transfer_first_pass": p.transfer_first_pass,
        "transport_failure_reason": p.transport_failure_reason,
        "forced_reject_reason": p.forced_reject_reason,
        "reject_category": p.reject_category,
        "physical_group_size_unknown": p.physical_group_size_unknown,
        "harvest_exception": p.harvest_exception,
        "created_at": p.created_at,
        "classified_at": p.classified_at,
        "distributing_at": p.distributing_at,
        "distributed_at": p.distributed_at,
        "classification_status": p.classification_status.value,
        "part_id": p.part_id,
        "part_name": p.part_name,
        "color_id": p.color_id,
        "color_name": p.color_name,
        "category_id": p.category_id,
        "confidence": p.confidence,
        "color_confidence": p.color_confidence,
        "destination_bin": list(p.destination_bin) if p.destination_bin else None,
        "brickognize_preview_url": p.brickognize_preview_url,
        # Correction-submission provenance from the applied Brickognize request.
        "brickognize_listing_id": p.brickognize_listing_id,
        "brickognize_item_rank": p.brickognize_item_rank,
        "brickognize_item_type": p.brickognize_item_type,
        "brickognize_color_rank": p.brickognize_color_rank,
        # Which service actually produced the applied color / mold.
        "color_provider": p.color_provider,
        "mold_provider": p.mold_provider,
    }


class RunRecorder:
    def __init__(self, gc: GlobalConfig):
        self.gc = gc
        self.run_id = gc.run_id
        self.machine_id = gc.machine_id
        self.sorting_profile_path = gc.sorting_profile_path
        self.started_at = time.time()
        self.pieces: list[KnownObject] = []
        self._native_recorded_pieces: set[str] = set()
        self.active_periods: list[dict[str, float]] = []
        self._current_active_start: Optional[float] = None

    def markRunning(self) -> None:
        if self._current_active_start is None:
            self._current_active_start = time.time()

    def markPaused(self) -> None:
        if self._current_active_start is not None:
            self.active_periods.append({
                "start": self._current_active_start,
                "end": time.time(),
            })
            self._current_active_start = None

    def recordPiece(self, piece: KnownObject) -> None:
        self.pieces.append(piece)
        # Durable write the instant the piece commits — survives the dev
        # soft-restart (os._exit) that skips save(). DB is the source of truth
        # for sorting history now; the JSON file below only carries the
        # runtime-stats / set-progress snapshots other pages still read.
        try:
            piece_records.recordPiece(
                _serializePiece(piece),
                run_id=self.run_id,
                machine_id=self.machine_id,
            )
        except Exception as e:
            self.gc.logger.warning(f"RunRecorder: failed to persist piece {piece.uuid}: {e}")

    def recordCommittedNativePiece(
        self, piece: KnownObject, reservation_id: str, delivery_id: str,
    ) -> None:
        """Adopt verified native history in memory without a second DB upsert."""
        from smart_bins_native_completion import verify_native_identity

        row = verify_native_identity(
            self.machine_id, reservation_id, piece.uuid, delivery_id)
        if row["runtime_run_id"] != self.run_id:
            raise ValueError("native history belongs to another runtime run")
        if piece.uuid not in self._native_recorded_pieces:
            self.pieces.append(piece)
            self._native_recorded_pieces.add(piece.uuid)

    def save(self) -> None:
        self.markPaused()
        ended_at = time.time()

        tracker = getattr(self.gc, "set_progress_tracker", None)
        if tracker is not None:
            tracker.save()

        if not hasattr(self.gc, "runtime_stats"):
            return
        try:
            runtime_stat_records.saveRun(
                self.run_id,
                self.gc.runtime_stats.snapshot(),
                machine_id=self.machine_id,
                sorting_profile_path=self.sorting_profile_path,
                started_at=self.started_at,
                ended_at=ended_at,
                total_pieces=len(self.pieces),
            )
            self.gc.logger.info(
                f"RunRecorder: saved run {self.run_id} runtime stats ({len(self.pieces)} pieces)"
            )
        except Exception as e:
            self.gc.logger.warning(f"RunRecorder: failed to save runtime stats: {e}")
