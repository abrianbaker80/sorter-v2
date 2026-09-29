"""Native completion uses only explicit receiver evidence on disposable SQLite."""

from dataclasses import replace
import time
import sqlite3

import pytest

from defs.known_object import KnownObject
import piece_records
import smart_bins_native_completion as completion
import smart_bins_native_custody as custody
import smart_bins_service as service
from subsystems.classification_channel.smart_bins_native_adapter import NativeCustodyAdapter
import test_smart_bins_reservations as reservation_fixtures
from test_smart_bins_reservations import (
    claim, connect, qualification, request,
)

prepared = reservation_fixtures.prepared


@pytest.fixture
def released(prepared):
    piece_records.initialize_piece_records()
    custody.initialize_native_schema("m", "p", "sorter", 0)
    completion.initialize_schema()
    reservation = claim(request(custody=False), qualification(), "native-rb04")
    identity = custody.NativeIdentity("m", "one", reservation["reservation_id"],
                                      "incarnation", 7, 0, "attempt")
    digest = service.configuration_digest()
    custody.bind_reservation(identity, digest)
    custody.arm(identity, digest, {"receiving_evidence": False})
    custody.consume(identity, digest)
    custody.observe(identity, "MOTOR_ACCEPTED",
                    {"proves_piece_displacement": False})
    piece = KnownObject(uuid="one", native_machine_id="m",
                        native_reservation_id=reservation["reservation_id"])
    return prepared, identity, piece


def evidence(identity, **overrides):
    value = completion.ReceivingEvidence(
        evidence_id="receiver-observation-1", machine_id="m",
        reservation_id=identity.reservation_id, piece_uuid="one",
        attempt_id=identity.attempt_id, incarnation=identity.incarnation,
        head_generation=identity.head_generation,
        route_revision=identity.route_revision, actual_kind="BIN",
        actual_slot_id="s0", actual_cycle_id="c0",
        evidence_type="PHYSICALLY_QUALIFIED_RECEIVER", policy_version=1,
        evidence_ref="independent-receiver-1", observed_at=time.time(),
        source_identity="qualified-test-source", qualifier_identity="test-policy",
    )
    return replace(value, **overrides)


def test_no_source_observation_grants_completion(released):
    path, identity, piece = released
    custody.observe(identity, "INFERRED_EXIT", {"receiving_evidence": False})
    assert completion.complete_native(piece, "m", identity.reservation_id, "run") is None
    with connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM piece_records").fetchone()[0] == 0
        assert conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[0] == "RELEASE_INTENT"


def test_explicit_extension_required_before_native_adapter(prepared):
    custody.initialize_native_schema("m", "p", "sorter", 0)
    with connect(prepared) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='smart_bin_native_completion_versions'"
        ).fetchone() is None
    with pytest.raises(custody.CustodyRefused, match="explicit RB04"):
        NativeCustodyAdapter("m")
    completion.initialize_schema()
    assert NativeCustodyAdapter("m").active


def test_qualified_receiver_commits_history_receipt_and_actual_credit(released):
    path, identity, piece = released
    observation = evidence(identity)
    assert completion.record_receiving_evidence(observation) == observation.evidence_id
    delivery_id = completion.complete_native(piece, "m", identity.reservation_id, "run")
    assert delivery_id == completion.verify_native_identity(
        "m", identity.reservation_id, piece.uuid)
    assert delivery_id == completion.complete_native(piece, "m", identity.reservation_id, "run")
    with connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM piece_records").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[0] == "COMPLETED"
        assert tuple(conn.execute("SELECT bin_x,bin_y,bin_z FROM piece_records").fetchone()) == (0, 0, 0)
    assert completion.record_receiving_evidence(observation) == observation.evidence_id
    with pytest.raises(completion.NativeCompletionRefused, match="conflict"):
        completion.record_receiving_evidence(replace(observation, evidence_ref="other"))


def test_wrong_generation_and_misroute(released):
    path, identity, piece = released
    with pytest.raises(completion.NativeCompletionRefused):
        completion.record_receiving_evidence(evidence(identity, head_generation=8))
    observation = evidence(identity, actual_slot_id="s1", actual_cycle_id="c1")
    completion.record_receiving_evidence(observation)
    delivery_id = completion.complete_native(piece, "m", identity.reservation_id, "run")
    assert delivery_id
    with connect(path) as conn:
        delivery = conn.execute("SELECT intended_slot_id,actual_slot_id FROM smart_bin_deliveries").fetchone()
        assert tuple(delivery) == ("s0", "s1")
        assert conn.execute("SELECT count(*) FROM smart_bin_discrepancies").fetchone()[0] == 1
        assert tuple(conn.execute("SELECT bin_x,bin_y,bin_z FROM piece_records").fetchone()) == (0, 0, 1)
    assert completion.current_blocker("m")


def test_history_failure_rolls_back_all_completion_facts(released, monkeypatch):
    path, identity, piece = released
    completion.record_receiving_evidence(evidence(identity))
    def fail(*args, **kwargs):
        raise RuntimeError("injected history failure")
    monkeypatch.setattr(completion, "recordPieceOnConnection", fail)
    with pytest.raises(RuntimeError, match="injected"):
        completion.complete_native(piece, "m", identity.reservation_id, "run")
    with connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM smart_bin_native_completion_receipts").fetchone()[0] == 0
        assert conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[0] == "RELEASE_INTENT"


@pytest.mark.parametrize("change", [
    {"piece_uuid": "other"},
    {"reservation_id": "other"},
    {"attempt_id": "other"},
    {"incarnation": "other"},
    {"head_generation": 8},
    {"route_revision": 1},
    {"policy_version": 2},
    {"evidence_type": "INFERRED_EXIT"},
    {"observed_at": 1.0},
])
def test_wrong_or_stale_receiving_evidence_never_completes(released, change):
    path, identity, piece = released
    with pytest.raises(completion.NativeCompletionRefused):
        completion.record_receiving_evidence(evidence(identity, **change))
    assert completion.complete_native(piece, "m", identity.reservation_id, "run") is None
    with connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 0


def test_second_receiver_for_same_reservation_conflicts(released):
    _, identity, _ = released
    first = evidence(identity)
    completion.record_receiving_evidence(first)
    with pytest.raises(completion.NativeCompletionRefused, match="conflicting"):
        completion.record_receiving_evidence(replace(
            first, evidence_id="another-evidence", actual_slot_id="s1",
            actual_cycle_id="c1",
        ))


def test_uncertain_custody_refuses_recorded_receiver(released):
    path, identity, piece = released
    completion.record_receiving_evidence(evidence(identity))
    custody.observe(identity, "EJECT_TIMEOUT_VISIBLE", {}, uncertain=True)
    with pytest.raises(completion.NativeCompletionRefused):
        completion.complete_native(piece, "m", identity.reservation_id, "run")
    with connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 0


def test_receipt_insert_failure_rolls_back_delivery_history_and_effects(released):
    path, identity, piece = released
    completion.record_receiving_evidence(evidence(identity))
    with connect(path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_native_receipt BEFORE INSERT "
            "ON smart_bin_native_completion_receipts BEGIN "
            "SELECT RAISE(ABORT,'injected receipt failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        completion.complete_native(piece, "m", identity.reservation_id, "run")
    with connect(path) as conn:
        for table in ("smart_bin_deliveries", "piece_records",
                      "smart_bin_native_completion_receipts",
                      "smart_bin_native_completion_effects"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_native_receipt_cannot_hide_missing_history(released):
    path, identity, piece = released
    completion.record_receiving_evidence(evidence(identity))
    delivery_id = completion.complete_native(piece, "m", identity.reservation_id, "run")
    with connect(path) as conn:
        conn.execute("DELETE FROM piece_records WHERE uuid=?", (piece.uuid,))
        assert conn.execute(
            "SELECT kind FROM smart_bin_current_obligations "
            "WHERE reservation_id=? AND kind='NATIVE_MISSING_HISTORY'",
            (identity.reservation_id,),
        ).fetchone()
    assert completion.current_blocker("m")
    with pytest.raises(completion.NativeCompletionRefused, match="inconsistent"):
        completion.verify_native_identity("m", identity.reservation_id,
                                          piece.uuid, delivery_id)
