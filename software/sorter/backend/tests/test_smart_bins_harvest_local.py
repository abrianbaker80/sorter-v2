"""Local journal only; detached synthetic readbacks, no Harvest application."""

import copy
from dataclasses import asdict, replace
import json
import sys

import pytest

from test_smart_bins_delivery import prepared as prepared, connect, claim, prepare, confirm, complete
from harvest_integration_storage import canonical, digest
import smart_bins_harvest_integration as journal
from smart_bins_storage import critical_transaction


@pytest.fixture
def local_journal(prepared):
    journal.initialize_schema()
    yield prepared
    assert not any(n.startswith("project_harvest_") for n in sys.modules)
    with connect(prepared) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'harvest_%'").fetchall()


def begin():
    reserved = claim()
    request = journal.OperationRequest(
        "op-allocation", "allocate", "m", reserved["reservation_id"], "piece", "synthetic-project",
        "ALLOCATE", 0, reserved["state_revision"], 7,
        {"method": "propose_live_allocation", "piece_id": "piece", "part_id": "3001", "color_id": "2"},
        activation_id="synthetic-activation")
    receipt = journal.begin_allocation_operation(request)
    assert receipt["code"] == "OK"
    return request, reserved, receipt


def synthetic_readback(request):
    op = journal.lookup_operation(request.operation_id)
    allocation = dict(allocation_id="synthetic-allocation", project_id=request.project_id,
                      piece_id=request.piece_uuid, runtime_id=request.activation_id,
                      quantity=1, status="planned", group_id="A")
    receipt = dict(operation_id=request.operation_id, request_key=op["request_key"],
                   payload_hash=op["payload_hash"], request_json=canonical(asdict(request)),
                   allocation_id=allocation["allocation_id"], allocation_json=canonical(allocation),
                   destination_json=canonical(dict(group_id="A", layer_index=0, section_index=0, bin_index=0)))
    return dict(project_id=request.project_id, piece_id=request.piece_uuid,
                allocation=allocation, receipts=[receipt])


def observe(request, *, source="READBACK", evidence=None, key="readback"):
    op = journal.lookup_operation(request.operation_id)
    return journal.record_remote_allocation_result(
        request.operation_id, source=source,
        evidence=evidence if evidence is not None else synthetic_readback(request),
        request_key=key, expected_operation_revision=op["row_revision"])


def advance(request, observation, key="advance"):
    return journal.advance_remote_result(
        request.operation_id, observation_id=observation["observation_id"], request_key=key,
        expected_operation_revision=journal.lookup_operation(request.operation_id)["row_revision"])


def linked():
    request, reserved, _ = begin()
    assert advance(request, observe(request))["code"] == "OK"
    result = journal.link_local_reservation(
        request.operation_id, request_key="link",
        expected_operation_revision=journal.lookup_operation(request.operation_id)["row_revision"],
        expected_reservation_revision=0)
    assert result["code"] == "OK"
    return request, reserved


def test_canonical_request_hash_is_deterministic_and_strict():
    assert canonical({"b": 2, "a": [1, "é"]}) == canonical({"a": [1, "é"], "b": 2})
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})
    with pytest.raises(ValueError):
        canonical({"a": float("nan")})


def test_intent_durable_exact_replay_conflict_and_no_distributed_transaction(local_journal):
    request, _, receipt = begin()
    assert journal.begin_allocation_operation(request) == receipt
    assert journal.begin_allocation_operation(replace(request, expected_project_revision=8))["code"] == "IDEMPOTENCY_CONFLICT"
    with connect(local_journal) as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert conn.execute("SELECT count(*) FROM smart_bin_external_operations").fetchone()[0] == 1
        assert conn.execute("SELECT request_json FROM smart_bin_harvest_operations").fetchone()[0] == canonical(asdict(request))
    assert journal.inspect_integration_recovery("m")["harvest_blockers"]


@pytest.mark.parametrize("source", ["ACK", "AMBIGUOUS"])
def test_acknowledgement_or_timeout_never_proves_remote_commit(local_journal, source):
    request, _, _ = begin()
    observed = observe(request, source=source)
    assert advance(request, observed)["code"] == "READBACK_REQUIRED"
    assert journal.lookup_operation(request.operation_id)["status"] == "LOCAL_INTENT"
    assert journal.inspect_integration_recovery("m")["harvest_blockers"]


def test_exact_observation_is_immutable_and_replay_adds_nothing(local_journal):
    request, _, _ = begin()
    evidence = synthetic_readback(request)
    revision = journal.lookup_operation(request.operation_id)["row_revision"]
    first = observe(request, evidence=evidence)
    replay = journal.record_remote_allocation_result(
        request.operation_id, source="READBACK", evidence=evidence, request_key="readback",
        expected_operation_revision=revision)
    assert replay == first
    with connect(local_journal) as conn:
        rows = conn.execute("SELECT evidence_json FROM smart_bin_harvest_observations").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0]) == evidence
    assert advance(request, first)["code"] == "OK"
    assert journal.lookup_operation(request.operation_id)["status"] == "REMOTE_CONFIRMED"


def test_newer_contradictory_readback_beats_old_ack(local_journal):
    request, _, _ = begin()
    old = observe(request)
    wrong = synthetic_readback(request)
    wrong["piece_id"] = "contradictory-piece"
    new = observe(request, evidence=wrong, key="newer")
    assert advance(request, old, "old-advance")["code"] == "LATEST_READBACK_REQUIRED"
    assert advance(request, new, "new-advance")["code"] == "REMOTE_IDENTITY_MISMATCH"
    assert journal.lookup_operation(request.operation_id)["reconciliation_required"] == 1


@pytest.mark.parametrize("field", ["payload_hash", "request_key", "allocation_id"])
def test_readback_requires_exact_receipt_identity(local_journal, field):
    request, _, _ = begin()
    wrong = copy.deepcopy(synthetic_readback(request))
    wrong["receipts"][0][field] = "wrong"
    result = advance(request, observe(request, evidence=wrong))
    assert result["code"] == "REMOTE_PAYLOAD_MISMATCH"
    assert journal.lookup_operation(request.operation_id)["status"] == "LOCAL_INTENT"


def test_local_journal_failure_rolls_back_all_effects(local_journal, monkeypatch):
    request, _, _ = begin()
    original = journal._receipt
    def fail(*args, **kwargs):
        raise OSError("receipt failure")
    monkeypatch.setattr(journal, "_receipt", fail)
    with pytest.raises(OSError, match="receipt failure"):
        observe(request)
    with connect(local_journal) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_harvest_observations").fetchone()[0] == 0
    monkeypatch.setattr(journal, "_receipt", original)
    assert observe(request)["code"] == "OK"


def test_completion_journal_staging_uses_caller_transaction(local_journal, monkeypatch):
    request, reserved = linked()
    intent = prepare(reserved)
    assert intent["code"] == "OK"
    assert confirm(reserved, intent)["code"] == "OK"
    delivered = complete(reserved, intent)
    assert delivered["code"] == "OK"
    with connect(local_journal) as conn:
        revision = conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id='m'").fetchone()[0]
    confirmation = replace(request, operation_id="op-confirm", request_key="confirm",
                           operation_kind="CONFIRM", allocation_id="synthetic-allocation",
                           delivery_id=delivered["delivery_id"], parent_operation_id=request.operation_id,
                           expected_reservation_revision=3, expected_machine_revision=revision,
                           request_payload={"method": "confirm_allocation", "piece_id": "piece"})
    with connect(local_journal) as conn:
        with pytest.raises(RuntimeError, match="caller-owned FULL/FK"):
            journal.stage_delivery_confirmation_on_connection(conn, confirmation)
    with pytest.raises(RuntimeError, match="outer rollback"):
        with journal.integration_lock, critical_transaction() as conn:
            journal.stage_delivery_confirmation_on_connection(conn, confirmation)
            assert conn.in_transaction
            raise RuntimeError("outer rollback")
    assert journal.lookup_operation("op-confirm") is None
    with journal.integration_lock, critical_transaction() as conn:
        def forbidden(*args, **kwargs):
            raise AssertionError("staging opened another connection")
        with monkeypatch.context() as patch:
            import sqlite3
            patch.setattr(sqlite3, "connect", forbidden)
            result = journal.stage_delivery_confirmation_on_connection(conn, confirmation)
            assert result["code"] == "OK" and conn.in_transaction
        conn.commit()
    assert journal.lookup_operation("op-confirm")["status"] == "DELIVERY_CONFIRM_PENDING"
    with connect(local_journal) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM piece_records").fetchone()[0] == 1
