"""Synthetic, isolated evidence tests for explicit inactive legacy backfill."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

import pytest

import local_state
import piece_records
import smart_bins_migration as migration
import smart_bins_storage as storage


@contextmanager
def connect(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
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
def db(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite"
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(path))
    monkeypatch.setenv("MACHINE_SPECIFIC_PARAMS_PATH", str(tmp_path / "machine.toml"))
    monkeypatch.setattr(piece_records, "_initialized", False)
    local_state.initialize_local_state()
    storage.initialize_schema()
    local_state.set_machine_id("m")
    local_state.create_bin_layout(name="fixed", layout={"layers": []},
                                  profile_id="profile", make_active=True)
    local_state.set_sorting_profile_sync_state({"profile_id": "profile",
                                                "artifact_hash": "stable-artifact"})
    piece_records.initialize_piece_records()
    yield path
    local_state.close_local_state_keeper()


def piece(uuid, session, *, coords=(0, 0, 0), part="3001", color="2", category="A"):
    now = time.time()
    payload = {"uuid": uuid, "destination_bin": list(coords), "distributed_at": now,
               "part_id": part, "color_id": color, "category_id": category,
               "classification_status": "classified"}
    local_state.record_piece_distribution(payload)
    # RunRecorder uses gc.run_id, independently of local_state's session UUID.
    piece_records.recordPiece(payload, run_id="runtime-run", machine_id="m")
    return payload


def install_and_apply(db, plan=None):
    migration.initialize_migration_schema()
    return migration.apply_legacy_backfill(plan or migration.plan_legacy_backfill(db))


def native_delivery(session, *, uuid="one", expect_duplicate=False, delivered_at=2):
    with storage.critical_transaction() as conn:
        conn.execute("INSERT INTO smart_bin_machines(machine_id) VALUES('m') "
                     "ON CONFLICT(machine_id) DO NOTHING")
        conn.execute("INSERT INTO smart_bin_policy_revisions "
                     "(id,machine_id,compiled_artifact_hash,policy_json,created_at) "
                     "VALUES('native-policy','m','hash','{}',1)")
        conn.execute("INSERT INTO smart_bin_group_keys(id,kind,namespace,provenance) "
                     "VALUES('native-group','category','local','known')")
        conn.execute("INSERT INTO smart_bin_slots "
                     "(id,machine_id,layout_revision,layer_index,section_index,bin_index) "
                     "VALUES('native-slot','m','native-layout',0,0,0)")
        conn.execute("INSERT INTO smart_bin_cycles "
                     "(id,machine_id,slot_id,opened_at,provenance) "
                     "VALUES('native-cycle','m','native-slot',1,'observed')")
        conn.execute("INSERT INTO smart_bin_reservations "
                     "(id,machine_id,piece_uuid,route_attempt,request_key,run_id,policy_revision_id,"
                     "group_key_id,routing_revision,quantity,intended_kind,intended_slot_id,"
                     "intended_cycle_id,state,created_at,updated_at) "
                     "VALUES('native-reservation','m',?,'a','native-request',?,"
                     "'native-policy','native-group',0,1,'BIN','native-slot','native-cycle',"
                     "'COMPLETED',1,1)", (uuid, session["id"]))
        statement = ("INSERT INTO smart_bin_deliveries "
                     "(id,reservation_id,machine_id,piece_uuid,run_id,policy_revision_id,group_key_id,"
                     "quantity,intended_kind,intended_slot_id,intended_cycle_id,actual_kind,"
                     "actual_slot_id,actual_cycle_id,evidence_ref,delivered_at) "
                     "VALUES('native-delivery','native-reservation','m',?,?,'native-policy',"
                     "'native-group',1,'BIN','native-slot','native-cycle','BIN',"
                     "'native-slot','native-cycle','synthetic-test-evidence',?)")
        if expect_duplicate:
            with pytest.raises(sqlite3.IntegrityError, match="piece already credited by historical contribution"):
                conn.execute(statement, (uuid, session["id"], delivered_at))
        else:
            conn.execute(statement, (uuid, session["id"], delivered_at))
        conn.commit()


def test_cross_session_continuity_and_repeated_carry_forward(db):
    first = local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", first)
    second = local_state.start_new_sorting_session(reason="profile_activated")
    piece("two", second)
    third = local_state.start_new_sorting_session(reason="profile_activated")
    plan = migration.plan_legacy_backfill(db)
    assert not any(a["source_ref"].startswith("snapshot:") for a in plan["anchors"])
    assert len(plan["anchors"]) == 1
    assert plan["anchors"][0]["linked_sessions"] == [third["id"], second["id"], first["id"]]
    assert {c["piece_uuid"] for c in plan["contributions"]} == {"one", "two"}
    assert not plan["balances"] and not plan["discrepancies"]
    result = install_and_apply(db, plan)
    assert result["contributions"] == 2 and result["cycles"] == 1
    contents = migration.read_recorded_contents(plan["anchors"][0]["cycle_id"], db)
    assert contents["total"] == 2 and contents["breakdown"][0]["historical"] == 2
    assert not contents["blocked"]


def test_runtime_run_and_sorting_session_are_distinct_provenance(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", session)
    with connect(db) as conn:
        history = conn.execute("SELECT run_id,machine_id,recorded_at FROM piece_records "
                               "WHERE uuid='one'").fetchone()
    assert history[:2] == ("runtime-run", "m") and history[0] != session["id"]
    plan = migration.plan_legacy_backfill(db)
    assert not plan["discrepancies"]
    assert len(plan["contributions"]) == 1
    assert plan["contributions"][0]["runtime_run_id"] == history[0]
    assert plan["contributions"][0]["sorting_session_id"] == session["id"]
    assert plan["contributions"][0]["source_distributed_at"] == history[2]
    install_and_apply(db, plan)
    with connect(db) as conn:
        assert conn.execute(
            "SELECT runtime_run_id,sorting_session_id FROM smart_bin_historical_contributions "
            "WHERE piece_uuid='one'"
        ).fetchone() == ("runtime-run", session["id"])


@pytest.mark.parametrize("change", [
    "UPDATE piece_records SET machine_id='other' WHERE uuid='one'",
    "UPDATE piece_records SET bin_x=1 WHERE uuid='one'",
    "UPDATE piece_records SET recorded_at=recorded_at+1 WHERE uuid='one'",
    "UPDATE piece_records SET run_id=NULL WHERE uuid='one'",
])
def test_conflicting_or_missing_history_evidence_is_uncertain(db, change):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", session)
    with connect(db) as conn:
        conn.execute(change)
        before = conn.execute("SELECT * FROM piece_records WHERE uuid='one'").fetchone()
    plan = migration.plan_legacy_backfill(db)
    assert not plan["contributions"]
    assert any(issue["kind"] == "unverified_piece_history" for issue in plan["discrepancies"])
    assert plan["anchors"][0]["blocked"]
    with connect(db) as conn:
        assert conn.execute("SELECT * FROM piece_records WHERE uuid='one'").fetchone() == before


def test_clear_boundary_and_repeated_snapshot_counted_once(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("before-clear", session)
    local_state.clear_current_session_bins(scope="bin", layer_index=0, section_index=0,
                                           bin_index=0, bin_categories=[[[["A"]]]])
    piece("after-clear", session)
    # A repeated carry-forward snapshot is evidence of one cutoff, not new stock.
    with connect(db) as conn:
        layer = conn.execute("SELECT * FROM bin_snapshot_layers ORDER BY id LIMIT 1").fetchone()
        columns = [row[1] for row in conn.execute("PRAGMA table_info(bin_snapshot_layers)")
                   if row[1] != "id"]
        original = dict(zip(["id", *columns], layer))
        cur = conn.execute("INSERT INTO bin_snapshot_layers (" + ",".join(columns) +
                           ") VALUES (" + ",".join("?" for _ in columns) + ")",
                           tuple(original[name] for name in columns))
        conn.execute("INSERT INTO bin_snapshot_items "
                     "SELECT ?,item_key,part_id,color_id,color_name,category_id,"
                     "classification_status,count,last_distributed_at,thumbnail,top_image,"
                     "bottom_image,brickognize_preview_url FROM bin_snapshot_items "
                     "WHERE snapshot_layer_id=?", (cur.lastrowid, original["id"]))
    plan = migration.plan_legacy_backfill(db)
    assert len(plan["anchors"]) == 2
    assert sorted(c["piece_uuid"] for c in plan["contributions"]) == ["after-clear", "before-clear"]
    assert not plan["discrepancies"]
    install_and_apply(db, plan)
    assert sorted(migration.read_recorded_contents(a["cycle_id"], db)["total"]
                  for a in plan["anchors"]) == [1, 1]


def test_imported_synthetic_identity_stays_opening_balance(db):
    local_state.start_new_sorting_session(reason="profile_activated")
    local_state.import_bin_contents_snapshot({"bins": [{"layer_index": 0, "section_index": 0,
        "bin_index": 0, "piece_count": 3, "unique_item_count": 1,
        "items": [{"key": "3001|2|A|classified", "part_id": "3001", "color_id": "2",
                   "category_id": "A", "classification_status": "classified", "count": 3}],
        "recent_pieces": [{"part_id": "3001", "color_id": "2", "category_id": "A"}]}]})
    plan = migration.plan_legacy_backfill(db)
    assert not plan["contributions"]
    assert plan["anchors"][0]["coverage"] == "historical_unknown"
    assert len(plan["balances"]) == 1 and plan["balances"][0]["quantity"] == 3
    assert any(issue["kind"] == "import_generated_or_unverified_piece"
               for issue in plan["discrepancies"])
    install_and_apply(db, plan)
    contents = migration.read_recorded_contents(plan["anchors"][0]["cycle_id"], db)
    assert contents["total"] == 3 and contents["blocked"]
    assert contents["breakdown"][0]["identity"]["category_id"] == "A"
    assert contents["breakdown"][0]["identity"]["part_namespace"] is None


def import_one_with_supplied_uuid():
    session = local_state.start_new_sorting_session(reason="profile_activated")
    imported = local_state.import_bin_contents_snapshot({"bins": [{
        "layer_index": 0, "section_index": 0, "bin_index": 0,
        "piece_count": 1, "unique_item_count": 1,
        "items": [{"key": "3001|2|A|classified", "part_id": "3001", "color_id": "2",
                   "category_id": "A", "classification_status": "classified", "count": 1}],
        "recent_pieces": [{"uuid": "one", "part_id": "3001", "color_id": "2",
                           "category_id": "A", "classification_status": "classified"}],
    }]})
    assert imported["session_id"] == session["id"] and imported["imported_bins"] == 1
    return session


def test_imported_event_native_overlap_withholds_opening_balance(db):
    session = import_one_with_supplied_uuid()
    native_delivery(session, uuid="one")
    legacy_tables = ("sorting_sessions", "bin_state_current", "bin_item_aggregates",
                     "piece_events", "bin_events", "piece_records")
    with connect(db) as conn:
        before = {table: conn.execute(f"SELECT * FROM {table}").fetchall()
                  for table in legacy_tables}
        native_before = conn.execute(
            "SELECT * FROM smart_bin_deliveries WHERE piece_uuid='one'"
        ).fetchall()
    plan = migration.plan_legacy_backfill(db)
    assert len(plan["anchors"]) == 1 and plan["anchors"][0]["blocked"]
    assert not plan["contributions"] and not plan["balances"]
    kinds = {issue["kind"] for issue in plan["discrepancies"]}
    assert "import_generated_or_unverified_piece" in kinds
    assert "potential_native_balance_overlap" in kinds
    install_and_apply(db, plan)
    with connect(db) as conn:
        assert {table: conn.execute(f"SELECT * FROM {table}").fetchall()
                for table in legacy_tables} == before
        assert conn.execute("SELECT * FROM smart_bin_deliveries WHERE piece_uuid='one'").fetchall() == native_before
    historical = migration.read_recorded_contents(plan["anchors"][0]["cycle_id"], db)
    assert historical["total"] == 0 and historical["blocked"]
    assert any(issue["kind"] == "potential_native_balance_overlap"
               for issue in historical["discrepancies"])
    assert migration.read_recorded_contents("native-cycle", db)["total"] == 1


def test_imported_supplied_uuid_without_overlap_retains_unknown_balance(db):
    import_one_with_supplied_uuid()
    plan = migration.plan_legacy_backfill(db)
    assert not plan["contributions"]
    assert len(plan["balances"]) == 1 and plan["balances"][0]["quantity"] == 1
    assert plan["balances"][0]["coverage"] == "historical_unknown"
    assert any(issue["kind"] == "import_generated_or_unverified_piece"
               for issue in plan["discrepancies"])
    assert not any(issue["kind"] == "potential_native_balance_overlap"
                   for issue in plan["discrepancies"])
    install_and_apply(db, plan)
    contents = migration.read_recorded_contents(plan["anchors"][0]["cycle_id"], db)
    assert contents["total"] == 1 and contents["blocked"]
    assert contents["breakdown"][0]["opening_balance"] == 1
    assert contents["coverage"] == "historical_unknown"


def test_pre_native_import_plan_rejected_after_overlap_appears(db):
    session = import_one_with_supplied_uuid()
    unsafe_plan = migration.plan_legacy_backfill(db)
    assert len(unsafe_plan["balances"]) == 1
    migration.initialize_migration_schema()
    native_delivery(session, uuid="one")
    with pytest.raises(migration.MigrationConflict, match="stale or modified"):
        migration.apply_legacy_backfill(unsafe_plan)
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM smart_bin_migration_imports").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM smart_bin_opening_balances").fetchone()[0] == 0


def test_pre_native_import_plan_cannot_replay_after_overlap_appears(db):
    session = import_one_with_supplied_uuid()
    old_plan = migration.plan_legacy_backfill(db)
    install_and_apply(db, old_plan)
    native_delivery(session, uuid="one")
    with pytest.raises(migration.MigrationConflict, match="stale or modified"):
        migration.apply_legacy_backfill(old_plan)
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM smart_bin_migration_imports").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM smart_bin_opening_balances").fetchone()[0] == 1


def test_aggregate_only_unknown_group_is_explicit(db):
    local_state.start_new_sorting_session(reason="profile_activated")
    local_state.import_bin_contents_snapshot({"bins": [{"layer_index": 0, "section_index": 0,
        "bin_index": 0, "piece_count": 2, "unique_item_count": 0}]})
    plan = migration.plan_legacy_backfill(db)
    assert not plan["contributions"]
    assert plan["balances"][0]["quantity"] == 2
    assert any(issue["kind"] == "unidentified_group" for issue in plan["discrepancies"])
    install_and_apply(db, plan)
    contents = migration.read_recorded_contents(plan["anchors"][0]["cycle_id"], db)
    assert contents["total"] == 2 and contents["blocked"]
    assert contents["breakdown"][0]["identity"]["kind"] == "unidentified"


def test_group_level_opening_balance_subtracts_only_linked_pieces(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("known", session)
    with connect(db) as conn:
        conn.execute("INSERT INTO bin_item_aggregates "
                     "(session_id,layer_index,section_index,bin_index,item_key,part_id,"
                     "color_id,category_id,classification_status,count) "
                     "VALUES(?,0,0,0,'3002|5|B|classified','3002','5','B','classified',2)",
                     (session["id"],))
        conn.execute("UPDATE bin_state_current SET piece_count=3,unique_item_count=2")
    plan = migration.plan_legacy_backfill(db)
    assert [row["piece_uuid"] for row in plan["contributions"]] == ["known"]
    assert len(plan["balances"]) == 1 and plan["balances"][0]["quantity"] == 2
    assert not plan["discrepancies"]
    install_and_apply(db, plan)
    contents = migration.read_recorded_contents(plan["anchors"][0]["cycle_id"], db)
    assert contents["total"] == 3
    by_part = {row["identity"]["part_id"]: row for row in contents["breakdown"]}
    assert by_part["3001"]["historical"] == 1
    assert by_part["3002"]["opening_balance"] == 2
    assert by_part["3002"]["identity"]["category_id"] == "B"
    assert by_part["3002"]["identity"]["color_namespace"] is None


def test_group_count_conflict_and_negative_residual_block_credit(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", session)
    piece("two", session)
    with connect(db) as conn:
        conn.execute("UPDATE bin_state_current SET piece_count=1")
        conn.execute("UPDATE bin_item_aggregates SET count=1")
    plan = migration.plan_legacy_backfill(db)
    kinds = {issue["kind"] for issue in plan["discrepancies"]}
    assert "negative_group_residual" in kinds and "negative_total_residual" in kinds
    assert not plan["contributions"] and not plan["balances"]
    install_and_apply(db, plan)
    contents = migration.read_recorded_contents(plan["anchors"][0]["cycle_id"], db)
    assert contents["total"] == 0 and contents["legacy_total"] == 1 and contents["blocked"]


def test_zero_count_with_positive_aggregates_is_blocked_not_hidden(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", session)
    with connect(db) as conn:
        conn.execute("UPDATE bin_state_current SET piece_count=0")
        before = conn.execute("SELECT * FROM bin_state_current").fetchall(), (
            conn.execute("SELECT * FROM bin_item_aggregates").fetchall())
    plan = migration.plan_legacy_backfill(db)
    assert len(plan["anchors"]) == 1 and plan["anchors"][0]["total"] == 0
    assert any(issue["kind"] == "count_group_conflict" for issue in plan["discrepancies"])
    assert not plan["contributions"] and not plan["balances"]
    install_and_apply(db, plan)
    with connect(db) as conn:
        assert before == (conn.execute("SELECT * FROM bin_state_current").fetchall(),
                          conn.execute("SELECT * FROM bin_item_aggregates").fetchall())
    assert migration.read_recorded_contents(plan["anchors"][0]["cycle_id"], db)["blocked"]


def test_orphan_positive_aggregate_is_plan_blocker(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", session)
    with connect(db) as conn:
        conn.execute("DELETE FROM bin_state_current")
        before = conn.execute("SELECT * FROM bin_item_aggregates").fetchall()
    plan = migration.plan_legacy_backfill(db)
    assert any(issue["kind"] == "orphan_positive_aggregate" for issue in plan["global_blockers"])
    migration.initialize_migration_schema()
    with pytest.raises(migration.MigrationConflict, match="unowned blockers"):
        migration.apply_legacy_backfill(plan)
    with connect(db) as conn:
        assert conn.execute("SELECT * FROM bin_item_aggregates").fetchall() == before
        assert conn.execute("SELECT COUNT(*) FROM smart_bin_migration_imports").fetchone()[0] == 0


@pytest.mark.parametrize("value", [None, "missing-session", "closed-session"])
def test_unowned_occupancy_with_missing_or_dangling_active_session_is_blocked(db, value):
    local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", local_state.get_active_sorting_session())
    with connect(db) as conn:
        if value is None:
            conn.execute("DELETE FROM metadata WHERE key='active_sorting_session_id'")
        elif value == "closed-session":
            conn.execute("UPDATE sorting_sessions SET status='closed'")
        else:
            conn.execute("UPDATE metadata SET value=? WHERE key='active_sorting_session_id'", (value,))
        before = conn.execute("SELECT * FROM bin_state_current").fetchall()
    plan = migration.plan_legacy_backfill(db)
    assert any(issue["kind"] == "unowned_current_occupancy" for issue in plan["global_blockers"])
    assert not plan["anchors"] and not plan["balances"]
    migration.initialize_migration_schema()
    with pytest.raises(migration.MigrationConflict, match="unowned blockers"):
        migration.apply_legacy_backfill(plan)
    with connect(db) as conn:
        assert conn.execute("SELECT * FROM bin_state_current").fetchall() == before
        assert conn.execute("SELECT COUNT(*) FROM smart_bin_migration_imports").fetchone()[0] == 0


def test_group_total_disagreement_and_unsupported_membership(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", session)
    with connect(db) as conn:
        conn.execute("UPDATE bin_state_current SET piece_count=2")
    plan = migration.plan_legacy_backfill(db)
    assert any(issue["kind"] == "count_group_conflict" for issue in plan["discrepancies"])
    assert not plan["balances"] and not plan["contributions"]
    with connect(db) as conn:
        conn.execute("UPDATE bin_state_current SET piece_count=1")
        conn.execute("UPDATE bin_item_aggregates SET item_key='other', part_id='3002'")
    plan = migration.plan_legacy_backfill(db)
    assert any(issue["kind"] == "unsupported_piece_group" for issue in plan["discrepancies"])
    assert not plan["contributions"]


def test_missing_layout_evidence_limits_cross_session_lineage(db):
    first = local_state.start_new_sorting_session(reason="profile_activated")
    piece("first", first)
    second = local_state.start_new_sorting_session(reason="profile_activated")
    piece("second", second)
    with connect(db) as conn:
        conn.execute("UPDATE bin_layouts SET updated_at=?", (time.time() + 100,))
    plan = migration.plan_legacy_backfill(db)
    anchor = plan["anchors"][0]
    assert anchor["linked_sessions"] == [second["id"]]
    assert anchor["coverage"] == "historical_unknown"
    assert {c["piece_uuid"] for c in plan["contributions"]} == {"second"}
    assert plan["balances"][0]["quantity"] == 1


def test_unlinked_earlier_event_cannot_overlap_native_and_opening_balance(db):
    first = local_state.start_new_sorting_session(reason="profile_activated")
    piece("first", first)
    second = local_state.start_new_sorting_session(reason="profile_activated")
    piece("second", second)
    with connect(db) as conn:
        conn.execute("UPDATE bin_layouts SET updated_at=?", (time.time() + 100,))
        before = conn.execute("SELECT * FROM bin_state_current").fetchall(), (
            conn.execute("SELECT * FROM piece_events").fetchall())
    native_delivery(first, uuid="first")
    plan = migration.plan_legacy_backfill(db)
    assert plan["anchors"][0]["linked_sessions"] == [second["id"]]
    assert {row["piece_uuid"] for row in plan["contributions"]} == set()
    assert not plan["balances"]
    assert any(issue["kind"] == "potential_native_balance_overlap"
               for issue in plan["discrepancies"])
    install_and_apply(db, plan)
    with connect(db) as conn:
        assert before == (conn.execute("SELECT * FROM bin_state_current").fetchall(),
                          conn.execute("SELECT * FROM piece_events").fetchall())
    assert migration.read_recorded_contents(plan["anchors"][0]["cycle_id"], db)["blocked"]


def test_unlinked_residual_with_later_nonoverlapping_native_delivery(db):
    first = local_state.start_new_sorting_session(reason="profile_activated")
    piece("first", first)
    second = local_state.start_new_sorting_session(reason="profile_activated")
    piece("second", second)
    with connect(db) as conn:
        conn.execute("UPDATE bin_layouts SET updated_at=?", (time.time() + 100,))
    native_delivery(first, uuid="later-unrelated", delivered_at=time.time() + 100)
    plan = migration.plan_legacy_backfill(db)
    assert not plan["discrepancies"]
    assert plan["anchors"][0]["coverage"] == "historical_unknown"
    assert {row["piece_uuid"] for row in plan["contributions"]} == {"second"}
    assert len(plan["balances"]) == 1 and plan["balances"][0]["quantity"] == 1
    install_and_apply(db, plan)
    contents = migration.read_recorded_contents(plan["anchors"][0]["cycle_id"], db)
    assert contents["total"] == 2 and contents["coverage"] == "historical_unknown"
    assert not contents["blocked"]


def test_conflicting_own_event_still_reports_native_overlap(db):
    first = local_state.start_new_sorting_session(reason="profile_activated")
    piece("same", first)
    second = local_state.start_new_sorting_session(reason="profile_activated")
    piece("same", second, part="3002")
    native_delivery(second, uuid="same")
    plan = migration.plan_legacy_backfill(db)
    kinds = {issue["kind"] for issue in plan["discrepancies"]}
    assert "conflicting_piece_identity" in kinds
    assert "potential_native_balance_overlap" in kinds
    assert not plan["contributions"] and not plan["balances"]


def test_proven_clear_separates_earlier_native_from_current_balance(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("before-clear", session)
    native_delivery(session, uuid="unrelated-before-clear")
    local_state.clear_current_session_bins(scope="bin", layer_index=0, section_index=0,
                                           bin_index=0)
    piece("after-clear", session)
    with connect(db) as conn:
        conn.execute("INSERT INTO bin_item_aggregates "
                     "(session_id,layer_index,section_index,bin_index,item_key,part_id,"
                     "color_id,category_id,classification_status,count) "
                     "VALUES(?,0,0,0,'3002|5|B|classified','3002','5','B','classified',1)",
                     (session["id"],))
        conn.execute("UPDATE bin_state_current SET piece_count=2,unique_item_count=2")
    plan = migration.plan_legacy_backfill(db)
    current = next(anchor for anchor in plan["anchors"]
                   if anchor["source_ref"].startswith("current:"))
    assert not any(issue["kind"] == "potential_native_balance_overlap"
                   and issue["cycle_id"] == current["cycle_id"]
                   for issue in plan["discrepancies"])
    assert any(row["cycle_id"] == current["cycle_id"] and row["quantity"] == 1
               for row in plan["balances"])


def test_snapshot_own_clear_does_not_hide_native_overlap(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("known", session)
    with connect(db) as conn:
        conn.execute("INSERT INTO bin_item_aggregates "
                     "(session_id,layer_index,section_index,bin_index,item_key,part_id,"
                     "color_id,category_id,classification_status,count) "
                     "VALUES(?,0,0,0,'3002|5|B|classified','3002','5','B','classified',1)",
                     (session["id"],))
        conn.execute("UPDATE bin_state_current SET piece_count=2,unique_item_count=2")
    native_delivery(session, uuid="possible-residual")
    local_state.clear_current_session_bins(scope="bin", layer_index=0, section_index=0,
                                           bin_index=0)
    plan = migration.plan_legacy_backfill(db)
    assert len(plan["anchors"]) == 1
    assert plan["anchors"][0]["source_ref"].startswith("snapshot:")
    assert any(issue["kind"] == "potential_native_balance_overlap"
               for issue in plan["discrepancies"])
    assert not plan["contributions"] and not plan["balances"]


def test_no_double_credit_across_native_and_historical_origins(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", session)
    plan = migration.plan_legacy_backfill(db)
    install_and_apply(db, plan)
    native_delivery(session, expect_duplicate=True)
    historical = migration.read_recorded_contents(plan["anchors"][0]["cycle_id"], db)
    assert historical["total"] == 1
    assert migration.read_recorded_contents("native-cycle", db)["total"] == 0


def test_native_first_duplicate_blocks_historical_credit(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", session)
    native_delivery(session)
    plan = migration.plan_legacy_backfill(db)
    assert any(issue["kind"] == "native_delivery_duplicate" for issue in plan["discrepancies"])
    assert not plan["contributions"] and not plan["balances"]
    install_and_apply(db, plan)
    assert migration.read_recorded_contents(plan["anchors"][0]["cycle_id"], db)["total"] == 0
    assert migration.read_recorded_contents("native-cycle", db)["total"] == 1


def test_extension_install_is_explicit_and_rejects_incomplete_schema(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", session)
    assert migration.plan_legacy_backfill(db)["contributions"]
    with connect(db) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='smart_bin_migration_versions'").fetchone() is None
    assert migration.initialize_migration_schema() == migration.EXTENSION_VERSION
    assert migration.initialize_migration_schema() == migration.EXTENSION_VERSION
    with connect(db) as conn:
        assert conn.execute("SELECT version FROM smart_bin_schema_versions").fetchone()[0] == 2
        conn.execute("DROP TRIGGER smart_bin_migration_no_native_duplicate_insert")
    with pytest.raises(migration.MigrationConflict, match="incomplete migration extension"):
        migration.initialize_migration_schema()


def test_old_extension_version_cannot_plan_or_replay(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", session)
    plan = migration.plan_legacy_backfill(db)
    assert plan["scope_key"] == "legacy-bin-backfill-v2"
    migration.initialize_migration_schema()
    with connect(db) as conn:
        conn.execute("UPDATE smart_bin_migration_versions SET version=1")
    with pytest.raises(migration.MigrationConflict, match="unsupported migration extension version"):
        migration.plan_legacy_backfill(db)
    with pytest.raises(migration.MigrationConflict, match="unsupported migration extension version"):
        migration.apply_legacy_backfill(plan)
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM smart_bin_migration_imports").fetchone()[0] == 0


def test_duplicate_piece_conflict_and_ambiguous_membership(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("same", session)
    local_state.clear_current_session_bins(scope="bin", layer_index=0, section_index=0,
                                           bin_index=0)
    # Legacy uniqueness is only (session, UUID), so use a second session.
    second = local_state.start_new_sorting_session(reason="profile_activated")
    piece("same", second, part="3002")
    plan = migration.plan_legacy_backfill(db)
    assert any(issue["kind"] == "conflicting_piece_identity" for issue in plan["discrepancies"])
    assert not plan["contributions"]


def test_unmapped_legacy_event_still_conflicts_with_current_piece(db):
    first = local_state.start_new_sorting_session(reason="profile_activated")
    piece("same", first)
    second = local_state.start_new_sorting_session(reason="profile_activated")
    piece("same", second)
    with connect(db) as conn:
        conn.execute("UPDATE bin_layouts SET updated_at=?", (time.time() + 100,))
    plan = migration.plan_legacy_backfill(db)
    assert plan["anchors"][0]["linked_sessions"] == [second["id"]]
    assert any(issue["kind"] == "conflicting_piece_identity" for issue in plan["discrepancies"])
    assert not plan["contributions"] and not plan["balances"]


def test_exact_replay_stale_plan_and_injected_failure(db, monkeypatch):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", session)
    plan = migration.plan_legacy_backfill(db)
    migration.initialize_migration_schema()
    original = migration._write_plan

    def fail_after_writes(conn, proposed):
        original(conn, proposed)
        raise RuntimeError("injected after all writes")

    monkeypatch.setattr(migration, "_write_plan", fail_after_writes)
    with pytest.raises(RuntimeError, match="injected"):
        migration.apply_legacy_backfill(plan)
    with connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_migration_imports").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM smart_bin_cycles").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM smart_bin_audit_events").fetchone()[0] == 0
    monkeypatch.setattr(migration, "_write_plan", original)
    first = migration.apply_legacy_backfill(plan)
    again = migration.apply_legacy_backfill(plan)
    assert first["replayed"] is False and again["replayed"] is True
    with connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_migration_imports").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM smart_bin_audit_events").fetchone()[0] == 1
    piece("two", session)
    with pytest.raises(migration.MigrationConflict, match="stale"):
        migration.apply_legacy_backfill(plan)
    with pytest.raises(migration.MigrationConflict, match="earlier import"):
        migration.apply_legacy_backfill(migration.plan_legacy_backfill(db))


def test_legacy_rows_settings_and_corrections_unchanged(db):
    session = local_state.start_new_sorting_session(reason="profile_activated")
    piece("one", session)
    with connect(db) as conn:
        conn.execute("UPDATE piece_records SET part_correct=0, correction_updated_at=17 WHERE uuid='one'")
        before = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in
                  ("sorting_sessions", "bin_state_current", "bin_item_aggregates",
                   "piece_events", "bin_events", "state_entries", "piece_records")}
    plan = migration.plan_legacy_backfill(db)
    install_and_apply(db, plan)
    with connect(db) as conn:
        after = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in before}
        assert after == before
        assert conn.execute("SELECT part_correct,correction_updated_at FROM piece_records "
                            "WHERE uuid='one'").fetchone() == (0, 17)
        assert conn.execute("SELECT version FROM smart_bin_schema_versions").fetchone()[0] == 2
        assert conn.execute("SELECT version FROM smart_bin_migration_versions").fetchone()[0] == 2
