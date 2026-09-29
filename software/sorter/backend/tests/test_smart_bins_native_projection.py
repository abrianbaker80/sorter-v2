"""Current authorization tracks later contradictions without scanning old rows."""

import json

import smart_bins_native_completion as native_completion
import smart_bins_native_custody as native_custody
import smart_bins_completion_recovery as recovery
import test_smart_bins_completion_recovery as recovery_fixtures
from test_smart_bins_completion_recovery import (
    connect, retained, complete_retained, close, late_hold,
)

prepared = recovery_fixtures.prepared


def _closed(path):
    ctx = retained(path)
    complete_retained(ctx)
    close(ctx)
    native_custody.initialize_native_schema("m", "p", "sorter", 0)
    native_completion.initialize_schema()
    return ctx


def test_closed_history_starts_clear_then_late_hold_blocks(prepared):
    ctx = _closed(prepared)
    with connect(prepared) as conn:
        assert recovery.inspect_on_connection(conn, "m")["followup_blockers"] == []
    assert not native_completion.current_blocker("m")
    late_hold(ctx, ctx.handoff)
    assert native_completion.current_blocker("m")
    with connect(prepared) as conn:
        assert conn.execute("SELECT kind FROM smart_bin_current_obligations "
                            "WHERE reservation_id=? AND kind='FOLLOWUP'",
                            (ctx.handoff.reservation_id,)).fetchone()


def test_history_deleted_after_close_blocks_current_authorization(prepared):
    _closed(prepared)
    assert not native_completion.current_blocker("m")
    with connect(prepared) as conn:
        conn.execute("DELETE FROM piece_records")
    assert native_completion.current_blocker("m")
    with connect(prepared) as conn:
        assert recovery.inspect_on_connection(conn, "m")["followup_blockers"]


def test_other_machine_obligation_is_isolated(prepared):
    _closed(prepared)
    with connect(prepared) as conn:
        conn.execute("INSERT INTO smart_bin_machines(machine_id) VALUES('other')")
        conn.execute("INSERT INTO smart_bin_native_holds "
                     "VALUES('other-hold','other','UNKNOWN_MATERIAL','{}',1,NULL)")
    assert not native_completion.current_blocker("m")
    assert native_completion.current_blocker("other")


def _copy_row(conn, table, row, **changes):
    values = dict(row)
    values.update(changes)
    if table == "piece_records":
        values.pop("id")
    columns = list(values)
    conn.execute(
        f"INSERT INTO {table} ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        tuple(values.values()),
    )


def _replace_piece(raw, piece):
    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return piece if value == "piece" else value
    return json.dumps(replace(json.loads(raw)), sort_keys=True)


def _synthetic_closed_history(conn, start, count):
    """Clone one accepted closure with unique durable identities and references."""
    source = {table: conn.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()
              for table in (
                  "smart_bin_reservations", "smart_bin_release_attempts",
                  "smart_bin_release_evidence", "piece_records",
                  "smart_bin_deliveries", "smart_bin_completion_followups",
              )}
    for index in range(start, start + count):
        reservation_id = f"synthetic-reservation-{index}"
        attempt_id = f"synthetic-attempt-{index}"
        delivery_id = f"synthetic-delivery-{index}"
        followup_id = f"synthetic-followup-{index}"
        piece = f"synthetic-piece-{index}"
        key = f"synthetic-key-{index}"
        _copy_row(conn, "smart_bin_reservations", source["smart_bin_reservations"],
                  id=reservation_id, piece_uuid=piece, request_key=key)
        _copy_row(conn, "smart_bin_release_attempts", source["smart_bin_release_attempts"],
                  id=attempt_id, reservation_id=reservation_id,
                  target_boundary=str(1000 + index))
        release = dict(source["smart_bin_release_evidence"])
        for name in ("intent_json", "exit_json", "completion_json"):
            release[name] = _replace_piece(release[name], piece)
        _copy_row(conn, "smart_bin_release_evidence", release,
                  attempt_id=attempt_id, reservation_id=reservation_id)
        _copy_row(conn, "piece_records", source["piece_records"], uuid=piece)
        _copy_row(conn, "smart_bin_deliveries", source["smart_bin_deliveries"],
                  id=delivery_id, reservation_id=reservation_id, piece_uuid=piece)
        followup = dict(source["smart_bin_completion_followups"])
        for name in ("custody_json", "completion_json"):
            followup[name] = _replace_piece(followup[name], piece)
        _copy_row(conn, "smart_bin_completion_followups", followup,
                  id=followup_id, reservation_id=reservation_id,
                  release_attempt_id=attempt_id, piece_uuid=piece,
                  delivery_id=delivery_id, completion_request_key=key)
        result = {"code": "OK", "state": "CLOSED", "followup_id": followup_id}
        conn.execute(
            "INSERT INTO smart_bin_completion_receipts VALUES(?,?,?,?,?,1)",
            (key + ":close", "m", "CLOSE", "synthetic-digest",
             json.dumps(result, sort_keys=True)),
        )


def test_current_projection_matches_inspector_and_stays_bounded(prepared):
    _closed(prepared)
    counts = {}
    with connect(prepared) as conn:
        for size, additional in ((1, 0), (51, 50), (201, 150)):
            _synthetic_closed_history(conn, size - additional - 1, additional)
            assert conn.execute(
                "SELECT count(*) FROM smart_bin_completion_followups"
            ).fetchone()[0] == size
            report = recovery.inspect_on_connection(conn, "m")
            assert report["followup_blockers"] == []
            queries = []
            conn.set_trace_callback(queries.append)
            assert not native_completion.current_blocker_on_connection(conn, "m")
            conn.set_trace_callback(None)
            counts[size] = len(queries)
            assert conn.execute(
                "SELECT count(*) FROM smart_bin_current_obligations"
            ).fetchone()[0] == 0
        assert len(set(counts.values())) == 1, counts
        plan = [str(tuple(row)) for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM smart_bin_current_obligations "
            "WHERE machine_id='m' LIMIT 1"
        )]
        assert any("smart_bin_current_obligations_machine" in row for row in plan)
        print(f"RB04 current authorization SQL statements: {counts}; plan: {plan}")


def test_projection_rollback_and_close_receipt_loss(prepared):
    ctx = _closed(prepared)
    with connect(prepared) as conn:
        assert not native_completion.current_blocker_on_connection(conn, "m")
        conn.execute("SAVEPOINT contradiction")
        conn.execute(
            "INSERT INTO smart_bin_discrepancies "
            "(id,machine_id,reservation_id,kind,status,details_json,created_at) "
            "VALUES('rollback','m',?,'LATE','open','{}',1)",
            (ctx.handoff.reservation_id,),
        )
        assert native_completion.current_blocker_on_connection(conn, "m")
        conn.execute("ROLLBACK TO contradiction")
        conn.execute("RELEASE contradiction")
        assert not native_completion.current_blocker_on_connection(conn, "m")
        conn.execute("DELETE FROM smart_bin_completion_receipts WHERE action='CLOSE'")
        assert native_completion.current_blocker_on_connection(conn, "m")
        assert conn.execute(
            "SELECT kind FROM smart_bin_current_obligations "
            "WHERE kind='CLOSURE_PENDING'"
        ).fetchone()


def test_native_history_loss_and_late_reconciliation_block(prepared):
    _closed(prepared)
    with connect(prepared) as conn:
        reservation_id = conn.execute(
            "SELECT id FROM smart_bin_reservations LIMIT 1"
        ).fetchone()[0]
        audit = conn.execute("SELECT * FROM smart_bin_audit_events LIMIT 1").fetchone()
        _copy_row(conn, "smart_bin_audit_events", audit,
                  id="later-audit", request_key="later-reconciliation",
                  reservation_id=reservation_id)
        conn.execute(
            "INSERT INTO smart_bin_reconciliations "
            "(id,machine_id,reservation_id,audit_id,evidence_json) "
            "VALUES('later','m',?,'later-audit','{}')",
            (reservation_id,),
        )
        assert native_completion.current_blocker_on_connection(conn, "m")
        assert recovery.inspect_on_connection(conn, "m")["followup_blockers"]
