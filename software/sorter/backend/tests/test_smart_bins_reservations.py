"""Isolated SQLite checks for the inactive P2B destination-reservation service."""

from __future__ import annotations

import os
import json
import atexit
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace

import pytest

# Paths are fixed before importing modules that may read local configuration.
_import_isolation = tempfile.TemporaryDirectory(prefix="smart-bin-reservation-import-")
atexit.register(_import_isolation.cleanup)
os.environ["LOCAL_STATE_DB_PATH"] = os.path.join(_import_isolation.name, "state.sqlite")
os.environ["MACHINE_SPECIFIC_PARAMS_PATH"] = os.path.join(_import_isolation.name, "machine.toml")

import local_state
import piece_records
import smart_bins_migration as migration
import smart_bins_service as service
import smart_bins_storage as storage


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
    local_state.close_local_state_keeper()
    path = tmp_path / "state.sqlite"
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(path))
    monkeypatch.setenv("MACHINE_SPECIFIC_PARAMS_PATH", str(tmp_path / "machine.toml"))
    monkeypatch.setattr(piece_records, "_initialized", False)
    local_state.initialize_local_state()
    storage.initialize_schema()
    migration.initialize_migration_schema()
    service.initialize_service_schema()
    with connect(path) as conn:
        conn.execute("INSERT INTO smart_bin_machines(machine_id) VALUES('m')")
        conn.execute("INSERT INTO smart_bin_policy_revisions VALUES('p','m','artifact','{}',1)")
        for key in ("A", "B", "misc"):
            conn.execute("INSERT INTO smart_bin_group_keys "
                         "(id,kind,namespace,part_id,provenance) VALUES(?,'category','sorter',?,'known')",
                         (key, key))
        conn.execute("INSERT INTO sorting_sessions(id,machine_id,started_at,status) "
                     "VALUES('session','m',1,'active')")
        for index in range(2):
            conn.execute("INSERT INTO smart_bin_slots "
                         "(id,machine_id,layout_revision,layer_index,section_index,bin_index) "
                         "VALUES(?,'m','layout',0,0,?)", (f"s{index}", index))
            conn.execute("INSERT INTO smart_bin_cycles "
                         "(id,machine_id,slot_id,opened_at,provenance) "
                         "VALUES(?,'m',?,1,'qualified_synthetic')", (f"c{index}", f"s{index}"))
    configure()
    yield path
    local_state.close_local_state_keeper()


def configure(*, limit=2, pool=False, dimension=None):
    """Explicit test configuration mutation; qualification itself only reads."""
    layout = {"layers": [{"sections": [["medium", "medium"]], "enabled": True,
                          "section_enabled": [True], "max_pieces_per_bin": limit,
                          "max_dimension_mm": dimension}]}
    with connect(local_state.local_state_db_path()) as conn:
        for key, value in (("bin_layout", layout),
                           ("not_in_inventory_bins", [[[pool, pool]]])):
            conn.execute("INSERT INTO state_entries(key,json_value,updated_at) VALUES(?,?,1) "
                         "ON CONFLICT(key) DO UPDATE SET json_value=excluded.json_value",
                         (key, json.dumps(value, sort_keys=True)))


def qualification(*, limit=2, two=False, sharing=False, pool=False,
                  dimension=None, external="reach-1", policy="p", artifact="artifact"):
    # The service checks these values against existing local-state configuration.
    slots = tuple(service.SlotQualification(
        slot_id=f"s{index}", cycle_id=f"c{index}", layout_revision="layout",
        enabled=True, reachable=True, not_in_inventory=pool,
        count_limit=limit, layer_max_dimension_mm=dimension,
        cycle_evidence_ref=f"qualified-c{index}") for index in range(2 if two else 1))
    return service.RoutingQualification(
        machine_id="m", policy_revision_id=policy, policy_artifact_hash=artifact,
        routing_revision=0, machine_state_revision=_machine_revision(),
        config_digest=service.configuration_digest(), external_revision=external,
        allow_normal_sharing=sharing, slots=slots)


def _machine_revision():
    with connect(local_state.local_state_db_path()) as conn:
        return conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id='m'").fetchone()[0]


def request(piece="one", group="A", quantity=1, *, pool=False, attempt="first",
            predecessor=None, dimension=None, too_big=False, custody=True):
    return service.ReservationRequest(
        piece_uuid=piece, route_attempt=attempt, sorting_session_id="session",
        group_key_id=group, quantity=quantity, not_in_inventory=pool,
        max_dimension_mm=dimension, too_big=too_big,
        owner_incarnation="owner" if custody else None,
        episode_id="episode" if custody else None,
        pocket_index=0 if custody else None,
        pocket_generation=1 if custody else None,
        predecessor_reservation_id=predecessor)


def claim(req, q, key):
    preview = service.preview(req, q)
    assert preview["code"] == "OK"
    return service.reserve(req, q, expected_state_revision=preview["state_revision"],
                           expected_qualification_hash=preview["qualification_hash"],
                           request_key=key)


def counts(path):
    with connect(path) as conn:
        return tuple(conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in (
            "smart_bin_assignments", "smart_bin_reservations", "smart_bin_audit_events",
            "smart_bin_request_receipts"))


def test_initialization_is_explicit_and_import_inert(tmp_path, monkeypatch):
    local_state.close_local_state_keeper()
    path = tmp_path / "empty.sqlite"
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(path))
    assert not path.exists()
    with pytest.raises(storage.UnsupportedSmartBinSchema):
        service.initialize_service_schema()
    with connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='smart_bin_service_versions'").fetchone()[0] == 0


def test_service_extension_rejects_old_version_and_facts_are_immutable(prepared):
    first = claim(request(), qualification(), "reserve")
    with connect(prepared) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="reservation facts are immutable"):
            conn.execute("UPDATE smart_bin_reservations SET quantity=2 WHERE id=?",
                         (first["reservation_id"],))
        with pytest.raises(sqlite3.IntegrityError, match="reservation facts are immutable"):
            conn.execute("UPDATE smart_bin_reservations SET owner_incarnation='other' WHERE id=?",
                         (first["reservation_id"],))
        conn.execute("UPDATE smart_bin_service_versions SET version=99")
    with pytest.raises(RuntimeError, match="unsupported smart-bin service schema version"):
        service.initialize_service_schema()
    with connect(prepared) as conn:
        assert conn.execute("SELECT version FROM smart_bin_service_versions").fetchone()[0] == 99


def test_exact_replay_after_reconnect_and_conflicts(prepared):
    configure(limit=1)
    q = qualification(limit=1)
    req = request()
    preview = service.preview(req, q)
    first = claim(req, q, "key")
    assert first["code"] == "OK"
    before = counts(prepared)
    assert service.reserve(req, q, expected_state_revision=preview["state_revision"],
                           expected_qualification_hash=preview["qualification_hash"],
                           request_key="key") == first
    assert counts(prepared) == before
    assert service.reserve(replace(req, quantity=2), q,
                           expected_state_revision=preview["state_revision"],
                           expected_qualification_hash=preview["qualification_hash"],
                           request_key="key")["code"] == "IDEMPOTENCY_CONFLICT"
    fresh = qualification(limit=1)
    assert service.reserve(req, fresh, expected_state_revision=1,
                           expected_qualification_hash=service.qualification_digest(fresh),
                           request_key="second")["code"] == "IN_FLIGHT"
    assert service.lookup_reservation("m", first["reservation_id"])["piece_uuid"] == "one"


def test_r2_preview_and_new_reserve_agree_for_in_flight_piece(prepared):
    req = request("held")
    first = claim(req, qualification(), "first")
    fresh = qualification()
    before = (counts(prepared), _machine_revision())
    assert service.preview(req, fresh)["code"] == "IN_FLIGHT"
    assert service.reserve(req, fresh, expected_state_revision=fresh.machine_state_revision,
                           expected_qualification_hash=service.qualification_digest(fresh),
                           request_key="new-key")["code"] == "IN_FLIGHT"
    # Repeating the committed key still returns the original result, even with
    # its stale preview revision and an in-flight journey.
    original = replace(fresh, machine_state_revision=0)
    assert service.reserve(req, original, expected_state_revision=0,
                           expected_qualification_hash=service.qualification_digest(original),
                           request_key="first") == first
    assert (counts(prepared), _machine_revision()) == before


def test_r2_preview_and_new_reserve_agree_for_cancelled_attempts(prepared):
    first = claim(request("piece"), qualification(), "first")
    proof = service.NonDispatchEvidence("owner", "episode", 0, 1, True, True, "no-dispatch")
    assert service.cancel_reserved("m", first["reservation_id"],
                                   expected_reservation_revision=0,
                                   request_key="cancel", evidence=proof)["code"] == "OK"
    fresh = qualification()
    before = (counts(prepared), _machine_revision())
    invalid = (
        request("piece"),
        request("piece", attempt="second"),
        request("piece", attempt="second", predecessor="wrong"),
    )
    for index, req in enumerate(invalid):
        assert service.preview(req, fresh)["code"] == "ROUTE_ATTEMPT_CONFLICT"
        assert service.reserve(req, fresh,
                               expected_state_revision=fresh.machine_state_revision,
                               expected_qualification_hash=service.qualification_digest(fresh),
                               request_key=f"bad-{index}")["code"] == "ROUTE_ATTEMPT_CONFLICT"
    assert (counts(prepared), _machine_revision()) == before
    linked = request("piece", attempt="second", predecessor=first["reservation_id"])
    assert service.preview(linked, fresh)["code"] == "OK"
    assert claim(linked, fresh, "linked")["code"] == "OK"


def test_r2_preview_returns_authoritative_revision_on_stale_qualification(prepared):
    stale = qualification()
    with connect(prepared) as conn:
        conn.execute("UPDATE smart_bin_machines SET state_revision=1 WHERE machine_id='m'")
    preview = service.preview(request(), stale)
    assert preview["code"] == "STALE_REVISION"
    assert preview["state_revision"] == 1
    assert preview["qualification_state_revision"] == 0


def test_exact_boundary_quantity_unlimited_and_positive_integer(prepared):
    q = qualification(limit=2)
    assert claim(request(quantity=2), q, "full")["code"] == "OK"
    assert service.preview(request("other"), qualification(limit=2))["code"] == "NO_ELIGIBLE_BIN"
    with pytest.raises(ValueError, match="positive integer"):
        service.preview(request("bad", quantity=0), qualification())
    with pytest.raises(ValueError, match="positive integer"):
        service.preview(request("bad", quantity=True), qualification())
    with connect(prepared) as conn:
        conn.execute("UPDATE smart_bin_reservations SET state='CANCELLED' WHERE piece_uuid='one'")
    configure(limit=None)
    assert claim(request("other", quantity=30), qualification(limit=None), "unlimited")["code"] == "OK"


@pytest.mark.parametrize("state", service.HELD_STATES)
def test_each_held_state_consumes_capacity(prepared, state):
    configure(limit=1)
    with connect(prepared) as conn:
        _direct_reservation(conn, "old", "old", state)
    assert service.preview(request("new"), qualification(limit=1))["code"] == "NO_ELIGIBLE_BIN"


@pytest.mark.parametrize("state", ("COMPLETED", "CANCELLED"))
def test_terminal_state_does_not_hold_capacity(prepared, state):
    configure(limit=1)
    with connect(prepared) as conn:
        _direct_reservation(conn, "old", "old", state)
    assert service.preview(request("new"), qualification(limit=1))["code"] == "OK"


def _direct_reservation(conn, id, piece, state, group="A", cycle="c0", quantity=1):
    conn.execute("INSERT INTO smart_bin_reservations "
                 "(id,machine_id,piece_uuid,route_attempt,request_key,run_id,policy_revision_id,"
                 "group_key_id,routing_revision,quantity,intended_kind,intended_slot_id,"
                 "intended_cycle_id,state,created_at,updated_at) "
                 "VALUES(?,'m',?,'first',?,'session','p',?,0,?,'BIN','s0',?,?,1,1)",
                 (id, piece, id, group, quantity, cycle, state))


def test_recorded_native_historical_and_opening_balance_count_once(prepared):
    with connect(prepared) as conn:
        conn.execute("INSERT INTO smart_bin_opening_balances "
                     "(id,cycle_id,group_key_id,quantity,provenance,coverage,created_at) "
                     "VALUES('opening','c0','A',1,'qualified_synthetic','complete',1)")
        conn.execute("INSERT INTO smart_bin_migration_imports VALUES('imp','fp','scope','plan',1,'{}')")
        conn.execute("INSERT INTO smart_bin_historical_contributions VALUES "
                     "('hist','imp','c0','m','historical','A',1,'[]','runtime','session',1,'source','observed')")
        _direct_reservation(conn, "old", "native", "COMPLETED")
        conn.execute("INSERT INTO smart_bin_deliveries "
                     "(id,reservation_id,machine_id,piece_uuid,run_id,policy_revision_id,group_key_id,"
                     "quantity,intended_kind,intended_slot_id,intended_cycle_id,actual_kind,"
                     "actual_slot_id,actual_cycle_id,evidence_ref,delivered_at) "
                     "VALUES('d','old','m','native','session','p','A',1,'BIN','s0','c0',"
                     "'BIN','s0','c0','exit',2)")
    projection = migration.read_recorded_contents("c0", prepared)
    assert projection["total"] == 3
    assert projection["breakdown"][0]["total"] == 3
    configure(limit=3)
    assert service.preview(request("new"), qualification(limit=3))["code"] == "NO_ELIGIBLE_BIN"
    configure(limit=4)
    assert service.preview(request("new"), qualification(limit=4))["code"] == "OK"
    assert service.preview(request("native"), qualification(limit=4))["code"] == "ALREADY_CREDITED"
    assert service.preview(request("historical"), qualification(limit=4))["code"] == "ALREADY_CREDITED"
    assert service.reserve(request("historical"), qualification(limit=4),
                           expected_state_revision=0,
                           expected_qualification_hash=service.qualification_digest(qualification(limit=4)),
                           request_key="reuse")["code"] == "ALREADY_CREDITED"


def test_r2_preview_rejects_historical_credit_without_mutation(prepared):
    with connect(prepared) as conn:
        conn.execute("INSERT INTO smart_bin_migration_imports VALUES('imp','fp','scope','plan',1,'{}')")
        conn.execute("INSERT INTO smart_bin_historical_contributions VALUES "
                     "('hist','imp','c0','m','already','A',1,'[]','runtime','session',1,'source','observed')")
    q = qualification()
    req = request("already")
    before = (counts(prepared), migration.read_recorded_contents("c0", prepared),
              _machine_revision())
    assert service.preview(req, q)["code"] == "ALREADY_CREDITED"
    assert service.reserve(req, q, expected_state_revision=q.machine_state_revision,
                           expected_qualification_hash=service.qualification_digest(q),
                           request_key="already-new")["code"] == "ALREADY_CREDITED"
    assert (counts(prepared), migration.read_recorded_contents("c0", prepared),
            _machine_revision()) == before


def seed_second_policy(path):
    with connect(path) as conn:
        conn.execute("INSERT INTO smart_bin_policy_revisions VALUES('q','m','artifact-q','{}',2)")


def test_r2_cross_policy_hold_cannot_look_empty_or_mix(prepared):
    seed_second_policy(prepared)
    first = claim(request("held", group="B"), qualification(), "held")
    assert first["cycle_id"] == "c0"
    current = qualification(policy="q", artifact="artifact-q")
    req = request("incoming", group="A")
    before = (counts(prepared), migration.read_recorded_contents("c0", prepared),
              _machine_revision())
    assert service.preview(req, current)["code"] == "NO_ELIGIBLE_BIN"
    assert service.reserve(req, current,
                           expected_state_revision=current.machine_state_revision,
                           expected_qualification_hash=service.qualification_digest(current),
                           request_key="cross-policy")["code"] == "NO_ELIGIBLE_BIN"
    # Sharing under Q cannot establish a mapping to P's held B.
    assert service.preview(req, replace(current, allow_normal_sharing=True))["code"] == "NO_ELIGIBLE_BIN"
    assert (counts(prepared), migration.read_recorded_contents("c0", prepared),
            _machine_revision()) == before


def test_r2_cross_policy_hold_allows_alternative_eligible_cycle(prepared):
    seed_second_policy(prepared)
    claim(request("held", group="B"), qualification(), "held")
    current = qualification(policy="q", artifact="artifact-q", two=True)
    req = request("incoming", group="A")
    preview = service.preview(req, current)
    assert preview["code"] == "OK" and preview["slot_id"] == "s1"
    result = claim(req, current, "alternative")
    assert result["code"] == "OK" and result["cycle_id"] == "c1"


def test_r2_unlabeled_same_policy_hold_requires_match_or_explicit_sharing(prepared):
    first = claim(request("held", group="B"), qualification(), "held")
    with connect(prepared) as conn:
        conn.execute("DELETE FROM smart_bin_assignments WHERE slot_id='s0'")
    fresh = qualification()
    req = request("incoming", group="A")
    before = (counts(prepared), migration.read_recorded_contents("c0", prepared),
              _machine_revision())
    assert service.preview(req, fresh)["code"] == "NO_ELIGIBLE_BIN"
    assert service.reserve(req, fresh,
                           expected_state_revision=fresh.machine_state_revision,
                           expected_qualification_hash=service.qualification_digest(fresh),
                           request_key="unlabeled-no-share")["code"] == "NO_ELIGIBLE_BIN"
    assert (counts(prepared), migration.read_recorded_contents("c0", prepared),
            _machine_revision()) == before
    same_group = request("another", group="B")
    assert service.preview(same_group, fresh)["preference"] == "recorded_match"
    sharing = replace(fresh, allow_normal_sharing=True)
    assert service.preview(req, sharing)["preference"] == "shared"
    assert claim(req, sharing, "explicit-share")["code"] == "OK"
    with connect(prepared) as conn:
        labels = {row[0] for row in conn.execute(
            "SELECT group_key_id FROM smart_bin_assignments WHERE slot_id='s0'")}
        assert labels == {"A", "B"}
        assert conn.execute("SELECT state FROM smart_bin_reservations WHERE id=?",
                            (first["reservation_id"],)).fetchone()[0] == "RESERVED"


def test_r2_compatible_unlabeled_hold_can_reserve_same_group(prepared):
    first = claim(request("held", group="B"), qualification(), "held")
    with connect(prepared) as conn:
        conn.execute("DELETE FROM smart_bin_assignments WHERE slot_id='s0'")
    current = qualification()
    same_group = request("another", group="B")
    preview = service.preview(same_group, current)
    assert preview["code"] == "OK" and preview["preference"] == "recorded_match"
    second = claim(same_group, current, "same-group")
    assert second["code"] == "OK" and second["cycle_id"] == first["cycle_id"]
    with connect(prepared) as conn:
        assert {row[0] for row in conn.execute(
            "SELECT group_key_id FROM smart_bin_assignments WHERE slot_id='s0'")} == {"B"}


def test_pool_sharing_preference_and_incompatible_contents(prepared):
    with connect(prepared) as conn:
        conn.execute("INSERT INTO smart_bin_opening_balances "
                     "(id,cycle_id,group_key_id,quantity,provenance,coverage,created_at) "
                     "VALUES('opening','c0','B',1,'qualified_synthetic','complete',1)")
    configure(limit=3)
    q = qualification(limit=3, two=True, sharing=True)
    selected = service.preview(request(), q)
    assert selected["slot_id"] == "s1" and selected["preference"] == "empty"
    q_one = qualification(limit=3, sharing=False)
    assert service.preview(request(), q_one)["code"] == "NO_ELIGIBLE_BIN"
    q_share = qualification(limit=3, sharing=True)
    assert service.preview(request(), q_share)["preference"] == "shared"
    assert service.preview(request(pool=True), q_share)["code"] == "NO_ELIGIBLE_BIN"
    configure(limit=3, pool=True)
    assert service.preview(request(pool=True), qualification(limit=3, pool=True))["preference"] == "shared"
    with connect(prepared) as conn:
        conn.execute("INSERT INTO smart_bin_assignments VALUES('label','m','s0','p','A',0,1)")
    configure(limit=3)
    assert service.preview(request(), qualification(limit=3, sharing=True))["code"] == "NO_ELIGIBLE_BIN"
    assert counts(prepared) == (1, 0, 0, 0)


def test_assigned_match_first_and_shared_selection_preserves_evidence(prepared):
    with connect(prepared) as conn:
        conn.execute("INSERT INTO smart_bin_opening_balances "
                     "(id,cycle_id,group_key_id,quantity,provenance,coverage,created_at) "
                     "VALUES('opening','c0','B',1,'qualified_synthetic','complete',1)")
        conn.execute("INSERT INTO smart_bin_assignments VALUES('label','m','s1','p','A',0,1)")
    configure(limit=3)
    q = qualification(limit=3, two=True, sharing=True)
    assert service.preview(request(), q)["slot_id"] == "s1"
    with connect(prepared) as conn:
        conn.execute("DELETE FROM smart_bin_assignments WHERE id='label'")
    q = qualification(limit=3, sharing=True)
    assert service.preview(request(), q)["preference"] == "shared"
    assert claim(request(), q, "share")["code"] == "OK"
    with connect(prepared) as conn:
        groups = [row[0] for row in conn.execute(
            "SELECT group_key_id FROM smart_bin_assignments WHERE slot_id='s0' ORDER BY group_key_id")]
        assert groups == ["A", "B"]
        assert conn.execute("SELECT quantity FROM smart_bin_opening_balances WHERE id='opening'").fetchone()[0] == 1
    assert migration.read_recorded_contents("c0", prepared)["total"] == 1


def test_misc_reject_fit_and_disabled_reachability(prepared):
    configure(limit=2, dimension=40)
    q = qualification(limit=2, dimension=40)
    misc = claim(request(group="misc"), q, "misc")
    assert misc["destination_kind"] == "REJECT" and misc["slot_id"] is None
    assert counts(prepared)[0] == 0
    q = qualification(limit=2, dimension=40)
    assert service.preview(request("large", dimension=41), q)["code"] == "NO_ELIGIBLE_BIN"
    assert service.preview(request("unknown", dimension=None), q)["code"] == "OK"
    assert service.preview(request("oversize", too_big=True), q)["destination_kind"] == "REJECT"
    slot = replace(q.slots[0], reachable=False)
    assert service.preview(request("blocked"), replace(q, slots=(slot,)))["code"] == "NO_ELIGIBLE_BIN"
    slot = replace(q.slots[0], enabled=False)
    assert service.preview(request("disabled"), replace(q, slots=(slot,)))["code"] == "NO_ELIGIBLE_BIN"


def test_stale_preview_configuration_and_unqualified_cycle(prepared):
    req = request()
    q = qualification()
    preview = service.preview(req, q)
    changed = replace(q, external_revision="reach-2")
    assert service.reserve(req, changed, expected_state_revision=0,
                           expected_qualification_hash=preview["qualification_hash"],
                           request_key="stale")["code"] == "STALE_CONFIGURATION"
    with connect(prepared) as conn:
        conn.execute("INSERT INTO state_entries VALUES('bin_categories','[]',2)")
    assert service.preview(req, q)["code"] == "STALE_CONFIGURATION"
    assert counts(prepared) == (0, 0, 0, 0)
    q = qualification()
    with connect(prepared) as conn:
        conn.execute("UPDATE smart_bin_cycles SET slot_id=NULL WHERE id='c0'")
    assert service.preview(req, q)["code"] == "NO_ELIGIBLE_BIN"
    with connect(prepared) as conn:
        conn.execute("UPDATE smart_bin_cycles SET slot_id='s0',closed_at=2 WHERE id='c0'")
    assert service.preview(req, q)["code"] == "NO_ELIGIBLE_BIN"


def test_machine_policy_group_slot_and_uncertain_contents_gates(prepared):
    req = request()
    q = qualification()
    with connect(prepared) as conn:
        conn.execute("UPDATE smart_bin_machines SET state_revision=1 WHERE machine_id='m'")
    assert service.preview(req, q)["code"] == "STALE_REVISION"
    q = qualification()
    assert service.preview(req, replace(q, policy_artifact_hash="wrong"))["code"] == "STALE_POLICY"
    assert service.preview(replace(req, group_key_id="absent"), q)["code"] == "UNQUALIFIED_GROUP"
    assert service.preview(req, replace(q, slots=(replace(q.slots[0], layout_revision="wrong"),)))["code"] == "STALE_CONFIGURATION"
    assert service.preview(req, replace(q, slots=(replace(q.slots[0], count_limit=None),)))["code"] == "STALE_CONFIGURATION"
    assert service.preview(req, replace(q, slots=(replace(q.slots[0], not_in_inventory=True),)))["code"] == "STALE_CONFIGURATION"
    with connect(prepared) as conn:
        conn.execute("INSERT INTO smart_bin_opening_balances "
                     "(id,cycle_id,quantity,provenance,coverage,created_at) "
                     "VALUES('unknown','c0',1,'unknown','unknown',1)")
    assert service.preview(req, qualification())["code"] == "NO_ELIGIBLE_BIN"
    assert counts(prepared) == (0, 0, 0, 0)


def test_imported_and_blocked_cycles_refuse_even_with_numeric_balance(prepared):
    q = qualification()
    with connect(prepared) as conn:
        conn.execute("INSERT INTO smart_bin_opening_balances "
                     "(id,cycle_id,group_key_id,quantity,provenance,coverage,created_at) "
                     "VALUES('balance','c0','A',1,'imported','historical_unknown',1)")
        conn.execute("UPDATE smart_bin_cycles SET provenance='migration_anchor_not_observed_empty' "
                     "WHERE id='c0'")
    assert service.preview(request(), q)["code"] == "NO_ELIGIBLE_BIN"
    with connect(prepared) as conn:
        conn.execute("UPDATE smart_bin_cycles SET provenance='qualified_synthetic' WHERE id='c0'")
        conn.execute("INSERT INTO smart_bin_discrepancies "
                     "(id,machine_id,cycle_id,kind,status,details_json,created_at) "
                     "VALUES('issue','m','c0','unknown','open','{}',1)")
    assert service.preview(request(), q)["code"] == "NO_ELIGIBLE_BIN"


def test_unresolved_native_delivery_blocks_receiving_cycle(prepared):
    with connect(prepared) as conn:
        _direct_reservation(conn, "old", "old", "EXIT_CONFIRMED")
        conn.execute("INSERT INTO smart_bin_deliveries "
                     "(id,reservation_id,machine_id,piece_uuid,run_id,policy_revision_id,group_key_id,"
                     "quantity,intended_kind,intended_slot_id,intended_cycle_id,actual_kind,"
                     "actual_slot_id,actual_cycle_id,evidence_ref,delivered_at) "
                     "VALUES('d','old','m','old','session','p','A',1,'BIN','s0','c0',"
                     "'BIN','s0','c0','exit',2)")
    configure(limit=10)
    assert service.preview(request(), qualification(limit=10))["code"] == "NO_ELIGIBLE_BIN"


def test_cancel_requires_current_owner_and_preserves_recorded_contents(prepared):
    with connect(prepared) as conn:
        conn.execute("INSERT INTO smart_bin_opening_balances "
                     "(id,cycle_id,group_key_id,quantity,provenance,coverage,created_at) "
                     "VALUES('balance','c0','A',1,'synthetic','complete',1)")
    first = claim(request(), qualification(limit=2), "reserve")
    before = migration.read_recorded_contents("c0", prepared)
    proof = service.NonDispatchEvidence("owner", "episode", 0, 1, True, True, "owner-confirmed")
    assert service.cancel_reserved("m", first["reservation_id"], expected_reservation_revision=0,
                                   request_key="cancel-no", evidence=replace(proof, current_owner=False))["code"] == "OWNER_EVIDENCE_REQUIRED"
    assert service.cancel_reserved("m", first["reservation_id"], expected_reservation_revision=0,
                                   request_key="cancel", evidence=proof)["code"] == "OK"
    assert migration.read_recorded_contents("c0", prepared)["total"] == before["total"] == 1
    assert service.cancel_reserved("m", first["reservation_id"], expected_reservation_revision=0,
                                   request_key="cancel", evidence=proof)["code"] == "OK"
    assert service.cancel_reserved("m", first["reservation_id"], expected_reservation_revision=0,
                                   request_key="cancel-again", evidence=proof)["code"] == "UNSAFE_STATE"
    assert service.preview(request("second"), qualification(limit=2))["code"] == "OK"
    assert service.reserve(request("one", attempt="second"), qualification(limit=2),
                           expected_state_revision=2,
                           expected_qualification_hash=service.qualification_digest(qualification(limit=2)),
                           request_key="missing-link")["code"] == "ROUTE_ATTEMPT_CONFLICT"
    retry = request("one", attempt="second", predecessor=first["reservation_id"])
    assert claim(retry, qualification(limit=2), "linked")["code"] == "OK"


def test_cancellation_stale_revision_and_pending_release_row_refuse(prepared):
    first = claim(request(), qualification(), "reserve")
    proof = service.NonDispatchEvidence("owner", "episode", 0, 1, True, True, "owner-confirmed")
    assert service.cancel_reserved("m", first["reservation_id"], expected_reservation_revision=1,
                                   request_key="stale", evidence=proof)["code"] == "STALE_REVISION"
    assert service.cancel_reserved("m", first["reservation_id"], expected_reservation_revision=0,
                                   request_key="no-proof", evidence=replace(proof, no_release_pending=False))["code"] == "OWNER_EVIDENCE_REQUIRED"
    with connect(prepared) as conn:
        conn.execute("INSERT INTO smart_bin_release_attempts VALUES('release',?,'owner','boundary',2)",
                     (first["reservation_id"],))
    assert service.cancel_reserved("m", first["reservation_id"], expected_reservation_revision=0,
                                   request_key="pending", evidence=proof)["code"] == "UNSAFE_STATE"
    assert service.lookup_reservation("m", first["reservation_id"])["state"] == "RESERVED"


@pytest.mark.parametrize("state", ("RELEASE_INTENT", "EXIT_CONFIRMED", "UNCERTAIN"))
def test_unsafe_cancellation_states_refuse(prepared, state):
    first = claim(request(), qualification(), "reserve")
    with connect(prepared) as conn:
        conn.execute("UPDATE smart_bin_reservations SET state=? WHERE id=?", (state, first["reservation_id"]))
    proof = service.NonDispatchEvidence("owner", "episode", 0, 1, True, True, "owner-confirmed")
    assert service.cancel_reserved("m", first["reservation_id"], expected_reservation_revision=0,
                                   request_key="cancel", evidence=proof)["code"] == "UNSAFE_STATE"


def test_last_unit_race_and_retry_with_current_revision(prepared):
    configure(limit=1)
    q = qualification(limit=1)
    first = request("first")
    second = request("second")
    previews = [service.preview(item, q) for item in (first, second)]
    barrier = threading.Barrier(2)

    def contend(index):
        barrier.wait()
        return service.reserve((first, second)[index], q,
                               expected_state_revision=previews[index]["state_revision"],
                               expected_qualification_hash=previews[index]["qualification_hash"],
                               request_key=f"race-{index}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(contend, index) for index in range(2)]
        results = [future.result() for future in futures]
    assert sorted(item["code"] for item in results) == ["OK", "STALE_REVISION"]
    loser = 0 if results[0]["code"] != "OK" else 1
    fresh = qualification(limit=1)
    assert service.reserve((first, second)[loser], fresh, expected_state_revision=1,
                           expected_qualification_hash=service.qualification_digest(fresh),
                           request_key="retry")["code"] == "NO_ELIGIBLE_BIN"
    assert counts(prepared) == (1, 1, 1, 1)


def test_injected_receipt_failure_rolls_back_whole_claim(prepared, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("receipt injection")
    monkeypatch.setattr(service, "_save_receipt", fail)
    with pytest.raises(RuntimeError, match="receipt injection"):
        claim(request(), qualification(), "fault")
    assert counts(prepared) == (0, 0, 0, 0)
    assert _machine_revision() == 0
