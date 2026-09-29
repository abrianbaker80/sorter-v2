"""Explicit native receiving evidence and canonical completion for Smart Bins.

No native observation is promoted to receiving proof by this module. A caller
must supply a separately qualified, immutable observation. Import has no DDL
or physical side effects.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass

import smart_bins_native_custody as custody
from piece_records import recordPieceOnConnection
from smart_bins_storage import critical_transaction


SCHEMA_VERSION = 1
EVIDENCE_POLICY_VERSION = 1


class NativeCompletionRefused(RuntimeError):
    """A native claim has no qualified, matching completion fact."""


@dataclass(frozen=True)
class ReceivingEvidence:
    evidence_id: str
    machine_id: str
    reservation_id: str
    piece_uuid: str
    attempt_id: str
    incarnation: str
    head_generation: int
    route_revision: int
    actual_kind: str
    actual_slot_id: str | None
    actual_cycle_id: str | None
    evidence_type: str
    policy_version: int
    evidence_ref: str
    observed_at: float
    source_identity: str
    qualifier_identity: str
    revision: int = 1


_DDL = (
    "CREATE TABLE smart_bin_native_completion_versions("
    "singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL)",
    "CREATE TABLE smart_bin_native_receiving_evidence("
    "evidence_id TEXT PRIMARY KEY,machine_id TEXT NOT NULL,reservation_id TEXT NOT NULL,"
    "piece_uuid TEXT NOT NULL,attempt_id TEXT NOT NULL,incarnation TEXT NOT NULL,"
    "head_generation INTEGER NOT NULL,route_revision INTEGER NOT NULL,"
    "actual_kind TEXT NOT NULL,actual_slot_id TEXT,actual_cycle_id TEXT,"
    "evidence_type TEXT NOT NULL,policy_version INTEGER NOT NULL,"
    "evidence_ref TEXT NOT NULL,observed_at REAL NOT NULL,source_identity TEXT NOT NULL,"
    "qualifier_identity TEXT NOT NULL,revision INTEGER NOT NULL,payload_hash TEXT NOT NULL,"
    "payload_json TEXT NOT NULL,FOREIGN KEY(reservation_id) REFERENCES smart_bin_reservations(id))",
    "CREATE UNIQUE INDEX smart_bin_native_receiving_by_reservation "
    "ON smart_bin_native_receiving_evidence(reservation_id)",
    "CREATE TRIGGER smart_bin_native_receiving_no_update BEFORE UPDATE "
    "ON smart_bin_native_receiving_evidence BEGIN SELECT RAISE(ABORT,'receiving evidence is immutable'); END",
    "CREATE TRIGGER smart_bin_native_receiving_no_delete BEFORE DELETE "
    "ON smart_bin_native_receiving_evidence BEGIN SELECT RAISE(ABORT,'receiving evidence is immutable'); END",
    "CREATE TABLE smart_bin_native_completion_receipts("
    "reservation_id TEXT PRIMARY KEY,machine_id TEXT NOT NULL,piece_uuid TEXT NOT NULL,"
    "evidence_id TEXT NOT NULL UNIQUE,delivery_id TEXT NOT NULL UNIQUE,"
    "history_run_id TEXT NOT NULL,receipt_hash TEXT NOT NULL,created_at REAL NOT NULL,"
    "FOREIGN KEY(reservation_id) REFERENCES smart_bin_reservations(id),"
    "FOREIGN KEY(evidence_id) REFERENCES smart_bin_native_receiving_evidence(evidence_id),"
    "FOREIGN KEY(delivery_id) REFERENCES smart_bin_deliveries(id))",
    "CREATE TRIGGER smart_bin_native_receipt_no_update BEFORE UPDATE "
    "ON smart_bin_native_completion_receipts BEGIN SELECT RAISE(ABORT,'native receipt is immutable'); END",
    "CREATE TRIGGER smart_bin_native_receipt_no_delete BEFORE DELETE "
    "ON smart_bin_native_completion_receipts BEGIN SELECT RAISE(ABORT,'native receipt is immutable'); END",
    "CREATE TABLE smart_bin_native_completion_effects("
    "reservation_id TEXT NOT NULL,effect TEXT NOT NULL,phase TEXT NOT NULL "
    "CHECK(phase IN ('PENDING','ATTEMPTED','SUCCEEDED')),"
    "attempt_id TEXT,updated_at REAL NOT NULL,failure_phase TEXT,failure_reason TEXT,"
    "failure_at REAL,PRIMARY KEY(reservation_id,effect),"
    "FOREIGN KEY(reservation_id) REFERENCES smart_bin_reservations(id))",
    "CREATE INDEX smart_bin_native_effects_current "
    "ON smart_bin_native_completion_effects(reservation_id,phase)",
    "CREATE INDEX smart_bin_native_claim_by_piece "
    "ON smart_bin_native_claims(machine_id,piece_uuid,created_at DESC)",
    "CREATE INDEX smart_bin_rb04_followup_by_piece "
    "ON smart_bin_completion_followups(piece_uuid)",
    "CREATE INDEX smart_bin_native_receipt_by_piece "
    "ON smart_bin_native_completion_receipts(piece_uuid)",
    "CREATE TABLE smart_bin_current_obligations("
    "machine_id TEXT NOT NULL,reservation_id TEXT NOT NULL,kind TEXT NOT NULL,"
    "source_id TEXT NOT NULL,PRIMARY KEY(kind,source_id))",
    "CREATE INDEX smart_bin_current_obligations_machine "
    "ON smart_bin_current_obligations(machine_id,reservation_id)",
)

# All refreshes execute inside the writer's transaction. A rollback cannot
# publish a current blocker that the canonical ledger did not commit.
_FOLLOWUP_BLOCKED = (
    "NEW.state<>'CLOSED' OR NEW.ownership_loss_reason IS NOT NULL OR "
    "NEW.failure_kind IS NOT NULL"
)
_DDL += (
    "CREATE TRIGGER smart_bin_current_followup_insert AFTER INSERT "
    "ON smart_bin_completion_followups BEGIN "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "SELECT NEW.machine_id,NEW.reservation_id,'FOLLOWUP',NEW.id WHERE "
    + _FOLLOWUP_BLOCKED + "; END",
    "CREATE TRIGGER smart_bin_current_followup_update AFTER UPDATE "
    "ON smart_bin_completion_followups BEGIN "
    "DELETE FROM smart_bin_current_obligations WHERE kind='FOLLOWUP' AND source_id=NEW.id; "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "SELECT NEW.machine_id,NEW.reservation_id,'FOLLOWUP',NEW.id WHERE "
    + _FOLLOWUP_BLOCKED + "; END",
    "CREATE TRIGGER smart_bin_current_discrepancy_insert AFTER INSERT "
    "ON smart_bin_discrepancies WHEN NEW.resolved_at IS NULL AND NEW.reservation_id IS NOT NULL "
    "BEGIN INSERT OR IGNORE INTO smart_bin_current_obligations "
    "VALUES(NEW.machine_id,NEW.reservation_id,'DISCREPANCY',NEW.id); END",
    "CREATE TRIGGER smart_bin_current_discrepancy_update AFTER UPDATE OF resolved_at "
    "ON smart_bin_discrepancies BEGIN "
    "DELETE FROM smart_bin_current_obligations WHERE kind='DISCREPANCY' AND source_id=NEW.id; "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "SELECT NEW.machine_id,NEW.reservation_id,'DISCREPANCY',NEW.id "
    "WHERE NEW.resolved_at IS NULL AND NEW.reservation_id IS NOT NULL; END",
    "CREATE TRIGGER smart_bin_current_reconciliation_insert AFTER INSERT "
    "ON smart_bin_reconciliations BEGIN "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "VALUES(NEW.machine_id,NEW.reservation_id,'RECONCILIATION',NEW.id); END",
    "CREATE TRIGGER smart_bin_current_completed_update AFTER UPDATE OF state "
    "ON smart_bin_reservations BEGIN "
    "DELETE FROM smart_bin_current_obligations WHERE kind='UNLINKED_COMPLETION' "
    "AND source_id=NEW.id; "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "SELECT NEW.machine_id,NEW.id,'UNLINKED_COMPLETION',NEW.id "
    "WHERE NEW.state='COMPLETED' AND NOT EXISTS "
    "(SELECT 1 FROM smart_bin_completion_followups f WHERE f.reservation_id=NEW.id) "
    "AND NOT EXISTS (SELECT 1 FROM smart_bin_native_completion_receipts n "
    "WHERE n.reservation_id=NEW.id); END",
    "CREATE TRIGGER smart_bin_current_followup_links AFTER INSERT "
    "ON smart_bin_completion_followups BEGIN "
    "DELETE FROM smart_bin_current_obligations "
    "WHERE kind='UNLINKED_COMPLETION' AND source_id=NEW.reservation_id; END",
    "CREATE TRIGGER smart_bin_current_native_receipt_insert AFTER INSERT "
    "ON smart_bin_native_completion_receipts BEGIN "
    "DELETE FROM smart_bin_current_obligations "
    "WHERE kind='UNLINKED_COMPLETION' AND source_id=NEW.reservation_id; END",
    "CREATE TRIGGER smart_bin_current_history_delete AFTER DELETE "
    "ON piece_records BEGIN "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "SELECT f.machine_id,f.reservation_id,'MISSING_HISTORY',f.id "
    "FROM smart_bin_completion_followups f WHERE f.piece_uuid=OLD.uuid "
    "AND f.delivery_id IS NOT NULL; "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "SELECT r.machine_id,r.reservation_id,'NATIVE_MISSING_HISTORY',r.reservation_id "
    "FROM smart_bin_native_completion_receipts r WHERE r.piece_uuid=OLD.uuid; END",
    "CREATE TRIGGER smart_bin_current_history_insert AFTER INSERT "
    "ON piece_records BEGIN "
    "DELETE FROM smart_bin_current_obligations WHERE kind='MISSING_HISTORY' "
    "AND source_id IN (SELECT f.id FROM smart_bin_completion_followups f "
    "WHERE f.piece_uuid=NEW.uuid AND f.machine_id=NEW.machine_id "
    "AND NEW.run_id=json_extract(f.completion_json,'$.runtime_run_id')); "
    "DELETE FROM smart_bin_current_obligations WHERE kind='NATIVE_MISSING_HISTORY' "
    "AND source_id IN (SELECT r.reservation_id FROM smart_bin_native_completion_receipts r "
    "WHERE r.piece_uuid=NEW.uuid AND r.machine_id=NEW.machine_id "
    "AND r.history_run_id=NEW.run_id); END",
    "CREATE TRIGGER smart_bin_current_history_update AFTER UPDATE OF machine_id,run_id "
    "ON piece_records BEGIN "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "SELECT f.machine_id,f.reservation_id,'MISSING_HISTORY',f.id "
    "FROM smart_bin_completion_followups f WHERE f.piece_uuid=NEW.uuid "
    "AND (NEW.machine_id IS NOT f.machine_id OR NEW.run_id IS NOT "
    "json_extract(f.completion_json,'$.runtime_run_id')); "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "SELECT r.machine_id,r.reservation_id,'NATIVE_MISSING_HISTORY',r.reservation_id "
    "FROM smart_bin_native_completion_receipts r WHERE r.piece_uuid=NEW.uuid "
    "AND (NEW.machine_id IS NOT r.machine_id OR NEW.run_id IS NOT r.history_run_id); "
    "DELETE FROM smart_bin_current_obligations WHERE kind='MISSING_HISTORY' "
    "AND source_id IN (SELECT f.id FROM smart_bin_completion_followups f "
    "WHERE f.piece_uuid=NEW.uuid AND NEW.machine_id=f.machine_id "
    "AND NEW.run_id=json_extract(f.completion_json,'$.runtime_run_id')); "
    "DELETE FROM smart_bin_current_obligations WHERE kind='NATIVE_MISSING_HISTORY' "
    "AND source_id IN (SELECT r.reservation_id FROM smart_bin_native_completion_receipts r "
    "WHERE r.piece_uuid=NEW.uuid AND NEW.machine_id=r.machine_id "
    "AND NEW.run_id=r.history_run_id); END",
)

_VALID_FOLLOWUP_SOURCE = (
    "SELECT 1 FROM smart_bin_reservations r "
    "JOIN smart_bin_release_attempts a ON a.reservation_id=r.id "
    "JOIN smart_bin_release_evidence e ON e.attempt_id=a.id "
    "LEFT JOIN smart_bin_deliveries d ON d.id=f.delivery_id "
    "LEFT JOIN piece_records p ON p.uuid=f.piece_uuid "
    "WHERE r.id=f.reservation_id AND r.machine_id=f.machine_id "
    "AND a.id=f.release_attempt_id AND e.exit_json IS NOT NULL "
    "AND (f.state='RETAINED' OR "
    "(e.completion_json=f.completion_json AND r.state='COMPLETED' "
    "AND d.reservation_id=f.reservation_id AND d.machine_id=f.machine_id "
    "AND d.piece_uuid=f.piece_uuid "
    "AND d.evidence_ref=json_extract(f.completion_json,'$.completion_evidence_ref') "
    "AND p.machine_id=f.machine_id "
    "AND p.run_id=json_extract(f.completion_json,'$.runtime_run_id')))"
)
for _operation in ("INSERT", "UPDATE"):
    _DDL += (
        f"CREATE TRIGGER smart_bin_current_followup_source_{_operation.lower()} "
        f"AFTER {_operation} ON smart_bin_completion_followups BEGIN "
        "DELETE FROM smart_bin_current_obligations WHERE kind='FOLLOWUP_SOURCE' "
        "AND source_id=NEW.id; "
        "INSERT OR IGNORE INTO smart_bin_current_obligations "
        "SELECT f.machine_id,f.reservation_id,'FOLLOWUP_SOURCE',f.id "
        "FROM smart_bin_completion_followups f WHERE f.id=NEW.id "
        f"AND NOT EXISTS ({_VALID_FOLLOWUP_SOURCE}); END",
    )
for _operation in ("INSERT", "UPDATE", "DELETE"):
    _alias = "OLD" if _operation == "DELETE" else "NEW"
    _DDL += (
        f"CREATE TRIGGER smart_bin_current_release_source_{_operation.lower()} "
        f"AFTER {_operation} ON smart_bin_release_evidence BEGIN "
        "DELETE FROM smart_bin_current_obligations WHERE kind='FOLLOWUP_SOURCE' "
        "AND source_id IN (SELECT id FROM smart_bin_completion_followups "
        f"WHERE release_attempt_id={_alias}.attempt_id); "
        "INSERT OR IGNORE INTO smart_bin_current_obligations "
        "SELECT f.machine_id,f.reservation_id,'FOLLOWUP_SOURCE',f.id "
        "FROM smart_bin_completion_followups f "
        f"WHERE f.release_attempt_id={_alias}.attempt_id "
        f"AND NOT EXISTS ({_VALID_FOLLOWUP_SOURCE}); END",
    )
for _operation in ("INSERT", "UPDATE", "DELETE"):
    _alias = "OLD" if _operation == "DELETE" else "NEW"
    _DDL += (
        f"CREATE TRIGGER smart_bin_current_delivery_source_{_operation.lower()} "
        f"AFTER {_operation} ON smart_bin_deliveries BEGIN "
        "DELETE FROM smart_bin_current_obligations WHERE kind='FOLLOWUP_SOURCE' "
        "AND source_id IN (SELECT id FROM smart_bin_completion_followups "
        f"WHERE delivery_id={_alias}.id); "
        "INSERT OR IGNORE INTO smart_bin_current_obligations "
        "SELECT f.machine_id,f.reservation_id,'FOLLOWUP_SOURCE',f.id "
        "FROM smart_bin_completion_followups f "
        f"WHERE f.delivery_id={_alias}.id "
        f"AND NOT EXISTS ({_VALID_FOLLOWUP_SOURCE}); END",
    )
for _operation in ("INSERT", "UPDATE", "DELETE"):
    _alias = "OLD" if _operation == "DELETE" else "NEW"
    _DDL += (
        f"CREATE TRIGGER smart_bin_current_history_source_{_operation.lower()} "
        f"AFTER {_operation} ON piece_records BEGIN "
        "DELETE FROM smart_bin_current_obligations WHERE kind='FOLLOWUP_SOURCE' "
        "AND source_id IN (SELECT id FROM smart_bin_completion_followups "
        f"WHERE piece_uuid={_alias}.uuid); "
        "INSERT OR IGNORE INTO smart_bin_current_obligations "
        "SELECT f.machine_id,f.reservation_id,'FOLLOWUP_SOURCE',f.id "
        "FROM smart_bin_completion_followups f "
        f"WHERE f.piece_uuid={_alias}.uuid "
        f"AND NOT EXISTS ({_VALID_FOLLOWUP_SOURCE}); END",
    )
for _operation in ("INSERT", "UPDATE", "DELETE"):
    _alias = "OLD" if _operation == "DELETE" else "NEW"
    _DDL += (
        f"CREATE TRIGGER smart_bin_current_attempt_source_{_operation.lower()} "
        f"AFTER {_operation} ON smart_bin_release_attempts BEGIN "
        "DELETE FROM smart_bin_current_obligations WHERE kind='FOLLOWUP_SOURCE' "
        "AND source_id IN (SELECT id FROM smart_bin_completion_followups "
        f"WHERE release_attempt_id={_alias}.id); "
        "INSERT OR IGNORE INTO smart_bin_current_obligations "
        "SELECT f.machine_id,f.reservation_id,'FOLLOWUP_SOURCE',f.id "
        "FROM smart_bin_completion_followups f "
        f"WHERE f.release_attempt_id={_alias}.id "
        f"AND NOT EXISTS ({_VALID_FOLLOWUP_SOURCE}); END",
    )
_DDL += (
    "CREATE TRIGGER smart_bin_native_history_facts_immutable BEFORE UPDATE OF "
    "run_id,machine_id,bin_x,bin_y,bin_z ON piece_records WHEN EXISTS("
    "SELECT 1 FROM smart_bin_native_completion_receipts n "
    "JOIN smart_bin_deliveries d ON d.id=n.delivery_id "
    "LEFT JOIN smart_bin_slots s ON s.id=d.actual_slot_id "
    "WHERE n.piece_uuid=OLD.uuid AND (NEW.machine_id IS NOT n.machine_id "
    "OR NEW.run_id IS NOT n.history_run_id "
    "OR NEW.bin_x IS NOT s.layer_index OR NEW.bin_y IS NOT s.section_index "
    "OR NEW.bin_z IS NOT s.bin_index)) BEGIN "
    "SELECT RAISE(ABORT,'canonical native history destination is immutable'); END",
    "CREATE TRIGGER smart_bin_current_release_fence BEFORE UPDATE OF status "
    "ON smart_bin_native_claims WHEN NEW.status IN ('RELEASE_INTENT','DISPATCH_CONSUMED') "
    "AND NEW.status IS NOT OLD.status AND ("
    "EXISTS(SELECT 1 FROM smart_bin_current_obligations "
    "WHERE machine_id=NEW.machine_id AND reservation_id<NEW.reservation_id LIMIT 1) "
    "OR EXISTS(SELECT 1 FROM smart_bin_current_obligations "
    "WHERE machine_id=NEW.machine_id AND reservation_id>NEW.reservation_id LIMIT 1) "
    "OR EXISTS(SELECT 1 FROM smart_bin_native_holds WHERE machine_id=NEW.machine_id "
    "AND resolved_at IS NULL LIMIT 1) "
    "OR EXISTS(SELECT 1 FROM smart_bin_discrepancies WHERE machine_id=NEW.machine_id "
    "AND resolved_at IS NULL LIMIT 1)) BEGIN "
    "SELECT RAISE(ABORT,'current completion obligation blocks native release'); END",
    "CREATE TRIGGER smart_bin_current_native_effect_insert AFTER INSERT "
    "ON smart_bin_native_completion_effects WHEN NEW.phase<>'SUCCEEDED' BEGIN "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "SELECT r.machine_id,NEW.reservation_id,'NATIVE_EFFECT',"
    "NEW.reservation_id||':'||NEW.effect FROM smart_bin_reservations r "
    "WHERE r.id=NEW.reservation_id; END",
    "CREATE TRIGGER smart_bin_current_native_effect_update AFTER UPDATE OF phase "
    "ON smart_bin_native_completion_effects BEGIN "
    "DELETE FROM smart_bin_current_obligations WHERE kind='NATIVE_EFFECT' "
    "AND source_id=NEW.reservation_id||':'||NEW.effect; "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "SELECT r.machine_id,NEW.reservation_id,'NATIVE_EFFECT',"
    "NEW.reservation_id||':'||NEW.effect FROM smart_bin_reservations r "
    "WHERE r.id=NEW.reservation_id AND NEW.phase<>'SUCCEEDED'; END",
    "CREATE TRIGGER smart_bin_native_effect_no_delete BEFORE DELETE "
    "ON smart_bin_native_completion_effects BEGIN "
    "SELECT RAISE(ABORT,'native publication obligation is retained'); END",
    "CREATE TRIGGER smart_bin_native_effect_forward BEFORE UPDATE "
    "ON smart_bin_native_completion_effects WHEN "
    "NEW.reservation_id IS NOT OLD.reservation_id OR NEW.effect IS NOT OLD.effect "
    "OR (OLD.phase='SUCCEEDED' AND NEW.phase<>'SUCCEEDED') "
    "OR (OLD.phase='ATTEMPTED' AND NEW.phase='PENDING') "
    "BEGIN SELECT RAISE(ABORT,'native publication phase cannot rewind'); END",
    "CREATE TRIGGER smart_bin_current_reservation_source AFTER UPDATE OF state "
    "ON smart_bin_reservations BEGIN "
    "DELETE FROM smart_bin_current_obligations WHERE kind='FOLLOWUP_SOURCE' "
    "AND source_id IN (SELECT id FROM smart_bin_completion_followups "
    "WHERE reservation_id=NEW.id); "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "SELECT f.machine_id,f.reservation_id,'FOLLOWUP_SOURCE',f.id "
    "FROM smart_bin_completion_followups f WHERE f.reservation_id=NEW.id "
    f"AND NOT EXISTS ({_VALID_FOLLOWUP_SOURCE}); END",
    "CREATE TRIGGER smart_bin_current_discrepancy_delete AFTER DELETE "
    "ON smart_bin_discrepancies BEGIN DELETE FROM smart_bin_current_obligations "
    "WHERE kind='DISCREPANCY' AND source_id=OLD.id; END",
    "CREATE TRIGGER smart_bin_current_reconciliation_delete AFTER DELETE "
    "ON smart_bin_reconciliations BEGIN DELETE FROM smart_bin_current_obligations "
    "WHERE kind='RECONCILIATION' AND source_id=OLD.id; END",
    "CREATE TRIGGER smart_bin_current_followup_delete AFTER DELETE "
    "ON smart_bin_completion_followups BEGIN "
    "DELETE FROM smart_bin_current_obligations WHERE source_id=OLD.id "
    "AND kind IN ('FOLLOWUP','FOLLOWUP_SOURCE','MISSING_HISTORY'); "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "SELECT r.machine_id,r.id,'UNLINKED_COMPLETION',r.id "
    "FROM smart_bin_reservations r WHERE r.id=OLD.reservation_id "
    "AND r.state='COMPLETED' AND NOT EXISTS "
    "(SELECT 1 FROM smart_bin_native_completion_receipts n "
    "WHERE n.reservation_id=r.id); END",
)

_VALID_CLOSE = (
    "SELECT 1 FROM smart_bin_completion_receipts c "
    "WHERE c.request_key=f.completion_request_key||':close' "
    "AND c.machine_id=f.machine_id AND c.action='CLOSE' "
    "AND CASE WHEN json_valid(c.result_json) THEN "
    "json_extract(c.result_json,'$.code')='OK' "
    "AND json_extract(c.result_json,'$.state')='CLOSED' "
    "AND json_extract(c.result_json,'$.followup_id')=f.id ELSE 0 END"
)
for _operation in ("INSERT", "UPDATE"):
    _DDL += (
        f"CREATE TRIGGER smart_bin_current_close_followup_{_operation.lower()} "
        f"AFTER {_operation} ON smart_bin_completion_followups BEGIN "
        "DELETE FROM smart_bin_current_obligations WHERE kind='CLOSURE_PENDING' "
        "AND source_id=NEW.id; "
        "INSERT OR IGNORE INTO smart_bin_current_obligations "
        "SELECT f.machine_id,f.reservation_id,'CLOSURE_PENDING',f.id "
        "FROM smart_bin_completion_followups f WHERE f.id=NEW.id "
        f"AND f.state='CLOSED' AND NOT EXISTS ({_VALID_CLOSE}); END",
    )
for _operation in ("INSERT", "UPDATE", "DELETE"):
    _alias = "OLD" if _operation == "DELETE" else "NEW"
    _DDL += (
        f"CREATE TRIGGER smart_bin_current_close_receipt_{_operation.lower()} "
        f"AFTER {_operation} ON smart_bin_completion_receipts BEGIN "
        "DELETE FROM smart_bin_current_obligations WHERE kind='CLOSURE_PENDING' "
        "AND source_id IN (SELECT id FROM smart_bin_completion_followups "
        f"WHERE completion_request_key=substr({_alias}.request_key,1,"
        f"length({_alias}.request_key)-6)); "
        "INSERT OR IGNORE INTO smart_bin_current_obligations "
        "SELECT f.machine_id,f.reservation_id,'CLOSURE_PENDING',f.id "
        "FROM smart_bin_completion_followups f "
        f"WHERE f.completion_request_key=substr({_alias}.request_key,1,"
        f"length({_alias}.request_key)-6) "
        f"AND f.state='CLOSED' AND NOT EXISTS ({_VALID_CLOSE}); END",
    )
_DDL += (
    "CREATE TRIGGER smart_bin_current_followup_reconciliation_insert "
    "AFTER INSERT ON smart_bin_followup_reconciliation_dispositions BEGIN "
    "INSERT OR IGNORE INTO smart_bin_current_obligations "
    "VALUES(NEW.machine_id,NEW.reservation_id,'OWNER_REQUALIFICATION',NEW.id); END",
)

EFFECTS = ("EVENT_ENQUEUED", "RUN_RECORDER_ADOPTED", "PROGRESS_RECORDED",
           "PROGRESS_SYNC_NOTIFIED", "CLOSE", "GATE_OPEN", "ADMISSION_RELEASE")


def _objects(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    return {(row[0], row[1]) for row in conn.execute(
        "SELECT type,name FROM sqlite_master")}


def check_schema(conn: sqlite3.Connection) -> None:
    custody._check(conn)
    try:
        rows = conn.execute(
            "SELECT singleton,version FROM smart_bin_native_completion_versions"
        ).fetchall()
        required = set()
        for sql in _DDL:
            tokens = sql.split()
            kind = "index" if "INDEX" in tokens[:3] else tokens[1].lower()
            name = tokens[tokens.index("INDEX") + 1] if kind == "index" else tokens[2]
            required.add((kind, name.split("(")[0]))
        if [tuple(row) for row in rows] != [(1, SCHEMA_VERSION)] or not required <= _objects(conn):
            raise NativeCompletionRefused("Native completion extension is incomplete")
    except sqlite3.Error as exc:
        raise NativeCompletionRefused("Native completion extension is unavailable") from exc


def initialize_schema() -> int:
    """Explicit versioned initialization; never called by ordinary startup."""
    import piece_records
    import smart_bins_completion_recovery as recovery
    import smart_bins_followup_reconciliation as followup_reconciliation
    import smart_bins_reconciliation as reconciliation

    # These are local, explicit prerequisites. None imports or activates Harvest.
    piece_records.initialize_piece_records()
    recovery.initialize_schema()
    reconciliation.initialize_schema()
    followup_reconciliation.initialize_schema()
    with critical_transaction() as conn:
        custody._check(conn)
        objects = _objects(conn)
        if ("table", "smart_bin_native_completion_versions") not in objects:
            for statement in _DDL:
                conn.execute(statement)
            conn.execute("INSERT INTO smart_bin_native_completion_versions VALUES(1,?)",
                         (SCHEMA_VERSION,))
            conn.execute(
                "INSERT OR IGNORE INTO smart_bin_current_obligations "
                "SELECT machine_id,reservation_id,'FOLLOWUP',id "
                "FROM smart_bin_completion_followups "
                "WHERE state<>'CLOSED' OR ownership_loss_reason IS NOT NULL "
                "OR failure_kind IS NOT NULL")
            conn.execute(
                "INSERT OR IGNORE INTO smart_bin_current_obligations "
                "SELECT f.machine_id,f.reservation_id,'FOLLOWUP_SOURCE',f.id "
                "FROM smart_bin_completion_followups f "
                f"WHERE NOT EXISTS ({_VALID_FOLLOWUP_SOURCE})")
            conn.execute(
                "INSERT OR IGNORE INTO smart_bin_current_obligations "
                "SELECT f.machine_id,f.reservation_id,'MISSING_HISTORY',f.id "
                "FROM smart_bin_completion_followups f "
                "JOIN smart_bin_deliveries d ON d.id=f.delivery_id "
                "LEFT JOIN piece_records p ON p.uuid=f.piece_uuid "
                "AND p.machine_id=f.machine_id "
                "AND p.run_id=json_extract(f.completion_json,'$.runtime_run_id') "
                "WHERE f.delivery_id IS NOT NULL AND p.uuid IS NULL")
            conn.execute(
                "INSERT OR IGNORE INTO smart_bin_current_obligations "
                "SELECT machine_id,reservation_id,'DISCREPANCY',id "
                "FROM smart_bin_discrepancies WHERE resolved_at IS NULL "
                "AND reservation_id IS NOT NULL")
            conn.execute(
                "INSERT OR IGNORE INTO smart_bin_current_obligations "
                "SELECT machine_id,reservation_id,'RECONCILIATION',id "
                "FROM smart_bin_reconciliations")
            conn.execute(
                "INSERT OR IGNORE INTO smart_bin_current_obligations "
                "SELECT r.machine_id,r.id,'UNLINKED_COMPLETION',r.id "
                "FROM smart_bin_reservations r WHERE r.state='COMPLETED' "
                "AND NOT EXISTS (SELECT 1 FROM smart_bin_completion_followups f "
                "WHERE f.reservation_id=r.id) AND NOT EXISTS "
                "(SELECT 1 FROM smart_bin_native_completion_receipts n "
                "WHERE n.reservation_id=r.id)")
            conn.execute(
                "INSERT OR IGNORE INTO smart_bin_current_obligations "
                "SELECT f.machine_id,f.reservation_id,'CLOSURE_PENDING',f.id "
                "FROM smart_bin_completion_followups f WHERE f.state='CLOSED' "
                f"AND NOT EXISTS ({_VALID_CLOSE})")
            conn.execute(
                "INSERT OR IGNORE INTO smart_bin_current_obligations "
                "SELECT machine_id,reservation_id,'OWNER_REQUALIFICATION',id "
                "FROM smart_bin_followup_reconciliation_dispositions")
        check_schema(conn)
        conn.commit()
    return SCHEMA_VERSION


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _claim(conn: sqlite3.Connection, machine_id: str, reservation_id: str):
    return conn.execute(
        "SELECT n.*,r.state AS reservation_state,r.row_revision,r.run_id,"
        "r.policy_revision_id,r.group_key_id,r.quantity,r.intended_kind,"
        "r.intended_slot_id,r.intended_cycle_id,r.routing_revision "
        "FROM smart_bin_native_claims n JOIN smart_bin_reservations r "
        "ON r.id=n.reservation_id AND r.machine_id=n.machine_id "
        "WHERE n.machine_id=? AND n.reservation_id=?",
        (machine_id, reservation_id)).fetchone()


def _validate_evidence(conn: sqlite3.Connection, evidence: ReceivingEvidence) -> None:
    if (evidence.policy_version != EVIDENCE_POLICY_VERSION or evidence.revision != 1
        or evidence.evidence_type != "PHYSICALLY_QUALIFIED_RECEIVER"
        or not all((evidence.evidence_id, evidence.machine_id, evidence.reservation_id,
                    evidence.piece_uuid, evidence.attempt_id, evidence.incarnation,
                    evidence.evidence_ref, evidence.source_identity,
                    evidence.qualifier_identity))
        or type(evidence.head_generation) is not int
        or type(evidence.route_revision) is not int
        or type(evidence.observed_at) not in (float, int)
        or evidence.observed_at <= 0 or evidence.observed_at > time.time()
        or (evidence.actual_kind == "BIN" and
            (not evidence.actual_slot_id or not evidence.actual_cycle_id))
        or (evidence.actual_kind == "REJECT" and
            (evidence.actual_slot_id is not None or evidence.actual_cycle_id is not None))
        or evidence.actual_kind not in ("BIN", "REJECT")):
        raise NativeCompletionRefused("Receiving evidence contract is invalid")
    row = _claim(conn, evidence.machine_id, evidence.reservation_id)
    if (row is None or row["piece_uuid"] != evidence.piece_uuid
        or row["attempt_id"] != evidence.attempt_id
        or row["incarnation"] != evidence.incarnation
        or row["head_generation"] != evidence.head_generation
        or row["route_revision"] != evidence.route_revision
        or row["routing_revision"] != evidence.route_revision):
        raise NativeCompletionRefused("Receiving evidence does not bind to the native claim")
    attempt = conn.execute(
        "SELECT created_at FROM smart_bin_release_attempts "
        "WHERE id=? AND reservation_id=? AND owner_incarnation=?",
        (evidence.attempt_id, evidence.reservation_id, evidence.incarnation)).fetchone()
    if attempt is None or evidence.observed_at < attempt[0]:
        raise NativeCompletionRefused("Receiving evidence predates or misses release")
    accepted = conn.execute(
        "SELECT observed_at FROM smart_bin_native_evidence "
        "WHERE reservation_id=? AND attempt_id=? AND kind='MOTOR_ACCEPTED' "
        "ORDER BY observed_at DESC LIMIT 1",
        (evidence.reservation_id, evidence.attempt_id)).fetchone()
    if (row["status"] not in ("ACCEPTED",)
        or accepted is None or evidence.observed_at < accepted[0]):
        raise NativeCompletionRefused(
            "Receiving observation must follow the accepted release receipt"
        )
    if evidence.actual_kind == "BIN":
        destination = conn.execute(
            "SELECT s.id,c.opened_at,c.closed_at FROM smart_bin_slots s "
            "JOIN smart_bin_cycles c ON c.slot_id=s.id AND c.machine_id=s.machine_id "
            "WHERE s.machine_id=? AND s.id=? AND c.id=?",
            (evidence.machine_id, evidence.actual_slot_id,
             evidence.actual_cycle_id)).fetchone()
        if (destination is None or evidence.observed_at < destination["opened_at"]
            or (destination["closed_at"] is not None and
                evidence.observed_at > destination["closed_at"])):
            raise NativeCompletionRefused("Actual receiver cycle is not qualified at observation")


def record_receiving_evidence(evidence: ReceivingEvidence) -> str:
    """Narrow programmatic seam. No runtime source supplies this by default."""
    payload = asdict(evidence)
    digest = _hash(payload)
    with critical_transaction() as conn:
        check_schema(conn)
        prior = conn.execute(
            "SELECT payload_hash FROM smart_bin_native_receiving_evidence WHERE evidence_id=?",
            (evidence.evidence_id,)).fetchone()
        if prior is not None:
            if prior[0] != digest:
                raise NativeCompletionRefused("Receiving evidence idempotency conflict")
            conn.commit()
            return evidence.evidence_id
        if conn.execute(
            "SELECT 1 FROM smart_bin_native_receiving_evidence WHERE reservation_id=?",
            (evidence.reservation_id,)).fetchone():
            raise NativeCompletionRefused(
                "Reservation already has conflicting receiving evidence"
            )
        _validate_evidence(conn, evidence)
        conn.execute(
            "INSERT INTO smart_bin_native_receiving_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (evidence.evidence_id, evidence.machine_id, evidence.reservation_id,
             evidence.piece_uuid, evidence.attempt_id, evidence.incarnation,
             evidence.head_generation, evidence.route_revision, evidence.actual_kind,
             evidence.actual_slot_id, evidence.actual_cycle_id, evidence.evidence_type,
             evidence.policy_version, evidence.evidence_ref, evidence.observed_at,
             evidence.source_identity, evidence.qualifier_identity, evidence.revision,
             digest, json.dumps(payload, sort_keys=True, allow_nan=False)))
        conn.commit()
    return evidence.evidence_id


def _receipt_on_connection(conn: sqlite3.Connection, machine_id: str,
                           reservation_id: str, piece_uuid: str,
                           delivery_id: str | None = None,
                           history_run_id: str | None = None) -> str | None:
    claim = _claim(conn, machine_id, reservation_id)
    if claim is None or claim["piece_uuid"] != piece_uuid:
        raise NativeCompletionRefused("Native machine, reservation or piece mismatch")
    receipt = conn.execute(
        "SELECT * FROM smart_bin_native_completion_receipts "
        "WHERE machine_id=? AND reservation_id=? AND piece_uuid=?",
        (machine_id, reservation_id, piece_uuid)).fetchone()
    if receipt is None:
        if delivery_id is not None:
            raise NativeCompletionRefused("Canonical completion receipt is missing")
        return None
    if delivery_id is not None and receipt["delivery_id"] != delivery_id:
        raise NativeCompletionRefused("Foreign native delivery receipt")
    if history_run_id is not None and receipt["history_run_id"] != history_run_id:
        raise NativeCompletionRefused("Foreign native run receipt")
    evidence = conn.execute(
        "SELECT * FROM smart_bin_native_receiving_evidence WHERE evidence_id=?",
        (receipt["evidence_id"],)).fetchone()
    delivery = conn.execute(
        "SELECT * FROM smart_bin_deliveries WHERE id=? AND reservation_id=?",
        (receipt["delivery_id"], reservation_id)).fetchone()
    history = conn.execute(
        "SELECT uuid,machine_id,run_id,bin_x,bin_y,bin_z "
        "FROM piece_records WHERE uuid=?",
        (piece_uuid,)).fetchone()
    slot = None
    if delivery is not None and delivery["actual_kind"] == "BIN":
        slot = conn.execute(
            "SELECT layer_index,section_index,bin_index FROM smart_bin_slots "
            "WHERE machine_id=? AND id=?",
            (machine_id, delivery["actual_slot_id"])).fetchone()
    if (evidence is None or delivery is None or history is None
        or claim["reservation_state"] != "COMPLETED"
        or evidence["machine_id"] != machine_id
        or evidence["reservation_id"] != reservation_id
        or evidence["piece_uuid"] != piece_uuid
        or evidence["attempt_id"] != claim["attempt_id"]
        or evidence["incarnation"] != claim["incarnation"]
        or evidence["head_generation"] != claim["head_generation"]
        or evidence["route_revision"] != claim["route_revision"]
        or (delivery["actual_kind"] == "BIN" and slot is None)
        or delivery["machine_id"] != machine_id
        or delivery["piece_uuid"] != piece_uuid
        or delivery["evidence_ref"] != evidence["evidence_ref"]
        or (delivery["actual_kind"], delivery["actual_slot_id"],
            delivery["actual_cycle_id"]) !=
           (evidence["actual_kind"], evidence["actual_slot_id"],
            evidence["actual_cycle_id"])
        or (history["machine_id"], history["run_id"]) !=
           (machine_id, receipt["history_run_id"])
        or (history["bin_x"], history["bin_y"], history["bin_z"]) !=
           (tuple(slot) if slot is not None else (None, None, None))):
        raise NativeCompletionRefused("Canonical delivery/history/evidence is inconsistent")
    try:
        payload = json.loads(evidence["payload_json"])
    except (ValueError, TypeError) as exc:
        raise NativeCompletionRefused("Receiving payload is malformed") from exc
    if (not isinstance(payload, dict)
        or evidence["payload_hash"] != _hash(payload)
        or any(payload.get(key) != evidence[key] for key in payload)):
        raise NativeCompletionRefused("Receiving payload hash or fields mismatch")
    receipt_facts = (machine_id, reservation_id, piece_uuid, receipt["evidence_id"],
                     receipt["delivery_id"], receipt["history_run_id"])
    if receipt["receipt_hash"] != _hash(receipt_facts):
        raise NativeCompletionRefused("Canonical receipt hash is inconsistent")
    return receipt["delivery_id"]


def verify_native_identity(machine_id: str, reservation_id: str, piece_uuid: str,
                           delivery_id: str | None = None,
                           history_run_id: str | None = None) -> str | None:
    """Verify ledger facts independently of event and KnownObject fields."""
    with custody.read_only() as conn:
        check_schema(conn)
        return _receipt_on_connection(conn, machine_id, reservation_id,
                                      piece_uuid, delivery_id, history_run_id)


def lookup_native_claim(machine_id: str, piece_uuid: str,
                        reservation_id: str | None = None) -> dict | None:
    """Identify a native piece without trusting an event's provenance fields."""
    with custody.read_only() as conn:
        check_schema(conn)
        if reservation_id is not None:
            row = conn.execute(
                "SELECT reservation_id FROM smart_bin_native_claims "
                "WHERE machine_id=? AND piece_uuid=? AND reservation_id=?",
                (machine_id, piece_uuid, reservation_id)).fetchone()
        else:
            row = conn.execute(
                "SELECT reservation_id FROM smart_bin_native_claims "
                "WHERE machine_id=? AND piece_uuid=? ORDER BY created_at DESC LIMIT 1",
                (machine_id, piece_uuid)).fetchone()
        if row is None:
            return None
        return {"reservation_id": row[0],
                "delivery_id": _receipt_on_connection(conn, machine_id, row[0],
                                                      piece_uuid)}


def delivery_details(machine_id: str, reservation_id: str,
                     piece_uuid: str, delivery_id: str) -> dict:
    with custody.read_only() as conn:
        check_schema(conn)
        _receipt_on_connection(conn, machine_id, reservation_id, piece_uuid, delivery_id)
        row = conn.execute(
            "SELECT d.actual_kind,d.actual_slot_id,e.observed_at,"
            "s.layer_index,s.section_index,s.bin_index "
            "FROM smart_bin_deliveries d "
            "JOIN smart_bin_native_completion_receipts r ON r.delivery_id=d.id "
            "JOIN smart_bin_native_receiving_evidence e ON e.evidence_id=r.evidence_id "
            "LEFT JOIN smart_bin_slots s ON s.id=d.actual_slot_id "
            "WHERE d.id=?", (delivery_id,)).fetchone()
        return {"distributed_at": row["observed_at"],
                "destination_bin": (row["layer_index"], row["section_index"],
                                    row["bin_index"])
                if row["actual_kind"] == "BIN" else None}


def _receiving_for_reservation(conn: sqlite3.Connection, reservation_id: str):
    row = conn.execute(
        "SELECT payload_json FROM smart_bin_native_receiving_evidence "
        "WHERE reservation_id=?", (reservation_id,)).fetchone()
    return ReceivingEvidence(**json.loads(row[0])) if row else None


def complete_native(piece, machine_id: str, reservation_id: str,
                    runtime_run_id: str) -> str | None:
    """Commit evidenced delivery, actual credit, history and receipt together.

    A missing receiving observation is a normal held outcome. This function has
    no callback, gate, transport or motion side effect.
    """
    from run_recorder import _serializePiece

    if not all((machine_id, reservation_id, runtime_run_id)):
        raise ValueError("Native completion identity and runtime run are required")
    with critical_transaction() as conn:
        check_schema(conn)
        existing = _receipt_on_connection(conn, machine_id, reservation_id,
                                          piece.uuid, history_run_id=runtime_run_id)
        if existing is not None:
            conn.commit()
            return existing
        row = _claim(conn, machine_id, reservation_id)
        if row is None or row["piece_uuid"] != piece.uuid:
            raise NativeCompletionRefused("Piece does not own native reservation")
        evidence = _receiving_for_reservation(conn, reservation_id)
        if evidence is None:
            conn.commit()
            return None
        _validate_evidence(conn, evidence)
        if (row["status"] != "ACCEPTED" or row["reservation_state"] != "RELEASE_INTENT"
            or current_blocker_on_connection(conn, machine_id,
                                             except_reservation=reservation_id)
            or conn.execute("SELECT 1 FROM smart_bin_native_holds WHERE machine_id=? "
                            "AND resolved_at IS NULL LIMIT 1", (machine_id,)).fetchone()
            or conn.execute("SELECT 1 FROM smart_bin_discrepancies WHERE machine_id=? "
                            "AND resolved_at IS NULL LIMIT 1", (machine_id,)).fetchone()
            or (("table", "smart_bin_reconciliations") in _objects(conn)
                and conn.execute("SELECT 1 FROM smart_bin_reconciliations WHERE machine_id=? "
                                 "AND reservation_id=? LIMIT 1",
                                 (machine_id, reservation_id)).fetchone())
            or not conn.execute("SELECT 1 FROM smart_bin_native_evidence "
                                "WHERE reservation_id=? AND attempt_id=? "
                                "AND kind='MOTOR_ACCEPTED' LIMIT 1",
                                (reservation_id, evidence.attempt_id)).fetchone()):
            raise NativeCompletionRefused("Current release or custody contradicts completion")
        actual_bin = None
        if evidence.actual_kind == "BIN":
            actual_bin = list(conn.execute(
                "SELECT layer_index,section_index,bin_index FROM smart_bin_slots "
                "WHERE machine_id=? AND id=?",
                (machine_id, evidence.actual_slot_id)).fetchone())
        delivery_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO smart_bin_deliveries "
            "(id,reservation_id,machine_id,piece_uuid,run_id,policy_revision_id,"
            "group_key_id,quantity,intended_kind,intended_slot_id,intended_cycle_id,"
            "actual_kind,actual_slot_id,actual_cycle_id,evidence_ref,delivered_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (delivery_id, reservation_id, machine_id, piece.uuid, row["run_id"],
             row["policy_revision_id"], row["group_key_id"], row["quantity"],
             row["intended_kind"], row["intended_slot_id"], row["intended_cycle_id"],
             evidence.actual_kind, evidence.actual_slot_id, evidence.actual_cycle_id,
             evidence.evidence_ref, evidence.observed_at))
        history = _serializePiece(piece)
        history["destination_bin"] = actual_bin
        history["distributed_at"] = evidence.observed_at
        recordPieceOnConnection(conn, history, run_id=runtime_run_id,
                                machine_id=machine_id)
        changed = conn.execute(
            "UPDATE smart_bin_reservations SET state='COMPLETED',"
            "row_revision=row_revision+1,updated_at=? "
            "WHERE machine_id=? AND id=? AND state='RELEASE_INTENT' AND row_revision=?",
            (time.time(), machine_id, reservation_id, row["row_revision"]))
        if changed.rowcount != 1:
            raise NativeCompletionRefused("Reservation changed during completion")
        if (evidence.actual_kind, evidence.actual_slot_id, evidence.actual_cycle_id) != (
            row["intended_kind"], row["intended_slot_id"], row["intended_cycle_id"]):
            conn.execute(
                "INSERT INTO smart_bin_discrepancies "
                "(id,machine_id,reservation_id,cycle_id,kind,status,evidence_ref,"
                "details_json,created_at) VALUES(?,?,?,?,?,'open',?,?,?)",
                (str(uuid.uuid4()), machine_id, reservation_id, evidence.actual_cycle_id,
                 "NATIVE_DESTINATION_MISMATCH", evidence.evidence_ref,
                 json.dumps(asdict(evidence), sort_keys=True, allow_nan=False), time.time()))
        for effect in EFFECTS:
            conn.execute(
                "INSERT INTO smart_bin_native_completion_effects "
                "(reservation_id,effect,phase,attempt_id,updated_at) "
                "VALUES(?,?,'PENDING',NULL,?)",
                (reservation_id, effect, time.time()))
        receipt_facts = (machine_id, reservation_id, piece.uuid, evidence.evidence_id,
                         delivery_id, runtime_run_id)
        conn.execute(
            "INSERT INTO smart_bin_native_completion_receipts VALUES(?,?,?,?,?,?,?,?)",
            (reservation_id, machine_id, piece.uuid, evidence.evidence_id, delivery_id,
             runtime_run_id, _hash(receipt_facts), time.time()))
        conn.execute("UPDATE smart_bin_machines SET state_revision=state_revision+1 "
                     "WHERE machine_id=?", (machine_id,))
        conn.commit()
    return verify_native_identity(machine_id, reservation_id, piece.uuid, delivery_id)


def effect_phase(machine_id: str, reservation_id: str, piece_uuid: str,
                 delivery_id: str, effect: str) -> str:
    if effect not in EFFECTS:
        raise ValueError("Unknown native publication effect")
    with custody.read_only() as conn:
        check_schema(conn)
        _receipt_on_connection(conn, machine_id, reservation_id, piece_uuid, delivery_id)
        row = conn.execute("SELECT phase FROM smart_bin_native_completion_effects "
                           "WHERE reservation_id=? AND effect=?",
                           (reservation_id, effect)).fetchone()
        if row is None:
            raise NativeCompletionRefused("Publication obligation is missing")
        return row[0]


def begin_effect(machine_id: str, reservation_id: str, piece_uuid: str,
                 delivery_id: str, effect: str) -> str | None:
    """Persist an attempt barrier before a nontransactional callback."""
    if effect not in EFFECTS or effect == "CLOSE":
        raise ValueError("Unknown or transactional native publication effect")
    with critical_transaction() as conn:
        check_schema(conn)
        _receipt_on_connection(conn, machine_id, reservation_id, piece_uuid, delivery_id)
        index = EFFECTS.index(effect)
        for predecessor in EFFECTS[:index]:
            phase = conn.execute(
                "SELECT phase FROM smart_bin_native_completion_effects "
                "WHERE reservation_id=? AND effect=?",
                (reservation_id, predecessor)).fetchone()
            if phase is None or phase[0] != "SUCCEEDED":
                raise NativeCompletionRefused("Earlier publication effect is unresolved")
        current = conn.execute(
            "SELECT phase FROM smart_bin_native_completion_effects "
            "WHERE reservation_id=? AND effect=?", (reservation_id, effect)).fetchone()
        if current is None or current[0] != "PENDING":
            conn.commit()
            return None
        if effect in ("GATE_OPEN", "ADMISSION_RELEASE") and current_blocker_on_connection(
            conn, machine_id, except_reservation=reservation_id):
            raise NativeCompletionRefused("Current same-machine blocker prevents gate effect")
        attempt_id = str(uuid.uuid4())
        conn.execute(
            "UPDATE smart_bin_native_completion_effects SET phase='ATTEMPTED',"
            "attempt_id=?,updated_at=? WHERE reservation_id=? AND effect=?",
            (attempt_id, time.time(), reservation_id, effect))
        conn.commit()
        return attempt_id


def finish_effect(machine_id: str, reservation_id: str, piece_uuid: str,
                  delivery_id: str, effect: str, attempt_id: str) -> None:
    with critical_transaction() as conn:
        check_schema(conn)
        _receipt_on_connection(conn, machine_id, reservation_id, piece_uuid, delivery_id)
        changed = conn.execute(
            "UPDATE smart_bin_native_completion_effects SET phase='SUCCEEDED',"
            "updated_at=? WHERE reservation_id=? AND effect=? "
            "AND phase='ATTEMPTED' AND attempt_id=? AND failure_phase IS NULL",
            (time.time(), reservation_id, effect, attempt_id))
        if changed.rowcount != 1:
            raise NativeCompletionRefused("Publication attempt receipt is stale")
        conn.commit()


def record_effect_failure(machine_id: str, reservation_id: str, piece_uuid: str,
                          delivery_id: str, effect: str, attempt_id: str,
                          failure_phase: str, reason: str) -> None:
    if failure_phase not in ("PUBLICATION_CALLBACKS", "GATE_OPEN",
                             "ADMISSION_RELEASE"):
        raise ValueError("Unknown native effect failure phase")
    with critical_transaction() as conn:
        check_schema(conn)
        _receipt_on_connection(conn, machine_id, reservation_id, piece_uuid, delivery_id)
        changed = conn.execute(
            "UPDATE smart_bin_native_completion_effects SET failure_phase=?,"
            "failure_reason=?,failure_at=?,updated_at=? "
            "WHERE reservation_id=? AND effect=? AND phase='ATTEMPTED' "
            "AND attempt_id=? AND failure_phase IS NULL",
            (failure_phase, reason, time.time(), time.time(),
             reservation_id, effect, attempt_id))
        if changed.rowcount != 1:
            raise NativeCompletionRefused("Native failure attribution is stale")
        conn.commit()


def close_publication(machine_id: str, reservation_id: str,
                      piece_uuid: str, delivery_id: str) -> None:
    """CLOSE persists only after every producer effect has a success receipt."""
    with critical_transaction() as conn:
        check_schema(conn)
        _receipt_on_connection(conn, machine_id, reservation_id, piece_uuid, delivery_id)
        for effect in EFFECTS[:EFFECTS.index("CLOSE")]:
            row = conn.execute(
                "SELECT phase FROM smart_bin_native_completion_effects "
                "WHERE reservation_id=? AND effect=?",
                (reservation_id, effect)).fetchone()
            if row is None or row[0] != "SUCCEEDED":
                raise NativeCompletionRefused("Producer publication is not complete")
        conn.execute(
            "UPDATE smart_bin_native_completion_effects SET phase='SUCCEEDED',"
            "updated_at=? WHERE reservation_id=? AND effect='CLOSE' AND phase='PENDING'",
            (time.time(), reservation_id))
        conn.commit()


def current_blocker_on_connection(conn: sqlite3.Connection, machine_id: str,
                                  except_reservation: str | None = None) -> bool:
    """Indexed current facts only; work does not grow with closed history."""
    check_schema(conn)
    if except_reservation is None:
        projected = conn.execute(
            "SELECT 1 FROM smart_bin_current_obligations WHERE machine_id=? LIMIT 1",
            (machine_id,)).fetchone()
    else:
        projected = (conn.execute(
            "SELECT 1 FROM smart_bin_current_obligations WHERE machine_id=? "
            "AND reservation_id<? LIMIT 1",
            (machine_id, except_reservation)).fetchone()
            or conn.execute(
                "SELECT 1 FROM smart_bin_current_obligations WHERE machine_id=? "
                "AND reservation_id>? LIMIT 1",
                (machine_id, except_reservation)).fetchone())
    if projected:
        return True
    if conn.execute(
        "SELECT 1 FROM smart_bin_reservations WHERE machine_id=? AND "
        "state IN ('RESERVED','RELEASE_INTENT','EXIT_CONFIRMED','UNCERTAIN') "
        "AND id IS NOT ? LIMIT 1", (machine_id, except_reservation)).fetchone():
        return True
    if conn.execute(
        "SELECT 1 FROM smart_bin_native_holds WHERE machine_id=? "
        "AND resolved_at IS NULL LIMIT 1", (machine_id,)).fetchone():
        return True
    if conn.execute(
        "SELECT 1 FROM smart_bin_discrepancies WHERE machine_id=? "
        "AND resolved_at IS NULL LIMIT 1", (machine_id,)).fetchone():
        return True
    return False


def current_blocker(machine_id: str,
                    except_reservation: str | None = None) -> bool:
    with custody.read_only() as conn:
        return current_blocker_on_connection(conn, machine_id, except_reservation)
