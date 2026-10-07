"""Inactive durable follow-up for a guarded native C4 completion.

Initialization is explicit. The row records the obligation before the
distribution transport advances; no callback or hardware action runs in a
SQLite transaction. An attempted publication is never replayed on restart.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from smart_bins_service import _check_service_schema, _digest
from smart_bins_storage import critical_transaction


SCHEMA_VERSION = 1


class CompletionRecoveryError(RuntimeError):
    """A guarded follow-up cannot be established or advanced."""


_DDL = (
    "CREATE TABLE smart_bin_completion_versions ("
    "singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton=1), "
    "version INTEGER NOT NULL, initialized_at REAL NOT NULL)",
    "CREATE TABLE smart_bin_completion_receipts ("
    "request_key TEXT NOT NULL PRIMARY KEY, machine_id TEXT NOT NULL, "
    "action TEXT NOT NULL, payload_hash TEXT NOT NULL, result_json TEXT NOT NULL, "
    "created_at REAL NOT NULL, "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id))",
    "CREATE TABLE smart_bin_completion_followups ("
    "id TEXT NOT NULL PRIMARY KEY, machine_id TEXT NOT NULL, "
    "reservation_id TEXT NOT NULL UNIQUE, release_attempt_id TEXT NOT NULL UNIQUE, "
    "owner_incarnation TEXT NOT NULL, piece_uuid TEXT NOT NULL, "
    "custody_json TEXT NOT NULL, marker_evidence_ref TEXT NOT NULL, "
    "intended_kind TEXT NOT NULL, intended_slot_id TEXT, intended_cycle_id TEXT, "
    "completion_request_key TEXT NOT NULL UNIQUE, completion_json TEXT, "
    "delivery_id TEXT UNIQUE, state TEXT NOT NULL CHECK(state IN "
    "('RETAINED','HELD','DELIVERY_PENDING_PUBLICATION',"
    "'PUBLICATION_ATTEMPTED','PUBLICATION_SUCCEEDED','CLOSED')), "
    "row_revision INTEGER NOT NULL DEFAULT 0 CHECK(row_revision>=0), "
    "ownership_loss_reason TEXT, failure_kind TEXT, failure_reason TEXT, "
    "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
    "CHECK((delivery_id IS NULL AND state IN ('RETAINED','HELD')) OR "
    "(delivery_id IS NOT NULL AND state IN "
    "('DELIVERY_PENDING_PUBLICATION','PUBLICATION_ATTEMPTED',"
    "'PUBLICATION_SUCCEEDED','CLOSED'))), "
    "CHECK((state='RETAINED' AND completion_json IS NULL) OR "
    "(state<>'RETAINED' AND completion_json IS NOT NULL)), "
    "FOREIGN KEY(machine_id,reservation_id) REFERENCES smart_bin_reservations(machine_id,id), "
    "FOREIGN KEY(release_attempt_id) REFERENCES smart_bin_release_attempts(id), "
    "FOREIGN KEY(delivery_id) REFERENCES smart_bin_deliveries(id))",
    "CREATE TRIGGER smart_bin_completion_source_insert "
    "BEFORE INSERT ON smart_bin_completion_followups WHEN NOT EXISTS ("
    "SELECT 1 FROM smart_bin_reservations r "
    "JOIN smart_bin_release_attempts a ON a.reservation_id=r.id "
    "JOIN smart_bin_release_evidence e ON e.attempt_id=a.id "
    "WHERE r.id=NEW.reservation_id AND r.machine_id=NEW.machine_id "
    "AND r.piece_uuid=NEW.piece_uuid AND r.owner_incarnation=NEW.owner_incarnation "
    "AND a.id=NEW.release_attempt_id AND e.exit_json IS NOT NULL) "
    "BEGIN SELECT RAISE(ABORT,'completion source mismatch'); END",
    "CREATE TRIGGER smart_bin_completion_identity_immutable "
    "BEFORE UPDATE OF id,machine_id,reservation_id,release_attempt_id,"
    "owner_incarnation,piece_uuid,custody_json,marker_evidence_ref,"
    "intended_kind,intended_slot_id,intended_cycle_id,completion_request_key "
    "ON smart_bin_completion_followups BEGIN "
    "SELECT RAISE(ABORT,'completion identity is immutable'); END",
    "CREATE TRIGGER smart_bin_completion_delivery_match "
    "BEFORE UPDATE OF delivery_id ON smart_bin_completion_followups "
    "WHEN NEW.delivery_id IS NOT NULL AND NOT EXISTS ("
    "SELECT 1 FROM smart_bin_deliveries d WHERE d.id=NEW.delivery_id "
    "AND d.reservation_id=NEW.reservation_id AND d.machine_id=NEW.machine_id "
    "AND d.piece_uuid=NEW.piece_uuid) "
    "BEGIN SELECT RAISE(ABORT,'completion delivery mismatch'); END",
    "CREATE TRIGGER smart_bin_completion_no_delete "
    "BEFORE DELETE ON smart_bin_completion_followups BEGIN "
    "SELECT RAISE(ABORT,'completion follow-up is retained'); END",
)

_REQUIRED = {
    "smart_bin_completion_versions", "smart_bin_completion_receipts",
    "smart_bin_completion_followups",
    "smart_bin_completion_source_insert", "smart_bin_completion_identity_immutable",
    "smart_bin_completion_delivery_match", "smart_bin_completion_no_delete",
}


def check_schema(conn) -> None:
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'smart_bin_completion_%'"
    )}
    if names != _REQUIRED:
        raise CompletionRecoveryError("inactive completion follow-up schema is absent or incomplete")
    rows = conn.execute("SELECT singleton,version FROM smart_bin_completion_versions").fetchall()
    if len(rows) != 1 or tuple(rows[0]) != (1, SCHEMA_VERSION):
        raise CompletionRecoveryError(
            "unsupported completion follow-up schema; migration is not implemented")


def initialize_schema() -> int:
    """Explicit installation only; an earlier incompatible extension is refused."""
    with critical_transaction() as conn:
        _check_service_schema(conn)
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'smart_bin_completion_%' LIMIT 1"
        ).fetchone()
        if present:
            check_schema(conn)
        else:
            for statement in _DDL:
                conn.execute(statement)
            conn.execute("INSERT INTO smart_bin_completion_versions VALUES(1,?,?)",
                         (SCHEMA_VERSION, time.time()))
        conn.commit()
    return SCHEMA_VERSION


def _row(conn, machine_id: str, followup_id: str):
    return conn.execute(
        "SELECT * FROM smart_bin_completion_followups WHERE machine_id=? AND id=?",
        (machine_id, followup_id)).fetchone()


def _receipt(conn, key: str, payload_hash: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_hash,result_json FROM smart_bin_completion_receipts "
        "WHERE request_key=?", (key,)).fetchone()
    if row is None:
        if conn.execute("SELECT 1 FROM smart_bin_audit_events WHERE request_key=?",
                        (key,)).fetchone():
            raise CompletionRecoveryError("follow-up idempotency conflict")
        return None
    if row["payload_hash"] != payload_hash:
        raise CompletionRecoveryError("follow-up idempotency conflict")
    return json.loads(row["result_json"])


def _save_receipt(conn, key: str, machine_id: str, action: str,
                  payload_hash: str, result: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO smart_bin_completion_receipts VALUES(?,?,?,?,?,?)",
        (key, machine_id, action, payload_hash,
         json.dumps(result, sort_keys=True, allow_nan=False), time.time()))


def _audit(conn, row, key: str, payload_hash: str, action: str, evidence_ref: str) -> int:
    before_row = conn.execute(
        "SELECT state_revision FROM smart_bin_machines WHERE machine_id=?",
        (row["machine_id"],)).fetchone()
    if before_row is None:
        raise CompletionRecoveryError("completion machine is absent")
    before = before_row[0]
    conn.execute("UPDATE smart_bin_machines SET state_revision=? WHERE machine_id=?",
                 (before + 1, row["machine_id"]))
    conn.execute(
        "INSERT INTO smart_bin_audit_events "
        "(id,machine_id,reservation_id,request_key,payload_hash,before_revision,"
        "after_revision,actor,reason,evidence_ref,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (str(uuid4()), row["machine_id"], row["reservation_id"], key, payload_hash,
         before, before + 1, "smart_bins_completion_recovery", action,
         evidence_ref, time.time()))
    return before + 1


def _finish(conn, row, key: str, payload_hash: str, action: str,
            evidence_ref: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    state_revision = _audit(conn, row, key, payload_hash, action, evidence_ref)
    result = {"code": "OK", "followup_id": row["id"], "state": row["state"],
              "row_revision": row["row_revision"], "state_revision": state_revision}
    if extra:
        result.update(extra)
    _save_receipt(conn, key, row["machine_id"], action, payload_hash, result)
    conn.commit()
    return result


def stage_handoff(handoff) -> dict[str, Any]:
    """Commit the exact owner/custody obligation before transport advancement."""
    if not all((handoff.followup_id, handoff.request_key, handoff.machine_id,
                handoff.reservation_id, handoff.attempt_id, handoff.piece.uuid)):
        raise CompletionRecoveryError("incomplete native follow-up identity")
    custody_json = json.dumps(asdict(handoff.custody), sort_keys=True, allow_nan=False)
    facts = (handoff.followup_id, handoff.machine_id, handoff.reservation_id,
             handoff.attempt_id, handoff.custody.owner_incarnation, handoff.piece.uuid,
             custody_json, handoff.marker_evidence_ref, handoff.intended_kind,
             handoff.intended_slot_id, handoff.intended_cycle_id, handoff.request_key)
    key = f"{handoff.request_key}:retain"
    payload_hash = _digest({"action": "RETAIN_HANDOFF", "facts": facts})
    with critical_transaction() as conn:
        _check_service_schema(conn)
        check_schema(conn)
        replay = _receipt(conn, key, payload_hash)
        if replay is not None:
            if replay.get("code") != "OK":
                raise CompletionRecoveryError("native handoff idempotency conflict")
            conn.commit()
            return replay
        reservation = conn.execute(
            "SELECT * FROM smart_bin_reservations WHERE machine_id=? AND id=?",
            (handoff.machine_id, handoff.reservation_id)).fetchone()
        attempt = conn.execute(
            "SELECT a.*,e.exit_json FROM smart_bin_release_attempts a "
            "JOIN smart_bin_release_evidence e ON e.attempt_id=a.id WHERE a.id=?",
            (handoff.attempt_id,)).fetchone()
        if (reservation is None or reservation["state"] != "EXIT_CONFIRMED"
            or reservation["row_revision"] != 2 or attempt is None
            or attempt["reservation_id"] != handoff.reservation_id
            or attempt["owner_incarnation"] != handoff.custody.owner_incarnation
            or reservation["piece_uuid"] != handoff.piece.uuid
            or reservation["owner_incarnation"] != handoff.custody.owner_incarnation
            or (reservation["episode_id"], reservation["pocket_index"],
                reservation["pocket_generation"]) !=
                (handoff.custody.episode_id, handoff.custody.pocket_index,
                 handoff.custody.pocket_generation)
            or (reservation["intended_kind"], reservation["intended_slot_id"],
                reservation["intended_cycle_id"]) !=
                (handoff.intended_kind, handoff.intended_slot_id,
                 handoff.intended_cycle_id)
            or str(attempt["target_boundary"]) != str(handoff.target_boundary)
            or not attempt["exit_json"]
            or json.loads(attempt["exit_json"])["marker_evidence_ref"]
                != handoff.marker_evidence_ref):
            raise CompletionRecoveryError("durable native exit does not match follow-up")
        now = time.time()
        conn.execute(
            "INSERT INTO smart_bin_completion_followups "
            "(id,machine_id,reservation_id,release_attempt_id,owner_incarnation,"
            "piece_uuid,custody_json,marker_evidence_ref,intended_kind,"
            "intended_slot_id,intended_cycle_id,completion_request_key,state,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'RETAINED',?,?)",
            (*facts, now, now))
        row = _row(conn, handoff.machine_id, handoff.followup_id)
        return _finish(conn, row, key, payload_hash, "RETAIN_HANDOFF",
                       handoff.marker_evidence_ref)


def bind_delivery(conn, *, machine_id: str, followup_id: str,
                  reservation_id: str, request_key: str, evidence,
                  delivery_id: str | None) -> None:
    """Called inside complete_native's transaction, before its sole commit."""
    check_schema(conn)
    row = _row(conn, machine_id, followup_id)
    if (row is None or row["reservation_id"] != reservation_id
        or row["completion_request_key"] != request_key
        or row["release_attempt_id"] != evidence.attempt_id
        or row["piece_uuid"] != evidence.custody.piece_uuid
        or row["custody_json"] != json.dumps(asdict(evidence.custody),
                                            sort_keys=True, allow_nan=False)
        or row["state"] != "RETAINED" or row["row_revision"] != 0
        or row["ownership_loss_reason"] is not None):
        raise CompletionRecoveryError("native completion follow-up is missing or held")
    completion_json = json.dumps(asdict(evidence), sort_keys=True, allow_nan=False)
    state = "DELIVERY_PENDING_PUBLICATION" if delivery_id else "HELD"
    conn.execute(
        "UPDATE smart_bin_completion_followups SET completion_json=?,delivery_id=?,"
        "state=?,row_revision=1,updated_at=? WHERE id=? AND row_revision=0",
        (completion_json, delivery_id, state, time.time(), followup_id))
    updated = _row(conn, machine_id, followup_id)
    _audit(conn, updated, f"{request_key}:delivery-link",
           _digest({"action": "DELIVERY_LINK", "completion": completion_json,
                    "delivery_id": delivery_id}),
           "delivery_link" if delivery_id else "delivery_held",
           evidence.completion_evidence_ref)


def transition(machine_id: str, followup_id: str, *, action: str,
               expected_revision: int, request_key: str, delivery_id: str,
               reason: str | None = None) -> dict[str, Any]:
    """Revisioned, action-qualified state change with exact replay first."""
    states = {
        "PUBLICATION_ATTEMPT": ("DELIVERY_PENDING_PUBLICATION", "PUBLICATION_ATTEMPTED"),
        "PUBLICATION_SUCCEEDED": ("PUBLICATION_ATTEMPTED", "PUBLICATION_SUCCEEDED"),
        "CLOSE": ("PUBLICATION_SUCCEEDED", "CLOSED"),
    }
    if action not in states or not all((machine_id, followup_id, request_key, delivery_id)):
        raise ValueError("invalid completion follow-up action or identity")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("invalid expected follow-up revision")
    payload_hash = _digest({"action": action, "machine_id": machine_id,
                            "followup_id": followup_id, "delivery_id": delivery_id,
                            "expected_revision": expected_revision, "reason": reason})
    with critical_transaction() as conn:
        _check_service_schema(conn)
        check_schema(conn)
        replay = _receipt(conn, request_key, payload_hash)
        if replay is not None:
            if replay.get("code") != "OK":
                raise CompletionRecoveryError("follow-up idempotency conflict")
            conn.commit()
            return replay
        row = _row(conn, machine_id, followup_id)
        before, after = states[action]
        if (row is None or row["state"] != before
            or row["row_revision"] != expected_revision
            or row["delivery_id"] != delivery_id
            or row["ownership_loss_reason"] is not None
            or row["failure_kind"] is not None):
            raise CompletionRecoveryError("follow-up transition is stale or held")
        conn.execute(
            "UPDATE smart_bin_completion_followups SET state=?,"
            "row_revision=row_revision+1,updated_at=? WHERE id=? AND row_revision=?",
            (after, time.time(), followup_id, expected_revision))
        updated = _row(conn, machine_id, followup_id)
        return _finish(conn, updated, request_key, payload_hash, action, delivery_id)


def record_hold(machine_id: str, followup_id: str, *, kind: str, reason: str,
                expected_revision: int, request_key: str,
                failure_phase: str | None = None) -> dict[str, Any]:
    """Keep the first witnessed loss/failure; a closed row can gain a blocker."""
    if kind not in ("OWNERSHIP_LOST", "SIDE_EFFECT_FAILED") or not all(
        (machine_id, followup_id, reason, request_key)
    ) or type(expected_revision) is not int or expected_revision < 0 or (
        failure_phase is not None and (kind != "SIDE_EFFECT_FAILED" or failure_phase not in
        ("PUBLICATION_CALLBACKS", "GATE_OPEN", "ADMISSION_RELEASE"))
    ):
        raise ValueError("invalid completion hold")
    payload = {"action": kind, "machine_id": machine_id,
               "followup_id": followup_id, "reason": reason,
               "expected_revision": expected_revision}
    # Preserve the exact payload hash of older, phase-unattributed receipts.
    if failure_phase is not None:
        payload["failure_phase"] = failure_phase
    payload_hash = _digest(payload)
    with critical_transaction() as conn:
        _check_service_schema(conn)
        check_schema(conn)
        replay = _receipt(conn, request_key, payload_hash)
        if replay is not None:
            if replay.get("code") != "OK":
                raise CompletionRecoveryError("follow-up hold idempotency conflict")
            conn.commit()
            return replay
        row = _row(conn, machine_id, followup_id)
        if row is None:
            raise CompletionRecoveryError("completion follow-up is absent")
        # A late witnessed contradiction remains evidence even when a close
        # raced it. Record the stale observation and keep the durable blocker.
        stale_observation = row["row_revision"] != expected_revision
        column = "ownership_loss_reason" if kind == "OWNERSHIP_LOST" else "failure_reason"
        if row[column] is not None:
            raise CompletionRecoveryError("follow-up already has first hold evidence")
        if kind == "OWNERSHIP_LOST":
            conn.execute(
                "UPDATE smart_bin_completion_followups SET ownership_loss_reason=?,"
                "row_revision=row_revision+1,updated_at=? WHERE id=?",
                (reason, time.time(), followup_id))
        else:
            conn.execute(
                "UPDATE smart_bin_completion_followups SET failure_kind=?,failure_reason=?,"
                "row_revision=row_revision+1,updated_at=? WHERE id=?",
                (kind, reason, time.time(), followup_id))
        updated = _row(conn, machine_id, followup_id)
        extra = {"stale_observation": stale_observation}
        if failure_phase is not None:
            extra["failure_phase"] = failure_phase
        return _finish(conn, updated, request_key, payload_hash, kind, reason, extra)


def read_on_connection(conn, machine_id: str, followup_id: str) -> dict[str, Any]:
    check_schema(conn)
    row = _row(conn, machine_id, followup_id)
    if row is None:
        raise CompletionRecoveryError("completion follow-up is absent")
    return dict(row)


def assess_on_connection(conn, machine_id: str, followup_id: str) -> dict[str, Any]:
    """Use the recovery inspector's evidence test for live authorization too."""
    return _assess_row(conn, read_on_connection(conn, machine_id, followup_id))


def _assess_row(conn, row: dict[str, Any]) -> dict[str, Any]:
    machine_id = row["machine_id"]
    source = conn.execute(
        "SELECT r.state AS reservation_state,a.id AS attempt_id,"
        "e.exit_json,e.completion_json AS source_completion_json "
        "FROM smart_bin_reservations r "
        "LEFT JOIN smart_bin_release_attempts a ON a.reservation_id=r.id "
        "LEFT JOIN smart_bin_release_evidence e ON e.attempt_id=a.id "
        "WHERE r.machine_id=? AND r.id=?",
        (machine_id, row["reservation_id"])).fetchone()
    delivered = None
    if row["delivery_id"] is not None:
        delivered = conn.execute(
            "SELECT d.id,d.evidence_ref,p.uuid AS history_uuid,"
            "p.machine_id AS history_machine_id,p.run_id AS history_run_id "
            "FROM smart_bin_deliveries d "
            "LEFT JOIN piece_records p ON p.uuid=d.piece_uuid "
            "WHERE d.id=? AND d.machine_id=? AND d.reservation_id=? "
            "AND d.piece_uuid=?",
            (row["delivery_id"], machine_id, row["reservation_id"],
             row["piece_uuid"])).fetchone()
    completion = None
    if row["completion_json"]:
        try:
            completion = json.loads(row["completion_json"])
        except (TypeError, ValueError):
            completion = None
    missing = (source is None or source["attempt_id"] != row["release_attempt_id"]
               or not source["exit_json"]
               or (row["state"] != "RETAINED"
                   and (completion is None or
                        source["source_completion_json"] != row["completion_json"]))
               or (row["delivery_id"] is not None
                   and (delivered is None or delivered["history_uuid"] is None
                        or delivered["history_machine_id"] != machine_id
                        or completion is None
                        or delivered["evidence_ref"] !=
                            completion.get("completion_evidence_ref")
                        or delivered["history_run_id"] !=
                            completion.get("runtime_run_id")))
               or (row["delivery_id"] is not None
                   and source["reservation_state"] != "COMPLETED"))
    row["missing_evidence"] = bool(missing)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                    "name='smart_bin_followup_reconciliation_versions'").fetchone():
        from smart_bins_followup_reconciliation import project_on_connection
        project_on_connection(conn, row)
    return row


def inspect_on_connection(conn, machine_id: str) -> dict[str, Any]:
    """Report all obligations and blockers within the caller's read snapshot."""
    check_schema(conn)
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM smart_bin_completion_followups WHERE machine_id=? "
        "ORDER BY created_at,id", (machine_id,))]
    blockers = []
    for row in rows:
        _assess_row(conn, row)
        if "remaining_obligations" in row:
            if row["remaining_obligations"]:
                blockers.append({"followup_id": row["id"], "reservation_id": row["reservation_id"],
                                 "state": row["state"], "missing_evidence": row["missing_evidence"],
                                 "ownership_loss_reason": row["ownership_loss_reason"],
                                 "failure_kind": row["failure_kind"],
                                 "remaining_obligations": row["remaining_obligations"],
                                 "resolved_obligations": row["resolved_obligations"],
                                 "owner_requalification_required": row["owner_requalification_required"],
                                 "physical_recovery_authorized": False})
        elif (row["state"] != "CLOSED" or row["ownership_loss_reason"] is not None
              or row["failure_kind"] is not None or row["missing_evidence"]):
            blockers.append({"followup_id": row["id"], "reservation_id": row["reservation_id"],
                             "state": row["state"], "missing_evidence": row["missing_evidence"],
                             "ownership_loss_reason": row["ownership_loss_reason"],
                             "failure_kind": row["failure_kind"]})
    unlinked = [dict(row) for row in conn.execute(
        "SELECT r.id AS reservation_id,d.id AS delivery_id,r.state "
        "FROM smart_bin_reservations r "
        "LEFT JOIN smart_bin_deliveries d ON d.reservation_id=r.id "
        "LEFT JOIN smart_bin_completion_followups f ON f.reservation_id=r.id "
        "WHERE r.machine_id=? AND r.state='COMPLETED' AND f.id IS NULL",
        (machine_id,))]
    blockers.extend({"followup_id": None, **row, "missing_evidence": True}
                    for row in unlinked)
    # Reconciliation never manufactures a normal completion/publication or
    # releases restart/physical ownership. Cancellation also needs requalification.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                    "name='smart_bin_reconciliations'").fetchone():
        for resolution in conn.execute(
            "SELECT x.id,r.id AS reservation_id,r.state FROM smart_bin_reconciliations x "
            "JOIN smart_bin_reservations r ON r.id=x.reservation_id WHERE x.machine_id=?",
            (machine_id,)):
            existing = next((b for b in blockers if b["reservation_id"] == resolution["reservation_id"]), None)
            if existing is None:
                existing = {"followup_id": None, "reservation_id": resolution["reservation_id"],
                            "state": resolution["state"]}
                blockers.append(existing)
            existing.update(origin="native_reconciliation", reconciliation_id=resolution["id"],
                            owner_requalification_required=True, physical_recovery_authorized=False)
    return {"followups": rows, "followup_blockers": blockers}


def authorization_on_connection(conn, machine_id: str, followup_id: str) -> tuple[
    dict[str, Any], list[dict[str, Any]]
]:
    """Assess one current handoff and all other machine obligations in one snapshot."""
    report = inspect_on_connection(conn, machine_id)
    current = next((row for row in report["followups"] if row["id"] == followup_id), None)
    if current is None:
        raise CompletionRecoveryError("completion follow-up is absent")
    other_blockers = [row for row in report["followup_blockers"]
                      if row["followup_id"] != followup_id]
    return current, other_blockers
