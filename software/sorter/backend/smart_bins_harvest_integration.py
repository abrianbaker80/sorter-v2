"""Inactive cross-store journal. This module never calls Harvest or a physical owner.

Lock order: lifecycle/physical owner outside -> integration_lock -> local SQLite.
Commit/close local SQLite before making a Harvest call. A remote timeout is never
permission to retry allocation, cancel custody, or deliver again. The read-only
projection is an admission obligation for future wiring, not a runtime gate.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from harvest_integration_storage import canonical, digest
from smart_bins_service import _check_service_schema
from smart_bins_storage import critical_transaction


SCHEMA_VERSION = 1
integration_lock = threading.RLock()
KINDS = {
    "ALLOCATE": ("LOCAL_INTENT", "REMOTE_CONFIRMED"),
    "CANCEL": ("CANCEL_INTENT", "CANCEL_CONFIRMED"),
    "CONFIRM": ("DELIVERY_CONFIRM_PENDING", "DELIVERY_CONFIRM_REMOTE_CONFIRMED"),
}
DDL = (
    "CREATE TABLE smart_bin_harvest_versions (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "version INTEGER NOT NULL, initialized_at REAL NOT NULL)",
    "CREATE TABLE smart_bin_harvest_operations (operation_id TEXT PRIMARY KEY NOT NULL "
    "REFERENCES smart_bin_external_operations(id), request_json TEXT NOT NULL, "
    "piece_uuid TEXT NOT NULL, project_id TEXT NOT NULL, activation_id TEXT, "
    "allocation_id TEXT, delivery_id TEXT, parent_operation_id TEXT "
    "REFERENCES smart_bin_harvest_operations(operation_id), local_snapshot_json TEXT NOT NULL, "
    "row_revision INTEGER NOT NULL DEFAULT 0 CHECK(row_revision>=0), "
    "readback_required INTEGER NOT NULL DEFAULT 1 CHECK(readback_required IN (0,1)), "
    "reconciliation_required INTEGER NOT NULL DEFAULT 0 CHECK(reconciliation_required IN (0,1)), "
    "confirmed_observation_id TEXT)",
    "CREATE TABLE smart_bin_harvest_observations (id TEXT PRIMARY KEY NOT NULL, "
    "operation_id TEXT NOT NULL REFERENCES smart_bin_harvest_operations(operation_id), "
    "sequence INTEGER NOT NULL, source TEXT NOT NULL CHECK(source IN ('ACK','READBACK','AMBIGUOUS')), "
    "evidence_json TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(operation_id,sequence))",
    "CREATE TABLE smart_bin_harvest_links (reservation_id TEXT PRIMARY KEY NOT NULL "
    "REFERENCES smart_bin_reservations(id), operation_id TEXT UNIQUE NOT NULL "
    "REFERENCES smart_bin_harvest_operations(operation_id), project_id TEXT NOT NULL, "
    "allocation_id TEXT NOT NULL, activation_id TEXT, allocation_json TEXT NOT NULL, "
    "created_at REAL NOT NULL, UNIQUE(project_id,allocation_id))",
    "CREATE TABLE smart_bin_harvest_receipts (request_key TEXT PRIMARY KEY NOT NULL "
    "REFERENCES smart_bin_audit_events(request_key), payload_hash TEXT NOT NULL, "
    "operation_id TEXT NOT NULL REFERENCES smart_bin_harvest_operations(operation_id), "
    "result_json TEXT NOT NULL)",
    "CREATE TRIGGER smart_bin_harvest_operations_identity BEFORE UPDATE OF operation_id,request_json,"
    "piece_uuid,project_id,activation_id,allocation_id,delivery_id,parent_operation_id,local_snapshot_json "
    "ON smart_bin_harvest_operations BEGIN SELECT RAISE(ABORT,'Harvest intent is immutable'); END",
    "CREATE TRIGGER smart_bin_harvest_operations_delete BEFORE DELETE ON smart_bin_harvest_operations "
    "BEGIN SELECT RAISE(ABORT,'Harvest intent is immutable'); END",
    "CREATE TRIGGER smart_bin_harvest_external_identity BEFORE UPDATE OF id,machine_id,reservation_id,"
    "system_name,operation_kind,request_key,payload_hash,created_at ON smart_bin_external_operations "
    "WHEN EXISTS(SELECT 1 FROM smart_bin_harvest_operations WHERE operation_id=OLD.id) "
    "BEGIN SELECT RAISE(ABORT,'Harvest external identity is immutable'); END",
    "CREATE TRIGGER smart_bin_harvest_external_reference BEFORE UPDATE OF external_ref ON smart_bin_external_operations "
    "WHEN EXISTS(SELECT 1 FROM smart_bin_harvest_operations WHERE operation_id=OLD.id) "
    "AND OLD.external_ref IS NOT NULL AND NEW.external_ref IS NOT OLD.external_ref "
    "BEGIN SELECT RAISE(ABORT,'Harvest allocation reference is immutable'); END",
    "CREATE TRIGGER smart_bin_harvest_external_phase BEFORE UPDATE OF status ON smart_bin_external_operations "
    "WHEN EXISTS(SELECT 1 FROM smart_bin_harvest_operations WHERE operation_id=OLD.id) AND NOT ("
    "NEW.status=OLD.status OR (OLD.operation_kind='ALLOCATE' AND OLD.status='LOCAL_INTENT' AND NEW.status='REMOTE_CONFIRMED') "
    "OR (OLD.operation_kind='CANCEL' AND OLD.status='CANCEL_INTENT' AND NEW.status='CANCEL_CONFIRMED') "
    "OR (OLD.operation_kind='CONFIRM' AND OLD.status='DELIVERY_CONFIRM_PENDING' AND NEW.status='DELIVERY_CONFIRM_REMOTE_CONFIRMED') "
    "OR (OLD.status IN ('REMOTE_CONFIRMED','CANCEL_CONFIRMED','DELIVERY_CONFIRM_REMOTE_CONFIRMED') AND NEW.status='COMPLETE')) "
    "BEGIN SELECT RAISE(ABORT,'Harvest phase cannot regress or skip'); END",
)
DDL += tuple(
    f"CREATE TRIGGER {table}_{action.lower()} BEFORE {action} ON {table} "
    "BEGIN SELECT RAISE(ABORT,'Harvest evidence is immutable'); END"
    for table in (
        "smart_bin_harvest_observations",
        "smart_bin_harvest_links",
        "smart_bin_harvest_receipts",
    )
    for action in ("UPDATE", "DELETE")
)
REQUIRED = {ddl.split()[2] for ddl in DDL}


@dataclass(frozen=True)
class OperationRequest:
    operation_id: str
    request_key: str
    machine_id: str
    reservation_id: str
    piece_uuid: str
    project_id: str
    operation_kind: str
    expected_reservation_revision: int
    expected_machine_revision: int
    expected_project_revision: int
    request_payload: dict[str, Any]
    activation_id: str | None = None
    allocation_id: str | None = None
    delivery_id: str | None = None
    parent_operation_id: str | None = None


def _exists(conn, name):
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
        is not None
    )


def check_schema(conn):
    _check_service_schema(conn)
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'smart_bin_harvest_%'"
        )
    }
    if names != REQUIRED:
        raise RuntimeError("Harvest local extension absent or incomplete")
    if [
        tuple(r)
        for r in conn.execute(
            "SELECT singleton,version FROM smart_bin_harvest_versions"
        )
    ] != [(1, SCHEMA_VERSION)]:
        raise RuntimeError(
            "Unsupported Harvest local extension; no automatic migration"
        )


def initialize_schema():
    with integration_lock, critical_transaction() as conn:
        _check_service_schema(conn)
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'smart_bin_harvest_%'"
        ).fetchone():
            check_schema(conn)
        else:
            for ddl in DDL:
                conn.execute(ddl)
            conn.execute(
                "INSERT INTO smart_bin_harvest_versions VALUES(1,?,?)",
                (SCHEMA_VERSION, time.time()),
            )
        conn.commit()
    return SCHEMA_VERSION


def _key(action, key):
    if not isinstance(key, str) or not key.strip():
        raise ValueError("request key required")
    return "harvest:" + action + ":" + key


def _replay(conn, key, payload_hash):
    row = conn.execute(
        "SELECT * FROM smart_bin_harvest_receipts WHERE request_key=?", (key,)
    ).fetchone()
    if row:
        return (
            json.loads(row["result_json"])
            if row["payload_hash"] == payload_hash
            else {"code": "IDEMPOTENCY_CONFLICT"}
        )
    if conn.execute(
        "SELECT 1 FROM smart_bin_audit_events WHERE request_key=?", (key,)
    ).fetchone():
        return {"code": "IDEMPOTENCY_CONFLICT"}
    return None


def _op(conn, operation_id):
    row = conn.execute(
        "SELECT e.*,h.* FROM smart_bin_external_operations e JOIN smart_bin_harvest_operations h "
        "ON h.operation_id=e.id WHERE e.id=?",
        (operation_id,),
    ).fetchone()
    return dict(row) if row else None


def _receipt(conn, op, key, payload_hash, *, code="OK", **extra):
    before = conn.execute(
        "SELECT state_revision FROM smart_bin_machines WHERE machine_id=?",
        (op["machine_id"],),
    ).fetchone()[0]
    now = time.time()
    conn.execute(
        "UPDATE smart_bin_machines SET state_revision=state_revision+1 WHERE machine_id=?",
        (op["machine_id"],),
    )
    conn.execute(
        "UPDATE smart_bin_harvest_operations SET row_revision=row_revision+1 WHERE operation_id=?",
        (op["id"],),
    )
    conn.execute(
        "UPDATE smart_bin_external_operations SET updated_at=? WHERE id=?",
        (now, op["id"]),
    )
    result = dict(
        code=code,
        operation_id=op["id"],
        phase=_op(conn, op["id"])["status"],
        operation_revision=op["row_revision"] + 1,
        before_state_revision=before,
        state_revision=before + 1,
        physical_recovery_authorized=False,
        **extra,
    )
    conn.execute(
        "INSERT INTO smart_bin_audit_events "
        "(id,machine_id,reservation_id,request_key,payload_hash,before_revision,after_revision,actor,reason,evidence_ref,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()),
            op["machine_id"],
            op["reservation_id"],
            key,
            payload_hash,
            before,
            before + 1,
            "smart_bins_harvest_integration",
            key.split(":")[1],
            op["id"],
            now,
        ),
    )
    conn.execute(
        "INSERT INTO smart_bin_harvest_receipts VALUES(?,?,?,?)",
        (key, payload_hash, op["id"], canonical(result)),
    )
    return result


def _reservation(conn, op):
    row = conn.execute(
        "SELECT * FROM smart_bin_reservations WHERE machine_id=? AND id=? AND piece_uuid=?",
        (op["machine_id"], op["reservation_id"], op["piece_uuid"]),
    ).fetchone()
    return dict(row) if row else None


def _delivery(conn, op):
    row = conn.execute(
        "SELECT * FROM smart_bin_deliveries WHERE reservation_id=?",
        (op["reservation_id"],),
    ).fetchone()
    return dict(row) if row else None


def _safe_cancel(conn, op):
    row = _reservation(conn, op)
    return bool(
        row
        and row["state"] == "CANCELLED"
        and not _delivery(conn, op)
        and not conn.execute(
            "SELECT 1 FROM smart_bin_release_attempts WHERE reservation_id=?",
            (op["reservation_id"],),
        ).fetchone()
    )


def _validate(request, kind):
    if not isinstance(request, OperationRequest) or request.operation_kind != kind:
        raise ValueError("wrong operation kind")
    for name in (
        "operation_id",
        "request_key",
        "machine_id",
        "reservation_id",
        "piece_uuid",
        "project_id",
    ):
        if (
            not isinstance(getattr(request, name), str)
            or not getattr(request, name).strip()
        ):
            raise ValueError("missing operation identity")
    for name in (
        "expected_reservation_revision",
        "expected_machine_revision",
        "expected_project_revision",
    ):
        if type(getattr(request, name)) is not int or getattr(request, name) < 0:
            raise ValueError("invalid expected revision")
    if (
        not isinstance(request.request_payload, dict)
        or request.request_payload.get("piece_id") != request.piece_uuid
    ):
        raise ValueError("payload must name the exact piece")
    return json.loads(canonical(asdict(request)))


def _begin(conn, request, kind):
    check_schema(conn)
    frozen = _validate(request, kind)
    key, payload_hash = _key(kind, request.request_key), digest(frozen)
    replay = _replay(conn, key, payload_hash)
    if replay is not None:
        return replay
    if _op(conn, request.operation_id):
        return {"code": "IDEMPOTENCY_CONFLICT"}
    row = _reservation(conn, frozen)
    if not row:
        return {"code": "LOCAL_RESERVATION_MISSING"}
    revision = conn.execute(
        "SELECT state_revision FROM smart_bin_machines WHERE machine_id=?",
        (request.machine_id,),
    ).fetchone()[0]
    if (
        row["row_revision"] != request.expected_reservation_revision
        or revision != request.expected_machine_revision
    ):
        return {"code": "STALE_REVISION"}
    if conn.execute(
        "SELECT 1 FROM smart_bin_external_operations WHERE system_name=? AND reservation_id=? AND operation_kind=?",
        ("harvest", request.reservation_id, kind),
    ).fetchone():
        return {"code": "OPERATION_ALREADY_EXISTS"}
    snapshot = {"reservation": row}
    if row["intended_kind"] == "BIN":
        coordinates = conn.execute(
            "SELECT layer_index,section_index,bin_index FROM smart_bin_slots WHERE id=? AND machine_id=?",
            (row["intended_slot_id"], request.machine_id),
        ).fetchone()
        if not coordinates:
            return {"code": "LOCAL_DESTINATION_MISSING"}
        snapshot["destination_bin"] = list(coordinates)
    if kind == "ALLOCATE":
        machines = {request.machine_id} | {
            r[0]
            for r in conn.execute(
                "SELECT e.machine_id FROM smart_bin_external_operations e JOIN smart_bin_harvest_operations h "
                "ON h.operation_id=e.id WHERE h.project_id=?",
                (request.project_id,),
            )
        }
        for machine in machines:
            recovery = inspect_on_connection(conn, machine)
            if (
                recovery["harvest_machine_blocked"]
                or request.project_id in recovery["blocked_harvest_projects"]
            ):
                return {"code": "PROJECT_ADMISSION_BLOCKED"}
        if (
            row["state"] != "RESERVED"
            or request.delivery_id
            or request.parent_operation_id
        ):
            return {"code": "UNSAFE_STATE"}
        method = request.request_payload.get("method")
        if method not in ("propose_allocation", "propose_live_allocation"):
            raise ValueError("unsupported allocation method")
        if method == "propose_live_allocation" and (
            not request.activation_id or row["quantity"] != 1
        ):
            raise ValueError(
                "live allocation requires activation identity and one piece"
            )
        if method == "propose_allocation" and (
            request.activation_id is not None
            or request.request_payload.get("mode") != "simulation"
            or request.request_payload.get("quantity") != row["quantity"]
        ):
            raise ValueError("simulation quantity or activation differs")
    else:
        parent = _op(conn, request.parent_operation_id)
        if (
            not parent
            or parent["operation_kind"] != "ALLOCATE"
            or parent["external_ref"] != request.allocation_id
            or parent["status"] not in ("REMOTE_CONFIRMED", "COMPLETE")
            or parent["readback_required"]
            or parent["reconciliation_required"]
            or any(
                parent[k] != frozen[k]
                for k in (
                    "machine_id",
                    "reservation_id",
                    "piece_uuid",
                    "project_id",
                    "activation_id",
                )
            )
        ):
            return {"code": "ALLOCATION_LINK_REQUIRED"}
        if _local_problems(conn, parent, require_terminal=False):
            return {"code": "ALLOCATION_EVIDENCE_INVALID"}
        if kind == "CANCEL":
            if not _safe_cancel(conn, frozen):
                return {"code": "UNSAFE_CANCEL"}
            if request.delivery_id or request.request_payload != dict(
                method="cancel_integration_allocation", piece_id=request.piece_uuid
            ):
                raise ValueError("invalid cancellation payload")
        else:
            native = _delivery(conn, frozen)
            link = conn.execute(
                "SELECT * FROM smart_bin_harvest_links WHERE operation_id=?",
                (parent["id"],),
            ).fetchone()
            if (
                not link
                or row["state"] != "COMPLETED"
                or not native
                or native["id"] != request.delivery_id
            ):
                return {"code": "NATIVE_DELIVERY_REQUIRED"}
            if native["actual_kind"] != "BIN" or request.request_payload != dict(
                method="confirm_allocation", piece_id=request.piece_uuid
            ):
                return {"code": "DELIVERY_CONFIRMATION_UNSUPPORTED"}
            if request.activation_id is None:
                return {"code": "PHYSICAL_ALLOCATION_REQUIRED"}
            coords = conn.execute(
                "SELECT layer_index,section_index,bin_index FROM smart_bin_slots WHERE id=? AND machine_id=?",
                (native["actual_slot_id"], request.machine_id),
            ).fetchone()
            if coords is None:
                return {"code": "LOCAL_DESTINATION_MISSING"}
            snapshot["delivery"] = native
            snapshot["confirmation_evidence"] = dict(
                physical_drop_confirmed=True,
                activation_id=request.activation_id,
                destination_bin=list(coords),
                piece_id=request.piece_uuid,
                integration=dict(
                    operation_id=request.operation_id,
                    request_key=key,
                    payload_hash=payload_hash,
                    machine_id=request.machine_id,
                    reservation_id=request.reservation_id,
                    delivery_id=request.delivery_id,
                    delivery_hash=digest(native),
                ),
            )
    now = time.time()
    conn.execute(
        "INSERT INTO smart_bin_external_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            request.operation_id,
            request.machine_id,
            request.reservation_id,
            "harvest",
            kind,
            key,
            payload_hash,
            request.allocation_id,
            KINDS[kind][0],
            None,
            now,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO smart_bin_harvest_operations "
        "(operation_id,request_json,piece_uuid,project_id,activation_id,allocation_id,delivery_id,parent_operation_id,local_snapshot_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            request.operation_id,
            canonical(frozen),
            request.piece_uuid,
            request.project_id,
            request.activation_id,
            request.allocation_id,
            request.delivery_id,
            request.parent_operation_id,
            canonical(snapshot),
        ),
    )
    return _receipt(conn, _op(conn, request.operation_id), key, payload_hash)


def begin_allocation_operation(request):
    with integration_lock, critical_transaction() as conn:
        result = _begin(conn, request, "ALLOCATE")
        conn.commit()
        return result


def begin_allocation_cancel(request):
    """Require an already committed native pre-release cancellation, never infer it."""
    with integration_lock, critical_transaction() as conn:
        result = _begin(conn, request, "CANCEL")
        conn.commit()
        return result


def stage_delivery_confirmation_on_connection(conn, request):
    """Inactive helper: caller owns integration_lock and FULL/FK transaction.

    Stage immediately after native delivery in that same transaction. Never
    commits, opens a second connection, or changes delivery/contents. Refusal
    raises so a future caller cannot accidentally commit an unjournaled delivery.
    """
    if (
        not conn.in_transaction
        or conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        or conn.execute("PRAGMA synchronous").fetchone()[0] != 2
    ):
        raise RuntimeError("caller-owned FULL/FK transaction required")
    result = _begin(conn, request, "CONFIRM")
    if result["code"] != "OK":
        raise ValueError(result["code"])
    return result


def _mutate(
    operation_id, action, request_key, payload, expected_operation_revision, fn
):
    key = _key(action, request_key)
    payload_hash = digest(
        dict(
            operation_id=operation_id,
            action=action,
            payload=payload,
            expected_operation_revision=expected_operation_revision,
        )
    )
    with integration_lock, critical_transaction() as conn:
        check_schema(conn)
        replay = _replay(conn, key, payload_hash)
        if replay is not None:
            conn.commit()
            return replay
        op = _op(conn, operation_id)
        if op is None:
            conn.commit()
            return {"code": "NOT_FOUND"}
        if (
            type(expected_operation_revision) is not int
            or op["row_revision"] != expected_operation_revision
        ):
            conn.commit()
            return {"code": "STALE_REVISION"}
        extra = fn(conn, op)
        result = _receipt(conn, op, key, payload_hash, **extra)
        conn.commit()
        return result


def _record(
    operation_id, kind, *, source, evidence, request_key, expected_operation_revision
):
    """Persist detached response data only; acceptance is a separate transaction."""
    if source not in ("ACK", "READBACK", "AMBIGUOUS"):
        raise ValueError("invalid source")
    frozen = json.loads(canonical(evidence))

    def record(conn, op):
        if op["operation_kind"] != kind:
            return {"code": "WRONG_OPERATION_KIND"}
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM smart_bin_harvest_observations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0]
        observation = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO smart_bin_harvest_observations VALUES(?,?,?,?,?,?)",
            (
                observation,
                operation_id,
                sequence,
                source,
                canonical(frozen),
                time.time(),
            ),
        )
        conn.execute(
            "UPDATE smart_bin_harvest_operations SET readback_required=1 WHERE operation_id=?",
            (operation_id,),
        )
        return {"observation_id": observation, "readback_required": True}

    return _mutate(
        operation_id,
        "RECORD_" + kind,
        request_key,
        dict(source=source, evidence=frozen),
        expected_operation_revision,
        record,
    )


def record_remote_allocation_result(operation_id, **kwargs):
    return _record(operation_id, "ALLOCATE", **kwargs)


def record_remote_cancel_result(operation_id, **kwargs):
    return _record(operation_id, "CANCEL", **kwargs)


def record_delivery_confirmation_result(operation_id, **kwargs):
    return _record(operation_id, "CONFIRM", **kwargs)


def _assess_remote(op, observation):
    """Only exact READBACK proves a phase; arbitrary ACK JSON never does."""
    if observation["source"] != "READBACK":
        return "READBACK_REQUIRED", None
    evidence = json.loads(observation["evidence_json"])
    request = json.loads(op["request_json"])
    if not isinstance(evidence, dict) or any(
        evidence.get(k) != request[v]
        for k, v in (("project_id", "project_id"), ("piece_id", "piece_uuid"))
    ):
        return "REMOTE_IDENTITY_MISMATCH", None
    remote = evidence.get("allocation")
    if remote is None:
        return "ABSENCE_IS_NOT_CANCELLATION", None
    if (
        not isinstance(remote, dict)
        or remote.get("project_id") != op["project_id"]
        or remote.get("piece_id") != op["piece_uuid"]
        or remote.get("runtime_id") != op["activation_id"]
        or not remote.get("allocation_id")
        or (
            op["external_ref"] is not None
            and remote["allocation_id"] != op["external_ref"]
        )
        or remote.get("quantity")
        != json.loads(op["local_snapshot_json"])["reservation"]["quantity"]
    ):
        return "REMOTE_IDENTITY_MISMATCH", None
    expected_status = {
        "ALLOCATE": "planned",
        "CANCEL": "undone",
        "CONFIRM": "confirmed",
    }[op["operation_kind"]]
    if remote.get("status") != expected_status:
        return "REMOTE_STATE_MISMATCH", remote
    receipts = evidence.get("receipts", [])
    if not isinstance(receipts, list):
        return "REMOTE_PAYLOAD_MISMATCH", remote
    origin_id = (
        op["id"] if op["operation_kind"] == "ALLOCATE" else op["parent_operation_id"]
    )
    origin = [
        r
        for r in receipts
        if isinstance(r, dict) and r.get("operation_id") == origin_id
    ]
    if not origin:
        return "REMOTE_REQUEST_PROVENANCE_MISSING", remote
    try:
        original_allocation = json.loads(origin[0]["allocation_json"])
        original_request = json.loads(origin[0]["request_json"])
        immutable_fields = set(original_allocation) - {
            "status",
            "evidence",
            "confirmed_at",
            "undone_at",
        }
        if digest(original_request) != origin[0]["payload_hash"]:
            return "REMOTE_PAYLOAD_MISMATCH", remote
        if (
            len(origin) != 1
            or not immutable_fields
            or any(original_allocation[k] != remote.get(k) for k in immutable_fields)
            or any(
                original_request[k] != request[k]
                for k in (
                    "machine_id",
                    "reservation_id",
                    "piece_uuid",
                    "project_id",
                    "activation_id",
                )
            )
            or original_request["operation_kind"] != "ALLOCATE"
        ):
            return "REMOTE_ALLOCATION_MISMATCH", remote
        if op["activation_id"] is not None:
            destination = json.loads(origin[0]["destination_json"])
            coords = [
                destination[k] for k in ("layer_index", "section_index", "bin_index")
            ]
            saved = json.loads(op["local_snapshot_json"])
            if destination["group_id"] != remote["group_id"] or coords != saved.get(
                "destination_bin"
            ):
                return "REMOTE_DESTINATION_MISMATCH", remote
    except (KeyError, TypeError, ValueError):
        return "REMOTE_PAYLOAD_MISMATCH", remote
    if op["operation_kind"] in ("ALLOCATE", "CANCEL"):
        matching = [
            r
            for r in receipts
            if isinstance(r, dict) and r.get("operation_id") == op["id"]
        ]
        if not matching:
            return "REMOTE_REQUEST_PROVENANCE_MISSING", remote
        if len(matching) != 1:
            return "REMOTE_PAYLOAD_MISMATCH", remote
        receipt = matching[0]
        try:
            stored = json.loads(receipt["request_json"])
        except (KeyError, TypeError, ValueError):
            return "REMOTE_PAYLOAD_MISMATCH", remote
        if (
            receipt.get("payload_hash") != op["payload_hash"]
            or digest(stored) != op["payload_hash"]
            or stored != request
            or receipt.get("request_key") != op["request_key"]
            or receipt.get("allocation_id") != remote["allocation_id"]
        ):
            return "REMOTE_PAYLOAD_MISMATCH", remote
    else:
        expected = json.loads(op["local_snapshot_json"])["confirmation_evidence"]
        if remote.get("evidence") != expected:
            return "REMOTE_DELIVERY_MISMATCH", remote
    return "OK", remote


def advance_remote_result(
    operation_id, *, observation_id, request_key, expected_operation_revision
):
    def advance(conn, op):
        obs = conn.execute(
            "SELECT * FROM smart_bin_harvest_observations WHERE operation_id=? ORDER BY sequence DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        if not obs or obs["id"] != observation_id:
            return {"code": "LATEST_READBACK_REQUIRED"}
        code, remote = _assess_remote(op, obs)
        if code == "OK" and _local_problems(conn, op, require_terminal=False):
            code = "LOCAL_EVIDENCE_MISMATCH"
        if code != "OK":
            if code.endswith("MISMATCH"):
                conn.execute(
                    "UPDATE smart_bin_harvest_operations SET reconciliation_required=1 WHERE operation_id=?",
                    (operation_id,),
                )
                conn.execute(
                    "INSERT INTO smart_bin_discrepancies "
                    "(id,machine_id,reservation_id,kind,status,evidence_ref,details_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        op["machine_id"],
                        op["reservation_id"],
                        "HARVEST_INTEGRATION_CONTRADICTION",
                        "open",
                        observation_id,
                        canonical(
                            dict(
                                operation_id=operation_id,
                                reason=code,
                                project_id=op["project_id"],
                            )
                        ),
                        time.time(),
                    ),
                )
            return {"code": code, "readback_required": True}
        if op["reconciliation_required"]:
            return {"code": "RECONCILIATION_REQUIRED"}
        conn.execute(
            "UPDATE smart_bin_harvest_operations SET readback_required=0,confirmed_observation_id=? WHERE operation_id=?",
            (observation_id, operation_id),
        )
        phase = (
            op["status"]
            if op["status"] == "COMPLETE"
            else KINDS[op["operation_kind"]][1]
        )
        conn.execute(
            "UPDATE smart_bin_external_operations SET status=?,external_ref=?,result_json=? WHERE id=?",
            (phase, remote["allocation_id"], canonical(remote), operation_id),
        )
        return {"remote_confirmation_known": True, "readback_required": False}

    return _mutate(
        operation_id,
        "ADVANCE",
        request_key,
        dict(observation_id=observation_id),
        expected_operation_revision,
        advance,
    )


def _local_problems(conn, op, *, require_terminal=True):
    problems = []
    row = _reservation(conn, op)
    saved = json.loads(op["local_snapshot_json"])
    if digest(json.loads(op["request_json"])) != op["payload_hash"]:
        problems.append("LOCAL_REQUEST_HASH_MISMATCH")
    if not row:
        return ["LOCAL_RESERVATION_MISSING"]
    immutable = set(saved["reservation"]) - {"state", "row_revision", "updated_at"}
    if any(row[k] != saved["reservation"][k] for k in immutable):
        problems.append("LOCAL_RESERVATION_CHANGED")
    if "destination_bin" in saved:
        coords = conn.execute(
            "SELECT layer_index,section_index,bin_index FROM smart_bin_slots WHERE id=? AND machine_id=?",
            (row["intended_slot_id"], op["machine_id"]),
        ).fetchone()
        if not coords or list(coords) != saved["destination_bin"]:
            problems.append("LOCAL_DESTINATION_CHANGED")
    native = _delivery(conn, op)
    if op["operation_kind"] == "CANCEL" and not _safe_cancel(conn, op):
        problems.append("UNSAFE_CANCEL")
    if op["operation_kind"] == "CONFIRM":
        if not native or native != saved.get("delivery") or row["state"] != "COMPLETED":
            problems.append("NATIVE_DELIVERY_CHANGED_OR_MISSING")
        link = conn.execute(
            "SELECT * FROM smart_bin_harvest_links WHERE reservation_id=? AND operation_id=?",
            (op["reservation_id"], op["parent_operation_id"]),
        ).fetchone()
        if not link or any(
            link[k] != op[k] for k in ("project_id", "allocation_id", "activation_id")
        ):
            problems.append("LOCAL_ALLOCATION_LINK_MISSING_OR_CHANGED")
    completed_cancellation = conn.execute(
        "SELECT e.id FROM smart_bin_external_operations e JOIN smart_bin_harvest_operations h "
        "ON h.operation_id=e.id WHERE h.parent_operation_id=? AND e.operation_kind='CANCEL' AND e.status='COMPLETE' "
        "AND h.readback_required=0 AND h.reconciliation_required=0",
        (op["id"],),
    ).fetchone()
    if (
        require_terminal
        and op["status"] == "COMPLETE"
        and op["operation_kind"] == "ALLOCATE"
    ):
        link = conn.execute(
            "SELECT * FROM smart_bin_harvest_links WHERE operation_id=?", (op["id"],)
        ).fetchone()
        safely_cancelled = bool(completed_cancellation and _safe_cancel(conn, op))
        if not safely_cancelled and (
            not link
            or link["reservation_id"] != op["reservation_id"]
            or link["allocation_id"] != op["external_ref"]
            or link["project_id"] != op["project_id"]
            or link["activation_id"] != op["activation_id"]
        ):
            problems.append("LOCAL_ALLOCATION_LINK_MISSING_OR_CHANGED")
        if row["state"] == "COMPLETED":
            confirmations = conn.execute(
                "SELECT e.id FROM smart_bin_external_operations e JOIN smart_bin_harvest_operations h ON h.operation_id=e.id "
                "WHERE h.parent_operation_id=? AND e.operation_kind='CONFIRM'",
                (op["id"],),
            ).fetchall()
            if not native or not confirmations:
                problems.append("DELIVERY_CONFIRMATION_MISSING")
        elif row["state"] == "CANCELLED":
            if not safely_cancelled:
                problems.append("CANCELLATION_PENDING")
    if op["status"] in ("COMPLETE", KINDS[op["operation_kind"]][1]):
        observation = conn.execute(
            "SELECT * FROM smart_bin_harvest_observations WHERE id=? AND operation_id=?",
            (op["confirmed_observation_id"], op["id"]),
        ).fetchone()
        if not observation or _assess_remote(op, observation)[0] != "OK":
            problems.append("REMOTE_CONFIRMATION_MISSING_OR_CHANGED")
        elif json.loads(observation["evidence_json"])["allocation"] != json.loads(
            op["result_json"]
        ):
            problems.append("REMOTE_RESULT_CHANGED")
    return problems


def link_local_reservation(
    operation_id,
    *,
    request_key,
    expected_operation_revision,
    expected_reservation_revision,
):
    def link(conn, op):
        if (
            op["operation_kind"] != "ALLOCATE"
            or op["status"] != "REMOTE_CONFIRMED"
            or op["readback_required"]
            or op["reconciliation_required"]
        ):
            return {"code": "REMOTE_CONFIRMATION_REQUIRED"}
        row = _reservation(conn, op)
        if (
            not row
            or row["state"] != "RESERVED"
            or row["row_revision"] != expected_reservation_revision
        ):
            return {"code": "STALE_OR_UNSAFE_RESERVATION"}
        if _local_problems(conn, op):
            return {"code": "LOCAL_EVIDENCE_MISMATCH"}
        remote = json.loads(op["result_json"])
        if conn.execute(
            "SELECT 1 FROM smart_bin_harvest_links WHERE reservation_id=? OR (project_id=? AND allocation_id=?)",
            (op["reservation_id"], op["project_id"], op["external_ref"]),
        ).fetchone():
            return {"code": "ALLOCATION_LINK_CONFLICT"}
        conn.execute(
            "INSERT INTO smart_bin_harvest_links VALUES(?,?,?,?,?,?,?)",
            (
                op["reservation_id"],
                op["id"],
                op["project_id"],
                op["external_ref"],
                op["activation_id"],
                canonical(remote),
                time.time(),
            ),
        )
        conn.execute(
            "UPDATE smart_bin_external_operations SET status='COMPLETE' WHERE id=?",
            (operation_id,),
        )
        return {"local_linked": True}

    return _mutate(
        operation_id,
        "LINK",
        request_key,
        dict(expected_reservation_revision=expected_reservation_revision),
        expected_operation_revision,
        link,
    )


def complete_operation(operation_id, *, request_key, expected_operation_revision):
    def finish(conn, op):
        if (
            op["operation_kind"] == "ALLOCATE"
            or op["status"] != KINDS[op["operation_kind"]][1]
            or op["readback_required"]
            or op["reconciliation_required"]
        ):
            return {"code": "REMOTE_CONFIRMATION_REQUIRED"}
        if _local_problems(conn, op):
            return {"code": "LOCAL_EVIDENCE_MISMATCH"}
        conn.execute(
            "UPDATE smart_bin_external_operations SET status='COMPLETE' WHERE id=?",
            (operation_id,),
        )
        if op["operation_kind"] == "CANCEL":
            parent = _op(conn, op["parent_operation_id"])
            if parent["status"] == "REMOTE_CONFIRMED":
                conn.execute(
                    "UPDATE smart_bin_external_operations SET status='COMPLETE' WHERE id=?",
                    (parent["id"],),
                )
                parent_key = _key("CANCEL_ALLOCATION", operation_id)
                _receipt(
                    conn,
                    parent,
                    parent_key,
                    digest(dict(cancel_operation_id=operation_id)),
                    cancel_operation_id=operation_id,
                )
        return {}

    return _mutate(
        operation_id, "COMPLETE", request_key, {}, expected_operation_revision, finish
    )


def lookup_operation(operation_id):
    from smart_bins_delivery import _readonly

    with _readonly() as conn:
        check_schema(conn)
        op = _op(conn, operation_id)
        if op:
            op["request"] = json.loads(op["request_json"])
            op["local_snapshot"] = json.loads(op["local_snapshot_json"])
        return op


def inspect_on_connection(conn, machine_id):
    """One local read snapshot; no remote calls, migration or physical authority."""
    external = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM smart_bin_external_operations WHERE machine_id=? AND system_name='harvest' ORDER BY created_at,id",
            (machine_id,),
        )
    ]
    if not external and not _exists(conn, "smart_bin_harvest_versions"):
        return {
            "harvest_operations": [],
            "harvest_blockers": [],
            "blocked_harvest_projects": [],
            "physical_recovery_authorized": False,
            "harvest_machine_blocked": False,
        }
    try:
        check_schema(conn)
        schema_ok = True
    except RuntimeError:
        schema_ok = False
    operations = []
    if not schema_ok and not external:
        blocker = dict(
            kind="HARVEST_INTEGRATION_SCHEMA_BLOCKER",
            machine_id=machine_id,
            admission_scope={
                "scope": "machine",
                "machine_id": machine_id,
                "project_id": None,
            },
            physical_recovery_authorized=False,
        )
        return dict(
            harvest_operations=[],
            harvest_blockers=[blocker],
            blocked_harvest_projects=[],
            harvest_machine_blocked=True,
            physical_recovery_authorized=False,
        )
    for ext in external:
        op = _op(conn, ext["id"]) if schema_ok else None
        remote_known = False
        try:
            problems = (
                _local_problems(conn, op)
                if op
                else ["INTEGRATION_SCHEMA_OR_INTENT_MISSING"]
            )
            if op and op["confirmed_observation_id"]:
                observation = conn.execute(
                    "SELECT * FROM smart_bin_harvest_observations WHERE id=? AND operation_id=?",
                    (op["confirmed_observation_id"], op["id"]),
                ).fetchone()
                remote_known = bool(
                    observation and _assess_remote(op, observation)[0] == "OK"
                )
        except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
            problems = ["INTEGRATION_EVIDENCE_INVALID"]
        if op:
            if op["status"] != "COMPLETE":
                problems.append("INCOMPLETE_INTEGRATION")
            if op["readback_required"]:
                problems.append("READBACK_REQUIRED")
            if op["reconciliation_required"]:
                problems.append("RECONCILIATION_REQUIRED")
        operations.append(
            dict(
                operation_id=ext["id"],
                operation_kind=ext["operation_kind"],
                phase=ext["status"],
                reservation_id=ext["reservation_id"],
                piece_uuid=op["piece_uuid"] if op else None,
                project_id=op["project_id"] if op else None,
                allocation_id=ext["external_ref"],
                activation_id=op["activation_id"] if op else None,
                delivery_id=op["delivery_id"] if op else None,
                local_reservation_exists=bool(_reservation(conn, op)) if op else False,
                local_delivery_exists=bool(_delivery(conn, op)) if op else False,
                remote_confirmation_known=remote_known,
                readback_required=bool(not op or op["readback_required"]),
                cancellation_pending=ext["operation_kind"] == "CANCEL"
                and ext["status"] != "COMPLETE",
                admission_scope={
                    "scope": "project" if op else "machine",
                    "machine_id": machine_id,
                    "project_id": op["project_id"] if op else None,
                },
                blockers=problems,
                physical_recovery_authorized=False,
            )
        )
    blockers = [
        dict(item, kind="HARVEST_INTEGRATION_BLOCKER")
        for item in operations
        if item["blockers"]
    ]
    return {
        "harvest_operations": operations,
        "harvest_blockers": blockers,
        "blocked_harvest_projects": sorted(
            {b["project_id"] for b in blockers if b["project_id"]}
        ),
        "physical_recovery_authorized": False,
        "harvest_machine_blocked": any(b["project_id"] is None for b in blockers),
    }


def inspect_integration_recovery(machine_id):
    from smart_bins_delivery import _readonly

    with _readonly() as conn:
        return inspect_on_connection(conn, machine_id)
