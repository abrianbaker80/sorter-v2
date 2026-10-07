"""Two real temporary stores, controlled clocks, no runtime/provider/hardware calls."""
# ruff: noqa: E402 -- storage/configuration roots must be isolated before imports.

from __future__ import annotations

import atexit
import copy
import os
import socket
import sqlite3
import tempfile
from dataclasses import replace
from types import SimpleNamespace

_isolation = tempfile.TemporaryDirectory(prefix="harvest-integration-import-")
atexit.register(_isolation.cleanup)
for _key, _value in {
    "LOCAL_STATE_DB_PATH": "state.sqlite",
    "MACHINE_SPECIFIC_PARAMS_PATH": "machine.toml",
    "PROJECT_HARVEST_DIR": "harvest",
}.items():
    os.environ[_key] = os.path.join(_isolation.name, _value)

import pytest
from test_smart_bins_delivery import (
    connect,
    claim,
    prepare,
    confirm,
    complete,
)
from test_project_harvest_projects import _store_with_draft, _bom, _resolved_policy
import local_state
import piece_records
import project_harvest_projects as harvest
import smart_bins_delivery as delivery
import smart_bins_service as service
import smart_bins_harvest_integration as journal
import test_smart_bins_delivery as native_fixture
from harvest_integration_storage import canonical
from smart_bins_storage import critical_transaction

prepared = native_fixture.prepared


def fail_call(*args, **kwargs):
    raise AssertionError("unexpected external or runtime call")


@pytest.fixture
def stores(prepared, tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(socket.socket, "connect", fail_call)
    for module in (journal, delivery, service, native_fixture):
        monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(harvest, "_now", lambda: "2027-01-15T08:00:00+00:00")
    root = tmp_path / "harvest"
    monkeypatch.setenv("PROJECT_HARVEST_DIR", str(root))
    store, draft = _store_with_draft(root, repeated_part=True)
    project = store.create_project(draft_id=draft["draft_id"])
    project = store.save_bom(
        project["project_id"],
        content=_bom({"part_id": "3001", "color_id": "2", "quantity": 3}),
        filename="bom.json",
        provider="private_moc",
    )
    project = store.update_review(
        project["project_id"], policy=_resolved_policy(project)
    )
    store.initialize_integration_schema()
    journal.initialize_schema()
    piece_records.initialize_piece_records()
    # Synthetic activation fixture only. Activation orchestration is outside this slice.
    assignments = [
        dict(
            group_id=g["id"],
            bin_id="bin-" + str(i),
            layer_index=0,
            section_index=0,
            bin_index=i,
            category_id="harvest:" + g["id"],
        )
        for i, g in enumerate(project["effective_groups"])
    ]
    assignments.append(
        dict(
            group_id=harvest.HARVEST_EXCEPTION_GROUP_ID,
            bin_id="exception",
            layer_index=1,
            section_index=0,
            bin_index=0,
            category_id="exception",
        )
    )
    activation = dict(
        activation_id="activation-test",
        project_id=project["project_id"],
        runtime_mode="live",
        status="active",
        bom_revision_id=project["bom"]["bom_revision_id"],
        assignments=assignments,
    )
    with store._connection() as conn:
        conn.execute(
            "UPDATE harvest_projects SET state='active',activation_json=? WHERE project_id=?",
            (canonical(activation), project["project_id"]),
        )
        conn.execute(
            "INSERT INTO harvest_runtime_state VALUES(1,?,?,?,?)",
            (
                project["project_id"],
                "activation-test",
                canonical(activation),
                "synthetic",
            ),
        )
        conn.commit()
    return SimpleNamespace(
        path=prepared, store=store, project=project, clock=clock, root=root
    )


def begin(stores, *, key="allocate", op="op-allocation"):
    reserved = claim()
    request = journal.OperationRequest(
        op,
        key,
        "m",
        reserved["reservation_id"],
        "piece",
        stores.project["project_id"],
        "ALLOCATE",
        0,
        reserved["state_revision"],
        stores.project["revision"],
        dict(
            method="propose_live_allocation",
            piece_id="piece",
            part_id="3001",
            color_id="2",
            item_candidates=None,
            color_candidates=None,
            classification_attempts=None,
        ),
        activation_id="activation-test",
    )
    receipt = journal.begin_allocation_operation(request)
    assert receipt["code"] == "OK", receipt
    return request, reserved, receipt


def remote_allocate(stores, request):
    payload = dict(request.request_payload)
    payload.pop("method")
    # A second local writer can begin while Harvest runs: no retained local transaction.
    with connect(stores.path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert conn.execute(
            "SELECT id FROM smart_bin_external_operations WHERE id=?",
            (request.operation_id,),
        ).fetchone()
    return stores.store.propose_live_allocation(
        **payload,
        integration_request=journal.lookup_operation(request.operation_id)["request"],
    )


def observe(stores, operation_id, *, source="READBACK", evidence=None, key="readback"):
    key = operation_id + ":" + key
    op = journal.lookup_operation(operation_id)
    if evidence is None:
        evidence = stores.store.lookup_integration_allocation(
            op["project_id"], piece_id=op["piece_uuid"]
        )
    fn = {
        "ALLOCATE": journal.record_remote_allocation_result,
        "CANCEL": journal.record_remote_cancel_result,
        "CONFIRM": journal.record_delivery_confirmation_result,
    }[op["operation_kind"]]
    receipt = fn(
        operation_id,
        source=source,
        evidence=evidence,
        request_key=key,
        expected_operation_revision=op["row_revision"],
    )
    assert receipt["code"] == "OK", receipt
    result = journal.advance_remote_result(
        operation_id,
        observation_id=receipt["observation_id"],
        request_key=key,
        expected_operation_revision=receipt["operation_revision"],
    )
    return result


def linked(stores):
    request, reserved, _ = begin(stores)
    allocation = remote_allocate(stores, request)
    adopted = observe(stores, request.operation_id)
    assert adopted["code"] == "OK", adopted
    receipt = journal.link_local_reservation(
        request.operation_id,
        request_key="link",
        expected_operation_revision=adopted["operation_revision"],
        expected_reservation_revision=0,
    )
    assert receipt["code"] == "OK", receipt
    return request, reserved, allocation


def confirmation(stores, *, stage=True):
    request, reserved, allocation = linked(stores)
    intent = prepare(reserved)
    assert intent["code"] == "OK", intent
    exited = confirm(reserved, intent)
    assert exited["code"] == "OK", exited
    delivered = complete(reserved, intent)
    assert delivered["code"] == "OK", delivered
    with connect(stores.path) as conn:
        native = dict(conn.execute("SELECT * FROM smart_bin_deliveries").fetchone())
        revision = conn.execute(
            "SELECT state_revision FROM smart_bin_machines WHERE machine_id='m'"
        ).fetchone()[0]
    confirm_request = replace(
        request,
        operation_id="op-confirm",
        request_key="confirm",
        operation_kind="CONFIRM",
        allocation_id=allocation["allocation_id"],
        delivery_id=native["id"],
        parent_operation_id=request.operation_id,
        expected_reservation_revision=3,
        expected_machine_revision=revision,
        request_payload=dict(method="confirm_allocation", piece_id="piece"),
    )
    if stage:
        with journal.integration_lock, critical_transaction() as conn:
            staged = journal.stage_delivery_confirmation_on_connection(
                conn, confirm_request
            )
            conn.commit()
        assert staged["code"] == "OK"
    return confirm_request, native


def native_dump(path):
    with connect(path) as conn:
        tables = (
            "smart_bin_reservations",
            "smart_bin_deliveries",
            "piece_records",
            "piece_events",
            "smart_bin_opening_balances",
        )
        return {
            table: [
                tuple(r)
                for r in conn.execute("SELECT * FROM " + table + " ORDER BY rowid")
            ]
            for table in tables
        }


def cancelled(stores):
    request, reserved, allocation = linked(stores)
    result = service.cancel_reserved(
        "m",
        reserved["reservation_id"],
        expected_reservation_revision=0,
        request_key="native-cancel",
        evidence=service.NonDispatchEvidence(
            "owner", "episode", 0, 1, True, True, "synthetic:never-dispatched"
        ),
    )
    assert result["code"] == "OK"
    req = replace(
        request,
        operation_id="op-cancel",
        request_key="cancel",
        operation_kind="CANCEL",
        allocation_id=allocation["allocation_id"],
        parent_operation_id=request.operation_id,
        expected_reservation_revision=1,
        expected_machine_revision=result["state_revision"],
        request_payload=dict(method="cancel_integration_allocation", piece_id="piece"),
    )
    receipt = journal.begin_allocation_cancel(req)
    assert receipt["code"] == "OK", receipt
    return req


def test_intent_crash_and_timeout_before_commit_stays_unresolved(stores):
    request, _, receipt = begin(stores)
    # Reopen both stores without doing the external mutation.
    local_state.close_local_state_keeper()
    stores.store = harvest.HarvestProjectStore(stores.root)
    assert journal.lookup_operation(request.operation_id)["status"] == "LOCAL_INTENT"
    assert (
        observe(stores, request.operation_id)["code"] == "ABSENCE_IS_NOT_CANCELLATION"
    )
    report = journal.inspect_integration_recovery("m")
    assert report["blocked_harvest_projects"] == [request.project_id]
    assert report["harvest_blockers"][0]["readback_required"]
    assert journal.begin_allocation_operation(request) == receipt


@pytest.mark.parametrize("lost_ack", [False, True])
def test_remote_commit_exact_readback_link_and_restart(stores, lost_ack):
    request, _, _ = begin(stores)
    allocation = remote_allocate(stores, request)
    if lost_ack:
        result = observe(
            stores,
            request.operation_id,
            source="AMBIGUOUS",
            evidence={"error": "timeout after commit"},
            key="timeout",
        )
        assert result["code"] == "READBACK_REQUIRED"
    else:
        assert (
            observe(
                stores,
                request.operation_id,
                source="ACK",
                evidence=allocation,
                key="ack",
            )["code"]
            == "READBACK_REQUIRED"
        )
    stores.store = harvest.HarvestProjectStore(stores.root)
    assert (
        remote_allocate(stores, request)["allocation_id"] == allocation["allocation_id"]
    )
    result = observe(stores, request.operation_id)
    linked_receipt = journal.link_local_reservation(
        request.operation_id,
        request_key="link",
        expected_operation_revision=result["operation_revision"],
        expected_reservation_revision=0,
    )
    assert linked_receipt["code"] == "OK"
    assert (
        journal.link_local_reservation(
            request.operation_id,
            request_key="link",
            expected_operation_revision=result["operation_revision"],
            expected_reservation_revision=0,
        )
        == linked_receipt
    )
    assert journal.inspect_integration_recovery("m")["harvest_blockers"] == []
    with stores.store._connection() as conn:
        assert (
            conn.execute("SELECT count(*) FROM harvest_allocations").fetchone()[0] == 1
        )
    assert delivery.inspect_recovery("m")["physical_recovery_authorized"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", "other"),
        ("piece_id", "other"),
        ("runtime_id", "other"),
        ("allocation_id", "other"),
        ("quantity", 2),
    ],
)
def test_wrong_readback_identity_rejected(stores, field, value):
    request, _, allocation = linked(stores)
    evidence = stores.store.lookup_integration_allocation(
        request.project_id, piece_id="piece"
    )
    evidence["allocation"][field] = value
    before = native_dump(stores.path)
    result = observe(
        stores, request.operation_id, evidence=evidence, key="contradiction"
    )
    assert result["code"] == "REMOTE_IDENTITY_MISMATCH"
    assert journal.lookup_operation(request.operation_id)["status"] == "COMPLETE"
    assert journal.inspect_integration_recovery("m")["harvest_blockers"]
    assert native_dump(stores.path) == before
    assert (
        observe(stores, request.operation_id, key="later-correct")["code"]
        == "RECONCILIATION_REQUIRED"
    )


def test_wrong_payload_and_missing_legacy_receipt(stores):
    request, _, _ = begin(stores)
    remote_allocate(stores, request)
    evidence = stores.store.lookup_integration_allocation(
        request.project_id, piece_id="piece"
    )
    original = copy.deepcopy(evidence)
    evidence["receipts"] = []
    assert (
        observe(stores, request.operation_id, evidence=evidence)["code"]
        == "REMOTE_REQUEST_PROVENANCE_MISSING"
    )
    original["receipts"][0]["payload_hash"] = "wrong"
    assert (
        observe(stores, request.operation_id, evidence=original, key="wrong")["code"]
        == "REMOTE_PAYLOAD_MISMATCH"
    )


def test_idempotency_conflicts_and_stale_checks(stores):
    request, _, first = begin(stores)
    assert journal.begin_allocation_operation(request) == first
    changed = replace(
        request, request_payload={**request.request_payload, "part_id": "other"}
    )
    assert journal.begin_allocation_operation(changed)["code"] == "IDEMPOTENCY_CONFLICT"
    remote_allocate(stores, request)
    with stores.store._connection() as conn:
        conn.execute("UPDATE harvest_projects SET revision=revision+1")
        conn.commit()
    assert remote_allocate(stores, request)["piece_id"] == "piece"
    with pytest.raises(harvest.HarvestProjectError, match="reused") as exc:
        args = dict(changed.request_payload)
        args.pop("method")
        stores.store.propose_live_allocation(
            **args, integration_request=journal.asdict(changed)
        )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("lost_ack", [False, True])
def test_cancellation_requires_native_cancel_and_exact_readback(stores, lost_ack):
    req = cancelled(stores)
    frozen = journal.lookup_operation(req.operation_id)["request"]
    remote = stores.store.cancel_integration_allocation(frozen)
    assert remote["status"] == "undone"
    if lost_ack:
        assert (
            observe(
                stores,
                req.operation_id,
                source="AMBIGUOUS",
                evidence={"error": "lost ack"},
                key="timeout",
            )["code"]
            == "READBACK_REQUIRED"
        )
    assert stores.store.cancel_integration_allocation(frozen) == remote
    adopted = observe(stores, req.operation_id)
    assert adopted["code"] == "OK", adopted
    final = journal.complete_operation(
        req.operation_id,
        request_key="cancel-done",
        expected_operation_revision=adopted["operation_revision"],
    )
    assert final["code"] == "OK"
    assert journal.inspect_integration_recovery("m")["harvest_blockers"] == []


@pytest.mark.parametrize(
    "state", ["RESERVED", "RELEASE_INTENT", "UNCERTAIN", "COMPLETED"]
)
def test_uncertain_or_uncancelled_reservation_cannot_cancel_remote(stores, state):
    request, reserved, allocation = linked(stores)
    if state != "RESERVED":
        intent = prepare(reserved)
        if state == "UNCERTAIN":
            with connect(stores.path) as conn:
                conn.execute("UPDATE smart_bin_reservations SET state='UNCERTAIN'")
        elif state == "COMPLETED":
            assert confirm(reserved, intent)["code"] == "OK"
            assert complete(reserved, intent)["code"] == "OK"
    with connect(stores.path) as conn:
        row = conn.execute(
            "SELECT row_revision FROM smart_bin_reservations"
        ).fetchone()[0]
        rev = conn.execute("SELECT state_revision FROM smart_bin_machines").fetchone()[
            0
        ]
    req = replace(
        request,
        operation_id="op-cancel",
        request_key="cancel",
        operation_kind="CANCEL",
        allocation_id=allocation["allocation_id"],
        parent_operation_id=request.operation_id,
        expected_reservation_revision=row,
        expected_machine_revision=rev,
        request_payload=dict(method="cancel_integration_allocation", piece_id="piece"),
    )
    assert journal.begin_allocation_cancel(req)["code"] == "UNSAFE_CANCEL"
    assert (
        stores.store.lookup_integration_allocation(req.project_id, piece_id="piece")[
            "allocation"
        ]["status"]
        == "planned"
    )


def test_confirm_ambiguity_preserves_native_delivery_and_completes_once(stores):
    req, native = confirmation(stores)
    before = native_dump(stores.path)
    assert (
        observe(
            stores,
            req.operation_id,
            source="AMBIGUOUS",
            evidence={"error": "timeout"},
            key="timeout",
        )["code"]
        == "READBACK_REQUIRED"
    )
    assert journal.inspect_integration_recovery("m")["blocked_harvest_projects"] == [
        req.project_id
    ]
    op = journal.lookup_operation(req.operation_id)
    evidence = op["local_snapshot"]["confirmation_evidence"]
    stores.store.confirm_allocation(
        req.project_id, req.allocation_id, evidence=evidence
    )
    stores.store = harvest.HarvestProjectStore(stores.root)
    assert (
        stores.store.confirm_allocation(
            req.project_id, req.allocation_id, evidence=evidence
        )["status"]
        == "confirmed"
    )
    adopted = observe(stores, req.operation_id)
    assert adopted["code"] == "OK", adopted
    final = journal.complete_operation(
        req.operation_id,
        request_key="complete-confirm",
        expected_operation_revision=adopted["operation_revision"],
    )
    assert final["code"] == "OK"
    assert (
        journal.complete_operation(
            req.operation_id,
            request_key="complete-confirm",
            expected_operation_revision=adopted["operation_revision"],
        )
        == final
    )
    assert native_dump(stores.path) == before
    assert journal.inspect_integration_recovery("m")["harvest_blockers"] == []


@pytest.mark.parametrize(
    "field", ["delivery_id", "reservation_id", "payload_hash", "operation_id"]
)
def test_confirm_wrong_delivery_evidence_rejected(stores, field):
    req, _ = confirmation(stores)
    evidence = journal.lookup_operation(req.operation_id)["local_snapshot"][
        "confirmation_evidence"
    ]
    evidence["integration"][field] = "wrong"
    stores.store.confirm_allocation(
        req.project_id, req.allocation_id, evidence=evidence
    )
    before = native_dump(stores.path)
    assert observe(stores, req.operation_id)["code"] == "REMOTE_DELIVERY_MISMATCH"
    assert native_dump(stores.path) == before


def test_local_phase_failure_rolls_back_remote_commit_survives(stores):
    request, _, _ = begin(stores)
    remote_allocate(stores, request)
    before = journal.lookup_operation(request.operation_id)
    with connect(stores.path) as conn:
        conn.execute(
            "CREATE TRIGGER injected_failure BEFORE INSERT ON smart_bin_audit_events BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        observe(stores, request.operation_id)
    assert journal.lookup_operation(request.operation_id) == before
    assert (
        stores.store.lookup_integration_allocation(
            request.project_id, piece_id="piece"
        )["allocation"]["status"]
        == "planned"
    )
    with connect(stores.path) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM smart_bin_harvest_observations"
            ).fetchone()[0]
            == 0
        )
        conn.execute("DROP TRIGGER injected_failure")
    assert observe(stores, request.operation_id)["code"] == "OK"


def test_record_receipt_replay_does_not_add_observation(stores):
    request, _, _ = begin(stores)
    args = dict(
        source="AMBIGUOUS",
        evidence={"error": "timeout"},
        request_key="timeout",
        expected_operation_revision=1,
    )
    first = journal.record_remote_allocation_result(request.operation_id, **args)
    assert (
        journal.record_remote_allocation_result(request.operation_id, **args) == first
    )
    assert (
        journal.record_remote_allocation_result(
            request.operation_id, **{**args, "evidence": {"error": "other"}}
        )["code"]
        == "IDEMPOTENCY_CONFLICT"
    )
    with connect(stores.path) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM smart_bin_harvest_observations"
            ).fetchone()[0]
            == 1
        )


def test_latest_observation_required_and_terminal_phase_cannot_regress(stores):
    request, _, _ = begin(stores)
    remote_allocate(stores, request)
    evidence = stores.store.lookup_integration_allocation(
        request.project_id, piece_id="piece"
    )
    old = journal.record_remote_allocation_result(
        request.operation_id,
        source="READBACK",
        evidence=evidence,
        request_key="one",
        expected_operation_revision=1,
    )
    latest = journal.record_remote_allocation_result(
        request.operation_id,
        source="AMBIGUOUS",
        evidence={},
        request_key="two",
        expected_operation_revision=2,
    )
    assert (
        journal.advance_remote_result(
            request.operation_id,
            observation_id=old["observation_id"],
            request_key="old",
            expected_operation_revision=latest["operation_revision"],
        )["code"]
        == "LATEST_READBACK_REQUIRED"
    )
    with connect(stores.path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="skip"):
            conn.execute("UPDATE smart_bin_external_operations SET status='COMPLETE'")


def test_recovery_readonly_projects_all_phases_and_retains_predecessor_blockers(
    stores, monkeypatch
):
    req, _ = confirmation(stores)
    with connect(stores.path) as conn:
        conn.execute(
            "INSERT INTO smart_bin_discrepancies (id,machine_id,reservation_id,kind,status,details_json,created_at) VALUES('p2c5','m',?,'RECONCILIATION_ALLOCATION_BLOCKER','open','{}',1)",
            (req.reservation_id,),
        )
    monkeypatch.setattr(stores.store, "lookup_integration_allocation", fail_call)
    before = native_dump(stores.path)
    report = delivery.inspect_recovery("m")
    assert report["harvest_blockers"][0]["phase"] == "DELIVERY_CONFIRM_PENDING"
    assert any(d["id"] == "p2c5" for d in report["unresolved_discrepancies"])
    assert report[
        "followup_blockers"
    ]  # existing native closure/publication obligations remain
    assert native_dump(stores.path) == before


def test_complete_missing_local_link_fails_closed(stores):
    request, _, _ = linked(stores)
    with connect(stores.path) as conn:
        conn.execute("DROP TRIGGER smart_bin_harvest_links_delete")
        conn.execute("DELETE FROM smart_bin_harvest_links")
    report = journal.inspect_integration_recovery("m")
    # Missing extension object is itself a fail-closed schema blocker.
    assert report["harvest_blockers"]


def test_helper_requires_transaction_and_staging_rolls_back_with_caller(stores):
    req, _ = confirmation(stores, stage=False)
    with connect(stores.path) as conn:
        with pytest.raises(RuntimeError, match="transaction"):
            journal.stage_delivery_confirmation_on_connection(conn, req)
    assert journal.lookup_operation(req.operation_id) is None
    before = native_dump(stores.path)
    with journal.integration_lock, critical_transaction() as conn:
        staged = journal.stage_delivery_confirmation_on_connection(conn, req)
        assert conn.execute(
            "SELECT id FROM smart_bin_external_operations WHERE id=?",
            (req.operation_id,),
        ).fetchone()
        conn.rollback()
    assert staged["code"] == "OK"
    assert journal.lookup_operation(req.operation_id) is None
    assert native_dump(stores.path) == before


def test_remote_extension_initialization_is_explicit_and_readback_unbounded(stores):
    request, _, _ = begin(stores)
    remote_allocate(stores, request)
    assert stores.store.initialize_integration_schema() == 1
    assert journal.initialize_schema() == 1
    with stores.store._connection() as conn:
        conn.execute("UPDATE harvest_integration_versions SET version=99")
        conn.commit()
    with pytest.raises(RuntimeError, match="schema"):
        stores.store.lookup_integration_allocation(request.project_id, piece_id="piece")


@pytest.mark.parametrize(
    "field,value",
    [
        ("part_id", "wrong"),
        ("color_id", "wrong"),
        ("group_id", "wrong"),
        ("mode", "acceptance"),
    ],
)
def test_allocation_result_must_match_remote_transaction_receipt(stores, field, value):
    request, _, _ = begin(stores)
    remote_allocate(stores, request)
    evidence = stores.store.lookup_integration_allocation(
        request.project_id, piece_id="piece"
    )
    evidence["allocation"][field] = value
    assert (
        observe(stores, request.operation_id, evidence=evidence)["code"]
        == "REMOTE_ALLOCATION_MISMATCH"
    )


def test_wrong_destination_blocks_link(stores):
    request, _, _ = begin(stores)
    with stores.store._connection() as conn:
        activation = journal.json.loads(
            conn.execute(
                "SELECT activation_json FROM harvest_runtime_state"
            ).fetchone()[0]
        )
        activation["assignments"][0]["bin_index"] = 1
        conn.execute(
            "UPDATE harvest_runtime_state SET activation_json=?",
            (canonical(activation),),
        )
        conn.commit()
    remote_allocate(stores, request)
    assert (
        observe(stores, request.operation_id)["code"] == "REMOTE_DESTINATION_MISMATCH"
    )
    with connect(stores.path) as conn:
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_harvest_links").fetchone()[0]
            == 0
        )


def test_cancel_after_crash_before_link_closes_both_operations(stores):
    request, reserved, _ = begin(stores)
    allocation = remote_allocate(stores, request)
    assert observe(stores, request.operation_id)["code"] == "OK"
    result = service.cancel_reserved(
        "m",
        reserved["reservation_id"],
        expected_reservation_revision=0,
        request_key="native-cancel",
        evidence=service.NonDispatchEvidence(
            "owner", "episode", 0, 1, True, True, "synthetic:never-dispatched"
        ),
    )
    assert result["code"] == "OK"
    cancel = replace(
        request,
        operation_id="cancel",
        request_key="cancel",
        operation_kind="CANCEL",
        allocation_id=allocation["allocation_id"],
        parent_operation_id=request.operation_id,
        expected_reservation_revision=1,
        expected_machine_revision=result["state_revision"],
        request_payload=dict(method="cancel_integration_allocation", piece_id="piece"),
    )
    assert journal.begin_allocation_cancel(cancel)["code"] == "OK"
    stores.store.cancel_integration_allocation(journal.asdict(cancel))
    accepted = observe(stores, cancel.operation_id)
    assert accepted["code"] == "OK"
    assert (
        journal.complete_operation(
            cancel.operation_id,
            request_key="done",
            expected_operation_revision=accepted["operation_revision"],
        )["code"]
        == "OK"
    )
    assert journal.lookup_operation(request.operation_id)["status"] == "COMPLETE"
    assert journal.inspect_integration_recovery("m")["harvest_blockers"] == []
    with connect(stores.path) as conn:
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_harvest_links").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("target", ["native_delivery", "confirmed_observation"])
def test_complete_fails_closed_on_missing_referenced_evidence(stores, target):
    req, _ = confirmation(stores)
    evidence = journal.lookup_operation(req.operation_id)["local_snapshot"][
        "confirmation_evidence"
    ]
    stores.store.confirm_allocation(
        req.project_id, req.allocation_id, evidence=evidence
    )
    adopted = observe(stores, req.operation_id)
    assert (
        journal.complete_operation(
            req.operation_id,
            request_key="done",
            expected_operation_revision=adopted["operation_revision"],
        )["code"]
        == "OK"
    )
    with connect(stores.path) as conn:
        if target == "native_delivery":
            conn.execute("UPDATE smart_bin_reservations SET state='UNCERTAIN'")
        else:
            conn.execute(
                "UPDATE smart_bin_harvest_operations SET confirmed_observation_id='missing' WHERE operation_id=?",
                (req.operation_id,),
            )
    assert journal.inspect_integration_recovery("m")["harvest_blockers"]


def test_remote_commit_rollback_does_not_leave_allocation_without_receipt(stores):
    request, _, _ = begin(stores)
    with stores.store._connection() as conn:
        conn.execute(
            "CREATE TRIGGER fail_receipt BEFORE INSERT ON harvest_integration_requests BEGIN SELECT RAISE(ABORT,'remote rollback'); END"
        )
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="remote rollback"):
        remote_allocate(stores, request)
    with stores.store._connection() as conn:
        assert (
            conn.execute("SELECT count(*) FROM harvest_allocations").fetchone()[0] == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM harvest_integration_requests"
            ).fetchone()[0]
            == 0
        )
    assert journal.lookup_operation(request.operation_id)["status"] == "LOCAL_INTENT"


def test_remote_cancel_refuses_confirmed_allocation_even_with_valid_local_intent(
    stores,
):
    req = cancelled(stores)
    # Contradictory remote confirmation races the pre-release cancellation.
    stores.store.confirm_allocation(
        req.project_id,
        req.allocation_id,
        evidence=dict(
            physical_drop_confirmed=True,
            activation_id=req.activation_id,
            destination_bin=[0, 0, 0],
            piece_id="piece",
        ),
    )
    with pytest.raises(harvest.HarvestProjectError) as exc:
        stores.store.cancel_integration_allocation(journal.asdict(req))
    assert exc.value.code == "INTEGRATION_UNSAFE_CANCEL"
    assert observe(stores, req.operation_id)["code"] == "REMOTE_STATE_MISMATCH"


def test_no_production_imports_or_mutations_outside_inactive_inspection(stores):
    import ast
    from pathlib import Path

    root = Path(journal.__file__).parent
    for name in (
        "coordinator.py",
        "project_harvest_runtime.py",
        "subsystems/distribution/sending.py",
        "subsystems/classification_channel/physical_runtime.py",
    ):
        tree = ast.parse((root / name).read_text())
        assert not any(
            isinstance(n, ast.ImportFrom)
            and n.module == "smart_bins_harvest_integration"
            for n in ast.walk(tree)
        )
    tree = ast.parse(Path(journal.__file__).read_text())
    assert not any(
        isinstance(n, ast.Attribute)
        and n.attr
        in (
            "propose_live_allocation",
            "confirm_allocation",
            "cancel_integration_allocation",
            "retire_c4_planned_allocations",
            "motor",
            "callback",
            "request_index",
        )
        for n in ast.walk(tree)
    )


def test_ambiguous_confirmation_blocks_new_journal_admission_for_affected_project(
    stores,
):
    req, _ = confirmation(stores)
    later = claim(piece="piece-2", key="reserve-2", pocket=1)
    new_request = replace(
        req,
        operation_id="later",
        request_key="later",
        operation_kind="ALLOCATE",
        reservation_id=later["reservation_id"],
        piece_uuid="piece-2",
        allocation_id=None,
        delivery_id=None,
        parent_operation_id=None,
        expected_reservation_revision=0,
        expected_machine_revision=later["state_revision"],
        request_payload=dict(
            method="propose_live_allocation",
            piece_id="piece-2",
            part_id="3001",
            color_id="2",
            item_candidates=None,
            color_candidates=None,
            classification_attempts=None,
        ),
    )
    assert (
        journal.begin_allocation_operation(new_request)["code"]
        == "PROJECT_ADMISSION_BLOCKED"
    )
    assert journal.lookup_operation("later") is None
    # Another project is outside this operation's admission scope.
    assert (
        journal.begin_allocation_operation(
            replace(new_request, project_id="other-project")
        )["code"]
        == "OK"
    )


def test_simulation_optional_request_replays_original_ack_and_conflicts(stores):
    request, _, _ = begin(stores)
    simulation = replace(
        request,
        operation_id="simulation",
        request_key="simulation",
        activation_id=None,
        request_payload=dict(
            method="propose_allocation",
            piece_id="simulation-piece",
            part_id="3001",
            color_id="2",
            quantity=1,
            mode="simulation",
        ),
        piece_uuid="simulation-piece",
    )
    with stores.store._connection() as conn:
        conn.execute("UPDATE harvest_projects SET state='draft',activation_json=NULL")
        conn.execute("DELETE FROM harvest_runtime_state")
        conn.commit()
    kwargs = dict(simulation.request_payload)
    kwargs.pop("method")
    first = stores.store.propose_allocation(
        simulation.project_id, **kwargs, integration_request=journal.asdict(simulation)
    )
    stores.store.confirm_allocation(
        simulation.project_id, first["allocation_id"], evidence={"simulation": True}
    )
    assert (
        stores.store.propose_allocation(
            simulation.project_id,
            **kwargs,
            integration_request=journal.asdict(simulation),
        )
        == first
    )
    kwargs["color_id"] = "5"
    changed = replace(
        simulation, request_payload={"method": "propose_allocation", **kwargs}
    )
    with pytest.raises(harvest.HarvestProjectError) as error:
        stores.store.propose_allocation(
            simulation.project_id, **kwargs, integration_request=journal.asdict(changed)
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_exact_readback_does_not_use_truncated_project_view(stores, monkeypatch):
    request, _, _ = begin(stores)
    first = remote_allocate(stores, request)
    monkeypatch.setattr(harvest, "MAX_RECENT_ALLOCATIONS", 0)
    assert stores.store.get_project(request.project_id)["allocations"] == []
    snapshot = stores.store.lookup_integration_allocation(
        request.project_id, piece_id="piece"
    )
    assert snapshot["allocation"]["allocation_id"] == first["allocation_id"]
    assert observe(stores, request.operation_id, evidence=snapshot)["code"] == "OK"


def test_missing_remote_extension_is_not_automatically_initialized(stores, tmp_path):
    root = tmp_path / "uninitialized-harvest"
    other = harvest.HarvestProjectStore(root)
    with other._connection() as conn:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'harvest_integration_%'"
            ).fetchall()
            == []
        )
    with connect(stores.path) as conn:
        conn.execute("UPDATE smart_bin_harvest_versions SET version=99")
    report = journal.inspect_integration_recovery("m")
    assert report["harvest_machine_blocked"]
    assert report["harvest_blockers"][0]["kind"] == "HARVEST_INTEGRATION_SCHEMA_BLOCKER"


@pytest.mark.parametrize("bad", [[], {"project_id": "wrong"}, None])
def test_arbitrary_ack_never_advances_local_truth(stores, bad):
    request, _, _ = begin(stores)
    before = native_dump(stores.path)
    assert (
        observe(stores, request.operation_id, source="ACK", evidence={"body": bad})[
            "code"
        ]
        == "READBACK_REQUIRED"
    )
    assert native_dump(stores.path) == before
    assert journal.lookup_operation(request.operation_id)["status"] == "LOCAL_INTENT"


@pytest.mark.parametrize("phase", ["allocated", "released", "uncertain", "delivered"])
def test_legacy_retirement_and_undo_cannot_discard_tagged_quota(stores, phase):
    if phase == "delivered":
        req, _ = confirmation(stores)
    else:
        req, reserved, allocation = linked(stores)
        req = replace(req, allocation_id=allocation["allocation_id"])
        if phase in ("released", "uncertain"):
            intent = prepare(reserved)
            assert intent["code"] == "OK"
            if phase == "uncertain":
                with connect(stores.path) as conn:
                    conn.execute("UPDATE smart_bin_reservations SET state='UNCERTAIN'")
    before = native_dump(stores.path)
    assert stores.store.retire_c4_planned_allocations() == 0
    assert stores.store.retire_c4_planned_allocations(piece_ids=["piece"]) == 0
    with pytest.raises(harvest.HarvestProjectError) as error:
        stores.store.undo_allocation(
            req.project_id, req.allocation_id, reason="unsafe legacy undo"
        )
    assert error.value.code == "INTEGRATION_RECONCILIATION_REQUIRED"
    assert (
        stores.store.lookup_integration_allocation(req.project_id, piece_id="piece")[
            "allocation"
        ]["status"]
        == "planned"
    )
    assert native_dump(stores.path) == before


def test_legacy_retirement_still_handles_untagged_allocations(stores):
    allocation = stores.store.propose_live_allocation(
        piece_id="legacy-piece", part_id="3001", color_id="2"
    )
    assert stores.store.retire_c4_planned_allocations(piece_ids=["legacy-piece"]) == 1
    assert (
        stores.store.lookup_integration_allocation(
            stores.project["project_id"], piece_id="legacy-piece"
        )["allocation"]["status"]
        == "undone"
    )
    assert (
        stores.store.undo_allocation(
            stores.project["project_id"],
            allocation["allocation_id"],
            reason="legacy replay",
        )["status"]
        == "undone"
    )
