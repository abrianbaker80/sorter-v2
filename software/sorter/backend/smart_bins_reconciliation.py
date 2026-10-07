"""Inactive, evidence-driven reconciliation of native claims without a follow-up.

The caller holds hardware_lifecycle_lock -> controller _operation_lock -> service
mutex, obtains a current owner snapshot, and keeps those locks through apply.
Snapshots are an authority boundary, not operator-supplied UI assertions. Production
collection is deliberately unwired. No callbacks or owner/physical locks are taken
here; a terminal reservation never authorizes release, admission or recovery.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from piece_records import recordPieceOnConnection
from smart_bins_delivery import _attempt, _custody_matches, _readonly, CustodyRef
from smart_bins_migration import read_recorded_contents_on_connection
from smart_bins_service import (
    HELD_STATES, _check_service_schema, _digest, _routing_layout,
    configuration_digest_on_connection,
)
from smart_bins_storage import critical_transaction


SCHEMA_VERSION = 1
_DDL = (
    "CREATE TABLE smart_bin_reconciliation_versions (singleton INTEGER PRIMARY KEY "
    "CHECK(singleton=1), version INTEGER NOT NULL, initialized_at REAL NOT NULL)",
    "CREATE TABLE smart_bin_reconciliations (id TEXT PRIMARY KEY NOT NULL, "
    "machine_id TEXT NOT NULL, reservation_id TEXT NOT NULL UNIQUE, "
    "audit_id TEXT NOT NULL UNIQUE, evidence_json TEXT NOT NULL, "
    "FOREIGN KEY(machine_id,reservation_id) REFERENCES smart_bin_reservations(machine_id,id), "
    "FOREIGN KEY(audit_id) REFERENCES smart_bin_audit_events(id))",
    "CREATE TABLE smart_bin_reconciliation_receipts (request_key TEXT PRIMARY KEY NOT NULL, "
    "machine_id TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN "
    "('RECONCILE_COMPLETED','RECONCILE_CANCELLED')), payload_hash TEXT NOT NULL, "
    "reconciliation_id TEXT NOT NULL UNIQUE, result_json TEXT NOT NULL, created_at REAL NOT NULL, "
    "FOREIGN KEY(request_key) REFERENCES smart_bin_audit_events(request_key), "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id), "
    "FOREIGN KEY(reconciliation_id) REFERENCES smart_bin_reconciliations(id))",
    "CREATE TABLE smart_bin_reconciliation_resolutions (discrepancy_id TEXT PRIMARY KEY NOT NULL, "
    "audit_id TEXT NOT NULL, evidence_ref TEXT NOT NULL, original_digest TEXT NOT NULL, "
    "FOREIGN KEY(discrepancy_id) REFERENCES smart_bin_discrepancies(id), "
    "FOREIGN KEY(audit_id) REFERENCES smart_bin_audit_events(id))",
)
_IMMUTABLE = ("smart_bin_reconciliations", "smart_bin_reconciliation_receipts",
              "smart_bin_reconciliation_resolutions")
_DDL += tuple(
    f"CREATE TRIGGER {table}_{op.lower()} BEFORE {op} ON {table} "
    "BEGIN SELECT RAISE(ABORT,'reconciliation evidence is immutable'); END"
    for table in _IMMUTABLE for op in ("UPDATE", "DELETE")
)
_REQUIRED = {ddl.split()[2] for ddl in _DDL}


@dataclass(frozen=True)
class EvidenceReference:
    ref: str
    provenance: str


@dataclass(frozen=True)
class OwnerSnapshot:
    machine_id: str
    reservation_id: str
    current_owner_incarnation: str
    authority_revision: str
    observed_at: float
    expires_at: float
    current_owner: bool
    quiescent: bool
    dispatch_invalidated: bool
    invalidated_attempt_id: str | None
    evidence: EvidenceReference


@dataclass(frozen=True)
class CycleMembership:
    """Attributable observation of cycle identity at this slot during the event.

    Coordinates or a current attachment alone are insufficient. Historical
    containment is supplied by the evidence collector and retained verbatim.
    """
    machine_id: str
    slot_id: str
    cycle_id: str
    valid_from: float
    valid_through: float
    evidence: EvidenceReference


@dataclass(frozen=True)
class PieceMetadata:
    # Group/category IDs are never substituted for these raw provider IDs.
    part_id: str | None = None
    part_namespace: str | None = None
    color_id: str | None = None
    color_namespace: str | None = None
    category_id: str | None = None
    classification_status: str | None = None
    runtime_run_id: str | None = None
    created_at: float | None = None
    provenance: str = "unknown"
    evidence: EvidenceReference | None = None


@dataclass(frozen=True)
class DispositionEvidence:
    machine_id: str
    reservation_id: str
    custody: CustodyRef
    # DELIVERY, NON_DISPATCH, or REMOVED_BEFORE_CONTENTS; no inference from absence.
    kind: str
    quantity: int | None
    entire_claim: bool
    references: tuple[EvidenceReference, ...]
    occurred_at: float | None = None
    actual_kind: str | None = None
    actual_slot_id: str | None = None
    actual_cycle_id: str | None = None
    membership: CycleMembership | None = None
    never_recorded_as_contents: bool = False
    cannot_still_arrive: bool = False
    metadata: PieceMetadata = PieceMetadata()
    harvest_allocation_ref: str | None = None


@dataclass(frozen=True)
class DiscrepancyResolution:
    discrepancy_id: str
    expected_digest: str
    evidence_ref: str


@dataclass(frozen=True)
class ReconciliationRequest:
    machine_id: str
    reservation_id: str
    request_key: str
    expected_reservation_revision: int
    expected_machine_revision: int
    outcome: str
    actor: str
    reason: str
    evidence: DispositionEvidence
    resolutions: tuple[DiscrepancyResolution, ...] = ()


def _exists(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def check_schema(conn) -> None:
    _check_service_schema(conn)
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'smart_bin_reconciliation%' "
        "AND name NOT LIKE 'sqlite_%'")}
    if names != _REQUIRED:
        raise RuntimeError("reconciliation extension is absent or incomplete")
    rows = conn.execute("SELECT singleton,version FROM smart_bin_reconciliation_versions").fetchall()
    if len(rows) != 1 or tuple(rows[0]) != (1, SCHEMA_VERSION):
        raise RuntimeError("unsupported reconciliation extension; no automatic migration")


def initialize_schema() -> int:
    """Explicit additive install only. History schema must also be prepared outside apply."""
    with critical_transaction() as conn:
        _check_service_schema(conn)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE "
                        "'smart_bin_reconciliation%' LIMIT 1").fetchone():
            check_schema(conn)
        else:
            for ddl in _DDL:
                conn.execute(ddl)
            conn.execute("INSERT INTO smart_bin_reconciliation_versions VALUES(1,?,?)",
                         (SCHEMA_VERSION, time.time()))
        conn.commit()
    return SCHEMA_VERSION


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _ref(value) -> bool:
    return (isinstance(value, EvidenceReference) and _text(value.ref)
            and _text(value.provenance) and value.provenance != "unknown")


def _time(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _validate_request(request, owner) -> None:
    if (not all(_text(v) for v in (request.machine_id, request.reservation_id,
                                   request.request_key, request.actor, request.reason))
        or request.outcome not in ("COMPLETED", "CANCELLED")
        or any(type(v) is not int or v < 0 for v in (
            request.expected_reservation_revision, request.expected_machine_revision))):
        raise ValueError("reconciliation identity, revisions, actor, reason and outcome required")
    # Reject noncanonical/nonfinite data before opening a database connection.
    _digest({"request": asdict(request), "owner": asdict(owner)})


def _receipt(conn, key, payload_hash):
    row = conn.execute("SELECT payload_hash,result_json FROM smart_bin_reconciliation_receipts "
                       "WHERE request_key=?", (key,)).fetchone()
    if row:
        return (json.loads(row[1]) if row[0] == payload_hash
                else {"code": "IDEMPOTENCY_CONFLICT"})
    if conn.execute("SELECT 1 FROM smart_bin_audit_events WHERE request_key=?", (key,)).fetchone():
        return {"code": "IDEMPOTENCY_CONFLICT"}
    return None


def _rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params)]


def _assess(conn, request, owner):
    """One snapshot for preview and apply, including writes by unfenced legacy writers."""
    check_schema(conn)
    evidence = request.evidence
    row = conn.execute("SELECT * FROM smart_bin_reservations WHERE machine_id=? AND id=?",
                       (request.machine_id, request.reservation_id)).fetchone()
    blockers: list[str] = []
    remaining: list[dict[str, Any]] = []
    effects: dict[str, dict[str, int]] = {}
    addressed = []
    source: dict[str, Any] = {"reservation": dict(row) if row else None}
    if row is None:
        blockers.append("NOT_FOUND")
    else:
        machine = conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id=?",
                               (request.machine_id,)).fetchone()[0]
        source["machine_revision"] = machine
        if row["state"] not in ("RELEASE_INTENT", "EXIT_CONFIRMED", "UNCERTAIN"):
            blockers.append("TERMINAL_OR_UNSUPPORTED_STATE_USE_CONTRADICTION_BOUNDARY")
        if (row["row_revision"] != request.expected_reservation_revision
            or machine != request.expected_machine_revision):
            blockers.append("STALE_REVISION")
        attempt = _attempt(conn, row["id"])
        source["attempt"] = dict(attempt) if attempt else None
        if row["state"] in ("RELEASE_INTENT", "EXIT_CONFIRMED") and attempt is None:
            blockers.append("RELEASE_ATTEMPT_EVIDENCE_MISSING")
        if row["state"] == "EXIT_CONFIRMED" and (attempt is None or not attempt["exit_json"]):
            blockers.append("EXIT_EVIDENCE_MISSING")
        now = time.time()
        if (owner.machine_id != request.machine_id or owner.reservation_id != row["id"]
            or not _text(owner.current_owner_incarnation) or not _text(owner.authority_revision)
            or owner.current_owner is not True or owner.quiescent is not True
            or owner.dispatch_invalidated is not True or not _ref(owner.evidence)
            or not _time(owner.observed_at) or not _time(owner.expires_at)
            or not owner.observed_at <= now < owner.expires_at
            or owner.expires_at - owner.observed_at > 5
            or owner.invalidated_attempt_id != (attempt["id"] if attempt else None)):
            blockers.append("CURRENT_OWNER_INVALIDATION_REQUIRED")
        if (evidence.machine_id != request.machine_id or evidence.reservation_id != row["id"]
            or not _custody_matches(row, evidence.custody)
            or not evidence.references or not all(_ref(ref) for ref in evidence.references)):
            blockers.append("ATTRIBUTABLE_EVIDENCE_REQUIRED")
        if (type(evidence.quantity) is not int or evidence.quantity != row["quantity"]
            or evidence.entire_claim is not True):
            blockers.append("ENTIRE_QUANTITY_REQUIRED")
        followups = (_rows(conn, "SELECT * FROM smart_bin_completion_followups WHERE reservation_id=?",
                           (row["id"],)) if _exists(conn, "smart_bin_completion_followups") else [])
        source["followups"] = followups
        if followups:
            blockers.append("COMPLETION_FOLLOWUP_DEFERRED")
        external = _rows(conn, "SELECT * FROM smart_bin_external_operations WHERE reservation_id=?",
                         (row["id"],))
        source["external_operations"] = external
        intent = json.loads(attempt["intent_json"]) if attempt else {}
        original = intent.get("evidence", {})
        if (external or evidence.harvest_allocation_ref is not None
            or original.get("harvest_allocation_ref") is not None or original.get("harvest_metadata")):
            blockers.append("HARVEST_OR_EXTERNAL_OPERATION_DEFERRED")
        credited = _rows(conn, "SELECT * FROM smart_bin_deliveries WHERE machine_id=? AND piece_uuid=?",
                         (request.machine_id, row["piece_uuid"]))
        historical = _rows(conn, "SELECT * FROM smart_bin_historical_contributions "
                           "WHERE machine_id=? AND piece_uuid=?", (request.machine_id, row["piece_uuid"]))
        source.update(credited=credited, historical=historical)
        if credited or historical or (attempt and attempt["completion_json"] is not None):
            blockers.append("ALREADY_CREDITED_OR_CONFLICTING_DELIVERY")
        legacy = _rows(conn, "SELECT e.* FROM piece_events e JOIN sorting_sessions s "
                       "ON s.id=e.session_id WHERE s.machine_id=? AND e.piece_uuid=?",
                       (request.machine_id, row["piece_uuid"]))
        history = (_rows(conn, "SELECT * FROM piece_records WHERE uuid=?", (row["piece_uuid"],))
                   if _exists(conn, "piece_records") else [])
        source.update(legacy=legacy, history=history)
        if legacy or any(item["recorded_at"] is not None or any(item[c] is not None
                         for c in ("bin_x", "bin_y", "bin_z")) for item in history):
            blockers.append("LEGACY_CONTENTS_OVERLAP_DEFERRED")
        if any(item["machine_id"] not in (None, request.machine_id) for item in history):
            blockers.append("CROSS_MACHINE_HISTORY")
        cycles = sorted({v for v in (row["intended_cycle_id"], evidence.actual_cycle_id) if v})
        source["cycles"] = {}
        for cycle_id in cycles:
            cycle = conn.execute("SELECT * FROM smart_bin_cycles WHERE id=?", (cycle_id,)).fetchone()
            if cycle is None or cycle["machine_id"] != request.machine_id:
                blockers.append("CROSS_MACHINE_OR_MISSING_CYCLE")
                continue
            balances = _rows(conn, "SELECT * FROM smart_bin_opening_balances WHERE cycle_id=?",
                             (cycle_id,))
            anchors = _rows(conn, "SELECT * FROM smart_bin_migration_anchors WHERE cycle_id=?",
                            (cycle_id,))
            projection = read_recorded_contents_on_connection(conn, cycle_id)
            source["cycles"][cycle_id] = dict(cycle=dict(cycle), balances=balances,
                                               anchors=anchors, projection=projection)
            if anchors or any(b["quantity"] > 0 for b in balances):
                blockers.append("OPENING_BALANCE_OR_LEGACY_OVERLAP_DEFERRED")
        issues = _rows(conn, "SELECT * FROM smart_bin_discrepancies WHERE machine_id=? ORDER BY id",
                       (request.machine_id,))
        source["discrepancies"] = issues
        refs = {ref.ref for ref in evidence.references}
        by_id = {item["id"]: item for item in issues}
        if len({item.discrepancy_id for item in request.resolutions}) != len(request.resolutions):
            blockers.append("DUPLICATE_RESOLUTION")
        for resolution in request.resolutions:
            issue = by_id.get(resolution.discrepancy_id)
            if (issue is None or issue["reservation_id"] != row["id"]
                or issue["resolved_at"] is not None or _digest(issue) != resolution.expected_digest
                or resolution.evidence_ref not in refs
                or issue["kind"] not in ("NATIVE_UNCERTAIN", "NATIVE_DESTINATION_MISMATCH")):
                blockers.append("UNSUPPORTED_OR_STALE_DISCREPANCY_RESOLUTION")
            else:
                addressed.append(issue["id"])
        for issue in issues:
            if issue["resolved_at"] is not None:
                continue
            if issue["reservation_id"] == row["id"] and issue["kind"] == "NATIVE_DESTINATION_MISMATCH":
                details = json.loads(issue["details_json"])
                if (request.outcome != "COMPLETED" or
                    any(details.get(key) != getattr(evidence, key) for key in
                        ("actual_kind", "actual_slot_id", "actual_cycle_id"))
                    or details.get("custody") != asdict(evidence.custody)):
                    blockers.append("CONFLICTING_DELIVERY_EVIDENCE")
            elif issue["reservation_id"] == row["id"] and issue["kind"] != "NATIVE_UNCERTAIN":
                blockers.append("UNSUPPORTED_CONFLICTING_EVIDENCE")
            if issue["id"] not in addressed:
                remaining.append({"discrepancy_id": issue["id"], "kind": issue["kind"]})
        if row["intended_cycle_id"]:
            effects[row["intended_cycle_id"]] = {"held_delta": -row["quantity"], "contents_delta": 0}
        if request.outcome == "CANCELLED":
            if (evidence.kind not in ("NON_DISPATCH", "REMOVED_BEFORE_CONTENTS")
                or evidence.never_recorded_as_contents is not True
                or evidence.cannot_still_arrive is not True
                or evidence.actual_kind is not None or evidence.actual_slot_id is not None
                or evidence.actual_cycle_id is not None or evidence.membership is not None):
                blockers.append("AFFIRMATIVE_NON_DELIVERY_REQUIRED")
            if evidence.kind == "NON_DISPATCH" and attempt and attempt["exit_json"]:
                blockers.append("NON_DISPATCH_CONFLICTS_WITH_EXIT")
        else:
            _assess_delivery(conn, request, row, attempt, source, blockers, effects, remaining)
    result = {"code": "REFUSED" if blockers else "READY", "blockers": sorted(set(blockers)),
              "reservation_id": request.reservation_id, "outcome": request.outcome,
              "quantity_effects": effects, "affected_cycles": sorted(effects),
              "addressed_discrepancies": addressed, "remaining_blockers": remaining,
              "origin": "native_reconciliation", "owner_requalification_required": True,
              "physical_recovery_authorized": False, "snapshot_hash": _digest(source)}
    result["preview_hash"] = _digest({"request": asdict(request), "owner": asdict(owner),
                                     "assessment": result})
    return result


def _assess_delivery(conn, request, row, attempt, source, blockers, effects, remaining):
    evidence = request.evidence
    if evidence.kind != "DELIVERY":
        blockers.append("DELIVERY_EVIDENCE_REQUIRED")
    # The accepted delivery schema requires an event time. Missing time is an
    # explicit blocker, never replaced by the reconciliation/audit clock.
    if (not _time(evidence.occurred_at) or evidence.occurred_at > time.time()
        or evidence.occurred_at < row["created_at"]
        or (attempt is not None and evidence.occurred_at < attempt["created_at"])):
        blockers.append("EVIDENCED_DELIVERY_TIME_REQUIRED")
    if attempt and attempt["exit_json"] and _time(evidence.occurred_at):
        if evidence.occurred_at < json.loads(attempt["exit_json"])["confirmed_at"]:
            blockers.append("DELIVERY_PRECEDES_CONFIRMED_EXIT")
    metadata = evidence.metadata
    has_metadata = any(getattr(metadata, key) is not None for key in
                       ("part_id", "part_namespace", "color_id", "color_namespace", "category_id",
                        "classification_status", "runtime_run_id", "created_at"))
    if (has_metadata and (metadata.provenance == "unknown" or not _ref(metadata.evidence))
        or (metadata.part_id is not None and not _text(metadata.part_namespace))
        or (metadata.color_id is not None and not _text(metadata.color_namespace))
        or (metadata.created_at is not None and not _time(metadata.created_at))):
        blockers.append("SOURCE_METADATA_PROVENANCE_REQUIRED")
    if not _exists(conn, "piece_records"):
        blockers.append("HISTORY_SCHEMA_NOT_INITIALIZED")
    if evidence.actual_kind == "REJECT":
        if any(v is not None for v in (evidence.actual_slot_id, evidence.actual_cycle_id, evidence.membership)):
            blockers.append("EXPLICIT_REJECT_REQUIRED")
        return
    if evidence.actual_kind != "BIN" or not evidence.actual_slot_id or not evidence.actual_cycle_id:
        blockers.append("EVIDENCED_ACTUAL_DESTINATION_REQUIRED")
        return
    slot = conn.execute("SELECT * FROM smart_bin_slots WHERE machine_id=? AND id=?",
                        (request.machine_id, evidence.actual_slot_id)).fetchone()
    cycle_source = source["cycles"].get(evidence.actual_cycle_id)
    membership = evidence.membership
    if (slot is None or cycle_source is None or membership is None
        or (membership.machine_id, membership.slot_id, membership.cycle_id) !=
        (request.machine_id, evidence.actual_slot_id, evidence.actual_cycle_id)
        or not _ref(membership.evidence)
        or not _time(membership.valid_from) or not _time(membership.valid_through)
        or not _time(evidence.occurred_at)
        or not membership.valid_from <= evidence.occurred_at <= membership.valid_through):
        blockers.append("HISTORICAL_CYCLE_MEMBERSHIP_REQUIRED")
        return
    cycle = cycle_source["cycle"]
    if (evidence.occurred_at < cycle["opened_at"]
        or (cycle["closed_at"] is not None and evidence.occurred_at > cycle["closed_at"])):
        blockers.append("DELIVERY_OUTSIDE_CYCLE_LIFETIME")
    effects.setdefault(evidence.actual_cycle_id, {"held_delta": 0, "contents_delta": 0})[
        "contents_delta"] += row["quantity"]
    held = _rows(conn, "SELECT * FROM smart_bin_reservations WHERE intended_cycle_id=? "
                 "AND state IN (?,?,?,?) AND id<>? ORDER BY id",
                 (evidence.actual_cycle_id, *HELD_STATES, row["id"]))
    assignments = _rows(conn, "SELECT * FROM smart_bin_assignments WHERE slot_id=? ORDER BY id",
                        (evidence.actual_slot_id,))
    source.update(slot=dict(slot), held=held, assignments=assignments,
                  config_digest=configuration_digest_on_connection(conn))
    reasons = ["ALLOCATION_REQUALIFICATION_REQUIRED"]
    if cycle["slot_id"] != evidence.actual_slot_id or cycle["closed_at"] is not None:
        reasons.append("HISTORICALLY_DETACHED_OR_CLOSED")
    layout = _routing_layout(conn)
    try:
        layer = layout.layers[slot["layer_index"]]
        layer.sections[slot["section_index"]][slot["bin_index"]]
        if not layer.enabled or not layer.section_enabled[slot["section_index"]]:
            reasons.append("DISABLED_DESTINATION")
        used = cycle_source["projection"]["total"] + row["quantity"] + sum(r["quantity"] for r in held)
        if layer.max_pieces_per_bin is not None and used > layer.max_pieces_per_bin:
            reasons.append("OVER_CAPACITY")
    except (AttributeError, IndexError, TypeError):
        reasons.append("UNQUALIFIED_CONFIGURATION")
    groups = {r["group_key_id"] for r in held + assignments}
    groups.update(r["group_key_id"] for r in cycle_source["projection"]["breakdown"] if r["total"])
    if groups - {row["group_key_id"]}:
        reasons.append("GROUP_COMPATIBILITY_UNQUALIFIED")
    remaining.append({"kind": "RECONCILIATION_ALLOCATION_BLOCKER",
                      "cycle_id": evidence.actual_cycle_id, "reasons": reasons})


def preview(request: ReconciliationRequest, owner: OwnerSnapshot) -> dict[str, Any]:
    """Read-only proposal; no schema creation, evidence retention or authority change."""
    _validate_request(request, owner)
    with _readonly() as conn:
        return _assess(conn, request, owner)


def apply(request: ReconciliationRequest, owner: OwnerSnapshot, *,
          expected_preview_hash: str) -> dict[str, Any]:
    """Commit evidence, contents/history, outcome, revisions and receipt atomically."""
    _validate_request(request, owner)
    if not _text(expected_preview_hash):
        raise ValueError("reviewed preview hash required")
    action = "RECONCILE_" + request.outcome
    payload_hash = _digest({"action": action, "request": asdict(request), "owner": asdict(owner),
                            "expected_preview_hash": expected_preview_hash})
    with critical_transaction() as conn:
        check_schema(conn)
        replay = _receipt(conn, request.request_key, payload_hash)
        if replay is not None:
            conn.commit()
            return replay
        plan = _assess(conn, request, owner)
        if plan["blockers"] or plan["preview_hash"] != expected_preview_hash:
            conn.rollback()
            return {**plan, "code": "REFUSED", "blockers": sorted(set(
                plan["blockers"] + (["STALE_PREVIEW"] if plan["preview_hash"] != expected_preview_hash else [])))}
        row = conn.execute("SELECT * FROM smart_bin_reservations WHERE id=?",
                           (request.reservation_id,)).fetchone()
        now, audit_id, reconciliation_id = time.time(), str(uuid.uuid4()), str(uuid.uuid4())
        evidence = request.evidence
        evidence_ref = evidence.references[0].ref
        conn.execute("INSERT INTO smart_bin_audit_events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (audit_id, request.machine_id, request.reservation_id, request.request_key,
                      payload_hash, request.expected_machine_revision,
                      request.expected_machine_revision + 1, request.actor, request.reason, evidence_ref, now))
        conn.execute("INSERT INTO smart_bin_reconciliations VALUES(?,?,?,?,?)",
                     (reconciliation_id, request.machine_id, request.reservation_id, audit_id,
                      json.dumps({"request": asdict(request), "owner": asdict(owner), "preview": plan},
                                 sort_keys=True, allow_nan=False)))
        delivery_id = None
        if request.outcome == "COMPLETED":
            delivery_id = str(uuid.uuid4())
            conn.execute("INSERT INTO smart_bin_deliveries "
                         "(id,reservation_id,machine_id,piece_uuid,run_id,policy_revision_id,group_key_id,"
                         "quantity,intended_kind,intended_slot_id,intended_cycle_id,actual_kind,"
                         "actual_slot_id,actual_cycle_id,evidence_ref,delivered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (delivery_id, row["id"], row["machine_id"], row["piece_uuid"], row["run_id"],
                          row["policy_revision_id"], row["group_key_id"], row["quantity"], row["intended_kind"],
                          row["intended_slot_id"], row["intended_cycle_id"], evidence.actual_kind,
                          evidence.actual_slot_id, evidence.actual_cycle_id, evidence_ref, evidence.occurred_at))
            destination = None
            if evidence.actual_kind == "BIN":
                destination = list(conn.execute("SELECT layer_index,section_index,bin_index "
                                               "FROM smart_bin_slots WHERE id=?",
                                               (evidence.actual_slot_id,)).fetchone())
            history = asdict(evidence.metadata)
            history.update(uuid=row["piece_uuid"], destination_bin=destination,
                           distributed_at=evidence.occurred_at)
            recordPieceOnConnection(conn, history, run_id=evidence.metadata.runtime_run_id,
                                    machine_id=request.machine_id)
        for resolution in request.resolutions:
            conn.execute("INSERT INTO smart_bin_reconciliation_resolutions VALUES(?,?,?,?)",
                         (resolution.discrepancy_id, audit_id, resolution.evidence_ref, resolution.expected_digest))
            conn.execute("UPDATE smart_bin_discrepancies SET status='resolved',resolved_at=? WHERE id=?",
                         (now, resolution.discrepancy_id))
        remaining = [dict(blocker) for blocker in plan["remaining_blockers"]]
        for blocker in remaining:
            if (blocker["kind"] == "RECONCILIATION_ALLOCATION_BLOCKER"
                and "discrepancy_id" not in blocker):
                blocker["discrepancy_id"] = str(uuid.uuid4())
                conn.execute("INSERT INTO smart_bin_discrepancies "
                             "(id,machine_id,reservation_id,cycle_id,kind,status,evidence_ref,details_json,created_at) "
                             "VALUES(?,?,?,?,?,'open',?,?,?)",
                             (blocker["discrepancy_id"], request.machine_id, row["id"], blocker["cycle_id"],
                              blocker["kind"], evidence_ref, json.dumps({**blocker, "audit_id": audit_id}), now))
        cursor = conn.execute("UPDATE smart_bin_reservations SET state=?,row_revision=row_revision+1,updated_at=? "
                              "WHERE id=? AND row_revision=? AND state=?",
                              (request.outcome, now, row["id"], request.expected_reservation_revision, row["state"]))
        if cursor.rowcount != 1:
            raise RuntimeError("reservation changed during reconciliation")
        conn.execute("UPDATE smart_bin_machines SET state_revision=state_revision+1 WHERE machine_id=?",
                     (request.machine_id,))
        result = {**plan, "code": "OK", "action": action, "state": request.outcome,
                  "delivery_id": delivery_id, "audit_id": audit_id, "reconciliation_id": reconciliation_id,
                  "reservation_revision": request.expected_reservation_revision + 1,
                  "state_revision": request.expected_machine_revision + 1,
                  "remaining_blockers": remaining, "completion_followup_created": False}
        conn.execute("INSERT INTO smart_bin_reconciliation_receipts VALUES(?,?,?,?,?,?,?)",
                     (request.request_key, request.machine_id, action, payload_hash, reconciliation_id,
                      json.dumps(result, sort_keys=True, allow_nan=False), now))
        conn.commit()
        return result


def lookup_request(request_key: str) -> dict[str, Any] | None:
    with _readonly() as conn:
        check_schema(conn)
        row = conn.execute("SELECT action,payload_hash,result_json FROM smart_bin_reconciliation_receipts "
                           "WHERE request_key=?", (request_key,)).fetchone()
        return {"action": row[0], "payload_hash": row[1], "result": json.loads(row[2])} if row else None
