"""Inactive evidence disposition for completed native delivery follow-ups.

The physical owner supplies a fresh snapshot while holding the established outer
locks. This module never calls a callback, gate, motor or external provider. An
audited resolution describes evidence; it never adopts an old handoff or grants
permission to resume admission.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import smart_bins_completion_recovery as completion_recovery
from smart_bins_delivery import _readonly
from smart_bins_reconciliation import EvidenceReference
from smart_bins_service import _check_service_schema, _digest
from smart_bins_storage import critical_transaction


SCHEMA_VERSION = 1
EFFECTS = ("EVENT_ENQUEUED", "RUN_RECORDER_ADOPTED", "PROGRESS_RECORDED",
           "PROGRESS_SYNC_NOTIFIED", "EVENT_CONSUMED")
PRODUCER_EFFECTS = EFFECTS[:4]
_DDL = (
    "CREATE TABLE smart_bin_followup_reconciliation_versions ("
    "singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton=1), "
    "version INTEGER NOT NULL, initialized_at REAL NOT NULL)",
    "CREATE TABLE smart_bin_followup_reconciliation_dispositions ("
    "id TEXT NOT NULL PRIMARY KEY, machine_id TEXT NOT NULL, followup_id TEXT NOT NULL, "
    "reservation_id TEXT NOT NULL, delivery_id TEXT NOT NULL, audit_id TEXT NOT NULL UNIQUE, "
    "request_key TEXT NOT NULL UNIQUE, request_json TEXT NOT NULL, owner_json TEXT NOT NULL, "
    "preview_hash TEXT NOT NULL, created_at REAL NOT NULL, "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id), "
    "FOREIGN KEY(followup_id) REFERENCES smart_bin_completion_followups(id), "
    "FOREIGN KEY(reservation_id) REFERENCES smart_bin_reservations(id), "
    "FOREIGN KEY(delivery_id) REFERENCES smart_bin_deliveries(id), "
    "FOREIGN KEY(audit_id) REFERENCES smart_bin_audit_events(id))",
    "CREATE TABLE smart_bin_followup_reconciliation_links ("
    "obligation_id TEXT NOT NULL, followup_id TEXT NOT NULL, disposition_id TEXT NOT NULL, "
    "evidence_ref TEXT NOT NULL, source_digest TEXT NOT NULL, "
    "PRIMARY KEY(followup_id,obligation_id), "
    "FOREIGN KEY(followup_id) REFERENCES smart_bin_completion_followups(id), "
    "FOREIGN KEY(disposition_id) REFERENCES smart_bin_followup_reconciliation_dispositions(id))",
    "CREATE TABLE smart_bin_followup_reconciliation_receipts ("
    "request_key TEXT NOT NULL PRIMARY KEY, action TEXT NOT NULL "
    "CHECK(action='RECONCILE_NATIVE_FOLLOWUP'), payload_hash TEXT NOT NULL, "
    "disposition_id TEXT NOT NULL UNIQUE, result_json TEXT NOT NULL, created_at REAL NOT NULL, "
    "FOREIGN KEY(disposition_id) REFERENCES smart_bin_followup_reconciliation_dispositions(id))",
)
for _table in ("dispositions", "links", "receipts"):
    for _op in ("UPDATE", "DELETE"):
        _DDL += (
            f"CREATE TRIGGER smart_bin_followup_reconciliation_{_table}_{_op.lower()} "
            f"BEFORE {_op} ON smart_bin_followup_reconciliation_{_table} "
            "BEGIN SELECT RAISE(ABORT,'follow-up reconciliation evidence is immutable'); END",
        )
_REQUIRED = {statement.split()[2] for statement in _DDL}


@dataclass(frozen=True)
class OwnerSnapshot:
    machine_id: str
    followup_id: str
    reservation_id: str
    current_owner_incarnation: str
    authority_revision: str
    observed_at: float
    expires_at: float
    current_owner: bool
    quiescent: bool
    publication_attempt_inactive: bool
    dispatch_invalidated: bool
    gate_open: bool | None
    evidence: EvidenceReference


@dataclass(frozen=True)
class EffectEvidence:
    effect: str
    status: str  # APPLIED, NOT_APPLIED, UNKNOWN, or proven NOT_REQUIRED.
    evidence: EvidenceReference
    proof_kind: str = "OWNER_OBSERVATION"
    followup_id: str = ""
    delivery_id: str = ""
    original_attempt_id: str = ""


@dataclass(frozen=True)
class HoldEvidence:
    hold_audit_id: str
    cause: str  # CALLBACKS_APPLIED, OWNER_INVALIDATED, GATE_OPEN.
    evidence: EvidenceReference


@dataclass(frozen=True)
class Resolution:
    obligation_id: str
    source_digest: str
    evidence_ref: str


@dataclass(frozen=True)
class ReconciliationRequest:
    machine_id: str
    followup_id: str
    reservation_id: str
    delivery_id: str
    request_key: str
    expected_machine_revision: int
    expected_followup_revision: int
    actor: str
    reason: str
    original_attempt_id: str
    original_marker_evidence_ref: str
    original_completion_evidence_ref: str
    effects: tuple[EffectEvidence, ...]
    holds: tuple[HoldEvidence, ...] = ()
    resolutions: tuple[Resolution, ...] = ()


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _reference(value: EvidenceReference) -> bool:
    return (isinstance(value, EvidenceReference) and _text(value.ref)
            and _text(value.provenance) and value.provenance != "unknown")


def _clock(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def check_schema(conn) -> None:
    _check_service_schema(conn)
    completion_recovery.check_schema(conn)
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'smart_bin_followup_reconciliation_%'")}
    if names != _REQUIRED:
        raise RuntimeError("follow-up reconciliation extension is absent or incomplete")
    rows = conn.execute("SELECT singleton,version FROM smart_bin_followup_reconciliation_versions")
    versions = rows.fetchall()
    if len(versions) != 1 or tuple(versions[0]) != (1, SCHEMA_VERSION):
        raise RuntimeError("unsupported follow-up reconciliation extension; no automatic migration")


def initialize_schema() -> int:
    """Explicit additive install; never called by import or runtime startup."""
    with critical_transaction() as conn:
        _check_service_schema(conn)
        completion_recovery.check_schema(conn)
        present = conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE "
                               "'smart_bin_followup_reconciliation_%' LIMIT 1").fetchone()
        if present:
            check_schema(conn)
        else:
            for statement in _DDL:
                conn.execute(statement)
            conn.execute("INSERT INTO smart_bin_followup_reconciliation_versions VALUES(1,?,?)",
                         (SCHEMA_VERSION, time.time()))
        conn.commit()
    return SCHEMA_VERSION


def _rows(conn, query, params=()):
    return [dict(row) for row in conn.execute(query, params)]


def _exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def _history(conn, followup_id):
    dispositions = _rows(conn, "SELECT * FROM smart_bin_followup_reconciliation_dispositions "
                         "WHERE followup_id=? ORDER BY created_at,id", (followup_id,))
    links = _rows(conn, "SELECT l.*,d.audit_id FROM smart_bin_followup_reconciliation_links l "
                  "JOIN smart_bin_followup_reconciliation_dispositions d ON d.id=l.disposition_id "
                  "WHERE l.followup_id=? ORDER BY l.obligation_id", (followup_id,))
    return dispositions, links


def _publication_receipts(conn, row):
    prefix = row["completion_request_key"]
    return _rows(conn, "SELECT * FROM smart_bin_completion_receipts WHERE request_key IN (?,?,?) "
                 "ORDER BY request_key", (prefix + ":publication-attempt",
                                          prefix + ":publication-succeeded", prefix + ":close"))


def _holds(conn, row):
    audits = _rows(conn, "SELECT a.*,c.result_json AS hold_result_json "
                   "FROM smart_bin_audit_events a "
                   "JOIN smart_bin_completion_receipts c ON c.request_key=a.request_key "
                   "AND c.action=a.reason WHERE a.machine_id=? AND a.reservation_id=? "
                   "AND a.reason IN ('OWNERSHIP_LOST','SIDE_EFFECT_FAILED') "
                   "ORDER BY a.created_at,a.id", (row["machine_id"], row["reservation_id"]))
    current = []
    for audit in audits:
        kind = audit["reason"]
        raw = (row["ownership_loss_reason"] if kind == "OWNERSHIP_LOST"
               else row["failure_reason"])
        if raw is not None and raw == audit["evidence_ref"]:
            try:
                recorded = json.loads(audit["hold_result_json"])
            except (TypeError, ValueError):
                recorded = {}
            phase = (recorded.get("failure_phase") if isinstance(recorded, dict)
                     and recorded.get("code") == "OK"
                     and recorded.get("followup_id") == row["id"] else None)
            if kind != "SIDE_EFFECT_FAILED" or phase not in (
                "PUBLICATION_CALLBACKS", "GATE_OPEN", "ADMISSION_RELEASE"
            ):
                phase = "UNKNOWN"
            current.append({"id": "HOLD:" + audit["id"], "kind": kind,
                            "source_digest": _digest(audit), "audit_id": audit["id"],
                            "original_reason": raw, "failure_phase": phase})
    return current


def _closure_obligation(row, receipts):
    """A delivery's normal close is separate from publication reconciliation."""
    if row["delivery_id"] is None:
        return None
    for receipt in receipts:
        if (row["state"] != "CLOSED" or receipt["action"] != "CLOSE"
            or receipt["machine_id"] != row["machine_id"]
            or receipt["request_key"] != row["completion_request_key"] + ":close"):
            continue
        try:
            result = json.loads(receipt["result_json"])
        except (TypeError, ValueError):
            continue
        if (isinstance(result, dict) and result.get("code") == "OK"
            and result.get("state") == "CLOSED"
            and result.get("followup_id") == row["id"]):
            return None
    return {"id": "CLOSURE_PENDING", "kind": "CLOSURE", "eligible": False}


def _effects(conn, row, dispositions, receipts):
    by_action = {item["action"]: item for item in receipts}
    applied_receipt = by_action.get("PUBLICATION_SUCCEEDED")
    attempted_receipt = by_action.get("PUBLICATION_ATTEMPT")
    result = {}
    for effect in EFFECTS:
        if effect in PRODUCER_EFFECTS[:2] and applied_receipt is not None:
            state, source = "APPLIED", "durable_publication_receipt"
        elif effect in PRODUCER_EFFECTS[2:] and applied_receipt is not None:
            # The old receipt proves the bounded function returned, but does
            # not record whether its optional progress tracker was installed.
            state, source = "UNKNOWN", "original_tracker_configuration_gap"
        elif effect in PRODUCER_EFFECTS and row["state"] == "DELIVERY_PENDING_PUBLICATION" and attempted_receipt is None:
            state, source = "NOT_APPLIED", "no_publication_attempt"
        else:
            state, source = "UNKNOWN", "legacy_per_effect_gap"
        claims = []
        for disposition in dispositions:
            request = json.loads(disposition["request_json"])
            claims.extend(item for item in request["effects"] if item["effect"] == effect)
        known = {claim["status"] for claim in claims if claim["status"] != "UNKNOWN"}
        if state != "UNKNOWN":
            known.add(state)
        if len(known) > 1:
            state = "CONFLICTING"
        elif known:
            state = next(iter(known))
            if source == "legacy_per_effect_gap":
                source = "attributed_reconciliation_evidence"
        result[effect] = {"status": state, "source": source,
                          "references": [claim["evidence"]["ref"] for claim in claims]}
    return result


def project_on_connection(conn, row: dict[str, Any]) -> dict[str, Any]:
    """Shared restart/current-owner assessment, without changing raw history."""
    if not _exists(conn, "smart_bin_followup_reconciliation_versions"):
        return row
    check_schema(conn)
    dispositions, links = _history(conn, row["id"])
    receipts = _publication_receipts(conn, row)
    effects = _effects(conn, row, dispositions, receipts)
    held = _holds(conn, row)
    resolved = {link["obligation_id"]: link for link in links}
    publication_required = row["state"] in ("DELIVERY_PENDING_PUBLICATION", "PUBLICATION_ATTEMPTED")
    if publication_required:
        publication_source = _digest({"followup_id": row["id"],
                                      "delivery_id": row["delivery_id"],
                                      "effects": effects,
                                      "attempt_receipt": next((r for r in receipts if r["action"] == "PUBLICATION_ATTEMPT"), None)})
    else:
        publication_source = None
    obligations = []
    if publication_required:
        obligations.append({"id": "PUBLICATION", "kind": "PUBLICATION",
                            "source_digest": publication_source,
                            "eligible": all(effects[name]["status"] in ("APPLIED", "NOT_REQUIRED")
                                            for name in PRODUCER_EFFECTS)})
    closure = _closure_obligation(row, receipts)
    if closure:
        obligations.append(closure)
    obligations += [{**item, "eligible": False} for item in held]
    if row["ownership_loss_reason"] is not None and not any(
        item["kind"] == "OWNERSHIP_LOST" for item in held
    ):
        obligations.append({"id": "UNATTRIBUTED_OWNERSHIP_HOLD", "kind": "OWNERSHIP_LOST",
                            "eligible": False})
    if row["failure_kind"] is not None and not any(
        item["kind"] == "SIDE_EFFECT_FAILED" for item in held
    ):
        obligations.append({"id": "UNATTRIBUTED_FAILURE_HOLD", "kind": "SIDE_EFFECT_FAILED",
                            "eligible": False})
    if row["state"] not in ("DELIVERY_PENDING_PUBLICATION", "PUBLICATION_ATTEMPTED",
                            "PUBLICATION_SUCCEEDED", "CLOSED"):
        obligations.append({"id": "UNSUPPORTED_FOLLOWUP_STATE", "kind": row["state"],
                            "eligible": False})
    active_discrepancies = _rows(conn, "SELECT id,kind FROM smart_bin_discrepancies "
                                 "WHERE machine_id=? AND reservation_id=? AND resolved_at IS NULL "
                                 "ORDER BY id", (row["machine_id"], row["reservation_id"]))
    remaining = [{**item, "resolution": resolved.get(item["id"])} for item in obligations
                 if item["id"] not in resolved]
    remaining += [{"id": "DISCREPANCY:" + item["id"], "kind": item["kind"]}
                  for item in active_discrepancies]
    if any(item["status"] == "CONFLICTING" for item in effects.values()):
        remaining.append({"id": "CONFLICTING_EFFECT_EVIDENCE", "kind": "EVIDENCE_CONFLICT"})
    if row["missing_evidence"]:
        remaining.append({"id": "DELIVERY_OR_HISTORY_EVIDENCE_MISSING", "kind": "DELIVERY"})
    if dispositions:
        remaining.append({"id": "OWNER_REQUALIFICATION", "kind": "OWNER"})
    row.update(effect_inventory=effects, original_obligations=obligations,
               resolved_obligations=links, remaining_obligations=remaining,
               reconciliation_history=[{"id": item["id"], "audit_id": item["audit_id"],
                                        "request_key": item["request_key"]} for item in dispositions],
               effect_coverage_gaps=[name for name, item in effects.items()
                                     if item["status"] == "UNKNOWN"],
               owner_requalification_required=bool(dispositions),
               physical_recovery_authorized=False)
    return row


def _validate(req, owner):
    if (not all(_text(value) for value in
                (req.machine_id, req.followup_id, req.reservation_id, req.delivery_id,
                 req.request_key, req.actor, req.reason, req.original_attempt_id,
                 req.original_marker_evidence_ref, req.original_completion_evidence_ref))
        or any(type(value) is not int or value < 0 for value in
               (req.expected_machine_revision, req.expected_followup_revision))):
        raise ValueError("follow-up reconciliation identity and expected revisions required")
    _digest({"request": asdict(req), "owner": asdict(owner)})


def _assess(conn, req, owner):
    check_schema(conn)
    blockers = []
    row = conn.execute("SELECT * FROM smart_bin_completion_followups WHERE machine_id=? AND id=?",
                       (req.machine_id, req.followup_id)).fetchone()
    source: dict[str, Any] = {"followup": dict(row) if row else None}
    proposal: dict[str, Any] = {"effects": {}, "resolvable": [], "resolve": [], "remaining": []}
    if row is None:
        blockers.append("FOLLOWUP_NOT_FOUND")
    else:
        machine = conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id=?",
                               (req.machine_id,)).fetchone()
        revision = machine[0] if machine else None
        source["machine_revision"] = revision
        if revision != req.expected_machine_revision or row["row_revision"] != req.expected_followup_revision:
            blockers.append("STALE_REVISION")
        now = time.time()
        if (owner.machine_id != req.machine_id or owner.followup_id != req.followup_id
            or owner.reservation_id != req.reservation_id or not _text(owner.current_owner_incarnation)
            or not _text(owner.authority_revision) or owner.current_owner is not True
            or owner.quiescent is not True or owner.publication_attempt_inactive is not True
            or owner.dispatch_invalidated is not True or not _reference(owner.evidence)
            or not _clock(owner.observed_at) or not _clock(owner.expires_at)
            or not owner.observed_at <= now < owner.expires_at
            or owner.expires_at - owner.observed_at > 5):
            blockers.append("CURRENT_OWNER_QUIESCENCE_REQUIRED")
        if (row["reservation_id"] != req.reservation_id
            or row["delivery_id"] != req.delivery_id
            or row["release_attempt_id"] != req.original_attempt_id
            or row["marker_evidence_ref"] != req.original_marker_evidence_ref):
            blockers.append("FOLLOWUP_IDENTITY_MISMATCH")
        reservation = conn.execute("SELECT * FROM smart_bin_reservations WHERE machine_id=? AND id=?",
                                   (req.machine_id, req.reservation_id)).fetchone()
        delivery = conn.execute("SELECT * FROM smart_bin_deliveries WHERE id=?", (req.delivery_id,)).fetchone()
        attempt = conn.execute("SELECT a.*,e.intent_json,e.exit_json,e.completion_json FROM "
                               "smart_bin_release_attempts a JOIN smart_bin_release_evidence e "
                               "ON e.attempt_id=a.id WHERE a.id=?", (req.original_attempt_id,)).fetchone()
        history = conn.execute("SELECT * FROM piece_records WHERE uuid=?", (row["piece_uuid"],)).fetchone()
        source.update(reservation=dict(reservation) if reservation else None,
                      delivery=dict(delivery) if delivery else None,
                      attempt=dict(attempt) if attempt else None,
                      history=dict(history) if history else None)
        assessed = completion_recovery._assess_row(conn, dict(row))
        source["assessed"] = assessed
        if (reservation is None or reservation["state"] != "COMPLETED"
            or delivery is None or delivery["machine_id"] != req.machine_id
            or delivery["reservation_id"] != req.reservation_id
            or delivery["piece_uuid"] != row["piece_uuid"]
            or attempt is None or attempt["reservation_id"] != req.reservation_id
            or history is None or assessed["missing_evidence"]):
            blockers.append("COMPLETED_MATCHING_DELIVERY_HISTORY_REQUIRED")
        try:
            completion = json.loads(row["completion_json"]) if row["completion_json"] else {}
            exit_evidence = json.loads(attempt["exit_json"]) if attempt and attempt["exit_json"] else {}
            intent_evidence = json.loads(attempt["intent_json"]) if attempt and attempt["intent_json"] else {}
        except (TypeError, ValueError):
            completion, exit_evidence, intent_evidence = {}, {}, {}
            blockers.append("ORIGINAL_EVIDENCE_MALFORMED")
        if (completion.get("completion_evidence_ref") != req.original_completion_evidence_ref
            or exit_evidence.get("marker_evidence_ref") != req.original_marker_evidence_ref
            or attempt is None or attempt["completion_json"] != row["completion_json"]):
            blockers.append("ORIGINAL_COMPLETION_EVIDENCE_MISMATCH")
        external = _rows(conn, "SELECT * FROM smart_bin_external_operations WHERE reservation_id=?",
                         (req.reservation_id,))
        source["external_operations"] = external
        if external or any(value for evidence in (completion, intent_evidence)
                           for key, value in evidence.items()
                           if key.startswith("harvest_") and key != "harvest_exception"):
            blockers.append("HARVEST_OR_EXTERNAL_OPERATION_DEFERRED")
        original_receipts = _publication_receipts(conn, row)
        source["publication_receipts"] = original_receipts
        holds = _holds(conn, row)
        source["holds"] = holds
        dispositions, links = _history(conn, row["id"])
        source["dispositions"] = dispositions
        source["links"] = links
        source["discrepancies"] = _rows(conn, "SELECT * FROM smart_bin_discrepancies "
                                        "WHERE machine_id=? AND reservation_id=? ORDER BY id",
                                        (req.machine_id, req.reservation_id))
        if len({e.effect for e in req.effects}) != len(req.effects) or any(e.effect not in EFFECTS for e in req.effects):
            blockers.append("DUPLICATE_OR_UNKNOWN_EFFECT")
        for item in req.effects:
            if (item.status not in ("APPLIED", "NOT_APPLIED", "UNKNOWN", "NOT_REQUIRED")
                or not _reference(item.evidence)
                or item.followup_id != req.followup_id
                or item.delivery_id != req.delivery_id
                or item.original_attempt_id != req.original_attempt_id
                or item.proof_kind not in ("OWNER_OBSERVATION", "ORIGINAL_TRACKER_ABSENT")
                or (item.proof_kind == "ORIGINAL_TRACKER_ABSENT"
                    and item.status != "NOT_REQUIRED")
                or (item.status == "NOT_REQUIRED" and
                    (item.effect not in ("PROGRESS_RECORDED", "PROGRESS_SYNC_NOTIFIED")
                     or item.proof_kind != "ORIGINAL_TRACKER_ABSENT"))):
                blockers.append("UNSUPPORTED_EFFECT_EVIDENCE")
        not_required = [e for e in req.effects if e.status == "NOT_REQUIRED"]
        if not_required and (len(not_required) != 2 or
                             {e.effect for e in not_required} != set(PRODUCER_EFFECTS[2:]) or
                             len({e.evidence.ref for e in not_required}) != 1):
            blockers.append("ORIGINAL_TRACKER_ABSENCE_NOT_PROVEN")
        current_effects = assessed["effect_inventory"]
        effects = {key: dict(value) for key, value in current_effects.items()}
        for item in req.effects:
            known = effects[item.effect]["status"]
            if known not in ("UNKNOWN", item.status) and item.status != "UNKNOWN":
                effects[item.effect]["status"] = "CONFLICTING"
            elif item.status != "UNKNOWN":
                effects[item.effect]["status"] = item.status
                effects[item.effect]["source"] = "proposed_attributed_evidence"
            effects[item.effect]["references"] = effects[item.effect]["references"] + [item.evidence.ref]
        if any(v["status"] == "CONFLICTING" for v in effects.values()):
            blockers.append("CONFLICTING_EFFECT_EVIDENCE")
        producer_applied = all(effects[name]["status"] in ("APPLIED", "NOT_REQUIRED")
                               for name in PRODUCER_EFFECTS)
        existing = {link["obligation_id"] for link in links}
        obligations = []
        if row["state"] in ("DELIVERY_PENDING_PUBLICATION", "PUBLICATION_ATTEMPTED"):
            obligations.append({"id": "PUBLICATION", "kind": "PUBLICATION",
                                "source_digest": _digest({"followup_id": row["id"],
                                    "delivery_id": row["delivery_id"], "effects": effects,
                                    "attempt_receipt": next((r for r in original_receipts
                                                             if r["action"] == "PUBLICATION_ATTEMPT"), None)}),
                                "eligible": producer_applied})
        closure = _closure_obligation(row, original_receipts)
        if closure:
            obligations.append(closure)
        hold_evidence = {item.hold_audit_id: item for item in req.holds}
        if len(hold_evidence) != len(req.holds):
            blockers.append("DUPLICATE_HOLD_EVIDENCE")
        for hold in holds:
            witness = hold_evidence.get(hold["audit_id"])
            eligible = False
            if witness and _reference(witness.evidence):
                # Receipt metadata records the original failed operation. The
                # caller's proposed cause cannot substitute for that record.
                gate_failure_possible = (row["state"] == "CLOSED" and
                                         {"PUBLICATION_SUCCEEDED", "CLOSE"}.issubset(
                                             {r["action"] for r in original_receipts}))
                eligible = ((hold["kind"] == "OWNERSHIP_LOST" and
                             witness.cause == "OWNER_INVALIDATED" and owner.dispatch_invalidated)
                            or (hold["kind"] == "SIDE_EFFECT_FAILED" and
                                ((hold["failure_phase"] == "PUBLICATION_CALLBACKS"
                                  and witness.cause == "CALLBACKS_APPLIED" and producer_applied)
                                 or (hold["failure_phase"] == "GATE_OPEN"
                                     and witness.cause == "GATE_OPEN" and gate_failure_possible
                                     and owner.gate_open is True))))
            obligations.append({**hold, "eligible": eligible})
        if set(hold_evidence) - {item["audit_id"] for item in holds}:
            blockers.append("UNSUPPORTED_HOLD_EVIDENCE")
        requested = {item.obligation_id: item for item in req.resolutions}
        if len(requested) != len(req.resolutions):
            blockers.append("DUPLICATE_RESOLUTION")
        available_refs = {item.evidence.ref for item in req.effects}
        available_refs.update(item.evidence.ref for item in req.holds)
        available_refs.add(owner.evidence.ref)
        available_refs.update(r["request_key"] for r in original_receipts)
        named = {item["id"]: item for item in obligations}
        for item in req.resolutions:
            target = named.get(item.obligation_id)
            if (target is None or target["id"] in existing or not target["eligible"]
                or item.source_digest != target["source_digest"]
                or item.evidence_ref not in available_refs):
                blockers.append("UNSUPPORTED_OR_STALE_RESOLUTION")
        proposal = {"effects": effects, "resolvable": [item for item in obligations
                    if item["eligible"] and item["id"] not in existing],
                    "resolve": [item.obligation_id for item in req.resolutions],
                    "remaining": [item for item in obligations if item["id"] not in existing
                                  and item["id"] not in requested],
                    "quantity_effect": 0,
                    "coverage_gaps": [name for name, item in effects.items()
                                      if item["status"] == "UNKNOWN"],
                    "owner_requalification_required": True,
                    "physical_recovery_authorized": False}
        if source["discrepancies"]:
            proposal["remaining"] += [{"id": "DISCREPANCY:" + d["id"], "kind": d["kind"]}
                                       for d in source["discrepancies"] if d["resolved_at"] is None]
        if any(item["status"] == "CONFLICTING" for item in effects.values()):
            proposal["remaining"].append({"id": "CONFLICTING_EFFECT_EVIDENCE",
                                          "kind": "EVIDENCE_CONFLICT"})
        proposal["remaining"].append({"id": "OWNER_REQUALIFICATION", "kind": "OWNER"})
    result = {"code": "REFUSED" if blockers else "READY", "blockers": sorted(set(blockers)),
              "followup_id": req.followup_id, "reservation_id": req.reservation_id,
              "delivery_id": req.delivery_id, "origin": "native_followup_reconciliation",
              **proposal, "snapshot_hash": _digest(source)}
    result["preview_hash"] = _digest({"request": asdict(req), "owner": asdict(owner),
                                     "assessment": result})
    return result


def preview(request: ReconciliationRequest, owner: OwnerSnapshot) -> dict[str, Any]:
    _validate(request, owner)
    with _readonly() as conn:
        return _assess(conn, request, owner)


def _receipt(conn, key, payload_hash):
    row = conn.execute("SELECT payload_hash,result_json FROM smart_bin_followup_reconciliation_receipts "
                       "WHERE request_key=?", (key,)).fetchone()
    if row:
        return json.loads(row[1]) if row[0] == payload_hash else {"code": "IDEMPOTENCY_CONFLICT"}
    if conn.execute("SELECT 1 FROM smart_bin_audit_events WHERE request_key=?", (key,)).fetchone():
        return {"code": "IDEMPOTENCY_CONFLICT"}
    return None


def apply(request: ReconciliationRequest, owner: OwnerSnapshot, *,
          expected_preview_hash: str) -> dict[str, Any]:
    _validate(request, owner)
    if not _text(expected_preview_hash):
        raise ValueError("reviewed preview hash required")
    payload_hash = _digest({"action": "RECONCILE_NATIVE_FOLLOWUP", "request": asdict(request),
                            "owner": asdict(owner), "preview_hash": expected_preview_hash})
    with critical_transaction() as conn:
        check_schema(conn)
        replay = _receipt(conn, request.request_key, payload_hash)
        if replay is not None:
            conn.commit()
            return replay
        plan = _assess(conn, request, owner)
        if plan["blockers"] or plan["preview_hash"] != expected_preview_hash:
            conn.rollback()
            return {**plan, "code": "REFUSED", "blockers": sorted(set(plan["blockers"] +
                    (["STALE_PREVIEW"] if plan["preview_hash"] != expected_preview_hash else [])))}
        now, audit_id, disposition_id = time.time(), str(uuid.uuid4()), str(uuid.uuid4())
        conn.execute("INSERT INTO smart_bin_audit_events "
                     "(id,machine_id,reservation_id,request_key,payload_hash,before_revision,"
                     "after_revision,actor,reason,evidence_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (audit_id, request.machine_id, request.reservation_id, request.request_key,
                      payload_hash, request.expected_machine_revision,
                      request.expected_machine_revision + 1, request.actor, request.reason,
                      owner.evidence.ref, now))
        conn.execute("INSERT INTO smart_bin_followup_reconciliation_dispositions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (disposition_id, request.machine_id, request.followup_id, request.reservation_id,
                      request.delivery_id, audit_id, request.request_key,
                      json.dumps(asdict(request), sort_keys=True, allow_nan=False),
                      json.dumps(asdict(owner), sort_keys=True, allow_nan=False),
                      expected_preview_hash, now))
        for item in request.resolutions:
            conn.execute("INSERT INTO smart_bin_followup_reconciliation_links VALUES(?,?,?,?,?)",
                         (item.obligation_id, request.followup_id, disposition_id,
                          item.evidence_ref, item.source_digest))
        cursor = conn.execute("UPDATE smart_bin_completion_followups SET row_revision=row_revision+1,"
                              "updated_at=? WHERE machine_id=? AND id=? AND row_revision=?",
                              (now, request.machine_id, request.followup_id,
                               request.expected_followup_revision))
        if cursor.rowcount != 1:
            raise RuntimeError("follow-up changed during reconciliation")
        conn.execute("UPDATE smart_bin_machines SET state_revision=state_revision+1 WHERE machine_id=?",
                     (request.machine_id,))
        result = {**plan, "code": "OK", "action": "RECONCILE_NATIVE_FOLLOWUP",
                  "audit_id": audit_id, "disposition_id": disposition_id,
                  "followup_revision": request.expected_followup_revision + 1,
                  "state_revision": request.expected_machine_revision + 1,
                  "quantity_effect": 0, "owner_requalification_required": True,
                  "physical_recovery_authorized": False}
        conn.execute("INSERT INTO smart_bin_followup_reconciliation_receipts VALUES(?,?,?,?,?,?)",
                     (request.request_key, "RECONCILE_NATIVE_FOLLOWUP", payload_hash,
                      disposition_id, json.dumps(result, sort_keys=True, allow_nan=False), now))
        conn.commit()
        return result


def lookup_request(request_key: str) -> dict[str, Any] | None:
    with _readonly() as conn:
        check_schema(conn)
        row = conn.execute("SELECT action,payload_hash,result_json FROM "
                           "smart_bin_followup_reconciliation_receipts WHERE request_key=?",
                           (request_key,)).fetchone()
        return ({"action": row[0], "payload_hash": row[1], "result": json.loads(row[2])}
                if row else None)
