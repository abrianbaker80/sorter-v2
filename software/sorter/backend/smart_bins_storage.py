"""Inactive smart-bin ledger schema and caller-owned SQLite transactions.

Nothing in this module initializes on import or participates in legacy startup.
P2A2 will backfill; later slices will provide admission and state transitions.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from local_state import _connect


SCHEMA_VERSION = 2


class UnsupportedSmartBinSchema(RuntimeError):
    """The ledger is absent, incomplete, or from an unsupported version."""


_DELIVERY_RESERVATION_MATCH = (
    "r.id = NEW.reservation_id AND "
    "r.machine_id IS NEW.machine_id AND r.piece_uuid IS NEW.piece_uuid AND "
    "r.run_id IS NEW.run_id AND r.policy_revision_id IS NEW.policy_revision_id AND "
    "r.group_key_id IS NEW.group_key_id AND r.quantity IS NEW.quantity AND "
    "r.intended_kind IS NEW.intended_kind AND "
    "r.intended_slot_id IS NEW.intended_slot_id AND "
    "r.intended_cycle_id IS NEW.intended_cycle_id"
)


_SCHEMA_DDL = (
    "CREATE TABLE IF NOT EXISTS smart_bin_schema_versions ("
    "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
    "version INTEGER NOT NULL CHECK(version >= 1), initialized_at REAL NOT NULL)",
    "CREATE UNIQUE INDEX IF NOT EXISTS smart_bin_sessions_machine_id "
    "ON sorting_sessions(machine_id, id)",
    "CREATE TABLE IF NOT EXISTS smart_bin_machines ("
    "machine_id TEXT NOT NULL PRIMARY KEY, state_revision INTEGER NOT NULL DEFAULT 0 "
    "CHECK(state_revision >= 0))",
    "CREATE TABLE IF NOT EXISTS smart_bin_policy_revisions ("
    "id TEXT NOT NULL PRIMARY KEY, machine_id TEXT NOT NULL, compiled_artifact_hash TEXT NOT NULL, "
    "policy_json TEXT NOT NULL, created_at REAL NOT NULL, "
    "UNIQUE(machine_id, id), "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id))",
    "CREATE TABLE IF NOT EXISTS smart_bin_group_keys ("
    "id TEXT NOT NULL PRIMARY KEY, kind TEXT NOT NULL, namespace TEXT NOT NULL, "
    "part_id TEXT, part_namespace TEXT, color_id TEXT, color_namespace TEXT, "
    "provenance TEXT NOT NULL CHECK(provenance IN ('known', 'unknown')))",
    "CREATE TABLE IF NOT EXISTS smart_bin_slots ("
    "id TEXT NOT NULL PRIMARY KEY, machine_id TEXT NOT NULL, layout_revision TEXT NOT NULL, "
    "layer_index INTEGER NOT NULL CHECK(layer_index >= 0), "
    "section_index INTEGER NOT NULL CHECK(section_index >= 0), "
    "bin_index INTEGER NOT NULL CHECK(bin_index >= 0), "
    "UNIQUE(machine_id, id), "
    "UNIQUE(machine_id, layout_revision, layer_index, section_index, bin_index), "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id))",
    "CREATE TABLE IF NOT EXISTS smart_bin_assignments ("
    "id TEXT NOT NULL PRIMARY KEY, machine_id TEXT NOT NULL, slot_id TEXT NOT NULL, "
    "policy_revision_id TEXT NOT NULL, "
    "group_key_id TEXT NOT NULL, routing_revision INTEGER NOT NULL "
    "CHECK(routing_revision >= 0), created_at REAL NOT NULL, "
    "UNIQUE(slot_id, policy_revision_id, group_key_id), "
    "FOREIGN KEY(machine_id, slot_id) REFERENCES smart_bin_slots(machine_id, id), "
    "FOREIGN KEY(machine_id, policy_revision_id) "
    "REFERENCES smart_bin_policy_revisions(machine_id, id), "
    "FOREIGN KEY(group_key_id) REFERENCES smart_bin_group_keys(id))",
    "CREATE TABLE IF NOT EXISTS smart_bin_containers ("
    "id TEXT NOT NULL PRIMARY KEY, machine_id TEXT NOT NULL, label TEXT, "
    "UNIQUE(machine_id, id), "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id))",
    "CREATE TABLE IF NOT EXISTS smart_bin_cycles ("
    "id TEXT NOT NULL PRIMARY KEY, machine_id TEXT NOT NULL, container_id TEXT, slot_id TEXT, "
    "opened_at REAL NOT NULL, closed_at REAL, close_reason TEXT, provenance TEXT NOT NULL, "
    "CHECK(closed_at IS NULL OR closed_at >= opened_at), "
    "UNIQUE(machine_id, id), "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id), "
    "FOREIGN KEY(machine_id, container_id) REFERENCES smart_bin_containers(machine_id, id), "
    "FOREIGN KEY(machine_id, slot_id) REFERENCES smart_bin_slots(machine_id, id))",
    "CREATE UNIQUE INDEX IF NOT EXISTS smart_bin_one_open_cycle_per_slot "
    "ON smart_bin_cycles(slot_id) WHERE slot_id IS NOT NULL AND closed_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS smart_bin_one_open_cycle_per_container "
    "ON smart_bin_cycles(container_id) WHERE container_id IS NOT NULL AND closed_at IS NULL",
    "CREATE TABLE IF NOT EXISTS smart_bin_reservations ("
    "id TEXT NOT NULL PRIMARY KEY, machine_id TEXT NOT NULL, piece_uuid TEXT NOT NULL, "
    "route_attempt TEXT NOT NULL, request_key TEXT NOT NULL UNIQUE, "
    "run_id TEXT NOT NULL, policy_revision_id TEXT NOT NULL, group_key_id TEXT NOT NULL, "
    "routing_revision INTEGER NOT NULL CHECK(routing_revision >= 0), "
    "quantity INTEGER NOT NULL CHECK(typeof(quantity) = 'integer' AND quantity > 0), "
    "intended_kind TEXT NOT NULL CHECK(intended_kind IN ('BIN', 'REJECT')), "
    "intended_slot_id TEXT, intended_cycle_id TEXT, "
    "owner_incarnation TEXT, episode_id TEXT, pocket_index INTEGER, "
    "pocket_generation INTEGER, "
    "state TEXT NOT NULL CHECK(state IN "
    "('RESERVED', 'RELEASE_INTENT', 'EXIT_CONFIRMED', 'UNCERTAIN', 'COMPLETED', 'CANCELLED')), "
    "row_revision INTEGER NOT NULL DEFAULT 0 CHECK(row_revision >= 0), "
    "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
    "UNIQUE(machine_id, piece_uuid, route_attempt), "
    "UNIQUE(machine_id, id), "
    "CHECK((intended_kind = 'BIN' AND intended_slot_id IS NOT NULL AND intended_cycle_id IS NOT NULL) "
    "OR (intended_kind = 'REJECT' AND intended_slot_id IS NULL AND intended_cycle_id IS NULL)), "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id), "
    "FOREIGN KEY(machine_id, run_id) REFERENCES sorting_sessions(machine_id, id), "
    "FOREIGN KEY(machine_id, policy_revision_id) "
    "REFERENCES smart_bin_policy_revisions(machine_id, id), "
    "FOREIGN KEY(group_key_id) REFERENCES smart_bin_group_keys(id), "
    "FOREIGN KEY(machine_id, intended_slot_id) REFERENCES smart_bin_slots(machine_id, id), "
    "FOREIGN KEY(machine_id, intended_cycle_id) REFERENCES smart_bin_cycles(machine_id, id))",
    "CREATE UNIQUE INDEX IF NOT EXISTS smart_bin_one_live_reservation_per_piece "
    "ON smart_bin_reservations(machine_id, piece_uuid) "
    "WHERE state IN ('RESERVED', 'RELEASE_INTENT', 'EXIT_CONFIRMED', 'UNCERTAIN')",
    "CREATE TRIGGER IF NOT EXISTS smart_bin_reservation_intent_immutable "
    "BEFORE UPDATE OF intended_kind, intended_slot_id, intended_cycle_id "
    "ON smart_bin_reservations WHEN "
    "NEW.intended_kind IS NOT OLD.intended_kind OR "
    "NEW.intended_slot_id IS NOT OLD.intended_slot_id OR "
    "NEW.intended_cycle_id IS NOT OLD.intended_cycle_id "
    "BEGIN SELECT RAISE(ABORT, 'reservation intended destination is immutable'); END",
    "CREATE TABLE IF NOT EXISTS smart_bin_release_attempts ("
    "id TEXT NOT NULL PRIMARY KEY, reservation_id TEXT NOT NULL, owner_incarnation TEXT NOT NULL, "
    "target_boundary TEXT NOT NULL, created_at REAL NOT NULL, "
    "UNIQUE(owner_incarnation, target_boundary), "
    "FOREIGN KEY(reservation_id) REFERENCES smart_bin_reservations(id))",
    "CREATE TABLE IF NOT EXISTS smart_bin_deliveries ("
    "id TEXT NOT NULL PRIMARY KEY, reservation_id TEXT NOT NULL UNIQUE, "
    "machine_id TEXT NOT NULL, piece_uuid TEXT NOT NULL, run_id TEXT NOT NULL, "
    "policy_revision_id TEXT NOT NULL, group_key_id TEXT NOT NULL, "
    "quantity INTEGER NOT NULL CHECK(typeof(quantity) = 'integer' AND quantity > 0), "
    "intended_kind TEXT NOT NULL CHECK(intended_kind IN ('BIN', 'REJECT')), "
    "intended_slot_id TEXT, intended_cycle_id TEXT, "
    "actual_kind TEXT NOT NULL CHECK(actual_kind IN ('BIN', 'REJECT')), "
    "actual_slot_id TEXT, actual_cycle_id TEXT, evidence_ref TEXT NOT NULL, "
    "delivered_at REAL NOT NULL, "
    "UNIQUE(machine_id, piece_uuid), "
    "CHECK((intended_kind = 'BIN' AND intended_slot_id IS NOT NULL AND intended_cycle_id IS NOT NULL) "
    "OR (intended_kind = 'REJECT' AND intended_slot_id IS NULL AND intended_cycle_id IS NULL)), "
    "CHECK((actual_kind = 'BIN' AND actual_slot_id IS NOT NULL AND actual_cycle_id IS NOT NULL) "
    "OR (actual_kind = 'REJECT' AND actual_slot_id IS NULL AND actual_cycle_id IS NULL)), "
    "FOREIGN KEY(reservation_id) REFERENCES smart_bin_reservations(id), "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id), "
    "FOREIGN KEY(machine_id, run_id) REFERENCES sorting_sessions(machine_id, id), "
    "FOREIGN KEY(machine_id, policy_revision_id) "
    "REFERENCES smart_bin_policy_revisions(machine_id, id), "
    "FOREIGN KEY(group_key_id) REFERENCES smart_bin_group_keys(id), "
    "FOREIGN KEY(machine_id, intended_slot_id) REFERENCES smart_bin_slots(machine_id, id), "
    "FOREIGN KEY(machine_id, intended_cycle_id) REFERENCES smart_bin_cycles(machine_id, id), "
    "FOREIGN KEY(machine_id, actual_slot_id) REFERENCES smart_bin_slots(machine_id, id), "
    "FOREIGN KEY(machine_id, actual_cycle_id) REFERENCES smart_bin_cycles(machine_id, id))",
    f"CREATE TRIGGER IF NOT EXISTS smart_bin_delivery_matches_reservation_insert "
    f"BEFORE INSERT ON smart_bin_deliveries WHEN NOT EXISTS ("
    f"SELECT 1 FROM smart_bin_reservations r WHERE {_DELIVERY_RESERVATION_MATCH}) "
    f"BEGIN SELECT RAISE(ABORT, 'delivery does not match reservation'); END",
    f"CREATE TRIGGER IF NOT EXISTS smart_bin_delivery_matches_reservation_update "
    f"BEFORE UPDATE OF reservation_id, machine_id, piece_uuid, run_id, "
    f"policy_revision_id, group_key_id, quantity, intended_kind, "
    f"intended_slot_id, intended_cycle_id ON smart_bin_deliveries "
    f"WHEN NOT EXISTS (SELECT 1 FROM smart_bin_reservations r "
    f"WHERE {_DELIVERY_RESERVATION_MATCH}) "
    f"BEGIN SELECT RAISE(ABORT, 'delivery does not match reservation'); END",
    "CREATE TRIGGER IF NOT EXISTS smart_bin_delivery_reservation_immutable "
    "BEFORE UPDATE OF reservation_id ON smart_bin_deliveries "
    "WHEN NEW.reservation_id IS NOT OLD.reservation_id "
    "BEGIN SELECT RAISE(ABORT, 'delivery reservation identity is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS smart_bin_reservation_delivery_facts_immutable "
    "BEFORE UPDATE OF id, machine_id, piece_uuid, run_id, policy_revision_id, "
    "group_key_id, quantity, intended_kind, intended_slot_id, intended_cycle_id "
    "ON smart_bin_reservations WHEN EXISTS ("
    "SELECT 1 FROM smart_bin_deliveries WHERE reservation_id = OLD.id) AND ("
    "NEW.id IS NOT OLD.id OR NEW.machine_id IS NOT OLD.machine_id OR "
    "NEW.piece_uuid IS NOT OLD.piece_uuid OR NEW.run_id IS NOT OLD.run_id OR "
    "NEW.policy_revision_id IS NOT OLD.policy_revision_id OR "
    "NEW.group_key_id IS NOT OLD.group_key_id OR NEW.quantity IS NOT OLD.quantity OR "
    "NEW.intended_kind IS NOT OLD.intended_kind OR "
    "NEW.intended_slot_id IS NOT OLD.intended_slot_id OR "
    "NEW.intended_cycle_id IS NOT OLD.intended_cycle_id) "
    "BEGIN SELECT RAISE(ABORT, 'reservation fact is referenced by delivery'); END",
    "CREATE TRIGGER IF NOT EXISTS smart_bin_delivery_intent_immutable "
    "BEFORE UPDATE OF intended_kind, intended_slot_id, intended_cycle_id "
    "ON smart_bin_deliveries WHEN "
    "NEW.intended_kind IS NOT OLD.intended_kind OR "
    "NEW.intended_slot_id IS NOT OLD.intended_slot_id OR "
    "NEW.intended_cycle_id IS NOT OLD.intended_cycle_id "
    "BEGIN SELECT RAISE(ABORT, 'delivery intended destination is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS smart_bin_delivery_actual_immutable "
    "BEFORE UPDATE OF actual_kind, actual_slot_id, actual_cycle_id "
    "ON smart_bin_deliveries WHEN "
    "NEW.actual_kind IS NOT OLD.actual_kind OR "
    "NEW.actual_slot_id IS NOT OLD.actual_slot_id OR "
    "NEW.actual_cycle_id IS NOT OLD.actual_cycle_id "
    "BEGIN SELECT RAISE(ABORT, 'delivery evidenced destination is immutable'); END",
    "CREATE TABLE IF NOT EXISTS smart_bin_opening_balances ("
    "id TEXT NOT NULL PRIMARY KEY, cycle_id TEXT NOT NULL, group_key_id TEXT, "
    "quantity INTEGER NOT NULL CHECK(typeof(quantity) = 'integer' AND quantity >= 0), "
    "provenance TEXT NOT NULL, coverage TEXT NOT NULL, created_at REAL NOT NULL, "
    "FOREIGN KEY(cycle_id) REFERENCES smart_bin_cycles(id), "
    "FOREIGN KEY(group_key_id) REFERENCES smart_bin_group_keys(id))",
    "CREATE TABLE IF NOT EXISTS smart_bin_audit_events ("
    "id TEXT NOT NULL PRIMARY KEY, machine_id TEXT NOT NULL, reservation_id TEXT, "
    "request_key TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL, "
    "before_revision INTEGER NOT NULL CHECK(before_revision >= 0), "
    "after_revision INTEGER NOT NULL CHECK(after_revision >= before_revision), "
    "actor TEXT NOT NULL, reason TEXT NOT NULL, evidence_ref TEXT, created_at REAL NOT NULL, "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id), "
    "FOREIGN KEY(reservation_id) REFERENCES smart_bin_reservations(id))",
    "CREATE TABLE IF NOT EXISTS smart_bin_discrepancies ("
    "id TEXT NOT NULL PRIMARY KEY, machine_id TEXT NOT NULL, reservation_id TEXT, cycle_id TEXT, "
    "kind TEXT NOT NULL, status TEXT NOT NULL, evidence_ref TEXT, details_json TEXT NOT NULL, "
    "created_at REAL NOT NULL, resolved_at REAL, "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id), "
    "FOREIGN KEY(reservation_id) REFERENCES smart_bin_reservations(id), "
    "FOREIGN KEY(cycle_id) REFERENCES smart_bin_cycles(id))",
    "CREATE TABLE IF NOT EXISTS smart_bin_external_operations ("
    "id TEXT NOT NULL PRIMARY KEY, machine_id TEXT NOT NULL, reservation_id TEXT, "
    "system_name TEXT NOT NULL, operation_kind TEXT NOT NULL, "
    "request_key TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL, "
    "external_ref TEXT, status TEXT NOT NULL, result_json TEXT, "
    "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id), "
    "FOREIGN KEY(reservation_id) REFERENCES smart_bin_reservations(id))",
)


def _schema_version(conn: sqlite3.Connection) -> int:
    present = {
        (row[0], row[1]) for row in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE name LIKE 'smart_bin_%'"
        )
    }
    if ("table", "smart_bin_schema_versions") not in present:
        if present:
            raise UnsupportedSmartBinSchema("unversioned smart-bin schema objects exist")
        return 0
    rows = conn.execute("SELECT singleton, version FROM smart_bin_schema_versions").fetchall()
    if len(rows) != 1 or rows[0][0] != 1:
        raise UnsupportedSmartBinSchema("invalid smart-bin schema version record")
    if rows[0][1] != SCHEMA_VERSION:
        raise UnsupportedSmartBinSchema(
            f"unsupported smart-bin schema version {rows[0][1]}; expected {SCHEMA_VERSION}; "
            "migration is not implemented"
        )
    required = set()
    for ddl in _SCHEMA_DDL:
        tokens = ddl.split()
        kind = "table" if tokens[1] == "TABLE" else (
            "trigger" if "TRIGGER" in tokens[:3] else "index"
        )
        required.add((kind, tokens[tokens.index("EXISTS") + 1]))
    if required - present:
        raise UnsupportedSmartBinSchema("smart-bin schema version has missing objects")
    return SCHEMA_VERSION


def check_schema_version(conn: sqlite3.Connection) -> int:
    """Read the explicit ledger version without creating schema or legacy state."""
    return _schema_version(conn)


def _configure_critical_connection(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise RuntimeError("nested or already-active SQLite transaction is unsupported")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys could not be enabled")
    if conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
        raise RuntimeError("SQLite FULL synchronization could not be enabled")


def _create_schema(conn: sqlite3.Connection) -> None:
    for statement in _SCHEMA_DDL:
        conn.execute(statement)


def initialize_schema() -> int:
    """Explicitly install the inactive v2 ledger in the local-state database.

    Requires the existing sorting-session table; never initializes or migrates
    legacy state itself. All DDL and the version row commit together.
    """
    conn = _connect()
    try:
        _configure_critical_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sorting_sessions'"
        ).fetchone() is None:
            raise UnsupportedSmartBinSchema("legacy sorting_sessions schema must be initialized first")
        version = _schema_version(conn)
        if version == 0:
            _create_schema(conn)
            conn.execute(
                "INSERT INTO smart_bin_schema_versions(singleton, version, initialized_at) "
                "VALUES(1, ?, unixepoch('now'))",
                (SCHEMA_VERSION,),
            )
        conn.commit()
        return SCHEMA_VERSION
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def critical_transaction(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """Begin a FULL-synchronous, FK-enforced transaction for caller writes.

    The caller explicitly commits. Exceptions or an uncommitted normal exit
    roll back. A supplied connection stays open; its original PRAGMAs return
    after the transaction. Schema initialization must happen beforehand.
    """
    owned = conn is None
    if conn is None:
        conn = _connect()
    active_at_entry = conn.in_transaction
    original_sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    original_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    try:
        _configure_critical_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        if _schema_version(conn) != SCHEMA_VERSION:
            raise UnsupportedSmartBinSchema("smart-bin schema is not initialized")
        yield conn
        if conn.in_transaction:
            raise RuntimeError("critical transaction requires an explicit caller commit")
    finally:
        if not active_at_entry and conn.in_transaction:
            conn.rollback()
        if owned:
            conn.close()
        elif not active_at_entry:
            conn.execute(f"PRAGMA synchronous = {int(original_sync)}")
            conn.execute(f"PRAGMA foreign_keys = {int(original_fk)}")
