"""Faults leave exact durable truth and never imply another physical release."""

import sqlite3

import pytest
import smart_bins_native_completion as completion
import test_smart_bins_native_completion as completion_fixtures
from test_smart_bins_native_completion import evidence
from test_smart_bins_reservations import connect
from test_smart_bins_sending import setup_sending

released = completion_fixtures.released
prepared = completion_fixtures.prepared


@pytest.mark.parametrize("table,operation,condition", [
    ("smart_bin_deliveries", "INSERT", "1"),
    ("piece_records", "INSERT", "1"),
    ("smart_bin_reservations", "UPDATE", "NEW.state='COMPLETED'"),
    ("smart_bin_native_completion_effects", "INSERT", "1"),
    ("smart_bin_native_completion_receipts", "INSERT", "1"),
])
def test_atomic_failure_rolls_back_every_completion_fact(
    released, table, operation, condition,
):
    path, identity, piece = released
    completion.record_receiving_evidence(evidence(identity))
    with connect(path) as conn:
        conn.execute(
            f"CREATE TRIGGER fail_stage BEFORE {operation} ON {table} "
            f"WHEN {condition} BEGIN SELECT RAISE(ABORT,'injected stage'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected stage"):
        completion.complete_native(piece, "m", identity.reservation_id, "run")
    with connect(path) as conn:
        for name in ("smart_bin_deliveries", "piece_records",
                     "smart_bin_native_completion_effects",
                     "smart_bin_native_completion_receipts"):
            assert conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] == 0
        assert conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[0] == "RELEASE_INTENT"


def test_receiving_insert_failure_retains_no_qualified_evidence(released):
    path, identity, _ = released
    with connect(path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_receiver BEFORE INSERT ON "
            "smart_bin_native_receiving_evidence BEGIN "
            "SELECT RAISE(ABORT,'receiver write failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="receiver write failed"):
        completion.record_receiving_evidence(evidence(identity))
    with connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_native_receiving_evidence").fetchone()[0] == 0


def test_commit_readback_ambiguity_recovers_same_receipt(released, monkeypatch):
    path, identity, piece = released
    completion.record_receiving_evidence(evidence(identity))
    original = completion.verify_native_identity
    def lost_ack(*args, **kwargs):
        raise OSError("readback unavailable after commit")
    monkeypatch.setattr(completion, "verify_native_identity", lost_ack)
    with pytest.raises(OSError, match="readback unavailable"):
        completion.complete_native(piece, "m", identity.reservation_id, "run")
    monkeypatch.setattr(completion, "verify_native_identity", original)
    delivered = completion.complete_native(piece, "m", identity.reservation_id, "run")
    assert delivered == original("m", identity.reservation_id, piece.uuid)
    with connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM piece_records").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM smart_bin_native_completion_receipts").fetchone()[0] == 1


def _effect(path, reservation_id, effect):
    with connect(path) as conn:
        return conn.execute(
            "SELECT phase,failure_phase FROM smart_bin_native_completion_effects "
            "WHERE reservation_id=? AND effect=?", (reservation_id, effect)
        ).fetchone()


def test_callback_failure_is_attributed_and_not_replayed(released, monkeypatch):
    path, identity, piece = released
    completion.record_receiving_evidence(evidence(identity))
    sending, _, shared, events = setup_sending(piece)
    attempts = []
    def fail_event(_event):
        attempts.append(1)
        raise OSError("injected callback")
    monkeypatch.setattr(events, "put", fail_event)
    assert sending.step() is None
    assert tuple(_effect(path, identity.reservation_id, "EVENT_ENQUEUED")) == (
        "ATTEMPTED", "PUBLICATION_CALLBACKS")
    sending._native_next_check_at = 0
    assert sending.step() is None
    assert attempts == [1]
    assert not shared.get_distribution_ready()


def test_publication_attempt_persistence_failure_runs_no_callback(released):
    path, identity, piece = released
    completion.record_receiving_evidence(evidence(identity))
    with connect(path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_attempt BEFORE UPDATE OF phase "
            "ON smart_bin_native_completion_effects "
            "WHEN NEW.effect='EVENT_ENQUEUED' AND NEW.phase='ATTEMPTED' "
            "BEGIN SELECT RAISE(ABORT,'attempt write failed'); END"
        )
    sending, _, shared, events = setup_sending(piece)
    assert sending.step() is None
    assert tuple(_effect(path, identity.reservation_id, "EVENT_ENQUEUED")) == (
        "PENDING", None)
    assert events.qsize() == 0
    assert not shared.get_distribution_ready()
    with connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM piece_records").fetchone()[0] == 1


@pytest.mark.parametrize("effect", ["EVENT_ENQUEUED", "CLOSE"])
def test_success_or_close_persistence_failure_does_not_open_gate(
    released, effect,
):
    path, identity, piece = released
    completion.record_receiving_evidence(evidence(identity))
    with connect(path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_effect_success BEFORE UPDATE OF phase "
            "ON smart_bin_native_completion_effects "
            f"WHEN NEW.effect='{effect}' AND NEW.phase='SUCCEEDED' "
            "BEGIN SELECT RAISE(ABORT,'phase persistence failed'); END"
        )
    sending, gc, shared, events = setup_sending(piece)
    gc.set_progress_tracker = None
    assert sending.step() is None
    expected = "ATTEMPTED" if effect == "EVENT_ENQUEUED" else "PENDING"
    assert _effect(path, identity.reservation_id, effect)[0] == expected
    assert not shared.get_distribution_ready()
    assert events.qsize() == 1


@pytest.mark.parametrize("effect,phase", [
    ("GATE_OPEN", "GATE_OPEN"),
    ("ADMISSION_RELEASE", "ADMISSION_RELEASE"),
])
def test_gate_and_admission_failures_are_separate(released, monkeypatch, effect, phase):
    path, identity, piece = released
    completion.record_receiving_evidence(evidence(identity))
    sending, gc, shared, _ = setup_sending(piece)
    gc.set_progress_tracker = None
    if effect == "GATE_OPEN":
        def fail_gate(value, **_kwargs):
            if value:
                raise OSError("injected gate failure")
        monkeypatch.setattr(shared, "set_distribution_gate", fail_gate)
    else:
        def fail_admission(*_args):
            raise OSError("injected admission failure")
        monkeypatch.setattr(gc.smart_bins_native_adapter, "release_completed",
                            fail_admission)
    assert sending.step() is None
    assert tuple(_effect(path, identity.reservation_id, effect)) == ("ATTEMPTED", phase)
    assert completion.current_blocker("m")
    assert _effect(path, identity.reservation_id, "CLOSE")[0] == "SUCCEEDED"
