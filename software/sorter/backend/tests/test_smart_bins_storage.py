"""Isolated SQLite checks for the inactive smart-bin storage foundation."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

import local_state
import piece_records
import smart_bins_storage as storage


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite"
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(path))
    monkeypatch.setenv("MACHINE_SPECIFIC_PARAMS_PATH", str(tmp_path / "machine.toml"))
    monkeypatch.setattr(piece_records, "_initialized", False)
    yield path
    local_state.close_local_state_keeper()


@contextmanager
def connect(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
    finally:
        conn.close()


def tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def prepared(db_path):
    local_state.initialize_local_state()
    storage.initialize_schema()
    return db_path


def seed(conn):
    conn.execute("INSERT INTO smart_bin_machines(machine_id) VALUES('m')")
    conn.execute(
        "INSERT INTO smart_bin_policy_revisions(id,machine_id,compiled_artifact_hash,policy_json,created_at) "
        "VALUES('p','m','hash','{}',1)"
    )
    conn.execute(
        "INSERT INTO smart_bin_group_keys(id,kind,namespace,provenance) "
        "VALUES('g','category','local','known')"
    )
    conn.execute(
        "INSERT INTO smart_bin_slots(id,machine_id,layout_revision,layer_index,section_index,bin_index) "
        "VALUES('s','m','layout',0,0,0)"
    )
    conn.execute("INSERT INTO smart_bin_containers(id,machine_id) VALUES('c','m')")
    conn.execute(
        "INSERT INTO smart_bin_cycles(id,machine_id,container_id,slot_id,opened_at,provenance) "
        "VALUES('cy','m','c','s',1,'observed')"
    )
    conn.execute(
        "INSERT INTO sorting_sessions(id,machine_id,started_at,status) VALUES('run','m',1,'active')"
    )


def reservation(conn, *, id="r", piece="piece", attempt="a", quantity=1, state="RESERVED"):
    conn.execute(
        "INSERT INTO smart_bin_reservations "
        "(id,machine_id,piece_uuid,route_attempt,request_key,run_id,policy_revision_id,"
        "group_key_id,routing_revision,quantity,intended_kind,intended_slot_id,"
        "intended_cycle_id,state,created_at,updated_at) "
        "VALUES(?,?,?,?,?,'run','p','g',0,?,'BIN','s','cy',?,1,1)",
        (id, "m", piece, attempt, id, quantity, state),
    )


def seed_alternatives(conn):
    conn.execute("INSERT INTO smart_bin_machines(machine_id) VALUES('m2')")
    conn.execute("INSERT INTO smart_bin_policy_revisions(id,machine_id,compiled_artifact_hash,policy_json,created_at) VALUES('p2','m2','hash2','{}',1)")
    conn.execute("INSERT INTO smart_bin_policy_revisions(id,machine_id,compiled_artifact_hash,policy_json,created_at) VALUES('p-other','m','hash3','{}',1)")
    conn.execute("INSERT INTO smart_bin_group_keys(id,kind,namespace,provenance) VALUES('g-other','category','local','known')")
    conn.execute("INSERT INTO smart_bin_slots(id,machine_id,layout_revision,layer_index,section_index,bin_index) VALUES('s2','m','layout',0,0,1)")
    conn.execute("INSERT INTO smart_bin_cycles(id,machine_id,slot_id,opened_at,provenance) VALUES('cy2','m','s2',1,'observed')")
    conn.execute("INSERT INTO smart_bin_slots(id,machine_id,layout_revision,layer_index,section_index,bin_index) VALUES('s-m2','m2','layout',0,0,0)")
    conn.execute("INSERT INTO smart_bin_cycles(id,machine_id,slot_id,opened_at,provenance) VALUES('cy-m2','m2','s-m2',1,'observed')")
    conn.execute("INSERT INTO sorting_sessions(id,machine_id,started_at,status) VALUES('run-other','m',1,'active')")
    conn.execute("INSERT INTO sorting_sessions(id,machine_id,started_at,status) VALUES('run-m2','m2',1,'active')")


def delivery(conn, **overrides):
    fields = dict(id="d", reservation_id="r", machine_id="m", piece_uuid="piece",
                  run_id="run", policy_revision_id="p", group_key_id="g", quantity=1,
                  intended_kind="BIN", intended_slot_id="s", intended_cycle_id="cy",
                  actual_kind="BIN", actual_slot_id="s", actual_cycle_id="cy",
                  evidence_ref="exit", delivered_at=2)
    fields.update(overrides)
    conn.execute(
        "INSERT INTO smart_bin_deliveries (" + ",".join(fields) + ") VALUES (" +
        ",".join("?" for _ in fields) + ")", tuple(fields.values())
    )


def test_explicit_rerunnable_init_and_legacy_state_preserved(db_path):
    assert not db_path.exists()
    with pytest.raises(storage.UnsupportedSmartBinSchema):
        storage.initialize_schema()
    with connect(db_path) as conn:
        assert not any(name.startswith("smart_bin_") for name in tables(conn))
    local_state.initialize_local_state()
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sorting_sessions(id,machine_id,started_at,status) "
            "VALUES('existing','legacy',1,'active')"
        )
        conn.execute("INSERT INTO state_entries(key,json_value,updated_at) VALUES('setting','{\"enabled\":true}',1)")
        before = conn.execute("SELECT * FROM sorting_sessions WHERE id='existing'").fetchone()
        setting = conn.execute("SELECT * FROM state_entries WHERE key='setting'").fetchone()
        assert not any(name.startswith("smart_bin_") for name in tables(conn))
    assert storage.initialize_schema() == storage.SCHEMA_VERSION
    assert storage.initialize_schema() == storage.SCHEMA_VERSION
    with connect(db_path) as conn:
        assert storage.check_schema_version(conn) == storage.SCHEMA_VERSION
        assert conn.execute("SELECT * FROM sorting_sessions WHERE id='existing'").fetchone() == before
        assert conn.execute("SELECT * FROM state_entries WHERE key='setting'").fetchone() == setting
        assert conn.execute("SELECT count(*) FROM smart_bin_schema_versions").fetchone()[0] == 1


def test_init_failure_rolls_back_all_ledger_ddl(db_path, monkeypatch):
    local_state.initialize_local_state()
    original = storage._create_schema

    def fail_after_first_table(conn):
        conn.execute(storage._SCHEMA_DDL[0])
        raise RuntimeError("injected failure")

    monkeypatch.setattr(storage, "_create_schema", fail_after_first_table)
    with pytest.raises(RuntimeError, match="injected failure"):
        storage.initialize_schema()
    with connect(db_path) as conn:
        assert not any(name.startswith("smart_bin_") for name in tables(conn))
        assert "sorting_sessions" in tables(conn)
    monkeypatch.setattr(storage, "_create_schema", original)
    assert storage.initialize_schema() == storage.SCHEMA_VERSION


def test_constraints_and_intended_destination_immutability(prepared):
    with storage.critical_transaction() as conn:
        seed(conn)
        reservation(conn)
        conn.commit()
    with connect(prepared) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            reservation(conn, id="bad-quantity", piece="q", quantity=0)
        with pytest.raises(sqlite3.IntegrityError):
            reservation(conn, id="duplicate-live", piece="piece", attempt="b")
        with pytest.raises(sqlite3.IntegrityError, match="reservation intended destination is immutable"):
            conn.execute("UPDATE smart_bin_reservations SET intended_kind='REJECT', "
                         "intended_slot_id=NULL, intended_cycle_id=NULL WHERE id='r'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO smart_bin_cycles(id,machine_id,slot_id,opened_at,provenance) "
                         "VALUES('second','m','s',2,'observed')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO smart_bin_opening_balances(id,cycle_id,quantity,provenance,coverage,created_at) "
                         "VALUES('bad','cy',-1,'observed','complete',2)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO smart_bin_opening_balances(id,cycle_id,quantity,provenance,coverage,created_at) "
                         "VALUES('missing','absent',1,'observed','complete',2)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO smart_bin_assignments(id,machine_id,slot_id,policy_revision_id,group_key_id,routing_revision,created_at) "
                         "VALUES('wrong-machine','other','s','p','g',0,2)")
        delivery(conn)
        with pytest.raises(sqlite3.IntegrityError, match="delivery evidenced destination is immutable"):
            conn.execute("UPDATE smart_bin_deliveries SET actual_kind='REJECT', "
                         "actual_slot_id=NULL, actual_cycle_id=NULL WHERE id='d'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO smart_bin_deliveries "
                         "(id,reservation_id,machine_id,piece_uuid,run_id,policy_revision_id,group_key_id,"
                         "quantity,intended_kind,intended_slot_id,intended_cycle_id,actual_kind,"
                         "actual_slot_id,actual_cycle_id,evidence_ref,delivered_at) "
                         "VALUES('d2','r','m','piece','run','p','g',1,'BIN','s','cy','BIN','s','cy','exit',2)")
        assert conn.execute("SELECT intended_slot_id FROM smart_bin_reservations WHERE id='r'").fetchone()[0] == "s"


@pytest.mark.parametrize("overrides", [
    {"piece_uuid": "different"},
    {"run_id": "run-other"},
    {"policy_revision_id": "p-other"},
    {"group_key_id": "g-other"},
    {"quantity": 2},
    {"intended_kind": "REJECT", "intended_slot_id": None, "intended_cycle_id": None},
    {"intended_slot_id": "s2"},
    {"intended_cycle_id": "cy2"},
    {"machine_id": "m2", "run_id": "run-m2", "policy_revision_id": "p2",
     "intended_slot_id": "s-m2", "intended_cycle_id": "cy-m2",
     "actual_slot_id": "s-m2", "actual_cycle_id": "cy-m2"},
])
def test_delivery_insert_must_match_reservation(prepared, overrides):
    with connect(prepared) as conn:
        seed(conn)
        seed_alternatives(conn)
        reservation(conn)
        with pytest.raises(sqlite3.IntegrityError, match="delivery does not match reservation"):
            delivery(conn, **overrides)


@pytest.mark.parametrize("overrides,error", [
    ({"piece_uuid": "different"}, "delivery does not match reservation"),
    ({"run_id": "run-other"}, "delivery does not match reservation"),
    ({"intended_kind": "REJECT", "intended_slot_id": None, "intended_cycle_id": None},
     "delivery (does not match reservation|intended destination is immutable)"),
    ({"machine_id": "m2", "run_id": "run-m2", "policy_revision_id": "p2",
     "intended_slot_id": "s-m2", "intended_cycle_id": "cy-m2",
     "actual_slot_id": "s-m2", "actual_cycle_id": "cy-m2"},
     "delivery (does not match reservation|intended destination is immutable|evidenced destination is immutable)"),
])
def test_delivery_update_must_match_reservation(prepared, overrides, error):
    with connect(prepared) as conn:
        seed(conn)
        seed_alternatives(conn)
        reservation(conn)
        delivery(conn)
        update = ", ".join(f"{name}=?" for name in overrides)
        with pytest.raises(sqlite3.IntegrityError, match=error):
            conn.execute(f"UPDATE smart_bin_deliveries SET {update} WHERE id='d'", tuple(overrides.values()))
        assert conn.execute("SELECT piece_uuid FROM smart_bin_deliveries WHERE id='d'").fetchone()[0] == "piece"


@pytest.mark.parametrize("column,value", [
    ("piece_uuid", "different"), ("run_id", "run-other"),
    ("policy_revision_id", "p-other"), ("group_key_id", "g-other"),
    ("quantity", 2),
])
def test_delivered_reservation_facts_cannot_change(prepared, column, value):
    with connect(prepared) as conn:
        seed(conn)
        seed_alternatives(conn)
        reservation(conn)
        delivery(conn)
        with pytest.raises(sqlite3.IntegrityError, match="reservation fact is referenced by delivery"):
            conn.execute(f"UPDATE smart_bin_reservations SET {column}=? WHERE id='r'", (value,))


def test_delivery_reservation_identity_cannot_be_repointed(prepared):
    with connect(prepared) as conn:
        seed(conn)
        reservation(conn)
        delivery(conn)
        conn.execute("UPDATE smart_bin_reservations SET state='COMPLETED' WHERE id='r'")
        reservation(conn, id="r2", attempt="b")
        with pytest.raises(sqlite3.IntegrityError, match="delivery reservation identity is immutable"):
            conn.execute("UPDATE smart_bin_deliveries SET reservation_id='r2' WHERE id='d'")


def test_consistent_delivery_and_independent_actual_destination(prepared):
    with connect(prepared) as conn:
        seed(conn)
        seed_alternatives(conn)
        reservation(conn)
        # The actual receiving cycle may differ from the reservation's intent.
        delivery(conn, actual_slot_id="s2", actual_cycle_id="cy2")
        assert conn.execute("SELECT intended_cycle_id,actual_cycle_id FROM smart_bin_deliveries "
                            "WHERE id='d'").fetchone() == ("cy", "cy2")
        # Historical evidence remains valid when the cycle is detached.
        conn.execute("UPDATE smart_bin_cycles SET slot_id=NULL WHERE id='cy2'")
        assert conn.execute("SELECT actual_cycle_id FROM smart_bin_deliveries WHERE id='d'").fetchone()[0] == "cy2"


def test_cross_machine_sorting_session_rejected(prepared):
    with connect(prepared) as conn:
        seed(conn)
        seed_alternatives(conn)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            conn.execute(
                "INSERT INTO smart_bin_reservations "
                "(id,machine_id,piece_uuid,route_attempt,request_key,run_id,policy_revision_id,"
                "group_key_id,routing_revision,quantity,intended_kind,intended_slot_id,"
                "intended_cycle_id,state,created_at,updated_at) "
                "VALUES('cross','m','other','a','cross','run-m2','p','g',0,1,'BIN','s','cy','RESERVED',1,1)"
            )
        reservation(conn)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            conn.execute("UPDATE sorting_sessions SET machine_id='m2' WHERE id='run'")


def test_cross_machine_updates_cannot_rewrite_reject_delivery(prepared):
    with connect(prepared) as conn:
        seed(conn)
        seed_alternatives(conn)
        conn.execute(
            "INSERT INTO smart_bin_reservations "
            "(id,machine_id,piece_uuid,route_attempt,request_key,run_id,policy_revision_id,"
            "group_key_id,routing_revision,quantity,intended_kind,state,created_at,updated_at) "
            "VALUES('reject-r','m','reject-piece','a','reject-r','run','p','g',0,1,"
            "'REJECT','RESERVED',1,1)"
        )
        delivery(conn, id="reject-d", reservation_id="reject-r", piece_uuid="reject-piece",
                 intended_kind="REJECT", intended_slot_id=None, intended_cycle_id=None,
                 actual_kind="REJECT", actual_slot_id=None, actual_cycle_id=None)
        with pytest.raises(sqlite3.IntegrityError, match="delivery does not match reservation"):
            conn.execute("UPDATE smart_bin_deliveries SET machine_id='m2', "
                         "run_id='run-m2', policy_revision_id='p2' WHERE id='reject-d'")
        with pytest.raises(sqlite3.IntegrityError, match="reservation fact is referenced by delivery"):
            conn.execute("UPDATE smart_bin_reservations SET machine_id='m2', "
                         "run_id='run-m2', policy_revision_id='p2' WHERE id='reject-r'")


def test_primary_text_ids_are_required(prepared):
    with connect(prepared) as conn:
        for table, column in (("smart_bin_machines", "machine_id"),
                              ("smart_bin_policy_revisions", "id"),
                              ("smart_bin_group_keys", "id"),
                              ("smart_bin_slots", "id"),
                              ("smart_bin_assignments", "id"),
                              ("smart_bin_containers", "id"),
                              ("smart_bin_cycles", "id"),
                              ("smart_bin_reservations", "id"),
                              ("smart_bin_release_attempts", "id"),
                              ("smart_bin_deliveries", "id"),
                              ("smart_bin_opening_balances", "id"),
                              ("smart_bin_audit_events", "id"),
                              ("smart_bin_discrepancies", "id"),
                              ("smart_bin_external_operations", "id")):
            info = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
            assert info[column][3] == 1, (table, column)
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
            conn.execute("INSERT INTO smart_bin_machines(machine_id) VALUES(NULL)")
        seed(conn)
        reservation(conn)
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
            delivery(conn, id=None)


def test_old_schema_version_is_rejected_without_repair(db_path):
    local_state.initialize_local_state()
    with connect(db_path) as conn:
        conn.execute("CREATE TABLE smart_bin_schema_versions (singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL, initialized_at REAL NOT NULL)")
        conn.execute("INSERT INTO smart_bin_schema_versions VALUES(1,1,1)")
        conn.execute("CREATE TABLE smart_bin_machines (machine_id TEXT PRIMARY KEY, state_revision INTEGER NOT NULL)")
        before = tables(conn)
    with pytest.raises(storage.UnsupportedSmartBinSchema, match="version 1"):
        storage.initialize_schema()
    with connect(db_path) as conn:
        assert tables(conn) == before
        assert conn.execute("SELECT version FROM smart_bin_schema_versions").fetchone()[0] == 1


def test_test_connection_helper_closes_on_success_and_failure(db_path):
    with connect(db_path) as conn:
        conn.execute("CREATE TABLE scratch (id INTEGER)")
        conn.execute("INSERT INTO scratch VALUES(1)")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="injected"):
        with connect(db_path) as failed:
            failed.execute("INSERT INTO scratch VALUES(2)")
            raise RuntimeError("injected")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed.execute("SELECT 1")
    with connect(db_path) as reader:
        assert reader.execute("SELECT count(*) FROM scratch").fetchone()[0] == 1


def test_critical_transaction_full_fk_nesting_and_rollback(prepared):
    with connect(prepared) as conn:
        conn.execute("PRAGMA synchronous = NORMAL")
        with pytest.raises(RuntimeError, match="explicit caller commit"):
            with storage.critical_transaction(conn) as tx:
                assert tx.execute("PRAGMA synchronous").fetchone()[0] == 2
                assert tx.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                tx.execute("INSERT INTO smart_bin_machines(machine_id) VALUES('rolled-back')")
                with pytest.raises(RuntimeError, match="nested"):
                    with storage.critical_transaction(tx):
                        pass
                assert tx.in_transaction
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM smart_bin_machines").fetchone()[0] == 0
        with pytest.raises(storage.UnsupportedSmartBinSchema):
            with storage.critical_transaction(conn) as tx:
                tx.execute("DELETE FROM smart_bin_schema_versions")
                tx.commit()
                # Check catches the missing version on the next entry.
            with storage.critical_transaction(conn):
                pass


def test_history_and_ledger_share_commit_and_rollback(prepared):
    piece_records.initialize_piece_records()
    with pytest.raises(RuntimeError, match="injected"):
        with storage.critical_transaction() as conn:
            conn.execute("INSERT INTO smart_bin_machines(machine_id) VALUES('m')")
            piece_records.recordPieceOnConnection(conn, {"uuid": "piece", "part_id": "A"}, run_id="run")
            raise RuntimeError("injected")
    with connect(prepared) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_machines").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM piece_records").fetchone()[0] == 0
    with storage.critical_transaction() as conn:
        conn.execute("INSERT INTO smart_bin_machines(machine_id) VALUES('m')")
        piece_records.recordPieceOnConnection(conn, {"uuid": "piece", "part_id": "A"}, run_id="run")
        with connect(prepared) as separate:
            assert separate.execute("SELECT count(*) FROM smart_bin_machines").fetchone()[0] == 0
            assert separate.execute("SELECT count(*) FROM piece_records").fetchone()[0] == 0
        conn.commit()
    with connect(prepared) as conn:
        assert conn.execute("SELECT part_id FROM piece_records WHERE uuid='piece'").fetchone()[0] == "A"
        conn.execute("UPDATE piece_records SET part_correct=0, correction_updated_at=12 WHERE uuid='piece'")
    with storage.critical_transaction() as conn:
        piece_records.recordPieceOnConnection(conn, {"uuid": "piece", "part_id": "B"}, run_id="run")
        conn.commit()
    with connect(prepared) as conn:
        assert conn.execute("SELECT part_id,part_correct,correction_updated_at FROM piece_records "
                            "WHERE uuid='piece'").fetchone() == ("B", 0, 12)
    piece_records.recordPiece({"uuid": "wrapper", "part_id": "C"}, run_id="run")
    with connect(prepared) as conn:
        assert conn.execute("SELECT part_id FROM piece_records WHERE uuid='wrapper'").fetchone()[0] == "C"
