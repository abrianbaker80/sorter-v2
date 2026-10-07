"""Inactive transactional smart-bin preview, admission, lookup and safe cancellation.

The physical owner must supply a freshly qualified snapshot under its outer
locks. This module never calls hardware or adopts a runtime route. Legacy
assignment writers are not yet fenced, so this service is not runtime-ready.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable

from local_state import _connect
from irl.bin_layout import _parseLayersDict
from smart_bins_eligibility import classify_bin, fits_dimension
from smart_bins_migration import read_recorded_contents_on_connection, _check_extension
from smart_bins_storage import critical_transaction, check_schema_version


SERVICE_SCHEMA_VERSION = 2
HELD_STATES = ("RESERVED", "RELEASE_INTENT", "EXIT_CONFIRMED", "UNCERTAIN")
_CONFIG_KEYS = ("bin_layout", "bin_categories", "not_in_inventory_bins", "sorting_profile_sync")
_SERVICE_DDL = (
    "CREATE TABLE smart_bin_service_versions (singleton INTEGER NOT NULL PRIMARY KEY "
    "CHECK(singleton=1), version INTEGER NOT NULL, initialized_at REAL NOT NULL)",
    "CREATE TABLE smart_bin_request_receipts (request_key TEXT NOT NULL PRIMARY KEY, "
    "machine_id TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN "
    "('RESERVE','CANCEL','PREPARE_RELEASE','CONFIRM_EXIT','COMPLETE_NATIVE',"
    "'MARK_UNCERTAIN','RECORD_CONTRADICTION')), "
    "payload_hash TEXT NOT NULL, reservation_id TEXT NOT NULL, result_json TEXT NOT NULL, "
    "created_at REAL NOT NULL, FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id), "
    "FOREIGN KEY(reservation_id) REFERENCES smart_bin_reservations(id))",
    "CREATE TABLE smart_bin_reservation_attempt_links (reservation_id TEXT NOT NULL PRIMARY KEY, "
    "predecessor_reservation_id TEXT, FOREIGN KEY(reservation_id) REFERENCES smart_bin_reservations(id), "
    "FOREIGN KEY(predecessor_reservation_id) REFERENCES smart_bin_reservations(id))",
    "CREATE TABLE smart_bin_release_evidence ("
    "attempt_id TEXT NOT NULL PRIMARY KEY, reservation_id TEXT NOT NULL UNIQUE, "
    "intent_json TEXT NOT NULL, exit_json TEXT, completion_json TEXT, "
    "FOREIGN KEY(attempt_id) REFERENCES smart_bin_release_attempts(id), "
    "FOREIGN KEY(reservation_id) REFERENCES smart_bin_reservations(id))",
    "CREATE UNIQUE INDEX smart_bin_one_attempt_per_reservation "
    "ON smart_bin_release_attempts(reservation_id)",
    "CREATE TRIGGER smart_bin_release_attempt_immutable BEFORE UPDATE ON "
    "smart_bin_release_attempts BEGIN SELECT RAISE(ABORT, 'release attempt is immutable'); END",
    "CREATE TRIGGER smart_bin_release_attempt_no_delete BEFORE DELETE ON "
    "smart_bin_release_attempts BEGIN SELECT RAISE(ABORT, 'release attempt is immutable'); END",
    "CREATE TRIGGER smart_bin_release_evidence_immutable BEFORE UPDATE ON "
    "smart_bin_release_evidence WHEN NEW.attempt_id IS NOT OLD.attempt_id OR "
    "NEW.reservation_id IS NOT OLD.reservation_id OR NEW.intent_json IS NOT OLD.intent_json OR "
    "(OLD.exit_json IS NOT NULL AND NEW.exit_json IS NOT OLD.exit_json) OR "
    "(OLD.completion_json IS NOT NULL AND NEW.completion_json IS NOT OLD.completion_json) OR "
    "(NEW.completion_json IS NOT NULL AND NEW.exit_json IS NULL) "
    "BEGIN SELECT RAISE(ABORT, 'release evidence is immutable'); END",
    "CREATE TRIGGER smart_bin_release_evidence_no_delete BEFORE DELETE ON "
    "smart_bin_release_evidence BEGIN SELECT RAISE(ABORT, 'release evidence is immutable'); END",
    "CREATE TRIGGER smart_bin_service_reservation_facts_immutable BEFORE UPDATE OF "
    "id,machine_id,piece_uuid,route_attempt,request_key,run_id,policy_revision_id,"
    "group_key_id,routing_revision,quantity,intended_kind,intended_slot_id,"
    "intended_cycle_id,owner_incarnation,episode_id,pocket_index,pocket_generation "
    "ON smart_bin_reservations WHEN "
    "NEW.id IS NOT OLD.id OR NEW.machine_id IS NOT OLD.machine_id OR "
    "NEW.piece_uuid IS NOT OLD.piece_uuid OR NEW.route_attempt IS NOT OLD.route_attempt OR "
    "NEW.request_key IS NOT OLD.request_key OR NEW.run_id IS NOT OLD.run_id OR "
    "NEW.policy_revision_id IS NOT OLD.policy_revision_id OR "
    "NEW.group_key_id IS NOT OLD.group_key_id OR "
    "NEW.routing_revision IS NOT OLD.routing_revision OR NEW.quantity IS NOT OLD.quantity OR "
    "NEW.intended_kind IS NOT OLD.intended_kind OR "
    "NEW.intended_slot_id IS NOT OLD.intended_slot_id OR "
    "NEW.intended_cycle_id IS NOT OLD.intended_cycle_id OR "
    "NEW.owner_incarnation IS NOT OLD.owner_incarnation OR "
    "NEW.episode_id IS NOT OLD.episode_id OR "
    "NEW.pocket_index IS NOT OLD.pocket_index OR "
    "NEW.pocket_generation IS NOT OLD.pocket_generation "
    "BEGIN SELECT RAISE(ABORT, 'reservation facts are immutable'); END",
)


@dataclass(frozen=True)
class SlotQualification:
    slot_id: str
    cycle_id: str
    layout_revision: str
    enabled: bool
    reachable: bool
    not_in_inventory: bool
    count_limit: int | None
    layer_max_dimension_mm: float | None
    cycle_evidence_ref: str


@dataclass(frozen=True)
class RoutingQualification:
    machine_id: str
    policy_revision_id: str
    policy_artifact_hash: str
    routing_revision: int
    machine_state_revision: int
    config_digest: str
    external_revision: str
    allow_normal_sharing: bool
    slots: tuple[SlotQualification, ...]


@dataclass(frozen=True)
class ReservationRequest:
    piece_uuid: str
    route_attempt: str
    sorting_session_id: str
    group_key_id: str
    quantity: int = 1
    not_in_inventory: bool = False
    max_dimension_mm: float | None = None
    too_big: bool = False
    owner_incarnation: str | None = None
    episode_id: str | None = None
    pocket_index: int | None = None
    pocket_generation: int | None = None
    predecessor_reservation_id: str | None = None


@dataclass(frozen=True)
class NonDispatchEvidence:
    owner_incarnation: str
    episode_id: str
    pocket_index: int
    pocket_generation: int
    current_owner: bool
    no_release_pending: bool
    evidence_ref: str


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def configuration_digest_on_connection(conn: sqlite3.Connection) -> str:
    """Fingerprint the existing authoritative local-state routing keys."""
    rows = conn.execute("SELECT key,json_value FROM state_entries WHERE key IN (?,?,?,?) "
                        "ORDER BY key", _CONFIG_KEYS).fetchall()
    active = conn.execute("SELECT id,layout_json,updated_at FROM bin_layouts "
                          "WHERE is_active=1 ORDER BY id").fetchall()
    return _digest({"entries": [tuple(row) for row in rows],
                    "active_layouts": [tuple(row) for row in active]})


def configuration_digest() -> str:
    conn = _connect()
    try:
        conn.execute("BEGIN")
        result = configuration_digest_on_connection(conn)
        conn.rollback()
        return result
    finally:
        conn.close()


def qualification_digest(qualification: RoutingQualification) -> str:
    return _digest(asdict(qualification))


def _check_service_schema(conn: sqlite3.Connection) -> None:
    if check_schema_version(conn) != 2:
        raise RuntimeError("smart-bin ledger v2 is required")
    _check_extension(conn, required=True)
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'smart_bin_service_%' "
        "OR type='table' AND name IN ('smart_bin_request_receipts',"
        "'smart_bin_reservation_attempt_links')")}
    required = {"smart_bin_service_versions", "smart_bin_request_receipts",
                "smart_bin_reservation_attempt_links"}
    if names != required:
        raise RuntimeError("inactive smart-bin service schema is absent or incomplete")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND "
                    "name='smart_bin_service_reservation_facts_immutable'").fetchone() is None:
        raise RuntimeError("inactive smart-bin service immutability guard is missing")
    extension_tables = {"smart_bin_release_evidence"}
    extension_triggers = {"smart_bin_release_attempt_immutable", "smart_bin_release_attempt_no_delete",
                          "smart_bin_release_evidence_immutable", "smart_bin_release_evidence_no_delete"}
    found_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE "
                                                  "type='table' AND name='smart_bin_release_evidence'")}
    found_triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE "
                                                    "type='trigger' AND name IN (?,?,?,?)",
                                                    tuple(extension_triggers))}
    if found_tables != extension_tables or found_triggers != extension_triggers:
        raise RuntimeError("inactive smart-bin service v2 schema is incomplete")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND "
                    "name='smart_bin_one_attempt_per_reservation'").fetchone() is None:
        raise RuntimeError("inactive smart-bin attempt uniqueness guard is missing")
    rows = conn.execute("SELECT singleton,version FROM smart_bin_service_versions").fetchall()
    if len(rows) != 1 or tuple(rows[0]) != (1, SERVICE_SCHEMA_VERSION):
        raise RuntimeError("unsupported smart-bin service schema version; migration is not implemented")


def initialize_service_schema() -> int:
    """Explicit additive initialization; never called at import or startup."""
    with critical_transaction() as conn:
        _check_extension(conn, required=True)
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND "
            "name IN ('smart_bin_service_versions','smart_bin_request_receipts',"
            "'smart_bin_reservation_attempt_links')")}
        if present:
            _check_service_schema(conn)
        else:
            for ddl in _SERVICE_DDL:
                conn.execute(ddl)
            conn.execute("INSERT INTO smart_bin_service_versions VALUES(1,?,?)",
                         (SERVICE_SCHEMA_VERSION, time.time()))
        conn.commit()
    return SERVICE_SCHEMA_VERSION


def _refuse(code: str, **details: Any) -> dict[str, Any]:
    return {"code": code, **details}


def _rollback_refusal(conn: sqlite3.Connection, code: str, **details: Any) -> dict[str, Any]:
    conn.rollback()
    return _refuse(code, **details)


def _receipt(conn: sqlite3.Connection, request_key: str, payload_hash: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT payload_hash,result_json FROM smart_bin_request_receipts "
                       "WHERE request_key=?", (request_key,)).fetchone()
    if row is None:
        if conn.execute("SELECT 1 FROM smart_bin_audit_events WHERE request_key=?",
                        (request_key,)).fetchone():
            return _refuse("IDEMPOTENCY_CONFLICT")
        return None
    if row[0] != payload_hash:
        return _refuse("IDEMPOTENCY_CONFLICT")
    return json.loads(row[1])


def _save_receipt(conn: sqlite3.Connection, request_key: str, machine_id: str,
                  action: str, payload_hash: str, reservation_id: str,
                  result: dict[str, Any]) -> None:
    conn.execute("INSERT INTO smart_bin_request_receipts VALUES(?,?,?,?,?,?,?)",
                 (request_key, machine_id, action, payload_hash, reservation_id,
                  json.dumps(result, sort_keys=True), time.time()))


def _validate_request(request: ReservationRequest, qualification: RoutingQualification) -> None:
    if any(not isinstance(v, str) or not v for v in (
        request.piece_uuid, request.route_attempt, request.sorting_session_id,
        request.group_key_id, qualification.machine_id, qualification.policy_revision_id,
        qualification.policy_artifact_hash, qualification.config_digest,
        qualification.external_revision)):
        raise ValueError("missing reservation identity or qualification revision")
    if type(request.quantity) is not int or request.quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    if type(qualification.routing_revision) is not int or qualification.routing_revision < 0:
        raise ValueError("invalid routing revision")
    if type(qualification.machine_state_revision) is not int or qualification.machine_state_revision < 0:
        raise ValueError("invalid machine revision")
    if len({slot.slot_id for slot in qualification.slots}) != len(qualification.slots):
        raise ValueError("duplicate qualified slot")
    for slot in qualification.slots:
        if not slot.slot_id or not slot.cycle_id or not slot.layout_revision or not slot.cycle_evidence_ref:
            raise ValueError("missing slot/cycle qualification")
        if slot.count_limit is not None and (type(slot.count_limit) is not int or slot.count_limit < 0):
            raise ValueError("invalid count limit")


def _validate_context(conn: sqlite3.Connection, request: ReservationRequest,
                      qualification: RoutingQualification) -> dict[str, Any] | None:
    machine = conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id=?",
                           (qualification.machine_id,)).fetchone()
    if machine is None:
        return _refuse("UNQUALIFIED_MACHINE")
    if machine[0] != qualification.machine_state_revision:
        return _refuse("STALE_REVISION", state_revision=machine[0])
    if configuration_digest_on_connection(conn) != qualification.config_digest:
        return _refuse("STALE_CONFIGURATION")
    if _routing_layout(conn) is None:
        return _refuse("UNQUALIFIED_CONFIGURATION")
    for slot in qualification.slots:
        row = conn.execute("SELECT layout_revision,layer_index,section_index,bin_index "
                           "FROM smart_bin_slots WHERE machine_id=? AND id=?",
                           (qualification.machine_id, slot.slot_id)).fetchone()
        if (row is None or row[0] != slot.layout_revision
            or not _configured_slot_matches(conn, slot, tuple(row[1:4]))):
            return _refuse("STALE_CONFIGURATION")
    policy = conn.execute("SELECT compiled_artifact_hash FROM smart_bin_policy_revisions "
                          "WHERE machine_id=? AND id=?",
                          (qualification.machine_id, qualification.policy_revision_id)).fetchone()
    if policy is None or policy[0] != qualification.policy_artifact_hash:
        return _refuse("STALE_POLICY")
    session = conn.execute("SELECT status FROM sorting_sessions WHERE machine_id=? AND id=?",
                           (qualification.machine_id, request.sorting_session_id)).fetchone()
    if session is None or session[0] != "active":
        return _refuse("UNQUALIFIED_SESSION")
    group = conn.execute("SELECT kind,namespace,part_id,provenance FROM smart_bin_group_keys "
                         "WHERE id=?", (request.group_key_id,)).fetchone()
    if group is None or group[3] != "known" or group[0] != "category" or not group[1]:
        return _refuse("UNQUALIFIED_GROUP")
    return None


def _routing_layout(conn: sqlite3.Connection):
    row = conn.execute("SELECT json_value FROM state_entries WHERE key='bin_layout'").fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(row[0])
        return _parseLayersDict(raw) if isinstance(raw, dict) else None
    except (ValueError, TypeError, KeyError):
        return None


def _configured_slot_matches(conn: sqlite3.Connection, slot: SlotQualification,
                             coordinates: tuple[int, int, int]) -> bool:
    layout = _routing_layout(conn)
    if layout is None:
        return False
    layer_index, section_index, bin_index = coordinates
    if layer_index >= len(layout.layers):
        return False
    layer = layout.layers[layer_index]
    if section_index >= len(layer.sections) or bin_index >= len(layer.sections[section_index]):
        return False
    if slot.enabled and (not layer.enabled or not layer.section_enabled[section_index]):
        return False
    if slot.count_limit != layer.max_pieces_per_bin or slot.layer_max_dimension_mm != layer.max_dimension_mm:
        return False
    row = conn.execute("SELECT json_value FROM state_entries "
                       "WHERE key='not_in_inventory_bins'").fetchone()
    if row is None:
        configured_pool = False
    else:
        try:
            configured_pool = json.loads(row[0])[layer_index][section_index][bin_index]
        except (ValueError, TypeError, IndexError, KeyError):
            return False
        if type(configured_pool) is not bool:
            return False
    return slot.not_in_inventory == configured_pool


def _candidate(conn: sqlite3.Connection, request: ReservationRequest,
               qualification: RoutingQualification, slot: SlotQualification,
               *, misc: str) -> tuple[tuple[Any, ...], dict[str, Any]] | None:
    if not slot.enabled or not slot.reachable or slot.not_in_inventory != request.not_in_inventory:
        return None
    if request.too_big or not fits_dimension(request.max_dimension_mm, slot.layer_max_dimension_mm):
        return None
    db_slot = conn.execute("SELECT layout_revision,layer_index,section_index,bin_index "
                           "FROM smart_bin_slots WHERE machine_id=? AND id=?",
                           (qualification.machine_id, slot.slot_id)).fetchone()
    if db_slot is None or db_slot[0] != slot.layout_revision:
        return None
    if not _configured_slot_matches(conn, slot, tuple(db_slot[1:4])):
        return None
    cycle = conn.execute("SELECT slot_id,closed_at,provenance FROM smart_bin_cycles "
                         "WHERE machine_id=? AND id=?", (qualification.machine_id, slot.cycle_id)).fetchone()
    if cycle is None or cycle[0] != slot.slot_id or cycle[1] is not None:
        return None
    if cycle[2] not in ("qualified_synthetic", "observed_empty", "observed"):
        return None
    if conn.execute("SELECT 1 FROM smart_bin_migration_anchors WHERE cycle_id=?",
                    (slot.cycle_id,)).fetchone() is not None:
        return None
    projection = read_recorded_contents_on_connection(conn, slot.cycle_id)
    if projection["blocked"] or projection["machine_id"] != qualification.machine_id:
        return None
    if conn.execute("SELECT 1 FROM smart_bin_discrepancies d LEFT JOIN smart_bin_reservations r "
                    "ON r.id=d.reservation_id WHERE r.intended_cycle_id=? AND d.resolved_at IS NULL",
                    (slot.cycle_id,)).fetchone() is not None:
        return None
    if conn.execute("SELECT 1 FROM smart_bin_discrepancies WHERE cycle_id=? AND "
                    "resolved_at IS NULL", (slot.cycle_id,)).fetchone() is not None:
        return None
    if conn.execute("SELECT 1 FROM smart_bin_deliveries d JOIN smart_bin_reservations r "
                    "ON r.id=d.reservation_id WHERE d.actual_cycle_id=? AND r.state!='COMPLETED'",
                    (slot.cycle_id,)).fetchone() is not None:
        return None
    if conn.execute("SELECT 1 FROM smart_bin_discrepancies x JOIN smart_bin_deliveries d "
                    "ON d.reservation_id=x.reservation_id WHERE d.actual_cycle_id=? "
                    "AND x.resolved_at IS NULL", (slot.cycle_id,)).fetchone() is not None:
        return None
    if conn.execute("SELECT 1 FROM smart_bin_reservations WHERE intended_cycle_id=? "
                    "AND state='UNCERTAIN'", (slot.cycle_id,)).fetchone() is not None:
        return None
    recorded: dict[str, int] = {}
    for item in projection["breakdown"]:
        identity = item["identity"]
        if item["group_key_id"] is None or identity is None or identity["provenance"] != "known":
            return None
        recorded[item["group_key_id"]] = item["total"]
        if identity["kind"] != "category" or identity["part_id"] == misc:
            return None
    labels = [tuple(row) for row in conn.execute(
        "SELECT a.group_key_id,a.routing_revision,g.kind,g.part_id,g.provenance "
        "FROM smart_bin_assignments a JOIN smart_bin_group_keys g ON g.id=a.group_key_id "
        "WHERE a.machine_id=? AND a.slot_id=? AND a.policy_revision_id=? ORDER BY a.created_at,a.id",
        (qualification.machine_id, slot.slot_id, qualification.policy_revision_id))]
    if any(row[1] != qualification.routing_revision or row[2] != "category"
           or row[3] == misc or row[4] != "known" for row in labels):
        return None
    # A held destination is occupied for compatibility as well as capacity.
    # Policy/routing changes have no implicit cross-policy group mapping; an
    # old hold must be explicitly reconciled before this cycle can be reused.
    held_rows = conn.execute(
        "SELECT r.quantity,r.group_key_id,r.policy_revision_id,r.routing_revision,"
        "r.intended_kind,r.intended_slot_id,g.kind,g.namespace,g.part_id,g.provenance "
        "FROM smart_bin_reservations r LEFT JOIN smart_bin_group_keys g ON g.id=r.group_key_id "
        "WHERE r.machine_id=? AND r.intended_cycle_id=? AND r.state IN (?,?,?,?)",
        (qualification.machine_id, slot.cycle_id, *HELD_STATES)).fetchall()
    incoming_namespace = conn.execute("SELECT namespace FROM smart_bin_group_keys WHERE id=?",
                                      (request.group_key_id,)).fetchone()[0]
    held = 0
    compatible_occupancy = dict(recorded)
    for row in held_rows:
        (quantity, group_id, policy_id, routing_revision, intended_kind,
         intended_slot_id, group_kind, group_namespace, part_id, provenance) = row
        if (policy_id != qualification.policy_revision_id
            or routing_revision != qualification.routing_revision
            or intended_kind != "BIN" or intended_slot_id != slot.slot_id
            or group_kind != "category" or provenance != "known"
            or not group_namespace or group_namespace != incoming_namespace
            or part_id == misc):
            return None
        held += quantity
        compatible_occupancy[group_id] = compatible_occupancy.get(group_id, 0) + quantity
    used = projection["total"] + held
    if used < 0 or (slot.count_limit is not None and used + request.quantity > slot.count_limit):
        return None
    label_ids = tuple(row[0] for row in labels)
    category = classify_bin(request.group_key_id, label_ids, used, compatible_occupancy,
                            allow_sharing=slot.not_in_inventory or qualification.allow_normal_sharing,
                            misc=misc)
    if category.kind not in ("assigned_match", "recorded_match", "empty", "shared"):
        return None
    rank = {"assigned_match": 0, "recorded_match": 1, "empty": 2, "shared": 3}[category.kind]
    score = (rank, len(category.assignments), used, slot.slot_id)
    return score, {"slot_id": slot.slot_id, "cycle_id": slot.cycle_id,
                   "kind": category.kind, "assignments": category.assignments,
                   "existing_labels": label_ids, "used": used, "limit": slot.count_limit}


def _select(conn: sqlite3.Connection, request: ReservationRequest,
            qualification: RoutingQualification) -> dict[str, Any]:
    failure = _validate_context(conn, request, qualification)
    if failure is not None:
        return failure
    conflict = _journey_conflict(conn, request, qualification.machine_id)
    if conflict is not None:
        return _refuse(conflict)
    group = conn.execute("SELECT part_id FROM smart_bin_group_keys WHERE id=?",
                         (request.group_key_id,)).fetchone()
    if group[0] == "misc" or request.too_big:
        return {"code": "OK", "destination_kind": "REJECT", "slot_id": None,
                "cycle_id": None, "assignment_groups": ()}
    candidates = [candidate for slot in qualification.slots
                  if (candidate := _candidate(conn, request, qualification, slot, misc="misc")) is not None]
    if not candidates:
        return _refuse("NO_ELIGIBLE_BIN")
    _, selected = min(candidates, key=lambda item: item[0])
    return {"code": "OK", "destination_kind": "BIN", "slot_id": selected["slot_id"],
            "cycle_id": selected["cycle_id"], "assignment_groups": selected["assignments"],
            "existing_labels": selected["existing_labels"], "used": selected["used"],
            "limit": selected["limit"], "preference": selected["kind"]}


def preview(request: ReservationRequest, qualification: RoutingQualification) -> dict[str, Any]:
    _validate_request(request, qualification)
    with critical_transaction() as conn:
        _check_service_schema(conn)
        result = _select(conn, request, qualification)
        result = {**result, "qualification_hash": qualification_digest(qualification),
                  "qualification_state_revision": qualification.machine_state_revision}
        result.setdefault("state_revision", qualification.machine_state_revision)
        conn.commit()
        return result


def _journey_conflict(conn: sqlite3.Connection, request: ReservationRequest,
                      machine_id: str) -> str | None:
    if _unresolved_journey_discrepancy(conn, machine_id, request.piece_uuid):
        return "UNRESOLVED_DELIVERY"
    if conn.execute("SELECT 1 FROM smart_bin_deliveries WHERE machine_id=? AND piece_uuid=?",
                    (machine_id, request.piece_uuid)).fetchone():
        return "ALREADY_CREDITED"
    if conn.execute("SELECT 1 FROM smart_bin_historical_contributions WHERE machine_id=? "
                    "AND piece_uuid=?", (machine_id, request.piece_uuid)).fetchone():
        return "ALREADY_CREDITED"
    rows = conn.execute("SELECT id,state FROM smart_bin_reservations WHERE machine_id=? "
                        "AND piece_uuid=? ORDER BY rowid DESC",
                        (machine_id, request.piece_uuid)).fetchall()
    if any(row[1] == "COMPLETED" for row in rows):
        return "ALREADY_CREDITED"
    if any(row[1] in HELD_STATES for row in rows):
        return "IN_FLIGHT"
    predecessor = rows[0][0] if rows else None
    if conn.execute("SELECT 1 FROM smart_bin_reservations WHERE machine_id=? AND "
                    "piece_uuid=? AND route_attempt=?", (machine_id, request.piece_uuid,
                                                        request.route_attempt)).fetchone():
        return "ROUTE_ATTEMPT_CONFLICT"
    if predecessor != request.predecessor_reservation_id:
        return "ROUTE_ATTEMPT_CONFLICT"
    if rows and rows[0][1] != "CANCELLED":
        return "ROUTE_ATTEMPT_CONFLICT"
    return None


def _unresolved_journey_discrepancy(conn: sqlite3.Connection, machine_id: str,
                                    piece_uuid: str) -> bool:
    """Fence unresolved evidence on any reservation for this machine/piece."""
    return conn.execute(
        "SELECT 1 FROM smart_bin_discrepancies d "
        "JOIN smart_bin_reservations r ON r.id=d.reservation_id "
        "WHERE d.machine_id=? AND r.machine_id=? AND r.piece_uuid=? "
        "AND d.resolved_at IS NULL LIMIT 1",
        (machine_id, machine_id, piece_uuid)).fetchone() is not None


def reserve(request: ReservationRequest, qualification: RoutingQualification, *,
            expected_state_revision: int, expected_qualification_hash: str,
            request_key: str) -> dict[str, Any]:
    """Commit one logical destination claim; caller keeps outer owner locks."""
    _validate_request(request, qualification)
    if not request_key or not expected_qualification_hash or type(expected_state_revision) is not int:
        raise ValueError("missing request key or expected revision")
    payload_hash = _digest({"request": asdict(request), "machine_id": qualification.machine_id,
                            "policy_revision_id": qualification.policy_revision_id,
                            "routing_revision": qualification.routing_revision,
                            "expected_state_revision": expected_state_revision,
                            "expected_qualification_hash": expected_qualification_hash})
    with critical_transaction() as conn:
        _check_service_schema(conn)
        replay = _receipt(conn, request_key, payload_hash)
        if replay is not None:
            conn.commit()
            return replay
        machine = conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id=?",
                               (qualification.machine_id,)).fetchone()
        if machine is None:
            return _rollback_refusal(conn, "UNQUALIFIED_MACHINE")
        if machine[0] != expected_state_revision:
            return _rollback_refusal(conn, "STALE_REVISION", state_revision=machine[0])
        if qualification_digest(qualification) != expected_qualification_hash:
            return _rollback_refusal(conn, "STALE_CONFIGURATION")
        selected = _select(conn, request, qualification)
        if selected["code"] != "OK":
            conn.rollback()
            return selected
        reservation_id = str(uuid.uuid4())
        now = time.time()
        if selected["destination_kind"] == "BIN":
            for group_id in selected["assignment_groups"]:
                if group_id not in selected["existing_labels"]:
                    conn.execute("INSERT INTO smart_bin_assignments "
                                 "(id,machine_id,slot_id,policy_revision_id,group_key_id,"
                                 "routing_revision,created_at) VALUES(?,?,?,?,?,?,?)",
                                 (str(uuid.uuid4()), qualification.machine_id,
                                  selected["slot_id"], qualification.policy_revision_id,
                                  group_id, qualification.routing_revision, now))
        conn.execute("INSERT INTO smart_bin_reservations "
                     "(id,machine_id,piece_uuid,route_attempt,request_key,run_id,policy_revision_id,"
                     "group_key_id,routing_revision,quantity,intended_kind,intended_slot_id,"
                     "intended_cycle_id,owner_incarnation,episode_id,pocket_index,pocket_generation,"
                     "state,row_revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                     "'RESERVED',0,?,?)",
                     (reservation_id, qualification.machine_id, request.piece_uuid,
                      request.route_attempt, request_key, request.sorting_session_id,
                      qualification.policy_revision_id, request.group_key_id,
                      qualification.routing_revision, request.quantity,
                      selected["destination_kind"], selected["slot_id"], selected["cycle_id"],
                      request.owner_incarnation, request.episode_id, request.pocket_index,
                      request.pocket_generation, now, now))
        conn.execute("INSERT INTO smart_bin_reservation_attempt_links VALUES(?,?)",
                     (reservation_id, request.predecessor_reservation_id))
        new_revision = machine[0] + 1
        conn.execute("UPDATE smart_bin_machines SET state_revision=? WHERE machine_id=?",
                     (new_revision, qualification.machine_id))
        conn.execute("INSERT INTO smart_bin_audit_events "
                     "(id,machine_id,reservation_id,request_key,payload_hash,before_revision,"
                     "after_revision,actor,reason,evidence_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()), qualification.machine_id, reservation_id, request_key,
                      payload_hash, machine[0], new_revision, "smart_bins_service", "reserve",
                      None, now))
        result = {"code": "OK", "reservation_id": reservation_id,
                  "destination_kind": selected["destination_kind"],
                  "slot_id": selected["slot_id"], "cycle_id": selected["cycle_id"],
                  "state": "RESERVED", "reservation_revision": 0,
                  "state_revision": new_revision,
                  "qualification_hash": expected_qualification_hash}
        _save_receipt(conn, request_key, qualification.machine_id, "RESERVE", payload_hash,
                      reservation_id, result)
        conn.commit()
        return result


def lookup_reservation(machine_id: str, reservation_id: str) -> dict[str, Any] | None:
    with critical_transaction() as conn:
        _check_service_schema(conn)
        row = conn.execute("SELECT * FROM smart_bin_reservations WHERE machine_id=? AND id=?",
                           (machine_id, reservation_id)).fetchone()
        result = dict(row) if row is not None else None
        conn.commit()
        return result


def cancel_reserved(machine_id: str, reservation_id: str, *,
                    expected_reservation_revision: int, request_key: str,
                    evidence: NonDispatchEvidence) -> dict[str, Any]:
    """Release only an undispatched RESERVED hold on affirmative owner proof."""
    if not machine_id or not reservation_id or not request_key or type(expected_reservation_revision) is not int:
        raise ValueError("missing cancellation identity or expected revision")
    payload_hash = _digest({"machine_id": machine_id, "reservation_id": reservation_id,
                            "expected_reservation_revision": expected_reservation_revision,
                            "evidence": asdict(evidence)})
    with critical_transaction() as conn:
        _check_service_schema(conn)
        replay = _receipt(conn, request_key, payload_hash)
        if replay is not None:
            conn.commit()
            return replay
        reservation = conn.execute("SELECT * FROM smart_bin_reservations WHERE machine_id=? AND id=?",
                                   (machine_id, reservation_id)).fetchone()
        if reservation is None:
            return _rollback_refusal(conn, "NOT_FOUND")
        if reservation["state"] != "RESERVED":
            return _rollback_refusal(conn, "UNSAFE_STATE")
        if reservation["row_revision"] != expected_reservation_revision:
            return _rollback_refusal(conn, "STALE_REVISION", reservation_revision=reservation["row_revision"])
        if (not evidence.current_owner or not evidence.no_release_pending or not evidence.evidence_ref
            or not evidence.owner_incarnation or not evidence.episode_id
            or evidence.owner_incarnation != reservation["owner_incarnation"]
            or evidence.episode_id != reservation["episode_id"]
            or evidence.pocket_index != reservation["pocket_index"]
            or evidence.pocket_generation != reservation["pocket_generation"]):
            return _rollback_refusal(conn, "OWNER_EVIDENCE_REQUIRED")
        if conn.execute("SELECT 1 FROM smart_bin_release_attempts WHERE reservation_id=?",
                        (reservation_id,)).fetchone() is not None:
            return _rollback_refusal(conn, "UNSAFE_STATE")
        before = conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id=?",
                              (machine_id,)).fetchone()[0]
        now = time.time()
        conn.execute("UPDATE smart_bin_reservations SET state='CANCELLED',row_revision=row_revision+1,"
                     "updated_at=? WHERE id=? AND state='RESERVED' AND row_revision=?",
                     (now, reservation_id, expected_reservation_revision))
        after = before + 1
        conn.execute("UPDATE smart_bin_machines SET state_revision=? WHERE machine_id=?", (after, machine_id))
        conn.execute("INSERT INTO smart_bin_audit_events "
                     "(id,machine_id,reservation_id,request_key,payload_hash,before_revision,"
                     "after_revision,actor,reason,evidence_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()), machine_id, reservation_id, request_key, payload_hash,
                      before, after, "smart_bins_service", "cancel_pre_release",
                      evidence.evidence_ref, now))
        result = {"code": "OK", "reservation_id": reservation_id, "state": "CANCELLED",
                  "reservation_revision": expected_reservation_revision + 1,
                  "state_revision": after, "evidence_ref": evidence.evidence_ref}
        _save_receipt(conn, request_key, machine_id, "CANCEL", payload_hash,
                      reservation_id, result)
        conn.commit()
        return result
