"""Dormant durable delivery contracts retained for historical data compatibility.

RB02 does not qualify marker/pocket evidence for native v0.3.0.
These APIs only persist explicitly supplied evidence. They create no
observations, install no runtime hooks, and grant or dispatch no motion.
A future native adapter requires separately accepted physical evidence.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any

import smart_bins_completion_recovery as completion_recovery

from local_state import local_state_db_path
from piece_records import initialize_piece_records, recordPieceOnConnection
from smart_bins_service import (
    ReservationRequest, RoutingQualification, _candidate, _check_service_schema,
    _digest, _receipt, _refuse, _rollback_refusal, _save_receipt,
    _unresolved_journey_discrepancy, _validate_context, qualification_digest,
)
from smart_bins_storage import critical_transaction


@dataclass(frozen=True)
class CustodyRef:
    piece_uuid: str
    owner_incarnation: str
    episode_id: str
    pocket_index: int
    pocket_generation: int


@dataclass(frozen=True)
class ReleaseEvidence:
    custody: CustodyRef
    target_boundary: int
    destination_kind: str
    slot_id: str | None
    cycle_id: str | None
    current_owner: bool
    route_ready: bool
    owner_evidence_ref: str
    route_evidence_ref: str
    observed_at: float
    expires_at: float
    max_dimension_mm: float | None = None
    too_big: bool = False
    harvest_allocation_ref: str | None = None
    harvest_metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExitEvidence:
    custody: CustodyRef
    attempt_id: str
    target_boundary: int
    marker_confirmed: bool
    marker_evidence_ref: str
    confirmed_at: float


@dataclass(frozen=True)
class CompletionEvidence:
    custody: CustodyRef
    attempt_id: str
    target_boundary: int
    completion_evidence_ref: str
    completed_at: float
    actual_kind: str
    actual_slot_id: str | None
    actual_cycle_id: str | None
    sorting_session_id: str
    runtime_run_id: str
    history_piece: dict[str, Any]
    harvest_allocation_ref: str | None = None
    harvest_metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class UncertaintyEvidence:
    piece_uuid: str
    owner_incarnation: str | None
    reason: str
    evidence_ref: str
    attempt_id: str | None = None


@dataclass(frozen=True)
class ContradictionEvidence:
    reason: str
    evidence_ref: str
    actual_kind: str | None = None
    actual_slot_id: str | None = None
    actual_cycle_id: str | None = None


def _identity(machine_id: str, reservation_id: str, request_key: str,
              expected_reservation_revision: int) -> None:
    if (not all(isinstance(value, str) and value for value in
                (machine_id, reservation_id, request_key))
        or type(expected_reservation_revision) is not int
        or expected_reservation_revision < 0):
        raise ValueError("missing lifecycle identity or expected revision")


def _hash(action: str, machine_id: str, reservation_id: str, revision: int,
          evidence: Any, qualification: RoutingQualification | None = None) -> str:
    return _digest({"action": action, "machine_id": machine_id,
                    "reservation_id": reservation_id,
                    "expected_reservation_revision": revision,
                    "evidence": asdict(evidence),
                    "qualification": asdict(qualification) if qualification else None})


def _reservation(conn: sqlite3.Connection, machine_id: str, reservation_id: str):
    return conn.execute("SELECT * FROM smart_bin_reservations WHERE machine_id=? AND id=?",
                        (machine_id, reservation_id)).fetchone()


def _current(row: sqlite3.Row | None, revision: int, state: str | tuple[str, ...]):
    if row is None:
        return _refuse("NOT_FOUND")
    allowed = (state,) if isinstance(state, str) else state
    if row["state"] not in allowed:
        return _refuse("UNSAFE_STATE", state=row["state"])
    if row["row_revision"] != revision:
        return _refuse("STALE_REVISION", reservation_revision=row["row_revision"])
    return None


def _custody_matches(row: sqlite3.Row, custody: CustodyRef) -> bool:
    return (isinstance(custody.piece_uuid, str) and bool(custody.piece_uuid)
            and isinstance(custody.owner_incarnation, str) and bool(custody.owner_incarnation)
            and isinstance(custody.episode_id, str) and bool(custody.episode_id)
            and type(custody.pocket_index) is int and custody.pocket_index >= 0
            and type(custody.pocket_generation) is int and custody.pocket_generation >= 0
            and custody.piece_uuid == row["piece_uuid"]
            and custody.owner_incarnation == row["owner_incarnation"]
            and custody.episode_id == row["episode_id"]
            and custody.pocket_index == row["pocket_index"]
            and custody.pocket_generation == row["pocket_generation"])


def _destination_valid(kind: str | None, slot_id: str | None,
                       cycle_id: str | None) -> bool:
    return ((kind == "BIN" and bool(slot_id) and bool(cycle_id))
            or (kind == "REJECT" and slot_id is None and cycle_id is None))


def _destination_references_qualified(conn: sqlite3.Connection, machine_id: str,
                                      kind: str, slot_id: str | None,
                                      cycle_id: str | None) -> bool:
    """Validate identity without binding historical evidence to attachment."""
    if kind == "REJECT":
        return slot_id is None and cycle_id is None
    if kind != "BIN" or not slot_id or not cycle_id:
        return False
    return (conn.execute("SELECT 1 FROM smart_bin_slots WHERE machine_id=? AND id=?",
                         (machine_id, slot_id)).fetchone() is not None
            and conn.execute("SELECT 1 FROM smart_bin_cycles WHERE machine_id=? AND id=?",
                             (machine_id, cycle_id)).fetchone() is not None)


def _harvest_metadata(evidence: ReleaseEvidence | CompletionEvidence) -> bool:
    if evidence.harvest_allocation_ref is not None or evidence.harvest_metadata:
        return True
    if isinstance(evidence, CompletionEvidence):
        return any(key.startswith("harvest_") and key != "harvest_exception" and value
                   for key, value in evidence.history_piece.items())
    return False


def _attempt(conn: sqlite3.Connection, reservation_id: str):
    return conn.execute("SELECT a.id,a.owner_incarnation,a.target_boundary,a.created_at,"
                        "e.intent_json,e.exit_json,e.completion_json "
                        "FROM smart_bin_release_attempts a JOIN smart_bin_release_evidence e "
                        "ON e.attempt_id=a.id WHERE a.reservation_id=?", (reservation_id,)).fetchone()


def _finish(conn: sqlite3.Connection, row: sqlite3.Row, request_key: str,
            payload_hash: str, action: str, *, new_state: str | None,
            evidence_ref: str, result: dict[str, Any], code: str = "OK") -> dict[str, Any]:
    machine_id, reservation_id = row["machine_id"], row["id"]
    before = conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id=?",
                          (machine_id,)).fetchone()[0]
    now = time.time()
    row_revision = row["row_revision"]
    if new_state is not None:
        cursor = conn.execute("UPDATE smart_bin_reservations SET state=?,row_revision=row_revision+1,"
                              "updated_at=? WHERE id=? AND row_revision=? AND state=?",
                              (new_state, now, reservation_id, row_revision, row["state"]))
        if cursor.rowcount != 1:
            raise RuntimeError("reservation changed during lifecycle transaction")
        row_revision += 1
    conn.execute("UPDATE smart_bin_machines SET state_revision=? WHERE machine_id=?",
                 (before + 1, machine_id))
    conn.execute("INSERT INTO smart_bin_audit_events "
                 "(id,machine_id,reservation_id,request_key,payload_hash,before_revision,"
                 "after_revision,actor,reason,evidence_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (str(uuid.uuid4()), machine_id, reservation_id, request_key, payload_hash,
                  before, before + 1, "smart_bins_delivery", action.lower(), evidence_ref, now))
    receipt = {"code": code, "reservation_id": reservation_id,
               "state": new_state or row["state"], "reservation_revision": row_revision,
               "state_revision": before + 1, **result}
    _save_receipt(conn, request_key, machine_id, action, payload_hash,
                  reservation_id, receipt)
    conn.commit()
    return receipt


def prepare_release(machine_id: str, reservation_id: str, *,
                    expected_reservation_revision: int, request_key: str,
                    qualification: RoutingQualification,
                    evidence: ReleaseEvidence) -> dict[str, Any]:
    """Persist intent only. A replayed receipt never authorizes motor dispatch."""
    _identity(machine_id, reservation_id, request_key, expected_reservation_revision)
    payload_hash = _hash("PREPARE_RELEASE", machine_id, reservation_id,
                         expected_reservation_revision, evidence, qualification)
    with critical_transaction() as conn:
        _check_service_schema(conn)
        replay = _receipt(conn, request_key, payload_hash)
        if replay is not None:
            conn.commit()
            return replay
        row = _reservation(conn, machine_id, reservation_id)
        refusal = _current(row, expected_reservation_revision, "RESERVED")
        if refusal:
            return _rollback_refusal(conn, **refusal)
        if _unresolved_journey_discrepancy(conn, machine_id, row["piece_uuid"]):
            return _rollback_refusal(conn, "UNRESOLVED_DELIVERY")
        now = time.time()
        if (not _custody_matches(row, evidence.custody)
            or not evidence.current_owner or not evidence.route_ready
            or not evidence.owner_evidence_ref or not evidence.route_evidence_ref
            or type(evidence.target_boundary) is not int or evidence.target_boundary < 0
            or type(evidence.observed_at) not in (int, float)
            or type(evidence.expires_at) not in (int, float)
            or evidence.observed_at > now + 1 or now - evidence.observed_at > 5
            or evidence.expires_at <= now or evidence.expires_at - evidence.observed_at > 5):
            return _rollback_refusal(conn, "OWNER_EVIDENCE_REQUIRED")
        if _harvest_metadata(evidence):
            return _rollback_refusal(conn, "HARVEST_DEFERRED")
        if (not _destination_valid(evidence.destination_kind, evidence.slot_id, evidence.cycle_id)
            or (evidence.destination_kind, evidence.slot_id, evidence.cycle_id)
            != (row["intended_kind"], row["intended_slot_id"], row["intended_cycle_id"])):
            return _rollback_refusal(conn, "DESTINATION_MISMATCH")
        if (qualification.machine_id != machine_id
            or qualification.policy_revision_id != row["policy_revision_id"]
            or qualification.routing_revision != row["routing_revision"]):
            return _rollback_refusal(conn, "STALE_POLICY")
        slot = next((item for item in qualification.slots
                     if item.slot_id == row["intended_slot_id"]
                     and item.cycle_id == row["intended_cycle_id"]), None)
        if row["intended_kind"] == "BIN" and slot is None:
            return _rollback_refusal(conn, "UNQUALIFIED_DESTINATION")
        request = ReservationRequest(
            piece_uuid=row["piece_uuid"], route_attempt=row["route_attempt"],
            sorting_session_id=row["run_id"], group_key_id=row["group_key_id"],
            quantity=0, not_in_inventory=slot.not_in_inventory if slot else False,
            max_dimension_mm=evidence.max_dimension_mm, too_big=evidence.too_big)
        context = _validate_context(conn, request, qualification)
        if context is not None:
            return _rollback_refusal(conn, **context)
        if row["intended_kind"] == "BIN" and _candidate(
            conn, request, qualification, slot, misc="misc") is None:
            return _rollback_refusal(conn, "OCCUPIED_INCOMPATIBLE")
        if conn.execute("SELECT 1 FROM smart_bin_release_attempts WHERE "
                        "owner_incarnation=? AND target_boundary=?",
                        (evidence.custody.owner_incarnation,
                         str(evidence.target_boundary))).fetchone():
            return _rollback_refusal(conn, "TARGET_ALREADY_USED")
        attempt_id = str(uuid.uuid4())
        conn.execute("INSERT INTO smart_bin_release_attempts VALUES(?,?,?,?,?)",
                     (attempt_id, reservation_id, evidence.custody.owner_incarnation,
                      str(evidence.target_boundary), now))
        conn.execute("INSERT INTO smart_bin_release_evidence "
                     "(attempt_id,reservation_id,intent_json) VALUES(?,?,?)",
                     (attempt_id, reservation_id, json.dumps({"evidence": asdict(evidence),
                      "qualification_hash": qualification_digest(qualification)},
                      sort_keys=True, allow_nan=False)))
        return _finish(conn, row, request_key, payload_hash, "PREPARE_RELEASE",
                       new_state="RELEASE_INTENT", evidence_ref=evidence.route_evidence_ref,
                       result={"attempt_id": attempt_id,
                               "target_boundary": evidence.target_boundary})


def confirm_exit(machine_id: str, reservation_id: str, *,
                 expected_reservation_revision: int, request_key: str,
                 evidence: ExitEvidence) -> dict[str, Any]:
    _identity(machine_id, reservation_id, request_key, expected_reservation_revision)
    payload_hash = _hash("CONFIRM_EXIT", machine_id, reservation_id,
                         expected_reservation_revision, evidence)
    with critical_transaction() as conn:
        _check_service_schema(conn)
        replay = _receipt(conn, request_key, payload_hash)
        if replay is not None:
            conn.commit()
            return replay
        row = _reservation(conn, machine_id, reservation_id)
        refusal = _current(row, expected_reservation_revision, "RELEASE_INTENT")
        if refusal:
            return _rollback_refusal(conn, **refusal)
        attempt = _attempt(conn, reservation_id)
        if (attempt is None or not _custody_matches(row, evidence.custody)
            or evidence.attempt_id != attempt["id"]
            or str(evidence.target_boundary) != attempt["target_boundary"]
            or not evidence.marker_confirmed or not evidence.marker_evidence_ref
            or type(evidence.confirmed_at) not in (int, float)
            or evidence.confirmed_at < attempt["created_at"]):
            return _rollback_refusal(conn, "MARKER_EVIDENCE_REQUIRED")
        conn.execute("UPDATE smart_bin_release_evidence SET exit_json=? WHERE attempt_id=? "
                     "AND exit_json IS NULL",
                     (json.dumps(asdict(evidence), sort_keys=True, allow_nan=False),
                      attempt["id"]))
        return _finish(conn, row, request_key, payload_hash, "CONFIRM_EXIT",
                       new_state="EXIT_CONFIRMED", evidence_ref=evidence.marker_evidence_ref,
                       result={"attempt_id": attempt["id"]})


def complete_native(machine_id: str, reservation_id: str, *,
                    expected_reservation_revision: int, request_key: str,
                    evidence: CompletionEvidence,
                    followup_id: str | None = None) -> dict[str, Any]:
    _identity(machine_id, reservation_id, request_key, expected_reservation_revision)
    payload_hash = _hash("COMPLETE_NATIVE", machine_id, reservation_id,
                         expected_reservation_revision, evidence)
    if followup_id is not None:
        if not isinstance(followup_id, str) or not followup_id:
            raise ValueError("invalid native follow-up identity")
        payload_hash = _digest({"completion_hash": payload_hash,
                                "followup_id": followup_id})
    # Resolve an ambiguous acknowledgement without a schema write. Recheck
    # inside the write transaction after preparing history for a new result.
    with _readonly() as conn:
        replay = _receipt(conn, request_key, payload_hash)
        if replay is not None:
            if followup_id is not None:
                followup = completion_recovery.read_on_connection(
                    conn, machine_id, followup_id)
                if (followup["reservation_id"] != reservation_id
                    or followup["completion_request_key"] != request_key
                    or followup["delivery_id"] != replay.get("delivery_id")):
                    raise completion_recovery.CompletionRecoveryError(
                        "native completion replay lacks its follow-up")
            return replay
    if _harvest_metadata(evidence):
        return _refuse("HARVEST_DEFERRED")
    # Schema changes cannot occur inside the critical delivery transaction.
    initialize_piece_records()
    with critical_transaction() as conn:
        _check_service_schema(conn)
        replay = _receipt(conn, request_key, payload_hash)
        if replay is not None:
            conn.commit()
            return replay
        row = _reservation(conn, machine_id, reservation_id)
        refusal = _current(row, expected_reservation_revision, "EXIT_CONFIRMED")
        if refusal:
            return _rollback_refusal(conn, **refusal)
        if followup_id is not None:
            completion_recovery.check_schema(conn)
            staged = conn.execute(
                "SELECT id FROM smart_bin_completion_followups WHERE reservation_id=?",
                (reservation_id,)).fetchone()
            if staged is None or staged["id"] != followup_id:
                return _rollback_refusal(conn, "FOLLOWUP_REQUIRED")
        else:
            # The explicit extension may be absent for a standalone caller.
            has_followups = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND "
                "name='smart_bin_completion_followups'").fetchone()
            if has_followups and conn.execute(
                "SELECT 1 FROM smart_bin_completion_followups WHERE reservation_id=?",
                (reservation_id,)).fetchone():
                return _rollback_refusal(conn, "FOLLOWUP_REQUIRED")
        if _harvest_metadata(evidence):
            return _rollback_refusal(conn, "HARVEST_DEFERRED")
        if conn.execute("SELECT 1 FROM smart_bin_discrepancies WHERE reservation_id=? "
                        "AND resolved_at IS NULL", (reservation_id,)).fetchone():
            return _rollback_refusal(conn, "UNRESOLVED_DELIVERY")
        attempt = _attempt(conn, reservation_id)
        exit_data = json.loads(attempt["exit_json"]) if attempt and attempt["exit_json"] else None
        if (attempt is None or exit_data is None
            or not _custody_matches(row, evidence.custody)
            or evidence.attempt_id != attempt["id"]
            or str(evidence.target_boundary) != attempt["target_boundary"]
            or not evidence.completion_evidence_ref
            or type(evidence.completed_at) not in (int, float)
            or evidence.completed_at < exit_data["confirmed_at"]
            or evidence.sorting_session_id != row["run_id"]
            or not evidence.runtime_run_id
            or evidence.history_piece.get("uuid") != row["piece_uuid"]):
            return _rollback_refusal(conn, "COMPLETION_EVIDENCE_REQUIRED")
        if not _destination_valid(evidence.actual_kind, evidence.actual_slot_id,
                                  evidence.actual_cycle_id):
            return _rollback_refusal(conn, "DESTINATION_MISMATCH")
        if not _destination_references_qualified(
            conn, machine_id, evidence.actual_kind, evidence.actual_slot_id,
            evidence.actual_cycle_id):
            return _rollback_refusal(conn, "UNQUALIFIED_DESTINATION")
        if ((evidence.actual_kind, evidence.actual_slot_id, evidence.actual_cycle_id)
            != (row["intended_kind"], row["intended_slot_id"], row["intended_cycle_id"])):
            conn.execute("INSERT INTO smart_bin_discrepancies "
                         "(id,machine_id,reservation_id,cycle_id,kind,status,evidence_ref,"
                         "details_json,created_at) VALUES(?,?,?,?,?,'open',?,?,?)",
                         (str(uuid.uuid4()), machine_id, reservation_id,
                          evidence.actual_cycle_id, "NATIVE_DESTINATION_MISMATCH",
                          evidence.completion_evidence_ref,
                          json.dumps(asdict(evidence), sort_keys=True, allow_nan=False), time.time()))
            if followup_id is not None:
                completion_recovery.bind_delivery(
                    conn, machine_id=machine_id, followup_id=followup_id,
                    reservation_id=reservation_id, request_key=request_key,
                    evidence=evidence, delivery_id=None)
            result = _finish(conn, row, request_key, payload_hash, "COMPLETE_NATIVE",
                             new_state="UNCERTAIN", evidence_ref=evidence.completion_evidence_ref,
                             result={"delivery_id": None, "discrepancy": "NATIVE_DESTINATION_MISMATCH"},
                             code="DESTINATION_MISMATCH")
            return result
        if evidence.actual_kind == "BIN":
            dest = conn.execute("SELECT layer_index,section_index,bin_index FROM "
                                "smart_bin_slots WHERE machine_id=? AND id=?",
                                (machine_id, evidence.actual_slot_id)).fetchone()
            destination_bin = list(dest)
        else:
            destination_bin = None
        delivery_id = str(uuid.uuid4())
        conn.execute("INSERT INTO smart_bin_deliveries "
                     "(id,reservation_id,machine_id,piece_uuid,run_id,policy_revision_id,"
                     "group_key_id,quantity,intended_kind,intended_slot_id,intended_cycle_id,"
                     "actual_kind,actual_slot_id,actual_cycle_id,evidence_ref,delivered_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (delivery_id, reservation_id, machine_id, row["piece_uuid"], row["run_id"],
                      row["policy_revision_id"], row["group_key_id"], row["quantity"],
                      row["intended_kind"], row["intended_slot_id"], row["intended_cycle_id"],
                      evidence.actual_kind, evidence.actual_slot_id, evidence.actual_cycle_id,
                      evidence.completion_evidence_ref, evidence.completed_at))
        history = dict(evidence.history_piece)
        history["destination_bin"] = destination_bin
        history["distributed_at"] = evidence.completed_at
        recordPieceOnConnection(conn, history, run_id=evidence.runtime_run_id,
                                machine_id=machine_id)
        conn.execute("UPDATE smart_bin_release_evidence SET completion_json=? "
                     "WHERE attempt_id=? AND completion_json IS NULL",
                     (json.dumps(asdict(evidence), sort_keys=True, allow_nan=False),
                      attempt["id"]))
        if followup_id is not None:
            completion_recovery.bind_delivery(
                conn, machine_id=machine_id, followup_id=followup_id,
                reservation_id=reservation_id, request_key=request_key,
                evidence=evidence, delivery_id=delivery_id)
        return _finish(conn, row, request_key, payload_hash, "COMPLETE_NATIVE",
                       new_state="COMPLETED", evidence_ref=evidence.completion_evidence_ref,
                       result={"attempt_id": attempt["id"], "delivery_id": delivery_id,
                               "actual_kind": evidence.actual_kind,
                               "actual_slot_id": evidence.actual_slot_id,
                               "actual_cycle_id": evidence.actual_cycle_id})


def mark_uncertain(machine_id: str, reservation_id: str, *,
                   expected_reservation_revision: int, request_key: str,
                   evidence: UncertaintyEvidence) -> dict[str, Any]:
    _identity(machine_id, reservation_id, request_key, expected_reservation_revision)
    payload_hash = _hash("MARK_UNCERTAIN", machine_id, reservation_id,
                         expected_reservation_revision, evidence)
    with critical_transaction() as conn:
        _check_service_schema(conn)
        replay = _receipt(conn, request_key, payload_hash)
        if replay is not None:
            conn.commit()
            return replay
        row = _reservation(conn, machine_id, reservation_id)
        refusal = _current(row, expected_reservation_revision,
                           ("RESERVED", "RELEASE_INTENT", "EXIT_CONFIRMED", "UNCERTAIN"))
        if refusal:
            return _rollback_refusal(conn, **refusal)
        attempt = _attempt(conn, reservation_id)
        if (evidence.piece_uuid != row["piece_uuid"] or not evidence.reason
            or not evidence.evidence_ref
            or (row["owner_incarnation"] is not None
                and evidence.owner_incarnation != row["owner_incarnation"])
            or (attempt is not None and evidence.attempt_id != attempt["id"])
            or (attempt is None and evidence.attempt_id is not None)):
            return _rollback_refusal(conn, "UNCERTAINTY_EVIDENCE_REQUIRED")
        conn.execute("INSERT INTO smart_bin_discrepancies "
                     "(id,machine_id,reservation_id,cycle_id,kind,status,evidence_ref,"
                     "details_json,created_at) VALUES(?,?,?,?,?,'open',?,?,?)",
                     (str(uuid.uuid4()), machine_id, reservation_id, row["intended_cycle_id"],
                      "NATIVE_UNCERTAIN", evidence.evidence_ref,
                      json.dumps(asdict(evidence), sort_keys=True, allow_nan=False), time.time()))
        return _finish(conn, row, request_key, payload_hash, "MARK_UNCERTAIN",
                       new_state="UNCERTAIN", evidence_ref=evidence.evidence_ref,
                       result={"attempt_id": attempt["id"] if attempt else None})


def record_contradiction(machine_id: str, reservation_id: str, *,
                         expected_reservation_revision: int, request_key: str,
                         evidence: ContradictionEvidence) -> dict[str, Any]:
    """Retain later contradictory evidence without rewriting terminal facts."""
    _identity(machine_id, reservation_id, request_key, expected_reservation_revision)
    payload_hash = _hash("RECORD_CONTRADICTION", machine_id, reservation_id,
                         expected_reservation_revision, evidence)
    with critical_transaction() as conn:
        _check_service_schema(conn)
        replay = _receipt(conn, request_key, payload_hash)
        if replay is not None:
            conn.commit()
            return replay
        row = _reservation(conn, machine_id, reservation_id)
        refusal = _current(row, expected_reservation_revision, ("COMPLETED", "CANCELLED"))
        if refusal:
            return _rollback_refusal(conn, **refusal)
        if not evidence.reason or not evidence.evidence_ref:
            return _rollback_refusal(conn, "CONTRADICTION_EVIDENCE_REQUIRED")
        if evidence.actual_kind is None and (
            evidence.actual_slot_id is not None or evidence.actual_cycle_id is not None):
            return _rollback_refusal(conn, "DESTINATION_MISMATCH")
        if evidence.actual_kind is not None and not _destination_valid(
            evidence.actual_kind, evidence.actual_slot_id, evidence.actual_cycle_id):
            return _rollback_refusal(conn, "DESTINATION_MISMATCH")
        if evidence.actual_kind is not None and not _destination_references_qualified(
            conn, machine_id, evidence.actual_kind, evidence.actual_slot_id,
            evidence.actual_cycle_id):
            return _rollback_refusal(conn, "DESTINATION_MISMATCH")
        discrepancy_id = str(uuid.uuid4())
        conn.execute("INSERT INTO smart_bin_discrepancies "
                     "(id,machine_id,reservation_id,cycle_id,kind,status,evidence_ref,"
                     "details_json,created_at) VALUES(?,?,?,?,?,'open',?,?,?)",
                     (discrepancy_id, machine_id, reservation_id,
                      evidence.actual_cycle_id or row["intended_cycle_id"],
                      "TERMINAL_CONTRADICTION", evidence.evidence_ref,
                      json.dumps(asdict(evidence), sort_keys=True, allow_nan=False), time.time()))
        return _finish(conn, row, request_key, payload_hash, "RECORD_CONTRADICTION",
                       new_state=None, evidence_ref=evidence.evidence_ref,
                       result={"discrepancy_id": discrepancy_id})


def lookup_request(request_key: str) -> dict[str, Any] | None:
    """Read a committed receipt after an ambiguous acknowledgement."""
    if not request_key:
        raise ValueError("missing request key")
    with _readonly() as conn:
        row = conn.execute("SELECT action,payload_hash,result_json FROM "
                           "smart_bin_request_receipts WHERE request_key=?",
                           (request_key,)).fetchone()
        return ({"action": row[0], "payload_hash": row[1],
                 "result": json.loads(row[2])} if row else None)


def validate_physical_claim(machine_id: str, reservation_id: str, custody: CustodyRef,
                            *, state: str, revision: int,
                            attempt_id: str | None = None,
                            target_boundary: int | None = None,
                            qualification: RoutingQualification | None = None) -> str:
    """Read-only custody fence for the guarded physical owner.

    This does not issue a motion permit. The caller must also prove its current
    hardware ownership and physical route while holding the controller locks.
    """
    if state not in ("RESERVED", "RELEASE_INTENT"):
        raise ValueError("unsupported physical claim state")
    with _readonly() as conn:
        row = _reservation(conn, machine_id, reservation_id)
        if row is None or row["state"] != state or row["row_revision"] != revision:
            return "UNSAFE_STATE"
        if not _custody_matches(row, custody):
            return "CUSTODY_MISMATCH"
        if _unresolved_journey_discrepancy(conn, machine_id, row["piece_uuid"]):
            return "UNRESOLVED_DELIVERY"
        if state == "RELEASE_INTENT":
            attempt = _attempt(conn, reservation_id)
            if (attempt is None or attempt["id"] != attempt_id
                or attempt["owner_incarnation"] != custody.owner_incarnation
                or attempt["target_boundary"] != str(target_boundary)
                or attempt["exit_json"] is not None):
                return "ATTEMPT_MISMATCH"
            if qualification is None:
                return "UNQUALIFIED_DESTINATION"
            if (qualification.machine_id != machine_id
                or qualification.policy_revision_id != row["policy_revision_id"]
                or qualification.routing_revision != row["routing_revision"]):
                return "STALE_POLICY"
            slot = next((item for item in qualification.slots
                         if item.slot_id == row["intended_slot_id"]
                         and item.cycle_id == row["intended_cycle_id"]), None)
            if row["intended_kind"] == "BIN" and slot is None:
                return "UNQUALIFIED_DESTINATION"
            request = ReservationRequest(
                piece_uuid=row["piece_uuid"], route_attempt=row["route_attempt"],
                sorting_session_id=row["run_id"], group_key_id=row["group_key_id"],
                quantity=0, not_in_inventory=slot.not_in_inventory if slot else False)
            context = _validate_context(conn, request, qualification)
            if context is not None:
                return context["code"]
            if row["intended_kind"] == "BIN" and _candidate(
                conn, request, qualification, slot, misc="misc") is None:
                return "OCCUPIED_INCOMPATIBLE"
        return "OK"


def inspect_recovery(machine_id: str) -> dict[str, Any]:
    """Read outstanding custody, evidence and blockers without a write lock."""
    if not machine_id:
        raise ValueError("missing machine")
    with _readonly() as conn:
        revision = conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id=?",
                                (machine_id,)).fetchone()
        if revision is None:
            raise ValueError("unknown machine")
        claims = [dict(row) for row in conn.execute(
            "SELECT * FROM smart_bin_reservations WHERE machine_id=? AND state IN "
            "('RESERVED','RELEASE_INTENT','EXIT_CONFIRMED','UNCERTAIN') ORDER BY created_at,id",
            (machine_id,))]
        for claim in claims:
            attempt = _attempt(conn, claim["id"])
            claim["attempt"] = ({"id": attempt["id"],
                                 "target_boundary": attempt["target_boundary"],
                                 "intent": json.loads(attempt["intent_json"]),
                                 "exit": json.loads(attempt["exit_json"]) if attempt["exit_json"] else None}
                                if attempt else None)
        discrepancies = [dict(row) for row in conn.execute(
            "SELECT * FROM smart_bin_discrepancies WHERE machine_id=? AND resolved_at IS NULL "
            "ORDER BY created_at,id", (machine_id,))]
        followups = completion_recovery.inspect_on_connection(conn, machine_id)
        from smart_bins_harvest_integration import inspect_on_connection
        harvest = inspect_on_connection(conn, machine_id)
        return {"machine_id": machine_id, "state_revision": revision[0],
                "claims": claims, "unresolved_discrepancies": discrepancies,
                **followups, **harvest}


@contextmanager
def _readonly():
    path = local_state_db_path()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        _check_service_schema(conn)
        yield conn
        conn.rollback()
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
