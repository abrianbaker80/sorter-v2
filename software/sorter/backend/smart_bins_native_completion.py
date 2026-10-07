"""Inactive, explicitly injected C4-to-Sending native completion adapter.

The physical bridge owns release and exit proof. This adapter carries that
proof across the existing transport slot and settles the same reservation once.
It never authorizes motion or starts a background retry.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from uuid import uuid4

import smart_bins_delivery as delivery
import smart_bins_completion_recovery as completion_recovery
from run_recorder import _serializePiece


class NativeCompletionError(RuntimeError):
    """The guarded handoff or durable completion cannot be adopted."""


class CurrentFollowupHeld(NativeCompletionError):
    """A historical write receipt does not authorize the current action."""


_HARVEST_FIELDS = (
    "harvest_project_id", "harvest_activation_id", "harvest_allocation_id",
    "harvest_group_id", "harvest_group_label", "harvest_exception",
)


def _harvest_bound(piece) -> bool:
    return any(getattr(piece, name, None) for name in _HARVEST_FIELDS)


def verify_native_identity(machine_id: str, reservation_id: str, piece_uuid: str,
                           delivery_id: str | None = None) -> dict:
    """Read actual ledger facts; caller supplied provenance is never a waiver."""
    if not all(isinstance(value, str) and value for value in
               (machine_id, reservation_id, piece_uuid)):
        raise NativeCompletionError("native reservation identity is incomplete")
    with delivery._readonly() as conn:
        reservation = conn.execute(
            "SELECT machine_id,piece_uuid,state FROM smart_bin_reservations "
            "WHERE machine_id=? AND id=?", (machine_id, reservation_id)).fetchone()
        if reservation is None or reservation["piece_uuid"] != piece_uuid:
            raise NativeCompletionError("native reservation belongs to another piece")
        result = {"state": reservation["state"]}
        if delivery_id is None:
            return result
        if not isinstance(delivery_id, str) or not delivery_id:
            raise NativeCompletionError("native delivery identity is incomplete")
        row = conn.execute(
            "SELECT d.id,d.reservation_id,d.machine_id,d.piece_uuid,d.run_id,"
            "d.actual_kind,d.actual_slot_id,d.actual_cycle_id,d.delivered_at,"
            "p.run_id AS runtime_run_id,p.machine_id AS history_machine_id "
            "FROM smart_bin_deliveries d "
            "JOIN piece_records p ON p.uuid=d.piece_uuid "
            "WHERE d.id=?", (delivery_id,)).fetchone()
        if (row is None or reservation["state"] != "COMPLETED"
            or (row["machine_id"], row["reservation_id"], row["piece_uuid"])
            != (machine_id, reservation_id, piece_uuid)
            or row["history_machine_id"] != machine_id
            or not row["runtime_run_id"]):
            raise NativeCompletionError("native delivery receipt is not durable for this piece")
        result.update(dict(row))
        return result


@dataclass
class NativeHandoff:
    piece: object
    machine_id: str
    reservation_id: str
    custody: delivery.CustodyRef
    attempt_id: str
    target_boundary: int
    marker_evidence_ref: str
    intended_kind: str
    intended_slot_id: str | None
    intended_cycle_id: str | None
    sorting_session_id: str
    destination_bin: tuple[int, int, int] | None
    request_key: str | None = None
    followup_id: str | None = None
    followup_revision: int = 0
    evidence: delivery.CompletionEvidence | None = None
    receipt: dict | None = None
    completion_error: Exception | None = None
    publication_state: str = "pending"
    publication_error: str | None = None
    publication_exception: Exception | None = None
    transport: object | None = None
    admission_released: bool = False
    ownership_lost: bool = False
    ownership_loss_reason: str | None = None
    ownership_loss_persisted: bool = False
    ownership_loss_observed_revision: int | None = None
    publication_durable: bool = False
    closure_durable: bool = False
    side_effect_reason: str | None = None
    side_effect_phase: str | None = None
    side_effect_observed_revision: int | None = None
    side_effect_persisted: bool = False


class NativeCompletionAdapter:
    """One retained guarded handoff; prior receipts remain in the ledger."""

    def __init__(self, bridge):
        if bridge is None or not getattr(bridge, "machine_id", None):
            raise ValueError("native completion requires an attached physical bridge")
        self.bridge = bridge
        self._handoff: NativeHandoff | None = None
        bridge.attach_completion_guard(self)

    def capture_handoff(self, event, binding) -> NativeHandoff:
        """Validate durable EXIT_CONFIRMED before transport can advance."""
        self.bridge.owner.assert_locked()
        state = self.bridge.index
        if (binding is None or not getattr(binding, "reservation_id", None)
            or state is None or state.stage != "handoff"
            or state.binding is not binding
            or state.reservation_id != binding.reservation_id
            or state.exit_evidence is None or state.exit_key is None
            or state.custody is None or not state.attempt_id
            or event.boundary != state.target.boundary
            or (event.pocket.pocket_id, event.pocket.generation) != binding.key):
            raise NativeCompletionError("guarded C4 exit lacks its matching handoff")
        piece = binding.piece
        if _harvest_bound(piece):
            raise NativeCompletionError("Harvest-bound C4 exit requires a separate bridge")
        if (getattr(piece, "native_machine_id", None) not in (None, self.bridge.machine_id)
            or getattr(piece, "native_reservation_id", None) not in
                (None, binding.reservation_id)
            or getattr(piece, "native_delivery_id", None) is not None):
            raise NativeCompletionError("foreign or completed native metadata on C4 piece")
        if (state.custody.piece_uuid != piece.uuid
            or state.exit_evidence.custody != state.custody
            or state.exit_evidence.attempt_id != state.attempt_id
            or state.exit_evidence.target_boundary != event.boundary
            or not state.exit_evidence.marker_confirmed):
            raise NativeCompletionError("marker proof differs from current custody")
        if self._handoff is not None and self._handoff.publication_state != "complete":
            raise NativeCompletionError("previous native completion remains held")
        with delivery._readonly() as conn:
            row = conn.execute(
                "SELECT r.*,a.id AS release_attempt_id,a.target_boundary AS release_boundary,"
                "e.exit_json FROM smart_bin_reservations r "
                "JOIN smart_bin_release_attempts a ON a.reservation_id=r.id "
                "JOIN smart_bin_release_evidence e ON e.attempt_id=a.id "
                "WHERE r.machine_id=? AND r.id=?",
                (self.bridge.machine_id, binding.reservation_id)).fetchone()
            if (row is None or row["state"] != "EXIT_CONFIRMED"
                or row["row_revision"] != 2
                or row["piece_uuid"] != piece.uuid
                or row["owner_incarnation"] != state.custody.owner_incarnation
                or row["episode_id"] != state.custody.episode_id
                or row["pocket_index"] != state.custody.pocket_index
                or row["pocket_generation"] != state.custody.pocket_generation
                or row["release_attempt_id"] != state.attempt_id
                or str(row["release_boundary"]) != str(event.boundary)
                or not row["exit_json"]
                or json.loads(row["exit_json"]) != asdict(state.exit_evidence)):
                raise NativeCompletionError("durable exit proof differs from C4 owner evidence")
            if row["intended_kind"] == "BIN":
                slot = conn.execute(
                    "SELECT layer_index,section_index,bin_index FROM smart_bin_slots "
                    "WHERE machine_id=? AND id=?",
                    (self.bridge.machine_id, row["intended_slot_id"])).fetchone()
                if slot is None:
                    raise NativeCompletionError("reserved destination slot is unavailable")
                destination_bin = tuple(slot)
            elif row["intended_kind"] == "REJECT":
                destination_bin = None
            else:
                raise NativeCompletionError("reserved destination kind is invalid")
            if (tuple(piece.destination_bin) if piece.destination_bin is not None else None) != destination_bin:
                raise NativeCompletionError("physical piece route differs from reserved destination")
            return NativeHandoff(
                piece, self.bridge.machine_id, binding.reservation_id,
                state.custody, state.attempt_id, event.boundary,
                state.exit_evidence.marker_evidence_ref, row["intended_kind"],
                row["intended_slot_id"], row["intended_cycle_id"], row["run_id"],
                destination_bin, request_key=str(uuid4()), followup_id=str(uuid4()))

    def retain_handoff(self, handoff: NativeHandoff, *, transport) -> None:
        previous = self._handoff
        if previous is not None and previous is not handoff:
            self.bridge.owner.assert_locked()
            if (not previous.admission_released or not previous.closure_durable
                or previous.publication_state != "complete" or previous.ownership_lost
                or previous.transport is not transport):
                raise NativeCompletionError("previous native completion remains held")
            try:
                self.require_current_followup(previous, states=("CLOSED",))
            except CurrentFollowupHeld as exc:
                raise NativeCompletionError("previous native completion remains held") from exc
        if previous is not handoff:
            self._handoff = handoff
            handoff.transport = transport
            handoff.piece.native_machine_id = handoff.machine_id
            handoff.piece.native_reservation_id = handoff.reservation_id
        elif handoff.transport is not transport:
            raise NativeCompletionError("retained native transport changed")
        self.persist_retained_handoff()

    def persist_retained_handoff(self) -> None:
        """Retry only the frozen pre-transport write; never advance transport."""
        handoff = self._handoff
        if handoff is None:
            raise NativeCompletionError("native handoff is not retained")
        retained = completion_recovery.stage_handoff(handoff)
        if retained["state"] != "RETAINED":
            raise NativeCompletionError("durable native handoff is not retained")
        row = self._durable_followup(handoff)
        if (row["completion_request_key"] != handoff.request_key
            or row["reservation_id"] != handoff.reservation_id
            or row["release_attempt_id"] != handoff.attempt_id
            or row["piece_uuid"] != handoff.piece.uuid
            or row["row_revision"] != handoff.followup_revision
            or row["ownership_loss_reason"] is not None
            or row["failure_kind"] is not None):
            raise NativeCompletionError("retained handoff receipt is no longer current")
        # The receipt describes revision zero. A replay after delivery linkage
        # must not move an already progressed in-memory handoff backwards.
        if row["state"] != "RETAINED" and handoff.receipt is None:
            raise NativeCompletionError("retained handoff progressed without its receipt")

    def require_handoff(self, piece) -> NativeHandoff:
        handoff = self._handoff
        if (handoff is None or handoff.piece is not piece
            or piece.native_machine_id != handoff.machine_id
            or piece.native_reservation_id != handoff.reservation_id
            or _harvest_bound(piece)):
            raise NativeCompletionError("distribution drop lacks matching native completion identity")
        expected_delivery = handoff.receipt["delivery_id"] if handoff.receipt else None
        if piece.native_delivery_id not in (None, expected_delivery):
            raise NativeCompletionError("distribution drop carries a foreign delivery receipt")
        return handoff

    def check_drop_ownership(self, transport) -> NativeHandoff | None:
        """Latch any observed loss against the retained handoff, before caching."""
        handoff = self._handoff
        if handoff is None:
            return None
        if handoff.ownership_lost:
            raise NativeCompletionError("guarded distribution drop ownership was lost")
        if transport is None or transport is not handoff.transport:
            self._latch_ownership_loss(handoff, "guarded distribution transport changed")
            raise NativeCompletionError("guarded distribution transport changed")
        try:
            dynamic = getattr(transport, "dynamic_mode", False)
            if not dynamic:
                current = transport.getPieceForDistributionDrop()
        except Exception as exc:
            self._latch_ownership_loss(
                handoff, "guarded distribution drop ownership read failed")
            raise NativeCompletionError("guarded distribution drop ownership read failed") from exc
        if dynamic or current is not handoff.piece:
            self._latch_ownership_loss(handoff, "guarded distribution drop ownership changed")
            raise NativeCompletionError("guarded distribution drop ownership changed")
        return handoff

    @staticmethod
    def _latch_ownership_loss(handoff: NativeHandoff, reason: str) -> None:
        handoff.ownership_lost = True
        if handoff.ownership_loss_reason is None:
            handoff.ownership_loss_reason = reason

    def persist_ownership_loss(self) -> None:
        """Called after the in-memory gate is fenced and pause is queued."""
        handoff = self._handoff
        if handoff is None or not handoff.ownership_lost or handoff.ownership_loss_persisted:
            return
        if handoff.ownership_loss_observed_revision is None:
            handoff.ownership_loss_observed_revision = handoff.followup_revision
        result = completion_recovery.record_hold(
            handoff.machine_id, handoff.followup_id, kind="OWNERSHIP_LOST",
            reason=handoff.ownership_loss_reason,
            expected_revision=handoff.ownership_loss_observed_revision,
            request_key=f"{handoff.request_key}:ownership-loss")
        handoff.followup_revision = result["row_revision"]
        handoff.ownership_loss_persisted = True

    def persist_side_effect_failure(self, reason: str, *, failure_phase: str | None = None) -> None:
        handoff = self._handoff
        if handoff is None or handoff.side_effect_persisted:
            return
        if handoff.side_effect_reason is None:
            handoff.side_effect_reason = reason
            handoff.side_effect_phase = failure_phase
            handoff.side_effect_observed_revision = handoff.followup_revision
        result = completion_recovery.record_hold(
            handoff.machine_id, handoff.followup_id, kind="SIDE_EFFECT_FAILED",
            reason=handoff.side_effect_reason,
            expected_revision=handoff.side_effect_observed_revision,
            request_key=f"{handoff.request_key}:side-effect-failed",
            failure_phase=handoff.side_effect_phase)
        handoff.followup_revision = result["row_revision"]
        handoff.side_effect_persisted = True

    def _durable_followup(self, handoff: NativeHandoff) -> dict:
        with delivery._readonly() as conn:
            return completion_recovery.read_on_connection(
                conn, handoff.machine_id, handoff.followup_id)

    def require_current_followup(self, handoff: NativeHandoff, *,
                                 states: tuple[str, ...]) -> dict:
        """Authorize against current durable evidence, never a replayed receipt."""
        try:
            with delivery._readonly() as conn:
                row, other_blockers = completion_recovery.authorization_on_connection(
                    conn, handoff.machine_id, handoff.followup_id)
            expected_delivery = handoff.receipt["delivery_id"] if handoff.receipt else None
            if (other_blockers or row["state"] not in states or row["missing_evidence"]
                or row["reservation_id"] != handoff.reservation_id
                or row["release_attempt_id"] != handoff.attempt_id
                or row["piece_uuid"] != handoff.piece.uuid
                or row["owner_incarnation"] != handoff.custody.owner_incarnation
                or row["custody_json"] != json.dumps(
                    asdict(handoff.custody), sort_keys=True, allow_nan=False)
                or row["marker_evidence_ref"] != handoff.marker_evidence_ref
                or row["completion_request_key"] != handoff.request_key
                or row["delivery_id"] != expected_delivery
                or row["ownership_loss_reason"] is not None
                or row["failure_kind"] is not None
                or handoff.ownership_lost):
                raise CurrentFollowupHeld("current native follow-up is held or contradicted")
            return row
        except CurrentFollowupHeld:
            raise
        except Exception as exc:
            raise CurrentFollowupHeld("current native follow-up eligibility unavailable") from exc

    def require_clear_predecessors(self, handoff: NativeHandoff) -> None:
        """Keep a new attempt pending if an older machine obligation is held."""
        try:
            with delivery._readonly() as conn:
                _, other_blockers = completion_recovery.authorization_on_connection(
                    conn, handoff.machine_id, handoff.followup_id)
            if other_blockers:
                raise CurrentFollowupHeld("previous native follow-up is held")
        except CurrentFollowupHeld:
            raise
        except Exception as exc:
            raise CurrentFollowupHeld("previous follow-up eligibility unavailable") from exc

    def begin_publication(self, handoff: NativeHandoff) -> None:
        # A predecessor can acquire a late hold after this piece's delivery
        # commits. Refuse to mark the callback attempt before assessing it.
        # The current row may itself have a replayable attempt receipt; only
        # the post-transition check decides whether callbacks are authorized.
        self.require_clear_predecessors(handoff)
        completion_recovery.transition(
            handoff.machine_id, handoff.followup_id,
            action="PUBLICATION_ATTEMPT", expected_revision=handoff.followup_revision,
            request_key=f"{handoff.request_key}:publication-attempt",
            delivery_id=handoff.receipt["delivery_id"])
        row = self.require_current_followup(handoff, states=("PUBLICATION_ATTEMPTED",))
        handoff.followup_revision = row["row_revision"]

    def record_published(self, handoff: NativeHandoff) -> None:
        completion_recovery.transition(
            handoff.machine_id, handoff.followup_id,
            action="PUBLICATION_SUCCEEDED", expected_revision=handoff.followup_revision,
            request_key=f"{handoff.request_key}:publication-succeeded",
            delivery_id=handoff.receipt["delivery_id"])
        row = self.require_current_followup(handoff, states=("PUBLICATION_SUCCEEDED",))
        handoff.followup_revision = row["row_revision"]
        handoff.publication_durable = True

    def close(self, handoff: NativeHandoff) -> None:
        try:
            row = self.require_current_followup(
                handoff, states=("PUBLICATION_SUCCEEDED", "CLOSED"))
        except CurrentFollowupHeld as exc:
            raise CurrentFollowupHeld("native follow-up is not ready to close") from exc
        if handoff.closure_durable:
            if row["state"] != "CLOSED":
                raise CurrentFollowupHeld("current native closure is absent")
            return
        if not self.verified_piece(handoff.piece):
            raise NativeCompletionError("native follow-up is not ready to close")
        completion_recovery.transition(
            handoff.machine_id, handoff.followup_id, action="CLOSE",
            expected_revision=handoff.followup_revision,
            request_key=f"{handoff.request_key}:close",
            delivery_id=handoff.receipt["delivery_id"])
        row = self.require_current_followup(handoff, states=("CLOSED",))
        handoff.followup_revision = row["row_revision"]
        handoff.closure_durable = True

    def blocks_admission(self) -> bool:
        handoff = self._handoff
        return handoff is not None and not self.releases_admission(
            handoff.reservation_id, handoff.attempt_id)

    def releases_admission(self, reservation_id: str, attempt_id: str) -> bool:
        handoff = self._handoff
        if not (handoff is not None and handoff.reservation_id == reservation_id
                and handoff.attempt_id == attempt_id and handoff.receipt is not None
                and handoff.publication_state == "complete"
                and handoff.admission_released and not handoff.ownership_lost):
            return False
        try:
            self.require_current_followup(handoff, states=("CLOSED",))
            return True
        except CurrentFollowupHeld:
            return False

    def hold_admission(self, piece) -> None:
        if self._handoff is not None and self._handoff.piece is piece:
            self._handoff.admission_released = False

    def release_admission(self, piece) -> None:
        handoff = self.require_handoff(piece)
        if (handoff.receipt is None or handoff.publication_state != "complete"
            or handoff.ownership_lost
            or not handoff.closure_durable):
            raise NativeCompletionError("native completion is not ready for admission")
        self.require_current_followup(handoff, states=("CLOSED",))
        handoff.admission_released = True

    def verified_piece(self, piece) -> bool:
        try:
            verify_native_identity(piece.native_machine_id,
                                   piece.native_reservation_id, piece.uuid,
                                   piece.native_delivery_id)
            return bool(piece.native_delivery_id)
        except (AttributeError, NativeCompletionError):
            return False

    def complete(self, piece, *, runtime_run_id: str, completed_at: float) -> tuple[NativeHandoff, dict]:
        handoff = self.require_handoff(piece)
        if handoff.receipt is not None:
            row = verify_native_identity(handoff.machine_id, handoff.reservation_id,
                                         piece.uuid, handoff.receipt["delivery_id"])
            if row["runtime_run_id"] != handoff.evidence.runtime_run_id:
                raise NativeCompletionError("native history runtime run changed")
            return handoff, handoff.receipt
        if (piece.stage.value if hasattr(piece.stage, "value") else piece.stage) == "distributed":
            raise NativeCompletionError("distributed flag lacks a durable native receipt")
        if (tuple(piece.destination_bin) if piece.destination_bin is not None else None) != handoff.destination_bin:
            raise NativeCompletionError("distribution route changed after guarded handoff")
        if handoff.evidence is None:
            if not runtime_run_id or not isinstance(completed_at, (int, float)):
                raise NativeCompletionError("native completion needs run and settling time")
            history = _serializePiece(piece)
            request_key = handoff.request_key
            handoff.evidence = delivery.CompletionEvidence(
                handoff.custody, handoff.attempt_id, handoff.target_boundary,
                f"settled:{handoff.marker_evidence_ref}:{request_key}",
                completed_at, handoff.intended_kind, handoff.intended_slot_id,
                handoff.intended_cycle_id, handoff.sorting_session_id,
                runtime_run_id, history)
        result = delivery.complete_native(
            handoff.machine_id, handoff.reservation_id,
            expected_reservation_revision=2, request_key=handoff.request_key,
            evidence=handoff.evidence, followup_id=handoff.followup_id)
        if (result.get("code") != "OK"
            or result.get("attempt_id") != handoff.attempt_id
            or not result.get("delivery_id")
            or (result.get("actual_kind"), result.get("actual_slot_id"),
                result.get("actual_cycle_id")) !=
                (handoff.intended_kind, handoff.intended_slot_id, handoff.intended_cycle_id)):
            raise NativeCompletionError(f"native completion unresolved: {result.get('code')}")
        row = verify_native_identity(handoff.machine_id, handoff.reservation_id,
                                     piece.uuid, result["delivery_id"])
        if row["runtime_run_id"] != handoff.evidence.runtime_run_id:
            raise NativeCompletionError("native history runtime run differs from receipt")
        followup = self._durable_followup(handoff)
        if (followup["state"] != "DELIVERY_PENDING_PUBLICATION"
            or followup["delivery_id"] != result["delivery_id"]
            or followup["completion_request_key"] != handoff.request_key):
            raise NativeCompletionError("native delivery lacks a durable follow-up")
        handoff.followup_revision = followup["row_revision"]
        handoff.receipt = result
        return handoff, result
