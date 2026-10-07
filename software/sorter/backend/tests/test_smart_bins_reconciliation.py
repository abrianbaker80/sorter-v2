"""Synthetic qualification only; fixtures set SQLite/config paths before imports."""

from test_smart_bins_delivery import prepared, connect, qualification, custody

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

import local_state
import piece_records
import smart_bins_completion_recovery as recovery
import smart_bins_delivery as delivery
import smart_bins_migration as migration
import smart_bins_reconciliation as reconciliation
import smart_bins_service as service
from smart_bins_reconciliation import (
    CycleMembership, DiscrepancyResolution, DispositionEvidence, EvidenceReference,
    OwnerSnapshot, PieceMetadata, ReconciliationRequest,
)


REF = EvidenceReference("synthetic:attributed-disposition", "synthetic owner observation")


@pytest.fixture
def ledger(prepared, monkeypatch):
    clock = [1_800_000_000.0]
    for module in (reconciliation, delivery, service):
        monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock[0]))
    reconciliation.initialize_schema()
    piece_records.initialize_piece_records()
    yield SimpleNamespace(path=prepared, clock=clock)


def released(ledger, *, state="RELEASE_INTENT", quantity=1, piece="piece", key="reserve"):
    q = qualification()
    req = service.ReservationRequest(piece, "first", "session", "A", quantity,
                                    owner_incarnation="owner", episode_id="episode",
                                    pocket_index=0, pocket_generation=1)
    result = service.reserve(req, q, expected_state_revision=q.machine_state_revision,
                             expected_qualification_hash=service.qualification_digest(q), request_key=key)
    assert result["code"] == "OK"
    now = ledger.clock[0]
    intent = delivery.prepare_release(
        "m", result["reservation_id"], expected_reservation_revision=0, request_key=key + ":intent",
        qualification=qualification(), evidence=delivery.ReleaseEvidence(
            custody(piece), 17, "BIN", "s0", "c0", True, True, "synthetic:owner", "synthetic:route", now, now + 4))
    assert intent["code"] == "OK"
    if state == "EXIT_CONFIRMED":
        ledger.clock[0] += 1
        result = delivery.confirm_exit(
            "m", result["reservation_id"], expected_reservation_revision=1, request_key=key + ":exit",
            evidence=delivery.ExitEvidence(custody(piece), intent["attempt_id"], 17, True,
                                            "synthetic:marker", ledger.clock[0]))
        assert result["code"] == "OK"
    elif state == "UNCERTAIN":
        result = delivery.mark_uncertain(
            "m", result["reservation_id"], expected_reservation_revision=1, request_key=key + ":uncertain",
            evidence=delivery.UncertaintyEvidence(piece, "owner", "acknowledgement lost",
                                                   "synthetic:uncertain", intent["attempt_id"]))
        assert result["code"] == "OK"
    ledger.clock[0] += 1
    return intent["reservation_id"]


def request_for(ledger, reservation, *, outcome="COMPLETED", slot="s0", cycle="c0", key="reconcile"):
    with connect(ledger.path) as conn:
        row = conn.execute("SELECT * FROM smart_bin_reservations WHERE id=?", (reservation,)).fetchone()
        rev = conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id='m'").fetchone()[0]
        attempt = conn.execute("SELECT id FROM smart_bin_release_attempts WHERE reservation_id=?", (reservation,)).fetchone()
    now = ledger.clock[0]
    proof = DispositionEvidence(
        "m", reservation, custody(row["piece_uuid"]), "DELIVERY" if outcome == "COMPLETED" else "REMOVED_BEFORE_CONTENTS",
        row["quantity"], True, (REF,), occurred_at=now,
        actual_kind="BIN" if outcome == "COMPLETED" else None,
        actual_slot_id=slot if outcome == "COMPLETED" else None,
        actual_cycle_id=cycle if outcome == "COMPLETED" else None,
        membership=CycleMembership("m", slot, cycle, now - 1, now, REF) if outcome == "COMPLETED" else None,
        never_recorded_as_contents=outcome == "CANCELLED", cannot_still_arrive=outcome == "CANCELLED")
    req = ReconciliationRequest("m", reservation, key, row["row_revision"], rev, outcome,
                                "synthetic recovery actor", "attributed disposition", proof)
    owner = OwnerSnapshot("m", reservation, "new-owner", "authority-generation-2", now, now + 4,
                          True, True, True, attempt[0] if attempt else None, REF)
    return req, owner


def apply(req, owner):
    plan = reconciliation.preview(req, owner)
    return reconciliation.apply(req, owner, expected_preview_hash=plan["preview_hash"])


def dump(path):
    with connect(path) as conn:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: [tuple(row) for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
                for name in names}


@pytest.mark.parametrize("state", ["RELEASE_INTENT", "EXIT_CONFIRMED", "UNCERTAIN"])
@pytest.mark.parametrize("actual", ["intended", "other", "reject"])
def test_delivery_intended_actual_reject_preserves_original_and_no_followup(ledger, state, actual):
    reservation = released(ledger, state=state, quantity=2)
    req, owner = request_for(ledger, reservation, slot="s1" if actual == "other" else "s0",
                             cycle="c1" if actual == "other" else "c0")
    if actual == "reject":
        req = replace(req, evidence=replace(req.evidence, actual_kind="REJECT", actual_slot_id=None,
                                           actual_cycle_id=None, membership=None))
    with connect(ledger.path) as conn:
        original = dict(conn.execute("SELECT * FROM smart_bin_reservations WHERE id=?", (reservation,)).fetchone())
        release = tuple(conn.execute("SELECT * FROM smart_bin_release_evidence").fetchone())
    before = dump(ledger.path)
    plan = reconciliation.preview(req, owner)
    assert plan["code"] == "READY", plan
    assert dump(ledger.path) == before
    final = reconciliation.apply(req, owner, expected_preview_hash=plan["preview_hash"])
    assert final["code"] == "OK" and final["state"] == "COMPLETED"
    assert final["physical_recovery_authorized"] is False
    assert final["completion_followup_created"] is False
    with connect(ledger.path) as conn:
        updated = dict(conn.execute("SELECT * FROM smart_bin_reservations WHERE id=?", (reservation,)).fetchone())
        credited = conn.execute("SELECT * FROM smart_bin_deliveries").fetchone()
        history = conn.execute("SELECT * FROM piece_records").fetchone()
        assert conn.execute("SELECT count(*) FROM smart_bin_completion_followups").fetchone()[0] == 0
        assert tuple(conn.execute("SELECT * FROM smart_bin_release_evidence").fetchone()) == release
        assert conn.execute("SELECT actor FROM smart_bin_audit_events WHERE id=?", (final["audit_id"],)).fetchone()[0] == req.actor
    for key in original.keys() - {"state", "row_revision", "updated_at"}:
        assert updated[key] == original[key]
    assert credited["quantity"] == 2 and credited["intended_cycle_id"] == "c0"
    assert credited["actual_cycle_id"] == (None if actual == "reject" else req.evidence.actual_cycle_id)
    assert history["run_id"] is None and history["seen_at"] is None and history["part_id"] is None
    assert history["recorded_at"] == req.evidence.occurred_at
    assert migration.read_recorded_contents("c0")["total"] == (2 if actual == "intended" else 0)
    assert migration.read_recorded_contents("c1")["total"] == (2 if actual == "other" else 0)
    report = delivery.inspect_recovery("m")
    assert report["claims"] == []
    assert any(b.get("origin") == "native_reconciliation" and b["owner_requalification_required"]
               for b in report["followup_blockers"])


def test_historical_detachment_and_disabled_over_capacity_record_truth(ledger):
    reservation = released(ledger, quantity=2)
    req, owner = request_for(ledger, reservation, slot="s1", cycle="c1")
    with connect(ledger.path) as conn:
        conn.execute("UPDATE smart_bin_cycles SET slot_id=NULL,closed_at=? WHERE id='c1'", (ledger.clock[0],))
        config = json.loads(conn.execute("SELECT json_value FROM state_entries WHERE key='bin_layout'").fetchone()[0])
        config["layers"][0].update(enabled=False, max_pieces_per_bin=1)
        conn.execute("UPDATE state_entries SET json_value=? WHERE key='bin_layout'", (json.dumps(config),))
    final = apply(req, owner)
    assert final["code"] == "OK", final
    blocker = next(b for b in final["remaining_blockers"] if b["kind"] == "RECONCILIATION_ALLOCATION_BLOCKER")
    assert set(blocker["reasons"]) >= {"OVER_CAPACITY", "DISABLED_DESTINATION", "HISTORICALLY_DETACHED_OR_CLOSED"}
    projection = migration.read_recorded_contents("c1")
    assert projection["total"] == 2 and projection["blocked"]
    # Making capacity/attachment look eligible does not erase the durable block.
    with connect(ledger.path) as conn:
        conn.execute("UPDATE smart_bin_cycles SET slot_id='s1',closed_at=NULL WHERE id='c1'")
        config["layers"][0].update(enabled=True, max_pieces_per_bin=3)
        conn.execute("UPDATE state_entries SET json_value=? WHERE key='bin_layout'", (json.dumps(config),))
        conn.execute("INSERT INTO smart_bin_discrepancies "
                     "(id,machine_id,cycle_id,kind,status,details_json,created_at) VALUES('block-c0','m','c0','independent','open','{}',1)")
    assert service.preview(service.ReservationRequest("later", "first", "session", "A"),
                           qualification(two=True))["code"] != "OK"


@pytest.mark.parametrize("kind", ["NON_DISPATCH", "REMOVED_BEFORE_CONTENTS"])
def test_cancel_releases_only_hold_and_retains_restart_blocker(ledger, kind):
    reservation = released(ledger, quantity=2)
    req, owner = request_for(ledger, reservation, outcome="CANCELLED")
    req = replace(req, evidence=replace(req.evidence, kind=kind))
    contents = migration.read_recorded_contents("c0")["total"]
    final = apply(req, owner)
    assert final["code"] == "OK" and final["state"] == "CANCELLED"
    assert final["quantity_effects"] == {"c0": {"held_delta": -2, "contents_delta": 0}}
    assert migration.read_recorded_contents("c0")["total"] == contents
    with connect(ledger.path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM piece_records").fetchone()[0] == 0
    assert delivery.inspect_recovery("m")["followup_blockers"][0]["owner_requalification_required"]


@pytest.mark.parametrize("change,code", [
    ({"quantity": None}, "ENTIRE_QUANTITY_REQUIRED"),
    ({"quantity": 1}, "ENTIRE_QUANTITY_REQUIRED"),
    ({"entire_claim": False}, "ENTIRE_QUANTITY_REQUIRED"),
    ({"references": ()}, "ATTRIBUTABLE_EVIDENCE_REQUIRED"),
    ({"references": (EvidenceReference("x", "unknown"),)}, "ATTRIBUTABLE_EVIDENCE_REQUIRED"),
    ({"machine_id": "other"}, "ATTRIBUTABLE_EVIDENCE_REQUIRED"),
    ({"actual_cycle_id": "missing"}, "CROSS_MACHINE_OR_MISSING_CYCLE"),
    ({"membership": None}, "HISTORICAL_CYCLE_MEMBERSHIP_REQUIRED"),
    ({"actual_kind": None}, "EVIDENCED_ACTUAL_DESTINATION_REQUIRED"),
    ({"occurred_at": None}, "EVIDENCED_DELIVERY_TIME_REQUIRED"),
    ({"harvest_allocation_ref": "quota"}, "HARVEST_OR_EXTERNAL_OPERATION_DEFERRED"),
    ({"metadata": PieceMetadata(part_id="A")}, "SOURCE_METADATA_PROVENANCE_REQUIRED"),
])
def test_weak_partial_cross_machine_evidence_refuses_without_changes(ledger, change, code):
    reservation = released(ledger, quantity=2)
    req, owner = request_for(ledger, reservation)
    req = replace(req, evidence=replace(req.evidence, **change))
    before = dump(ledger.path)
    result = apply(req, owner)
    assert result["code"] == "REFUSED" and code in result["blockers"], result
    assert dump(ledger.path) == before


@pytest.mark.parametrize("change", [
    {"quiescent": False}, {"current_owner": False}, {"dispatch_invalidated": False},
    {"invalidated_attempt_id": "other"}, {"current_owner_incarnation": ""},
    {"authority_revision": ""}, {"machine_id": "other"}, {"observed_at": 1},
])
def test_current_owner_snapshot_is_required(ledger, change):
    req, owner = request_for(ledger, released(ledger), outcome="CANCELLED")
    before = dump(ledger.path)
    result = apply(req, replace(owner, **change))
    assert "CURRENT_OWNER_INVALIDATION_REQUIRED" in result["blockers"]
    assert dump(ledger.path) == before


@pytest.mark.parametrize("change", [{"cannot_still_arrive": False}, {"never_recorded_as_contents": False},
                                    {"kind": "PAUSED"}, {"kind": "EMPTY_LOOKING_SLOT"}])
def test_cancellation_requires_affirmative_disposition(ledger, change):
    req, owner = request_for(ledger, released(ledger), outcome="CANCELLED")
    result = apply(replace(req, evidence=replace(req.evidence, **change)), owner)
    assert "AFFIRMATIVE_NON_DELIVERY_REQUIRED" in result["blockers"]


def test_exit_confirmation_conflicts_with_non_dispatch(ledger):
    req, owner = request_for(ledger, released(ledger, state="EXIT_CONFIRMED"), outcome="CANCELLED")
    result = apply(replace(req, evidence=replace(req.evidence, kind="NON_DISPATCH")), owner)
    assert "NON_DISPATCH_CONFLICTS_WITH_EXIT" in result["blockers"]


def test_stale_preview_cannot_overwrite_later_evidence_or_configuration(ledger):
    req, owner = request_for(ledger, released(ledger))
    plan = reconciliation.preview(req, owner)
    with connect(ledger.path) as conn:
        # Simulates a legacy writer that failed to advance the machine revision.
        conn.execute("INSERT INTO smart_bin_discrepancies "
                     "(id,machine_id,cycle_id,kind,status,details_json,created_at) VALUES('later','m','c0','later-evidence','open','{}',1)")
    before = dump(ledger.path)
    result = reconciliation.apply(req, owner, expected_preview_hash=plan["preview_hash"])
    assert result["code"] == "REFUSED" and "STALE_PREVIEW" in result["blockers"]
    assert dump(ledger.path) == before
    fresh = reconciliation.preview(req, owner)
    ledger.clock[0] += 10
    assert "CURRENT_OWNER_INVALIDATION_REQUIRED" in reconciliation.apply(
        req, owner, expected_preview_hash=fresh["preview_hash"])["blockers"]


@pytest.mark.parametrize("source", ["external", "balance", "legacy_event", "history", "followup"])
def test_separate_workflows_defer(ledger, source):
    reservation = released(ledger, state="EXIT_CONFIRMED")
    req, owner = request_for(ledger, reservation)
    with connect(ledger.path) as conn:
        if source == "external":
            conn.execute("INSERT INTO smart_bin_external_operations VALUES('external','m',?,'harvest','quota',"
                         "'external-key','hash',NULL,'planned',NULL,1,1)", (reservation,))
        elif source == "balance":
            conn.execute("INSERT INTO smart_bin_opening_balances VALUES('balance','c0','A',1,'legacy','unknown',1)")
        elif source == "legacy_event":
            conn.execute("INSERT INTO piece_events(session_id,piece_uuid,layer_index,section_index,bin_index,bin_epoch,distributed_at) "
                         "VALUES('session','piece',0,0,0,0,1)")
        elif source == "history":
            conn.execute("INSERT INTO piece_records(uuid,recorded_at) VALUES('piece',1)")
        else:
            attempt = conn.execute("SELECT id FROM smart_bin_release_attempts").fetchone()[0]
            conn.execute("INSERT INTO smart_bin_completion_followups "
                         "(id,machine_id,reservation_id,release_attempt_id,owner_incarnation,piece_uuid,custody_json,"
                         "marker_evidence_ref,intended_kind,intended_slot_id,intended_cycle_id,completion_request_key,state,created_at,updated_at) "
                         "VALUES('followup','m',?,?,'owner','piece',?,'synthetic:marker','BIN','s0','c0','finish','RETAINED',1,1)",
                         (reservation, attempt, json.dumps({})))
    before = dump(ledger.path)
    result = apply(req, owner)
    assert result["code"] == "REFUSED" and any("DEFERRED" in c for c in result["blockers"])
    assert dump(ledger.path) == before


def test_exact_replay_changed_payload_and_terminal_contradiction(ledger):
    req, owner = request_for(ledger, released(ledger))
    plan = reconciliation.preview(req, owner)
    final = reconciliation.apply(req, owner, expected_preview_hash=plan["preview_hash"])
    assert final["code"] == "OK"
    before = dump(ledger.path)
    ledger.clock[0] += 100
    assert reconciliation.apply(req, owner, expected_preview_hash=plan["preview_hash"]) == final
    assert reconciliation.lookup_request(req.request_key)["result"] == final
    assert reconciliation.apply(replace(req, reason="changed"), owner,
                                expected_preview_hash=plan["preview_hash"])["code"] == "IDEMPOTENCY_CONFLICT"
    assert dump(ledger.path) == before
    new_req, new_owner = request_for(ledger, req.reservation_id, outcome="CANCELLED", key="contradict")
    assert apply(new_req, new_owner)["code"] == "REFUSED"
    assert dump(ledger.path) == before
    retained = delivery.record_contradiction(
        "m", req.reservation_id, expected_reservation_revision=final["reservation_revision"],
        request_key="retain-contradiction", evidence=delivery.ContradictionEvidence("later observation", "synthetic:later"))
    assert retained["code"] == "OK" and retained["state"] == "COMPLETED"
    assert delivery.inspect_recovery("m")["unresolved_discrepancies"]


def test_competing_requests_have_one_terminal_outcome(ledger):
    reservation = released(ledger)
    delivery_req, owner = request_for(ledger, reservation)
    cancel_req, _ = request_for(ledger, reservation, outcome="CANCELLED", key="cancel")
    plans = [reconciliation.preview(req, owner) for req in (delivery_req, cancel_req)]
    barrier = threading.Barrier(2)
    def run(pair):
        req, plan = pair
        barrier.wait(timeout=10)
        return reconciliation.apply(req, owner, expected_preview_hash=plan["preview_hash"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, zip((delivery_req, cancel_req), plans)))
    assert sorted(result["code"] for result in results) == ["OK", "REFUSED"]
    with connect(ledger.path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_reconciliations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM smart_bin_reconciliation_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] <= 1


@pytest.mark.parametrize("table", ["piece_records", "smart_bin_reservations", "smart_bin_machines",
                                    "smart_bin_reconciliation_receipts"])
def test_injected_failure_rolls_back_every_effect(ledger, table):
    req, owner = request_for(ledger, released(ledger, state="UNCERTAIN"))
    with connect(ledger.path) as conn:
        issue = dict(conn.execute("SELECT * FROM smart_bin_discrepancies").fetchone())
    req = replace(req, resolutions=(DiscrepancyResolution(issue["id"], service._digest(issue), REF.ref),))
    plan = reconciliation.preview(req, owner)
    op = "UPDATE" if table in ("smart_bin_reservations", "smart_bin_machines") else "INSERT"
    with connect(ledger.path) as conn:
        conn.execute(f"CREATE TRIGGER injected_failure BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    before = dump(ledger.path)
    with pytest.raises(Exception, match="injected failure"):
        reconciliation.apply(req, owner, expected_preview_hash=plan["preview_hash"])
    assert dump(ledger.path) == before


def test_corrections_namespaces_and_distinct_run_identity_survive(ledger):
    req, owner = request_for(ledger, released(ledger))
    metadata = PieceMetadata("raw-3001", "ldraw", "raw-5", "bricklink", "category-A", "classified",
                             "runtime-run", ledger.clock[0] - 10, "synthetic recognition", REF)
    req = replace(req, evidence=replace(req.evidence, metadata=metadata))
    with connect(ledger.path) as conn:
        conn.execute("INSERT INTO piece_records(uuid,part_correct,color_corrected_id,part_feedback_submitted) VALUES('piece',0,'corrected',1)")
    result = apply(req, owner)
    assert result["code"] == "OK"
    with connect(ledger.path) as conn:
        history = conn.execute("SELECT * FROM piece_records").fetchone()
        native = conn.execute("SELECT * FROM smart_bin_deliveries").fetchone()
        retained = json.loads(conn.execute("SELECT evidence_json FROM smart_bin_reconciliations").fetchone()[0])
    assert (history["part_correct"], history["color_corrected_id"], history["part_feedback_submitted"]) == (0, "corrected", 1)
    assert (native["run_id"], history["run_id"], native["group_key_id"], history["part_id"]) == ("session", "runtime-run", "A", "raw-3001")
    assert retained["request"]["evidence"]["metadata"]["part_namespace"] == "ldraw"
    assert retained["request"]["evidence"]["metadata"]["color_namespace"] == "bricklink"


def test_resolution_keeps_original_details_link_and_unrelated_blockers(ledger):
    reservation = released(ledger, state="UNCERTAIN")
    req, owner = request_for(ledger, reservation, outcome="CANCELLED")
    with connect(ledger.path) as conn:
        issue = dict(conn.execute("SELECT * FROM smart_bin_discrepancies").fetchone())
        conn.execute("INSERT INTO smart_bin_discrepancies "
                     "(id,machine_id,cycle_id,kind,status,details_json,created_at) VALUES('unrelated','m','c1','independent','open','{}',1)")
    req = replace(req, resolutions=(DiscrepancyResolution(issue["id"], service._digest(issue), REF.ref),))
    final = apply(req, owner)
    assert final["code"] == "OK"
    projection = migration.read_recorded_contents("c0")
    assert projection["blocked"] is False and projection["active_discrepancies"] == []
    assert projection["discrepancies"][0]["details_json"] == issue["details_json"]
    assert projection["discrepancies"][0]["resolution"]["audit_id"] == final["audit_id"]
    assert migration.read_recorded_contents("c1")["blocked"] is True
    assert {b["discrepancy_id"] for b in final["remaining_blockers"]} == {"unrelated"}
    with connect(ledger.path) as conn:
        link = conn.execute("SELECT * FROM smart_bin_reconciliation_resolutions").fetchone()
        assert link["audit_id"] == final["audit_id"]
        original = dict(conn.execute("SELECT * FROM smart_bin_discrepancies WHERE id=?", (issue["id"],)).fetchone())
        for key in issue.keys() - {"status", "resolved_at"}:
            assert original[key] == issue[key]


def test_extension_explicit_versioned_inert_and_fails_closed(ledger):
    assert reconciliation.initialize_schema() == 1
    with connect(ledger.path) as conn:
        conn.execute("UPDATE smart_bin_reconciliation_versions SET version=99")
    with pytest.raises(RuntimeError, match="unsupported reconciliation"):
        reconciliation.initialize_schema()
    with connect(ledger.path) as conn:
        assert conn.execute("SELECT version FROM smart_bin_reconciliation_versions").fetchone()[0] == 99


def test_no_physical_callbacks_permits_or_lock_acquisition(ledger, monkeypatch):
    from test_smart_bins_physical_bridge import Bench, Owner
    req, owner = request_for(ledger, released(ledger))
    bench = Bench(Owner())
    def forbidden(*args, **kwargs):
        raise AssertionError("physical or publication path must not run")
    monkeypatch.setattr(bench.runtime, "request_index", forbidden, raising=False)
    monkeypatch.setattr(delivery, "complete_native", forbidden)
    monkeypatch.setattr(recovery, "stage_handoff", forbidden)
    monkeypatch.setattr(bench.rig, "start", forbidden)
    monkeypatch.setattr(bench.chute, "discharge", forbidden)
    initial_commands = list(bench.rig.commands)
    initial_blocker = bench.bridge.recovery_blocker
    with bench.owner.lock:
        bench.owner.assert_locked()
        assert apply(req, owner)["code"] == "OK"
    assert bench.rig.commands == initial_commands == []
    assert bench.bridge.recovery_blocker == initial_blocker
    assert Bench(Owner()).bridge.recovery_blocker


@pytest.mark.parametrize("field", ["expected_machine_revision", "expected_reservation_revision"])
def test_stale_revisions_refuse(ledger, field):
    req, owner = request_for(ledger, released(ledger))
    before = dump(ledger.path)
    assert "STALE_REVISION" in apply(replace(req, **{field: 999}), owner)["blockers"]
    assert dump(ledger.path) == before


@pytest.mark.parametrize("kind", ["slot", "cycle", "history", "custody", "membership"])
def test_existing_cross_machine_or_wrong_custody_references_refuse(ledger, kind):
    req, owner = request_for(ledger, released(ledger))
    with connect(ledger.path) as conn:
        conn.execute("INSERT INTO smart_bin_machines VALUES('other',0)")
        conn.execute("INSERT INTO smart_bin_slots VALUES('other-slot','other','layout',0,0,0)")
        conn.execute("INSERT INTO smart_bin_cycles(id,machine_id,slot_id,opened_at,provenance) VALUES('other-cycle','other','other-slot',1,'observed')")
        if kind == "history":
            conn.execute("INSERT INTO piece_records(uuid,machine_id) VALUES('piece','other')")
    if kind == "slot":
        req = replace(req, evidence=replace(req.evidence, actual_slot_id="other-slot"))
    elif kind == "cycle":
        req = replace(req, evidence=replace(req.evidence, actual_cycle_id="other-cycle"))
    elif kind == "custody":
        req = replace(req, evidence=replace(req.evidence, custody=replace(req.evidence.custody, pocket_generation=99)))
    elif kind == "membership":
        req = replace(req, evidence=replace(req.evidence, membership=replace(req.evidence.membership, machine_id="other")))
    before = dump(ledger.path)
    assert apply(req, owner)["code"] == "REFUSED"
    assert dump(ledger.path) == before


@pytest.mark.parametrize("conflict", [False, True])
def test_native_destination_mismatch_requires_matching_evidence(ledger, conflict):
    reservation = released(ledger, state="EXIT_CONFIRMED")
    with connect(ledger.path) as conn:
        attempt = conn.execute("SELECT id FROM smart_bin_release_attempts").fetchone()[0]
    completion = delivery.CompletionEvidence(custody(), attempt, 17, "synthetic:actual",
        ledger.clock[0], "BIN", "s1", "c1", "session", "runtime-run", {"uuid": "piece"})
    assert delivery.complete_native("m", reservation, expected_reservation_revision=2,
        request_key="native-mismatch", evidence=completion)["code"] == "DESTINATION_MISMATCH"
    req, owner = request_for(ledger, reservation, slot="s0" if conflict else "s1",
                             cycle="c0" if conflict else "c1")
    with connect(ledger.path) as conn:
        issue = dict(conn.execute("SELECT * FROM smart_bin_discrepancies").fetchone())
    req = replace(req, resolutions=(DiscrepancyResolution(issue["id"], service._digest(issue), REF.ref),))
    before = dump(ledger.path)
    result = apply(req, owner)
    if conflict:
        assert "CONFLICTING_DELIVERY_EVIDENCE" in result["blockers"]
        assert dump(ledger.path) == before
    else:
        assert result["code"] == "OK"
        assert migration.read_recorded_contents("c1")["total"] == 1
        assert migration.read_recorded_contents("c0")["total"] == 0


@pytest.mark.parametrize("change", ["digest", "reference", "unrelated"])
def test_resolution_requires_exact_addressed_discrepancy(ledger, change):
    reservation = released(ledger, state="UNCERTAIN")
    req, owner = request_for(ledger, reservation, outcome="CANCELLED")
    with connect(ledger.path) as conn:
        issue = dict(conn.execute("SELECT * FROM smart_bin_discrepancies").fetchone())
    resolution = DiscrepancyResolution(issue["id"], service._digest(issue), REF.ref)
    resolution = replace(resolution, **{
        "digest": {"expected_digest": "stale"}, "reference": {"evidence_ref": "unrelated-proof"},
        "unrelated": {"discrepancy_id": "unrelated"}}[change])
    before = dump(ledger.path)
    assert "UNSUPPORTED_OR_STALE_DISCREPANCY_RESOLUTION" in apply(
        replace(req, resolutions=(resolution,)), owner)["blockers"]
    assert dump(ledger.path) == before


def test_full_sync_connection_history_and_receipt_immutability(ledger, monkeypatch):
    req, owner = request_for(ledger, released(ledger))
    original = reconciliation.recordPieceOnConnection
    seen = []
    def verify(conn, *args, **kwargs):
        assert conn.in_transaction
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM smart_bin_reconciliations").fetchone()[0] == 1
        seen.append(conn)
        return original(conn, *args, **kwargs)
    monkeypatch.setattr(reconciliation, "recordPieceOnConnection", verify)
    assert apply(req, owner)["code"] == "OK" and len(seen) == 1
    with connect(ledger.path) as conn:
        for table in ("smart_bin_reconciliations", "smart_bin_reconciliation_receipts"):
            with pytest.raises(Exception, match="reconciliation evidence is immutable"):
                conn.execute(f"DELETE FROM {table}")


def test_configuration_and_history_changes_invalidate_preview(ledger):
    req, owner = request_for(ledger, released(ledger))
    plan = reconciliation.preview(req, owner)
    with connect(ledger.path) as conn:
        config = json.loads(conn.execute("SELECT json_value FROM state_entries WHERE key='bin_layout'").fetchone()[0])
        config["layers"][0]["max_pieces_per_bin"] = 0
        conn.execute("UPDATE state_entries SET json_value=? WHERE key='bin_layout'", (json.dumps(config),))
    assert "STALE_PREVIEW" in reconciliation.apply(req, owner, expected_preview_hash=plan["preview_hash"])["blockers"]
    plan = reconciliation.preview(req, owner)
    with connect(ledger.path) as conn:
        conn.execute("INSERT INTO piece_records(uuid,recorded_at) VALUES('piece',1)")
    result = reconciliation.apply(req, owner, expected_preview_hash=plan["preview_hash"])
    assert set(result["blockers"]) >= {"STALE_PREVIEW", "LEGACY_CONTENTS_OVERLAP_DEFERRED"}


def test_group_incompatibility_is_credited_and_blocked(ledger):
    req, owner = request_for(ledger, released(ledger), slot="s1", cycle="c1")
    with connect(ledger.path) as conn:
        conn.execute("INSERT INTO smart_bin_group_keys(id,kind,namespace,part_id,provenance) VALUES('B','category','sorter','B','known')")
        conn.execute("INSERT INTO smart_bin_assignments VALUES('assignment','m','s1','p','B',0,1)")
    result = apply(req, owner)
    assert result["code"] == "OK"
    assert "GROUP_COMPATIBILITY_UNQUALIFIED" in result["remaining_blockers"][0]["reasons"]
    with connect(ledger.path) as conn:
        assert conn.execute("SELECT group_key_id FROM smart_bin_assignments WHERE id='assignment'").fetchone()[0] == "B"


def test_missing_extensions_and_history_never_auto_initialize(prepared, monkeypatch):
    with pytest.raises(RuntimeError, match="absent or incomplete"):
        with delivery._readonly() as conn:
            reconciliation.check_schema(conn)
    with connect(prepared) as conn:
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'smart_bin_reconciliation%'").fetchone()[0] == 0


def test_request_key_collision_with_existing_action_refuses(ledger):
    req, owner = request_for(ledger, released(ledger), key="reserve")
    before = dump(ledger.path)
    assert apply(req, owner)["code"] == "IDEMPOTENCY_CONFLICT"
    assert dump(ledger.path) == before


def test_already_credited_inconsistent_claim_is_never_credited_or_cancelled_again(ledger):
    req, owner = request_for(ledger, released(ledger))
    assert apply(req, owner)["code"] == "OK"
    with connect(ledger.path) as conn:
        # A corrupted/non-service state transition cannot make a delivery reusable.
        conn.execute("UPDATE smart_bin_reservations SET state='UNCERTAIN' WHERE id=?", (req.reservation_id,))
    for outcome in ("COMPLETED", "CANCELLED"):
        another, fresh_owner = request_for(ledger, req.reservation_id, outcome=outcome, key=outcome)
        before = dump(ledger.path)
        result = apply(another, fresh_owner)
        assert "ALREADY_CREDITED_OR_CONFLICTING_DELIVERY" in result["blockers"]
        assert dump(ledger.path) == before


def test_unaddressed_uncertainty_stays_active_after_cancellation(ledger):
    req, owner = request_for(ledger, released(ledger, state="UNCERTAIN"), outcome="CANCELLED")
    result = apply(req, owner)
    assert result["code"] == "OK" and result["remaining_blockers"]
    assert migration.read_recorded_contents("c0")["blocked"]
    assert delivery.inspect_recovery("m")["unresolved_discrepancies"]


def test_absent_history_schema_refuses_without_ddl(ledger):
    req, owner = request_for(ledger, released(ledger))
    with connect(ledger.path) as conn:
        conn.execute("DROP TABLE piece_records")
    before = dump(ledger.path)
    result = apply(req, owner)
    assert "HISTORY_SCHEMA_NOT_INITIALIZED" in result["blockers"]
    assert dump(ledger.path) == before


def test_existing_allocation_blocker_is_preserved_without_duplication(ledger):
    req, owner = request_for(ledger, released(ledger))
    with connect(ledger.path) as conn:
        conn.execute("INSERT INTO smart_bin_discrepancies "
                     "(id,machine_id,cycle_id,kind,status,details_json,created_at) "
                     "VALUES('old-allocation','m','c1','RECONCILIATION_ALLOCATION_BLOCKER','open','{}',1)")
    result = apply(req, owner)
    assert result["code"] == "OK", result
    assert {b["discrepancy_id"] for b in result["remaining_blockers"]} >= {"old-allocation"}
    with connect(ledger.path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_discrepancies").fetchone()[0] == 2
        assert tuple(conn.execute("SELECT details_json,resolved_at FROM smart_bin_discrepancies "
                                  "WHERE id='old-allocation'").fetchone()) == ("{}", None)
