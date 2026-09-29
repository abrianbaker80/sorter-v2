"""Versioned native custody only. No motion, receiving credit, or implicit DDL."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3
import threading
import time
import uuid

from local_state import local_state_db_path
from smart_bins_storage import critical_transaction, check_schema_version

SCHEMA_VERSION = 1
HELD = ("RESERVED", "RELEASE_INTENT", "EXIT_CONFIRMED", "UNCERTAIN")
EVIDENCE_TYPES = frozenset(
    {
        "ROUTE_VERIFIED",
        "ROUTE_UNCERTAIN",
        "MOTOR_ACCEPTED",
        "MOTOR_REJECTED",
        "MOTOR_AMBIGUOUS",
        "INFERRED_EXIT",
        "EJECT_TIMEOUT_VISIBLE",
        "TRACK_LOST_REIDENTIFIED",
        "SOFTWARE_SLOT_PROMOTION",
        "STALL_PRE_CLEAR_PROMOTION",
        "CHANNEL_CLEAR_OBSERVED",
        "UNKNOWN_MATERIAL",
        "RELEASE_INTENT",
        "DISPATCH_CONSUMED",
    }
)


class CustodyRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class NativeIdentity:
    machine_id: str
    piece_uuid: str
    reservation_id: str
    incarnation: str
    head_generation: int
    route_revision: int
    attempt_id: str


_DDL = (
    "CREATE TABLE smart_bin_native_versions(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)",
    "CREATE TABLE smart_bin_native_settings(machine_id TEXT PRIMARY KEY, policy_id TEXT NOT NULL, namespace TEXT NOT NULL, routing_revision INTEGER NOT NULL, FOREIGN KEY(machine_id,policy_id) REFERENCES smart_bin_policy_revisions(machine_id,id))",
    "CREATE TABLE smart_bin_native_claims(reservation_id TEXT PRIMARY KEY, machine_id TEXT NOT NULL, piece_uuid TEXT NOT NULL, incarnation TEXT NOT NULL, head_generation INTEGER NOT NULL CHECK(head_generation>=0), route_revision INTEGER NOT NULL CHECK(route_revision>=0), config_digest TEXT NOT NULL, attempt_id TEXT UNIQUE, status TEXT NOT NULL CHECK(status IN ('RESERVED','RELEASE_INTENT','DISPATCH_CONSUMED','ACCEPTED','REJECTED','UNCERTAIN')), revision INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL, FOREIGN KEY(machine_id,reservation_id) REFERENCES smart_bin_reservations(machine_id,id))",
    "CREATE TABLE smart_bin_native_evidence(id TEXT PRIMARY KEY, reservation_id TEXT, machine_id TEXT NOT NULL, attempt_id TEXT, incarnation TEXT NOT NULL, head_generation INTEGER, kind TEXT NOT NULL, details_json TEXT NOT NULL, observed_at REAL NOT NULL, FOREIGN KEY(reservation_id) REFERENCES smart_bin_native_claims(reservation_id))",
    "CREATE TABLE smart_bin_native_holds(id TEXT PRIMARY KEY, machine_id TEXT NOT NULL, kind TEXT NOT NULL, details_json TEXT NOT NULL, created_at REAL NOT NULL, resolved_at REAL)",
    "CREATE INDEX smart_bin_native_current_reservations ON smart_bin_reservations(machine_id,state,id)",
    "CREATE INDEX smart_bin_native_current_holds ON smart_bin_native_holds(machine_id) WHERE resolved_at IS NULL",
    "CREATE INDEX smart_bin_native_current_discrepancies ON smart_bin_discrepancies(machine_id) WHERE resolved_at IS NULL",
    "CREATE INDEX smart_bin_native_piece_evidence ON smart_bin_native_evidence(reservation_id,observed_at)",
    "CREATE TRIGGER smart_bin_native_evidence_no_update BEFORE UPDATE ON smart_bin_native_evidence BEGIN SELECT RAISE(ABORT,'native evidence is immutable'); END",
    "CREATE TRIGGER smart_bin_native_evidence_no_delete BEFORE DELETE ON smart_bin_native_evidence BEGIN SELECT RAISE(ABORT,'native evidence is immutable'); END",
    "CREATE TRIGGER smart_bin_native_claim_no_delete BEFORE DELETE ON smart_bin_native_claims BEGIN SELECT RAISE(ABORT,'native custody is retained'); END",
    "CREATE TRIGGER smart_bin_native_status_forward BEFORE UPDATE OF status ON smart_bin_native_claims WHEN NOT (NEW.status=OLD.status OR NEW.status='UNCERTAIN' OR (OLD.status='RESERVED' AND NEW.status='RELEASE_INTENT') OR (OLD.status='RELEASE_INTENT' AND NEW.status='DISPATCH_CONSUMED') OR (OLD.status='DISPATCH_CONSUMED' AND NEW.status IN ('ACCEPTED','REJECTED'))) BEGIN SELECT RAISE(ABORT,'native release cannot be replayed'); END",
    "CREATE TRIGGER smart_bin_native_identity_immutable BEFORE UPDATE OF reservation_id,machine_id,piece_uuid,incarnation,head_generation,route_revision,config_digest ON smart_bin_native_claims BEGIN SELECT RAISE(ABORT,'native custody identity immutable'); END",
    "CREATE TRIGGER smart_bin_native_attempt_immutable BEFORE UPDATE OF attempt_id ON smart_bin_native_claims WHEN OLD.attempt_id IS NOT NULL AND NEW.attempt_id IS NOT OLD.attempt_id BEGIN SELECT RAISE(ABORT,'native attempt immutable'); END",
)


def _tables(conn):
    return {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _check(conn):
    check_schema_version(conn)
    try:
        rows = conn.execute(
            "SELECT singleton,version FROM smart_bin_native_versions"
        ).fetchall()
        if [tuple(r) for r in rows] != [(1, SCHEMA_VERSION)]:
            raise CustodyRefused("Unsupported native custody version")
        objects = {
            (r[0], r[1]) for r in conn.execute("SELECT type,name FROM sqlite_master")
        }
        required = {
            (sql.split()[1].lower(), sql.split()[2].split("(")[0]) for sql in _DDL
        }
        if not required <= objects:
            raise CustodyRefused("Incomplete native custody extension")
    except sqlite3.Error as exc:
        raise CustodyRefused("Native custody extension unavailable") from exc


def initialize_native_schema(
    machine_id: str, policy_id: str, namespace: str, routing_revision: int
) -> int:
    """Explicit opt-in after RB02 initialization; never called by startup."""
    if (
        not machine_id
        or not policy_id
        or not namespace
        or type(routing_revision) is not int
        or routing_revision < 0
    ):
        raise ValueError("Invalid native configuration")
    with critical_transaction() as conn:
        from smart_bins_service import _check_service_schema

        _check_service_schema(conn)
        if "smart_bin_native_versions" not in _tables(conn):
            for sql in _DDL:
                conn.execute(sql)
            conn.execute(
                "INSERT INTO smart_bin_native_versions VALUES(1,?)", (SCHEMA_VERSION,)
            )
        _check(conn)
        row = conn.execute(
            "SELECT policy_id,namespace,routing_revision FROM smart_bin_native_settings WHERE machine_id=?",
            (machine_id,),
        ).fetchone()
        if row is not None and tuple(row) != (policy_id, namespace, routing_revision):
            raise CustodyRefused(
                "Native configuration already bound; explicit reconciliation required"
            )
        conn.execute(
            "INSERT OR IGNORE INTO smart_bin_native_settings VALUES(?,?,?,?)",
            (machine_id, policy_id, namespace, routing_revision),
        )
        conn.commit()
    return SCHEMA_VERSION


@contextmanager
def read_only(path: Path | None = None):
    path = Path(path or local_state_db_path()).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def startup_state(machine_id: str) -> dict:
    """One startup inspection; does not create a missing file or any schema."""
    if not local_state_db_path().exists():
        return {"active": False, "blocked": False}
    with read_only() as conn:
        tables = _tables(conn)
        if not any(t.startswith("smart_bin_") for t in tables):
            return {"active": False, "blocked": False}
        check_schema_version(conn)
        held = (
            conn.execute(
                "SELECT id FROM smart_bin_reservations WHERE machine_id=? AND state IN (?,?,?,?) LIMIT 1",
                (machine_id, *HELD),
            ).fetchone()
            is not None
        )
        discrepancy = (
            conn.execute(
                "SELECT 1 FROM smart_bin_discrepancies WHERE machine_id=? AND resolved_at IS NULL LIMIT 1",
                (machine_id,),
            ).fetchone()
            is not None
        )
        followup = False
        if "smart_bin_completion_followups" in tables:
            followup = (
                conn.execute(
                    "SELECT 1 FROM smart_bin_completion_followups WHERE machine_id=? AND state<>'CLOSED' LIMIT 1",
                    (machine_id,),
                ).fetchone()
                is not None
            )
        if "smart_bin_native_versions" not in tables:
            return {"active": False, "blocked": held or discrepancy or followup}
        _check(conn)
        settings = conn.execute(
            "SELECT * FROM smart_bin_native_settings WHERE machine_id=?", (machine_id,)
        ).fetchone()
        native_hold = (
            conn.execute(
                "SELECT 1 FROM smart_bin_native_holds WHERE machine_id=? AND resolved_at IS NULL LIMIT 1",
                (machine_id,),
            ).fetchone()
            is not None
        )
        return {
            "active": settings is not None,
            "blocked": held or discrepancy or followup or native_hold,
            "settings": dict(settings) if settings else None,
        }


def has_blocker(conn, machine_id, except_reservation=None):
    if conn.execute(
        "SELECT 1 FROM smart_bin_native_holds WHERE machine_id=? AND resolved_at IS NULL LIMIT 1",
        (machine_id,),
    ).fetchone():
        return True
    if conn.execute(
        "SELECT 1 FROM smart_bin_discrepancies WHERE machine_id=? AND resolved_at IS NULL LIMIT 1",
        (machine_id,),
    ).fetchone():
        return True
    return (
        conn.execute(
            "SELECT 1 FROM smart_bin_reservations WHERE machine_id=? AND state IN (?,?,?,?) AND id IS NOT ? LIMIT 1",
            (machine_id, *HELD, except_reservation),
        ).fetchone()
        is not None
    )


def _event(conn, identity, kind, details):
    if kind not in EVIDENCE_TYPES:
        raise ValueError("Unknown native evidence type")
    conn.execute(
        "INSERT INTO smart_bin_native_evidence VALUES(?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()),
            identity.reservation_id,
            identity.machine_id,
            identity.attempt_id or None,
            identity.incarnation,
            identity.head_generation,
            kind,
            json.dumps(details, sort_keys=True, allow_nan=False),
            time.time(),
        ),
    )


def _current(conn, identity):
    _check(conn)
    row = conn.execute(
        "SELECT n.*,r.state AS reservation_state,r.row_revision,r.routing_revision FROM smart_bin_native_claims n JOIN smart_bin_reservations r ON r.id=n.reservation_id WHERE n.reservation_id=? AND n.machine_id=?",
        (identity.reservation_id, identity.machine_id),
    ).fetchone()
    if (
        row is None
        or row["reservation_state"] not in HELD
        or any(
            row[k] != getattr(identity, k)
            for k in ("piece_uuid", "incarnation", "head_generation", "route_revision")
        )
        or row["routing_revision"] != identity.route_revision
    ):
        raise CustodyRefused("Stale native owner, head or route")
    return row


def bind_reservation(identity: NativeIdentity, config_digest: str):
    with critical_transaction() as conn:
        _check(conn)
        row = conn.execute(
            "SELECT * FROM smart_bin_reservations WHERE machine_id=? AND id=?",
            (identity.machine_id, identity.reservation_id),
        ).fetchone()
        if (
            row is None
            or row["state"] != "RESERVED"
            or row["piece_uuid"] != identity.piece_uuid
            or row["routing_revision"] != identity.route_revision
        ):
            raise CustodyRefused("Reservation cannot bind native custody")
        now = time.time()
        conn.execute(
            "INSERT INTO smart_bin_native_claims VALUES(?,?,?,?,?,?,?,NULL,'RESERVED',0,?,?)",
            (
                identity.reservation_id,
                identity.machine_id,
                identity.piece_uuid,
                identity.incarnation,
                identity.head_generation,
                identity.route_revision,
                config_digest,
                now,
                now,
            ),
        )
        conn.commit()


def _transition_reservation(conn, row, state):
    conn.execute(
        "UPDATE smart_bin_reservations SET state=?,row_revision=row_revision+1,updated_at=? WHERE id=?",
        (state, time.time(), row["reservation_id"]),
    )
    conn.execute(
        "UPDATE smart_bin_machines SET state_revision=state_revision+1 WHERE machine_id=?",
        (row["machine_id"],),
    )


def arm(identity: NativeIdentity, config_digest: str, route_evidence: dict):
    """Commit intent before READY; no permit may be reconstructed by replay."""
    with critical_transaction() as conn:
        row = _current(conn, identity)
        if not identity.attempt_id:
            raise ValueError("Release attempt identity is required")
        if (
            row["status"] != "RESERVED"
            or row["reservation_state"] != "RESERVED"
            or row["row_revision"] != 0
            or row["attempt_id"] is not None
        ):
            raise CustodyRefused("Release intent already exists; never replay motion")
        if (
            has_blocker(conn, identity.machine_id, identity.reservation_id)
            or row["config_digest"] != config_digest
        ):
            raise CustodyRefused("Route changed or custody blocker exists")
        _event(conn, identity, "ROUTE_VERIFIED", route_evidence)
        _event(conn, identity, "RELEASE_INTENT", {})
        conn.execute(
            "INSERT INTO smart_bin_release_attempts VALUES(?,?,?,?,?)",
            (
                identity.attempt_id,
                identity.reservation_id,
                identity.incarnation,
                f"native:{identity.head_generation}:{identity.attempt_id}",
                time.time(),
            ),
        )
        conn.execute(
            "UPDATE smart_bin_native_claims SET attempt_id=?,status='RELEASE_INTENT',revision=revision+1,updated_at=? WHERE reservation_id=?",
            (identity.attempt_id, time.time(), identity.reservation_id),
        )
        _transition_reservation(conn, row, "RELEASE_INTENT")
        conn.commit()


def consume(identity: NativeIdentity, config_digest: str):
    with critical_transaction() as conn:
        row = _current(conn, identity)
        if (
            row["status"] != "RELEASE_INTENT"
            or row["reservation_state"] != "RELEASE_INTENT"
            or row["row_revision"] != 1
            or row["attempt_id"] != identity.attempt_id
            or row["config_digest"] != config_digest
            or has_blocker(conn, identity.machine_id, identity.reservation_id)
        ):
            raise CustodyRefused("Dispatch permit is stale or consumed")
        conn.execute(
            "UPDATE smart_bin_native_claims SET status='DISPATCH_CONSUMED',revision=revision+1,updated_at=? WHERE reservation_id=?",
            (time.time(), identity.reservation_id),
        )
        _event(conn, identity, "DISPATCH_CONSUMED", {})
        conn.commit()


def observe(identity: NativeIdentity, kind: str, details: dict, *, uncertain=False):
    with critical_transaction() as conn:
        row = _current(conn, identity)
        if row["attempt_id"] is not None and row["attempt_id"] != identity.attempt_id:
            raise CustodyRefused("Observation belongs to another release attempt")
        if kind == "INFERRED_EXIT" and row["status"] != "ACCEPTED":
            raise CustodyRefused("Inferred exit requires accepted owned release")
        _event(conn, identity, kind, details)
        status = {
            "MOTOR_ACCEPTED": "ACCEPTED",
            "MOTOR_REJECTED": "REJECTED",
            "MOTOR_AMBIGUOUS": "UNCERTAIN",
        }.get(kind)
        if kind.startswith("MOTOR_") and row["status"] != "DISPATCH_CONSUMED":
            raise CustodyRefused("Command outcome without unique consumed dispatch")
        if uncertain:
            status = "UNCERTAIN"
            _transition_reservation(conn, row, "UNCERTAIN")
        if status:
            conn.execute(
                "UPDATE smart_bin_native_claims SET status=?,revision=revision+1,updated_at=? WHERE reservation_id=?",
                (status, time.time(), identity.reservation_id),
            )
        conn.commit()


def hold(machine_id, kind, details):
    with critical_transaction() as conn:
        _check(conn)
        affected = [
            dict(r)
            for r in conn.execute(
                "SELECT id AS reservation_id,machine_id,piece_uuid,state,intended_kind,intended_slot_id,intended_cycle_id,row_revision "
                "FROM smart_bin_reservations WHERE machine_id=? AND state IN (?,?,?,?)",
                (machine_id, *HELD),
            )
        ]
        snapshot = {**details, "affected_reservations": affected}
        conn.execute(
            "INSERT INTO smart_bin_native_holds VALUES(?,?,?,?,?,NULL)",
            (
                str(uuid.uuid4()),
                machine_id,
                kind,
                json.dumps(snapshot, sort_keys=True),
                time.time(),
            ),
        )
        for row in affected:
            _transition_reservation(conn, row, "UNCERTAIN")
            conn.execute(
                "UPDATE smart_bin_native_claims SET status='UNCERTAIN',revision=revision+1,updated_at=? WHERE reservation_id=?",
                (time.time(), row["reservation_id"]),
            )
        conn.commit()


def machine_observation(machine_id, incarnation, kind, details):
    if kind not in EVIDENCE_TYPES:
        raise ValueError("Unknown native observation")
    with critical_transaction() as conn:
        _check(conn)
        conn.execute(
            "INSERT INTO smart_bin_native_evidence VALUES(?,NULL,?,NULL,?,NULL,?,?,?)",
            (
                str(uuid.uuid4()),
                machine_id,
                incarnation,
                kind,
                json.dumps(details, sort_keys=True, allow_nan=False),
                time.time(),
            ),
        )
        conn.commit()


# All native/manual/startup motion shares this lock. No SQLite transaction spans
# the yielded physical side effect. No controller or hardware is imported here.
MOTION_LOCK = threading.RLock()
_installed = None
_scope = threading.local()


def install(adapter):
    global _installed
    with MOTION_LOCK:
        _installed = adapter


@contextmanager
def motion_entry(kind: str, role: str = ""):
    with MOTION_LOCK:
        if _installed is not None:
            _installed.check_motion(kind, role, getattr(_scope, "value", None))
        yield


@contextmanager
def owned_scope(value):
    with MOTION_LOCK:
        old = getattr(_scope, "value", None)
        _scope.value = value
        try:
            yield
        finally:
            _scope.value = old


def guard_motion(kind, *, stop_value=False):
    """Driver entry fence shared by native code and background/manual callers."""
    from functools import wraps

    def decorate(fn):
        @wraps(fn)
        def guarded(self, *args, **kwargs):
            if stop_value and args and not args[0]:
                return fn(self, *args, **kwargs)
            with motion_entry(kind, getattr(self, "_name", "route")):
                return fn(self, *args, **kwargs)

        return guarded

    return decorate
