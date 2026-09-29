"""Isolated SQLite qualification for inactive native smart-bin delivery."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest

import local_state
import piece_records
import smart_bins_delivery as delivery
import smart_bins_completion_recovery as completion_recovery
import smart_bins_migration as migration
import smart_bins_service as service
import smart_bins_storage as storage
from smart_bins_test_support import close_keeper as _close_keeper


@contextmanager
def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
    finally:
        conn.close()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    _close_keeper()
    path = tmp_path / "state.sqlite"
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(path))
    monkeypatch.setenv("MACHINE_SPECIFIC_PARAMS_PATH", str(tmp_path / "machine.toml"))
    monkeypatch.setattr(piece_records, "_initialized", False)
    local_state.initialize_local_state()
    storage.initialize_schema()
    migration.initialize_migration_schema()
    service.initialize_service_schema()
    completion_recovery.initialize_schema()
    with connect(path) as conn:
        conn.execute("INSERT INTO smart_bin_machines(machine_id) VALUES('m')")
        conn.execute("INSERT INTO smart_bin_policy_revisions VALUES('p','m','artifact','{}',1)")
        conn.execute("INSERT INTO smart_bin_group_keys "
                     "(id,kind,namespace,part_id,provenance) "
                     "VALUES('A','category','sorter','A','known')")
        conn.execute("INSERT INTO sorting_sessions(id,machine_id,started_at,status) "
                     "VALUES('session','m',1,'active')")
        for index in range(2):
            conn.execute("INSERT INTO smart_bin_slots "
                         "(id,machine_id,layout_revision,layer_index,section_index,bin_index) "
                         "VALUES(?,'m','layout',0,0,?)", (f"s{index}", index))
            conn.execute("INSERT INTO smart_bin_cycles "
                         "(id,machine_id,slot_id,opened_at,provenance) "
                         "VALUES(?,'m',?,1,'qualified_synthetic')", (f"c{index}", f"s{index}"))
        layout = {"layers": [{"sections": [["medium", "medium"]], "enabled": True,
                              "section_enabled": [True], "max_pieces_per_bin": 3,
                              "max_dimension_mm": None}]}
        for key, value in (("bin_layout", layout),
                           ("not_in_inventory_bins", [[[False, False]]])):
            conn.execute("INSERT INTO state_entries(key,json_value,updated_at) VALUES(?,?,1)",
                         (key, json.dumps(value, sort_keys=True)))
    yield path
    _close_keeper()


def qualification(*, two=False):
    with connect(local_state.local_state_db_path()) as conn:
        revision = conn.execute("SELECT state_revision FROM smart_bin_machines "
                                "WHERE machine_id='m'").fetchone()[0]
    slots = tuple(service.SlotQualification(
        slot_id=f"s{index}", cycle_id=f"c{index}", layout_revision="layout",
        enabled=True, reachable=True, not_in_inventory=False, count_limit=3,
        layer_max_dimension_mm=None, cycle_evidence_ref=f"qualified-c{index}")
        for index in range(2 if two else 1))
    return service.RoutingQualification(
        machine_id="m", policy_revision_id="p", policy_artifact_hash="artifact",
        routing_revision=0, machine_state_revision=revision,
        config_digest=service.configuration_digest(), external_revision="route-current",
        allow_normal_sharing=False, slots=slots)


def claim(piece="piece", key="reserve", *, pocket=0, two=False):
    request = service.ReservationRequest(
        piece_uuid=piece, route_attempt="first", sorting_session_id="session",
        group_key_id="A", owner_incarnation="owner", episode_id="episode",
        pocket_index=pocket, pocket_generation=1)
    q = qualification(two=two)
    preview = service.preview(request, q)
    assert preview["code"] == "OK"
    result = service.reserve(
        request, q, expected_state_revision=preview["state_revision"],
        expected_qualification_hash=preview["qualification_hash"], request_key=key)
    assert result["code"] == "OK"
    return result


def custody(piece="piece", pocket=0):
    return delivery.CustodyRef(piece, "owner", "episode", pocket, 1)


def release_evidence(piece="piece", pocket=0, boundary=17, *, slot="s0", cycle="c0"):
    now = time.time()
    return delivery.ReleaseEvidence(
        custody(piece, pocket), boundary, "BIN", slot, cycle, True, True,
        "owner-observation", "route-observation", now, now + 4)


def prepare(claimed, *, key="intent", evidence=None, q=None):
    return delivery.prepare_release(
        "m", claimed["reservation_id"], expected_reservation_revision=0,
        request_key=key, qualification=q or qualification(),
        evidence=evidence or release_evidence())


def exit_evidence(attempt, *, piece="piece", pocket=0, boundary=17):
    return delivery.ExitEvidence(custody(piece, pocket), attempt, boundary,
                                 True, "marker-confirmation", time.time() + 1)


def confirm(claimed, intent, *, key="exit", evidence=None):
    return delivery.confirm_exit(
        "m", claimed["reservation_id"], expected_reservation_revision=1,
        request_key=key, evidence=evidence or exit_evidence(intent["attempt_id"]))


def completion_evidence(attempt, *, actual_slot="s0", actual_cycle="c0",
                        piece="piece", pocket=0, boundary=17):
    return delivery.CompletionEvidence(
        custody(piece, pocket), attempt, boundary, "software-drop-complete",
        time.time() + 2, "BIN", actual_slot, actual_cycle, "session", "runtime-run",
        {"uuid": piece, "part_id": "raw-3001", "color_id": "raw-5",
         "category_id": "A", "classification_status": "classified"})


def complete(claimed, intent, *, key="complete", evidence=None):
    return delivery.complete_native(
        "m", claimed["reservation_id"], expected_reservation_revision=2,
        request_key=key, evidence=evidence or completion_evidence(intent["attempt_id"]))


def snapshot(path):
    with connect(path) as conn:
        return {name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in ("smart_bin_release_attempts", "smart_bin_release_evidence",
                             "smart_bin_deliveries", "smart_bin_audit_events",
                             "smart_bin_request_receipts", "smart_bin_discrepancies",
                             "piece_records") if name != "piece_records" or conn.execute(
                                 "SELECT 1 FROM sqlite_master WHERE name='piece_records'").fetchone()}


def test_valid_lifecycle_atomic_history_capacity_and_replay(prepared):
    claimed = claim()
    cycle = migration.read_recorded_contents("c0", prepared)
    assert cycle["total"] == 0
    with connect(prepared) as conn:
        held_before = conn.execute("SELECT sum(quantity) FROM smart_bin_reservations "
                                   "WHERE intended_cycle_id='c0' AND state='RESERVED'").fetchone()[0]
    assert held_before == 1
    piece_records.initialize_piece_records()
    with connect(prepared) as conn:
        conn.execute("INSERT INTO piece_records "
                     "(uuid,part_correct,color_corrected_id,part_feedback_submitted) "
                     "VALUES('piece',0,'corrected',1)")
    release = release_evidence()
    route = qualification()
    first = prepare(claimed, evidence=release, q=route)
    assert first["code"] == "OK" and first["state"] == "RELEASE_INTENT"
    assert prepare(claimed, evidence=release, q=route) == first
    exit_proof = exit_evidence(first["attempt_id"])
    second = confirm(claimed, first, evidence=exit_proof)
    assert second["code"] == "OK" and second["state"] == "EXIT_CONFIRMED"
    assert confirm(claimed, first, evidence=exit_proof) == second
    assert migration.read_recorded_contents("c0", prepared)["total"] == 0
    completion = completion_evidence(first["attempt_id"])
    final = complete(claimed, first, evidence=completion)
    assert final["code"] == "OK" and final["state"] == "COMPLETED"
    counts = snapshot(prepared)
    assert complete(claimed, first, evidence=completion) == final
    assert snapshot(prepared) == counts
    assert counts["smart_bin_deliveries"] == 1
    assert counts["smart_bin_release_attempts"] == 1
    assert counts["smart_bin_audit_events"] == 4
    projection = migration.read_recorded_contents("c0", prepared)
    assert projection["total"] == 1
    with connect(prepared) as conn:
        held_after = conn.execute("SELECT count(*) FROM smart_bin_reservations WHERE "
                                  "intended_cycle_id='c0' AND state IN "
                                  "('RESERVED','RELEASE_INTENT','EXIT_CONFIRMED','UNCERTAIN')").fetchone()[0]
        row = conn.execute("SELECT * FROM piece_records WHERE uuid='piece'").fetchone()
        native = conn.execute("SELECT * FROM smart_bin_deliveries").fetchone()
    assert held_after == 0 and projection["total"] + held_after == held_before
    assert row["run_id"] == "runtime-run" and native["run_id"] == "session"
    assert (row["part_id"], row["part_correct"], row["color_corrected_id"],
            row["part_feedback_submitted"]) == ("raw-3001", 0, "corrected", 1)
    assert (row["bin_x"], row["bin_y"], row["bin_z"]) == (0, 0, 0)
    assert delivery.lookup_request("complete")["result"] == final
    assert delivery.inspect_recovery("m")["claims"] == []


def test_release_refuses_missing_stale_wrong_owner_route_and_destination(prepared):
    claimed = claim()
    good = release_evidence()
    invalid = [replace(good, custody=replace(good.custody, owner_incarnation="other")),
               replace(good, custody=replace(good.custody, episode_id="other")),
               replace(good, current_owner=False), replace(good, route_ready=False),
               replace(good, target_boundary=-1),
               replace(good, expires_at=time.time() - 1),
               replace(good, slot_id="s1", cycle_id="c1")]
    for index, evidence in enumerate(invalid):
        assert prepare(claimed, key=f"bad-{index}", evidence=evidence)["code"] != "OK"
    stale = replace(qualification(), machine_state_revision=100)
    assert prepare(claimed, key="stale-q", q=stale)["code"] == "STALE_REVISION"
    assert delivery.prepare_release("m", claimed["reservation_id"],
                                    expected_reservation_revision=9, request_key="stale-row",
                                    qualification=qualification(), evidence=good)["code"] == "STALE_REVISION"
    assert snapshot(prepared)["smart_bin_release_attempts"] == 0
    assert service.lookup_reservation("m", claimed["reservation_id"])["state"] == "RESERVED"


def test_duplicate_target_replay_conflict_and_wrong_exit_evidence(prepared):
    first = claim()
    release = release_evidence()
    route = qualification()
    intent = prepare(first, evidence=release, q=route)
    before = snapshot(prepared)
    assert prepare(first, evidence=release, q=route) == intent
    assert snapshot(prepared) == before
    assert prepare(first, evidence=replace(release_evidence(), target_boundary=18))["code"] == "IDEMPOTENCY_CONFLICT"
    other = claim("other", "reserve-other", pocket=1)
    assert prepare(other, key="intent-other", q=qualification(),
                   evidence=release_evidence("other", 1))["code"] == "TARGET_ALREADY_USED"
    bad = exit_evidence(intent["attempt_id"])
    for index, evidence in enumerate((replace(bad, attempt_id="wrong"),
                                       replace(bad, target_boundary=18),
                                       replace(bad, custody=custody("other")),
                                       replace(bad, marker_confirmed=False))):
        assert confirm(first, intent, key=f"exit-bad-{index}", evidence=evidence)["code"] != "OK"
    assert delivery.confirm_exit("m", first["reservation_id"],
                                 expected_reservation_revision=9, request_key="exit-stale",
                                 evidence=bad)["code"] == "STALE_REVISION"
    assert confirm(first, intent)["code"] == "OK"
    assert confirm(first, intent, evidence=replace(bad, marker_evidence_ref="different"))["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("stage,table", [
    ("intent", "smart_bin_release_attempts"),
    ("exit", "smart_bin_release_evidence"),
    ("complete", "piece_records"),
    ("completion_receipt", "smart_bin_request_receipts"),
])
def test_injected_write_failure_rolls_back_every_record(prepared, stage, table):
    claimed = claim()
    intent = prepare(claimed) if stage != "intent" else None
    if stage in ("complete", "completion_receipt"):
        confirm(claimed, intent)
        piece_records.initialize_piece_records()
    before = snapshot(prepared)
    state_before = service.lookup_reservation("m", claimed["reservation_id"])
    with connect(prepared) as conn:
        operation = "INSERT" if stage != "exit" else "UPDATE"
        conn.execute(f"CREATE TRIGGER fail_delivery BEFORE {operation} ON {table} "
                     "BEGIN SELECT RAISE(ABORT, 'injected delivery failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected delivery failure"):
        if stage == "intent":
            prepare(claimed)
        elif stage == "exit":
            confirm(claimed, intent)
        else:
            complete(claimed, intent)
    assert snapshot(prepared) == before
    assert service.lookup_reservation("m", claimed["reservation_id"]) == state_before
    assert delivery.lookup_request({"intent": "intent", "exit": "exit",
                                    "complete": "complete",
                                    "completion_receipt": "complete"}[stage]) is None


def test_lost_ack_recovery_is_read_only_and_same_key_resolves(prepared):
    claimed = claim()
    release = release_evidence()
    route = qualification()
    intent = prepare(claimed, evidence=release, q=route)
    before = snapshot(prepared)
    assert delivery.lookup_request("intent")["result"] == intent
    report = delivery.inspect_recovery("m")
    assert report["claims"][0]["attempt"]["id"] == intent["attempt_id"]
    assert report["claims"][0]["attempt"]["exit"] is None
    assert snapshot(prepared) == before
    assert prepare(claimed, evidence=release, q=route) == intent
    assert snapshot(prepared) == before


def test_completion_mismatch_becomes_uncertain_without_credit(prepared):
    claimed = claim()
    intent = prepare(claimed)
    confirm(claimed, intent)
    with connect(prepared) as conn:
        conn.execute("UPDATE smart_bin_cycles SET slot_id=NULL,closed_at=2 WHERE id='c1'")
    result = complete(claimed, intent, evidence=completion_evidence(
        intent["attempt_id"], actual_slot="s1", actual_cycle="c1"))
    assert result["code"] == "DESTINATION_MISMATCH" and result["state"] == "UNCERTAIN"
    assert delivery.lookup_request("complete")["result"] == result
    assert migration.read_recorded_contents("c0", prepared)["total"] == 0
    assert migration.read_recorded_contents("c1", prepared)["total"] == 0
    assert snapshot(prepared)["smart_bin_deliveries"] == 0
    assert snapshot(prepared)["smart_bin_discrepancies"] == 1
    assert complete(claimed, intent)["code"] == "IDEMPOTENCY_CONFLICT"
    assert delivery.inspect_recovery("m")["unresolved_discrepancies"][0]["cycle_id"] == "c1"


def test_uncertainty_holds_capacity_and_blocks_normal_completion(prepared):
    claimed = claim()
    intent = prepare(claimed)
    confirmed = confirm(claimed, intent)
    uncertainty = delivery.UncertaintyEvidence("piece", "owner", "receipt ambiguous",
                                                "uncertain-observation", intent["attempt_id"])
    result = delivery.mark_uncertain("m", claimed["reservation_id"],
                                     expected_reservation_revision=2, request_key="uncertain",
                                     evidence=uncertainty)
    assert result["state"] == "UNCERTAIN" and result["reservation_revision"] == 3
    assert delivery.mark_uncertain("m", claimed["reservation_id"],
                                   expected_reservation_revision=2, request_key="uncertain",
                                   evidence=uncertainty) == result
    assert complete(claimed, intent)["code"] == "UNSAFE_STATE"
    assert service.cancel_reserved("m", claimed["reservation_id"],
                                   expected_reservation_revision=3, request_key="cancel",
                                   evidence=service.NonDispatchEvidence(
                                       "owner", "episode", 0, 1, True, True, "false-proof"))["code"] == "UNSAFE_STATE"
    assert migration.read_recorded_contents("c0", prepared)["total"] == 0
    assert delivery.inspect_recovery("m")["claims"][0]["state"] == "UNCERTAIN"
    assert confirmed["state_revision"] < result["state_revision"]


def test_terminal_contradiction_preserves_delivery_and_blocks_both_cycles(prepared):
    claimed = claim()
    intent = prepare(claimed)
    confirm(claimed, intent)
    completion = completion_evidence(intent["attempt_id"])
    final = complete(claimed, intent, evidence=completion)
    evidence = delivery.ContradictionEvidence("later different destination", "late-observation",
                                               "BIN", "s1", "c1")
    result = delivery.record_contradiction(
        "m", claimed["reservation_id"], expected_reservation_revision=3,
        request_key="contradiction", evidence=evidence)
    assert result["code"] == "OK" and result["state"] == "COMPLETED"
    assert delivery.record_contradiction(
        "m", claimed["reservation_id"], expected_reservation_revision=3,
        request_key="contradiction", evidence=evidence) == result
    assert complete(claimed, intent, evidence=completion) == final
    with connect(prepared) as conn:
        assert conn.execute("SELECT actual_cycle_id FROM smart_bin_deliveries").fetchone()[0] == "c0"
    assert service.lookup_reservation("m", claimed["reservation_id"])["row_revision"] == 3
    assert service.preview(service.ReservationRequest(
        "next", "first", "session", "A", owner_incarnation="owner",
        episode_id="episode", pocket_index=2, pocket_generation=1),
        qualification(two=True))["code"] == "NO_ELIGIBLE_BIN"


def test_harvest_refusal_and_service_v1_rejection(prepared):
    claimed = claim()
    evidence = replace(release_evidence(), harvest_allocation_ref="allocation")
    assert prepare(claimed, evidence=evidence)["code"] == "HARVEST_DEFERRED"
    assert snapshot(prepared)["smart_bin_release_attempts"] == 0
    intent = prepare(claimed)
    confirm(claimed, intent)
    completion = replace(completion_evidence(intent["attempt_id"]),
                         harvest_allocation_ref="allocation")
    before = snapshot(prepared)
    assert complete(claimed, intent, evidence=completion)["code"] == "HARVEST_DEFERRED"
    assert snapshot(prepared) == before
    nested = replace(completion_evidence(intent["attempt_id"]),
                     history_piece={"uuid": "piece", "harvest_allocation_ref": "allocation"})
    assert complete(claimed, intent, evidence=nested)["code"] == "HARVEST_DEFERRED"
    assert snapshot(prepared) == before
    with connect(prepared) as conn:
        conn.execute("UPDATE smart_bin_service_versions SET version=1")
    with pytest.raises(RuntimeError, match="unsupported smart-bin service schema version"):
        service.initialize_service_schema()


def test_existing_hold_is_not_counted_twice_at_release(prepared):
    claimed = claim()
    with connect(prepared) as conn:
        raw = json.loads(conn.execute("SELECT json_value FROM state_entries "
                                      "WHERE key='bin_layout'").fetchone()[0])
        raw["layers"][0]["max_pieces_per_bin"] = 1
        conn.execute("UPDATE state_entries SET json_value=? WHERE key='bin_layout'",
                     (json.dumps(raw, sort_keys=True),))
    q = qualification()
    q = replace(q, slots=(replace(q.slots[0], count_limit=1),),
                config_digest=service.configuration_digest())
    result = prepare(claimed, q=q)
    assert result["code"] == "OK"
    with connect(prepared) as conn:
        assert conn.execute("SELECT state FROM smart_bin_reservations WHERE id=?",
                            (claimed["reservation_id"],)).fetchone()[0] == "RELEASE_INTENT"


def test_release_refuses_cycle_no_longer_attached(prepared):
    claimed = claim()
    with connect(prepared) as conn:
        conn.execute("UPDATE smart_bin_cycles SET slot_id=NULL,closed_at=2 WHERE id='c0'")
    result = prepare(claimed)
    assert result["code"] == "OCCUPIED_INCOMPATIBLE"
    assert snapshot(prepared)["smart_bin_release_attempts"] == 0


def test_release_refuses_unresolved_reservation_discrepancy(prepared):
    claimed = claim()
    with connect(prepared) as conn:
        conn.execute("INSERT INTO smart_bin_discrepancies "
                     "(id,machine_id,reservation_id,cycle_id,kind,status,details_json,created_at) "
                     "VALUES('issue','m',?,'c0','UNRESOLVED','open','{}',1)",
                     (claimed["reservation_id"],))
    assert prepare(claimed)["code"] == "UNRESOLVED_DELIVERY"
    assert snapshot(prepared)["smart_bin_release_attempts"] == 0


def test_completion_uses_historical_cycle_even_after_detachment(prepared):
    claimed = claim()
    intent = prepare(claimed)
    confirm(claimed, intent)
    with connect(prepared) as conn:
        conn.execute("UPDATE smart_bin_cycles SET slot_id=NULL,closed_at=2 WHERE id='c0'")
    result = complete(claimed, intent)
    assert result["code"] == "OK"
    assert migration.read_recorded_contents("c0", prepared)["total"] == 1


def test_completion_identity_checks_leave_exit_hold_unchanged(prepared):
    claimed = claim()
    intent = prepare(claimed)
    confirm(claimed, intent)
    piece_records.initialize_piece_records()
    before = snapshot(prepared)
    good = completion_evidence(intent["attempt_id"])
    bad = (replace(good, attempt_id="wrong"),
           replace(good, target_boundary=18),
           replace(good, custody=custody("other")),
           replace(good, sorting_session_id="runtime-run"),
           replace(good, history_piece={"uuid": "other"}),
           replace(good, completion_evidence_ref=""))
    for index, evidence in enumerate(bad):
        assert complete(claimed, intent, key=f"bad-complete-{index}",
                        evidence=evidence)["code"] == "COMPLETION_EVIDENCE_REQUIRED"
    assert delivery.complete_native(
        "m", claimed["reservation_id"], expected_reservation_revision=99,
        request_key="stale-complete", evidence=good)["code"] == "STALE_REVISION"
    assert snapshot(prepared) == before
    assert service.lookup_reservation("m", claimed["reservation_id"])["state"] == "EXIT_CONFIRMED"


def test_uncertainty_from_reserved_has_no_attempt_and_keeps_hold(prepared):
    claimed = claim()
    evidence = delivery.UncertaintyEvidence("piece", "owner", "unverified custody",
                                            "owner-observation")
    result = delivery.mark_uncertain("m", claimed["reservation_id"],
                                     expected_reservation_revision=0,
                                     request_key="uncertain-before-release", evidence=evidence)
    assert result["code"] == "OK" and result["state"] == "UNCERTAIN"
    assert result["attempt_id"] is None
    assert snapshot(prepared)["smart_bin_release_attempts"] == 0
    assert prepare(claimed)["code"] == "UNSAFE_STATE"
    assert migration.read_recorded_contents("c0", prepared)["total"] == 0
    assert delivery.inspect_recovery("m")["claims"][0]["state"] == "UNCERTAIN"


def test_reject_delivery_keeps_virtual_destination(prepared):
    with connect(prepared) as conn:
        conn.execute("INSERT INTO smart_bin_group_keys "
                     "(id,kind,namespace,part_id,provenance) "
                     "VALUES('misc','category','sorter','misc','known')")
    q = qualification()
    request = service.ReservationRequest(
        "reject-piece", "first", "session", "misc", owner_incarnation="owner",
        episode_id="episode", pocket_index=3, pocket_generation=1)
    preview = service.preview(request, q)
    assert preview["code"] == "OK" and preview["destination_kind"] == "REJECT"
    claimed = service.reserve(request, q, expected_state_revision=preview["state_revision"],
                              expected_qualification_hash=preview["qualification_hash"],
                              request_key="reserve-reject")
    evidence = replace(release_evidence("reject-piece", 3), destination_kind="REJECT",
                       slot_id=None, cycle_id=None)
    intent = prepare(claimed, q=qualification(), evidence=evidence)
    assert intent["code"] == "OK"
    marker = exit_evidence(intent["attempt_id"], piece="reject-piece", pocket=3)
    assert confirm(claimed, intent, evidence=marker)["code"] == "OK"
    completed = replace(completion_evidence(intent["attempt_id"], piece="reject-piece", pocket=3),
                        actual_kind="REJECT", actual_slot_id=None, actual_cycle_id=None)
    assert complete(claimed, intent, evidence=completed)["code"] == "OK"
    with connect(prepared) as conn:
        assert conn.execute("SELECT actual_kind,actual_slot_id,actual_cycle_id FROM "
                            "smart_bin_deliveries").fetchone()[:] == ("REJECT", None, None)
        assert conn.execute("SELECT bin_x,bin_y,bin_z FROM piece_records WHERE "
                            "uuid='reject-piece'").fetchone()[:] == (None, None, None)


def _linked_request(predecessor_id):
    return service.ReservationRequest(
        "piece", "second", "session", "A", owner_incarnation="owner",
        episode_id="episode", pocket_index=1, pocket_generation=1,
        predecessor_reservation_id=predecessor_id)


def _alternative_qualification():
    q = qualification(two=True)
    return replace(q, slots=(q.slots[1],))


def _cancel_first(claimed):
    proof = service.NonDispatchEvidence("owner", "episode", 0, 1, True, True,
                                        "no-release-pending")
    result = service.cancel_reserved(
        "m", claimed["reservation_id"], expected_reservation_revision=0,
        request_key="cancel-first", evidence=proof)
    assert result["code"] == "OK"
    return proof, result


def _contradict_cancelled(claimed):
    evidence = delivery.ContradictionEvidence("late physical evidence",
                                               "late-observation")
    result = delivery.record_contradiction(
        "m", claimed["reservation_id"], expected_reservation_revision=1,
        request_key="contradict-first", evidence=evidence)
    assert result["code"] == "OK"
    return evidence, result


def test_r2_cancelled_contradiction_fences_alternative_retry_and_preserves_replays(prepared):
    first = claim()
    proof, cancelled = _cancel_first(first)
    evidence, contradiction = _contradict_cancelled(first)
    linked = _linked_request(first["reservation_id"])
    alternative = _alternative_qualification()
    before = snapshot(prepared)
    with connect(prepared) as conn:
        revision = conn.execute("SELECT state_revision FROM smart_bin_machines "
                                "WHERE machine_id='m'").fetchone()[0]
    assert service.preview(linked, alternative)["code"] == "UNRESOLVED_DELIVERY"
    assert service.reserve(
        linked, alternative, expected_state_revision=revision,
        expected_qualification_hash=service.qualification_digest(alternative),
        request_key="blocked-retry")["code"] == "UNRESOLVED_DELIVERY"
    assert snapshot(prepared) == before
    with connect(prepared) as conn:
        assert conn.execute("SELECT state_revision FROM smart_bin_machines "
                            "WHERE machine_id='m'").fetchone()[0] == revision
    assert service.lookup_reservation("m", first["reservation_id"])["state"] == "CANCELLED"
    assert service.cancel_reserved(
        "m", first["reservation_id"], expected_reservation_revision=0,
        request_key="cancel-first", evidence=proof) == cancelled
    assert delivery.record_contradiction(
        "m", first["reservation_id"], expected_reservation_revision=1,
        request_key="contradict-first", evidence=evidence) == contradiction
    assert snapshot(prepared) == before
    unrelated = service.ReservationRequest(
        "unrelated", "first", "session", "A", owner_incarnation="owner",
        episode_id="episode", pocket_index=2, pocket_generation=1)
    preview = service.preview(unrelated, alternative)
    assert preview["code"] == "OK"
    assert service.reserve(
        unrelated, alternative, expected_state_revision=preview["state_revision"],
        expected_qualification_hash=preview["qualification_hash"],
        request_key="unrelated-admission")["code"] == "OK"


def test_r2_existing_successor_cannot_prepare_after_predecessor_contradiction(prepared):
    first = claim()
    _cancel_first(first)
    linked = _linked_request(first["reservation_id"])
    alternative = _alternative_qualification()
    preview = service.preview(linked, alternative)
    assert preview["code"] == "OK" and preview["slot_id"] == "s1"
    successor = service.reserve(
        linked, alternative, expected_state_revision=preview["state_revision"],
        expected_qualification_hash=preview["qualification_hash"],
        request_key="successor")
    assert successor["code"] == "OK"
    _contradict_cancelled(first)
    before = snapshot(prepared)
    current = service.lookup_reservation("m", successor["reservation_id"])
    with connect(prepared) as conn:
        revision = conn.execute("SELECT state_revision FROM smart_bin_machines "
                                "WHERE machine_id='m'").fetchone()[0]
    release = release_evidence(piece="piece", pocket=1, slot="s1", cycle="c1")
    assert delivery.prepare_release(
        "m", successor["reservation_id"], expected_reservation_revision=0,
        request_key="blocked-intent", qualification=_alternative_qualification(),
        evidence=release)["code"] == "UNRESOLVED_DELIVERY"
    assert snapshot(prepared) == before
    with connect(prepared) as conn:
        assert conn.execute("SELECT state_revision FROM smart_bin_machines "
                            "WHERE machine_id='m'").fetchone()[0] == revision
    assert service.lookup_reservation("m", successor["reservation_id"]) == current
    assert delivery.lookup_request("blocked-intent") is None
    # An exact old reservation receipt remains a read-only replay.
    assert service.reserve(
        linked, alternative, expected_state_revision=preview["state_revision"],
        expected_qualification_hash=preview["qualification_hash"],
        request_key="successor") == successor
    assert snapshot(prepared) == before


def test_r2_ordinary_undisputed_cancelled_retry_still_reserves(prepared):
    first = claim()
    _cancel_first(first)
    linked = _linked_request(first["reservation_id"])
    alternative = _alternative_qualification()
    preview = service.preview(linked, alternative)
    assert preview["code"] == "OK"
    successor = service.reserve(
        linked, alternative, expected_state_revision=preview["state_revision"],
        expected_qualification_hash=preview["qualification_hash"],
        request_key="normal-retry")
    assert successor["code"] == "OK" and successor["slot_id"] == "s1"


def test_r2_misdelivery_rejects_invalid_destination_references_without_writes(prepared):
    claimed = claim()
    intent = prepare(claimed)
    confirm(claimed, intent)
    piece_records.initialize_piece_records()
    with connect(prepared) as conn:
        conn.execute("INSERT INTO smart_bin_machines(machine_id) VALUES('other-machine')")
        conn.execute("INSERT INTO smart_bin_slots "
                     "(id,machine_id,layout_revision,layer_index,section_index,bin_index) "
                     "VALUES('other-slot','other-machine','layout',0,0,0)")
        conn.execute("INSERT INTO smart_bin_cycles "
                     "(id,machine_id,slot_id,opened_at,provenance) "
                     "VALUES('other-cycle','other-machine','other-slot',1,'observed')")
    before = snapshot(prepared)
    original = service.lookup_reservation("m", claimed["reservation_id"])
    with connect(prepared) as conn:
        revision = conn.execute("SELECT state_revision FROM smart_bin_machines "
                                "WHERE machine_id='m'").fetchone()[0]
    invalid = (("other-slot", "other-cycle"), ("s1", "other-cycle"),
               ("other-slot", "c1"), ("s1", "missing-cycle"),
               ("missing-slot", "c1"))
    for index, (slot, cycle) in enumerate(invalid):
        result = complete(claimed, intent, key=f"invalid-destination-{index}",
                          evidence=completion_evidence(intent["attempt_id"],
                                                       actual_slot=slot, actual_cycle=cycle))
        assert result["code"] == "UNQUALIFIED_DESTINATION"
        assert snapshot(prepared) == before
        assert service.lookup_reservation("m", claimed["reservation_id"]) == original
        assert delivery.lookup_request(f"invalid-destination-{index}") is None
        assert migration.read_recorded_contents("c0", prepared)["total"] == 0
        assert migration.read_recorded_contents("c1", prepared)["total"] == 0
    with connect(prepared) as conn:
        assert conn.execute("SELECT state_revision FROM smart_bin_machines "
                            "WHERE machine_id='m'").fetchone()[0] == revision
